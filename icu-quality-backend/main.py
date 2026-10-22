# main.py
# 打包兼容：注解延迟求值，避免 dict|None / list[str] 在 Python<3.10 导入即崩
from __future__ import annotations

from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from datetime import date, datetime, timedelta
from ai_analyzer import analyze, get_all_ai_decisions, override_ai_decision, ensure_ai_cache_collection as ensure_ai_cache
from db import assert_db_ready, get_open_bed_count, get_occupied_bed_days, get_staff_count, get_icu04_apache_data, get_bundle_data, get_bundle_data_v2, judge_bundle_v3_for_patient, get_icu08_data, get_icu06_data, get_icu09_data, get_icu10_data, get_icu11_data, get_icu12_data, get_icu13_data, get_icu14_data, get_icu15_data, get_icu16_data, get_icu17_data, get_icu18_data, get_icu19_data, get_cauti_data, get_tri_tube_suspected_warnings, get_sepsis_alert_warnings, confirm_tri_tube_warning, get_dvt_prevention_patients, get_client, BED_DB_NAMES, PROFESSION_CN, get_patient_census, get_patient_census_detail, iter_bed_dbs, resolve_bundle_dc_pid
import random
import os
import sys
import time as time_module
import threading
import uuid
import summary as summary_module
import calendar
import logging
import traceback
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, JSONResponse
from starlette.requests import Request

logger = logging.getLogger("main")

app = FastAPI(title="ICU质控指标API")

# 全局异常处理：返回 JSON 而不是 HTML
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s: %s\n%s", request.url.path, exc, traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal Server Error: {str(exc)[:200]}"},
    )


def _runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_base_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return _runtime_base_dir()

# ---- 定时调度（后台线程，每天凌晨跑一次） ----
_scheduler_started = False


def _start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _run_daily():
        while True:
            now = datetime.now()
            # 每天凌晨 2:00 执行
            next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            wait_sec = (next_run - now).total_seconds()
            time_module.sleep(wait_sec)
            try:
                logger.info("[scheduler] Starting daily rebuild at %s", datetime.now())
                from scheduler import scheduler as sched_mgr

                # 获取分布式锁
                if not sched_mgr.acquire_lock("daily_rebuild"):
                    logger.warning("[scheduler] Cannot acquire lock, skip daily rebuild")
                    continue

                task_id = None
                try:
                    task_id = sched_mgr.record_start("daily_rebuild", periods=[], extra_info={"trigger": "cron"})
                    stats = summary_module.rebuild_recent(months=13)
                    sched_mgr.record_finish(task_id, status="completed", stats=stats)
                except Exception as e:
                    logger.error("[scheduler] Daily rebuild failed: %s\n%s", e, traceback.format_exc())
                    if task_id:
                        sched_mgr.record_finish(task_id, status="failed", error=str(e))
                finally:
                    sched_mgr.release_lock("daily_rebuild")
            except Exception as e:
                logger.error("[scheduler] Scheduler error: %s", e)

    t = threading.Thread(target=_run_daily, daemon=True)
    t.start()
    logger.info("[scheduler] Daily rebuild scheduler started")


def _startup_completeness_check():
    """启动后异步检查预聚合完整性并补算缺失月份。不阻塞 FastAPI。"""
    try:
        from scheduler import scheduler as sched_mgr, ensure_scheduler_collections
        ensure_scheduler_collections()

        # 检查是否有未完成的任务
        incomplete = sched_mgr.check_incomplete_tasks(max_age_hours=24)
        if incomplete:
            logger.info("[startup] Found %d incomplete tasks, will retry", len(incomplete))

        # 获取全部科室
        dept_codes = summary_module._get_all_dept_codes()
        if not dept_codes:
            logger.warning("[startup] No dept codes found, skipping completeness check")
            return

        # 获取分布式锁
        if not sched_mgr.acquire_lock("startup_rebuild"):
            logger.warning("[startup] Cannot acquire lock, skip rebuild")
            return

        task_id = None
        try:
            # 检查最近13个月完整性
            periods = summary_module._natural_months_back(13)
            historical_periods = periods[:-1]
            current_period = periods[-1]

            # 历史月份只补缺失，当前月每天强制重算
            missing_historical = summary_module.find_missing_periods(dept_codes, historical_periods)

            # 当前月始终加入重算列表
            periods_to_rebuild = list(dict.fromkeys(missing_historical + [current_period]))

            if not periods_to_rebuild:
                logger.info("[startup] All %d periods complete, no rebuild needed", len(periods))
                task_id = sched_mgr.record_start("startup_check", periods, extra_info={"trigger": "startup"})
                sched_mgr.record_finish(task_id, status="completed", stats={"skipped": len(periods), "missing": 0})
                return

            logger.info("[startup] %d periods to rebuild: %s (historical missing: %d, current month: %s)",
                         len(periods_to_rebuild), periods_to_rebuild, len(missing_historical), current_period)
            task_id = sched_mgr.record_start("startup_rebuild", periods_to_rebuild, extra_info={"trigger": "startup"})
            stats = summary_module.rebuild_summary(dept_codes, periods_to_rebuild)
            sched_mgr.record_finish(task_id, status="completed", stats=stats)
            logger.info("[startup] Rebuild completed: %d/%d success", stats["success"], stats["total"])

            # 同时补算明细缓存
            try:
                detail_stats = rebuild_detail_cache(dept_codes, periods_to_rebuild)
                logger.info("[startup] Detail cache rebuild: %d/%d success",
                             detail_stats["success"], detail_stats["total"])
            except Exception as e:
                logger.warning("[startup] Detail cache rebuild failed: %s", e)

            # 清理进程内缓存
            _cache_clear()

        except Exception as e:
            logger.error("[startup] Rebuild failed: %s\n%s", e, traceback.format_exc())
            if task_id:
                sched_mgr.record_finish(task_id, status="failed", error=str(e))
        finally:
            sched_mgr.release_lock("startup_rebuild")

    except Exception as e:
        logger.error("[startup] Completeness check failed: %s\n%s", e, traceback.format_exc())


@app.on_event("startup")
def on_startup():
    try:
        available_dbs = assert_db_ready()
        logger.info("[startup] Available bed DBs: %s", available_dbs)
    except RuntimeError as e:
        logger.warning("[startup] %s", e)
    summary_module.ensure_summary_collection()
    ensure_detail_cache_collection()
    ensure_exclusion_collection()
    from db import ensure_infection_site_indexes
    ensure_infection_site_indexes()
    ensure_ai_cache()
    ensure_clinical_indexes()
    # 注入升压药标签 (VASO_WIDE)
    _inject_vaso_wide_labels()
    _start_scheduler()
    # 启动后异步检查完整性
    threading.Thread(target=_startup_completeness_check, daemon=True).start()


def _inject_vaso_wide_labels():
    """从医院药物字典注入 classification=='血管活性' 的药物名称到 bundle_engine。"""
    try:
        from scoring.bundle_engine import set_vaso_wide_labels
        sc = get_client("SmartCare")["SmartCare"]
        # 从 configDrug 表读取血管活性药物
        vaso_drugs = list(sc.configDrug.find(
            {"classification": {"$regex": "血管活性", "$options": "i"}},
            {"name": 1, "_id": 0}
        ))
        labels = {d["name"].strip().lower() for d in vaso_drugs if d.get("name")}
        if labels:
            set_vaso_wide_labels(labels)
            print(f"[startup] VASO_WIDE_LABELS injected: {len(labels)} drugs")
        else:
            # 兜底: 使用常见升压药名称
            fallback = {
                "去甲肾上腺素", "多巴胺", "多巴酚丁胺", "肾上腺素",
                "血管加压素", "苯肾上腺素", "米力农", "左西孟旦",
                "norepinephrine", "dopamine", "dobutamine", "epinephrine",
                "vasopressin", "phenylephrine", "milrinone", "levosimendan",
            }
            set_vaso_wide_labels(fallback)
            print(f"[startup] VASO_WIDE_LABELS fallback: {len(fallback)} drugs")
    except Exception as e:
        print(f"[startup] WARNING: VASO_WIDE_LABELS injection failed: {e}")

# 简易缓存（TTL 60秒，支持 nocache 参数强制刷新）
_cache = {}
_CACHE_TTL = 60


def _cache_key(prefix, *args):
    return f"{prefix}:{':'.join(str(a) for a in args)}"


def _cache_get(key, nocache: bool = False):
    if nocache:
        return None
    entry = _cache.get(key)
    if entry and time_module.time() - entry["ts"] < _CACHE_TTL:
        return entry["val"]
    return None


def _cache_set(key, val):
    _cache[key] = {"val": val, "ts": time_module.time()}


def _cache_clear():
    """清理进程内缓存。"""
    _cache.clear()


DETAIL_CACHE_COLLECTION = "icu_indicator_detail_cache"
# 缓存版本号：修改口径时 +1，旧条目自然失效
CACHE_VERSION = 16  # v11: ICU-07 口径修复(分母队列反查pid/剔假匹配/气压治疗)
                    # v12: ICU-07 明细统一住院号=mrn + detail_id=在科记录_id(分子分母可逐行对账)
                    # v13: ICU-04/07/09/10 统一**人数口径**(按 mrn 去重，
                    #      与 INDICATORS_CONFIG「患者数」定义一致；明细一人一行)
                    # v14: 跨月明细按「统计月+患者」成行 —— 与汇总逐月相加口径对齐，
                    #      tab 行数 == 公式条分子/分母（stat_month 字段供统计月列展示）
                    # v15: ICU-06 分子 hisPid→多条在科记录映射修复（重复住院号培养医嘱
                    #      不再随机丢映射），旧明细缓存作废按新口径重算
                    # v16: 检验链路修复(VI_ICU_EXAM 患者键 pid→hisPid、ITEM join
                    #      reportID→examID、窗口比较 naive/aware 时区) —— ICU-06 等
                    #      依赖检验的明细按新数据重算（修复前 list 已重建但明细
                    #      缓存仍是旧值，出现 list 313 vs detail 319 不一致）


def _dept_cache_key(dept_codes: list) -> str:
    return ",".join(dept_codes) if len(dept_codes) > 1 else dept_codes[0]


def _get_detail_cache_collection():
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            return db[DETAIL_CACHE_COLLECTION]
        except Exception:
            continue
    return None


EXCLUSION_COLLECTION = "icu_indicator_exclusion"


def _get_exclusion_collection():
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            return db[EXCLUSION_COLLECTION]
        except Exception:
            continue
    return None


def ensure_exclusion_collection():
    coll = _get_exclusion_collection()
    if coll is None:
        return
    try:
        coll.create_index(
            [("dept_code", 1), ("period", 1), ("code", 1), ("exclusion_key", 1)],
            unique=True, background=True,
        )
        coll.create_index([("code", 1), ("period", 1)], background=True)
    except Exception as e:
        print(f"[exclusion] ensure index failed: {e}")


def _invalidate_detail_cache(dept_codes, period, code):
    # The process-local detail payload cache otherwise keeps a stale response
    # alive after a successful manual exclusion/restoration.
    _cache.clear()
    coll = _get_detail_cache_collection()
    if coll is None:
        return
    try:
        coll.delete_many({"dept_code": _dept_cache_key(dept_codes), "period": period, "code": code})
    except Exception as e:
        logger.warning("Failed to invalidate detail cache for %s/%s/%s: %s", dept_codes, period, code, e)


def _trigger_summary_rebuild(dept_codes, periods, indicators=None):
    try:
        summary_module.rebuild_summary(dept_codes, periods, indicators=indicators)
    except Exception as e:
        logger.error("Summary rebuild failed for %s/%s: %s\n%s", dept_codes, periods, e, traceback.format_exc())


def _get_exclusion_map(dept_codes, period, code):
    dept_key = _dept_cache_key(dept_codes)
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            coll = db[EXCLUSION_COLLECTION]
            docs = list(coll.find(
                {"dept_code": dept_key, "period": period, "code": code, "excluded": True},
                {"exclusion_key": 1, "reason_code": 1, "reason_text": 1, "operator": 1, "_id": 0}
            ))
            return {d["exclusion_key"]: d for d in docs}
        except Exception:
            continue
    return {}


def ensure_detail_cache_collection():
    coll = _get_detail_cache_collection()
    if coll is None:
        return
    try:
        coll.create_index(
            [("dept_code", 1), ("period", 1), ("code", 1), ("part", 1)],
            unique=True,
            background=True,
        )
        coll.create_index([("period", 1), ("code", 1)], background=True)
        coll.create_index([("updated_at", -1)], background=True)
    except Exception as e:
        print(f"[detail-cache] ensure index failed: {e}")


def ensure_clinical_indexes():
    """为临床集合创建索引，加速 tri-tube / sepsis 查询"""
    for db_name, db in iter_bed_dbs():
        try:
            # bedside: 最常用的集合，覆盖 sputum/fever/vent/latest_* 查询
            db.bedside.create_index(
                [("pid", 1), ("valid", 1), ("code", 1), ("time", -1)],
                background=True, name="idx_bedside_pid_valid_code_time",
            )
            db.bedside.create_index(
                [("pid", 1), ("time", -1), ("code", 1)],
                background=True, name="idx_bedside_pid_time_code",
            )
            # patient: 覆盖 _tri_tube_patient_scope
            db.patient.create_index(
                [("deptCode", 1), ("status", 1), ("icuAdmissionTime", 1)],
                background=True, name="idx_patient_dept_status_admit",
            )
            # diseaseDiagnosis: 覆盖 _confirmed_tri_tube_keys
            db.diseaseDiagnosis.create_index(
                [("pid", 1), ("diagnosisTime", 1), ("propertyType", 1)],
                background=True, name="idx_diag_pid_time_type",
            )
            # temperatureData: 覆盖 _fever_evidence
            db.temperatureData.create_index(
                [("pid", 1), ("record_time", -1)],
                background=True, name="idx_temp_pid_time",
            )
            # tubeExe: 覆盖 _long_tube_patients
            db.tubeExe.create_index(
                [("pid", 1), ("type", 1), ("startTime", 1), ("endTime", 1)],
                background=True, name="idx_tube_pid_type_time",
            )
            # criticalValue: 覆盖 _wbc_evidence (pid 分支)
            db.criticalValue.create_index(
                [("pid", 1), ("publishTime", -1)],
                background=True, name="idx_critval_pid_time",
            )
            print(f"[clinical-indexes] ensured on {db_name}")
        except Exception as e:
            print(f"[clinical-indexes] failed on {db_name}: {e}")


def _is_historical_period(period: str) -> bool:
    try:
        y, m = [int(x) for x in period.split("-")]
        now = datetime.now()
        return (y, m) < (now.year, now.month)
    except Exception:
        return False


def _read_detail_cache(dept_codes: list, period: str, code: str, part: str):
    coll = _get_detail_cache_collection()
    if coll is None:
        return None
    doc = coll.find_one({
        "dept_code": _dept_cache_key(dept_codes),
        "period": period,
        "code": code,
        "part": part,
        "cache_version": CACHE_VERSION,
    }, {"_id": 0})
    if not doc:
        return None
    return doc.get("patients", [])


