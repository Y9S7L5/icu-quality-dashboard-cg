"""
MongoDB 检验数据批量查询模块。
消除 N+1 查询，支持按患者批次查询 VI_ICU_EXAM 和 VI_ICU_EXAM_ITEM。

核心设计：
1. 一次传入一批 his_pid
2. 批量查询 VI_ICU_EXAM
3. 建立 his_pid → examID/reportID 映射
4. examID/reportID 分批查询 VI_ICU_EXAM_ITEM
5. 在 Python 内按患者和时间窗分配
6. $in 列表按合理大小分块（200-500）
7. 禁止对每个患者重复扫描大表
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from tzutil import as_utc  # noqa: E402 — naive = 真 UTC（收敛点）

# $in 列表分块大小
CHUNK_SIZE = 200

# itemCode → 标准码映射 (多编码兼容)
LAB_MAP = {
    "PLT": "PLT",
    "platelet": "PLT",
    "TBIL": "TBIL",
    "sCr": "CREA",
    "Cr": "CREA",
    "CREA": "CREA",
}


def _aware(dt: datetime) -> datetime:
    """
    确保时区感知。naive = 真 UTC（存储帧铁律，2026-10 实证）→ tzutil.as_utc。
    """
    return as_utc(dt)


def _safe_float(val) -> Optional[float]:
    """安全转 float，失败返回 None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def batch_fetch_lab_observations(
    dc,
    his_pids: List[str],
    eval_time: datetime,
    lookback_hours: int = 24,
    seen: Optional[set] = None,
) -> Dict[str, dict]:
    """
    批量查询检验数据，替代逐患者 N+1 查询。

    Args:
        dc: DataCenter 数据库连接
        his_pids: 批量患者 ID 列表
        eval_time: 评估时间
        lookback_hours: 回溯窗口（小时）
        seen: 可选的外部 seen 集合，用于跨批次去重

    Returns:
        {his_pid: {"observations": [...], "status": str, "error": str|None, "data_complete": bool}}
        status 取值: "success" | "no_data" | "timeout" | "query_error"
    """
    window_start = eval_time - timedelta(hours=lookback_hours)

    # 初始化结果：每个患者有 observations, status, error, data_complete
    result = {pid: {"observations": [], "status": "success", "error": None, "data_complete": True}
              for pid in his_pids}

    # 如果未提供 seen 集合，使用内部集合
    internal_seen = seen is None
    if internal_seen:
        seen = set()

    if not his_pids or dc is None:
        if dc is None:
            for pid in his_pids:
                result[pid] = {"observations": [], "status": "query_error",
                               "error": "DataCenter unavailable", "data_complete": False}
        return result

    logger.info("batch_fetch_lab: %d patients, window=%s to %s",
                len(his_pids), window_start, eval_time)

    # Step 1: 分块查询 VI_ICU_EXAM
    exam_by_pid = {}  # his_pid → [{examID, reportID, collectTime}]
    chunk_failed_pids = set()

    for i in range(0, len(his_pids), CHUNK_SIZE):
        chunk = his_pids[i:i + CHUNK_SIZE]

        try:
            exam_docs = list(dc.VI_ICU_EXAM.find(
                {
                    # #fix: 原查 "pid" —— VI_ICU_EXAM 实际字段为 hisPid（0 文档含 pid），
                    # 恒返回空 → 检验链路静默 no_data → SOFA 肝/肾/凝血分项全缺
                    "hisPid": {"$in": chunk},
                    "collectTime": {"$gte": window_start, "$lte": eval_time}
                },
                {"hisPid": 1, "examID": 1, "reportID": 1, "collectTime": 1}
            ).max_time_ms(15000).limit(5000))

            # 检测 limit 截断
            if len(exam_docs) >= 5000:
                logger.warning("VI_ICU_EXAM chunk %d-%d hit limit(5000), possible truncation",
                             i, i + CHUNK_SIZE)

            for doc in exam_docs:
                pid = doc.get("hisPid")  # #fix: VI_ICU_EXAM 字段为 hisPid（原读 pid 恒 None）
                if pid not in exam_by_pid:
                    exam_by_pid[pid] = []
                exam_by_pid[pid].append({
                    "examID": doc.get("examID"),
                    "reportID": doc.get("reportID"),
                    "collectTime": doc.get("collectTime")
                })

            logger.debug("VI_ICU_EXAM chunk %d-%d: %d docs", i, i + CHUNK_SIZE, len(exam_docs))

        except Exception as e:
            error_msg = str(e)
            if "MaxTimeMSExpired" in error_msg:
                chunk_status = "timeout"
            else:
                chunk_status = "query_error"

            for pid in chunk:
                result[pid] = {"observations": [], "status": chunk_status,
                               "error": error_msg, "data_complete": False}
                chunk_failed_pids.add(pid)

            logger.warning("VI_ICU_EXAM batch query failed for chunk %d-%d: %s",
                         i, i + CHUNK_SIZE, e)
            continue

    # 标记无检验数据的患者（不是查询失败，而是确实没有数据）
    for pid in his_pids:
        if pid not in exam_by_pid and pid not in chunk_failed_pids:
            result[pid] = {"observations": [], "status": "no_data",
                           "error": None, "data_complete": True}

    if not exam_by_pid:
        logger.info("batch_fetch_lab: no exam docs found")
        return result

    # Step 2: 收集所有 examID 和 reportID
    all_exam_ids = []
    all_report_ids = []
    exam_time_map = {}  # str(id) → collectTime
    exam_pid_map = {}   # str(id) → his_pid

    for pid, exams in exam_by_pid.items():
        for exam in exams:
            eid = exam.get("examID")
            rid = exam.get("reportID")
            ct = exam.get("collectTime")

            if eid:
                eid_str = str(eid)
                all_exam_ids.append(eid)
                exam_time_map[eid_str] = ct
                exam_pid_map[eid_str] = pid

            if rid:
                rid_str = str(rid)
                all_report_ids.append(rid)
                exam_time_map[rid_str] = ct
                exam_pid_map[rid_str] = pid

    if not all_exam_ids and not all_report_ids:
        logger.info("batch_fetch_lab: no exam/report IDs found")
        return result

    logger.info("batch_fetch_lab: %d exam IDs, %d report IDs",
                len(all_exam_ids), len(all_report_ids))

    # Step 3: 批量查询 VI_ICU_EXAM_ITEM
    target_item_codes = list(LAB_MAP.keys())

    # 分块查询 examID
    for i in range(0, len(all_exam_ids), CHUNK_SIZE):
        chunk = all_exam_ids[i:i + CHUNK_SIZE]

        try:
            items = list(dc.VI_ICU_EXAM_ITEM.find(
                {
                    "examID": {"$in": chunk},
                    "itemCode": {"$in": target_item_codes}
                },
                {"examID": 1, "itemCode": 1, "itemValue": 1, "result": 1,
                 "unit": 1, "itemName": 1}
            ).max_time_ms(15000).limit(10000))

            # 检测 limit 截断
            if len(items) >= 10000:
                logger.warning("VI_ICU_EXAM_ITEM exam chunk %d-%d hit limit(10000), possible truncation",
                             i, i + CHUNK_SIZE)

            # 处理查询结果
            _process_exam_items(items, exam_time_map, exam_pid_map,
                              LAB_MAP, result, window_start, eval_time, seen)

            logger.debug("VI_ICU_EXAM_ITEM exam chunk %d-%d: %d items",
                        i, i + CHUNK_SIZE, len(items))

        except Exception as e:
            error_msg = str(e)
            if "MaxTimeMSExpired" in error_msg:
                chunk_status = "timeout"
            else:
                chunk_status = "query_error"

            # 找出受影响的患者
            affected_pids = set()
            for eid in chunk:
                pid = exam_pid_map.get(str(eid))
                if pid:
                    affected_pids.add(pid)

            for pid in affected_pids:
                if result[pid]["status"] == "success":
                    result[pid]["status"] = chunk_status
                    result[pid]["error"] = error_msg
                    result[pid]["data_complete"] = False

            logger.warning("VI_ICU_EXAM_ITEM batch query failed for exam chunk %d-%d: %s",
                         i, i + CHUNK_SIZE, e)
            continue

    # 分块查询 reportID（如果存在）
    for i in range(0, len(all_report_ids), CHUNK_SIZE):
        chunk = all_report_ids[i:i + CHUNK_SIZE]

        try:
            items = list(dc.VI_ICU_EXAM_ITEM.find(
                {
                    # #fix: ITEM 表无 reportID 字段 —— 其 examID 实际引用主表 reportID
                    # （原查 reportID 恒 0 条 → reportID 型检验永远丢失）
                    "examID": {"$in": chunk},
                    "itemCode": {"$in": target_item_codes}
                },
                {"examID": 1, "itemCode": 1, "itemValue": 1, "result": 1,
                 "unit": 1, "itemName": 1}
            ).max_time_ms(15000).limit(10000))

            # 检测 limit 截断
            if len(items) >= 10000:
                logger.warning("VI_ICU_EXAM_ITEM report chunk %d-%d hit limit(10000), possible truncation",
                             i, i + CHUNK_SIZE)

            _process_exam_items(items, exam_time_map, exam_pid_map,
                              LAB_MAP, result, window_start, eval_time, seen)

            logger.debug("VI_ICU_EXAM_ITEM report chunk %d-%d: %d items",
                        i, i + CHUNK_SIZE, len(items))

        except Exception as e:
            error_msg = str(e)
            if "MaxTimeMSExpired" in error_msg:
                chunk_status = "timeout"
            else:
                chunk_status = "query_error"

            affected_pids = set()
            for rid in chunk:
                pid = exam_pid_map.get(str(rid))
                if pid:
                    affected_pids.add(pid)

            for pid in affected_pids:
                if result[pid]["status"] == "success":
                    result[pid]["status"] = chunk_status
                    result[pid]["error"] = error_msg
                    result[pid]["data_complete"] = False

            logger.warning("VI_ICU_EXAM_ITEM batch query failed for report chunk %d-%d: %s",
                         i, i + CHUNK_SIZE, e)
            continue

    # 统计结果
    total_obs = sum(len(r["observations"]) for r in result.values())
    patients_with_data = sum(1 for r in result.values() if r["observations"])
    patients_timeout = sum(1 for r in result.values() if r["status"] == "timeout")
    patients_query_error = sum(1 for r in result.values() if r["status"] == "query_error")
    patients_no_data = sum(1 for r in result.values() if r["status"] == "no_data")
    logger.info("batch_fetch_lab complete: %d/%d patients with data, %d observations "
                "(timeout=%d, query_error=%d, no_data=%d)",
                patients_with_data, len(his_pids), total_obs,
                patients_timeout, patients_query_error, patients_no_data)

    return result


