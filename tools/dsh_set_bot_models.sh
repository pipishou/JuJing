#!/usr/bin/env bash
# 给五个 Bot 统一授权模型：opencode-go / deepseek-v4.1-flash / reasoning high
# 前置：完全退出 DSH（Tobs CLI 是单写者，DSH 运行时会返回 lease-unavailable）
# 用法：bash tools/dsh_set_bot_models.sh            # 建预设 + 应用到五个 Bot
#       PRESET_NAME=... PROVIDER=... MODEL=... EFFORT=high bash tools/dsh_set_bot_models.sh
set -uo pipefail

NODE_BIN="${NODE_BIN:-D:/tools/node/node.exe}"
if [ ! -x "$NODE_BIN" ]; then
  NODE_BIN="$(command -v node || true)"
fi
if [ -z "${NODE_BIN:-}" ]; then
  echo "[FATAL] 找不到 node 可执行文件（设 NODE_BIN=...）" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEEPSEEKBOT="${DEEPSEEKBOT:-$HOME/.dsh/profiles/desktop/node_modules/deepseekbot/dist/deepseekbot.mjs}"
if [ ! -f "$DEEPSEEKBOT" ]; then
  echo "[FATAL] 找不到 deepseekbot CLI：$DEEPSEEKBOT" >&2
  exit 2
fi

PRESET_NAME="${PRESET_NAME:-opencode-flash-high}"
PROVIDER="${PROVIDER:-dsh-opencode-go}"
MODEL="${MODEL:-deepseek-v4.1-flash}"
EFFORT="${EFFORT:-high}"

mkdir -p "$ROOT/runtime/logs"
LOG="$ROOT/runtime/logs/set-bot-models-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

tobs() { "$NODE_BIN" "$DEEPSEEKBOT" "$@"; }

echo "== 探活（lease 检查）"
if ! tobs list >/dev/null 2>&1; then
  echo "[FATAL] CLI 不可写：请先完全退出 DSH（任务管理器确认无 DeepSeek Harness 进程）再跑本脚本"
  echo "[LOG] $LOG"
  exit 2
fi

echo "== 现有预设"
tobs model-presets || true

echo "== 建预设 ${PRESET_NAME}（${PROVIDER} / ${MODEL} / effort=${EFFORT}）"
tobs model-preset-create \
  --name "$PRESET_NAME" \
  --orchestrator-provider "$PROVIDER" --orchestrator-model "$MODEL" --orchestrator-effort "$EFFORT" \
  --assignment-provider "$PROVIDER" --assignment-model "$MODEL" --assignment-effort "$EFFORT"

echo "== 重新读取预设"
PRESETS_JSON="$(tobs model-presets || echo '{}')"
echo "$PRESETS_JSON"
PRESET_ID="$("$NODE_BIN" -e '
try {
  const j = JSON.parse(require("fs").readFileSync(0, "utf8"));
  const list = j.presets || (j.data && j.data.presets) || [];
  const hit = list.find(p => (p.name || "") === process.env.PRESET_NAME) || list[0];
  process.stdout.write(hit && (hit.id || hit.presetId) ? (hit.id || hit.presetId) : "");
} catch (e) { process.stdout.write(""); }
' 2>/dev/null <<<"$PRESETS_JSON")"

if [ -z "$PRESET_ID" ]; then
  echo "[FATAL] 没能从 model-presets 里取到预设 id，请打开 Bot 模式设置手工选择模型，或把上面的 JSON 发我"
  echo "[LOG] $LOG"
  exit 3
fi
echo "[OK] 预设 id = $PRESET_ID"

BOTS=(
  "bot-5f46e53ba61b4c58966d3528bd47653c|工作助理"
  "bot-0d8da16ee82042be85ccee7225008b53|AI 专家"
  "bot-f9e7ff3cf9294b85801a43b98361a3c9|产品专家"
  "bot-b28c350128d24abab3f492fe20c63b53|业务专家"
  "bot-0c08af3a9864458885d1c072b767bfca|内容操盘手"
)

fail=0
for entry in "${BOTS[@]}"; do
  id="${entry%%|*}"; name="${entry##*|}"
  echo "== 应用预设 → $name（$id）"
  if tobs model-preset-apply "$id" --preset "$PRESET_ID"; then
    echo "[OK] $name"
  else
    echo "[FAIL] $name"; fail=$((fail+1))
  fi
done

echo "== 复核"
tobs list || true

if [ "$fail" -ne 0 ]; then
  echo "[WARN] $fail 个 Bot 应用预设失败，见 $LOG"
  exit 1
fi
echo "[DONE] 五个 Bot 已统一到 ${PRESET_NAME}（${PROVIDER}/${MODEL}/${EFFORT}）"
echo "[LOG] $LOG"
