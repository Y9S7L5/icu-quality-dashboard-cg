# -*- coding: utf-8 -*-
"""ICU-05 cg 简化口径（bundle_cg）纯函数测试 — 无 Mongo。"""
from datetime import datetime, timedelta

from config.bundle_rules import CG_CONFIRM_DIAG_KEYWORDS, CG_SHOCK_DIAG_KEYWORDS
from scoring.bundle_cg import (
    filter_cg_denominator_stays,
    evaluate_cg_windows,
    R_BLOOD_GAS, R_SPUTUM, R_BLOOD_CULTURE, R_ANTIBIOTIC, R_RECHECK,
)

# 月边界（naive UTC 帧，与 tzutil utc_day_start/end 同构的简化值）
M_START = datetime(2026, 6, 1)
M_END = datetime(2026, 6, 30, 23, 59, 59)


def _stay(diagnosis_list=None, admit=datetime(2026, 6, 10, 8), discharge=None,
          clinical="脓毒性休克;脓毒血症"):
    return {
        "_id": "id1", "mrn": "M001", "hisPid": "H001",
        "icuAdmissionTime": admit, "icuDischargeTime": discharge,
        "deptCode": "ICU01",
        "diagnosisHistoryList": diagnosis_list or [],
        "clinicalDiagnosis": clinical,
    }


# ============================================================
# 分母筛选（两关：clinicalDiagnosis 命中 → 月归属）
# ============================================================

def test_hit_shock_keywords_in_month_included():
    stays = [_stay([{"diagnosis": "感染性休克", "time": datetime(2026, 6, 12)}])]
    out = filter_cg_denominator_stays(stays, M_START, M_END)
    assert len(out) == 1
    assert out[0]["cg_diag_hit_time"] == datetime(2026, 6, 12)
    assert out[0]["cg_diag_path"] == "diagnosis_history_list"

    stays = [_stay([{"diagnosis": "脓毒性休克", "time": datetime(2026, 6, 5)}])]
    assert len(filter_cg_denominator_stays(stays, M_START, M_END)) == 1


def test_clinical_gate_blocks_history_hit():
    """clinicalDiagnosis 不命中 → 即使诊断列表有月内命中也不入组（第一关）。"""
    stays = [_stay([{"diagnosis": "脓毒性休克", "time": datetime(2026, 6, 12)}],
                   clinical="肺部感染;Ⅱ型呼吸衰竭")]
    assert filter_cg_denominator_stays(stays, M_START, M_END) == []


def test_old_confirm_keywords_only_not_included():
    """clinicalDiagnosis/列表只有 脓毒血症/败血症 → 第一关不过，不进分母。"""
    stays = [_stay([{"diagnosis": "脓毒血症", "time": datetime(2026, 6, 12)}],
                   clinical="脓毒血症"),
             _stay([{"diagnosis": "败血症", "time": datetime(2026, 6, 12)}],
                   clinical="败血症")]
    assert filter_cg_denominator_stays(stays, M_START, M_END) == []


def test_hit_time_outside_month_excluded():
    """有诊断列表 → 照旧逻辑：月内无命中不入组（不回退入科时间）。"""
    stays = [_stay([{"diagnosis": "感染性休克", "time": datetime(2026, 7, 1)}]),
             _stay([{"diagnosis": "脓毒性休克", "time": datetime(2026, 5, 31)}])]
    assert filter_cg_denominator_stays(stays, M_START, M_END) == []


def test_no_history_falls_back_to_admission_month():
    """无诊断列表 + clinicalDiagnosis 命中 → 按 icuAdmissionTime 归月。"""
    # 入科在月内 → 入组，命中时间=入科时间，路径=fallback
    stays = [_stay(None, admit=datetime(2026, 6, 10, 8))]
    out = filter_cg_denominator_stays(stays, M_START, M_END)
    assert len(out) == 1
    assert out[0]["cg_diag_hit_time"] == datetime(2026, 6, 10, 8)
    assert out[0]["cg_diag_path"] == "clinical_diagnosis_fallback"
    assert "脓毒性休克" in out[0]["cg_diag_text"]
    # 入科在月外 → 不入本月分母（归入科月）
    stays = [_stay([], admit=datetime(2026, 5, 20)),
             _stay(None, admit=datetime(2026, 7, 2))]
    assert filter_cg_denominator_stays(stays, M_START, M_END) == []


def test_no_history_no_clinical_hit_excluded():
    """无诊断列表且 clinicalDiagnosis 不命中 → 出局。"""
    stays = [_stay(None, clinical="肺部感染")]
    assert filter_cg_denominator_stays(stays, M_START, M_END) == []


def test_earliest_hit_wins():
    stays = [_stay([
        {"diagnosis": "感染性休克", "time": datetime(2026, 6, 20)},
        {"diagnosis": "脓毒性休克", "time": datetime(2026, 6, 3)},
    ])]
    out = filter_cg_denominator_stays(stays, M_START, M_END)
    assert out[0]["cg_diag_hit_time"] == datetime(2026, 6, 3)


def test_keywords_constant_is_exactly_two():
    """新关键词仅两词；旧确诊关键词元组保持不变（default 不受影响）。"""
    assert CG_SHOCK_DIAG_KEYWORDS == ("脓毒性休克", "感染性休克")
    assert CG_CONFIRM_DIAG_KEYWORDS == ("脓毒血症", "败血症", "感染性休克")


