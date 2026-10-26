# -*- coding: utf-8 -*-
"""
report_export.py —《重症质控指标上报》月度上报值计算（含 APACHEⅡ 分段）。

数据口径与看板一致：汇总值走 indicator_list（预聚合 + ICU-05 实时兜底），
模板特有的拆分（原有/新入、死亡类别、24h APACHE 窗口、仅滤器）在此计算。
values[key] = null 表示无系统数据（前端留空，由人工填报）。
"""
from __future__ import annotations

import calendar
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger("report_export")

# 模板 key —— 与前端 reportTemplate.js 的 REPORT_ROWS 必须一一对应
# apache_* 为 APACHEⅡ 分段人数（插在 10.1/10.2 之后，后续编号顺延），
# 其余 key 保持《重症质控指标上报（2024年版）》原编号（仅 label 展示号顺延）
REPORT_KEYS = [
    "2", "3", "5", "6", "8",
    "10.1", "10.2", "apache_lt15_rate",
    "apache_lt10", "apache_10_15", "apache_15_20", "apache_20_25", "apache_ge25",
    "11.1", "11.2",
    "15", "16", "17", "18", "20", "21",
    "23.1", "23.2", "24", "25", "26",
    "28", "29", "31.1", "31.2", "33.1", "33.2",
    "37", "38", "40.1", "40.2",
    "42", "43", "45", "47", "48", "50", "51",
    "53", "54", "56", "57",
    "59.1", "59.2", "60", "62.1", "62.2", "63",
    "64", "65", "66", "67", "69", "71",
]

# 无系统数据源的项（人工填报）—— 前端留空，此处恒 null
NO_SOURCE_KEYS = {"64", "65", "66", "67", "69", "71"}

_VALUES_CACHE: dict = {}
_VALUES_TTL = 600.0  # 10 分钟：重复导出同一月份直接命中


def _month_bounds(period: str):
    y, m = map(int, period.split("-"))
    return f"{period}-01", f"{period}-{calendar.monthrange(y, m)[1]:02d}"


def _is_carry_in(admit, start_dt) -> bool:
    """原有患者判定：入科时间早于统计期初（与明细 patient_type 同一规则）。"""
    return bool(admit) and admit < start_dt


