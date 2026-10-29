# -*- coding: utf-8 -*-
"""F6–F11 负向/正向断言（不依赖 UI 运行）。"""
import copy
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

# F7：签字入口显式拒收
app = (ROOT / 'server/app.py').read_text(encoding='utf-8')
case('F7 post_sign 有拦截', 'allow_unverified_demo' in app and '不得签字' in app, '')

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

bad = [n for n, ok, _ in CASES if not ok]
print(f"test_f6_f11: {len(CASES) - len(bad)}/{len(CASES)} 通过" + ('' if not bad else ' | 失败: ' + '; '.join(bad)))
raise SystemExit(0 if not bad else 1)
