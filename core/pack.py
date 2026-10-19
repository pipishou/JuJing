# -*- coding: utf-8 -*-
"""行业包加载（§2.4）：pack.json 是阈值 / ROI 锚点 / 不可逆动作的唯一事实源。

规则：
  - load(pack_id="manufacturing", root=None) -> dict
  - 环境变量 BOT_PACK 可指向任意 pack.json（A5 用它验证「改包→报告随之变化」）
  - root 缺省为包根（core/ 的上一级目录）
  - 只用 stdlib（json），不依赖 PyYAML
"""
import json
import os
import sys
from pathlib import Path

DEFAULT_PACK_ID = "manufacturing"
_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_KEYS = ("pack_id", "version", "industry", "thresholds", "roi_anchors",
                 "irreversible_actions", "six_dims", "crew_id")
REQUIRED_THRESHOLDS = ("refill_baseline_pct", "refill_target_pct",
                       "cell_null_rate_manual", "t30_window_days")


class PackError(RuntimeError):
    """行业包缺失/损坏。"""


def pack_path(pack_id=DEFAULT_PACK_ID, root=None):
    """返回 pack.json 的绝对路径（BOT_PACK 优先）。"""
    env = os.environ.get("BOT_PACK", "").strip()
    if env:
        return Path(env)
    base = Path(root) if root else _ROOT
    return base / "packs" / str(pack_id) / "pack.json"


def validate(doc, path=""):
    """校验行业包字段完整性；返回原 doc（就地补 _pack_path）。"""
    if not isinstance(doc, dict):
        raise PackError(f"行业包顶层必须是对象: {path}")
    missing = [k for k in REQUIRED_KEYS if k not in doc]
    if missing:
        raise PackError(f"行业包缺字段 {missing}: {path}")
    missing_t = [k for k in REQUIRED_THRESHOLDS if k not in (doc.get("thresholds") or {})]
    if missing_t:
        raise PackError(f"行业包 thresholds 缺 {missing_t}: {path}")
    if not isinstance(doc.get("roi_anchors"), dict) or not doc["roi_anchors"]:
        raise PackError(f"行业包 roi_anchors 必须是非空对象: {path}")
    doc["_pack_path"] = str(path)
    return doc


def load_file(path):
    """按显式路径加载行业包（assemble --pack-path / 测试用）。"""
    p = Path(path)
    if not p.is_file():
        raise PackError(f"行业包不存在: {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise PackError(f"行业包 JSON 解析失败: {p}: {exc}")
    return validate(doc, p)


def load(pack_id=DEFAULT_PACK_ID, root=None):
    """加载并校验行业包；BOT_PACK 环境变量优先；任何缺失/字段不全 → PackError。"""
    return load_file(pack_path(pack_id, root))


# ---- ROI 锚点消费（制造业动作 → pack 锚点键）----
ANCHOR_KEYWORDS = (
    ("mold_change", ("换模", "模具更换", "mold_change", "mold change", "换模具")),
    ("maintenance", ("保养", "maintenance", "点检", "维护")),
    ("overtime_clean", ("停机", "停线", "加班", "overtime", "downtime", "line_stop")),
)


def anchor_key_for(action_text):
    """按动作中文文本匹配 pack 锚点键；匹配不到返回 None。"""
    text = str(action_text or "")
    for key, words in ANCHOR_KEYWORDS:
        if any(w in text for w in words):
            return key
    return None


def roi_for(pack, action_text):
    """用 pack 的 roi_anchors 为动作生成 deterministic ROI 文本（供缺 ROI 时兜底）。"""
    key = anchor_key_for(action_text)
    anchors = pack.get("roi_anchors") or {}
    if key is None:
        key = next(iter(anchors))
    value = anchors.get(key)
    return {
        "cost": f"¥{value}",
        "avoided": f"¥{value}",
        "anchor": f"{key}（pack:{pack.get('pack_id')}）",
        "roi_source": f"pack:{pack.get('pack_id')}",
    }


def is_irreversible(pack, action_text):
    """动作是否属于行业包声明的不可逆动作。"""
    text = str(action_text or "")
    for name in pack.get("irreversible_actions") or []:
        label = str(name)
        if label and (label in text or label.replace("_", "") in text.replace("_", "")):
            return True
    # 中文动作关键词兜底（停线/换模不可逆按包内声明）
    for word in ("停线", "line_stop", "换模", "conditional_mold_change"):
        if word in text:
            return True
    return False


def threshold(pack, key):
    """读一个阈值（float）。缺失时抛 PackError。"""
    try:
        return float((pack.get("thresholds") or {})[key])
    except Exception as exc:  # noqa: BLE001
        raise PackError(f"行业包阈值 {key} 缺失/非数值: {pack.get('_pack_path')}") from exc


if __name__ == "__main__":
    pack = load()
    print(json.dumps(pack, ensure_ascii=False, indent=2))
    sys.exit(0)
