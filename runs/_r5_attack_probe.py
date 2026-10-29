# -*- coding: utf-8 -*-
"""第五轮独立攻击复测（裁判自建，不复用 tests/test_f6_f11.py 的 payload）。"""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(r'D:\work\产品类\个人产品\bot-2\JuJing\bot-team')
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from core import gates  # noqa: E402

base = json.loads((ROOT / 'runs/T-20261028-054/report.json').read_text(encoding='utf-8-sig'))
OUT = []


def rec(tag, reasons, expect_reject=True):
    rejected = bool(reasons)
    ok = rejected == expect_reject
    OUT.append((tag, ok, rejected, reasons))
    print(('[OK]  ' if ok else '[FAIL]') + f' {tag} rejected={rejected}')
    for x in reasons:
        print('        - ' + str(x)[:200])


print('=== 攻击① 伪造 security_gate.action=调整排班 + decision.security_gate.confirmed_by ===')


def forge(action_report, action_decision, conf='kf41'):
    r = copy.deepcopy(base)
    r['security_gate'] = {'status': 'confirmed', 'action': action_report, 'confirmed_by': conf}
    r['decision_log_entry']['security_gate'] = {'status': 'confirmed', 'action': action_report,
                                                'requested_by': 'x', 'confirmed_by': conf}
    dec = {'status': 'proposed', 'task_id': r.get('task_id'),
           'security_gate': {'status': 'confirmed', 'action': action_decision, 'confirmed_by': conf}}
    return r, dec


sig = {'signer': 'kf41', 'report_sha256': 'x', 'task_id': base.get('task_id')}

# ①-1 报告侧+决策侧都伪造「调整排班」（非 pack 不可逆动作）
r, dec = forge('调整排班', '调整排班')
rec('①-1 两侧均伪造 调整排班', gates.check_finalize(dec, r, sig, ('kf41',), True))

# ①-2 只在报告侧伪造，决策侧合法（line_stop）
r, dec = forge('调整排班', 'line_stop')
rec('①-2 仅报告侧伪造 调整排班', gates.check_finalize(dec, r, sig, ('kf41',), True))

# ①-3 只在决策侧伪造，报告侧合法
r, dec = forge('line_stop', '调整排班')
rec('①-3 仅决策侧伪造 调整排班', gates.check_finalize(dec, r, sig, ('kf41',), True))

# ①-4 带修饰词/空白变体
for act in (' 调整排班 ', '调整排班（不占产线）', '排班微调', 'ADJUST_SHIFT'):
    r, dec = forge(act, act)
    rec(f'①-4 变体 {act!r}', gates.check_finalize(dec, r, sig, ('kf41',), True))

# ①-5 隐蔽洞：两侧都是 pack 合法不可逆动作，但互不相同（确认 A 执行 B）
r, dec = forge('line_stop', 'mold_change')
rec('①-5 line_stop vs mold_change 两侧合法但互不相同', gates.check_finalize(dec, r, sig, ('kf41',), True))

# ①-6 只有 dl.security_gate 伪造，顶层/决策侧合法
r, dec = forge('line_stop', 'line_stop')
r['decision_log_entry']['security_gate'] = {'status': 'confirmed', 'action': '调整排班', 'confirmed_by': 'kf41'}
rec('①-6 仅 decision_log_entry.security_gate.action 伪造', gates.check_finalize(dec, r, sig, ('kf41',), True))

# ①-7 对照组：全合法 + gate_confirmed=True 应当「除不可签外无 pack 相关拒绝」
r, dec = forge('line_stop', 'line_stop')
r['security_gate'] = {'status': 'pending_user_confirmation', 'requested_by': 'x', 'requested_at': '2026-10-26',
                      'reason': 'y', 'action': 'line_stop'}
r['decision_log_entry']['security_gate'] = {'status': 'confirmed', 'action': 'line_stop', 'confirmed_by': 'kf41'}
ctrl = gates.check_finalize(dec, r, sig, ('kf41',), True)
print('  对照组拒绝原因:')
for x in ctrl:
    print('        * ' + str(x)[:200])
print('  对照组含 pack/不可逆 拒绝:', any(('pack' in x or '不可逆' in x) for x in ctrl))

print()
print('=== 攻击② pack 畸形 + gate action 伪造 → real_report_errors 必须报错 ===')
for shape in (None, [], {}, {'pack_id': ''}, {'pack_id': None}, 'nonexistent_pack', 123,
              {'pack_id': ['a']}, {'pack_id': '../../packs/manufacturing'}):
    r = copy.deepcopy(base)
    r['pack'] = shape
    r['security_gate'] = {'status': 'pending_user_confirmation', 'action': '调整排班'}
    errs = gates.real_report_errors(r, ROOT)
    hit = [e for e in errs if ('pack' in e or '不可逆' in e)]
    rec(f'② pack={shape!r}', hit)

print()
print('=== 攻击③ sum(points)==total & consistency_errors ===')
sc = base['scorecard']
s = round(sum(sc['points'].values()), 2)
print('  total =', sc['total'], '| sum(points) =', s, '| equal =', s == sc['total'])
import core.report_contract as rc
print('  consistency_errors =', rc.consistency_errors(sc))
print('  scorecard_errors   =', gates.scorecard_errors(sc))
print('  closure.total =', json.loads((ROOT / 'runs/T-20261028-054/loop/closure.json').read_text(encoding='utf-8-sig'))['total'])

bad = [t for t, ok, _, _ in OUT if not ok]
print()
print(f'攻击用例: {len(OUT) - len(bad)}/{len(OUT)} 符合预期' + ('' if not bad else ' | 异常: ' + '; '.join(bad)))
sys.exit(0)
