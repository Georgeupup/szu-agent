#!/usr/bin/env bash
# 爱小荔一键部署（Ubuntu 22.04 / 24.04，root 运行，可重复执行）
#
#   本机：tar 打包项目（排除 .venv/.venv-win/.env/resource/缓存）→ scp 到服务器 /tmp/szu-agent-upload.tgz
#         .env 单独 scp 到 /tmp/szu-agent.env（只有第一次需要）
#   服务器：bash /home/szu/szu-agent/deploy/setup-server.sh <服务器公网IP>   （第一次在解压目录里运行）
#
# 做的事：建 szu 账号 → 装 nginx、uv → 解压代码 → uv sync → systemd 两个服务（真实服务多人模式、演示站）
#        → 服务器密钥（已有就不动！换了所有人的 user_id 都会变）→ 自签名证书（已有就不动）→ nginx → 启动并自检
set -euo pipefail
IP="${1:?用法：bash setup-server.sh <服务器公网IP>}"
APP=/home/szu/szu-agent
UV=/home/szu/.local/bin/uv

id szu >/dev/null 2>&1 || useradd -m -s /bin/bash szu
chmod 750 /home/szu
export DEBIAN_FRONTEND=noninteractive
command -v nginx >/dev/null || { apt-get update -qq && apt-get install -y -qq nginx >/dev/null; }
[ -x "$UV" ] || sudo -u szu -H bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null

# 代码
if [ -f /tmp/szu-agent-upload.tgz ]; then
  sudo -u szu mkdir -p "$APP"
  sudo -u szu tar -xzf /tmp/szu-agent-upload.tgz -C "$APP"
  rm /tmp/szu-agent-upload.tgz
fi
if [ -f /tmp/szu-agent.env ]; then
  install -o szu -g szu -m 600 /tmp/szu-agent.env "$APP/.env" && rm /tmp/szu-agent.env
fi
[ -f "$APP/.env" ] || { echo "缺少 $APP/.env（参考 deploy/env.example）"; exit 1; }
chmod 600 "$APP/.env"
(cd "$APP" && sudo -u szu -H "$UV" sync --frozen 2>&1 | tail -1)

# systemd
install -m 644 "$APP/deploy/szu-agent.service" "$APP/deploy/szu-agent-demo.service" /etc/systemd/system/
D=/etc/systemd/system/szu-agent.service.d
mkdir -p "$D"
if [ ! -f "$D/multiuser.conf" ]; then
  umask 077
  { echo "# 多人模式（只给真实服务）。含服务器密钥：权限 600，要备份到服务器以外的地方，不要更换"
    echo "[Service]"
    echo "Environment=SZU_AGENT_MULTIUSER=1"
    echo "Environment=SZU_AGENT_COOKIE_SECURE=1"
    echo "Environment=SZU_AGENT_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  } > "$D/multiuser.conf"
  umask 022
fi
chmod 600 "$D/multiuser.conf"

# 自签名证书（有域名后用 certbot 换掉）
mkdir -p /etc/nginx/ssl && chmod 700 /etc/nginx/ssl
if [ ! -f /etc/nginx/ssl/szu-selfsigned.crt ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=$IP" -addext "subjectAltName=IP:$IP" \
    -keyout /etc/nginx/ssl/szu-selfsigned.key -out /etc/nginx/ssl/szu-selfsigned.crt 2>/dev/null
  chmod 600 /etc/nginx/ssl/szu-selfsigned.key
fi

# nginx
rm -f /etc/nginx/sites-enabled/default
install -m 644 "$APP/deploy/nginx/szu-demo.conf" "$APP/deploy/nginx/szu-real.conf" "$APP/deploy/nginx/szu-proxy.inc" /etc/nginx/conf.d/
nginx -t -q

systemctl daemon-reload
systemctl enable -q szu-agent szu-agent-demo nginx
systemctl restart szu-agent szu-agent-demo
systemctl reload nginx || systemctl restart nginx
sleep 8

echo "== 自检"
systemctl is-active szu-agent szu-agent-demo nginx | paste -sd' '
echo "真实服务（应为 multiuser:true）：$(curl -sk https://127.0.0.1/api/meta)"
echo "演示站（应为 mock:true）：      $(curl -s http://127.0.0.1/api/meta)"
echo "完成。真实服务 https://$IP （自签名证书，第一次要点“继续访问”），演示站 http://$IP"
