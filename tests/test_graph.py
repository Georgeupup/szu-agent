"""图逻辑测试：用假 LLM + 假工具，验证路由和人工确认。"""

import json

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from szu_agent.agent.graph import build_graph
from szu_agent.agent.profile import ProfileStore, make_profile_tools

from .conftest import ScriptedChatModel, ai_tool_call

executed: list[str] = []


@tool
def sports_reserve_preview(campus: str, project: str, date: str, slot: str) -> str:
    """预览预约"""
    executed.append("preview")
    return json.dumps({"ok": True, "data": {"preview": {"campus": campus, "venue": project, "date": date, "slot": slot}, "confirmationToken": "tok-1"}})


@tool
def sports_reserve_confirm(confirmation_token: str) -> str:
    """提交预约"""
    executed.append(f"confirm:{confirmation_token}")
    return json.dumps({"ok": True, "data": {"submitted": True}})


@tool
def campus_events(query: str | None = None) -> str:
    """查活动"""
    executed.append("events")
    return json.dumps({"ok": True, "data": {"notices": [{"title": "百团大战"}]}})


def make_graph(script, tmp_path):
    executed.clear()
    llm = ScriptedChatModel(script=script)
    profile = ProfileStore(tmp_path / "profile.json")
    tools = [sports_reserve_preview, sports_reserve_confirm, campus_events, *make_profile_tools(profile)]
    return build_graph(llm, tools, profile=profile, checkpointer=InMemorySaver()), llm, profile


CFG = {"configurable": {"thread_id": "t"}}
PREVIEW_ARGS = {"campus": "粤海校区", "project": "羽毛球", "date": "2026-11-15", "slot": "20:00-21:00"}


async def test_read_only_tools_run_without_confirmation(tmp_path):
    graph, _, _ = make_graph([ai_tool_call("campus_events", {"query": "社团"}), AIMessage(content="有百团大战")], tmp_path)
    out = await graph.ainvoke({"messages": [("user", "最近有什么社团活动")]}, CFG)
    assert executed == ["events"]
    assert out["messages"][-1].content == "有百团大战"


async def test_confirm_tool_interrupts_and_shows_server_preview(tmp_path):
    graph, _, _ = make_graph(
        [
            ai_tool_call("sports_reserve_preview", PREVIEW_ARGS, "c1"),
            AIMessage(content="预览如下，确认吗？"),
            ai_tool_call("sports_reserve_confirm", {"confirmation_token": "tok-1"}, "c2"),
            AIMessage(content="预约成功，记得支付"),
        ],
        tmp_path,
    )
    await graph.ainvoke({"messages": [("user", "约明晚8点羽毛球")]}, CFG)
    assert executed == ["preview"]

    await graph.ainvoke({"messages": [("user", "确认")]}, CFG)
    state = await graph.aget_state(CFG)
    assert state.next == ("human_review",)
    payload = state.interrupts[0].value
    # 展示给用户的是预览工具（服务端）返回的参数，而不是 LLM 的复述
    assert payload["actions"][0]["preview"]["slot"] == "20:00-21:00"
    assert executed == ["preview"]  # 暂停期间绝不执行

    out = await graph.ainvoke(Command(resume={"approved": True}), CFG)
    assert executed == ["preview", "confirm:tok-1"]
    assert out["messages"][-1].content == "预约成功，记得支付"


async def test_rejection_skips_tool_and_informs_llm(tmp_path):
    graph, llm, _ = make_graph(
        [ai_tool_call("sports_reserve_confirm", {"confirmation_token": "tok-1"}, "c9"), AIMessage(content="好的，已取消")],
        tmp_path,
    )
    await graph.ainvoke({"messages": [("user", "直接帮我约")]}, CFG)
    out = await graph.ainvoke(Command(resume={"approved": False, "reason": "换成9点"}), CFG)

    assert executed == []
    rejected = [m for m in out["messages"] if isinstance(m, ToolMessage)][-1]
    assert rejected.tool_call_id == "c9" and "USER_REJECTED" in rejected.content and "换成9点" in rejected.content
    assert out["messages"][-1].content == "好的，已取消"


