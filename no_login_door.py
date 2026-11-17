# -*- coding: utf-8 -*-
"""免登录门答案源：按题 id 取平台标准答案，零凭据。

端点：GET zjy2.icve.com.cn/prod-api/spoc/noLogin/question/{qid}/0/0
返回：{code:200, data:{answer, dataJson, typeId, title, questionSubList?}}
data=null 视为平台确实没有该题，不重试、不回落。
"""

import json
import os
import re
import threading
import time

import requests

from bank_utils import (
    answer_blank,
    judge_to_submit_value,
    normalize_title,
    parse_options,
    reverse_answer_to_sortorders,
    _clean_content,
)
from utils import log

BASE_URL = os.getenv("ICVE_NOLOGIN_BASE_URL",
                     "https://zjy2.icve.com.cn/prod-api/spoc/noLogin/question")
TIMEOUT_S = float(os.getenv("ICVE_NOLOGIN_TIMEOUT_S", "10"))
FAIL_LIMIT = int(os.getenv("ICVE_NOLOGIN_FAIL_LIMIT", "3"))
COOLDOWN_S = float(os.getenv("ICVE_NOLOGIN_COOLDOWN_S", "600"))

ALLOW_TYPE_IDS = ("1", "2", "3", "4", "5", "6")
COMPOSITE_TYPE_IDS = ("8", "9", "10")

_session = requests.Session()
_session.trust_env = False
_lock = threading.Lock()
_streak = 0
_open_until = 0.0
_cache = {}
_MISS = object()

_BLANK_RE = re.compile(r"【\s*空\s*([一二三四五六七八九十\d]+)\s*】")


def enabled():
    return str(os.getenv("ICVE_NOLOGIN_DOOR", "1")).strip().lower() not in ("0", "false", "off")


def _fail():
    global _streak, _open_until
    _streak += 1
    if _streak >= FAIL_LIMIT:
        _open_until = time.time() + COOLDOWN_S
        _streak = 0
        log(f"[免登录门] 连续 {FAIL_LIMIT} 次不可用，冷却 {int(COOLDOWN_S // 60)} 分钟", "WARNING")


def fetch_question(question_id):
    """返回 (data|None, verdict)，verdict ∈ ok/miss/net/open。"""
    global _streak, _open_until
    if time.time() < _open_until:
        return None, "open"
    try:
        resp = _session.get(
            f"{BASE_URL}/{question_id}/0/0",
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=TIMEOUT_S,
        )
    except Exception:
        _fail()
        return None, "net"
    if resp.status_code != 200:
        _fail()
        return None, "net"
    try:
        j = resp.json() or {}
    except Exception:
        _fail()
        return None, "net"
    if j.get("code") != 200:
        _fail()
        return None, "net"
    _streak = 0
    _open_until = 0.0
    data = j.get("data")
    return (data, "ok") if isinstance(data, dict) else (None, "miss")


def _center_options(d):
    dj = (d or {}).get("dataJson")
    try:
        arr = json.loads(dj) if isinstance(dj, str) else (dj or [])
    except Exception:
        return []
    return [o for o in arr if isinstance(o, dict)] if isinstance(arr, list) else []


def _answer_orders(ans):
    return [x.strip() for x in re.split(r"[,，、]", str(ans or "")) if x.strip()]


def blank_count(title):
    return len(set(_BLANK_RE.findall(str(title or ""))))


def split_blanks(ans, title):
    parts = [x.strip() for x in re.split(r"[,，]", str(ans or "")) if x.strip()]
    if not parts:
        return []
    n = blank_count(title)
    if n:
        return parts if len(parts) == n else []
    return parts if len(parts) == 1 else []


def split_blanks_by_slots(ans, q):
    if blank_count(str((q or {}).get("title") or "")):
        return []
    slots = [o for o in (parse_options(q) or []) if isinstance(o, dict)]
    n = len(slots)
    if n < 2:
        return []
    parts = [x.strip() for x in re.split(r"[,，]", str(ans or "")) if x.strip()]
    return parts if len(parts) == n and all(parts) else []


def _door_slot_parts(d):
    opts = [o for o in _center_options(d) if isinstance(o, dict)]
    orders = []
    for o in opts:
        try:
            orders.append(int(str(o.get("SortOrder"))))
        except (TypeError, ValueError):
            orders = None
            break
    if orders is not None:
        opts = [o for _, o in sorted(zip(orders, opts), key=lambda p: p[0])]
    return [t for t in (_clean_content(o.get("Content")) for o in opts) if t]


