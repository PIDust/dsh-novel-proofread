"""合并版扫描器：一遍扫描覆盖三个校对维度，只做机械定位，真伪与改法由阶段B 模型判定。

维度（--only 可选其一或多个，逗号分隔；默认全部）：
  basic  基本：广告/作者的话、屏蔽字、编码与符号、章节结构、重复段落、颜文字问号、缺失符号长句、的地得（可选）
  punct  标点与换行：段尾/引号内缺收尾标点、异常换行（硬回车、标点后单字、行首标点、空行异常）
  typo   错别字：词库（可增量追加）、高频词形近/同音变体、叠字、OCR 字符
三个维度共用的检测（成对符号、段尾杂字）只登记一次；同一位置被多条规则命中时由 dedup() 合并成一条。
"""
import logging
import re
from collections import Counter
from pathlib import Path

import pandas as pd

from common import (AUTHOR_NOTE_RE, CJK, PUNCT_END, TRAIL_JUNK_RE, author_note_spans, is_blank, is_chapter,
                    scan_chapters, scan_dict, scan_duplicates, scan_ocr_chars, scan_pairs, scan_redup, snip)
from typo_dict import BASE_TYPOS, CONFUSION_GROUPS, DICT_EXCLUDE

log = logging.getLogger("proofread")
MODE = "小说校对"
DIMENSIONS = ("basic", "punct", "typo")
RESULTS = ["需修改", "不处理", "疑似未修改", "成对符号异常", "异常换行"]
MODIFY_RESULTS = ["需修改"]  # 阶段C 自动修改
CONFIRM_RESULTS = ["疑似未修改", "成对符号异常", "异常换行"]  # 步骤7 在对话中请用户确认
MAX_CTX_RADIUS = 3

# ---------------------------------------------------------------- 基本维度的规则表
AD_PATTERNS = [  # 广告/水印（同一行同一类只登记一次）
    (r"(https?://|www\.)\S+|[A-Za-z0-9-]+\.(com|net|org|cc|cn|la|info|xyz|vip|me|tw)\b", "网址"),
    (r"[Qq]{2}群?[:：]?\s*\d{5,}|群号[:：]?\s*\d{5,}|微信公众号|关注公众号|加群", "QQ群/公众号"),
    (r"最新章节|请访问|手机阅读|手机用户请|本章未完|下载本书|txt下载|全文阅读|免费阅读|无弹窗|首发|"
     r"笔趣阁|顶点小说|八一中文|飘天|起点中文网|纵横中文网|17K|百度搜索|天才一秒记住|一秒记住|记住本站|本站地址", "推广语/站点名"),
    (r"求(月票|推荐票|订阅|收藏|打赏)|更新时间[:：]|本章字数[:：]", "求票/更新信息"),
]
KAOMOJI_PARTS = "ω∀▽Д´`°・･ﾟ≧≦＾^ε∇ヮдﾉノヽ＼／\\/≖ಠ_﹏□◇△▲●○◕‿｡︶^"
KAOMOJI_RE = re.compile(f"[（(][^{CJK}（）()]{{0,4}}[?？][^{CJK}（）()]{{0,4}}[)）]|[{re.escape(KAOMOJI_PARTS)}][?？]|[?？][{re.escape(KAOMOJI_PARTS)}]")
SYMBOL_PATTERNS = [
    (r"�|锟斤拷|烫烫|屯屯", "乱码字符"),
    (r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]", "不可见字符"),
    (r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "控制字符"),
    (r"&(nbsp|amp|lt|gt|quot|#\d+);|<\s*/?\s*(p|br|div|span)[^>]*>", "HTML残留"),
    (r"[０-９Ａ-Ｚａ-ｚ]+", "全角字母数字（规范为半角）"),
    (f"(?<=[{CJK}])[,;:!?](?=[{CJK}“”‘’]|$)|(?<=[{CJK}])\\.(?=[{CJK}]|$)", "中文间半角标点（规范为全角）"),
    (r"\.{3,}|。{2,}|(?<!…)…(?!…)|…{3,}|·{3,}", "省略号或标点重复"),  # “。。”既像省略号又像重复标点，合并为一类
    (r"(?<![—])—(?![—])|-{2,}|—{3,}", "破折号不规范（规范为——）"),
    (f"(?<=[{CJK}])[ 　]+(?=[{CJK}])", "中文间空格"),
    (r"[，、；：！？]{2,}|[，、；：][。！？]|[。！？][，、；：]", "省略号或标点重复"),
]
SHIELD_RE = re.compile(f"(?<=[{CJK}，。！？“”])\\*+|\\*+(?=[{CJK}])|□+|■+|×{{2,}}|(?<=[{CJK}])(XX+|xx+)")
LATIN_RE = re.compile(f"(?<![A-Za-z])((?<=[{CJK}])[A-Za-z]{{1,8}}|[A-Za-z]{{1,8}}(?=[{CJK}]))(?![A-Za-z])")
# 拼音音节（阡陌居整理的常用汉字拼音表压缩版）：小写串能完整拆成音节才视为拼音屏蔽嫌疑
PINYIN_RE = re.compile(r"(?:(?:[bpmfdtnlgkhjqxrzcsyw]|zh|ch|sh)?(?:iang|iong|uang|ang|eng|ing|ong|ian|iao|uai|uan|ai|ei|ao|ou|an|en|er|in|un|ia|ie|iu|ua|uo|ue|ui|ve|a|o|e|i|u|v))+")
LATIN_OK = {"PS", "VIP", "QQ", "OK", "CEO", "DNA", "GPS", "PK", "BOSS", "Boss", "APP", "App", "KTV", "VS", "TV", "ID", "IQ", "EQ",
            "NPC", "BUG", "Bug", "UFO", "MV", "CD", "DVD", "PPT", "WIFI", "WiFi", "AI", "U盘",
            "AK", "BP", "GB", "CT", "XX", "TNT", "VCD", "FBI", "CIA", "cina", "kevin"}  # 实测确认为正常用法的缩写/外文
