// 爱小荔前端：无框架、无构建、无 CDN 依赖
"use strict";

const $ = (s) => document.querySelector(s);
const log = $("#log");
const input = $("#input");
const send = $("#send");

const TOOL_LABELS = {
  campus_events: "搜索校园活动", notice_search: "搜索公文通", notice_view: "阅读公告",
  lecture_list: "查看讲座", lecture_progress: "讲座学习进度", auth_status: "检查登录状态",
  sports_campuses: "查看校区", sports_projects: "查看运动项目", sports_slots: "查询可约时段",
  sports_fields: "查询空闲场地", sports_bookings: "查看我的预约",
  sports_reserve_preview: "生成预约预览", sports_reserve_confirm: "提交预约",
  sports_cancel_preview: "预览取消", sports_cancel_confirm: "取消预约", sports_balance: "查体育经费",
  electricity_buildings: "查看宿舍楼", electricity_query: "查询电费", electricity_purchases: "查询充值记录",
  course_today: "查询课表", library_search: "搜索馆藏", remember_user_info: "记住偏好",
  notice_read_many: "阅读公告正文", show_notice_board: "整理成通知面板",
  weather_today: "查询天气", electricity_purchases: "查询充值记录", show_recharge_card: "电费充值",
  library_book: "查看馆藏位置", library_prebook_preview: "生成预借预览", library_prebook_confirm: "提交预借",
  library_my_prebooks: "查看我的预借", library_prebook_cancel_preview: "预览取消预借", library_prebook_cancel_confirm: "取消预借",
  campus_guide: "查校园资料库", academic_calendar: "查校历", policy_search: "查评选细则", booth_list: "查看公共空间", booth_slots: "查询空间时段", booth_my_reservations: "查看空间预约",
  booth_reserve_preview: "生成空间预约预览", booth_reserve_confirm: "提交空间预约", booth_cancel_preview: "预览取消空间预约", booth_cancel_confirm: "取消空间预约",
};
const WEEK = "日一二三四五六";

// ---------------- 会话 ----------------
function safeStorage(fn, fallback) { try { return fn(); } catch { return fallback; } }
// 对话保存在服务器的 SQLite 里；这里只记住“上次打开的是哪个”，关掉浏览器再打开也能接着聊
let threadId = safeStorage(() => localStorage.getItem("szu-thread"), null) || newThreadId();
function newThreadId() {
  const id = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).slice(0, 36);
  return useThread(id);
}
function useThread(id) {
  safeStorage(() => localStorage.setItem("szu-thread", id));
  return id;
}
let busy = false;

// ---------------- 极简 Markdown（先转义，再加标签，不会注入 HTML） ----------------
function esc(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s（(：:])(https?:\/\/[^\s<）)，。]+)/g, '$1<a href="$2" target="_blank" rel="noopener">链接</a>');
}
function md(src) {
  const lines = src.replace(/\r/g, "").split("\n");
  const out = [];
  let list = null, para = [], table = [];
  const flushPara = () => { if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; };
  const flushList = () => { if (list) out.push(`</${list}>`); list = null; };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\|?\s*:?-{2,}/.test(r)).map((r) => r.replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim())));
    const [head, ...body] = rows;
    out.push(`<div class="table-wrap"><table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${
      body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    table = [];
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); flushList(); table.push(line.trim()); continue; }
    flushTable();
    const ul = line.match(/^\s*[-*•]\s+(.*)/), ol = line.match(/^\s*\d+[.)、]\s+(.*)/), h = line.match(/^#{1,4}\s+(.*)/);
    if (ul || ol) {
      flushPara();
      const kind = ul ? "ul" : "ol";
      if (list !== kind) { flushList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline((ul || ol)[1])}</li>`);
    } else if (h) { flushPara(); flushList(); out.push(`<p class="h">${inline(h[1])}</p>`); }
    else if (!line.trim() || /^-{3,}$/.test(line.trim())) { flushPara(); flushList(); }
    else { flushList(); para.push(line); }
  }
  flushPara(); flushList(); flushTable();
  return out.join("");
}

// ---------------- 渲染 ----------------
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}
function nearBottom() { return window.innerHeight + window.scrollY >= document.body.scrollHeight - 160; }
function scrollDown(force) { if (force || nearBottom()) window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }); }
// 打开对话 / 切回聊天：直接停在最新消息处。平滑滚动在卡片还在渲染（高度变化）时会停在半路，
// 所以立即跳到底，下一帧和卡片异步内容加载后再补一次（用户这期间自己滚动了就不再动）
function jumpToBottom() {
  const go = () => { window.scrollTo({ top: document.body.scrollHeight, behavior: "instant" }); return window.scrollY; };
  let y = go();
  requestAnimationFrame(() => { y = go(); });
  setTimeout(() => { if (Math.abs(window.scrollY - y) < 4) go(); }, 450);
}

function showChatStarted(when) {
  if (!$("#empty").hidden) {
    $("#empty").hidden = true;
    const d = when ? new Date(when) : new Date();
    log.append(el("div", "divider", `${d.getMonth() + 1}月${d.getDate()}日 星期${WEEK[d.getDay()]}`));
  }
}
function addUser(text) { showChatStarted(); const b = el("div", "bubble me"); b.textContent = text; log.append(b); }
function addAssistant(text = "") { const b = el("div", "bubble ai"); b.raw = text; b.innerHTML = md(text); log.append(b); return b; }
function addError(text) { log.append(el("div", "bubble err", esc(text))); }

function argSummary(name, a = {}) {
  if (name === "electricity_query" || name === "electricity_purchases") return [a.building, a.room].filter(Boolean).join(" ");
  if (name === "sports_reserve_preview" || name === "sports_fields") return [a.date, a.slot, a.field].filter(Boolean).join(" ");
  if (name === "remember_user_info") return a.value || "";
  if (name === "notice_read_many") return `${(a.notice_ids || []).length} 篇`;
  if (name === "show_notice_board") return a.topic || "";
  if (name === "weather_today") return a.campus || "";
  if (name === "booth_slots") return a.date || "";
  return a.query || a.keyword || a.project || a.campus || a.date || "";
}
function stepHtml(name, args, state) {
  const waiting = state === "run" && name.endsWith("_confirm"); // 写操作在等用户点确认，不是在跑
  const icon = waiting ? "•" : state === "run" ? '<span class="spin"></span>' : state === "ok" ? "✓" : "✕";
  const arg = waiting ? "等你确认" : argSummary(name, args);
  return `<span class="st">${icon}</span>${esc(TOOL_LABELS[name] || name)}${arg ? ` <span class="arg">· ${esc(String(arg))}</span>` : ""}`;
}

let thinkingEl = null;
function showThinking() {
  hideThinking();
  thinkingEl = el("div", "thinking", '<svg width="18" height="18"><use href="#lychee"/></svg>正在想…');
  log.append(thinkingEl); scrollDown();
}
function hideThinking() { thinkingEl?.remove(); thinkingEl = null; }

// ---------------- 通知面板（Generative UI：AI 用结构化参数描述，前端负责画） ----------------
const KIND_CLASS = { "申请/报名": "k-apply", "公示/结果": "k-result", "活动/讲座": "k-event", "安排/提醒": "k-remind", "通知": "k-info" };
function daysLeft(iso) {
  const d = new Date(iso + "T23:59:59"), now = new Date();
  return Math.ceil((d - now) / 86400000);
}
function dueLabel(iso) {
  const n = daysLeft(iso), md = `${+iso.slice(5, 7)}/${+iso.slice(8, 10)}`;
  if (n < 0) return { text: `${md} 已截止`, cls: "past" };
  if (n === 0) return { text: `今天截止`, cls: "urgent" };
  return { text: `${md} 截止 · 还剩 ${n} 天`, cls: n <= 3 ? "urgent" : n <= 7 ? "soon" : "" };
}
const BOARD_VISIBLE = 3; // 默认只展开前 3 条，避免一个面板把聊天记录挤出屏幕

