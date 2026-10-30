# -*- coding: utf-8 -*-
"""
导出 ICU-07 DVT预防率 指定科室/月份的分子分母明细到项目根目录（Excel + CSV）。

直接调用生产函数，导出结果与打包后的页面完全一致（**人数口径**，与 INDICATORS_CONFIG
里「患者数」的定义一致；明细一人一行，可与页面逐行对上）：
  分母 = db.get_icu_denominator_stays() 经 dedup_persons() 去重 → 页面分母
  分子 = db.get_dvt_prevention_patients()["num_stays"]（生产端已按患者去重）→ 页面分子

用法:
    python scripts/icu07_export_detail.py [科室] [年] [月]
    python scripts/icu07_export_detail.py 3439 2026 9
"""
import sys, io, re, csv, calendar
from pathlib import Path
from datetime import datetime as dt
from collections import defaultdict, Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from db import (get_datacenter_db, _keyword_regex,
                DRUG_DVT_KEYWORDS, MECH_DVT_KEYWORDS, FILTER_KEYWORDS,
                FLUSH_EXCLUDE_KEYWORDS, EXECUTED_ORDER_STATUSES,
                get_icu_denominator_stays, get_dvt_prevention_patients,
                classify_dvt_order, DVT_LAB_FALSE_MATCH_PATTERN, dedup_persons)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DEPT = sys.argv[1] if len(sys.argv) > 1 else "3439"
YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
MONTH = int(sys.argv[3]) if len(sys.argv) > 3 else 9
START = f"{YEAR}-{MONTH:02d}-01"
END = f"{YEAR}-{MONTH:02d}-{calendar.monthrange(YEAR, MONTH)[1]:02d}"
S = dt.fromisoformat(START)
_end = dt.fromisoformat(END)
E = dt(_end.year, _end.month, _end.day, 23, 59, 59)

DEPT_NAME = {"3439": "江南重症医学科", "2927": "重症医学科", "2915": "急诊重症监护",
             "3442": "江南感染病科ICU", "3414": "江南CCU", "3412": "江南急诊重症监护",
             "2964": "呼吸内科四病区", "2953": "心血管内科四病区", "3409": "江南RICU",
             "3487": "江南产房NICU", "3452": "江南NICU"}

print(f"=== ICU-07 DVT预防率 · 科室 {DEPT} {DEPT_NAME.get(DEPT, '')} · {START} ~ {END} ===\n")

# ============================================================
# 1. 分母（生产函数）
# ============================================================
stays = get_icu_denominator_stays([DEPT], START, END)
stays.sort(key=lambda d: (d.get("icuAdmissionTime") or S, str(d.get("hisBed") or "")))
den_mrns = {s.get("mrn") for s in stays if s.get("mrn")}
den_rec, den_pat = len(stays), len(den_mrns)      # 人次 / 人数（人次仅用于对账）
# 页面分母 = 人数口径，一人一行（与 icu04_den、ICU-07 分母完全一致）
den_persons = dedup_persons(stays)

# ============================================================
# 2. 分子（生产函数，生产端已按患者去重 → 人数口径）
# ============================================================
dvt = get_dvt_prevention_patients([DEPT], START, END)
num_stays = dvt.get("num_stays", [])              # 已去重，一人一行
num_mrns = {s.get("mrn") for s in num_stays if s.get("mrn")}
num_pat = dvt.get("all_count", len(num_mrns))     # 人数（页面分子）
num_rec = dvt.get("all_stay_count", num_pat)      # 人次（仅审计对照）
assert len(num_stays) == num_pat, \
    f"分子明细 {len(num_stays)} 行 != 分子人数 {num_pat}"

# ============================================================
# 3. 修复前口径（旧代码逻辑，用于对账）
#    ZYBR 按 deptCode+时间窗过滤 + 纯医嘱名匹配（无途径/检验剔除）
#    注意：旧代码的 all_count = len(all_pids)，按 DataCenter pid 计，
#    且从不与分母队列求交 —— 这正是分子分母不同源的问题。
# ============================================================
dc = get_datacenter_db()
# 历次修复新增的物理预防关键词（修复前词表不含它们，对账基准须同样排除）
ADDED_MECH_KEYWORDS = ["气压治疗", "运动功能训练"]
# 修复前的物理预防关键词表
OLD_MECH_KEYWORDS = [k for k in MECH_DVT_KEYWORDS if k not in ADDED_MECH_KEYWORDS]


