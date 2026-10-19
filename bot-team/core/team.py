# -*- coding: utf-8 -*-
"""团队数据聚合（M1.5 §2.1）：team/*.yaml + packs/*/pack.json → /api/team 的单一事实源。

  load_team(root=None) -> {"team_id", "employees":[...], "crews":[...], "shelf":{...},
                           "roster":{...}, "pack":{...}, "warnings":[...], "invalid":[...]}

规则：
  - 员工：team/employees.yaml 的 employees，补 sprite_key（长 id → 像素角色短 key）
    与 persona（team/roles/*.md 的 YAML frontmatter；无 frontmatter 则 {}）。
  - 班组：team/crews.yaml 的 crews，followup 兼容 end.followup（yaml 里挂在 end 下）。
  - 校验不抛：员工 id 重复 / crews.steps.agent 不在员工册 / pack.crew_id 不在 crews
    → 记 warning + invalid 条目，仍然返回已能解析的部分（server 按 invalid 决定 500）。
  - 只依赖 stdlib（YAML 用 venv 内置 PyYAML；缺 PyYAML 时给明确报错）。
"""
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

# 像素角色短 key（与 ui/sprites.js 的 PAL 调色板一一对应）
SPRITE_KEYS = ("host", "analyst", "attr", "biz", "qa", "data", "content", "cost")

# 长员工 id → 短精灵 key（修复 M1「长 slug 直接当 sprite key → 头像全灰」的错位）
SPRITE_BY_ID = {
    "host": "host",
    "consulting-analyst": "analyst",
    "ai-attribution-expert": "attr",
    "manufacturing-ops-expert": "biz",
    "compliance-reviewer": "qa",
    "data-fetcher": "data",
    "report-recorder": "content",
    # 未上架员工（employees.yaml 的 coming 段）：先占位，上架即用
    "content-operator": "content",
    "cost-steward": "cost",
}

EMPLOYEE_FIELDS = ("id", "name", "layer", "category", "state", "summary", "role_file", "skills", "sample")
YAML_FILES = ("team/employees.yaml", "team/crews.yaml", "team/shelf.yaml", "team/roster.yaml")


class TeamError(RuntimeError):
    """团队数据不可读（文件缺失/依赖缺失）。load_team 内部尽量降级为 warnings。"""


def _yaml_module():
    try:
        import yaml  # noqa: PLC0415  venv 内置 PyYAML；只读 YAML，不写
    except Exception as exc:  # noqa: BLE001
        raise TeamError("core.team 需要 PyYAML 读取 team/*.yaml（venv 已内置，请用 venv python 运行）") from exc
    return yaml


def _read_yaml(root, rel, warnings, invalid):
    """读一个 yaml 文件；失败只记 warning/invalid，返回 {}（不抛）。"""
    path = Path(root) / rel
    if not path.is_file():
        warnings.append(f"缺少团队数据文件: {rel}")
        invalid.append({"kind": "missing_file", "path": rel})
        return {}
    try:
        doc = _yaml_module().safe_load(path.read_text(encoding="utf-8-sig"))
    except TeamError:
        raise
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"团队数据解析失败: {rel}: {exc}")
        invalid.append({"kind": "bad_yaml", "path": rel, "error": str(exc)})
        return {}
    if not isinstance(doc, dict):
        warnings.append(f"团队数据顶层不是对象: {rel}")
        invalid.append({"kind": "bad_yaml", "path": rel})
        return {}
    return doc


def parse_frontmatter(text):
    """解析 markdown 开头的 YAML frontmatter；没有/解析失败 → {}。"""
    if not isinstance(text, str) or not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            block = "\n".join(lines[1:idx]).strip()
            if not block:
                return {}
            try:
                doc = _yaml_module().safe_load(block)
            except Exception:  # noqa: BLE001 frontmatter 坏不该拖垮 /api/team
                return {}
            return doc if isinstance(doc, dict) else {}
    return {}


def _role_file_path(root, role_file):
    """把 employees.yaml 里的 role_file 解析为 root 内路径；越界/缺失 → None。"""
    text = str(role_file or "").strip()
    if not text:
        return None
    rel = text.replace("\\", "/").lstrip("/")
    path = (Path(root) / rel).resolve()
    try:
        path.relative_to(Path(root).resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _persona_for(root, role_file, warnings, emp_id):
    path = _role_file_path(root, role_file)
    if path is None:
        if role_file:
            warnings.append(f"员工 {emp_id} 的 role_file 不存在或越界: {role_file}")
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        warnings.append(f"员工 {emp_id} 的 role_file 读取失败: {role_file}: {exc}")
        return {}
    return parse_frontmatter(text)


def _norm_list(value):
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, (tuple, set)):
        return [str(x) for x in value]
    if value in (None, ""):
        return []
    return [str(value)]


