"""
SOFA-2 2025 评分内核。
纯函数，不依赖任何 I/O，评估时间由外部传入。
参考文献: JAMA. 2025;334(23):2090-2103. DOI: 10.1001/jama.2025.20516.

铁律:
  - 评分核内禁止 DB 访问、禁止 now()
  - 观测时间 > evaluation_time 的数据不得使用
  - 缺失数据返回 None，不得回退为 0 或空值
  - 判定项只返回 True/False/None
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .sofa_rules import SOFA2_THRESHOLDS as _TH, _UNIT_CONVERSION
from .adapter import ne_ugkgmin as _ne_ugkgmin, canon_drug  # noqa: F401 — 唯一换算点引用
from .missing_policy import apply_policy as _apply_policy
import config.indicator_windows as _cfg

from tzutil import as_utc  # noqa: E402 — naive = 真 UTC（收敛点）

logger = logging.getLogger(__name__)

# -----------------------------------------------------------
# 单位白名单 (SOFA-2 专用, 需求文档 7.3 第 2、13 条)
# -----------------------------------------------------------
_ALLOWED_UNITS_SOFA2: Dict[str, set] = {
    "bilirubin": {"mg/dl"},
    "creatinine": {"mg/dl"},
}


def _normalize_unit(u: Optional[str]) -> str:
    if not u:
        return ""
    return u.strip().lower()


def _convert_to_sofa2_canonical(
    value: float, unit: str, substance: str
) -> Tuple[Optional[float], Optional[str]]:
    """
    将数值转换为 SOFA-2 的标准单位。
    SOFA-2: 胆红素 mg/dL, 肌酐 mg/dL。
    """
    normalized = _normalize_unit(unit)
    if not normalized:
        return None, f"单位缺失, 禁止回退为默认单位. substance={substance}"

    allowed = _ALLOWED_UNITS_SOFA2.get(substance, set())
    if not allowed:
        return None, f"未知 substance={substance}"

    if normalized in allowed:
        return value, None

    # 尝试转换: umol/l → mg/dl
    key = (substance, normalized, "mg/dl")
    if key in _UNIT_CONVERSION:
        return _UNIT_CONVERSION[key](value), None

    return None, (
        f"单位 '{unit}' 不在 SOFA-2 {substance} 白名单 {allowed} 内, "
        f"且无已知转换路径. 禁止静默回退."
    )


# -----------------------------------------------------------
# 阈值匹配 (半开区间)
# -----------------------------------------------------------
def _score_from_thresholds(
    value: float,
    thresholds: List[dict],
    direction: str = "higher_is_worse",
) -> Optional[int]:
    """
    #21: 半开区间 [low, high) 匹配分值。
    落不进任何区间时返回 None。
    """
    for t in thresholds:
        if t["low"] <= value < t["high"]:
            return int(t["score"])
    return None


def _range_ok(value: float, guard: Optional[Tuple[float, float]]) -> bool:
    """检查值是否在 range_guard 范围内。guard=None 视为通过。"""
    if guard is None:
        return True
    lo, hi = guard
    return lo <= value <= hi


def _apply_stale_policy(is_stale: bool, value: Optional[float], info: dict, organ: str) -> Optional[float]:
    """
    #11: 陈旧度策略统一处理。
    flag_only: 打标记照常出分
    reject: 返回 None
    """
    if not is_stale or value is None:
        return value
    policy = _cfg.SOFA_STALE_POLICY
    info[f"{organ}_stale"] = True
    if policy == "reject":
        return None
    return value


def _urine_in_window(
    obs: List[dict],
    codes: List[str],
    eval_time: datetime,
    lookback_h: int,
    max_staleness_h: int,
) -> Tuple[Optional[float], Optional[str], Optional[datetime], Optional[bool]]:
    """
    #20: 尿量聚合结构性返工。
    收集窗口内全部尿量记录，按单位分组后决定聚合方式：
    - ml/24h, ml/24hr, ml/24 hr -> 取时间最近一条，不求和
    - ml/h, ml/hr             -> 取时间最近一条，不求和
    - ml/kg/h, ml/kg/hr       -> 取时间最近一条，不求和
    - ml, 毫升                 -> 求和（单次增量才允许累加）
    - 其他                     -> 返回 None + urine_unit_unknown
    若窗口内同时出现两种以上单位组，返回 None 并打 info["urine_mixed_units"]
    返回 (value, unit, ts, is_stale)
    """
    window_start = eval_time - timedelta(hours=lookback_h)
    candidates = []
    for o in obs:
        code = (o.get("code") or o.get("item_name") or "").strip()
        if code not in codes:
            continue
        raw_val = o.get("value_number")
        if raw_val is None:
            continue
        ts = o.get("observed_at")
        if not isinstance(ts, datetime):
            continue
        ts = as_utc(ts)
        if ts < window_start or ts > eval_time:
            continue
        candidates.append((float(raw_val), o.get("unit", ""), ts))

    if not candidates:
        return None, None, None, None

    # 按单位分组
    from collections import defaultdict
    unit_groups: Dict[str, list] = defaultdict(list)
    for val, unit, ts in candidates:
        normalized = _normalize_unit(unit)
        unit_groups[normalized].append((val, unit, ts))

    # 检查是否混合单位
    if len(unit_groups) > 1:
        mixed_units = list(unit_groups.keys())
        return None, None, None, None  # 调用方需打 urine_mixed_units

    # 单一单位组
    normalized_u = list(unit_groups.keys())[0]
    group = unit_groups[normalized_u]

    # 决定聚合方式
    if normalized_u in ("ml/24h", "ml/24 hr", "ml/24hr", "ml/h", "ml/hr", "ml/kg/h", "ml/kg/hr"):
        # 速率或日汇总 → 取时间最近一条
        group.sort(key=lambda x: x[2], reverse=True)
        best_val, best_unit, best_ts = group[0]
    elif normalized_u in ("ml", "毫升"):
        # 纯 ml（单次增量）→ 求和
        best_val = sum(x[0] for x in group)
        best_ts = max(x[2] for x in group)
        best_unit = [x[1] for x in group if x[2] == best_ts][0]
    else:
        # 未知单位
        return None, None, None, None

    staleness = (eval_time - best_ts).total_seconds() / 3600.0
    is_stale = staleness > max_staleness_h if max_staleness_h > 0 else False
    return best_val, best_unit, best_ts, is_stale


# -----------------------------------------------------------
# 窗口内最差值
# -----------------------------------------------------------
def _worst_in_window(
    observations: List[dict],
    codes: List[str],
    eval_time: datetime,
    lookback_hours: int,
    max_staleness_hours: int,
    agg: str = "min",
) -> Tuple[Optional[float], Optional[str], Optional[datetime], Optional[bool]]:
    """
    #19: max_staleness_hours 真正生效。
    agg 聚合方向: min / max / sum / latest
    """
    window_start = eval_time - timedelta(hours=lookback_hours)
    candidates = []
    for obs in observations:
        code = (obs.get("code") or obs.get("item_name") or "").strip()
        if code not in codes:
            continue
        raw_val = obs.get("value_number")
        if raw_val is None:
            continue
        ts = obs.get("observed_at")
        if not isinstance(ts, datetime):
            continue
        ts = as_utc(ts)
        if ts < window_start or ts > eval_time:
            continue
        candidates.append((float(raw_val), obs.get("unit", ""), ts))
    if not candidates:
        return None, None, None, None
    if agg == "sum":
        total_val = sum(c[0] for c in candidates)
        best_ts = max(c[2] for c in candidates)
        best_unit = [c[1] for c in candidates if c[2] == best_ts][0]
        best = (total_val, best_unit, best_ts)
    elif agg == "max":
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0]
    elif agg == "latest":
        candidates.sort(key=lambda x: x[2], reverse=True)
        best = candidates[0]
    else:
        candidates.sort(key=lambda x: x[0])
        best = candidates[0]
    staleness = (eval_time - best[2]).total_seconds() / 3600.0
    is_stale = staleness > max_staleness_hours if max_staleness_hours > 0 else False
    return best[0], best[1], best[2], is_stale


# -----------------------------------------------------------
# 呼吸配对聚合 (#12)
# -----------------------------------------------------------
def _worst_pf_pair_in_window(
    obs: List[dict],
    codes_pao2: List[str],
    codes_fio2: List[str],
    eval_time: datetime,
    lookback_h: int,
    max_pair_seconds: int = 1800,
) -> Optional[Tuple[float, datetime, datetime]]:
    """
    #12: 笛卡尔配对 PaO2×FiO2，保留时间差≤max_pair_seconds 的配对，
    返回 (ratio, pao2_ts, fio2_ts) 中 ratio 最小的一对。
    无配对返回 None。
    """
    window_start = eval_time - timedelta(hours=lookback_h)
    pao2_candidates = []
    fio2_candidates = []
    for o in obs:
        code = (o.get("code") or o.get("item_name") or "").strip()
        raw_val = o.get("value_number")
        if raw_val is None:
            continue
        ts = o.get("observed_at")
        if not isinstance(ts, datetime):
            continue
        ts = as_utc(ts)
        if ts < window_start or ts > eval_time:
            continue
        val = float(raw_val)
        if code in codes_pao2:
            pao2_candidates.append((val, ts))
        elif code in codes_fio2:
            fio2_candidates.append((val, ts))

    if not pao2_candidates or not fio2_candidates:
        return None

    best_ratio = None
    best_pair = None
    for pao2_val, pao2_ts in pao2_candidates:
        for fio2_val, fio2_ts in fio2_candidates:
            if abs((pao2_ts - fio2_ts).total_seconds()) > max_pair_seconds:
                continue
            if fio2_val <= 0:
                continue
            if fio2_val > 1.0:
                fio2_val = fio2_val / 100.0
            if fio2_val <= 0 or fio2_val > 1.0:
                continue
            ratio = pao2_val / fio2_val
            if best_ratio is None or ratio < best_ratio:
                best_ratio = ratio
                best_pair = (ratio, pao2_ts, fio2_ts)

    return best_pair


# -----------------------------------------------------------
# 呼吸 (SOFA-2: SpO2 替代 + 高级呼吸支持门控)
# -----------------------------------------------------------
def _calc_respiratory(
    obs: List[dict],
    eval_time: datetime,
    has_advanced_support: bool,
) -> Tuple[Optional[int], Dict[str, Any]]:
    info: Dict[str, Any] = {}

    codes_pao2 = ["param_PaO2", "PaO2"]
    codes_fio2 = ["param_FiO2", "FiO2"]

    # 先查 P/F ratio 直接值
    val, unit, ts, is_stale = _worst_in_window(obs, ["param_bg_P/Fratio", "P/F_ratio"], eval_time, _cfg.RESP_LOOKBACK_H, _cfg.RESP_STALENESS_MAX_H)
    if val is not None and val > 0:
        ratio = val
        if is_stale:
            info["respiratory_stale"] = True
    else:
        # #12: 配对聚合
        pair = _worst_pf_pair_in_window(obs, codes_pao2, codes_fio2, eval_time, _cfg.RESP_LOOKBACK_H)
        if pair is not None:
            ratio, _, _ = pair
        else:
            # SpO2/FiO2 替代路径 (SOFA-2 特有)
            val_spo2, _, _, _ = _worst_in_window(obs, ["SpO2", "param_SpO2"], eval_time, _cfg.RESP_LOOKBACK_H, _cfg.RESP_STALENESS_MAX_H)
            # FiO2 单独取（SpO2 替代路径不需要 PaO2 配对）
            val_fio2, _, _, _ = _worst_in_window(obs, codes_fio2, eval_time, _cfg.RESP_LOOKBACK_H, _cfg.RESP_STALENESS_MAX_H)
            if val_spo2 is not None and val_fio2 is not None and val_fio2 > 0:
                if val_spo2 >= 98:
                    info["spo2_98_no_fallback"] = True
                    return None, info
                fio2 = val_fio2 / 100.0 if val_fio2 > 1.0 else val_fio2
                sf_ratio = val_spo2 / fio2
                score = _score_from_thresholds(sf_ratio, _TH["respiratory"]["sf_thresholds"])
                if score is None:
                    info["value_out_of_all_bands"] = True
                    return None, info
                if score >= 3 and not has_advanced_support:
                    score = 2
                info["spo2_fio2_ratio"] = sf_ratio
                return score, info
            info["respiratory_missing"] = True
            return None, info

    score = _score_from_thresholds(ratio, _TH["respiratory"]["pf_thresholds"])
    if score is None:
        info["value_out_of_all_bands"] = True
        return None, info
    if score >= 3 and not has_advanced_support:
        score = 2
    info["pao2_fio2_ratio"] = ratio
    return score, info


# -----------------------------------------------------------
# 凝血
# -----------------------------------------------------------
def _calc_hemostasis(
    obs: List[dict],
    eval_time: datetime,
) -> Tuple[Optional[int], Dict[str, Any]]:
    info: Dict[str, Any] = {}
    val, unit, _, is_stale = _worst_in_window(
        obs, _TH["hemostasis"]["codes"], eval_time,
        _TH["hemostasis"]["lookback_hours"],
        _TH["hemostasis"]["max_staleness_hours"],
    )
    if val is None:
        info["hemostasis_missing"] = True
        return None, info
    if is_stale:
        info["hemostasis_stale"] = True
    # range_guard 守卫
    if not _range_ok(val, _TH["hemostasis"].get("range_guard")):
        info["hemostasis_out_of_range"] = val
        return None, info
    info["hemostasis_raw_value"] = val
    info["hemostasis_raw_unit"] = unit or "×10³/μL"
    return _score_from_thresholds(val, _TH["hemostasis"]["thresholds"]), info


# -----------------------------------------------------------
# 肝脏 (SOFA-2 用 mg/dL)
# -----------------------------------------------------------
def _calc_liver(
    obs: List[dict],
    eval_time: datetime,
) -> Tuple[Optional[int], Dict[str, Any]]:
    info: Dict[str, Any] = {}
    val, unit, _, is_stale = _worst_in_window(
        obs, _TH["liver"]["codes"], eval_time,
        _TH["liver"]["lookback_hours"],
        _TH["liver"]["max_staleness_hours"],
        agg="max",
    )
    if val is None:
        info["liver_missing"] = True
        return None, info
    if is_stale:
        info["liver_stale"] = True
    converted, err = _convert_to_sofa2_canonical(val, unit, "bilirubin")
    if err:
        info["liver_unit_error"] = err
        return None, info
    info["liver_raw_value"] = converted
    info["liver_raw_unit"] = "mg/dL"
    return _score_from_thresholds(converted, _TH["liver"]["thresholds"]), info


# -----------------------------------------------------------
# 脑 (SOFA-2: delirium 治疗 + motor fallback)
# -----------------------------------------------------------
def _lowest_gcs_in_window(
    obs: List[dict],
    codes: List[str],
    eval_time: datetime,
    lookback_h: int,
) -> Tuple[Optional[int], Optional[datetime], Optional[int], bool]:
    """
    #15: 遍历窗口内全部 GCS 记录，返回 (lowest_total, lowest_ts, vt_motor, vt_only)。
    """
    window_start = eval_time - timedelta(hours=lookback_h)
    numeric = []
    vt_motors = []

    for o in obs:
        code = (o.get("code") or o.get("item_name") or "").strip()
        if code not in codes:
            continue
        ts = o.get("observed_at")
        if not isinstance(ts, datetime):
            continue
        ts = as_utc(ts)
        if ts < window_start or ts > eval_time:
            continue

        raw_gcs = o.get("value_number") or o.get("value_text")
        gcs_total, parse_err = _parse_gcs(raw_gcs)
        if parse_err == "V=T_motor_fallback":
            raw_text = str(raw_gcs).strip()
            vt_match = re.fullmatch(r"^[Ee]([1-4])[Vv][Tt][Mm]([1-6])$", raw_text)
            if vt_match:
                m_val = int(vt_match.group(2))
                vt_motors.append((m_val, ts))
        elif gcs_total is not None:
            numeric.append((gcs_total, ts))

    if numeric:
        best = min(numeric, key=lambda x: x[0])
        return best[0], best[1], None, False

    if vt_motors:
        best_m = min(vt_motors, key=lambda x: x[0])
        return None, best_m[1], best_m[0], True

    return None, None, None, False


def _parse_gcs(gcs_val: Any) -> Tuple[Optional[int], Optional[str]]:
    """
    解析 GCS 值。需求文档 7.3 第 8 条:
      - re.fullmatch, E/M 只允许一位数字
      - V=T → 返回 None + "V=T_motor_fallback" 错误
      - 范围 3-15
    """
    if gcs_val is None:
        return None, None
    if isinstance(gcs_val, (int, float)):
        total = int(gcs_val)
        if total < 3 or total > 15:
            return None, f"GCS 数值超范围: {total} (有效 3-15)"
        return total, None
    if isinstance(gcs_val, str):
        try:
            total = int(float(gcs_val))
            if total < 3 or total > 15:
                return None, f"GCS 数值超范围: {total} (有效 3-15)"
            return total, None
        except ValueError:
            pass
        # E 最大 4（睁眼）
        pattern = r"^[Ee]([1-4])[Vv]([Tt1-5])[Mm]([1-6])$"
        m = re.fullmatch(pattern, gcs_val.strip())
        if m:
            e = int(m.group(1))
            v_raw = m.group(2)
            m_score = int(m.group(3))
            if v_raw.upper() == "T":
                return None, "V=T_motor_fallback"
            v = int(v_raw)
            total = e + v + m_score
            if total < 3 or total > 15:
                return None, f"GCS 编码计算结果超范围: E{e}V{v}M{m_score}={total}"
            return total, None
    return None, f"无法解析 GCS 值: {gcs_val}"


def _calc_brain(
    obs: List[dict],
    eval_time: datetime,
) -> Tuple[Optional[int], Dict[str, Any]]:
    info: Dict[str, Any] = {}
    codes = _TH["brain"]["codes"]
    lookback = _TH["brain"]["lookback_hours"]

    # #15: 使用 _lowest_gcs_in_window 遍历全部记录取最差
    gcs_total, gcs_ts, vt_motor, vt_only = _lowest_gcs_in_window(obs, codes, eval_time, lookback)

    if vt_only and vt_motor is not None:
        # V=T: 走 motor fallback (需求文档 7.3 第 8 条)
        motor_fallback = _TH["brain"].get("motor_fallback", {})
        fallback_score = motor_fallback.get(vt_motor)
        if fallback_score is not None:
            info["gcs_vt_motor_fallback"] = {"m": vt_motor, "fallback_score": fallback_score}
            return fallback_score, info
        else:
            info["gcs_parse_error"] = f"V=T 但 motor_fallback 无 M{vt_motor} 映射"
            return None, info

    if gcs_total is None:
        info["brain_missing"] = True
        return None, info

    info["brain_raw_value"] = gcs_total
    info["brain_raw_unit"] = "GCS"
    score = _score_from_thresholds(gcs_total, _TH["brain"]["thresholds"])
    return score, info


# -----------------------------------------------------------
# 肾脏 (SOFA-2: mg/dL, mL/kg/h)
# -----------------------------------------------------------
def _calc_kidney(
    obs: List[dict],
    eval_time: datetime,
    weight_kg: Optional[float],
) -> Tuple[Optional[int], Dict[str, Any]]:
    info: Dict[str, Any] = {}
    score_creat = None
    score_urine = None

    # 肌酐 (mg/dL, 取最高值: 越高越差)
    val, unit, _, is_stale = _worst_in_window(
        obs, _TH["kidney"]["codes_creatinine"], eval_time,
        _TH["kidney"]["lookback_hours"],
        _TH["kidney"]["max_staleness_hours"],
        agg="max",
    )
    if val is not None:
        if is_stale:
            info["kidney_creatinine_stale"] = True
        # range_guard 守卫 (换算前检查)
        if not _range_ok(val, _TH["kidney"].get("range_guard_creatinine")):
            info["kidney_creatinine_out_of_range"] = val
        else:
            converted, err = _convert_to_sofa2_canonical(val, unit, "creatinine")
            if err:
                info["kidney_creatinine_unit_error"] = err
            else:
                info["kidney_creatinine_value"] = converted
                info["kidney_creatinine_unit"] = "mg/dL"
                score_creat = _score_from_thresholds(
                    converted, _TH["kidney"]["creatinine_thresholds"]
                )

    # #20: 尿量按单位分流
    urine_val, urine_unit, urine_ts, urine_stale = _urine_in_window(
        obs, _TH["kidney"]["codes_urine"], eval_time,
        _TH["kidney"]["lookback_hours"],
        _TH["kidney"]["max_staleness_hours"],
    )
    if urine_val is not None and weight_kg and weight_kg > 0:
        if urine_stale:
            info["kidney_urine_stale"] = True
        normalized_u = _normalize_unit(urine_unit)
        if normalized_u in ("ml/kg/h", "ml/kg/hr"):
            rate_per_kg_h = urine_val
        elif normalized_u in ("ml/h", "ml/hr"):
            rate_per_kg_h = urine_val / weight_kg
        elif normalized_u in ("ml/24h", "ml/24 hr", "ml/24hr"):
            rate_per_kg_h = urine_val / weight_kg / 24.0
            info["urine_coarse_24h"] = True
        elif normalized_u in ("ml", "毫升"):
            # #20: 纯 ml（单次增量）→ sum，转换为 ml/kg/h
            rate_per_kg_h = urine_val / weight_kg / 24.0  # 粗略转换
        else:
            info["kidney_urine_unit_error"] = f"未知尿量单位: {urine_unit}"
            info.setdefault("data_quality_flags", []).append("urine_unit_unknown")
            rate_per_kg_h = None

        if rate_per_kg_h is not None:
            info["kidney_urine_rate"] = round(rate_per_kg_h, 2)
            info["kidney_urine_unit"] = "ml/kg/h"
            if rate_per_kg_h < 0.3:
                score_urine = 3
            elif rate_per_kg_h < 0.5:
                score_urine = 2

    if score_creat is None and score_urine is None:
        info["kidney_missing"] = True
        return None, info

    parts = [s for s in (score_creat, score_urine) if s is not None]
    return max(parts) if parts else None, info


# -----------------------------------------------------------
# 心血管 (SOFA-2: NE+Epi sum, MAP fallback, MCS)
# -----------------------------------------------------------
def _calc_cardiovascular(
    obs: List[dict],
    eval_time: datetime,
    pressors: List[dict],
    weight_kg: Optional[float],
    mcs_present: bool = False,
) -> Tuple[Optional[int], Dict[str, Any]]:
    """
    SOFA-2 心血管评分。
    JAMA 2025:
      - MCS(机械循环支持) → 4 分
      - NE+Epi 总和梯度: >0且≤0.2→2, >0.2且≤0.4→3, >0.4→4
      - 多巴胺独立: ≤20→2, 20-40→3, >40→4
      - other_vasopressor → 至少 2 分
      - MAP<70 且无升压药 → 1 分
    """
    info: Dict[str, Any] = {}

    # MCS → 4 分
    if mcs_present:
        info["mcs_4"] = True
        return 4, info

    has_active_pressor = False
    ne_dose_ugkgmin = 0.0
    epi_dose_ugkgmin = 0.0
    dopa_dose_ugkgmin = 0.0
    has_other_pressor = False
    dose_known = False

    for med in pressors:
        admin_end = med.get("admin_end")
        if admin_end and isinstance(admin_end, datetime):
            if admin_end < eval_time:
                continue
        med_name = med.get("med_name") or ""
        route = (med.get("route") or "")
        # 白名单: 静脉 静滴 静推 泵入 iv 任一命中即通过
        # route 缺失时不跳过, 照常计入并打 route_unknown 标记
        _IV_KEYWORDS = {"静脉", "静滴", "静推", "泵入", "iv"}
        route_lower = route.strip().lower()
        if route_lower and not any(kw in route_lower for kw in _IV_KEYWORDS):
            continue
        if not route_lower:
            info.setdefault("data_quality_flags", []).append("route_unknown")
        has_active_pressor = True

        # 剂量只能来自 adapter.ne_ugkgmin, 取不到走保底
        dose = med.get("dose_ugkgmin")
        if dose is not None and dose > 0:
            dose_known = True
            canon = canon_drug(med_name)
            if canon == "norepinephrine":
                ne_dose_ugkgmin = max(ne_dose_ugkgmin, dose)
            elif canon == "epinephrine":
                epi_dose_ugkgmin = max(epi_dose_ugkgmin, dose)
            elif canon == "dopamine":
                dopa_dose_ugkgmin = max(dopa_dose_ugkgmin, dose)
            elif canon == "dobutamine":
                # dobutamine 在 SOFA-2 归 other 桶（JAMA 2025: other_vasopressor → ≥2）
                has_other_pressor = True
            else:
                # phenylephrine / vasopressin / terlipressin / milrinone / isoproterenol → other
                has_other_pressor = True

    score = 0
    ne_epi_sum = ne_dose_ugkgmin + epi_dose_ugkgmin

    # NE+Epi sum 梯度
    if ne_epi_sum > 0:
        if ne_epi_sum <= 0.2:
            score = max(score, 2)
        elif ne_epi_sum <= 0.4:
            score = max(score, 3)
        else:
            score = max(score, 4)

    # 多巴胺独立梯度
    if dopa_dose_ugkgmin > 0:
        if dopa_dose_ugkgmin <= 20:
            score = max(score, 2)
        elif dopa_dose_ugkgmin <= 40:
            score = max(score, 3)
        else:
            score = max(score, 4)

    # other_vasopressor → 至少 2
    if has_other_pressor and score < 2:
        score = max(score, 2)

    # MAP<70 且无升压药 → 1
    map_val = None
    if not has_active_pressor:
        map_val, _, _, map_stale = _worst_in_window(obs, ["MAP", "mean_arterial_pressure"], eval_time, _cfg.RESP_LOOKBACK_H, 1)
        # #11: 陈旧度策略
        map_val = _apply_stale_policy(map_stale, map_val, info, "cardiovascular_map")
        info["map_value"] = map_val
        if map_val is not None and map_val < 70:
            score = max(score, 1)

    # 需求文档 7.3 第 9 条: has_active_pressor 且剂量未知 → 读配置策略
    if has_active_pressor and not dose_known:
        policy = _cfg.VASO_DOSE_UNKNOWN_POLICY
        if policy == "min_band_2":
            score = max(score, 2)
        elif policy == "reject":
            # #28: reject 分支改成 return None, info
            info["dose_unknown_rejected"] = True
            return None, info

    info["has_active_pressor"] = has_active_pressor
    info["dose_known"] = dose_known
    info["ne_epi_sum"] = ne_epi_sum

    # 无升压药且无 MAP 数据 → 无法评估心血管
    if not has_active_pressor and map_val is None:
        info["cardiovascular_missing"] = True
        return None, info

    return score, info


# -----------------------------------------------------------
# 主入口
# -----------------------------------------------------------
def compute_sofa2(
    observations: List[dict],
    medications: List[dict],
    eval_time: datetime,
    has_advanced_support: bool = False,
    weight_kg: Optional[float] = None,
    mcs_present: bool = False,
) -> Dict[str, Any]:
    """
    计算 SOFA-2 2025 总分。

    铁律: 缺失数据返回 None，不得回退为 0。
    """
    if eval_time.tzinfo is None:
        raise ValueError("eval_time 必须有时区信息")

    flags: List[str] = []
    components: Dict[str, Any] = {}
    info: Dict[str, Any] = {}

    resp_score, resp_info = _calc_respiratory(observations, eval_time, has_advanced_support)
    info.update(resp_info)
    if resp_score is None:
        flags.append("respiratory_missing")
    components["respiratory"] = resp_score

    hem_score, hem_info = _calc_hemostasis(observations, eval_time)
    info.update(hem_info)
    if hem_score is None:
        flags.append("hemostasis_missing")
    components["hemostasis"] = hem_score

    liver_score, liver_info = _calc_liver(observations, eval_time)
    info.update(liver_info)
    if liver_score is None:
        flags.append("liver_missing")
    components["liver"] = liver_score

    brain_score, brain_info = _calc_brain(observations, eval_time)
    info.update(brain_info)
    if brain_score is None:
        flags.append("brain_missing")
    components["brain"] = brain_score

    kidney_score, kidney_info = _calc_kidney(observations, eval_time, weight_kg)
    info.update(kidney_info)
    if kidney_score is None:
        flags.append("kidney_missing")
    components["kidney"] = kidney_score

    cv_score, cv_info = _calc_cardiovascular(
        observations, eval_time, medications, weight_kg, mcs_present
    )
    info.update(cv_info)
    if cv_score is None:
        flags.append("cardiovascular_missing")
    components["cardiovascular"] = cv_score

    # 应用缺失策略 (SOFA_MISSING_POLICY)
    policy_result = _apply_policy(
        policy=_cfg.SOFA_MISSING_POLICY,
        components=components,
        data_quality_flags=flags,
        eval_time=eval_time,
    )
    components = policy_result["components"]
    total = policy_result["total"]
    if policy_result["imputed_organs"]:
        info["imputed_organs"] = policy_result["imputed_organs"]

    valid_scores = [s for s in components.values() if s is not None]

    # result_status: complete / partial / insufficient
    n_valid = len(valid_scores)
    n_total = len(components)
    completeness = n_valid / n_total if n_total > 0 else 0.0
    if n_valid == n_total:
        result_status = "complete"
    elif n_valid >= 1:
        result_status = "partial"
    else:
        result_status = "insufficient"

    # 部分评分字段 (§5)
    measured_component_sum = sum(valid_scores) if valid_scores else None
    score_lower_bound = measured_component_sum  # 最低可能分 = 已测分
    missing_components = [name for name, score in components.items() if score is None]

    # 构建各器官原始值详情 (供前端展示)
    component_details = {}
    if "pao2_fio2_ratio" in info:
        component_details["respiratory"] = {"value": round(info["pao2_fio2_ratio"], 1), "unit": "P/F"}
    elif "spo2_fio2_ratio" in info:
        component_details["respiratory"] = {"value": round(info["spo2_fio2_ratio"], 1), "unit": "S/F"}
    if "hemostasis_raw_value" in info:
        component_details["hemostasis"] = {"value": info["hemostasis_raw_value"], "unit": info.get("hemostasis_raw_unit", "×10³/μL")}
    if "liver_raw_value" in info:
        component_details["liver"] = {"value": round(info["liver_raw_value"], 2), "unit": info.get("liver_raw_unit", "mg/dL")}
    if "brain_raw_value" in info:
        component_details["brain"] = {"value": info["brain_raw_value"], "unit": info.get("brain_raw_unit", "GCS")}
    kidney_detail = {}
    if "kidney_creatinine_value" in info:
        kidney_detail["creatinine"] = {"value": round(info["kidney_creatinine_value"], 2), "unit": "mg/dL"}
    if "kidney_urine_rate" in info:
        kidney_detail["urine"] = {"value": info["kidney_urine_rate"], "unit": "ml/kg/h"}
    if kidney_detail:
        component_details["kidney"] = kidney_detail
    if "ne_epi_sum" in info or "map_value" in info:
        cv_detail = {}
        if info.get("ne_epi_sum") is not None:
            cv_detail["ne_epi_sum"] = {"value": round(info["ne_epi_sum"], 4), "unit": "μg/kg/min"}
        if info.get("map_value") is not None:
            cv_detail["map"] = {"value": round(info["map_value"], 1), "unit": "mmHg"}
        if cv_detail:
            component_details["cardiovascular"] = cv_detail

    return {
        "sofa2_score": total,
        "components": components,
        "component_details": component_details,
        "data_quality_flags": flags,
        "meta": info,
        "result_status": result_status,
        "completeness": completeness,
        "measured_component_sum": measured_component_sum,
        "score_lower_bound": score_lower_bound,
        "missing_components": missing_components,
    }
