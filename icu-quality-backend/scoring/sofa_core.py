"""
经典 SOFA 1996 评分内核。
纯函数，不依赖任何 I/O，评估时间由外部传入。
参考文献: Vincent JL, et al. Intensive Care Medicine, 1996;22:707-710.

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

from .sofa_rules import (
    CLASSIC_SOFA_THRESHOLDS as _TH,
)
from .adapter import ne_ugkgmin as _ne_ugkgmin, canon_drug  # noqa: F401 — 唯一换算点引用
from .missing_policy import apply_policy as _apply_policy
import config.indicator_windows as _cfg

from tzutil import as_utc  # noqa: E402 — naive = 真 UTC（收敛点）

logger = logging.getLogger(__name__)

# -----------------------------------------------------------
# 单位白名单 (经典 SOFA 专用, 需求文档 7.3 第 2、13 条)
# -----------------------------------------------------------
_ALLOWED_UNITS_CLASSIC: Dict[str, set] = {
    "bilirubin": {"umol/l", "μmol/l", "micromol/l", "微摩尔/升"},
    "creatinine": {"umol/l", "μmol/l", "micromol/l", "微摩尔/升"},
}


def _normalize_unit(u: Optional[str]) -> str:
    """单位标准化: strip + lower。"""
    if not u:
        return ""
    return u.strip().lower()


def _convert_to_classic_canonical(
    value: float, unit: str, substance: str
) -> Tuple[Optional[float], Optional[str]]:
    """
    将数值转换为经典 SOFA 的标准单位。
    经典 SOFA: 胆红素 μmol/L, 肌酐 μmol/L。

    返回 (converted_value, error_message)。
    成功时 error_message=None。
    """
    normalized = _normalize_unit(unit)

    if not normalized:
        # 需求文档 7.3 第 2 条: 无单位不可运行
        return None, f"单位缺失, 禁止回退为默认单位. substance={substance}"

    allowed = _ALLOWED_UNITS_CLASSIC.get(substance, set())
    if not allowed:
        return None, f"未知 substance={substance}, 无法校验单位"

    if normalized in allowed:
        # 已经是经典 SOFA 标准单位
        return value, None

    # 需求文档 7.3 第 2、13 条: 单位不在白名单 → 拒绝，不做跨 variant 转换
    return None, (
        f"单位 '{unit}' 不在经典 SOFA {substance} 白名单 {allowed} 内. "
        f"经典 SOFA 仅接受 μmol/L, 禁止从 mg/dL 转换."
    )


# -----------------------------------------------------------
# 分值阈值判定 (半开区间, 需求文档 7.3 第 4 条)
# -----------------------------------------------------------
def _score_from_thresholds(
    value: float,
    thresholds: List[dict],
    direction: str = "higher_is_worse",
) -> Optional[int]:
    """
    #21: 半开区间 [low, high) 匹配分值。
    direction: "higher_is_worse" 或 "lower_is_worse"，由各阈值表声明。
    落不进任何区间时返回 None 并打 value_out_of_all_bands，禁止返回 thresholds[0] 或 thresholds[-1]。
    """
    for t in thresholds:
        if t["low"] <= value < t["high"]:
            return int(t["score"])
    # #21: 落不进任何区间 → 返回 None
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
# 辅助: 从观测列表中找窗口内最差值
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
    在 [eval_time - lookback, eval_time] 窗口内，按 code 匹配观测，
    返回 (worst_value, unit, observed_at, is_stale)。

    #19: max_staleness_hours 真正生效 — best 超过此阈值时 is_stale=True。

    agg 聚合方向:
      - min:   取最低值 (PLT, PF ratio, GCS)
      - max:   取最高值 (胆红素, 肌酐)
      - sum:   求和 (尿量)
      - latest: 取最新一条
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
        # min (default)
        candidates.sort(key=lambda x: x[0])
        best = candidates[0]

    # #19: 陈旧度检查
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

    # 收集窗口内全部候选
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

    # 笛卡尔配对，只保留时间差≤max_pair_seconds 的配对
    best_ratio = None
    best_pair = None
    for pao2_val, pao2_ts in pao2_candidates:
        for fio2_val, fio2_ts in fio2_candidates:
            if abs((pao2_ts - fio2_ts).total_seconds()) > max_pair_seconds:
                continue
            # FiO2 守卫
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
# 呼吸 (需求文档 7.3 第 12 条: 经典 SOFA 需机械通气门控)
# -----------------------------------------------------------
def _calc_respiratory(
    obs: List[dict],
    eval_time: datetime,
    has_advanced_support: bool,
) -> Tuple[Optional[int], Dict[str, Any]]:
    """
    PaO2/FiO2 ratio 评分。
    需求文档 7.3 第 12 条: 经典 SOFA 的 score=3/4 需 has_advanced_support=True。
    """
    info: Dict[str, Any] = {}

    codes_pao2 = ["param_PaO2", "PaO2"]
    codes_fio2 = ["param_FiO2", "FiO2"]
    codes_ratio = ["param_bg_P/Fratio", "P/F_ratio"]

    # 先查 ratio 直接值
    val, unit, ts, is_stale = _worst_in_window(obs, codes_ratio, eval_time, _cfg.RESP_LOOKBACK_H, _cfg.RESP_STALENESS_MAX_H)
    if val is not None and val > 0:
        ratio = val
        if is_stale:
            info["respiratory_stale"] = True
    else:
        # #12: 配对聚合
        pair = _worst_pf_pair_in_window(obs, codes_pao2, codes_fio2, eval_time, _cfg.RESP_LOOKBACK_H)
        if pair is None:
            info["respiratory_missing"] = True
            return None, info
        ratio, ts_pao2, ts_fio2 = pair

    score = _score_from_thresholds(ratio, _TH["respiratory"]["thresholds"])
    if score is None:
        info["value_out_of_all_bands"] = True
        return None, info

    # 需求文档 7.3 第 12 条: 经典 SOFA score=3 或 4 需机械通气门控
    if score >= 3 and not has_advanced_support:
        score = 2

    info["pao2_fio2_ratio"] = ratio
    return score, info


# -----------------------------------------------------------
# 凝血 (PLT)
# -----------------------------------------------------------
def _calc_coagulation(
    obs: List[dict],
    eval_time: datetime,
) -> Tuple[Optional[int], Dict[str, Any]]:
    info: Dict[str, Any] = {}
    val, unit, ts, is_stale = _worst_in_window(
        obs, _TH["coagulation"]["codes"], eval_time,
        _TH["coagulation"]["lookback_hours"],
        _TH["coagulation"]["max_staleness_hours"],
    )
    if val is None:
        info["coagulation_missing"] = True
        return None, info
    if is_stale:
        info["coagulation_stale"] = True
    # range_guard 守卫
    if not _range_ok(val, _TH["coagulation"].get("range_guard")):
        info["coagulation_out_of_range"] = val
        return None, info
    score = _score_from_thresholds(val, _TH["coagulation"]["thresholds"])
    return score, info


# -----------------------------------------------------------
# 肝脏 (需求文档 7.3 第 2、13 条: 单位白名单)
# -----------------------------------------------------------
def _calc_liver(
    obs: List[dict],
    eval_time: datetime,
) -> Tuple[Optional[int], Dict[str, Any]]:
    info: Dict[str, Any] = {}
    val, unit, ts, is_stale = _worst_in_window(
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

    # range_guard 守卫 (换算前检查)
    if not _range_ok(val, _TH["liver"].get("range_guard")):
        info["liver_out_of_range"] = val
        return None, info

    # 需求文档 7.3 第 2 条: 单位白名单
    converted, err = _convert_to_classic_canonical(val, unit, "bilirubin")
    if err:
        info["liver_unit_error"] = err
        return None, info

    score = _score_from_thresholds(converted, _TH["liver"]["thresholds"])
    return score, info


# -----------------------------------------------------------
# 心血管 (需求文档 7.3 第 1 条: Vincent 1996 原文逻辑)
# -----------------------------------------------------------
def _calc_cardiovascular(
    obs: List[dict],
    eval_time: datetime,
    pressors: List[dict],
    weight_kg: Optional[float],
) -> Tuple[Optional[int], Dict[str, Any]]:
    """
    经典 SOFA 心血管评分。
    需求文档 7.3 第 1 条:
      - MAP<70 → score=1
      - 多巴胺分档: ≤5→2, >5且≤15→3, >15→4
      - NE/Epi 剂量梯度: ≤0.1→3, >0.1→4 (Vincent 1996 原文)
      - 第 9 条: has_active_pressor 且剂量未知 → score=max(score,2)
    """
    info: Dict[str, Any] = {}

    # 检查是否有升压药活动
    has_active_pressor = False
    ne_dose_ugkgmin = 0.0
    epi_dose_ugkgmin = 0.0
    dopa_dose_ugkgmin = 0.0
    dobu_dose_ugkgmin = 0.0
    dose_known = False

    for med in pressors:
        admin_end = med.get("admin_end")
        if admin_end and isinstance(admin_end, datetime):
            if admin_end < eval_time:
                continue  # 已结束

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
                dobu_dose_ugkgmin = max(dobu_dose_ugkgmin, dose)
            # phenylephrine / vasopressin / terlipressin / milrinone / isoproterenol → other

    # 检查 MAP
    map_val, _, _, map_stale = _worst_in_window(
        obs, ["MAP", "mean_arterial_pressure"], eval_time, _cfg.RESP_LOOKBACK_H, 1
    )
    # #11: 陈旧度策略
    map_val = _apply_stale_policy(map_stale, map_val, info, "cardiovascular_map")

    score = 0

    # MAP<70 且无升压药 → score=1
    if map_val is not None and map_val < 70 and not has_active_pressor:
        score = 1

    # 多巴胺分档 (经典 SOFA: ≤5→2, >5且≤15→3, >15→4)
    if dopa_dose_ugkgmin > 0:
        if dopa_dose_ugkgmin <= 5:
            score = max(score, 2)
        elif dopa_dose_ugkgmin <= 15:
            score = max(score, 3)
        else:
            score = max(score, 4)

    # 多巴酚丁胺 (经典 SOFA: 任意剂量→2)
    if dobu_dose_ugkgmin > 0:
        score = max(score, 2)

    # NE/Epi 剂量梯度 (Vincent 1996: ≤0.1→3, >0.1→4, 单位 ug/kg/min)
    ne_epi_sum = ne_dose_ugkgmin + epi_dose_ugkgmin
    if ne_epi_sum > 0:
        if ne_epi_sum <= 0.1:
            score = max(score, 3)
        else:
            score = max(score, 4)

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
    info["ne_dose_ugkgmin"] = ne_dose_ugkgmin
    info["map_value"] = map_val

    # 无升压药且无 MAP 数据 → 无法评估心血管
    if not has_active_pressor and map_val is None:
        info["cardiovascular_missing"] = True
        return None, info

    return score, info


# -----------------------------------------------------------
# 中枢神经 (GCS, 需求文档 7.3 第 8 条: 编码解析)
# -----------------------------------------------------------

def _lowest_gcs_in_window(
    obs: List[dict],
    codes: List[str],
    eval_time: datetime,
    lookback_h: int,
) -> Tuple[Optional[int], Optional[datetime], Optional[int], bool]:
    """
    #15: 遍历窗口内全部 GCS 记录，返回 (lowest_total, lowest_ts, vt_motor, vt_only)。
    - numeric 非空 → 返回最小值 + 时间戳
    - numeric 为空且 vt 非空 → 返回 motor fallback（M 取最小），vt_only=True
    - 两者都空 → (None, None, None, False)
    """
    window_start = eval_time - timedelta(hours=lookback_h)
    numeric = []  # (total, ts)
    vt_motors = []  # (m_val, ts)

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
            # 提取 M 值
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
      - 数值: 直接返回，范围 3-15
      - 字符串 "E1VTM1": 用 re.fullmatch 解析，E/M 只允许一位数字
      - V=T 时返回 None，调用方走 motor fallback，禁止当 4 分加进总分
      - 字符串 "15": 解析为数字

    返回 (total_score, error)。
    """
    if gcs_val is None:
        return None, None

    if isinstance(gcs_val, (int, float)):
        total = int(gcs_val)
        if total < 3 or total > 15:
            return None, f"GCS 数值超范围: {total} (有效 3-15)"
        return total, None

    if isinstance(gcs_val, str):
        # 尝试数值解析
        try:
            total = int(float(gcs_val))
            if total < 3 or total > 15:
                return None, f"GCS 数值超范围: {total} (有效 3-15)"
            return total, None
        except ValueError:
            pass

        # E1VTM1 格式解析: E 最大 4（睁眼），M 只允许一位数字，V 允许 T 或一位数字
        pattern = r"^[Ee]([1-4])[Vv]([Tt1-5])[Mm]([1-6])$"
        m = re.fullmatch(pattern, gcs_val.strip())
        if m:
            e = int(m.group(1))
            v_raw = m.group(2)
            m_score = int(m.group(3))
            if v_raw.upper() == "T":
                # V=T → 调用方走 motor fallback，返回 None
                return None, "V=T_motor_fallback"
            v = int(v_raw)
            total = e + v + m_score
            if total < 3 or total > 15:
                return None, f"GCS 编码计算结果超范围: E{e}V{v}M{m_score}={total}"
            return total, None

    return None, f"无法解析 GCS 值: {gcs_val}"


