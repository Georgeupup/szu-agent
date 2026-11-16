"""上下文工程：token 估算、按轮压缩成摘要、兜底裁剪、时间戳，以及“前缀稳定”（能命中 DeepSeek 缓存）。"""

import json
from datetime import datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from szu_agent.agent import context
from szu_agent.agent.graph import build_graph
from szu_agent.agent.profile import ProfileStore

from .conftest import ScriptedChatModel, ai_tool_call, assert_valid_tool_sequence

CFG = {"configurable": {"thread_id": "ctx"}}
BIG = "公告正文" * 500  # 约 1200 token


@tool
def notice_view(id: str) -> str:
    """读公告"""
    return json.dumps({"ok": True, "data": {"id": id, "body": BIG}}, ensure_ascii=False)


def turn(i: int) -> list:
    return [HumanMessage(f"问题{i}", id=f"h{i}"), ai_tool_call("notice_view", {"id": str(i)}, f"c{i}"),
            ToolMessage(BIG, tool_call_id=f"c{i}", name="notice_view", id=f"t{i}"), AIMessage(f"回答{i}", id=f"a{i}")]


def test_estimate_tokens_follows_deepseek_ratio():
    assert context.estimate_tokens("") == 0
    assert context.estimate_tokens("深圳大学") == 3        # 4 × 0.6
    assert context.estimate_tokens("hello world") == 4     # 11 × 0.3


def test_choose_cut_keeps_whole_recent_turns():
    msgs = [m for i in range(5) for m in turn(i)]
    cut = context.choose_cut(msgs, keep=3000)
    assert isinstance(msgs[cut], HumanMessage)
    assert context.total_tokens(msgs[cut:]) <= 3000 < context.total_tokens(msgs[cut - 4:])
    assert context.choose_cut(turn(0)) is None  # 只有一轮：没有可压缩的


def test_fit_budget_drops_old_turns_then_clips_but_keeps_latest_tool_result():
    msgs = [m for i in range(5) for m in turn(i)]
    out = context.fit_budget(msgs, limit=3000)
    assert isinstance(out[0], HumanMessage) and context.total_tokens(out) <= 3000

    # 单轮就超了：较早的工具结果截短，最后一批（模型这一步要用的）保持完整
    one = [HumanMessage("q"), ai_tool_call("notice_view", {}, "x"), ToolMessage(BIG, tool_call_id="x", name="notice_view"),
           ai_tool_call("notice_view", {}, "y"), ToolMessage(BIG, tool_call_id="y", name="notice_view")]
    out = context.fit_budget(one, limit=1500)
    assert "已截断" in out[2].content and out[4].content == BIG
    assert_valid_tool_sequence(out)


def test_stamp_uses_send_time_and_does_not_touch_state():
    m = HumanMessage("明天的羽毛球", additional_kwargs={"sent_at": "2026-11-14T18:50:00+08:00"})
    out = context.stamp_user_messages([m])
    assert out[0].content == "[2026-11-14 星期六 18:50 第11教学周] 明天的羽毛球"
    assert m.content == "明天的羽毛球"  # 只改副本，界面里显示的还是原话


def test_transcript_for_summary_drops_confirmation_tokens():
    msgs = [HumanMessage("约羽毛球"), ai_tool_call("sports_reserve_confirm", {"confirmation_token": "secret-tok"}, "c"),
            ToolMessage('{"ok": true, "data": {"confirmationToken": "secret-tok", "orderNo": "A123"}}', tool_call_id="c", name="x")]
    text = context.render_transcript(msgs)
    assert "secret-tok" not in text and "A123" in text


@pytest.fixture
def small_budget(monkeypatch):
    monkeypatch.setattr(context, "COMPACT_TRIGGER", 4000)
    monkeypatch.setattr(context, "KEEP_RECENT", 1500)


def make(script, summaries, tmp_path):
    llm = ScriptedChatModel(script=script)
    summary_llm = ScriptedChatModel(script=summaries)
    graph = build_graph(llm, [notice_view], profile=ProfileStore(tmp_path / "p.json"),
                        checkpointer=InMemorySaver(), summary_llm=summary_llm)
    return graph, llm, summary_llm


def reads(i):
    return [ai_tool_call("notice_view", {"id": str(i)}, f"c{i}"), AIMessage(f"回答{i}")]


async def test_long_conversation_is_compacted_into_summary(tmp_path, small_budget):
    script = [m for i in range(6) for m in reads(i)]
    graph, llm, summary_llm = make(script, [AIMessage("- 用户在找奖学金通知，已读过 0~2 号公告")], tmp_path)
    for i in range(6):
        await graph.ainvoke({"messages": [("user", f"读第{i}条")]}, CFG)

    state = (await graph.aget_state(CFG)).values
    assert len(state["messages"]) == 24  # 完整历史一条不删（界面恢复对话用）
    assert state["summary"].startswith("- 用户在找奖学金")
    assert len(summary_llm.calls) == 1  # 超预算时批量压缩一次，不是每轮都压

    last_input = llm.calls[-1]
    assert "更早的对话摘要" in last_input[0].content and "已读过 0~2" in last_input[0].content
    texts = [m.content for m in last_input[1:] if isinstance(m, HumanMessage)]
    assert not any("读第0条" in t for t in texts) and any("读第5条" in t for t in texts)
    assert context.total_tokens(last_input[1:]) <= context.COMPACT_TRIGGER
    # 每条用户消息都记下了发送时间，并以时间戳形式交给模型
    assert all("sent_at" in m.additional_kwargs for m in state["messages"] if isinstance(m, HumanMessage))
    assert texts[0].startswith("[20")


async def test_summary_failure_does_not_break_the_turn(tmp_path, small_budget):
    script = [m for i in range(6) for m in reads(i)]
    graph, llm, _ = make(script, [], tmp_path)  # 摘要模型剧本为空 -> 每次摘要都报错
    for i in range(6):
        out = await graph.ainvoke({"messages": [("user", f"读第{i}条")]}, CFG)
    assert out["messages"][-1].content == "回答5"
    assert not (await graph.aget_state(CFG)).values.get("summary")


async def test_prompt_prefix_is_stable_between_turns(tmp_path):
    """DeepSeek 缓存按前缀命中：第二轮发给模型的消息，开头必须和第一轮逐字相同。
    以前系统提示里写着“当前时间”，每分钟都在变，整段缓存永远命中不了。"""
    graph, llm, _ = make([*reads(1), AIMessage("好的")], [], tmp_path)
    await graph.ainvoke({"messages": [("user", "读第1条")]}, CFG)
    await graph.ainvoke({"messages": [("user", "谢谢")]}, CFG)

    first, second = llm.calls[-2], llm.calls[-1]
    dump = lambda ms: [(type(m).__name__, m.content) for m in ms]
    assert dump(second[:len(first)]) == dump(first)
    assert isinstance(second[0], SystemMessage)
    # 同一分钟内跑完的测试看不出时间变化，所以直接检查：系统提示里不能有当前日期
    assert datetime.now().strftime("%Y-%m-%d") not in second[0].content