def _process_exam_items(
    items: List[dict],
    exam_time_map: Dict[str, datetime],
    exam_pid_map: Dict[str, str],
    lab_map: Dict[str, str],
    result: Dict[str, Any],
    window_start: datetime,
    eval_time: datetime,
    seen: Optional[set] = None,
) -> None:
    """
    处理检验项目结果，去重并分配给患者。

    Args:
        items: VI_ICU_EXAM_ITEM 查询结果
        exam_time_map: exam_id_str → collectTime
        exam_pid_map: exam_id_str → his_pid
        lab_map: itemCode → std_code 映射
        result: 输出结果字典 {his_pid: {"observations": [...], ...}}
        window_start: 时间窗口开始
        eval_time: 评估时间
        seen: 可选的外部 seen 集合，用于跨批次去重
    """
    # 如果未提供 seen 集合，使用内部集合
    internal_seen = seen is None
    if internal_seen:
        seen = set()

    for doc in items:
        # 提取检验值
        raw_val = _safe_float(doc.get("itemValue"))
        if raw_val is None:
            raw_val = _safe_float(doc.get("result"))
        if raw_val is None:
            continue

        # 获取关联 ID
        exam_id = doc.get("examID") or doc.get("reportID")
        if not exam_id:
            continue

        exam_id_str = str(exam_id)

        # 获取患者 PID
        pid = exam_pid_map.get(exam_id_str)
        if not pid or pid not in result:
            continue

        # 获取检验时间
        collect_time = exam_time_map.get(exam_id_str)
        if not collect_time or not isinstance(collect_time, datetime):
            continue

        # 时间窗口校验
        collect_time_aware = _aware(collect_time)
        # window_start/eval_time 为 naive(=真UTC)，collect_time_aware 是 UTC-aware，
        # 统一经 _aware 归一后比较，避免 naive/aware 混比 TypeError
        ws_aware = _aware(window_start)
        ev_aware = _aware(eval_time)
        if collect_time_aware < ws_aware or collect_time_aware > ev_aware:
            continue

        # 去重
        item_code = doc.get("itemCode")
        dedup_key = (pid, exam_id_str, item_code)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # 标准化代码
        std_code = lab_map.get(item_code)
        if not std_code:
            continue

        # 添加观测
        result[pid]["observations"].append({
            "code": std_code,
            "value_number": raw_val,
            "unit": (doc.get("unit") or "").strip(),
            "observed_at": collect_time_aware,
            "item_name": doc.get("itemName", ""),
            "source": "VI_ICU_EXAM_ITEM",
        })


