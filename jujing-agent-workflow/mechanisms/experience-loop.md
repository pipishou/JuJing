# 双循环与双回流（P4，v2.0）

> **机制权威件**：外环（项目执行环）与内环（skill 演化环）的**咬合协议**。内环四齿的命令与每轮协议见 `self-optimization.md`（本件不重复）；本件只管「一次事件如何同时喂两个环」。
> 判据键＝`assets/params.json#loop`（本件路径／出口判据挂载清单／档案模板路径）；机检＝`probe_usage.py kb-lint` ⑨。

## 双循环

```
外环＝项目执行环（作用于产品）
  需求 → 开发 → 测试 → 部署 → 反馈 ─┐
   ▲                               │
   └── 修正卡（用户可见效果＋可机判定言）←┘

内环＝skill 演化环（作用于 skill 自身）
  WAL 信号 → 积累(≥ params.json#wal.promote_min) → 棘轮(实测提升) → 控制(人类确认)
        ▲                                                    │
        └──── 消费探针回流（痕迹/缺席告警）＋热度回流（heat 正向加分·观测非门）────┘
```

## 咬合点＝双回流：一次事件，两件产物

| 侧 | 产物 | 落点 | 判定 |
|---|---|---|---|
| 产品 | **修正卡**：用户可见效果＋可机判定言 | 项目侧 INBOX／决策台账 | 可验收 |
| 经验 | **WAL 行**：`wal.py add --clause <锚点> --actor <留痕人>`（M2 准入） | `data/wal.jsonl` | 可探针 |

只做经验侧＝教训只被记录、没变成要修的卡；只做产品侧＝修了产品、skill 不长记性。**两件缺一＝双回流断裂**（④/⑧ 出口判据红，kb-lint ⑨ 挂载机检）。

## 出口判据挂载（`params.json#loop.backflow_hooks`）

- **④ 测试出口**：三绿＋机检全过＋用户视角断言过＋二次核查结论＋接线验证绿＋**双回流两件在位**；
- **⑧ 运营出口**：北极星在动＋成本在台账＋无死功能＋告警有人接＋机制三问有答案＋**双回流两件在位**。

## 全循环留痕链（写入→消费→评分→晋升/退役；kb-lint ⑨ 重放）

1. **写入**：`wal.py add`（信号）→ `data/wal.jsonl`；
2. **消费**：`probe_usage.py sample → record --actor` → `data/probe.jsonl`；**热度回流**：`probe_usage.py heat` 按条款聚合（正向＝独立源命中加分／负向＝空转扣分，权重见 `params.json#governance`）——**观测非门**，只作晋升/退役评审的参考输入，不进任何退出码；
3. **评分**：`rubric_score.py score`（须引用真实批次）→ `data/scores.jsonl`；
4. **晋升/退役**：`promote-check → mark-promoted`（**ids 必须真实存在，悬空＝留痕复算断裂**）→ 条目/条款修订；`retire-check → cutoff` 退役。

**复算**＝`kb-lint` ⑨ 从上述数据重放四段计数＋promoted ids 完整性——「跑通了双循环」必须可复算，不可复算的自称＝公理 5 假绿。

## 与姊妹 skill 的接口边界（能力归属公理延伸）

- 聚鲸内环只演化**自身**（判据／条款／机检项／路由；append-only＋棘轮＋人类确认）。
- **从零造新 skill** ＝ 走 `skill-creator`；**优化通用 skill 的技法** ＝ 走 `skill-improver`——两者是具体能力工具，聚鲸不重复实现（已否：路由脚本、向量检索同理），只出「何时该用它们」的路由：新增件前过 `evolution.md` 扩展四问，若属造新 skill／通用优化即出聚鲸边界，借对应工具。
- 边界判定：改的是**聚鲸自身的判据/条款**＝内环；改的是**别的 skill**＝出边界走 `skill-improver`；**从零造新 skill**＝走 `skill-creator`。

## 退役条件（扩展四问④）

双回流连续 `params.json#probe.self_stall_warn` 轮无人执行（每轮协议第 1 步空转证据）→ 本件退役候选，走 `retire-check`。
