#!/usr/bin/env node
/**
 * DSH 会话事件 → 舞台 tape 适配器（B3 第一版）
 *
 * 输入：DSH 会话日志 `<session>.v4.jsonl.zstd`（多帧 zstd；每帧一段 JSONL）
 * 输出：{ file, meta, counts, beats, beatsTotal }
 *
 * 用法：
 *   node orchestration/dsh-session-tape.mjs <session.v4.jsonl.zstd> [out.json]
 *
 * 映射（第一版，只做可回放的最小集；不认识的事件只计数，不猜）：
 *   session            → meta（id/cwd/preset）
 *   user/message       → { kind:'user', text }
 *   assistant/message  → { kind:'speak', text }（只取 text；无 text 只计 think，不灌满舞台）
 *   tool/call          → { kind:'tool', tool, args }（358 条/会话量级 → 舞台"动作"）
 *   tool/result        → 计数（正文过大，不入 tape；可按需扩展）
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { zstdDecompressSync } from 'node:zlib';

const file = process.argv[2];
if (!file) {
  console.error('usage: node dsh-session-tape.mjs <session.v4.jsonl.zstd> [out.json]');
  process.exit(2);
}

const buf = readFileSync(file);
const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
const frames = [];
let pos = 0;
while (true) {
  const i = buf.indexOf(MAGIC, pos);
  if (i < 0) break;
  const j = buf.indexOf(MAGIC, i + 4);
  const end = j < 0 ? buf.length : j;
  try {
    frames.push(zstdDecompressSync(buf.subarray(i, end)).toString('utf8'));
  } catch (e) {
    console.error('frame decode failed:', e.message);
  }
  pos = end;
}

const lines = frames.join('').split('\n').filter(Boolean);
const counts = {};
const beats = [];
let meta = null;
let reasoningTurns = 0;

function clip(s, n) { return String(s ?? '').replace(/\s+/g, ' ').trim().slice(0, n); }
function textOf(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((c) => c && c.type === 'text' && c.text).map((c) => c.text).join('\n');
}
function reasoningOf(content) {
  if (!Array.isArray(content)) return '';
  return content.filter((c) => c && c.type === 'reasoning' && c.text).map((c) => c.text).join('\n');
}

for (const line of lines) {
  let o;
  try { o = JSON.parse(line); } catch { continue; }
  const t = o.type || o.kind || '?';
  counts[t] = (counts[t] || 0) + 1;
  const d = o.data || {};
  if (t === 'session') {
    meta = { id: o.id, cwd: o.cwd, agentPreset: o.agentPreset, createdAt: o.createdAt };
  } else if (t === 'user/message') {
    const txt = clip(textOf(d.message?.content) || d.text || '', 300);
    if (txt) beats.push({ kind: 'user', text: txt });
  } else if (t === 'assistant/message') {
    const content = d.message?.content;
    const txt = clip(textOf(content), 400);
    if (txt) beats.push({ kind: 'speak', text: txt });
    else {
      const r = reasoningOf(content);
      if (clip(r, 10)) reasoningTurns += 1;
    }
  } else if (t === 'tool/call' || t === 'tool-call') {
    beats.push({
      kind: 'tool',
      tool: d.name || o.name || '?',
      args: clip(d.arguments ?? o.arguments ?? '', 160),
    });
  }
}

const out = { file, meta, counts, reasoningTurns, beats: beats.slice(0, 240), beatsTotal: beats.length };
const json = JSON.stringify(out, null, 2);
if (process.argv[3]) {
  writeFileSync(process.argv[3], json + '\n', 'utf8');
  console.log(`wrote ${process.argv[3]} beats=${out.beatsTotal} tools=${out.beats.filter(b => b.kind === 'tool').length} speak=${out.beats.filter(b => b.kind === 'speak').length}`);
} else {
  console.log(json);
}
