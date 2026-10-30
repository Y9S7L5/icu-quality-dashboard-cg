# -*- coding: utf-8 -*-
"""ICU-07 DVT预防率 —— 科室3439 指定月份的正确分子/分母。

口径：
  分母 = SmartCare.patient 中 deptCode=本科室、ICU在科区间与统计期有交集的记录
  分子 = 分母中的患者，在统计期内存在“已执行”的 DVT 预防医嘱（药物/物理）
关键点：分子不再经 DataCenter.VI_ICU_ZYBR.deptCode 过滤 —— ZYBR 每个 mrn 只保留
最近一次科室住院记录，转出本科室的患者会被该过滤条件误剔除。
"""
import sys, io, re, calendar
from pathlib import Path
from datetime import datetime as dt
from collections import defaultdict, Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import (get_client, BED_DB_NAMES, get_datacenter_db, _keyword_regex,
                DRUG_DVT_KEYWORDS, MECH_DVT_KEYWORDS, FILTER_KEYWORDS,
                FLUSH_EXCLUDE_KEYWORDS, EXECUTED_ORDER_STATUSES,
                get_dvt_prevention_patients, get_icu04_apache_data)

DEPT = sys.argv[1] if len(sys.argv) > 1 else "3439"
YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
MONTH = int(sys.argv[3]) if len(sys.argv) > 3 else 9
START = f"{YEAR}-{MONTH:02d}-01"
END = f"{YEAR}-{MONTH:02d}-{calendar.monthrange(YEAR, MONTH)[1]:02d}"
S = dt.fromisoformat(START)
_end = dt.fromisoformat(END)
E = dt(_end.year, _end.month, _end.day, 23, 59, 59)

db = get_client()[BED_DB_NAMES[0]]
dc = get_datacenter_db()

print(f"=== ICU-07 DVT预防率 · 科室 {DEPT} · {START} ~ {END} ===\n")

# ---------- 分母 ----------
den = list(db.patient.find(
    {"deptCode": {"$in": [DEPT]}, "status": {"$ne": "invalid"},
     "icuAdmissionTime": {"$lte": E},
     "$or": [{"icuDischargeTime": {"$gte": S}},
             {"icuDischargeTime": None},
             {"icuDischargeTime": {"$exists": False}}]},
    {"mrn": 1, "name": 1, "hisBed": 1, "icuAdmissionTime": 1, "icuDischargeTime": 1}))
den_mrns = {d.get("mrn") for d in den}
den_rec = len(den)
den_pat = len(den_mrns)

# ---------- 分子 ----------
zy = list(dc["VI_ICU_ZYBR"].find(
    {"mrn": {"$in": list(den_mrns)}}, {"mrn": 1, "pid": 1, "deptCode": 1}))
pid2mrn = {z.get("pid"): z.get("mrn") for z in zy if z.get("pid")}

re_all = _keyword_regex(DRUG_DVT_KEYWORDS + MECH_DVT_KEYWORDS + FILTER_KEYWORDS)
re_drug = _keyword_regex(DRUG_DVT_KEYWORDS)
re_mech = _keyword_regex(MECH_DVT_KEYWORDS)
re_flush = _keyword_regex(FLUSH_EXCLUDE_KEYWORDS)

orders = list(dc["VI_ICU_ZYYZ"].find(
    {"pid": {"$in": list(pid2mrn)},
     "orderName": {"$regex": re_all, "$options": "i"},
     "orderTime": {"$gte": S, "$lte": E},
     "status": {"$in": EXECUTED_ORDER_STATUSES}},
    {"pid": 1, "orderName": 1, "orderTime": 1}).limit(50000))

drug_pids, mech_pids = set(), set()
for o in orders:
    nm = o.get("orderName") or ""
    pid = o.get("pid") or ""
    if not pid or not nm:
        continue
    if re.search(re_drug, nm, re.I):
        if re.search(re_flush, nm, re.I):
            continue
        drug_pids.add(pid)
    if re.search(re_mech, nm, re.I):
        mech_pids.add(pid)