def _employees(root, raw, warnings, invalid):
    out = []
    seen = set()
    for i, item in enumerate(raw.get("employees") or []):
        if not isinstance(item, dict):
            warnings.append(f"employees[{i}] 不是对象，已跳过")
            invalid.append({"kind": "employee_not_object", "index": i})
            continue
        emp_id = str(item.get("id") or "").strip()
        if not emp_id:
            warnings.append(f"employees[{i}] 缺 id，已跳过")
            invalid.append({"kind": "employee_missing_id", "index": i})
            continue
        if emp_id in seen:
            warnings.append(f"员工 id 重复: {emp_id}")
            invalid.append({"kind": "duplicate_employee_id", "id": emp_id})
            continue
        seen.add(emp_id)
        emp = {key: item.get(key) for key in EMPLOYEE_FIELDS if key in item}
        emp["id"] = emp_id
        emp["name"] = str(item.get("name") or emp_id)
        emp["layer"] = str(item.get("layer") or "")
        emp["category"] = str(item.get("category") or "")
        emp["state"] = str(item.get("state") or "")
        emp["summary"] = str(item.get("summary") or "")
        emp["role_file"] = str(item.get("role_file") or "")
        emp["skills"] = _norm_list(item.get("skills"))
        sample = item.get("sample")
        emp["sample"] = sample if isinstance(sample, (dict, list)) else str(sample or "")
        sprite = SPRITE_BY_ID.get(emp_id)
        if not sprite:
            warnings.append(f"员工 {emp_id} 未登记精灵 key，回退 host")
            sprite = "host"
            invalid.append({"kind": "sprite_key", "id": emp_id})
        elif sprite not in SPRITE_KEYS:
            warnings.append(f"员工 {emp_id} 精灵 key={sprite!r} 不在像素角色库，回退 host")
            sprite = "host"
            invalid.append({"kind": "sprite_key", "id": emp_id, "sprite_key": sprite})
        emp["sprite_key"] = sprite
        emp["persona"] = _persona_for(root, emp["role_file"], warnings, emp_id)
        out.append(emp)
    return out


def _crews(raw, emp_ids, warnings, invalid):
    out = []
    for i, item in enumerate(raw.get("crews") or []):
        if not isinstance(item, dict) or not item.get("id"):
            warnings.append(f"crews[{i}] 缺 id，已跳过")
            invalid.append({"kind": "crew_missing_id", "index": i})
            continue
        crew_id = str(item.get("id"))
        steps = []
        for j, step in enumerate(item.get("steps") or []):
            if not isinstance(step, dict):
                warnings.append(f"班组 {crew_id} steps[{j}] 不是对象，已跳过")
                invalid.append({"kind": "crew_step_not_object", "crew": crew_id, "step": j})
                continue
            agent = str(step.get("agent") or "")
            if agent not in emp_ids:
                warnings.append(f"班组 {crew_id} steps[{j}].agent={agent!r} 不在员工册")
                invalid.append({"kind": "crew_step_agent", "crew": crew_id, "step": j, "agent": agent})
            steps.append({"agent": agent, "what": str(step.get("what") or "")})
        end = item.get("end") if isinstance(item.get("end"), dict) else {}
        followup = end.get("followup") if isinstance(end.get("followup"), dict) else item.get("followup")
        out.append({
            "id": crew_id,
            "name": str(item.get("name") or crew_id),
            "purpose": str(item.get("purpose") or ""),
            "want": str(item.get("want") or ""),
            "needs": str(item.get("needs") or ""),
            "steps": steps,
            "end": end,
            "followup": followup if isinstance(followup, dict) else {},
        })
    return out


def _shelf(raw):
    doc = {"states": {}, "badges": {}, "categories": []}
    states = raw.get("states")
    if isinstance(states, dict):
        doc["states"] = states
    badges = raw.get("badges")
    if isinstance(badges, dict):
        doc["badges"] = badges
    categories = raw.get("categories")
    if isinstance(categories, list):
        doc["categories"] = categories
    rules = raw.get("shelf_rules")
    if rules is not None:
        doc["rules"] = rules
    return doc


def _load_pack(root, warnings, invalid):
    try:
        from . import pack as pack_mod  # 延迟导入，避免 core 包初启时循环

        return pack_mod.load(root=str(Path(root)))
    except Exception as exc:  # noqa: BLE001 包缺失不拖垮 /api/team，但要留痕
        warnings.append(f"行业包不可读: {exc}")
        invalid.append({"kind": "pack", "error": str(exc)})
        return {}


def load_team(root=None):
    """聚合团队数据；顶层校验失败只进 warnings/invalid，绝不抛（文件缺失除外）。"""
    root = Path(root) if root else DEFAULT_ROOT
    warnings = []
    invalid = []
    employees_raw = _read_yaml(root, YAML_FILES[0], warnings, invalid)
    crews_raw = _read_yaml(root, YAML_FILES[1], warnings, invalid)
    shelf_raw = _read_yaml(root, YAML_FILES[2], warnings, invalid)
    roster_raw = _read_yaml(root, YAML_FILES[3], warnings, invalid)

    employees = _employees(root, employees_raw, warnings, invalid)
    emp_ids = {emp["id"] for emp in employees}
    crews = _crews(crews_raw, emp_ids, warnings, invalid)
    crew_ids = {crew["id"] for crew in crews}
    pack = _load_pack(root, warnings, invalid)

    crew_id = str((pack or {}).get("crew_id") or "")
    if crew_id and crew_id not in crew_ids:
        warnings.append(f"pack.crew_id={crew_id!r} 不在 crews {sorted(crew_ids)}")
        invalid.append({"kind": "pack_crew_id", "crew_id": crew_id, "crews": sorted(crew_ids)})

    team_id = str(employees_raw.get("team_id") or roster_raw.get("team_id") or "manufacturing-ops-team-dsh")
    return {
        "team_id": team_id,
        "employees": employees,
        "crews": crews,
        "shelf": _shelf(shelf_raw),
        "roster": roster_raw,
        "pack": pack,
        "warnings": warnings,
        "invalid": invalid,
    }


if __name__ == "__main__":
    doc = load_team(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    sys.exit(0)
