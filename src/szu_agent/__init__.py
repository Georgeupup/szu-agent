"""深大校园事务 Agent。

分层：
    chat.py        终端交互入口
    agent/         LangGraph 状态图（LLM 决策 + 人工确认 + 记忆）
    web/           Web 界面（FastAPI + SSE），含登录卡片
    mcp_server.py  MCP Server：带类型的工具、写操作的二次确认令牌
    campus/        访问学校系统：统一认证登录（扫码/密码）、会话持久化、各子系统适配器
"""

__version__ = "0.1.0"