def _calc_cns(
    obs: List[dict],
    eval_time: datetime,
) -> Tuple[Optional[int], Dict[str, Any]]:
    info: Dict[str, Any] = {}
    codes = _TH["central_nervous_system"]["codes"]
    lookback = _TH["central_nervous_system"]["lookback_hours"]

    # #15: 使用 _lowest_gcs_in_window 遍历全部记录取最差
    gcs_total, gcs_ts, vt_motor, vt_only = _lowest_gcs_in_window(obs, codes, eval_time, lookback)

    if vt_only and vt_motor is not None:
        # #16: V=T 分支 — 借用 SOFA-2 的 motor fallback 表
        from .sofa_rules import SOFA2_THRESHOLDS
        motor_fallback = SOFA2_THRESHOLDS["brain"].get("motor_fallback", {})
        fallback_score = motor_fallback.get(vt_motor)
        if fallback_score is not None:
            info["gcs_vt_motor_fallback"] = {"m": vt_motor, "fallback_score": fallback_score}
            info["data_quality_flags"] = ["gcs_vt_motor_fallback_borrowed_from_sofa2"]
            return fallback_score, info
        else:
            info["gcs_parse_error"] = f"V=T 但 motor_fallback 无 M{vt_motor} 映射"
            return None, info

    if gcs_total is None:
        info["cns_missing"] = True
        return None, info

    # range_guard 守卫
    if not _range_ok(gcs_total, _TH["central_nervous_system"].get("range_guard")):
        info["cns_out_of_range"] = gcs_total
        return None, info

    score = _score_from_thresholds(gcs_total, _TH["central_nervous_system"]["thresholds"])
    return score, info


