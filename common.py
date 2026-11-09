"""三个校对 skill 共用的机械检测函数（只做定位，不做判定）。

每个检测函数返回 [(行号, 候选类型, 结构标记, 上下文片段, 备注)]，由 scanner.scan() 汇总。
"""
import re
from collections import Counter

CJK = "\u4e00-\u9fff\u3400-\u4dbf"  # 汉字（基本区+扩展A）
CHAPTER_RE = re.compile(r"^[\s　]*(第([0-9０-９零〇一二三四五六七八九十百千万两]+)([章回节卷集部])|序章|楔子|尾声|番外|引子|后记)(.*)$")
PAIRS = {"（": "）", "(": ")", "《": "》", "【": "】", "〈": "〉", "「": "」", "『": "』", "［": "］"}
CLOSERS = {v: k for k, v in PAIRS.items()}
CROSS_LIMIT = 30  # 引号跨段最多容忍的行数，超过视为未闭合


def snip(text, start, end=None, pad=12):
    """截取 [start-pad, end+pad) 的最小片段，两端截断处用 … 标记。"""
    end = start + 1 if end is None else end
    s, e = max(0, start - pad), min(len(text), end + pad)
    return ("…" if s > 0 else "") + text[s:e] + ("…" if e < len(text) else "")


def is_chapter(line):
    """章节标题：匹配“第X章”等开头，整行不超过 40 字，章号后不紧跟标点、整行不以句末标点结尾。
    用于排除“第三回，三枚全部是花朝上。”“第一回亮势是用龙孝武。”这类正文句子；“第十一章 柳腰易折，媚骨天成”仍算标题。"""
    m = CHAPTER_RE.match(line)
    if not m:
        return False
    t, rest = line.strip(), m.group(4) or ""
    if m.group(3) and m.group(3) != "章" and rest and not re.match(r"[ 　:：]", rest):
        return False  # “第十八集团军”“第三回合”：非“章”的单位后必须接空格/冒号或直接结束
    if len(t) > 40 or re.match(r"[，。！？；、,!?;]", rest):
        return False
    # 章号后有空格/冒号的（“第五百七十四章 惠妙儿是谁？”）允许以问号、叹号结尾；紧贴正文的必须不以句末标点结尾
    return bool(re.match(r"[ 　:：]", rest)) or not rest or t[-1] not in "。！？!?"


# 格式错误的章节标题：章号含错字（如“四面”）、“章”误为“间”、章号与“章”之间有空格。
# 只匹配章号≥2 字的情况，避免误伤“第二张纸条”这类正文。
MALFORMED_HEAD_RE = re.compile(r"^[\s　]*第[0-9０-９零〇一二三四五六七八九十百千万两面]{2,10}[章间][ 　]|"
                               r"^[\s　]*第[0-9０-９零〇一二三四五六七八九十百千万两]{1,10}[ 　]+章")


def is_blank(line):
    return not line.strip(" 　\t　")


# ---------------------------------------------------------------- 成对符号
AUTHOR_NOTE_KEY = re.compile(r"更|章|码字|卡文|月票|推荐票|打赏|盟主|请假|欠|补|俺")
AUTHOR_NOTE_RE = re.compile(r"「[^」]{0,200}(更|章|码字|卡文|月票|推荐票|打赏|盟主|请假|欠|补|俺)[^」]{0,200}」|[（(](ps|PS|Ps|作者)[:：][^)）]*[)）]")


def author_note_spans(lines):
    """返回 {行号: [(起, 止)]}：每行中属于「作者的话」的字符区间，含跨行的作者的话（起始行从「到行尾，中间行整行，结尾行到」）。"""
    spans, open_ln = {}, None
    for ln, (t, _) in enumerate(lines, 1):
        if open_ln:
            end = t.find("」")
            spans.setdefault(ln, []).append((0, len(t) if end < 0 else end + 1))
            if end >= 0:
                open_ln = None
            continue
        spans[ln] = [m.span() for m in AUTHOR_NOTE_RE.finditer(t)]
        k = t.rfind("「")
        if k >= 0 and "」" not in t[k:] and AUTHOR_NOTE_KEY.search(t[k:]):
            spans[ln].append((k, len(t)))
            open_ln = ln
    return spans


