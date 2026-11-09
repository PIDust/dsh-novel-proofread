#!/usr/bin/env python3
"""小说校对 skill 执行脚本（检测规则在同目录 scanner.py，三个维度 basic/punct/typo 一遍扫描）。

子命令（按流程顺序）：
  prepare   步骤1：只读检查输入文件（编码/行数/章节），并检测或生成错别字词库（启用 typo 维度时）
  init      步骤3：生成执行批次码、备份原文件、创建批次信息与思考过程文件
  scan      步骤4/阶段A：脚本单遍扫描，产出冻结的候选清单 TSV
  ctx       阶段B 辅助：按固定窗口打印某行上下文（禁止全文检索）
  judge     阶段B：把模型写的判定 TSV 回填进候选清单（每条只能判一次）
  apply     阶段C：由候选清单生成修复清单，逐条“恰好匹配一次”替换，写回原文件
  pending   阶段B 辅助：分页列出尚未判定的候选
  confirm   步骤7：列出需要用户确认的问题（疑似未修改/成对符号异常/异常换行/替换失败），供模型在对话中询问用户
  resolve   步骤7：把用户在对话中确认的决定回填并应用（未经用户确认不得交付）
  renumber  步骤7：章节号顺序重排（仅在确认相邻章节语义连续、只是编号错误时使用；默认只预览）
  diff      由代码对比备份与当前原文件，生成校对前后 diff（禁止 LLM 手写 diff）
  finalize  步骤8：校验用户确认已完成，生成已处理报告并把 diff 嵌入（不生成“未处理报告”）

所有产出文件都带同一批次码，并以独占方式创建（'x' 模式），不覆盖已有文件。
"""
import argparse
import bisect
import csv
import datetime as dt
import difflib
import json
import logging
import os
import re
import secrets
import shutil
import string
import sys
from pathlib import Path

import pandas as pd

from common import CHAPTER_RE, chapter_runs, is_chapter, num2cn  # 章节标题判定与扫描器保持一致
import scanner  # 检测规则：提供 MODE / RESULTS / MODIFY_RESULTS / CONFIRM_RESULTS / prepare() / scan() / post_apply()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
log = logging.getLogger("proofread")

CAND_COLS = ["候选ID", "行号", "候选类型", "结构标记", "上下文片段", "判定状态", "判定结果", "判定依据", "修改动作", "备注",
             "旧片段", "新片段", "加入词库"]  # 末三列由 judge 回填：旧/新片段供 apply 精确替换；加入词库仅对错别字有效
JUDGE_COLS = ["候选ID", "判定结果", "判定依据", "旧片段", "新片段"]  # 可选第6列“加入词库”（是/否，仅对错别字有效）
FIX_COLS = ["候选ID", "行号", "旧片段", "新片段", "修改理由"]
FAIL_COLS = ["候选ID", "行号", "旧片段", "新片段", "失败类型", "目标行实际内容摘要"]
MERGE_NEXT = "[[与下行合并]]"  # 旧片段或新片段写这个标记 = 把该行与下一行合并（另一列可留空）
CONFIRM_COLS = ["候选ID", "用户决定", "旧片段", "新片段", "说明"]  # resolve 输入；用户决定：修改 / 不改
DELETE_LINE = "[[删除整行]]"  # 新片段写这个标记 = 删除整行（旧片段须等于该行去首尾空白后的全部内容）
END_ANCHOR = "[[行尾]]"  # 旧片段写这个标记 = 把新片段追加到该行末尾（行尾空白之前），用于补收尾标点


class FatalError(Exception):
    """不可继续执行的错误：立即停止并报告，禁止静默兜底。"""


# ---------------------------------------------------------------- 文件读写
def read_text(path):
    """读取 txt：依次尝试 utf-8-sig / gb18030，返回 (lines, encoding, has_bom)。
    lines 为 [(内容, 行尾)]，行尾原样保留，保证写回时只改动被替换的字符。"""
    raw = Path(path).read_bytes()
    if not raw.strip():
        raise FatalError(f"文件为空：{path}")
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            log.info("按 %s 解码失败，尝试下一种编码", enc)  # GBK 文本走到这里属正常
    else:
        raise FatalError(f"无法识别文件编码（utf-8/gb18030 均失败）：{path}")
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    lines = []
    parts = text.split("\n")
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        if last and p == "":
            break  # 文件以换行结尾时 split 会多出一个空串
        if p.endswith("\r"):
            lines.append((p[:-1], "\r\n" if not last else "\r"))
        else:
            lines.append((p, "" if last else "\n"))
    return lines, ("utf-8" if enc == "utf-8-sig" else enc), has_bom


