#!/usr/bin/env bash
# 一次性创建五个 Bot（工作助理 + 四专家）
# 前置：必须先关闭 DSH（GUI 持有 profile writer lease，CLI 无法写入）
# 用法：bash tools/dsh_create_bots.sh
set -uo pipefail

NODE_BIN="${NODE_BIN:-D:/tools/node/node.exe}"
[ -x "$NODE_BIN" ] || NODE_BIN="$(command -v node || echo node)"
DEEPSEEKBOT="${DEEPSEEKBOT:-$HOME/.dsh/profiles/desktop/node_modules/deepseekbot/dist/deepseekbot.mjs}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$ROOT/packs/expert_market/manufacturing-advisory-board/agents"
MEM="${BOT_MEMORY_DIR:-D:/work/产品类/个人产品/bot-2/bot-memory}"

if [ ! -f "$DEEPSEEKBOT" ]; then echo "[FATAL] 找不到 CLI：$DEEPSEEKBOT"; exit 1; fi
if [ ! -d "$MEM" ]; then echo "[FATAL] 找不到记忆仓：$MEM"; exit 1; fi

tobs() { "$NODE_BIN" "$DEEPSEEKBOT" "$@"; }

LOG_DIR="$ROOT/runtime/logs"; mkdir -p "$LOG_DIR"; LOG="$LOG_DIR/create-bots-$(date +%Y%m%d-%H%M%S).log"; exec > >(tee -a "$LOG") 2>&1; echo "[log] $LOG"
echo "== 0. 检查 lease 与预设"
if ! out=$(tobs list 2>&1); then echo "$out"; echo "[FATAL] CLI 不可写：请先完全退出 DSH 再跑本脚本"; exit 2; fi
echo "$out" | head -5
echo "--- 可用模型预设"
tobs model-presets 2>&1 | head -20
PRESET="${BOT_PRESET:-}"
if [ -z "$PRESET" ]; then
  PRESET=$(tobs model-presets 2>/dev/null | grep -oE '"(id|preset)":"[^"]+"' | head -1 | cut -d'"' -f4)
fi
echo "--- 使用预设：${PRESET:-<默认>}"

make_bot() {
  local name="$1" persona_file="$2" memory_dir="$3" extra="$4"
  local args=(create --name "$name" --memory-location "$MEM/$memory_dir")
  [ -n "$PRESET" ] && args+=(--preset "$PRESET")
  if [ -n "$extra" ]; then
    { printf '%s\n\n' "$extra"; cat "$persona_file"; } | tobs "${args[@]}" --persona-stdin --core-memory "$MEM/$memory_dir/MEMORY.md"
  else
    cat "$persona_file" | tobs "${args[@]}" --persona-stdin --core-memory "$MEM/$memory_dir/MEMORY.md"
  fi
  echo "[rc=$?] $name"
}

ASSISTANT_HEAD="你是工作助理，也是这个团队的团长。你不带任何名人腔调，只做四件事：开局、判级、派活、收敛。判级 L0 自己答 ≤10s／L1 派 1 个 ≤40s／L2 派 2–3 个 ≤90s／L3 全员 ≤150s（只有涉及签字、对外、多部门才用 L3）。收敛时必须写清「未采纳谁、为什么」。"

echo "== 1/5 工作助理"
make_bot "工作助理" "$AGENTS/jobs.md" "assistant" "$ASSISTANT_HEAD"
echo "== 2/5 AI 专家"
make_bot "AI 专家" "$AGENTS/karpathy.md" "ai-expert" ""
echo "== 3/5 产品专家"
make_bot "产品专家" "$AGENTS/jobs.md" "product-expert" ""
echo "== 4/5 业务专家"
make_bot "业务专家" "$AGENTS/kazuo-inamori.md" "business-expert" ""
echo "== 5/5 内容操盘手"
make_bot "内容操盘手" "$AGENTS/content-operator.md" "content-operator" ""

echo "== 结果"
tobs list 2>&1 | head -40
