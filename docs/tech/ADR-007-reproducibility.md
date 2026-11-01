# ADR-007 可复现与测试门

- 状态：建议（待用户确认）
- 日期：2026-10-30

## 背景

仓库没有 `requirements.txt` / `pyproject.toml`；当前依赖 Python 3.12.13 + PyYAML 6.0.3 只存在于本机 venv。换工作电脑/迁移时必须可复现。

## 决策

1. 增加 `pyproject.toml`：
   - `requires-python = ">=3.12,<3.13"`；
   - 运行依赖：`PyYAML==6.0.3`（如无其他）；
   - 可选开发依赖：无强制测试框架，保留现有脚本；
2. 增加 `requirements.lock`（或 `uv.lock`）固定解析结果；
3. 所有测试用统一入口：
   ```bash
   python -m tests.test_core_m15
   python -m tests.test_m15_acceptance
   python -m tests.test_mvp_gate --selfcheck
   ```
4. 增加 `scripts/dev.ps1`：创建/复用 `.venv`、安装依赖、跑基线；
5. 增加 `docs/tech/MIGRATION.md`：换机步骤、数据目录、端口、回滚；
6. CI 可选：先本地门，不引入 GitHub Actions 直到仓库公开边界明确。

## 备选与不选理由

- 不做清单，靠 README：换机必踩坑；不选。
- 引入 pytest/tox：现有 29 个脚本测试可跑，先不增加框架；后续需要再评估。
- 容器化：单用户本地，Docker 增加复杂度；T1 再评估。

## 后果

- 正面：换机可复现；依赖升级有锁；测试入口统一。
- 负面：需要维护 lock 文件；Python 小版本固定。
- 失效边界：需要多平台/多 Python 版本时，评估 uv/tox；仍不引入运行时框架。

## 验收

- 干净机器按 `MIGRATION.md` 可在 15 分钟内跑通基线；
- `pyproject.toml` 的 Python/依赖与 venv 一致；
- 三条测试命令全绿；
- 迁移后 `runs/`、`.octop_home`、数据目录不丢。