def old_numerator():
    """返回 (all_pids, 旧分子涉及的den侧mrns, ZYBR过滤池by_pid, 匹配到的医嘱)"""
    zybr = list(dc["VI_ICU_ZYBR"].find(
        {"deptCode": {"$in": [DEPT]}, "admitTime": {"$lte": E},
         "$or": [{"dischargeTime": {"$gte": S}},
                 {"dischargeTime": None},
                 {"dischargeTime": ""}]},
        {"pid": 1, "mrn": 1, "name": 1, "deptCode": 1}))
    by_pid = {d["pid"]: d for d in zybr if d.get("pid")}
    if not by_pid:
        return set(), set(), {}, []
    pat = {k: _keyword_regex(v) for k, v in (
        ("drug", DRUG_DVT_KEYWORDS), ("mech", OLD_MECH_KEYWORDS),
        ("all", DRUG_DVT_KEYWORDS + OLD_MECH_KEYWORDS + FILTER_KEYWORDS),
        ("flush", FLUSH_EXCLUDE_KEYWORDS))}
    orders = list(dc["VI_ICU_ZYYZ"].find(
        {"pid": {"$in": list(by_pid)},
         "orderName": {"$regex": pat["all"], "$options": "i"},
         "orderTime": {"$gte": S, "$lte": E},
         "status": {"$in": EXECUTED_ORDER_STATUSES}},
        {"pid": 1, "orderName": 1, "orderTime": 1}).limit(50000))
    drug_pids, mech_pids = set(), set()
    for o in orders:
        nm, pid = o.get("orderName", ""), o.get("pid", "")
        if not pid or not nm:
            continue
        if re.search(pat["drug"], nm, re.I) and not re.search(pat["flush"], nm, re.I):
            drug_pids.add(pid)
        if re.search(pat["mech"], nm, re.I):
            mech_pids.add(pid)
    all_pids = drug_pids | mech_pids
    den_side = {by_pid[p].get("mrn") for p in all_pids} & den_mrns
    return all_pids, den_side, by_pid, orders


old_all_pids, old_den_mrns, old_zybr_by_pid, old_orders = old_numerator()
# 修复前页面显示的分子 = len(all_pids)，与分母不同源，无法折算成人次
old_page_num = len(old_all_pids)
old_non_den_pids = sum(
    1 for p in old_all_pids
    if old_zybr_by_pid.get(p, {}).get("mrn") not in den_mrns)
old_den_pids = old_page_num - old_non_den_pids
# 逐行对账用：旧分子涉及的、且确实在分母里的患者
old_mrns = old_den_mrns
old_rec = sum(1 for s in stays if s.get("mrn") in old_mrns)

# ZYBR 过滤池（用于判断“是不是被 deptCode/时间窗过滤剔掉的”）
old_pids = set(old_zybr_by_pid)

# ZYBR 全量（不加科室过滤），用于解释差异
zy_all = list(dc["VI_ICU_ZYBR"].find({"mrn": {"$in": list(den_mrns)}},
                                     {"pid": 1, "mrn": 1, "name": 1,
                                      "deptCode": 1, "admitTime": 1,
                                      "dischargeTime": 1}))
zy_by_mrn = defaultdict(list)
pids_of_mrn = defaultdict(set)
for z in zy_all:
    zy_by_mrn[z.get("mrn")].append(z)
    if z.get("pid"):
        pids_of_mrn[z.get("mrn")].add(z.get("pid"))

# 一个 mrn 可能对应多个 pid（历史住院），全部纳入查询
mrn_of_pid = {}
for m, ps in pids_of_mrn.items():
    for p in ps:
        mrn_of_pid.setdefault(p, m)

