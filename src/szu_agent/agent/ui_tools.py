"""界面工具（Generative UI）：LLM 用结构化参数“画”界面，前端负责渲染。

和让 LLM 直接输出 Markdown 相比：
  - 结构由 Pydantic schema 约束，字段缺失或格式不对会被拒绝，LLM 会收到错误并重试
  - 前端可以做排序、倒计时、颜色这类 Markdown 做不到的事
  - 同一份数据在终端里也能渲染成表格

这类工具不访问学校系统，所以不放在 MCP Server 里，而是 agent 本地的工具。
"""

from __future__ import annotations

import re
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

NoticeKind = Literal["申请/报名", "公示/结果", "活动/讲座", "安排/提醒", "通知"]


class NoticeCard(BaseModel):
    id: str | None = Field(default=None, description="公告 id")
    title: str = Field(description="通知标题（可以适当缩短）")
    url: str | None = Field(default=None, description="原文链接")
    publisher: str | None = None
    date: str | None = Field(default=None, description="发布日期 YYYY-MM-DD")
    kind: NoticeKind = Field(description="通知类型")
    audience: str | None = Field(default=None, description="面向谁，如“本科生”“研究生”“全体学生”")
    deadline: str | None = Field(default=None, description="截止日期 YYYY-MM-DD；正文里没有写就留空，不要猜")
    summary: str = Field(description="一句话说清这条通知是什么，不超过 40 字")
    key_points: list[str] = Field(default_factory=list, max_length=4,
                                  description="最多 4 条要点：金额/名额/条件/材料等，每条不超过 30 字")
    action: str | None = Field(default=None, description="用户下一步该做什么，如“12 月 18 日前在 szupu 平台提交申请”")

    @field_validator("deadline", "date")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("日期必须是 YYYY-MM-DD，没有就留空")
        return v


class NoticeGroup(BaseModel):
    name: str = Field(description="分组名，如“现在可以申请”“已出结果”“与你关系不大”")
    items: list[NoticeCard] = Field(min_length=1, max_length=8)


@tool
def show_notice_board(topic: str, summary: str, groups: list[NoticeGroup], tip: str | None = None) -> str:
    """把整理好的公文通通知以可视化卡片面板展示给用户（含截止日期时间线）。

    在读过通知正文（notice_read_many）之后调用。按“对用户有什么用”分组，最重要的组放最前面。
    topic：主题，如“奖学金”；summary：一两句话总览（共找到多少、最值得关注的是什么）；
    tip：可选的一句提醒，如“研究生的学业奖学金名单公示较多，已为你略过”。
    调用后不要在回复里重复卡片内容，只用一两句话补充或询问下一步。"""
    n = sum(len(g.items) for g in groups)
    deadlines = sum(1 for g in groups for c in g.items if c.deadline)
    return f"已在界面上展示「{topic}」面板：{len(groups)} 组 {n} 条通知，其中 {deadlines} 条有截止日期。不要在回复里重复卡片内容。"


@tool
def show_recharge_card(building: str, room: str) -> str:
    """在界面上弹出宿舍电费充值卡片：剩余电量、按日均用电算的“充 20/50/100 元各能多用几天”，以及微信充值页的二维码。
    用户问“帮我充电费”“电费快没了怎么办”，或者查到剩余电量偏低时调用。
    付款只能由用户用微信扫码、在微信里完成——你不能代付，也不要说“已充值”。调用后只需一句话提醒扫码充值。"""
    return f"已在界面上展示 {building} {room} 的充值卡片（含充值方案和微信扫码入口）。不要重复卡片里的数字。"


UI_TOOLS = [show_notice_board, show_recharge_card]
UI_TOOL_NAMES = {t.name for t in UI_TOOLS}
