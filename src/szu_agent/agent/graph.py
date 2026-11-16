"""LangGraph 状态图。

                           ┌───────────────────────────────┐
                           ▼                               │
    START ─▶ prepare ─▶ agent ──(无工具调用)──▶ END          │
                │                               │
                ├──(只读工具)──────────▶ tools ──┤
                │                               │
                └──(写操作)─▶ human_review ─────┤
                                 │ 同意 ─▶ tools
                                 └ 拒绝 ─▶ 回到 agent（带“用户已拒绝”的 ToolMessage）

为什么不用 create_react_agent 一行搞定？因为预约需要“确定性”的人工确认：
是否需要确认由图的路由决定，而不是靠 prompt 让 LLM 自觉。即使 LLM 被诱导直接调用
sports_reserve_confirm，也一定会停在 human_review 节点等用户点头。

prepare 每轮只在开始时跑一次：给新消息记下发送时间，历史太长时压缩成摘要（见 context.py）。
放在单独节点而不是 agent 里，是因为 agent 在一轮中会因为工具调用反复进入，压缩只该发生在轮次边界。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from . import context
from .context import message_text
from .. import users
from .profile import Profiles, resolve_profile
from .prompts import build_system_prompt

if TYPE_CHECKING:
    from .usage import UsageStore

log = logging.getLogger(__name__)

# 需要人工确认的工具 -> 对应的预览工具（用来找出预览时锁定的参数给用户看）
SENSITIVE_TOOLS: dict[str, str] = {
    "sports_reserve_confirm": "sports_reserve_preview",
    "sports_cancel_confirm": "sports_cancel_preview",
    "library_prebook_confirm": "library_prebook_preview",
    "library_prebook_cancel_confirm": "library_prebook_cancel_preview",
    "booth_reserve_confirm": "booth_reserve_preview",
    "booth_cancel_confirm": "booth_cancel_preview",
}

def find_preview(messages: list[AnyMessage], preview_tool: str, token: str) -> dict[str, Any] | None:
    """在历史里找到签发该 token 的预览结果。展示给用户的是服务端锁定的参数，而不是 LLM 的转述。"""
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage) and msg.name == preview_tool:
            text = message_text(msg)
            if token not in text:
                continue
            try:
                return json.loads(text)["data"]["preview"]
            except (json.JSONDecodeError, KeyError, TypeError):
                m = re.search(r"\{.*\}", text, re.S)
                return {"raw": m.group(0) if m else text}
    return None


def repair_tool_pairs(messages: list[AnyMessage]) -> list[AnyMessage]:
    """OpenAI 兼容接口要求：带 tool_calls 的 AI 消息后面必须紧跟每个调用的 ToolMessage，否则整段对话都会 400。

    两种情况会留下“有调用、没结果”的历史：
      1. 确认卡片还没点，用户就发了新消息（human_review 的中断被新一轮对话跳过）
      2. 工具执行到一半被取消（刷新页面、断网），checkpoint 里只有 AI 的 tool_calls
    这里在发给 LLM 之前补上“未完成”的结果；只影响发给模型的副本，不改动已保存的状态。
    """
    out: list[AnyMessage] = []
    for i, m in enumerate(messages):
        out.append(m)
        if not (isinstance(m, AIMessage) and m.tool_calls):
            continue
        answered = set()
        for n in messages[i + 1:]:
            if not isinstance(n, ToolMessage):
                break
            answered.add(n.tool_call_id)
        for tc in m.tool_calls:
            if tc["id"] not in answered:
                reason = "用户没有确认就发了新消息" if tc["name"] in SENSITIVE_TOOLS else "执行被中断（可能是页面刷新或网络断开）"
                out.append(ToolMessage(
                    content=json.dumps({"ok": False, "error": {"code": "NOT_EXECUTED",
                                        "message": f"该操作没有执行：{reason}。如果用户仍需要，请重新发起。"}}, ensure_ascii=False),
                    tool_call_id=tc["id"], name=tc["name"], status="error"))
    # 补丁紧跟在 AI 消息后面，原有的 ToolMessage 随后照常追加，所以每组调用和结果始终相邻
    return out


def apply_user_choices(tool_calls: list[dict], actions: list[dict], decision: dict) -> list[dict]:
    """确认卡片上除了“确认/再想想”，还可以让用户做选择（预借的取书地点、公共空间的时段）。

    选择结果由图写进工具调用的参数，不经过 LLM：模型不需要“听懂”用户点了什么，也没法改掉它。
    模型自己在 pickup_id 里填的值一律丢掉，用户没点时用预览里预选的那个（卡片上显示的就是它）。
    """
    out = []
    for tc in tool_calls:
        if tc["name"] == "library_prebook_confirm":
            preview = next((a.get("preview") or {} for a in actions if a["tool"] == tc["name"]), {})
            allowed = {o["value"] for o in preview.get("pickupOptions") or []}
            chosen = decision.get("pickup") if decision.get("pickup") in allowed else preview.get("pickupId")
            args = {k: v for k, v in tc["args"].items() if k != "pickup_id"}
            if chosen:
                args["pickup_id"] = chosen
            tc = {**tc, "args": args}
        elif tc["name"] == "booth_reserve_confirm":
            # 公共空间的时段：用户在卡片上点选的格号。只接受预览里列出的空闲格；没点就用预览里预选的
            preview = next((a.get("preview") or {} for a in actions if a["tool"] == tc["name"]), {})
            allowed = {s["index"] for s in preview.get("freeSlots") or []}
            picked = decision.get("slots") or []
            chosen = sorted({i for i in picked if isinstance(i, int) and i in allowed}) if picked else preview.get("selectedSlots") or []
            args = {k: v for k, v in tc["args"].items() if k != "slots"}
            if chosen:
                args["slots"] = chosen
            tc = {**tc, "args": args}
        out.append(tc)
    return out


class AgentState(MessagesState):
    # 已压缩进摘要的历史的摘要；context_start_id 是第一条“未压缩”消息的 id。
    # 用 id 而不是下标：即使以后有删除消息的操作，也不会错位
    summary: str
    context_start_id: str | None


def build_model_input(state: dict[str, Any], system_prompt: str) -> list[AnyMessage]:
    """每次调用 LLM 前组装消息：系统提示 + 窗口（补齐残缺调用 → 兜底裁剪 → 加时间戳）。"""
    msgs = state["messages"]
    window = msgs[context.window_start(msgs, state.get("context_start_id")):]
    window = context.fit_budget(repair_tool_pairs(window))
    return [SystemMessage(system_prompt), *context.stamp_user_messages(window)]


def build_graph(
    llm: BaseChatModel,
    tools: list[BaseTool],
    *,
    profile: Profiles,
    checkpointer: BaseCheckpointSaver | None = None,
    summary_llm: BaseChatModel | None = None,
    usage: "UsageStore | None" = None,
):
    llm_with_tools = llm.bind_tools(tools)
    summary_llm = summary_llm or llm  # 摘要可以换成更便宜的模型
    tool_node = ToolNode(tools, handle_tool_errors=True)

    def record_usage(msg: Any, purpose: str, model_llm: BaseChatModel, config: RunnableConfig, started: float) -> None:
        """记一笔模型调用用量（token、缓存命中、耗时、花费）。只记数字不记内容；记账失败不能影响回答。"""
        if usage is None:
            return
        try:
            usage.record(msg, purpose=purpose, model=getattr(model_llm, "model_name", None),
                         thread_id=(config.get("configurable") or {}).get("thread_id"),
                         user_id=users.user_of(config),
                         latency_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001
            log.warning("记录模型用量失败：%s", type(exc).__name__)

    async def prepare(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        update: dict[str, Any] = {}
        msgs = state["messages"]
        last = msgs[-1] if msgs else None
        if isinstance(last, HumanMessage) and "sent_at" not in last.additional_kwargs:
            # 同 id 的消息会被 add_messages 原地替换，所以时间戳会随 checkpoint 一起保存
            update["messages"] = [last.model_copy(update={
                "additional_kwargs": {**last.additional_kwargs, "sent_at": datetime.now().astimezone().isoformat(timespec="seconds")}})]

        start = context.window_start(msgs, state.get("context_start_id"))
        window = msgs[start:]
        if context.total_tokens(window) <= context.COMPACT_TRIGGER:
            return update
        cut = context.choose_cut(window)
        if cut is None:
            return update
        try:
            started = time.monotonic()
            summary = await context.summarize(
                summary_llm, state.get("summary", ""), window[:cut],
                on_response=lambda m: record_usage(m, "summary", summary_llm, config, started))
        except Exception as exc:  # 摘要失败不影响这一轮回答：窗口不前移，由 fit_budget 兜底
            log.warning("压缩对话历史失败：%s", type(exc).__name__)
            return update
        log.info("压缩对话历史：%d 条消息并入摘要", cut)
        return {**update, "summary": summary, "context_start_id": window[cut].id}

    async def agent(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        system_prompt = build_system_prompt(resolve_profile(profile, users.user_of(config)).render(), state.get("summary", ""))
        started = time.monotonic()
        response = await llm_with_tools.ainvoke(build_model_input(state, system_prompt))
        record_usage(response, "chat", llm, config, started)
        return {"messages": [response]}

    def route(state: AgentState) -> Literal["tools", "human_review", "__end__"]:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return END
        if any(tc["name"] in SENSITIVE_TOOLS for tc in last.tool_calls):
            return "human_review"
        return "tools"

    def human_review(state: AgentState) -> Command[Literal["tools", "agent"]]:
        last: AIMessage = state["messages"][-1]
        actions = []
        for tc in last.tool_calls:
            if tc["name"] not in SENSITIVE_TOOLS:
                continue
            token = tc["args"].get("confirmation_token", "")
            actions.append({
                "tool": tc["name"],
                "preview": find_preview(state["messages"], SENSITIVE_TOOLS[tc["name"]], token),
            })

        # 图在这里暂停，状态存进 checkpointer；调用方用 Command(resume={"approved": ...}) 恢复
        decision = interrupt({"type": "confirm_action", "actions": actions})

        if isinstance(decision, dict) and decision.get("approved"):
            calls = apply_user_choices(last.tool_calls, actions, decision)
            if calls == last.tool_calls:
                return Command(goto="tools")
            # 同 id 的 AI 消息会被原地替换：执行的是“用户在卡片上的选择”，而不是模型填的值
            return Command(goto="tools", update={"messages": [last.model_copy(update={"tool_calls": calls})]})

        reason = decision.get("reason") if isinstance(decision, dict) else None
        # 每个 tool_call 都必须有对应的 ToolMessage，否则下一轮请求会被 API 拒绝
        rejected = [
            ToolMessage(
                content=json.dumps(
                    {"ok": False, "error": {"code": "USER_REJECTED", "message": f"用户拒绝了该操作{'：' + reason if reason else ''}，不要再次尝试，询问用户想怎么调整。"}},
                    ensure_ascii=False,
                ),
                tool_call_id=tc["id"],
                name=tc["name"],
            )
            for tc in last.tool_calls
        ]
        return Command(goto="agent", update={"messages": rejected})

    async def tools(state: AgentState, config: RunnableConfig) -> Any:
        """执行工具前把“当前用户”放进 contextvar：MCP 工具调用会把它写进请求元数据（runtime.UserMetaSession），
        工具进程据此用这个人的学校登录凭证。来源是 config（web 层按会话 Cookie 写入），不是模型输出。"""
        token = users.current_user.set(users.user_of(config))
        try:
            return await tool_node.ainvoke(state, config)
        finally:
            users.current_user.reset(token)

    graph = StateGraph(AgentState)
    graph.add_node("prepare", prepare)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_node("human_review", human_review)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "agent")
    graph.add_conditional_edges("agent", route, ["tools", "human_review", END])
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)
