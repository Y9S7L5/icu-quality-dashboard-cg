# -*- coding: utf-8 -*-
"""复现 ICU-07 分子口径的各个中间阶段，解释 57.24% → 71.03% 的差异来源。"""
import sys, io
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

DEPT, S, E = ["3439"], "2026-09-01", "2026-09-30"
den = db.get_icu_denominator_stays(DEPT, S, E)
den_rec, den_pat = len(den), len({d.get("mrn") for d in den})
print(f"分母固定: {den_rec} 人次 / {den_pat} 人\n")

ORIG_MECH = list(db.MECH_DVT_KEYWORDS)
ORIG_CLASSIFY = db.classify_dvt_order


def make_classify(with_lab: bool, with_route: bool):
    """with_*=False 时退回旧代码的纯医嘱名匹配（无检验/途径剔除）。"""
    real = ORIG_CLASSIFY

    def legacy(nm, exe=None, code=None):
        if not nm:
            return ""
        rx = db._dvt_patterns()
        if with_lab and rx["lab"].search(nm):
            return ""
        if rx["drug"].search(nm):
            if rx["flush"].search(nm):
                return ""
            if not with_route:
                return "drug"
        elif not with_route:
            pass
        if not with_route:
            # 旧代码：药物命中且含封管关键词 → continue，连机械也不判
            if rx["drug"].search(nm) and rx["flush"].search(nm):
                return ""
            if rx["drug"].search(nm):
                return "drug"
            if rx["mech"].search(nm):
                return "mech"
            if rx["filter"].search(nm):
                return "filter"
            return ""
        return real(nm, exe, code)

    return legacy


def run(label, mech_keywords, classify_fn):
    db.MECH_DVT_KEYWORDS = list(mech_keywords)
    db._DVT_RES = None
    db.classify_dvt_order = classify_fn
    r = db.get_dvt_prevention_patients(DEPT, S, E)
    n_rec, n_pat = r.get("all_stay_count", 0), r.get("all_count", 0)
    ms = r.get("num_stays", [])
    mech = len({s["mrn"] for s in ms if "物理" in s.get("measure", "")})
    drug = len({s["mrn"] for s in ms if "药物" in s.get("measure", "")})
    print(f"{label}")
    print(f"   分子 {n_rec} 人次 / {n_pat} 人   物理预防 {mech} 人 / 药物预防 {drug} 人"
          f"   →  {n_rec / den_rec * 100:.2f}%  (人次)  "
          f"{n_pat / den_pat * 100:.2f}%  (人数)")
    return n_rec, n_pat


real = ORIG_CLASSIFY
legacy_no_lab = make_classify(with_lab=False, with_route=False)
legacy_with_lab = make_classify(with_lab=True, with_route=False)

# 注：本脚本的 pid 池始终是「分母队列反查」的新口径，
# 无法在这里复现真正的页面原值 66人/45.52%（那需要重放 ZYBR 科室+时间窗过滤，
# 见 icu07_export_detail.py 的 old_numerator()）。
b = run("① 旧关键词 + 纯名称匹配（pid 池已是新口径）= 当时说的 57.24%",
        [k for k in ORIG_MECH if k != "气压治疗"], legacy_no_lab)

c = run("② + 剔检验假匹配 = 当时说的 52.41%",
        [k for k in ORIG_MECH if k != "气压治疗"], legacy_with_lab)

d = run("③ + 加「气压治疗」关键词（本院 IPC 收费医嘱）",
        ORIG_MECH, legacy_with_lab)

e = run("④ + 看给药途径剔管路维护/血液净化 = 最终口径",
        ORIG_MECH, real)

db.MECH_DVT_KEYWORDS = ORIG_MECH
db._DVT_RES = None
db.classify_dvt_order = ORIG_CLASSIFY
print("\n最终口径复核:", db.get_dvt_prevention_patients(DEPT, S, E).get("all_stay_count"), "人次")