LONG_RE = re.compile(f"[{CJK}A-Za-z0-9]{{60,}}")  # 连续 60 字无标点
DE_RULES = [  # “的地得”误用（默认关闭）
    (re.compile(f"([{CJK}])\\1的(?=[走跑说笑看道点叫喊问答想望跳哭])"), "叠词+的+动词，疑应为“地”"),
    (re.compile(r"(?<=[走跑说笑看哭唱写跳飞打吃喝睡站坐])的(?=很|非常|太|十分|极|飞快|要命)"), "动词+的+程度，疑应为“得”"),
]

# ---------------------------------------------------------------- 标点维度
QUOTE_END = set("。！？…—～!?~")
QUOTE_RE = {"双引号": re.compile(r"“([^“”]*)”"), "单引号": re.compile(r"‘([^‘’]*)’")}
SOFT_END = set("，、；：,;:")  # 段尾以这些结尾 = 教程定义的“硬回车”

# ---------------------------------------------------------------- 错别字维度
DICT_NAME = "校对词库.tsv"
DICT_COLS = ["错误用字", "正确用字", "说明"]
# 高频虚词/常用字组成的混淆组在 n-gram 统计里会跨词边界产生大量正常组合（如“一章/一张”“有那/有哪”），不参与变体检测
VARIANT_SKIP = {"在再", "那哪", "大太犬", "做作", "人入", "土士", "千干于", "日曰", "末未", "己已巳", "即既", "须需",
                "象像", "带戴", "坐座", "做座", "份分", "历厉", "历励", "天添", "清青晴情", "月悦越", "雪血", "宇羽雨禹"}
STOP_CHARS = set("的了是在着过地得和与也都就又还把被给向对从一不我你他她它这那有")
CONFUSE = {}  # 字 → 所有与之混淆的字（一个字可出现在多个组里，取并集）
for _g in (g for g in CONFUSION_GROUPS if g not in VARIANT_SKIP):
    for _c in _g:
        CONFUSE[_c] = CONFUSE.get(_c, "") + _g


