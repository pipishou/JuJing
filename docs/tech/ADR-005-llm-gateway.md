# ADR-005 LLM 网关

- 状态：建议（待用户确认）
- 日期：2026-10-30

## 背景

当前 `core/llm.py` 及多处调用直接拼 provider/模型；换模型、算成本、加重试、做降级都要改业务代码。5 角色会诊会放大这个问题。

## 决策

建一个薄网关，业务只依赖接口：

```python
class LLMGateway:
    def complete(self, *, purpose, messages, schema=None,
                 model_policy="default", timeout_s=60,
                 budget_cny=None) -> LLMResult: ...
```

职责：

1. **provider 适配**：DeepSeek / OpenAI / 本地兼容端点；一个 provider adapter 一层；
2. **model policy**：`default` / `fast` / `deep` / `cheap`，映射到具体模型与参数；
3. **超时/重试/降级**：超时重试一次，仍失败返回 `blocked`，不编造；
4. **成本/延迟**：记录 token、费用、耗时、模型、prompt 版本；
5. **prompt 版本**：`prompt_ref + version`，变更留痕；
6. **输出契约**：需要 JSON 时用 schema 校验；失败重试一次，仍失败标 `invalid_output`；
7. **测试**：提供 fake provider，单测不联网。

## 备选与不选理由

- 每个 agent 自己调：短期快，长期无法算账/换模型；不选。
- LiteLLM/OpenRouter 等网关库：会增加依赖与黑箱；当前 provider 少，先自建薄层；需要多 provider 时再评估。
- 框架内置 router：绑定框架；不选。

## 后果

- 正面：成本可见、模型可换、失败可降级、prompt 可追溯。
- 负面：多一层抽象；需要维护 model policy 表。
- 失效边界：需要流式/多模态/工具调用编排时，评估专门网关；仍不引入 agent 框架。

## 验收

- 业务代码不再直接 import provider SDK；
- 每次调用有 `purpose/model/tokens/cost/latency/prompt_version`；
- fake provider 下单测覆盖成功/超时/非法 JSON/预算超限；
- 换模型只改 policy，不改业务。