orders_all = []
if mrn_of_pid:
    rx = _keyword_regex(DRUG_DVT_KEYWORDS + MECH_DVT_KEYWORDS + FILTER_KEYWORDS)
    orders_all = list(dc["VI_ICU_ZYYZ"].find(
        {"pid": {"$in": list(mrn_of_pid)},
         "orderName": {"$regex": rx, "$options": "i"},
         "orderTime": {"$gte": S, "$lte": E},
         "status": {"$in": EXECUTED_ORDER_STATUSES}},
        {"pid": 1, "orderName": 1, "orderTime": 1,
         "exeMethod": 1, "exeMethodCode": 1}).limit(200000))

lab_re = re.compile(DVT_LAB_FALSE_MATCH_PATTERN, re.I)
orders_by_mrn = defaultdict(list)
for o in orders_all:
    pid, nm = o.get("pid"), o.get("orderName") or ""
    m = mrn_of_pid.get(pid)
    if not m or m not in den_mrns or not nm:
        continue
    kind = classify_dvt_order(nm, o.get("exeMethod"), o.get("exeMethodCode"))
    orders_by_mrn[m].append({
        "name": nm, "time": o.get("orderTime"), "kind": kind,
        "exe": o.get("exeMethod") or "", "lab": bool(lab_re.search(nm)),
    })

# 完全靠历次新增关键词才命中物理预防的医嘱（旧关键词表里没有它们）
mech_kw_re = re.compile(
    _keyword_regex([k for k in MECH_DVT_KEYWORDS if k not in ADDED_MECH_KEYWORDS]), re.I)


def only_qiya(mrn):
    """该患者的物理预防是否完全依赖历次修复新增的关键词（气压治疗/运动功能训练）。"""
    has_mech = any(x["kind"] == "mech" for x in orders_by_mrn.get(mrn, []))
    if not has_mech:
        return False
    return not any(x["kind"] == "mech" and mech_kw_re.search(x["name"])
                   for x in orders_by_mrn.get(mrn, []))


def delta_reason(mrn):
    """新口径纳入、旧口径没纳入的原因。"""
    zs = zy_by_mrn.get(mrn, [])
    pids = {z.get("pid") for z in zs if z.get("pid")}
    if pids and not (pids & old_pids):
        depts = {str(z.get("deptCode")) for z in zs}
        if DEPT not in depts:
            return (f"旧口径按 VI_ICU_ZYBR.deptCode={DEPT} 过滤被剔除"
                    f"（该表每个 mrn 只保留最近一次科室记录，Z值= {'/'.join(sorted(depts))}，"
                    f"该患者已转出本科室）")
        return (f"旧口径按 VI_ICU_ZYBR 时间窗过滤被剔除"
                f"（deptCode={DEPT} 但 ZYBR 的入/出院时间与 {START[:7]} 不重叠）")
    if only_qiya(mrn):
        return "旧关键词表缺「气压治疗/运动功能训练」—— 本院物理预防医嘱，旧口径漏掉"
    return "旧口径用纯医嘱名匹配且分子分母不同源，该患者被连带漏掉"


def removed_reason(mrn):
    """旧口径纳入、新口径剔除的原因。"""
    recs = orders_by_mrn.get(mrn, [])
    if recs and any(x["lab"] for x in recs) and not any(x["kind"] for x in recs):
        return "关键词「肝素」误命中检验项目（肝素结合蛋白HBP/降钙素原PCT二联检），非预防措施"
    if recs and not any(x["kind"] for x in recs):
        bad = [x["exe"] for x in recs if x["exe"]]
        return f"给药途径属管路维护/体外循环（{'/'.join(sorted(set(bad))) or '无途径'}），非 DVT 预防"
    return "旧口径命中、新口径未纳入（需人工复核）"


added = num_mrns - old_mrns
removed = old_mrns - num_mrns
kept = num_mrns & old_mrns
added_rec = sum(1 for s in stays if s.get("mrn") in added)
removed_rec = sum(1 for s in stays if s.get("mrn") in removed)


def fmt(t):
    return t.strftime("%Y-%m-%d %H:%M") if isinstance(t, dt) else ""


stays_by_mrn = defaultdict(list)
for s in stays:
    stays_by_mrn[s.get("mrn")].append(s)