function noticeCardHtml(c) {
  const d = c.deadline ? dueLabel(c.deadline) : null;
  const safeUrl = /^https?:\/\//.test(c.url || "") ? c.url : null;
  return `<article class="ncard">
    <div class="ncard-tags">
      <span class="tag ${KIND_CLASS[c.kind] || "k-info"}">${esc(c.kind || "通知")}</span>
      ${c.audience ? `<span class="tag">${esc(c.audience)}</span>` : ""}
      ${d ? `<span class="due ${d.cls}">${esc(d.text)}</span>` : ""}
    </div>
    <h4>${safeUrl ? `<a href="${esc(safeUrl)}" target="_blank" rel="noopener">${esc(c.title)}</a>` : esc(c.title)}</h4>
    ${c.summary ? `<p class="ncard-sum">${esc(c.summary)}</p>` : ""}
    ${(c.key_points || []).length ? `<ul>${c.key_points.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}
    ${c.action ? `<div class="ncard-action">${esc(c.action)}</div>` : ""}
    ${/^\d+$/.test(c.id || "") ? `<div class="ncard-files" data-id="${esc(c.id)}"></div>` : ""}
    <div class="ncard-meta">${esc([c.publisher, c.date].filter(Boolean).join(" · "))}${safeUrl ? ` · <a href="${esc(safeUrl)}" target="_blank" rel="noopener">原文</a>` : ""}</div>
  </article>`;
}

function groupsHtml(groups) {
  return groups.map((g) => `
    <section class="board-group">
      <h3>${esc(g.name)}<span>${g.items.length}</span></h3>
      ${g.items.map(noticeCardHtml).join("")}
    </section>`).join("");
}

// 附件：卡片真正显示出来时才去取（折叠里的等展开再取），由本机后端带着登录凭证代为下载
const PAPERCLIP = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M21 11.5 12.5 20a5 5 0 0 1-7-7l8.5-8.5a3.5 3.5 0 0 1 5 5L10.5 18a2 2 0 0 1-3-3l7.5-7.5"/></svg>';
function loadAttachments(root) {
  root.querySelectorAll(".ncard-files:not([data-loaded])").forEach(async (box) => {
    box.dataset.loaded = "1";
    const r = await fetch(`/api/notices/${box.dataset.id}/attachments`).then((x) => x.json()).catch(() => null);
    const files = r?.ok ? r.attachments : [];
    if (!files.length) return box.remove();
    box.innerHTML = `<span class="files-label">附件</span>${files.map((f) =>
      `<a class="file" href="/api/notices/attachment?url=${encodeURIComponent(f.url)}&name=${encodeURIComponent(f.name)}&id=${encodeURIComponent(box.dataset.id)}" download="${esc(f.name)}" title="下载 ${esc(f.name)}">${PAPERCLIP}<span>${esc(f.name)}</span></a>`).join("")}`;
  });
}

function addNoticeBoard(a) {
  showChatStarted();
  const groups = (a.groups || []).map((g) => ({ name: g.name, items: g.items || [] })).filter((g) => g.items.length);
  // 按分组顺序（AI 已把最重要的放前面）取前 3 条展示，剩下的保留原分组放进折叠区
  const shown = [], rest = [];
  let budget = BOARD_VISIBLE;
  for (const g of groups) {
    const head = g.items.slice(0, Math.max(budget, 0)), tail = g.items.slice(head.length);
    budget -= head.length;
    if (head.length) shown.push({ name: g.name, items: head });
    if (tail.length) rest.push({ name: g.name, items: tail });
  }
  const hidden = rest.reduce((n, g) => n + g.items.length, 0);
  const dues = groups.flatMap((g) => g.items).filter((c) => c.deadline).sort((x, y) => x.deadline.localeCompare(y.deadline));
  const upcoming = dues.filter((c) => daysLeft(c.deadline) >= 0);
  const tl = (upcoming.length ? upcoming : dues).slice(0, 3);

  const board = el("div", "board");
  board.innerHTML = `
    <div class="board-head">
      <svg width="26" height="26"><use href="#lychee"/></svg>
      <div><div class="board-topic">${esc(a.topic || "通知")}</div><p class="board-summary">${esc(a.summary || "")}</p></div>
    </div>
    ${tl.length ? `<div class="timeline"><div class="tl-title">截止日期${dues.length > tl.length ? `<span>另有 ${dues.length - tl.length} 个</span>` : ""}</div>${tl.map((c) => {
      const d = dueLabel(c.deadline);
      return `<div class="tl-item ${d.cls}"><span class="tl-dot"></span><span class="tl-date">${esc(d.text)}</span><span class="tl-name">${esc(c.title)}</span></div>`;
    }).join("")}</div>` : ""}
    ${groupsHtml(shown)}
    ${hidden ? `<details class="board-more"><summary><span class="more-open">展开其余 ${hidden} 条</span><span class="more-close">收起</span></summary>${groupsHtml(rest)}</details>` : ""}
    ${a.tip ? `<p class="board-tip">${esc(a.tip)}</p>` : ""}`;
  log.append(board);
  // 先把折叠区里的标记为“稍后”，再加载可见卡片的附件
  board.querySelectorAll(".board-more .ncard-files").forEach((x) => x.dataset.loaded = "later");
  loadAttachments(board);
  board.querySelector(".board-more")?.addEventListener("toggle", (e) => {
    if (!e.target.open) return;
    e.target.querySelectorAll('.ncard-files[data-loaded="later"]').forEach((x) => delete x.dataset.loaded);
    loadAttachments(e.target);
  });
}

// ---------------- 付款卡片 ----------------
// 付款必须在学校页面完成（支付页禁止被嵌入、密码用随机数字键盘），这里只负责：显示余额、给出入口、自动确认是否付款成功
const PAY_POLL_MS = 10_000, PAY_POLL_MAX = 180; // 最多盯 30 分钟
// replace：传入要被替换的元素（确认卡片），付款卡片原地顶替它，界面上只留一张
function addPayCard(b, replace = null) {
  showChatStarted();
  const card = el("div", "paycard");
  card.innerHTML = `
    <div class="confirm-head"><svg width="20" height="20"><use href="#lychee"/></svg><span class="pay-title">待支付</span></div>
    <dl>
      <dt>项目</dt><dd>${esc([b.campus, b.project].filter(Boolean).join(" · "))}</dd>
      <dt>场地</dt><dd>${esc(b.field || "")}</dd>
      <dt>时间</dt><dd>${esc([b.date, b.slot].filter(Boolean).join(" "))}</dd>
      <dt>订单号</dt><dd class="num">${esc(b.orderNo)}</dd>
    </dl>
    <div class="pay-money">正在查询体育经费余额…</div>
    <p class="note">未支付的预约会被学校自动取消。点下面的按钮会打开学校的「我的预约」，找到这笔订单点「支付」；查询密码只在学校页面上输入。</p>
    <div class="actions">
      <a class="btn primary pay-go" target="_blank" rel="noopener" href="https://ehall.szu.edu.cn/qljfwapp/sys/lwSzuCgyy/index.do#/myBooking">去学校页面付款</a>
      <button class="btn ghost pay-cancel" type="button">取消预约</button>
      <button class="btn ghost pay-cancel-no" type="button" hidden>算了</button>
    </div>
    <p class="pay-cancel-err" hidden></p>
    <div class="pay-status"><span class="spin"></span>等待付款，每 10 秒自动检查一次</div>`;
  if (replace && document.body.contains(replace)) replace.replaceWith(card); else log.append(card);
  scrollDown(true);
  bindPayCancel(card, b.orderNo);
  let n = 0;
  const check = async () => {
    if (!document.body.contains(card) || card.dataset.final) return; // 新对话清空了界面 / 已有结果
    const r = await fetch(`/api/sports/payment/${encodeURIComponent(b.orderNo)}`).then((x) => x.json()).catch(() => null);
    if (r?.ok) {
      const price = r.referencePriceCny, fund = r.sportsFundCny;
      const enough = price == null || fund >= price;
      card.querySelector(".pay-money").innerHTML =
        `${price != null ? `参考价格 <strong>${price} 元</strong>（同项目上次实付）· ` : ""}体育经费余额 <strong>${fund} 元</strong>` +
        (enough ? "" : `<span class="pay-warn">不够，支付时请选「校园卡」</span>`);
      if (r.payUrl) card.querySelector(".pay-go").href = r.payUrl;
      if (r.payDeadline) {
        const left = Math.floor((new Date(r.payDeadline.replace(" ", "T")) - new Date()) / 60000);
        const hm = r.payDeadline.slice(11, 16);
        card.querySelector(".note").innerHTML = left > 0
          ? `<strong class="${left <= 10 ? "pay-urgent" : ""}">请在 ${hm} 前付款（还剩 ${left} 分钟）</strong>，超时学校会自动取消。点下面的按钮打开学校的「我的预约」，找到这笔订单点「支付」；查询密码只在学校页面上输入。`
          : `已过付款截止时间 ${hm}，学校可能已经取消了这笔预约。`;
      }
      const bk = r.booking;
      if (!bk.cancellable) card.querySelector(".pay-cancel")?.remove(); // 学校显示已不能取消
      if (bk.paid) return payFinal(card, "paid", `已支付${bk.amountCny ? ` ${bk.amountCny} 元` : ""}，到时间直接去就行`);
      if (bk.cancelled) return payFinal(card, "cancelled", bk.payState || "预约已取消");
    } else if (r?.error) {
      card.querySelector(".pay-status").innerHTML = `<span class="spin"></span>${esc(r.error.message)}，稍后重试`;
    }
    if (++n < PAY_POLL_MAX) setTimeout(check, PAY_POLL_MS);
    else card.querySelector(".pay-status").textContent = "已停止自动检查，可以问我“我的预约付了没有”";
  };
  check();
}
// 取消按钮：点第一下变成“确认取消？”，第二下才真的取消（不能撤回，防止手滑）。不经过 AI，直接调用后端
function bindPayCancel(card, orderNo) {
  const btn = card.querySelector(".pay-cancel"), no = card.querySelector(".pay-cancel-no"), err = card.querySelector(".pay-cancel-err");
  if (!btn) return;
  let armed = false, timer = null;
  const disarm = () => { armed = false; btn.textContent = "取消预约"; btn.classList.remove("danger"); no.hidden = true; clearTimeout(timer); };
  no.addEventListener("click", disarm);
  btn.addEventListener("click", async () => {
    if (!armed) {
      armed = true; btn.textContent = "确认取消？"; btn.classList.add("danger"); no.hidden = false; err.hidden = true;
      timer = setTimeout(disarm, 5000); // 5 秒内没确认就恢复原状
      return;
    }
    clearTimeout(timer);
    btn.disabled = true; no.hidden = true; btn.textContent = "取消中…";
    const r = await fetch(`/api/sports/cancel/${encodeURIComponent(orderNo)}`, { method: "POST" }).then((x) => x.json()).catch(() => null);
    if (r?.ok) return payFinal(card, "cancelled", r.verified === false ? "已提交取消，学校那边状态还没更新，稍后可以问我确认" : "已取消预约，名额已释放");
    btn.disabled = false; disarm();
    err.textContent = r?.error?.message || "取消失败，请稍后再试";
    err.hidden = false;
  });
}

function payFinal(card, kind, text) {
  card.dataset.final = kind;
  card.classList.add(kind);
  card.querySelector(".pay-title").textContent = kind === "paid" ? "✓ 已支付" : "预约已取消";
  card.querySelector(".actions")?.remove();
  card.querySelector(".note")?.remove();
  card.querySelector(".pay-cancel-err")?.remove();
  card.querySelector(".pay-status").textContent = text;
}

// ---------------- 电费充值卡片 ----------------
// 付款只能在微信里完成（JSAPI 支付），卡片给出充值方案和微信扫码入口
async function addRechargeCard(a) {
  showChatStarted();
  const card = el("div", "rechargecard");
  card.innerHTML = `<div class="confirm-head"><svg width="20" height="20"><use href="#lychee"/></svg>宿舍电费充值</div><div class="rc-body"><span class="spin"></span></div>`;
  log.append(card); scrollDown(true);
  const q = new URLSearchParams({ building: a.building || "", room: a.room || "" });
  const r = await fetch(`/api/electricity/recharge?${q}`).then((x) => x.json()).catch(() => null);
  if (!r?.ok) { card.querySelector(".rc-body").innerHTML = '<p class="note">充值信息暂时取不到</p>'; return; }
  const low = r.daysLeft != null ? r.daysLeft < 3 : r.remainingKwh != null && r.remainingKwh < 20;
  const status = r.remainingKwh == null
    ? `<p class="rc-status">${esc([r.building, r.room].filter(Boolean).join(" "))}${r.error ? ` · 剩余电量暂时查不到（${esc(r.error.message)}）` : ""}</p>`
    : `<p class="rc-status ${low ? "low" : ""}">${esc(r.building)} ${esc(r.room)} · 剩余 <strong>${r.remainingKwh} 度</strong>` +
      (r.dailyKwh ? `，按日均 ${r.dailyKwh} 度大约还能用 <strong>${r.daysLeft} 天</strong>` : "") + `</p>`;
  const opts = r.options.map((o) => `<li><strong>${o.amountCny} 元</strong><span>≈ ${o.kwh} 度</span>${o.extraDays != null ? `<span>多用 ${o.extraDays} 天</span>` : ""}</li>`).join("");
  card.querySelector(".rc-body").outerHTML = `
    ${status}
    <div class="rc-main">
      <ul class="rc-options">${opts}</ul>
      <div class="rc-qr">${r.qrSvg}<span>微信扫一扫去充值</span></div>
    </div>
    <p class="rc-steps">扫码进不去的话：${esc(r.paySteps)}</p>
    <p class="note">电价约 ${r.pricePerKwh} 元/度。付款在微信里完成（学校的充值页只支持微信支付），这里不会下单，也不接触任何支付信息。</p>`;
  scrollDown();
}

// 确认卡片：展示服务端预览，而不是模型的转述
function confirmRows(action) {
  const p = action.preview;
  if (!p) return [["提示", "没有找到对应的预览，服务端会拒绝这个操作"]];
  if (action.tool === "library_prebook_cancel_confirm")
    return [["图书", p.title], ["取书地点", p.pickup], ["提交日期", p.submittedAt], ["状态", p.status]].filter(([, v]) => v);
  if (action.tool === "library_prebook_confirm")  // 取书地点在下面用按钮选
    return [["图书", p.book], ["通知手机", p.phone], ["通知邮箱", p.email]].filter(([, v]) => v);
  if (action.tool === "booth_reserve_confirm")  // 时段在下面的格子里点选，这一行跟着选择更新
    return [["空间", p.booth], ["位置", p.location], ["日期", p.date], ["时段", p.time || "在下面点选"], ["年级", p.grade], ["联系手机", p.phone]].filter(([, v]) => v);
  if (action.tool === "booth_cancel_confirm")
    return [["空间", p.boothName], ["日期", p.date], ["时段", (p.slots || []).join("、")], ["状态", p.statusName || p.status]].filter(([, v]) => v);
  if (action.tool === "sports_cancel_confirm")
    return [["项目", [p.campus, p.project].filter(Boolean).join(" · ")], ["场地", p.field], ["时间", p.timeRange],
            ["状态", [p.status, p.payState].filter(Boolean).join(" / ")], ["订单号", p.orderNo]].filter(([, v]) => v);
  const slot = p.selected || {};
  return [["校区", p.campus], ["项目", p.venue], ["方式", p.mode], ["场地", p.field], ["日期", p.date], ["时段", slot.label], ["剩余", slot.remaining]]
    .filter(([, v]) => v != null && v !== "");
}
function addConfirm(payload) {
  for (const action of payload.actions || []) {
    const isCancel = action.tool === "sports_cancel_confirm" || action.tool === "booth_cancel_confirm";
    const isBooth = action.tool === "booth_reserve_confirm";
    const isPrebook = action.tool === "library_prebook_confirm";
    const isPrebookCancel = action.tool === "library_prebook_cancel_confirm";
    const head = isPrebookCancel ? "确认取消这条预借？" : isPrebook ? "确认预借这本书" : isCancel ? "确认取消这条预约？" : "确认预约";
    const note = isPrebookCancel ? "取消后馆员不会再为你取书；之后还需要的话可以重新预借。"
      : isPrebook ? "馆员会把书送到取书地点。收到到书通知后请在 3 天内去取，违约三次会被暂停预借资格 30 天。"
      : isBooth ? "提交后立即生效。请在预约时段内入场，预约结束仍未入场算爽约，一个月内爽约三次会被禁约 30 天。"
      : isCancel ? (action.preview?.paid ? "这笔预约已经付过款，退款以学校规则为准。取消后名额会释放给其他同学。" : "取消后名额会释放给其他同学。")
      : "提交后会弹出付款卡片，需要在学校页面完成支付。";
    const card = el("div", "confirm");
    card.innerHTML = `
      <div class="confirm-head"><svg width="20" height="20"><use href="#lychee"/></svg>${head}</div>
      <dl>${confirmRows(action).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join("")}</dl>
      ${pickupChooser(action)}
      ${slotChooser(action)}
      <p class="note">${note}</p>
      <div class="actions">
        <button class="btn primary" data-ok="1">${isPrebook ? "确认预借" : isCancel || isPrebookCancel ? "确认取消" : "确认预约"}</button>
        <button class="btn ghost" data-ok="0">再想想</button>
      </div>`;
    const ok = card.querySelector("[data-ok='1']");
    const picks = card.querySelectorAll(".choice button");
    if (picks.length) {
      ok.disabled = !card.querySelector(".choice button.on");
      picks.forEach((b) => b.addEventListener("click", () => {
        picks.forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-checked", x === b); });
        ok.disabled = false;
      }));
    }
    const slotPick = isBooth ? wireSlotChooser(card, action.preview || {}, ok) : null;
    card.querySelectorAll(".actions button").forEach((b) => b.addEventListener("click", () => {
      if (b.disabled) return;
      if (slotPick) return decide(card, b.dataset.ok === "1", { slots: slotPick() });
      const picked = card.querySelector(".choice button.on");
      decide(card, b.dataset.ok === "1", picked ? { pickup: picked.dataset.value } : {});
    }));
    log.append(card);
  }
  scrollDown(true);
}
// 预借：取书地点在确认卡片里点选，选择结果随“确认”一起交给后端，由图写进工具调用（不经过 AI）
function pickupChooser(action) {
  const p = action.preview || {};
  if (action.tool !== "library_prebook_confirm" || !p.pickupOptions?.length) return "";
  return `<div class="choice-label">取书地点</div><div class="choice" role="radiogroup" aria-label="取书地点">${
    p.pickupOptions.map((o) => `<button type="button" role="radio" data-value="${esc(o.value)}" aria-checked="${o.value === p.pickupId}"
      class="${o.value === p.pickupId ? "on" : ""}">${esc(o.name)}</button>`).join("")}</div>`;
}

// 公共空间：像学校页面一样点选半小时格子（连续、最多 maxSlotsPerDay 格）。选择随“确认”交给后端，由图写进工具调用
function slotChooser(action) {
  const p = action.preview || {};
  if (action.tool !== "booth_reserve_confirm" || !p.freeSlots?.length) return "";
  const sel = new Set(p.selectedSlots || []);
  return `<div class="choice-label">选择时段（半小时一格，连续最多 ${p.maxSlotsPerDay || 4} 格）</div>
    <div class="slot-grid" role="group" aria-label="可约时段">${p.freeSlots.map((s) =>
      `<button type="button" data-i="${s.index}" class="${sel.has(s.index) ? "on" : ""}" aria-pressed="${sel.has(s.index)}"
        title="${esc(s.start)}-${esc(s.end)}">${esc(s.start)}</button>`).join("")}</div>`;
}
function wireSlotChooser(card, p, okBtn) {
  const slots = new Map((p.freeSlots || []).map((s) => [s.index, s]));
  const max = p.maxSlotsPerDay || 4;
  let sel = (p.selectedSlots || []).filter((i) => slots.has(i)).sort((a, b) => a - b);
  const timeCell = [...card.querySelectorAll("dt")].find((d) => d.textContent === "时段")?.nextElementSibling;
  const render = () => {
    card.querySelectorAll(".slot-grid button").forEach((b) => {
      const on = sel.includes(+b.dataset.i);
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", on);
    });
    if (timeCell) timeCell.textContent = sel.length ? `${slots.get(sel[0]).start}-${slots.get(sel[sel.length - 1]).end}` : "在下面点选";
    okBtn.disabled = !sel.length;
  };
  card.querySelectorAll(".slot-grid button").forEach((b) => b.addEventListener("click", () => {
    if (b.disabled) return;
    const i = +b.dataset.i, lo = sel[0], hi = sel[sel.length - 1];
    if (!sel.length) sel = [i];
    else if (i === lo || i === hi) sel = sel.length === 1 ? [] : sel.filter((x) => x !== i);  // 点两头：缩短
    else if ((i === lo - 1 || i === hi + 1) && sel.length < max) sel = [...sel, i].sort((a, b) => a - b);  // 紧挨着：延长
    else sel = [i];  // 不相邻 / 已到上限：从这一格重新选
    render();
  }));
  render();
  return () => { card.querySelectorAll(".slot-grid button").forEach((b) => { b.disabled = true; }); return sel; };
}

let confirmingCard = null; // 用户刚点了“确认”、正在提交的那张卡片
async function decide(card, approved, extra = {}) {
  if (busy) return;
  card.querySelectorAll(".choice button").forEach((b) => { b.disabled = true; });
  card.querySelector(".actions").outerHTML =
    `<div class="result ${approved ? "ok" : ""}">${approved ? "✓ 已确认，正在提交…" : "已取消，你可以告诉我想怎么调整"}</div>`;
  confirmingCard = approved ? card : null;
  await runTurn("/api/resume", { thread_id: threadId, approved, ...(approved ? extra : {}) });
  // 预约成功时这张卡片已经被付款卡片替换掉了；还在的话说明是取消预约，或者提交没成功
  if (approved && document.body.contains(card)) {
    const r = card.querySelector(".result");
    if (card.dataset.outcome === "ok") r.textContent = "✓ 已完成";
    else { r.className = "result"; r.textContent = "没有成功，原因见下方说明"; }
  }
  confirmingCard = null;
}

// ---------------- SSE ----------------
// 返回 true 表示正常收到了 done；false 表示连接中途断了（对话仍在后台继续）
async function streamPost(url, body, onEvent) {
  let res;
  try {
    res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch (e) { onEvent({ type: "error", message: "连不上服务，确认 szu-agent-web 还在运行" }); return true; }
  if (res.status === 409) { const j = await res.json().catch(() => ({})); onEvent({ type: "error", message: j.detail || "上一条消息还在处理中" }); return true; }
  if (!res.ok) { onEvent({ type: "error", message: `请求失败（${res.status}）` }); return true; }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "", gotDone = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data: ")) continue; // 以冒号开头的是心跳，忽略
          const ev = JSON.parse(line.slice(6));
          if (ev.type === "done") gotDone = true;
          onEvent(ev);
        }
      }
    }
  } catch (e) { /* 连接被中间的代理断开 */ }
  return gotDone;
}

// 连接断了：对话在服务器后台继续跑，等它结束后整段重新加载
async function recoverAfterDisconnect() {
  const note = el("div", "thinking", '<svg width="18" height="18"><use href="#lychee"/></svg>连接中断了，后台还在处理，完成后会自动显示…');
  log.append(note); scrollDown(true);
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    const h = await fetch(`/api/history/${encodeURIComponent(threadId)}`).then((r) => r.json()).catch(() => null);
    if (h && !h.running) break;
  }
  note.remove();
  log.innerHTML = "";
  await restore();
}

async function runTurn(url, body) {
  busy = true; updateSend();
  let bubble = null, steps = null, touched = new Set(), needLogin = false, mfaTarget = null, needContact = false, needVpn = false;
  const stepEls = {}, pendingBoards = {};
  showThinking();
  const completed = await streamPost(url, body, (ev) => {
    switch (ev.type) {
      case "token":
        hideThinking();
        if (!bubble) { bubble = addAssistant(); steps = null; }
        bubble.raw += ev.text; bubble.innerHTML = md(bubble.raw);
        break;
      case "message":
        hideThinking();
        if (bubble) { bubble.raw = ev.text; bubble.innerHTML = md(ev.text); } else addAssistant(ev.text);
        bubble = null; steps = null;
        break;
      case "tool_start":
        hideThinking();
        bubble = null;
        if (ev.name === "show_notice_board" || ev.name === "show_recharge_card") { pendingBoards[ev.id] = { name: ev.name, args: ev.args }; break; }
        if (!steps) { steps = el("div", "steps"); log.append(steps); }
        stepEls[ev.id] = el("span", "step", stepHtml(ev.name, ev.args, "run"));
        stepEls[ev.id].args = ev.args;
        steps.append(stepEls[ev.id]);
        touched.add(ev.name);
        break;
      case "tool_end": {
        if (ev.id in pendingBoards) {
          // 参数没通过校验时 ok=false，AI 会修正后重试，所以失败的这次不画
          if (ev.ok) {
            const pb = pendingBoards[ev.id];
            if (pb.name === "show_recharge_card") addRechargeCard(pb.args); else addNoticeBoard(pb.args);
            steps = null;
          }
          delete pendingBoards[ev.id];
          showThinking();
          break;
        }
        const s = stepEls[ev.id];
        if (s) { s.className = `step ${ev.ok ? "ok" : "fail"}`; s.innerHTML = stepHtml(ev.name, s.args, ev.ok ? "ok" : "fail"); }
        if (!Object.values(stepEls).some((x) => x.className === "step")) showThinking();
        if (s) delete stepEls[ev.id];
        if (ev.code === "LOGIN_REQUIRED") needLogin = true;
        if (ev.code === "WEBVPN_LOGIN_REQUIRED") needVpn = true;
        if (ev.code === "CONTACT_REQUIRED") needContact = true;
        if (ev.booking) addPayCard(ev.booking, confirmingCard);
        else if (confirmingCard && /_confirm$/.test(ev.name)) confirmingCard.dataset.outcome = ev.ok ? "ok" : "fail";
        if (ev.code === "MFA_REQUIRED") mfaTarget = ev.target || mfaTarget;
        break;
      }
      case "confirm": hideThinking(); addConfirm(ev); break;
      case "error": hideThinking(); addError(ev.message); break;
      case "done": hideThinking(); break;
    }
    scrollDown();
  });
  hideThinking();
  if (!completed) await recoverAfterDisconnect();
  busy = false; updateSend();
  if (needLogin) addLoginCard({ resume: true });
  else if (needVpn) addLoginCard({ resume: true, realm: "webvpn" });
  else if (mfaTarget) addMfaCard({ target: mfaTarget, resume: true });
  else if (needContact) addContactCard({ resume: true });
  if (touched.has("remember_user_info") || touched.has("electricity_query") || touched.has("sports_reserve_confirm")) loadToday();
}

async function ask(text) {
  text = text.trim();
  if (!text || busy) return;
  if (multiuser && !auth.appSession) { switchView("chat"); addLoginCard({ resume: false }); return; } // 输入框里的字留着，登录后再发
  // 还没点的确认卡片在发新消息后作废（后端会把那次操作记为“未确认、未执行”）
  log.querySelectorAll(".confirm .actions").forEach((a) => {
    a.outerHTML = '<div class="result">已失效：你发了新消息，这个操作没有执行</div>';
  });
  input.value = ""; autosize();
  addUser(text); scrollDown(true);
  await runTurn("/api/chat", { thread_id: threadId, message: text });
  input.focus();
}

// ---------------- 恢复历史 ----------------
async function restore() {
  const res = await fetch(`/api/history/${encodeURIComponent(threadId)}`).catch(() => null);
  if (!res?.ok) return;
  const { messages, pending, running } = await res.json();
  if (!messages.length) return;
  if (running && !busy) setTimeout(async () => { busy = true; updateSend(); await recoverAfterDisconnect(); busy = false; updateSend(); }, 0);
  showChatStarted(messages.find((m) => m.sentAt)?.sentAt);
  let steps = null;
  for (const m of messages) {
    if (m.role === "compact") { log.append(el("div", "divider", "以上较早的内容已整理成摘要，助手仍记得要点")); steps = null; }
    else if (m.role === "user") { addUser(m.text); steps = null; }
    else if (m.role === "assistant") { addAssistant(m.text); steps = null; }
    else if (m.role === "tool" && m.name === "show_notice_board") {
      if (m.ok) { addNoticeBoard(m.args); steps = null; }
    }
    else if (m.role === "tool" && m.name === "show_recharge_card") {
      if (m.ok) { addRechargeCard(m.args); steps = null; }
    }
    else if (m.role === "tool") {
      if (!steps) { steps = el("div", "steps"); log.append(steps); }
      const state = m.ok === undefined ? "run" : m.ok ? "ok" : "fail";
      steps.append(el("span", `step ${state === "run" ? "" : state}`, stepHtml(m.name, m.args, state)));
      if (m.booking) { addPayCard(m.booking); steps = null; }
    }
  }
  if (pending) addConfirm(pending);
  jumpToBottom();
}

// ---------------- 左侧：时钟与今日 ----------------
function tick() {
  const d = new Date();
  $("#clockDow").textContent = `周${WEEK[d.getDay()]}`;
  $("#clockDate").textContent = `${d.getMonth() + 1}月${d.getDate()}日`;
  $("#clockTime").textContent = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const h = d.getHours();
  $("#greeting").textContent = `${h < 5 ? "夜深了" : h < 11 ? "早上好" : h < 14 ? "中午好" : h < 18 ? "下午好" : "晚上好"}，今天想做点什么？`;
}

// 左侧“今日荔园”：校区天气。天气也会传给背景（阴天云更多更灰，下雨有雨丝）
const WEATHER_ICON = {
  clear: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>',
  partly: '<circle cx="9" cy="9" r="3.4"/><path d="M9 2.8v1.4M2.8 9h1.4M4.6 4.6l1 1M13.4 4.6l-1 1"/><path d="M8.5 19.5h9a3.3 3.3 0 0 0 .3-6.6 4.6 4.6 0 0 0-8.8 1.1 2.8 2.8 0 0 0-.5 5.5z"/>',
  cloudy: '<path d="M7 18.5h10.5a3.8 3.8 0 0 0 .4-7.6 5.4 5.4 0 0 0-10.3 1.3A3.2 3.2 0 0 0 7 18.5z"/>',
  overcast: '<path d="M7 18.5h10.5a3.8 3.8 0 0 0 .4-7.6 5.4 5.4 0 0 0-10.3 1.3A3.2 3.2 0 0 0 7 18.5z"/><path d="M5 8.5a4 4 0 0 1 6.5-2.3"/>',
  fog: '<path d="M4 9h16M3 13h18M5 17h14"/>',
  drizzle: '<path d="M7 14.5h10.5a3.8 3.8 0 0 0 .4-7.6 5.4 5.4 0 0 0-10.3 1.3A3.2 3.2 0 0 0 7 14.5z"/><path d="M9 17.5l-.6 1.5M13 17.5l-.6 1.5M17 17.5l-.6 1.5"/>',
  rain: '<path d="M7 13.5h10.5a3.8 3.8 0 0 0 .4-7.6 5.4 5.4 0 0 0-10.3 1.3A3.2 3.2 0 0 0 7 13.5z"/><path d="M8.5 16l-1.2 3.5M12.5 16l-1.2 3.5M16.5 16l-1.2 3.5"/>',
  storm: '<path d="M7 13h10.5a3.8 3.8 0 0 0 .4-7.6 5.4 5.4 0 0 0-10.3 1.3A3.2 3.2 0 0 0 7 13z"/><path d="M12.5 14.5l-2 3.5h3l-2 3.5"/>',
  snow: '<path d="M7 13h10.5a3.8 3.8 0 0 0 .4-7.6 5.4 5.4 0 0 0-10.3 1.3A3.2 3.2 0 0 0 7 13z"/><path d="M9 17v3M7.7 18.5h2.6M15 17v3M13.7 18.5h2.6"/>',
};
async function loadToday() {
  const box = $("#weather");
  const w = await fetch("/api/weather").then((r) => r.json()).catch(() => null);
  if (!w?.ok) {
    box.innerHTML = '<div class="w-sub">天气暂时取不到</div>';
    return;
  }
  window.LycheeScene?.setWeather({ kind: w.kind, cloud: w.cloud });
  // 极简：只保留天气、温度、温度区间、体感、湿度（下雨提醒交给 AI 回答“要不要带伞”）
  box.innerHTML = `
    <div class="w-now">
      <svg class="w-icon" viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${WEATHER_ICON[w.kind] || WEATHER_ICON.cloudy}</svg>
      <span class="w-temp">${w.tempC}°</span>
      <span class="w-desc">${esc(w.desc)}</span>
    </div>
    <div class="w-sub">${w.todayMin}° ~ ${w.todayMax}° · 体感 ${w.feelsC}° · 湿度 ${w.humidity}%</div>`;
  box.title = `${w.campus} · ${w.updatedAt} 更新`;  // 鼠标悬停才显示来源和时间
}
setInterval(loadToday, 15 * 60 * 1000);

// ---------------- 记忆视图 ----------------
async function loadMemory() {
  renderAccount();
  const list = $("#memoryList");
  const res = await fetch("/api/profile").catch(() => null);
  const { items = [] } = res?.ok ? await res.json() : {};
  list.innerHTML = items.length ? "" : '<div class="mem-empty">还没有记住任何事。<br>聊天时告诉我你住哪栋、常去哪个校区运动，我会记下来。</div>';
  for (const it of items) {
    const row = el("div", "mem", `<span class="k">${esc(it.label)}</span><span class="v">${esc(it.value)}</span><button>忘掉</button>`);
    row.querySelector("button").onclick = async () => {
      await fetch(`/api/profile/${it.key}`, { method: "DELETE" });
      loadMemory(); loadToday();
    };
    list.append(row);
  }
}

function switchView(view) {
  document.querySelectorAll(".seg button").forEach((b) => {
    const on = b.dataset.view === view;
    b.classList.toggle("on", on); b.setAttribute("aria-selected", on);
  });
  $("#chatView").hidden = view !== "chat";
  $("#memoryView").hidden = view !== "memory";
  $("#historyView").hidden = view !== "history";
  $("#composer").hidden = view !== "chat";
  if (view === "memory") loadMemory();
  if (view === "history") loadThreads();
  if (view === "chat") jumpToBottom();
}

// ---------------- 历史对话 ----------------
function ago(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const days = Math.round((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days === 0) return `今天 ${hm}`;
  if (days === 1) return `昨天 ${hm}`;
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

async function loadThreads() {
  const list = $("#threadList");
  const res = await fetch("/api/threads").then((r) => r.json()).catch(() => null);
  const items = res?.items || [];
  if (!items.length) { list.innerHTML = '<div class="mem-empty">还没有对话</div>'; return; }
  list.innerHTML = "";
  for (const t of items) {
    const row = el("div", `mem thread${t.id === threadId ? " current" : ""}`);
    row.innerHTML = `<span class="v">${esc(t.title)}</span><span class="k">${ago(t.updatedAt)}</span><button aria-label="删除这个对话">删除</button>`;
    row.querySelector(".v").addEventListener("click", () => openThread(t.id));
    row.querySelector(".k").addEventListener("click", () => openThread(t.id));
    row.querySelector("button").addEventListener("click", async () => {
      if (!confirm(`删除对话“${t.title}”？删除后无法恢复。`)) return;
      const r = await fetch(`/api/threads/${encodeURIComponent(t.id)}`, { method: "DELETE" }).catch(() => null);
      if (!r?.ok) return;
      if (t.id === threadId) resetChat();
      loadThreads();
    });
    list.append(row);
  }
}

function resetChat() {
  threadId = newThreadId();
  log.innerHTML = ""; $("#empty").hidden = false;
}

async function openThread(id) {
  if (busy) return;
  threadId = useThread(id);
  log.innerHTML = ""; $("#empty").hidden = false;
  switchView("chat");
  await restore();
  jumpToBottom();
}

// ---------------- 登录 ----------------
// 密码只通过 /api/auth/password 发给本机后端，再由后端发给深大统一认证；不会进入对话，也不会发给 AI 模型。
let auth = { loggedIn: false };
let multiuser = false; // 部署到服务器给多人用：先用学校统一认证登录爱小荔，每个人的记忆和历史分开
let activeLogin = null; // 同一时间只保留一张可用的登录卡片

async function refreshAuth() {
  const res = await fetch("/api/auth").catch(() => null);
  auth = res?.ok ? await res.json() : { loggedIn: false };
  $("#accountBtn").classList.toggle("in", !!auth.loggedIn);
  $("#accountBtn").title = auth.loggedIn ? "已登录深大统一认证" : "未登录，点击登录";
  return auth;
}

// realm="webvpn"：事务中心的系统挂在学校 WebVPN 上，要在 WebVPN 的认证入口单独登录一次，
// 登录后学校通常还要二次验证——在这张卡片里原地接着验证（必须是同一个会话，见 cas.CasClient）
function addLoginCard({ resume, realm = "main" }) {
  if (activeLogin && document.body.contains(activeLogin.card)) { activeLogin.card.scrollIntoView({ behavior: "smooth", block: "center" }); return; }
  showChatStarted();
  const card = el("div", "login");
  const vpn = realm === "webvpn";
  card.innerHTML = `
    <div class="confirm-head"><svg width="20" height="20"><use href="#lychee"/></svg>${vpn ? "登录学校 WebVPN" : "登录深大统一身份认证"}</div>
    ${vpn ? '<p class="qr-tip" style="margin:-4px 0 14px">事务中心的预约系统挂在学校 WebVPN 上，需要单独登录一次（之后可能还要二次验证）</p>' : ""}
    <div class="login-tabs" role="tablist">
      <button class="on" data-tab="qr" role="tab">扫码登录</button><button data-tab="pwd" role="tab">账号密码</button>
    </div>
    <div class="login-body" data-panel="qr">
      <div class="qr-box"><img alt="登录二维码" hidden><span class="spin"></span></div>
      <p class="qr-tip">用<strong>企业微信</strong>扫一扫</p>
    </div>
    <form class="login-body" data-panel="pwd" hidden autocomplete="on">
      <input name="username" placeholder="学号 / 工号" autocomplete="username" required>
      <input name="password" type="password" placeholder="密码" autocomplete="current-password" required>
      <label class="remember"><input type="checkbox" name="remember" checked> 7 天内免登录</label>
      <button class="btn primary" type="submit">登录</button>
      <p class="login-err" hidden></p>
    </form>
    <p class="note">${multiuser
      ? "密码经服务器转发给深大统一认证，不会保存，也不会发给 AI 模型。登录后服务器会保存学校的登录状态（不是密码），退出登录即删除。"
      : "密码由本机直接发给深大统一认证，不会发给 AI 模型，也不会被保存。"}</p>`;
  log.append(card);
  scrollDown(true);
  const state = { card, resume, realm, timer: null, loginId: null, done: false };
  activeLogin = state;

  const tabs = card.querySelectorAll(".login-tabs button");
  tabs.forEach((t) => t.addEventListener("click", () => {
    tabs.forEach((x) => x.classList.toggle("on", x === t));
    card.querySelectorAll(".login-body").forEach((p) => { p.hidden = p.dataset.panel !== t.dataset.tab; });
    if (t.dataset.tab === "qr" && !state.loginId) startQr(state); else if (t.dataset.tab === "pwd") card.querySelector("input[name=username]").focus();
  }));
  card.querySelector("form").addEventListener("submit", (e) => { e.preventDefault(); passwordLogin(state, e.target); });
  startQr(state);
}

async function startQr(state) {
  clearTimeout(state.timer);
  const box = state.card.querySelector(".qr-box"), img = box.querySelector("img"), tip = state.card.querySelector(".qr-tip");
  img.hidden = true; box.querySelector(".spin")?.remove(); box.insertAdjacentHTML("beforeend", '<span class="spin"></span>');
  tip.innerHTML = "用<strong>企业微信</strong>扫一扫";
  const res = await fetch("/api/auth/qr", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ realm: state.realm || "main" }) })
    .then((r) => r.json()).catch(() => null);
  box.querySelector(".spin")?.remove();
  if (!res?.ok) { tip.textContent = res?.error?.message || "二维码获取失败，可以改用账号密码"; return; }
  state.loginId = res.loginId; img.src = res.image; img.hidden = false;
  const poll = async () => {
    if (state.done || !document.body.contains(state.card)) return;
    const r = await fetch(`/api/auth/qr/${state.loginId}`).then((x) => x.json()).catch(() => null);
    const st = r?.state;
    if (st === "confirmed") return r.mfa ? loginNeedsMfa(state, r.mfa) : loginDone(state);
    if (st === "scanned") tip.textContent = "已扫码，请在手机上确认登录";
    if (st === "expired" || r?.ok === false) {
      state.loginId = null;
      tip.innerHTML = `${esc(r?.error?.message || "二维码已过期")} <button class="link">刷新</button>`;
      tip.querySelector("button").onclick = () => startQr(state);
      return;
    }
    state.timer = setTimeout(poll, 1500);
  };
  state.timer = setTimeout(poll, 1500);
}

async function passwordLogin(state, form) {
  const err = form.querySelector(".login-err"), btn = form.querySelector("button[type=submit]");
  err.hidden = true; btn.disabled = true; btn.textContent = "登录中…";
  const body = { username: form.username.value.trim(), password: form.password.value, remember: form.remember.checked, realm: state.realm || "main" };
  const r = await fetch("/api/auth/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then((x) => x.json()).catch(() => null);
  form.password.value = ""; // 用完立即从页面上清掉
  btn.disabled = false; btn.textContent = "登录";
  if (r?.ok) return r.mfa ? loginNeedsMfa(state, r.mfa) : loginDone(state);
  err.textContent = r?.error?.code === "CAPTCHA_REQUIRED" ? "学校要求滑块验证码，请改用扫码登录" : (r?.error?.message || "登录失败");
  err.hidden = false;
}

// 登录成功但学校要求二次验证：同一张卡片原地换成验证步骤，验证通过后由 mfaDone 接着刚才的任务
function loginNeedsMfa(state, mfa) {
  state.done = true; clearTimeout(state.timer);
  renderMfaStep(state, mfa.mfaId, mfa);
}

async function loginDone(state) {
  state.done = true; clearTimeout(state.timer); activeLogin = null;
  const firstLogin = multiuser && !auth.appSession; // 多人模式第一次登录：身份变了，重新加载这个人的历史和记忆
  state.card.querySelector(".login-tabs").remove();
  state.card.querySelectorAll(".login-body").forEach((p) => p.remove());
  state.card.querySelector(".note").outerHTML = `<div class="result ok">✓ 已登录${multiuser ? "" : "，凭证保存在本机"}，之后的任务会自动使用</div>`;
  await refreshAuth();
  if (firstLogin) { location.reload(); return; }
  loadToday();
  if (state.resume && !busy) ask("我登录好了，请继续刚才的事");
}

// 二次验证：验证码 / 密码只发给本机后端，不进入对话
async function addMfaCard({ target, resume }) {
  if (activeLogin && document.body.contains(activeLogin.card)) { activeLogin.card.scrollIntoView({ behavior: "smooth", block: "center" }); return; }
  showChatStarted();
  const card = el("div", "login");
  card.innerHTML = `<div class="confirm-head"><svg width="20" height="20"><use href="#lychee"/></svg>学校要求二次验证</div>
    <div class="login-body"><span class="spin"></span></div>`;
  log.append(card); scrollDown(true);
  const state = { card, resume, done: false };
  activeLogin = state;
  const r = await fetch("/api/auth/mfa", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target }) })
    .then((x) => x.json()).catch(() => null);
  if (r?.ok && r.needed === false) return mfaDone(state, "不需要验证了");
  if (!r?.ok) { card.querySelector(".login-body").innerHTML = `<p class="login-err">${esc(r?.error?.message || "无法开始二次验证")}</p>`; activeLogin = null; return; }

  renderMfaStep(state, r.mfaId, r);
}

// 一步验证的表单。学校可能连着要求两步（例如企业微信验证码之后再要手机短信），提交后 needed=true 就用新的方式重画
function renderMfaStep(state, mfaId, info) {
  const { card } = state, methods = info.methods;
  const lead = info.step > 1 ? `第 ${info.step - 1} 步已通过，学校还要求再验证一次${info.reason ? "：" + info.reason : ""}` : info.reason;
  card.innerHTML = `
    <div class="confirm-head"><svg width="20" height="20"><use href="#lychee"/></svg>学校要求二次验证</div>
    ${lead ? `<p class="qr-tip" style="margin:-4px 0 14px">${esc(lead)}</p>` : ""}
    <div class="login-tabs" role="tablist">${methods.map((m, i) => `<button class="${i ? "" : "on"}" data-m="${esc(m.type)}">${esc(m.type === "2" ? "密码" : m.name)}</button>`).join("")}</div>
    <form class="login-body" autocomplete="off">
      <div class="code-row"><input name="secret" required><button type="button" class="btn ghost send-code">发送验证码</button></div>
      <button class="btn primary" type="submit">验证</button>
      <p class="login-err" hidden></p>
    </form>
    <p class="note">验证码由本机直接发给深大统一认证，不会发给 AI 模型。</p>`;
  const form = card.querySelector("form"), err = form.querySelector(".login-err"), sendBtn = form.querySelector(".send-code");
  // 第一步默认企业微信；之后的步骤默认学校页面当前选中的方式（通常就是它要求的那种）
  let method = (info.step > 1 && methods.find((m) => m.type === info.current)) ? info.current
    : (methods.find((m) => m.type === "4") || methods[0]).type;
  const setMethod = (m) => {
    method = m;
    card.querySelectorAll(".login-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
    const pwd = m === "2";
    form.secret.type = pwd ? "password" : "text";
    form.secret.placeholder = pwd ? "再次输入统一认证密码（账号就是已登录的这个）" : "验证码";
    form.secret.inputMode = pwd ? "text" : "numeric";
    form.secret.autocomplete = pwd ? "current-password" : "one-time-code";
    sendBtn.hidden = pwd; err.hidden = true; form.secret.value = "";
  };
  card.querySelectorAll(".login-tabs button").forEach((b) => b.addEventListener("click", () => setMethod(b.dataset.m)));
  setMethod(method);

  sendBtn.addEventListener("click", async () => {
    sendBtn.disabled = true; err.hidden = true;
    const x = await fetch(`/api/auth/mfa/${mfaId}/send`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ method }) })
      .then((y) => y.json()).catch(() => null);
    err.hidden = false;
    err.style.color = x?.ok ? "var(--leaf)" : "";
    err.textContent = x?.ok ? x.message : (x?.error?.message || "发送失败");
    let left = x?.ok ? 60 : 0;
    const tick = () => { if (left <= 0) { sendBtn.disabled = false; sendBtn.textContent = "发送验证码"; return; } sendBtn.textContent = `${left--}s`; setTimeout(tick, 1000); };
    tick();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true; btn.textContent = "验证中…"; err.hidden = true; err.style.color = "";
    // 后端异常（500）、流程过期（404）时没有 error.message：把状态码显示出来，别只剩一句“验证失败”
    const x = await fetch(`/api/auth/mfa/${mfaId}/submit`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ method, secret: form.secret.value.trim() }) })
      .then(async (y) => { const j = await y.json().catch(() => ({})); return y.ok ? j : { ok: false, error: { message: `${j.detail || "本机后端出错"}（HTTP ${y.status}）` } }; })
      .catch(() => ({ ok: false, error: { message: "连不上本机后端" } }));
    form.secret.value = "";
    btn.disabled = false; btn.textContent = "验证";
    if (x?.ok && x.needed) return renderMfaStep(state, mfaId, x);
    if (x?.ok && x.appLogin) { mfaDone(state, "✓ 验证通过，已登录"); location.reload(); return; } // 多人模式：登录时的验证，身份刚建立
    if (x?.ok) return mfaDone(state, "✓ 验证通过");
    err.textContent = (x?.error?.message || "验证失败") + (x?.error?.code && x.error.code !== "MFA_FAILED" ? `（${x.error.code}）` : "");
    err.hidden = false;
  });
}

function mfaDone(state, text) {
  state.done = true; activeLogin = null;
  state.card.querySelectorAll(".login-tabs, .login-body, .note, .qr-tip").forEach((x) => x.remove());
  state.card.insertAdjacentHTML("beforeend", `<div class="result ok">${esc(text)}</div>`);
  loadToday();
  if (state.resume && !busy) ask("我验证好了，请继续刚才的事");
}

// ---------------- 预借手机号 ----------------
// 和密码一样只走本机后端接口：不经过 AI，AI 在预览里只能看到打码后的号码
function addContactCard({ resume }) {
  showChatStarted();
  const card = el("div", "login");
  card.innerHTML = `
    <div class="confirm-head"><svg width="20" height="20"><use href="#lychee"/></svg>填写联系手机号</div>
    <form class="login-body" autocomplete="on">
      <input name="phone" type="tel" inputmode="numeric" maxlength="11" placeholder="11 位手机号" autocomplete="tel" required>
      <button class="btn primary" type="submit">保存</button>
      <p class="login-err" hidden></p>
    </form>
    <p class="note">图书馆预借、事务中心空间预约会用到。只保存在你自己的电脑上，不会发给 AI 模型（AI 只能看到 138****5678 这样的形式）。</p>`;
  log.append(card); scrollDown(true);
  const form = card.querySelector("form");
  form.phone.focus();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = card.querySelector(".login-err");
    const res = await fetch("/api/contact", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone: form.phone.value.trim() }) }).catch(() => null);
    if (!res?.ok) { err.hidden = false; err.textContent = "手机号格式不对，请填 11 位手机号"; return; }
    const { phone } = await res.json();
    form.outerHTML = `<div class="result ok">✓ 已保存 ${esc(phone)}</div>`;
    if (resume && !busy) ask("手机号填好了，请继续刚才的事");
  });
}

function renderAccount() {
  const box = $("#account");
  box.innerHTML = auth.loggedIn
    ? `<div class="mem"><span class="k">统一身份认证</span><span class="v">已登录${auth.method === "qr" ? "（扫码）" : auth.method === "password" ? "（账号密码）" : ""}</span><button id="logoutBtn">退出登录</button></div>`
    : '<div class="mem"><span class="k">统一身份认证</span><span class="v" style="color:var(--muted)">未登录</span><button id="loginBtn">去登录</button></div>';
  $("#logoutBtn")?.addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    if (multiuser) { location.reload(); return; } // 多人模式退出 = 注销这个浏览器上的身份
    await refreshAuth(); renderAccount(); loadToday();
  });
  $("#loginBtn")?.addEventListener("click", () => { switchView("chat"); addLoginCard({ resume: false }); });
  if (multiuser && auth.appSession) {
    const row = el("div", "mem", '<span class="k">我的数据</span><span class="v" style="color:var(--muted)">对话、记忆、联系手机、学校登录状态</span><button>全部删除</button>');
    row.querySelector("button").onclick = async () => {
      if (!confirm("删除你在爱小荔服务器上的全部数据？对话历史、记忆、联系手机、学校登录状态都会删除，不能恢复。")) return;
      const r = await fetch("/api/me", { method: "DELETE" }).catch(() => null);
      if (r?.ok) location.reload(); else alert((await r?.json().catch(() => null))?.detail || "删除失败，请稍后再试");
    };
    box.append(row);
  }
  fetch("/api/contact").then((r) => r.json()).then(({ phone }) => {
    const row = el("div", "mem", `<span class="k">联系手机</span><span class="v"${phone ? "" : ' style="color:var(--muted)"'}>${phone ? esc(phone) : "未填写"}</span><button>${phone ? "删除" : "填写"}</button>`);
    row.querySelector("button").onclick = async () => {
      if (phone) { await fetch("/api/contact", { method: "DELETE" }); renderAccount(); }
      else { switchView("chat"); addContactCard({ resume: false }); }
    };
    box.append(row);
  }).catch(() => {});
}

// ---------------- 输入 ----------------
function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
function updateSend() { send.disabled = busy || !input.value.trim(); }

input.addEventListener("input", () => { autosize(); updateSend(); });
input.addEventListener("keydown", (e) => {
  // 中文输入法选词时按回车不能发送
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); ask(input.value); }
});
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
document.querySelectorAll(".suggests .pill").forEach((b) => b.addEventListener("click", () => ask(b.dataset.q)));
document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
$("#newChat").addEventListener("click", () => {
  if (busy) return;
  resetChat();
  switchView("chat"); input.focus();
});

$("#accountBtn").addEventListener("click", async () => {
  await refreshAuth();
  if (auth.loggedIn) switchView("memory"); else { switchView("chat"); addLoginCard({ resume: false }); }
});

// ---------------- 启动 ----------------
tick(); setInterval(tick, 10_000);
Promise.all([refreshAuth(), fetch("/api/meta").then((r) => r.json()).catch(() => ({}))]).then(([, m]) => {
  multiuser = !!m.multiuser;
  $("#modeChip").hidden = !m.mock;
  const where = multiuser ? "保存在服务器上，只有你自己能看到" : "保存在你自己的电脑上";
  $("#historyNote").textContent = `${where}，${m.historyDays > 0 ? `超过 ${m.historyDays} 天自动删除` : "不会自动删除"}。登录密码和验证码从不进入对话，所以也不会出现在这里。`;
  if (multiuser && !auth.appSession) addLoginCard({ resume: false });
});
restore();
loadToday();
