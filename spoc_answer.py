# -*- coding: utf-8 -*-
"""SPOC 答题交卷链路：卷型判定、作业 file/draft 双腿、考试交卷、无视设备锁。"""

import json
import os
import random
import re
import time
import uuid
from contextlib import contextmanager

from bank_utils import answer_blank, parse_options
from no_login_door import merge_door_composite
from utils import log

_ABSENT = object()

_SPOC_WEB_HEADERS = {
    "platform-type": None,
    "log-equipment": "3",
    "log-equipment-app-version": None,
    "log-equipment-model": None,
    "log-equipment-api-version": None,
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}


def _profile_off():
    return str(os.getenv("SPOC_PROFILE_OFF", "")).strip() == "1"


@contextmanager
def _web_session(client):
    hdr = client.session.headers
    saved = {k: (hdr[k] if k in hdr else _ABSENT) for k in _SPOC_WEB_HEADERS}
    try:
        for k, v in _SPOC_WEB_HEADERS.items():
            if v is None:
                hdr.pop(k, None)
            else:
                hdr[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is _ABSENT:
                hdr.pop(k, None)
            else:
                hdr[k] = v


def _web_post(client, path, body=None, timeout=30):
    with _web_session(client):
        return client.api_post(path, body or {}, timeout=timeout)


def _web_get(client, path, params=None, timeout=30):
    with _web_session(client):
        return client.api_get(path, params or {}, timeout=timeout)


def _paper_is_question_bank(data):
    pt = str((data or {}).get("typeId") or "").strip()
    sm = str((data or {}).get("questionSettingMethod") or "").strip()
    return pt == "1" and bool(sm) and sm != "3"


def paper_profile(client, exam_id, class_id, nickname, need_sign=True, sheet_paper_ids=None):
    if _profile_off():
        return None
    try:
        info = _web_get(client, "spoc/exam/info", {"id": exam_id, "classId": class_id}, timeout=8)
    except Exception:
        return None
    if not isinstance(info, dict) or info.get("code") != 200:
        return None
    data = info.get("data")
    if not isinstance(data, dict):
        return None
    paper_type = str(data.get("typeId") or "").strip()
    method = str(data.get("questionSettingMethod") or "").strip()
    if paper_type == "1" and method and method != "3":
        family = "question_bank"
    elif method == "3" or paper_type == "3":
        family = "document"
    elif paper_type == "2":
        family = "exam"
    else:
        return None
    prof = {"family": family, "task_id": "", "sign_id": "", "data": data}
    if family != "question_bank":
        return prof
    prof["task_id"] = str(data.get("taskId") or "").strip()
    if need_sign:
        try:
            face = _web_get(client, "spoc/exam/paper",
                            {"id": exam_id, "classId": class_id, "device": "1"}, timeout=8)
        except Exception:
            return None
        if not isinstance(face, dict) or not face.get("questions"):
            return None
        ids = {str(q.get("id") or "") for q in (face.get("questions") or []) if q.get("id")}
        want = {str(x) for x in (sheet_paper_ids or set()) if x}
        if want and ids and want != ids:
            log(f"[{nickname}] SPOC作业profile选腿中止：卷面行不一致", "WARNING")
            return None
        prof["sign_id"] = str(face.get("signId") or "")
    return prof


def homework_exam_family(client, nickname, exam_id, class_id, sheet_paper_ids):
    try:
        info = _web_get(client, "spoc/exam/info", {"id": exam_id, "classId": class_id}, timeout=8)
    except Exception:
        return None
    if not isinstance(info, dict) or info.get("code") != 200:
        return None
    data = info.get("data") or {}
    if not _paper_is_question_bank(data):
        return None
    try:
        face = _web_get(client, "spoc/exam/paper",
                        {"id": exam_id, "classId": class_id, "device": "1"}, timeout=8)
    except Exception:
        return None
    if not isinstance(face, dict) or not face.get("questions"):
        return None
    ids = {str(q.get("id") or "") for q in (face.get("questions") or []) if q.get("id")}
    want = {str(x) for x in (sheet_paper_ids or set()) if x}
    if want and ids and want != ids:
        log(f"[{nickname}] SPOC作业换腿中止：卷面行不一致", "ERROR")
        return None
    return str(data.get("taskId") or ""), str(face.get("signId") or "")


def _looks_window_closed(msg):
    s = str(msg or "")
    return any(k in s for k in ("非作答时间", "不在作答", "作答时间", "禁止进入"))


def _mentions_device(msg):
    s = str(msg or "")
    return any(k in s for k in ("设备", "验证", "其他设备", "另一台", "顶掉", "退出考试"))


def _enter_sheet(client, exam_id, class_id):
    msg = ""
    try:
        resp = client.api_post("spoc/file/exam/detail/with/student", {
            "examId": exam_id, "classId": class_id, "device": "2", "groupId": "0",
        }, timeout=30)
        if resp and resp.get("code") == 200 and resp.get("data"):
            d = resp["data"]
            return (d.get("answerSheets") or [], d.get("taskId") or d.get("id") or d.get("recordId"),
                    d.get("signId"), "")
        msg = str((resp or {}).get("msg") or "")
    except Exception as e:
        msg = str(e)
    return [], None, None, msg


def _load_paper(client, exam_id, class_id):
    try:
        res = _web_post(client, "spoc/exam/loadExamPaper", {
            "device": "1", "id": exam_id, "resitId": None, "classId": class_id,
        }, timeout=30)
    except Exception as e:
        return None, str(e)
    if res and res.get("code") == 200 and isinstance(res.get("data"), dict):
        return res["data"], ""
    return None, str((res or {}).get("msg") or "")


def _answer_map(questions):
    by_id = {}
    by_title = {}
    for q in questions or []:
        if not isinstance(q, dict):
            continue
        for key in (q.get("questionId"), q.get("id")):
            if key:
                by_id.setdefault(str(key), q)
        title = q.get("title") or q.get("examName") or q.get("examContent")
        if title:
            key = re.sub(r"<[^>]+>", "", str(title)).strip()
            if key:
                by_title.setdefault(key, q)
    return by_id, by_title


def _answer_of(q):
    if not isinstance(q, dict):
        return ""
    for k in ("answer", "correctAnswer", "rightAnswer"):
        v = q.get(k)
        if not answer_blank(v):
            return str(v)
    return ""


def _lookup(q, by_id, by_title):
    qid = str(q.get("questionId") or q.get("id") or "")
    hit = by_id.get(qid) if qid else None
    if hit:
        return hit
    title = q.get("title") or q.get("examName") or q.get("examContent")
    if title:
        key = re.sub(r"<[^>]+>", "", str(title)).strip()
        if key:
            return by_title.get(key)
    return None


def _composite_records(parent_no, parent_paper_id, subs):
    recs = []
    if not parent_paper_id or not isinstance(subs, list):
        return recs
    for j, sb in enumerate(subs):
        if not isinstance(sb, dict):
            continue
        t = str(sb.get("typeId") or "")
        if t not in ("1", "2", "3"):
            continue
        raw = str(sb.get("answer") or "").strip()
        sub_id = str(sb.get("id") or "").strip()
        if not sub_id or not raw:
            continue
        rec = {"questionNo": json.dumps([parent_no, j], separators=(",", ":")),
               "answer": raw, "paperId": parent_paper_id, "paperSubId": sub_id}
        if t in ("1", "2"):
            dj = sb.get("dataJson")
            try:
                dj = json.loads(dj) if isinstance(dj, str) else (dj or [])
            except Exception:
                dj = []
            if isinstance(dj, list) and dj:
                rec["optionSort"] = json.dumps(dj, ensure_ascii=False, separators=(",", ":"))
        recs.append(rec)
    return recs


def _full_options(q, ans_q):
    opts = [o for o in (parse_options(q) or []) if isinstance(o, dict)]
    if not opts:
        opts = [o for o in (parse_options(ans_q) or []) if isinstance(o, dict)]
    if not opts:
        opts = [o for o in ((q.get("_door_options") or [])) if isinstance(o, dict)]
    if not opts:
        return []
    backup = [o for o in (parse_options(ans_q) or []) if isinstance(o, dict)]
    if not backup:
        backup = [o for o in ((q.get("_door_options") or [])) if isinstance(o, dict)]
    out = []
    for idx, opt in enumerate(opts):
        content = opt.get("Content", "")
        if not content and idx < len(backup):
            content = backup[idx].get("Content", "")
        out.append({
            "Content": content,
            "IsAnswer": False,
            "SortOrder": str(opt.get("SortOrder", "")),
            "CaseSensitive": str(opt.get("CaseSensitive", "false")).lower(),
        })
    return out


def _record_item(i, q, raw_answer, ans_q):
    type_id = str(q.get("typeId") or "")
    q_id = q.get("id") or q.get("questionId") or ""
    raw = str(raw_answer or "")
    option_sort = ""
    if type_id in ("1", "2", "3"):
        opts = _full_options(q, ans_q)
        if type_id == "3" and not opts:
            option_sort = json.dumps([
                {"label": "1", "SortOrder": "A", "Content": "正确"},
                {"label": "0", "SortOrder": "B", "Content": "错误"},
            ], ensure_ascii=False)
        elif opts:
            option_sort = json.dumps(opts, ensure_ascii=False)
        rec = {"questionNo": i + 1, "typeId": type_id, "knowledgePointsId": "",
               "paperId": q_id, "optionSort": option_sort, "answer": raw}
        return rec
    if type_id in ("4", "5"):
        ans_list = []
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    ans_list = [str(x) for x in parsed]
            except Exception:
                pass
        if not ans_list and raw:
            if "," in raw:
                ans_list = [x.strip() for x in raw.split(",")]
            elif "，" in raw:
                ans_list = [x.strip() for x in raw.split("，")]
            else:
                ans_list = [raw]
        if not ans_list:
            ans_list = [""]
        return {"questionNo": i + 1, "typeId": type_id, "knowledgePointsId": "",
                "paperId": q_id, "answer": json.dumps(ans_list, ensure_ascii=False)}
    rec = {"questionNo": i + 1, "typeId": type_id, "knowledgePointsId": "",
           "paperId": q_id, "answer": raw}
    opts = _full_options(q, ans_q)
    if opts:
        rec["optionSort"] = json.dumps(opts, ensure_ascii=False)
    return rec


def _build_records(target_questions, by_id, by_title):
    recs = []
    src = []
    for i, q in enumerate(target_questions):
        tid = str(q.get("typeId") or "")
        qid = q.get("id") or q.get("questionId") or ""
        ans_q = _lookup(q, by_id, by_title) or q
        raw = _answer_of(ans_q) or _answer_of(q)
        if tid in ("8", "9", "10"):
            subs = (q.get("taskExamSubProblemRecordPaperVos")
                    or q.get("questionSubList") or [])
            comp = _composite_records(i, qid, subs)
            if comp:
                recs.extend(comp)
                src.extend([i] * len(comp))
                continue
            raw = ""
        recs.append(_record_item(i, q, raw, ans_q))
        src.append(i)
    return recs, src


def _missing_indices(recs):
    return [i for i, r in enumerate(recs) if answer_blank(r.get("answer"))]


def _delete_old_records(client, nickname, exam_id, class_id, current_id):
    try:
        rec_data = client.api_get("spoc/exam/record/list", {
            "examId": exam_id, "classId": class_id, "pageNum": "1", "pageSize": "10",
        })
        if not rec_data or rec_data.get("code") != 200 or not rec_data.get("data"):
            return
        rows = rec_data["data"].get("rows") if isinstance(rec_data["data"], dict) else rec_data["data"]
        if not isinstance(rows, list):
            return
        for row in rows:
            rid = row.get("id")
            if rid and str(rid) != str(current_id or ""):
                client.api_delete("spoc/exam/record", {"id": str(rid)})
    except Exception as e:
        log(f"[{nickname}] 删除旧记录异常: {e}", "WARNING")


def _hw_used_sec(n):
    base = max(360, min(1500, int(n or 0) * 8))
    lo = max(360, int(base * 0.85))
    hi = min(1500, int(base * 1.15))
    return random.randint(lo, hi) if hi > lo else base


def _exam_used_sec(n, category_id, duration):
    base = int(n or 0) * random.randint(15, 40)
    exam_time = max(base, random.randint(30, 120))
    cat = str(category_id or "")
    if cat == "2":
        exam_time = max(random.randint(960, 1080), min(exam_time, random.randint(960, 1080)))
    elif cat == "3":
        exam_time = max(random.randint(300, 500), min(exam_time, random.randint(1600, 1900)))
    else:
        exam_time = max(random.randint(60, 180), min(exam_time, random.randint(1600, 1900)))
    if duration and cat == "2":
        cap = max(60, int(duration) - 60)
        if exam_time > cap:
            exam_time = cap
    return exam_time


def _file_stu_answer(q, rec):
    tid = str(q.get("typeId") or "")
    ans = str(rec.get("answer") or "")
    if tid == "3" and ans in ("0", "1") and not parse_options(q):
        return ans
    if tid in ("1", "2", "3"):
        try:
            opts = json.loads(rec.get("optionSort") or "[]")
        except Exception:
            opts = []
        if not isinstance(opts, list) or not opts:
            opts = [o for o in (parse_options(q) or []) if isinstance(o, dict)]
        if opts:
            chosen = {x.strip() for x in ans.split(",") if x.strip()}
            for o in opts:
                o["IsAnswer"] = str(o.get("SortOrder", "")) in chosen
            return json.dumps(opts, ensure_ascii=False)
    return ans


def _submit_homework_file(client, nickname, exam_id, class_id, course_info_id, course_id,
                          target_questions, recs, used, record_id=None):
    if len(recs) != len(target_questions):
        return None
    hw = _web_post(client, "spoc/file/exam/get/non/answer/paper", {
        "examId": exam_id, "classId": class_id, "device": 1,
    }, timeout=30) or {}
    data = hw.get("data") or {}
    rows = data.get("answerSheets") or []
    task = data.get("taskId") or record_id
    if hw.get("code") != 200 or not rows or not task:
        return None
    by_paper = {}
    for q, rec in zip(target_questions, recs):
        qid = str((q.get("id") or q.get("questionId") or ""))
        if qid:
            by_paper[qid] = (q, rec)
    answer_records = []
    for row in rows:
        rid = str(row.get("id") or "")
        pair = by_paper.get(rid)
        if not pair:
            continue
        q, rec = pair
        answer_records.append({
            "id": str(uuid.uuid4()).upper(),
            "taskId": task,
            "classId": class_id,
            "examId": exam_id,
            "paperId": rid,
            "typeId": str(q.get("typeId") or row.get("typeId") or ""),
            "stuAnswer": _file_stu_answer(q, rec),
            "fileInfo": "[]",
        })
    if not answer_records:
        return None
    ok_n = 0
    for item in answer_records:
        r = _web_post(client, "spoc/file/exam/auto/record", {
            "classId": class_id, "examId": exam_id, "fileInfo": item["fileInfo"],
            "id": item["id"], "paperId": item["paperId"], "stuAnswer": item["stuAnswer"],
            "taskId": task, "typeId": item["typeId"],
        }, timeout=30)
        if r and r.get("code") == 200:
            ok_n += 1
    if not ok_n:
        return None
    wait = random.randint(180, 240)
    log(f"[{nickname}] SPOC作业逐题落答案 {ok_n}/{len(answer_records)}，等待 {wait} 秒后交卷", "INFO")
    time.sleep(wait)
    res = _web_post(client, "spoc/file/exam/submit", {
        "taskId": task, "classId": class_id, "courseId": course_id,
        "courseInfoId": course_info_id, "examId": exam_id,
        "examTime": max(used, wait), "device": 1, "answerRecords": answer_records,
    }, timeout=120)
    ok = bool(res and res.get("code") == 200)
    return ok, str((res or {}).get("msg") or ("提交成功" if ok else "提交失败"))


def _submit_homework_draft(client, nickname, exam_id, class_id, course_info_id, course_id,
                           category_id, recs, src, task, used, sign_id=""):
    rows = []
    for i, rec in enumerate(recs):
        rn = rec.get("questionNo")
        row = {
            "questionNo": rn if isinstance(rn, str) else (src[i] if i < len(src) else i),
            "optionSort": rec.get("optionSort", ""),
            "answer": str(rec.get("answer", "")),
            "paperId": rec.get("paperId", ""),
        }
        if "paperSubId" in rec:
            row["paperSubId"] = rec["paperSubId"]
        rows.append(row)
    draft = _web_post(client, "spoc/exam/record/draft", {
        "categoryId": int(category_id or 1), "courseId": course_id,
        "courseInfoId": course_info_id, "examId": exam_id, "examTime": used,
        "groupId": "0", "isLast": True, "status": "",
        "taskExamProblemRecordList": rows, "updateBy": "", "updateTime": "",
        "userId": "", "classId": class_id, "id": task or "", "resitId": "", "device": "1",
    }, timeout=60)
    if not draft or draft.get("code") != 200:
        return False, str((draft or {}).get("msg") or "草稿保存失败")
    if not task:
        info = _web_get(client, "spoc/exam/info", {"id": exam_id, "classId": class_id}) or {}
        task = str(((info.get("data") or {}).get("taskId")) or "").strip()
        if not task:
            return False, "草稿已保存但未取到作答记录 id"
    body = {
        "categoryId": int(category_id or 1), "courseId": course_id,
        "courseInfoId": course_info_id, "examId": exam_id, "examTime": used,
        "groupId": "0", "isLast": True, "status": "",
        "taskExamProblemRecordList": recs, "updateBy": "", "updateTime": "",
        "userId": "", "classId": class_id, "id": task, "resitId": None,
        "isAuto": 0, "device": "1",
    }
    if sign_id:
        body["sid"] = sign_id
    res = _web_post(client, "spoc/exam/record", body, timeout=300)
    ok = bool(res and res.get("code") == 200)
    return ok, str((res or {}).get("msg") or ("提交成功" if ok else "提交失败"))


def _submit_homework(client, nickname, exam_id, class_id, course_info_id, course_id,
                     category_id, target_questions, recs, src, record_id, sign_id):
    used = _hw_used_sec(len(recs))
    paper_ids = {str(r.get("paperId") or "") for r in recs if r.get("paperId")}
    prof = paper_profile(client, exam_id, class_id, nickname,
                         need_sign=not sign_id, sheet_paper_ids=paper_ids)
    if prof and prof.get("family") == "question_bank":
        if not sign_id and prof.get("sign_id"):
            sign_id = prof["sign_id"]
        return _submit_homework_draft(client, nickname, exam_id, class_id, course_info_id,
                                      course_id, category_id, recs, src,
                                      prof.get("task_id") or record_id, used, sign_id)
    file_res = _submit_homework_file(client, nickname, exam_id, class_id, course_info_id,
                                     course_id, target_questions, recs, used, record_id)
    if file_res is not None:
        return file_res
    fb = homework_exam_family(client, nickname, exam_id, class_id, paper_ids)
    if not fb:
        return False, "未能进入本次作业作答（平台未返回可用卷面）"
    task, fb_sign = fb
    if fb_sign and not sign_id:
        sign_id = fb_sign
    return _submit_homework_draft(client, nickname, exam_id, class_id, course_info_id,
                                  course_id, category_id, recs, src,
                                  task or record_id, used, sign_id)


def _submit_exam(client, nickname, exam_id, class_id, course_info_id, course_id,
                 category_id, recs, record_id, sign_id, duration):
    used = _exam_used_sec(len(recs), category_id, duration)
    _delete_old_records(client, nickname, exam_id, class_id, record_id)
    log(f"[{nickname}] SPOC考试等待 {used} 秒后交卷", "INFO")
    time.sleep(used)
    if record_id:
        paper, _msg = _load_paper(client, exam_id, class_id)
        if paper:
            rec = paper.get("taskExamRecord") or {}
            new_id = rec.get("id")
            if new_id and str(new_id) != str(record_id):
                return False, "作答记录在等待期间变更，本次未交卷"
            ids = {str(q.get("id") or "") for q in (paper.get("questions") or []) if q.get("id")}
            mine = {str(r.get("paperId") or "") for r in recs if r.get("paperId")}
            if ids and mine and ids != mine:
                return False, "卷面在等待期间被重新抽取，本次未交卷"
    body = {
        "categoryId": int(category_id) if str(category_id).isdigit() else 1,
        "courseId": course_id, "courseInfoId": course_info_id, "examId": exam_id,
        "examTime": used, "groupId": "0", "isLast": True, "status": "",
        "taskExamProblemRecordList": recs, "updateBy": "", "updateTime": "",
        "userId": "", "classId": class_id, "resitId": None, "isAuto": 0, "device": "1",
    }
    if record_id:
        body["id"] = record_id
    if sign_id and str(category_id) != "2":
        body["sid"] = sign_id
    res = _web_post(client, "spoc/exam/record", body, timeout=300)
    m = re.search(r"须作答满\s*(\d+)\s*分钟", str((res or {}).get("msg") or ""))
    if res and res.get("code") == 500 and m:
        need = int(m.group(1)) * 60
        extra = max(0, need + 90 - used)
        log(f"[{nickname}] 平台要求作答满 {need} 秒，补等 {extra} 秒后重交", "INFO")
        if extra:
            time.sleep(extra)
        body["examTime"] = used + extra
        res = _web_post(client, "spoc/exam/record", body, timeout=300)
    ok = bool(res and res.get("code") == 200)
    return ok, str((res or {}).get("msg") or ("提交成功" if ok else "提交失败"))


def submit_spoc_exam(client, nickname, exam_id, class_id, course_info_id, course_id,
                     category_id, title, questions, exam_time=0):
    """SPOC 交卷主入口。返回 (ok, msg)。"""
    from zjy_client import sanitize_own_answer_sheet

    student_questions, record_id, sign_id, enter_msg = _enter_sheet(client, exam_id, class_id)
    if enter_msg and not student_questions:
        if _mentions_device(enter_msg):
            log(f"[{nickname}] 进卷被设备验证拦截（{enter_msg}），按无视设备锁继续", "WARNING")
        else:
            log(f"[{nickname}] 进卷未成功（{enter_msg}），继续尝试", "WARNING")

    paper, paper_msg = _load_paper(client, exam_id, class_id)
    duration = 0
    paper_qs = []
    not_exam_paper = False
    if paper:
        rec = paper.get("taskExamRecord") or {}
        record_id = rec.get("id") or paper.get("taskId") or record_id
        sign_id = paper.get("signId") or rec.get("signId") or sign_id
        try:
            duration = int(paper.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        paper_qs = paper.get("questions") or []
    else:
        not_exam_paper = "试卷非考试试卷" in str(paper_msg or "")
        if _looks_window_closed(paper_msg):
            return False, f"当前不在本卷作答窗口内（平台：{paper_msg}）"
        if paper_msg and not not_exam_paper:
            if _mentions_device(paper_msg):
                log(f"[{nickname}] 进卷被设备验证拦截（{paper_msg}），按无视设备锁继续", "WARNING")
            else:
                log(f"[{nickname}] 进卷未成功（{paper_msg}），继续尝试", "WARNING")

    target = student_questions or paper_qs or questions or []
    if not target:
        return False, "未获取到题目"
    if student_questions:
        sanitize_own_answer_sheet(student_questions, "交卷卷面", clear_draft=True)
    merge_door_composite(target, nickname)
    by_id, by_title = _answer_map(questions)
    recs, src = _build_records(target, by_id, by_title)
    missing = _missing_indices(recs)
    if missing:
        log(f"[{nickname}] SPOC交卷中止：{len(missing)}/{len(recs)} 题未匹配到答案", "WARNING")
        return False, f"有 {len(missing)} 道题未匹配到答案，已拒绝交卷"

    is_hw = str(category_id) == "1" or not_exam_paper
    if is_hw:
        return _submit_homework(client, nickname, exam_id, class_id, course_info_id, course_id,
                                category_id, target, recs, src, record_id, sign_id)
    return _submit_exam(client, nickname, exam_id, class_id, course_info_id, course_id,
                        category_id, recs, record_id, sign_id, duration)
