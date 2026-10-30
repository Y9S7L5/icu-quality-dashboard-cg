# -*- coding: utf-8 -*-
"""ICU-07 口径选项对比：科室3439 指定月份。"""
import sys, io, re, calendar
from pathlib import Path
from datetime import datetime as dt
from collections import defaultdict, Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import (get_client, BED_DB_NAMES, get_datacenter_db, _keyword_regex,
                DRUG_DVT_KEYWORDS, MECH_DVT_KEYWORDS, FILTER_KEYWORDS,
                FLUSH_EXCLUDE_KEYWORDS, EXECUTED_ORDER_STATUSES)

DEPT = sys.argv[1] if len(sys.argv) > 1 else "3439"
YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
MONTH = int(sys.argv[3]) if len(sys.argv) > 3 else 9
S = dt.fromisoformat(f"{YEAR}-{MONTH:02d}-01")
_e = dt.fromisoformat(f"{YEAR}-{MONTH:02d}-{calendar.monthrange(YEAR, MONTH)[1]:02d}")
E = dt(_e.year, _e.month, _e.day, 23, 59, 59)

db = get_client()[BED_DB_NAMES[0]]
dc = get_datacenter_db()

den = list(db.patient.find(
    {"deptCode": {"$in": [DEPT]}, "status": {"$ne": "invalid"},
     "icuAdmissionTime": {"$lte": E},
     "$or": [{"icuDischargeTime": {"$gte": S}}, {"icuDischargeTime": None},
             {"icuDischargeTime": {"$exists": False}}]},
    {"mrn": 1, "name": 1}))
den_mrns = {d.get("mrn") for d in den}
den_rec, den_pat = len(den), len(den_mrns)

zy = list(dc["VI_ICU_ZYBR"].find({"mrn": {"$in": list(den_mrns)}},
                                  {"mrn": 1, "pid": 1, "deptCode": 1}))
pid2mrn = {z.get("pid"): z.get("mrn") for z in zy if z.get("pid")}
# 当前口径的 pid 池（ZYBR.deptCode=本科室 + 时间窗）
zy_dept = list(dc["VI_ICU_ZYBR"].find(
    {"deptCode": DEPT, "admitTime": {"$lte": E},
     "$or": [{"dischargeTime": {"$gte": S}}, {"dischargeTime": None},
             {"dischargeTime": ""}]}, {"pid": 1}))
cur_pids_pool = {z.get("pid") for z in zy_dept}

re_all = _keyword_regex(DRUG_DVT_KEYWORDS + MECH_DVT_KEYWORDS + FILTER_KEYWORDS)
re_drug = _keyword_regex(DRUG_DVT_KEYWORDS)
re_mech = _keyword_regex(MECH_DVT_KEYWORDS)
re_flush = _keyword_regex(FLUSH_EXCLUDE_KEYWORDS)
LAB_FP = re.compile(r"肝素结合蛋白|HBP|降钙素原|PCT|二联检|三联检")
# 短名封管剂量：*肝素钠注射液(海普天)0.250万iu —— 无“封管/冲管/有创压用”字样
FLUSH_DOSE = re.compile(r"肝素钠注射液\(海普天\)0\.(0625|125|250|25)万iu", re.I)
STOP_ACT = re.compile(r"^\s*停\*")


def get_orders(statuses, wide=False):
    q = {"pid": {"$in": list(pid2mrn)},
         "orderName": {"$regex": re_all, "$options": "i"},
         "status": {"$in": statuses}}
    if not wide:
        q["orderTime"] = {"$gte": S, "$lte": E}
    return list(dc["VI_ICU_ZYYZ"].find(
        q, {"pid": 1, "orderName": 1, "orderTime": 1, "status": 1, "orderType": 1}
    ).limit(200000))