def add_args(p):
    p.add_argument("--only", default="basic,punct,typo", help="只检查指定维度，逗号分隔：basic,punct,typo（默认全部）")
    p.add_argument("--check-de", action="store_true", help="[basic] 开启“的地得”误用检测（默认关闭）")
    p.add_argument("--no-redup", action="store_true", help="[typo] 关闭叠字检测")
    p.add_argument("--short-quote", type=int, default=2, help="[punct] 独立对话引号内容不超过该字数、且后面还有正文时不登记（默认 2）")
    p.add_argument("--min-freq", type=int, default=5, help="[typo] 高频词变体：主写法至少出现次数（默认 5）")
    p.add_argument("--ratio", type=float, default=0.1, help="[typo] 变体次数 ≤ 主写法×该比例才登记（默认 0.1）")


def dims(a):
    d = {x.strip() for x in getattr(a, "only", "basic,punct,typo").split(",") if x.strip()}
    bad = d - set(DIMENSIONS)
    if bad or not d:
        raise ValueError(f"--only 只能取 {DIMENSIONS} 中的值，收到：{sorted(bad) or '空'}")
    return d


# ---------------------------------------------------------------- 词库
def dict_path(src):
    return Path(src).with_name(DICT_NAME)


def load_dict(src):
    df = pd.read_csv(dict_path(src), sep="\t", dtype=str, keep_default_na=False, encoding="utf-8")
    if list(df.columns[:3]) != DICT_COLS:
        raise ValueError(f"{dict_path(src)} 表头应为 {DICT_COLS}，实际 {list(df.columns)}")
    bad = df[df["错误用字"].str.strip() == ""]
    if len(bad):
        log.warning("词库中有 %d 行“错误用字”为空，已忽略", len(bad))
    return df[df["错误用字"].str.strip() != ""]


def prepare(path, lines, a):
    d = dims(a)
    print(f"检查维度：{','.join(x for x in DIMENSIONS if x in d)}")
    if "typo" not in d:
        return
    dp = dict_path(path)
    if dp.exists():
        print(f"词库检测：已存在 {dp}，词条 {len(load_dict(path))} 条（将直接加载）")
        return
    with open(dp, "x", encoding="utf-8", newline="") as f:  # 只在不存在时创建
        pd.DataFrame(BASE_TYPOS, columns=DICT_COLS).to_csv(f, sep="\t", index=False)
    print(f"词库检测：不存在，已生成基础词库 {dp}（{len(BASE_TYPOS)} 条）")


# ---------------------------------------------------------------- 主入口
def scan(lines, path, a):
    d = dims(a)
    hits, state = scan_pairs(lines)  # 三个维度共用：成对符号只登记一次
    notes = author_note_spans(lines)
    for ln, marks in state.items():  # 跨段引语：只有起始段结尾无标点时才可能是错误断行（如“他们向黄河/逃窜”）
        for mk in marks:
            m = re.search(r"跨段闭合\(始于L(\d+)\)", mk)
            if m:
                start = lines[int(m.group(1)) - 1][0].rstrip(" 　\t")
                if start and start[-1] not in PUNCT_END:
                    hits.append((ln, "成对符号", mk + "，起始段结尾无标点", snip(start, len(start) - 1, pad=20),
                                 "疑错误断行；网文/出版惯例的跨段引语属正常写法"))
    if d & {"basic", "punct"}:
        hits += scan_trail_junk(lines, notes)
    if "basic" in d:
        hits += scan_basic(lines, notes, a)
    if "punct" in d:
        hits += scan_punct(lines, notes, state, a)
    if "typo" in d:
        if not dict_path(path).exists():
            raise FileNotFoundError(f"未找到 {dict_path(path)}，请先执行 prepare")
        entries = list(load_dict(path)[DICT_COLS].itertuples(index=False, name=None))
        hits += scan_dict(lines, entries, "错别字嫌疑", DICT_EXCLUDE) + scan_ocr_chars(lines)
        if not getattr(a, "no_redup", False):
            hits += scan_redup(lines)
        hits += scan_variants(lines, a.min_freq, a.ratio)
    return dedup(hits, lines)


WORD_RE = re.compile(r"「([^」]+)」")


