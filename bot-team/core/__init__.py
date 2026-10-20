# -*- coding: utf-8 -*-
"""bot-team 后端核心层。

模块：
  ids.py             —— 同日唯一编号（T-/DL-/D-/A-/E-/C-），文件锁 + 原子写
  provenance.py      —— data_refs 的 sha256/size 溯源与校验
  gates.py           —— 真实报告门 / 签字收口门（A2/A3 的机器判据）
  pack.py            —— 行业包加载（阈值、ROI 锚点、不可逆动作/词表）
  report_contract.py —— 六维评分卡 100 分制与报告契约（A4 的机器判据）
  team.py            —— team/*.yaml 聚合为 /api/team 数据（M1.5 §2.1）
  events.py          —— run.log 事件追加/读取（M1.5 §2.2）
  gate.py            —— securityGate 人工确认 gate.json（M1.5 §2.3）
  paths.py           —— 上传文件名 / 任务 id 白名单（M1.5 §2.4）
  scene.py           —— 场景卡 SceneSpec v1：种子加载/六类校验/降级/发现（B18/B19）
  decision_card.py   —— P2 决策卡 DEC：report 的呈现层视图（§3.3，不写回 report.json）

依赖纪律：除 team.py 读 team/*.yaml 时按需导入 PyYAML（venv 内置、仅在 load_team 调用时
导入）外，core 只用 stdlib；不得引入 pandas 或其它第三方包。
"""

__all__ = [
    "ids",
    "provenance",
    "gates",
    "pack",
    "report_contract",
    "team",
    "events",
    "gate",
    "paths",
    "scene",
    "decision_card",
]
