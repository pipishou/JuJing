# 企微接入（P1：群机器人出站）

> 目标：先让"交付卡 / T+7 回看 / 成本月报"能推到企微内部群；**不做接收**（接收要自建应用回调+公网服务，P2）。
> 结论：**企业微信群机器人 webhook 是最快路径**——5 分钟配置、无需公网服务器、无加解密。

## 1. 两种企微接入对比

| 方式 | 能力 | 代价 | 结论 |
|---|---|---|---|
| **群机器人 webhook** | 单向推送 markdown/text/图片/文件 | 无需服务器；每机器人约 20 条/分钟 | **P1 选它** |
| 自建应用（corpid/secret/agentid） | 收消息、通讯录、审批、主动发消息 | 需公网回调 URL、加解密、IP 白名单、应用审核 | P2 再做 |

依据：[企业微信官方文档-群机器人](https://developer.work.weixin.qq.com/document/path/91770)（配置与 markdown 语法）；频率限制 20 条/分钟见 [腾讯云文档](https://cloud.tencent.com.cn/document/product/248/50413) 与 [UnionPay 文档](https://doc.yun.unionpay.com/tcloud/omtool/BARAD/203393/638721/65575/weekhook)；社区组件参考 [wecom-notifier](https://pypi.org/project/wecom-notifier/0.1.5/)（本地 20/分钟预防 + 重试；我们暂不引第三方依赖，用标准库实现）。

## 2. 5 分钟配置

1. 企微群 → 群设置 → 群机器人 → 添加机器人 → 复制 webhook URL；
2. 本机设置环境变量（不要写进仓库）：
   ```powershell
   setx WECOM_WEBHOOK "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=***"
   ```
3. 测试（dry-run 不发送）：
   ```powershell
   python tools/wecom_notify.py --kind delivery --title "补料率归因" --finding "模具 M-03（0.63）" --cannot "信息员换人" --roi "净省 ¥1,500" --cost "AI ¥0.02 / 人工 2.5h" --next "10-25 复查 ≤5.40%" --dry-run
   ```
4. 去掉 `--dry-run` 即真发；日志只显示 key 后 4 位。

## 3. 消息模板（已写进 `team/integrations/wecom.yaml`）

- **交付卡**：主因 / 不确定性 / ROI / 成本 / 下一步 / 链接（一条消息，不附原始数据）；
- **回看提醒**：目标 / 实际 / 请回填 verdict；由 GitHub Actions 或本机计划任务触发；
- **成本月报**：AI 成本 / 人工节省 / 净收益。

## 4. 安全与边界

- webhook 是密钥：只放环境变量/本机，**不入库、不写日志、不进公开群**；
- 客户敏感数据不发群；只发内部群；消息最小化；
- 群机器人**不能接收任务**；"在企微里接单"要 P2 自建应用；
- 频率 20 条/分钟；批量提醒需排队/多机器人。

## 5. P2（等 P1 跑通再评估）

1. 自建应用：回调 URL + Token/AESKey + 公网服务；支持"群里 @机器人 接单/回填"；
2. 通讯录/审批：老板在企微审批交付卡；
3. 多群/部门路由：不同客户群不同机器人（webhook 池）。

## 6. 与四段闭环的关系

- 交付卡推送 = 决策记录"已交付"的对外动作；
- 回看提醒 = 闭环卡 G4 的触发器；
- 成本月报 = 成本透明的对外证据。
