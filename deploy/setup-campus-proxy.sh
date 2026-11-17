#!/usr/bin/env bash
# 国内服务器上的“访问学校转发”（root 运行，可重复执行）：bash setup-campus-proxy.sh <首尔服务器IP> [端口，默认 18888]
#
# 只给首尔服务器用：只接受首尔 IP 的连接，只转发到 *.szu.edu.cn。转发的是 HTTPS 隧道，这台机器看不到内容。
# 这台机器不对外提供网站，不需要备案。云服务器安全组里这个端口也只对首尔 IP 放行（两层限制，避免变成公开代理）。
set -euo pipefail
FRONT="${1:?用法：bash setup-campus-proxy.sh <首尔服务器IP> [端口]}"
PORT="${2:-18888}"
export DEBIAN_FRONTEND=noninteractive
command -v tinyproxy >/dev/null || { apt-get update -qq && apt-get install -y -qq tinyproxy >/dev/null; }

cat > /etc/tinyproxy/szu-filter <<'EOF'
^(.+\.)?szu\.edu\.cn$
EOF
cat > /etc/tinyproxy/tinyproxy.conf <<EOF
# 由 szu-agent deploy/setup-campus-proxy.sh 生成
User tinyproxy
Group tinyproxy
Port $PORT
Timeout 120
MaxClients 100
LogLevel Warning
Syslog On
DisableViaHeader Yes
# 只允许首尔服务器（127.0.0.1 留给下面的自检）
Allow 127.0.0.1
Allow $FRONT
# 只转发学校的 HTTPS / HTTP
ConnectPort 443
Filter "/etc/tinyproxy/szu-filter"
FilterType ere
FilterDefaultDeny Yes
EOF
systemctl enable -q tinyproxy
systemctl restart tinyproxy
sleep 1

echo "== 自检（经本机转发）"
echo "学校（应为 200/302 和很短的连接时间）：$(curl -s -o /dev/null -x http://127.0.0.1:$PORT -w '%{http_code}  %{time_starttransfer}s' https://ehall.szu.edu.cn/ || echo 失败)"
echo "其他网站（应被拒绝，000 或 403）：     $(curl -s -o /dev/null -x http://127.0.0.1:$PORT -w '%{http_code}' https://www.baidu.com/ || true)"
echo "完成。还要做：云服务器安全组放行 TCP $PORT，来源只填 $FRONT/32"
echo "然后在首尔服务器上：CAMPUS_PROXY=http://<本机公网IP>:$PORT bash setup-server.sh <首尔IP>"
