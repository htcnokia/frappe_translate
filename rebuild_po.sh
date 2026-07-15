cd ~/frappe-bench
./env/bin/python rebuild_po.py
echo " 正在刷新系统语言"
bench compile-po-to-mo --app zh_localization --locale zh --force
bench compile-po-to-mo --app zh_localization --locale zh_TW --force
echo " 正在重建缓存"
bench --site site1.local clear-cache
echo  " 完成，请强制刷新浏览器查看效果"
