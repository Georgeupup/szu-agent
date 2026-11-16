"""上下文工程：决定每次调用 LLM 时，把哪些历史、以什么形式交给模型。

三个目标，按优先级：
  1. 不出错：发给模型的消息必须合法（tool_call 与结果成对），且不超过上下文窗口
  2. 不丢关键信息：早期对话里的校区、订单号、用户的要求，压缩后仍然可用
  3. 省钱、低延迟：DeepSeek 对“和上次请求相同的前缀”自动缓存，命中部分按约 1/10 计费、首字更快。
     所以发给模型的前缀要尽量逐字不变——
       - 系统提示只放很少变的内容（指令、用户画像、对话摘要），“当前时间”不放进去
       - 时间改成写在每条用户消息前面（用的是消息发送时的时间，所以旧消息的渲染结果永远不变）
       - 不做“每轮都把旧工具结果截短”这类按轮次变化的处理，否则每轮前缀都会变；
         压缩只在超出预算时批量做一次（compact），之后前缀又稳定下来

整体流程（每轮对话开始时一次，见 graph.prepare 节点）：

    完整历史（checkpoint 里，一条不删，界面恢复对话用）
      └─ context_start_id 之前的部分 → 已压缩进 summary
      └─ 之后的部分 = 窗口；窗口超过 COMPACT_TRIGGER → 把较早的几轮交给 LLM 合并进 summary，
         只保留最近 KEEP_RECENT 以内的完整轮次
    发给模型 = [系统提示（含 summary）] + 窗口（补齐残缺的工具调用、加时间戳、兜底裁剪）

为什么自己写而不是用 langchain 的 trim_messages / langmem 的 SummarizationNode：
需要“按轮次切分”“确认令牌不能进摘要”“兜底裁剪不能切掉最近一次工具结果”这些业务约束，
自己写只有一百多行，也更容易测试和解释。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Callable, Iterable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

# —— 预算（单位：token，均为估算值）——
# DeepSeek 上下文 64K+；系统提示约 2K，41 个工具的 schema 约 8.5K（2026-11-16 估算）。历史给到 24K 以内足够，
# 再多只会让模型更容易被旧内容干扰，也更贵。
COMPACT_TRIGGER = 24_000   # 窗口超过这个值时，在下一轮开始前压缩
KEEP_RECENT = 8_000        # 压缩后至少保留的最近内容（按整轮保留，最后一轮一定保留）
HARD_LIMIT = 40_000        # 兜底：摘要失败或单轮本身就很大时，发给模型前强制裁剪到这个值以内
CLIP_TOOL_CHARS = 1_500    # 兜底裁剪时，单条工具结果保留的字符数
SUMMARY_INPUT_CHARS = 600  # 生成摘要时，每条工具结果只给摘要模型看这么多字

WEEKDAYS = "一二三四五六日"
_CJK = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]")


# ------------------------------ token 估算 ------------------------------ #

def estimate_tokens(text: str) -> int:
    """按 DeepSeek 官方给的换算估算：1 个中文字符约 0.6 token，1 个英文字符约 0.3 token。

    不用真正的分词器：DeepSeek 的 tokenizer 要额外下载，而这里只用来做预算判断，
    误差 10%~20% 完全可以接受（预算本身离上下文上限留了很大余量）。
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return math.ceil(cjk * 0.6 + (len(text) - cjk) * 0.3)


def message_text(msg: AnyMessage) -> str:
    """content 可能是 str，也可能是 content block 列表。"""
    content = msg.content
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)


def message_tokens(msg: AnyMessage) -> int:
    n = 4 + estimate_tokens(message_text(msg))  # 每条消息的角色、分隔符等固定开销
    if isinstance(msg, AIMessage):
        for tc in msg.tool_calls:
            n += 8 + estimate_tokens(tc["name"]) + estimate_tokens(json.dumps(tc["args"], ensure_ascii=False))
    return n


def total_tokens(messages: Iterable[AnyMessage]) -> int:
    return sum(message_tokens(m) for m in messages)


# ------------------------------ 按轮次切分 ------------------------------ #

def turn_starts(messages: list[AnyMessage]) -> list[int]:
    """每一轮从一条 HumanMessage 开始。只在轮次边界切，就不会把 tool_call 和它的结果拆开。"""
    return [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]


def window_start(messages: list[AnyMessage], start_id: str | None) -> int:
    if start_id:
        for i, m in enumerate(messages):
            if m.id == start_id:
                return i
    return 0


def choose_cut(window: list[AnyMessage], keep: int | None = None) -> int | None:
    """从后往前累加整轮，找到保留部分的起点；返回 None 表示没有可以压缩的旧轮次。"""
    keep = KEEP_RECENT if keep is None else keep
    starts = turn_starts(window)
    if len(starts) < 2:
        return None
    cut = starts[-1]  # 最后一轮（当前问题）一定保留
    for s in reversed(starts[:-1]):
        if total_tokens(window[s:]) > keep:
            break
        cut = s
    return cut if cut > 0 else None


# ------------------------------ 摘要 ------------------------------ #

