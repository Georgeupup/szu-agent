proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_buffering off;          # 流式回复（SSE）需要关掉缓冲
proxy_read_timeout 300s;
# 压缩脚本/样式/JSON（app.js 65KB → 约 15KB）。不压缩 text/event-stream：压缩会攒数据，流式回复会卡住
gzip on;
gzip_proxied any;
gzip_min_length 1024;
gzip_types text/css application/javascript text/javascript application/json image/svg+xml;
