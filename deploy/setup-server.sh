#!/usr/bin/env bash
# 爱小荔一键部署（Ubuntu 22.04 / 24.04，root 运行，可重复执行）
#
#   本机：tar 打包项目（排除 .venv/.venv-win/.env/resource/缓存）→ scp 到服务器 /tmp/szu-agent-upload.tgz
#         .env 单独 scp 到 /tmp/szu-agent.env（只有第一次需要）
#   服务器：bash setup-server.sh <服务器公网IP> [域名]
#           域名只需第一次给（记在 /etc/nginx/ssl/szu-domain），之后更新代码照常 bash setup-server.sh <IP>
#   访问学校经国内服务器转发（先在国内服务器上运行 setup-campus-proxy.sh）：
#           CAMPUS_PROXY=http://国内IP:18888 bash setup-server.sh <IP>   （只需一次；CAMPUS_PROXY=off 关掉）
#   不要演示站：DEMO=off bash setup-server.sh <IP>   （只需一次，记在 /etc/szu-agent/no-demo；DEMO=on 恢复）
#           关掉后 http（80 端口）全部跳转到 https 的真实服务
#
# 做的事：建 szu 账号 → 装 nginx、uv → 解压代码 → uv sync → systemd 两个服务（真实服务多人模式、演示站）
#        → 服务器密钥（已有就不动！换了所有人的 user_id 都会变）→ 证书 → nginx → 启动并自检
# 证书：有域名且域名已解析到本机 → Let's Encrypt（certbot webroot，自动续期）；否则按 IP 自签名（已有就不动）。
#       有域名时：https://域名 = 真实服务，http://域名 跳转到 https；演示站（开着的话）是 http://<IP>。
set -euo pipefail
IP="${1:?用法：bash setup-server.sh <服务器公网IP> [域名]}"
DOMAIN="${2:-}"
case "$DOMAIN" in http://*|https://*) REAL_URL="$DOMAIN"; DOMAIN="";; esac   # 兼容旧用法：第二个参数是网址
[ -n "$DOMAIN" ] || DOMAIN="$(cat /etc/nginx/ssl/szu-domain 2>/dev/null || true)"
APP=/home/szu/szu-agent
mkdir -p /etc/szu-agent
case "${DEMO:-}" in off) touch /etc/szu-agent/no-demo;; on) rm -f /etc/szu-agent/no-demo;; esac
if [ -f /etc/szu-agent/no-demo ]; then WITH_DEMO=""; else WITH_DEMO=1; fi
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
# 自签名证书（没有域名、或域名证书还没申请下来时用）
mkdir -p /etc/nginx/ssl && chmod 700 /etc/nginx/ssl
if [ ! -f /etc/nginx/ssl/szu-selfsigned.crt ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=$IP" -addext "subjectAltName=IP:$IP" \
    -keyout /etc/nginx/ssl/szu-selfsigned.key -out /etc/nginx/ssl/szu-selfsigned.crt 2>/dev/null
  chmod 600 /etc/nginx/ssl/szu-selfsigned.key
fi

# nginx：szu-real.conf 通过 szu-cert.inc 引用证书，这里按有没有正式证书生成
LE=/etc/letsencrypt/live/$DOMAIN
write_cert_inc() {
  if [ -n "$DOMAIN" ] && [ -f "$LE/fullchain.pem" ]; then
    printf 'ssl_certificate     %s/fullchain.pem;\nssl_certificate_key %s/privkey.pem;\n' "$LE" "$LE"
    # 只走 https：防止有人在 http 页面输密码（2026-11-16 的事故）
    echo 'add_header Strict-Transport-Security "max-age=31536000" always;'
  else
    printf 'ssl_certificate     /etc/nginx/ssl/szu-selfsigned.crt;\nssl_certificate_key /etc/nginx/ssl/szu-selfsigned.key;\n'
  fi > /etc/nginx/conf.d/szu-cert.inc
}
rm -f /etc/nginx/sites-enabled/default
install -m 644 "$APP/deploy/nginx/szu-real.conf" "$APP/deploy/nginx/szu-proxy.inc" /etc/nginx/conf.d/
rm -f /etc/nginx/conf.d/szu-domain.conf /etc/nginx/conf.d/szu-http.conf /etc/nginx/conf.d/szu-demo.conf
if [ -n "$WITH_DEMO" ]; then
  install -m 644 "$APP/deploy/nginx/szu-demo.conf" /etc/nginx/conf.d/
else
  # 没有演示站：http 全部跳到 https 的真实服务（证书验证路径除外）
  mkdir -p /var/www/certbot
  cat > /etc/nginx/conf.d/szu-http.conf <<NGINX
server {
    listen 80 default_server;
    server_name _;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://${DOMAIN:-$IP}\$request_uri; }
}
NGINX
fi
if [ -n "$DOMAIN" ]; then
  mkdir -p /var/www/certbot
  # http://域名：只留证书验证用的路径，其余跳转到 https（演示站只在 http://<IP>）
  cat > /etc/nginx/conf.d/szu-domain.conf <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
NGINX
fi
write_cert_inc
nginx -t -q
systemctl enable -q nginx
systemctl reload nginx 2>/dev/null || systemctl restart nginx

if [ -n "$DOMAIN" ] && [ ! -f "$LE/fullchain.pem" ]; then
  RESOLVED="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
  if [ "$RESOLVED" != "$IP" ]; then
    echo "!! $DOMAIN 解析到 '${RESOLVED:-无}'，不是 $IP：先去域名控制台加 A 记录，生效后再运行 bash setup-server.sh $IP $DOMAIN。暂用自签名证书"
  else
    command -v certbot >/dev/null || apt-get install -y -qq certbot >/dev/null
    # 不留邮箱（Let's Encrypt 已不发到期提醒；续期由 certbot.timer 自动做）。要留就设 CERTBOT_EMAIL
    if [ -n "${CERTBOT_EMAIL:-}" ]; then MAIL=(--email "$CERTBOT_EMAIL"); else MAIL=(--register-unsafely-without-email); fi
    if certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" --non-interactive --agree-tos "${MAIL[@]}" \
         --deploy-hook "systemctl reload nginx"; then
      echo "$DOMAIN" > /etc/nginx/ssl/szu-domain
      write_cert_inc && nginx -t -q && systemctl reload nginx
    else
      echo "!! 证书申请失败（看上面 certbot 的输出；连续失败有频率限制，查清原因再重试）。暂用自签名证书"
    fi
  fi
fi
if [ -n "$DOMAIN" ] && [ -f "$LE/fullchain.pem" ]; then HAVE_CERT=1; REAL_URL="${REAL_URL:-https://$DOMAIN}"
else HAVE_CERT=""; REAL_URL="${REAL_URL:-https://$IP}"; fi

# 访问学校经国内服务器转发（只给真实服务；演示站不访问学校）。设置一次后保留，CAMPUS_PROXY=off 删除
if [ "${CAMPUS_PROXY:-}" = off ]; then
  rm -f "$D/campus-proxy.conf"
elif [ -n "${CAMPUS_PROXY:-}" ]; then
  printf '[Service]\nEnvironment=SZU_AGENT_CAMPUS_PROXY=%s\n' "$CAMPUS_PROXY" > "$D/campus-proxy.conf"
fi

# 演示站上显示“去真实服务”的链接
mkdir -p /etc/systemd/system/szu-agent-demo.service.d
printf '[Service]\nEnvironment=SZU_AGENT_REAL_URL=%s\n' "$REAL_URL" > /etc/systemd/system/szu-agent-demo.service.d/real-url.conf

systemctl daemon-reload
if [ -n "$WITH_DEMO" ]; then
  systemctl enable -q szu-agent szu-agent-demo
  systemctl restart szu-agent szu-agent-demo
else
  systemctl disable --now -q szu-agent-demo 2>/dev/null || true
  systemctl enable -q szu-agent
  systemctl restart szu-agent
fi
sleep 8

echo "== 自检"
systemctl is-active szu-agent nginx | paste -sd' '
echo "真实服务（应为 multiuser:true）：$(curl -sk https://127.0.0.1/api/meta)"
if [ -n "$WITH_DEMO" ]; then
  echo "演示站（应为 mock:true）：      $(curl -s http://127.0.0.1/api/meta)"
else
  echo "演示站已关闭；http 跳转到：     $(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://127.0.0.1/)"
fi
if [ -f "$D/campus-proxy.conf" ]; then
  P="$(sed -n 's/^Environment=SZU_AGENT_CAMPUS_PROXY=//p' "$D/campus-proxy.conf")"
  echo "学校 直连：        $(curl -s -o /dev/null -w '首字节 %{time_starttransfer}s' https://ehall.szu.edu.cn/ || echo 失败)"
  echo "学校 经 $P：$(curl -s -o /dev/null -x "$P" -w '首字节 %{time_starttransfer}s' https://ehall.szu.edu.cn/ || echo 失败（会自动直连，检查转发服务器和安全组）)"
fi
if [ -n "$HAVE_CERT" ]; then
  echo "正式证书（不加 -k 也应成功）：  $(curl -s --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/api/meta" || echo 失败)"
  echo "证书到期：$(openssl x509 -enddate -noout -in "$LE/fullchain.pem")（certbot.timer 自动续期：$(systemctl is-active certbot.timer)）"
  echo "完成。真实服务 https://$DOMAIN${WITH_DEMO:+ ，演示站 http://$IP}"
else
  echo "完成。真实服务 https://$IP （自签名证书，第一次要点“继续访问”）${WITH_DEMO:+，演示站 http://$IP}"
fi
