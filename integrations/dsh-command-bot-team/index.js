// DSH slash-command bridge for the bot-team attention API.
// Registered commands are user-facing (no model message), exactly what Feishu
// sends through dsh-im when the owner types /today, /queue, /park, /resume.

const name = "command-bot-team";
const inject = ["commands"];

const API = (process.env.BOT_TEAM_API || "http://127.0.0.1:8787").replace(/\/+$/, "");
const TIMEOUT_MS = 6000;

function failure(error) {
  const message = error && error.message ? error.message : String(error);
  return {
    kind: "error",
    text:
      `bot-team 服务不可用：${message}\n` +
      `请先启动 8787：python -B server/app.py\n` +
      `（或设置 BOT_TEAM_API 指向实际地址）`,
  };
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(API + path, {
      ...options,
      headers: {
        "content-type": "application/json",
        ...(options.headers || {}),
      },
      signal: controller.signal,
    });
    const raw = await response.text();
    let data;
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch {
      data = raw;
    }
    if (!response.ok) {
      const detail = typeof data === "string" ? data : JSON.stringify(data);
      throw new Error(`HTTP ${response.status} ${detail}`);
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

function itemLine(item, index) {
  const title = item && item.title ? item.title : "未命名";
  const owner = item && item.owner ? item.owner : "未指定";
  const due = item && item.due ? item.due : "无截止";
  return `${index}. ${title}（${owner}，${due}）`;
}

function ticketLine(ticket, index) {
  const question = ticket && ticket.question ? ticket.question : "待拍板";
  const options = Array.isArray(ticket && ticket.options) ? ticket.options.join(" / ") : "";
  const rec = ticket && ticket.recommendation ? `推荐：${ticket.recommendation}` : "无推荐";
  const deadline = ticket && ticket.deadline ? `截止：${ticket.deadline}` : "无截止";
  return `${index}. ${question}\n   选项：${options}\n   ${rec}；${deadline}`;
}

async function showToday() {
  const data = await api("/api/v1/attention/today");
  const wip = data.wip || {};
  const lines = [
    data.brief || "今天没有必须推进的事。",
    `WIP ${wip.active ?? 0}/${wip.limit ?? 2}，空槽 ${wip.slots_left ?? 0}`,
  ];
  const tickets = Array.isArray(data.tickets) ? data.tickets : [];
  if (tickets.length > 0) {
    lines.push("", `待拍板 ${tickets.length} 张：`);
    tickets.forEach((ticket, index) => lines.push(ticketLine(ticket, index + 1)));
  }
  return { kind: "success", text: lines.join("\n") };
}

async function showQueue() {
  const data = await api("/api/v1/attention/queue");
  const wip = data.wip || {};
  const active = Array.isArray(data.active) ? data.active : [];
  const parked = Array.isArray(data.parked) ? data.parked : [];
  const lines = [`WIP ${wip.active ?? 0}/${wip.limit ?? 2}`, "", `进行中 ${active.length} 件：`];
  if (active.length === 0) lines.push("（空）");
  active.forEach((item, index) => lines.push(itemLine(item, index + 1)));
  lines.push("", `停泊 ${parked.length} 件：`);
  if (parked.length === 0) lines.push("（空）");
  parked.forEach((item, index) => lines.push(itemLine(item, index + 1)));
  return { kind: "success", text: lines.join("\n") };
}

function requireId(rawInput, usage) {
  const id = String(rawInput || "").trim();
  if (!id) throw new Error(usage);
  return encodeURIComponent(id);
}

async function parkItem(rawInput) {
  const id = requireId(rawInput, "用法：/park <事项ID>");
  const data = await api(`/api/v1/workitems/${id}/park`, {
    method: "POST",
    body: JSON.stringify({ reason: "manual" }),
  });
  const item = data.item || {};
  return { kind: "success", text: `已停泊：${item.title || item.id || "事项"}\nWIP ${data.wip?.active ?? 0}/${data.wip?.limit ?? 2}` };
}

async function resumeItem(rawInput) {
  const id = requireId(rawInput, "用法：/resume <事项ID>");
  const data = await api(`/api/v1/workitems/${id}/resume`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  const item = data.item || {};
  return { kind: "success", text: `已恢复：${item.title || item.id || "事项"}（${item.status || ""}）\nWIP ${data.wip?.active ?? 0}/${data.wip?.limit ?? 2}` };
}

async function openTicket(rawInput) {
  const parts = String(rawInput || "").split("|").map((part) => part.trim());
  if (parts.length < 5) {
    throw new Error("用法：/ticket 问题 | 选项A | 选项B | 推荐 | 截止（YYYY-MM-DD）");
  }
  const [question, ...rest] = parts;
  const deadline = rest.pop();
  const recommendation = rest.pop();
  const options = rest.filter(Boolean);
  const data = await api("/api/v1/attention/tickets", {
    method: "POST",
    body: JSON.stringify({ question, options, recommendation, deadline }),
  });
  const ticket = data.ticket || {};
  return {
    kind: "success",
    text: `已开票：${ticket.ticket_id || ticket.id || ""}\n${ticketLine(ticket, 1)}`,
  };
}

async function showMetrics() {
  const data = await api("/api/v1/attention/metrics?days=3");
  const answers = data.answers || {};
  return {
    kind: "success",
    text:
      `过去 3 天：打断 ${answers.interruptions ?? 0} 次，` +
      `处理票 ${answers.tickets_processed ?? 0} 张，WIP 峰值 ${answers.wip_peak ?? 0}\n` +
      `样本：${data.history ? "有事件" : "暂无真实事件（不伪造）"}`,
  };
}

function handler(fn) {
  return async (invocation) => {
    try {
      return await fn(invocation.rawInput);
    } catch (error) {
      return failure(error);
    }
  };
}

function apply(ctx) {
  ctx.commands.register({
    name: "today",
    description: "今日最多 3 件 + 待拍板票（bot-team 注意力台）",
    input: { hint: "" },
    handler: handler(showToday),
  });
  ctx.commands.register({
    name: "queue",
    description: "完整队列：进行中 / 停泊（bot-team）",
    input: { hint: "" },
    handler: handler(showQueue),
  });
  ctx.commands.register({
    name: "park",
    description: "停泊一个事项，释放 WIP 槽",
    input: { hint: "<事项ID>" },
    handler: handler(parkItem),
  });
  ctx.commands.register({
    name: "resume",
    description: "恢复一个停泊事项（受 WIP≤2 约束）",
    input: { hint: "<事项ID>" },
    handler: handler(resumeItem),
  });
  ctx.commands.register({
    name: "ticket",
    description: "开一张待拍板票",
    input: { hint: "问题 | 选项A | 选项B | 推荐 | 截止" },
    handler: handler(openTicket),
  });
  ctx.commands.register({
    name: "bt-metrics",
    description: "注意力基线：打断 / 票 / WIP 峰值",
    input: { hint: "" },
    handler: handler(showMetrics),
  });
}

export { apply, inject, name };