def _write_detail_cache(dept_codes: list, period: str, code: str, part: str, items: list):
    coll = _get_detail_cache_collection()
    if coll is None:
        return
    # 先删除旧版本缓存
    coll.delete_many({
        "dept_code": _dept_cache_key(dept_codes),
        "period": period,
        "code": code,
        "part": part,
        "cache_version": {"$ne": CACHE_VERSION},
    })
    coll.update_one(
        {
            "dept_code": _dept_cache_key(dept_codes),
            "period": period,
            "code": code,
            "part": part,
            "cache_version": CACHE_VERSION,
        },
        {
            "$set": {
                "dept_code": _dept_cache_key(dept_codes),
                "period": period,
                "code": code,
                "part": part,
                "cache_version": CACHE_VERSION,
                "patients": items,
                "count": len(items),
                "updated_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )


def _detail_cache_payload(code: str, period: str, part: str, icu_unit: str, nocache: bool = False):
    dept_codes = _resolve_dept_codes(icu_unit)
    if _is_historical_period(period) and not nocache:
        cached = _read_detail_cache(dept_codes, period, code, part)
        if cached is not None:
            return cached
    items = query_detail(code, period, part, icu_unit)
    if _is_historical_period(period):
        _write_detail_cache(dept_codes, period, code, part, items)
    return items


def rebuild_detail_cache(dept_codes: list, periods: list, indicators: list = None,
                         icu_unit: str = "all", progress_callback=None) -> dict:
    indicators = indicators or list(summary_module.INDICATOR_COMPUTERS.keys())
    stats = {"total": 0, "success": 0, "failed": 0, "errors": []}
    census_parts = ("carry_in", "new_admit", "discharge")
    tasks = [(p, c, part) for p in periods if _is_historical_period(p)
             for c in indicators for part in (("numerator", "denominator") if c != "ICU-00" else census_parts)]
    for period, code, part in tasks:
        try:
            if progress_callback:
                progress_callback(stats["total"], stats["success"], stats["failed"], period, code, part, "started")
            items = query_detail(code, period, part, icu_unit)
            _write_detail_cache(dept_codes, period, code, part, items)
            stats["success"] += 1
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append({"period": period, "indicator": code, "part": part, "error": str(e)[:200]})
        stats["total"] += 1
        if progress_callback:
            progress_callback(stats["total"], stats["success"], stats["failed"], period, code, part, "finished")
    return stats


# 响应头中间件：禁止浏览器缓存 API
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response: Response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

app.add_middleware(NoCacheMiddleware)

# deptCode → 科室名称映射（从 department 表读取，带缓存兜底）
DEPT_MAP = {
    "JJL000282": "ICU病区",
    "JJL000283": "ICU护理单元",
    "0801": "ICU病区老",
}

# 前端科室筛选 → deptCode 列表
def _resolve_dept_codes(icu_unit: str) -> list:
    """
    将前端科室筛选参数解析为 deptCode 列表。
    'all' 时从数据库动态加载全部科室编码。
    """
    if icu_unit and icu_unit != "all":
        # 直接按 code 或 name 匹配
        for db_name in BED_DB_NAMES:
            try:
                db = get_client()[db_name]
                doc = db.department.find_one({
                    "$or": [{"code": icu_unit}, {"name": icu_unit}]
                }, {"code": 1})
                if doc:
                    return [doc["code"]]
            except Exception:
                continue
        # 没匹配到，当作 code 直接使用
        return [icu_unit]

    # 'all'：从数据库取所有科室 code
    codes = []
    for db_name in BED_DB_NAMES:
        try:
            db = get_client()[db_name]
            docs = list(db.department.find({}, {"code": 1, "_id": 0}))
            if docs:
                codes = [d["code"] for d in docs]
                break
        except Exception:
            continue
    return codes if codes else ["JJL000282", "JJL000283", "0801"]  # 最终兜底

# 19个质控指标的元数据与中文映射
INDICATORS_CONFIG = {
    "ICU-01": {"name": "ICU床位使用率", "unit": "%", "good": (75, 90), "warn": (60, 95), "dir": "range"},
    "ICU-02": {"name": "ICU医师床位比", "unit": ":1", "good": (0.8, 99), "warn": (0.5, 99), "dir": "higher"},
    "ICU-03": {"name": "ICU护士床位比", "unit": ":1", "good": (3.0, 99), "warn": (2.5, 99), "dir": "higher"},
    "ICU-04": {"name": "APACHEⅡ≥15分收治率", "unit": "%", "good": (50, 100), "warn": (30, 100), "dir": "higher"},
    "ICU-04F": {"name": "APACHEⅡ＜15分收治率", "unit": "%", "good": (0, 50), "warn": (0, 70), "dir": "lower"},
    "ICU-04A": {"name": "APACHEⅡ评分<10分患者总数", "unit": "人", "good": (0, 999999), "warn": (0, 999999), "dir": "range"},
    "ICU-04B": {"name": "10≤APACHEⅡ评分<15分患者总数", "unit": "人", "good": (0, 999999), "warn": (0, 999999), "dir": "range"},
    "ICU-04C": {"name": "15≤APACHEⅡ评分<20分患者总数", "unit": "人", "good": (0, 999999), "warn": (0, 999999), "dir": "range"},
    "ICU-04D": {"name": "20≤APACHEⅡ评分<25分患者总数", "unit": "人", "good": (0, 999999), "warn": (0, 999999), "dir": "range"},
    "ICU-04E": {"name": "APACHEⅡ评分≥25分患者总数", "unit": "人", "good": (0, 999999), "warn": (0, 999999), "dir": "range"},
    "ICU-05-1h": {"name": "感染性休克1h Bundle完成率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-05-3h": {"name": "感染性休克3h Bundle完成率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-05-6h": {"name": "感染性休克6h Bundle完成率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-06": {"name": "抗菌药物治疗前病原学送检率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-07": {"name": "DVT预防率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-08": {"name": "中重度ARDS俯卧位通气实施率", "unit": "%", "good": (80, 100), "warn": (60, 100), "dir": "higher",
               "numerator_desc": "住院期间有俯卧位记录的患者数",
               "denominator_desc": "中重度ARDS患者数(P/F<150且PEEP≥5且有创氧疗途径)"},
    "ICU-09": {"name": "ICU镇痛评估率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-10": {"name": "ICU镇静评估率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-11": {"name": "ICU患者标化病死指数(SMR)", "unit": "", "good": (0, 1.0), "warn": (0, 1.2), "dir": "lower"},
    "ICU-12": {"name": "非计划气管插管拔管率", "unit": "%", "good": (0, 5), "warn": (0, 10), "dir": "lower"},
    "ICU-13": {"name": "拔管后48h再插管率", "unit": "%", "good": (0, 5), "warn": (0, 12), "dir": "lower"},
    "ICU-14": {"name": "非计划转入ICU率", "unit": "%", "good": (0, 5), "warn": (0, 10), "dir": "lower"},
    "ICU-15": {"name": "转出ICU后48h重返率", "unit": "%", "good": (0, 3), "warn": (0, 6), "dir": "lower"},
    "ICU-16": {"name": "VAP发病率", "unit": "‰", "good": (0, 8), "warn": (0, 14), "dir": "lower"},
    "ICU-17": {"name": "CRBSI发病率", "unit": "‰", "good": (0, 1), "warn": (0, 3.5), "dir": "lower"},
    "ICU-18": {"name": "急性脑损伤意识评估率", "unit": "%", "good": (90, 100), "warn": (70, 100), "dir": "higher"},
    "ICU-19": {"name": "48h内肠内营养启动率", "unit": "%", "good": (80, 100), "warn": (60, 100), "dir": "higher"},
    "CAUTI": {"name": "CAUTI尿管相关感染率", "unit": "‰", "good": (0, 2), "warn": (0, 5), "dir": "lower"},
}

NAME_MAP = {code: cfg["name"] for code, cfg in INDICATORS_CONFIG.items()}
UNIT_MAP = {code: cfg["unit"] for code, cfg in INDICATORS_CONFIG.items()}
NO_DATA_INDICATORS = set()

# 数据源详细说明 (供下钻明细弹窗使用)
SOURCE_DESC = {
    "ICU-01": {"numerator": "实际占用床位数累计之和", "denominator": "实际开放床位数累计之和"},
    "ICU-02": {"numerator": "ICU在岗执业医师总人数", "denominator": "实际开放床位数总和"},
    "ICU-03": {"numerator": "ICU在岗执业护士总人数", "denominator": "实际开放床位数总和"},
    "ICU-04": {"numerator": "首次APACHEⅡ评分≥15分的患者数", "denominator": "同期入住ICU的患者总人数"},
    "ICU-04F": {"numerator": "首次APACHEⅡ评分<15分的患者数", "denominator": "同期入住ICU的患者总人数"},
    "ICU-04A": {"numerator": "首次APACHEⅡ评分<10分的患者数", "denominator": "同期入住ICU的患者总人数"},
    "ICU-04B": {"numerator": "首次APACHEⅡ评分10～14分的患者数", "denominator": "同期入住ICU的患者总人数"},
    "ICU-04C": {"numerator": "首次APACHEⅡ评分15～19分的患者数", "denominator": "同期入住ICU的患者总人数"},
    "ICU-04D": {"numerator": "首次APACHEⅡ评分20～24分的患者数", "denominator": "同期入住ICU的患者总人数"},
    "ICU-04E": {"numerator": "首次APACHEⅡ评分≥25分的患者数", "denominator": "同期入住ICU的患者总人数"},
    "ICU-05-1h": {"numerator": "1h内完成bundle患者数", "denominator": "确诊感染性休克患者数"},
    "ICU-05-3h": {"numerator": "3h内完成bundle患者数", "denominator": "确诊感染性休克患者数"},
    "ICU-05-6h": {"numerator": "6h内完成bundle患者数", "denominator": "确诊感染性休克患者数"},
    "ICU-06": {"numerator": "首剂抗菌药前完成病原学送检(培养/镜检/免疫/分子)的患者数", "denominator": "以治疗为目的使用抗菌药的患者数(已剔除围术期/短疗程预防)"},
    "ICU-07": {"numerator": "实施物理或药物DVT预防措施的患者数", "denominator": "同期入住ICU的所有患者数"},
    "ICU-08": {"numerator": "实施俯卧位通气治疗的患者数", "denominator": "满足PEEP≥5且P/F<150的ARDS患者数"},
    "ICU-09": {"numerator": "进行镇痛评分(NRS/CPOT/BPS)测定的患者数", "denominator": "同期入住ICU的所有患者数"},
    "ICU-10": {"numerator": "进行镇静评分(RASS/SAS)测定的患者数", "denominator": "同期入住ICU的所有患者数"},
    "ICU-11": {"numerator": "实际死亡患者总数", "denominator": "预计死亡概率(SMR公式计算)累计总和"},
    "ICU-12": {"numerator": "非计划自行拔除气管导管的例数", "denominator": "行气管插管机械通气拔管的总例数"},
    "ICU-13": {"numerator": "气管插管拔除后48小时内重新插管的例数", "denominator": "行气管插管机械通气拔管的总例数"},
    "ICU-14": {"numerator": "非计划由手术室/病房转入ICU的患者数", "denominator": "同期手术后入ICU患者的总人数"},
    "ICU-15": {"numerator": "转出ICU后48小时内再次因同一/新发病情收治入ICU的患者数", "denominator": "同期转出ICU的患者总人数"},
    "ICU-16": {"numerator": "新发生呼吸机相关性肺炎(VAP)的例次数", "denominator": "有创呼吸机累计使用总天数"},
    "ICU-17": {"numerator": "新发生中心导管相关血流感染(CRBSI)的例次数", "denominator": "中心血管导管患者-导管日：同一患者同一日存在≥1根中心静脉导管/PICC/中心静脉透析或CRRT通路计1日；动脉导管、PICCO、中长导管等非中心导管不计入；置管日与拔管日均按占用日历日计入"},
    "ICU-18": {"numerator": "进行意识状态评分(GCS/FOUR)测定的急性脑损伤患者数", "denominator": "同期收治的急性脑损伤患者总数"},
    "ICU-19": {"numerator": "入科48小时内启动肠内营养(EN)支持的患者数", "denominator": "同期入住ICU时间超过48小时的患者总数"},
    "CAUTI": {"numerator": "新发生尿管相关感染(CAUTI)的例次数", "denominator": "导尿管留置累计使用总天数"},
}

# 基础Mock数据产生器
def get_mock_base_values():
    return {
        "ICU-01": 82.3, "ICU-02": 0.9, "ICU-03": 2.8, "ICU-04": 58.2,
        "ICU-05-1h": 88.0, "ICU-05-3h": 76.0, "ICU-05-6h": 65.0, "ICU-06": 93.5,
        "ICU-07": 87.1, "ICU-08": 72.4, "ICU-09": 95.2, "ICU-10": 91.8,
        "ICU-11": 0.92, "ICU-12": 3.2, "ICU-13": 4.1, "ICU-14": 6.8,
        "ICU-15": 2.5, "ICU-16": 9.1, "ICU-17": 1.8, "ICU-18": 89.3, "ICU-19": 78.6,
    }

def eval_status(code, val) -> str:
    cfg = INDICATORS_CONFIG.get(code)
    if not cfg:
        return "unknown"
    if isinstance(val, dict):
        val = val.get("3h", 75.0)
    
    g_lo, g_hi = cfg["good"]
    w_lo, w_hi = cfg["warn"]
    if g_lo <= val <= g_hi:
        return "good"
    if w_lo <= val <= w_hi:
        return "warn"
    return "danger"


def _summary_row_to_api(r: dict) -> dict:
    code = r["indicator"]
    value = r.get("value", 0)
    numerator = r.get("numerator")
    denominator = r.get("denominator")
    if code in NO_DATA_INDICATORS and (numerator in (0, None)) and denominator:
        numerator = None
        denominator = None
        value = None
    result_row = {
        "code": code,
        "name": NAME_MAP.get(code, code),
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "unit": UNIT_MAP.get(code, ""),
        "status": "unknown" if value is None else eval_status(code, value),
    }
    census = r.get("census")
    if census:
        result_row["census"] = census
    # ICU-05 诊断字段透传
    for diag_field in ("candidate_mode", "raw_candidate_count", "high_probability_count",
                       "probable_count", "pending_review_count", "not_candidate_count",
                       "diagnosis_based_count", "candidate_den",
                       "raw_num", "raw_den", "excluded_num", "excluded_den",
                       "new_shock_count", "old_shock_count", "shock_diff"):
        if r.get(diag_field) is not None:
            result_row[diag_field] = r[diag_field]
    return result_row


def _calc_value(code: str, numerator: float, denominator: float) -> float:
    if code in ("ICU-04A", "ICU-04B", "ICU-04C", "ICU-04D", "ICU-04E"):
        # APACHE 分段：值 = 该段患者人数
        return float(numerator or 0)
    if denominator <= 0:
        return 0.0
    if code == "ICU-11":
        return round(numerator / denominator, 2)
    if code == "ICU-18":
        return round(numerator / denominator * 100, 2)
    unit = UNIT_MAP.get(code, "")
    if unit == "‰":
        return round(numerator / denominator * 1000, 1)
    if unit == ":1":
        return round(numerator / denominator, 1)
    return round(numerator / denominator * 100, 1)


def _live_summary_row(code: str, period: str, icu_unit: str) -> dict | None:
    year, month = period.split("-")
    start_date = f"{year}-{month}-01"
    end_day = calendar.monthrange(int(year), int(month))[1]
    end_date = f"{year}-{month}-{end_day:02d}"
    dept_codes = _resolve_dept_codes(icu_unit)

    data = None
    if code == "ICU-09":
        data = get_icu09_data(dept_codes, start_date, end_date)
    elif code == "ICU-10":
        data = get_icu10_data(dept_codes, start_date, end_date)
    elif code == "ICU-11":
        data = get_icu11_data(dept_codes, start_date, end_date)
    elif code == "ICU-12":
        data = get_icu12_data(dept_codes, start_date, end_date)
    elif code == "ICU-13":
        data = get_icu13_data(dept_codes, start_date, end_date)
    elif code == "ICU-14":
        data = get_icu14_data(dept_codes, start_date, end_date)
    elif code == "ICU-15":
        data = get_icu15_data(dept_codes, start_date, end_date)
    elif code == "ICU-16":
        data = get_icu16_data(dept_codes, start_date, end_date)
    elif code == "ICU-17":
        data = get_icu17_data(dept_codes, start_date, end_date)
    elif code == "ICU-18":
        data = get_icu18_data(dept_codes, start_date, end_date)
    elif code == "ICU-19":
        data = get_icu19_data(dept_codes, start_date, end_date)
    elif code == "CAUTI":
        data = get_cauti_data(dept_codes, start_date, end_date)
    if not data:
        return None

    numerator = data.get("num_count", 0)
    denominator = data.get("den_count", 0)
    value = _calc_value(code, numerator, denominator)
    return {
        "code": code,
        "name": NAME_MAP.get(code, code),
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "unit": UNIT_MAP.get(code, ""),
        "status": eval_status(code, value),
    }


def _repair_stale_summary_rows(rows: list, period: str, icu_unit: str) -> list:
    fixed = []
    for row in rows:
        code = row.get("code")
        if code in NO_DATA_INDICATORS:
            fixed.append(_empty_indicator_row(code))
            continue
        needs_live = (
            code in ("ICU-09", "ICU-10") and row.get("numerator", 0) == 0
        ) or (
            code == "ICU-11" and (row.get("numerator", 0) == 0 or row.get("value", 0) == 0)
        ) or (
            code in ("ICU-12", "ICU-13", "ICU-14", "ICU-15", "ICU-16", "ICU-17", "ICU-18", "ICU-19", "CAUTI") and row.get("value", 0) == 0
        )
        if needs_live:
            live = _live_summary_row(row["code"], period, icu_unit)
            if live and (live.get("numerator", 0) > 0 or live.get("denominator", 0) > 0):
                fixed.append(live)
                continue
        fixed.append(row)
    return fixed


def _empty_indicator_row(code: str) -> dict:
    return {
        "code": code,
        "name": NAME_MAP.get(code, code),
        "numerator": None,
        "denominator": None,
        "value": None,
        "unit": UNIT_MAP.get(code, ""),
        "status": "unknown",
    }


def _ensure_no_data_indicator_rows(rows: list) -> list:
    by_code = {r.get("code"): r for r in rows}
    for code in NO_DATA_INDICATORS:
        by_code[code] = _empty_indicator_row(code)
    return sorted(by_code.values(), key=lambda x: x.get("code", ""))

# ---- ICU-01 分母：从 MongoDB 读取实际开放床位日数 ----
def get_icu01_bed_days(dept: str, start_date: str, end_date: str) -> int:
    """
    ICU床位使用率 分母 = 实际开放总床日数（开放床位数 × 统计天数）。

    优先 bedRecord（deptCode 支持逗号分隔多科室 + time + bedNum），
    兜底 configBed（统计 hisName 条数）。

    dept=all 时汇总全部 ICU 科室的床位数，再乘以天数。
    """
    dept_codes = _resolve_dept_codes(dept)

    # 计算统计天数
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
        ed = datetime.strptime(end_date, "%Y-%m-%d")
        days = (ed - sd).days + 1
        if days <= 0:
            days = 30
    except Exception:
        days = 30

    # 汇总各科室床位数
    total_beds = 0
    for dc in dept_codes:
        total_beds += get_open_bed_count(dc, start_date, end_date)

    # 无数据兜底：跨库 configBed 总计
    if total_beds == 0:
        for db_name in BED_DB_NAMES:
            try:
                db = get_client()[db_name]
                total_beds = sum(
                    db.configBed.count_documents({"deptCode": dc})
                    for dc in dept_codes
                )
                if total_beds > 0:
                    break
            except Exception:
                continue

    return total_beds * days


# ---- 辅助数据查询实现 ----
def query_summary(period: str, icu_unit: str = "all"):
    """
    第一级：某月所有指标的汇总列表，包括分子分母
    """
    # 计算当月的起止日期
    year, month = period.split("-")
    start_date = f"{year}-{month}-01"
    end_day = calendar.monthrange(int(year), int(month))[1]
    end_date = f"{year}-{month}-{end_day:02d}"

    # ----- ICU-01 分母：从 MongoDB 取真实床位日数 -----
    icu01_den = get_icu01_bed_days(icu_unit, start_date, end_date)

    # ----- ICU-01 分子：从 patient 表取实际占用总床日数 -----
    icu01_num = 0
    dept_codes = _resolve_dept_codes(icu_unit)
    for dc in dept_codes:
        icu01_num += get_occupied_bed_days(dc, start_date, end_date)

    # 计算 ICU-01 比值
    if icu01_num > 0 and icu01_den > 0:
        icu01_value = round(icu01_num / icu01_den * 100, 1)
    elif icu01_den > 0:
        icu01_value = 0.0
    else:
        icu01_value = None

    # ----- ICU-02/03 分子：从 account 表取真实医师/护士人数 -----
    icu02_num = 0
    icu03_num = 0
    for dc in dept_codes:
        icu02_num += get_staff_count(dc, "doctor")
        icu03_num += get_staff_count(dc, "nurse")

    # ICU-02/03 分母 = 开放床位数
    icu02_den = 0
    for dc in dept_codes:
        icu02_den += get_open_bed_count(dc, start_date, end_date)

    # ----- ICU-04 分子分母：从 score 表取真实 APACHEⅡ 数据 -----
    icu04_data = get_icu04_apache_data(dept_codes, start_date, end_date)
    icu04_num = icu04_data["num_count"]
    icu04_den = icu04_data["den_count"]

    # ----- ICU-06：抗菌药物前病原学送检率（DataCenter.VI_ICU_ZYYZ）-----
    icu06_data = get_icu06_data(dept_codes, start_date, end_date)
    icu06_num = icu06_data["num_count"]
    icu06_den = icu06_data["den_count"]
    if icu06_den == 0:
        icu06_num = 0

    # ----- ICU-09：镇痛评估率（bedside + score 双源）-----
    icu09_data = get_icu09_data(dept_codes, start_date, end_date)
    icu09_num = icu09_data["num_count"]
    icu09_den = icu09_data["den_count"]
    if icu09_den == 0:
        icu09_num = 0

    # ----- ICU-10：镇静评估率（bedside param_score_rass_obs + score rass）-----
    icu10_data = get_icu10_data(dept_codes, start_date, end_date)
    icu10_num = icu10_data["num_count"]
    icu10_den = icu10_data["den_count"]
    if icu10_den == 0:
        icu10_num = 0

    # ----- ICU-11：标化病死指数 SMR（实际死亡数 / 预计死亡数）-----
    icu11_data = get_icu11_data(dept_codes, start_date, end_date)
    icu11_num = icu11_data["num_count"]
    icu11_den = icu11_data["den_count"]

    # ----- ICU-12/13：人工气道非计划拔管与48h再置管 -----
    icu12_data = get_icu12_data(dept_codes, start_date, end_date)
    icu12_num = icu12_data["num_count"]
    icu12_den = icu12_data["den_count"]
    icu13_data = get_icu13_data(dept_codes, start_date, end_date)
    icu13_num = icu13_data["num_count"]
    icu13_den = icu13_data["den_count"]

    # ----- ICU-14：非计划转入ICU率 -----
    icu14_data = get_icu14_data(dept_codes, start_date, end_date)
    icu14_num = icu14_data["num_count"]
    icu14_den = icu14_data["den_count"]
    icu15_data = get_icu15_data(dept_codes, start_date, end_date)
    icu15_num = icu15_data["num_count"]
    icu15_den = icu15_data["den_count"]

    # ----- ICU-16/17/CAUTI：三管院感发病率 -----
    icu16_data = get_icu16_data(dept_codes, start_date, end_date)
    icu16_num = icu16_data["num_count"]
    icu16_den = icu16_data["den_count"]
    icu17_data = get_icu17_data(dept_codes, start_date, end_date)
    icu17_num = icu17_data["num_count"]
    icu17_den = icu17_data["den_count"]
    icu18_data = get_icu18_data(dept_codes, start_date, end_date)
    icu18_num = icu18_data["num_count"]
    icu18_den = icu18_data["den_count"]
    icu19_data = get_icu19_data(dept_codes, start_date, end_date)
    icu19_num = icu19_data["num_count"]
    icu19_den = icu19_data["den_count"]
    cauti_data = get_cauti_data(dept_codes, start_date, end_date)
    cauti_num = cauti_data["num_count"]
    cauti_den = cauti_data["den_count"]

    # ----- ICU-07：DVT预防率（DataCenter.VI_ICU_ZYYZ 医嘱包含匹配）-----
    dvt_data = get_dvt_prevention_patients(dept_codes, start_date, end_date)
    # 人数口径（INDICATORS_CONFIG 定义分子分母均为“患者数”）：
    # 分子=实施预防的患者数 all_count，分母=同期在科患者数 icu04_den（均按 mrn 去重）
    icu07_num = dvt_data.get("all_count", 0)
    icu07_den = icu04_den  # 分母=同期在科患者（同ICU-04，共用 get_icu_denominator_stays）

    # ----- ICU-08：ARDS俯卧位实施率（三闸门分母 + 俯卧位分子）-----
    icu08_data = get_icu08_data(dept_codes, start_date, end_date)
    icu08_num = icu08_data["num_count"]
    icu08_den = icu08_data["den_count"]

    # ----- ICU-05-1h/3h/6h：V3 双集合查询 + 完整分母逻辑 -----
    bundle_data = get_bundle_data_v2(dept_codes, start_date, end_date)
    bun_den = bundle_data["total"]
    bun_1h = bundle_data["h1_num"]
    bun_3h = bundle_data["h3_num"]
    bun_6h = bundle_data["h6_num"]

    summary_data = {
        "ICU-01": {"num": icu01_num, "den": icu01_den},
        "ICU-02": {"num": icu02_num, "den": icu02_den},
        "ICU-03": {"num": icu03_num, "den": icu02_den},
        "ICU-04": {"num": icu04_num, "den": icu04_den},
        "ICU-05-1h": {"num": bun_1h, "den": bun_den},
        "ICU-05-3h": {"num": bun_3h, "den": bun_den},
        "ICU-05-6h": {"num": bun_6h, "den": bun_den},
        "ICU-06": {"num": icu06_num, "den": icu06_den},
        "ICU-07": {"num": icu07_num, "den": icu07_den},
        "ICU-08": {"num": icu08_num, "den": icu08_den},
        "ICU-09": {"num": icu09_num, "den": icu09_den},
        "ICU-10": {"num": icu10_num, "den": icu10_den},
        "ICU-11": {"num": icu11_num, "den": icu11_den},
        "ICU-12": {"num": icu12_num, "den": icu12_den},
        "ICU-13": {"num": icu13_num, "den": icu13_den},
        "ICU-14": {"num": icu14_num, "den": icu14_den},
        "ICU-15": {"num": icu15_num, "den": icu15_den},
        "ICU-16": {"num": icu16_num, "den": icu16_den},
        "ICU-17": {"num": icu17_num, "den": icu17_den},
        "ICU-18": {"num": icu18_num, "den": icu18_den},
        "ICU-19": {"num": icu19_num, "den": icu19_den},
        "CAUTI": {"num": cauti_num, "den": cauti_den},
    }
    
    rows = []
    for code, info in summary_data.items():
        if code == "ICU-01" and icu01_den > 0:
            display_val = icu01_value
            val_for_status = icu01_value
        else:
            # 根据变化后的分子分母重算比值
            if info["den"] in (0, None):
                display_val = None
                val_for_status = None
            else:
                cfg = INDICATORS_CONFIG.get(code, {})
                multiplier = 100
                if cfg.get("unit") == "‰":
                    multiplier = 1000
                elif cfg.get("unit") == ":1":
                    multiplier = 1
                if code == "ICU-11":
                    display_val = round(info["num"] / info["den"], 2)
                elif code == "ICU-18":
                    display_val = round(info["num"] / info["den"] * multiplier, 2)
                else:
                    display_val = round(info["num"] / info["den"] * multiplier, 1)
                val_for_status = display_val
        rows.append({
            "indicator": code,
            "numerator": info["num"],
            "denominator": info["den"],
            "value": display_val,
            "status": "unknown" if val_for_status is None else eval_status(code, val_for_status)
        })
    return rows

_TREND_LIVE_CACHE = {}    # ck → (ts, vals)  ICU-05 实时趋势缓存
_TREND_LIVE_TTL = 1800    # 30 分钟：实时逐月计算单科室约 0.6s/月、全院口径更重，
                          # 月份已并行化，冷开启销可接受后拉长缓存减少重算


def query_trend(code: str, year: int, icu_unit: str = "all"):
    """
    第二级：单指标全年 12 个月趋势数据。只读预聚合表，避免趋势弹窗触发实时重算。
    ICU-05：预聚合与列表实时计算同源同值（同一 _compute_icu05、同一次重建），
    常态直接读预聚合（毫秒级）；仅当该年预聚合完全缺失（新科室未重建）时，
    才 4 路并行实时兜底（缓存 30 分钟）——避免全院口径实时 12 个月曾达 7 分钟。
    """
    periods = [f"{year}-{m:02d}" for m in range(1, 13)]
    dept_codes = _resolve_dept_codes(icu_unit)
    rows = summary_module.read_summary(dept_codes, periods, [code])
    by_period = {r.get("period"): r.get("value") for r in rows}
    vals = {m: by_period.get(f"{year}-{m:02d}") for m in range(1, 13)}

    if code in ("ICU-05-1h", "ICU-05-3h", "ICU-05-6h") and not any(
            v is not None for v in vals.values()):
        # 兜底：该年预聚合完全缺失（从未重建的新科室）时才实时并行计算。
        # 常态（预聚合已重建，与实时同源同值）直接毫秒返回，
        # 避免全院口径 12 个月实时计算曾达 7 分钟的打开延迟。
        hour = code.rsplit("-", 1)[1]
        ck = f"trend_icu05:{code}:{year}:{icu_unit}"
        entry = _TREND_LIVE_CACHE.get(ck)
        if entry and time_module.time() - entry[0] < _TREND_LIVE_TTL:
            vals = entry[1]
        else:
            # 12 个月 4 路并行（各月互不共享可变状态：vaso 标签只读、mongo 线程安全）
            import calendar as _calendar
            from concurrent.futures import ThreadPoolExecutor
            from summary import _compute_icu05

            def _month_val(m: int):
                end_day = _calendar.monthrange(year, m)[1]
                d = _compute_icu05(dept_codes, f"{year}-{m:02d}-01",
                                   f"{year}-{m:02d}-{end_day:02d}", hour)
                return m, (d.get("val") if d else None)

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(_month_val, m) for m in range(1, 13)]
                for fut in futures:
                    try:
                        m, v = fut.result()
                        if v is not None:
                            vals[m] = v
                    except Exception as e:
                        logger.warning("ICU-05 trend live compute failed %s %04d: %s",
                                       code, year, e)
            _TREND_LIVE_CACHE[ck] = (time_module.time(), vals)

    return vals


def _enrich_admission_discharge(items, dept_codes):
    """Batch-add admission_type and discharge_type from patient collection."""
    if not items:
        return items
    mrns = set()
    for it in items:
        mid = it.get("mrn") or it.get("patient_id") or it.get("hisPid") or ""
        if mid and mid not in ("-", "—"):
            mrns.add(str(mid))
    if not mrns:
        return items
    lookup = {}
    for db_name in BED_DB_NAMES:
        try:
            db = get_client()[db_name]
            cursor = db.patient.find(
                {"mrn": {"$in": list(mrns)}},
                {"mrn": 1, "admissionType": 1, "dischargedType": 1, "_id": 0}
            )
            for d in cursor:
                lookup[d.get("mrn", "")] = {
                    "admission_type": d.get("admissionType", ""),
                    "discharge_type": d.get("dischargedType", ""),
                }
            if lookup:
                break
        except Exception:
            continue
    for it in items:
        mid = it.get("mrn") or it.get("patient_id") or it.get("hisPid") or ""
        info = lookup.get(str(mid), {})
        it["admission_type"] = info.get("admission_type", "")
        it["discharge_type"] = info.get("discharge_type", "")
    return items

def query_detail(code: str, period: str, part: str, icu_unit: str = "all"):
    """
    第三级：明细数据。
    ICU-01 分子 → 患者明细（含在床天数）
    ICU-01 分母 → 床位配置明细
    其他指标 → 患者明细
    """
    import calendar
    year, month = period.split("-")
    start_date = f"{year}-{month}-01"
    end_day = calendar.monthrange(int(year), int(month))[1]
    end_date = f"{year}-{month}-{end_day:02d}"

    from datetime import datetime as dt, timedelta
    start_dt = dt.fromisoformat(start_date)
    end_dt = dt.fromisoformat(end_date)
    dept_codes = _resolve_dept_codes(icu_unit)

    # ---- ICU-01 分母：床位数 × 天数（汇总，不列明细床位） ----
    if code == "ICU-01" and part == "denominator":
        total_beds = 0
        for db_name in BED_DB_NAMES:
            try:
                db = get_client()[db_name]
                total_beds = sum(
                    db.configBed.count_documents({"deptCode": dc})
                    for dc in dept_codes
                )
                if total_beds > 0:
                    break
            except Exception:
                continue
        days = (end_dt - start_dt).days + 1
        return [{
            "patient_id": "—",
            "name": f"开放床位数 {total_beds} 张 × 统计 {days} 天 = {total_beds * days} 总床日",
            "gender": "",
            "age": "",
            "bed_no": f"{total_beds} 张",
            "dept": "",
            "admit_time": f"统计 {days} 天",
            "discharge_time": "",
            "admission_source": "",
            "value": total_beds * days,
        }]

    # ---- ICU-00：患者动态明细 ----
    if code == "ICU-00":
        from db import get_patient_census_detail as _census_detail
        items = _census_detail(dept_codes, start_date, end_date, part)
        items = [{
            "patient_id": p.get("patient_id", ""),
            "name": p.get("name", ""),
            "gender": "",
            "age": "",
            "bed_no": p.get("hisBed", ""),
            "dept": "",
            "admit_time": str(p.get("icuAdmissionTime", ""))[:16],
            "discharge_time": str(p.get("icuDischargeTime", ""))[:16],
            "admission_source": "",
            "value": 1,
            "detail_id": p.get("patient_id", ""),
            "mrn": p.get("mrn", ""),
            "patient_type": p.get("patient_type", ""),
            "los_days": p.get("los_days"),
        } for p in items]
        _enrich_admission_discharge(items, dept_codes)
        return items

    # ---- ICU-08：ARDS俯卧位实施率明细 ----
    if code == "ICU-08":
        data = get_icu08_data(dept_codes, start_date, end_date)
        if part == "numerator":
            _ek_map = {p.get("pid"): p.get("exclusion_key", "") for p in data.get("den_patients", [])}
            items = []
            for p in data.get("num_patients", []):
                items.append({
                    "patient_id": p.get("mrn", ""),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": f"俯卧{p.get('prone_count',0)}次",
                    "dept": "",
                    "admit_time": str(p.get("prone_times", [""])[0])[:16] if p.get("prone_times") else "",
                    "discharge_time": "",
                    "admission_source": "",
                    "value": p.get("prone_count", 0),
                    "exclusion_key": p.get("exclusion_key", "") or _ek_map.get(p.get("pid", ""), ""),
                    "pid_ref": p.get("pid", ""),
                })
            excl_map = _get_exclusion_map(dept_codes, period, code)
            for item in items:
                ek = item.get("exclusion_key", "")
                if ek in excl_map:
                    item["excluded"] = True
                    item["reason_code"] = excl_map[ek].get("reason_code", "")
                    item["reason_text"] = excl_map[ek].get("reason_text", "")
                    item["operator"] = excl_map[ek].get("operator", "")
                else:
                    item["excluded"] = False
            _enrich_admission_discharge(items, dept_codes)
            return items
        else:
            # Build pid->exclusion_key map from den_patients
            _ek_map = {p.get("pid"): p.get("exclusion_key", "") for p in data.get("den_patients", [])}
            items = []
            for p in data.get("den_patients", []):
                flow_v = p.get('flow_val')
                flow_s = f"  流速: {flow_v}L/min" if flow_v is not None else ""
                items.append({
                    "patient_id": p.get("mrn", ""),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": f"P/F={p.get('pf_ratio','?')}  PEEP={p.get('peep','?')}",
                    "dept": "",
                    "admit_time": f"纳入: {p.get('arm','')}  |  氧疗途径: {p.get('o2_route','?')}{flow_s}",
                    "discharge_time": str(p.get("pf_time", ""))[:16] if p.get("pf_time") else "",
                    "admission_source": "",
                    "value": round(p.get('pf_ratio', 0), 1) if isinstance(p.get('pf_ratio'), (int, float)) else p.get('pf_ratio', 0),
                    "exclusion_key": p.get("exclusion_key", ""),
                    "pid_ref": p.get("pid", ""),
                })
            excl_map = _get_exclusion_map(dept_codes, period, code)
            for item in items:
                ek = item.get("exclusion_key", "")
                if ek in excl_map:
                    item["excluded"] = True
                    item["reason_code"] = excl_map[ek].get("reason_code", "")
                    item["reason_text"] = excl_map[ek].get("reason_text", "")
                    item["operator"] = excl_map[ek].get("operator", "")
                else:
                    item["excluded"] = False
            _enrich_admission_discharge(items, dept_codes)
            return items

    # ---- ICU-04：从 score 表取 APACHEⅡ 明细 ----
    if code == "ICU-04":
        data = get_icu04_apache_data(dept_codes, start_date, end_date)
        if part == "numerator":
            # 返回首次 APACHEⅡ ≥ 15 的患者
            items = []
            for p in data.get("num_patients", []):
                items.append({
                    "patient_id": p.get("mrn") or p.get("hisPid") or p.get("patientId") or str(p.get("_id", "-"))[-8:],
                    "name": p.get("name", "-"),
                    "gender": "",
                    "age": "",
                    "bed_no": p.get("hisBed", ""),
                    "dept": "",
                    "admit_time": p.get("score_time").strftime("%Y-%m-%d %H:%M") if p.get("score_time") else "-",
                    "discharge_time": "",
                    "admission_source": "",
                    "value": p.get("score", 0),
                })
            _enrich_admission_discharge(items, dept_codes)
            return items
        else:
            # 分母：所有在科患者
            items = []
            for p in data.get("den_patients", []):
                items.append({
                    "patient_id": p.get("mrn") or p.get("hisPid") or p.get("patientId") or str(p.get("_id", "-"))[-8:],
                    "name": p.get("name", "-"),
                    "gender": "",
                    "age": "",
                    "bed_no": p.get("hisBed", ""),
                    "dept": "",
                    "admit_time": p.get("icuAdmissionTime").strftime("%Y-%m-%d %H:%M") if p.get("icuAdmissionTime") else "-",
                    "discharge_time": "",
                    "admission_source": "",
                    "patient_type": "原有" if p.get("icuAdmissionTime", end_dt) < start_dt else "新入",
                    "value": 1,
                })
            _enrich_admission_discharge(items, dept_codes)
            return items

    # ---- ICU-04A～E / ICU-04F：APACHEⅡ 分段明细 ----
    if code in ("ICU-04A", "ICU-04B", "ICU-04C", "ICU-04D", "ICU-04E", "ICU-04F"):
        from db import get_apache_score_bands
        data = get_apache_score_bands(dept_codes, start_date, end_date)
        if part == "numerator":
            if code == "ICU-04F":
                band_rows = (data.get("bands") or {}).get("lt10", []) + \
                            (data.get("bands") or {}).get("10_15", [])
            else:
                band_key = {"ICU-04A": "lt10", "ICU-04B": "10_15", "ICU-04C": "15_20",
                            "ICU-04D": "20_25", "ICU-04E": "ge25"}[code]
                band_rows = (data.get("bands") or {}).get(band_key, [])
            items = []
            for p in band_rows:
                items.append({
                    "patient_id": p.get("mrn") or p.get("hisPid") or p.get("patientId") or str(p.get("_id", "-"))[-8:],
                    "name": p.get("name", "-"),
                    "gender": "",
                    "age": "",
                    "bed_no": p.get("hisBed", ""),
                    "dept": "",
                    "admit_time": p.get("score_time").strftime("%Y-%m-%d %H:%M") if p.get("score_time") else "-",
                    "discharge_time": "",
                    "admission_source": "",
                    "value": p.get("score", 0),
                })
            _enrich_admission_discharge(items, dept_codes)
            return items
        items = []
        for p in data.get("den_patients", []):
            items.append({
                "patient_id": p.get("mrn") or p.get("hisPid") or p.get("patientId") or str(p.get("_id", "-"))[-8:],
                "name": p.get("name", "-"),
                "gender": "",
                "age": "",
                "bed_no": p.get("hisBed", ""),
                "dept": "",
                "admit_time": p.get("icuAdmissionTime").strftime("%Y-%m-%d %H:%M") if p.get("icuAdmissionTime") else "-",
                "discharge_time": "",
                "admission_source": "",
                "patient_type": "原有" if p.get("icuAdmissionTime", end_dt) < start_dt else "新入",
                "value": 1,
            })
        _enrich_admission_discharge(items, dept_codes)
        return items

    # ---- ICU-05-1h/3h/6h：Bundle 明细 (V3) ----
    if code in ("ICU-05-1h", "ICU-05-3h", "ICU-05-6h"):
        try:
            data = get_bundle_data_v2(dept_codes, start_date, end_date)
        except Exception as e:
            logger.error("ICU-05 get_bundle_data_v2 failed: %s", e)
            data = {"den_patients": [], "h1_patients": [], "h3_patients": [], "h6_patients": []}
        # 使用 get_bundle_data_v2 已经计算好的 v3 结果
        den_patients = data.get("den_patients", [])
        v3_results = {}
        for p in den_patients:
            mrn = p.get("mrn", "")
            v3 = p.get("v3", {})
            if v3:
                v3_results[mrn] = v3

        # SOFA 评分摘要构建
        def _build_sofa_summary(sofa_data):
            if not sofa_data:
                return None
            classic = sofa_data.get("classic") or {}
            sofa2 = sofa_data.get("sofa2") or {}
            return {
                "classic_score": classic.get("sofa_score"),
                "classic_components": classic.get("components"),
                "classic_result_status": classic.get("result_status"),
                "classic_completeness": classic.get("completeness"),
                "classic_meta": classic.get("meta", {}),
                "sofa2_score": sofa2.get("sofa2_score"),
                "sofa2_components": sofa2.get("components"),
                "sofa2_component_details": sofa2.get("component_details", {}),
                "sofa2_result_status": sofa2.get("result_status"),
                "sofa2_completeness": sofa2.get("completeness"),
                "sofa2_meta": sofa2.get("meta", {}),
                "sofa2_data_quality_flags": sofa2.get("data_quality_flags", []),
                "eval_time": sofa_data.get("eval_time"),
                "t0": sofa_data.get("t0"),
                "version_meta": sofa_data.get("version_meta"),
                "data_quality_flags": sofa_data.get("data_quality_flags", []),
                "fetch_meta": sofa_data.get("fetch_meta", {}),
            }

        # 临床识别层摘要构建
        def _build_clinical_layer_summary(cl):
            if not cl:
                return None
            layer2 = cl.get("layer2_organ_dysfunction") or {}
            return {
                "infection": cl.get("layer1_infection"),
                "organ_dysfunction": {
                    "sofa2_total": layer2.get("sofa2_total"),
                    "sofa2_baseline": layer2.get("sofa2_baseline"),
                    "sofa2_delta": layer2.get("sofa2_delta"),
                    "baseline_known": layer2.get("baseline_known"),
                    "acute_basis": layer2.get("acute_basis"),
                    "acute_assumption_applied": layer2.get("acute_assumption_applied"),
                    "current_score_elevated": layer2.get("current_score_elevated"),
                    "has_acute_organ_dysfunction": layer2.get("has_acute_organ_dysfunction"),
                    "sofa2_completeness": layer2.get("sofa2_completeness"),
                    "sofa2_result_status": layer2.get("sofa2_result_status"),
                },
                "sepsis": cl.get("layer3_sepsis"),
                "shock": cl.get("layer4_shock"),
            }

        # 构建 v3 字段的完整映射
        def _build_v3_dict(v3, window):
            if not v3 or not isinstance(v3, dict):
                return {}
            # Window decisions are nested by contract.  Merge only the
            # explicitly requested window with encounter-level evidence.
            v3 = {**v3, **(v3.get(f"bundle_{window}") or {})}
            # 构建乳酸完整记录（序列化时间字段）
            lac_all = v3.get("lactate_all") or []
            lactate_all_serialized = []
            for lac in lac_all:
                item = dict(lac)
                st = item.get("sample_time", "")
                if hasattr(st, 'isoformat'):
                    item["sample_time"] = st.isoformat()[:19]
                lactate_all_serialized.append(item)

            # 原因（#修复: 引擎产出 bundle_xh.reasons 数组，此前读 reason/reason_codes 恒为空）
            # gate 级原因（NOT_SEPTIC_SHOCK 等）在顶层 reason；bundle 级原因在合并后的 reasons[]
            bundle_reasons = v3.get("reasons") or []
            gate_reason = v3.get("reason") or ""
            primary_reason = gate_reason or (bundle_reasons[0] if bundle_reasons else "")

            return {
                "t0": str(v3.get("t0", ""))[:16] if v3.get("t0") else "",
                "finish": v3.get("finish"),
                # 三步判定（P3 时间线用）: None=数据缺失 / False=未达标 / True=达标
                "step1": v3.get("step1"),
                "step2": v3.get("step2"),
                "step3": v3.get("step3"),
                "finish_path": v3.get("finish_path", ""),
                "has_lactate_recheck": v3.get("has_lactate_recheck"),
                "is_septic_shock": v3.get("is_septic_shock"),
                "a1": v3.get("a1"), "b3": v3.get("b3"),
                "c1": v3.get("c1"), "c2": v3.get("c2"), "c3": v3.get("c3"),
                "k1": v3.get("k1"), "k2": v3.get("k2"),
                "i1": v3.get("i1"), "i2": v3.get("i2"), "i3": v3.get("i3"),
                "s1": v3.get("s1"), "s2": v3.get("s2"), "s3": v3.get("s3"), "s4": v3.get("s4"),
                # 乳酸
                "lactate": v3.get("lactate_initial"),
                "lactate_time": str(v3.get("lactate_initial_time", ""))[:16] if v3.get("lactate_initial_time") else "",
                "lactate_max": v3.get("lactate_max"),
                "lactate_recheck": v3.get("lactate_recheck_value"),
                "lactate_recheck_time": str(v3.get("lactate_recheck_time", ""))[:16] if v3.get("lactate_recheck_time") else "",
                "lactate_all": lactate_all_serialized,
                # MAP
                "map": v3.get("map_min"),
                "map_time": str(v3.get("map_time", ""))[:16] if v3.get("map_time") else "",
                # GCS
                "gcs": v3.get("gcs_min"),
                "gcs_time": str(v3.get("gcs_time", ""))[:16] if v3.get("gcs_time") else "",
                # P/F
                "pf_ratio": v3.get("pf_ratio_min"),
                "pf_ratio_time": str(v3.get("pf_ratio_time", ""))[:16] if v3.get("pf_ratio_time") else "",
                # 抗生素
                "antibiotic_time": str(v3.get("antibiotic_time", ""))[:16] if v3.get("antibiotic_time") else "",
                "antibiotic_name": v3.get("antibiotic_name", ""),
                # 血培养
                "culture_time": str(v3.get("culture_time", ""))[:16] if v3.get("culture_time") else "",
                "culture_name": v3.get("culture_name", ""),
                # 液体
                "fluid_ml": v3.get("fluid_3h_ml", 0),
                "has_fluid_1h": v3.get("has_fluid_1h"),
                # 血管活性药
                "vaso_name": v3.get("vaso_name", ""),
                "vaso_start_time": str(v3.get("vaso_start_time", ""))[:16] if v3.get("vaso_start_time") else "",
                # 原因
                "reason": primary_reason,
                "reason_codes": bundle_reasons or ([gate_reason] if gate_reason else []),
                # 数据质量
                "data_quality_flags": v3.get("data_quality_flags", []),
                # G-1: 感染部位
                "site_confirmed": v3.get("site_confirmed", False),
                "site_primary": v3.get("site_primary", ""),
                "site_secondary": v3.get("site_secondary", []),
                "site_evidence_type": v3.get("site_evidence_type", ""),
                # SOFA 评分
                "sofa": _build_sofa_summary(v3.get("sofa")),
                # 临床识别层
                "clinical_layer": _build_clinical_layer_summary(v3.get("clinical_layer")),
                # 新口径休克状态
                "shock_status_new": v3.get("shock_status_new"),
                "sofa2_total": v3.get("sofa2_total"),
                # 候选引擎结果 (直接从 v3 读取，而非 clinical_layer.candidate)
                "candidate_status": v3.get("candidate_status") or (v3.get("clinical_layer") or {}).get("candidate", {}).get("candidate_status"),
                "candidate_pathways": v3.get("candidate_pathways") or (v3.get("clinical_layer") or {}).get("candidate", {}).get("candidate_pathways", []),
                "candidate_reasons": v3.get("candidate_reasons") or (v3.get("clinical_layer") or {}).get("candidate", {}).get("candidate_reasons", []),
                "clinical_confirmation_status": v3.get("clinical_confirmation_status") or (v3.get("clinical_layer") or {}).get("candidate", {}).get("clinical_confirmation_status"),
                "supporting_evidence": v3.get("supporting_evidence") or (v3.get("clinical_layer") or {}).get("candidate", {}).get("supporting_evidence", []),
                "missing_evidence": v3.get("missing_evidence") or (v3.get("clinical_layer") or {}).get("candidate", {}).get("missing_evidence", []),
                "is_septic_shock_candidate": v3.get("is_septic_shock_candidate", False),
            }

        hour = code.split("-")[2]  # '1h', '3h', '6h'
        key = f"h{hour[0]}_patients"
        excl_map = _get_exclusion_map(dept_codes, period, code)
        if part == "numerator":
            items = []
            for p in data.get(key, []):
                mrn = p.get("mrn", "")
                v3 = p.get("v3") or {}
                # 获取 admission_type
                admission_type = p.get("admission_type", "")
                items.append({
                    "detail_id": p.get("detail_id", ""),
                    "exclusion_key": p.get("exclusion_key", ""),
                    "patient_id": mrn, "name": p.get("name", ""),
                    "gender": "", "age": "", "bed_no": p.get("hisBed", ""), "dept": "",
                    "admit_time": str(v3.get("t0", ""))[:16] if v3.get("t0") else "",
                    "discharge_time": "", "admission_source": "",
                    "admission_type": admission_type,
                    "value": 1,
                    "t0": str(v3.get("t0", ""))[:16] if v3.get("t0") else "",
                    "diagnose": p.get("diagnose", ""),
                    "sc_pid": p.get("sc_pid", ""),
                    "v3": _build_v3_dict(v3, hour),
                    # 候选引擎字段
                    "candidate_status": p.get("candidate_status") or v3.get("candidate_status", "not_candidate"),
                    "candidate_pathways": p.get("candidate_pathways") or v3.get("candidate_pathways", []),
                    "clinical_confirmation_status": p.get("clinical_confirmation_status") or v3.get("clinical_confirmation_status", "insufficient"),
                    "is_septic_shock_candidate": p.get("is_septic_shock_candidate") or v3.get("is_septic_shock_candidate", False),
                })
            _enrich_admission_discharge(items, dept_codes)
            for item in items:
                if item.get("exclusion_key") in excl_map:
                    item.update({"excluded": True, **excl_map[item["exclusion_key"]]})
            return items
        else:
            items = []
            for d in den_patients:
                mrn = d.get("mrn", "")
                v3 = d.get("v3") or {}
                # 候选池: 显示所有候选患者(含pending_review)
                # 新口径: candidate_status != "not_candidate"
                # 兼容旧逻辑: 如果没有candidate引擎结果，用is_septic_shock
                candidate_status = d.get("candidate_status") or v3.get("candidate_status")
                is_candidate = d.get("is_septic_shock_candidate") or v3.get("is_septic_shock_candidate", False)
                if not is_candidate and candidate_status == "not_candidate" and not v3.get("is_septic_shock"):
                    continue
                # 获取 admission_type
                sc_pid = d.get("sc_pid")
                admission_type = ""
                try:
                    if sc_pid:
                        sc = get_client("SmartCare")["SmartCare"]
                        patient_doc = sc.patient.find_one({'_id': sc_pid}, {'admissionType': 1})
                        if patient_doc:
                            admission_type = patient_doc.get('admissionType', '')
                except Exception:
                    pass
                items.append({
                    "detail_id": d.get("detail_id", ""),
                    "exclusion_key": d.get("exclusion_key", ""),
                    "patient_id": mrn, "name": d.get("name", ""),
                    "gender": "", "age": "", "bed_no": "", "dept": "",
                    "admit_time": str(d.get("diagnosisTime", ""))[:16] if d.get("diagnosisTime") else "",
                    "discharge_time": "", "admission_source": "",
                    "admission_type": admission_type,
                    "value": 1,
                    "t0": str(v3.get("t0", ""))[:16] if v3.get("t0") else "",
                    "diagnose": d.get("diagnose", ""),
                    "sc_pid": d.get("sc_pid", ""),
                    "v3": _build_v3_dict(v3, hour),
                    # 候选引擎字段 (顶层，供前端直接读取)
                    "candidate_status": d.get("candidate_status") or v3.get("candidate_status", "not_candidate"),
                    "candidate_pathways": d.get("candidate_pathways") or v3.get("candidate_pathways", []),
                    "clinical_confirmation_status": d.get("clinical_confirmation_status") or v3.get("clinical_confirmation_status", "insufficient"),
                    "is_septic_shock_candidate": d.get("is_septic_shock_candidate") or v3.get("is_septic_shock_candidate", False),
                })
            _enrich_admission_discharge(items, dept_codes)
            for item in items:
                if item.get("exclusion_key") in excl_map:
                    item.update({"excluded": True, **excl_map[item["exclusion_key"]]})
            return items

    # ---- ICU-06：抗菌药物送检率明细（含治疗/预防判定） ----
    if code == "ICU-06":
        data = get_icu06_data(dept_codes, start_date, end_date)
        if part == "numerator":
            items = []
            for p in data.get("num_patients", []):
                tt = p.get("test_time")
                items.append({
                    "patient_id": p.get("patient_id", p.get("mrn", "")),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": p.get("test_name", "")[:60] or p.get("test_source", ""),
                    "dept": "",
                    "admit_time": tt.strftime("%Y-%m-%d %H:%M") if hasattr(tt, 'strftime') else str(tt)[:16] if tt else "",
                    "discharge_time": "",
                    "admission_source": "",
                    "value": 1,
                })
            _enrich_admission_discharge(items, dept_codes)
            return items
        else:
            items = []
            for p in data.get("den_patients", []):
                at = p.get("abx_time")
                purpose = p.get("purpose") or "未判定"
                decided_by = p.get("decided_by", "rule")
                reason = p.get("reason", "")
                confidence = p.get("confidence", 1.0)
                need_review = p.get("need_review", False)
                drug = p.get("abx_drug", "")[:50] or "抗菌药"
                # 抗菌药 + 目的徽章 (仅非 rule 时标注来源)
                if decided_by == "rule":
                    drug_display = f"{drug} [{purpose}]"
                else:
                    drug_display = f"{drug} [{purpose}·{decided_by}]"
                abx_time_str = at.strftime("%m/%d %H:%M") if hasattr(at, 'strftime') else str(at)[:16] if at else ""
                # 低置信度标记: AI 置信度<0.6 或 fallback 或 need_review
                is_low_conf = (
                    need_review
                    or decided_by == "fallback"
                    or (decided_by == "ai" and confidence < 0.6)
                )
                items.append({
                    "patient_id": p.get("patient_id", p.get("mrn", "")),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": drug_display,
                    "dept": f"c={confidence:.2f}" if decided_by in ("ai", "fallback") else "",
                    "admit_time": f"⏱{abx_time_str} | {reason[:60]}" if reason else abx_time_str,
                    "discharge_time": "",
                    "admission_source": "low_confidence" if is_low_conf else "",
                    "value": p.get("total_doses", 1),
                })
            _enrich_admission_discharge(items, dept_codes)
            return items

    # ---- ICU-09：镇痛评估率明细 ----
    if code == "ICU-09":
        data = get_icu09_data(dept_codes, start_date, end_date)
        if part == "numerator":
            items = []
            for p in data.get("num_patients", []):
                at = p.get("assess_time")
                items.append({
                    "patient_id": p.get("patient_id", p.get("mrn", "")),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": p.get("assess_source", ""),
                    "dept": p.get("assess_scale", ""),
                    "admit_time": at.strftime("%Y-%m-%d %H:%M") if hasattr(at, 'strftime') else str(at)[:16] if at else "",
                    "discharge_time": "",
                    "admission_source": "",
                    "value": p.get("assess_value", ""),
                })
            _enrich_admission_discharge(items, dept_codes)
            return items
        else:
            items = []
            for p in data.get("den_patients", []):
                at = p.get("icu_admit")
                items.append({
                    "patient_id": p.get("patient_id", p.get("mrn", "")),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": p.get("hisBed", ""),
                    "dept": "",
                    "admit_time": at.strftime("%Y-%m-%d %H:%M") if hasattr(at, 'strftime') else str(at)[:16] if at else "",
                    "discharge_time": "",
                    "patient_type": "原有" if at and at < start_dt else "新入",
                    "admission_source": "",
                    "value": 1,
                })
            _enrich_admission_discharge(items, dept_codes)
            return items

    # ---- ICU-10：镇静评估率明细 ----
    if code == "ICU-10":
        data = get_icu10_data(dept_codes, start_date, end_date)
        if part == "numerator":
            items = []
            for p in data.get("num_patients", []):
                at = p.get("assess_time")
                items.append({
                    "patient_id": p.get("patient_id", p.get("mrn", "")),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": p.get("assess_source", ""),
                    "dept": p.get("assess_scale", ""),
                    "admit_time": at.strftime("%Y-%m-%d %H:%M") if hasattr(at, 'strftime') else str(at)[:16] if at else "",
                    "discharge_time": "",
                    "admission_source": "",
                    "value": p.get("assess_value", ""),
                })
            _enrich_admission_discharge(items, dept_codes)
            return items
        else:
            items = []
            for p in data.get("den_patients", []):
                at = p.get("icu_admit")
                items.append({
                    "patient_id": p.get("patient_id", p.get("mrn", "")),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": p.get("hisBed", ""),
                    "dept": "",
                    "admit_time": at.strftime("%Y-%m-%d %H:%M") if hasattr(at, 'strftime') else str(at)[:16] if at else "",
                    "discharge_time": "",
                    "admission_source": "",
                    "patient_type": "原有" if at and at < start_dt else "新入",
                    "value": 1,
                })
            _enrich_admission_discharge(items, dept_codes)
            return items

    # ---- ICU-11：标化病死指数(SMR)明细 ----
    if code == "ICU-11":
        data = get_icu11_data(dept_codes, start_date, end_date)
        src = data.get("num_patients" if part == "numerator" else "den_patients", [])
        items = []
        for p in src:
            at = p.get("apache_time")
            dt_out = p.get("icu_discharge")
            cal_dead = p.get("apache_calDead", 0)
            items.append({
                "patient_id": p.get("patient_id", p.get("mrn", "")),
                "name": p.get("name", ""),
                "gender": "", "age": "",
                "bed_no": p.get("dischargedType", ""),
                "dept": p.get("dept_code", ""),
                "admit_time": at.strftime("%Y-%m-%d %H:%M") if hasattr(at, 'strftime') else str(at)[:16] if at else "",
                "discharge_time": dt_out.strftime("%Y-%m-%d %H:%M") if hasattr(dt_out, 'strftime') else str(dt_out)[:16] if dt_out else "",
                "admission_source": "",
                "value": round(cal_dead, 4) if isinstance(cal_dead, (int, float)) else cal_dead,
            })
        _enrich_admission_discharge(items, dept_codes)
        return items

    # ---- ICU-12/13：人工气道明细 ----
    if code in ("ICU-12", "ICU-13"):
        data = get_icu12_data(dept_codes, start_date, end_date) if code == "ICU-12" \
            else get_icu13_data(dept_codes, start_date, end_date)
        src = data.get("num_patients" if part == "numerator" else "den_patients", [])
        items = []
        for p in src:
            tube_start = p.get("tube_start")
            tube_end = p.get("tube_end")
            reinsert_start = p.get("reinsert_start")
            if code == "ICU-13" and part == "numerator":
                event = f"{p.get('tube_type', '')} → {p.get('reinsert_type', '')}"
                time_desc = (
                    f"拔管 {tube_end.strftime('%Y-%m-%d %H:%M') if hasattr(tube_end, 'strftime') else str(tube_end)[:16]}"
                    f" / 再置管 {reinsert_start.strftime('%Y-%m-%d %H:%M') if hasattr(reinsert_start, 'strftime') else str(reinsert_start)[:16]}"
                )
            else:
                event = p.get("tube_type", "")
                time_desc = tube_end.strftime("%Y-%m-%d %H:%M") if hasattr(tube_end, 'strftime') else str(tube_end)[:16] if tube_end else ""
            items.append({
                "detail_id": p.get("tube_id", ""),
                "patient_id": p.get("patient_id", p.get("mrn", "")),
                "name": p.get("name", ""),
                "gender": "", "age": "",
                "bed_no": event,
                "dept": p.get("dept_code", ""),
                "admit_time": time_desc,
                "discharge_time": tube_start.strftime("%Y-%m-%d %H:%M") if hasattr(tube_start, 'strftime') else str(tube_start)[:16] if tube_start else "",
                "admission_source": "",
                "tube_type": p.get("tube_type", ""),
                "tube_start": tube_start.strftime("%Y-%m-%d %H:%M") if hasattr(tube_start, 'strftime') else str(tube_start)[:16] if tube_start else "",
                "tube_end": tube_end.strftime("%Y-%m-%d %H:%M") if hasattr(tube_end, 'strftime') else str(tube_end)[:16] if tube_end else "",
                "unplanned": p.get("unplanned", False),
                "reinsert_type": p.get("reinsert_type", ""),
                "reinsert_start": reinsert_start.strftime("%Y-%m-%d %H:%M") if hasattr(reinsert_start, 'strftime') else str(reinsert_start)[:16] if reinsert_start else "",
                "value": 1,
            })
        _enrich_admission_discharge(items, dept_codes)
        return items

    # ---- ICU-14：非计划转入ICU率明细 ----
    if code == "ICU-14":
        data = get_icu14_data(dept_codes, start_date, end_date)
        src = data.get("num_patients" if part == "numerator" else "den_patients", [])
        items = []
        for p in src:
            icu_admit = p.get("icuAdmissionTime")
            basis = (
                "转入类型含“手术”和“转入”；转入计划=“非计划转入”"
                if part == "numerator"
                else "转入类型含“手术”和“转入”"
            )
            items.append({
                "detail_id": p.get("pid", ""),
                "patient_id": p.get("patient_id", p.get("mrn", "")),
                "name": p.get("name", ""),
                "gender": "", "age": "",
                "bed_no": p.get("admissionType", ""),
                "dept": p.get("operation_name", ""),
                "admit_time": icu_admit.strftime("%Y-%m-%d %H:%M") if hasattr(icu_admit, 'strftime') else str(icu_admit)[:16] if icu_admit else "",
                "discharge_time": "",
                "admission_source": p.get("admissionPlan", ""),
                "admissionType": p.get("admissionType", ""),
                "admissionPlan": p.get("admissionPlan", ""),
                "operation_name": p.get("operation_name", ""),
                "icuAdmissionTime": icu_admit.strftime("%Y-%m-%d %H:%M") if hasattr(icu_admit, 'strftime') else str(icu_admit)[:16] if icu_admit else "",
                "basis": basis,
                "value": 1,
            })
        _enrich_admission_discharge(items, dept_codes)
        return items

    # ---- ICU-16/17/CAUTI：三管院感明细 ----
    if code == "ICU-15":
        data = get_icu15_data(dept_codes, start_date, end_date)
        src = data.get("num_patients" if part == "numerator" else "den_patients", [])
        items = []
        for p in src:
            discharge_time = p.get("icu_discharge")
            re_admit = p.get("re_icu_admit")
            discharge_text = discharge_time.strftime("%Y-%m-%d %H:%M") if hasattr(discharge_time, "strftime") else str(discharge_time)[:16] if discharge_time else ""
            re_admit_text = re_admit.strftime("%Y-%m-%d %H:%M") if hasattr(re_admit, "strftime") else str(re_admit)[:16] if re_admit else "/"
            source_text = "合并历史" if p.get("source") == "patInIcuHistoryList" else "Patient记录"
            readmit_source = "合并历史" if p.get("re_admit_source") == "patInIcuHistoryList" else ("Patient记录" if p.get("re_admit_source") else "")
            basis = (
                f"出科后48小时内重返ICU；依据：{source_text}"
                + (f" + {readmit_source}" if readmit_source else "")
                if re_admit
                else f"同期转出ICU；依据：{source_text}"
            )
            items.append({
                "detail_id": f"{p.get('pid', '')}-{discharge_text}",
                "patient_id": p.get("patient_id", p.get("mrn", "")),
                "name": p.get("name", ""),
                "gender": "", "age": "",
                "bed_no": p.get("dept_code", ""),
                "dept": p.get("re_admit_dept_code", ""),
                "admit_time": discharge_text,
                "discharge_time": re_admit_text,
                "admission_source": source_text,
                "icuDischargeTime": discharge_text,
                "reIcuAdmissionTime": re_admit_text,
                "event_source": source_text,
                "basis": basis,
                "value": 1,
            })
        _enrich_admission_discharge(items, dept_codes)
        return items

    if code == "ICU-18":
        data = get_icu18_data(dept_codes, start_date, end_date)
        src = data.get("num_patients" if part == "numerator" else "den_patients", [])
        items = []
        for p in src:
            admit_time = p.get("icu_admit")
            assess_time = p.get("first_assess_time")
            admit_text = admit_time.strftime("%Y-%m-%d %H:%M") if hasattr(admit_time, "strftime") else str(admit_time)[:16] if admit_time else ""
            assess_text = assess_time.strftime("%Y-%m-%d %H:%M") if hasattr(assess_time, "strftime") else str(assess_time)[:16] if assess_time else "/"
            assessed_text = "是" if p.get("assessed") else "否"
            items.append({
                "detail_id": p.get("pid", ""),
                "patient_id": p.get("patient_id", p.get("mrn", "")),
                "name": p.get("name", ""),
                "gender": "", "age": "",
                "bed_no": p.get("category", ""),
                "dept": p.get("den_source", ""),
                "admit_time": admit_text,
                "discharge_time": assess_text,
                "admission_source": p.get("assess_source", ""),
                "brain_category": p.get("category", ""),
                "den_source": p.get("den_source", ""),
                "evidence": p.get("evidence", ""),
                "ai_confidence": p.get("ai_confidence", ""),
                "assessed": assessed_text,
                "firstAssessTime": assess_text,
                "assessSource": p.get("assess_source", ""),
                "basis": f"{p.get('den_source', '')}: {p.get('evidence', '')}",
                "value": 1,
            })
        _enrich_admission_discharge(items, dept_codes)
        return items

    if code == "ICU-19":
        data = get_icu19_data(dept_codes, start_date, end_date)
        src = data.get("num_patients" if part == "numerator" else "den_patients", [])
        items = []
        source_label = {
            "bedside": "护理评估",
            "classification": "营养医嘱",
            "name": "医嘱名称",
            "none": "未启动",
        }
        for p in src:
            admit_time = p.get("icu_admit")
            en_time = p.get("en_start_time")
            discharge_time = p.get("icu_discharge")
            admit_text = admit_time.strftime("%Y-%m-%d %H:%M") if hasattr(admit_time, "strftime") else str(admit_time)[:16] if admit_time else ""
            en_text = en_time.strftime("%Y-%m-%d %H:%M") if hasattr(en_time, "strftime") else str(en_time)[:16] if en_time else "/"
            discharge_text = discharge_time.strftime("%Y-%m-%d %H:%M") if hasattr(discharge_time, "strftime") else str(discharge_time)[:16] if discharge_time else ""
            contraindication = "、".join(p.get("contraindication_hits", [])) if p.get("contraindication_hits") else ""
            source = p.get("source", "none")
            source_text = source_label.get(source, source)
            hit = p.get("hit", "")
            route = p.get("route", "")
            hit_text = hit or route or p.get("drug_name", "")
            if not hit_text:
                hit_text = "未检出"
            window_text = "48h内启动" if p.get("within_48h") else ("超过48h启动" if en_time else "未启动")
            contraindication_text = contraindication or "无"
            basis = (
                f"{window_text}；来源：{source_text}；命中：{hit_text}；"
                f"禁忌证标注：{contraindication_text}"
            )
            items.append({
                "detail_id": p.get("pid", ""),
                "patient_id": p.get("patient_id", p.get("mrn", "")),
                "name": p.get("name", ""),
                "gender": "", "age": "",
                "bed_no": source_text,
                "dept": hit_text,
                "admit_time": admit_text,
                "discharge_time": en_text,
                "admission_source": window_text,
                "icuAdmissionTime": admit_text,
                "icuDischargeTime": discharge_text,
                "enStartTime": en_text,
                "enSource": source_text,
                "enHit": hit_text,
                "enRoute": route,
                "enDrug": p.get("drug_name", ""),
                "contraindication": contraindication,
                "windowResult": window_text,
                "basis": basis,
                "value": 1 if p.get("within_48h") else 0,
            })
        _enrich_admission_discharge(items, dept_codes)
        return items

    if code in ("ICU-16", "ICU-17", "CAUTI"):
        getter = {
            "ICU-16": get_icu16_data,
            "ICU-17": get_icu17_data,
            "CAUTI": get_cauti_data,
        }[code]
        data = getter(dept_codes, start_date, end_date)
        src = data.get("num_patients" if part == "numerator" else "den_patients", [])
        items = []
        if part == "numerator":
            for p in src:
                dtm = p.get("diagnosisTime")
                items.append({
                    "detail_id": p.get("diagnosis_id", ""),
                    "patient_id": p.get("patient_id", p.get("mrn", "")),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": p.get("diseaseType", ""),
                    "dept": p.get("notes", ""),
                    "admit_time": dtm.strftime("%Y-%m-%d %H:%M") if hasattr(dtm, 'strftime') else str(dtm)[:16] if dtm else "",
                    "discharge_time": "",
                    "admission_source": p.get("lastEditUserId", ""),
                    "diseaseType": p.get("diseaseType", ""),
                    "diagnosisTime": dtm.strftime("%Y-%m-%d %H:%M") if hasattr(dtm, 'strftime') else str(dtm)[:16] if dtm else "",
                    "notes": p.get("notes", ""),
                    "basis": p.get("dedup_basis", ""),
                    "value": 1,
                })
        else:
            grouped = {}
            for p in src:
                group_key = (
                    p.get("pid") or p.get("patient_id") or p.get("mrn", ""),
                    p.get("device_type", ""),
                    "" if code == "ICU-17" else (p.get("tube_type", "") or p.get("device_value", "")),
                )
                g = grouped.setdefault(group_key, {
                    "base": p,
                    "days": set(),
                    "tube_ids": set(),
                    "record_times": [],
                    "starts": [],
                    "ends": [],
                })
                if p.get("device_day"):
                    g["days"].add(p.get("device_day"))
                if p.get("tube_id"):
                    g["tube_ids"].add(p.get("tube_id"))
                for field, target in (("record_time", "record_times"), ("tube_start", "starts"), ("tube_end", "ends")):
                    if p.get(field):
                        g[target].append(p.get(field))

            for _, g in grouped.items():
                p = g["base"]
                day_count = len(g["days"])
                point_count = len(g["tube_ids"]) if g["tube_ids"] else len(g["record_times"])
                point_label = "记录点数" if code == "ICU-16" else "置管点数"
                if code == "ICU-17":
                    point_label = "中心导管数"
                first_day = min(g["days"]) if g["days"] else ""
                last_day = max(g["days"]) if g["days"] else ""
                start_time = min(g["starts"]) if g["starts"] else None
                end_time = max(g["ends"]) if g["ends"] else None
                day_range = f"{first_day} ~ {last_day}" if first_day and last_day and first_day != last_day else first_day
                tube_type = p.get("tube_type", "") or p.get("device_value", "")
                if code == "ICU-17" and p.get("tube_types"):
                    tube_type = "、".join(p.get("tube_types", []))
                excluded = p.get("excluded_evidence", []) or []
                excluded_text = ""
                if excluded:
                    excluded_text = "；剔除：" + "、".join(
                        f"{e.get('tube_type', '')}({e.get('exclude_reason', '')})"
                        for e in excluded[:5]
                    )
                admission_source = f"{day_count}天，{point_count}个{point_label}{excluded_text}"
                basis_text = f"{p.get('dedup_basis') or '按病人+管道/设备合并'}；累计{day_count}天，{point_label}{point_count}{excluded_text}"
                if code == "ICU-17":
                    admission_source = f"{day_count}个中心导管日，涉及{point_count}根中心导管{excluded_text}"
                    basis_text = f"{p.get('dedup_basis') or '患者-中心导管日去重'}；累计{day_count}个中心导管日，涉及{point_count}根中心导管{excluded_text}"
                items.append({
                    "detail_id": f"{p.get('pid', '')}-{p.get('device_type', '')}-{tube_type}",
                    "patient_id": p.get("patient_id", p.get("mrn", "")),
                    "name": p.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": p.get("device_type", ""),
                    "dept": tube_type,
                    "admit_time": str(day_count),
                    "discharge_time": "",
                    "admission_source": admission_source,
                    "device_type": p.get("device_type", ""),
                    "device_value": p.get("device_value", ""),
                    "tube_type": tube_type,
                    "device_day": day_range,
                    "device_days": day_count,
                    "tube_points": point_count,
                    "record_time": "",
                    "tube_start": start_time.strftime("%Y-%m-%d %H:%M") if hasattr(start_time, 'strftime') else str(start_time)[:16] if start_time else "",
                    "tube_end": end_time.strftime("%Y-%m-%d %H:%M") if hasattr(end_time, 'strftime') else str(end_time)[:16] if end_time else "",
                    "basis": basis_text,
                    "value": day_count,
                })
        _enrich_admission_discharge(items, dept_codes)
        return items

    # ---- ICU-07：DVT预防率明细 ----
    if code == "ICU-07":
        data = get_dvt_prevention_patients(dept_codes, start_date, end_date)
        if part == "numerator":
            # 人数口径：num_stays 已按患者去重，行数 == 汇总分子（all_count），
            # 与分母明细同为一人一行，可逐行核对
            items = []
            for s in data.get("num_stays", []):
                orders = s.get("matched_orders", [])
                _dis = s.get("icuDischargeTime")
                items.append({
                    # 「住院号」列与分母明细同为 mrn，分子分母可逐行对上；
                    # detail_id = 该次在科记录 _id（两侧都取该患者入科最早的一次），
                    # 所以分子的 detail_id 一定 ⊆ 分母的 detail_id
                    "patient_id": s.get("mrn", "") or s.get("patient_id", ""),
                    "detail_id": str(s.get("stay_id", ""))[-8:] or s.get("patient_id", ""),
                    "mrn": s.get("mrn", ""),
                    "name": s.get("name", ""),
                    "gender": "", "age": "",
                    "bed_no": s.get("measure", ""),          # 列头「预防措施」
                    "dept": s.get("deptCode", ""),
                    "admit_time": orders[0][:60] if orders else "",   # 列头「医嘱示例」
                    "discharge_time": _dis.strftime("%Y-%m-%d %H:%M") if _dis else "",
                    "admission_source": "",
                    "patient_type": "原有" if s.get("icuAdmissionTime") and s.get("icuAdmissionTime") < start_dt else "新入",
                    "value": s.get("order_count", 0),        # 列头「医嘱条数」
                })
            _enrich_admission_discharge(items, dept_codes)
            return items
        else:
            data2 = get_icu04_apache_data(dept_codes, start_date, end_date)
            items = [{"patient_id": p.get("mrn", "") or p.get("patientId", ""),
                      "detail_id": str(p.get("_id", ""))[-8:],
                      "mrn": p.get("mrn", ""),
                      "name": p.get("name", ""),
                      "gender": "", "age": "", "bed_no": p.get("hisBed", ""), "dept": p.get("deptCode", ""),
                      "admit_time": p.get("icuAdmissionTime").strftime("%Y-%m-%d %H:%M") if p.get("icuAdmissionTime") else "-",
                      "discharge_time": p.get("icuDischargeTime").strftime("%Y-%m-%d %H:%M") if p.get("icuDischargeTime") else "",
                      "admission_source": "", "patient_type": "原有" if p.get("icuAdmissionTime", end_dt) < start_dt else "新入", "value": 1}
                     for p in data2.get("den_patients", [])]
            _enrich_admission_discharge(items, dept_codes)
            return items

    # ---- ICU-02/03：从 account 表取真实医护人员明细 ----
    if code in ("ICU-02", "ICU-03"):
        from db import DOCTOR_PROFESSIONS, NURSE_PROFESSIONS
        professions = DOCTOR_PROFESSIONS if code == "ICU-02" else NURSE_PROFESSIONS
        staff = []
        if part == "numerator":
            for db_name in BED_DB_NAMES:
                try:
                    db = get_client()[db_name]
                    docs = list(db.account.find(
                        {
                            "valid": "valid",
                            "departmentCode": {"$regex": "|".join(dept_codes)},
                            "profession": {"$in": list(professions)},
                        },
                        {
                            "username": 1, "trueName": 1, "profession": 1,
                            "sex": 1, "educationLevel": 1, "entryTime": 1,
                            "departmentCode": 1, "_id": 0,
                        },
                    ))
                    if docs:
                        for d in docs:
                            staff.append({
                                "patient_id": d.get("username", "-"),
                                "name": d.get("trueName") or d.get("username", "-"),
                                "gender": d.get("sex", ""),
                                "age": "",
                                "bed_no": PROFESSION_CN.get(d.get("profession", ""), d.get("profession", "")),
                                "dept": d.get("departmentCode", ""),
                                "admit_time": d.get("entryTime").strftime("%Y-%m-%d") if d.get("entryTime") else "-",
                                "discharge_time": "",
                                "admission_source": d.get("educationLevel", ""),
                                "value": 1,
                            })
                        break
                except Exception:
                    continue
        else:
            # 分母：床位配置
            total_beds = 0
            for db_name in BED_DB_NAMES:
                try:
                    db = get_client()[db_name]
                    total_beds = sum(db.configBed.count_documents({"deptCode": dc}) for dc in dept_codes)
                    if total_beds > 0: break
                except Exception: continue
            staff = [{
                "patient_id": "—",
                "name": f"开放床位数 {total_beds} 张",
                "gender": "", "age": "",
                "bed_no": f"{total_beds} 张", "dept": "",
                "admit_time": "", "discharge_time": "", "admission_source": "",
                "value": total_beds,
            }]
        _enrich_admission_discharge(staff, dept_codes)
        return staff

    # ---- 分子 / 其他指标：患者明细 ----
    patients = []
    for db_name in BED_DB_NAMES:
        try:
            db = get_client()[db_name]
            query = {
                "deptCode": {"$in": dept_codes},
                "status": {"$ne": "invalid"},
                "icuAdmissionTime": {"$lte": dt(end_dt.year, end_dt.month, end_dt.day, 23, 59, 59)},
                "$or": [
                    {"icuDischargeTime": {"$gte": start_dt}},
                    {"icuDischargeTime": None},
                    {"icuDischargeTime": {"$exists": False}},
                ],
            }
            docs = list(db.patient.find(
                query,
                {
                    "mrn": 1, "hisPid": 1, "name": 1, "hisBed": 1, "gender": 1, "birthday": 1,
                    "icuAdmissionTime": 1, "icuDischargeTime": 1,
                    "admissionSource": 1, "dept": 1, "status": 1, "_id": 0,
                },
            ).limit(200))
            if docs:
                for d in docs:
                    age = ""
                    if d.get("birthday"):
                        try:
                            bd = d["birthday"]
                            if hasattr(bd, 'year'):
                                age = str(end_dt.year - bd.year)
                        except Exception:
                            pass

                    admit_dt = d.get("icuAdmissionTime")
                    discharge_dt = d.get("icuDischargeTime")
                    mrn = d.get("mrn") or d.get("hisPid") or "-"

                    # ICU-01 分子：计算每位患者的在床天数
                    if code == "ICU-01" and part == "numerator" and admit_dt:
                        first_midnight = dt(admit_dt.year, admit_dt.month, admit_dt.day) + timedelta(days=1)
                        if discharge_dt:
                            last_midnight = dt(discharge_dt.year, discharge_dt.month, discharge_dt.day)
                        else:
                            last_midnight = end_dt
                        first = max(first_midnight, start_dt)
                        last = min(last_midnight, end_dt)
                        bed_days = max(0, (last - first).days + 1) if first <= last else 0
                    else:
                        bed_days = d.get("status", "-")

                    patients.append({
                        "patient_id": mrn,
                        "name": d.get("name") or "-",
                        "gender": d.get("gender", ""),
                        "age": age,
                        "bed_no": d.get("hisBed", "-"),
                        "dept": d.get("dept", ""),
                        "admit_time": (admit_dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M") if admit_dt else "-",
                        "discharge_time": (discharge_dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M") if discharge_dt else "在科",
                        "admission_source": d.get("admissionSource", ""),
                        "value": bed_days,
                    })
                break
        except Exception:
            continue

    _enrich_admission_discharge(patients, dept_codes)
    return patients


# ---- REST 接口 ----

@app.get("/api/indicators")
def get_all_indicators(start: date, end: date, dept: str = "all"):
    """
    返回实时大屏数据。
    values 为所选区间聚合值，trend 为逐月真实指标值，不再使用 mock/random。
    """
    if end < start:
        start, end = end, start

    month_labels = []
    y, m = start.year, start.month
    while (y < end.year) or (y == end.year and m <= end.month):
        month_labels.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1

    end_period = month_labels[-1] if len(month_labels) > 1 else ""
    rows = indicator_list(month_labels[0], dept, end_period, nocache=True)
    values = {r["code"]: r.get("value") for r in rows}
    numerators = {r["code"]: r.get("numerator") for r in rows}
    denominators = {r["code"]: r.get("denominator") for r in rows}
    trend = {}
    for r in rows:
        code = r["code"]
        if r.get("months") and r.get("monthly"):
            trend[code] = r["monthly"]
            continue
        trend[code] = []
        for mon in month_labels:
            live = indicator_list(mon, dept, nocache=True)
            item = next((x for x in live if x.get("code") == code), None)
            trend[code].append(item.get("value") if item else None)

    # 缺失月份检测 (从 rows 中提取)
    data_complete = True
    missing_periods = []
    aggregation_periods = month_labels
    for r in rows:
        if r.get("data_complete") is False:
            data_complete = False
        if r.get("missing_periods"):
            missing_periods = r["missing_periods"]
        break  # 所有行共享相同的 missing_periods

    # ICU-05 诊断字段 (从 rows 中提取)
    icu05_diag = {}
    for r in rows:
        if r.get("code", "").startswith("ICU-05"):
            for df in ("candidate_mode", "raw_candidate_count", "high_probability_count",
                       "probable_count", "pending_review_count", "not_candidate_count",
                       "diagnosis_based_count", "candidate_den",
                       "old_shock_count", "new_shock_count"):
                if r.get(df) is not None:
                    icu05_diag[df] = r[df]
            break

    return {
        "values": values,
        "numerators": numerators,
        "denominators": denominators,
        "trend": trend,
        "months": [f"{int(mon.split('-')[1])}月" for mon in month_labels],
        "periods": month_labels,
        "rows": rows,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        # 数据完整性
        "data_complete": data_complete,
        "missing_periods": missing_periods,
        "aggregation_periods": aggregation_periods,
        # ICU-05 诊断字段
        **icu05_diag,
    }

@app.post("/api/ai/analyze")
def ai_analyze(payload: dict):
    period = payload.get("period", "2026-06")
    indicators_list = payload.get("indicators", [])
    
    values_dict = {}
    if indicators_list:
        for item in indicators_list:
            if isinstance(item, dict) and "code" in item and "value" in item:
                values_dict[item["code"]] = item["value"]
    else:
        values_dict = payload.get("values", get_mock_base_values())
        
    return analyze(period, values_dict)


def _periods_between(start_period: str, end_period: str = "") -> list:
    if not end_period:
        end_period = start_period
    sy, sm = start_period.split("-")
    ey, em = end_period.split("-")
    periods = []
    y, m = int(sy), int(sm)
    while (y < int(ey)) or (y == int(ey) and m <= int(em)):
        periods.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return periods


def _month_end_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _aggregate_dashboard_rows(dept_codes: list, periods: list, icu_unit: str) -> tuple[list, dict]:
    summary_rows = summary_module.read_summary(dept_codes, periods)
    rows_by_period = {}
    for row in summary_rows:
        rows_by_period.setdefault(row.get("period"), []).append(row)

    # 本月若尚未预聚合，允许只对本月兜底实时计算；历史月份不实时重算。
    now = datetime.now()
    current_period = f"{now.year}-{now.month:02d}"
    if current_period in periods and not rows_by_period.get(current_period):
        try:
            live_rows = query_summary(current_period, icu_unit)
            rows_by_period[current_period] = [
                {
                    "period": current_period,
                    "indicator": r.get("code"),
                    "numerator": r.get("numerator"),
                    "denominator": r.get("denominator"),
                    "value": r.get("value"),
                }
                for r in live_rows
            ]
        except Exception:
            rows_by_period[current_period] = []

    agg = {}
    for period in periods:
        for row in rows_by_period.get(period, []):
            code = row.get("indicator") or row.get("code")
            if not code:
                continue
            if code not in agg:
                agg[code] = {
                    "code": code,
                    "name": NAME_MAP.get(code, code),
                    "unit": UNIT_MAP.get(code, ""),
                    "numerator": 0,
                    "denominator": 0,
                    "monthly": {},
                }
            num = row.get("numerator")
            den = row.get("denominator")
            agg[code]["numerator"] += num or 0
            agg[code]["denominator"] += den or 0
            agg[code]["monthly"][period] = row.get("value")

    for code in ("ICU-02", "ICU-03"):
        last_rows = rows_by_period.get(periods[-1], []) if periods else []
        for row in last_rows:
            if (row.get("indicator") or row.get("code")) == code and code in agg:
                agg[code]["numerator"] = row.get("numerator")
                agg[code]["denominator"] = row.get("denominator")

    result = []
    for code in UNIT_MAP:
        item = agg.get(code)
        if not item:
            result.append({
                "code": code,
                "name": NAME_MAP.get(code, code),
                "unit": UNIT_MAP.get(code, ""),
                "numerator": None,
                "denominator": None,
                "value": None,
                "status": "unknown",
                "monthly": [None for _ in periods],
            })
            continue
        num, den = item.get("numerator"), item.get("denominator")
        value = None if num is None or den in (None, 0) else _calc_value(code, num, den)
        item["value"] = value
        item["status"] = "unknown" if value is None else eval_status(code, value)
        item["monthly"] = [item["monthly"].get(p) for p in periods]
        result.append(item)
    return result, rows_by_period


def _dashboard_risk_hint(code: str, status: str) -> str:
    if status == "unknown":
        return "当前范围暂无预聚合结果，建议先刷新该时间段。"
    hints = {
        "ICU-06": "建议核查治疗性抗菌药判定、用药前送检流程和低置信度 AI 判定。",
        "ICU-16": "建议核查有创通气维护、VAP 诊断确认和呼吸机日分母明细。",
        "ICU-17": "建议核查中心导管置管维护、导管日去重和 CRBSI 诊断确认。",
        "CAUTI": "建议核查导尿管留置必要性、导尿管日去重和 CAUTI 诊断确认。",
        "ICU-11": "建议核查 APACHE 评分完整性、死亡/转归数据和预计死亡率口径。",
        "ICU-19": "建议核查入科 48h 内 EN 启动记录、禁忌证留痕和营养医嘱匹配。",
    }
    return hints.get(code, "建议质控团队核查该指标分子、分母明细及相关流程依从性。")


def _dashboard_trend_delta(values: list) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    if len(nums) < 2:
        return None
    return round(nums[-1] - nums[0], 2)


def _format_tri_warning_basis(warn_type: str, evidence: list, confidence=None) -> str:
    type_text = warn_type or "三管感染"
    evidence_names = []
    for ev in evidence or []:
        name = ev.get("type") if isinstance(ev, dict) else ""
        if name:
            evidence_names.append(str(name))
    evidence_names = list(dict.fromkeys(evidence_names))
    if evidence_names:
        basis = f"满足疑似{type_text}线索：{'、'.join(evidence_names)}。"
    else:
        basis = f"系统发现疑似{type_text}线索。"
    if confidence is not None:
        try:
            basis += f" 置信度 {float(confidence):.2f}。"
        except Exception:
            pass
    return basis + "需医生确认后才可能进入正式指标。"


@app.get("/api/dashboard/command-center")
def dashboard_command_center(period: str, end_period: str = "", icu_unit: str = "all"):
    """
    实时大屏指挥舱聚合接口：只做轻量编排，不改各指标业务口径。
    历史数据优先读月度预聚合；本月缺失时允许实时兜底。
    """
    periods = _periods_between(period, end_period)
    dept_codes = _resolve_dept_codes(icu_unit)
    rows, _ = _aggregate_dashboard_rows(dept_codes, periods, icu_unit)
    values = {r["code"]: r.get("value") for r in rows}
    trend = {r["code"]: r.get("monthly", []) for r in rows}
    months = [f"{int(p.split('-')[1])}月" for p in periods]

    risk_rank = {"danger": 0, "warn": 1, "unknown": 2, "good": 3}
    abnormal = []
    for row in rows:
        if row.get("status") in ("danger", "warn", "unknown"):
            item = dict(row)
            item["delta"] = _dashboard_trend_delta(row.get("monthly", []))
            item["hint"] = _dashboard_risk_hint(row["code"], row.get("status"))
            abnormal.append(item)
    abnormal.sort(key=lambda x: (risk_rank.get(x.get("status"), 9), x.get("code", "")))

    risk_counts = {
        "danger": sum(1 for r in rows if r.get("status") == "danger"),
        "warn": sum(1 for r in rows if r.get("status") == "warn"),
        "unknown": sum(1 for r in rows if r.get("status") == "unknown"),
        "good": sum(1 for r in rows if r.get("status") == "good"),
    }
    needs_attention = risk_counts["danger"] + risk_counts["warn"]
    overall_status = "danger" if risk_counts["danger"] else ("warn" if risk_counts["warn"] else "good")

    try:
        ai = analyze(f"{period}~{end_period or period}", values)
    except Exception:
        ai = {"summary": "AI 分析暂不可用，请先查看异常指标与待办线索。", "abnormal": [], "hints": []}

    sy, sm = [int(x) for x in periods[0].split("-")]
    ey, em = [int(x) for x in periods[-1].split("-")]
    start_date = f"{sy}-{sm:02d}-01"
    end_date = f"{ey}-{em:02d}-{_month_end_day(ey, em):02d}"

    tri_summary = {"count": 0, "types": {}, "items": [], "notice": ""}
    try:
        tri_items = get_tri_tube_suspected_warnings(dept_codes, start_date, end_date, min_hours=48)
        type_counts = {}
        for item in tri_items:
            key = item.get("suspect_type") or item.get("type") or item.get("infection_type") or "疑似感染"
            type_counts[key] = type_counts.get(key, 0) + 1
        preview_items = []
        for item in tri_items[:8]:
            warn_type = item.get("suspect_type") or item.get("type") or item.get("infection_type") or "疑似感染"
            evidence = item.get("evidence") or []
            confidence = item.get("confidence")
            preview_items.append({
                "patient_id": item.get("patient_id") or item.get("mrn") or item.get("hisPid") or item.get("pid", ""),
                "name": item.get("name", ""),
                "type": warn_type,
                "time": item.get("diagnosis_time") or item.get("time") or item.get("event_time") or "",
                "basis": item.get("basis") or item.get("reason") or _format_tri_warning_basis(warn_type, evidence, confidence),
                "confidence": confidence,
                "evidence": evidence,
                "rule": "至少同时满足：相关装置留置超过 48 小时，并出现感染相关证据；未人工确认前不计入正式感染分子。",
            })
        tri_summary = {
            "count": len(tri_items),
            "types": type_counts,
            "items": preview_items,
            "notice": "AI 疑似预警仅作医生确认线索，未确认前不计入正式指标分子。",
        }
    except Exception as e:
        tri_summary = {"count": 0, "types": {}, "items": [], "notice": f"三管预警读取失败：{str(e)[:80]}"}

    sepsis_summary = {"count": 0, "types": {}, "items": [], "notice": ""}
    try:
        sepsis_items = get_sepsis_alert_warnings(dept_codes, start_date, end_date, limit=30)
        risk_counts_by_type = {}
        preview_items = []
        for item in sepsis_items:
            risk = item.get("risk") or "unknown"
            risk_counts_by_type[risk] = risk_counts_by_type.get(risk, 0) + 1
        for item in sepsis_items[:8]:
            preview_items.append({
                "patient_id": item.get("patient_id") or item.get("mrn") or item.get("hisPid") or item.get("pid", ""),
                "name": item.get("name", ""),
                "type": item.get("type") or "脓毒症早期预警",
                "risk": item.get("risk", "unknown"),
                "qsofa": item.get("qsofa"),
                "basis": item.get("basis", ""),
                "action": item.get("action", ""),
                "by": item.get("by", "ai"),
                "evaluated": item.get("evaluated", item.get("by") != "fallback"),
                "confidence": item.get("confidence"),
                "evidence": item.get("evidence") or [],
                "rule": "Sepsis-3 / qSOFA 辅助识别，仅提示是否建议临床团队评估，不作为诊断或治疗建议。",
            })
        sepsis_summary = {
            "count": len(sepsis_items),
            "types": risk_counts_by_type,
            "items": preview_items,
            "notice": "脓毒症早期预警仅作质控分诊提示，不替代临床诊断与处置。",
        }
    except Exception as e:
        sepsis_summary = {"count": 0, "types": {}, "items": [], "notice": f"脓毒症预警读取失败：{str(e)[:80]}"}

    low_confidence = {"count": 0, "items": []}
    try:
        decisions = get_all_ai_decisions(
            period_start=period,
            period_end=end_period or period,
            min_confidence=0.6,
            limit=20,
        )
        low_confidence = {"count": len(decisions), "items": decisions[:8]}
    except Exception:
        low_confidence = {"count": 0, "items": []}

    ai_todos = []
    if tri_summary["count"]:
        ai_todos.append({
            "type": "tri_tube_warning",
            "title": "三管疑似感染待确认",
            "count": tri_summary["count"],
            "description": "建议感染质控人员核查疑似 VAP/CRBSI/CAUTI 线索。",
        })
    if sepsis_summary["count"]:
        ai_todos.append({
            "type": "sepsis_alert",
            "title": "脓毒症早期预警待评估",
            "count": sepsis_summary["count"],
            "description": "基于 Sepsis-3 / qSOFA 的质控分诊提示，建议临床团队评估高危线索。",
        })
    if low_confidence["count"]:
        ai_todos.append({
            "type": "low_confidence_abx",
            "title": "抗菌药 AI 判定待复核",
            "count": low_confidence["count"],
            "description": "建议复核 ICU-06 分母中低置信度治疗/预防用药判定。",
        })

    # Census data
    census = {}
    census_trend = []
    try:
        for p in periods:
            sy, sm = [int(x) for x in p.split("-")]
            c = get_patient_census(dept_codes, f"{sy}-{sm:02d}-01",
                                    f"{sy}-{sm:02d}-{_month_end_day(sy, sm):02d}")
            census_trend.append({"period": p, **c})
        if census_trend:
            census = census_trend[-1]
    except Exception:
        census = {"carry_in": 0, "new_admit": 0, "discharge": 0, "carry_out": 0, "total": 0}

    # 检测数据完整性
    data_complete = True
    missing_periods = []
    for row in rows:
        if row.get("status") == "unknown":
            # unknown 表示该指标在该月份无预聚合数据
            monthly = row.get("monthly", [])
            for i, val in enumerate(monthly):
                if val is None and i < len(periods):
                    mp = periods[i]
                    if mp not in missing_periods:
                        missing_periods.append(mp)
    if missing_periods:
        data_complete = False

    # 调度器状态
    try:
        from scheduler import scheduler as sched_mgr
        sched_status = sched_mgr.get_latest_status()
        rebuilding = sched_status.get("rebuilding", False)
        last_rebuilt_at = sched_mgr.get_last_success_at()
    except Exception:
        rebuilding = False
        last_rebuilt_at = None

    # 如果发现缺失月份且未在重建中，触发补算（当前月始终重算）
    if missing_periods and not rebuilding:
        try:
            import threading as _threading
            # 使用实际自然月作为当前月（不是查询范围的第一天）
            from datetime import datetime as _dt
            actual_current_period = _dt.now().strftime("%Y-%m")
            # 仅当实际当前月在查询范围时才加入重算列表
            all_to_rebuild = list(missing_periods)
            if actual_current_period in periods and actual_current_period not in all_to_rebuild:
                all_to_rebuild.append(actual_current_period)
            def _bg_rebuild():
                try:
                    from scheduler import scheduler as _sched
                    if not _sched.acquire_lock("auto_fix_missing"):
                        logger.warning("[command_center] Cannot acquire lock, skip rebuild")
                        return
                    try:
                        _task_id = _sched.record_start("auto_fix_missing", all_to_rebuild,
                                                        extra_info={"trigger": "command_center"})
                        stats = summary_module.rebuild_summary(dept_codes, all_to_rebuild)
                        _sched.record_finish(_task_id, status="completed", stats=stats)
                    finally:
                        _sched.release_lock("auto_fix_missing")
                except Exception as e:
                    logger.error("Auto-fix missing periods failed: %s", e)
            _threading.Thread(target=_bg_rebuild, daemon=True).start()
            rebuilding = True
        except Exception:
            pass

    return {
        "period": period,
        "end_period": end_period or period,
        "icu_unit": icu_unit,
        "months": months,
        "periods": periods,
        "rows": rows,
        "values": values,
        "trend": trend,
        "risk": {
            "overall_status": overall_status,
            "counts": risk_counts,
            "abnormal_count": risk_counts["danger"],
            "warning_count": risk_counts["warn"],
            "attention_count": needs_attention,
            "unknown_count": risk_counts["unknown"],
            "headline": f"{risk_counts['danger']}项严重异常，{risk_counts['warn']}项预警，合计{needs_attention}项需关注",
            "explain": "严重异常=超过异常阈值的指标；预警=未达最佳但仍在预警范围的指标；AI异常列表中的数量为严重异常+预警合计。",
        },
        "abnormal": abnormal,
        "ai": {
            "summary": ai.get("summary", ""),
            "abnormal": ai.get("abnormal", []),
            "hints": ai.get("hints", []),
            "todos": ai_todos,
            "tri_tube": tri_summary,
            "sepsis_alert": sepsis_summary,
            "low_confidence": low_confidence,
            "explain": "AI待办=三管疑似线索数+脓毒症早期预警+低置信度抗菌药判定数；疑似线索不等同于已确诊事件。",
        },
        "census": census if "census" in dir() else {},
        "census_trend": census_trend if "census_trend" in dir() else [],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        # 数据完整性
        "data_complete": data_complete,
        "missing_periods": missing_periods,
        "rebuilding": rebuilding,
        "last_rebuilt_at": last_rebuilt_at,
    }

@app.get("/api/indicators/list")
def indicator_list(period: str, icu_unit: str = "all", end_period: str = "", nocache: bool = False):
    """
    第一级：指标汇总列表。
    period=2026-06 单月, period=2026-01&end_period=2026-06 跨月汇总。
    nocache=true 强制刷新，绕过服务器缓存。
    """
    ck = _cache_key("list", period, icu_unit, end_period)
    cached = _cache_get(ck, nocache=nocache)
    if cached is not None:
        return cached
    if end_period:
        # 跨月汇总：逐月累加分子/分母，记录每月比值
        start_y, start_m = period.split("-")
        end_y, end_m = end_period.split("-")
        month_labels = []
        y, m = int(start_y), int(start_m)
        while (y < int(end_y)) or (y == int(end_y) and m <= int(end_m)):
            month_labels.append(f"{y}-{m:02d}")
            m += 1
            if m > 12:
                m = 1
                y += 1

        summary_rows = summary_module.read_summary(_resolve_dept_codes(icu_unit), month_labels)
        if summary_rows:
            rows_by_month = {}
            for r in summary_rows:
                rows_by_month.setdefault(r["period"], []).append(r)
        else:
            rows_by_month = {}

        # 检测缺失月份
        months_with_data = set(rows_by_month.keys())
        missing_periods = [m for m in month_labels if m not in months_with_data]
        data_complete = len(missing_periods) == 0

        agg = {}       # code → {num, den, unit, name, monthly: {mon: val}, diag_fields}
        for mon in month_labels:
            rows = rows_by_month.get(mon) or []
            if rows_by_month.get(mon):
                rows = [_summary_row_to_api(r) for r in rows]
            for r in rows:
                code = r.get("indicator") or r.get("code")
                if code not in agg:
                    agg[code] = {"num": 0, "den": 0, "unit": UNIT_MAP.get(code, ""),
                                 "name": NAME_MAP.get(code, code), "monthly": {},
                                 "diag_fields": {}}
                agg[code]["num"] += r.get("numerator", 0)
                agg[code]["den"] += r.get("denominator", 0)
                agg[code]["monthly"][mon] = r.get("value", 0)
                # 累加诊断字段
                for df in ("raw_candidate_count", "high_probability_count", "probable_count",
                           "pending_review_count", "not_candidate_count", "diagnosis_based_count",
                           "candidate_den", "raw_num", "raw_den", "excluded_num", "excluded_den"):
                    if r.get(df) is not None:
                        agg[code]["diag_fields"][df] = agg[code]["diag_fields"].get(df, 0) + r[df]
                if r.get("candidate_mode"):
                    agg[code]["diag_fields"]["candidate_mode"] = r["candidate_mode"]

        # ICU-02/03 是时点值(人数/床位数),跨月不累加,取最后一个月
        for code in ("ICU-02", "ICU-03"):
            if code in agg and len(month_labels) > 1:
                last = rows_by_month.get(month_labels[-1]) or []
                for r in last:
                    if (r.get("indicator") or r.get("code")) == code:
                        agg[code]["num"] = r.get("numerator", 0)
                        agg[code]["den"] = r.get("denominator", 0)

        # ICU-05 跨月：预聚合优先（与单月/趋势同策略）。
        # 预聚合行由上方聚合循环累加进 agg（monthly 逐月记录）；
        # 仅对预聚合缺失的月份实时补算 —— 全覆盖时零实时开销。
        dept_codes = _resolve_dept_codes(icu_unit)
        icu05_missing = {}
        for hour in ("1h", "3h", "6h"):
            code = f"ICU-05-{hour}"
            pre = agg.get(code)
            miss = [mon for mon in month_labels
                    if not pre or mon not in pre.get("monthly", {})]
            if miss:
                icu05_missing[code] = miss

        if icu05_missing:
            try:
                from scoring.bundle_engine import set_vaso_wide_labels
                sc = get_client("SmartCare")["SmartCare"]
                vaso_drugs = list(sc.configDrug.find(
                    {"classification": {"$regex": "血管活性", "$options": "i"}},
                    {"name": 1, "_id": 0}
                ))
                labels = {d["name"].strip() for d in vaso_drugs if d.get("name")}
                if labels:
                    set_vaso_wide_labels(labels)
            except Exception:
                pass

            for code, missing_mons in icu05_missing.items():
                hour = code.rsplit("-", 1)[1]
                # #修复: 单位原硬编码 "‰"，ICU-05 配置为 "%"（跨月值被误算成千分比）
                if code not in agg:
                    agg[code] = {"num": 0, "den": 0, "unit": UNIT_MAP.get(code, ""),
                                 "name": NAME_MAP.get(code, code),
                                 "monthly": {}, "diag_fields": {}}
                for mon in missing_mons:
                    try:
                        from summary import _compute_icu05
                        import calendar
                        y, m = map(int, mon.split("-"))
                        end_day = calendar.monthrange(y, m)[1]
                        icu05_data = _compute_icu05(dept_codes, f"{mon}-01", f"{mon}-{end_day:02d}", hour)
                        if icu05_data:
                            # #修复: _compute_icu05 返回的键是 num/den/val（原读 numerator/denominator/value 恒为 None）
                            agg[code]["num"] += icu05_data.get("num", 0)
                            agg[code]["den"] += icu05_data.get("den", 0)
                            agg[code]["monthly"][mon] = icu05_data.get("val", 0)
                            for df in ("candidate_den", "high_probability_count", "probable_count",
                                       "pending_review_count", "not_candidate_count", "raw_candidate_count",
                                       "old_shock_count", "new_shock_count"):
                                if icu05_data.get(df) is not None:
                                    agg[code]["diag_fields"][df] = agg[code]["diag_fields"].get(df, 0) + icu05_data[df]
                            if icu05_data.get("candidate_mode"):
                                agg[code]["diag_fields"]["candidate_mode"] = icu05_data["candidate_mode"]
                    except Exception as e:
                        logger.warning("ICU-05-%s realtime failed for %s: %s", hour, mon, e)

        result = []
        for code in UNIT_MAP:
            if code not in agg:
                agg[code] = {
                    "num": None,
                    "den": None,
                    "unit": UNIT_MAP.get(code, ""),
                    "name": NAME_MAP.get(code, code),
                    "monthly": {},
                }
        for code, v in agg.items():
            if v["num"] is None or v["den"] is None:
                val = None
            elif code in ("ICU-04A", "ICU-04B", "ICU-04C", "ICU-04D", "ICU-04E"):
                val = float(v["num"] or 0)  # APACHE 分段：值=人数
            elif code == "ICU-11":
                val = round(v["num"] / v["den"], 2) if v["den"] > 0 else 0
            elif v["unit"] == "‰":
                val = round(v["num"] / v["den"] * 1000, 1) if v["den"] > 0 else 0
            elif v["unit"] == ":1":
                val = round(v["num"] / v["den"], 1) if v["den"] > 0 else 0
            else:
                val = round(v["num"] / v["den"] * 100, 1) if v["den"] > 0 else 0
            row = {
                "code": code,
                "name": v["name"],
                "numerator": v["num"],
                "denominator": v["den"],
                "value": val,
                "unit": v["unit"],
                "status": "unknown" if val is None else eval_status(code, val),
                "months": month_labels,
                "monthly": [v["monthly"].get(mon) for mon in month_labels],
                # 缺失月份检测
                "data_complete": data_complete,
                "missing_periods": missing_periods,
                "aggregation_periods": month_labels,
            }
            # 诊断字段
            if v.get("diag_fields"):
                row.update(v["diag_fields"])
            result.append(row)
        result = sorted(result, key=lambda x: x["code"])
        result = _ensure_no_data_indicator_rows(result)
        _cache_set(ck, result)
        return result

    # 单月查询
    dept_codes = _resolve_dept_codes(icu_unit)
    rows = summary_module.read_summary(dept_codes, [period])
    if rows:
        result = [_summary_row_to_api(r) for r in rows]
    else:
        result = []

    # ICU-05 预聚合优先（性能关键路径）：
    # 预聚合与实时是同一 _compute_icu05、同一次重建，同源同值；
    # 原「恒实时覆盖」使列表每次冷启动多付 3×全量 V3 判定
    # （单科室 ~2s、全院 60s+，是列表打开慢的最大头）。
    # 仅当预聚合缺失（新科室未重建）才实时兜底。
    icu05_missing = [h for h in ("1h", "3h", "6h")
                     if not any(r.get("code") == f"ICU-05-{h}" for r in result)]
    if icu05_missing:
        try:
            from scoring.bundle_engine import set_vaso_wide_labels
            sc = get_client("SmartCare")["SmartCare"]
            vaso_drugs = list(sc.configDrug.find(
                {"classification": {"$regex": "血管活性", "$options": "i"}},
                {"name": 1, "_id": 0}
            ))
            labels = {d["name"].strip() for d in vaso_drugs if d.get("name")}
            if labels:
                set_vaso_wide_labels(labels)
        except Exception:
            pass

        start_date = f"{period}-01"
        import calendar
        y, m = map(int, period.split("-"))
        end_day = calendar.monthrange(y, m)[1]
        end_date = f"{period}-{end_day:02d}"

        for hour in icu05_missing:
            try:
                from summary import _compute_icu05
                icu05_data = _compute_icu05(dept_codes, start_date, end_date, hour)
                if icu05_data:
                    # icu05_missing 保证 result 中无此 code，直接追加
                    code = f"ICU-05-{hour}"
                    result.append(_summary_row_to_api({
                        "indicator": code,
                        # #修复: 键名对齐 _compute_icu05 的 num/den/val（原 numerator/denominator/value 恒 None）
                        "denominator": icu05_data.get("den"),
                        "numerator": icu05_data.get("num"),
                        "value": icu05_data.get("val"),
                        "old_shock_count": icu05_data.get("old_shock_count"),
                        "new_shock_count": icu05_data.get("new_shock_count"),
                        "candidate_den": icu05_data.get("candidate_den"),
                        "candidate_mode": icu05_data.get("candidate_mode"),
                        "high_probability_count": icu05_data.get("high_probability_count"),
                        "probable_count": icu05_data.get("probable_count"),
                        "pending_review_count": icu05_data.get("pending_review_count"),
                        "not_candidate_count": icu05_data.get("not_candidate_count"),
                        "raw_candidate_count": icu05_data.get("raw_candidate_count"),
                        "diagnosis_based_count": icu05_data.get("diagnosis_based_count"),
                    }))
            except Exception as e:
                logger.warning("ICU-05-%s realtime compute failed: %s", hour, e)

    existing_codes = {r.get("code") for r in result}
    for code in UNIT_MAP:
        if code not in existing_codes:
            result.append(_empty_indicator_row(code))
    result = _ensure_no_data_indicator_rows(result)
    _cache_set(ck, result)
    return result

@app.get("/api/indicators/{code}/trend")
def indicator_trend(code: str, year: int, icu_unit: str = "all",
                    start_month: int = 1, end_month: int = 12):
    """第二级：某指标趋势，支持指定月份范围"""
    rows = query_trend(code, year, icu_unit)
    months = [f"{m}月" for m in range(start_month, end_month + 1)]
    values = [rows.get(m) for m in range(start_month, end_month + 1)]
    return {
        "code": code,
        "name": NAME_MAP.get(code, code),
        "months": months,
        "values": values,
    }


@app.get("/api/export/report-values")
def export_report_values(period: str, icu_unit: str = "all", nocache: bool = False):
    """
    《重症质控指标上报（2024年版）》月度上报值（53 项）。

    period=YYYY-MM；values[key]=null 表示无系统数据（导出时留空，人工填报）。
    计算逻辑见 report_export.py，口径与看板指标一致。
    """
    import re as _re
    if not _re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period or ""):
        raise HTTPException(status_code=400, detail="period 须为 YYYY-MM 格式")
    from report_export import compute_report_values, _VALUES_CACHE
    if nocache:
        _VALUES_CACHE.pop((period, icu_unit), None)
    return compute_report_values(period, icu_unit)


@app.get("/api/indicators/{code}/detail")
def indicator_detail(code: str, period: str, part: str, icu_unit: str = "all", end_period: str = "",
                     nocache: bool = False, limit: int = 200, offset: int = 0):
    """
    第三级：分子/分母下钻明细。
    支持 end_period 跨月聚合。
    nocache=true 强制刷新。
    """
    limit = max(1, min(int(limit or 200), 1000))
    offset = max(0, int(offset or 0))
    ck = _cache_key("detail", code, period, part, icu_unit, end_period)
    cached = _cache_get(ck, nocache=nocache)
    if cached is not None:
        result = dict(cached)
        all_items = result.get("all_patients") or result.get("patients", [])
        result["count"] = len(all_items)
        result["limit"] = limit
        result["offset"] = offset
        result["has_more"] = offset + limit < len(all_items)
        result["patients"] = all_items[offset:offset + limit]
        result.pop("all_patients", None)
        return result
    # 跨月汇总
    if end_period:
        start_y, start_m = period.split("-")
        end_y, end_m = end_period.split("-")
        months = []
        y, m = int(start_y), int(start_m)
        while (y < int(end_y)) or (y == int(end_y) and m <= int(end_m)):
            months.append(f"{y}-{m:02d}")
            m += 1
            if m > 12:
                m = 1
                y += 1
        # 聚合口径与汇总「逐月相加」对齐：
        # - 汇总行（pid=="—"）与 ICU-01 床日累计：跨月合并为一行（保持原语义）
        # - 患者类行：按「统计月 + 患者」成行，同一患者跨月各占一行，
        #   行数恒等于公式条分子/分母（stat_month 供前端「统计月」列展示）
        merged = {}
        for mon in months:
            for idx, p in enumerate(_detail_cache_payload(code, mon, part, icu_unit, nocache=nocache)):
                pid = p.get("detail_id") or p.get("patient_id", "")
                if pid == "—" or (code == "ICU-01" and part == "numerator"):
                    # 累加数值
                    if pid in merged:
                        merged[pid]["value"] = (merged[pid].get("value", 0) or 0) + (p.get("value", 0) or 0)
                    else:
                        merged[pid] = dict(p)
                else:
                    key = f"{mon}||{pid}" if pid else f"{mon}||#{idx}"
                    row = dict(p)
                    row["stat_month"] = mon
                    merged[key] = row
        items = list(merged.values())

        # 跨月分母：重算总天数和名称
        if code == "ICU-01" and part == "denominator":
            import calendar
            total_days = sum(
                calendar.monthrange(int(m.split("-")[0]), int(m.split("-")[1]))[1]
                for m in months
            )
            # 取床位数
            dcodes = _resolve_dept_codes(icu_unit)
            total_beds_count = 0
            for db_name in BED_DB_NAMES:
                try:
                    db = get_client()[db_name]
                    total_beds_count = sum(db.configBed.count_documents({"deptCode": dc}) for dc in dcodes)
                    if total_beds_count > 0: break
                except Exception: continue
            if total_beds_count == 0: total_beds_count = 18
            for p in items:
                if p.get("patient_id") == "—":
                    p["name"] = f"开放床位数 {total_beds_count} 张 × 统计 {total_days} 天 = {total_beds_count * total_days} 总床日"
                    p["value"] = total_beds_count * total_days
                    p["bed_no"] = f"{total_beds_count} 张"
                    p["admit_time"] = f"统计 {total_days} 天"
    else:
        items = _detail_cache_payload(code, period, part, icu_unit, nocache=nocache)

    # 各指标明细描述
    # ICU-05: 附加原因分布（#设计: 三色语义 — 红=明确未达标 / 橙=顺序问题 / 灰=数据缺失）
    reason_summary = None
    if code.startswith("ICU-05") and part == "denominator":
        def _agg_reasons(patients):
            done = failed = uncertain = 0
            failed_ctr, uncertain_ctr = {}, {}
            for p in patients:
                v3 = p.get("v3") or {}
                codes = v3.get("reason_codes") or []
                fin = v3.get("finish")
                if fin is True:
                    done += 1
                    continue
                primary = (codes[0] if codes else None) or v3.get("reason") or ""
                if fin is False:
                    failed += 1
                    if primary:
                        failed_ctr[primary] = failed_ctr.get(primary, 0) + 1
                else:
                    uncertain += 1
                    if primary:
                        uncertain_ctr[primary] = uncertain_ctr.get(primary, 0) + 1
            to_list = lambda ctr: sorted(
                ({"code": c, "count": n} for c, n in ctr.items()),
                key=lambda x: -x["count"])
            return {"done": done, "failed": failed, "uncertain": uncertain,
                    "failed_reasons": to_list(failed_ctr),
                    "uncertain_reasons": to_list(uncertain_ctr)}
        reason_summary = _agg_reasons(items)

    if code == "ICU-01":
        source_desc = "实际占用总床日数 — 每位患者在统计期内的在床天数" if part == "numerator" \
            else "实际开放总床日数 — 各科室床位配置（每床每日计1床日）"
    elif code == "ICU-02":
        source_desc = "分子：来自 account 表，筛选 valid 医师（主任医师/副主任医师/主治医师/医师/规培/进修/实习）" if part == "numerator" \
            else "分母：来自 configBed 表，统计科室实际开放床位数"
    elif code == "ICU-03":
        source_desc = "分子：来自 account 表，筛选 valid 护士（护士长/护理组长/护士/规培/进修/实习护士）" if part == "numerator" \
            else "分母：来自 configBed 表，统计科室实际开放床位数"
    elif code == "ICU-04":
        source_desc = "分子：来自 score 表，当月首次 APACHEⅡ 评分 total ≥ 15 的患者" if part == "numerator" \
            else "分母：来自 patient 表，统计期内在科患者（排除 invalid）。人数口径 —— 按住院号去重，同一患者当月多次入科只算一人"
    elif code in ("ICU-04A", "ICU-04B", "ICU-04C", "ICU-04D", "ICU-04E", "ICU-04F"):
        band_desc = {
            "ICU-04A": "APACHEⅡ < 10 分",
            "ICU-04B": "10 ≤ APACHEⅡ < 15 分",
            "ICU-04C": "15 ≤ APACHEⅡ < 20 分",
            "ICU-04D": "20 ≤ APACHEⅡ < 25 分",
            "ICU-04E": "APACHEⅡ ≥ 25 分",
            "ICU-04F": "APACHEⅡ < 15 分",
        }[code]
        source_desc = (
            f"分子：来自 score 表，当月首次 APACHEⅡ 评分落入「{band_desc}」的患者（人数口径）"
            if part == "numerator"
            else "分母：来自 patient 表，统计期内在科患者（排除 invalid）。人数口径 —— 按住院号去重，与 ICU-04 同队列"
        )
    elif code in ("ICU-05-1h", "ICU-05-3h", "ICU-05-6h"):
        h = code.split("-")[2]
        # 人话文案（#设计: 替换原 V3/K1/A1/B3 技术黑话）
        if part == "numerator":
            source_desc = (f"分子：{h} 内完成 Bundle 三步的患者 — "
                           "①乳酸测定 ②先做血培养、再用抗生素 ③液体复苏达标（1h窗有液体即可，3h/6h窗≥1500ml）")
        else:
            source_desc = ("分母：入院 24h 内进 ICU、且确诊脓毒性休克的患者 — "
                           "同时满足 ①器官障碍（氧合/GCS/MAP/升压药任一）②感染证据（诊断/抗感染/病原学送检任一）"
                           "③乳酸≥2 且在用升压药；T0 = 入科首条医嘱时间")
    elif code == "ICU-06":
        source_desc = (f"分子：来自 VI_ICU_ZYYZ 培养类检验医嘱，首次抗生素前有病原学送检的患者（送检≤首剂时间）" if part == "numerator"
            else f"分母：来自 drugExe 抗菌药执行记录，经三层判定（A感染信号→B围术期→C短疗程→AI灰区）确认治疗目的，已剔除预防性用药")
    elif code == "ICU-07":
        source_desc = ("分子：来自 DataCenter.VI_ICU_ZYYZ，统计期内已执行的抗凝药或机械预防医嘱"
                       "（按分母队列 mrn 反查 pid，不经 ZYBR 科室过滤；剔除检验假匹配、"
                       "封管/有创压管路维护、血液净化管路抗凝）" if part == "numerator"
            else "分母：来自 patient 表，统计期内在科患者（deptCode 属本科室、排除 invalid、入出科时间与统计期有交集）。"
                 "人数口径 —— 按住院号去重，同一患者当月多次入科只算一人，与分子同口径")
    elif code == "ICU-09":
        source_desc = "分子：床旁评估记录或量表评分记录中完成镇痛评分（NRS/CPOT/BPS等）的患者，明细展示评分中文名、分值和时间" if part == "numerator" \
            else "分母：同期入住ICU的患者总人数（人数口径 —— 按住院号去重，同一患者当月多次入科只算一人）"
    elif code == "ICU-10":
        source_desc = "分子：床旁评估记录或量表评分记录中完成镇静评分（RASS等）的患者，明细展示评分中文名、分值和时间" if part == "numerator" \
            else "分母：同期入住ICU的患者总人数（人数口径 —— 按住院号去重，同一患者当月多次入科只算一人）"
    elif code == "ICU-11":
        source_desc = "分子：已结案完整病例中在院死亡或非医嘱离院的患者数" if part == "numerator" \
            else "分母：死亡、出院、非医嘱离院且有入科24小时内首次APACHEⅡ评分病例的预计死亡率之和"
    elif code == "ICU-12":
        source_desc = "分子：气管插管/气插管/气切套管拔管记录中 unPlannedEndTube 为 true 的非计划拔管，已排除换管" if part == "numerator" \
            else "分母：统计期内气管插管/气插管/气切套管拔管记录，已排除 replace=true 换管"
    elif code == "ICU-13":
        source_desc = "分子：拔管后48小时内同患者再次气管插管/气插管/气切套管，气管插管转气切套管计入，replace=true 换管不计" if part == "numerator" \
            else "分母：统计期内气管插管/气插管/气切套管拔管记录，已排除 replace=true 换管"
    elif code == "ICU-19":
        source_desc = (
            "分子：入住ICU超过48小时患者中，入科48小时内启动肠内营养(EN)者；优先取护理评估，其次营养医嘱和医嘱名称兜底"
            if part == "numerator"
            else "分母：同期入住ICU时间超过48小时的患者；EN禁忌证仅标注留痕，不自动剔除"
        )
    elif code == "ICU-00":
        part_labels = {"carry_in": "原有患者（期初在科）", "new_admit": "新入患者", "discharge": "出科患者"}
        source_desc = part_labels.get(part, "患者动态明细")
    else:
        desc_info = SOURCE_DESC.get(code, {"numerator": "分子明细", "denominator": "分母明细"})
        source_desc = desc_info.get(part, "明细")

    result = {
        "code": code,
        "name": NAME_MAP.get(code, code),
        "part": part,
        "count": len(items),
        "source_desc": source_desc,
        "all_patients": items,
    }
    if reason_summary is not None:
        result["reason_summary"] = reason_summary
    _cache_set(ck, result)
    paged = dict(result)
    paged["limit"] = limit
    paged["offset"] = offset
    paged["has_more"] = offset + limit < len(items)
    paged["patients"] = items[offset:offset + limit]
    paged.pop("all_patients", None)
    return paged


# ============================================================
# 人工排除 CRUD 接口
# ============================================================

@app.get("/api/indicators/{code}/exclusions")
def get_exclusions(code: str, period: str, icu_unit: str = "all"):
    dept_codes = _resolve_dept_codes(icu_unit)
    dept_key = _dept_cache_key(dept_codes)
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            coll = db[EXCLUSION_COLLECTION]
            docs = list(coll.find(
                {"dept_code": dept_key, "period": period, "code": code},
                {"_id": 0},
            ).sort("created_at", -1))
            return {"exclusions": docs}
        except Exception:
            continue
    return {"exclusions": []}


@app.post("/api/indicators/{code}/exclusions")
def add_exclusion(code: str, body: dict = None):
    if not body:
        return {"error": "Missing body"}
    dept_codes = _resolve_dept_codes(body.get("icu_unit", "all"))
    dept_key = _dept_cache_key(dept_codes)
    now = datetime.utcnow()
    doc = {
        "dept_code": dept_key,
        "period": body["period"],
        "code": code,
        "exclusion_key": body["exclusion_key"],
        "patient_id": body.get("patient_id", ""),
        "name": body.get("name", ""),
        "excluded": True,
        "reason_code": body.get("reason_code", ""),
        "reason_text": body.get("reason_text", ""),
        "operator": body.get("operator", ""),
        "created_at": now,
        "updated_at": now,
        "history": [{"action": "exclude", "time": now, "operator": body.get("operator", ""),
                      "reason_code": body.get("reason_code", ""), "reason_text": body.get("reason_text", "")}],
    }
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            coll = db[EXCLUSION_COLLECTION]
            history_entry = doc.pop("history")[0]
            doc.pop("created_at")
            coll.update_one(
                {"dept_code": dept_key, "period": body["period"], "code": code,
                 "exclusion_key": body["exclusion_key"]},
                {"$set": doc,
                 "$setOnInsert": {"created_at": now},
                 "$push": {"history": history_entry}},
                upsert=True,
            )
            _invalidate_detail_cache(dept_codes, body["period"], code)
            _trigger_summary_rebuild(dept_codes, [body["period"]], indicators=[code])
            return {"ok": True}
        except Exception as exc:
            logger.exception("Failed to save exclusion for %s/%s: %s", code, body.get("exclusion_key"), exc)
            continue
    return {"ok": False, "error": "Unable to save exclusion; see server log for database error"}


@app.delete("/api/indicators/{code}/exclusions/{exclusion_key}")
def remove_exclusion(code: str, exclusion_key: str, period: str = "", icu_unit: str = "all"):
    dept_codes = _resolve_dept_codes(icu_unit)
    dept_key = _dept_cache_key(dept_codes)
    now = datetime.utcnow()
    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            coll = db[EXCLUSION_COLLECTION]
            result = coll.update_one(
                {"dept_code": dept_key, "period": period, "code": code,
                 "exclusion_key": exclusion_key},
                {"$set": {"excluded": False, "updated_at": now},
                 "$push": {"history": {"action": "restore", "time": now, "operator": "system",
                                       "reason_code": "", "reason_text": ""}}},
            )
            if result.modified_count:
                _invalidate_detail_cache(dept_codes, period, code)
                _trigger_summary_rebuild(dept_codes, [period], indicators=[code])
            return {"ok": True}
        except Exception as exc:
            logger.exception("Failed to restore exclusion for %s/%s: %s", code, exclusion_key, exc)
            continue
    return {"ok": False, "error": "Unable to restore exclusion; see server log for database error"}


# ============================================================
# V3 Bundle 判定详情 API
# ============================================================

@app.get("/api/bundle/v3/{mrn}")
def get_bundle_v3_detail(mrn: str, period: str = ""):
    """获取单患者的 V3 Bundle 判定详情。"""
    if not period:
        now = datetime.utcnow()
        period = f"{now.year}-{now.month:02d}"
    year, month = map(int, period.split("-"))
    start_date = f"{year}-{month:02d}-01"
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    end_date = f"{year}-{month:02d}-{last_day}"

    # 从分母患者中查找
    data = get_bundle_data_v2([], start_date, end_date)
    for p in data.get("den_patients", []):
        if p.get("mrn") == mrn:
            sc_pid = p.get("sc_pid")
            dc_pid = resolve_bundle_dc_pid(p)
            t0 = p.get("t0")
            diag = p.get("diagnose", "")
            if t0 and sc_pid:
                res = judge_bundle_v3_for_patient(sc_pid, dc_pid, mrn, t0, diag)
                return {"ok": True, "patient": {
                    "mrn": mrn, "name": p.get("name", ""),
                    "t0": str(t0) if t0 else None,
                    "diagnose": diag,
                }, "v3": res}
            return {"ok": True, "patient": {"mrn": mrn, "name": p.get("name", "")},
                    "v3": {"reason": "NO_T0"}}
    return {"error": f"Patient MRN {mrn} not found in {period} denominator"}


@app.get("/api/patients/{pid}/sofa")
def get_patient_sofa(pid: str, eval_time: str = "", event_id: str = ""):
    """
    获取单患者的正式 SOFA 评分结果（经典 SOFA + SOFA-2）。

    改进:
    1. 不再调用 get_bundle_data_v2 扫描全月候选
    2. 要求或解析明确的住院/ICU事件和eval_time
    3. ObjectId解析失败后仍按MRN/hisPid查找
    4. 返回明确的400/404/422/500状态
    5. 限制查询范围和执行时间
    """
    from scoring.sofa_bridge import compute_sofa_scores

    # 参数验证
    if not pid:
        return JSONResponse(status_code=400, content={"error": "Missing pid parameter"})

    # 查找患者信息
    pat = None
    sc_pid = None
    mrn = None
    dc_pid = None

    for db_name in BED_DB_NAMES:
        try:
            db = get_client(db_name)[db_name]
            # 尝试按 sc_pid (ObjectId) 查找
            if len(pid) == 24:
                try:
                    from bson import ObjectId
                    pat = db.patient.find_one({"_id": ObjectId(pid)})
                except Exception:
                    pass  # ObjectId解析失败，继续按MRN查找
            # 按 hisPid/MRN 查找
            if not pat:
                pat = db.patient.find_one({"hisPid": pid})
            if not pat:
                pat = db.patient.find_one({"mrn": pid})
            if pat:
                sc_pid = str(pat.get("_id", ""))
                mrn = pat.get("hisPid") or pat.get("mrn") or pid
                dc_pid = pat.get("dcPid", "")
                break
        except Exception:
            continue

    if not pat:
        return JSONResponse(status_code=404, content={"error": f"Patient {pid} not found"})

    # 确定评估时间
    et = None
    if eval_time:
        try:
            et = datetime.fromisoformat(eval_time.replace("Z", "+00:00"))
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid eval_time format: {eval_time}. Use ISO format."}
            )
    if et is None:
        et = datetime.utcnow()

    # 获取 T0
    # 优先从 event_id 获取，否则不使用eval_time作为fallback
    # 修复: eval_time只表示评分时点，不等于T0(感染时点)
    t0 = None
    t0_source = None
    if event_id:
        try:
            from bson import ObjectId
            for db_name in BED_DB_NAMES:
                try:
                    db = get_client(db_name)[db_name]
                    event = db.events.find_one({"_id": ObjectId(event_id)})
                    if event:
                        t0 = event.get("t0") or event.get("admit_time")
                        t0_source = "event"
                        break
                except Exception:
                    continue
        except Exception as e:
            logger.warning("Event lookup failed for event_id=%s: %s", event_id, e)

    t0_status = "found" if t0 else "missing"

    # 计算SOFA评分
    try:
        result = compute_sofa_scores(
            sc_pid=sc_pid, mrn=mrn, dc_pid=dc_pid,
            t0=t0, eval_time=et,
        )
        return {
            "ok": True,
            "pid": pid,
            "sc_pid": sc_pid,
            "mrn": mrn,
            "dc_pid": dc_pid,
            "t0": t0.isoformat() if t0 else None,
            "t0_status": t0_status,
            "t0_source": t0_source,
            "eval_time": et.isoformat() if et else None,
            "sofa": result,
        }
    except Exception as e:
        logger.error("SOFA computation failed for pid=%s: %s\n%s", pid, e, traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"error": f"SOFA computation failed: {str(e)[:200]}"}
        )


# ============================================================
# Stage 7: 三层人工覆盖 API
# ============================================================

@app.get("/api/bundle/{pid}/overrides")
def get_bundle_overrides(pid: str, level: str = None):
    """获取患者的三层覆盖记录。"""
    from db import get_overrides
    docs = get_overrides(pid, level=level)
    return {"overrides": docs}


@app.post("/api/bundle/{pid}/overrides")
def add_bundle_override(pid: str, body: dict = None):
    """添加三层覆盖记录。"""
    if not body:
        return {"error": "Missing body"}
    from db import save_override
    body["pid"] = pid
    # 解析 eval_time
    eval_time_raw = body.get("eval_time")
    if isinstance(eval_time_raw, str):
        try:
            body["eval_time"] = datetime.fromisoformat(eval_time_raw.replace("Z", "+00:00"))
        except ValueError:
            body["eval_time"] = datetime.utcnow()
    elif not isinstance(eval_time_raw, datetime):
        body["eval_time"] = datetime.utcnow()
    result = save_override(body)
    return result


@app.delete("/api/bundle/overrides/{override_key}")
def remove_bundle_override(override_key: str):
    """删除三层覆盖记录。"""
    from db import delete_override
    result = delete_override(override_key)
    return result


@app.get("/api/bundle/override-stats")
def bundle_override_stats(period: str, icu_unit: str = "all"):
    """获取覆盖统计。"""
    from db import get_override_stats
    year, month = period.split("-")
    import calendar
    end_day = calendar.monthrange(int(year), int(month))[1]
    start = f"{year}-{month}-01"
    end = f"{year}-{month}-{end_day:02d}"
    dept_codes = _resolve_dept_codes(icu_unit)
    result = get_override_stats(dept_codes, start, end)
    return result


# ============================================================
# Stage 6: 感染部位 API
# ============================================================

@app.get("/api/infection_site")
def api_get_infection_site(exclusion_key: str):
    """查询未撤销的感染部位记录。"""
    from db import get_infection_site
    doc = get_infection_site(exclusion_key)
    return {"found": doc is not None, "record": doc}


@app.post("/api/infection_site")
def api_create_infection_site(body: dict = None):
    """新建/替换感染部位记录。已存在未撤销记录时先撤销再插入。"""
    if not body:
        return {"error": "Missing body"}
    from db import create_infection_site
    try:
        result = create_infection_site(body)
        return {"ok": True, **result}
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}


@app.delete("/api/infection_site/{record_id}")
def api_revoke_infection_site(record_id: str, revoked_by: str = "system"):
    """撤销感染部位记录（逻辑删除，不物理删除）。"""
    from db import revoke_infection_site
    try:
        result = revoke_infection_site(record_id, revoked_by)
        return {"ok": True, **result}
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/infection_site/suggest")
def api_suggest_infection_site(pid: str, eval_time: str = None):
    """
    自动推荐感染部位。读取诊断、医嘱、培养标本，返回打分结果。
    纯函数，不写库。
    """
    from db import suggest_infection_site
    from datetime import datetime as _dt
    from pymongo import ASCENDING

    # 解析 eval_time
    if eval_time:
        try:
            et = _dt.fromisoformat(eval_time.replace("Z", "+00:00"))
        except ValueError:
            et = datetime.utcnow()
    else:
        et = datetime.utcnow()

    # 采集 pid 的所有入库记录，取诊断文本、医嘱名、培养标本
    diagnosis_text = ""
    order_names: list[str] = []
    culture_specimens: list[str] = []

    for db_name in BED_DB_NAMES:
        try:
            db = get_client()[db_name]
            doc = db.VI_ICU_ZYBR.find_one(
                {"hisPid": pid},
                {"diagnose": 1, "_id": 0},
            )
            if doc:
                diagnosis_text = doc.get("diagnose", "") or ""
            break
        except Exception:
            continue

    # 医嘱名
    for db_name in BED_DB_NAMES:
        try:
            db = get_client()[db_name]
            cursor = db.VI_ICU_ZYYZ.find(
                {"hisPid": pid, "orderStatus": {"$in": ["V", "F", "E"]}},
                {"orderName": 1, "_id": 0},
            ).limit(200)
            order_names = [d.get("orderName", "") for d in cursor if d.get("orderName")]
            break
        except Exception:
            continue

    # 培养标本
    for db_name in BED_DB_NAMES:
        try:
            db = get_client()[db_name]
            cursor = db.VI_DC_YJJC.find(
                {"pid": pid, "item_name": {"$regex": "培养|culture", "$options": "i"}},
                {"specimen_name": 1, "_id": 0},
            ).limit(50)
            culture_specimens = [d.get("specimen_name", "") for d in cursor if d.get("specimen_name")]
            break
        except Exception:
            continue

    suggestions = suggest_infection_site(diagnosis_text, order_names, culture_specimens)
    return {"suggestions": suggestions}


class TriTubeConfirmPayload(BaseModel):
    pid: str
    suspect_type: str
    diagnosis_time: str = ""
    user_id: str
    notes: str = ""


@app.get("/api/tri-tube/warnings")
def tri_tube_warnings(period: str, icu_unit: str = "all", min_hours: int = 48, nocache: bool = False):
    """
    AI疑似三管感染预警，仅作医生确认线索，不计入正式指标分子。
    """
    ck = _cache_key("tri_tube_warnings", period, icu_unit, min_hours)
    cached = _cache_get(ck, nocache)
    if cached is not None:
        return cached

    import calendar
    year, month = period.split("-")
    start_date = f"{year}-{month}-01"
    end_day = calendar.monthrange(int(year), int(month))[1]
    end_date = f"{year}-{month}-{end_day:02d}"
    dept_codes = _resolve_dept_codes(icu_unit)
    items = get_tri_tube_suspected_warnings(dept_codes, start_date, end_date, min_hours=min_hours)
    result = {
        "count": len(items),
        "notice": "AI疑似预警，需医生确认；未确认前不计入ICU-16/ICU-17/CAUTI分子",
        "items": items,
    }
    _cache_set(ck, result)
    return result


@app.post("/api/tri-tube/warnings/confirm")
def tri_tube_warning_confirm(payload: TriTubeConfirmPayload):
    """
    医生确认疑似预警后，写入正式感染诊断记录。
    """
    if not payload.user_id:
        return {"ok": False, "error": "user_id required"}
    diagnosis_time = datetime.fromisoformat(payload.diagnosis_time) if payload.diagnosis_time else datetime.now()
    try:
        doc = confirm_tri_tube_warning(
            pid=payload.pid,
            suspect_type=payload.suspect_type,
            diagnosis_time=diagnosis_time,
            user_id=payload.user_id,
            notes=payload.notes,
        )
        return {"ok": True, "diagnosis": doc}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/departments")
def get_departments():
    """
    从 MongoDB department 表读取所有科室列表。
    用于前端下拉菜单和 URL deptCode 参数解析。
    """
    departments = []
    for db_name in BED_DB_NAMES:
        try:
            db = get_client()[db_name]
            docs = list(db.department.find({}, {
                "code": 1, "name": 1, "shortName": 1, "hospitalName": 1, "_id": 0
            }))
            if docs:
                departments = docs
                break
        except Exception:
            continue

    # 如果 MongoDB 无数据，回退到静态映射
    if not departments:
        departments = [
            {"code": k, "name": v, "shortName": v}
            for k, v in DEPT_MAP.items()
        ]

    return departments


# ---- 预聚合汇总表接口 ----

@app.get("/api/summary/list")
def summary_list(dept: str = "all", start_period: str = "", end_period: str = ""):
    """
    从 icu_monthly_summary 汇总表读取指标数据（毫秒级）。
    """
    dept_codes = _resolve_dept_codes(dept)
    periods = []
    if start_period and end_period:
        sy, sm = start_period.split("-")
        ey, em = end_period.split("-")
        y, m = int(sy), int(sm)
        while (y < int(ey)) or (y == int(ey) and m <= int(em)):
            periods.append(f"{y}-{m:02d}")
            m += 1
            if m > 12: m, y = 1, y + 1
    elif start_period:
        periods = [start_period]
    else:
        periods = [f"{datetime.now().year}-{datetime.now().month:02d}"]

    rows = summary_module.read_summary(dept_codes, periods)
    return rows


@app.post("/api/admin/rebuild-summary")
def admin_rebuild(dept: str = "all", start_period: str = "", end_period: str = "",
                  indicators: str = ""):
    """
    手动触发预聚合。可指定范围。
    POST /api/admin/rebuild-summary?dept=all&start_period=2024-01&end_period=2025-12
    """
    dept_codes = _resolve_dept_codes(dept)
    periods = []
    if start_period and end_period:
        sy, sm = start_period.split("-")
        ey, em = end_period.split("-")
        y, m = int(sy), int(sm)
        while (y < int(ey)) or (y == int(ey) and m <= int(em)):
            periods.append(f"{y}-{m:02d}")
            m += 1
            if m > 12: m, y = 1, y + 1
    else:
        # 默认最近 13 个月
        now = datetime.now()
        for i in range(13):
            d = now - timedelta(days=30 * i)
            periods.append(f"{d.year}-{d.month:02d}")
        periods = list(set(periods))
        periods.sort()

    ind_list = [i.strip() for i in indicators.split(",") if i.strip()] if indicators else None
    if ind_list:
        ind_list = [i for i in ind_list if i in summary_module.INDICATOR_COMPUTERS]

    stats = summary_module.rebuild_summary(dept_codes, periods, ind_list)
    stats["dept_codes"] = dept_codes
    stats["periods"] = periods
    # #fix: 同步失效明细缓存 —— 本端点此前只重建预聚合、不碰明细缓存，
    # 口径变更后出现 list(新值) vs detail(旧缓存) 不一致（ICU-06 313 vs 319）。
    # 删除而非重算：明细在下次下钻时按当前代码惰性重算，保持端点快速返回。
    try:
        coll = _get_detail_cache_collection()
        if coll is not None:
            deleted = coll.delete_many({
                "dept_code": _dept_cache_key(dept_codes),
                "period": {"$in": periods},
            }).deleted_count
            stats["detail_cache_invalidated"] = deleted
    except Exception as e:
        logger.warning("Failed to invalidate detail cache after admin rebuild: %s", e)
    return stats


@app.get("/api/admin/rebuild-status")
def admin_rebuild_status():
    """查看最近一次预聚合状态（含调度器状态）。"""
    from scheduler import scheduler as sched_mgr
    sched_status = sched_mgr.get_latest_status()
    last_success = sched_mgr.get_last_success_at()

    for db_name in BED_DB_NAMES:
        try:
            db = get_client()[db_name]
            coll = db["icu_monthly_summary"]
            total = coll.count_documents({})
            latest = list(coll.find({}, {"period": 1, "updated_at": 1, "_id": 0}).sort("updated_at", -1).limit(1))
            periods = coll.distinct("period")
            depts = coll.distinct("dept_code")
            indicators = coll.distinct("indicator")
            return {
                "total_docs": total,
                "periods": sorted(periods),
                "depts": depts,
                "indicators": sorted(indicators),
                "latest_update": latest[0] if latest else None,
                # 调度器状态
                "scheduler": sched_status,
                "last_success_at": last_success,
                "rebuilding": sched_status.get("rebuilding", False),
            }
        except Exception:
            continue
    return {"error": "Database not available", "scheduler": sched_status, "rebuilding": sched_status.get("rebuilding", False)}


@app.post("/api/admin/trigger-rebuild")
def admin_trigger_rebuild(
    periods: str = "",
    force: bool = False,
    dept: str = "all",
):
    """
    手动触发预聚合重建。

    POST /api/admin/trigger-rebuild?periods=2026-06,2026-07&force=true
    POST /api/admin/trigger-rebuild  (自动补缺失月份)
    """
    from scheduler import scheduler as sched_mgr
    import threading as _threading

    dept_codes = _resolve_dept_codes(dept)

    if periods:
        period_list = [p.strip() for p in periods.split(",") if p.strip()]
    else:
        period_list = summary_module._natural_months_back(13)
        if not force:
            missing = summary_module.find_missing_periods(dept_codes, period_list)
            if not missing:
                return {"status": "no_missing", "message": "所有月份数据已完整"}
            period_list = missing

    task_id = sched_mgr.record_start("manual_rebuild", period_list,
                                      extra_info={"trigger": "manual", "force": force, "dept": dept})

    def _run():
        try:
            stats = summary_module.rebuild_summary(dept_codes, period_list)
            sched_mgr.record_finish(task_id, status="completed", stats=stats)
            # 补算明细缓存
            historical = [p for p in period_list if _is_historical_period(p)]
            if historical:
                try:
                    rebuild_detail_cache(dept_codes, historical)
                except Exception as e:
                    logger.warning("Detail cache rebuild failed during manual rebuild: %s", e)
        except Exception as e:
            logger.error("Manual rebuild failed: %s\n%s", e, traceback.format_exc())
            sched_mgr.record_finish(task_id, status="failed", error=str(e))

    _threading.Thread(target=_run, daemon=True).start()
    return {"task_id": task_id, "status": "started", "periods": period_list, "force": force}


# ---- 手动刷新端点（异步） ----

_refresh_tasks = {}       # task_id → {status, dept_codes, period, stats?, error?}
_refresh_lock = threading.Lock()
_REFRESH_TASK_TTL = 3600  # 任务状态保留 1 小时，整年刷新完成后仍可查看结果


@app.post("/api/refresh")
def trigger_refresh(dept_code: str = "all", year: int = None, month: int = None,
                    start_period: str = "", end_period: str = "",
                    indicators: str = ""):
    """
    手动触发当前科室+当前月份的预聚合重建（异步）。

    POST /api/refresh?dept_code=JJL000282&year=2026&month=6

    内部使用 _resolve_dept_codes 解析（与 indicator_list 同源），
    保证 summary 写入的 dept_code key 与前端查询完全一致。

    返回 task_id，前端轮询 GET /api/refresh/{task_id} 获取状态。
    """
    # 参数校验与默认值
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    dept_codes = _resolve_dept_codes(dept_code)
    if not start_period:
        start_period = f"{year}-{month:02d}"
    if not end_period:
        end_period = start_period

    sy, sm = start_period.split("-")
    ey, em = end_period.split("-")
    periods = []
    y, m = int(sy), int(sm)
    while (y < int(ey)) or (y == int(ey) and m <= int(em)):
        periods.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1

    indicator_list = [i.strip() for i in indicators.split(",") if i.strip()] if indicators else None
    if indicator_list:
        indicator_list = [i for i in indicator_list if i in summary_module.INDICATOR_COMPUTERS]
    else:
        indicator_list = list(summary_module.INDICATOR_COMPUTERS.keys())
    historical_periods = [p for p in periods if _is_historical_period(p)]
    summary_total = len(periods) * len(indicator_list)
    detail_total = len(historical_periods) * len(indicator_list) * 2

    task_key = "|".join([
        dept_code,
        ",".join(dept_codes),
        ",".join(periods),
        ",".join(indicator_list),
    ])
    task_id = uuid.uuid4().hex[:12]
    with _refresh_lock:
        for existing_id, existing in _refresh_tasks.items():
            if existing.get("status") == "running" and existing.get("task_key") == task_key:
                return {"task_id": existing_id, **existing, "reused": True}
        _refresh_tasks[task_id] = {
            "status": "running",
            "dept_codes": dept_codes,
            "periods": periods,
            "dept_code_param": dept_code,
            "task_key": task_key,
            "total": summary_total + detail_total,
            "summary_total": summary_total,
            "detail_total": detail_total,
            "done": 0,
            "started": 0,
            "success": 0,
            "failed": 0,
            "phase": "summary",
            "current_period": periods[0] if periods else "",
            "current_indicator": indicator_list[0] if indicator_list else "",
            "started_at": datetime.utcnow().isoformat(),
        }

    def _run_rebuild():
        try:
            progress_base = {"done": 0, "success": 0, "failed": 0}
            def _progress(total, success, failed, current_period="", current_indicator="", event="progress"):
                progress_base["done"] = total
                progress_base["success"] = success
                progress_base["failed"] = failed
                with _refresh_lock:
                    if task_id in _refresh_tasks:
                        payload = {
                            "done": total,
                            "success": success,
                            "failed": failed,
                        }
                        if current_period:
                            payload["current_period"] = current_period
                        if current_indicator:
                            payload["current_indicator"] = current_indicator
                        if event == "started":
                            payload["started"] = min(
                                _refresh_tasks[task_id].get("started", 0) + 1,
                                _refresh_tasks[task_id].get("total", 0),
                            )
                        payload["last_event"] = event
                        payload["updated_at"] = datetime.utcnow().isoformat()
                        _refresh_tasks[task_id].update(payload)

            def _detail_progress(total, success, failed, current_period="", current_indicator="", part="", event="progress"):
                with _refresh_lock:
                    if task_id in _refresh_tasks:
                        done = progress_base["done"] + total
                        payload = {
                            "phase": "detail",
                            "done": done,
                            "success": progress_base["success"] + success,
                            "failed": progress_base["failed"] + failed,
                            "current_period": current_period,
                            "current_indicator": current_indicator,
                            "current_part": part,
                            "last_event": event,
                            "updated_at": datetime.utcnow().isoformat(),
                        }
                        if event == "started":
                            payload["started"] = min(
                                _refresh_tasks[task_id].get("started", 0) + 1,
                                _refresh_tasks[task_id].get("total", 0),
                            )
                        _refresh_tasks[task_id].update(payload)

            stats = summary_module.rebuild_summary(
                dept_codes,
                periods,
                indicator_list,
                progress_callback=_progress,
            )
            detail_stats = rebuild_detail_cache(
                dept_codes,
                periods,
                indicator_list,
                icu_unit=dept_code,
                progress_callback=_detail_progress,
            )
            with _refresh_lock:
                _refresh_tasks[task_id].update({
                    "status": "completed",
                    "phase": "completed",
                    "done": summary_total + detail_stats["total"],
                    "success": stats["success"] + detail_stats["success"],
                    "failed": stats["failed"] + detail_stats["failed"],
                    "stats": {
                        "total": summary_total + detail_stats["total"],
                        "success": stats["success"] + detail_stats["success"],
                        "failed": stats["failed"] + detail_stats["failed"],
                        "summary": stats,
                        "detail": detail_stats,
                    },
                    "completed_at": datetime.utcnow().isoformat(),
                })
        except Exception as e:
            with _refresh_lock:
                _refresh_tasks[task_id].update({
                    "status": "error",
                    "error": str(e)[:500],
                    "completed_at": datetime.utcnow().isoformat(),
                })

    t = threading.Thread(target=_run_rebuild, daemon=True)
    t.start()

    # 清理过期任务（超过 TTL 的已完成/错误任务）
    with _refresh_lock:
        expired = [
            tid for tid, tinfo in _refresh_tasks.items()
            if tinfo["status"] in ("completed", "error")
            and (datetime.utcnow() - datetime.fromisoformat(tinfo.get("completed_at", tinfo["started_at"]))).total_seconds() > _REFRESH_TASK_TTL
        ]
        for tid in expired:
            del _refresh_tasks[tid]

    return {
        "task_id": task_id,
        "status": "running",
        "dept_codes": dept_codes,
        "periods": periods,
        "indicator_count": len(indicator_list),
        "total": summary_total + detail_total,
        "summary_total": summary_total,
        "detail_total": detail_total,
        "started": 0,
    }


@app.get("/api/refresh/{task_id}")
def get_refresh_status(task_id: str):
    """
    查询刷新任务状态。

    GET /api/refresh/abc123def456
    → {task_id, status: "running"|"completed"|"error", stats?, error?}
    """
    with _refresh_lock:
        task = _refresh_tasks.get(task_id)
    if not task:
        return {"task_id": task_id, "status": "not_found"}
    return {"task_id": task_id, **task}


# ============================================================
# ICU-06 AI 决策复核接口
# ============================================================

@app.get("/api/ai-decisions")
def list_ai_decisions(dept: str = "all", period_start: str = "",
                      period_end: str = "", min_confidence: float = None,
                      limit: int = 500):
    """
    查询 AI 判定记录，供主任复核。
    可选筛选：科室、时间范围、置信度阈值。

    GET /api/ai-decisions?period_start=2026-06&min_confidence=0.6
    返回: [{hisPid, task, purpose, confidence, reason, decided_by, created_at}]
    """
    return get_all_ai_decisions(
        dept_codes=None,
        period_start=period_start or None,
        period_end=period_end or None,
        min_confidence=min_confidence,
        limit=limit,
    )


@app.post("/api/ai-decisions/override")
def override_ai(payload: dict):
    """
    人工推翻 AI 判定。

    POST /api/ai-decisions/override
    Body: {
      "hisPid": "ZY0100000001",
      "purpose": "治疗性",
      "reason": "患者有明确肺部感染影像学证据",
      "overridden_by": "张主任"
    }

    写回 ai_decision_cache 表，标记 by='manual_override'，置信度=1.0。
    下次同 hisPid 查询时直接使用人工判定，不再调 AI。
    """
    hispid = payload.get("hisPid", "").strip()
    purpose = payload.get("purpose", "").strip()
    reason = payload.get("reason", "").strip()
    overridden_by = payload.get("overridden_by", "主任").strip()

    if not hispid:
        return {"success": False, "error": "hisPid is required"}
    if purpose not in ("治疗性", "预防性"):
        return {"success": False, "error": "purpose must be 治疗性 or 预防性"}
    if not reason:
        return {"success": False, "error": "reason is required"}

    return override_ai_decision(hispid, purpose, reason, overridden_by)


def _mount_frontend():
    dist_dir = _resource_base_dir() / "frontend_dist"
    index_file = dist_dir / "index.html"
    if not index_file.exists():
        return

    @app.get("/", include_in_schema=False)
    def serve_index():
        return FileResponse(index_file)

    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="frontend")


_mount_frontend()


def main():
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT") or os.getenv("BACKEND_PORT") or "8091")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
