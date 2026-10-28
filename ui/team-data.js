window.TEAM_DATA = {
  "generated_at": "2026-10-18",
  "source": "team/employees.yaml+crews.yaml+shelf.yaml",
  "employees": [
    {
      "id": "host",
      "name": "工作助理",
      "role": "L3 · 常驻职能",
      "layer": "L3",
      "cat": "resident",
      "state": "res",
      "stateText": "常驻",
      "init": "host",
      "tags": [
        "team-orchestration",
        "delivery-card"
      ],
      "sample": "交付卡 DL-20261018-001",
      "summary": "接单、出 DAG、预派发确认、收敛报告、写决策日志与交付卡",
      "hireable": false
    },
    {
      "id": "consulting-analyst",
      "name": "咨询分析师",
      "role": "L0 · 交付岗",
      "layer": "L0",
      "cat": "deliver",
      "state": "",
      "stateText": "在岗",
      "init": "consulting-analyst",
      "tags": [
        "data-snapshot",
        "field-contract-audit"
      ],
      "sample": "11 因素 × (T, T-30, Δ, 可得性) 对比表",
      "summary": "取数对账、T vs T-30 快照、11 因素扫描与空值率标注；只陈述事实",
      "hireable": true
    },
    {
      "id": "ai-attribution-expert",
      "name": "AI 归因专家",
      "role": "L1 · 交付岗",
      "layer": "L1",
      "cat": "deliver",
      "state": "",
      "stateText": "在岗",
      "init": "ai-attribution-expert",
      "tags": [
        "exclusion-six-steps",
        "crp-guard"
      ],
      "sample": "主因 M-03（0.63）＋残差 12 条",
      "summary": "封闭世界内排除法六步；finding≤2，cannot_attribute 强制非空",
      "hireable": true
    },
    {
      "id": "manufacturing-ops-expert",
      "name": "制造业业务专家",
      "role": "L2 · 交付岗",
      "layer": "L2",
      "cat": "deliver",
      "state": "",
      "stateText": "在岗",
      "init": "manufacturing-ops-expert",
      "tags": [
        "direction-check",
        "roi-anchors"
      ],
      "sample": "保养 ¥500 < 换模 ¥800 + 停机 ¥1200",
      "summary": "方向一致性校验、ROI 三锚点算账、不可逆动作标记",
      "hireable": true
    },
    {
      "id": "content-operator",
      "name": "内容操盘手",
      "role": "L1 · 交付岗",
      "layer": "L1",
      "cat": "deliver",
      "state": "",
      "stateText": "在岗",
      "init": "content-operator",
      "tags": [
        "content-plan",
        "title-variants"
      ],
      "sample": "换模 60 秒脚本（钩子/证据/判断/动作 + 5 个标题候选）",
      "summary": "把真实经营事实做成可分发内容：选题判断、四段结构、标题候选、48 小时复盘口径",
      "hireable": true
    },
    {
      "id": "compliance-reviewer",
      "name": "合规质检",
      "role": "cross · 质量岗",
      "layer": "cross",
      "cat": "quality",
      "state": "res",
      "stateText": "常驻",
      "init": "compliance-reviewer",
      "tags": [
        "compliance-check"
      ],
      "sample": "6+ 员工复用的合规底稿",
      "summary": "共享合规底稿（夸大词表/假紧迫/价钱口径/功效边界），班组交付前复核",
      "hireable": false
    },
    {
      "id": "data-fetcher",
      "name": "取数员",
      "role": "craft · 交付岗",
      "layer": "craft",
      "cat": "deliver",
      "state": "craft",
      "stateText": "工序",
      "init": "data-fetcher",
      "tags": [
        "read-only-fetch"
      ],
      "sample": "数据合同审计：字段覆盖率与缺失项",
      "summary": "只读拿真数据（不互动、不点赞、不评论），班组第一道工序",
      "hireable": true
    },
    {
      "id": "report-recorder",
      "name": "记录员",
      "role": "support · 质量岗",
      "layer": "support",
      "cat": "quality",
      "state": "res",
      "stateText": "常驻",
      "init": "report-recorder",
      "tags": [
        "decision-log",
        "followup"
      ],
      "sample": "DL 条目与回看清单",
      "summary": "决策日志、交付卡归档、T+7 回看催办",
      "hireable": false
    },
    {
      "id": "cost-steward",
      "name": "成本管家",
      "role": "coming · 待上架",
      "layer": "-",
      "cat": "coming",
      "state": "coming",
      "stateText": "待上架",
      "init": "cost-steward",
      "tags": [
        "能力包缺失"
      ],
      "sample": "能力包未写",
      "summary": "依赖 usage 数据源未接，不上架（死按钮比没按钮伤害大）",
      "hireable": false
    }
  ],
  "crews": [
    {
      "id": "c-refill-attribution",
      "name": "补料率归因",
      "purpose": "给小型制造业的补料率异常做根因归因与 ROI 算账",
      "steps": [
        {
          "agent": "consulting-analyst",
          "what": "读取 T/T-30，11 因素快照与空值率，输出 snapshot"
        },
        {
          "agent": "manufacturing-ops-expert",
          "what": "装载因素表，标注可得性与先验权重，输出 factor_table"
        },
        {
          "agent": "ai-attribution-expert",
          "what": "排除法六步，finding≤2，cannot_attribute 强制非空"
        },
        {
          "agent": "manufacturing-ops-expert",
          "what": "方向校验＋ROI 三锚点算账，输出 advice"
        },
        {
          "agent": "host",
          "what": "收敛报告、决策日志、交付卡"
        }
      ],
      "followup_days": 7
    },
    {
      "id": "c-downtime-attribution",
      "name": "异常停机归因",
      "purpose": "同底座换数据源，处理停机时长异常",
      "steps": [
        {
          "agent": "data-fetcher",
          "what": "只读取停机与保养数据，输出数据合同"
        },
        {
          "agent": "ai-attribution-expert",
          "what": "排除法归因，标注残差"
        },
        {
          "agent": "manufacturing-ops-expert",
          "what": "方向校验＋停机损失算账"
        },
        {
          "agent": "host",
          "what": "报告与交付卡"
        }
      ],
      "followup_days": 7
    },
    {
      "id": "c-weekly-ops",
      "name": "周报与任务管理",
      "purpose": "内部经营周报与任务收口",
      "steps": [
        {
          "agent": "data-fetcher",
          "what": "只读拉取任务与指标数据"
        },
        {
          "agent": "manufacturing-ops-expert",
          "what": "汇总经营动作与风险"
        },
        {
          "agent": "host",
          "what": "生成周报与下周动作"
        }
      ],
      "followup_days": 7
    },
    {
      "id": "c-compliance-review",
      "name": "合规审查",
      "purpose": "横切复核班组交付物（夸大/假紧迫/价钱口径/功效边界）",
      "steps": [
        {
          "agent": "compliance-reviewer",
          "what": "按合规底稿逐项检查，输出问题清单"
        },
        {
          "agent": "host",
          "what": "过/打回（打回必须改变交付物）"
        }
      ],
      "followup_days": 7
    },
    {
      "id": "c-content-publish",
      "name": "内容选题与产出",
      "purpose": "把一条真实经营事实做成可分发内容（视频号/公众号），产出可直接拍的方案",
      "steps": [
        {
          "agent": "consulting-analyst",
          "what": "取数对账，给出可引用的数字与时间窗，标出涉密项"
        },
        {
          "agent": "manufacturing-ops-expert",
          "what": "校验事实方向与算账口径，指出不能外发的商业信息"
        },
        {
          "agent": "content-operator",
          "what": "选题判断＋四段结构＋标题候选＋48h 复盘阈值"
        },
        {
          "agent": "compliance-reviewer",
          "what": "合规复核（夸大词/客户信息/功效边界）"
        },
        {
          "agent": "host",
          "what": "收敛成可拍内容方案与发布单"
        }
      ],
      "followup_days": 7
    }
  ],
  "context": {
    "goal": {
      "goal_id": "G-20261018-01",
      "entity": "塑胶厂-B线",
      "metric": "补料率",
      "baseline": 7.2,
      "target": 5.4,
      "deadline": "2026-11-17",
      "cadence": "weekly",
      "owner": "产线负责人",
      "status": "active",
      "linked_decisions": [
        "D-20261018-01"
      ],
      "progress": [
        {
          "date": "2026-10-18",
          "value": 7.2,
          "source": "T 窗口 7 日均值",
          "note": "起点"
        }
      ],
      "created_at": "2026-10-18",
      "review_due": "2026-10-25"
    },
    "memories": {
      "analyst": [
        "补料率口径=补料次数/总次数；B 级数据偏差<10%；信息员换人期间上报偏差可能 5%~10%"
      ],
      "biz": [
        "该厂优先动作顺序：先保养（¥500）后换模（¥800）；换模属不可逆动作，只呈请 securityGate"
      ],
      "host": [
        "交付偏好：一页纸、先结论后证据、必须写 ROI 与下一步；不接受模型术语"
      ]
    },
    "cost": {
      "cost_id": "C-20261018-01",
      "task_id": "T-20261019-B-refill-01",
      "date": "2026-10-18",
      "ai_cost_cny": 0.02,
      "ai_tokens": 79000,
      "human_hours_saved": 2.5,
      "human_hourly_cny": 50,
      "human_cost_saved_cny": 125,
      "action_cost_cny": 500,
      "avoided_loss_cny": 2000,
      "net_benefit_cny": 1500,
      "roi": 3.0,
      "source": "estimate",
      "verified": false,
      "notes": "示例值；真实任务请用 billing/实际工时替换，verified 由记录员确认"
    },
    "source": "examples"
  }
};
