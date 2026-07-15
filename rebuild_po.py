#!/usr/bin/env python3
# rebuild_po.py —— 从修正后的缓存重建 zh.po / zh_TW.po
import os, json, polib
from opencc import OpenCC

BENCH = os.path.expanduser("~/frappe-bench")
APP = "zh_localization"
LOC = f"{BENCH}/apps/{APP}/{APP}/locale"
CACHE = f"{BENCH}/translation_cache.json"
TW_OVERRIDE = f"{BENCH}/zh_tw_override.json"   # 可选：繁体独立用词覆盖表

cache = json.load(open(CACHE, encoding="utf-8"))
overrides = json.load(open(TW_OVERRIDE, encoding="utf-8")) if os.path.exists(TW_OVERRIDE) else {}
cc = OpenCC("s2twp")

zh = polib.POFile();  zh.metadata = {"Content-Type": "text/plain; charset=utf-8", "Language": "zh"}
tw = polib.POFile();  tw.metadata = {"Content-Type": "text/plain; charset=utf-8", "Language": "zh_TW"}

for msgid, zh_text in cache.items():
    if not zh_text:
        continue
    zh.append(polib.POEntry(msgid=msgid, msgstr=zh_text))
    tw_text = overrides.get(msgid) or cc.convert(zh_text)   # 覆盖优先，否则 OpenCC 转
    tw.append(polib.POEntry(msgid=msgid, msgstr=tw_text))

zh.save(f"{LOC}/zh.po")
tw.save(f"{LOC}/zh_TW.po")
print(f"重建完成：{len(zh)} 条 zh，{len(tw)} 条 zh_TW")