def dedup(hits, lines):
    """同一行里同一个词被多条规则命中时只保留一条，其余规则写进结构标记。
    例：“时侯”同时命中词库和高频词变体，“的的”同时命中叠字和词库。命中词取结构标记里第一个「」中的内容。
    没有「」命中词的候选（符号、标点、结构类）不参与合并，避免把同一行不同位置的问题误合并。"""
    keep, seen = [], {}
    for h in hits:
        ln, typ, mark, sn, note = h
        m = WORD_RE.search(mark)
        if not m or typ in ("成对符号", "章节结构", "重复段落"):
            keep.append(h)
            continue
        key = (ln, m.group(1), sn)
        if key in seen:
            i = seen[key]
            k = keep[i]
            keep[i] = (k[0], k[1], f"{k[2]}；另命中[{typ}]{mark}", k[3], k[4])
            continue
        seen[key] = len(keep)
        keep.append(h)
    if len(keep) < len(hits):
        log.info("去重：同一位置的重复候选合并 %d 条", len(hits) - len(keep))
    return keep


def scan_basic(lines, notes, a):
    hits = scan_chapters(lines) + scan_duplicates(lines)
    for ln, (t, _) in enumerate(lines, 1):
        chap = is_chapter(t)
        ads = [(mark, m) for pat, mark in AD_PATTERNS for m in [re.search(pat, t, re.I if mark == "网址" else 0)] if m]
        if ads:  # 同一行命中多类广告规则时合并成一条（如网址+推广语）
            m0 = min((m for _, m in ads), key=lambda x: x.start())
            hits.append((ln, "广告残留嫌疑", "+".join(k for k, _ in ads), snip(t, m0.start(), max(m.end() for _, m in ads)),
                         "整行广告用新片段[[删除整行]]；嵌在正文中只删广告片段"))
        for a0, b0 in notes.get(ln, []):
            whole = t[:a0].strip(" 　") == "" and t[b0:].strip(" 　") == ""
            hits.append((ln, "广告残留嫌疑", "作者的话" + ("(整行)" if whole else "(行内片段)"), snip(t, a0, b0, 4)[:60],
                         "整行用[[删除整行]]；行内片段旧片段写该段、新片段留空"))
        for m in KAOMOJI_RE.finditer(t):
            hits.append((ln, "颜文字问号", "颜文字内含问号", snip(t, m.start(), m.end(), 8), "按 a/b/c/d 四类判定"))
        for pat, mark in SYMBOL_PATTERNS:
            if chap and mark == "中文间空格":
                continue  # 章节号与标题之间的空格是规范写法
            for m in re.finditer(pat, t):
                hits.append((ln, "编码与符号异常", mark, snip(t, m.start(), m.end()), repr(m.group())[:20]))
        if not chap:
            for m in SHIELD_RE.finditer(t):
                hits.append((ln, "屏蔽字嫌疑", f"「{m.group()}」×{len(m.group())}", snip(t, m.start(), m.end()), "星号数通常等于被屏蔽字数，可能有例外"))
            for m in LATIN_RE.finditer(t):  # 只登记小写拼音串（ji、shu、落shen）和 2 个大写字母的缩写屏蔽（QJ）
                g = m.group()
                if g in LATIN_OK or g.lower() in ("www", "http", "https", "com", "net"):
                    continue
                if g.islower() and PINYIN_RE.fullmatch(g):
                    hits.append((ln, "屏蔽字嫌疑", f"中文间拼音「{g}」", snip(t, m.start(), m.end()), "可能是拼音屏蔽，也可能是外文/代号"))
                elif g.isupper() and len(g) == 2:
                    hits.append((ln, "屏蔽字嫌疑", f"中文间字母缩写「{g}」", snip(t, m.start(), m.end()), "可能是缩写屏蔽，也可能是正常缩写"))
        for m in LONG_RE.finditer(t):
            hits.append((ln, "缺失符号嫌疑", f"连续{len(m.group())}字无标点", snip(t, m.start(), m.start() + 20), ""))
        if getattr(a, "check_de", False):
            for pat, mark in DE_RULES:
                for m in pat.finditer(t):
                    hits.append((ln, "的地得误用嫌疑", mark, snip(t, m.start(), m.end()), ""))
    return hits


