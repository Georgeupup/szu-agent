proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_buffering off;          # 流式回复（SSE）需要关掉缓冲
proxy_read_timeout 300s;
