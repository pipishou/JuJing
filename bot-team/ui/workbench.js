/* Bot Team 主工作台脚本：无框架、无外链，直接对接 server/app.py 的 /api */
(function () {
  "use strict";

  const API = location.protocol === "file:" ? "" : "/api";
  const $ = (id) => document.getElementById(id);
  const state = { tasks: [], current: null, detail: null, busy: false };

  const STATUS = {
    created: ["已建任务", "created"],
    uploaded: ["已上传", "uploaded"],
    running: ["运行中", "running"],
    ready: ["待签字", "ready"],
    approved: ["已签字", "approved"],
    failed: ["失败", "failed"],
  };

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function text(v) { return v == null || v === "" ? "—" : esc(v); }
  function fmtTime(s) {
    if (!s) return "—";
    try { return new Date(s).toLocaleString("zh-CN", { hour12: false }); } catch (e) { return esc(s); }
  }
  function statusOf(s) { return STATUS[s] || [s || "未知", "created"]; }

  /* 合入 index.html 后的读侧兼容：本地服务可能返回裸数组 / 扁平任务对象（字段名为 task_id / state）。 */
  function normTask(t) {
    if (!t || typeof t !== "object") return t;
    if (!t.id && t.task_id) t.id = t.task_id;
    if (!t.status && t.state) t.status = t.state;
    if (!t.line && t.input && t.input.line) t.line = t.input.line;
    if (!t.context) t.context = t.title || "";
    return t;
  }

  function setHealth(ok, msg) {
    const el = $("health");
    el.className = "health " + (ok ? "ok" : "bad");
    $("healthText").textContent = msg || (ok ? "服务已连接" : "服务未连接");
  }

  function toast(msg, bad) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast show" + (bad ? " bad" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.className = "toast"; }, 4200);
  }

  function banner(msg, kind) {
    const el = $("banner");
    if (!msg) { el.hidden = true; el.textContent = ""; return; }
    el.hidden = false;
    el.className = "banner " + (kind || "");
    el.textContent = msg;
  }

  async function api(path, opts) {
    let res;
    try {
      res = await fetch(API + path, opts || {});
    } catch (e) {
      throw new Error("无法连接本地服务。请先运行：python -B server/app.py --port 8787");
    }
    const raw = await res.text();
    let data = null;
    try { data = raw ? JSON.parse(raw) : {}; } catch (e) { data = { ok: false, error: raw.slice(0, 300) }; }
    if (!res.ok || data.ok === false) {
      const err = new Error(data.error || ("HTTP " + res.status));
      err.payload = data;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function postJSON(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  /* ---------------- tasks ---------------- */

  async function loadTasks(keepCurrent) {
    const data = await api("/tasks");
    const list = Array.isArray(data) ? data : (data.tasks || []);
    state.tasks = list.map(normTask);
    renderTasks();
    if (!keepCurrent) {
      if (state.current && state.tasks.some((t) => t.id === state.current)) return;
      if (state.tasks.length) state.current = state.tasks[0].id;
    }
    if (state.current) {
      try { await loadTask(state.current); } catch (e) { /* 列表仍可用 */ }
    }
  }

  function renderTasks() {
    const box = $("tasks");
    if (!state.tasks.length) {
      box.innerHTML = '<div class="muted">还没有任务</div>';
      return;
    }
    box.innerHTML = state.tasks.map((t) => {
      const st = statusOf(t.status);
      return '<div class="task ' + (t.id === state.current ? "on" : "") + '" data-id="' + esc(t.id) + '">' +
        '<div class="task-top"><span class="task-id">' + esc(t.id) + '</span>' +
        '<span class="pill ' + st[1] + '">' + esc(st[0]) + '</span></div>' +
        '<div class="task-sub">' + esc(t.line || "—") + ' 线 · ' + esc(t.context || "未填上下文") +
        ' · ' + fmtTime(t.updated_at) + '</div></div>';
    }).join("");
    Array.from(box.querySelectorAll(".task")).forEach((el) => {
      el.addEventListener("click", () => selectTask(el.getAttribute("data-id")));
    });
  }

  async function selectTask(id) {
    if (!id || state.current === id) return;
    state.current = id;
    renderTasks();
    await loadTask(id);
  }

  async function loadTask(id) {
    const data = await api("/tasks/" + encodeURIComponent(id));
    const t = normTask(data.task || data);
    const rawStatus = data.status;
    const statusText = (rawStatus && typeof rawStatus === "object") ? (rawStatus.state || rawStatus.step) : rawStatus;
    if (!t.status && statusText) t.status = statusText;
    if (!t.report && data.report) t.report = data.report;
    state.current = id;
    state.detail = t;
    renderTasks();
    setUploadEnabled(!!state.current);
    renderDetail(t);
  }

  /* ---------------- create / upload / run / sign ---------------- */

  async function createTask() {
    const body = {
      task_id: $("fId").value.trim(),
      runner: "external",
      line: $("fLine").value.trim(),
      t_start: $("fTStart").value,
      t_end: $("fTEnd").value,
      base_start: $("fBStart").value,
      base_end: $("fBEnd").value,
      expected: $("fExpected").value.trim(),
      context: $("fContext").value.trim(),
      owner: $("fOwner").value.trim(),
      signer: $("fSigner").value.trim(),
    };
    if (!body.t_start || !body.t_end || !body.base_start || !body.base_end) {
      toast("T 窗口与基线窗口都要填", true); return;
    }
    if (!body.expected) { toast("目标 / 预期不能为空", true); return; }
    const btn = $("createBtn");
    btn.disabled = true; btn.textContent = "创建中…";
    try {
      const data = await postJSON("/tasks", body);
      const created = data.task || data;
      state.current = created.id || created.task_id;
      toast("任务已创建：" + state.current);
      $("fId").value = "";
      await loadTasks();
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false; btn.textContent = "创建任务";
    }
  }

  async function uploadFiles() {
    if (!state.current) { toast("先创建或选择一个任务", true); return; }
    const csv = $("fCsv").files[0];
    const map = $("fMap").files[0];
    const wf = $("fWf").files[0];
    if (!csv && !map && !wf) { toast("至少选择一个文件", true); return; }
    if (!csv) { toast("请选择 CSV / Excel 数据文件（表格由服务端转 T/T30）", true); return; }
    const fd = new FormData();
    fd.append("table", csv, csv.name);
    if (map) fd.append("field_map", map, map.name);
    // ingest_task_data 需要显式窗口；取建任务表单里的日期
    [["fTStart", "t_start"], ["fTEnd", "t_end"], ["fBStart", "base_start"], ["fBEnd", "base_end"]]
      .forEach(function (pair) { const el = $(pair[0]); if (el && el.value) fd.append(pair[1], el.value); });
    const btn = $("uploadBtn");
    btn.disabled = true; btn.textContent = "上传中…";
    try {
      await api("/tasks/" + encodeURIComponent(state.current) + "/data", { method: "POST", body: fd });
      toast("文件已上传");
      await loadTask(state.current);
      await loadTasks(true);
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = !state.current; btn.textContent = "上传到当前任务";
    }
  }

  async function runTask() {
    if (!state.current) return;
    const btn = $("runBtn");
    btn.disabled = true; btn.textContent = "运行中…";
    banner("");
    try {
      const data = await postJSON("/tasks/" + encodeURIComponent(state.current) + "/run", { runner: "external" });
      toast("收口完成，报告已生成");
      if (data.log) $("log").textContent = data.log;
    } catch (e) {
      const p = e.payload || {};
      banner((p.log || e.message) + (p.exit ? "\n[exit " + p.exit + "]" : ""));
      toast(e.message, true);
    } finally {
      btn.disabled = false;
      try { await loadTask(state.current); await loadTasks(true); } catch (e) { /* ignore */ }
      if (!state.detail) btn.textContent = "运行收口";
    }
  }

  async function signTask() {
    if (!state.current) return;
    const signer = ($("fSigner").value || "").trim();
    if (!signer) { toast("先在左侧填「签字人」", true); $("fSigner").focus(); return; }
    const btn = $("signBtn");
    btn.disabled = true; btn.textContent = "签字中…";
    try {
      await postJSON("/tasks/" + encodeURIComponent(state.current) + "/sign", { signer: signer });
      toast("已签字，决策已 approved");
    } catch (e) {
      banner(e.message, "info");
      toast(e.message, true);
    } finally {
      try { await loadTask(state.current); await loadTasks(true); } catch (e) { /* ignore */ }
      const approved = state.detail && state.detail.status === "approved";
      btn.disabled = approved;
      btn.textContent = approved ? "已签字" : "签字确认";
    }
  }

  /* ---------------- render detail ---------------- */

  function renderDetail(t) {
    if (!t) {
      $("empty").hidden = false; $("detail").hidden = true; return;
    }
    $("empty").hidden = true;
    $("detail").hidden = false;
    const inp = t.input || {};
    const st = statusOf(t.status);
    $("dTask").textContent = t.id;
    $("dTitle").textContent = inp.context || "补料率归因任务";
    $("dStatus").className = "status " + st[1];
    $("dStatus").textContent = st[0];
    $("dMeta").innerHTML =
      '<span>产线 <b>' + text(inp.line) + '</b></span>' +
      '<span>T <b>' + text(inp.t_start) + ' → ' + text(inp.t_end) + '</b></span>' +
      '<span>基线 <b>' + text(inp.base_start) + ' → ' + text(inp.base_end) + '</b></span>' +
      '<span>负责人 <b>' + text(inp.owner) + '</b></span>' +
      '<span>更新 <b>' + fmtTime(t.updated_at) + '</b></span>';

    const runBtn = $("runBtn");
    const locked = t.status === "approved";
    runBtn.disabled = locked || t.status === "running" || state.busy;
    runBtn.textContent = locked ? "已归档" : (t.status === "running" ? "运行中…" : "运行收口");
    runBtn.title = locked ? "已签字交付不可重跑；如需重跑请新建任务" : "";
    const signBtn = $("signBtn");
    signBtn.disabled = !t.report || t.status === "approved" || t.status === "running";
    signBtn.textContent = t.status === "approved" ? "已签字" : "签字确认";

    $("report").innerHTML = renderReport(t);
    $("log").textContent = t.log || "暂无日志（运行后显示）";
  }

  function renderReport(t) {
    const r = t.report;
    if (!r) {
      return '<div class="card"><h3>还没有报告</h3><div class="muted">上传 CSV/Excel、字段映射和 workflow-result.json 后，点「运行收口」。</div></div>';
    }
    const goal = r.goal || {};
    const goalText = typeof goal === "string" ? goal
      : (goal.metric || goal.expected || goal.description || "—") +
        (goal.target ? "；目标 " + goal.target : "") +
        (goal.current ? "；当前 " + goal.current : "");
    const findings = Array.isArray(r.finding) ? r.finding : [];
    const cannot = Array.isArray(r.cannot_attribute) ? r.cannot_attribute : [];
    const score = r.scorecard || {};
    let html = "";

    /* summary */
    html += '<div class="card"><h3><span class="num">01</span>交付摘要</h3><div class="kv">' +
      '<div class="k">任务</div><div class="v">' + text(r.task_id || t.id) + (r.is_demo ? ' <span class="chip warn">demo</span>' : ' <span class="chip ok">real</span>') + '</div>' +
      '<div class="k">生成时间</div><div class="v">' + fmtTime(r.generated_at) + '</div>' +
      '<div class="k">目标</div><div class="v">' + esc(goalText) + '</div>' +
      '<div class="k">结论</div><div class="v">' + (findings.length ? '<b>找到 ' + findings.length + ' 个候选因素</b>' : '<b>不可归因（证据不足）</b>') + '</div>' +
      '</div></div>';

    /* findings */
    html += '<div class="card"><h3><span class="num">02</span>归因候选</h3>';
    if (!findings.length) {
      html += '<div class="nofind"><b>finding = 0，交付卡走「不可归因」路径。</b><br/>这不是失败：证据不足时不硬归因，先补数据或缩小问题。下方列出需要补的证据。</div>';
    } else {
      findings.forEach((f, i) => {
        const ev = Array.isArray(f.evidence) ? f.evidence : [];
        html += '<div class="finding"><div class="rank">' + (i + 1) + '</div><div class="body">' +
          '<div class="title">' + text(f.factor || f.title || "未命名因素") +
          (f.confidence != null ? ' <span class="chip ok">置信 ' + esc(f.confidence) + '</span>' : '') + '</div>';
        ev.slice(0, 4).forEach((e) => { html += '<div class="reason">· ' + esc(e) + '</div>'; });
        html += '</div></div>';
      });
    }
    html += '</div>';

    /* cannot attribute */
    if (cannot.length) {
      html += '<div class="card"><h3><span class="num">03</span>不可归因 / 证据缺口 <span class="chip warn">' + cannot.length + ' 条</span></h3><div class="chips">';
      cannot.slice(0, 8).forEach((c) => { html += '<span class="chip warn" title="' + esc(c) + '">' + esc(c) + '</span>'; });
      html += '</div>';
      if (cannot.length > 8) html += '<div class="muted">还有 ' + (cannot.length - 8) + ' 条，见报告 JSON。</div>';
      html += '</div>';
    }

    /* scorecard */
    const rows = Object.keys(score).filter((k) => k !== "total" && typeof score[k] !== "object");
    if (rows.length) {
      const labels = {
        data_completeness: "数据完整", timeliness: "时效", significance: "显著性",
        exclusivity: "排他", history: "历史", human_ai_consistency: "人机一致",
      };
      let total = 0, n = 0;
      const bars = rows.map((k) => {
        const raw = score[k];
        let v = null;
        if (typeof raw === "number" && isFinite(raw)) v = raw;
        else {
          const m = String(raw == null ? "" : raw).match(/(\d+(?:\.\d+)?)/);
          if (m) v = Number(m[1]);
        }
        if (v == null) v = 0;
        v = Math.max(0, Math.min(5, v));
        total += v; n++;
        return '<div class="score-row"><span>' + esc(labels[k] || k) + '</span><div class="score-bar"><i style="width:' +
          (v / 5 * 100) + '%"></i></div><b>' + v + '/5</b></div>';
      }).join("");
      const pct = n ? Math.round(total / n / 5 * 100) : 0;
      html += '<div class="card"><h3><span class="num">04</span>六维评分卡</h3><div class="score">' + bars +
        '<div class="score-total"><b>' + pct + '</b><span>/100 · 六维均值（演示值非最终评级）</span></div></div></div>';
    }

    /* recommendation */
    if (r.recommendation) {
      const rec = r.recommendation;
      html += '<div class="card"><h3><span class="num">05</span>建议 / 决策</h3>';
      if (Array.isArray(rec)) {
        html += '<div class="rec-list">';
        rec.forEach((item, i) => {
          if (typeof item === "string") {
            html += '<div class="reco-item"><div class="act">' + (i + 1) + '. ' + esc(item) + '</div></div>';
            return;
          }
          const roi = item.roi || {};
          const meta = [];
          if (item.owner) meta.push("责任人 " + item.owner);
          if (item.due) meta.push("截止 " + item.due);
          if (roi.cost != null) meta.push("成本 ¥" + roi.cost);
          if (roi.avoided != null) meta.push("避免 ¥" + roi.avoided);
          if (item.irreversibility) meta.push(item.irreversibility === "irreversible" ? "不可逆" : "可逆");
          html += '<div class="reco-item"><div class="act">' + (i + 1) + '. ' + esc(item.action || item.title || "未命名建议") + '</div>';
          if (meta.length) html += '<div class="reco-meta">' + meta.map((m) => '<span class="chip">' + esc(m) + '</span>').join("") + '</div>';
          if (roi.anchor) html += '<div class="reco-reason">ROI 锚点：' + esc(roi.anchor) + '</div>';
          html += '</div>';
        });
        html += '</div>';
      } else {
        html += '<div class="reco">' + esc(rec) + '</div>';
      }
      if (r.decision_log_entry) {
        html += '<div class="decision" style="margin-top:10px">' + Object.keys(r.decision_log_entry).map((k) =>
          '<span class="chip">' + esc(k) + '：' + esc(r.decision_log_entry[k]) + '</span>').join("") + '</div>';
      }
      html += '</div>';
    }

    /* source / coverage */
    if (r.source || r.role_coverage) {
      html += '<div class="card"><h3><span class="num">06</span>数据来源与角色覆盖</h3><div class="kv">';
      if (r.source) html += '<div class="k">来源</div><div class="v hash">' + esc(JSON.stringify(r.source)) + '</div>';
      if (r.role_coverage) html += '<div class="k">角色</div><div class="v hash">' + esc(JSON.stringify(r.role_coverage)) + '</div>';
      html += '</div></div>';
    }
    return html;
  }

  /* ---------------- bindings ---------------- */

  function bindFile(inputId, labelId) {
    const input = $(inputId);
    input.addEventListener("change", () => {
      const f = input.files[0];
      $(labelId).textContent = f ? f.name : "选择文件";
    });
  }

  function setUploadEnabled(on) {
    $("uploadBtn").disabled = !on;
    $("uploadHint").textContent = on ? "上传到 " + state.current : "先创建任务";
  }

  function init() {
    bindFile("fCsv", "csvName");
    bindFile("fMap", "mapName");
    bindFile("fWf", "wfName");
    $("createBtn").addEventListener("click", createTask);
    $("uploadBtn").addEventListener("click", uploadFiles);
    $("runBtn").addEventListener("click", runTask);
    $("signBtn").addEventListener("click", signTask);
    $("refreshBtn").addEventListener("click", () => loadTasks().catch((e) => toast(e.message, true)));

    api("/health").then((h) => {
      setHealth(true, "服务 v" + (h.api_version || h.version || "—"));
      setUploadEnabled(!!state.current);
      return loadTasks();
    }).then(() => {
      setUploadEnabled(!!state.current);
    }).catch((e) => {
      setHealth(false, "服务未连接");
      banner(e.message);
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
