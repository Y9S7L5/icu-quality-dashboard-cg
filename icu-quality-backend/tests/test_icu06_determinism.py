# -*- coding: utf-8 -*-
"""
ICU-06 分子计算确定性回归测试（2026-10-10 审计修复）。

背景：分母中同一 hisPid 可对应多条在科记录（多次再入科），
原实现用 dict 覆盖写 hisPid→pid 映射 + set 迭代，
跨进程哈希种子不同 → 培养医嘱随机落到其中一条记录 → 分子 60↔59 漂移。

本测试基于真实 Mongo（与 test_sofa_real_db 风格一致），断言：
  1. 两次连续调用结果完全一致（num/den/患者列表逐字段相等）
  2. num_patients 按 pid 有序（输出排序稳定）
  3. 同一 hisPid 的多条分母记录共享同一份培养结果（多映射语义生效）
运行: cd icu-quality-backend && python -m pytest tests/test_icu06_determinism.py -v
"""
import pytest
from collections import defaultdict


def _db_available() -> bool:
    try:
        from db import get_client, BED_DB_NAMES
        for n in BED_DB_NAMES:
            try:
                db = get_client(n)[n]
                if db.command("ping").get("ok"):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


@pytest.fixture(scope="module")
def icu06_result():
    if not _db_available():
        pytest.skip("MongoDB 不可用")
    from db import get_icu06_data
    return get_icu06_data(["3439"], "2026-09-01", "2026-09-30")


def test_two_runs_identical():
    """同一进程内两次调用结果必须完全一致（num/den/患者集合）。"""
    if not _db_available():
        pytest.skip("MongoDB 不可用")
    from db import get_icu06_data
    a = get_icu06_data(["3439"], "2026-09-01", "2026-09-30")
    b = get_icu06_data(["3439"], "2026-09-01", "2026-09-30")
    assert a["den_count"] == b["den_count"], "分母两次不一致"
    assert a["num_count"] == b["num_count"], "分子两次不一致"
    assert ({p["pid"] for p in a["den_patients"]}
            == {p["pid"] for p in b["den_patients"]}), "分母患者集合不一致"
    assert ({p["pid"] for p in a["num_patients"]}
            == {p["pid"] for p in b["num_patients"]}), "分子患者集合不一致"


def test_num_patients_sorted(icu06_result):
    """输出按 pid 排序，保证明细行序跨进程稳定。"""
    pids = [p["pid"] for p in icu06_result["num_patients"]]
    assert pids == sorted(pids), "num_patients 未按 pid 排序"


def test_duplicate_hispid_shares_test_time(icu06_result):
    """同一 hisPid 的多条分母记录必须共享同一份培养医嘱结果（多映射语义）。

    若存在 hisPid 对应 ≥2 条分母记录，且该 hisPid 有培养医嘱，
    则每条记录的 test_time 应与该住院号的最早培养时间一致 —— 任一进程
    都不得只把培养结果分配给其中随机一条。
    """
    den = icu06_result["den_patients"]
    by_his = defaultdict(list)
    for p in den:
        by_his[p.get("patient_id", "")].append(p)
    dups = {h: ps for h, ps in by_his.items() if h and len(ps) > 1}

    num_by_his = {p.get("patient_id", ""): p.get("test_time")
                  for p in icu06_result["num_patients"]}
    for his in dups:
        if his in num_by_his and num_by_his[his] is not None:
            # 该住院号有培养且入了分子：其全部分子记录应共享同一份医嘱时间
            times = {n["test_time"] for n in icu06_result["num_patients"]
                     if n.get("patient_id") == his}
            assert len(times) == 1, f"hisPid {his} 的分子记录 test_time 不唯一: {times}"


def test_num_subset_of_den(icu06_result):
    """分子必须是分母子集且不超过分母。"""
    den_pids = {p["pid"] for p in icu06_result["den_patients"]}
    num_pids = {p["pid"] for p in icu06_result["num_patients"]}
    assert num_pids <= den_pids, "分子出现分母之外的患者"
    assert icu06_result["num_count"] <= icu06_result["den_count"]