# ============================================================
# 4. 组装表格
# ============================================================
den_rows = []
for i, d in enumerate(den_persons, 1):        # 人数口径：一人一行
    m = d.get("mrn")
    recs = orders_by_mrn.get(m, [])
    kinds = sorted({x["kind"] for x in recs if x["kind"]})
    admit = d.get("icuAdmissionTime")
    in_num = m in num_mrns
    den_rows.append({
        "序号": i,
        "住院号(mrn)": m,
        "姓名": d.get("name", ""),
        "床号": d.get("hisBed", ""),
        "科室": d.get("deptCode", ""),
        "入ICU时间": fmt(admit),
        "出ICU时间": fmt(d.get("icuDischargeTime")) or "仍在科",
        "在科天数": (round(((d.get("icuDischargeTime") or E) - admit).total_seconds() / 86400, 1)
                     if admit else ""),
        "患者类型": "原有" if (admit and admit < S) else "新入",
        "纳入分母": "是",
        "是否纳入分子": "是" if in_num else "否",
        "预防方式": "/".join(kinds) if in_num else "",
        "医嘱条数": len(recs) if in_num else 0,
        "医嘱示例": " | ".join(sorted({x["name"] for x in recs})[:3]) if in_num else "",
        "9月本科室在科次数": len(stays_by_mrn[m]),
        "修复前是否分子": "是" if m in old_mrns else "否",
    })

num_rows = []
for i, s in enumerate(num_stays, 1):
    m = s.get("mrn")
    recs = orders_by_mrn.get(m, [])
    kinds = sorted({x["kind"] for x in recs if x["kind"]})
    ad, dd = s.get("icuAdmissionTime"), s.get("icuDischargeTime")
    num_rows.append({
        "序号": i,
        "住院号(mrn)": m,
        "姓名": s.get("name", ""),
        "床号": s.get("hisBed", ""),
        "科室": s.get("deptCode", ""),
        "预防方式": s.get("measure", ""),
        "预防细分": "/".join(kinds),
        "入ICU时间": fmt(ad),
        "出ICU时间": fmt(dd) or "仍在科",
        "患者类型": "原有" if (ad and ad < S) else "新入",
        "医嘱条数": s.get("order_count", 0),
        "医嘱示例": " | ".join(s.get("matched_orders", [])),  # db 已控制≤6条且药物3+物理3两类都露出
        "有药物预防": "是" if any(k in ("drug",) for k in kinds) else "否",
        "有物理预防": "是" if any(k in ("mech",) for k in kinds) else "否",
        "修复前是否分子": "是" if m in old_mrns else "否",
        "差异说明": delta_reason(m) if m in added else "",
    })

miss_rows = []
for i, m in enumerate(sorted(den_mrns - num_mrns, key=str), 1):
    recs = stays_by_mrn[m]
    zy = zy_by_mrn.get(m, [{}])[0]
    miss_rows.append({
        "序号": i,
        "住院号(mrn)": m,
        "姓名": recs[0].get("name", ""),
        "床号": recs[0].get("hisBed", ""),
        "科室": recs[0].get("deptCode", ""),
        "入ICU时间": "; ".join(fmt(r.get("icuAdmissionTime")) for r in recs),
        "出ICU时间": "; ".join(fmt(r.get("icuDischargeTime")) or "仍在科" for r in recs),
        "本科室在科次数": len(recs),
        "匹配到的相关医嘱条数": len(orders_by_mrn.get(m, [])),
        "相关医嘱": " | ".join(sorted({x["name"] for x in orders_by_mrn.get(m, [])})[:3]),
        "原因": ("统计期内无「已执行」状态的 DVT 预防医嘱（药物/物理）"
                 if not orders_by_mrn.get(m) else
                 "命中关键词但均被剔除（检验项目/管路维护/非执行状态）"),
        "ZYBR最近科室": zy.get("deptCode", ""),
    })

# 差异对账
def why_counts(mrn_set, reason_fn):
    c = Counter()
    for m in mrn_set:
        c[reason_fn(m)] += 1
    return c

