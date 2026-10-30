# -*- coding: utf-8 -*-
"""诊断 ICU-07 DVT预防率 科室3439 9月份分子/分母是否混入其他科室。"""
import sys, io, calendar
from pathlib import Path
from datetime import datetime as dt
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import (get_client, BED_DB_NAMES, get_datacenter_db, iter_bed_dbs,
                _keyword_regex, DRUG_DVT_KEYWORDS, MECH_DVT_KEYWORDS,
                FILTER_KEYWORDS, FLUSH_EXCLUDE_KEYWORDS, EXECUTED_ORDER_STATUSES,
                get_dvt_prevention_patients, get_icu04_apache_data)

DEPT = sys.argv[1] if len(sys.argv) > 1 else "3439"
YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
MONTH = int(sys.argv[3]) if len(sys.argv) > 3 else 9
START = f"{YEAR}-{MONTH:02d}-01"
END = f"{YEAR}-{MONTH:02d}-{calendar.monthrange(YEAR, MONTH)[1]:02d}"
start_dt, end_dt = dt.fromisoformat(START), dt.fromisoformat(END)
end_of_day = dt(end_dt.year, end_dt.month, end_dt.day, 23, 59, 59)

print(f"=== ICU-07 DVT预防率  科室={DEPT}  {START} ~ {END} ===\n")

db = get_client()[BED_DB_NAMES[0]]

# ---------- 1. 当前代码口径 ----------
cur = get_dvt_prevention_patients([DEPT], START, END)
icu04 = get_icu04_apache_data([DEPT], START, END)
cur_num = cur.get("all_count", 0)
cur_den = icu04["den_count"]
print(f"[当前代码] 分子={cur_num}  分母={cur_den}  比率={cur_num/cur_den*100:.2f}%" if cur_den
      else f"[当前代码] 分子={cur_num} 分母=0")

# ---------- 2. 分母明细 + 真实 deptCode ----------
den_docs = list(db.patient.find(
    {
        "deptCode": {"$in": [DEPT]},
        "status": {"$ne": "invalid"},
        "icuAdmissionTime": {"$lte": end_of_day},
        "$or": [
            {"icuDischargeTime": {"$gte": start_dt}},
            {"icuDischargeTime": None},
            {"icuDischargeTime": {"$exists": False}},
        ],
    },
    {"mrn": 1, "hisPid": 1, "name": 1, "hisBed": 1, "deptCode": 1,
     "icuAdmissionTime": 1, "icuDischargeTime": 1, "status": 1, "wardCode": 1},
))
print(f"\n[分母查询] deptCode={DEPT} 命中 {len(den_docs)} 条")
print("  deptCode 分布:", dict(Counter(str(d.get('deptCode')) for d in den_docs)))

# ---------- 3. 全库同期在科患者，按 deptCode 拆 ----------
all_docs = list(db.patient.find(
    {
        "status": {"$ne": "invalid"},
        "icuAdmissionTime": {"$lte": end_of_day},
        "$or": [
            {"icuDischargeTime": {"$gte": start_dt}},
            {"icuDischargeTime": None},
            {"icuDischargeTime": {"$exists": False}},
        ],
    },
    {"mrn": 1, "deptCode": 1, "name": 1},
))
print(f"\n[全库同期在科患者] {len(all_docs)} 条")
by_dept = Counter(str(d.get('deptCode')) for d in all_docs)
for k, v in sorted(by_dept.items(), key=lambda x: -x[1]):
    print(f"    deptCode={k}: {v}")

# deptCode 的形态（是否 list / 前缀包含）
sample = [d.get("deptCode") for d in all_docs[:5]]
print("  deptCode 样例:", sample)

# 模糊匹配：deptCode 字段里是否还有别的编码（多科室患者）
multi = [d for d in all_docs
         if isinstance(d.get("deptCode"), (list, tuple))
         or (isinstance(d.get("deptCode"), str) and "," in d.get("deptCode"))]
print(f"  多值 deptCode 患者数: {len(multi)}")
for d in multi[:10]:
    print("     ", d.get("mrn"), d.get("deptCode"))

# ---------- 4. 分子明细及其所属科室 ----------
all_pat = {}
for key in ("drug_patients", "mech_patients"):
    for p in cur.get(key, []):
        all_pat.setdefault(p.get("pid"), p)
