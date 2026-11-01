# ADR-010 Router–Worker 分离

- 状态：建议（待用户确认）
- 日期：2026-11-01
- 关联：`docs/tech/ATTENTION_ARCHITECTURE_2026-11-01.md`

## 背景

现在五个席位在一个会话里轮流说话。任务一多，主上下文被 4–5 个任务的全文撑爆，团长开始忘事；一个任务出错会污染整场对话。superagent 的 Router/Worker 分离提供了参考：主会话只路由，每个 Ticket 独立 Worker。

## 决策

1. **Router（团长）**：只接来源、判断 ignore/observe/park/route/ticket、维护 WIP/预算、收口六行 handoff；不做具体技术判断，单次路由输出 ≤30 字。
2. **Worker**：每个 Work Item 一个独立会话/目录；自己读证据、跑工具、写产物；只回传六行。
3. **上下文单向**：Worker → Router 只传 handoff；Router 不回灌全量日志。
4. **用户只有一个前台**：飞书只和 Router 对话；5 席位是被调用的能力，不是 5 个聊天窗口。
5. **失败隔离**：worker 失败只影响该 Work Item，可重派；Router 不崩。

## 备选与不选理由

- 单会话五角色：演示简单，但上下文/污染问题会随任务数放大；不选为长期架构。
- 每个 agent 一个飞书 bot：用户要管 5 个窗口，违背注意力目标；不选。
- 直接接 superagent 运行时：单 GitHub 仓库、DSH 版本门、danger-full-access；不选。

## 后果

- 正面：Router 上下文稳定；任务可并行；失败隔离；用户只面对一个前台。
- 负面：需要 worker 调度、会话生命周期、六行协议；短期实现成本高。
- 失效边界：需要跨机 worker/多用户时，再评估队列与服务拆分。

## 验收

- 4 个并行任务时，Router 上下文 < 单任务全文的 1/3（token 计量）；
- 每个 worker 只回六行，无全文日志；
- 一个 worker 失败不影响其他任务与 Router；
- 用户只收到 Router 的票/日报，不收到 worker 直接消息。