def hit(o, exclude_lab=True, exclude_flushdose=True, allow_stop=True):
    nm = o.get("orderName") or ""
    if exclude_lab and LAB_FP.search(nm):
        return None
    if STOP_ACT.match(nm) and not allow_stop:
        return None
    if re.search(re_drug, nm, re.I):
        if re.search(re_flush, nm, re.I):
            pass  # 整体剔除（不计药物）
        elif exclude_flushdose and FLUSH_DOSE.search(nm) and not re.search(re_mech, nm, re.I):
            pass
        else:
            return "drug"
    if re.search(re_mech, nm, re.I):
        return "mech"
    if re.search(_keyword_regex(FILTER_KEYWORDS), nm, re.I):
        return "filter"
    return None


# ---------- 口径 A: 当前页面（ZYBR dept 过滤 + 已执行 + 无检验剔除） ----------
ordsA = [o for o in get_orders(EXECUTED_ORDER_STATUSES) if o.get("pid") in cur_pids_pool]
A = {pid2mrn[o["pid"]] for o in ordsA if hit(o, exclude_lab=False, exclude_flushdose=False)
     and pid2mrn.get(o["pid"]) in den_mrns}

# ---------- 口径 B: 去 ZYBR dept 过滤 + 已执行 + 无检验剔除 ----------
B = {pid2mrn[o["pid"]] for o in ordsA if hit(o, exclude_lab=False, exclude_flushdose=False)
     and pid2mrn.get(o["pid"]) in den_mrns}

# ---------- 口径 C(推荐): B + 剔检验假匹配 ----------
C = {pid2mrn[o["pid"]] for o in ordsA if hit(o, exclude_lab=True, exclude_flushdose=False)
     and pid2mrn.get(o["pid"]) in den_mrns}

# ---------- 口径 D: C + 剔短名封管剂量 ----------
D = {pid2mrn[o["pid"]] for o in ordsA if hit(o, exclude_lab=True, exclude_flushdose=True)
     and pid2mrn.get(o["pid"]) in den_mrns}

# ---------- 口径 E: D + 放宽状态（含已停止/已取消） ----------
ordsW = get_orders(["已执行", "已审核", "已停止", "已取消", "停止", "3"])
Eset = {pid2mrn[o["pid"]] for o in ordsW if hit(o, exclude_lab=True, exclude_flushdose=True)
        and pid2mrn.get(o["pid"]) in den_mrns}

# ---------- 口径 F: D + 不认停嘱（仅起始医嘱） ----------
F = {pid2mrn[o["pid"]] for o in ordsA if hit(o, exclude_lab=True, exclude_flushdose=True, allow_stop=False)
     and pid2mrn.get(o["pid"]) in den_mrns}

name_map = {d.get("mrn"): d.get("name") for d in den}


def rec(m):
    return sum(1 for d in den if d.get("mrn") in m)


def show(tag, s):
    print(f"  {tag:34s} 分子 {len(s):3d}人 / {rec(s):3d}人次 → {rec(s)/den_rec*100:6.2f}%  (人数 {len(s)/den_pat*100:.2f}%)")


print(f"=== 科室 {DEPT}  {S:%Y-%m-%d} ~ {E:%Y-%m-%d} ===")
print(f"分母 {den_rec} 人次 / {den_pat} 人\n")
print("口径对比:")
show("A 当前页面(有bug)", A)
show("B 去ZYBR科室过滤", B)
show("C B+剔检验假匹配(上轮推荐)", C)
show("D C+剔短名封管剂量", D)
show("E D+状态放宽(含已停止)", Eset)
show("F D+不认停嘱", F)

print("\n差集明细:")
def diff(x, y, label):
    s = x - y
    print(f"  {label}: {len(s)} 人")
    for m in sorted(s, key=str):
        print(f"     {m} {name_map.get(m,'')}")
diff(B, A, "B-A (ZYBR科室过滤漏掉)")
diff(C, B, "C-B (应为空, C⊂B)")
diff(D, C, "D-C (剔检验后又剔的)")
diff(Eset, D, "E-D (状态放宽新增)")
diff(D, Eset, "D-E (状态放宽反而少的? 应空)")
diff(D, F, "D-F (仅靠停嘱计入的)")

# 检验假匹配且没有别的证据的人
lab_only = {m for m in B - C}
print(f"\n纯检验假匹配(应剔): {len(lab_only)} 人 -> {sorted(lab_only)}")
