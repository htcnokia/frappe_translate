cd ~/frappe-bench
./env/bin/python auto_translate.py \
    --engine llm \
    --llm-url http://localhost:4000/v1 \
    --llm-key sk-none \
    --llm-model qwen2.5-72b \
    --llm-batch 40 \
    --llm-concurrency 6 \
    --regen-pot --site site1.local