def batch_fetch_lab_observations_with_status(
    dc,
    his_pids: List[str],
    eval_time: datetime,
    lookback_hours: int = 24,
    seen: Optional[set] = None,
) -> Tuple[Dict[str, List[dict]], Dict[str, str], List[str]]:
    """
    带状态返回的批量查询版本。

    Returns:
        (observations_by_pid, status_by_pid, failed_pids)
        - observations_by_pid: {his_pid: [observations]}
        - status_by_pid: {his_pid: status}
          status 取值: "success" | "no_data" | "timeout" | "query_error"
        - failed_pids: 查询失败的患者列表
    """
    # 如果未提供 seen 集合，使用内部集合
    internal_seen = seen is None
    if internal_seen:
        seen = set()
    window_start = eval_time - timedelta(hours=lookback_hours)
    # 使用与 batch_fetch_lab_observations 相同的格式
    result = {pid: {"observations": [], "status": "success", "error": None, "data_complete": True}
              for pid in his_pids}
    status = {pid: "success" for pid in his_pids}
    failed_pids = []

    if not his_pids or dc is None:
        if dc is None:
            for pid in his_pids:
                result[pid] = {"observations": [], "status": "query_error",
                               "error": "DataCenter unavailable", "data_complete": False}
                status[pid] = "query_error"
            failed_pids = list(his_pids)
        return result, status, failed_pids

    logger.info("batch_fetch_lab_with_status: %d patients", len(his_pids))

    # Step 1: 分块查询 VI_ICU_EXAM
    exam_by_pid = {}  # his_pid → [{examID, reportID, collectTime}]

    for i in range(0, len(his_pids), CHUNK_SIZE):
        chunk = his_pids[i:i + CHUNK_SIZE]

        try:
            exam_docs = list(dc.VI_ICU_EXAM.find(
                {
                    # #fix: 原查 "pid" —— VI_ICU_EXAM 实际字段为 hisPid（0 文档含 pid），
                    # 恒返回空 → 检验链路静默 no_data → SOFA 肝/肾/凝血分项全缺
                    "hisPid": {"$in": chunk},
                    "collectTime": {"$gte": window_start, "$lte": eval_time}
                },
                {"hisPid": 1, "examID": 1, "reportID": 1, "collectTime": 1}
            ).max_time_ms(15000).limit(5000))

            for doc in exam_docs:
                pid = doc.get("hisPid")  # #fix: VI_ICU_EXAM 字段为 hisPid（原读 pid 恒 None）
                if pid not in exam_by_pid:
                    exam_by_pid[pid] = []
                exam_by_pid[pid].append({
                    "examID": doc.get("examID"),
                    "reportID": doc.get("reportID"),
                    "collectTime": doc.get("collectTime")
                })

        except Exception as e:
            error_msg = str(e)
            if "MaxTimeMSExpired" in error_msg:
                chunk_status = "timeout"
            else:
                chunk_status = "query_error"

            for pid in chunk:
                status[pid] = chunk_status
                result[pid] = {"observations": [], "status": chunk_status,
                               "error": error_msg, "data_complete": False}
                failed_pids.append(pid)

            logger.warning("VI_ICU_EXAM batch query failed for chunk %d-%d: %s",
                         i, i + CHUNK_SIZE, e)
            continue

    # 标记无检验数据的患者
    for pid in his_pids:
        if pid not in exam_by_pid and pid not in [p for p in failed_pids] and status[pid] == "success":
            status[pid] = "no_data"
            result[pid] = {"observations": [], "status": "no_data",
                           "error": None, "data_complete": True}

    if not exam_by_pid:
        return result, status, failed_pids

    # Step 2: 收集所有 examID 和 reportID
    all_exam_ids = []
    all_report_ids = []
    exam_time_map = {}  # str(id) → collectTime
    exam_pid_map = {}   # str(id) → his_pid

    for pid, exams in exam_by_pid.items():
        for exam in exams:
            eid = exam.get("examID")
            rid = exam.get("reportID")
            ct = exam.get("collectTime")

            if eid:
                eid_str = str(eid)
                all_exam_ids.append(eid)
                exam_time_map[eid_str] = ct
                exam_pid_map[eid_str] = pid

            if rid:
                rid_str = str(rid)
                all_report_ids.append(rid)
                exam_time_map[rid_str] = ct
                exam_pid_map[rid_str] = pid

    if not all_exam_ids and not all_report_ids:
        return result, status, failed_pids

    # Step 3: 批量查询 VI_ICU_EXAM_ITEM
    target_item_codes = list(LAB_MAP.keys())

    # 分块查询 examID
    for i in range(0, len(all_exam_ids), CHUNK_SIZE):
        chunk = all_exam_ids[i:i + CHUNK_SIZE]

        try:
            items = list(dc.VI_ICU_EXAM_ITEM.find(
                {
                    "examID": {"$in": chunk},
                    "itemCode": {"$in": target_item_codes}
                },
                {"examID": 1, "itemCode": 1, "itemValue": 1, "result": 1,
                 "unit": 1, "itemName": 1}
            ).max_time_ms(15000).limit(10000))

            _process_exam_items(items, exam_time_map, exam_pid_map,
                              LAB_MAP, result, window_start, eval_time, seen)

        except Exception as e:
            error_msg = str(e)
            # 找出受影响的患者
            affected_pids = set()
            for eid in chunk:
                pid = exam_pid_map.get(str(eid))
                if pid:
                    affected_pids.add(pid)

            if "MaxTimeMSExpired" in error_msg:
                chunk_status = "timeout"
            else:
                chunk_status = "query_error"

            for pid in affected_pids:
                if status[pid] == "success":  # 只覆盖成功状态
                    status[pid] = chunk_status
                    result[pid] = {"observations": result[pid]["observations"],
                                   "status": chunk_status, "error": error_msg,
                                   "data_complete": False}
                    failed_pids.append(pid)

            logger.warning("VI_ICU_EXAM_ITEM batch query failed for exam chunk %d-%d: %s",
                         i, i + CHUNK_SIZE, e)
            continue

    # 分块查询 reportID
    for i in range(0, len(all_report_ids), CHUNK_SIZE):
        chunk = all_report_ids[i:i + CHUNK_SIZE]

        try:
            items = list(dc.VI_ICU_EXAM_ITEM.find(
                {
                    # #fix: ITEM 表无 reportID 字段 —— 其 examID 实际引用主表 reportID
                    # （原查 reportID 恒 0 条 → reportID 型检验永远丢失）
                    "examID": {"$in": chunk},
                    "itemCode": {"$in": target_item_codes}
                },
                {"examID": 1, "itemCode": 1, "itemValue": 1, "result": 1,
                 "unit": 1, "itemName": 1}
            ).max_time_ms(15000).limit(10000))

            _process_exam_items(items, exam_time_map, exam_pid_map,
                              LAB_MAP, result, window_start, eval_time, seen)

        except Exception as e:
            error_msg = str(e)
            affected_pids = set()
            for rid in chunk:
                pid = exam_pid_map.get(str(rid))
                if pid:
                    affected_pids.add(pid)

            if "MaxTimeMSExpired" in error_msg:
                chunk_status = "timeout"
            else:
                chunk_status = "query_error"

            for pid in affected_pids:
                if status[pid] == "success":
                    status[pid] = chunk_status
                    result[pid] = {"observations": result[pid]["observations"],
                                   "status": chunk_status, "error": error_msg,
                                   "data_complete": False}
                    failed_pids.append(pid)

            logger.warning("VI_ICU_EXAM_ITEM batch query failed for report chunk %d-%d: %s",
                         i, i + CHUNK_SIZE, e)
            continue

    # 统计
    total_obs = sum(len(r["observations"]) for r in result.values())
    patients_with_data = sum(1 for r in result.values() if r["observations"])
    logger.info("batch_fetch_lab_with_status complete: %d/%d patients with data, %d observations",
                patients_with_data, len(his_pids), total_obs)

    return result, status, failed_pids
