#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业微信群机器人推送（出站，P1）。

用法：
  python tools/wecom_notify.py --kind delivery --title "补料率归因" \
      --finding "模具磨损 M-03（0.63）" --cannot "信息员换人 5%~10%" \
      --roi "净省 ¥1,500" --cost "AI ¥0.02 / 人工节省 2.5h" \
      --next "10-25 复查 ≤5.40%"
  python tools/wecom_notify.py --kind review --title "T+7 回看" --content "目标 5.40%，实际待填"
  python tools/wecom_notify.py --kind cost --title "成本月报" --content "AI ¥0.12 / 人工 12h"
  # 测试：--dry-run 只打印 payload，不发请求

安全：
- webhook 从环境变量 WECOM_WEBHOOK 读取（或 --webhook 临时传）；不写入日志、不入库。
- 企业微信群机器人限制约 20 条/分钟；超频请拆分 webhook 或排队。
- 只发内部群；客户敏感数据不要发群。
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import date

ENDPOINT = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def mask(webhook: str) -> str:
    if not webhook:
        return "<unset>"
    return webhook[:24] + "..." + webhook[-4:] if len(webhook) > 32 else "<set>"


def build(kind: str, args) -> str:
    if kind == "delivery":
        return (
            f"## 交付卡 · {args.title}\n"
            f"> 主因：{args.finding or '待填'}\n"
            f"- 不确定性：{args.cannot or '待填'}\n"
            f"- ROI：{args.roi or '待填'}\n"
            f"- 成本：{args.cost or '待填'}\n"
            f"- 下一步：{args.next or '待填'}\n"
            f"- 日期：{date.today().isoformat()}"
        )
    if kind == "review":
        return f"## 回看提醒 · {args.title}\n> {args.content or '请回填实际结果与 verdict'}\n- 日期：{date.today().isoformat()}"
    return f"## 成本与价值 · {args.title}\n> {args.content or '待填'}\n- 日期：{date.today().isoformat()}"


def send(webhook: str, content: str, dry_run: bool) -> int:
    payload = {"msgtype": "markdown", "markdown": {"content": content[:4000]}}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if dry_run:
        print("[dry-run]", json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    req = urllib.request.Request(ENDPOINT + "?key=" + webhook, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 发送失败（webhook {mask(webhook)}）：{exc}", file=sys.stderr)
        return 1
    if data.get("errcode") == 0:
        print(f"[OK] 已发送（webhook {mask(webhook)}）")
        return 0
    print(f"[ERROR] errcode={data.get('errcode')} errmsg={data.get('errmsg')}", file=sys.stderr)
    return 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="企业微信群机器人推送（出站）")
    p.add_argument("--kind", choices=["delivery", "review", "cost"], default="delivery")
    p.add_argument("--title", default="经营归因")
    p.add_argument("--content", default="")
    p.add_argument("--finding", default="")
    p.add_argument("--cannot", default="")
    p.add_argument("--roi", default="")
    p.add_argument("--cost", default="")
    p.add_argument("--next", default="")
    p.add_argument("--webhook", default="")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    webhook = args.webhook or os.environ.get("WECOM_WEBHOOK", "")
    content = build(args.kind, args)
    if not webhook:
        print("[INFO] 未配置 WECOM_WEBHOOK，自动 dry-run", file=sys.stderr)
        return send("", content, True)
    return send(webhook, content, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

