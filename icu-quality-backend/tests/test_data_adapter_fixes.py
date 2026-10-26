"""
data_adapter.py 修复验证测试。

覆盖:
- BGA 代码映射 (PaO2, FiO2, P/F ratio, Lactate, SpO2)
- BGA 单位保留
- 通气状态点对点检查 (非24h存在检查)
- 观测 item_code/source 字段
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from scoring.data_adapter import (
    _fetch_bga_observations,
    _fetch_bedside_observations,
    _fetch_ventilator_status_point_in_time,
)


def _aware(dt):
    """naive = 真 UTC（存储帧铁律，2026-10 实证）→ aware UTC。"""
    from tzutil import as_utc
    return as_utc(dt)


class TestBGACodeMapping:
    """BGA代码映射测试"""

    def test_pao2_code_mapping(self):
        """param_bg_pAO2 应映射为 PaO2"""
        from unittest.mock import MagicMock, patch
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)
        window_start = eval_time - timedelta(hours=24)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_pAO2", "fVal": 85.0, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": "mmHg"},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert len(obs) == 1
        assert obs[0]["code"] == "PaO2"
        assert obs[0]["value_number"] == 85.0
        assert obs[0]["unit"] == "mmHg"
        assert obs[0]["item_code"] == "param_bg_pAO2"
        assert obs[0]["source"] == "bGATemp"

    def test_po2_lowercase_variant(self):
        """param_bg_po2 (小写) 应映射为 PaO2"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_po2", "fVal": 90.0, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": "mmHg"},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert len(obs) == 1
        assert obs[0]["code"] == "PaO2"
        assert obs[0]["item_code"] == "param_bg_po2"

    def test_fio2_code_mapping(self):
        """param_bg_FiO2 应映射为 FiO2"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_FiO2", "fVal": 60.0, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": "%"},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert len(obs) == 1
        assert obs[0]["code"] == "FiO2"
        assert obs[0]["value_number"] == 60.0

    def test_pf_ratio_code_mapping(self):
        """param_bg_P/Fratio 应映射为 P/F_ratio"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_P/Fratio", "fVal": 250.0, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": "mmHg"},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert len(obs) == 1
        assert obs[0]["code"] == "P/F_ratio"
        assert obs[0]["value_number"] == 250.0

    def test_lactate_code_mapping(self):
        """param_bg_Lac 应映射为 Lactate"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_Lac", "fVal": 3.5, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": "mmol/L"},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert len(obs) == 1
        assert obs[0]["code"] == "Lactate"
        assert obs[0]["value_number"] == 3.5
        assert obs[0]["unit"] == "mmol/L"

    def test_spo2_code_mapping(self):
        """param_bg_SpO2 应映射为 SpO2"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_SpO2", "fVal": 95.0, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": "%"},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert len(obs) == 1
        assert obs[0]["code"] == "SpO2"
        assert obs[0]["value_number"] == 95.0

    def test_unknown_code_filtered(self):
        """未知BGA代码应被过滤"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_unknown", "fVal": 99.0, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": ""},
                {"code": "param_bg_pAO2", "fVal": 85.0, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": "mmHg"},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert len(obs) == 1
        assert obs[0]["code"] == "PaO2"


class TestBGAUnitPreservation:
    """BGA单位保留测试"""

    def test_unit_from_bedside_entry(self):
        """应优先使用bedside条目自带的unit"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_pAO2", "fVal": 85.0, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": "kPa"},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert obs[0]["unit"] == "kPa"  # 优先使用bedside自带的unit

    def test_unit_fallback_to_mapping(self):
        """当bedside条目无unit时，应使用已知单位映射"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_pAO2", "fVal": 85.0, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": ""},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert obs[0]["unit"] == "mmHg"  # 回退到已知单位映射

    def test_lactate_unit_mapping(self):
        """乳酸应使用mmol/L单位"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        bga_doc = {
            "mrn": "M001",
            "bedsides": [
                {"code": "param_bg_Lac", "fVal": 2.5, "valid": "valid",
                 "time": eval_time - timedelta(hours=1), "unit": ""},
            ]
        }
        sc.bGATemp.find.return_value.max_time_ms.return_value.limit.return_value = [bga_doc]

        obs = _fetch_bga_observations(sc, "M001", eval_time)

        assert obs[0]["unit"] == "mmol/L"