add_why = why_counts(added, delta_reason)
rem_why = why_counts(removed, removed_reason)
mech_dist = Counter(s.get("measure", "") for s in num_stays)

# ============================================================
# 5. 写 Excel
# ============================================================
OUT = PROJECT_ROOT / f"ICU07_DVT预防率_{DEPT}_{DEPT_NAME.get(DEPT, '')}_{START[:7]}_分子分母明细.xlsx"

wb = Workbook()
ws0 = wb.active
assert ws0 is not None
ws0.title = "口径说明与汇总"
thin = Side(style="thin", color="D0D0D0")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
HDR_FILL = PatternFill("solid", fgColor="1F4E79")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=13, color="1F4E79")
WARN_FILL = PatternFill("solid", fgColor="FCE4D6")
YES_FILL = PatternFill("solid", fgColor="E2EFDA")
NO_FILL = PatternFill("solid", fgColor="F2F2F2")


def write_sheet(ws, title, subtitle, rows, highlight_col=None):
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = subtitle
    ws["A2"].font = Font(size=9, color="808080")
    if not rows:
        return
    cols = list(rows[0].keys())
    hr = 4
    for c, name in enumerate(cols, 1):
        cell = ws.cell(hr, c, name)
        cell.fill, cell.font, cell.border = HDR_FILL, HDR_FONT, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for r, row in enumerate(rows, hr + 1):
        for c, name in enumerate(cols, 1):
            cell = ws.cell(r, c, row.get(name, ""))
            cell.border = BORDER
            cell.font = Font(size=10)
            cell.alignment = Alignment(vertical="center")
        if highlight_col and highlight_col in row:
            v = str(row.get(highlight_col, ""))
            fill = (YES_FILL if v.startswith("是") else
                    WARN_FILL if "漏" in v else
                    NO_FILL if v in ("否", "-") else None)
            if fill:
                ws.cell(r, cols.index(highlight_col) + 1).fill = fill
    for c, name in enumerate(cols, 1):
        width = max(len(str(name)) * 2, 10)
        for row in rows[:200]:
            width = max(width, min(len(str(row.get(name, ""))) + 2, 46))
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.freeze_panes = ws.cell(hr + 1, 1)
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(cols))}{hr + len(rows)}"


# 人数口径为页面口径（分子分母均按患者数去重）
ratio_new = num_pat / den_pat * 100 if den_pat else 0
ratio_stay = num_rec / den_rec * 100 if den_rec else 0     # 人次口径，仅对照
# 修复前页面分子 = len(all_pids)（pid 口径，与分母不同源），比值按旧页面的算法 = 分子/分母
ratio_old = old_page_num / den_rec * 100 if den_rec else 0

summary = [
    ("统计对象", f"科室 {DEPT} {DEPT_NAME.get(DEPT, '')}"),
    ("统计周期", f"{START} ~ {END}"),
    ("", ""),
    ("【修复后口径（打包后页面使用的口径）】", ""),
    ("★ 比率（人数口径）", f"{ratio_new:.2f}%   ({num_pat}/{den_pat})"),
    ("分母（去重患者数）", den_pat),
    ("分子（去重患者数）", num_pat),
    ("", ""),
    ("【对照：人次口径】", ""),
    ("分母（在科人次）", den_rec),
    ("分子（在科人次）", num_rec),
    ("比率", f"{ratio_stay:.2f}%   ({num_rec}/{den_rec})"),
    ("预防方式分布（按患者数）", "  ".join(f"{k} {v}" for k, v in sorted(mech_dist.items()))),
    ("", ""),
    ("【修复前口径（旧 bug，页面上显示的值）】", ""),
    ("分子", f"{old_page_num}  ——  按 DataCenter pid 计，"
     f"取自 VI_ICU_ZYBR.deptCode={DEPT}，从不与分母队列求交"),
    ("比率", f"{ratio_old:.2f}%   ({old_page_num}/{den_rec})"),
    ("分子中不属于 9 月分母的 pid", f"{old_non_den_pids} 个"
     f"（分子分母不同源：分子按 ZYBR 科室取，分母按 patient 在科区间取）"),
    ("分母里被旧口径漏掉的患者", f"{len(added)} 人 / {added_rec} 人次"),
    ("", ""),
    ("【新旧差异对账】", ""),
    ("两侧都有", f"{len(kept)} 人 / {num_rec - added_rec} 人次"),
    ("新增（旧口径漏计）", f"{len(added)} 人 / {added_rec} 人次"),
]
for k, v in add_why.most_common():
    summary.append((f"    └ {k}", f"{v} 人"))
