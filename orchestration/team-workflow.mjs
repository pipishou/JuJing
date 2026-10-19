// DSH workflow · 制造业归因团队五步 DAG（s1‖s2 → s3 → s4；s5 由 host/Lead 收口）
// 用法：DSH 会话中调用 workflow 工具，脚本内容＝本文件；args = { "base": "<bot-team-dsh 绝对路径>" }
// 约束：workflow 脚本无文件系统/网络；读文件由各 agent 自己完成。

const base = args.base;
if (!base) throw new Error('args.base 必填：bot-team-dsh 包根绝对路径');
const demo = `${base}/demo`;
const dataDir = args.data_dir || demo; // 真实任务：把 T/T30 JSON 放到 data_dir，传 args.data_dir 即可
const assets = `${base}/assets`;
const roles = `${base}/team/roles`;
// 行业包（§2.4）：ROI 锚点/阈值/不可逆动作的唯一来源；默认制造业包，可传 args.pack_path 覆盖
const packPath = args.pack_path || `${base}/packs/manufacturing/pack.json`;

const str = { type: 'string' };
const numOrStr = { oneOf: [{ type: 'string' }, { type: 'number' }] };

const snapshotSchema = {
  type: 'object',
  properties: {
    snapshot: {
      type: 'array',
      items: {
        type: 'object',
        properties: { factor: str, t_value: str, t30_value: str, delta: str, availability: str, note: str },
        required: ['factor', 'availability'],
      },
    },
    null_rate: str,
    data_ok: { type: 'boolean' },
  },
  required: ['snapshot', 'null_rate'],
};

const factorTableSchema = {
  type: 'object',
  properties: {
    factor_table: {
      type: 'array',
      items: {
        type: 'object',
        properties: { factor: str, availability: str, prior_weight: str, reason: str },
        required: ['factor', 'availability'],
      },
    },
  },
  required: ['factor_table'],
};

const attributionSchema = {
  type: 'object',
  properties: {
    finding: {
      type: 'array',
      items: {
        type: 'object',
        properties: { factor: str, confidence: numOrStr, evidence: { type: 'array', items: str } },
        required: ['factor', 'confidence', 'evidence'],
      },
    },
    cannot_attribute: { type: 'array', items: str },
    excluded: { type: 'array', items: str },
    scorecard: {
      type: 'object',
      properties: {
        data_completeness: numOrStr,
        timeliness: numOrStr,
        significance: numOrStr,
        exclusivity: numOrStr,
        history: numOrStr,
        human_ai_consistency: numOrStr,
      },
    },
  },
  required: ['finding', 'cannot_attribute'],
};

const adviceSchema = {
  type: 'object',
  properties: {
    direction_check: str,
    recommendation: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          action: str,
          roi: { type: 'object', properties: { cost: str, avoided: str, anchor: str }, required: ['cost', 'avoided', 'anchor'] },
          irreversibility: { type: 'string', enum: ['reversible', 'irreversible'] },
        },
        required: ['action', 'roi', 'irreversibility'],
      },
    },
    security_gate_requested: { type: 'boolean' },
  },
  required: ['recommendation'],
};

phase('S1‖S2 感知与准备');
log('s1 咨询分析师：T vs T-30 快照；s2 业务专家：因素表与先验权重（并行）');

const [snapshot, factorTable] = await parallel([
  () => agent(
    `你是「咨询分析师」（L0 感知）。先读角色章程 ${roles}/consulting-analyst.md，再读 ${assets}/factors_table.yaml，` +
    `然后读取 ${dataDir}/material_refill_T.json 与 ${dataDir}/material_refill_T30.json。` +
    `对 11 类因素做 T 与 T-30 的快照对比，标注每因素可得性(available/partial/missing)与整体空值率；` +
    `只陈述数据事实，不下归因结论、不补数。最后只输出符合 schema 的 JSON：{snapshot:[{factor,t_value,t30_value,delta,availability,note}], null_rate, data_ok}。`,
    { schema: snapshotSchema, label: 's1-consulting-analyst', phase: 'S1' },
  ),
  () => agent(
    `你是「制造业业务专家」（L2 侧准备，s2）。先读角色章程 ${roles}/manufacturing-ops-expert.md，再读 ${assets}/factors_table.yaml 与 ${assets}/scorecard_six_dim.yaml。` +
    `为每个因素标注数据可得性(available/partial/missing)与先验权重（必须写现场理由），输出 factor_table。` +
    `最后只输出符合 schema 的 JSON：{factor_table:[{factor,availability,prior_weight,reason}]}。`,
    { schema: factorTableSchema, label: 's2-factor-table', phase: 'S1' },
  ),
]);

phase('S3 归因（排除法六步）');
const attribution = await agent(
  `你是「AI 归因专家」（L1）。先读角色章程 ${roles}/ai-attribution-expert.md，再读 ${assets}/exclusion_six_steps.yaml、${assets}/prompt_crp.yaml、${assets}/factors_table.yaml、${assets}/scorecard_six_dim.yaml。` +
  `执行排除法六步（快照→变化→排除→方向→反事实→残差）。输入如下：\n` +
  `snapshot=${JSON.stringify(snapshot)}\nfactor_table=${JSON.stringify(factorTable)}\n` +
  `只在因素枚举表封闭世界内归因；finding≤2 且每条带 confidence 与 evidence；cannot_attribute 强制非空。` +
  `最后只输出符合 schema 的 JSON：{finding:[{factor,confidence,evidence:[...]}], cannot_attribute:[...], excluded:[...], scorecard:{六维}}。`,
  { schema: attributionSchema, label: 's3-attribution', phase: 'S3' },
);

phase('S4 方向校验与 ROI 建议');
const advice = await agent(
  `你是「制造业业务专家」（L2 建议，s4）。先读角色章程 ${roles}/manufacturing-ops-expert.md、行业包 ${packPath}（ROI 锚点 roi_anchors、不可逆动作 irreversible_actions、阈值 thresholds 均以它为准）、${assets}/irreversible_actions.yaml、${assets}/decision_log.schema.yaml。` +
  `对下列归因做方向一致性校验，并用行业包 ${packPath} 里的 roi_anchors 逐条算账（金额只能取自该文件的锚点，不得硬编码任何金额）：\n` +
  `attribution=${JSON.stringify(attribution)}\n` +
  `建议必须可执行（谁/哪台设备/何时/花多少/省多少）；若建议含不可逆动作，标记 irreversibility=irreversible 并置 security_gate_requested=true（只呈请、不执行）。` +
  `最后只输出符合 schema 的 JSON：{direction_check, recommendation:[{action,roi:{cost,avoided,anchor},irreversibility}], security_gate_requested}。`,
  { schema: adviceSchema, label: 's4-recommendation', phase: 'S4' },
);

log('s1–s4 完成；s5 由 host/Lead 收口成报告并写决策日志');
return { snapshot, factorTable, attribution, advice };
