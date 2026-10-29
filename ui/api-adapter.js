/* ui/api-adapter.js -- M1.5 §2.6 工作台适配层（无第三方依赖，ES2017，file:// 下也可加载）。
 *
 * 职责（只做数据接入 + 事件归一化，不改视觉）：
 *   1. 启动时 GET /api/health：成功 -> 真数据模式；失败 -> 静默保持静态演示（不抛未捕获错误）。
 *   2. 团队数据：GET /api/team -> employees/crews/shelf/pack，供货架/员工详情/班组卡/座位使用。
 *   3. 多任务：GET /api/tasks 列表 + selectTask(id)；舞台/交付卡/KPI/本场数据全部跟随当前任务。
 *   4. 事件流：GET /api/tasks/<id>/events?stream=1（EventSource，since 续传、断线重连）；
 *      失败回退 /log?after=n 轮询；再失败回退静态 demo（index.html 自带）。
 *   5. 写操作：POST /api/tasks（可带 data_files）、/data、/run(带 runner)、/sign、/gate、/finalize、
 *      /outcome（T+7 回填）、/api/scenes/derive（场景派生：spec 校验模式 / metric_id+data_ref 生成模式）。
 *
 * 设计约束：本文件不直接改 DOM（渲染留在 index.html 的 hooks），避免与动画互踩；
 *           任何 fetch/EventSource 失败都不得打断页面（file:// 打开时必须还能看静态演示）。
 *
 * 舞台 beat 契约（与 index.html 的 BEATS 同构，扩展 actors 支持并行阶段同屏）：
 *   {phase, tag, short, text, actors:[{role,key,name,agent,text}], role?, pos?}
 *   phase ∈ s1..s5|gate|intake|data|status；旧 run.log 行（ts/phase/role/line）读时补 kind=status、agent=role。
 */