def _content_of(d, order):
    for o in _center_options(d):
        if str(o.get("SortOrder")) == str(order):
            return _clean_content(o.get("Content"))
    return ""


def _adopt_image_options(q, d, ans):
    if str((q or {}).get("typeId") or "") not in ("1", "2"):
        return False
    own = parse_options(q) or []
    if not own or any(_clean_content(o.get("Content")) for o in own):
        return False
    parts = [x.strip() for x in re.split(r"[,，]", str(ans or "")) if x.strip()]
    if not parts or not all(x.isdigit() for x in parts):
        return False
    m_opts = _center_options(d)
    if not m_opts or not all(str(o.get("Content") or "").strip() for o in m_opts):
        return False
    if len(m_opts) != len(own):
        return False
    marked = sorted(str(o.get("SortOrder")) for o in m_opts
                    if str(o.get("IsAnswer")).lower() == "true")
    got = sorted(parts)
    if not marked or marked != got:
        return False
    own_orders = {str(o.get("SortOrder")) for o in own}
    if not set(got) <= own_orders:
        return False
    q["answer"] = ",".join(got)
    q["dataJson"] = json.dumps(m_opts, ensure_ascii=False)
    return True


def adopt_from_door(q, d):
    """把门返回的题目写入 q['answer']（本卷 SortOrder 形态）。返回是否采信。"""
    tid = str(q.get("typeId") or "")
    ans = str(d.get("answer") or "").strip()

    if tid in ("1", "2"):
        if _adopt_image_options(q, d, ans):
            return True
        opts = parse_options(q) or []
        if ans:
            orders_src = _answer_orders(ans)
        else:
            orders_src = [str(o.get("SortOrder")) for o in _center_options(d)
                          if str(o.get("IsAnswer")).lower() == "true"]
        if not orders_src:
            return False
        orders = []
        for o in orders_src:
            text = _content_of(d, o)
            if not text:
                return False
            so = reverse_answer_to_sortorders(text, opts)
            if not so:
                return False
            orders.extend(x for x in so.split(",") if x)
        uniq = list(dict.fromkeys(orders))
        if not uniq:
            return False
        q["answer"] = ",".join(uniq)
        return True

    if tid == "3":
        opts = parse_options(q) or []
        if not opts and ans in ("0", "1"):
            q["answer"] = ans
            return True
        if ans:
            texts = [_content_of(d, o) for o in _answer_orders(ans)]
        else:
            texts = [_content_of(d, str(o.get("SortOrder"))) for o in _center_options(d)
                     if str(o.get("IsAnswer")).lower() == "true"]
        text = next((t for t in texts if t), "")
        so = judge_to_submit_value(text or ans, opts)
        if not so:
            return False
        q["answer"] = so
        return True

    if tid in ("4", "5"):
        slots = [o for o in (parse_options(q) or []) if isinstance(o, dict)]
        n_sheet = len(slots)
        n_title = blank_count(str(q.get("title") or ""))
        segs_a = [x.strip() for x in re.split(r"[,，]", ans) if x.strip()] if ans else []
        segs_p = _door_slot_parts(d) if tid == "4" else []

        if n_title and len(segs_a) == n_title:
            q["answer"] = json.dumps(segs_a, ensure_ascii=False)
            return True
        if not n_title and len(segs_a) == 1:
            q["answer"] = json.dumps(segs_a, ensure_ascii=False)
            return True
        if not n_title and n_sheet >= 2 and len(segs_a) == n_sheet:
            q["answer"] = json.dumps(segs_a, ensure_ascii=False)
            return True
        if tid == "4" and not ans and n_sheet and len(segs_p) == n_sheet:
            q["answer"] = json.dumps(segs_p, ensure_ascii=False)
            return True
        if tid == "5" and n_title <= 1:
            whole = _clean_content(ans)
            if whole:
                q["answer"] = json.dumps([whole], ensure_ascii=False)
                return True
        if tid == "4" and max(n_title, n_sheet) <= 1:
            whole = _clean_content(ans) if ans else ""
            if not whole and segs_p:
                whole = segs_p[0] if len(segs_p) == 1 else ",".join(segs_p)
            if whole:
                q["answer"] = json.dumps([whole], ensure_ascii=False)
                return True
        if tid == "4" and n_title >= 2 and len(segs_p) == n_title:
            q["answer"] = json.dumps(segs_p, ensure_ascii=False)
            return True
        if tid == "4" and not n_title and n_sheet >= 2 and ans and len(segs_p) == n_sheet:
            q["answer"] = json.dumps(segs_p, ensure_ascii=False)
            return True
        return False

    if tid == "6":
        text = _clean_content(ans)
        if not text:
            return False
        q["answer"] = text
        return True

    return False