def write_text(path, lines, encoding, has_bom):
    """原子写回原文件：先写同目录临时文件再 os.replace，失败则抛错（不得另存“校对后”文件）。"""
    data = "".join(c + e for c, e in lines).encode(encoding)
    if has_bom:
        data = b"\xef\xbb\xbf" + data
    tmp = Path(path).with_name(f".{Path(path).name}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise FatalError(f"无法原地覆盖原文件，停止执行：{e}")


def read_tsv(path, cols=None):
    df = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False, quoting=csv.QUOTE_NONE, encoding="utf-8")
    if cols:
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise FatalError(f"{path} 缺少列：{missing}（应为：{cols}）")
    return df


def write_tsv_new(path, df):
    """独占创建 TSV，已存在则报错，保证增量保存不覆盖。"""
    with open(path, "x", encoding="utf-8", newline="") as f:
        df.to_csv(f, sep="\t", index=False, quoting=csv.QUOTE_NONE)


def write_new(path, content):
    with open(path, "x", encoding="utf-8") as f:
        f.write(content)


def clean_cell(s):
    """TSV 单元格内不允许出现制表符/换行，可视化替换，避免破坏表结构。"""
    return str(s).replace("\t", "⇥").replace("\r", "").replace("\n", "⏎")


# ---------------------------------------------------------------- 批次信息
def out_path(meta, suffix):
    """按【原名-执行批次码-xxx】规则拼接产出文件路径。"""
    src = Path(meta["file"])
    return src.with_name(f"{src.stem}-{meta['batch']}-{suffix}")


def load_meta(meta_path):
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    if meta.get("mode") != scanner.MODE:
        raise FatalError(f"批次信息的 mode={meta.get('mode')} 与当前脚本的 mode={scanner.MODE} 不一致")
    return meta


def chapter_index(lines):
    """返回每行所属章节标题（取该行之前最近的章节行），用于报告中的“位置”。"""
    cur, res = "（正文开头）", []
    for c, _ in lines:
        if is_chapter(c):
            cur = c.strip()[:30]
        res.append(cur)
    return res


# ---------------------------------------------------------------- 子命令
def cmd_prepare(a):
    lines, enc, bom = read_text(a.file)
    chapters = sum(1 for c, _ in lines if is_chapter(c))
    print(f"文件：{a.file}\n编码：{enc}{'(BOM)' if bom else ''}\n总行数：{len(lines)}\n识别到章节行：{chapters}")
    scanner.prepare(Path(a.file), lines, a)


