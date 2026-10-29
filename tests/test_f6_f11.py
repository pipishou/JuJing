# -*- coding: utf-8 -*-
"""F6–F11 负向/正向断言（不依赖 UI 运行）。"""
import copy
import types
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r'D:\work\产品类\个人产品\bot-2\JuJing\bot-team')
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')


import core.gates as gates  # noqa: E402
from core import artifact as art  # noqa: E402
base = json.loads((ROOT / 'runs/T-20261028-054/report.json').read_text(encoding='utf-8-sig'))
CASES = []


def case(name, ok, detail=''):
    CASES.append((name, ok, detail))
    print(('[OK]  ' if ok else '[FAIL]') + ' ' + name + ('' if ok else ' :: ' + str(detail)[:180]))


# F8：三字段不得跳过
for field in ('verdict_one_line', 'sign_scope', 'evidence_tier'):
    r = copy.deepcopy(base)
    r.pop(field, None)
    errs = gates.real_report_errors(r, ROOT)
    case(f'F8 缺 {field} 被拒', any(field in e for e in errs), errs)

# F7：不可签不得 approved
r = copy.deepcopy(base)
r['sign_scope'] = 'no_stop'
reasons = gates.check_finalize({'status': 'proposed', 'task_id': r.get('task_id')}, r, {'signer': 'kf41', 'report_sha256': 'x'})
case('F7 no_stop 拒 approved', any('不可签' in x or 'no_stop' in x for x in reasons), reasons)

# F12/F7：签字入口行为级（直接调用 post_sign 用的判定函数）
_nl = types.ModuleType('notify_lark')
for _n in ('notify_signed', 'notify', 'send', 'notify_gate'):
    setattr(_nl, _n, lambda *a, **k: None)
sys.modules.setdefault('notify_lark', _nl)
from server.app import sign_block_reason  # noqa: E402

_ns = {'sign_scope': 'no_stop', 'verdict_one_line': '不可签·待人工复核：x', 'is_demo': True}
case('F12 blocked 一律拒', sign_block_reason(_ns, {'state': 'blocked'}, {}) is not None, '')
case('F12 不可签无旗拒', sign_block_reason(_ns, {'state': 'ready'}, {}) is not None, '')
case('F12 非 demo 带旗仍拒',
     sign_block_reason({**_ns, 'is_demo': False}, {'state': 'ready'}, {'allow_unverified_demo': True}) is not None, '')
case('F12 demo 带旗允许练签',
     sign_block_reason(_ns, {'state': 'ready'}, {'allow_unverified_demo': True}) is None, '')

# F13：pack 为字符串时仍须校验 gate.action（异常不得被吞）
_r = copy.deepcopy(base)
_r["security_gate"] = {"status": "pending_user_confirmation", "action": "调整排班"}
_errs = gates.real_report_errors(_r, ROOT)
case('F13 pack=str 仍校验 gate.action',
     any('不是 pack 不可逆动作' in e for e in _errs), _errs)

# F13：validate CLI 严格三字段（不可 any() 跳过）
_legacy = copy.deepcopy(base)
for _f in ('verdict_one_line', 'sign_scope', 'evidence_tier', 'artifact_sha256', 'seal'):
    _legacy.pop(_f, None)
_lt = ROOT / 'runs' / '_f13_legacy_probe.json'
_lt.write_text(json.dumps(_legacy, ensure_ascii=False, indent=2), encoding='utf-8')
_p = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tests/validate_report.py'), str(_lt)], capture_output=True)
_o = (_p.stdout + _p.stderr).decode('utf-8', 'replace')
case('F13 三字段全缺即 FAIL', _p.returncode != 0 and 'FAIL' in _o, _o[-160:])
_p2 = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tests/validate_report.py'), str(_lt), '--allow-legacy'], capture_output=True)
_o2 = (_p2.stdout + _p2.stderr).decode('utf-8', 'replace')
case('F13 legacy 显式放行', _p2.returncode == 0 and 'WARN' in _o2, _o2[-160:])
_lt.unlink(missing_ok=True)

# F6：UI 渲染裁决字段
adapter = (ROOT / 'ui/api-adapter.js').read_text(encoding='utf-8')
case('F6 UI 渲染 verdict_one_line', 'verdict_one_line' in adapter and 'sign_scope' in adapter, '')