def scan_trail_junk(lines, notes):
    """段尾杂字或断行（basic 与 punct 共用，只登记一次）：收尾标点后又多出一个字符，如“。寻”“。”、”“试探。我”。"""
    hits = []
    for ln, (raw, _) in enumerate(lines, 1):
        for a0, b0 in sorted(notes.get(ln, []), reverse=True):
            raw = raw[:a0] + raw[b0:]
        t = raw.strip(" 　\t")
        mj = TRAIL_JUNK_RE.search(t) if len(t) > 2 and not is_chapter(raw) else None
        if mj:
            nln, ntext, blanks = next_text(lines, ln)
            hits.append((ln, "段尾杂字或断行", f"收尾标点后多出【{mj.group()}】；下一段L{nln}首字:{ntext[:1] or '无'}；中间空行:{blanks}",
                         snip(t, len(t) - 1, pad=16), "与下一行拼接通顺→异常换行；否则是杂字→需修改（旧片段写标点+杂字，新片段写标点）"))
    return hits


def next_text(lines, ln):
    """返回 ln 之后第一个非空行 (行号, 内容, 中间空行数)。"""
    k = ln
    while k < len(lines) and is_blank(lines[k][0]):
        k += 1
    return (k + 1, lines[k][0].strip(" 　\t"), k - ln) if k < len(lines) else (None, "", k - ln)


def scan_punct(lines, notes, state, a):
    hits, gaps, run = [], Counter(), 0
    for t, _ in lines:  # 全文段间空行数的众数，偏离的视为空行异常嫌疑
        if is_blank(t):
            run += 1
        else:
            gaps[run] += 1
            run = 0
    usual = gaps.most_common(1)[0][0] if gaps else 0
    for ln, (raw, _) in enumerate(lines, 1):
        for a0, b0 in sorted(notes.get(ln, []), reverse=True):  # 作者的话不属于正文，先剔除再判断段尾
            raw = raw[:a0] + raw[b0:]
        t = raw.strip(" 　\t")
        if not t or is_chapter(raw):
            continue
        nln, ntext, blanks = next_text(lines, ln)
        qstate = "、".join(sorted(state.get(ln, []))) or "引号正常"
        hint = f"引号状态:{qstate}；下一段L{nln}首字:{ntext[:1] or '无'}；中间空行:{blanks}"
        # 0) 收尾标点后多出一个字符（“。寻”“”、”）：共用检测，只登记一次。可能是杂字，也可能是断行
        if len(t) > 2 and TRAIL_JUNK_RE.search(t):
            continue  # 收尾标点后多出一个字符：由共用的段尾杂字检测登记（scan_trail_junk），这里不再按缺标点处理
        # 1) 段落结尾无标点
        if t[-1] not in PUNCT_END:
            typ = "异常换行嫌疑(段尾为逗号顿号等)" if t[-1] in SOFT_END else "段落结尾无标点"
            hits.append((ln, typ, hint, snip(t, len(t) - 1, pad=16), "诗词歌谣/地名时间标注/列表可能是正常写法"))
        # 2) 独立对话引号内结尾无标点（位于段首或紧跟冒号；嵌在句中的引语末尾本就不加标点）
        for name, pat in QUOTE_RE.items():
            for m in pat.finditer(t):
                inner = m.group(1)
                if not inner or inner[-1] in QUOTE_END:
                    continue
                said = m.start() == 0 or bool(re.search(r"[：:]\s*$", t[:m.start()]))
                if not said or len(inner) <= a.short_quote and m.end() < len(t):
                    continue
                mark = f"{name}已闭合，引号内末字后无收尾标点；{'段首对话' if m.start() == 0 else '冒号后对话'}；{qstate}"
                hits.append((ln, f"{name}内结尾无标点", mark, snip(t, m.end() - 2, m.end(), pad=14),
                             "补标点时旧片段写“末字+右引号”，新片段写“末字+标点+右引号”"))
        # 3) 空行异常（章节标题前后、书首信息区的空行属排版）
        if blanks > usual and blanks >= 2 and nln and not is_chapter(lines[nln - 1][0]) and ln > 10:
            hits.append((ln, "异常换行嫌疑(空行异常)", f"段后空行{blanks}个(全文常见{usual}个)", snip(t, len(t) - 1, pad=16), ""))
    for ln, (raw, _) in enumerate(lines, 1):  # 4) 行首为标点（错误断行造成）
        t = raw.strip(" 　\t")
        if t and t[0] in "，。、；：！？）》】”’,.;:!?)":
            hits.append((ln, "异常换行嫌疑(行首标点)", "行首为标点，疑似上一段被错误断开", snip(t, 0, pad=16), ""))
    return hits