summary += [
    ("剔除（旧口径多算）", f"{len(removed)} 人 / {removed_rec} 人次"),
]
for k, v in rem_why.most_common():
    summary.append((f"    └ {k}", f"{v} 人"))
summary += [
    ("", ""),
    ("【分母核查】", ""),
    ("科室归属", f"{den_rec} 条在科记录 deptCode 全部 = {DEPT}，无其他科室患者"
     f"（去重: {sorted({str(s.get('deptCode')) for s in stays})}）"),
    ("人次>人数原因", f"分母原始 {den_rec} 人次中有 {den_rec - den_pat} 条属同一患者"
     f"9 月内 2 次入科（转出后再次入科）；指标定义分子分母均为「患者数」，"
     f"故按人数计 —— 分母 {den_pat} 人、分子 {num_pat} 人"),
    ("", ""),
    ("分母数据源", "SmartCare.patient：deptCode∈本科室 且 status!=invalid 且 "
     "icuAdmissionTime<=期末 且 (icuDischargeTime>=期初 或 未出院)"),
    ("分子数据源", "DataCenter.VI_ICU_ZYYZ：统计期内 status∈已执行/已停止 的医嘱，"
     "医嘱名匹配 抗凝药/物理预防 关键字"),
    ("分子 pid 来源", "按分母队列的 mrn 反查 VI_ICU_ZYBR（不加 deptCode/时间窗过滤 —— "
     "该表每 mrn 只保留最近一次科室记录，转出本科室的患者会被误剔除）"),
    ("剔除的假阳性", "① 检验项目(肝素结合蛋白HBP/降钙素原PCT二联检) "
     "② 管路维护途径(有创压用/封管用/冲管) ③ 血液净化管路抗凝"),
    ("新增的物理预防", "①「气压治疗费 单肢 bid」= 间歇充气加压(IPC)，本院的 IPC 收费医嘱 "
                    "②「运动功能训练」= 早期运动训练（院方口径 2026-10）"),
    ("比率口径", "分子分母均按「人数」（按 mrn 去重的患者数）—— 与 INDICATORS_CONFIG "
     "中 ICU-04/07/09/10「患者数」的定义一致；同一患者当月多次入科只算一人"),
]

ws0["A1"] = (f"ICU-07 DVT预防率 · 分子分母明细 · 科室 {DEPT} "
             f"{DEPT_NAME.get(DEPT, '')} · {START[:7]}")
ws0["A1"].font = Font(bold=True, size=14, color="1F4E79")
r = 3
for k, v in summary:
    is_hdr = k.startswith("【")
    ws0.cell(r, 1, k).font = Font(bold=is_hdr, size=10,
                                  color="1F4E79" if is_hdr else "000000")
    c = ws0.cell(r, 2, v)
    c.font = Font(size=10)
    c.alignment = Alignment(vertical="center", wrap_text=True)
    if isinstance(v, str) and "★" in str(k):
        ws0.cell(r, 1).font = Font(bold=True, size=11, color="C00000")
        c.font = Font(size=12, bold=True, color="C00000")
    r += 1
ws0.column_dimensions["A"].width = 46
ws0.column_dimensions["B"].width = 96

r += 2
ws0.cell(r, 1, "旧口径漏计的分子患者（修复前后对比）").font = Font(bold=True, size=11, color="C00000")
r += 1
for c, h in enumerate(["住院号(mrn)", "姓名", "床号", "ZYBR最近科室", "漏计原因"], 1):
    cell = ws0.cell(r, c, h)
    cell.fill, cell.font, cell.border = HDR_FILL, HDR_FONT, BORDER
