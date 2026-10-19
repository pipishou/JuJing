# UI 概念原型 · AI 员工事务所（v0.2）

单文件、离线可开、无外部依赖：`index.html`。**假数据**，用于 G0「第一眼」评审与视觉方向定稿。

## 打开方式

```powershell
start D:\项目\bot\bot-team-dsh\ui\index.html
# 可选：?beat=3 直接停在第四拍（截图/评审用）
start "D:\项目\bot\bot-team-dsh\ui\index.html?beat=3"
```

## v0.7.1 签字内容哈希（2026-10-18）

- 「签字确认」不再只是改 DOM：浏览器用 `crypto.subtle` 对交付卡内容做 SHA-256，显示前 16 位；与 `tools/sign_delivery.py` 的报告 SHA256 对齐（UI 是示例内容哈希，正式签字以工具输出 signature.json 为准）。

## v0.6 关系/目标/成本/企微（2026-10-18）

- 班组卡加**目标进度条**（7.2%→5.4%，T+7 复查）；员工详情加**跨任务记忆** chips；交付卡加「企微推送」按钮（原型，实际工具 `tools/wecom_notify.py`）。
- 记忆/目标/成本/企微的 schema 与规则见 `team/memory`、`team/goals`、`team/cost`、`team/integrations`。

## v0.3.1 接单向导 + 成本行（2026-10-18）

- 顶栏「新建任务」：工厂/产线/指标/目标/T 窗口/基线/数据目录/负责人 → 生成 `task-*.json`（真实任务入口）。
- 交付卡新增成本 chips：`AI 成本 ¥0.02`、`人工节省 2.5h`（示例值）；舞台「本场数据」也写成本行。
- 真实任务步骤见 `docs/REAL_TASK_PLAYBOOK_2026-10-19_zh.md`；产品基准见 `docs/PRODUCT_BENCHMARK_2026-10-18_zh.md`。

## v0.3 动画 + 真实 tape 回放（按用户反馈：A/B 都要做）

- **A 角色帧**：`sprites.js` 支持 idle / walk0 / walk1 / blink / talk / happy / think / surprised；舞台每拍切换时上一角色走两步，当前角色说话口型开合（260ms），每 3.6s 随机眨眼，按幕给「思考/开心/说话/惊讶」表情。
- **B 真实 tape**：`tools/gen_tape_ui.py` 把 `runs/dsh-tape-sample.json`（DSH 真实会话，379 beats）转成 `ui/tape-data.js`（120 beats，工具按语义映射到角色）；舞台右上按钮切「真实 tape」或 `?mode=tape&t=N` 定格；底部时间码显示 `TAPE:xx`。

## v0.2.1 角色升级（按用户反馈：参考开罗系列像素角色设计）

- 角色全部改为**原创像素小人**（`sprites.js`）：20×26 像素网格、大头小身、1px 深色描边、亮配色、职业道具（耳机/平板/放大镜/安全帽/合规夹/数据箱）；仅参考开罗风格语言，**不提取、不复用任何开罗素材**。
- 员工/班组数据来自 `team/*.yaml` → `tools/gen_ui_data.py` → `team-data.js`（单一事实源，KPI 动态）。
- 舞台数据源：`orchestration/dsh-session-tape.mjs` 把 DSH 会话日志（JSONL.zstd）转成 tape beat（样例 `runs/dsh-tape-sample.json`，379 beats/229 tools）。

## v0.2 三个升级方向（按用户 2026-10-18 反馈）

| 方向 | 做了什么 |
|---|---|
| **舞台更电影感/更等距** | 加深色剧场内胆＋上下电影黑边（ACT III / REC / timecode）；等距办公室（地板网格/地毯/三工位/椅子/侧柜/绿植/顶灯）；聚光灯跟随、角色待机浮动、交接虚线光束、换幕遮罩；右上「当前台词」卡＋底部字幕条＋五拍时间轴 |
| **员工货架更像招聘商店** | 「招聘台」横幅＋分类 Tab（全部/在岗/常驻/工序/待上架）＋搜索；卡片含头像、层级、**成果样例条**、状态徽章、技能标签；coming 卡片显式写「为什么不能招」（能力包缺失/依赖未就绪） |
| **交付卡更像老板能签字的成果** | 信纸版式（报告编号/日期/产线/置信）＋主因条＋不确定性 chips＋ROI 账＋下一步；**签字栏**：报告人 / 复核人 / 老板签字虚线；「签字确认 / 打回修改 / 导出」按钮与「待签字」橡皮章；点击签字盖章变「已签字」 |

## 仍不做（原型边界）

- 无真实 agent 接入；播放为 5 拍预设动画（`?beat=` 可定格）；
- 员工/班组/交付为演示数据；签字/导出为假动作；
- 视觉资产全内联（离线可用）；<1100px 有降级布局。

## 数据与下一步（B2）

- 员工/班组数据：`team/employees.yaml`、`team/crews.yaml`、`team/shelf.yaml`、`team/delivery-card.schema.yaml`（v0.2 已落）；
- 舞台数据：DSH 会话日志（JSONL.zstd）→ 事件适配器 → tape（替代旧 stage-viewer 的 Octop tape）；
- 交付卡：`runs/demo-report.json` 同构 + 签字状态机。

## 真数据模式（B2 · 2026-10-19）

`index.html` 末尾新增 `api-adapter.js`（`<script src="api-adapter.js">`）。打开页面时先探测同源 `/api/health`：

- **成功** → 真数据模式：从 `/api/tasks` 拉任务与 KPI，从 `/api/tasks/<id>` 渲染交付卡/本场数据，
  从 `/api/tasks/<id>/log` 的 phase 事件生成舞台五拍台词；顶栏出现「▶ 运行本任务」，
  签字按钮改为 `POST /api/tasks/<id>/sign`，签字栏显示工具返回的报告 SHA256 前 16 位。
- **失败**（`file://` 打开或本地服务未启动）→ 自动保持原有静态演示，动画照常、控制台无未捕获异常。

启动本地服务（见 `server/README.md`）：

```powershell
& "D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe" -B server/app.py
# 浏览器打开 http://127.0.0.1:8787/
```

`?task=<task_id>` 可指定要渲染的 `runs/<task_id>/`；不指定时优先选已有报告的任务。
接线集中在 `ui/api-adapter.js` 的 `bindRealData` 钩子（`onMode/onTasks/onTask/onBeats/onSign/onError`），
原有像素/动画/演示数据（`team-data.js`、`tape-data.js`、`sprites.js`）与 `tests/validate_structure.py`
依赖的字面量（`PixelSprites`、`beats`、`openIntake`、`generateTask`、`TEAM_DATA` 等）均保持不变。
