# -*- coding: utf-8 -*-
"""题库纯函数：选项解析、文字/SortOrder 互转、判断同义、答案判空。"""

import json
import re

BLANK_JOINER = "＃"
OPTION_JOINER = "|"

_OPTION_META_RE = re.compile(r"知识点\s*[:：].*$", re.S)


def answer_blank(value):
    """答案是否等于没作答（含填空的 ["", ""] 形态）。"""
    ans = str(value if value is not None else "").strip()
    if not ans:
        return True
    if ans.startswith("["):
        try:
            parsed = json.loads(ans)
        except Exception:
            return False
        if isinstance(parsed, list):
            return not any(str(x).strip() for x in parsed)
    return False


def normalize_title(title):
    if not title:
        return ""
    s = re.sub(r"<[^>]+>", "", str(title))
    s = re.sub(r"&[a-zA-Z]+;", "", s)
    s = re.sub(r"&#\d+;", "", s)
    s = re.sub(r"\s+", "", s).replace("\u3000", "")
    return s.lower()


def _clean_content(text):
    if text is None:
        return ""
    s = re.sub(r"<[^>]+>", "", str(text))
    s = re.sub(r"&nbsp;", " ", s)
    return s.strip()


def _strip_option_meta(text):
    if text is None:
        return ""
    s = str(text)
    cut = _OPTION_META_RE.sub("", s).strip()
    return cut if cut else s.strip()


def parse_options(q):
    """dataJson/optionSort -> [{SortOrder, Content, IsAnswer?}]。"""
    dj = q.get("dataJson") or q.get("optionSort")
    if isinstance(dj, str):
        try:
            dj = json.loads(dj)
        except Exception:
            return []
    if not isinstance(dj, list):
        return []
    out = []
    for opt in dj:
        if isinstance(opt, dict) and opt.get("Content") is not None:
            opt = dict(opt, Content=_strip_option_meta(opt.get("Content")))
        out.append(opt)
    return out


def translate_answer_text(type_id, raw_answer, options):
    """原始答案 -> 文字化答案；无法可靠翻译返回空串。"""
    raw = str(raw_answer if raw_answer is not None else "").strip()
    if not raw:
        return ""
    tid = str(type_id or "")

    if tid == "7":
        return ""

    if tid in ("1", "2"):
        so_map = {}
        for opt in options or []:
            if not isinstance(opt, dict):
                continue
            so = str(opt.get("SortOrder", "")).strip()
            content = _clean_content(opt.get("Content", ""))
            if so and content:
                so_map[so] = content
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            return ""
        texts = []
        for p in parts:
            t = so_map.get(p)
            if t is None:
                if not texts and raw in set(so_map.values()):
                    return raw
                return ""
            texts.append(t)
        return OPTION_JOINER.join(texts)

    if tid == "3":
        for opt in options or []:
            if not isinstance(opt, dict):
                continue
            so = str(opt.get("SortOrder", "")).strip()
            content = _clean_content(opt.get("Content", ""))
            if so and so == raw:
                if any(k in content for k in ("正确", "对", "√", "T", "true")):
                    return "正确"
                if any(k in content for k in ("错误", "错", "×", "F", "false")):
                    return "错误"
                return ""
        v = raw.lower()
        if raw in ("1", "true", "True", "正确", "对", "A"):
            return "正确"
        if raw in ("0", "false", "False", "错误", "错", "B"):
            return "错误"
        if v in ("true", "false"):
            return "正确" if v == "true" else "错误"
        return ""

    if tid == "4":
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                blanks = [_clean_content(x) for x in parsed]
                if not any(blanks):
                    return ""
                return BLANK_JOINER.join(blanks)
        except Exception:
            pass
        return _clean_content(raw)

    return _clean_content(raw)


def reverse_answer_to_sortorders(answer_text, target_options):
    """文字化答案 -> 目标卷 SortOrder 串；任一文字对不上返回空串。"""
    if not answer_text:
        return ""
    content_map = {}
    for opt in target_options or []:
        if not isinstance(opt, dict):
            continue
        so = str(opt.get("SortOrder", "")).strip()
        content = _clean_content(opt.get("Content", ""))
        if so and content:
            content_map[content] = so
    if not content_map:
        return ""
    wanted = [t for t in (_strip_option_meta(x)
                          for x in str(answer_text).split(OPTION_JOINER)) if t]
    orders = []
    for w in wanted:
        so = content_map.get(w)
        if so is None:
            w_norm = re.sub(r"\s+", "", w)
            for content, cand in content_map.items():
                if re.sub(r"\s+", "", content) == w_norm:
                    so = cand
                    break
        if so is None:
            return ""
        orders.append(so)
    return ",".join(orders)


JUDGE_SYNONYM_GROUPS = (
    ("正确", "对", "√", "true", "T", "是"),
    ("错误", "错", "×", "false", "F", "否"),
)


def judge_synonyms(text):
    t = str(text or "").strip()
    if not t:
        return []
    low = t.lower()
    for group in JUDGE_SYNONYM_GROUPS:
        if t in group or low in {g.lower() for g in group}:
            return [g for g in group if g != t and g.lower() != low]
    return []


def judge_to_submit_value(answer_text, target_options=None):
    """判断题文字 -> 本卷提交值；无选项时退到平台惯用 1/0。"""
    t = str(answer_text or "").strip()
    if not t:
        return ""
    opts = [o for o in (target_options or []) if isinstance(o, dict)]
    if opts:
        for cand in (t,) + tuple(judge_synonyms(t)):
            so = reverse_answer_to_sortorders(cand, opts)
            if so:
                return so
        return ""
    if t in JUDGE_SYNONYM_GROUPS[0] or t.lower() in {g.lower() for g in JUDGE_SYNONYM_GROUPS[0]}:
        return "1"
    if t in JUDGE_SYNONYM_GROUPS[1] or t.lower() in {g.lower() for g in JUDGE_SYNONYM_GROUPS[1]}:
        return "0"
    return ""