def scan_variants(lines, min_freq, ratio):
    """高频 2~3 字词中某字换成形近/同音混淆字后低频出现 → 登记（覆盖专名不一致与形近误打）。"""
    body = [(ln, AUTHOR_NOTE_RE.sub("", t)) for ln, (t, _) in enumerate(lines, 1) if not is_chapter(t)]
    cnt = Counter()
    for _, t in body:
        for seg in re.findall(f"[{CJK}]+", t):
            for n in (2, 3):
                cnt.update(seg[i:i + n] for i in range(len(seg) - n + 1))
    flagged = {}
    for w, f in cnt.items():
        if f < min_freq:
            continue
        for i, ch in enumerate(w):
            if len(w) == 2 and w[1 - i] in STOP_CHARS:
                continue  # “枫的/风的”这类跨词边界的 2 字组合，另一个字是虚词时不可靠
            for alt in CONFUSE.get(ch, ""):
                if alt == ch:
                    continue
                v = w[:i] + alt + w[i + 1:]
                if 0 < cnt.get(v, 0) <= max(1, f * ratio) and cnt[v] < f:
                    flagged[v] = (w, f, cnt[v])
    for v in [v for v in flagged if len(v) == 2]:  # 去掉被更长变体覆盖的短变体
        if any(v in L for L in flagged if len(L) == 3):
            del flagged[v]
    hits = []
    if not flagged:
        return hits
    pat = re.compile("|".join(map(re.escape, sorted(flagged, key=len, reverse=True))))  # 合成一个正则，避免平方级耗时
    for ln, t in body:
        for m in pat.finditer(t):
            w, f, c = flagged[m.group()]
            hits.append((ln, "高频词变体", f"「{m.group()}」{c}次 vs 主写法「{w}」{f}次", snip(t, m.start(), m.end()),
                         "可能是专名不一致或形近/同音误打，也可能是不同的正常词"))
    return hits


# ---------------------------------------------------------------- 阶段C 后处理：词库增量追加
def core_pair(old, new):
    """去掉为保证唯一而带上的前后文，得到最小错字对；不足 2 字时补上下文（左侧是虚词时优先向右补）。"""
    old = old.strip().strip("“”‘’「」，。！？、：；")
    new = new.strip().strip("“”‘’「」，。！？、：；")
    p = 0
    while p < min(len(old), len(new)) and old[p] == new[p]:
        p += 1
    q = 0
    while q < min(len(old), len(new)) - p and old[-1 - q] == new[-1 - q]:
        q += 1
    a, b_old, b_new = p, len(old) - q, len(new) - q
    while min(b_old - a, b_new - a) < 2 and (a > 0 or q > 0):
        if a > 0 and (old[a - 1] not in STOP_CHARS or q == 0):
            a -= 1
        else:
            b_old, b_new, q = b_old + 1, b_new + 1, q - 1
    return old[a:b_old], new[a:b_new]


def post_apply(meta, done):
    """把判定为需修改、替换成功且标记“加入词库=是”的错字对追加进词库（不覆盖已有行）。未启用 typo 维度时跳过。"""
    dp = dict_path(meta["file"])
    marked = done[done["加入词库"] == "是"]
    if not dp.exists():
        if len(marked):
            log.warning("有 %d 条标记了加入词库，但词库不存在（未启用 typo 维度？），已跳过", len(marked))
        return
    old, new = set(load_dict(meta["file"])["错误用字"]), []
    for _, r in marked.iterrows():
        w, c = core_pair(r["旧片段"], r["新片段"])
        if not w or w in old or "\t" in w + c or "\n" in w + c:
            continue
        old.add(w)
        new.append(f"{w}\t{c}\t批次{meta['batch']}确认（{r['候选ID']}）\n")
    if new:
        with open(dp, "a", encoding="utf-8") as f:  # 追加模式，只增不改
            f.writelines(new)
    print(f"词库增量追加：{len(new)} 条 → {dp}")