class TestVentilatorPointInTime:
    """通气状态点对点检查测试"""

    def test_ventilator_active_at_eval_time(self):
        """eval_time时PEEP>0应返回dict且is_active=True"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        sc.bedside.find_one.return_value = {
            "code": "param_vent_peep",
            "value_number": 8.0,
            "time": eval_time - timedelta(minutes=30),
        }

        result = _fetch_ventilator_status_point_in_time(sc, "P001", eval_time)

        # 返回 dict 而非 bool
        assert isinstance(result, dict)
        assert result["is_active"] is True
        assert result["status"] == "active"

    def test_ventilator_inactive_when_no_data(self):
        """无通气数据应返回dict且status=unknown"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        sc.bedside.find_one.return_value = None

        result = _fetch_ventilator_status_point_in_time(sc, "P001", eval_time)

        # 无数据 → unknown (非 False)
        assert isinstance(result, dict)
        assert result["is_active"] is False
        assert result["status"] == "unknown"

    def test_ventilator_inactive_when_peep_zero(self):
        """PEEP=0应返回dict且is_active=False"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        sc.bedside.find_one.return_value = {
            "code": "param_vent_peep",
            "value_number": 0.0,
            "time": eval_time - timedelta(minutes=30),
        }

        result = _fetch_ventilator_status_point_in_time(sc, "P001", eval_time)

        assert isinstance(result, dict)
        assert result["is_active"] is False
        assert result["status"] == "inactive"

    def test_ventilator_active_vt(self):
        """VT>0应返回dict且is_active=True"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        sc.bedside.find_one.return_value = {
            "code": "param_vent_vt",
            "value_number": 450.0,
            "time": eval_time - timedelta(minutes=30),
        }

        result = _fetch_ventilator_status_point_in_time(sc, "P001", eval_time)

        assert isinstance(result, dict)
        assert result["is_active"] is True
        assert result["status"] == "active"

    def test_ventilator_active_pip(self):
        """PIP>0应返回dict且is_active=True"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        sc.bedside.find_one.return_value = {
            "code": "param_vent_pip",
            "value_number": 20.0,
            "time": eval_time - timedelta(minutes=30),
        }

        result = _fetch_ventilator_status_point_in_time(sc, "P001", eval_time)

        assert isinstance(result, dict)
        assert result["is_active"] is True
        assert result["status"] == "active"

    def test_ventilator_expired_data_ignored(self):
        """超过tolerance_hours的数据应返回stale"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        # 数据在5小时前 (超过默认4小时tolerance)
        sc.bedside.find_one.return_value = {
            "code": "param_vent_peep",
            "value_number": 8.0,
            "time": eval_time - timedelta(hours=5),
        }

        result = _fetch_ventilator_status_point_in_time(sc, "P001", eval_time, tolerance_hours=4)

        # 数据超过tolerance_hours → stale
        assert isinstance(result, dict)
        assert result["is_active"] is False
        assert result["status"] == "stale"


class TestObservationMetadata:
    """观测元数据字段测试"""

    def test_bedside_has_item_code(self):
        """bedside观测应包含item_code字段"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        sc.bedside.find.return_value.max_time_ms.return_value.limit.return_value = [
            {"code": "param_nibp_m", "value_number": 75.0, "time": eval_time,
             "valid": True, "unit": "mmHg"},
        ]

        obs = _fetch_bedside_observations(sc, "P001", eval_time)

        assert len(obs) >= 1
        for o in obs:
            assert "item_code" in o
            assert "source" in o
            assert o["source"] == "bedside"

    def test_bedside_gcs_has_item_code(self):
        """GCS编码格式观测应包含item_code字段"""
        from unittest.mock import MagicMock
        sc = MagicMock()
        eval_time = datetime(2025, 1, 15, 12, 0, 0)

        # GCS编码格式 E4VtM6 会匹配正则，但 value_number 为 None 会被跳过
        # 使用普通GCS值来测试
        sc.bedside.find.return_value.max_time_ms.return_value.limit.return_value = [
            {"code": "param_score_gcs_obs", "strVal": "15", "value_number": 15.0,
             "time": eval_time, "valid": True, "unit": ""},
        ]

        obs = _fetch_bedside_observations(sc, "P001", eval_time)

        assert len(obs) >= 1
        gcs_obs = [o for o in obs if o.get("code") == "param_score_gcs_obs"]
        assert len(gcs_obs) >= 1
        assert gcs_obs[0]["item_code"] == "param_score_gcs_obs"
        assert gcs_obs[0]["source"] == "bedside"


class TestPFRatioCodeRecognition:
    """P/F ratio代码识别测试"""

    def test_pf_ratio_code_recognized_by_sofa_core(self):
        """P/F_ratio代码应被sofa_core识别"""
        from scoring.sofa_core import compute_sofa_classic

        eval_time = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        observations = [
            {"code": "P/F_ratio", "value_number": 250.0, "unit": "mmHg",
             "observed_at": eval_time - timedelta(hours=1)},
        ]

        result = compute_sofa_classic(observations, [], eval_time)

        assert result["sofa_score"] is not None
        # components["respiratory"] is an int score directly
        resp_score = result["components"]["respiratory"]
        assert resp_score is not None
        assert resp_score >= 1  # P/F<300 → score>=1

    def test_pf_ratio_code_recognized_by_sofa2_core(self):
        """P/F_ratio代码应被sofa2_core识别"""
        from scoring.sofa2_core import compute_sofa2

        eval_time = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        observations = [
            {"code": "P/F_ratio", "value_number": 250.0, "unit": "mmHg",
             "observed_at": eval_time - timedelta(hours=1)},
        ]

        result = compute_sofa2(observations, [], eval_time)

        assert result["sofa2_score"] is not None
        # components["respiratory"] is an int score directly
        resp_score = result["components"]["respiratory"]
        assert resp_score is not None
        assert resp_score >= 1  # P/F<300 → score>=1

    def test_lactate_code_recognized_by_bundle(self):
        """Lactate代码应被bundle engine识别"""
        from scoring.sofa_core import _worst_in_window

        eval_time = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        observations = [
            {"code": "Lactate", "value_number": 3.5, "unit": "mmol/L",
             "observed_at": eval_time - timedelta(hours=1)},
        ]

        # 验证Lactate代码能被正确识别
        val, unit, ts, stale = _worst_in_window(
            observations, ["param_Lac", "lactate", "Lactate"],
            eval_time, 24, 24
        )

        assert val == 3.5
        assert unit == "mmol/L"