async def test_profile_is_remembered_and_injected_into_prompt(tmp_path):
    graph, llm, profile = make_graph(
        [
            ai_tool_call("remember_user_info", {"key": "dorm_building", "value": "红豆斋"}),
            AIMessage(content="记住了"),
            AIMessage(content="ok"),
        ],
        tmp_path,
    )
    await graph.ainvoke({"messages": [("user", "我住红豆斋")]}, CFG)
    assert profile.load() == {"dorm_building": "红豆斋"}
    await graph.ainvoke({"messages": [("user", "查电费")]}, {"configurable": {"thread_id": "new"}})
    assert "宿舍楼栋：红豆斋" in llm.calls[-1][0].content


async def test_parallel_remember_calls_do_not_lose_updates(tmp_path):
    # 真实模型（DeepSeek）会在一条消息里并行发出两次 remember_user_info
    two_calls = AIMessage(content="", tool_calls=[
        {"name": "remember_user_info", "args": {"key": "dorm_building", "value": "红豆斋"}, "id": "m1", "type": "tool_call"},
        {"name": "remember_user_info", "args": {"key": "dorm_room", "value": "838"}, "id": "m2", "type": "tool_call"},
    ])
    for _ in range(5):  # 竞态不是每次都触发，多跑几次
        graph, _, profile = make_graph([two_calls, AIMessage(content="ok")], tmp_path)
        profile.path.unlink(missing_ok=True)
        await graph.ainvoke({"messages": [("user", "我住红豆斋838")]}, CFG)
        assert profile.load() == {"dorm_building": "红豆斋", "dorm_room": "838"}



async def test_ui_tool_rejects_bad_dates_so_llm_can_retry(tmp_path):
    from szu_agent.agent.ui_tools import show_notice_board

    card = {"title": "国家奖学金", "kind": "申请/报名", "summary": "二年级以上本科生可申请"}
    bad = {"topic": "奖学金", "summary": "s", "groups": [{"name": "可申请", "items": [{**card, "deadline": "11月20日"}]}]}
    good = {"topic": "奖学金", "summary": "s", "groups": [{"name": "可申请", "items": [{**card, "deadline": "2026-11-20"}]}]}
    executed.clear()
    llm = ScriptedChatModel(script=[ai_tool_call("show_notice_board", bad, "b1"), ai_tool_call("show_notice_board", good, "b2"),
                                    AIMessage(content="最近的截止是 11 月 20 日")])
    graph = build_graph(llm, [show_notice_board], profile=ProfileStore(tmp_path / "p.json"), checkpointer=InMemorySaver())
    out = await graph.ainvoke({"messages": [("user", "奖学金")]}, CFG)
    results = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert results[0].status == "error" and "YYYY-MM-DD" in results[0].content  # 错误信息回到 LLM
    assert "1 条有截止日期" in results[1].content


async def test_new_message_while_confirmation_pending(tmp_path):
    """确认卡片还没点，用户就发了新消息：历史里会留下没有结果的 tool_call。"""
    graph, llm, _ = make_graph(
        [ai_tool_call("sports_reserve_confirm", {"confirmation_token": "tok-1"}, "c1"), AIMessage(content="好的，换个话题")],
        tmp_path,
    )
    await graph.ainvoke({"messages": [("user", "直接帮我约")]}, CFG)
    assert (await graph.aget_state(CFG)).next == ("human_review",)
    out = await graph.ainvoke({"messages": [("user", "算了，先帮我查下活动")]}, CFG)
    assert out["messages"][-1].content == "好的，换个话题"
    assert executed == []  # 没确认的预约绝不能被执行


async def test_tool_interrupted_midway(tmp_path):
    """工具执行到一半被取消（刷新页面/断网），checkpoint 里只有 AI 的 tool_calls 没有结果。"""
    from langchain_core.messages import HumanMessage

    graph, llm, _ = make_graph([AIMessage(content="继续")], tmp_path)
    broken = [HumanMessage("查奖学金"), ai_tool_call("campus_events", {}, "x1"), HumanMessage("怎么没反应")]
    await graph.aupdate_state(CFG, {"messages": broken})
    out = await graph.ainvoke({"messages": [("user", "继续")]}, CFG)
    assert out["messages"][-1].content == "继续"


