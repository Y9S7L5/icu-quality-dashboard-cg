"""
SOFA 评分桥接层。
将 data_adapter 的输出喂入经典 SOFA 和 SOFA-2 评分器，
返回结构化评分结果，附加版本元数据。

铁律:
  - 本模块只做数据传递和结果包装，不做评分计算
  - 评分器返回 None 的分项如实传递，不得回填 0
  - 版本元数据从 sofa_rules.py 读取，不得硬编码
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from tzutil import as_utc  # noqa: E402 — naive = 真 UTC（收敛点）

logger = logging.getLogger(__name__)


def _aware(dt: datetime) -> datetime:
    """
    确保时区感知。naive = 真 UTC（存储帧铁律，2026-10 实证）→ tzutil.as_utc。
    """
    return as_utc(dt)


def compute_sofa_scores(
    sc_pid: str,
    mrn: str,
    dc_pid: str,
    t0: datetime,
    eval_time: Optional[datetime] = None,
    weight_kg: Optional[float] = None,
    batch_lab_cache: Optional[dict] = None,
) -> dict:
    """
    正式 SOFA 评分入口。
    从 MongoDB 提取数据，运行经典 SOFA 和 SOFA-2 评分器，返回完整结果。

    Args:
        sc_pid: SmartCare patient _id
        mrn: 病案号
        dc_pid: DataCenter pid
        t0: T0 时间
        eval_time: 评估时间，默认 T0+24h
        weight_kg: 体重 (可选)
        batch_lab_cache: 批量预加载的检验数据缓存 {his_pid: {observations, status, ...}}

    Returns:
        {
            "classic": {sofa_score, components, data_quality_flags, meta, result_status, completeness},
            "sofa2": {sofa2_score, components, data_quality_flags, meta, result_status, completeness},
            "eval_time": datetime,
            "t0": datetime,
            "fetch_meta": {...},
            "data_quality_flags": [...],
            "version_meta": {classic: {...}, sofa2: {...}},
            "data_complete": bool,
        }
    """
    from scoring.data_adapter import fetch_patient_obs_meds
    from scoring.sofa_core import compute_sofa_classic
    from scoring.sofa2_core import compute_sofa2
    from scoring.sofa_rules import CLASSIC_SOFA_META, SOFA2_META

    if t0 is None:
        return _empty_result("NO_T0")

    t0 = _aware(t0)
    if eval_time is None:
        eval_time = t0 + timedelta(hours=24)
    eval_time = _aware(eval_time)

    # 1. 提取数据（支持批量缓存）
    data = fetch_patient_obs_meds(sc_pid, mrn, dc_pid, t0, eval_time, weight_kg,
                                  batch_lab_cache=batch_lab_cache)
    observations = data["observations"]
    medications = data["medications"]
    has_advanced_support = data["has_advanced_support"]
    ventilator_status = data.get("ventilator_status", {"is_active": False, "status": "unknown", "details": {}})
    w_kg = data["weight_kg"]
    flags = list(data["data_quality_flags"])
    fetch_meta = data["fetch_meta"]
    data_complete = data.get("data_complete", True)

    # 2. 运行经典 SOFA
    classic_result = None
    try:
        classic_result = compute_sofa_classic(
            observations=observations,
            medications=medications,
            eval_time=eval_time,
            has_advanced_support=has_advanced_support,
            weight_kg=w_kg,
        )
    except Exception as e:
        logger.warning("Classic SOFA computation failed for sc_pid=%s: %s", sc_pid, e)
        flags.append(f"classic_sofa_error: {e}")

    # 3. 运行 SOFA-2
    sofa2_result = None
    try:
        sofa2_result = compute_sofa2(
            observations=observations,
            medications=medications,
            eval_time=eval_time,
            has_advanced_support=has_advanced_support,
            weight_kg=w_kg,
        )
    except Exception as e:
        logger.warning("SOFA-2 computation failed for sc_pid=%s: %s", sc_pid, e)
        flags.append(f"sofa2_error: {e}")

    # 4. 构建返回
    classic_out = classic_result or _empty_classic_result()
    sofa2_out = sofa2_result or _empty_sofa2_result()

    # 附加评分器内部的 data_quality_flags
    if classic_result:
        flags.extend(classic_result.get("data_quality_flags", []))
    if sofa2_result:
        flags.extend(sofa2_result.get("data_quality_flags", []))

    return {
        "classic": classic_out,
        "sofa2": sofa2_out,
        "eval_time": eval_time,
        "t0": t0,
        "fetch_meta": fetch_meta,
        "data_quality_flags": list(set(flags)),
        "ventilator_status": ventilator_status,
        "has_advanced_support": has_advanced_support,
        "version_meta": {
            "classic": CLASSIC_SOFA_META,
            "sofa2": SOFA2_META,
        },
        "data_complete": data_complete,
    }


def compute_sofa_scores_from_data(
    observations: List[dict],
    medications: List[dict],
    eval_time: datetime,
    has_advanced_support: bool = False,
    weight_kg: Optional[float] = None,
    t0: Optional[datetime] = None,
) -> dict:
    """
    直接从预构建的观测/用药数据计算 SOFA 评分。
    用于测试或已有数据的场景。

    Args:
        observations: 标准化观测列表
        medications: 标准化用药列表
        eval_time: 评估时间
        has_advanced_support: 是否有高级呼吸支持
        weight_kg: 体重
        t0: T0 时间 (可选)

    Returns:
        与 compute_sofa_scores 相同结构
    """
    from scoring.sofa_core import compute_sofa_classic
    from scoring.sofa2_core import compute_sofa2
    from scoring.sofa_rules import CLASSIC_SOFA_META, SOFA2_META

    eval_time = _aware(eval_time)
    if t0 is not None:
        t0 = _aware(t0)

    flags = []

    # 运行经典 SOFA
    classic_result = None
    try:
        classic_result = compute_sofa_classic(
            observations=observations,
            medications=medications,
            eval_time=eval_time,
            has_advanced_support=has_advanced_support,
            weight_kg=weight_kg,
        )
    except Exception as e:
        logger.warning("Classic SOFA computation failed: %s", e)
        flags.append(f"classic_sofa_error: {e}")

    # 运行 SOFA-2
    sofa2_result = None
    try:
        sofa2_result = compute_sofa2(
            observations=observations,
            medications=medications,
            eval_time=eval_time,
            has_advanced_support=has_advanced_support,
            weight_kg=weight_kg,
        )
    except Exception as e:
        logger.warning("SOFA-2 computation failed: %s", e)
        flags.append(f"sofa2_error: {e}")

    classic_out = classic_result or _empty_classic_result()
    sofa2_out = sofa2_result or _empty_sofa2_result()

    if classic_result:
        flags.extend(classic_result.get("data_quality_flags", []))
    if sofa2_result:
        flags.extend(sofa2_result.get("data_quality_flags", []))

    return {
        "classic": classic_out,
        "sofa2": sofa2_out,
        "eval_time": eval_time,
        "t0": t0,
        "fetch_meta": {},
        "data_quality_flags": list(set(flags)),
        "version_meta": {
            "classic": CLASSIC_SOFA_META,
            "sofa2": SOFA2_META,
        },
    }


# ============================================================
# 临床识别层构建
# ============================================================

def build_clinical_layer(
    sofa_result: dict,
    infection_evidence: Optional[dict] = None,
    has_vasopressor_wide: bool = False,
    lactate_value: Optional[float] = None,
    map_value: Optional[float] = None,
    has_fluid_resuscitation: Optional[bool] = None,
    vaso_name: Optional[str] = None,
) -> dict:
    """
    基于 SOFA 评分结果构建五层临床识别结构。

    关键改进:
    1. 候选信号(candidate_signal)与临床确认(clinical_confirmation_status)分离
    2. 部分评分标记 measured_component_sum / score_lower_bound / missing_components
    3. 基线未知时标记假设，不默认已确认
    4. 休克确认: MAP恢复不绕过容量判断; has_fluid=False不自动confirmed
    5. 6h Bundle: rule_pending 状态

    Returns:
        五层临床识别结构
    """
    from config.indicator_windows import SOFA_GATE_MODE
    from config.candidate_rules import BUNDLE_6H_STATUS

    # #fix: .get(key, {}) returns None when key exists with None value
    sofa2 = sofa_result.get("sofa2") or {}
    classic = sofa_result.get("classic") or {}

    sofa2_score = sofa2.get("sofa2_score")
    sofa2_components = sofa2.get("components", {})
    sofa2_completeness = sofa2.get("completeness", 0.0)
    sofa2_status = sofa2.get("result_status", "insufficient")

    classic_score = classic.get("sofa_score")

    # ---- Layer 1: 感染证据 ----
    if infection_evidence is None:
        infection_evidence = {}
    has_infection = infection_evidence.get("has_infection")
    layer1 = {
        "has_infection": has_infection,
        "i1": infection_evidence.get("i1"),
        "i2": infection_evidence.get("i2"),
        "i3": infection_evidence.get("i3"),
    }

    # ---- Layer 2: 急性器官功能障碍 ----
    # SOFA-2 基线 (从 sofa_result 获取)
    # #fix: .get(key, {}) returns None when key exists with None value
    sofa2_baseline_info = sofa_result.get("sofa2_baseline") or {}
    baseline_sofa2 = sofa2_baseline_info.get("sofa2_score")
    baseline_status = sofa2_baseline_info.get("baseline_status", "unknown")
    baseline_known = baseline_sofa2 is not None
    delta = None
    if sofa2_score is not None and baseline_sofa2 is not None:
        delta = sofa2_score - baseline_sofa2

    # 部分评分: 计算下限和缺失组件
    measured_component_sum = None
    score_lower_bound = None
    missing_components = []
    if sofa2_status == "partial":
        valid_scores = [s for s in sofa2_components.values() if s is not None]
        measured_component_sum = sum(valid_scores) if valid_scores else 0
        score_lower_bound = measured_component_sum
        missing_components = [k for k, v in sofa2_components.items() if v is None]

    # 当前总分≥2 → 有器官功能障碍信号
    current_score_elevated = sofa2_score is not None and sofa2_score >= 2

    # 急性变化判定
    has_acute_organ_dysfunction = None
    acute_basis = "unknown"
    acute_assumption_applied = False

    if delta is not None:
        # 基线已知 → 用 delta 判定
        if delta >= 2:
            has_acute_organ_dysfunction = True
            acute_basis = "delta_ge_2"
        else:
            has_acute_organ_dysfunction = False
            acute_basis = "delta_lt_2"
    elif current_score_elevated:
        # 基线未知但当前≥2 → 门控逻辑仍判True，但标记假设
        has_acute_organ_dysfunction = True
        acute_basis = "baseline_unknown_current_ge_2"
        acute_assumption_applied = True
    elif sofa2_score is not None and sofa2_score < 2:
        # 当前总分<2 → 不满足器官功能障碍
        has_acute_organ_dysfunction = False
        acute_basis = "current_lt_2"
    elif sofa2_status == "insufficient":
        has_acute_organ_dysfunction = None
        acute_basis = "insufficient_data"

    # 部分评分: 不能直接确认急性变化
    if sofa2_status == "partial" and has_acute_organ_dysfunction is True:
        if acute_basis == "baseline_unknown_current_ge_2":
            # 部分评分+基线未知 → 候选信号但不确认
            pass  # has_acute_organ_dysfunction 保持 True 作为候选信号

    layer2 = {
        "sofa2_total": sofa2_score,
        "sofa2_baseline": baseline_sofa2,
        "sofa2_delta": delta,
        "baseline_known": baseline_known,
        "baseline_status": baseline_status,
        "acute_assumption_applied": acute_assumption_applied,
        "sofa2_components": sofa2_components,
        "sofa2_completeness": sofa2_completeness,
        "sofa2_result_status": sofa2_status,
        "classic_total": classic_score,
        "has_acute_organ_dysfunction": has_acute_organ_dysfunction,
        "acute_basis": acute_basis,
        "current_score_elevated": current_score_elevated,
        # 部分评分详情
        "measured_component_sum": measured_component_sum,
        "score_lower_bound": score_lower_bound,
        "missing_components": missing_components,
        # S1-S4 保留为辅助信号
        "s1_signal": _organ_signal(sofa2_components, "respiratory"),
        "s2_signal": _organ_signal(classic.get("components", {}), "central_nervous_system"),
        "s3_signal": _organ_signal(classic.get("components", {}), "cardiovascular"),
        "s4_signal": has_vasopressor_wide,
    }

    # ---- Layer 3: 脓毒症判定 ----
    is_sepsis = None
    sepsis_basis = "sepsis3_sofa2"
    if has_infection is True and has_acute_organ_dysfunction is True:
        is_sepsis = True
        if acute_basis == "baseline_unknown_current_ge_2":
            sepsis_basis = "sepsis3_sofa2_pending_baseline"
        elif sofa2_status == "partial":
            sepsis_basis = "sepsis3_sofa2_partial_score"
        else:
            sepsis_basis = "sepsis3_sofa2_confirmed"
    elif has_infection is False:
        is_sepsis = False
    elif has_acute_organ_dysfunction is False and has_infection is True:
        is_sepsis = False

    layer3 = {
        "is_sepsis": is_sepsis,
        "sepsis_basis": sepsis_basis,
        "gate_mode": SOFA_GATE_MODE,
    }

    # ---- Layer 4: 脓毒性休克判定 ----
    # 修复: MAP恢复不绕过容量判断; has_fluid=False不自动confirmed
    shock_criteria = {
        "sepsis_confirmed": is_sepsis,
        "sepsis_basis": sepsis_basis,
        "map_below_65_observed": None,  # 修复: 单次MAP不等于持续低血压
        "vasopressor_required": None,
        "vasopressor_active_at_eval": None,
        "lactate_gt_2": None,
        "lactate_borderline": None,
        "volume_assessed": has_fluid_resuscitation,
        "volume_adequate": None,
    }

    # 乳酸判定 (严格 >2, 不是 ≥2)
    lactate_borderline = False
    if lactate_value is not None:
        if lactate_value == 2.0:
            lactate_borderline = True
            shock_criteria["lactate_gt_2"] = False
            shock_criteria["lactate_borderline"] = True
        else:
            shock_criteria["lactate_gt_2"] = lactate_value > 2
            shock_criteria["lactate_borderline"] = False

    # MAP 观测 (修复: 不是"持续"低血压，是"观测到"低于65)
    map_recovered = False
    if map_value is not None:
        shock_criteria["map_below_65_observed"] = map_value < 65
        if map_value >= 65 and has_vasopressor_wide:
            map_recovered = True

    # 升压药需求
    shock_criteria["vasopressor_required"] = has_vasopressor_wide
    shock_criteria["vasopressor_active_at_eval"] = has_vasopressor_wide

    # 综合判定 (修复: MAP恢复不绕过容量判断)
    shock_status = _determine_shock_status(
        is_sepsis=is_sepsis,
        has_vasopressor=has_vasopressor_wide,
        lactate_value=lactate_value,
        lactate_borderline=lactate_borderline,
        map_value=map_value,
        map_recovered=map_recovered,
        has_fluid=has_fluid_resuscitation,
    )

    layer4 = {
        "shock_status": shock_status,
        "criteria": shock_criteria,
        "lactate_value": lactate_value,
        "lactate_borderline": lactate_borderline,
        "map_recovered": map_recovered,
        "vaso_name": vaso_name,
    }

    # ---- Layer 5: Bundle 完成 ----
    # 6h: 如果规则待确认，显示 status=rule_pending
    layer5 = {
        "1h": None,
        "3h": None,
        "6h": {"status": BUNDLE_6H_STATUS} if BUNDLE_6H_STATUS == "rule_pending" else None,
    }

    # ---- 候选信号与临床确认分离 ----
    from scoring.candidate_engine import extract_candidate
    candidate_info = extract_candidate(
        diagnosis_text=infection_evidence.get("diagnosis_text"),
        infection_evidence=infection_evidence,
        has_vasopressor_wide=has_vasopressor_wide,
        lactate_value=lactate_value,
        map_value=map_value,
        sofa2_result=sofa2,
        classic_sofa_result=classic,
        has_fluid_resuscitation=has_fluid_resuscitation,
        s1_s4_signals={
            "s1": _organ_signal_bool(sofa2_components, "respiratory"),
            "s2": _organ_signal_bool(classic.get("components", {}), "central_nervous_system"),
            "s3": _organ_signal_bool(classic.get("components", {}), "cardiovascular"),
            "s4": has_vasopressor_wide,
        },
    )

    return {
        "layer1_infection": layer1,
        "layer2_organ_dysfunction": layer2,
        "layer3_sepsis": layer3,
        "layer4_shock": layer4,
        "layer5_bundle": layer5,
        "candidate": candidate_info,
    }


def _organ_signal(components: dict, organ: str) -> Optional[bool]:
    """将器官分值转换为辅助信号 (≥1 为 True)。"""
    score = components.get(organ)
    if score is None:
        return None
    return score >= 1


def _organ_signal_bool(components: dict, organ: str) -> Optional[bool]:
    """将器官分值转换为布尔信号，None时返回None。"""
    score = components.get(organ)
    if score is None:
        return None
    return score >= 1


def _determine_shock_status(
    is_sepsis: Optional[bool],
    has_vasopressor: bool,
    lactate_value: Optional[float],
    lactate_borderline: bool,
    map_value: Optional[float],
    map_recovered: bool,
    has_fluid: Optional[bool],
) -> str:
    """
    综合判定脓毒性休克状态。
    返回: "confirmed" | "not_confirmed" | "pending_review" | "borderline" | "insufficient_data"

    脓毒性休克 = 脓毒症 + (持续需要升压药维持MAP ≥65) + 乳酸>2mmol/L
    容量状态不明时返回 pending_review，不得自动 confirmed。
    """
    # 脓毒症未确认 → 不可能是脓毒性休克
    if is_sepsis is False:
        return "not_confirmed"
    if is_sepsis is None:
        return "insufficient_data"

    # 脓毒症已确认，检查休克条件
    # 需要: 升压药 + 乳酸>2
    if not has_vasopressor:
        return "not_confirmed"

    # 乳酸边界值
    if lactate_borderline:
        return "borderline"

    # 乳酸缺失
    if lactate_value is None:
        if has_vasopressor:
            return "pending_review"  # 有升压药但乳酸缺失
        return "insufficient_data"

    # 乳酸 ≤ 2
    if lactate_value <= 2:
        return "not_confirmed"

    # 乳酸 > 2 + 升压药
    # 检查 MAP 状态
    if map_recovered:
        # MAP 已恢复但仍依赖升压药
        # 升压药依赖本身就是休克证据，但容量状态必须已评估才能 confirmed
        if has_fluid is True:
            return "confirmed"
        # 容量未知或明确不满足 → pending_review (候选但不自动确认)
        return "pending_review"

    if map_value is not None and map_value >= 65 and not map_recovered:
        # MAP 正常且无升压药依赖 → 不确定
        return "pending_review"

    # 液体复苏状态
    if has_fluid is None:
        return "pending_review"  # 液体复苏状态未知，不得自动confirmed

    if has_fluid is False:
        # 明确不满足容量复苏 → pending_review (候选但不自动确认)
        return "pending_review"

    # 满足所有条件: 升压药 + 乳酸>2 + MAP低 + 容量已评估
    return "confirmed"


def _empty_result(reason: str) -> dict:
    """返回空评分结果。"""
    return {
        "classic": _empty_classic_result(),
        "sofa2": _empty_sofa2_result(),
        "eval_time": None,
        "t0": None,
        "fetch_meta": {},
        "data_quality_flags": [reason],
        "version_meta": {},
    }


def _empty_classic_result() -> dict:
    return {
        "sofa_score": None,
        "components": {
            "respiratory": None, "coagulation": None, "liver": None,
            "cardiovascular": None, "central_nervous_system": None, "renal": None,
        },
        "data_quality_flags": ["no_data"],
        "meta": {},
        "result_status": "insufficient",
        "completeness": 0.0,
    }


def _empty_sofa2_result() -> dict:
    return {
        "sofa2_score": None,
        "components": {
            "respiratory": None, "hemostasis": None, "liver": None,
            "brain": None, "kidney": None, "cardiovascular": None,
        },
        "data_quality_flags": ["no_data"],
        "meta": {},
        "result_status": "insufficient",
        "completeness": 0.0,
    }
