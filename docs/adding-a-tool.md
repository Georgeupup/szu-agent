# 如何新增一个工具

以 `electricity_purchases`（查询电费充值记录）为例，这是项目里真实加过的一个工具，所有代码都可以对照着看。

## 先想清楚：数据从哪来

动手前先回答三个问题，它们决定了代码写在哪里：

| 问题 | 例子 |
|---|---|
| 学校的哪个系统、哪个接口？请求参数是什么？ | 电控系统 `POST /cgcSims/selectList.do`，`type=1` 是充值记录 |
| 需要登录吗？需要校园网吗？ | 不需要登录，但只能在校园网访问 |
| 是只读还是会改变状态（预约、取消、报名）？ | 只读 |

接口参数的来源（按可靠程度排序）：
1. 别人开源的项目里已经写明的 HTTP 请求（例如 szu-dianchacha、szu_gym_helper）
2. 自己在浏览器里按 F12 → Network 抓包
3. 推测（不推荐，失败概率高，代码里要标注 UNVERIFIED）

## 七个步骤

```
campus/                     ← 访问学校系统（和 AI 无关，可以单独测试）
  electricity_config.py     ① 静态配置（如楼栋编号）
  parsers.py                ② 解析器：HTML/JSON → Python 字典（纯函数）
  service.py                ③ 真实实现：发请求、调用解析器
  mock.py                   ④ 演示实现：返回样例数据
mcp_server.py               ⑤ 注册成工具：参数说明、输出裁剪（AI 只看得到这一层）
agent/prompts.py            ⑥ 告诉 AI 什么时候用（可选）
web/static/app.js           ⑥ 界面上工具胶囊的中文名（可选）
tests/                      ⑦ 测试
```

### ① 配置：`campus/electricity_config.py`
不会变化的数据（楼栋编号、项目代码）单独放，不要散落在逻辑里。注明来源。

### ② 解析器：`campus/parsers.py`
输入网页文本，输出字典。**不发请求**，这样可以用保存下来的样例页面离线测试。

```python
def parse_sims_page(html: str, record_type: int) -> dict:
    ...
    # cells[2] 是购电人姓名，属于个人信息，不返回
    rows.append({"purchaseType": cells[3], "kwh": _num(cells[4]), "amountCny": _num(cells[5]), ...})
```

两条规则：
- **个人信息在这一层就丢掉**（姓名、学号、手机号），后面谁都拿不到
- **认不出就报错**，抛 `CampusError("PAGE_CHANGED", ...)`，不要返回半截数据

### ③ 真实实现：`campus/service.py` 的 `RealCampus`
```python
async def electricity_purchases(self, building, room, campus=None, from_date=None, to_date=None) -> dict:
    data = await self._electricity(building, room, campus, from_date, to_date, record_type=1, days=90)
    return {**data, "totalCny": round(sum(r["amountCny"] or 0 for r in data["records"]), 2), ...}
```
- 用 `self.http.request(...)` 发请求。它已经处理了：登录状态（会自动单点登录）、二次验证识别、校园网识别、编码、缓存、日志
- 不需要登录的系统传 `require_login=False`
- 出错一律 `raise CampusError("错误码", "给人看的说明")`；新错误码的处理建议加到 `campus/errors.py` 的 `AGENT_HINTS`

### ④ 演示实现：`campus/mock.py` 的 `MockCampus`
**方法名和参数必须和 `RealCampus` 一模一样**，返回结构也要一样。否则演示模式和测试会缺这个功能。

### ⑤ 注册工具：`mcp_server.py`
```python
@mcp.tool(annotations=READ_ONLY)          # 会改变状态的用 WRITE，并且必须拆成 preview/confirm 两个工具
async def electricity_purchases(
    building: Annotated[str, Field(description="宿舍楼栋名，如“红豆斋”，支持唯一的部分匹配")],
    room: Annotated[str, Field(description="房间号，如“838”")],
    ...
) -> dict[str, Any]:
    """查询宿舍的电费充值记录……用于“这个月充了多少电费”“上次什么时候充的”。需要校园网。"""
    res = await call(svc.electricity_purchases(building, room, campus, from_date, to_date))
    if res["ok"]:
        res["data"] = {... 只保留回答需要的字段，最多 20 条 ...}
    return res
```
这是 **AI 唯一能看到的部分**：
- **函数名**：动词清晰，和已有工具风格一致（`领域_动作`）
- **docstring**：写清楚“什么时候该用我”，最好带用户的原话例子。AI 选工具主要靠这个
- **参数 description**：写格式和例子（`YYYY-MM-DD`、“如 838”）
- **用 `call(...)` 包一层**：把异常转成 `{"ok": false, "error": ...}`，不会打断对话
- **裁剪输出**：工具返回值会进入模型上下文，越短越省钱、越不容易跑偏

写完不需要改 Agent 代码：图会自动从 MCP Server 加载所有工具。

### ⑥ 可选：提示词和界面
- `agent/prompts.py`：如果 AI 容易选错工具，在“能力与工作方式”里加一句什么时候用
- `web/static/app.js` 的 `TOOL_LABELS`：工具调用胶囊上显示的中文名（不加就显示英文函数名）

### ⑦ 测试
最少写两种：

**解析器测试**（`tests/test_campus.py`）：用保存的样例页面。真实页面里有个人信息的话，手写一个最小样例，不要把真实页面放进仓库。

**请求测试**：用 `httpx.MockTransport` 模拟学校服务器，验证你发出去的请求对不对：
```python
def handler(req):
    if req.url.path == "/cgcSims/login.do":
        return httpx.Response(200, content='<input name="roomId" value="R-9">'.encode("gb18030"))
    ...
svc = RealCampus(CampusHttp(SessionStore(tmp_path / "s.json"), transport=httpx.MockTransport(handler)))
data = await svc.electricity_purchases("红豆斋", "838")
assert "张三" not in json.dumps(data, ensure_ascii=False)   # 购电人姓名没有泄露
```

运行：`uv run pytest`

## 验证

1. `uv run pytest`：全部通过
2. `uv run szu-agent-web --mock`：用自然语言问一句，看 AI 有没有选对工具
3. 真实模式下试一次；出问题看 `~/.szu-agent/debug.log`（只记地址和状态码，不含隐私）

## 界面工具（让结果变成卡片、图表）
如果想让某类结果在界面上“画”出来（比如通知面板），参考 `agent/ui_tools.py` 的 `show_notice_board`：
1. 用 Pydantic 模型定义数据结构（字段描述就是给 AI 的填写说明，校验失败 AI 会收到错误并重试）
2. 写一个 `@tool` 函数，返回一句“已展示，不要重复内容”
3. 加到 `UI_TOOLS` 列表
4. 在 `web/static/app.js` 的 `runTurn` 里，`tool_end` 成功时用 `pendingBoards` 里保存的参数渲染；终端版在 `chat.py` 加对应的渲染
5. 在 `prompts.py` 里说明什么时候调用

界面工具不访问学校系统，所以放在 agent 本地，而不是 MCP Server。

## 写操作额外的要求
预约、取消、报名这类**会改变状态**的功能，必须照 `sports_reserve_preview` / `sports_reserve_confirm` 的写法：
- 拆成 `*_preview`（只预览、签发一次性令牌）和 `*_confirm`（只接受令牌）两个工具
- `*_confirm` 用 `WRITE` 注解
- 把 `*_confirm` 加进 `agent/graph.py` 的 `SENSITIVE_TOOLS`，图会在执行前强制暂停等用户确认