def scan_pairs(lines):
    """检测引号/括号配对，返回 (候选列表, 每行引号状态字典)。

    双引号“”与单引号‘’按状态机跨行跟踪，识别两种合法的跨段引语写法：
      - 出版惯例：多段引语每段以左引号开头，只有末段有右引号；
      - 网文惯例：首段左引号、末段右引号，中间段不加引号。
    这两种情况不登记候选，只在行状态里标注，供“结尾无标点”检测参考。
    其他括号类只做行内栈匹配。
    """
    hits, state = [], {}
    for qo, qc, name in (("“", "”", "双引号"), ("‘", "’", "单引号")):
        open_ln = open_col = None  # 当前未闭合左引号的位置
        for ln, (text, _) in enumerate(lines, 1):
            if is_chapter(text) and open_ln:
                hits.append((open_ln, "成对符号", f"{name}未闭合(遇章节标题)", snip(lines[open_ln - 1][0], open_col), ""))
                open_ln = None
            for col, ch in enumerate(text):
                if ch == qo:
                    if open_ln is None:
                        open_ln, open_col = ln, col
                    elif open_ln != ln and text[:col].strip(" 　") == "":
                        state.setdefault(open_ln, set()).add(f"{name}跨段(出版惯例)")
                        open_ln, open_col = ln, col  # 出版惯例：新段左引号接续引语
                    else:
                        hits.append((ln, "成对符号", f"{name}嵌套/错位(左引号重复)", snip(text, col), ""))
                        open_ln, open_col = ln, col
                elif ch == qc:
                    if open_ln is None:
                        hits.append((ln, "成对符号", f"{name}多余右引号", snip(text, col), ""))
                    else:
                        if open_ln != ln:
                            state.setdefault(ln, set()).add(f"{name}跨段闭合(始于L{open_ln})")
                        open_ln = None
            if open_ln is not None and ln - open_ln > CROSS_LIMIT:
                hits.append((open_ln, "成对符号", f"{name}未闭合(超过{CROSS_LIMIT}行)", snip(lines[open_ln - 1][0], open_col), ""))
                open_ln = None
            if open_ln is not None:
                state.setdefault(ln, set()).add(f"{name}未闭合中")
        if open_ln is not None:
            hits.append((open_ln, "成对符号", f"{name}未闭合(至文末)", snip(lines[open_ln - 1][0], open_col), ""))
    note_spans = author_note_spans(lines)  # 作者的话另由广告/作者的话检测登记，这里跳过
    for ln, (text, _) in enumerate(lines, 1):  # 括号类：行内栈匹配
        notes = note_spans.get(ln, [])
        stack = []
        for col, ch in enumerate(text):
            if any(a <= col < b for a, b in notes):
                continue
            if ch in PAIRS:
                stack.append((ch, col))
            elif ch in CLOSERS:
                if stack and stack[-1][0] == CLOSERS[ch]:
                    stack.pop()
                else:
                    hits.append((ln, "成对符号", f"多余或错位的{ch}", snip(text, col), ""))
        hits += [(ln, "成对符号", f"{ch}未闭合", snip(text, col), "") for ch, col in stack]
        for m in re.finditer(r"[「」『』]", text):  # 繁体引号：「」对应“”，『』对应‘’
            if not any(a <= m.start() < b for a, b in notes):
                hits.append((ln, "成对符号", f"繁体引号{m.group()}", snip(text, m.start()), "简体对应：「」→“”，『』→‘’"))
    return hits, state


# ---------------------------------------------------------------- 章节结构
def cn2num(s):
    """中文/阿拉伯数字章节号转整数；无法解析返回 None。"""
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if s.isdigit():
        return int(s)
    digits = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    if all(c in digits for c in s):  # “一二三章”这种逐位写法
        return int("".join(str(digits[c]) for c in s))
    total = sec = num = 0
    for c in s:
        if c in digits:
            num = digits[c]
        elif c in units:
            u = units[c]
            if u == 10000:
                total, sec, num = (total + sec + num) * u, 0, 0
            else:
                sec += (num or 1) * u
                num = 0
        else:
            return None
    return total + sec + num


