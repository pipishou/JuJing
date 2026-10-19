# -*- coding: utf-8 -*-
"""bot-team 后端核心层（零第三方依赖，仅 stdlib）。

模块：
  ids.py             —— 同日唯一编号（T-/DL-/D-/A-/E-/C-），文件锁 + 原子写
  provenance.py      —— data_refs 的 sha256/size 溯源与校验
  gates.py           —— 真实报告门 / 签字收口门（A2/A3 的机器判据）
  pack.py            —— 行业包加载（阈值、ROI 锚点、不可逆动作）
  report_contract.py —— 六维评分卡 100 分制与报告契约（A4 的机器判据）

注意：core 不得 import yaml / pandas / 任何第三方包；需要 YAML 的工具自己读。
"""

__all__ = ["ids", "provenance", "gates", "pack", "report_contract"]