ok_pids = drug_pids | mech_pids
num_mrns = {pid2mrn[p] for p in ok_pids} & den_mrns

# 严格口径：医嘱时间落在某次本科室在科区间内
stays = defaultdict(list)
for d in den:
    a = d.get("icuAdmissionTime")
    b = d.get("icuDischargeTime") or E
    if a:
        stays[d.get("mrn")].append((a, b))
strict_mrns = set()
for o in orders:
    pid = o.get("pid") or ""
    nm = o.get("orderName") or ""
    if not pid or not nm:
        continue
    hit = re.search(re_drug, nm, re.I) and not re.search(re_flush, nm, re.I)
    hit = hit or re.search(re_mech, nm, re.I)
    if not hit:
        continue
    m = pid2mrn.get(pid)
    t = o.get("orderTime")
    if m in den_mrns and t and any(a <= t <= b for a, b in stays.get(m, [])):
        strict_mrns.add(m)

# ---------- 当前代码口径（对照） ----------
cur = get_dvt_prevention_patients([DEPT], START, END)
icu04 = get_icu04_apache_data([DEPT], START, END)
cur_num = cur.get("all_count", 0)
cur_den = icu04["den_count"]

# ---------- 输出 ----------
print("【当前页面口径】")
print(f"  分子 = {cur_num}   分母 = {cur_den}   比率 = {cur_num / cur_den * 100:.2f}%"
      if cur_den else "  无数据")
print("  → 分子经 VI_ICU_ZYBR.deptCode=3439 过滤，而 ZYBR 每个 mrn 只保留最近一次科室记录，\n"
      "    转出本科室的患者被剔除，分子漏计。\n")

print("【修正后口径】")
print(f"  分母（在科记录数） = {den_rec}")
print(f"  分母（在科人数）   = {den_pat}")
print(f"  分子（人数）       = {len(num_mrns)}")
print(f"  分子（按在科记录） = {sum(1 for d in den if d.get('mrn') in num_mrns)}")
print(f"  比率 = {len(num_mrns)}/{den_pat} = {len(num_mrns) / den_pat * 100:.2f}%   （按人数）")
print(f"  比率 = {sum(1 for d in den if d.get('mrn') in num_mrns)}/{den_rec} = "
      f"{sum(1 for d in den if d.get('mrn') in num_mrns) / den_rec * 100:.2f}%   （按记录/人次）")
print(f"  （严格口径：医嘱须发生在本科室在科区间内 → 分子 {len(strict_mrns)}，"
      f"{len(strict_mrns) / den_pat * 100:.2f}%）")

# 漏计明细：当前分子 = (ZYBR.deptCode=本科室) ∩ 有DVT医嘱
zy_dept = list(dc["VI_ICU_ZYBR"].find(
    {"deptCode": DEPT, "admitTime": {"$lte": E},
     "$or": [{"dischargeTime": {"$gte": S}}, {"dischargeTime": None}, {"dischargeTime": ""}]},
    {"mrn": 1, "pid": 1, "deptCode": 1}))
cur_pids = {z.get("pid") for z in zy_dept} & ok_pids
mrn_all = {z.get("pid"): z.get("mrn") for z in zy_dept}
cur_mrns = {mrn_all.get(p) for p in cur_pids} & den_mrns

missed = num_mrns - cur_mrns
print(f"\n  当前口径漏计的分子患者：{len(missed)} 人")

zy_by_mrn = {z.get("mrn"): z for z in zy}
print("\n--- 被漏计的患者（ZYBR 最近科室 ≠ 本科室） ---")
name_map = {d.get("mrn"): d.get("name") for d in den}
bed_map = {d.get("mrn"): d.get("hisBed") for d in den}
for m in sorted(missed):
    z = zy_by_mrn.get(m, {})
    print(f"    {m}  {name_map.get(m, '')}  床号{bed_map.get(m, '')}  "
          f"ZYBR最近科室={z.get('deptCode')}")