def test_repair_only_fills_missing_results():
    from langchain_core.messages import HumanMessage

    from szu_agent.agent.graph import repair_tool_pairs

    from .conftest import assert_valid_tool_sequence

    two = AIMessage(content="", tool_calls=[
        {"name": "campus_events", "args": {}, "id": "a", "type": "tool_call"},
        {"name": "sports_reserve_confirm", "args": {}, "id": "b", "type": "tool_call"}])
    msgs = [HumanMessage("q"), two, ToolMessage("ok", tool_call_id="a", name="campus_events"), HumanMessage("next")]
    fixed = repair_tool_pairs(msgs)
    assert_valid_tool_sequence(fixed)
    patch = [m for m in fixed if isinstance(m, ToolMessage) and m.tool_call_id == "b"][0]
    assert "没有确认" in patch.content and len(fixed) == len(msgs) + 1
    assert repair_tool_pairs(fixed) == fixed  # 已经完整的历史不做任何改动


OPTIONS = [{"value": "4", "name": "丽湖A2"}, {"value": "6", "name": "南区取书柜"}, {"value": "3", "name": "北馆二楼"}]


@tool
def library_prebook_preview(ctrlno: str, pickup: str | None = None) -> str:
    """预览预借"""
    executed.append("preview")
    return json.dumps({"ok": True, "data": {"preview": {"book": "三体", "pickup": None, "pickupId": None, "pickupOptions": OPTIONS},
                                            "confirmationToken": "tok-p"}})


@tool
def library_prebook_confirm(confirmation_token: str, pickup_id: str | None = None) -> str:
    """提交预借"""
    executed.append(f"prebook:{pickup_id}")
    return json.dumps({"ok": True, "data": {"submitted": True}})


async def test_pickup_chosen_on_card_overrides_model(tmp_path):
    """用户在确认卡片上点的取书地点由图写进工具参数；模型自己填的 pickup_id 不算数。"""
    executed.clear()
    llm = ScriptedChatModel(script=[
        ai_tool_call("library_prebook_preview", {"ctrlno": "1901562"}, "p1"),
        ai_tool_call("library_prebook_confirm", {"confirmation_token": "tok-p", "pickup_id": "4"}, "p2"),
        AIMessage(content="预借成功"),
    ])
    graph = build_graph(llm, [library_prebook_preview, library_prebook_confirm],
                        profile=ProfileStore(tmp_path / "p.json"), checkpointer=InMemorySaver())
    await graph.ainvoke({"messages": [("user", "帮我借三体")]}, CFG)
    state = await graph.aget_state(CFG)
    assert state.interrupts[0].value["actions"][0]["preview"]["pickupOptions"] == OPTIONS  # 卡片据此画按钮
    out = await graph.ainvoke(Command(resume={"approved": True, "pickup": "6"}), CFG)
    assert executed == ["preview", "prebook:6"] and out["messages"][-1].content == "预借成功"


def test_apply_user_choices_ignores_invalid_or_missing_choice():
    from szu_agent.agent.graph import apply_user_choices

    call = [{"name": "library_prebook_confirm", "args": {"confirmation_token": "t", "pickup_id": "4"}, "id": "x", "type": "tool_call"}]
    actions = [{"tool": "library_prebook_confirm", "preview": {"pickupId": "3", "pickupOptions": OPTIONS}}]
    assert apply_user_choices(call, actions, {"pickup": "99"})[0]["args"]["pickup_id"] == "3"   # 不在选项里：用卡片上预选的
    assert apply_user_choices(call, actions, {})[0]["args"]["pickup_id"] == "3"
    no_default = [{"tool": "library_prebook_confirm", "preview": {"pickupId": None, "pickupOptions": OPTIONS}}]
    assert "pickup_id" not in apply_user_choices(call, no_default, {})[0]["args"]  # 模型填的 4 被丢掉，服务端会要求重新选