def num2cn(n):
    """整数转中文章号，风格与常见网文一致：十一、一百一十、一千零八、一千二百。"""
    d = "零一二三四五六七八九"
    if n < 10:
        return d[n]
    if n < 20:
        return "十" + (d[n % 10] if n % 10 else "")
    out, zero = "", False
    for unit, v in (("千", 1000), ("百", 100), ("十", 10), ("", 1)):
        q = n // v % 10
        if q:
            if zero and out:
                out += "零"
            out += d[q] + unit
            zero = False
        elif out:
            zero = True
    return out


def chapter_runs(lines):
    """返回全书“章”标题序列 [(行号, 章号, 标题全文, 编号是否为阿拉伯数字)]，卷/部/集出现时视为新的一段（允许重置）。"""
    seq = []
    for ln, (t, _) in enumerate(lines, 1):
        m = CHAPTER_RE.match(t)
        if not (m and is_chapter(t) and m.group(2)):
            continue
        if m.group(3) in "卷部集":
            seq.append((ln, None, t.strip(), False))
        elif m.group(3) == "章":
            seq.append((ln, cn2num(m.group(2)), t.strip(), bool(re.match(r"[0-9０-９]", m.group(2)))))
    return seq


def scan_chapters(lines):
    """章节编号跳跃/重复/倒退、空章节、标题缺空格、计数方式不统一。卷标题出现时允许章号重置。"""
    hits, prev, styles = [], None, Counter()
    heads = [(ln, CHAPTER_RE.match(t)) for ln, (t, _) in enumerate(lines, 1) if is_chapter(t)]
    for i, (ln, m) in enumerate(heads):
        text = lines[ln - 1][0].strip()
        if not m.group(2):
            continue
        kind, num = m.group(3), cn2num(m.group(2))
        if kind != "章":  # 只校验“章”的连续性；卷/部/集出现时允许章号重置，回/节常见于正文，不参与编号校验
            if kind in "卷部集":
                prev = None
            continue
        styles["阿拉伯数字" if re.match(r"[0-9０-９]", m.group(2)) else "中文数字"] += 1
        if num is None:
            hits.append((ln, "章节结构", "章节号无法解析", text[:40], ""))
        elif prev is not None:
            if num == prev[1]:
                hits.append((ln, "章节结构", f"章节号重复(同L{prev[0]})", text[:40], ""))
            elif num != prev[1] + 1:
                hits.append((ln, "章节结构", f"章节号{'跳跃' if num > prev[1] else '倒退'}({prev[1]}→{num})", text[:40], ""))
        if num is not None:
            prev = (ln, num)
        if m.group(4) and not re.match(r"[ 　:：]", m.group(4)):
            hits.append((ln, "章节结构", "章节号与标题间缺空格", text[:40], ""))
        nxt = heads[i + 1][0] if i + 1 < len(heads) else len(lines) + 1
        body = sum(1 for t, _ in lines[ln:nxt - 1] if not is_blank(t))
        if body == 0:
            hits.append((ln, "章节结构", "空章节(标题后无正文)", text[:40], ""))
    for ln, (t, _) in enumerate(lines, 1):  # 形似章节标题但格式错误（章号错字、“章”写成“间/张”、章号与“章”之间有空格）
        if not is_chapter(t) and len(t.strip()) <= 40 and MALFORMED_HEAD_RE.match(t) and t.strip()[-1] not in "。！？!?，,":
            hits.append((ln, "章节结构", "疑似章节标题格式错误", t.strip()[:40], "如“第一千四面一十九章”“第一千四百五十五间”“第一千一百零七 章”"))
    if len(styles) > 1:
        minor = min(styles, key=styles.get)
        for ln, m in heads:
            if m.group(2) and m.group(3) == "章":
                is_ar = bool(re.match(r"[0-9０-９]", m.group(2)))
                if (minor == "阿拉伯数字") == is_ar:
                    hits.append((ln, "章节结构", f"计数方式不统一(少数派:{minor})", lines[ln - 1][0].strip()[:40], ""))
    return hits


