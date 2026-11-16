"""统一的错误类型。

工具层捕获 CampusError，转成 {"ok": false, "error": {...}} 交给 LLM。
每个错误码附一句 agentHint：LLM 不需要理解 HTTP 细节，只需要知道下一步该让用户做什么。
"""

from __future__ import annotations

from typing import Any

AGENT_HINTS: dict[str, str] = {
    "LOGIN_REQUIRED": "需要登录深大统一身份认证。告诉用户在界面上出现的登录卡片里扫码或输入账号密码（终端用户运行 `szu-agent login`）。绝对不要在聊天里索要密码。",
    "MFA_REQUIRED": "已登录，但学校要求二次验证（短信/企业微信验证码）。界面会自动弹出验证卡片，简短地请用户完成验证，验证后你会接着完成刚才的事；不要重试，不要在聊天里索要验证码。",
    "CAPTCHA_REQUIRED": "学校要求滑块验证码，程序不会尝试破解。建议用户改用扫码登录。",
    "LOGIN_FAILED": "登录失败，把 message 原样告诉用户（例如密码错误）。",
    "NETWORK_REQUIRED": "该服务仅限校园网访问。把 message 里的说明转告用户（里面写了当前部署方式下怎么办），不要重试。",
    "NETWORK_ERROR": "连接学校服务器失败，可能是网络问题，可以让用户稍后重试一次。",
    "PERMISSION_DENIED": "当前账号无权访问该服务，如实告知用户。",
    "RATE_LIMITED": "学校系统疑似限流。立即停止调用任何工具，告诉用户稍后再试。",
    "PAGE_CHANGED": "学校网页结构和程序预期不一致，适配器需要更新。告诉用户该功能暂时不可用，不要重试。",
    "NOT_SUPPORTED_YET": "这个功能的学校接口还没有接入（需要抓包确认）。如实告诉用户，并给出 url 让用户自己去网页操作。",
    "SPORTS_FIELD_SELECTION_REQUIRED": "该时段有多个可选场地，把候选列给用户，让用户选一个。",
    "SPORTS_SLOT_UNAVAILABLE": "该时段不可预约，建议用 sports_slots 查看其他时段。",
    "SPORTS_PROJECT_NOT_FOUND": "项目名不对，先用 sports_projects 查询项目列表再匹配。",
    "SPORTS_NOT_CANCELLABLE": "学校显示这条预约已不能取消（可能已开始、已完成或已入场），如实告诉用户。",
    "SPORTS_ALREADY_CANCELLED": "这条预约已经取消了，不需要再操作。",
    "SPORTS_CANCEL_FAILED": "学校拒绝了取消，把 message 原样告诉用户，不要自动重试。",
    "SPORTS_SUBMIT_FAILED": "学校系统拒绝了预约，把 message 原样告诉用户。不要自动重试。",
    "ELECTRICITY_ROOM_NOT_FOUND": "楼栋或房间号无法匹配，先用 electricity_buildings 确认楼栋名，再向用户核对房间号。",
    "NOT_FOUND": "没有找到对应的记录，向用户确认输入。",
    "CONTACT_REQUIRED": "需要联系手机号（图书馆预借、事务中心空间预约都要）。界面会自动弹出填写手机号的卡片，简短地请用户填写，填好后你会接着完成刚才的事；绝对不要在聊天里索要手机号。",
    "LIBRARY_PICKUP_REQUIRED": "重新调用 library_prebook_preview 和 library_prebook_confirm，让用户在确认卡片上点选取书地点；不要在聊天里列出选项。",
    "LIBRARY_ALREADY_PREBOOKED": "用户已经预借过这本书，不需要再操作。",
    "LIBRARY_NOT_PREBOOKABLE": "这本书现在不能预借，把 message 告诉用户；可以用 library_book 看各册状态，建议到馆借阅。",
    "LIBRARY_PREBOOK_QUOTA_FULL": "预借数量已满，把 message 告诉用户，建议到馆借阅。",
    "LIBRARY_EMAIL_REQUIRED": "图书馆账户里没有邮箱。请用户先在图书馆网站的预借页面（error.url）手动填写一次。",
    "LIBRARY_NOT_CANCELLABLE": "这条预借当前状态不能取消（可能馆员已经在处理），如实告诉用户。",
    "LIBRARY_CANCEL_FAILED": "图书馆拒绝了取消，把 message 原样告诉用户，不要自动重试。",
    "LIBRARY_PREBOOK_UNCONFIRMED": "提交了但没能确认结果，把 message 原样告诉用户。不要重试（可能重复提交），请用户登录图书馆“我的图书馆 → 我的预借”查看（error.url）。",
    "WEBVPN_LOGIN_REQUIRED": "这个系统要经学校 WebVPN 访问（事务中心；服务器版的讲座也是），需要单独登录一次 WebVPN。界面会自动弹出登录卡片（扫码或账号密码，可能还要二次验证），简短地请用户完成，完成后你会接着做刚才的事；不要重试，绝对不要在聊天里索要密码或验证码。",
    "BOOTH_SLOT_UNAVAILABLE": "这个时段约不了。把 message 告诉用户，有 freeRanges 就据此建议别的时段，用户选好后重新预览。",
    "BOOTH_SLOT_REQUIRED": "重新调用 booth_reserve_preview（不传 start/end）和 booth_reserve_confirm，让用户在确认卡片上点选时段；不要在聊天里列出时段。",
    "BOOTH_NOT_ELIGIBLE": "用户当前被禁止预约（爽约或材料不合规），把 message 原样告诉用户，不要重试。",
    "BOOTH_GRADE_REQUIRED": "预约要填年级（入学年份，如 2024）。问用户一次，用 remember_user_info 记下来，再重新预览。",
    "BOOTH_RESERVE_UNCONFIRMED": "提交了但没能确认结果。不要重试（可能重复预约），请用户在 error.url 的“我的预约记录”里查看。",
    "BOOTH_REQUEST_FAILED": "事务中心系统拒绝了请求，把 message 原样告诉用户，不要自动重试。",
}


class CampusError(Exception):
    def __init__(self, code: str, message: str, *, hint: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            err["hint"] = self.hint
        if self.code in AGENT_HINTS:
            err["agentHint"] = AGENT_HINTS[self.code]
        err.update(self.details)
        return err
