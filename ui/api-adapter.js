/* ui/api-adapter.js -- M1 构建规格 §2.7 真数据适配层（无第三方依赖，ES2017）。
 *
 * 职责（只做数据接入，不改视觉）：
 *   1. 启动时 GET /api/health：成功 -> 真数据模式；失败（file:// 直开 / 服务未起）-> 静默保持静态演示。
 *   2. 真数据模式：轮询 /api/tasks、/api/tasks/<id>、/api/tasks/<id>/log?after=n，
 *      把 report.json / status.json / run.log 归一化成 model / beats 交给 index.html 的 hooks 渲染。
 *   3. 写操作：create(POST /api/tasks)、run(POST /run)、sign(POST /sign -> sha256)、finalize(POST /finalize)。
 *
 * 设计约束：本文件不直接改 DOM（渲染留在 index.html 内联脚本的 hooks 里），避免与现有动画互踩；
 *           任何 fetch 失败都不得抛出到控制台打断页面（file:// 打开时必须还能看静态演示）。
 *
 * 舞台 beat 契约：{role, tag, who, frame, text, pos, short} 与 index.html 的 BEATS 同构；
 * run.log 的 phase ∈ s1..s5|gate 映射为 5 幕（s1 感知 / s2 因素 / s3 归因 / s4 建议 / s5 收口）。
 */
