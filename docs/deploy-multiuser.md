# 部署到服务器给多人用

单人模式（默认）什么都不用改。下面是 `SZU_AGENT_MULTIUSER=1` 时的做法和注意事项。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `SZU_AGENT_MULTIUSER=1` | 是 | 开启多人模式：先用学校统一认证登录爱小荔，每人的数据分开 |
| `SZU_AGENT_SECRET` | 强烈建议 | 算 user_id 的服务器密钥（任意长随机串，如 `python -c "import secrets;print(secrets.token_hex(32))"`）。不设会在数据目录生成 `secret.key`。**部署后不要换、要备份**：换了所有人的 user_id 都会变，等于数据全部丢失 |
| `SZU_AGENT_COOKIE_SECURE=1` | HTTPS 时 | 会话 Cookie 加 Secure 标记（反向代理后面应用看到的是 http，要显式打开） |
| `SZU_AGENT_ADMINS` | 否 | 能看 `/usage` 的 user_id，逗号分隔。自己登录后打开 `/api/me` 看到的 `userId` 就是 |
| `SZU_AGENT_HOME` | 否 | 数据目录，默认 `~/.szu-agent` |
| `LLM_API_KEY` 等 | 是 | 同单人模式（`.env`） |

## 部署到服务器（Ubuntu 22.04 / 24.04）

用一键脚本 `deploy/setup-server.sh`（可重复执行：已有的服务器密钥和证书不会被覆盖）。部署出来的结构：

| 服务 | 端口 | 说明 |
|---|---|---|
| `szu-agent`（真实服务） | 127.0.0.1:8000 ← Nginx **HTTPS 443** | 多人模式；开关、服务器密钥在 `/etc/systemd/system/szu-agent.service.d/multiuser.conf`（600） |
| `szu-agent-demo`（演示站） | 127.0.0.1:8001 ← Nginx **HTTP 80** | 演示数据、单人模式，给访客看 |

两个服务共用 `/home/szu/szu-agent/.env`，所以**多人模式开关只能放在 systemd drop-in 里，不能写进 .env**。

**第一次部署**（本机 PowerShell，在项目目录里）：

```powershell
tar --exclude=.venv --exclude=.venv-win --exclude=.env --exclude=resource --exclude=__pycache__ --exclude=.pytest_cache -czf ..\szu-agent.tgz .
scp ..\szu-agent.tgz root@服务器:/tmp/szu-agent-upload.tgz
scp .env root@服务器:/tmp/szu-agent.env
scp deploy\setup-server.sh root@服务器:/tmp/
ssh root@服务器 "bash /tmp/setup-server.sh 服务器公网IP"
```

最后会打印自检结果。云服务器的安全组要放行 80、443 端口。

**更新代码**：同样打包上传到 `/tmp/szu-agent-upload.tgz`，再运行 `bash /home/szu/szu-agent/deploy/setup-server.sh 服务器公网IP`（不用再传 .env）。

**要备份到服务器以外的地方**：`multiuser.conf`（服务器密钥，丢了或换了所有人的数据都对不上）和 `.env`（模型 key）。

**没有域名时**是按 IP 签发的自签名证书，浏览器第一次会提示不受信任。有域名后：`apt install certbot python3-certbot-nginx`，
把 `szu-real.conf` 的 `server_name` 改成域名，`certbot --nginx -d 域名` 换成正式证书。境内服务器用域名要先 ICP 备案。

## 必须 HTTPS

账号密码登录时，密码会经过我们的服务器转发给学校。没有 HTTPS 时密码在网络上是明文。
用 Caddy / Nginx 做反向代理并配证书，应用本身只监听 `127.0.0.1`：

```
SZU_AGENT_HOST=127.0.0.1 SZU_AGENT_MULTIUSER=1 SZU_AGENT_COOKIE_SECURE=1 uv run szu-agent-web --port 8000
```

## 服务器在校外时哪些功能能用

学校系统是**服务器**去访问的，同学的浏览器在不在校园网都没关系。多人模式默认按“服务器在校外”处理（`SZU_AGENT_NETWORK=offcampus`；服务器其实在校园网里就设成 `campus`）。2026-11-16 在校外实测：

| 系统 | 校外访问方式 |
|---|---|
| 统一认证、体育预约（ehall）、图书馆、公文通（www1） | 公网，直连 |
| 讲座（lecture） | 直连提示“仅限内网”。经 WebVPN 理论上可行，但暂不做：服务器版提示用本机版 |
| 事务中心（静音舱、琴房等） | 本来就在 WebVPN 上 |
| 电费（SIMS，内网 IP） | WebVPN 也打不开，服务器版用不了，提示在校园网下用本机版 |

不建议用 SecureLink 零信任让服务器接入内网：那是用一个人的身份给所有人开通内网（相当于转借账号），
还会触发风控；它适合同学在自己电脑上配合本机版使用。

## 数据放在哪

```
~/.szu-agent/                  目录权限 700
  secret.key                   服务器密钥（没设 SZU_AGENT_SECRET 时）
  accounts.sqlite              用户和应用会话（只有 user_id 和令牌哈希，没有学号）
  history.sqlite               所有人的对话（按 user_id 隔离，checkpointer 的键带用户前缀）
  usage.sqlite                 模型用量（只有数字）
  users/<user_id>/             每个人一个目录，权限 700
    session.json               学校登录状态（Cookie，不是密码）
    profile.json               记忆
    contact.json               预借联系手机
```

备份时整个目录一起备份（尤其是 `secret.key`）。

## 隐私说明要告诉用户

- 服务器会保存你的**学校登录状态**（不是密码），用来替你查询和预约；退出登录即删除
- 对话历史保存 30 天，只有你自己能看到；管理员的用量统计里只有数字，看不到对话内容和标题
- 记忆页面可以“全部删除”：对话、记忆、联系手机、学校登录状态、用量记录一次清空

## 还没做（上线前要补）

- 按用户限流和每日用量上限（防止一个人刷接口把额度用光）
- 学校登录状态加密存储（现在靠文件权限保护）
- 进程重启后，进行中的预约确认令牌会失效（令牌只存在内存里）
- 用户多了以后把 SQLite 换成 Postgres（checkpointer 有现成的 Postgres 版本）
