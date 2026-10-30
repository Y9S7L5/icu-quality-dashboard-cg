# -*- coding: utf-8 -*-
"""
ICU-05 cg 变体简化口径（2026-10 新口径）。

仅当 BUNDLE_HOSPITAL_VARIANT == "cg" 时启用（db.get_bundle_data_v2 顶部短路），
default 变体完全不经过本模块。

分母（按月 M，人次口径）:
  - 该月在科时间重叠的 ICU 患者（同 get_icu_denominator_stays 圈定方式）
  - 第一关：patient.clinicalDiagnosis 含 CG_SHOCK_DIAG_KEYWORDS
    （脓毒性休克 / 感染性休克）
  - 第二关（按月归属）：
    - 有 diagnosisHistoryList → 诊断列表中有 diagnosis 命中关键词且 time
      落在月 M 的条目（最早命中），无月内命中不入组
    - 无 diagnosisHistoryList（诊断未变化的患者）→ 按 icuAdmissionTime
      归属月份，入科时间在月 M 才入组，命中时间记为入科时间
  - 无任何临床门控（不要求器官障碍 / 感染证据 / Sepsis-3 休克确认）

分子窗口 = 在科期间 [icuAdmissionTime, icuDischargeTime]（未出科用当前
时间兜底）——不限入科后 1/3/6 小时，做过即可；1h/3h/6h 判定口径一致

分子（四项全满足，无顺序要求）:
  1. 血气:   VI_ICU_ZYYZ orderName 含 "ICU血气" ≥1 条
  2. 痰培养: orderName 含 "细菌培养" ≥1 条
  3. 血培养: orderName 含 "血培养鉴定" ≥1 条
  4. 抗生素: drugExe 关键词首条命中（现有逻辑不变）
  6h 另需: 在科期间 ICU血气 ≥2 条（复查血气）

判定为二值（缺证据 = False，无 None 态）；原因码:
  BLOOD_GAS_MISSING / SPUTUM_CULTURE_MISSING / BLOOD_CULTURE_MISSING /
  ANTIBIOTIC_MISSING / RECHECK_BGA_MISSING
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Optional

from config.bundle_rules import CG_ORDER_KEYWORDS, CG_SHOCK_DIAG_KEYWORDS
from tzutil import utc_day_start, utc_day_end_wide

logger = logging.getLogger(__name__)

# ---- 抗生素关键词（与 db.judge_bundle_v3_for_patient 共用，单一来源）----
ABX_KEYWORDS = [
    '头孢', '培南', '青霉', '万古', '阿奇', '左氧', '莫西',
    '甲硝唑', '奥硝唑', '利奈', '替考', '磷霉素', '美罗',
    '亚胺', '厄他', '哌酮', '他唑', '舒巴坦', '氨苄', '哌拉',
    '阿莫', '克拉', '多西', '米诺', '替加', '达托', '夫西',
    '磺胺', '呋喃', '吡哌', '诺氟', '环丙', '氧氟',
]
NON_ABX_KEYWORDS = ['肝素', '低分子', '华法林', '阿司匹林', '氯吡格雷',
                    '维生素', '氯化钾', '胰岛素', '碳酸氢钠', '呋塞米',
                    '甘露醇', '地塞米松', '甲泼尼龙', '丙泊酚', '咪达唑仑',
                    '芬太尼', '瑞芬太尼', '右美托咪定', '吗啡', '布洛芬',
                    '对乙酰氨基酚', '皮试', '皮试剂']

# ---- 原因码 ----
R_BLOOD_GAS = "BLOOD_GAS_MISSING"
R_SPUTUM = "SPUTUM_CULTURE_MISSING"
R_BLOOD_CULTURE = "BLOOD_CULTURE_MISSING"
R_ANTIBIOTIC = "ANTIBIOTIC_MISSING"
R_RECHECK = "RECHECK_BGA_MISSING"

_BGA_RE = re.compile(CG_ORDER_KEYWORDS["blood_gas"])  # ICU血气
_SPUTUM_KW = CG_ORDER_KEYWORDS["sputum_culture"]      # 细菌培养
_BLOOD_CULTURE_KW = CG_ORDER_KEYWORDS["blood_culture"]  # 血培养鉴定

WINDOWS = (1, 3, 6)


def _naive_utc(dt):
    """归一到 naive UTC 帧（存储帧铁律）；非 datetime → None。"""
    if dt is None or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _in(ts, start, end) -> bool:
    """start <= ts <= end（均已归一 naive UTC）。ts 无效 → False。"""
    ts = _naive_utc(ts)
    return ts is not None and start <= ts <= end


# ============================================================
# 纯函数：分母筛选
# ============================================================

def filter_cg_denominator_stays(stays: list, month_start: datetime,
                                month_end: datetime) -> list:
    """按「clinicalDiagnosis 命中 + 按月归属」筛选在科记录。

    第一关：clinicalDiagnosis 含 CG_SHOCK_DIAG_KEYWORDS，不命中直接出局。
    第二关（月归属）：
      - 有 diagnosisHistoryList → 列表中有关键词命中且 time 在月 M 的条目
        （取最早），无月内命中不入组（照旧逻辑）
      - 无 diagnosisHistoryList → 按 icuAdmissionTime 归月（在月 M 内才入组）

    stays: get_icu_denominator_stays 同形状（含 clinicalDiagnosis /
    diagnosisHistoryList 投影）。返回 enrich 后的行（附加
    cg_diag_hit_time / cg_diag_text / cg_diag_path）。
    在科重叠月份的条件由上游查询保证，这里只判诊断命中与月归属。
    """
    out = []
    for stay in stays or []:
        clin_text = str(stay.get("clinicalDiagnosis") or "")
        # 第一关：当前诊断必须命中关键词
        if not any(kw in clin_text for kw in CG_SHOCK_DIAG_KEYWORDS):
            continue

        hit_time = None
        hit_text = None
        path = None
        hist = stay.get("diagnosisHistoryList") or []
        if hist:
            # 有诊断列表 → 旧逻辑：月内命中的最早条目
            hits = []
            for d in hist:
                if not isinstance(d, dict):
                    continue
                t = _naive_utc(d.get("time"))
                text = str(d.get("diagnosis") or "")
                if t is None:
                    continue
                if any(kw in text for kw in CG_SHOCK_DIAG_KEYWORDS) \
                        and month_start <= t <= month_end:
                    hits.append((t, text))
            if not hits:
                continue
            hits.sort(key=lambda x: x[0])
            hit_time, hit_text = hits[0]
            path = "diagnosis_history_list"
        else:
            # 无诊断列表 → 按入科时间归属月份
            t0 = _naive_utc(stay.get("icuAdmissionTime"))
            if t0 is None or not (month_start <= t0 <= month_end):
                continue
            hit_time = t0
            hit_text = clin_text
            path = "clinical_diagnosis_fallback"

        row = dict(stay)
        row["cg_diag_hit_time"] = hit_time
        row["cg_diag_text"] = hit_text
        row["cg_diag_path"] = path
        out.append(row)
    return out


# ============================================================
# 纯函数：在科期间判定（不限入科后 1/3/6h，四项做过即可）
# ============================================================

def evaluate_cg_windows(t0: datetime,
                        stay_end: Optional[datetime],
                        blood_gas_times: list,
                        sputum_times: list,
                        blood_culture_times: list,
                        abx_time: Optional[datetime]) -> dict:
    """在科区间 [icuAdmissionTime, icuDischargeTime] 判定四项（二值）。

    1h/3h/6h 三个窗口判定口径一致（四项在在科期间做过即可，不再限定
    入科后 N 小时内）；仅 6h 额外要求血气 ≥2 条（复查血气）。
    stay_end 为空（未出科）→ 用当前 UTC 时间兜底。
    """
    t0 = _naive_utc(t0)
    end = _naive_utc(stay_end) or datetime.utcnow()
    if end < t0:
        end = t0

    bg_in = [t for t in (_naive_utc(x) for x in (blood_gas_times or []))
             if t is not None and t0 <= t <= end]
    sput_in = [t for t in (_naive_utc(x) for x in (sputum_times or []))
               if t is not None and t0 <= t <= end]
    bc_in = [t for t in (_naive_utc(x) for x in (blood_culture_times or []))
             if t is not None and t0 <= t <= end]
    abx_ok = _in(abx_time, t0, end)

    bg_ok = bool(bg_in)
    sput_ok = bool(sput_in)
    bc_ok = bool(bc_in)

    reasons = []
    if not bg_ok:
        reasons.append(R_BLOOD_GAS)
    if not sput_ok:
        reasons.append(R_SPUTUM)
    if not bc_ok:
        reasons.append(R_BLOOD_CULTURE)
    if not abx_ok:
        reasons.append(R_ANTIBIOTIC)

    step1 = bg_ok
    step2 = sput_ok and bc_ok and abx_ok
    result = {}
    for n in WINDOWS:
        if n == 6:
            recheck_ok = len(bg_in) >= 2
            if not recheck_ok:
                n_reasons = reasons + [R_RECHECK]
            else:
                n_reasons = list(reasons)
            step3 = recheck_ok
        else:
            step3 = True
            n_reasons = list(reasons)
        finish = step1 and step2 and bool(step3)

        result[f"bundle_{n}h"] = {
            "finish": finish,
            "step1": step1,
            "step2": step2,
            "step3": bool(step3),
            "finish_path": "cg_stay_four_items",
            "reasons": n_reasons,
            # 判定列映射: a1=血气, b3=第二步(培养+抗生素)
            "a1": bg_ok,
            "b1": bc_ok,
            "b2": sput_ok and bc_ok,
            "b3": step2,
            "c1": None, "c2": None, "c3": None,
            "t0": t0,
            "stay_end": end,
            "b3_reason": n_reasons[0] if not step2 else None,
            # 在科期间证据时间（明细红字/时间列用）
            "blood_gas_time": min(bg_in) if bg_in else None,
            "blood_gas_count": len(bg_in),
            "sputum_time": min(sput_in) if sput_in else None,
            "culture_time": min(bc_in) if bc_in else None,
            "antibiotic_time": abx_time if abx_ok else None,
            "has_lactate_recheck": (len(bg_in) >= 2) if n == 6 else None,
        }
    return result


# ============================================================
# 编排：分母 + 证据抓取 + 判定（与 get_bundle_data_v2 同返回形状）
# ============================================================

def get_bundle_data_cg(dept_codes: list, start_date: str, end_date: str) -> dict:
    """cg 简化口径主入口。返回:
    {total, h1_num, h3_num, h6_num, h1_patients, h3_patients, h6_patients,
     den_patients, old_shock_count, candidate_mode}
    """
    from db import build_exclusion_key, iter_bed_dbs

    month_start = utc_day_start(start_date)
    month_end = utc_day_end_wide(end_date)

    result = {
        "total": 0, "h1_num": 0, "h3_num": 0, "h6_num": 0,
        "h1_patients": [], "h3_patients": [], "h6_patients": [],
        "den_patients": [],
        "old_shock_count": 0, "candidate_mode": "cg",
    }

    # ---- 1. 在科记录（重叠月）+ clinicalDiagnosis / diagnosisHistoryList 命中 ----
    query: dict = {
        "status": {"$ne": "invalid"},
        "icuAdmissionTime": {"$lte": month_end},
        "$or": [
            {"icuDischargeTime": {"$gte": month_start}},
            {"icuDischargeTime": None},
            {"icuDischargeTime": {"$exists": False}},
        ],
    }
    if dept_codes:
        query["deptCode"] = {"$in": [str(c) for c in dept_codes]}

    stays: list = []
    for _db_name, db in iter_bed_dbs():
        try:
            stays = list(db.patient.find(query, {
                "_id": 1, "mrn": 1, "hisPid": 1, "patientId": 1, "name": 1,
                "hisBed": 1, "icuAdmissionTime": 1, "icuDischargeTime": 1,
                "deptCode": 1, "diagnosisHistoryList": 1,
                "clinicalDiagnosis": 1,
            }))
        except Exception as _exc:
            logger.warning("DB %s failed in get_bundle_data_cg: %s", _db_name, _exc)
            continue
        if stays:
            break

    rows = filter_cg_denominator_stays(stays, month_start, month_end)

    # ---- 2. 组装分母行 ----
    den_patients: list = []
    for row in rows:
        t0 = row.get("icuAdmissionTime")
        if not isinstance(t0, datetime):
            continue
        _id = str(row.get("_id", ""))
        his_pid = str(row.get("hisPid") or "")
        event_id = his_pid or _id
        pat = {
            "_id": _id,
            "mrn": str(row.get("mrn") or ""),
            "hisPid": his_pid,
            "name": row.get("name", ""),
            "hisBed": row.get("hisBed", ""),
            "deptCode": row.get("deptCode", ""),
            "sc_pid": _id,
            "dc_pid": event_id,
            "diagnose": row.get("cg_diag_text", ""),
            "diagnosisTime": row.get("cg_diag_hit_time"),
            "icuAdmissionTime": t0,
            "icuDischargeTime": row.get("icuDischargeTime"),
            "t0": t0,
            "t0_source": "icu_admission",
            "source": ("cg_diagnosis_history_list"
                       if row.get("cg_diag_path") == "diagnosis_history_list"
                       else "cg_clinical_diagnosis"),
            "cg_diag_hit_time": row.get("cg_diag_hit_time"),
            "cg_diag_path": row.get("cg_diag_path", ""),
            # 候选字段（详情页过滤/列渲染用；新口径下恒为诊断入组）
            "candidate_status": "high_probability",
            "candidate_pathways": ["diagnosis_list"],
            "clinical_confirmation_status": "confirmed",
            "is_septic_shock_candidate": True,
        }
        pat["detail_id"] = build_exclusion_key(event_id, t0)
        pat["exclusion_key"] = pat["detail_id"]
        den_patients.append(pat)

    result["den_patients"] = den_patients
    result["total"] = len(den_patients)
    result["old_shock_count"] = len(den_patients)

    if not den_patients:
        return result

    # ---- 3. 证据批量抓取 ----
    evidence = _fetch_evidence(den_patients)

    # ---- 4. 逐患者在科期间判定 ----
    for pat in den_patients:
        ev = evidence.get(pat["exclusion_key"], {})
        try:
            windows = evaluate_cg_windows(
                pat["t0"],
                pat.get("icuDischargeTime"),
                ev.get("blood_gas_times", []),
                ev.get("sputum_times", []),
                ev.get("blood_culture_times", []),
                ev.get("abx_time"),
            )
        except Exception as _exc:
            logger.warning("cg stay eval failed for %s: %s", pat.get("mrn"), _exc)
            windows = evaluate_cg_windows(
                pat["t0"], pat.get("icuDischargeTime"), [], [], [], None)

        pat["v3"] = _synthesize_v3(pat, windows, ev)
        if windows["bundle_1h"]["finish"]:
            result["h1_patients"].append(pat)
            result["h1_num"] += 1
        if windows["bundle_3h"]["finish"]:
            result["h3_patients"].append(pat)
            result["h3_num"] += 1
        if windows["bundle_6h"]["finish"]:
            result["h6_patients"].append(pat)
            result["h6_num"] += 1

    return result


def _fetch_evidence(den_patients: list) -> Dict[str, dict]:
    """按 exclusion_key 返回 {blood_gas_times, sputum_times, blood_culture_times,
    abx_time, abx_name}。单源失败只影响该源，不丢分母。"""
    from db import get_client

    ev: Dict[str, dict] = {p["exclusion_key"]: {
        "blood_gas_times": [], "sputum_times": [], "blood_culture_times": [],
        "abx_time": None, "abx_name": None,
    } for p in den_patients}

    # ---- VI_ICU_ZYYZ 医嘱（血气/痰培养/血培养，一次批量查，无状态过滤）----
    try:
        dc = get_client("DataCenter")["DataCenter"]
        dc_map = {}  # dc_pid -> exclusion_key 列表（同 hisPid 可能对应多行）
        for p in den_patients:
            dc_map.setdefault(str(p["dc_pid"]), []).append(p["exclusion_key"])
        min_t0 = min(p["t0"] for p in den_patients)
        # 在科期间判定：查询上界取各自出科时间（未出科用当前时间兜底）
        _now = datetime.utcnow()
        max_end = max((_naive_utc(p.get("icuDischargeTime")) or _now)
                      for p in den_patients)
        combined = ("|".join([_BGA_RE.pattern,
                              re.escape(_SPUTUM_KW),
                              re.escape(_BLOOD_CULTURE_KW)]))
        docs = dc.VI_ICU_ZYYZ.find(
            {"pid": {"$in": list(dc_map.keys())},
             "orderName": {"$regex": combined},
             "orderTime": {"$gte": min_t0, "$lte": max_end}},
            {"pid": 1, "orderName": 1, "orderTime": 1},
        ).sort("orderTime", 1)
        for doc in docs:
            t = doc.get("orderTime")
            if not isinstance(t, datetime):
                continue
            name = str(doc.get("orderName") or "")
            for key in dc_map.get(str(doc.get("pid") or ""), []):
                if _BGA_RE.search(name):
                    ev[key]["blood_gas_times"].append(t)
                if _SPUTUM_KW in name:
                    ev[key]["sputum_times"].append(t)
                if _BLOOD_CULTURE_KW in name:
                    ev[key]["blood_culture_times"].append(t)
    except Exception as _exc:
        logger.warning("cg order evidence fetch failed: %s", _exc)

    # ---- drugExe 抗生素（现有关键词逻辑，逐患者，首条命中）----
    try:
        sc = get_client("SmartCare")["SmartCare"]
        for p in den_patients:
            try:
                t0 = p["t0"]
                # 在科期间：出科时间兜底当前时间
                t_end = _naive_utc(p.get("icuDischargeTime")) or datetime.utcnow()
                docs = sc.drugExe.find(
                    {"pid": p["sc_pid"],
                     "startTime": {"$gte": t0, "$lte": t_end}},
                    {"drugList.name": 1, "startTime": 1},
                ).sort("startTime", 1)
                for dd in docs:
                    st = dd.get("startTime")
                    if not st:
                        continue
                    for dl in dd.get("drugList", []):
                        name = str(dl.get("name", ""))
                        is_abx = any(kw in name for kw in ABX_KEYWORDS)
                        if is_abx and not any(kw in name for kw in NON_ABX_KEYWORDS):
                            ev[p["exclusion_key"]]["abx_time"] = st
                            ev[p["exclusion_key"]]["abx_name"] = name
                            break
                    if ev[p["exclusion_key"]]["abx_time"]:
                        break
            except Exception as _exc:
                logger.warning("cg abx fetch failed for %s: %s", p.get("mrn"), _exc)
    except Exception as _exc:
        logger.warning("cg drugExe connect failed: %s", _exc)

    return ev


def _synthesize_v3(pat: dict, windows: dict, ev: dict) -> dict:
    """合成与 _build_v3_dict 契约兼容的 v3 dict（详情页读取）。"""
    w6 = windows["bundle_6h"]
    return {
        "t0": pat["t0"],
        "mrn": pat.get("mrn", ""),
        "sc_pid": pat.get("sc_pid", ""),
        "dc_pid": pat.get("dc_pid", ""),
        # 门控：新口径下恒为入组（诊断列表命中即分母）
        "gate": {"is_septic_shock": True, "has_infection": True,
                 "has_organ_dysfunction": True, "reason": "CG_DIAGNOSIS_LIST"},
        "is_septic_shock": True,
        # 遭遇级证据（顶层展示）
        "antibiotic_time": ev.get("abx_time"),
        "antibiotic_name": ev.get("abx_name") or "",
        "blood_gas_times": ev.get("blood_gas_times", []),
        "has_lactate_recheck": w6.get("has_lactate_recheck"),
        # 无乳酸/MAP/升压药等旧口径数据 — 明细列显示 "—"
        "lactate_all": [],
        "sofa": None,
        "clinical_layer": None,
        "candidate_info": {
            "is_septic_shock_candidate": True,
            "candidate_status": "high_probability",
            "candidate_pathways": ["diagnosis_list"],
            "clinical_confirmation_status": "confirmed",
        },
        **windows,
    }
