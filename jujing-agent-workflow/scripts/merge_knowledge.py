#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聚鲸工作流 · 记忆合并协议（M2 多人共享·无 git 版）

两份 skill 拷贝（你／合伙人）各自生长后的合并。判据与协议：
  assets/params.json#memory（actors 准入登记表；endorsement 佐证口径）
  knowledge/README.md §三（轻量版边界：合并协议落点）
  references/routing.md §七（三步接入）

用法:
  python scripts/merge_knowledge.py --theirs <对方skill根>            # dry-run（默认，只报告不写）
  python scripts/merge_knowledge.py --theirs <对方skill根> --apply    # 先自动 zip 备份，再写合并结果

范围与规则:
  1. data/{probe,effect,scores,wal}.jsonl —— 行级合并（append-only 精神）：
     解析后逐字段全等的行只保留一条（双方各自追加的行互不冲突），其余全保留。
     零丢行断言：合并后 本方行集 ⊆ 结果集 且 对方行集 ⊆ 结果集（写后自验，违反即报错）。
  2. wal.jsonl 另做 ID 冲突检测：同 id 不同内容 ＝ 冲突（绝不自动改写——改 id ＝ 改写历史，
     与 append-only 精神冲突）。有冲突时该文件不写，退出码 2，人工裁决后重跑。
  3. knowledge/entries/*.md —— 同名同内容跳过；同名不同内容 ＝ 冲突（人工裁决，
     裁决依据见下）；仅一方独有 ＝ 复制并入。
  4. knowledge/INDEX.md 与 params.json 不自动合并：两个权威索引/配置，脚本只报告差异，
     人工并表后必须跑 `probe_usage.py kb-lint` 全绿（② 游离件机检兜底漏注册）。
  5. actors 差异报告：对方留痕出现的 actor 若未在我方 params#memory.actors 登记，
     提示准入登记（不阻塞 apply——数据不丢；但 kb-lint ⑤ 会报红，直到人工登记或删行）。

冲突裁决协议（人工决策，四类决策永远留人——knowledge/README.md §二）:
  - 依据：data/wal.jsonl＋data/effect.jsonl 消费留痕多者可信度高；同证据量取更新时间新者；
  - 动作：人工编辑文件（改内容或删行），裁决理由一行记入 references/evolution.md；
  - wal promoted 行引用旧 id 的，裁决改 id 时人工同步 ids 列表；
  - 裁决后重跑本脚本 --apply 直至零冲突，再跑 kb-lint 全绿收口。

安全网: --apply 写入前自动把本方 data/＋knowledge/ 打包 zip 至 --backup-dir
  （默认 <skill>/../jujing-backups/<时间戳>.zip）；无 git ＝ 用快照兜底（README §三）。
"""
import argparse, datetime, json, pathlib, sys, zipfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_JSONLS = ("probe.jsonl", "effect.jsonl", "scores.jsonl", "wal.jsonl")
_PARAMS = _ROOT / "assets" / "params.json"


def _read_jsonl(p):
    if not p.exists():
        return [], []
    texts, rows = [], []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        texts.append(line)
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append(None)   # 损坏行按文本原样保留（不丢行），不参与全等比较
    return texts, rows


def _backup(mine, backup_dir):
    """--apply 写入前的 zip 快照（无 git 的安全网，判据 knowledge/README.md §三）。"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = pathlib.Path(backup_dir) if backup_dir else mine.parent / "jujing-backups"
    bdir.mkdir(parents=True, exist_ok=True)
    zp = bdir / f"jujing-skill-{ts}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for sub in ("data", "knowledge", "assets"):
            for f in sorted((mine / sub).rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(mine).as_posix())
    print(f"[merge] 备份完成: {zp}")
    return zp


def _merge_one_jsonl(base, other):
    base_texts, base_rows = _read_jsonl(base)
    other_texts, other_rows = _read_jsonl(other)
    seen, out = [], []
    for t, r in list(zip(base_texts, base_rows)) + list(zip(other_texts, other_rows)):
        key = json.dumps(r, ensure_ascii=False, sort_keys=True) if r is not None else f"RAW::{t}"
        if key not in seen:
            seen.append(key)
            out.append(t)
    return out, base_rows, other_rows


def _id_conflicts(base_rows, other_rows):
    """同 id 不同内容 ＝ 冲突（信号行与 promoted 行均检测）。返回冲突对列表。"""
    by_id = {}
    for r in base_rows:
        if r is not None and r.get("id") is not None:
            by_id[r["id"]] = r
    conflicts = []
    for r in other_rows:
        if r is None or r.get("id") is None:
            continue
        if r["id"] in by_id and json.dumps(by_id[r["id"]], sort_keys=True) != json.dumps(r, sort_keys=True):
            conflicts.append((r["id"], by_id[r["id"]], r))
    return conflicts


def _file_sha(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser(description="聚鲸工作流 记忆合并协议（M2；dry-run 默认，--apply 才写）")
    ap.add_argument("--theirs", required=True, help="对方 skill 根目录（含 assets/data/knowledge）")
    ap.add_argument("--apply", action="store_true", help="执行写入（先自动 zip 备份本方 data+knowledge）")
    ap.add_argument("--backup-dir", default=None, help="备份目录（缺省 <skill>/../jujing-backups/）")
    a = ap.parse_args()

    theirs = pathlib.Path(a.theirs).resolve()
    for d in ("assets/params.json", "data", "knowledge"):
        if not (theirs / d).exists():
            sys.exit(f"[merge] --theirs 结构不完整：缺 {d}（须为完整 skill 根目录）")
    print(f"[merge] 本方 = {_ROOT}")
    print(f"[merge] 对方 = {theirs}（模式：{'APPLY' if a.apply else 'dry-run'}）")

    # 0. actors 准入 diff（不阻塞，但 kb-lint ⑤ 会红直到人工登记）
    try:
        actors_mine = {k: v for k, v in json.loads(_PARAMS.read_text(encoding="utf-8"))["memory"]["actors"].items()
                       if not k.startswith("_")}
        actors_theirs = {k: v for k, v in json.loads((theirs / "assets" / "params.json").read_text(encoding="utf-8"))["memory"]["actors"].items()
                         if not k.startswith("_")}
    except (json.JSONDecodeError, KeyError) as e:
        sys.exit(f"[merge] params.json#memory.actors 读取失败: {e}")
    unknown = set(actors_theirs) - set(actors_mine)
    print(f"[merge] actors 登记: 本方 {sorted(actors_mine)}；对方新增未登记 {sorted(unknown) or '无'}"
          + ("（建议准入登记进 params#memory.actors，否则 kb-lint ⑤ 报红）" if unknown else ""))

    conflicts_any = False
    wal_conflicts = False
    if a.apply:
        _backup(_ROOT, a.backup_dir)          # 写入前快照：无 git 的安全网

    # 1. jsonl 行级合并＋wal ID 冲突检测
    for name in _JSONLS:
        out, base_rows, other_rows = _merge_one_jsonl(_ROOT / "data" / name, theirs / "data" / name)
        dup = len(base_rows) + len(other_rows) - len(out)
        print(f"[merge] data/{name}: 本方 {len(base_rows)} 行 ＋ 对方 {len(other_rows)} 行 "
              f"-> 合并 {len(out)} 行（全等去重 {dup} 行）")
        if name == "wal.jsonl":
            conflicts = _id_conflicts(base_rows, other_rows)
            if conflicts:
                conflicts_any = wal_conflicts = True
                print(f"[merge] data/wal.jsonl: ⚠ ID 冲突 {len(conflicts)} 对（同 id 不同内容，绝不自动改写）:")
                for wid, b, o in conflicts:
                    print(f"    id={wid}")
                    print(f"      本方: {json.dumps(b, ensure_ascii=False)[:120]}")
                    print(f"      对方: {json.dumps(o, ensure_ascii=False)[:120]}")
                print("    -> 人工裁决（协议见本脚本 docstring：按消费留痕多者/时间新者，理由记 evolution.md），改后重跑")
        if a.apply and not wal_conflicts:
            (_ROOT / "data" / name).write_text("".join(t + "\n" for t in out), encoding="utf-8")
            # 零丢行自验
            _, after_rows = _read_jsonl(_ROOT / "data" / name)
            after_keys = {json.dumps(r, ensure_ascii=False, sort_keys=True) if r is not None else None
                          for r in after_rows}
            lost = [r for r in base_rows + other_rows
                    if (json.dumps(r, ensure_ascii=False, sort_keys=True) if r is not None else None) not in after_keys]
            if lost:
                sys.exit(f"[merge] 零丢行断言失败：data/{name} 丢失 {len(lost)} 行——检查备份并人工恢复")
            print(f"[merge] data/{name}: 已写入，零丢行断言 OK（本方⊆结果、对方⊆结果）")

    # 2. entries 合并
    ent_m, ent_t = _ROOT / "knowledge" / "entries", theirs / "knowledge" / "entries"
    copied = skipped = 0
    if ent_t.exists():
        for f in sorted(ent_t.glob("*.md")):
            dst = ent_m / f.name
            t_text = f.read_text(encoding="utf-8")
            if dst.exists():
                if _file_sha(dst.read_text(encoding="utf-8")) == _file_sha(t_text):
                    skipped += 1
                else:
                    conflicts_any = True
                    print(f"[merge] knowledge/entries/{f.name}: ⚠ 同名不同内容（人工裁决，协议见 docstring）")
            else:
                if a.apply:
                    dst.write_text(t_text, encoding="utf-8")
                copied += 1
                print(f"[merge] knowledge/entries/{f.name}: 对方独有 -> 并入"
                      + ("" if a.apply else "（dry-run 未写）"))
        print(f"[merge] entries: 独有并入 {copied}、同名同内容跳过 {skipped}"
              + (f"、已复制 {copied}" if a.apply else "（dry-run 未写）"))
        if a.apply and copied:
            print("[merge] 提示：新并入条目须人工注册进 knowledge/INDEX.md（漏注册会被 kb-lint ② 抓出）")

    # 3. INDEX/params 不自动合并——报告差异
    idx_m = (_ROOT / "knowledge" / "INDEX.md").read_text(encoding="utf-8")
    idx_t = (theirs / "knowledge" / "INDEX.md").read_text(encoding="utf-8")
    if _file_sha(idx_m) != _file_sha(idx_t):
        print("[merge] knowledge/INDEX.md: 双方不一致（两个权威索引不自动合并，人工并表）")
    if _file_sha(_PARAMS.read_text(encoding="utf-8")) != _file_sha((theirs / "assets" / "params.json").read_text(encoding="utf-8")):
        print("[merge] assets/params.json: 双方不一致（判据配置不自动合并，人工对齐）")

    if conflicts_any:
        print("[merge] 存在冲突：相关文件未写入（dry-run/apply 同），人工裁决后重跑本脚本")
        return 2
    if a.apply:
        print("[merge] 合并完成（零丢行）。收口三连：probe_usage.py size ＋ kb-lint 全绿；"
              "冲突裁决理由已记 evolution.md（如有）")
    else:
        print("[merge] dry-run 结束（未写任何文件）。确认后加 --apply 执行（自动 zip 备份）")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