# ============================================================
# 在科期间判定（不限入科后 1/3/6h）
# ============================================================

T0 = datetime(2026, 6, 10, 8)
STAY_END = datetime(2026, 6, 17, 8)  # 在科 7 天


def _ev(bg=None, sput=None, bc=None, abx=None):
    return (bg or [], sput or [], bc or [], abx)


def _all_in_1h():
    return _ev(bg=[T0 + timedelta(minutes=10)],
               sput=[T0 + timedelta(minutes=15)],
               bc=[T0 + timedelta(minutes=20)],
               abx=T0 + timedelta(minutes=30))


def test_all_four_and_two_bg_all_windows_pass():
    bg, sput, bc, abx = _all_in_1h()
    bg = bg + [T0 + timedelta(hours=2)]  # 复查血气（≥2 条）
    r = evaluate_cg_windows(T0, STAY_END, bg, sput, bc, abx)
    assert r["bundle_1h"]["finish"] is True
    assert r["bundle_3h"]["finish"] is True
    assert r["bundle_6h"]["finish"] is True
    assert r["bundle_6h"]["reasons"] == []


def test_evidence_days_after_admission_still_counts():
    """在科期间内、入科数天后的医嘱也算（不再限定 1/3/6h）。
    1h/3h/6h 三窗口判定一致。"""
    bg = [T0 + timedelta(minutes=10), T0 + timedelta(days=3)]   # 血气 2 条
    sput = [T0 + timedelta(days=3, minutes=5)]                  # 痰培养第 3 天
    bc = [T0 + timedelta(days=3, minutes=10)]                   # 血培养第 3 天
    abx = T0 + timedelta(days=3, minutes=15)
    r = evaluate_cg_windows(T0, STAY_END, bg, sput, bc, abx)
    assert r["bundle_1h"]["finish"] is True
    assert r["bundle_3h"]["finish"] is True
    assert r["bundle_6h"]["finish"] is True
    assert r["bundle_6h"]["blood_gas_count"] == 2


def test_evidence_after_discharge_ignored():
    """出科后的医嘱不算（窗口上界=出科时间）。"""
    after = STAY_END + timedelta(days=1)
    r = evaluate_cg_windows(T0, STAY_END, [after], [after], [after], after)
    assert r["bundle_1h"]["finish"] is False
    assert R_BLOOD_GAS in r["bundle_1h"]["reasons"]
    assert R_SPUTUM in r["bundle_1h"]["reasons"]
    assert R_BLOOD_CULTURE in r["bundle_1h"]["reasons"]
    assert R_ANTIBIOTIC in r["bundle_1h"]["reasons"]


def test_6h_single_bg_fails_recheck_only():
    bg, sput, bc, abx = _all_in_1h()  # 仅 1 条血气
    r = evaluate_cg_windows(T0, STAY_END, bg, sput, bc, abx)
    assert r["bundle_1h"]["finish"] is True
    assert r["bundle_3h"]["finish"] is True
    assert r["bundle_6h"]["finish"] is False
    assert r["bundle_6h"]["reasons"] == [R_RECHECK]


def test_missing_sputum_single_reason():
    _, _, bc, abx = _all_in_1h()
    bg = [T0 + timedelta(minutes=10), T0 + timedelta(hours=2)]
    r = evaluate_cg_windows(T0, STAY_END, bg, [], bc, abx)
    assert r["bundle_3h"]["finish"] is False
    assert r["bundle_3h"]["reasons"] == [R_SPUTUM]
    assert R_BLOOD_GAS not in r["bundle_3h"]["reasons"]


def test_all_missing_reasons():
    r = evaluate_cg_windows(T0, STAY_END, [], [], [], None)
    for n in (1, 3, 6):
        assert r[f"bundle_{n}h"]["finish"] is False
    assert r["bundle_1h"]["reasons"] == [R_BLOOD_GAS, R_SPUTUM,
                                         R_BLOOD_CULTURE, R_ANTIBIOTIC]
    assert R_RECHECK in r["bundle_6h"]["reasons"]


def test_stay_boundaries_inclusive():
    """入科时刻与出科时刻均算命中（闭区间）。"""
    r = evaluate_cg_windows(
        T0,
        STAY_END,
        [T0, STAY_END],                 # 起点 + 出科端点（复查计 2 条）
        [T0],                           # 恰在起点
        [STAY_END],                     # 恰在出科端点
        T0,                             # 起点
    )
    assert r["bundle_1h"]["finish"] is True
    assert r["bundle_3h"]["finish"] is True
    assert r["bundle_6h"]["finish"] is True
    assert r["bundle_6h"]["blood_gas_count"] == 2


def test_evidence_before_t0_ignored():
    before = T0 - timedelta(hours=1)
    r = evaluate_cg_windows(T0, STAY_END, [before], [before], [before],
                            T0 - timedelta(minutes=5))
    assert r["bundle_1h"]["finish"] is False
    assert R_BLOOD_GAS in r["bundle_1h"]["reasons"]
    assert R_ANTIBIOTIC in r["bundle_1h"]["reasons"]