print(f"\n[分子] 去重患者 {len(all_pat)} (drug={len(cur.get('drug_patients',[]))} mech={len(cur.get('mech_patients',[]))})")

# 分子患者在 DataCenter VI_ICU_ZYBR 上的 deptCode
dc = get_datacenter_db()
pids = list(all_pat.keys())
zybr = list(dc["VI_ICU_ZYBR"].find(
    {"pid": {"$in": pids}}, {"pid": 1, "mrn": 1, "name": 1, "deptCode": 1}))
zybr_map = {z["pid"]: z for z in zybr}
print("  分子患者 DataCenter deptCode 分布:",
      dict(Counter(str(z.get('deptCode')) for z in zybr)))

# 分子患者在 SmartCare patient 表上的 deptCode
mrns = [zybr_map.get(p, {}).get("mrn") for p in pids]
mrns = [m for m in mrns if m]
sc_docs = list(db.patient.find({"mrn": {"$in": mrns}},
                               {"mrn": 1, "deptCode": 1, "name": 1, "hisBed": 1}))
sc_map = {d.get("mrn"): d for d in sc_docs}
print("  分子患者 SmartCare deptCode 分布:",
      dict(Counter(str(d.get('deptCode')) for d in sc_docs)))

# ---------- 5. 正确口径：分子分母都限定本科室 ----------
den_ids = {str(d["_id"]) for d in den_docs}
num_in_dept = []
num_out_dept = []
for p in all_pat.values():
    z = zybr_map.get(p.get("pid"), {})
    m = z.get("mrn", "")
    sc = sc_map.get(m, {})
    row = {
        "pid": p.get("pid"), "mrn": m,
        "name": sc.get("name") or z.get("name") or p.get("name", ""),
        "hisBed": sc.get("hisBed", ""),
        "dc_dept": str(z.get("deptCode")),
        "sc_dept": str(sc.get("deptCode")),
    }
    if str(sc.get("deptCode")) == DEPT or str(z.get("deptCode")) == DEPT:
        num_in_dept.append(row)
    else:
        num_out_dept.append(row)

print(f"\n[分子归属] 属于 {DEPT}: {len(num_in_dept)}   其他科室: {len(num_out_dept)}")
if num_out_dept:
    print("  --- 非本科室分子 ---")
    for r in num_out_dept:
        print("   ", r)

# ---------- 6. 交叉：分子/分母 是否同一集合 ----------
# 正确分子 = 分母集合 ∩ 有DVT医嘱的患者
den_mrns = {d.get("mrn") for d in den_docs}
den_hispids = {d.get("hisPid") for d in den_docs}
print(f"\n[交集检查] 分母 mrn 数={len(den_mrns)}")

print("\n--- 分母明细（当前口径） ---")
for d in sorted(den_docs, key=lambda x: str(x.get('icuAdmissionTime'))):
    print(f"  {d.get('mrn')}  {d.get('hisBed',''):<6} deptCode={d.get('deptCode')} "
          f"入ICU={str(d.get('icuAdmissionTime'))[:16]} 出ICU={str(d.get('icuDischargeTime'))[:16]} "
          f"{d.get('name','')}")

print("\n--- 分子明细 ---")
for r in sorted(num_in_dept + num_out_dept, key=lambda x: str(x['mrn'])):
    mark = "OK " if (r['sc_dept'] == DEPT or r['dc_dept'] == DEPT) else "OUT"
    print(f"  [{mark}] {r['mrn']} bed={r['hisBed']:<6} dc={r['dc_dept']} sc={r['sc_dept']} {r['name']}")

# ---------- 7. 结论 ----------
print("\n=== 结论 ===")
print(f"分母(=ICU-04口径, deptCode={DEPT}) = {cur_den}")
print(f"分子(原始 all_count) = {cur_num}")
print(f"分子(限定本科室)     = {len(num_in_dept)}")
if cur_den:
    print(f"当前展示比率 = {cur_num}/{cur_den} = {cur_num/cur_den*100:.2f}%")
    print(f"修正后比率   = {len(num_in_dept)}/{cur_den} = {len(num_in_dept)/cur_den*100:.2f}%")