SUMMARY_PROMPT = """你在为一个校园事务助手压缩对话历史。把【已有摘要】和【新对话】合并成一份新的摘要，供助手之后继续对话时参考。

必须保留：
- 用户的目标、明确提出的要求和约束（校区、日期、时间段、项目、宿舍等）
- 已经完成的事和结果：预约/取消是否成功、订单号、查到的关键数字（电量、余额）、读过的通知要点（截止日期、条件）
- 还没完成、等用户回复的事
不要保留：
- 确认令牌（confirmationToken）——令牌一次性且很快过期，旧令牌绝不能再用
- 寒暄、助手的完整措辞、工具调用细节、链接以外的大段原文
- 学号、姓名、手机号等个人身份信息

用第三人称、要点列表、中文，不超过 400 字。只输出摘要本身。"""


def render_transcript(messages: list[AnyMessage]) -> str:
    """把一段历史渲染成纯文本给摘要模型看。工具结果只取开头，避免摘要请求本身就很大。"""
    lines: list[str] = []
    for m in messages:
        text = message_text(m).strip()
        if isinstance(m, HumanMessage):
            sent = m.additional_kwargs.get("sent_at", "")
            lines.append(f"用户{f'（{sent[:16]}）' if sent else ''}：{text}")
        elif isinstance(m, AIMessage):
            for tc in m.tool_calls:
                args = {k: v for k, v in tc["args"].items() if k != "confirmation_token"}
                lines.append(f"助手调用 {tc['name']}({json.dumps(args, ensure_ascii=False)})")
            if text:
                lines.append(f"助手：{text}")
        elif isinstance(m, ToolMessage):
            text = re.sub(r'"confirmationToken"\s*:\s*"[^"]*"', '"confirmationToken": "（已省略）"', text)
            if len(text) > SUMMARY_INPUT_CHARS:
                text = text[:SUMMARY_INPUT_CHARS] + "…（后略）"
            lines.append(f"工具 {m.name} 返回：{text}")
    return "\n".join(lines)


async def summarize(llm: BaseChatModel, previous: str, messages: list[AnyMessage],
                    on_response: Callable[[Any], None] | None = None) -> str:
    out = await llm.ainvoke([
        SystemMessage(SUMMARY_PROMPT),
        HumanMessage(f"【已有摘要】\n{previous or '（无）'}\n\n【新对话】\n{render_transcript(messages)}"),
    ])
    if on_response:
        on_response(out)  # 记用量：摘要请求也要花钱
    summary = message_text(out).strip()
    if not summary:
        raise ValueError("摘要为空")
    return summary


# ------------------------------ 发给模型前的处理 ------------------------------ #

def time_stamp(iso: str | None) -> str | None:
    try:
        t = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None
    if not t:
        return None
    from ..knowledge.calendar import week_label  # 只读本地 yaml，结果只取决于日期，不影响前缀缓存

    week = week_label(t.date())
    return f"{t:%Y-%m-%d} 星期{WEEKDAYS[t.weekday()]} {t:%H:%M}" + (f" {week}" if week else "")


def stamp_user_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """在每条用户消息前加上它的发送时间（只改发给模型的副本）。

    比“系统提示里写当前时间”多两个好处：
      - 渲染结果只取决于消息本身，前缀稳定，能命中缓存
      - 对话可以跨天保存后继续，模型能分清“昨天那条消息里的明天”和“今天说的明天”
    """
    out: list[AnyMessage] = []
    for m in messages:
        stamp = time_stamp(m.additional_kwargs.get("sent_at")) if isinstance(m, HumanMessage) else None
        out.append(m.model_copy(update={"content": f"[{stamp}] {message_text(m)}"}) if stamp else m)
    return out


def fit_budget(messages: list[AnyMessage], limit: int | None = None) -> list[AnyMessage]:
    """兜底：正常情况下 compact 已经把窗口控制在预算内，这里只处理摘要失败、单轮巨大等情况。

    1. 从最早的整轮开始丢，至少保留最后一轮
    2. 还超就把较早的长工具结果截短——但最后一批工具结果不动（模型这一步正要用它，比如确认令牌）
    """
    limit = HARD_LIMIT if limit is None else limit
    if total_tokens(messages) <= limit:
        return messages
    starts = turn_starts(messages)
    for s in starts:
        if total_tokens(messages[s:]) <= limit:
            return messages[s:]
    msgs = messages[starts[-1]:] if starts else list(messages)

    last_batch = set()
    for m in reversed(msgs):
        if not isinstance(m, ToolMessage):
            break
        last_batch.add(id(m))
    out = []
    for m in msgs:
        text = message_text(m)
        if isinstance(m, ToolMessage) and id(m) not in last_batch and len(text) > CLIP_TOOL_CHARS:
            m = m.model_copy(update={"content": text[:CLIP_TOOL_CHARS] + "…（结果过长已截断，需要细节请重新调用该工具）"})
        out.append(m)
    return out


def context_stats(messages: list[AnyMessage], start_id: str | None, summary: str) -> dict[str, Any]:
    """给界面/日志看的上下文用量，方便观察压缩是否按预期发生。"""
    start = window_start(messages, start_id)
    return {
        "messages": len(messages),
        "windowMessages": len(messages) - start,
        "windowTokens": total_tokens(messages[start:]),
        "summaryTokens": estimate_tokens(summary),
        "compactTrigger": COMPACT_TRIGGER,
    }