(function () {
  'use strict';

  var API_BASE = ''; // 同源（index.html 由 server/app.py 提供时）
  var POLL_MS = 2500;
  var PHASE_ORDER = ['s1', 's2', 's3', 's4', 's5'];
  var PHASE_LABEL = { s1: '快照', s2: '因素', s3: '排除', s4: '建议', s5: '收口', gate: '校验门' };
  var PHASE_ROLE_HINT = { s1: 'analyst', s2: 'biz', s3: 'attr', s4: 'biz', s5: 'host', gate: 'host' };
  var ROLE_NAME = { host: '工作助理', analyst: '咨询分析师', attr: 'AI 归因专家', biz: '制造业业务专家' };
  var ROLE_POS = { host: [50, 36], analyst: [24, 56], attr: [76, 56], biz: [50, 64] };
  var ROLE_ALIAS = {
    'host': 'host', '工作助理': 'host',
    'consulting-analyst': 'analyst', '咨询分析师': 'analyst', 'data-fetcher': 'analyst', '取数员': 'analyst',
    'ai-attribution-expert': 'attr', 'AI 归因专家': 'attr',
    'manufacturing-ops-expert': 'biz', '制造业业务专家': 'biz', '业务专家': 'biz'
  };
  var SIX_DIMS = ['data_completeness', 'timeliness', 'significance', 'exclusivity', 'history', 'human_ai_consistency'];

  var state = {
    live: false,
    connecting: false,
    stopped: false,
    health: null,
    error: null,
    tasks: [],
    taskParam: null,
    currentTaskId: null,
    detail: null,
    model: null,
    log: [],
    logAfter: 0,
    beats: [],
    beatSource: '',
    signature: null,
    hooks: {},
    timer: null,
    failures: 0,
    lastBeatsKey: ''
  };

  // ---------------------------------------------------------------- utils --
  function has(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  function queryParam(name) {
    try {
      return new URLSearchParams(window.location.search).get(name);
    } catch (err) {
      var match = new RegExp('[?&]' + name + '=([^&]*)').exec(window.location.search || '');
      return match ? decodeURIComponent(match[1]) : null;
    }
  }

  function toast(message) {
    try {
      if (typeof window.toast === 'function') window.toast(message);
    } catch (err) { /* 静态演示页没有 toast 也不该崩 */ }
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
      if (ref && typeof ref === 'object') names.push(ref.path || ref.file || '');
      else names.push(String(ref || ''));
    });
    names = names.filter(Boolean).map(function (name) {
      return String(name).split(/[\\/]/).pop();
    });
    return names.length ? names.join(' / ') : '—';
  }

  function scoreInfo(scorecard, report) {
    var sc = scorecard || {};
    var doc = report || {};
    var sum = 0;
    var dims = 0;
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
  function buildModel(detail) {
    var report = (detail && detail.report) || null;
    var status = (detail && detail.status) || {};
    var signature = (detail && detail.signature) || null;
    var task = (detail && detail.task) || {};
    if (!report) {
      return {
        report: null, status: status, signature: signature, task: task,
        title: taskTitle(task), score: scoreInfo(null, null), roi: null,
        finding: [], cannot: [], recommendation: [], log: null, pack: null,
        refs: [], isDemo: false, state: status.state || 'created', step: status.step || ''
      };
    }
    var recommendation = report.recommendation || [];
    var first = recommendation[0] || {};
    var decisionLog = report.decision_log_entry || {};
    var refs = decisionLog.data_refs;
    if (refs && !Array.isArray(refs)) refs = [refs];
    return {
      report: report,
      status: status,
      signature: signature,
      task: task,
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
      state: status.state || '',
      step: status.step || '',
      decisionStatus: signature ? 'signed' : 'pending'
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
    var review = list.filter(function (task) { return task && task.signed && !task.reviewed; }).length;
    var dues = list.map(function (task) { return task && task.review_due; }).filter(Boolean).sort();
    return {
      total: list.length,
      running: running,
      week: week,
      review: review,
      delivered: withReport.length,
      nextDue: dues.length ? dues[0] : null
    };
  }

  // ---------------------------------------------------------------- beats --
  function makeBeat(role, phase, text) {
    var id = ROLE_NAME[role] ? role : 'host';
    return {
      role: id,
      tag: phase.toUpperCase() + ' ' + ROLE_NAME[id],
      who: ROLE_NAME[id],
      frame: (phase === 's2' || phase === 's4') ? 'talk' : 'think',
      text: trunc(text, 220),
      pos: ROLE_POS[id],
      short: '第 ' + phase.slice(1) + ' 幕 · ' + (PHASE_LABEL[phase] || phase)
    };
  }

  function mapRole(role, phase) {
    var key = String(role == null ? '' : role).trim();
    if (key && has(ROLE_NAME, key)) return key;
    if (key && has(ROLE_ALIAS, key)) return ROLE_ALIAS[key];
    return PHASE_ROLE_HINT[phase] || 'host';
  }

  function beatsFromLog(events, detail) {
    var byPhase = {};
    (events || []).forEach(function (event) {
      if (event && event.phase && !has(byPhase, event.phase)) byPhase[event.phase] = event;
    });
    var beats = [];
    PHASE_ORDER.forEach(function (phase) {
      var event = byPhase[phase];
      if (!event) return;
      var role = mapRole(event.role, phase);
      beats.push(makeBeat(role, phase, event.line));
    });
    if (beats.length >= 2) {
      state.beatSource = 'run.log';
      return beats;
    }
    var fromReport = beatsFromReport(detail);
    if (fromReport.length >= 2) {
      state.beatSource = 'report.json';
      return fromReport;
    }
    state.beatSource = beats.length ? 'run.log' : '';
    return beats;
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
    beats.push(makeBeat('analyst', 's1',
      '读取数据：' + refsText(log.data_refs) + '；目标：' + (report.goal || '—')));
    beats.push(makeBeat('biz', 's2',
      '六维打分：' + (score.total == null ? '—' : score.total + '/100') +
      (score.verdict ? '（' + score.verdict + '）' : '') +
      (report.pack && report.pack.pack_id ? '；行业包 ' + report.pack.pack_id : '')));
    beats.push(makeBeat('attr', 's3', finding.length
      ? '主因：' + (first.factor || '—') + '（置信 ' + (first.confidence == null ? '—' : first.confidence) +
        '）；cannot_attribute ' + (report.cannot_attribute || []).length + ' 条'
      : '不可归因：证据不足（cannot_attribute ' + (report.cannot_attribute || []).length + ' 条），建议先补数据'));
    beats.push(makeBeat('biz', 's4', rec.action
      ? rec.action
      : 'ROI 锚点：' + ((rec.roi && rec.roi.anchor) || '—')));
    beats.push(makeBeat('host', 's5',
      '交付卡 ' + (log.id || '—') + ' · ' + (score.verdict || '待复核') +
      '；下一步：' + (log.expected_result || 'T+7 回看')));
    return beats;
  }

  function beatsKey(beats) {
    return beats.map(function (beat) { return beat.role + '|' + beat.text; }).join('§');
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
    return tasks.length ? tasks[0].task_id : null;
  }

  function refreshAll() {
    return requestJSON('/api/tasks').then(function (tasks) {
      state.tasks = Array.isArray(tasks) ? tasks : [];
      if (state.hooks.onTasks) state.hooks.onTasks(state.tasks, summarize(state.tasks));
      var wanted = pickCurrentId(state.tasks);
      if (!wanted) {
        state.currentTaskId = null;
        state.detail = null;
        state.model = null;
        state.log = [];
        state.logAfter = 0;
        state.beats = [];
        state.signature = null;
        if (state.hooks.onTask) state.hooks.onTask(null, summarize(state.tasks));
        return state;
      }
      var switched = wanted !== state.currentTaskId;
      state.currentTaskId = wanted;
      if (switched) {
        state.log = [];
        state.logAfter = 0;
        state.beats = [];
        state.lastBeatsKey = '';
      }
      var base = '/api/tasks/' + encodeURIComponent(wanted);
      return requestJSON(base).then(function (detail) {
        state.detail = detail;
        state.model = buildModel(detail);
        state.signature = state.model.signature;
        var logPath = base + '/log?after=' + (switched ? 0 : state.logAfter);
        return requestJSON(logPath).then(function (events) {
          if (Array.isArray(events) && events.length) {
            state.log = switched ? events.slice() : state.log.concat(events);
            var last = events[events.length - 1];
            if (last && typeof last.n === 'number' && last.n > state.logAfter) state.logAfter = last.n;
          } else if (switched) {
            state.log = [];
          }
          var beats = beatsFromLog(state.log, state.detail);
          if (beats.length >= 2) {
            state.beats = beats;
            var key = beatsKey(beats);
            if (key !== state.lastBeatsKey) {
              state.lastBeatsKey = key;
              if (state.hooks.onBeats) state.hooks.onBeats(beats, state.beatSource);
            }
          }
          if (state.hooks.onTask) state.hooks.onTask(state.model, summarize(state.tasks));
          if (state.hooks.onSignature) state.hooks.onSignature(state.signature, state.model);
          return state;
        });
      });
    });
  }

  // ---------------------------------------------------------------- poll --
  function stopPolling() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
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
      if (state.hooks.onMode) state.hooks.onMode(true, health);
      return refreshAll().then(function () {
        startPolling();
        return state;
      });
    });
  }

  function init(hooks) {
    state.hooks = hooks || {};
    state.taskParam = queryParam('task');
    return connect().catch(function (error) {
      state.live = false;
      state.error = error;
      stopPolling();
      if (state.hooks.onMode) state.hooks.onMode(false, null, error);
      return state;
    });
  }

  function reconnect() {
    stopPolling();
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

  function create(intake) {
    return postJSON('/api/tasks', intake || {}).then(function (created) {
      if (created && created.task_id) {
        state.currentTaskId = created.task_id;
        state.taskParam = created.task_id;
      }
      return refreshAll().then(function () { return created; });
    });
  }

  function run(taskId) {
    var id = requireTaskId(taskId);
    return postJSON('/api/tasks/' + encodeURIComponent(id) + '/run', {}).then(function (result) {
      setTimeout(tick, 400);
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

  function finalize(taskId) {
    var id = requireTaskId(taskId);
    return postJSON('/api/tasks/' + encodeURIComponent(id) + '/finalize', {}).then(function (result) {
      return refreshAll().then(function () { return result; });
    });
  }

  // --------------------------------------------------------------- export --
  window.APIAdapter = {
    API_BASE: API_BASE,
    state: state,
    init: init,
    reconnect: reconnect,
    refresh: refreshAll,
    create: create,
    run: run,
    sign: sign,
    finalize: finalize,
    isLive: function () { return state.live; },
    currentTaskId: function () { return state.currentTaskId; },
    currentDetail: function () { return state.detail; },
    currentModel: function () { return state.model; },
    currentLog: function () { return state.log.slice(); },
    tasks: function () { return state.tasks.slice(); },
    beats: function () { return state.beats.slice(); },
    summarize: summarize,
    tools: {
      scoreInfo: scoreInfo,
      refsText: refsText,
      moneyOf: moneyOf,
      trunc: trunc,
      taskTitle: taskTitle
    }
  };
})();
