#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Frappe/ERPNext v16 自动翻译流水线（独立脚本）
英文基准 -> LLM(可选) 或 Google 免费翻译成简体 -> OpenCC 转 zh_TW 繁体 -> 编译 .mo

用法：
    cd ~/frappe-bench
    # 纯 Google（默认）
    ./env/bin/python auto_translate.py --regen-pot --site site1.local
    # 使用本地 LLM（vLLM + liteLLM，OpenAI 兼容），失败自动回退 Google
    ./env/bin/python auto_translate.py --engine llm \
        --llm-url http://localhost:4000/v1 --llm-key sk-none \
        --llm-model qwen2.5-72b --llm-batch 40 --llm-concurrency 6 \
        --regen-pot --site site1.local
"""

import os
import re
import sys
import json
import glob
import time
import argparse
import subprocess
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed

import polib
from deep_translator import GoogleTranslator
from opencc import OpenCC


# ---------------- 配置区 ----------------
BENCH = os.path.expanduser("~/frappe-bench")
TARGET_APP = "zh_localization"                              # 汇总译文写入的自定义 app
CACHE = os.path.join(BENCH, "translation_cache.json")      # 增量缓存
BATCH = 20                                                 # Google 每批翻译条数
SLEEP = 1.2                                                # Google 每批之间限速（秒）
OPENCC_CONFIG = "s2twp"                                    # 简体->台湾繁体；港式改 s2hk
SRC_LANG, ZH, ZH_TW = "en", "zh", "zh_TW"

# 术语表：常翻错的 ERP 术语，强制覆盖（简体）
GLOSSARY = {
    "Sales Invoice": "销售发票",
    "Purchase Order": "采购订单",
    "Purchase Invoice": "采购发票",
    "Stock Entry": "库存记录",
    "Warehouse": "仓库",
    "Journal Entry": "日记账分录",
    "Cost Center": "成本中心",
    "Frappe": "Frappe",
    "Frappe Learning": "学习",
    "Frappe School": "学校",
    # 按你行业继续补充……
}

# 含以下模式的字符串跳过翻译（占位符/纯代码/纯符号），直接保留原文
SKIP_PAT = re.compile(r"^[\W\d_]*$")                        # 纯符号/数字
PLACEHOLDER = re.compile(r"(\{[^}]*\}|<[^>]+>|%\w|%\(\w+\)s)")

# LLM 提示词
SYSTEM_PROMPT = (
    "你是专业的软件本地化翻译引擎，负责把 ERP 系统(Frappe/ERPNext)界面英文术语翻译成简体中文。"
    "要求：1) 只输出翻译，不要解释；2) 保持 ERP 专业术语准确；"
    "3) 严格保留占位符(如 {0}、{{ }}、<b>、%s)原样不译；"
    "4) 按输入 JSON 数组顺序返回等长的 JSON 数组，只返回数组本身。"
)
# ---------------------------------------


# ============ 基础工具 ============
def sh(cmd):
    print("  $", cmd)
    subprocess.run(cmd, shell=True, cwd=BENCH, check=True)


def acquire_lock():
    """文件锁：防止多个翻译进程并发写坏 PO/缓存。"""
    lock_path = os.path.join(BENCH, ".auto_translate.lock")
    fp = open(lock_path, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("已有翻译进程在运行，退出。")
        sys.exit(0)
    return fp   # 持有引用直到进程结束，锁才释放


def load_cache():
    if os.path.exists(CACHE):
        return json.load(open(CACHE, encoding="utf-8"))
    return {}


def save_cache(cache):
    json.dump(cache, open(CACHE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def collect_untranslated():
    """遍历所有 app 的 zh.po，收集 msgstr 为空的 msgid（连同 msgctxt 作为 key）。
    跳过 TARGET_APP 自身（它是我们回写的目标，不是源）。"""
    missing = {}   # (msgctxt, msgid) -> None
    for po_path in glob.glob(f"{BENCH}/apps/*/*/locale/zh.po"):
        if f"/apps/{TARGET_APP}/" in po_path:
            continue
        try:
            po = polib.pofile(po_path)
        except Exception as e:
            print("跳过无法解析的 PO:", po_path, e)
            continue
        for entry in po:
            if entry.obsolete or not entry.msgid.strip():
                continue
            if not entry.msgstr.strip():
                missing[(entry.msgctxt or "", entry.msgid)] = None
    return list(missing.keys())


def protect(text):
    """把占位符替换成不被翻译破坏的哨兵，返回 (处理后文本, 映射)。"""
    tokens = {}

    def repl(m):
        key = f"\u2402{len(tokens)}\u2403"   # 用控制字符做哨兵
        tokens[key] = m.group(0)
        return key

    return PLACEHOLDER.sub(repl, text), tokens


def restore(text, tokens):
    for k, v in tokens.items():
        text = text.replace(k, v)
    return text


def seed_skips(todo, cache):
    """把纯符号/占位符类词条直接以原文写入缓存，不发给任何翻译引擎。
    返回真正需要翻译的 todo 子集。"""
    real = []
    for ctxt, msgid in todo:
        if SKIP_PAT.match(msgid):
            cache.setdefault(msgid, msgid)
        else:
            real.append((ctxt, msgid))
    return real


# ============ Google 引擎 ============
def translate_missing(keys, cache):
    """用 Google 翻译 keys 中尚未命中缓存的词条。keys 为 (ctxt, msgid) 列表。"""
    translator = GoogleTranslator(source=SRC_LANG, target="zh-CN")
    todo = [k for k in keys if k[1] not in cache]
    print(f"[Google] 待翻译 {len(todo)} 条")

    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        srcs, protes, maps = [], [], []
        for _, msgid in chunk:
            p, m = protect(msgid)
            protes.append(p)
            maps.append(m)
            srcs.append(msgid)

        # 需要翻译的（跳过纯符号）
        idxs = [j for j, s in enumerate(srcs) if not SKIP_PAT.match(s)]
        payload = [protes[j] for j in idxs]
        result = list(protes)   # 默认原样
        if payload:
            for attempt in range(3):
                try:
                    trans = translator.translate_batch(payload)
                    for pos, j in enumerate(idxs):
                        result[j] = trans[pos] or protes[j]
                    break
                except Exception as e:
                    print("  翻译重试:", e)
                    time.sleep(5 * (attempt + 1))

        for j, (ctxt, msgid) in enumerate(chunk):
            zh = restore(result[j], maps[j])
            cache[msgid] = GLOSSARY.get(msgid, zh)
        save_cache(cache)   # 每批落盘，中断安全
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)}")
        time.sleep(SLEEP)

    # 术语表始终覆盖
    for k, v in GLOSSARY.items():
        cache[k] = v
    save_cache(cache)


# ============ LLM 引擎 ============
def _make_llm_client(url, key):
    from openai import OpenAI
    return OpenAI(base_url=url, api_key=key)


def llm_healthcheck(args):
    """返回 True 表示 LLM 可用；否则打印原因并返回 False。"""
    if not args.llm_url or not args.llm_model:
        print("未提供 --llm-url/--llm-model，回退 Google")
        return False
    try:
        client = _make_llm_client(args.llm_url, args.llm_key)
        resp = client.chat.completions.create(
            model=args.llm_model,
            messages=[{"role": "user", "content": "ping，只回复 ok"}],
            temperature=0, max_tokens=8, timeout=15,
        )
        txt = (resp.choices[0].message.content or "").strip()
        print(f"LLM 健康检查通过：{args.llm_url} / {args.llm_model}（返回 {txt!r}）")
        return True
    except Exception as e:
        print(f"⚠️ LLM 不可用（{e}），自动回退 Google 翻译")
        return False


def _llm_translate_batch(client, model, texts):
    """把一批英文 texts 翻成简体，返回等长列表。彻底失败则返回原文列表。"""
    payload = json.dumps(texts, ensure_ascii=False)
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",
                     "content": f"翻译以下 JSON 数组中的每个字符串：\n{payload}"},
                ],
                temperature=0,
                max_tokens=4096,
                timeout=120,
            )
            content = (resp.choices[0].message.content or "").strip()
            # 容错：提取第一个 [ ... ] JSON 数组
            m = re.search(r"\[.*\]", content, re.DOTALL)
            arr = json.loads(m.group(0) if m else content)
            if isinstance(arr, list) and len(arr) == len(texts):
                return [str(x) for x in arr]
            raise ValueError(f"返回条数不符：{len(arr)} != {len(texts)}")
        except Exception as e:
            print(f"  LLM 批次重试({attempt + 1}): {e}")
            time.sleep(2 * (attempt + 1))
    return list(texts)  # 三次失败：保留原文，交由调用方回退 Google


def translate_with_llm(todo, cache, args):
    """用 LLM 翻译 todo，返回 LLM 未能完成的词条列表（供 Google 兜底）。"""
    client = _make_llm_client(args.llm_url, args.llm_key)
    model = args.llm_model
    batches = [todo[i:i + args.llm_batch] for i in range(0, len(todo), args.llm_batch)]
    print(f"[LLM] {len(todo)} 条，分 {len(batches)} 批，并发 {args.llm_concurrency}")

    failed = []                      # LLM 没搞定的，回退 Google
    consecutive_fail = 0             # 连续失败批次计数
    CIRCUIT_LIMIT = 5                # 连续 5 批失败即熔断，剩余全转 Google
    done = 0
    tripped = False

    def work(batch):
        srcs = [msgid for _, msgid in batch]
        prot, maps = zip(*(protect(s) for s in srcs))
        res = _llm_translate_batch(client, model, list(prot))
        out = []
        for i in range(len(batch)):
            restored = restore(res[i], maps[i])
            out.append((batch[i][1], restored, srcs[i]))
        return out

    with ThreadPoolExecutor(max_workers=args.llm_concurrency) as pool:
        future_map = {pool.submit(work, b): b for b in batches}
        finished = set()
        for fut in as_completed(future_map):
            batch = future_map[fut]
            finished.add(fut)
            try:
                results = fut.result()
                batch_failed = False
                for msgid, zh, src in results:
                    # 译文与原文完全相同且原文含字母 → 视为未翻译（LLM 回退了原文）
                    if zh == src and re.search(r"[A-Za-z]", src):
                        failed.append((None, msgid))
                        batch_failed = True
                    else:
                        cache[msgid] = GLOSSARY.get(msgid, zh)
                consecutive_fail = consecutive_fail + 1 if batch_failed else 0
            except Exception as e:
                print(f"  批次异常，转 Google：{e}")
                failed.extend([(None, m) for _, m in batch])
                consecutive_fail += 1
            done += len(batch)
            save_cache(cache)
            print(f"  {done}/{len(todo)}（待回退 {len(failed)}）")

            if consecutive_fail >= CIRCUIT_LIMIT and not tripped:
                print(f"⚠️ 连续 {CIRCUIT_LIMIT} 批失败，LLM 熔断，剩余全部转 Google")
                tripped = True
                # 取消尚未开始的任务
                for f in future_map:
                    if f not in finished:
                        f.cancel()
                # 已提交但未完成/被取消的批次，其词条归入 failed
                for f, b in future_map.items():
                    if f not in finished:
                        failed.extend([(None, m) for _, m in b])
                break

    for k, v in GLOSSARY.items():
        cache[k] = v
    save_cache(cache)

    # 去重后返回真正还需 Google 兜底的（已进缓存的排除）
    seen, uniq = set(), []
    for ctxt, msgid in failed:
        if msgid not in cache and msgid not in seen:
            seen.add(msgid)
            uniq.append((ctxt, msgid))
    return uniq


# ============ 回写 PO ============
def write_po(keys, cache):
    loc = f"{BENCH}/apps/{TARGET_APP}/{TARGET_APP}/locale"
    os.makedirs(loc, exist_ok=True)
    cc = OpenCC(OPENCC_CONFIG)

    zh_po = polib.POFile()
    zh_po.metadata = {"Content-Type": "text/plain; charset=utf-8", "Language": ZH}
    tw_po = polib.POFile()
    tw_po.metadata = {"Content-Type": "text/plain; charset=utf-8", "Language": ZH_TW}

    seen = set()
    for ctxt, msgid in keys:
        if msgid in seen:
            continue
        seen.add(msgid)
        zh_text = cache.get(msgid)
        if not zh_text:
            continue
        kw = {"msgid": msgid}
        if ctxt:
            kw["msgctxt"] = ctxt
        zh_po.append(polib.POEntry(msgstr=zh_text, **kw))
        tw_po.append(polib.POEntry(msgstr=cc.convert(zh_text), **kw))

    zh_po.save(f"{loc}/{ZH}.po")
    tw_po.save(f"{loc}/{ZH_TW}.po")
    print(f"已写入 {loc}/{ZH}.po 与 {loc}/{ZH_TW}.po（共 {len(seen)} 条）")


# ============ 主流程 ============
def parse_args():
    ap = argparse.ArgumentParser(description="Frappe/ERPNext 自动翻译流水线")
    ap.add_argument("--site", default=None, help="站点名，用于 clear-cache")
    ap.add_argument("--regen-pot", action="store_true",
                    help="先重抽 POT 并同步各 app 的 zh.po")
    # 翻译引擎
    ap.add_argument("--engine", choices=["google", "llm"], default="google",
                    help="翻译引擎：google=deep-translator，llm=本地/远程大模型")
    ap.add_argument("--llm-url", default=None,
                    help="LLM base_url，如 http://localhost:4000/v1")
    ap.add_argument("--llm-key", default="sk-none",
                    help="LLM api key，liteLLM 未鉴权时随便填")
    ap.add_argument("--llm-model", default=None,
                    help="模型名，如 qwen2.5-72b-instruct")
    ap.add_argument("--llm-batch", type=int, default=40,
                    help="LLM 单次请求翻译条数")
    ap.add_argument("--llm-concurrency", type=int, default=4,
                    help="LLM 并发请求数")
    return ap.parse_args()


def main():
    _lock = acquire_lock()          # 全程持有，进程退出自动释放
    args = parse_args()

    if args.regen_pot:
        for app_dir in glob.glob(f"{BENCH}/apps/*/*/locale"):
            app = app_dir.split("/apps/")[1].split("/")[0]
            if app == TARGET_APP:   # 跳过目标 app，避免清空聚合译文
                continue
            try:
                sh(f"bench generate-pot-file --app {app}")
                sh(f"bench update-po-files --app {app} --locale {ZH}")
            except subprocess.CalledProcessError:
                print("  跳过", app)

    cache = load_cache()
    keys = collect_untranslated()
    todo = [k for k in keys if k[1] not in cache]
    todo = seed_skips(todo, cache)   # 纯符号直接入缓存，不发引擎

    if not todo:
        print("无新增待翻译词条")
    elif args.engine == "llm" and llm_healthcheck(args):
        leftover = translate_with_llm(todo, cache, args)
        if leftover:
            print(f"LLM 未完成 {len(leftover)} 条，改用 Google 兜底")
            translate_missing(leftover, cache)
    else:
        # 未选 llm，或健康检查失败 → 全量 Google
        translate_missing(todo, cache)

    write_po(keys, cache)

    # 编译自定义 app 的语言包
    sh(f"bench compile-po-to-mo --app {TARGET_APP} --locale {ZH} --force")
    sh(f"bench compile-po-to-mo --app {TARGET_APP} --locale {ZH_TW} --force")
    if args.site:
        sh(f"bench --site {args.site} clear-cache")
    print("完成。刷新浏览器即可看到翻译。")


if __name__ == "__main__":
    main()
