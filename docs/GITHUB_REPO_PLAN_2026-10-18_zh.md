# JuJing GitHub 仓库接入方案（2026-10-18）

> 背景：用户已建仓库 `https://github.com/pipishou/JuJing`，要求**先不打包迁移**，改用仓库承接。
> 已确认：该仓库是**公开仓库**（GitHub 页面可访问；MIT；当前含 `jujing-agent-workflow`、`jujing-junshi` 两个 skill 目录 + `.github`）。
> 纪律：**未获用户明确下令，不执行任何 git 命令**（add/commit/push/branch 一律等指令）。

## 1. 公开仓库的第一原则：三类东西不能进

| 类别 | 例子 | 原因 |
|---|---|---|
| 密钥/凭据 | `.credentials.yaml`、`.env*`、`*.key`、token | 公开即泄露 |
| 个人/客户数据 | `.junshi/ledger.md`、`runs/task-*/` 真实工厂数据、会话日志/tape 原始 zstd | 隐私与商业机密 |
| 受限第三方资产 | REA 逆向件、Octop 源树、开罗素材、商业字体/图标 | 版权/许可 |

**可进**：代码、schema、测试、CI、脱敏 demo、公开文档、原创像素素材（本项目为代码原生绘制）。
> 注意：`skills/*/data/` 是军师的个人记忆（WAL/sessions），**不要**随 skill 一起公开；如要公开 skill，先清空/脱敏 `data/`。

## 2. 建议目标结构

```text
JuJing/
├─ jujing-agent-workflow/          # 现有，保持路径不变（README 安装说明依赖）
├─ jujing-junshi/                  # 现有，保持路径不变
├─ bot-team/                       # 新增：产品包（bot-team-dsh 去掉 dist/runs/task/.junshi）
│  ├─ SKILL.md / README.md / MIGRATION.md
│  ├─ team/ assets/ demo/ orchestration/ tests/ ui/ templates/ docs/
│  └─ .gitignore
├─ docs/
│  ├─ PRODUCT_BENCHMARK_2026-10-18_zh.md
│  ├─ FOUR_LOOP_UPGRADE_2026-10-18_zh.md
│  ├─ REAL_TASK_PLAYBOOK_2026-10-19_zh.md
│  └─ GITHUB_REPO_PLAN_2026-10-18_zh.md
├─ .github/workflows/validate.yml  # 结构校验 + 密钥扫描
├─ .gitignore
└─ README.md                       # 产品一句话 + 安装/验收
```

- 不移动现有两个 skill 目录（移动会破坏 README 的 clone/cp 指引）；
- `bot-team/` 作为第二产品目录并列，README 增加"产品包"段；
- 真实任务数据放本机 `D:\项目\bot\runs\task-*`，永不入库。

## 3. `.gitignore`（bot-team 与仓库根各一份）

```gitignore
# secrets
.env
.env.*
.credentials*
*.key
*.pem
# personal / project memory
.junshi/
skills/*/data/
# runtime / real data
runs/task-*/
*.zst
*.jsonl.zstd
dist/
# caches
__pycache__/
*.pyc
node_modules/
.venv/
# OS
Thumbs.db
.DS_Store
```

> demo 数据保留：`demo/*.json`、`runs/demo-report.json`、`runs/TEST-RECEIPT-*.md`、`runs/dsh-tape-sample.json` 可进；真实任务与原始会话不可进。

## 4. CI（GitHub Actions）最小版

`validate.yml`：

```yaml
name: validate
on: [push, pull_request]
jobs:
  structure:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install pyyaml
      - run: python bot-team/tests/validate_structure.py
      - run: python bot-team/tests/validate_report.py bot-team/runs/demo-report.json
```

- 不加任何密钥；不在 CI 跑真实数据；
- 可选：`gitleaks` 扫描（公开库建议开 GitHub Secret Scanning + Push Protection）；
- 若把 skill 放进仓库，再加 `python jujing-agent-workflow/scripts/probe_usage.py selfcheck`（先确认无个人 data 依赖）。

## 5. 分支/PR/发版纪律

- `main` 保护：禁止直接 push；PR 至少 1 次自检（结构校验绿 + 无密钥 + 无真实数据）。
- 分支：`feat/<topic>`、`fix/<topic>`；一个 PR 一件事。
- 提交信息：`feat|fix|docs|chore(scope): ...`；不写客户名/工厂名。
- 发版：tag `v0.x.y`；release notes 写"行为变化 + 未验证项"。
- **git 操作必须用户明确下令**：本方案只是布局，不代表已提交。

## 6. 迁移步骤（等用户下令后）

1. 在本机 `D:\项目\bot` 建 `bot-team/` 镜像（排除 `dist/`、`runs/task-*`、`.junshi/`、`__pycache__`）；
2. 复制 `00_docs` 三份到 `docs/`；
3. 加 `.gitignore` 与 `.github/workflows/validate.yml`；
4. 本地跑 `validate_structure.py` 与 `validate_report.py`（不联网）；
5. 用户确认后：`git checkout -b feat/bot-team` → `git add` 指定路径 → `git commit` → `git push` → PR；
6. PR 里贴：结构校验输出、文件清单、无密钥/无真实数据声明。

## 7. 拍板与执行（2026-10-18）

1. **bot-team 进公开仓库：已拍板并执行**——commit `ef609e8` 已 push 到 `main`；只放公开安全子集。
2. **skill 的 `data/` 保持本机**（推荐）；公开库不携带个人记忆。
3. **CI 已配置** `.github/workflows/validate.yml`；本地同命令 PASS。

> 公开边界：方法论原文、真实工厂数据、个人账本、内部策略文档不入库。