# -----------------------------------------------------------
# 肾脏 (需求文档 7.3 第 2、13 条)
# -----------------------------------------------------------
def _calc_renal(
    obs: List[dict],
    eval_time: datetime,
) -> Tuple[Optional[int], Dict[str, Any]]:
    """
    经典 SOFA 肾脏评分。
    肌酐: μmol/L (经典 SOFA 标准单位)
    尿量: mL/24h
    """
    info: Dict[str, Any] = {}
    score_creat = None
    score_urine = None

    # 肌酐 (取最高值: 越高越差)
    val, unit, ts, is_stale = _worst_in_window(
        obs, _TH["renal"]["codes_creatinine"], eval_time,
        _TH["renal"]["lookback_hours"],
        _TH["renal"]["max_staleness_hours"],
        agg="max",
    )
    if val is not None:
        if is_stale:
            info["renal_creatinine_stale"] = True
        # range_guard 守卫 (换算前检查)
        if not _range_ok(val, _TH["renal"].get("range_guard_creatinine")):
            info["renal_creatinine_out_of_range"] = val
        else:
            converted, err = _convert_to_classic_canonical(val, unit, "creatinine")
            if err:
                info["renal_creatinine_unit_error"] = err
            else:
                score_creat = _score_from_thresholds(
                    converted, _TH["renal"]["creatinine_thresholds"]
                )

    # #20: 尿量按单位分流
    urine_val, urine_unit, urine_ts, urine_stale = _urine_in_window(
        obs, _TH["renal"]["codes_urine"], eval_time,
        _TH["renal"]["lookback_hours"],
        _TH["renal"]["max_staleness_hours"],
    )
    if urine_val is not None:
        if urine_stale:
            info["renal_urine_stale"] = True
        normalized_u = _normalize_unit(urine_unit)
        if normalized_u in ("ml/24h", "ml/24 hr", "ml/24hr"):
            score_urine = _score_from_thresholds(
                urine_val, _TH["renal"]["urine_thresholds"]
            )
        elif normalized_u in ("ml/h", "ml/hr"):
            score_urine = _score_from_thresholds(
                urine_val * 24, _TH["renal"]["urine_thresholds"]
            )
        elif normalized_u in ("ml", "毫升"):
            score_urine = _score_from_thresholds(
                urine_val, _TH["renal"]["urine_thresholds"]
            )
        else:
            info["renal_urine_unit_error"] = f"未知尿量单位: {unit}"
            info["data_quality_flags"] = ["urine_unit_unknown"]

    if score_creat is None and score_urine is None:
        info["renal_missing"] = True
        return None, info

    # 取两者最高分
    parts = [s for s in (score_creat, score_urine) if s is not None]
    return max(parts) if parts else None, info


