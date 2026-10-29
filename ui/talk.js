/* 团长对话 + 今日提醒（MVP E6 前端）
 * - 提醒：GET /api/reminders（服务端首次读取会落 issued 事件），判定 POST /api/reminders/judge
 * - 对话：POST /api/dialog（第一轮不带 dialog_id，之后带上）
 * 纯 IIFE，无依赖；失败一律 toast，不静默吞。
 */
(function () {
  'use strict';

  var state = { reminders: null, dialogId: '', messages: [], busy: false, rounds: 0 };

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(msg, bad) {
    var t = el('talkToast');
    if (!t) { return; }
    t.textContent = msg;
    t.className = 'talk-toast on' + (bad ? ' bad' : '');
    clearTimeout(t._h);
    t._h = setTimeout(function () { t.className = 'talk-toast'; }, 2600);
  }
  function api(method, path, body) {
    var opt = { method: method, headers: { 'Content-Type': 'application/json' }, cache: 'no-store' };
    if (body !== undefined) { opt.body = JSON.stringify(body); }
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok || d.ok === false) { throw new Error(d.error || ('HTTP ' + r.status)); }
        return d;
      });
    });
  }

  /* ---------- 提醒 ---------- */
  function reminderRow(r, primary) {
    var cls = 'talk-rem' + (primary ? ' primary' : '');
    var due = r.overdue_days != null ? ('逾期 ' + r.overdue_days + ' 天') : (r.reason || '');
    return '<div class="' + cls + '" data-id="' + esc(r.item_id) + '">' +
      '<div class="talk-rem-h"><b>' + esc(r.title || r.item_id) + '</b>' +
      '<span class="talk-tag">' + esc(r.kind_label || r.kind || '') + '</span></div>' +
      '<div class="talk-rem-m">' + esc(r.item_id) + ' · ' + esc(r.owner || '未指派') + ' · ' + esc(due) + '</div>' +
      (r.suppressed_today
        ? '<div class="talk-rem-done">今天已判过，不再提醒</div>'
        : '<div class="talk-rem-b">' +
          '<button class="talk-btn ok" data-j="ok">准，我处理</button>' +
          '<button class="talk-btn no" data-j="not_ok">不准</button>' +
          '<button class="talk-btn snooze" data-j="snooze">别烦我</button>' +
          '</div>') +
      '</div>';
  }

  function paintReminders(d) {
    state.reminders = d;
    var host = el('talkRems'), meta = el('talkRemsMeta');
    if (!host) { return; }
    var rows = d.rows || [];
    var main = rows.slice(0, 3), rest = rows.slice(3);
    if (!rows.length) {
      host.innerHTML = '<div class="talk-empty">今天没有到期的活。要么清单是空的，要么你真的干净了。</div>';
    } else {
      host.innerHTML = main.map(function (r) { return reminderRow(r, true); }).join('') +
        (rest.length
          ? '<details class="talk-more"><summary>其余 ' + rest.length + ' 件到期事项</summary>' +
            rest.map(function (r) { return reminderRow(r, false); }).join('') + '</details>'
          : '');
    }
    if (meta) {
      var c = d.counts || {}, s = d.stats || {};
      meta.textContent = '到期 ' + (c.total || 0) + ' 件 · 今日已发 ' + (c.issued_today || 0) +
        ' 条 · 累计判定率 ' + (s.judge_rate == null ? '—' : (s.judge_rate * 100).toFixed(0) + '%');
    }
  }

  function loadReminders() {
    return api('GET', '/api/reminders').then(paintReminders).catch(function (e) {
      toast('提醒读取失败：' + e.message, true);
      if (el('talkRems')) { el('talkRems').innerHTML = '<div class="talk-empty">提醒读取失败：' + esc(e.message) + '</div>'; }
    });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-j]');
    if (!btn) { return; }
    var card = btn.closest('.talk-rem');
    if (!card) { return; }
    var itemId = card.getAttribute('data-id');
    var verdict = btn.getAttribute('data-j');
    btn.disabled = true;
    api('POST', '/api/reminders/judge', { item_id: itemId, verdict: verdict, by: 'owner' })
      .then(function () { toast(verdict === 'ok' ? '记下了：准' : verdict === 'not_ok' ? '记下了：这条不准' : '今天不打扰'); return loadReminders(); })
      .catch(function (err) { btn.disabled = false; toast('判定失败：' + err.message, true); });
  });

  /* ---------- 对话 ---------- */
  function bubble(role, text, extra) {
    return '<div class="talk-msg ' + role + '"><div class="talk-who">' +
      (role === 'user' ? '我' : '团长') + '</div><div class="talk-body">' + esc(text).replace(/\n/g, '<br>') +
      (extra || '') + '</div></div>';
  }

  function paintThread() {
    var host = el('talkThread');
    if (!host) { return; }
    host.innerHTML = state.messages.length
      ? state.messages.map(function (m) { return bubble(m.role, m.text, m.extra); }).join('')
      : '<div class="talk-empty">还没开口。按「让团长先说」，它会从今天的清单里挑一件。</div>';
    host.scrollTop = host.scrollHeight;
  }

  function pushReply(d) {
    state.dialogId = d.dialog_id || state.dialogId;
    state.rounds = d.round || state.rounds;
    var extra = '';
    if (d.status && d.status !== 'open') {
      extra = '<div class="talk-status">已收敛：' + esc(d.status) + (d.decision && d.decision.type && d.decision.type !== 'none'
        ? ' · 决定：' + esc(d.decision.type) : '') + '</div>';
    }
    state.messages.push({ role: 'assistant', text: d.reply || '', extra: extra });
    if (d.unresolved && d.unresolved.length) {
      state.messages.push({
        role: 'assistant',
        text: '未解决：\n- ' + d.unresolved.map(function (u) {
          return typeof u === 'string' ? u : (u.question || JSON.stringify(u));
        }).join('\n- ')
      });
    }
    paintThread();
  }

  function send(text) {
    if (state.busy || !text) { return; }
    state.busy = true;
    state.messages.push({ role: 'user', text: text });
    paintThread();
    var box = el('talkInput');
    if (box) { box.value = ''; }
    if (el('talkSend')) { el('talkSend').disabled = true; }
    var body = { message: text, actor: 'owner' };
    if (state.dialogId) { body.dialog_id = state.dialogId; }
    api('POST', '/api/dialog', body).then(function (d) {
      pushReply(d);
      if (d.status && d.status !== 'open') { state.dialogId = ''; }
    }).catch(function (e) {
      toast('对话失败：' + e.message, true);
      state.messages.push({ role: 'assistant', text: '（模型通道异常：' + e.message + '）' });
      paintThread();
    }).then(function () {
      state.busy = false;
      if (el('talkSend')) { el('talkSend').disabled = false; }
    });
  }

  function kickoff() {
    var top = (state.reminders && state.reminders.rows || []).slice(0, 3).map(function (r) {
      return r.item_id + ' ' + (r.title || '');
    }).join('；');
    send('今天先干什么？' + (top ? '我这边到期的有：' + top + '。你给我排个序，说清先做哪件、为什么。' : '清单里没有到期项。'));
  }

  function wire() {
    var sendBtn = el('talkSend'), box = el('talkInput');
    if (sendBtn) { sendBtn.addEventListener('click', function () { send((box && box.value || '').trim()); }); }
    if (box) {
      box.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) { e.preventDefault(); send((box.value || '').trim()); }
      });
    }
    var kick = el('talkKick');
    if (kick) { kick.addEventListener('click', function () { if (!state.busy) { kickoff(); } }); }
    var refresh = el('talkRefresh');
    if (refresh) { refresh.addEventListener('click', function () { loadReminders(); }); }
    var reset = el('talkReset');
    if (reset) {
      reset.addEventListener('click', function () {
        state.dialogId = ''; state.messages = []; state.rounds = 0; paintThread(); toast('已开新对话');
      });
    }
  }

  window.TalkView = {
    mount: function () {
      wire();
      paintThread();
      loadReminders();
      // AI 先说：每天第一次进来自动开口一次（失败不阻塞）
      try {
        var key = 'talk_auto_' + new Date().toISOString().slice(0, 10);
        if (!localStorage.getItem(key)) {
          localStorage.setItem(key, '1');
          setTimeout(function () { if (!state.busy && !state.messages.length) { kickoff(); } }, 400);
        }
      } catch (e) { /* localStorage 不可用就不自动开口 */ }
    },
    reload: loadReminders
  };
})();