def cmd_init(a):
    src = Path(a.file).resolve()
    lines, enc, bom = read_text(src)  # 先确认可读，异常输入不产生任何文件
    batch = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
    if not re.fullmatch(r"\d{8}-\d{6}-[A-Za-z0-9]{6}", batch):
        raise FatalError(f"批次码生成异常：{batch}")
    meta = {"mode": scanner.MODE, "file": str(src), "batch": batch, "encoding": enc, "bom": bom,
            "lines": len(lines)}
    backup = src.with_name(f"{src.stem}-{batch}-备份{src.suffix}")
    if backup.exists():
        raise FatalError(f"备份文件已存在，拒绝覆盖：{backup}")
    shutil.copy2(src, backup)
    if backup.read_bytes() != src.read_bytes():
        raise FatalError("备份校验失败：备份内容与原文件不一致")
    meta["backup"] = str(backup)
    meta_path = out_path(meta, "批次信息.json")
    write_new(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
    write_new(out_path(meta, "思考过程.md"),
              f"# 思考过程（批次 {batch}）\n\n- 文件：{src.name}\n\n")
    print(f"批次码：{batch}\n备份：{backup}\n批次信息：{meta_path}\n思考过程：{out_path(meta, '思考过程.md')}")


def cmd_scan(a):
    meta = load_meta(a.meta)
    cand_path = out_path(meta, "候选清单.tsv")
    if cand_path.exists():
        raise FatalError(f"候选清单已存在（阶段A 只允许执行一次，清单已冻结）：{cand_path}")
    lines, _, _ = read_text(meta["file"])
    hits = scanner.scan(lines, Path(meta["file"]), a)  # [(行号, 类型, 结构标记, 片段, 备注)]
    hits.sort(key=lambda h: h[0])  # 单遍、按行号单向排序
    rows, per_line = [], {}
    for i, (ln, typ, mark, snippet, note) in enumerate(hits, 1):
        per_line[ln] = per_line.get(ln, 0) + 1
        rows.append([f"C{i:05d}-L{ln}-{per_line[ln]}", ln, typ, clean_cell(mark), clean_cell(snippet),
                     "待判定", "", "", "", clean_cell(note), "", "", ""])
    df = pd.DataFrame(rows, columns=CAND_COLS)
    write_tsv_new(cand_path, df)
    stats = df["候选类型"].value_counts().to_dict() if len(df) else {}
    print(f"候选清单：{cand_path}\n候选总数：{len(df)}\n按类型：{json.dumps(stats, ensure_ascii=False)}")


def cmd_ctx(a):
    meta = load_meta(a.meta)
    radius = min(a.radius, scanner.MAX_CTX_RADIUS)  # 固定上下文窗口，超过上限自动截断
    if a.radius > scanner.MAX_CTX_RADIUS:
        log.warning("请求半径 %d 超过本 skill 上限 %d，已截断", a.radius, scanner.MAX_CTX_RADIUS)
    lines, _, _ = read_text(meta["file"])  # 一次读取，支持一次查看多行，避免大文件反复读盘
    targets = [int(x) for x in re.split(r"[,，\s]+", str(a.line).strip()) if x]
    for t in targets:
        if len(targets) > 1:
            print(f"===== L{t} =====")
        for n in range(max(1, t - radius), min(len(lines), t + radius) + 1):
            print(f"{'>>' if n == t else '  '}{n:>7}│{lines[n - 1][0]}")


def cmd_pending(a):
    meta = load_meta(a.meta)
    cand = read_tsv(out_path(meta, "候选清单.tsv"), CAND_COLS)
    todo = cand[cand["判定状态"] != "已判定"]
    print(f"# 待判定 {len(todo)} / 共 {len(cand)}，以下显示前 {min(a.limit, len(todo))} 条")
    cols = ["候选ID", "行号", "候选类型", "结构标记", "上下文片段", "备注"]
    print("\t".join(cols))
    for r in todo.head(a.limit)[cols].itertuples(index=False):
        print("\t".join(map(str, r)))


def cmd_judge(a):
    meta = load_meta(a.meta)
    cand_path = out_path(meta, "候选清单.tsv")
    cand = read_tsv(cand_path, CAND_COLS)
    jd = read_tsv(a.judgments, JUDGE_COLS)
    idx = {cid: i for i, cid in enumerate(cand["候选ID"])}
    ok = bad = 0
    for _, r in jd.iterrows():
        cid = r["候选ID"].strip()
        if cid not in idx:
            log.error("候选ID 不在冻结清单中（不得新增候选）：%s", cid); bad += 1; continue
        i = idx[cid]
        if cand.at[i, "判定状态"] == "已判定":
            log.error("候选 %s 已判定并锁定，拒绝改判", cid); bad += 1; continue
        res = r["判定结果"].strip()
        if res not in scanner.RESULTS:
            log.error("候选 %s 判定结果非法：%s（允许：%s）", cid, res, scanner.RESULTS); bad += 1; continue
        if not r["判定依据"].strip():
            log.error("候选 %s 缺少判定依据", cid); bad += 1; continue
        if res in scanner.MODIFY_RESULTS:
            if not r["旧片段"]:
                log.error("候选 %s 判定为 %s 但旧片段为空", cid, res); bad += 1; continue
            if r["旧片段"] == r["新片段"]:
                log.error("候选 %s 旧片段与新片段相同", cid); bad += 1; continue
            action = f"{r['旧片段']} → {r['新片段'] or '（删除）'}"
        else:
            action = "不修改"
        keep = res in scanner.MODIFY_RESULTS or res in scanner.CONFIRM_RESULTS  # 待确认类保留“建议改法”
        olds, news = (r["旧片段"], r["新片段"]) if keep else ("", "")
        to_dict = "是" if res in scanner.MODIFY_RESULTS and r.get("加入词库", "").strip() == "是" else ""
        cand.loc[i, ["判定状态", "判定结果", "判定依据", "修改动作", "旧片段", "新片段", "加入词库"]] = \
            ["已判定", res, clean_cell(r["判定依据"]), clean_cell(action), olds, news, to_dict]
        ok += 1
    tmp = cand_path.with_suffix(".tmp")
    cand.to_csv(tmp, sep="\t", index=False, quoting=csv.QUOTE_NONE)
    os.replace(tmp, cand_path)
    left = int((cand["判定状态"] != "已判定").sum())
    print(f"本次回填成功：{ok}，拒绝：{bad}，剩余待判定：{left}")
    if bad:
        sys.exit(2)


def apply_fixes(contents, fixes):
    """逐条应用 [(候选ID, 行号, 旧片段, 新片段)]，行号以本次应用前的文本为准。每条要求旧片段在目标行恰好出现一次，
    失败只记录、不重试。返回 (新内容列表, 需删除的行号集合, 失败列表, 成功数)。"""
    contents, deleted, fails, success = list(contents), set(), [], 0
    for cid, ln, old, new in fixes:
        if MERGE_NEXT in (old, new):  # 合并段落：标记写在旧片段或新片段都可以，按整行处理
            old, new = MERGE_NEXT, MERGE_NEXT
        def fail(kind, actual=""):
            fails.append([cid, ln, clean_cell(old), clean_cell(new), kind, clean_cell(actual[:80])])
            log.warning("替换失败 %s 行%d：%s", cid, ln, kind)
        if not 1 <= ln <= len(contents):
            fail("行号越界"); continue
        cur = contents[ln - 1]
        if ln in deleted:
            fail("目标行已被删除或已并入上一行", cur); continue
        if old == END_ANCHOR:
            body = cur.rstrip(" 　\t")
            contents[ln - 1] = body + new + cur[len(body):]; success += 1; continue
        if new in (DELETE_LINE, MERGE_NEXT):
            if new == DELETE_LINE and cur.strip() != old.strip():
                fail(f"0次匹配（{new}要求旧片段等于整行）", cur); continue
            if new == MERGE_NEXT:
                if ln >= len(contents) or ln + 1 in deleted:
                    fail("下一行不存在或已被删除", cur); continue
                contents[ln - 1] = cur.rstrip(" 　\t") + contents[ln].strip(" 　\t")
            deleted.add(ln + 1 if new == MERGE_NEXT else ln); success += 1; continue
        cnt = cur.count(old)
        if cnt != 1:
            fail("0次匹配" if cnt == 0 else f"多次匹配({cnt}次)", cur); continue
        contents[ln - 1] = cur.replace(old, new, 1); success += 1
    return contents, deleted, fails, success


def rebuild(lines, contents, deleted):
    """按原行尾重组文本，跳过被删除/被合并的行。"""
    return [(c, e) for n, (c, (_, e)) in enumerate(zip(contents, lines), 1) if n not in deleted]


DEFAULT_TAG_RE = re.compile(r"^\s*【(默认改|默认不改|需询问)】")  # 判定依据开头的默认处理标签，见 judging-rules.md“步骤 7 默认处理规则”


def confirm_items(meta, cand):
    """需要用户确认的条目：判定为 CONFIRM_RESULTS 的候选 + apply 替换失败的条目。“默认”列取自判定依据开头的标签，缺省为需询问。"""
    items = cand[cand["判定结果"].isin(scanner.CONFIRM_RESULTS)][["候选ID", "行号", "判定结果", "候选类型", "上下文片段", "判定依据", "旧片段", "新片段"]]
    fail_path = out_path(meta, "失败清单.tsv")
    if fail_path.exists():
        f = read_tsv(fail_path, FAIL_COLS)
        items = pd.concat([items, pd.DataFrame({"候选ID": f["候选ID"], "行号": f["行号"], "判定结果": "替换失败",
                                                "候选类型": f["失败类型"], "上下文片段": f["目标行实际内容摘要"],
                                                "判定依据": "【需询问】原计划：" + f["旧片段"] + " → " + f["新片段"],
                                                "旧片段": "", "新片段": ""})])
    items = items.copy()
    items["默认"] = [m.group(1) if m else "需询问" for m in items["判定依据"].map(DEFAULT_TAG_RE.match)]
    return items


def cmd_confirm(a):
    meta = load_meta(a.meta)
    if not out_path(meta, "修复清单.tsv").exists():
        raise FatalError("请先执行 apply")
    items = confirm_items(meta, read_tsv(out_path(meta, "候选清单.tsv"), CAND_COLS))
    chap = chapter_index(read_text(meta["backup"])[0])
    cnt = items["默认"].value_counts().to_dict()
    print(f"# 需要用户确认：{len(items)} 条（默认改 {cnt.get('默认改', 0)}，默认不改 {cnt.get('默认不改', 0)}，"
          f"需询问 {cnt.get('需询问', 0)}）。请在对话中按组询问用户，拿到明确答复后用 resolve 回填；未确认前不得交付")
    for tag in ("需询问", "默认改", "默认不改"):
        for res, sub in items[items["默认"] == tag].groupby("判定结果", sort=False):
            print(f"\n## 【{tag}】{res}（{len(sub)} 条）")
            for k, r in enumerate(sub.itertuples(index=False), 1):
                fix = f"  建议：{r.旧片段} → {r.新片段 or '（删除）'}" if r.旧片段 else ""
                print(f"{k}. [{r.候选ID}] L{r.行号}（{chap[int(r.行号) - 1]}）{r.候选类型}：「{r.上下文片段}」—— "
                      f"{DEFAULT_TAG_RE.sub('', r.判定依据)}{fix}")
    if a.draft:  # 按默认规则生成决定草稿：默认改→修改（用建议改法），默认不改→不改，需询问→待定（必须问用户后改成修改/不改）
        dec = {"默认改": "修改", "默认不改": "不改", "需询问": "待定"}
        draft = pd.DataFrame({"候选ID": items["候选ID"], "用户决定": items["默认"].map(dec),
                              "旧片段": items["旧片段"], "新片段": items["新片段"], "说明": "按默认规则：" + items["默认"]})
        bad = draft[(draft["用户决定"] == "修改") & (draft["旧片段"] == "")]
        draft.loc[bad.index, "用户决定"] = "待定"  # 默认改却没有建议改法的，降级为待定
        write_tsv_new(Path(a.draft), draft)
        print(f"\n# 决定草稿：{a.draft}（待定 {int((draft['用户决定'] == '待定').sum())} 条，必须问用户后改成“修改/不改”）")


def cmd_resolve(a):
    meta = load_meta(a.meta)
    rec_path = out_path(meta, "用户确认记录.tsv")
    if rec_path.exists():
        raise FatalError(f"用户确认已回填过：{rec_path}（用户追加的决定请在下一轮校对中处理）")
    items = confirm_items(meta, read_tsv(out_path(meta, "候选清单.tsv"), CAND_COLS))
    dec = read_tsv(a.decisions, CONFIRM_COLS)
    need, got = set(items["候选ID"]), set(dec["候选ID"])
    if need - got or got - need:
        raise FatalError(f"确认决定与待确认清单不一致：缺少 {sorted(need - got)[:10]}，多出 {sorted(got - need)[:10]}")
    bad = dec[~dec["用户决定"].isin(["修改", "不改"]) | ((dec["用户决定"] == "修改") & (dec["旧片段"] == ""))]
    if len(bad):
        raise FatalError(f"用户决定只能是“修改/不改”（“待定”必须先问用户），且“修改”必须给出旧片段：{bad['候选ID'].tolist()[:10]}")
    lines, enc, bom = read_text(meta["file"])
    del_path = out_path(meta, "删除行号.json")
    removed = json.loads(del_path.read_text(encoding="utf-8")) if del_path.exists() else []
    if len(lines) != meta["lines"] - len(removed):
        raise FatalError(f"当前行数 {len(lines)} 与预期 {meta['lines'] - len(removed)} 不符（原文件在 apply 之后被改动过），拒绝继续")
    shift = lambda n: n - bisect.bisect_left(removed, n)  # 原行号 → apply 后的行号
    ln_of = {c: shift(int(n)) for c, n in zip(items["候选ID"], items["行号"])}
    gone = [c for c, n in zip(items["候选ID"], items["行号"]) if int(n) in set(removed)]
    if gone:
        log.warning("以下待确认条目所在行已在 apply 中删除，若用户要求修改将失败：%s", gone[:10])
    todo = dec[dec["用户决定"] == "修改"]
    fixes = sorted(((r.候选ID, ln_of[r.候选ID], r.旧片段, r.新片段) for r in todo.itertuples(index=False)), key=lambda x: x[1])
    contents, deleted, fails, success = apply_fixes([c for c, _ in lines], fixes)
    write_text(meta["file"], rebuild(lines, contents, deleted), enc, bom)
    failed = {f[0] for f in fails}
    dec["应用结果"] = ["失败" if c in failed else ("成功" if d == "修改" else "不改") for c, d in zip(dec["候选ID"], dec["用户决定"])]
    write_tsv_new(rec_path, dec.apply(lambda col: col.map(clean_cell)))
    print(f"用户确认：{len(dec)} 条；要求修改 {len(todo)} 条，成功 {success}，失败 {len(fails)}；不改 {len(dec) - len(todo)} 条")
    for f in fails:
        log.error("用户确认的修改未能应用 %s 行%d：%s（旧片段须在该行恰好出现一次）", f[0], f[1], f[4])


def cmd_renumber(a):
    """章节号重排（仅用于用户已确认“相邻章节语义连续、只是编号错误”的情况）。
    默认只预览；加 --apply 才写回原文件，并记入【原名-批次码-章节重排记录.tsv】。行数不变，可在 resolve 之后执行。"""
    meta = load_meta(a.meta)
    lines, enc, bom = read_text(meta["file"])
    seq, fixes, k = chapter_runs(lines), [], 0
    for ln, num, title, arabic in seq:
        if num is None:  # 卷/部/集：章号从 1 重新开始
            k = 0
            continue
        k += 1
        if num == k:
            continue
        m = CHAPTER_RE.match(lines[ln - 1][0])
        old_no = m.group(2)
        new_no = str(k) if arabic else num2cn(k)
        fixes.append([f"R{len(fixes) + 1:05d}", ln, f"第{old_no}章", f"第{new_no}章", f"原章号{num}→{k}"])
    df = pd.DataFrame(fixes, columns=FIX_COLS)
    print(f"章节标题 {sum(1 for s in seq if s[1] is not None)} 个，需要改号 {len(df)} 个")
    for r in df.head(a.preview).itertuples(index=False):
        print(f"L{r[1]}  {r[2]} → {r[3]}  （{r[4]}）")
    if not a.apply:
        print("# 以上为预览；用户确认相邻章节语义连续后，加 --apply 执行")
        return
    contents, deleted, fails, success = apply_fixes([c for c, _ in lines], [f[:4] for f in fixes])
    if fails:
        raise FatalError(f"有 {len(fails)} 个章节标题无法唯一匹配，已放弃重排（未写回）：{fails[:3]}")
    write_text(meta["file"], rebuild(lines, contents, deleted), enc, bom)
    write_tsv_new(out_path(meta, "章节重排记录.tsv"), df)
    print(f"已重排 {success} 个章节标题")


def cmd_apply(a):
    meta = load_meta(a.meta)
    cand = read_tsv(out_path(meta, "候选清单.tsv"), CAND_COLS)
    pending = cand[cand["判定状态"] != "已判定"]
    if len(pending):
        raise FatalError(f"仍有 {len(pending)} 条候选未判定，不能进入阶段C（首个：{pending.iloc[0]['候选ID']}）")
    fixes = []
    for _, r in cand[cand["判定结果"].isin(scanner.MODIFY_RESULTS)].iterrows():
        fixes.append([r["候选ID"], int(r["行号"]), r["旧片段"], r["新片段"], r["判定依据"]])
    fix_df = pd.DataFrame(fixes, columns=FIX_COLS)
    write_tsv_new(out_path(meta, "修复清单.tsv"), fix_df)

    lines, enc, bom = read_text(meta["file"])
    if Path(meta["backup"]).read_bytes() != Path(meta["file"]).read_bytes():
        raise FatalError("原文件在备份后已被改动，拒绝在不一致的基础上应用修改")
    contents, deleted, fails, success = apply_fixes([c for c, _ in lines], [f[:4] for f in fixes])
    write_text(meta["file"], rebuild(lines, contents, deleted), enc, bom)
    if deleted:  # 记录被删除的原行号，resolve 据此把原行号换算到 apply 之后的行号
        Path(out_path(meta, "删除行号.json")).write_text(json.dumps(sorted(deleted)), encoding="utf-8")
    if fails:
        write_tsv_new(out_path(meta, "失败清单.tsv"), pd.DataFrame(fails, columns=FAIL_COLS))
    if hasattr(scanner, "post_apply"):  # 后处理（错别字词库增量追加），只处理替换成功的条目
        failed = {f[0] for f in fails}
        scanner.post_apply(meta, cand[cand["判定结果"].isin(scanner.MODIFY_RESULTS) & ~cand["候选ID"].isin(failed)])
    print(f"修复清单：{len(fixes)} 条；成功：{success}；失败：{len(fails)}"
          + (f"\n失败清单：{out_path(meta, '失败清单.tsv')}" if fails else ""))


def build_diff(meta):
    """用 difflib 对比备份（校对前）与原文件（校对后），返回 (diff 文本, 变更行数统计)。"""
    before, _, _ = read_text(meta["backup"])
    after, _, _ = read_text(meta["file"])
    src = Path(meta["file"]).name
    diff = list(difflib.unified_diff([c for c, _ in before], [c for c, _ in after],
                                     fromfile=f"{src}（校对前：{Path(meta['backup']).name}）",
                                     tofile=f"{src}（校对后）", n=0, lineterm=""))
    minus = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    plus = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    return "\n".join(diff), minus, plus


def cmd_diff(a):
    meta = load_meta(a.meta)
    text, minus, plus = build_diff(meta)
    path = out_path(meta, "校对diff.diff")
    if not path.exists():
        write_new(path, text + "\n")
    elif path.read_text(encoding="utf-8").rstrip("\n") != text:
        raise FatalError(f"{path} 已存在且与当前计算结果不一致（原文件在 diff 生成后被改动）")
    print(f"# diff 文件：{path}\n# 删除行 {minus}，新增行 {plus}（由 difflib 自动计算）")
    lines = text.splitlines()
    if a.max_lines and len(lines) > a.max_lines:
        print("\n".join(lines[:a.max_lines]))
        print(f"# ……diff 共 {len(lines)} 行，以上为前 {a.max_lines} 行（脚本截断），完整内容见 {path}")
    else:
        print(text if text else "# 校对前后无差异")


def md_cell(s):
    return str(s).replace("|", "\\|")


def cmd_finalize(a):
    meta = load_meta(a.meta)
    cand = read_tsv(out_path(meta, "候选清单.tsv"), CAND_COLS)
    if not out_path(meta, "修复清单.tsv").exists():
        raise FatalError("修复清单不存在，请先执行 apply")
    think = out_path(meta, "思考过程.md")
    if len(think.read_text(encoding="utf-8").strip().splitlines()) <= 4:
        raise FatalError(f"思考过程文件尚未写入任何内容：{think}")
    items = confirm_items(meta, cand)
    rec_path = out_path(meta, "用户确认记录.tsv")
    if len(items) and not rec_path.exists():
        raise FatalError(f"还有 {len(items)} 条问题未经用户确认：先用 confirm 列出并在对话中询问用户，再用 resolve 回填，最后才能交付")
    rec = read_tsv(rec_path) if rec_path.exists() else pd.DataFrame(columns=CONFIRM_COLS + ["应用结果"])
    fail_path = out_path(meta, "失败清单.tsv")
    failed_ids = set(read_tsv(fail_path, FAIL_COLS)["候选ID"]) if fail_path.exists() else set()
    chap = chapter_index(read_text(meta["backup"])[0])  # 位置信息以校对前文本为准
    diff_text, minus, plus = build_diff(meta)
    diff_path = out_path(meta, "校对diff.diff")
    if not diff_path.exists():
        write_new(diff_path, diff_text + "\n")
    elif diff_path.read_text(encoding="utf-8").rstrip("\n") != diff_text:
        raise FatalError(f"{diff_path} 是 resolve 之前生成的，与当前结果不一致；请在 resolve 之后再运行 diff")

    done = cand[cand["判定结果"].isin(scanner.MODIFY_RESULTS) & ~cand["候选ID"].isin(failed_ids)]
    user_done = rec[rec["应用结果"] == "成功"]
    out = [f"# 校对报告（批次 {meta['batch']}）\n",
           f"- 原文件：{meta['file']}\n- 备份文件：{meta['backup']}\n- 执行批次码：{meta['batch']}\n"
           f"- 候选总数：{len(cand)}；自动修改：{len(done)}；经用户确认后修改：{len(user_done)}；"
           f"用户确认不改：{int((rec['应用结果'] == '不改').sum())}\n",
           "\n## 已修改项\n", "| 候选ID | 行号 | 位置（章节） | 类型 | 原文 → 改后 | 修改理由 | 来源 |", "|---|---|---|---|---|---|---|"]
    for _, r in done.iterrows():
        ln = int(r["行号"])
        out.append(f"| {r['候选ID']} | {ln} | {md_cell(chap[ln - 1])} | {r['候选类型']} | {md_cell(r['修改动作'])} | "
                   f"{md_cell(r['判定依据'])} | 自动 |")
    ln_of = dict(zip(items["候选ID"], items["行号"])) if len(items) else {}
    for _, r in user_done.iterrows():
        ln = int(ln_of.get(r["候选ID"], 0) or 0)
        out.append(f"| {r['候选ID']} | {ln} | {md_cell(chap[ln - 1]) if ln else ''} | 用户确认 | "
                   f"{md_cell(r['旧片段'] + ' → ' + (r['新片段'] or '（删除）'))} | {md_cell(r['说明'])} | 用户确认 |")
    ren_path = out_path(meta, "章节重排记录.tsv")
    ren = read_tsv(ren_path, FIX_COLS) if ren_path.exists() else pd.DataFrame(columns=FIX_COLS)
    for r in ren.itertuples(index=False):
        out.append(f"| {r[0]} | {r[1]} | | 章节重排 | {md_cell(r[2] + ' → ' + r[3])} | {md_cell(r[4])} | 用户确认 |")
    if not len(done) and not len(user_done) and not len(ren):
        out.append("| （无） | | | | | | |")
    out += ["\n## 校对前后 diff（difflib 自动计算，非人工/LLM 编写）\n",
            f"- diff 文件：{diff_path}\n- 删除行 {minus}，新增行 {plus}\n", "```diff", diff_text or "# 无差异", "```\n"]
    write_new(out_path(meta, "校对报告.md"), "\n".join(out))
    # 一致性核对：修改只改写、删除或合并行，不新增行，所以“删除行数 - 新增行数”应等于被删除（含合并）的行数
    del_cnt = len(read_text(meta["backup"])[0]) - len(read_text(meta["file"])[0])
    if minus - plus != del_cnt:
        log.warning("diff 行数(-%d/+%d) 与实际减少的行数(%d) 不一致，请人工核对", minus, plus, del_cnt)
    print("\n".join(str(out_path(meta, s)) for s in ["校对报告.md", "思考过程.md", "校对diff.diff"]))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prepare"); s.add_argument("--file", required=True); scanner.add_args(s)
    s = sub.add_parser("init"); s.add_argument("--file", required=True)
    s = sub.add_parser("scan"); s.add_argument("--meta", required=True); scanner.add_args(s)
    s = sub.add_parser("ctx"); s.add_argument("--meta", required=True); s.add_argument("--line", required=True, help="行号；可用逗号分隔一次查看多行，如 12,345,678")
    s.add_argument("--radius", type=int, default=scanner.MAX_CTX_RADIUS)
    s = sub.add_parser("pending"); s.add_argument("--meta", required=True); s.add_argument("--limit", type=int, default=50)
    s = sub.add_parser("judge"); s.add_argument("--meta", required=True); s.add_argument("--judgments", required=True)
    s = sub.add_parser("apply"); s.add_argument("--meta", required=True)
    s = sub.add_parser("confirm"); s.add_argument("--meta", required=True)
    s.add_argument("--draft", help="按默认规则生成用户决定草稿 TSV 的路径（不能已存在）")
    s = sub.add_parser("resolve"); s.add_argument("--meta", required=True); s.add_argument("--decisions", required=True)
    s = sub.add_parser("renumber"); s.add_argument("--meta", required=True); s.add_argument("--apply", action="store_true")
    s.add_argument("--preview", type=int, default=30)
    s = sub.add_parser("diff"); s.add_argument("--meta", required=True); s.add_argument("--max-lines", type=int, default=0)
    s = sub.add_parser("finalize"); s.add_argument("--meta", required=True)
    a = p.parse_args()
    try:
        globals()[f"cmd_{a.cmd}"](a)
    except (FatalError, OSError, ValueError) as e:  # 含文件不存在/已存在、词库格式错误
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