# -----------------------------------------------------------
# 主入口
# -----------------------------------------------------------
def compute_sofa_classic(
    observations: List[dict],
    medications: List[dict],
    eval_time: datetime,
    has_advanced_support: bool = False,
    weight_kg: Optional[float] = None,
) -> Dict[str, Any]:
    """
    计算经典 SOFA 1996 总分。

    Args:
        observations: 观测数据列表，每条含 code, value_number, unit, observed_at
        medications: 用药数据列表，每条含 med_name, route, dose_ugkgmin, admin_end
        eval_time: 评估时间 (必须有时区)
        has_advanced_support: 是否有高级呼吸支持 (机械通气等)
        weight_kg: 体重 (用于 NE 剂量换算)

    Returns:
        {
            "sofa_score": int,          # 总分 0-24
            "components": {...},        # 各器官分值
            "data_quality_flags": [...],
            "meta": {...},
        }

    铁律: 缺失数据返回 None，不得回退为 0。
    """
    if eval_time.tzinfo is None:
        raise ValueError("eval_time 必须有时区信息")

    flags: List[str] = []
    components: Dict[str, Any] = {}
    info: Dict[str, Any] = {}

    # 呼吸
    resp_score, resp_info = _calc_respiratory(observations, eval_time, has_advanced_support)
    info.update(resp_info)
    if resp_score is None:
        flags.append("respiratory_missing")
    components["respiratory"] = resp_score

    # 凝血
    coag_score, coag_info = _calc_coagulation(observations, eval_time)
    info.update(coag_info)
    if coag_score is None:
        flags.append("coagulation_missing")
    components["coagulation"] = coag_score

    # 肝脏
    liver_score, liver_info = _calc_liver(observations, eval_time)
    info.update(liver_info)
    if liver_score is None:
        flags.append("liver_missing")
    components["liver"] = liver_score

    # 心血管
    cv_score, cv_info = _calc_cardiovascular(observations, eval_time, medications, weight_kg)
    info.update(cv_info)
    if cv_score is None:
        flags.append("cardiovascular_missing")
    components["cardiovascular"] = cv_score

    # 中枢神经
    cns_score, cns_info = _calc_cns(observations, eval_time)
    info.update(cns_info)
    if cns_score is None:
        flags.append("cns_missing")
    components["central_nervous_system"] = cns_score

    # 肾脏
    renal_score, renal_info = _calc_renal(observations, eval_time)
    info.update(renal_info)
    if renal_score is None:
        flags.append("renal_missing")
    components["renal"] = renal_score

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

    # 计算总分 (仅对有值的器官求和)
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

    return {
        "sofa_score": total,
        "components": components,
        "data_quality_flags": flags,
        "meta": info,
        "result_status": result_status,
        "completeness": completeness,
    }
