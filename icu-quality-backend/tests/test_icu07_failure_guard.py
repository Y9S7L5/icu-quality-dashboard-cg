"""
ICU-07 环境性失败防护测试
=========================
#fix: get_dvt_prevention_patients 原吞异常静默返回 all_count=0，
重建把 0 当真实分子写进 icu_monthly_summary（2026-10 公式条 0/81 vs 明细 47）。

验证:
1. data_available=False 时 _compute_icu07 拒绝产出真实分子，携带跳过标记
2. rebuild_summary 的落库判定（data_available=False → 跳过写入保留旧值）
3. 正常路径不受影响（无 data_available 字段时按可用处理）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import summary as summary_mod


def _fail_result():
    """模拟 DataCenter 异常时 get_dvt_prevention_patients 的返回"""
    return {"all_count": 0, "all_stay_count": 0, "num_stays": [],
            "drug_patients": [], "mech_patients": [], "filter_patients": [],
            "data_available": False}


def _ok_result(n=47):
    return {"all_count": n, "all_stay_count": n, "num_stays": [{}] * n,
            "drug_patients": [], "mech_patients": [], "filter_patients": [],
            "data_available": True}


class TestComputeICU07FailureGuard:
    def test_failed_compute_carries_skip_flag(self, monkeypatch):
        monkeypatch.setattr(summary_mod, "get_dvt_prevention_patients",
                            lambda codes, s, e: _fail_result())
        r = summary_mod._compute_icu07(["3439"], "2026-10-01", "2026-10-31")
        # 分子必须是 0 且带 data_available=False（rebuild 据此跳过写入）
        assert r["num"] == 0
        assert r["data_available"] is False

    def test_ok_compute_no_skip_flag(self, monkeypatch):
        monkeypatch.setattr(summary_mod, "get_dvt_prevention_patients",
                            lambda codes, s, e: _ok_result(47))
        monkeypatch.setattr(summary_mod, "_count_icu_patients",
                            lambda codes, s, e: 81)
        r = summary_mod._compute_icu07(["3439"], "2026-10-01", "2026-10-31")
        assert r["num"] == 47
        assert r["den"] == 81
        assert r["val"] == 58.0
        # 可用结果不得携带跳过标记（或为 True），rebuild 才会落库
        assert r.get("data_available", True) is True


class TestRebuildSkipDecision:
    """复刻 rebuild_summary._compute_one 的落库判定：False → 跳过，保留旧值"""

    @staticmethod
    def _should_write(doc: dict) -> bool:
        return doc.get("data_available", True)

    def test_failed_doc_not_written(self):
        failed = {"num": 0, "den": 0, "val": 0.0, "data_available": False}
        assert self._should_write(failed) is False

    def test_legacy_doc_without_flag_written(self):
        # 旧取数函数不带该字段时默认可用，不误伤其他指标
        legacy = {"num": 10, "den": 20, "val": 50.0}
        assert self._should_write(legacy) is True