def compute_report_values(period: str, icu_unit: str = "all") -> dict:
    """计算单月上报值（含 APACHEⅡ 分段），返回 {period, icu_unit, has_data, values}。"""
    ck = (period, icu_unit)
    hit = _VALUES_CACHE.get(ck)
    if hit and time.time() - hit[0] < _VALUES_TTL:
        return hit[1]

    # 延迟导入避免与 main.py 循环依赖
    from main import indicator_list, _detail_cache_payload, _resolve_dept_codes
    from db import (get_icu_denominator_stays, dedup_persons, person_key_of,
                    get_dvt_prevention_patients, get_icu11_data, iter_bed_dbs,
                    get_apache_score_bands)

    start, end = _month_bounds(period)
    # 入科时间存储为真 UTC → 期初边界北京−8h（_is_carry_in 原有/新入判定）
    from tzutil import utc_day_start
    start_dt = utc_day_start(start)
    dept_codes = _resolve_dept_codes(icu_unit)
    vals: dict = {k: None for k in REPORT_KEYS}

    # ---- 1. 汇总值（与看板同源，含 ICU-05 实时兜底） ----
    rows = indicator_list(period, icu_unit)
    by = {r.get("code"): r for r in rows}

    def _num(code):
        r = by.get(code)
        return r.get("numerator") if r else None

    def _den(code):
        r = by.get(code)
        return r.get("denominator") if r else None

    vals["2"], vals["3"] = _num("ICU-01"), _den("ICU-01")
    vals["5"], vals["6"] = _num("ICU-02"), _den("ICU-02")
    vals["8"] = _num("ICU-03")
    vals["15"], vals["16"], vals["17"] = (
        _num("ICU-05-1h"), _num("ICU-05-3h"), _num("ICU-05-6h"))
    vals["18"] = next((v for v in (_den("ICU-05-1h"), _den("ICU-05-3h"),
                                   _den("ICU-05-6h")) if v is not None), None)
    vals["20"], vals["21"] = _num("ICU-06"), _den("ICU-06")
    vals["28"], vals["29"] = _num("ICU-08"), _den("ICU-08")
    vals["42"], vals["43"] = _num("ICU-12"), _den("ICU-12")
    vals["45"] = _num("ICU-13")
    vals["47"], vals["48"] = _num("ICU-14"), _den("ICU-14")
    vals["50"], vals["51"] = _num("ICU-15"), _den("ICU-15")
    vals["53"], vals["54"] = _num("ICU-16"), _den("ICU-16")
    vals["56"], vals["57"] = _num("ICU-17"), _den("ICU-17")
    vals["60"], vals["63"] = _den("ICU-18"), _den("ICU-19")

    # ---- 2. 在科队列：11.1/11.2 + 10.1/10.2 ----
    stays = get_icu_denominator_stays(dept_codes, start, end)
    persons = dedup_persons(stays)
    if stays:
        carry = sum(1 for p in persons
                    if _is_carry_in(p.get("icuAdmissionTime"), start_dt))
        vals["11.1"], vals["11.2"] = carry, len(persons) - carry

        # ---- 2b. APACHEⅡ 分段人数（插在 10.1/10.2 之后）----
        # 口径与 ICU-04-01 一致（同队列、人数去重、当月首次 apacheII total），
        # 仅分值区间不同；无评分的不进桶。
        try:
            bands = get_apache_score_bands(dept_codes, start, end)
            vals["apache_lt10"] = bands.get("lt10", 0)      # <10
            vals["apache_10_15"] = bands.get("10_15", 0)    # 10≤·<15
            vals["apache_15_20"] = bands.get("15_20", 0)    # 15≤·<20
            vals["apache_20_25"] = bands.get("20_25", 0)    # 20≤·<25
            vals["apache_ge25"] = bands.get("ge25", 0)      # ≥25
            # APACHEⅡ＜15分收治率 = 首次评分<15人数 / 同期收治患者总数
            _den = bands.get("den_count", 0) or 0
            _lt15 = (bands.get("lt10", 0) or 0) + (bands.get("10_15", 0) or 0)
            vals["apache_lt15_rate"] = round(_lt15 / _den * 100, 1) if _den > 0 else 0.0
        except Exception as exc:
            logger.warning("report_export apache score bands failed: %s", exc)

        # 10.1 / 10.2：24h 窗口 APACHE≥15
        pid_to_key = {str(s["_id"]): person_key_of(s)
                      for s in stays if s.get("_id")}
        first_by_pid: dict = {}
        for _db_name, db in iter_bed_dbs():
            try:
                for r in db.score.aggregate([
                    {"$match": {"pid": {"$in": list(pid_to_key)},
                                "scoreType": "apacheII", "valid": True}},
                    {"$sort": {"time": 1}},
                    {"$group": {"_id": "$pid", "t": {"$first": "$time"},
                                "total": {"$first": "$total"}}},
                ]):
                    first_by_pid[r["_id"]] = (r.get("t"), r.get("total") or 0)
                break
            except Exception as exc:
                logger.warning("report_export apache query failed: %s", exc)
                continue
        scores_by_key: dict = {}
        for pid, entry in first_by_pid.items():
            key = pid_to_key.get(pid)
            if not key:
                continue
            cur = scores_by_key.get(key)
            if cur is None or (entry[0] and (cur[0] is None or entry[0] < cur[0])):
                scores_by_key[key] = entry
        carry_hit = new_hit = 0
        for p in persons:
            key = person_key_of(p)
            at = p.get("icuAdmissionTime")
            entry = scores_by_key.get(key)
            if not at or not entry or entry[0] is None:
                continue
            if not (at <= entry[0] <= at + timedelta(hours=24)):
                continue
            if (entry[1] or 0) < 15:
                continue
            if _is_carry_in(at, start_dt):
                carry_hit += 1
            else:
                new_hit += 1
        vals["10.1"], vals["10.2"] = carry_hit, new_hit

        # ---- 3. DVT：23.x / 24 / 25 / 26 ----
        dvt = get_dvt_prevention_patients(dept_codes, start, end)
        ns = dvt.get("num_stays") or []
        dvt_carry = sum(1 for s in ns
                        if _is_carry_in(s.get("icuAdmissionTime"), start_dt))
        vals["23.1"], vals["23.2"] = dvt_carry, len(ns) - dvt_carry
        vals["24"] = sum(1 for s in ns if s.get("measure") == "药物预防")
        vals["25"] = sum(1 for s in ns if s.get("measure") == "机械预防")
        drug = {x.get("mrn") for x in dvt.get("drug_patients") or []}
        mech = {x.get("mrn") for x in dvt.get("mech_patients") or []}
        filt = {x.get("mrn") for x in dvt.get("filter_patients") or []}
        den_mrns = {s.get("mrn") for s in stays}
        vals["26"] = len((filt - drug - mech) & den_mrns)

    # ---- 4. ICU-09 / ICU-10：分子关联分母 patient_type → 31.x / 33.x ----
    # 分子明细的 patient_id ⊆ 分母明细（已验证），分母行带 patient_type；
    # 分子明细的 admit_time 实为评估时间，不能用来判断原有/新入。
    # 汇总缺失/分子为 0 时跳过明细拉取（空月份避免触发实时重算）。
    for code, k1, k2 in (("ICU-09", "31.1", "31.2"),
                         ("ICU-10", "33.1", "33.2")):
        list_num = _num(code)
        if list_num is None:
            continue
        if list_num == 0:
            vals[k1], vals[k2] = 0, 0
            continue
        try:
            den_rows = _detail_cache_payload(code, period, "denominator", icu_unit)
            num_rows = _detail_cache_payload(code, period, "numerator", icu_unit)
        except Exception as exc:
            logger.warning("report_export %s detail failed: %s", code, exc)
            continue
        if not den_rows:
            continue
        # 人次口径：同一患者多次入科会有多行，patient_id 不再唯一，
        # 用在科记录 detail_id 关联；历史缓存无 detail_id 时退回 patient_id。
        def _row_key(p):
            return p.get("detail_id") or p.get("patient_id")
        type_map = {_row_key(p): p.get("patient_type") for p in den_rows}
        carry = sum(1 for p in num_rows
                    if type_map.get(_row_key(p)) == "原有")
        vals[k1], vals[k2] = carry, len(num_rows) - carry

    # ---- 5. ICU-11：37 / 38 / 40.1 / 40.2 ----
    if _num("ICU-11") is not None:
        try:
            d11 = get_icu11_data(dept_codes, start, end)
        except Exception as exc:
            logger.warning("report_export icu11 failed: %s", exc)
            d11 = None
        if d11 and (d11.get("den_patients") or d11.get("num_patients")):
            vals["37"] = sum(1 for p in d11["num_patients"]
                             if p.get("dischargedType") == "死亡")
            vals["38"] = sum(1 for p in d11["num_patients"]
                             if p.get("dischargedType") == "非医嘱离院")
            s_carry = s_new = 0.0
            for p in d11["den_patients"]:
                v = p.get("apache_calDead") or 0
                if _is_carry_in(p.get("icu_admit"), start_dt):
                    s_carry += v
                else:
                    s_new += v
            vals["40.1"] = round(s_carry, 4)
            vals["40.2"] = round(s_new, 4)

    # ---- 6. ICU-18 / ICU-19 分子按入科时间拆 → 59.x / 62.x ----
    # 明细行的入科时间在 admit_time 字段（'YYYY-MM-DD HH:MM' 字符串），
    # 与 'YYYY-MM-01' 字符串比较即可判定是否早于期初（同格式字典序=时间序）。
    period_start = f"{period}-01"
    for code, k1, k2 in (("ICU-18", "59.1", "59.2"),
                         ("ICU-19", "62.1", "62.2")):
        list_num = _num(code)
        if list_num is None:
            continue
        if list_num == 0:
            vals[k1], vals[k2] = 0, 0
            continue
        try:
            num_rows = _detail_cache_payload(code, period, "numerator", icu_unit)
        except Exception as exc:
            logger.warning("report_export %s detail failed: %s", code, exc)
            num_rows = None
        if num_rows:
            carry = sum(1 for p in num_rows
                        if (p.get("admit_time") or "")
                        and str(p["admit_time"]) < period_start)
            vals[k1], vals[k2] = carry, len(num_rows) - carry
        else:
            # 明细缺失但汇总有值：无法拆分，新入取汇总值，原有留空
            logger.warning("report_export %s num detail empty but summary=%s",
                           code, list_num)
            vals[k1], vals[k2] = None, list_num

    # ---- 7. 无数据源项恒 null ----
    for k in NO_SOURCE_KEYS:
        vals[k] = None

    result = {
        "period": period,
        "icu_unit": icu_unit,
        # 有在科患者，或汇总存在非零值 → 视为有数据；
        # 空月份（在科=0 且仅有 ICU-05 实时兜底的 0 值）判为无数据，前端跳过导出
        "has_data": bool(stays) or any(v is not None and v != 0
                                       for v in vals.values()),
        "values": vals,
    }
    if len(_VALUES_CACHE) > 200:
        now = time.time()
        for key in [k for k, v in _VALUES_CACHE.items()
                    if now - v[0] >= _VALUES_TTL]:
            _VALUES_CACHE.pop(key, None)
    _VALUES_CACHE[ck] = (time.time(), result)
    return result