def _fetch_cached(qid):
    with _lock:
        if qid in _cache:
            hit = _cache[qid]
            return None if hit is _MISS else hit, ("miss" if hit is _MISS else "ok")
    data, verdict = fetch_question(qid)
    if verdict == "ok":
        with _lock:
            _cache[qid] = data
    elif verdict == "miss":
        with _lock:
            _cache[qid] = _MISS
    return data, verdict


def merge_door_answers(questions, nickname=""):
    """给三键全空的题补答案；已有答案不覆盖。返回补题数。"""
    if not enabled():
        return 0
    targets = []
    for q in questions or []:
        if not isinstance(q, dict):
            continue
        if str(q.get("typeId") or "") not in ALLOW_TYPE_IDS:
            continue
        if any(not answer_blank(q.get(k)) for k in ("answer", "correctAnswer", "rightAnswer")):
            continue
        qid = str(q.get("questionId") or q.get("id") or "").strip()
        if qid:
            targets.append((q, qid))
    if not targets:
        return 0

    filled = hit = miss = bad = 0
    for q, qid in targets:
        data, verdict = _fetch_cached(qid)
        if verdict == "ok":
            hit += 1
            if adopt_from_door(q, data):
                q["_door_options"] = _center_options(data)
                filled += 1
        elif verdict == "miss":
            miss += 1
        else:
            bad += 1
    log(f"[免登录门] 采信 {filled} 题（命中 {hit}、无此题 {miss}、不可用 {bad}）"
        + (f" | {nickname}" if nickname else ""), "INFO")
    return filled


def _composite_sheet_subs(q):
    subs = q.get("taskExamSubProblemRecordPaperVos") or q.get("questionSubList") or []
    return [s for s in subs if isinstance(s, dict)] if isinstance(subs, list) else []


def _composite_sub_sortorder(bank_sub, sheet_sub):
    tid = str(sheet_sub.get("typeId") or "")
    ans = str(bank_sub.get("answer") or "").strip()
    if not ans:
        return ""
    opts = parse_options(sheet_sub) or []
    if tid == "3":
        if not opts and ans in ("0", "1"):
            return ans
        text = next((t for t in (_content_of(bank_sub, o) for o in _answer_orders(ans)) if t), "")
        return judge_to_submit_value(text or ans, opts)
    orders = []
    for o in _answer_orders(ans):
        text = _content_of(bank_sub, o)
        if not text:
            return ""
        so = reverse_answer_to_sortorders(text, opts)
        if not so:
            return ""
        for x in so.split(","):
            if x and x not in orders:
                orders.append(x)
    return ",".join(orders)


def merge_door_composite(questions, nickname=""):
    """复合大题（8/9/10）父题取 questionSubList，写卷面子行 answer。"""
    if not enabled():
        return 0
    targets = []
    for q in questions or []:
        if not isinstance(q, dict) or str(q.get("typeId") or "") not in COMPOSITE_TYPE_IDS:
            continue
        fillable = [s for s in _composite_sheet_subs(q)
                    if str(s.get("typeId") or "") in ("1", "2", "3")
                    and answer_blank(s.get("answer"))]
        if not fillable:
            continue
        qid = str(q.get("questionId") or "").strip()
        if qid:
            targets.append((q, qid))
    if not targets:
        return 0

    filled = 0
    for q, qid in targets:
        data, verdict = _fetch_cached(qid)
        if verdict != "ok" or not isinstance(data, dict):
            continue
        bank_subs = data.get("questionSubList")
        if not isinstance(bank_subs, list) or not bank_subs:
            continue
        for j, ss in enumerate(_composite_sheet_subs(q)):
            t = str(ss.get("typeId") or "")
            if t not in ("1", "2", "3") or not answer_blank(ss.get("answer")):
                continue
            bs = bank_subs[j] if j < len(bank_subs) else None
            if not isinstance(bs, dict) or str(bs.get("typeId") or "") != t:
                continue
            bt, st = normalize_title(bs.get("title")), normalize_title(ss.get("title"))
            if bt and st and bt != st:
                continue
            so = _composite_sub_sortorder(bs, ss)
            if so:
                ss["answer"] = so
                filled += 1
    if filled:
        log(f"[免登录门] 复合题子行补答案 {filled} 条" + (f" | {nickname}" if nickname else ""), "INFO")
    return filled