(function () {
  'use strict';

  var API_BASE = ''; // 同源（页面由 server/app.py 提供）
  var POLL_MS = 5000;       // 兜底轮询（SSE 正常时只做 KPI/任务列表刷新）
  var TASKS_MS = 1500;      // 任务列表/状态轮询
  var PHASE_ORDER = ['s1', 's2', 's3', 's4', 's5'];
  var PHASE_LABEL = { s1: '快照', s2: '因素', s3: '排除', s4: '建议', s5: '收口', gate: '校验门', intake: '接单', data: '数据', status: '状态' };
  var PHASE_ROLE_HINT = { s1: 'analyst', s2: 'biz', s3: 'attr', s4: 'biz', s5: 'host', gate: 'host', intake: 'host', data: 'data', status: 'host' };
  var ROLE_NAME = { host: '工作助理', analyst: '咨询分析师', attr: 'AI 归因专家', biz: '制造业业务专家', quality: '合规质检', data: '取数员' };
  var ROLE_POS = { host: [50, 36], analyst: [24, 56], attr: [76, 56], biz: [50, 64], quality: [70, 58], data: [20, 60] };
  var ROLE_ALIAS = {
    'host': 'host', '工作助理': 'host',
    'consulting-analyst': 'analyst', '咨询分析师': 'analyst', 'data-fetcher': 'analyst', '取数员': 'analyst',
    'ai-attribution-expert': 'attr', 'AI 归因专家': 'attr', 'ai归因专家': 'attr',
    'manufacturing-ops-expert': 'biz', '制造业业务专家': 'biz', '业务专家': 'biz',
    'compliance-reviewer': 'quality', '合规质检': 'quality',
    'report-recorder': 'data', '记录员': 'data'
  };
  var SIX_DIMS = ['data_completeness', 'timeliness', 'significance', 'exclusivity', 'history', 'human_ai_consistency'];
  var STATE_LABEL = {
    created: '已建单', running: '运行中', assembled: '已装配', awaiting_sign: '待签字',
    signed: '已交付', failed: '失败', rejected: '已退回'
  };
  var KIND_LABEL = { user: '用户', speak: '发言', tool: '工具', think: '思考', status: '状态', artifact: '产物' };
  // task-15 / C-2：交付卡卡型（宽松读取，缺省 attribution；未知值原样透出，渲染端兜底）
  var CARD_KIND_LABEL = { attribution: '归因卡', review: '回看卡', content: '内容卡' };

  var state = {
    live: false,
    connecting: false,
    stopped: false,
    health: null,
    error: null,
    team: null,
    teamError: null,
    tasks: [],
    taskParam: null,
    currentTaskId: null,
    detail: null,
    model: null,
    log: [],
    logAfter: 0,
    events: [],
    eventCursor: 0,
    sse: null,
    sseFailures: 0,
    sseLive: false,
    beats: [],
    beatSource: '',
    signature: null,
    gate: null,
    runner: 'fixture',
    lastUpload: null,
    // B15：场景台（SceneSpec v1，来自 /api/scenes；前端只认 schema，不写场景常量）
    scenes: [],
    // 本次会话里由 POST /api/scenes/derive 派生出来的场景（服务端不落盘，
    // 所以刷新 /api/scenes 后要保留，否则 P0 会把派生结果冲掉）
    derivedScenes: [],
    sceneMeta: null,
    sceneId: null,
    scene: null,
    sceneError: null,
    sceneParam: null,
    // R4：P3 决策日志（跨任务时间线，来自 GET /api/decisions）
    decisions: [],
    decisionFacets: { scenes: [], entities: [], verdicts: [] },
    decisionMeta: { total: 0, matched: 0, generated_at: '' },
    decisionError: null,
    hooks: {},
    timer: null,
    taskTimer: null,
    failures: 0,
    lastBeatsKey: ''
  };

  // ---------------------------------------------------------------- utils --
  function has(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  function queryParam(name) {
    try { return new URLSearchParams(window.location.search).get(name); }
    catch (err) {
      var match = new RegExp('[?&]' + name + '=([^&]*)').exec(window.location.search || '');
      return match ? decodeURIComponent(match[1]) : null;
    }
  }

  function toast(message) {
    try { if (typeof window.toast === 'function') window.toast(message); } catch (err) { /* 静态页没有 toast */ }
  }

  function trunc(text, limit) {
    var str = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
    if (limit && str.length > limit) return str.slice(0, limit - 1) + '…';
    return str;
  }

  function moneyOf(text) {
    var match = String(text == null ? '' : text).match(/[¥￥]\s*([\d,]+(?:\.\d+)?)/);
    if (match) return '¥' + match[1];
    var plain = String(text == null ? '' : text).match(/([\d,]+(?:\.\d+)?)\s*元/);
    return plain ? '¥' + plain[1] : '';
  }

  function refsText(refs) {
    if (!refs) return '—';
    var list = Array.isArray(refs) ? refs : [refs];
    var names = [];
    list.forEach(function (ref) {
      if (ref && typeof ref === 'object') names.push(ref.path || ref.file || ref.name || '');
      else names.push(String(ref || ''));
    });
    names = names.filter(Boolean).map(function (name) { return String(name).split(/[\\/]/).pop(); });
    return names.length ? names.join(' / ') : '—';
  }

  function scoreInfo(scorecard, report) {
    var sc = scorecard || {};
    var doc = report || {};
    var sum = 0, dims = 0;
    var total = typeof sc.total === 'number' ? sc.total : null;
    var verdict = sc.verdict || doc.verdict || null;
    SIX_DIMS.forEach(function (key) {
      var value = sc[key];
      if (typeof value === 'number') { sum += value; dims += 1; }
      else if (value && typeof value.score === 'number') { sum += value.score; dims += 1; }
    });
    if (total == null && dims > 0) total = Math.round((sum / (dims * 5)) * 1000) / 10;
    return { total: total, sum: sum, dims: dims, verdict: verdict, raw: sc };
  }

  function taskTitle(task) {
    if (!task || typeof task !== 'object') return '未命名任务';
    var keys = ['title', 'context', 'summary'];
    for (var i = 0; i < keys.length; i += 1) {
      var value = task[keys[i]];
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
    var parts = [];
    if (task.plant) parts.push(String(task.plant));
    if (task.line) parts.push(task.line + '线');
    if (task.metric) parts.push(String(task.metric));
    return parts.length ? parts.join(' · ') : String(task.task_id || '未命名任务');
  }

  function stateLabel(value) { return STATE_LABEL[value] || value || '未知'; }

  function stateDot(value) {
    return ({ created: 'wait', running: 'run', assembled: 'wait', awaiting_sign: 'sign', signed: 'ok', failed: 'err', rejected: 'err' })[value] || 'wait';
  }

  // 员工 id（长 slug 或短 key）-> 精灵 key。 /api/team 的 sprite_key 优先。
  function spriteKeyFor(id) {
    var key = String(id == null ? '' : id).trim();
    if (state.team) {
      var employees = state.team.employees || [];
      for (var i = 0; i < employees.length; i += 1) {
        var emp = employees[i] || {};
        if (emp.id === key && emp.sprite_key) return emp.sprite_key;
      }
      var crews = state.team.crews || [];
      for (var c = 0; c < crews.length; c += 1) {
        var steps = (crews[c] && crews[c].steps) || [];
        for (var s = 0; s < steps.length; s += 1) {
          var step = steps[s] || {};
          if (step.agent === key && step.sprite_key) return step.sprite_key;
        }
      }
    }
    if (has(ROLE_NAME, key)) return key;
    if (has(ROLE_ALIAS, key)) return ROLE_ALIAS[key];
    var lower = key.toLowerCase();
    for (var alias in ROLE_ALIAS) {
      if (Object.prototype.hasOwnProperty.call(ROLE_ALIAS, alias) && lower.indexOf(alias.toLowerCase()) >= 0) return ROLE_ALIAS[alias];
    }
    return 'host';
  }

  function employeeFor(id) {
    var team = state.team || {};
    var employees = team.employees || [];
    for (var i = 0; i < employees.length; i += 1) {
      if (employees[i] && employees[i].id === id) return employees[i];
    }
    return null;
  }

  function crewList() {
    return (state.team && Array.isArray(state.team.crews)) ? state.team.crews : [];
  }

  function packInfo() {
    return (state.team && state.team.pack) || {};
  }

  // 当前任务对应班组：task.crew_id > pack.crew_id > 第一个
  function currentCrew(task) {
    var crews = crewList();
    if (!crews.length) return null;
    var wanted = (task && (task.crew_id || task.crew)) || packInfo().crew_id;
    for (var i = 0; i < crews.length; i += 1) {
      if (crews[i] && (crews[i].id === wanted || crews[i].name === wanted)) return crews[i];
    }
    return crews[0];
  }

  // ---------------------------------------------------------------- http --
  function requestJSON(path, options) {
    var opts = Object.assign({ cache: 'no-store' }, options || {});
    return fetch(API_BASE + path, opts).then(function (res) {
      return res.text().then(function (text) {
        var body = null;
        if (text) {
          try { body = JSON.parse(text); } catch (err) { body = null; }
        }
        if (!res.ok) {
          var message = (body && (body.error || body.message)) || ('HTTP ' + res.status);
          var error = new Error(message);
          error.status = res.status;
          error.body = body;
          throw error;
        }
        if (body === null) throw new Error('响应不是 JSON: ' + path);
        return body;
      });
    });
  }

  function postJSON(path, payload) {
    return requestJSON(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
  }

  // ---------------------------------------------------------------- model --
  function cardKindOf(report, detail) {
    var raw = (report && report.card_kind) || (detail && detail.card_kind) || '';
    var kind = String(raw || '').trim();
    return kind || 'attribution';
  }

  function buildModel(detail) {
    var report = (detail && detail.report) || null;
    var status = (detail && detail.status) || {};
    var signature = (detail && detail.signature) || null;
    var signatureStale = Boolean(detail && detail.signature_stale);
    var task = (detail && detail.task) || {};
    if (!report) {
      return {
        report: null, status: status, signature: signature, task: task,
        signatureStale: signatureStale,
        title: taskTitle(task), score: scoreInfo(null, null), roi: null,
        finding: [], cannot: [], recommendation: [], log: null, pack: null,
        refs: [], isDemo: Boolean(task.is_demo), state: status.state || task.state || 'created',
        step: status.step || '', gate: null, irreversible: [],
        dataReady: Boolean(task.data_ready || (task.data_files && task.data_files.length)),
        scene: (detail && detail.scene) || null,
        loop: (detail && detail.loop) || null,
        outcome: (detail && detail.outcome) || null,
        outcomePending: detail ? detail.outcome_pending !== false : true,
        // task-15 / C-2：卡型三分支 + 本卡班组 + content_plan（供 P2 决策卡渲染）
        cardKind: cardKindOf(null, detail),
        cardLabel: CARD_KIND_LABEL[cardKindOf(null, detail)] || cardKindOf(null, detail),
        crew: (detail && detail.crew) || null,
        contentPlan: (detail && detail.content_plan) || null,
        review: (detail && detail.review) || null
      };
    }
    var recommendation = report.recommendation || [];
    var first = recommendation[0] || {};
    var decisionLog = report.decision_log_entry || {};
    var refs = decisionLog.data_refs;
    if (refs && !Array.isArray(refs)) refs = [refs];
    var gate = report.security_gate || null;
    var irreversible = [];
    recommendation.forEach(function (item) {
      if (item && (item.irreversibility === true || (item.irreversibility && item.irreversibility !== false && item.irreversibility !== 'reversible'))) {
        irreversible.push(item);
      }
    });
    if (!irreversible.length && report.security_gate_requested) irreversible.push({ action: '报告标记需审批门确认（security_gate_requested=true）', irreversibility: true });
    return {
      report: report, status: status, signature: signature, task: task,
      title: report.goal || taskTitle(task),
      score: scoreInfo(report.scorecard, report),
      roi: first.roi || null,
      finding: report.finding || [],
      cannot: report.cannot_attribute || [],
      recommendation: recommendation,
      log: decisionLog,
      pack: report.pack || null,
      refs: refs || [],
      isDemo: report.is_demo === true,
      state: status.state || task.state || '',
      step: status.step || '',
      gate: gate,
      irreversible: irreversible,
      signatureStale: signatureStale,
      dataReady: Boolean(task.data_ready || (task.data_files && task.data_files.length)),
      scene: (detail && detail.scene) || null,
      loop: (detail && detail.loop) || null,
      outcome: (detail && detail.outcome) || null,
      outcomePending: detail ? detail.outcome_pending !== false : true,
      // task-15 / C-2：卡型三分支 + 本卡班组 + content_plan（供 P2 决策卡渲染）
      cardKind: cardKindOf(report, detail),
      cardLabel: CARD_KIND_LABEL[cardKindOf(report, detail)] || cardKindOf(report, detail),
      crew: (detail && detail.crew) || null,
      contentPlan: (report.content_plan && typeof report.content_plan === 'object')
        ? report.content_plan : ((detail && detail.content_plan) || null),
      review: (report.review && typeof report.review === 'object')
        ? report.review : ((detail && detail.review) || null)
    };
  }

  function summarize(tasks) {
    var list = Array.isArray(tasks) ? tasks : [];
    var now = Date.now();
    var withReport = list.filter(function (task) { return task && task.has_report; });
    var running = list.filter(function (task) {
      return task && ['created', 'running', 'assembled', 'awaiting_sign'].indexOf(task.state) >= 0;
    }).length;
    var week = withReport.filter(function (task) {
      var stamp = Date.parse(task.updated_at || '');
      return !Number.isFinite(stamp) || (now - stamp) < 7 * 24 * 3600 * 1000;
    }).length;
    var review = list.filter(function (task) { return task && (task.signed || task.state === 'signed') && !task.reviewed; }).length;
    var dues = list.map(function (task) { return task && task.review_due; }).filter(Boolean).sort();
    var demo = list.filter(function (task) { return task && task.is_demo; }).length;
    return {
      total: list.length, running: running, week: week, review: review,
      delivered: withReport.length, demo: demo, nextDue: dues.length ? dues[0] : null
    };
  }

  // ---------------------------------------------------------------- beats --
  function normalizePhase(phase) {
    var key = String(phase == null ? '' : phase).trim().toLowerCase();
    if (PHASE_ORDER.indexOf(key) >= 0) return key;
    if (key === 'gate' || key === 'intake' || key === 'data') return key;
    return 'status';
  }

  function makeBeat(role, phase, text, meta) {
    var key = spriteKeyFor(role);
    return {
      role: key,
      phase: phase,
      tag: phase.toUpperCase() + ' ' + (ROLE_NAME[key] || key),
      who: ROLE_NAME[key] || key,
      frame: (phase === 's2' || phase === 's4') ? 'talk' : 'think',
      text: trunc(text, 260),
      pos: ROLE_POS[key] || ROLE_POS.host,
      short: '第 ' + phase.slice(1) + ' 幕 · ' + (PHASE_LABEL[phase] || phase),
      actors: [{ role: key, name: ROLE_NAME[key] || key, text: trunc(text, 260), agent: (meta && meta.agent) || role }],
      kind: (meta && meta.kind) || 'status',
      seq: meta && meta.seq
    };
  }

  function mapRole(role, phase) { return spriteKeyFor(role || PHASE_ROLE_HINT[phase]); }

  // 从事件数组生成"场"：s1‖s2 并行场合并为一场（同屏多角色）；其余 phase 各一场。
  function scenesFromEvents(events) {
    var groups = {};
    var order = [];
    (events || []).forEach(function (event) {
      if (!event) return;
      var phase = normalizePhase(event.phase);
      if (!has(groups, phase)) { groups[phase] = { phase: phase, actors: [], lines: [], seq: event.seq || 0, kind: event.kind || 'status' }; order.push(phase); }
      var group = groups[phase];
      var role = mapRole(event.role || event.agent, phase);
      group.seq = event.seq || group.seq;
      group.lines.push(event.line || '');
      var existing = null;
      group.actors.forEach(function (actor) { if (actor.role === role) existing = actor; });
      if (existing) { existing.text = trunc(event.line, 260); existing.agent = event.agent || role; }
      else {
        group.actors.push({ role: role, name: ROLE_NAME[role] || role, text: trunc(event.line, 260), agent: event.agent || event.role || role });
      }
      if (event.kind) group.kind = event.kind;
    });
    var scenes = [];
    var usedParallel = false;
    order.forEach(function (phase) {
      var group = groups[phase];
      if (!group) return;
      if (phase === 's1' || phase === 's2') {
        if (usedParallel) return;
        usedParallel = true;
        var merged = { phase: 's1s2', kind: 'status', seq: group.seq, actors: [], lines: [] };
        ['s1', 's2'].forEach(function (key) {
          if (!groups[key]) return;
          merged.actors = merged.actors.concat(groups[key].actors);
          merged.lines = merged.lines.concat(groups[key].lines);
          merged.seq = Math.max(merged.seq, groups[key].seq);
        });
        scenes.push(finishScene(merged));
        return;
      }
      scenes.push(finishScene(group));
    });
    return scenes;
  }

  function finishScene(group) {
    var actors = group.actors.length ? group.actors : [{ role: 'host', name: ROLE_NAME.host, text: group.lines[0] || '', agent: 'host' }];
    var phase = group.phase;
    var label = phase === 's1s2' ? 'S1‖S2 并行 · 快照/因素' : (PHASE_LABEL[phase] || phase);
    return {
      phase: phase,
      tag: phase === 's1s2' ? 'S1‖S2 并行' : (phase.toUpperCase() + ' ' + (PHASE_LABEL[phase] || phase)),
      short: phase === 's1s2' ? '并行场 · 快照/因素' : ('第 ' + phase.slice(1) + ' 幕 · ' + (PHASE_LABEL[phase] || phase)),
      text: actors.map(function (actor) { return actor.name + '：' + actor.text; }).join(' ／ '),
      actors: actors,
      role: actors[0] ? actors[0].role : 'host',
      pos: (actors[0] && ROLE_POS[actors[0].role]) || ROLE_POS.host,
      kind: group.kind || 'status',
      seq: group.seq,
      parallel: phase === 's1s2',
      label: label
    };
  }

  function beatsFromReport(detail) {
    var report = detail && detail.report;
    if (!report) return [];
    var finding = report.finding || [];
    var first = finding[0] || {};
    var score = scoreInfo(report.scorecard, report);
    var rec = (report.recommendation || [])[0] || {};
    var log = report.decision_log_entry || {};
    var beats = [];
    beats.push(makeBeat('analyst', 's1', '读取数据：' + refsText(log.data_refs) + '；目标：' + (report.goal || '—')));
    beats.push(makeBeat('biz', 's2', '六维打分：' + (score.total == null ? '—' : score.total + '/100') + (score.verdict ? '（' + score.verdict + '）' : '') + (report.pack && report.pack.pack_id ? '；行业包 ' + report.pack.pack_id : '')));
    beats.push(makeBeat('attr', 's3', finding.length
      ? '主因：' + (first.factor || '—') + '（置信 ' + (first.confidence == null ? '—' : first.confidence) + '）；cannot_attribute ' + (report.cannot_attribute || []).length + ' 条'
      : '不可归因：证据不足（cannot_attribute ' + (report.cannot_attribute || []).length + ' 条），建议先补数据'));
    beats.push(makeBeat('biz', 's4', rec.action ? rec.action : 'ROI 锚点：' + ((rec.roi && rec.roi.anchor) || '—')));
    var _v1 = report.verdict_one_line || score.verdict || '待复核';
    var _ss = report.sign_scope ? (' · 签范围 ' + report.sign_scope) : '';
    beats.push(makeBeat('host', 's5', _v1 + _ss + ' · 交付卡 ' + (log.id || '—') + '；下一步：' + (log.expected_result || 'T+7 回看')));
    return beats;
  }

  function beatsFromLog(events, detail) {
    var beats = scenesFromEvents(events);
    if (beats.length >= 2) { state.beatSource = 'run.log'; return beats; }
    var fromReport = beatsFromReport(detail);
    if (fromReport.length >= 2) { state.beatSource = 'report.json'; return fromReport; }
    state.beatSource = beats.length ? 'run.log' : '';
    return beats;
  }

  function beatsKey(beats) {
    return beats.map(function (beat) { return (beat.phase || '') + '|' + (beat.actors || []).map(function (actor) { return actor.role + ':' + actor.text; }).join(',') + '|' + beat.text; }).join('§');
  }

  function applyScenes(scenes, source) {
    if (!scenes || scenes.length < 2) return;
    state.beats = scenes;
    state.beatSource = source || state.beatSource;
    var key = beatsKey(scenes);
    if (key !== state.lastBeatsKey) {
      state.lastBeatsKey = key;
      if (state.hooks.onBeats) state.hooks.onBeats(scenes, state.beatSource);
    }
  }

  // --------------------------------------------------------------- events --
  function trimEvents() {
    if (state.events.length > 600) state.events = state.events.slice(-600);
  }

  function acceptEvent(event) {
    if (!event || typeof event !== 'object') return false;
    var seq = Number(event.seq != null ? event.seq : event.n);
    if (!Number.isFinite(seq)) seq = state.eventCursor + 1;
    if (seq <= state.eventCursor && state.events.length) return false;
    event.seq = seq;
    state.eventCursor = Math.max(state.eventCursor, seq);
    state.events.push(event);
    trimEvents();
    if (state.hooks.onEvent) state.hooks.onEvent(event, state.events.slice());
    var scenes = scenesFromEvents(state.events);
    applyScenes(scenes, 'events');
    return true;
  }

  function closeStream() {
    var sse = state.sse;
    state.sse = null;
    state.sseLive = false;
    if (sse && sse.es) { try { sse.es.close(); } catch (err) { /* ignore */ } }
    if (state.hooks.onSse) state.hooks.onSse(false);
  }

  function openStream() {
    if (!state.live || !state.currentTaskId || typeof EventSource === 'undefined') return;
    closeStream();
    var taskId = state.currentTaskId;
    var url = API_BASE + '/api/tasks/' + encodeURIComponent(taskId) + '/events?stream=1&since=' + state.eventCursor;
    var es;
    try { es = new EventSource(url); } catch (err) { return; }
    state.sse = { es: es, taskId: taskId, failures: 0 };
    es.onopen = function () { state.sseLive = true; if (state.hooks.onSse) state.hooks.onSse(true); };
    es.onmessage = function (message) {
      var payload = null;
      try { payload = JSON.parse(message.data); } catch (err) { return; }
      acceptEvent(payload);
    };
    es.onerror = function () {
      if (!state.sse || state.sse.es !== es) return;
      state.sseLive = false;
      if (state.hooks.onSse) state.hooks.onSse(false);
      // 服务端不支持 SSE 或断开：关闭后用 since 游标重连；连续失败则彻底回退 /log 轮询。
      try { es.close(); } catch (err) { /* ignore */ }
      state.sse = null;
      state.sseFailures += 1;
      if (state.sseFailures <= 4) {
        setTimeout(function () { openStream(); }, 1200 * state.sseFailures);
      }
    };
  }

  // --------------------------------------------------------------- refresh --
  function pickCurrentId(tasks) {
    if (state.taskParam) {
      var hit = tasks.filter(function (task) { return task && task.task_id === state.taskParam; })[0];
      if (hit) return hit.task_id;
    }
    if (state.currentTaskId) {
      var still = tasks.filter(function (task) { return task && task.task_id === state.currentTaskId; })[0];
      if (still) return still.task_id;
    }
    var withReport = tasks.filter(function (task) { return task && task.has_report; })[0];
    if (withReport) return withReport.task_id;
    var running = tasks.filter(function (task) { return task && task.state === 'running'; })[0];
    if (running) return running.task_id;
    return tasks.length ? tasks[0].task_id : null;
  }

  function resetTaskState() {
    state.log = [];
    state.logAfter = 0;
    state.events = [];
    state.eventCursor = 0;
    state.beats = [];
    state.lastBeatsKey = '';
    state.signature = null;
    state.gate = null;
    closeStream();
  }

  function refreshTask() {
    var taskId = state.currentTaskId;
    if (!taskId) {
      state.detail = null; state.model = null;
      if (state.hooks.onTask) state.hooks.onTask(null, summarize(state.tasks));
      return Promise.resolve(state);
    }
    var base = '/api/tasks/' + encodeURIComponent(taskId);
    return requestJSON(base).then(function (detail) {
      // 切换任务回来时若发现任务变了，丢弃旧事件
      if (taskId !== state.currentTaskId) return state;
      state.detail = detail;
      state.model = buildModel(detail);
      state.signature = state.model.signature;
      state.gate = state.model.gate;
      if (state.hooks.onTask) state.hooks.onTask(state.model, summarize(state.tasks));
      if (state.hooks.onSignature) state.hooks.onSignature(state.signature, state.model);
      if (state.hooks.onGate) state.hooks.onGate(state.gate, state.model);
      if (state.sse || state.eventCursor > 0) return state;
      // 无 SSE 时的兜底：/log 增量
      return requestJSON(base + '/log?after=' + state.logAfter).then(function (events) {
        if (Array.isArray(events) && events.length) {
          state.log = state.log.concat(events);
          var last = events[events.length - 1];
          if (last && typeof last.n === 'number' && last.n > state.logAfter) state.logAfter = last.n;
          state.events = state.events.concat(events.map(function (event) {
            return { seq: event.n, ts: event.ts, phase: event.phase, role: event.role, agent: event.role, kind: 'status', line: event.line };
          }));
          trimEvents();
          state.eventCursor = Math.max(state.eventCursor, state.logAfter);
        }
        var beats = beatsFromLog(state.events.length ? state.events : state.log, state.detail);
        if (beats.length >= 2) applyScenes(beats, state.beatSource);
        return state;
      });
    });
  }

  function refreshAll() {
    return requestJSON('/api/tasks').then(function (tasks) {
      state.tasks = Array.isArray(tasks) ? tasks : [];
      if (state.hooks.onTasks) state.hooks.onTasks(state.tasks, summarize(state.tasks));
      var wanted = pickCurrentId(state.tasks);
      if (!wanted) {
        state.currentTaskId = null;
        resetTaskState();
        state.detail = null; state.model = null;
        if (state.hooks.onTask) state.hooks.onTask(null, summarize(state.tasks));
        return state;
      }
      var switched = wanted !== state.currentTaskId;
      state.currentTaskId = wanted;
      if (switched) { state.taskParam = wanted; resetTaskState(); }
      return refreshTask().then(function () {
        if (state.live && !state.sse) openStream();
        return state;
      });
    });
  }

  // ---------------------------------------------------------------- poll --
  function stopPolling() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    if (state.taskTimer) { clearInterval(state.taskTimer); state.taskTimer = null; }
  }

  function tick() {
    if (state.connecting || state.stopped) return;
    state.connecting = true;
    refreshAll().then(function () {
      state.connecting = false;
      state.failures = 0;
    }).catch(function (error) {
      state.connecting = false;
      state.failures += 1;
      if (state.hooks.onError) state.hooks.onError(error);
      if (state.failures >= 3) {
        stopPolling();
        state.live = false;
        state.error = error;
        closeStream();
        if (state.hooks.onMode) state.hooks.onMode(false, null, error);
      }
    });
  }

  function startPolling() {
    stopPolling();
    if (state.stopped) return;
    state.timer = setInterval(tick, POLL_MS);
  }

  // ----------------------------------------------------------------- init --
  function connect() {
    return requestJSON('/api/health').then(function (health) {
      if (!health || health.ok !== true) throw new Error('health 未返回 ok=true');
      state.health = health;
      state.live = true;
      state.stopped = false;
      state.failures = 0;
      state.sseFailures = 0;
      if (state.hooks.onMode) state.hooks.onMode(true, health);
      return loadTeam().catch(function (error) {
        state.teamError = error;
        if (state.hooks.onTeamError) state.hooks.onTeamError(error);
        return null;
      }).then(function () {
        return refreshAll();
      }).then(function () {
        return loadScenes();
      }).then(function () {
        startPolling();
        return state;
      });
    });
  }

  function loadTeam() {
    return requestJSON('/api/team').then(function (team) {
      if (!team || typeof team !== 'object') throw new Error('/api/team 返回为空');
      if (team.ok === false) throw new Error(team.error || '/api/team 失败');
      state.team = team;
      state.teamError = null;
      if (state.hooks.onTeam) state.hooks.onTeam(team);
      return team;
    });
  }

  // ---------------------------------------------------------------- scenes --
  // B15 场景台数据层（设计文档 §4.5）。只做接入与归一化，渲染在 index.html hooks。
  // GET /api/scenes -> {ok, scenes:[SceneSpec...], pack_id, generated_at, spec_version, contract_errors}
  // POST /api/scenes/derive {prompt?|metric_id?|data_ref?} -> 合法 SceneSpec / {ok:false,cannot_derive}
  function sceneListPayload(payload) {
    var body = payload || {};
    var scenes = Array.isArray(body.scenes) ? body.scenes.slice() : (Array.isArray(body) ? body.slice() : []);
    var errors = [];
    (body.contract_errors || body.warnings || []).forEach(function (item) {
      if (!item) return;
      errors.push(typeof item === 'string' ? item : (item.reason || item.error || item.code || JSON.stringify(item)));
    });
    return {
      scenes: scenes,
      errors: errors,
      meta: {
        pack_id: body.pack_id || '',
        generated_at: body.generated_at || '',
        spec_version: body.spec_version || 'scene.v1',
        contract_errors: errors
      }
    };
  }

  function mergeScenes(base, extra) {
    var list = (base || []).slice();
    (extra || []).forEach(function (item) {
      if (!item || !item.scene_id) return;
      var hit = false;
      list.forEach(function (one) { if (one && one.scene_id === item.scene_id) hit = true; });
      if (!hit) list.push(item);
    });
    return list;
  }

  function rememberDerived(spec) {
    if (!spec || !spec.scene_id) return;
    var hit = state.derivedScenes.filter(function (item) { return item && item.scene_id === spec.scene_id; })[0];
    if (hit) { Object.keys(spec).forEach(function (key) { hit[key] = spec[key]; }); return; }
    state.derivedScenes = state.derivedScenes.concat([spec]);
  }

  function loadScenes() {
    return requestJSON('/api/scenes').then(function (payload) {
      var parsed = sceneListPayload(payload);
      state.scenes = mergeScenes(parsed.scenes, state.derivedScenes);
      state.sceneMeta = parsed.meta;
      state.sceneError = parsed.errors.length ? parsed.errors.join('；') : null;
      var wanted = state.sceneParam || state.sceneId;
      var match = null;
      if (wanted) match = state.scenes.filter(function (s) { return s && s.scene_id === wanted; })[0] || null;
      if (!match && state.scenes.length) match = state.scenes[0];
      state.sceneId = match ? match.scene_id : null;
      state.scene = match;
      if (state.hooks.onScenes) state.hooks.onScenes(state.scenes, state.sceneMeta, state.sceneError);
      if (match && state.hooks.onScene) state.hooks.onScene(match, state.sceneMeta);
      return state.scenes;
    }).catch(function (error) {
      state.scenes = [];
      state.scene = null;
      state.sceneError = error && error.message ? error.message : String(error);
      state.sceneMeta = { pack_id: '', generated_at: '', spec_version: 'scene.v1', contract_errors: [state.sceneError] };
      if (state.hooks.onScenes) state.hooks.onScenes([], state.sceneMeta, state.sceneError);
      return [];
    });
  }

  function selectScene(sceneId) {
    var id = String(sceneId || '');
    var match = state.scenes.filter(function (s) { return s && s.scene_id === id; })[0] || null;
    state.sceneParam = id || null;
    state.sceneId = match ? match.scene_id : null;
    state.scene = match;
    try {
      var url = match ? ('?scene=' + encodeURIComponent(match.scene_id)) : location.pathname;
      history.replaceState(null, '', url);
    } catch (err) { /* file:// 忽略 */ }
    if (match && state.hooks.onScene) state.hooks.onScene(match, state.sceneMeta);
    return match;
  }

  function deriveScene(options) {
    var opts = options || {};
    // 双模式（Lead 契约 m01016）：显式 spec → 校验模式；否则生成模式（prompt/metric_id/data_ref）
    if (opts.spec && typeof opts.spec === 'object') {
      return postJSON('/api/scenes/derive', { spec: opts.spec }).then(function (result) {
        var payload = result || {};
        if (payload.ok === false) {
          var reasons = payload.cannot_derive || payload.reasons || payload.error || 'SceneSpec 未通过校验';
          var error = new Error(Array.isArray(reasons) ? reasons.join('；') : String(reasons));
          error.payload = payload;
          throw error;
        }
        return payload;
      });
    }
    var body = {};
    ['prompt', 'metric_id', 'data_ref'].forEach(function (key) {
      if (opts[key] != null && String(opts[key]).trim() !== '') body[key] = String(opts[key]).trim();
    });
    if (!Object.keys(body).length) throw new Error('派生场景需至少提供 prompt / metric_id / data_ref 之一（或传 spec 走校验模式）');
    return postJSON('/api/scenes/derive', body).then(function (result) {
      var payload = result || {};
      if (payload.ok === false) {
        var reasons = payload.cannot_derive || payload.reasons || payload.error || '无法派生合法 SceneSpec';
        throw new Error(Array.isArray(reasons) ? reasons.join('；') : String(reasons));
      }
      var spec = payload.scene || payload.spec || null;
      if (!spec || !spec.scene_id) throw new Error('派生结果缺少合法 SceneSpec（scene_id）');
      var replaced = false;
      state.scenes = state.scenes.map(function (item) {
        if (item && item.scene_id === spec.scene_id) { replaced = true; return spec; }
        return item;
      });
      if (!replaced) state.scenes = state.scenes.concat([spec]);
      rememberDerived(spec);
      if (state.hooks.onScenes) state.hooks.onScenes(state.scenes, state.sceneMeta, state.sceneError);
      selectScene(spec.scene_id);
      return spec;
    });
  }

  function sceneForTask(detail) {
    var instance = detail && detail.scene;
    if (!instance || typeof instance !== 'object') return null;
    var id = instance.scene_id || (instance.spec && instance.spec.scene_id) || '';
    var spec = id ? state.scenes.filter(function (s) { return s && s.scene_id === id; })[0] : null;
    return { scene_id: id, spec: spec || null, instance: instance };
  }

  function init(hooks) {
    state.hooks = hooks || {};
    state.taskParam = queryParam('task');
    state.sceneParam = queryParam('scene');
    return connect().catch(function (error) {
      state.live = false;
      state.error = error;
      stopPolling();
      closeStream();
      if (state.hooks.onMode) state.hooks.onMode(false, null, error);
      return state;
    });
  }

  function reconnect() {
    stopPolling();
    closeStream();
    return connect().catch(function (error) {
      state.live = false;
      state.error = error;
      if (state.hooks.onMode) state.hooks.onMode(false, null, error);
      return state;
    });
  }

  // -------------------------------------------------------------- actions --
  function requireTaskId(taskId) {
    var id = taskId || state.currentTaskId;
    if (!id) throw new Error('当前没有选中任务');
    return id;
  }

  function selectTask(taskId) {
    var id = String(taskId || '');
    if (!id) return Promise.resolve(state);
    if (id === state.currentTaskId) return refreshAll();
    state.currentTaskId = id;
    state.taskParam = id;
    resetTaskState();
    try { history.replaceState(null, '', '?task=' + encodeURIComponent(id)); } catch (err) { /* file:// 下忽略 */ }
    return refreshAll();
  }

  function create(intake, files) {
    var body = Object.assign({}, intake || {});
    if (files && files.length) body.data_files = files;
    var runner = body.runner;
    delete body.runner;
    return postJSON('/api/tasks', body).then(function (created) {
      if (runner) state.runner = runner;
      if (created && created.task_id) {
        state.currentTaskId = created.task_id;
        state.taskParam = created.task_id;
        resetTaskState();
        try { history.replaceState(null, '', '?task=' + encodeURIComponent(created.task_id)); } catch (err) { /* ignore */ }
      }
      return refreshAll().then(function () { return created; });
    });
  }

  function uploadData(files) {
    var id = requireTaskId();
    return postJSON('/api/tasks/' + encodeURIComponent(id) + '/data', { files: files || [] }).then(function (result) {
      return refreshAll().then(function () { return result; });
    });
  }

  // B10：上传 Excel/CSV（+ 可选字段映射 YAML）让服务端转 T/T30 数据文件。
  // 契约：POST /api/tasks/<id>/data multipart
  //   table(.csv/.xlsx/.xls ≤8MB) / field_map(可选 YAML) / line / t_start / t_end / base_start / base_end
  //   成功 {ok,t_records,t30_records,files:[{path,sha256,size}]}；失败 4xx/5xx {error}
  function uploadTable(taskId, options) {
    var id = requireTaskId(taskId);
    var opts = options || {};
    var table = opts.file || opts.table || null;
    if (!table) throw new Error('请选择 CSV / Excel 表格文件');
    if (typeof FormData !== 'function') throw new Error('当前浏览器不支持 multipart 上传');
    var form = new FormData();
    form.append('table', table, table.name || 'table.csv');
    if (opts.fieldMapFile) form.append('field_map', opts.fieldMapFile, opts.fieldMapFile.name || 'field-map.yaml');
    ['line', 't_start', 't_end', 'base_start', 'base_end'].forEach(function (key) {
      var value = opts[key];
      if (value != null && String(value).trim() !== '') form.append(key, String(value).trim());
    });
    return requestJSON('/api/tasks/' + encodeURIComponent(id) + '/data', {
      method: 'POST',
      body: form
    }).then(function (result) {
      var body = result || {};
      if (body.ok === false) throw new Error(body.error || body.message || '上传转换失败');
      state.lastUpload = body;
      if (state.hooks.onUpload) state.hooks.onUpload(body, id);
      return refreshTask().catch(function () { return null; }).then(function () {
        return refreshAll().then(function () { return body; });
      });
    });
  }

  function run(taskId, runner) {
    var id = requireTaskId(taskId);
    var mode = runner || state.runner || 'fixture';
    state.runner = mode;
    return postJSON('/api/tasks/' + encodeURIComponent(id) + '/run', { runner: mode }).then(function (result) {
      setTimeout(tick, 500);
      return result;
    });
  }

  function sign(signer, taskId) {
    var id = requireTaskId(taskId);
    var body = { signer: String(signer == null ? '' : signer).trim() || '老板' };
    return postJSON('/api/tasks/' + encodeURIComponent(id) + '/sign', body).then(function (result) {
      state.signature = {
        report_sha256: result.sha256,
        signer: result.signer || body.signer,
        signed_at: result.signed_at || ''
      };
      if (state.hooks.onSign) state.hooks.onSign(result, state.model);
      return refreshAll().then(function () { return result; });
    });
  }

  function gate(action, confirmer, comment) {
    var id = requireTaskId();
    var body = {
      action: action === 'reject' ? 'reject' : 'confirm',
      confirmer: String(confirmer == null ? '' : confirmer).trim() || '老板',
      comment: String(comment == null ? '' : comment)
    };
    return postJSON('/api/tasks/' + encodeURIComponent(id) + '/gate', body).then(function (result) {
      state.gate = result && (result.gate || result);
      if (state.hooks.onGate) state.hooks.onGate(state.gate, state.model);
      return refreshTask().then(function () { return result; });
    });
  }

  function finalize(taskId) {
    var id = requireTaskId(taskId);
    return postJSON('/api/tasks/' + encodeURIComponent(id) + '/finalize', {}).then(function (result) {
      return refreshAll().then(function () { return result; });
    });
  }

  // T+7 回填：只认 server/app.py 的 OUTCOME_FIELDS 六个字段，多传一律 400。
  var OUTCOME_FIELDS = ['observed_outcome', 'actual_action', 'override_reason',
                        'action_cost', 'increment_value', 'outcome_window'];

  function outcomeValue(value) {
    if (value === undefined || value === null) return undefined;
    if (typeof value !== 'string') return value;
    var text = value.trim();
    if (!text) return undefined;
    if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
    if (text.charAt(0) === '{' || text.charAt(0) === '[') {
      try {
        var parsed = JSON.parse(text);
        if (parsed && typeof parsed === 'object') return parsed;
      } catch (err) { /* 非法 JSON：原样上送，由服务端给可读错误 */ }
    }
    return text;
  }

  function postOutcome(taskId, payload) {
    var id = requireTaskId(taskId);
    var source = payload || {};
    var body = {};
    OUTCOME_FIELDS.forEach(function (key) {
      var value = outcomeValue(source[key]);
      if (value === undefined) return;
      if (key === 'outcome_window' && /^\d{1,4}$/.test(String(value))) value = String(value) + 'd';
      body[key] = value;
    });
    if (body.observed_outcome === undefined) throw new Error('observed_outcome 必填（T+7 实测值/描述）');
    return postJSON('/api/tasks/' + encodeURIComponent(id) + '/outcome', body).then(function (result) {
      return refreshAll().then(function () { return result; });
    });
  }

  // ------------------------------------------------------------ decisions --
  // R4：P3 决策日志（跨任务时间线）。
  // GET /api/decisions?scene=&entity=&verdict=&conclusion=&overridden=&filled=&limit=
  //   -> {ok, count, total, matched, decisions:[...], facets:{scenes,entities,verdicts}}
  // 只读端点：服务端扫 runs/*/report.json + outcome.json，前端不另建事实源。
  function decisionListPayload(payload) {
    var body = payload || {};
    var rows = Array.isArray(body.decisions) ? body.decisions.slice() : [];
    var facets = body.facets && typeof body.facets === 'object' ? body.facets : {};
    return {
      decisions: rows,
      facets: {
        scenes: Array.isArray(facets.scenes) ? facets.scenes.slice() : [],
        entities: Array.isArray(facets.entities) ? facets.entities.slice() : [],
        verdicts: Array.isArray(facets.verdicts) ? facets.verdicts.slice() : []
      },
      total: typeof body.total === 'number' ? body.total : rows.length,
      matched: typeof body.matched === 'number' ? body.matched : rows.length,
      generated_at: body.generated_at || ''
    };
  }

  function listDecisions(filters) {
    var f = filters || {};
    var query = [];
    ['scene', 'entity', 'verdict', 'conclusion', 'overridden', 'filled', 'limit'].forEach(function (key) {
      var value = f[key];
      if (value === undefined || value === null || value === '') return;
      query.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
    });
    var path = '/api/decisions' + (query.length ? ('?' + query.join('&')) : '');
    return requestJSON(path).then(function (payload) {
      var parsed = decisionListPayload(payload);
      state.decisions = parsed.decisions;
      state.decisionFacets = parsed.facets;
      state.decisionMeta = { total: parsed.total, matched: parsed.matched, generated_at: parsed.generated_at };
      if (state.hooks.onDecisions) state.hooks.onDecisions(state.decisions, state.decisionFacets, state.decisionMeta);
      return state.decisions;
    }).catch(function (error) {
      state.decisions = [];
      state.decisionFacets = { scenes: [], entities: [], verdicts: [] };
      state.decisionMeta = { total: 0, matched: 0, generated_at: '' };
      state.decisionError = error && error.message ? error.message : String(error);
      if (state.hooks.onDecisions) state.hooks.onDecisions([], state.decisionFacets, state.decisionMeta);
      return [];
    });
  }

  function currentCrewForUi() {
    var task = (state.model && state.model.task) || (state.detail && state.detail.task) || null;
    return currentCrew(task);
  }

  // --------------------------------------------------------------- export --
  window.APIAdapter = {
    API_BASE: API_BASE,
    state: state,
    init: init,
    reconnect: reconnect,
    refresh: refreshAll,
    refreshTask: refreshTask,
    loadTeam: loadTeam,
    loadScenes: loadScenes,
    listScenes: loadScenes,
    selectScene: selectScene,
    deriveScene: deriveScene,
    sceneForTask: sceneForTask,
    scenes: function () { return state.scenes.slice(); },
    scene: function () { return state.scene; },
    sceneMeta: function () { return state.sceneMeta; },
    selectTask: selectTask,
    create: create,
    uploadData: uploadData,
    uploadTable: uploadTable,
    run: run,
    sign: sign,
    gate: gate,
    finalize: finalize,
    postOutcome: postOutcome,
    listDecisions: listDecisions,
    decisions: function () { return state.decisions.slice(); },
    decisionFacets: function () { return state.decisionFacets; },
    isLive: function () { return state.live; },
    currentTaskId: function () { return state.currentTaskId; },
    currentDetail: function () { return state.detail; },
    currentModel: function () { return state.model; },
    currentLog: function () { return state.log.slice(); },
    events: function () { return state.events.slice(); },
    tasks: function () { return state.tasks.slice(); },
    beats: function () { return state.beats.slice(); },
    team: function () { return state.team; },
    crew: currentCrewForUi,
    runner: function () { return state.runner; },
    setRunner: function (value) { state.runner = value === 'external' ? 'external' : 'fixture'; return state.runner; },
    sseLive: function () { return state.sseLive; },
    summarize: summarize,
    stateLabel: stateLabel,
    stateDot: stateDot,
    spriteKeyFor: spriteKeyFor,
    employeeFor: employeeFor,
    roleName: function (key) { return ROLE_NAME[key] || key; },
    kindLabel: function (kind) { return KIND_LABEL[kind] || kind || '状态'; },
    tools: {
      scoreInfo: scoreInfo, refsText: refsText, moneyOf: moneyOf, trunc: trunc,
      taskTitle: taskTitle, stateLabel: stateLabel
    }
  };
})();