# ---------------------------------------------------------------- 重复段落
def scan_duplicates(lines, min_len=20):
    """非空、长度≥min_len 的段落重复出现时，登记第 2 次及以后的出现位置。"""
    seen = {}
    for ln, (t, _) in enumerate(lines, 1):
        key = t.strip(" 　\t")
        if len(key) >= min_len and not is_chapter(t):
            seen.setdefault(key, []).append(ln)
    hits = []
    for key, lns in seen.items():  # 同一段落只在第 2 次出现处登记一条，其余出现位置写进备注，避免候选爆炸
        if len(lns) > 1:
            more = "、".join(f"L{n}" for n in lns[2:12]) + ("…" if len(lns) > 12 else "")
            hits.append((lns[1], "重复段落", f"与L{lns[0]}完全相同(全文共{len(lns)}次)", key[:30] + "…",
                         f"其余出现位置：{more}" if more else ""))
    return hits


# ---------------------------------------------------------------- 通用机械规则
PUNCT_END = set("。！？…—～”’）】》」』!?~.)")
TRAIL_JUNK_RE = re.compile(r"(?<=[。！？…”’])([、，]|[^\s　。！？…”’、，—～）)】》」』“‘（(《【「『\"'])$")  # 收尾标点后多出的一个杂字或顿号


def scan_dict(lines, entries, typ="词库命中", exclude=None):
    """词库匹配：entries 为 [(错误用字, 正确用字, 说明)]。以数字结尾的词条（如“老2”）要求后面不再接数字。
    exclude：{错误用字: 正则}，命中位置前后各 3 字的窗口内匹配到该正则时视为误命中，不登记。"""
    hits, exclude = [], {k: re.compile(v) for k, v in (exclude or {}).items()}
    pats = [(re.compile(re.escape(w) + (r"(?![0-9])" if w[-1].isdigit() else "")), w, r, d) for w, r, d in entries if w]
    for ln, (t, _) in enumerate(lines, 1):
        for pat, w, r, d in pats:
            for m in pat.finditer(t):
                if w in exclude and exclude[w].search(t[max(0, m.start() - 3):m.end() + 3]):
                    continue
                hits.append((ln, typ, f"命中「{w}」(词库建议「{r}」)", snip(t, m.start(), m.end()), d))
    return hits


REDUP_CHARS = "的了是在我你他她它和就也都把被着过吗呢吧这那有不会能得地"  # 不含“个人一说啊”：个个/人人/一一/说说/啊啊多为正常叠词
REDUP_OK = re.compile(r"了了(?=[，。！？”之事])|不了了|(?<=[目的手段])的的|的的确确|实实在在|在在(?=的)|神神在在|现在在|会会(?=[那这他她你我它长])|(?<=[塌喜动谢])地地|"
                      r"正在在|存在在|就就是|是是非非|有有无无|地地道道|过过(?=日子|瘾)|吧吧(?=嗒)|这这|那那|我我|你你|他他|她她")


def scan_redup(lines):
    """叠字嫌疑：常用虚词/代词连续出现两次（如“的的”“了了”）；正常叠词（“卿卿我我”等）由阶段B 判定。"""
    pat = re.compile(f"([{REDUP_CHARS}])\\1")
    hits = []
    for ln, (t, _) in enumerate(lines, 1):
        ok = [m.span() for m in REDUP_OK.finditer(t)]
        for m in pat.finditer(t):
            if any(a <= m.start() < e for a, e in ok):
                continue
            hits.append((ln, "叠字嫌疑", f"「{m.group()}」", snip(t, m.start(), m.end()), "教程：叠字须人工判断，禁止批量"))
    return hits


def scan_ocr_chars(lines):
    """OCR 字符误识：中文句中夹 1/7（疑为！/？）、0/O/o（疑为○/〇/。）、l/I（疑为丨/1）。"""
    pats = [
        (re.compile(f"(?<=[{CJK}])[17](?=[”’」』]|$|[{CJK}]{{0,1}}[“‘])"), "疑1→！或7→？"),
        (re.compile(f"(?<=[{CJK}])[0Oo](?=[{CJK}])"), "疑0/O→〇或。"),
        (re.compile(f"(?<=[{CJK}])[lI|](?=[{CJK}])"), "疑l/I/|→丨或1"),
    ]
    hits = []
    for ln, (t, _) in enumerate(lines, 1):
        if is_chapter(t):
            continue  # 章节标题里的数字是章号
        for p, mark in pats:
            for m in p.finditer(t):
                hits.append((ln, "OCR误识嫌疑", mark, snip(t, m.start()), ""))
    return hits
