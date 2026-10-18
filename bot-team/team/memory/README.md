# 员工记忆（关系层）

> 目的：让员工在跨任务时"记得住"客户偏好、数据口径、上次结论；但**记忆 ≠ 事实**，每条记忆必须带来源与有效期。

## 记忆类型
| kind | 例子 | 默认有效期 |
|---|---|---|
| `profile` | 客户/工厂画像、产线结构 | 180 天 |
| `preference` | 老板要看一页纸、先结论后证据、只认 ROI | 180 天 |
| `data_caveat` | 补料率口径、B 级数据偏差<10%、信息员换人 | 90 天 |
| `decision` | 上次采纳/否决的建议与理由 | 365 天 |
| `outcome` | 上次实际结果（命中/未命中） | 365 天 |

## 检索规则（写进代码前先定死）
1. 按 `entity + employee_id` 取，最多 5 条；
2. 排序：`outcome/decision` 优先，其次 `data_caveat/preference`；
3. 隐私：`customer_sensitive` 只进私有环境，不进公开群/公开仓库；
4. 过期即不注入；每条注入必须带 `memory_id + source_ref`；
5. 记忆只作上下文，**不得覆盖证据**：与当前数据冲突时以数据为准并标记冲突。

## 文件
- `employee-memory.schema.yaml`：字段与枚举
- `example-memory.json`：3 条脱敏样例