# F10：封存/篡改
r = copy.deepcopy(base)
art.seal(r, 'test', 'unit')
case('F10 封存后可验', art.verify(r)[0], art.verify(r)[1])
r['finding'][0]['confidence'] = 0.99
case('F10 篡改后失配', not art.verify(r)[0], art.verify(r)[1])

# F9：证据合同（inference_only 记 measured 被拒）
r = copy.deepcopy(base)
r['evidence_tier'] = 'inference_only'
r.setdefault('evidence', []).append({'id': 'E-X', 'tier': 'measured', 'url': 'not-a-url'})
tmp = ROOT / 'runs' / '_f9_probe.json'
tmp.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding='utf-8')
p = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tests/validate_report.py'), str(tmp)],
                   capture_output=True)
out = (p.stdout + p.stderr).decode('utf-8', 'replace')
case('F9 证据合同拦截', 'FAIL' in out and ('tier 与 evidence_tier 冲突' in out or 'url 不可解析' in out), out[-300:])
tmp.unlink(missing_ok=True)

# F11：回看队列剔除 demo、按 decision id 去重
p = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tools/review_due.py'), '--root', str(ROOT / 'runs'), '--today', '2026-11-04'],
                   capture_output=True)
out = (p.stdout + p.stderr).decode('utf-8', 'replace')
case('F11 demo 不进队列', 'T-20261028-054' not in out, out[:200])
case('F11 决策去重', out.count('D-20261020-001') <= 1, out.count('D-20261020-001'))

# F18：签字前复跑动作绑定——伪造 gate.action 不得放行
_r = copy.deepcopy(base)
_r["security_gate"] = {"status": "confirmed", "action": "调整排班", "confirmed_by": "kf41"}
_dec = {"status": "proposed", "task_id": _r.get("task_id"),
        "security_gate": {"status": "confirmed", "action": "调整排班", "confirmed_by": "kf41"}}
_reasons = gates.check_finalize(_dec, _r, {"signer": "kf41", "report_sha256": "x"},
                                allowlist=("kf41",), gate_confirmed=True)
case('F18 伪造 gate.action 签字被拒',
     any('不可逆' in x or 'pack' in x for x in _reasons), _reasons)

# F19：pack 四形状（缺失/None/list/空 pack_id）必须 fail-closed
for shape, label in ((None, '缺失'), ([], 'list'), ({"pack_id": ""}, '空 pack_id')):
    _r2 = copy.deepcopy(base)
    _r2["pack"] = shape
    _r2["security_gate"] = {"status": "pending_user_confirmation", "action": "调整排班"}
    _e2 = gates.real_report_errors(_r2, ROOT)
    case(f'F19 pack {label} 不静默跳过', any(('pack' in e or '不可逆' in e) for e in _e2), _e2)

# F22：缺契约字段的遗留/测试 run（含"可删"）不得进真实回看队列
import tempfile, shutil
_tmp = Path(tempfile.mkdtemp(prefix='rd_'))
(_tmp / 'x/loop').mkdir(parents=True)
(_tmp / 'x/report.json').write_text(json.dumps({"task_id": "T-LEGACY", "is_demo": None}), encoding='utf-8')
(_tmp / 'x/loop/decision.json').write_text(json.dumps({"id": "D-LEGACY", "question": "UI 端到端实跑（可删）"}), encoding='utf-8')
(_tmp / 'x/loop/closure.json').write_text(json.dumps({"verdict": "unverified", "due": "2026-11-02"}), encoding='utf-8')
_p2 = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tools/review_due.py'), '--root', str(_tmp), '--today', '2026-11-04'],
                     capture_output=True)
_out2 = (_p2.stdout + _p2.stderr).decode('utf-8', 'replace')
case('F22 legacy/可删 run 隔离', '| D-LEGACY |' not in _out2 and 'quarantined' in _out2, _out2[:200])
shutil.rmtree(_tmp, ignore_errors=True)

bad = [n for n, ok, _ in CASES if not ok]
print(f"test_f6_f11: {len(CASES) - len(bad)}/{len(CASES)} 通过" + ('' if not bad else ' | 失败: ' + '; '.join(bad)))
raise SystemExit(0 if not bad else 1)