for m in sorted(added, key=str):
    s0 = stays_by_mrn[m][0]
    zy = zy_by_mrn.get(m, [{}])[0]
    r += 1
    for c, v in enumerate([m, s0.get("name", ""), s0.get("hisBed", ""),
                           zy.get("deptCode", ""), delta_reason(m)], 1):
        cell = ws0.cell(r, c, v)
        cell.border = BORDER
        cell.font = Font(size=10)
        cell.fill = WARN_FILL

r += 2
ws0.cell(r, 1, "旧口径多算、新口径剔除的患者").font = Font(bold=True, size=11, color="C00000")
r += 1
for c, h in enumerate(["住院号(mrn)", "姓名", "床号", "剔除原因", "相关医嘱"], 1):
    cell = ws0.cell(r, c, h)
    cell.fill, cell.font, cell.border = HDR_FILL, HDR_FONT, BORDER
for m in sorted(removed, key=str):
    s0 = stays_by_mrn[m][0]
    r += 1
    for c, v in enumerate([m, s0.get("name", ""), s0.get("hisBed", ""),
                           removed_reason(m),
                           " | ".join(sorted({x["name"] for x in orders_by_mrn.get(m, [])})[:3])], 1):
        cell = ws0.cell(r, c, v)
        cell.border = BORDER
        cell.font = Font(size=10)
        cell.fill = YES_FILL
for col, w in zip("ABCDE", (16, 12, 8, 30, 78)):
    ws0.column_dimensions[col].width = w

write_sheet(wb.create_sheet("分母明细(按人数)"),
            f"分母明细 —— {den_pat} 人（原始在科 {den_rec} 人次）",
            f"科室 {DEPT} {DEPT_NAME.get(DEPT, '')} · {START} ~ {END} · "
            f"数据源 SmartCare.patient · 一人一行 · 绿色=已纳入分子",
            den_rows, highlight_col="是否纳入分子")

write_sheet(wb.create_sheet("分子明细(按人数)"),
            f"分子明细 —— {num_pat} 人  →  {ratio_new:.2f}%"
            f"（人次对照 {num_rec}/{den_rec} = {ratio_stay:.2f}%）",
            f"科室 {DEPT} {DEPT_NAME.get(DEPT, '')} · {START} ~ {END} · "
            f"数据源 DataCenter.VI_ICU_ZYYZ 已执行 DVT 预防医嘱",
            num_rows, highlight_col="修复前是否分子")

write_sheet(wb.create_sheet("未纳入分子的患者"),
            f"分母中无 DVT 预防医嘱的患者 —— {den_pat - num_pat} 人",
            f"科室 {DEPT} {DEPT_NAME.get(DEPT, '')} · {START} ~ {END}",
            miss_rows)

wb.save(OUT)

# --- CSV（UTF-8 BOM，Excel 可直接打开）---
for sheet_name, rows in (("分母明细", den_rows), ("分子明细", num_rows),
                         ("未纳入分子", miss_rows)):
    if not rows:
        continue
    csv_path = OUT.with_name(f"{OUT.stem}_{sheet_name}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

print("已导出:")
print(" ", OUT)
for p in sorted(OUT.parent.glob(OUT.stem + "*.csv")):
    print(" ", p)
print()
print(f"分母（人数口径·页面）    {den_pat} 人   （原始在科 {den_rec} 人次）")
print(f"分子（人数口径·页面）    {num_pat} 人   → {ratio_new:.2f}%")
print(f"分子分母（人次口径对照） {num_rec} / {den_rec} → {ratio_stay:.2f}%")
print(f"分子（修复前，页面值）   {old_page_num} 人(pid口径) → {ratio_old:.2f}%")
print(f"  └ 其中 mrn 属于 9 月分母的 {old_den_pids} 个 pid"
      f" → 去重 {len(old_mrns)} 人 / {old_rec} 人次；不在分母的 {old_non_den_pids} 个 pid")
print(f"预防方式分布             {dict(mech_dist)}")
print(f"\n新增 {len(added)} 人:")
for k, v in add_why.most_common():
    print(f"   {v:>3} 人  {k}")
print(f"剔除 {len(removed)} 人:")
for k, v in rem_why.most_common():
    print(f"   {v:>3} 人  {k}")
