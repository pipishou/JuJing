# 专家包（员工资产源）

这里是**员工人格与职责的源**，可提交、可迁移——换电脑或换仓库不会丢。运行时 Octop 平台按约定路径加载 `.octop/expert_market/<pack>/`，那是**副本**。

```
packs/expert_market/
├── manufacturing-advisory-board/      五人士参谋团（卡帕西 / 乔布斯 / 内容操盘手 / 业务专家 + 工作助理）
│   ├── SOUL.md                        总则、角色表、合法 subagent_type、条件派发、验收表
│   ├── manifest.json                  包标识与 quick_prompt
│   ├── agents/*.md                    职责层（含 <!-- PERSONA:START/END --> 人格注入块）
│   └── persona/<slug>/
│       ├── distill/SKILL.md           女娲格式重蒸稿（跟包走，不被女娲更新覆盖）
│       ├── VOICE.md                   表达纪律
│       ├── SOURCES.md                 来源与可采信度、重蒸记录
│       └── inbox/ ingested/           新素材投递与已消化区
└── manufacturing-business-team/       早期业务班组包（判据资产 + 三角色）
```

## 两条命令

```bash
python tools/rebuild_persona.py --check     # 人格块与源是否一致（漂移=rc1）
python tools/rebuild_persona.py --apply     # 从 persona/*/distill 重生成 agents/*.md 人格块

python tools/sync_expert_pack.py                 # 仓库 ⇄ 运行副本 差异（dry-run）
python tools/sync_expert_pack.py --to-runtime    # 部署：仓库 → .octop
python tools/sync_expert_pack.py --from-runtime  # 捞回：.octop → 仓库
python tools/sync_expert_pack.py --check         # 进机检：有差异 rc=1
```

## 纪律

- **仓库是源**：改人格只改仓库，然后 `--to-runtime` 部署，不要直接改 `.octop` 里的副本。
- 女娲（`~/.workbuddy/skills/huashu-nuwa`）只负责造人；蒸馏产物一旦落进 `persona/<slug>/distill/`，就**跟包走**，不再受女娲更新影响。
- 素材先进 `inbox/`，消化后进 `ingested/`，结论写进 `SOURCES.md` 并标可采信度（高/中/低）。
- 不挂真人名的角色（如内容操盘手）必须写明「主题蒸馏」与参考底子，禁止伪造专家身份。
