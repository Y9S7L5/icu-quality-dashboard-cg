# db.py - MongoDB 连接 & ICU-01 分母逻辑（从 .env 读取配置）
# 打包兼容：注解延迟求值，避免 dict|None / list[str] 在 Python<3.10 导入即崩
from __future__ import annotations

import os
from pathlib import Path
from pymongo import MongoClient
from datetime import datetime, timedelta, timezone
from typing import Optional
from collections import defaultdict
import re
from dotenv import load_dotenv
import logging

# 时区统一：存储帧=真 UTC；边界北京→UTC(−8)，展示 UTC→北京(+8)
from tzutil import as_utc, to_bj_naive, utc_day_start, utc_day_end_wide, fmt_bj  # noqa: F401

logger = logging.getLogger("db")
if not logger.handlers:
    _diag_handler = logging.StreamHandler()
    _diag_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_diag_handler)
logger.setLevel(logging.INFO)

# 医院变体开关（config/bundle_rules.py: BUNDLE_HOSPITAL_VARIANT）
# cg 变体: 整库路由到 SmartCare_cg / DataCenter_cg + bundle 提取口径切换
from config.bundle_rules import (  # noqa: E402
    BUNDLE_HOSPITAL_VARIANT,
    CG_ORDER_KEYWORDS,
    CG_CONFIRM_DIAG_KEYWORDS,
    septic_shock_confirmed as _septic_shock_confirmed,
)

# 加载 .env 文件。打包成二进制后，优先读取可执行文件同目录的 .env。
import sys

if getattr(sys, "frozen", False):
    env_candidates = [
        Path(sys.executable).resolve().parent / ".env",
        Path.cwd() / ".env",
    ]
else:
    env_candidates = [
        Path(__file__).parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
    ]
_env_loaded = False
for env_path in env_candidates:
    if env_path.exists():
        load_dotenv(env_path)
        _env_loaded = True
        break
if not _env_loaded:
    print("[db] WARNING: no .env file found in any candidate path", file=sys.stderr)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _aware(dt: datetime) -> datetime:
    """确保时区感知。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def build_exclusion_key(pid: str, event_time) -> str:
    # type: (str, object) -> str
    """生成稳定的排除键: pid|YYYYMMDDHHMM"""
    if isinstance(event_time, datetime):
        ts = event_time.strftime("%Y%m%d%H%M")
    else:
        ts = str(event_time)[:16].replace("-", "").replace(" ", "").replace(":", "")
    return pid + "|" + ts


# ============================================================
# G-1: 感染部位人工确认与入库
# ============================================================

INFECTION_SITE_COLLECTION = "icu_infection_site"


def _get_infection_site_collection():
    """#21: 固定使用 BED_DB_NAMES[0]，连不上直接抛异常。"""
    db_name = BED_DB_NAMES[0]
    try:
        db = get_client(db_name)[db_name]
        return db[INFECTION_SITE_COLLECTION]
    except Exception as e:
        raise RuntimeError(f"无法连接到主库 {db_name}: {e}")


def ensure_infection_site_indexes():
    """创建 icu_infection_site 集合索引。在 main.py on_startup 中调用。"""
    coll = _get_infection_site_collection()
    if coll is None:
        return
    try:
        # 唯一索引: (indicator_code, exclusion_key, revoked_at) 部分唯一
        coll.create_index(
            [("indicator_code", 1), ("exclusion_key", 1), ("revoked_at", 1)],
            unique=True, background=True,
            partialFilterExpression={"revoked_at": None},
        )
        coll.create_index([("period", 1), ("dept_code", 1)], background=True)
        coll.create_index([("pid", 1)], background=True)
        logger.info("[infection_site] indexes ensured")
    except Exception as e:
        logger.warning("[infection_site] ensure index failed: %s", e)


def suggest_infection_site(
    diagnosis_text: str,
    order_names: list,
    culture_specimens: list,
) -> list:
    """
    纯函数，不写库。按关键词表打分，返回 [{site, score, matched_keywords}] 倒序。
    全部为零 → 返回空数组。
    """
    from config.infection_sites import SITE_SUGGEST_KEYWORDS

    combined_text = " ".join([
        diagnosis_text or "",
        " ".join(order_names or []),
        " ".join(culture_specimens or []),
    ])

    results = []
    for site, keywords in SITE_SUGGEST_KEYWORDS.items():
        matched = [kw for kw in keywords if kw in combined_text]
        if matched:
            results.append({
                "site": site,
                "score": len(matched),
                "matched_keywords": matched,
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def get_infection_site(exclusion_key: str) -> dict | None:
    """查询未撤销的感染部位记录。"""
    coll = _get_infection_site_collection()
    if coll is None:
        return None
    return coll.find_one(
        {"indicator_code": "ICU-05", "exclusion_key": exclusion_key, "revoked_at": None},
        {"_id": 0},
    )


def get_infection_sites_bulk(exclusion_keys: list) -> dict:
    """#20: 批量查询感染部位记录，返回 {exclusion_key: doc} 字典。"""
    coll = _get_infection_site_collection()
    if coll is None:
        return {}
    if not exclusion_keys:
        return {}
    cursor = coll.find(
        {"indicator_code": "ICU-05", "exclusion_key": {"$in": exclusion_keys}, "revoked_at": None},
        {"_id": 0},
    )
    return {doc["exclusion_key"]: doc for doc in cursor}


def create_infection_site(doc: dict) -> dict:
    """
    创建感染部位记录。
    #18: 原子操作 — 先插新、再撤旧，失败时回滚。
    """
    from config.infection_sites import validate_site_code, validate_evidence_type

    coll = _get_infection_site_collection()
    if coll is None:
        raise RuntimeError("无法获取 icu_infection_site 集合")

    # 校验
    primary_site = doc.get("primary_site")
    if not validate_site_code(primary_site):
        raise ValueError(f"primary_site 不合法: {primary_site}")

    secondary_sites = doc.get("secondary_sites", [])
    for s in secondary_sites:
        if not validate_site_code(s):
            raise ValueError(f"secondary_site 不合法: {s}")
    if primary_site in secondary_sites:
        raise ValueError("primary_site 不得同时出现在 secondary_sites 里")

    evidence_type = doc.get("evidence_type")
    if not validate_evidence_type(evidence_type):
        raise ValueError(f"evidence_type 不合法: {evidence_type}")

    if not doc.get("exclusion_key"):
        raise ValueError("exclusion_key 不能为空")
    if not doc.get("confirmed_by"):
        raise ValueError("confirmed_by 不能为空")

    # 检查是否已存在未撤销记录
    existing = coll.find_one({
        "indicator_code": "ICU-05",
        "exclusion_key": doc["exclusion_key"],
        "revoked_at": None,
    })

    replaced = False
    new_id = None
    if existing:
        # #18: 先插新记录
        new_doc = {
            "indicator_code": "ICU-05",
            "exclusion_key": doc["exclusion_key"],
            "pid": doc.get("pid", ""),
            "disease_id": doc.get("disease_id"),
            "period": doc.get("period", ""),
            "dept_code": doc.get("dept_code", ""),
            "patient_name": doc.get("patient_name", ""),
            "primary_site": primary_site,
            "secondary_sites": secondary_sites,
            "evidence_type": evidence_type,
            "evidence_text": (doc.get("evidence_text") or "")[:500],
            "confirmed_by": doc["confirmed_by"],
            "confirmed_at": datetime.now(timezone.utc),
            "revoked_at": None,
            "revoked_by": None,
            "superseded_by": None,
            "source": doc.get("source", "manual"),
        }
        try:
            result = coll.insert_one(new_doc)
            new_id = result.inserted_id
        except Exception as e:
            # 唯一索引冲突 → 并发
            raise RuntimeError(f"并发插入冲突: {e}")

        # #18: 再撤销旧记录
        try:
            coll.update_one(
                {"_id": existing["_id"]},
                {"$set": {"revoked_at": datetime.now(timezone.utc),
                          "superseded_by": doc.get("confirmed_by", "system")}},
            )
        except Exception as e:
            # 撤销失败 → 回滚新记录
            if new_id:
                coll.delete_one({"_id": new_id})
            raise RuntimeError(f"撤销旧记录失败，已回滚: {e}")
        replaced = True

    # 插入新记录
    new_doc = {
        "indicator_code": "ICU-05",
        "exclusion_key": doc["exclusion_key"],
        "pid": doc.get("pid", ""),
        "disease_id": doc.get("disease_id"),
        "period": doc.get("period", ""),
        "dept_code": doc.get("dept_code", ""),
        "patient_name": doc.get("patient_name", ""),
        "primary_site": primary_site,
        "secondary_sites": secondary_sites,
        "evidence_type": evidence_type,
        "evidence_text": (doc.get("evidence_text") or "")[:500],
        "confirmed_by": doc["confirmed_by"],
        "confirmed_at": datetime.now(timezone.utc),
        "revoked_at": None,
        "revoked_by": None,
        "superseded_by": None,
        "source": doc.get("source", "manual"),
    }
    coll.insert_one(new_doc)
    return {"replaced": replaced}


def revoke_infection_site(record_id: str, revoked_by: str) -> dict:
    """撤销感染部位记录（只置 revoked_at，不物理删除）。"""
    from bson import ObjectId

    coll = _get_infection_site_collection()
    if coll is None:
        raise RuntimeError("无法获取 icu_infection_site 集合")

    existing = coll.find_one({"_id": ObjectId(record_id)})
    if not existing:
        raise ValueError("记录不存在")
    if existing.get("revoked_at") is not None:
        raise ValueError("记录已撤销")

    coll.update_one(
        {"_id": ObjectId(record_id)},
        {"$set": {"revoked_at": datetime.now(timezone.utc), "revoked_by": revoked_by}},
    )
    return {"revoked": True}


# ==== 统计窗口交集化 ====
# 缺陷根源:分母限定统计月,分子却放开整段住院期。
# 任何"当月是否发生过某事件"的查询,时间条件都必须来自本函数。
def _month_window(pat, start_dt, end_dt_wide):
    """返回患者 pat 在统计月内的有效观察窗口 [win_start, win_end]。

    win_start = max(月初 00:00:00, icuAdmissionTime)
    win_end   = min(月末 23:59:59, icuDischargeTime)  未出科取月末,绝不用 now()

    入参均为 UTC 帧瞬间（start_dt=北京月初00:00 的 UTC 值，
    end_dt_wide=北京月末23:59:59 的 UTC 值，见 tzutil）。

    若返回的 win_start > win_end,表示该患者本月不在科,调用方必须 continue。
    """
    month_start = start_dt
    month_end = end_dt_wide

    admit = pat.get("icuAdmissionTime")
    win_start = max(month_start, admit) if isinstance(admit, datetime) else month_start

    discharge = pat.get("icuDischargeTime")
    # 关键:未出科 -> 取月末,不是 datetime.now()
    win_end = min(month_end, discharge) if isinstance(discharge, datetime) else month_end

    return win_start, win_end


def _stay_window(pat, end_dt_wide):
    """返回该次 ICU 住院（在科记录）自身的观察窗口 [admit, min(discharge, 月末)+24h]。

    与 _month_window 的区别：起点取**入科时间本身**（可早于月初）——
    「原有患者」入科时（上个月）做的首次 APACHEⅡ 评分 / 评估，
    属于本次住院期间，分子判定必须能看到（#fix 原有患者满足分子却漏计）。

    终点含出科后 24h 宽限：出科操作触发的评分/评估补录常晚于出科瞬间
    （实证：陈杰新出科后 8 秒、游绍禄出科后 9.7h 补录 APACHEⅡ，此前被上界滤掉）。
    文档 pid 本身即该在科记录 _id，归属无歧义，宽限只是容纳晚录入。

    入参均为 UTC 帧瞬间。返回 None 表示该次住院与统计期无交集（不应出现在分母队列）。
    """
    admit = pat.get("icuAdmissionTime")
    win_start = admit if isinstance(admit, datetime) else None
    discharge = pat.get("icuDischargeTime")
    if isinstance(discharge, datetime):
        win_end = min(discharge, end_dt_wide) + timedelta(hours=24)
    else:
        win_end = end_dt_wide
    if win_start is not None and win_start > win_end:
        return None
    return win_start, win_end


# ============================================================
# 数据库连接配置
# ============================================================
class DBConfig:
    def __init__(self, prefix: str):
        self.host = _env(f"{prefix}_DB_HOST", "127.0.0.1")
        self.port = int(_env(f"{prefix}_DB_PORT", "27017"))
        self.user = _env(f"{prefix}_DB_USER")
        self.password = _env(f"{prefix}_DB_PASSWORD")
        self.auth_db = _env(f"{prefix}_DB_AUTH")
        # 业务库名：优先 *_DB_NAME；未配置时回退 *_DB_AUTH（本项目历来把 AUTH 当库名用）
        self.db_name = _env(f"{prefix}_DB_NAME") or self.auth_db or ""


# SmartCare（ICU 临床业务库）
SMARTCARE_CFG = DBConfig("SMARTCARE")
# DataCenter（数据中心 / 指标汇总库）
DATACENTER_CFG = DBConfig("DATACENTER")


def _variant_db_names() -> tuple[str, str]:
    """当前实际使用的 (SmartCare库名, DataCenter库名)。

    解析顺序（改 .env 后重启后端生效）:
      1. SMARTCARE_DB_NAME / DATACENTER_DB_NAME
      2. SMARTCARE_DB_AUTH / DATACENTER_DB_AUTH（与默认库同名时可省略 NAME）
      3. 按 BUNDLE_HOSPITAL_VARIANT 回退：default → SmartCare/DataCenter，cg → *_cg
    """
    sc_default = "SmartCare_cg" if BUNDLE_HOSPITAL_VARIANT == "cg" else "SmartCare"
    dc_default = "DataCenter_cg" if BUNDLE_HOSPITAL_VARIANT == "cg" else "DataCenter"
    sc_name = SMARTCARE_CFG.db_name or sc_default
    dc_name = DATACENTER_CFG.db_name or dc_default
    return sc_name, dc_name


# bedRecord / configBed / patient 所在的数据库（来自 .env，不再被 cg 开关硬编码覆盖）
_bed_sc_name, _ = _variant_db_names()
BED_DB_NAMES = [_bed_sc_name]
if not BED_DB_NAMES or not BED_DB_NAMES[0]:
    raise RuntimeError(
        "BED_DB_NAMES is empty — set SMARTCARE_DB_NAME or SMARTCARE_DB_AUTH in .env"
    )

def iter_bed_dbs():
    """Yield (db_name, db) for each valid bed database, skipping empty names.
    Logs a warning on connection/query errors but does not propagate them."""
    for db_name in BED_DB_NAMES:
        if not db_name:
            continue
        try:
            db = get_client(db_name)[db_name]
            yield db_name, db
        except Exception as exc:
            logger.warning("iter_bed_dbs: skipping %s — %s: %s",
                           db_name, type(exc).__name__, exc)
            continue


def assert_db_ready():
    """Validate each BED_DB_NAMES entry: non-empty, reachable, and patient collection exists.
    Returns list of actually-available db names."""
    available = []
    for db_name in BED_DB_NAMES:
        if not db_name:
            logger.warning("assert_db_ready: skipping empty db_name")
            continue
        try:
            db = get_client(db_name)[db_name]
            db.command("ping")
            coll_names = db.list_collection_names()
            count = db.patient.count_documents({}, limit=1) if "patient" in coll_names else 0
            logger.info("assert_db_ready: %s OK (patient docs: %s)", db_name, count)
            available.append(db_name)
        except Exception as exc:
            logger.warning("assert_db_ready: %s FAILED — %s: %s",
                           db_name, type(exc).__name__, exc)
    if not available:
        raise RuntimeError(
            "assert_db_ready: none of the configured BED_DB_NAMES are reachable. "
            "Check SMARTCARE_DB_NAME / SMARTCARE_DB_AUTH and network in .env."
        )
    logger.info("assert_db_ready: available DBs = %s", available)
    return available


# ============================================================
# 连接缓存
# ============================================================
_clients: dict[str, MongoClient] = {}


def _make_client(cfg: DBConfig, db_name: str) -> MongoClient:
    """
    创建 MongoDB 连接。
    优先用 .env 配置的账号密码认证；认证失败时自动回退到无认证连接。
    """
    if cfg.user:
        try:
            client = MongoClient(
                host=cfg.host,
                port=cfg.port,
                username=cfg.user,
                password=cfg.password,
                authSource=cfg.auth_db,
                serverSelectionTimeoutMS=3000,
            )
            # 验证认证是否成功
            client[db_name].command("ping")
            return client
        except Exception:
            pass  # 认证失败 → 回退无认证连接

    # 无认证回退
    return MongoClient(
        host=cfg.host,
        port=cfg.port,
        serverSelectionTimeoutMS=5000,
    )


class _VariantClient:
    """按 .env 库名重定向 MongoClient 薄代理。

    逻辑名 client["SmartCare"] / client["DataCenter"] 会落到 .env 配置的实际库名
    （*_DB_NAME 或 *_DB_AUTH，未配置时按变体回退 SmartCare/DataCenter 或 *_cg）。
    仅重载 __getitem__，其余属性全部透传给真实 client。
    """

    __slots__ = ("_real",)

    def __init__(self, real: MongoClient):
        self._real = real

    def __getitem__(self, name: str):
        sc_name, dc_name = _variant_db_names()
        if name == "SmartCare":
            name = sc_name
        elif name == "DataCenter":
            name = dc_name
        return self._real[name]

    def __getattr__(self, item):
        return getattr(self._real, item)


def get_client(db_name: str = "SmartCare") -> MongoClient:
    """
    懒加载 MongoDB 连接。
    优先匹配 DataCenter / SmartCare 配置，按 db_name 归属选择合适的凭证。
    返回带医院变体重定向的代理（逻辑名 SmartCare/DataCenter → .env 实际库名）。
    """
    if db_name in _clients:
        return _clients[db_name]

    # 根据库名猜测所属配置
    sc_name, dc_name = _variant_db_names()
    if db_name.startswith("SmartCare") or db_name in ("SmartCare", sc_name):
        cfg = SMARTCARE_CFG
    else:
        cfg = DATACENTER_CFG

    client = _VariantClient(_make_client(cfg, db_name))
    _clients[db_name] = client
    return client


def get_smartcare_client() -> MongoClient:
    """获取 SmartCare 库的连接（用于 bedRecord/configBed 查询）"""
    return get_client(BED_DB_NAMES[0])


def get_datacenter_client() -> MongoClient:
    """获取 DataCenter 库的连接"""
    _, dc_name = _variant_db_names()
    return get_client(dc_name)


def get_datacenter_db():
    """获取 .env 指定的 DataCenter 数据库句柄。"""
    _, db_name = _variant_db_names()
    return get_datacenter_client()[db_name]


def _keyword_regex(keywords: list[str]) -> str:
    variants = []
    seen = set()
    for kw in keywords:
        if kw and kw not in seen:
            seen.add(kw)
            variants.append(re.escape(kw))
    return "|".join(variants)


# 不同院区/接口的医嘱状态编码不完全一致：
# - 旧接口常见 "3"
# - 换医院时把对方 HIS 的状态枚举配到 .env 的 EXECUTED_ORDER_STATUSES
#   （逗号分隔多个值，中英文逗号都行），重启后端生效；取消/作废类不纳入。
EXECUTED_ORDER_STATUSES = [s.strip() for s in _env(
    "EXECUTED_ORDER_STATUSES", "已执行,已审核,停止,3"
).replace("，", ",").split(",") if s.strip()] or ["已执行", "已审核", "停止", "3"]
# 不同院区 yaoType 编码不一致：标准医院检验类="检验"，部分医院用编码（如 "C"）。
# 可在 .env 配 LAB_ORDER_TYPES（逗号分隔多个值），影响 ICU-06 病原学送检。
LAB_ORDER_TYPES = [s.strip() for s in _env(
    "LAB_ORDER_TYPES", "检验"
).replace("，", ",").split(",") if s.strip()] or ["检验"]

# ICU-11 SMR：首次 APACHE II 评分的入科后窗口小时数（.env 可配，默认 24）。
# 院端评估常晚于入科 24h 录入时可放宽（如 48），重启后端生效；
# 改动后需重建 ICU-11 预聚合与明细缓存（POST /api/admin/rebuild-summary）。
try:
    APACHE_WINDOW_HOURS = float(_env("APACHE_WINDOW_HOURS", "24"))
    if APACHE_WINDOW_HOURS <= 0:
        raise ValueError("APACHE_WINDOW_HOURS must be > 0")
except ValueError:
    logger.warning("Invalid APACHE_WINDOW_HOURS=%r in .env, fallback to 24",
                   _env("APACHE_WINDOW_HOURS", ""))
    APACHE_WINDOW_HOURS = 24.0


# ============================================================
# ICU-01 分母查询
# ============================================================

def get_open_bed_count(dept_code: str, start_date: str, end_date: str) -> int:
    """
    ICU-02/03（医师/护士床位比）分母 —— 统计期末实际开放床位数。

    优先级：
      1. bedRecord 集合：deptCode（支持逗号分隔多科室）+ time 生效规则，
         取统计期末之前最近一条记录的 bedNum（例：10 月查询 → 10-15 起的 17 张）
      2. configBed 集合：统计该 deptCode 下的床位数量（hisName 条数）

    参数：
      dept_code  - 科室编码，如 "JJL000282"
      start_date - 统计开始日期 "YYYY-MM-DD"
      end_date   - 统计结束日期 "YYYY-MM-DD"

    返回：开放床位数，无数据返回 0
    """
    try:
        end_dt = utc_day_end_wide(end_date)
    except ValueError:
        return 0

    for db_name, db in iter_bed_dbs():
        try:

            # ---- Priority 1: bedRecord ----
            # deptCode 可能是逗号分隔的多科室字段，如 "JJL000283,0801,JJL000282"
            # time 之前的记录在期末仍生效，故只设上界并取最新一条
            bed_record = db.bedRecord.find_one(
                {
                    "deptCode": {"$regex": dept_code},
                    "time": {"$lte": end_dt},
                },
                sort=[("time", -1)],
            )
            if bed_record and bed_record.get("bedNum"):
                return int(bed_record["bedNum"])

            # ---- Priority 2: configBed 兜底 ----
            # 每个 configBed 文档对应一张床位，hisName 是床位号
            config_count = db.configBed.count_documents({"deptCode": dept_code})
            if config_count > 0:
                return config_count

        except Exception as _exc:
            logger.warning("DB %s failed in get_open_bed_count: %s", db_name, _exc)
            continue

    return 0


def get_open_bed_days(dept_code: str, start_date: str, end_date: str) -> int:
    """
    ICU-01（ICU床位使用率）分母 —— 实际开放总床日数（逐日累计）。

    bedRecord 每条记录的 bedNum 从 time 起生效；统计期内逐日取
    "当日 23:59:59 前最近一条" 的 bedNum 累加。
    例：9 月及以前 16 张、10-15 起 17 张 → 10 月分母 = 14×16 + 17×17。

    无 bedRecord 时兜底 configBed 床位条数 × 统计天数。
    """
    try:
        start_day = datetime.fromisoformat(start_date).date()
        end_day = datetime.fromisoformat(end_date).date()
    except ValueError:
        return 0
    if end_day < start_day:
        return 0
    days = (end_day - start_day).days + 1

    for db_name, db in iter_bed_dbs():
        try:
            recs = [
                r for r in db.bedRecord.find(
                    {"deptCode": {"$regex": dept_code}},
                    {"time": 1, "bedNum": 1},
                ).sort("time", 1)
                if r.get("time") is not None and r.get("bedNum")
            ]
            if recs:
                total = 0
                ri = -1  # 当前生效记录下标（time 升序，前向扫描）
                day = start_day
                while day <= end_day:
                    day_end = utc_day_end_wide(day.strftime("%Y-%m-%d"))
                    while ri + 1 < len(recs) and recs[ri + 1]["time"] <= day_end:
                        ri += 1
                    if ri >= 0:
                        total += int(recs[ri]["bedNum"])
                    day += timedelta(days=1)
                return total

            # ---- 兜底：configBed ----
            config_count = db.configBed.count_documents({"deptCode": dept_code})
            if config_count > 0:
                return config_count * days

        except Exception as _exc:
            logger.warning("DB %s failed in get_open_bed_days: %s", db_name, _exc)
            continue

    return 0


def get_all_dept_open_beds(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    批量获取多个科室的开放床位数。
    返回 {dept_code: bed_count}
    """
    result = {}
    for dc in dept_codes:
        result[dc] = get_open_bed_count(dc, start_date, end_date)
    return result


# ============================================================
# ICU-01 分子：实际占用总床日数
# ============================================================

def calc_patient_bed_days(admit_dt, discharge_dt, start_date: str, end_date: str) -> int:
    """
    单患者在统计期内的占用床日（ICU-01 分子口径，2026-10 院方确认）：
      - 过 0 点计 1 天：患者在北京日 00:00 时仍在 ICU（入科 < 0点 ≤ 出科），该日计 1 床日
      - 当日入 ICU 当日出院（未跨 0 点）：住满 ≥ 6 小时计 1 床日
      - 住 ICU 总时长 < 6 小时：不计床日（即使跨了 0 点也不计）
      - 未出院患者计到统计期末

    汇总（get_occupied_bed_days）与下钻明细（main.query_detail）共用本函数，
    保证明细逐人床日之和恒等于列表分子。
    """
    try:
        start_day = datetime.fromisoformat(start_date).date()
        end_day = datetime.fromisoformat(end_date).date()
    except ValueError:
        return 0
    if end_day < start_day or not admit_dt:
        return 0

    # 统计期边界（UTC 瞬间帧：北京 start_day 00:00 ~ end_day+1 00:00）
    # 必须 as_utc 归一为 aware：下方与 as_utc(admit/discharge) 做比较/减法，
    # naive 混 aware 会抛 TypeError，分子会被吞成 0
    period_start = as_utc(utc_day_start(start_date))
    period_end_excl = period_start + timedelta(days=(end_day - start_day).days + 1)
    day_delta = timedelta(days=1)
    min_los = timedelta(hours=6)

    admit_u = as_utc(admit_dt)
    discharge_u = as_utc(discharge_dt) if discharge_dt else None

    # 已出院且总时长 < 6h：不计床日
    if discharge_u is not None and (discharge_u - admit_u) < min_los:
        return 0

    # 在科区间右端裁剪到统计期末；未出院计到期末
    hi = min(discharge_u, period_end_excl) if discharge_u else period_end_excl

    # 统计期起点之前已出科：不计。
    # hi == period_start（出科恰在期初 0 点）按口径「入科 < 0点 ≤ 出科」仍计 1 天，
    # 故只排除 hi < period_start
    if hi < period_start:
        return 0

    # 跨 0 点天数：m 满足 admit < period_start + m*24h <= hi，且 0 <= m <= end_day
    d_hi = (hi - period_start) // day_delta
    d_hi = min(d_hi, (end_day - start_day).days)  # 期末次日 0 点不计（hi 裁剪会落到该瞬间）
    d_lo = (admit_u - period_start) // day_delta  # 可为负（期前入科）
    first_m = max(0, d_lo + 1)
    crossed = d_hi - first_m + 1 if d_hi >= first_m else 0

    if crossed > 0:
        return crossed
    if discharge_u is not None:
        # 当日入当日出（≥6h）计 1 天，入科日须落在统计期内
        bj_day = to_bj_naive(admit_dt).date()
        if start_day <= bj_day <= end_day:
            return 1
    return 0


def get_occupied_bed_days(dept_code: str, start_date: str, end_date: str) -> int:
    """
    ICU-01（ICU床位使用率）分子 —— 实际占用总床日数。
    口径见 calc_patient_bed_days；本函数按科室汇总逐患者床日。
    """
    try:
        datetime.fromisoformat(start_date)
        datetime.fromisoformat(end_date)
    except ValueError:
        return 0

    total = 0

    for db_name, db in iter_bed_dbs():
        try:

            # 查询符合条件的患者（只取需要的字段）
            patients = list(db.patient.find(
                {
                    "deptCode": dept_code,
                    "status": {"$ne": "invalid"},
                    "icuAdmissionTime": {"$lte": utc_day_end_wide(end_date)},
                    "$or": [
                        {"icuDischargeTime": {"$gte": utc_day_start(start_date)}},
                        {"icuDischargeTime": None},
                        {"icuDischargeTime": {"$exists": False}},
                    ],
                },
                {"icuAdmissionTime": 1, "icuDischargeTime": 1},
            ))

            if not patients:
                continue  # 当前库无数据，尝试下一个

            for p in patients:
                total += calc_patient_bed_days(
                    p.get("icuAdmissionTime"),
                    p.get("icuDischargeTime"),
                    start_date, end_date,
                )

            break  # 有数据就不再查下一个库

        except Exception as _exc:
            logger.warning("DB %s failed in get_occupied_bed_days: %s", db_name, _exc)
            continue

    return total


# ============================================================
# ICU-02/03：医师/护士人数（从 account 表读取）
# ============================================================

# ICU-02/03 分子按职位过滤。注意：本院 professionsTable 中
# StandardizedDoctor→「医务部」、StandardizedNurse→「护理部」属行政职能，
# 不计入医师/护士床位比分子。
DOCTOR_PROFESSIONS = {
    "AttendingDoctor", "DeputyDirector", "Director", "DirectorAssistant",
    "Doctor", "Intern", "StudyDoctor",
}
NURSE_PROFESSIONS = {
    "Matron", "MatronAssistant", "Nurse", "NurseLeader",
    "PracticeNurse", "StudyNurse",
}

# 职位中文名内置兜底（professionsTable 不可用时使用；正常以库内表为准）
PROFESSION_CN = {
    "AttendingDoctor": "主治医师",
    "DeputyDirector": "副主任医师",
    "Director": "主任医师",
    "DirectorAssistant": "主任助理",
    "Doctor": "医师",
    "Intern": "实习生",
    "StandardizedDoctor": "规培医师",
    "StudyDoctor": "进修医师",
    "Matron": "护士长",
    "MatronAssistant": "护士长助理",
    "Nurse": "护士",
    "NurseLeader": "护理组长",
    "PracticeNurse": "实习护士",
    "StandardizedNurse": "规培护士",
    "StudyNurse": "进修护士",
    "MedicalRecordRoom": "病案室",
    "InformationDept": "信息科",
    "Admin": "管理员",
    "SystemAdmin": "系统管理员",
}

_profession_cn_cache = {"map": None, "ts": 0.0}


def get_profession_cn_map(force: bool = False) -> dict:
    """
    职位中文名映射：优先读库内 professionsTable 集合（医院可自行维护
    职位码→中文名，如 StandardizedDoctor→医务部），失败时回退内置
    PROFESSION_CN。进程内缓存 10 分钟。
    """
    import time as _time
    now = _time.time()
    if (not force and _profession_cn_cache["map"] is not None
            and now - _profession_cn_cache["ts"] < 600):
        return _profession_cn_cache["map"]
    result = dict(PROFESSION_CN)
    for db_name, db in iter_bed_dbs():
        try:
            for d in db.professionsTable.find(
                    {}, {"professions": 1, "professionName": 1, "_id": 0}):
                code, name = d.get("professions"), d.get("professionName")
                if code and name:
                    result[code] = name
            break
        except Exception as _exc:
            logger.warning("DB %s failed in get_profession_cn_map: %s", db_name, _exc)
            continue
    _profession_cn_cache["map"] = result
    _profession_cn_cache["ts"] = now
    return result


def get_staff_count(dept_code: str, role: str) -> int:
    """
    从 account 表统计某科室的医师或护士人数。

    role: 'doctor' → DOCTOR_PROFESSIONS（不含医务部/StandardizedDoctor）
          'nurse'  → NURSE_PROFESSIONS（不含护理部/StandardizedNurse）

    参数 dept_code 支持逗号分隔多科室字段的 regex 匹配。
    """
    professions = DOCTOR_PROFESSIONS if role == "doctor" else NURSE_PROFESSIONS

    for db_name, db in iter_bed_dbs():
        try:
            count = db.account.count_documents({
                "valid": "valid",
                "departmentCode": {"$regex": dept_code},
                "profession": {"$in": list(professions)},
            })
            if count > 0:
                return count
        except Exception as _exc:
            logger.warning("DB %s failed in get_staff_count: %s", db_name, _exc)
            continue
    return 0


# ============================================================
# ICU 分母共用查询（ICU-04-01 / ICU-07 等「同期在科患者」口径）
# ============================================================

def get_icu_denominator_stays(dept_codes: list, start_date: str, end_date: str) -> list:
    """
    统计期内该科室的在科记录（**按人次**，一个患者多次入科算多条）。

    条件：deptCode 属于本科室、status != invalid、
          icuAdmissionTime <= 期末 且 (icuDischargeTime >= 期初 或 未出院)。

    ICU-04-01、ICU-07 等分母共用此查询，保证分子分母来自同一队列。
    """
    from datetime import datetime as dt

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_of_day = utc_day_end_wide(end_date)

    for _db_name, db in iter_bed_dbs():
        try:
            stays = list(db.patient.find(
                {
                    "deptCode": {"$in": dept_codes},
                    "status": {"$ne": "invalid"},
                    "icuAdmissionTime": {"$lte": end_of_day},
                    "$or": [
                        {"icuDischargeTime": {"$gte": start_dt}},
                        {"icuDischargeTime": None},
                        {"icuDischargeTime": {"$exists": False}},
                    ],
                },
                {"_id": 1, "mrn": 1, "hisPid": 1, "patientId": 1, "name": 1,
                 "hisBed": 1, "icuAdmissionTime": 1, "icuDischargeTime": 1, "deptCode": 1},
            ))
        except Exception as _exc:
            logger.warning("DB %s failed in get_icu_denominator_stays: %s", _db_name, _exc)
            continue
        if stays:
            return stays
    return []


def person_key_of(row: dict) -> str:
    """
    患者唯一键：mrn（住院号）优先，依次退回 hisPid / pid / _id。

    同一患者当月多次入科时，各条记录的 mrn、hisPid 相同，据此归并为「一个人」。
    全部键都为空时返回带 id() 的哨兵值 —— 宁可不去重（退回人次），
    也绝不能把两个不同患者合并成一个。
    """
    for k in ("mrn", "hisPid", "pid", "_id"):
        v = row.get(k)
        if v:
            return str(v)
    return f"\x00unkeyed:{id(row)}"


def dedup_persons(rows: list, time_key: str = "icuAdmissionTime") -> list:
    """
    人数口径去重：同一患者多条记录只保留 time_key 最早的一条。

    ICU-04-01 / ICU-07 / ICU-09 / ICU-10 的 INDICATORS_CONFIG 定义分子分母都是
    「患者数」，而 SmartCare patient 表一行 = 一次 ICU 入住，直接 len() 得到的是
    人次（2026-09 科室3439 有 7 人各入科 2 次：145 人次 / 138 人）。
    分子分母都要过这里，才能保证两边都是「人数」。

    time_key: 决定保留哪一条（ICU-04-01 分子用 score_time、ICU-09/10 用 assess_time）。
    """
    best = {}
    for row in rows:
        key = person_key_of(row)
        cur = best.get(key)
        if cur is None:
            best[key] = row
            continue
        a, b = cur.get(time_key), row.get(time_key)
        # 有时间的优先于没时间的；两个都有则取更早的（对应「首次」评估）
        if b is not None and (a is None or b < a):
            best[key] = row
    out = list(best.values())
    # 按时间排序，保证明细输出稳定（num_pids 等来源是 set，不排会抖）
    out.sort(key=lambda r: str(r.get(time_key) or "9999-99-99 99:99:99"))
    return out


# ============================================================
# ICU-04-01：APACHEⅡ≥15 收治率
# ============================================================

def get_icu04_apache_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    返回 {den_count, num_count, num_patients, den_patients}

    分母：统计期内该科室在科记录（status != invalid，入出科时间与统计期有交集）
    分子：分母在科记录中，**本次住院期间**首次 apacheII 评分 total ≥ 15 的人次

    口径：人次（一次入科算一人次，不按 mrn 去重）。
    评分窗口 = 本次住院 [入科, 出科∧月末]（_stay_window）——
    「原有患者」上个月入科时做的首次评分同样计入（#fix 原有患者满足分子却漏计）。
    """
    from datetime import datetime as dt, timedelta
    from bson import ObjectId

    end_dt = utc_day_start(end_date)
    end_wide = utc_day_end_wide(end_date)

    result = {"den_count": 0, "num_count": 0, "num_patients": [], "den_patients": [],
              "score_map": {}}   # str(在科_id) → 本次住院首次 apacheII total（明细标 未评/已评）

    # 分母：在科记录（与 ICU-07 共用 get_icu_denominator_stays，保证同队列）；
    # 人次口径：一条在科记录 = 一人次，明细与计数同为一行一人次
    all_stays = get_icu_denominator_stays(dept_codes, start_date, end_date)
    den_ids = [str(p["_id"]) for p in all_stays]
    result["den_count"] = len(all_stays)
    result["den_patients"] = all_stays
    if not den_ids:
        return result

    pat_map = {str(p["_id"]): p for p in all_stays}
    # 每条在科记录自身的观察窗口 [入科, 出科∧月末]，分子按此窗口判定
    win_map = {}
    for pid_str, p in pat_map.items():
        w = _stay_window(p, end_wide)
        if w:
            win_map[pid_str] = w

    for db_name, db in iter_bed_dbs():
        try:
            # 分子：拉取分母队列全部 apacheII 评分（≤月末，不限月初），
            # Python 侧按每条在科记录的窗口取「本次住院首次」——
            # 上个月入科时打的首次评分也能命中
            cursor = db.score.find(
                {"pid": {"$in": den_ids},
                 "scoreType": "apacheII",
                 "valid": True,
                 "time": {"$lte": end_wide}},
                {"pid": 1, "total": 1, "time": 1},
            ).sort("time", 1)
            first_by_pid = {}
            for s in cursor:
                pid_str = s.get("pid", "")
                if pid_str in first_by_pid:
                    continue  # 该次住院已取到首次评分
                w = win_map.get(pid_str)
                if not w:
                    continue
                t = s.get("time")
                if not isinstance(t, dt) or t < w[0] or t > w[1]:
                    continue  # 不在本次住院窗口内（早于入科/晚于出科的脏数据）
                first_by_pid[pid_str] = s

            # 分母明细「未评/已评」标记：有本次住院窗口内有效评分即已评（与分值无关）
            result["score_map"] = {k: v.get("total") for k, v in first_by_pid.items()}

            num_rows = []
            for pid_str, s in first_by_pid.items():
                try:
                    total = float(s.get("total"))
                except (TypeError, ValueError):
                    continue
                if total < 15:
                    continue
                p = pat_map.get(pid_str)
                if p:
                    num_rows.append({
                        "_id": pid_str,
                        "mrn": p.get("mrn", "") or p.get("hisPid", ""),
                        "patientId": p.get("patientId", ""),
                        "name": p.get("name", ""),
                        "hisBed": p.get("hisBed", ""),
                        "score": s.get("total", 0),
                        "score_time": s.get("time"),
                    })

            # 人次口径：一行一在科记录，不按 mrn 去重
            result["num_count"] = len(num_rows)
            result["num_patients"] = num_rows
            break

        except Exception as _exc:
            logger.warning("DB %s failed in get_icu04_apache_data: %s", db_name, _exc)
            continue

    return result


# ============================================================
# APACHEⅡ 分段人数（上报表 6–10 行）
# ============================================================

def get_apache_score_bands(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    返回 {lt10, 10_15, 15_20, 20_25, ge25, scored, den_count,
         bands: {桶名: [患者明细]}, den_patients}

    队列/口径与 ICU-04-01 完全一致：
      分母 = get_icu_denominator_stays（人次口径，一条在科记录 = 一人次）。
    评分口径 = 分母在科记录中，**本次住院期间**首次 apacheII total
             （窗口 [入科, 出科∧月末]，不限统计月内，不设 total 阈值）。
    无评分的不进桶。

    分值边界（半开区间，与上报表截图 6–10 行一致）：
      lt10  = total < 10
      10_15 = 10 ≤ total < 15
      15_20 = 15 ≤ total < 20
      20_25 = 20 ≤ total < 25
      ge25  = total ≥ 25
    """
    from datetime import datetime as dt

    end_dt = utc_day_start(end_date)
    end_wide = utc_day_end_wide(end_date)

    result = {
        "lt10": 0, "10_15": 0, "15_20": 0, "20_25": 0, "ge25": 0,
        "scored": 0, "den_count": 0,
        "bands": {"lt10": [], "10_15": [], "15_20": [], "20_25": [], "ge25": []},
        "den_patients": [],
        "score_map": {},   # str(在科_id) → 首次 apacheII total（分母明细标 未评/已评）
    }

    # 分母：与 ICU-04-01 完全相同的在科队列（人次口径，不去重）
    all_stays = get_icu_denominator_stays(dept_codes, start_date, end_date)
    den_ids = [str(p["_id"]) for p in all_stays]
    result["den_count"] = len(all_stays)
    result["den_patients"] = all_stays
    if not den_ids:
        return result

    def _bucket(total):
        """按分值落桶；无法转数值的返回 None（不进桶）。"""
        try:
            t = float(total)
        except (TypeError, ValueError):
            return None
        if t < 10:
            return "lt10"
        if t < 15:
            return "10_15"
        if t < 20:
            return "15_20"
        if t < 25:
            return "20_25"
        return "ge25"

    pat_map = {str(p["_id"]): p for p in all_stays}
    win_map = {}
    for pid_str, p in pat_map.items():
        w = _stay_window(p, end_wide)
        if w:
            win_map[pid_str] = w

    for db_name, db in iter_bed_dbs():
        try:
            # 拉取分母队列全部 apacheII 评分（≤月末，不限月初），Python 侧
            # 按每条在科记录窗口取「本次住院首次」——原有患者上个月入科时
            # 打的首次评分同样进桶
            cursor = db.score.find(
                {"pid": {"$in": den_ids},
                 "scoreType": "apacheII",
                 "valid": True,
                 "time": {"$lte": end_wide}},
                {"pid": 1, "total": 1, "time": 1},
            ).sort("time", 1)
            first_by_pid = {}
            for s in cursor:
                pid_str = s.get("pid", "")
                if pid_str in first_by_pid:
                    continue
                w = win_map.get(pid_str)
                if not w:
                    continue
                t = s.get("time")
                if not isinstance(t, dt) or t < w[0] or t > w[1]:
                    continue
                first_by_pid[pid_str] = s

            # 分母明细「未评/已评」标记（含无法落桶的分值，只看有无评分）
            result["score_map"] = {k: v.get("total") for k, v in first_by_pid.items()}

            scored_rows = []
            for pid_str, s in first_by_pid.items():
                p = pat_map.get(pid_str)
                if not p:
                    continue
                scored_rows.append({
                    "_id": pid_str,
                    "mrn": p.get("mrn", "") or p.get("hisPid", ""),
                    "patientId": p.get("patientId", ""),
                    "name": p.get("name", ""),
                    "hisBed": p.get("hisBed", ""),
                    "score": s.get("total"),
                    "score_time": s.get("time"),
                })

            # 人次口径：一行一在科记录，不按 mrn 去重
            for p in scored_rows:
                b = _bucket(p.get("score"))
                if b is None:
                    continue
                p = dict(p)
                p["score"] = float(p["score"]) if p.get("score") is not None else None
                result["bands"][b].append(p)

            for b in ("lt10", "10_15", "15_20", "20_25", "ge25"):
                result[b] = len(result["bands"][b])
            result["scored"] = sum(result[b] for b in
                                   ("lt10", "10_15", "15_20", "20_25", "ge25"))
            break

        except Exception as _exc:
            logger.warning("DB %s failed in get_apache_score_bands: %s", db_name, _exc)
            continue

    return result


# ============================================================
# ICU-05：感染性休克 Bundle 完成率（1h/3h/6h）
# ============================================================

def get_bundle_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    返回 {total, h1_num, h3_num, h6_num, h1_patients, h3_patients, h6_patients, den_patients}
    分母 = diseaseDiagnosis 表中 diseaseType='脓毒性休克', 诊断时间在统计期内,
           且患者在该科室的 patient 表中(status != invalid)
    分子1h/3h/6h = infectionShockV2 中 bundle 达标
    """
    from datetime import datetime as dt

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)

    result = {"total": 0, "h1_num": 0, "h3_num": 0, "h6_num": 0,
              "h1_patients": [], "h3_patients": [], "h6_patients": [], "den_patients": []}

    for db_name, db in iter_bed_dbs():
        try:

            # 分母：diseaseDiagnosis 表（扩展关键词：脓毒性休克/脓毒症/脓毒血症/败血症）
            diagnoses = list(db.diseaseDiagnosis.find(
                {
                    "diseaseType": {"$regex": SEPSIS_DIAG_KEYWORDS, "$options": "i"},
                    "valid": {"$ne": False},
                    "diagnosisTime": {
                        "$gte": start_dt,
                        "$lte": utc_day_end_wide(end_date),
                    },
                },
                {"pid": 1, "patientName": 1, "mrn": 1, "diagnosisTime": 1},
            ))
            if not diagnoses:
                continue

            # 关联 patient 表，过滤 deptCode 和 status
            shock_pids = list(set(d["pid"] for d in diagnoses))
            # pid 是字符串，patient._id 是 ObjectId，需转换比较
            from bson import ObjectId
            obj_ids = []
            for pid in shock_pids:
                try:
                    obj_ids.append(ObjectId(pid))
                except Exception:
                    pass

            patients = list(db.patient.find(
                {
                    "_id": {"$in": obj_ids},
                    "deptCode": {"$in": dept_codes},
                    "status": {"$ne": "invalid"},
                },
                {"_id": 1, "mrn": 1, "hisPid": 1, "name": 1, "hisBed": 1},
            ))
            valid_pids = set(str(p["_id"]) for p in patients)
            pat_map = {str(p["_id"]): p for p in patients}

            # 筛出在该科室的有效诊断，建立 {diseaseId_string: diag} 映射
            diag_by_id = {}
            den_list = []
            for d in diagnoses:
                if d["pid"] in valid_pids:
                    diag_by_id[str(d["_id"])] = d
                    den_list.append(d)
            result["total"] = len(den_list)

            # AI 确认（附加字段，不阻断分母）
            try:
                from ai_analyzer import classify_septic_shock_with_ai, extract_sofa_qsofa
            except ImportError:
                classify_septic_shock_with_ai = None

            den_items = []
            for d in den_list:
                item = {"_id": d["pid"], "mrn": d.get("mrn", ""), "name": d.get("patientName", ""),
                        "hisBed": "", "diagnosisTime": d.get("diagnosisTime"),
                        "diseaseId": str(d["_id"]), "sc_pid": str(d["pid"]),
                        "diagnose": d.get("diseaseType", ""),
                        "t0": d.get("diagnosisTime"), "t0_source": "diagnosis_time"}
                p = pat_map.get(d["pid"])
                if p:
                    item["mrn"] = item["mrn"] or p.get("hisPid", "")
                    item["hisBed"] = p.get("hisBed", "")
                    item["hisPid"] = str(p.get("hisPid", ""))
                # AI 确认（before=诊断时间，回顾性提取 SOFA）
                if classify_septic_shock_with_ai and item.get("hisPid"):
                    try:
                        sofa = extract_sofa_qsofa(d["pid"], item["hisPid"],
                                                  before=d.get("diagnosisTime"))
                        verdict = classify_septic_shock_with_ai(
                            disease_id=str(d["_id"]),
                            his_pid=item["hisPid"], data=sofa)
                        item["ai_confirm"] = bool(verdict.get("confirm"))
                        item["ai_reason"] = verdict.get("reason", "")
                        item["low_confidence"] = bool(verdict.get("low_confidence"))
                        item["qc_t0"] = verdict.get("t0")
                        if item["qc_t0"]:
                            item["t0"] = item["qc_t0"]
                            item["t0_source"] = "qc_t0"
                        item["sofa_delta"] = verdict.get("sofa_delta")
                    except Exception:
                        item["ai_confirm"] = None
                        item["ai_reason"] = "AI 判定异常"
                den_items.append(item)
            result["den_patients"] = den_items

            if not den_list:
                continue

            # 分子：通过 diseaseId 关联 infectionShockV2
            diag_id_strings = list(diag_by_id.keys())
            shocks = list(db.infectionShockV2.find(
                {"diseaseId": {"$in": diag_id_strings}},
                {"diseaseId": 1, "group1H": 1, "group3H": 1, "group6H": 1},
            ))
            shock_map = {s["diseaseId"]: s for s in shocks}

            for did_str, s in shock_map.items():
                diag = diag_by_id.get(did_str)
                if not diag:
                    continue
                pid_str = diag["pid"]
                p = pat_map.get(pid_str)
                if not p:
                    continue
                info = {
                    "_id": pid_str,
                    "mrn": p.get("mrn", "") or p.get("hisPid", ""),
                    "name": p.get("name", ""),
                    "hisBed": p.get("hisBed", ""),
                }
                g1 = s.get("group1H", {}) or {}
                g3 = s.get("group3H", {}) or {}
                g6 = s.get("group6H", {}) or {}
                if g1.get("baStandard") or g1.get("finish"):
                    result["h1_num"] += 1
                    result["h1_patients"].append(info)
                if g3.get("baStandard") or g3.get("finish"):
                    result["h3_num"] += 1
                    result["h3_patients"].append(info)
                # 6h达标 = 路径1(6h全部完成) OR 路径2(休克纠正 AND 1h达标 AND 3h达标)
                h1_ok = g1.get("baStandard") or g1.get("finish")
                h3_ok = g3.get("baStandard") or g3.get("finish")
                h6_ok = g6.get("baStandard") or g6.get("finish")
                shock_corrected = g1.get("finish") or g3.get("finish")
                if h6_ok or (shock_corrected and h1_ok and h3_ok):
                    result["h6_num"] += 1
                    result["h6_patients"].append(info)

            break
        except Exception as _exc:
            logger.warning("DB %s failed in get_bundle_data: %s", db_name, _exc)
            continue

    return result


# ---- ICU-05 v3 诊断关键词（扩展版） ----
SEPSIS_DIAG_KEYWORDS = (
    "感染性休克|脓毒性休克|脓毒症休克|脓毒血症|败血症|脓毒症"
    "|septic shock|sepsis"
)


def resolve_bundle_dc_pid(pat: dict) -> str:
    """judge_bundle_v3_for_patient 用的 DC pid（VI_ICU_ZYYZ.pid = hisPid）。

    cg 变体: diseaseDiagnosis 源患者的 _id 是 SC ObjectId，查 VI_ICU_ZYYZ
    必须用 hisPid；vi_zybr 源患者的 _id 本身就是 hisPid，语义不变。
    default: 沿用现状 _id，行为完全不变。
    """
    if BUNDLE_HOSPITAL_VARIANT == "cg":
        return pat.get("hisPid") or pat.get("_id", "")
    return pat.get("_id", "")


def _apply_cg_confirm_time(den_patients: list) -> None:
    """cg 变体: 分母确诊时间取 patient.diagnosisHistoryList 中最早含
    脓毒血症/败血症/感染性休克 的 time。

    - 无命中的患者保留原 diagnosisTime（其候选身份来自 diseaseDiagnosis /
      VI_ICU_ZYBR 的诊断正则，不因确诊时间缺失被剔除）
    - t0 若原本就是确诊时间兜底（t0_source=diagnosis_time）则同步迁移
    """
    mrns = {str(p.get("mrn")) for p in den_patients if p.get("mrn")}
    if not mrns:
        return
    try:
        sc = get_client("SmartCare")["SmartCare"]
        earliest = {}
        for doc in sc.patient.find(
            {"mrn": {"$in": list(mrns)}},
            {"mrn": 1, "diagnosisHistoryList": 1},
        ):
            m = str(doc.get("mrn") or "")
            hits = [
                d.get("time")
                for d in (doc.get("diagnosisHistoryList") or [])
                if isinstance(d.get("time"), datetime)
                and any(k in str(d.get("diagnosis") or "")
                        for k in CG_CONFIRM_DIAG_KEYWORDS)
            ]
            if hits:
                earliest[m] = min(hits)
        for pat in den_patients:
            t = earliest.get(str(pat.get("mrn") or ""))
            if not t:
                continue
            old_dt = pat.get("diagnosisTime")
            pat["diagnosisTime"] = t
            pat["confirm_time_source"] = "diagnosisHistoryList"
            if (not pat.get("qc_t0")
                    and pat.get("t0") == old_dt
                    and pat.get("t0_source") == "diagnosis_time"):
                pat["t0"] = t
                pat["t0_source"] = "confirm_diagnosis_list"
    except Exception as _exc:
        logger.warning("cg confirm time resolution failed: %s", _exc)


def get_bundle_data_v2(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    Bundle 判定 V3 — 双集合查询 + 完整分母逻辑。

    分母来源:
      1. SmartCare diseaseDiagnosis (diseaseType 匹配)
      2. DataCenter VI_ICU_ZYBR (diagnose 管道符分隔，扩展关键词)
      3. SmartCare patient.clinicalDiagnosis / admissionDiagnosis (兜底)

    分母过滤:
      - 入院24h内进ICU (admitTime - inPatientTime <= 24h)
      - 入科类型在 [入院, 手术入院, 外院] 内（可选，数据允许时启用）

    T0 锚点:
      - 从 DC VI_ICU_ZYYZ 取该患者第一条医嘱 orderTime
      - 兜底：admitTime

    返回:
        {
            "total": int,
            "h1_num": int, "h3_num": int, "h6_num": int,
            "h1_patients": [...], "h3_patients": [...], "h6_patients": [...],
            "den_patients": [...],
        }
    """
    # cg 简化口径（2026-10）: 分母=诊断列表命中, 分母/分子全走 bundle_cg，
    # 短路本函数的候选收集 + v3 三道门链路；default 变体不经过此处。
    if BUNDLE_HOSPITAL_VARIANT == "cg":
        from scoring.bundle_cg import get_bundle_data_cg
        return get_bundle_data_cg(dept_codes, start_date, end_date)

    from datetime import datetime as dt, timedelta

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)

    result = {
        "total": 0, "h1_num": 0, "h3_num": 0, "h6_num": 0,
        "h1_patients": [], "h3_patients": [], "h6_patients": [],
        "den_patients": [],
    }

    # ---- 阶段 1: SmartCare diseaseDiagnosis (原有逻辑) ----
    result_sc = get_bundle_data(dept_codes, start_date, end_date)

    # 合并 SmartCare 结果
    # V2 owns the statistics.  Use the legacy query only as a source of
    # candidate events; importing its accumulated numerators mixes contracts.
    result["den_patients"].extend(result_sc["den_patients"])

    # ---- 阶段 2: DataCenter VI_ICU_ZYBR (扩展关键词 + 24h 过滤) ----
    try:
        dc = get_client("DataCenter")["DataCenter"]
    except Exception as _exc:
        logger.warning("DataCenter connection failed: %s", _exc)
        return result

    try:
        # 查询 VI_ICU_ZYBR：扩展关键词（脓毒血症/败血症/感染性休克/脓毒性休克等）
        dc_query = {
                "diagnose": {"$regex": SEPSIS_DIAG_KEYWORDS, "$options": "i"},
                "admitTime": {
                    "$gte": start_dt,
                    "$lte": utc_day_end_wide(end_date),
                },
            }
        if dept_codes:
            # #修复: VI_ICU_ZYBR.deptCode 是字符串类型，需转为 str 匹配
            dc_query["deptCode"] = {"$in": [str(c) for c in dept_codes]}
        dc_diagnoses = list(dc.VI_ICU_ZYBR.find(
            dc_query,
            {
                "pid": 1,
                "name": 1,
                "mrn": 1,
                "admitTime": 1,
                "inPatientTime": 1,
                "diagnose": 1,
                "bed": 1,
                "deptTrue": 1,
                "deptCode": 1,
            },
        ))

        if dc_diagnoses:
            # 去重: 已在 SmartCare 中出现的患者不再重复计入
            sc_pids = {p.get("_id") or p.get("pid") for p in result_sc["den_patients"]}
            sc_mrns = {p.get("mrn") or p.get("hisPid") for p in result_sc["den_patients"]}

            # 收集 DC pid 用于批量查 T0
            dc_pids_for_t0 = []

            # #修复: deptCode 是字符串，需与 str 类型比较
            dept_codes_str = {str(c) for c in dept_codes} if dept_codes else set()
            for dx in dc_diagnoses:
                if dept_codes_str and str(dx.get("deptCode", "")) not in dept_codes_str:
                    continue
                mrn = str(dx.get("mrn") or "").strip()
                patient_id = str(dx.get("pid") or "").strip()

                # 去重: 如果该患者已在 SmartCare 结果中，跳过
                if patient_id in sc_pids or mrn in sc_mrns:
                    continue

                # 24h 窗口过滤: 入院到入ICU <= 24h
                in_patient_time = dx.get("inPatientTime")
                admit_time = dx.get("admitTime")
                if isinstance(in_patient_time, dt) and isinstance(admit_time, dt):
                    delta_hours = (admit_time - in_patient_time).total_seconds() / 3600
                    if delta_hours > 24:
                        continue  # 超过24h，不进分母

                dc_pids_for_t0.append(patient_id)

                result["total"] += 1
                result["den_patients"].append({
                    "_id": patient_id,
                    "mrn": mrn,
                    "name": dx.get("name", ""),
                    "hisBed": dx.get("bed", ""),
                    "diagnosisTime": admit_time,
                    "inPatientTime": in_patient_time,
                    "diseaseId": str(dx.get("_id", "")),
                    "deptCode": dx.get("deptCode", ""),
                    "diagnose": dx.get("diagnose", ""),
                    "source": "vi_zybr",
                })

            # 批量查 T0: 从 VI_ICU_ZYYZ 取每患者第一条医嘱 orderTime
            if dc_pids_for_t0:
                _resolve_t0_for_dc_patients(dc, dc_pids_for_t0, result["den_patients"])

    except Exception as _exc:
        logger.warning("DC VI_ICU_ZYBR query failed: %s", _exc)

    # ---- 阶段 3: MRN → SC pid 桥接（供 judge_bundle_v3_for_patient 使用） ----
    # #修复: 需要区分同一患者的不同住院/ICU事件
    all_mrns = {p.get("mrn") for p in result["den_patients"] if p.get("mrn")}
    mrn_to_sc_pid = {}
    if all_mrns:
        for db_name, db in iter_bed_dbs():
            try:
                # 查询患者信息，包括入科时间，用于区分不同住院事件
                pat_docs = list(db.patient.find(
                    {"mrn": {"$in": list(all_mrns)}},
                    {"mrn": 1, "_id": 1, "icuAdmissionTime": 1, "icuDischargeTime": 1},
                ))
                for pd in pat_docs:
                    m = pd.get("mrn", "")
                    if m:
                        # 如果MRN已存在，检查是否是不同的住院事件
                        if m in mrn_to_sc_pid:
                            # 比较入科时间，选择最近的住院事件
                            existing_pid = mrn_to_sc_pid[m]
                            # 简单策略：选择第一个匹配的（后续可以优化为选择最近的）
                            continue
                        mrn_to_sc_pid[m] = str(pd["_id"])
                if mrn_to_sc_pid:
                    break
            except Exception as _exc:
                logger.warning("MRN→SC pid bridge failed on %s: %s", db_name, _exc)
                continue

    # 回填 sc_pid 到 den_patients
    for pat in result["den_patients"]:
        mrn = pat.get("mrn", "")
        if mrn and mrn in mrn_to_sc_pid:
            pat["sc_pid"] = mrn_to_sc_pid[mrn]
        elif pat.get("source") != "vi_zybr":
            # SmartCare 来源的患者，_id 本身就是 sc_pid
            pat["sc_pid"] = str(pat.get("_id", ""))

    # ---- 阶段 3.5: cg 变体 — 分母确诊时间取 patient 诊断 list 最早命中 ----
    if BUNDLE_HOSPITAL_VARIANT == "cg":
        _apply_cg_confirm_time(result["den_patients"])

    # ---- 阶段 4: 批量预加载检验数据 ----
    # 收集所有患者的 his_pid 和时间窗口，批量查询 VI_ICU_EXAM 和 VI_ICU_EXAM_ITEM
    batch_lab_cache = {}
    his_pids_with_time = []  # [(his_pid, window_start, eval_time), ...]
    global_seen = set()  # 全局去重集合，用于跨批次去重

    for pat in result["den_patients"]:
        his_pid = pat.get("hisPid") or pat.get("mrn")
        t0 = pat.get("t0")
        if his_pid and t0:
            # 计算时间窗口: T0-2h 到 T0+24h（覆盖 SOFA 评分所需的检验数据）
            from datetime import timedelta
            window_start = t0 - timedelta(hours=2) if isinstance(t0, datetime) else None
            eval_time = t0 + timedelta(hours=24) if isinstance(t0, datetime) else None
            if window_start and eval_time:
                his_pids_with_time.append((his_pid, window_start, eval_time))

    # 批量查询检验数据
    if his_pids_with_time:
        try:
            from scoring.lab_batch import batch_fetch_lab_observations

            # 计算全局窗口
            all_window_starts = [w[1] for w in his_pids_with_time]
            all_eval_times = [w[2] for w in his_pids_with_time]
            global_window_start = min(all_window_starts)
            global_eval_time = max(all_eval_times)

            # 收集所有 his_pid
            unique_his_pids = list(set(w[0] for w in his_pids_with_time))

            # 连接 DataCenter
            try:
                dc = get_client("DataCenter")["DataCenter"]
                batch_lab_cache = batch_fetch_lab_observations(
                    dc=dc,
                    his_pids=unique_his_pids,
                    eval_time=global_eval_time,
                    lookback_hours=int((global_eval_time - global_window_start).total_seconds() / 3600) + 1,
                    seen=global_seen,
                )
                logger.info("Batch lab query completed: %d patients, %d total observations",
                           len(batch_lab_cache), sum(len(obs) for obs in batch_lab_cache.values()))
            except Exception as e:
                logger.warning("Batch lab query failed: %s", e)
                batch_lab_cache = {}
        except ImportError:
            logger.warning("lab_batch module not available, falling back to single queries")
            batch_lab_cache = {}

    # ---- 阶段 5: V3 判定 → h1/h3/h6 计数 ----
    # Resolve T0 per candidate; a SmartCare-only period must not rely on a
    # separate DataCenter candidate to receive its fallback T0.
    for pat in result["den_patients"]:
        if not pat.get("t0"):
            pat["t0"] = pat.get("qc_t0") or pat.get("diagnosisTime")
            pat["t0_source"] = "qc_t0" if pat.get("qc_t0") else "diagnosis_time_fallback"
        if not pat.get("t0"):
            pat["event_mapping_status"] = "MISSING_T0"

    for pat in result["den_patients"]:
        sc_pid = pat.get("sc_pid")
        t0 = pat.get("t0")
        diag = pat.get("diagnose", "")
        his_pid = pat.get("hisPid") or pat.get("mrn")
        if not t0 or not sc_pid:
            pat["event_mapping_status"] = pat.get("event_mapping_status", "MISSING_EVENT_ID")
            continue
        _v3_step = "init"
        try:
            _v3_step = "exclusion_key"
            event_id = str(pat.get("dc_pid") or pat.get("_id") or sc_pid)
            pat["detail_id"] = build_exclusion_key(event_id, t0)
            pat["exclusion_key"] = pat["detail_id"]
            _v3_step = "judge_bundle_v3_for_patient"
            v3 = judge_bundle_v3_for_patient(sc_pid, resolve_bundle_dc_pid(pat), pat.get("mrn", ""), t0, diag,
                                            batch_lab_cache=batch_lab_cache)
            _v3_step = "post_judge"
            pat["v3"] = v3
            # 分母判定: 使用候选引擎 (高召回四通道)
            # 休克确认: bundle_rules.SHOCK_RULE 口径 → _septic_shock_confirmed
            # 新口径: candidate_info 由 judge_bundle_v3_for_patient 唯一计算，此处直接复用
            cl = v3.get("clinical_layer") or {}
            # #fix: .get(key, {}) 在 key 存在且值为 None 时仍返回 None
            shock_layer = cl.get("layer4_shock") or {}
            _v3_step = "septic_shock_confirmed"
            old_shock_confirmed = _septic_shock_confirmed(v3)
            # 候选引擎集成: 复用 v3["candidate_info"]，不重复调用 extract_candidate
            _v3_step = "candidate_info"
            candidate_info = v3.get("candidate_info")
            if candidate_info and candidate_info.get("candidate_status"):
                pat["candidate_info"] = candidate_info
                pat["is_septic_shock_candidate"] = candidate_info.get("is_septic_shock_candidate", False)
                pat["candidate_status"] = candidate_info.get("candidate_status", "not_candidate")
                pat["candidate_pathways"] = candidate_info.get("candidate_pathways", [])
                pat["clinical_confirmation_status"] = candidate_info.get("clinical_confirmation_status", "insufficient")
            else:
                # judge_bundle_v3_for_patient 未返回有效 candidate_info → pending_review
                logger.warning("No candidate_info in v3 for %s, marking pending_review", pat.get("mrn"))
                pat["candidate_info"] = {
                    "is_septic_shock_candidate": True,
                    "candidate_status": "pending_review",
                    "candidate_pathways": ["engine_error"],
                    "clinical_confirmation_status": "insufficient",
                    "candidate_reasons": ["候选引擎未返回结果，需人工复核"],
                    "missing_evidence": ["candidate_info_missing"],
                    "error_code": "CANDIDATE_INFO_MISSING",
                }
                pat["is_septic_shock_candidate"] = True
                pat["candidate_status"] = "pending_review"
                pat["candidate_pathways"] = ["engine_error"]
                pat["clinical_confirmation_status"] = "insufficient"
            # 影子比对字段保留
            _v3_step = "shock_status_new"
            pat["shock_status_new"] = shock_layer.get("shock_status")
            pat["sofa2_total"] = (cl.get("layer2_organ_dysfunction") or {}).get("sofa2_total")
            # 旧口径分子: K1 AND K2 (正式分子)
            # #fix: .get(key, {}) returns None when key exists with None value
            # Use (d.get(key) or {}) to safely default to empty dict
            _v3_step = "bundles"
            bundle_1h = v3.get("bundle_1h") or {}
            bundle_3h = v3.get("bundle_3h") or {}
            bundle_6h = v3.get("bundle_6h") or {}
            if old_shock_confirmed:
                if bundle_1h.get("finish") is True:
                    result["h1_num"] += 1
                    result["h1_patients"].append(pat)
                if bundle_3h.get("finish") is True:
                    result["h3_num"] += 1
                    result["h3_patients"].append(pat)
                if bundle_6h.get("finish") is True:
                    result["h6_num"] += 1
                    result["h6_patients"].append(pat)
            # 影子分子: 候选引擎结果不为 not_candidate 的患者
            # 新候选分子不受旧K1/K2过滤
            is_shadow_candidate = pat.get("candidate_status", "not_candidate") != "not_candidate"
            if is_shadow_candidate:
                if bundle_1h.get("finish") is True:
                    if not old_shock_confirmed:
                        result.setdefault("shadow_h1_patients", []).append(pat)
                if bundle_3h.get("finish") is True:
                    if not old_shock_confirmed:
                        result.setdefault("shadow_h3_patients", []).append(pat)
                if bundle_6h.get("finish") is True:
                    if not old_shock_confirmed:
                        result.setdefault("shadow_h6_patients", []).append(pat)
        except Exception as _exc:
            import traceback
            logger.warning("V3 judgment failed for %s: %s\n%s",
                           pat.get("mrn"), _exc, traceback.format_exc())
            continue

    # 重新计算分母
    from config.candidate_rules import CANDIDATE_ENGINE_MODE

    # 旧口径 → 现行 SHOCK_RULE 确认口径
    # #fix: .get(key, {}) returns None when key exists with None value
    old_shock_count = sum(1 for p in result["den_patients"]
                          if _septic_shock_confirmed(p.get("v3")))

    # 新口径: 候选引擎结果 → 影子分母
    candidate_count = sum(1 for p in result["den_patients"]
                          if p.get("candidate_status", "not_candidate") != "not_candidate")
    high_prob_count = sum(1 for p in result["den_patients"]
                          if p.get("candidate_status") == "high_probability")
    probable_count = sum(1 for p in result["den_patients"]
                         if p.get("candidate_status") == "probable")
    pending_count = sum(1 for p in result["den_patients"]
                        if p.get("candidate_status") == "pending_review")

    if CANDIDATE_ENGINE_MODE == "shadow":
        # shadow 模式: 正式分母使用旧口径，候选引擎结果作为影子数据
        result["total"] = old_shock_count
        result["candidate_shadow"] = {
            "candidate_count": candidate_count,
            "high_probability_count": high_prob_count,
            "probable_count": probable_count,
            "pending_review_count": pending_count,
            "not_candidate_count": len(result["den_patients"]) - candidate_count,
        }
    elif CANDIDATE_ENGINE_MODE == "active":
        # active 模式: 正式分母使用候选引擎结果
        result["total"] = candidate_count
    else:
        result["total"] = old_shock_count

    # 影子比对字段 (始终保留)
    result["old_shock_count"] = old_shock_count
    result["candidate_mode"] = CANDIDATE_ENGINE_MODE

    return result


def _resolve_t0_for_dc_patients(dc, dc_pids: list, den_patients: list):
    """
    批量从 VI_ICU_ZYYZ 解析 T0（第一条医嘱 orderTime）。
    回填 den_patients 中每条记录的 t0 字段。
    """
    from datetime import datetime as dt

    # 批量查询所有 DC 患者的医嘱，按 pid+orderTime 排序
    try:
        pipeline = [
            {"$match": {"pid": {"$in": dc_pids}}},
            {"$sort": {"pid": 1, "orderTime": 1}},
            {"$group": {
                "_id": "$pid",
                "first_order_time": {"$first": "$orderTime"},
                "first_order_name": {"$first": "$orderName"},
            }},
        ]
        t0_results = list(dc.VI_ICU_ZYYZ.aggregate(pipeline))
        t0_map = {str(r["_id"]): r["first_order_time"] for r in t0_results if r.get("first_order_time")}
        t0_name_map = {str(r["_id"]): r.get("first_order_name", "") for r in t0_results}
    except Exception as _exc:
        logger.warning("T0 resolution from VI_ICU_ZYYZ failed: %s", _exc)
        t0_map = {}
        t0_name_map = {}

    # 回填 t0
    for pat in den_patients:
        pid = pat.get("_id", "")
        if pid in t0_map:
            pat["t0"] = t0_map[pid]
            pat["t0_source"] = "first_order"
            pat["t0_first_order_name"] = t0_name_map.get(pid, "")
        else:
            # 兜底: 用 admitTime
            pat["t0"] = pat.get("diagnosisTime")
            pat["t0_source"] = "admit_time_fallback"


# ============================================================
# ICU-08 氧疗途径判定（主判据，替代通气模式猜测）
# ============================================================

# 无创氧疗途径 — 命中任一即排除
O2_ROUTE_NON_INVASIVE = {"鼻塞", "面罩", "无创", "高流量", "箱氧", "鼻导管",
                          "储氧面罩", "低流量", "低流量用氧", "低流量箱氧",
                          "未吸氧", "鼻氧", "面罩吸氧", "储氧", "鼻", "拒绝",
                          "拒绝吸氧", "暂停吸氧", "球囊", "呼吸球囊", "球囊通气",
                          "简易呼吸器", "家用呼吸机", "家用", "文丘里", "无创高频",
                          "高频", "鼻管辅"}

# 有创氧疗途径 — 经人工气道
O2_ROUTE_INVASIVE = {"管辅", "切辅", "管氧", "切氧", "管文", "切文", "管高", "切高", "有创"}


def _num_from_item(item: dict, prefer_str: bool = False) -> float | None:
    keys = ("strVal", "fVal") if prefer_str else ("fVal", "strVal")
    for key in keys:
        raw = item.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(str(raw).replace("%", "").replace("L/min", "").strip())
        except (TypeError, ValueError):
            continue
    return None


def _pf_ratio_from_bedsides(bedsides: list) -> float | None:
    vals = {}
    for item in bedsides or []:
        if item.get("valid") != "valid":
            continue
        code = item.get("code")
        if code == "param_bg_P/Fratio":
            return _num_from_item(item)
        if code == "param_bg_OI":
            vals["oi"] = _num_from_item(item, prefer_str=True)
        elif code == "param_bg_po2":
            vals["po2"] = _num_from_item(item)
        elif code == "param_bg_FiO2":
            vals["fio2"] = _num_from_item(item, prefer_str=True)
    if vals.get("oi") is not None:
        return vals["oi"]
    po2 = vals.get("po2")
    fio2 = vals.get("fio2")
    if po2 is not None and fio2:
        fio2_fraction = fio2 / 100 if fio2 > 1 else fio2
        if fio2_fraction > 0:
            return po2 / fio2_fraction
    return None


def _parse_o2_routes(raw: str) -> set:
    """解析氧疗途径字符串（可能含 、/ 分隔的组合值），返回归一化集合。过滤纯数字脏数据。"""
    if not raw:
        return set()
    s = raw.strip().replace("、", ",").replace("，", ",").replace("/", ",")
    s = s.replace("+", ",").replace(" ", "")
    routes = set()
    for x in s.split(","):
        x = x.strip()
        if not x:
            continue
        # 过滤纯数字（脏数据）
        try:
            float(x)
            continue  # 纯数字，跳过
        except ValueError:
            pass
        routes.add(x)
    return routes


def is_invasive_by_o2route(raw_route: str) -> tuple:
    """
    氧疗途径判定有创/无创。
    返回 (is_invasive: bool, routes: set, reason: str)
    """
    routes = _parse_o2_routes(raw_route)
    if not routes:
        return False, routes, "空值"

    # 无创命中 → 排除
    non_inv = {kw for route in routes for kw in O2_ROUTE_NON_INVASIVE if kw in route}
    if non_inv:
        return False, routes, f"无创途径({','.join(non_inv)})"

    # 有创命中 → 纳入
    inv = {kw for route in routes for kw in O2_ROUTE_INVASIVE if kw in route}
    if inv:
        return True, routes, f"有创途径({','.join(inv)})"

    # 未知 → 排除（保守）
    return False, routes, f"未知途径({','.join(routes)})"


# ============================================================
# ICU-08：中重度ARDS俯卧位实施率 — 诊断分母 + 俯卧位分子
# ============================================================

# 分母诊断关键词：patient.clinicalDiagnosis 命中任一即视为 ARDS 患者（不区分大小写）
ARDS_DIAG_PATTERN = "呼吸窘迫综合征|ARDS"


def get_icu08_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    ICU-08（中重度ARDS俯卧位通气实施率）分母+分子。

    分母：同期 ICU 在科患者中，patient.clinicalDiagnosis 包含
          "呼吸窘迫综合征" 或 "ARDS" 的患者（不区分大小写）。
          同期口径与 get_icu_denominator_stays 一致：
          icuAdmissionTime <= 期末 且 (icuDischargeTime >= 期初 或 未出科)。

    分子：分母患者在 [统计月 ∩ 本次住院] 窗口内存在
          bedside code=param_TiWei、strVal 含"俯卧位"、valid=true 记录的患者数。

    口径说明：SmartCare patient 一行 = 一次 ICU 入住（bedside.pid 按次住院关联），
    同一患者多次入科按在科记录各计一条，分子分母同队列。

    返回: {den_count, num_count, den_patients, num_patients}
    """
    start_dt = utc_day_start(start_date)
    end_dt_wide = utc_day_end_wide(end_date)

    den_patients = []

    for db_name, db in iter_bed_dbs():
        try:
            cursor = db.patient.find(
                {
                    "deptCode": {"$in": dept_codes},
                    "status": {"$ne": "invalid"},
                    "icuAdmissionTime": {"$lte": end_dt_wide},
                    "$or": [
                        {"icuDischargeTime": {"$gte": start_dt}},
                        {"icuDischargeTime": None},
                        {"icuDischargeTime": {"$exists": False}},
                    ],
                    "clinicalDiagnosis": {
                        "$regex": ARDS_DIAG_PATTERN,
                        "$options": "i",
                    },
                },
                {"_id": 1, "mrn": 1, "hisPid": 1, "name": 1, "hisBed": 1,
                 "clinicalDiagnosis": 1, "icuAdmissionTime": 1, "icuDischargeTime": 1},
            ).sort("icuAdmissionTime", 1)
            for pat in cursor:
                pid = str(pat["_id"])
                den_patients.append({
                    "pid": pid,
                    "mrn": pat.get("mrn", "") or pat.get("hisPid", ""),
                    "name": pat.get("name", ""),
                    "hisBed": pat.get("hisBed", ""),
                    "diagnosis": pat.get("clinicalDiagnosis", ""),
                    "icuAdmissionTime": pat.get("icuAdmissionTime"),
                    "icuDischargeTime": pat.get("icuDischargeTime"),
                    "exclusion_key": build_exclusion_key(pid, pat.get("icuAdmissionTime")),
                })
            break
        except Exception as _exc:
            logger.warning("DB %s failed in get_icu08_data: %s", db_name, _exc)
            continue

    logger.info("[ICU-08] window=%s ~ %s dept=%s 诊断口径分母=%d",
                start_date, end_date, dept_codes, len(den_patients))

    # 分子：统计月 ∩ 住院期 窗口内的俯卧位
    num_result = get_ards_prone_numerator(den_patients, start_date, end_date)

    # 窗口自检：分子不得大于分母
    if num_result["num_count"] > len(den_patients):
        logger.warning("[ICU-08] 分子(%d) 大于 分母(%d),窗口口径异常,已截断",
                       num_result["num_count"], len(den_patients))
        num_result["num_count"] = len(den_patients)

    return {
        "den_count": len(den_patients),
        "num_count": num_result["num_count"],
        "den_patients": den_patients,
        "num_patients": num_result["num_patients"],
    }


def get_ards_prone_numerator(den_patients: list, start_date: str, end_date: str) -> dict:
    """
    ICU-08 分子：分母患者中，[统计月 ∩ 本次住院] 窗口内存在俯卧位记录的患者数。

    判据：bedside 表 code=param_TiWei, strVal 含"俯卧位", valid=true,
          time 落在窗口内。未出科取月末，绝不用 now()。

    参数:
      den_patients: 分母患者列表（get_icu08_data 产出），
                    每项含 pid / mrn / name / icuAdmissionTime / icuDischargeTime
      start_date: 统计月起始日期 "YYYY-MM-DD"
      end_date: 统计月结束日期 "YYYY-MM-DD"

    返回: {num_count, num_patients: [{pid, mrn, name, prone_times[], prone_count}]}
    """
    start_dt = utc_day_start(start_date)
    end_dt_wide = utc_day_end_wide(end_date)

    result = {"num_count": 0, "num_patients": []}

    for db_name, db in iter_bed_dbs():
        try:
            for p in den_patients:
                pid = p.get("pid", "")
                if not pid:
                    continue

                # 统计月 ∩ 本次住院 的观察窗口
                win_start, win_end = _month_window(p, start_dt, end_dt_wide)
                if win_start > win_end:
                    continue  # 本月不在科

                prone_records = list(db.bedside.find(
                    {
                        "pid": pid,
                        "code": "param_TiWei",
                        "valid": True,
                        "strVal": {"$regex": "俯卧位"},
                        "time": {"$gte": win_start, "$lte": win_end},
                    },
                    {"strVal": 1, "time": 1},
                ).sort("time", 1).limit(50))

                if prone_records:
                    result["num_count"] += 1
                    result["num_patients"].append({
                        "pid": pid,
                        "mrn": p.get("mrn", ""),
                        "name": p.get("name", ""),
                        "prone_times": [r["time"] for r in prone_records[:10]],
                        "prone_count": len(prone_records),
                        "exclusion_key": p.get("exclusion_key", ""),
                    })
            break
        except Exception as _exc:
            logger.warning("DB %s failed in get_ards_prone_numerator: %s", db_name, _exc)
            continue

    return result


# ============================================================
# ICU-07：DVT预防率 — 从 DataCenter.VI_ICU_ZYYZ 取数
# ============================================================

# ---- 可配置关键词列表 ----

# 药物预防：抗凝药词根（正则 contains，不区分大小写）
DRUG_DVT_KEYWORDS = [
    "肝素", "heparin",           # 肝素类（含依诺肝素/enoxaparin、达肝素/dalteparin、那屈肝素/nadroparin等）
    "磺达肝癸", "fondaparinux",  # 戊聚糖类
    "沙班", "xaban",             # Xa因子抑制剂（利伐沙班/rivaroxaban、阿哌沙班/apixaban等）
    "达比加群", "dabigatran",
    "华法林", "warfarin",
    "比伐芦定", "bivalirudin",
    "阿加曲班", "argatroban",
]

# 封管/冲管排除词（肝素+这些=导管维护,非DVT预防）
FLUSH_EXCLUDE_KEYWORDS = [
    "封管", "冲管", "封管液", "flush", "lock",
    "有创压用", "创压用", "动脉压用",  # 有创血压管路维护
]

# 物理（机械）预防关键词
MECH_DVT_KEYWORDS = [
    "间歇充气加压", "IPC",
    "充气加压", "加压泵", "抗栓泵",
    "弹力袜", "压力袜", "梯度压力袜", "抗血栓袜",
    "足底静脉泵", "足底泵", "VFP", "足底脉冲",
    "气压治疗",   # 本院收费项「气压治疗费 单肢 bid」= 间歇充气加压(IPC)，不加会漏掉绝大多数物理预防
    "运动功能训练",  # 院方口径 2026-10：早期运动训练属 DVT 物理预防（含「运动功能训练-每增加10分钟」加收项）
]

# 检验项目假匹配：关键词"肝素"会命中「肝素结合蛋白（HBP）/降钙素原（PCT）二联检」，
# 这是抽血化验不是预防措施，必须剔除。
DVT_LAB_FALSE_MATCH_PATTERN = "肝素结合蛋白|降钙素原|二联检|三联检|HBP|PCT"

# 给药途径/用途属于管路维护、体外循环抗凝的执行记录 —— 不是 DVT 预防。
# VI_ICU_ZYYZ 的短医嘱名（如 "*肝素钠注射液(海普天)0.250万iu"）不带用法，
# 名称匹配无法排除，必须看 exeMethod / exeMethodCode：
#   有创压用(50) / 封管用(41) = 导管维护、血液净化(40) = CRRT/血液净化管路抗凝
NON_PROPHYLAXIS_EXE_METHODS = ("有创压用", "封管用", "冲管", "血液净化", "PICC", "CVC")
NON_PROPHYLAXIS_EXE_CODES = ("50", "41", "40")

# 滤器关键词（单独标记，不直接计入分子 — 待质控医生确认）
FILTER_KEYWORDS = [
    "静脉滤器", "滤器植入", "IVC滤器",
]


def _dvt_patterns():
    """构建 ICU-07 医嘱匹配用的编译正则（惰性，模块导入时关键词已就绪）。"""
    global _DVT_RES
    if _DVT_RES is None:
        _DVT_RES = {
            "all": re.compile(_keyword_regex(DRUG_DVT_KEYWORDS + MECH_DVT_KEYWORDS + FILTER_KEYWORDS), re.I),
            "drug": re.compile(_keyword_regex(DRUG_DVT_KEYWORDS), re.I),
            "mech": re.compile(_keyword_regex(MECH_DVT_KEYWORDS), re.I),
            "filter": re.compile(_keyword_regex(FILTER_KEYWORDS), re.I),
            "flush": re.compile(_keyword_regex(FLUSH_EXCLUDE_KEYWORDS), re.I),
            "lab": re.compile(DVT_LAB_FALSE_MATCH_PATTERN, re.I),
            "bad_exe": re.compile("|".join(NON_PROPHYLAXIS_EXE_METHODS)),
        }
    return _DVT_RES


_DVT_RES = None


def classify_dvt_order(order_name: str, exe_method=None, exe_code=None) -> str:
    """
    判断 VI_ICU_ZYYZ 的一条医嘱是否为 DVT 预防措施。

    返回: "drug"（药物预防） / "mech"（物理预防） / "filter"（滤器，不计分子） / ""（不是）

    剔除三类假阳性：
      1. 检验项目 —— 关键词"肝素"会命中「肝素结合蛋白（HBP）/降钙素原（PCT）二联检」
      2. 名称带封管/冲管/有创压用的导管维护医嘱
      3. 给药途径为管路维护或体外循环抗凝 —— 短医嘱名（如 "*肝素钠注射液(海普天)0.250万iu"）
         不带用法，只能靠 exeMethod / exeMethodCode 判断
    注意：「停*xxx」停止医嘱记录 status=已执行，说明当月确实存在过该预防医嘱，予以保留。
    """
    if not order_name:
        return ""
    rx = _dvt_patterns()
    if rx["lab"].search(order_name):
        return ""

    hit_drug = bool(rx["drug"].search(order_name))
    hit_mech = bool(rx["mech"].search(order_name))
    hit_filter = bool(rx["filter"].search(order_name))
    if not (hit_drug or hit_mech or hit_filter):
        return ""

    if hit_drug:
        if rx["flush"].search(order_name):
            hit_drug = False
        else:
            exe = str(exe_method or "")
            code = str(exe_code or "")
            if rx["bad_exe"].search(exe) or code in NON_PROPHYLAXIS_EXE_CODES:
                hit_drug = False

    if hit_drug and hit_mech:
        return "drug"
    if hit_drug:
        return "drug"
    if hit_mech:
        return "mech"
    if hit_filter:
        return "filter"
    return ""


def get_dvt_prevention_patients(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    ICU-07 分子：实施了 DVT 预防措施的在科记录。
    数据源：DataCenter.VI_ICU_ZYYZ（医嘱名称关键词匹配 + 给药途径过滤）。

    队列 = 分母队列（get_icu_denominator_stays，与 ICU-04-01 完全一致），
    再按 mrn 反查 DataCenter.VI_ICU_ZYBR.pid 取医嘱。

    ⚠ 这里不能用 VI_ICU_ZYBR.deptCode 过滤：该表每个 mrn 只保留最近一次住院科室记录，
      已转出本科室的患者会被整条剔除（2026-09 科室3439 曾因此漏计 12 人）。

    口径：**人次**（一条在科记录一行，不按 mrn 去重）。
    分子判定从分母每条在科记录出发：该次住院窗口 [入科, 出科∧月末] 内
    有药物/物理预防医嘱即计入——「原有患者」上个月入科时下的医嘱同样命中
    （#fix 原有患者满足分子却漏计）。
    药物预防 = 肝素等抗凝药医嘱；物理预防 = 气压治疗、运动功能训练等
    （院方口径 2026-10，明细 measure 展示「药物预防/物理预防/药物+物理」）。

    返回:
        drug_patients / mech_patients / filter_patients —— 按人归类的明细列表（审计用）
        all_count      —— 纳入分子的在科人次（算比率用它）
        all_stay_count —— 同 all_count（保留字段兼容旧消费方）
        num_stays      —— 分子明细（一行一在科记录，与 all_count 行数一致）
    """
    from datetime import datetime as dt

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_of_day = utc_day_end_wide(end_date)

    result = {
        "drug_pids": set(), "mech_pids": set(), "filter_pids": set(),
        "drug_patients": [], "mech_patients": [], "filter_patients": [],
        "all_count": 0, "all_stay_count": 0, "num_stays": [],
        # False = 环境性失败（DataCenter 不可达/映射失败），all_count 的 0 不可信。
        # 预聚合侧见此标记跳过写入保留旧值；明细侧拒绝产出空列表。
        # #fix: 原吞异常静默返回 0，重建把 0 当真实分子落库（2026-10 公式条 0 vs 明细 47）
        "data_available": True,
    }

    try:
        # Step 1: 分母队列（按人次），与 ICU-04-01 同一查询 —— 保证分子分母同队列
        stays = get_icu_denominator_stays(dept_codes, start_date, end_date)
        den_mrns = {s.get("mrn") for s in stays if s.get("mrn")}
        if not den_mrns:
            return result

        # 代表次在科：与分母明细同样取该患者入科最早的一次（仅审计列表用）
        den_persons = dedup_persons(stays)
        stay_by_mrn = {s.get("mrn"): s for s in den_persons}

        db = get_datacenter_db()

        # Step 2: mrn → DataCenter pid（不按 deptCode / 时间过滤，见 docstring）
        zybr_docs = list(db["VI_ICU_ZYBR"].find(
            {"mrn": {"$in": list(den_mrns)}},
            {"pid": 1, "mrn": 1, "name": 1},
        ))
        zybr_by_pid = {d["pid"]: d for d in zybr_docs if d.get("pid")}
        valid_pids = set(zybr_by_pid.keys())
        if not valid_pids:
            # 分母有人却一个 pid 都映射不到 = DataCenter ZYBR 查询失败/数据漂移，
            # 不是「无人预防」——标记不可用，防止汇总按 0 落库
            result["data_available"] = False
            return result

        # Step 3: 医嘱——下界放宽到分母队列最早入科时间（覆盖「原有患者」
        # 入科时即下医嘱的情况），上界仍为月末；按次住院窗口的精确匹配在 Step 5
        rx = _dvt_patterns()
        order_q = {
            "pid": {"$in": list(valid_pids)},
            "orderName": {"$regex": rx["all"].pattern, "$options": "i"},
            "orderTime": {"$lte": end_of_day},
            "status": {"$in": EXECUTED_ORDER_STATUSES},
        }
        admits = [s.get("icuAdmissionTime") for s in stays
                  if isinstance(s.get("icuAdmissionTime"), dt)]
        if admits:
            order_q["orderTime"]["$gte"] = min(admits)
        orders = list(db["VI_ICU_ZYYZ"].find(
            order_q,
            {"pid": 1, "orderName": 1, "orderTime": 1, "exeMethod": 1, "exeMethodCode": 1},
        ).limit(200000))

        # Step 4: 分类（剔检验假匹配 / 名称封管 / 给药途径为管路维护或血液净化）
        # 每条医嘱保留 (名称, 开立时间) 供按次住院窗口匹配
        drug_by_pid, mech_by_pid, filter_by_pid = {}, {}, {}
        for o in orders:
            pid = o.get("pid", "")
            name = o.get("orderName", "")
            if not pid or not name:
                continue
            if pid not in valid_pids:
                continue
            mrn = zybr_by_pid.get(pid, {}).get("mrn")
            if mrn not in den_mrns:
                continue
            kind = classify_dvt_order(name, o.get("exeMethod"), o.get("exeMethodCode"))
            entry = (name, o.get("orderTime"))
            if kind == "drug":
                drug_by_pid.setdefault(pid, []).append(entry)
            elif kind == "mech":
                mech_by_pid.setdefault(pid, []).append(entry)
            elif kind == "filter":
                filter_by_pid.setdefault(pid, []).append(entry)

        result["drug_pids"] = set(drug_by_pid)
        result["mech_pids"] = set(mech_by_pid)
        result["filter_pids"] = set(filter_by_pid)

        def build_list(by_pid):
            """按 DataCenter pid 归类的审计列表（mrn 级，保留字段兼容 report_export）。"""
            out = []
            for pid, olist in by_pid.items():
                zybr = zybr_by_pid.get(pid, {})
                mrn = zybr.get("mrn", "")
                stay = stay_by_mrn.get(mrn, {})
                out.append({
                    "pid": pid,
                    "mrn": mrn,
                    "patient_id": stay.get("hisPid") or mrn,   # 住院号
                    "name": stay.get("name") or zybr.get("name", ""),
                    "hisBed": stay.get("hisBed", ""),
                    "icuAdmissionTime": stay.get("icuAdmissionTime"),
                    "icuDischargeTime": stay.get("icuDischargeTime"),
                    "matched_orders": [n for n, _t in olist[:5]],
                    "order_count": len(olist),
                })
            return out

        result["drug_patients"] = build_list(drug_by_pid)
        result["mech_patients"] = build_list(mech_by_pid)
        result["filter_patients"] = build_list(filter_by_pid)

        # Step 5: 分子 —— 从分母每条在科记录出发，该次住院窗口内
        # 有药物/物理预防医嘱即计入（人次口径，一行一在科记录）
        orders_by_mrn = defaultdict(list)   # mrn → [(name, time, kind)]
        for src, kind in ((drug_by_pid, "drug"), (mech_by_pid, "mech")):
            for _pid, entries in src.items():
                _mrn = zybr_by_pid.get(_pid, {}).get("mrn")
                if _mrn:
                    orders_by_mrn[_mrn].extend((n, t, kind) for n, t in entries)

        num_stays = []
        for s in stays:
            mrn = s.get("mrn")
            w = _stay_window(s, end_of_day)
            if not w:
                continue
            in_drug = in_mech = False
            drug_names, mech_names = [], []
            for name, t, kind in orders_by_mrn.get(mrn, []):
                if isinstance(t, dt) and (t < w[0] or t > w[1]):
                    continue  # 医嘱不在该次住院窗口内（属于另一次入科）
                if kind == "drug":
                    in_drug = True
                    drug_names.append(name)
                else:
                    in_mech = True
                    mech_names.append(name)
            if not (drug_names or mech_names):
                continue
            # 示例两类都露出：药物≤3 + 物理≤3（#fix 药物+物理的患者示例被高频药物挤满）
            matched = drug_names[:3] + mech_names[:3]
            num_stays.append({
                "patient_id": s.get("hisPid") or mrn,
                # stay_id = SmartCare patient._id，与分母明细同一主键，
                # 分子/分母同一次在科可逐行对上；同一患者 2 次入科时也能区分
                "stay_id": str(s.get("_id", "")),
                "mrn": mrn,
                "name": s.get("name", ""),
                "hisBed": s.get("hisBed", ""),
                "deptCode": s.get("deptCode", ""),
                "icuAdmissionTime": s.get("icuAdmissionTime"),
                "icuDischargeTime": s.get("icuDischargeTime"),
                "measure": ("药物+物理" if in_drug and in_mech
                            else "药物预防" if in_drug else "物理预防"),
                "order_count": len(drug_names) + len(mech_names),
                "matched_orders": matched,
                # 页面「医嘱示例」列只取一条时，两类各留一条代表（药物+物理可拼接展示）
                "sample_drug": drug_names[0] if drug_names else "",
                "sample_mech": mech_names[0] if mech_names else "",
            })
        # 人次口径：不按 mrn 去重，行数 == all_count
        result["num_stays"] = num_stays
        result["all_count"] = len(num_stays)
        result["all_stay_count"] = len(num_stays)

        print(f"[ICU-07] 分母 {len(stays)}人次/{len(den_mrns)}人 | "
              f"分子 {result['all_count']}人次 | "
              f"药物 {len(drug_by_pid)}pid 物理 {len(mech_by_pid)}pid")

    except Exception as e:
        print(f"[ICU-07] Error: {e}")
        # 环境性失败：调用方按 data_available=False 处理，
        # 勿把这里静默返回的 0 当成真实分子落库/缓存
        result["data_available"] = False

    return result


# ============================================================
# ICU-05 Bundle 组件提取（跨库查询）
# 液体量 → SC drugExe | 抗生素 → SC drugExe | 血培养 → DC VI_ICU_ZYYZ
# ============================================================

def get_bundle_components_from_dc(dc_pids, t0_map):
    """
    从 DC 脓毒性休克患者出发，跨库提取 Bundle 组件数据。

    Args:
        dc_pids: DC VI_ICU_ZYBR 的 pid 列表 (str)
        t0_map: {dc_pid: t0_datetime} T0时间映射

    Returns:
        {dc_pid: {
            'sc_pid': str,
            'fluids_3h_ml': float,   # T0~T0+3h 液体量 (ml)
            'fluids_6h_ml': float,   # T0~T0+6h 液体量 (ml)
            'abx_in_1h': bool,       # T0~T0+1h 是否给予抗生素
            'abx_in_3h': bool,       # T0~T0+3h 是否给予抗生素
            'culture_in_1h': bool,   # T0~T0+1h 是否留取血培养
            'abx_details': [...],    # 抗生素明细
            'fluid_details': [...],  # 液体明细
            'culture_details': [...],# 血培养明细
        }}
    """
    result = {}
    if not dc_pids:
        return result

    dc = get_client("DataCenter")["DataCenter"]
    sc = get_client("SmartCare")["SmartCare"]

    # Step 1: DC pid → mrn 映射
    dc_pts = list(dc.VI_ICU_ZYBR.find(
        {'pid': {'$in': dc_pids}},
        {'pid': 1, 'mrn': 1, 'name': 1}
    ))
    mrn_to_dc = {}
    dc_to_mrn = {}
    for pt in dc_pts:
        dp = str(pt['pid'])
        mrn = pt.get('mrn')
        if mrn:
            mrn_to_dc[str(mrn)] = dp
            dc_to_mrn[dp] = str(mrn)
            result[dp] = {'sc_pid': None}

    if not mrn_to_dc:
        return result

    # Step 2: mrn → SC _id 映射
    mrns = list(mrn_to_dc.keys())
    sc_patients = list(sc.patient.find(
        {'mrn': {'$in': mrns}},
        {'_id': 1, 'mrn': 1}
    ))
    sc_pid_set = set()
    mrn_to_sc = {}
    for sp in sc_patients:
        sc_pid = str(sp['_id'])
        mrn = str(sp.get('mrn', ''))
        mrn_to_sc[mrn] = sc_pid
        sc_pid_set.add(sc_pid)

    # 回填 sc_pid
    for dp in result:
        mrn = dc_to_mrn.get(dp)
        if mrn and mrn in mrn_to_sc:
            result[dp]['sc_pid'] = mrn_to_sc[mrn]

    # Step 3: 批量查 SC drugExe（抗生素 + 液体）
    abx_keywords = [
        '头孢', '培南', '青霉', '万古', '阿奇', '左氧', '莫西',
        '甲硝唑', '奥硝唑', '利奈', '替考', '磷霉素', '美罗',
        '亚胺', '厄他', '哌酮', '他唑', '舒巴坦', '氨苄', '哌拉',
        '阿莫', '克拉', '多西', '米诺', '替加', '达托', '夫西',
        '磺胺', '呋喃', '吡哌', '诺氟', '环丙', '氧氟',
    ]
    fluid_keywords = [
        '氯化钠', '林格', '盐水', '白蛋白', '羟乙基', '葡萄糖',
        '注射液', '氨基酸', '脂肪乳', '碳酸氢钠',
    ]

    # 为每个 SC pid 查询 drugExe（时间窗口取最大6h）
    for dp, info in result.items():
        sc_pid = info.get('sc_pid')
        if not sc_pid:
            continue
        t0 = t0_map.get(dp)
        if not t0:
            continue

        # 确保 t0 是 datetime
        if isinstance(t0, str):
            try:
                t0 = datetime.fromisoformat(t0)
            except (ValueError, TypeError):
                continue

        # drugExe.startTime 是 naive，统一用 naive
        if t0.tzinfo is not None:
            t0 = t0.replace(tzinfo=None)

        t0_6h = t0 + timedelta(hours=6)

        # 查 drugExe
        drug_docs = list(sc.drugExe.find(
            {'pid': sc_pid, 'startTime': {'$gte': t0, '$lte': t0_6h}},
            {'drugList': 1, 'startTime': 1, '_id': 0}
        ))

        abx_list = []
        fluid_list = []
        for d in drug_docs:
            st = d.get('startTime')
            if not st:
                continue
            hours_from_t0 = (st - t0).total_seconds() / 3600

            for dl in d.get('drugList', []):
                name = str(dl.get('name', ''))
                dose = dl.get('dose')
                unit = dl.get('unit', '')
                liquid = dl.get('liquidAmount')

                is_abx = any(kw in name for kw in abx_keywords)
                is_fluid = any(kw in name for kw in fluid_keywords)

                # 排除非抗生素药物（肝素、抗凝、营养等）
                non_abx_keywords = ['肝素', '低分子', '华法林', '阿司匹林', '氯吡格雷',
                                    '维生素', '氯化钾', '胰岛素', '碳酸氢钠', '呋塞米',
                                    '甘露醇', '地塞米松', '甲泼尼龙', '丙泊酚', '咪达唑仑',
                                    '芬太尼', '瑞芬太尼', '右美托咪定', '吗啡', '布洛芬',
                                    '对乙酰氨基酚', '皮试', '皮试剂']
                is_real_abx = is_abx and not any(kw in name for kw in non_abx_keywords)

                if is_real_abx:
                    abx_list.append({
                        'name': name, 'dose': dose, 'unit': unit,
                        'time': st, 'hours': round(hours_from_t0, 2)
                    })
                if is_fluid:
                    fluid_list.append({
                        'name': name, 'dose': dose, 'unit': unit,
                        'liquid_ml': liquid, 'time': st,
                        'hours': round(hours_from_t0, 2)
                    })

        info['abx_details'] = abx_list
        info['fluid_details'] = fluid_list
        info['abx_in_1h'] = any(a['hours'] <= 1.0 for a in abx_list)
        info['abx_in_3h'] = any(a['hours'] <= 3.0 for a in abx_list)
        info['fluids_3h_ml'] = sum(
            f['liquid_ml'] for f in fluid_list
            if f['hours'] <= 3.0 and f.get('liquid_ml')
        )
        info['fluids_6h_ml'] = sum(
            f['liquid_ml'] for f in fluid_list
            if f['hours'] <= 6.0 and f.get('liquid_ml')
        )

    # Step 4: 查 DC VI_ICU_ZYYZ 血培养
    # 批量查所有 dc_pids 的血培养
    culture_docs = list(dc.VI_ICU_ZYYZ.find(
        {
            'pid': {'$in': dc_pids},
            'orderName': {'$regex': '血培养'}
        },
        {'pid': 1, 'orderName': 1, 'orderTime': 1, 'status': 1, '_id': 0}
    ))

    for cd in culture_docs:
        dp = str(cd.get('pid', ''))
        if dp not in result:
            continue
        t0 = t0_map.get(dp)
        if not t0:
            continue
        if isinstance(t0, str):
            try:
                t0 = datetime.fromisoformat(t0)
            except (ValueError, TypeError):
                continue
        if t0.tzinfo is not None:
            t0 = t0.replace(tzinfo=None)

        ct = cd.get('orderTime')
        if not ct:
            continue
        hours_from_t0 = (ct - t0).total_seconds() / 3600

        if 'culture_details' not in result[dp]:
            result[dp]['culture_details'] = []
        result[dp]['culture_details'].append({
            'name': cd.get('orderName', ''),
            'time': ct,
            'hours': round(hours_from_t0, 2),
            'status': cd.get('status', '')
        })

    # 回填 culture_in_1h（只算 T0 之后的血培养）
    for dp, info in result.items():
        cultures = info.get('culture_details', [])
        info['culture_in_1h'] = any(0 <= c['hours'] <= 1.0 for c in cultures)

    return result


def judge_bundle_for_dc_patient(dc_pid, t0, component, sc_pid, eval_time=None):
    """
    为单个 DC 患者执行 Bundle 完成判定。

    Args:
        dc_pid: DC pid (str)
        t0: T0 时间 (datetime)
        component: get_bundle_components_from_dc 返回的单个患者数据
        sc_pid: SC pid (str)
        eval_time: 评估时间，默认 T0+24h

    Returns:
        dict: bundle_engine 判定结果 + 组件明细
    """
    from scoring.bundle_engine import (
        judge_1h_bundle, judge_3h_bundle, judge_6h_bundle, judge_bundle_finish
    )

    if not t0 or not sc_pid:
        return {'bundle_complete': None, 'reason': 'missing_t0_or_sc_pid'}

    # 确保 naive datetime（与数据库一致）
    if t0.tzinfo is not None:
        t0 = t0.replace(tzinfo=None)

    if eval_time is None:
        eval_time = t0 + timedelta(hours=24)
    if eval_time.tzinfo is not None:
        eval_time = eval_time.replace(tzinfo=None)

    sc = get_client("SmartCare")["SmartCare"]

    # 查乳酸 (bGATemp bedsides code=param_bg_Lac)
    t0_6h = t0 + timedelta(hours=6)
    bga_docs = list(sc.bGATemp.find(
        {
            'eventExe.pid': sc_pid,
            'bedsides': {
                '$elemMatch': {
                    'code': 'param_bg_Lac',
                    'valid': 'valid',
                    'time': {'$gte': t0 - timedelta(hours=2), '$lte': t0_6h}
                }
            }
        },
        {'bedsides.$': 1, '_id': 0}
    ))

    lac_values = []
    for doc in bga_docs:
        for bs in doc.get('bedsides', []):
            if bs.get('code') == 'param_bg_Lac' and bs.get('valid') == 'valid':
                lac_val = bs.get('fVal')
                lac_time = bs.get('time')
                if lac_val is not None and lac_time:
                    lac_values.append({'value': lac_val, 'time': lac_time})

    lac_values.sort(key=lambda x: x['time'])

    # 初始乳酸（T0前后2h内最近的）
    initial_lac = None
    initial_lac_time = None
    for lv in lac_values:
        if lv['time'] <= t0 + timedelta(hours=1):
            initial_lac = lv['value']
            initial_lac_time = lv['time']

    # 复测乳酸（T0后1h之后的）
    recheck_lac = None
    recheck_lac_time = None
    for lv in lac_values:
        if lv['time'] > t0 + timedelta(hours=1):
            recheck_lac = lv['value']
            recheck_lac_time = lv['time']
            break

    # 从 component 提取数据
    abx_details = component.get('abx_details', [])
    culture_details = component.get('culture_details', [])
    fluid_details = component.get('fluid_details', [])

    # 抗生素：取 T0 后最早的
    abx_in_window = [a for a in abx_details if a['hours'] >= 0]
    first_abx = min(abx_in_window, key=lambda x: x['hours']) if abx_in_window else None
    antibiotic_done = bool(first_abx)
    antibiotic_time = first_abx['time'] if first_abx else None

    # 血培养：取 T0 后最早的
    culture_in_window = [c for c in culture_details if c['hours'] >= 0]
    first_culture = min(culture_in_window, key=lambda x: x['hours']) if culture_in_window else None
    culture_done = bool(first_culture)
    culture_time = first_culture['time'] if first_culture else None

    # 液体量
    fluids_3h = component.get('fluids_3h_ml', 0)
    fluids_6h = component.get('fluids_6h_ml', 0)

    # 调用 bundle_engine
    judge_1h = judge_1h_bundle(
        t0=t0, eval_time=eval_time,
        blood_culture_done=culture_done,
        blood_culture_time=culture_time,
        lactate_measured=initial_lac is not None,
        lactate_time=initial_lac_time,
        antibiotic_done=antibiotic_done,
        antibiotic_time=antibiotic_time,
        has_shock=True,
    )

    judge_3h = judge_3h_bundle(
        t0=t0, eval_time=eval_time,
        lactate_measured=initial_lac is not None,
        lactate_time=initial_lac_time,
        lactate_recheck=recheck_lac is not None,
        lactate_recheck_time=recheck_lac_time,
        blood_culture_done=culture_done,
        blood_culture_time=culture_time,
        antibiotic_done=antibiotic_done,
        antibiotic_time=antibiotic_time,
        fluid_30mlkg_done=fluids_3h >= 1500,  # 粗估 50kg × 30ml/kg
        fluid_30mlkg_time=t0 + timedelta(hours=3) if fluids_3h >= 1500 else None,
    )

    judge_6h = judge_6h_bundle(
        t0=t0, eval_time=eval_time,
        has_shock=True,
        map_target_met=False,
        map_target_met_time=None,
        vasopressor_started=False,
        vasopressor_start_time=None,
        initial_lactate=initial_lac,
        lactate_recheck_done=recheck_lac is not None,
        lactate_recheck_time=recheck_lac_time,
        fluid_1500_done=fluids_6h >= 1500,
        fluid_1500_time=t0 + timedelta(hours=6) if fluids_6h >= 1500 else None,
    )

    finish = judge_bundle_finish(
        t0=t0, has_shock=True,
        judge_1h=judge_1h,
        judge_3h=judge_3h,
        judge_6h=judge_6h,
    )

    return {
        **finish,
        'judge_1h': judge_1h,
        'judge_3h': judge_3h,
        'judge_6h': judge_6h,
        'initial_lactate': initial_lac,
        'initial_lactate_time': initial_lac_time,
        'recheck_lactate': recheck_lac,
        'fluids_3h_ml': fluids_3h,
        'fluids_6h_ml': fluids_6h,
        'abx_count': len(abx_details),
        'culture_count': len(culture_details),
        't0': t0,
    }


def judge_bundle_v3_for_patient(sc_pid: str, dc_pid: str, mrn: str, t0: datetime,
                                 diagnosis_text: str, eval_time=None,
                                 batch_lab_cache: dict = None) -> dict:
    """
    V3 版本的单患者 Bundle 全量判定。

    接线所有数据源：
    - S1: bGATemp param_bg_P/Fratio (窗口内最低值) — 按 mrn 查
    - S2: score gcsScore (total) 或 bedside param_score_gcs_obs (窗口内最低值)
    - S3: bedside param_nibp_m (窗口内最低值)
    - S4/K2: drugExe 升压药匹配 VASO_WIDE
    - I1: 诊断文本关键词匹配
    - I2: drugExe 抗生素
    - I3: VI_ICU_ZYYZ 病原学送检
    - K1/A1: bGATemp param_bg_Lac — 按 mrn 查
    - B1: drugExe 抗生素时间
    - B2: VI_ICU_ZYYZ 血培养时间
    - C1: 同 S3
    - C2: bGATemp param_bg_Lac (最高值)
    - C3-1h/C3-3h: drugExe 液体量

    Args:
        sc_pid: SmartCare patient _id (str)
        dc_pid: DataCenter VI_ICU_ZYBR pid (str)
        mrn: 病案号 (str)，用于 bGATemp 查询
        t0: T0 时间 (datetime)
        diagnosis_text: 诊断文本 (管道符分隔)
        eval_time: 评估时间，默认 T0+24h
        batch_lab_cache: 批量预加载的检验数据缓存 {his_pid: {observations, status, ...}}

    Returns:
        dict: 完整 v3 判定结果
    """
    from scoring.bundle_engine import (
        judge_bundle_v3, _classify_vasopressor,
        CRYSTALLOID_KEYWORDS, COLLOID_KEYWORDS,
    )

    if not t0:
        return {"finish": None, "reason": "NO_T0"}

    # 确保 naive datetime
    if t0.tzinfo is not None:
        t0 = t0.replace(tzinfo=None)
    if eval_time is None:
        eval_time = t0 + timedelta(hours=24)
    if eval_time.tzinfo is not None:
        eval_time = eval_time.replace(tzinfo=None)

    sc = get_client("SmartCare")["SmartCare"]
    try:
        dc = get_client("DataCenter")["DataCenter"]
    except Exception:
        dc = None

    t0_1h = t0 + timedelta(hours=1)
    t0_3h = t0 + timedelta(hours=3)
    t0_6h = t0 + timedelta(hours=6)

    # ---- S1: 氧合指数 P/F ratio (bGATemp 用 mrn 查询) ----
    pf_ratio_min = None
    pf_ratio_time = None
    try:
        bga_pf = list(sc.bGATemp.find(
            {'mrn': mrn,
             'bedsides': {'$elemMatch': {'code': 'param_bg_P/Fratio', 'valid': 'valid',
                                          'time': {'$gte': t0, '$lte': t0_6h}}}},
            {'bedsides': 1}
        ))
        for doc in bga_pf:
            for bs in doc.get('bedsides', []):
                val = bs.get('fVal')
                t = bs.get('time')
                if val is not None:
                    if pf_ratio_min is None or val < pf_ratio_min:
                        pf_ratio_min = val
                        pf_ratio_time = t
    except Exception:
        pass

    # ---- S2: GCS ----
    gcs_min = None
    gcs_time = None
    try:
        # 优先从 score 表取 (scoreType='gcsScore', total 字段)
        gcs_score = sc.score.find_one(
            {'pid': sc_pid, 'scoreType': 'gcsScore', 'valid': True,
             'time': {'$gte': t0, '$lte': t0_6h}},
            sort=[('time', 1)]
        )
        if gcs_score and gcs_score.get('total') is not None:
            gcs_min = int(gcs_score['total'])
            gcs_time = gcs_score.get('time')
        else:
            # 兜底: bedside param_score_gcs_obs (格式 E2V2M3)
            gcs_doc = sc.bedside.find_one(
                {'pid': sc_pid, 'code': 'param_score_gcs_obs', 'valid': True,
                 'time': {'$gte': t0, '$lte': t0_6h}},
                sort=[('time', 1)]
            )
            if gcs_doc:
                gcs_str = str(gcs_doc.get('strVal', ''))
                gcs_time = gcs_doc.get('time')
                # 尝试直接数值
                try:
                    gcs_min = int(float(gcs_str.strip()))
                except (ValueError, TypeError):
                    # 解析 E2V2M3 格式
                    import re
                    e_match = re.search(r'E(\d+)', gcs_str, re.IGNORECASE)
                    v_match = re.search(r'V(\d+)', gcs_str, re.IGNORECASE)
                    m_match = re.search(r'M(\d+)', gcs_str, re.IGNORECASE)
                    if e_match and v_match and m_match:
                        gcs_min = int(e_match.group(1)) + int(v_match.group(1)) + int(m_match.group(1))
    except Exception:
        pass

    # ---- S3/C1: MAP (无创平均动脉压 param_nibp_m) ----
    map_min = None
    map_time = None
    map_values = []
    try:
        map_docs = list(sc.bedside.find(
            {'pid': sc_pid, 'code': 'param_nibp_m', 'valid': True,
             'time': {'$gte': t0, '$lte': t0_6h}},
            {'strVal': 1, 'time': 1}
        ).limit(500))
        for md in map_docs:
            try:
                val = float(str(md.get('strVal', '0')).strip())
                if val > 0:
                    map_values.append({'value': val, 'time': md.get('time')})
                    if map_min is None or val < map_min:
                        map_min = val
                        map_time = md.get('time')
            except (ValueError, TypeError):
                pass
    except Exception:
        pass

    # ---- S4/K2: 血管活性药 ----
    # #修复: T0前开始并持续的血管活性药应该被识别
    has_vasopressor = False
    vaso_name = None
    vaso_start_time = None
    vaso_is_prestarter = False  # 标记是否为T0前开始的用药
    vasopressor_status = "inactive"  # active / inactive / unknown
    try:
        # 查询T0前后24h内的血管活性药记录
        # 这样可以捕获T0前开始并持续的用药
        t0_minus_24h = t0 - timedelta(hours=24)
        vaso_docs = list(sc.drugExe.find(
            {'pid': sc_pid, 'startTime': {'$gte': t0_minus_24h, '$lte': t0_6h}},
            {'drugList.name': 1, 'startTime': 1, 'drugActionList': 1}
        ).sort('startTime', 1).limit(500))
        for vd in vaso_docs:
            st = vd.get('startTime')
            if not st:
                continue
            for dl in vd.get('drugList', []):
                name = str(dl.get('name', ''))
                in_wide, in_strict = _classify_vasopressor(name)
                if in_wide or in_strict:
                    actions = vd.get('drugActionList') or []
                    # 检查是否在T0时仍在使用
                    is_prestarter = st <= t0
                    if is_prestarter:
                        # 无动作记录 → 状态未知 (不得默认为活跃或不活跃)
                        if not actions:
                            vasopressor_status = "unknown"
                            vaso_name = name
                            vaso_start_time = st
                            break
                        # 检查是否有停止记录
                        has_stop = False
                        for action in actions:
                            action_type = action.get('type', '')
                            if action_type in ['停止', '暂停', '取消']:
                                has_stop = True
                                break
                        if not has_stop:
                            has_vasopressor = True
                            vasopressor_status = "active"
                            vaso_name = name
                            vaso_start_time = st
                            vaso_is_prestarter = True
                            break
                    else:
                        # T0后开始的用药
                        has_vasopressor = True
                        vasopressor_status = "active"
                        vaso_name = name
                        vaso_start_time = st
                        vaso_is_prestarter = False
                        break
            if has_vasopressor or vasopressor_status == "unknown":
                break
    except Exception:
        pass

    # ---- K1/A1/C2: 乳酸 (bGATemp 用 mrn 查询) ----
    lactate_initial = None
    lactate_initial_time = None
    lactate_max = None
    lactate_recheck_time = None
    lactate_recheck_value = None
    lactate_all = []  # 1h—3h 乳酸完整记录
    lac_values = []
    try:
        bga_lac = list(sc.bGATemp.find(
            {'mrn': mrn,
             'bedsides': {'$elemMatch': {'code': 'param_bg_Lac', 'valid': 'valid',
                                          'time': {'$gte': t0 - timedelta(hours=2), '$lte': t0_6h}}}},
            {'bedsides': 1}
        ))
        lac_values = []
        for doc in bga_lac:
            for bs in doc.get('bedsides', []):
                if bs.get('code') == 'param_bg_Lac' and bs.get('valid') == 'valid':
                    val = bs.get('fVal')
                    t = bs.get('time')
                    if val is not None and t:
                        lac_values.append({'value': val, 'time': t})
        lac_values.sort(key=lambda x: x['time'])

        # 初始乳酸（T0 前后2h内，T0之前的最近值）
        for lv in lac_values:
            if lv['time'] <= t0 + timedelta(hours=1):
                lactate_initial = lv['value']
                lactate_initial_time = lv['time']

        # 最高乳酸（T0~T0+6h）
        lac_in_window = [lv for lv in lac_values if t0 <= lv['time'] <= t0_6h]
        if lac_in_window:
            lactate_max = max(lv['value'] for lv in lac_in_window)
            if lactate_initial is None:
                lactate_initial = lac_in_window[0]['value']
                lactate_initial_time = lac_in_window[0]['time']

        # 复测乳酸（T0+1h后的乳酸）
        lac_recheck = [lv for lv in lac_values if lv['time'] >= t0 + timedelta(hours=1)]
        if lac_recheck:
            lactate_recheck_value = lac_recheck[-1]['value']
            lactate_recheck_time = lac_recheck[-1]['time']

        # 构建乳酸完整记录列表（去重依据: 原始 time 字段 + value）
        _seen_lac = set()
        t0_1h = t0 + timedelta(hours=1)
        t0_3h = t0 + timedelta(hours=3)
        for lv in lac_values:
            t = lv['time']
            val = lv['value']
            # 去重: 同一采样时间 + 同一原始值视为重复
            dedup_key = (t, val)
            if dedup_key in _seen_lac:
                continue
            _seen_lac.add(dedup_key)
            minutes_from_t0 = round((t - t0).total_seconds() / 60, 1)
            # 所属时段: 使用 (T0+1h, T0+3h] 分组，首次乳酸在 [T0-2h, T0+1h]
            if t <= t0_1h:
                period_label = "首次"
            elif t <= t0_3h:
                period_label = "1h—3h"
            else:
                period_label = "3h—6h"
            # 有效状态: 在 T0-2h ~ T0+6h 范围内且 valid=valid
            lactate_all.append({
                'value': round(val, 2),
                'raw_value': val,
                'unit': 'mmol/L',
                'sample_time': fmt_bj(t, "%Y-%m-%dT%H:%M:%S"),
                'time_basis': '采样时间',
                'source': 'bGATemp(血气分析)',
                'minutes_from_t0': minutes_from_t0,
                'period_label': period_label,
                'valid': True,
            })
    except Exception:
        pass

    # ---- I2/B1: 抗生素 ----
    has_antibiotic = False
    antibiotic_time = None
    antibiotic_name = None
    try:
        # 关键词单一来源: scoring/bundle_cg.py（内容与原硬编码逐字一致）
        from scoring.bundle_cg import ABX_KEYWORDS as abx_keywords
        from scoring.bundle_cg import NON_ABX_KEYWORDS as non_abx
        drug_docs = list(sc.drugExe.find(
            {'pid': sc_pid, 'startTime': {'$gte': t0, '$lte': t0_6h}},
            {'drugList.name': 1, 'startTime': 1}
        ).sort('startTime', 1))
        for dd in drug_docs:
            st = dd.get('startTime')
            if not st:
                continue
            for dl in dd.get('drugList', []):
                name = str(dl.get('name', ''))
                is_abx = any(kw in name for kw in abx_keywords)
                is_real = is_abx and not any(kw in name for kw in non_abx)
                if is_real:
                    has_antibiotic = True
                    if antibiotic_time is None:
                        antibiotic_time = st
                        antibiotic_name = name
                    break
            if has_antibiotic:
                break
    except Exception:
        pass

    # ---- I3/B2: 病原学送检 / 血培养 ----
    # #修复: I3和B2应该区分血培养和其他培养
    # I3: 病原学送检（包括血培养、痰培养、尿培养等）
    # B2: 血培养（仅血培养）
    # cg 变体: 按 VI_ICU_ZYYZ 医嘱名识别 — 痰培养=含"细菌培养"、血培养=含"血培养鉴定"
    if BUNDLE_HOSPITAL_VARIANT == "cg":
        _cult_regex = (CG_ORDER_KEYWORDS["sputum_culture"] + "|"
                       + CG_ORDER_KEYWORDS["blood_culture"])
        _blood_kw = CG_ORDER_KEYWORDS["blood_culture"]
    else:
        _cult_regex = ('血培养|痰培养|尿培养|细菌培养|真菌培养|分泌物培养'
                       '|引流液培养|胸水培养|腹水培养|脑脊液培养|导管培养')
        _blood_kw = '血培养'
    has_culture = False
    culture_time = None
    culture_name = None
    has_blood_culture = False
    blood_culture_time = None
    blood_culture_name = None
    try:
        if dc is not None:
            # 查询所有病原学送检记录
            culture_docs = list(dc.VI_ICU_ZYYZ.find(
                {'pid': dc_pid, 'orderName': {'$regex': _cult_regex}},
                {'orderName': 1, 'orderTime': 1}
            ).sort('orderTime', 1))
            for cd in culture_docs:
                ct = cd.get('orderTime')
                order_name = cd.get('orderName', '')
                if ct and ct >= t0:
                    # I3: 任何病原学送检都算
                    if not has_culture:
                        has_culture = True
                        culture_time = ct
                        culture_name = order_name
                    # B2: 只有血培养才算
                    if _blood_kw in order_name and not has_blood_culture:
                        has_blood_culture = True
                        blood_culture_time = ct
                        blood_culture_name = order_name
    except Exception:
        pass

    # ---- cg 变体: 血气识别（VI_ICU_ZYYZ 医嘱名含"ICU血气"） ----
    # 供 A1（乳酸测定）窗口判定；乳酸数值仍取 bGATemp（有则优先生效）
    blood_gas_times = []
    if BUNDLE_HOSPITAL_VARIANT == "cg":
        try:
            if dc is not None:
                bg_docs = dc.VI_ICU_ZYYZ.find(
                    {'pid': dc_pid,
                     'orderName': {'$regex': CG_ORDER_KEYWORDS['blood_gas']}},
                    {'orderTime': 1, '_id': 0}
                ).sort('orderTime', 1)
                blood_gas_times = [b.get('orderTime') for b in bg_docs
                                   if b.get('orderTime')]
        except Exception:
            pass

    # ---- C3: 液体 ----
    has_fluid_1h = False
    fluid_3h_ml = 0
    fluid_6h_ml = 0
    try:
        fluid_keywords_list = list(CRYSTALLOID_KEYWORDS | COLLOID_KEYWORDS)
        fluid_docs = list(sc.drugExe.find(
            {'pid': sc_pid, 'startTime': {'$gte': t0, '$lte': t0_6h}},
            {'drugList.name': 1, 'drugList.liquidAmount': 1, 'startTime': 1}
        ))
        for fd in fluid_docs:
            st = fd.get('startTime')
            if not st:
                continue
            hours = (st - t0).total_seconds() / 3600
            for dl in fd.get('drugList', []):
                name = str(dl.get('name', ''))
                is_fluid = any(kw in name for kw in fluid_keywords_list)
                if is_fluid:
                    if hours <= 1:
                        has_fluid_1h = True
                    liquid = dl.get('liquidAmount')
                    if liquid:
                        try:
                            liq_ml = float(liquid)
                            if hours <= 3:
                                fluid_3h_ml += liq_ml
                            if hours <= 6:
                                fluid_6h_ml += liq_ml
                        except (ValueError, TypeError):
                            pass
    except Exception:
        pass

    # ---- G-1: 查询感染部位确认状态 ----
    site_confirmed = False
    try:
        from config.indicator_windows import SITE_REQUIRED as _SITE_REQUIRED
        if _SITE_REQUIRED:
            ek = build_exclusion_key(dc_pid, t0)
            site_doc = get_infection_site(ek)
            if site_doc and site_doc.get("primary_site"):
                site_confirmed = True
    except Exception:
        pass

    # ---- 调用 v3 引擎 ----
    # #8: 1h 和 3h 各自独立提供数据
    # #修复: w1h和w3h窗口数据应该独立，不能共享相同的数据
    # #修复: B2血培养应该使用blood_culture_time，而不是culture_time
    # Calculate evidence from raw records after applying each deadline.  The
    # earlier implementation first calculated a 6h extreme and then tested
    # only the timestamp that happened to own that extreme, which allowed a
    # 5h MAP/lactate to contaminate 1h/3h decisions.
    def _window_values(records, end):
        return [r for r in records if t0 <= r.get('time') <= min(end, eval_time)]

    lac_1h = _window_values(lac_values, t0_1h)
    lac_3h = _window_values(lac_values, t0_3h)
    map_1h = _window_values(map_values, t0_1h)
    map_3h = _window_values(map_values, t0_3h)
    abx_1h = antibiotic_time if antibiotic_time and t0 <= antibiotic_time <= min(t0_1h, eval_time) else None
    abx_3h = antibiotic_time if antibiotic_time and t0 <= antibiotic_time <= min(t0_3h, eval_time) else None
    blood_1h = blood_culture_time if blood_culture_time and t0 <= blood_culture_time <= min(t0_1h, eval_time) else None
    blood_3h = blood_culture_time if blood_culture_time and t0 <= blood_culture_time <= min(t0_3h, eval_time) else None

    # 6h 窗口数据
    lac_6h = _window_values(lac_values, t0_6h)
    map_6h = _window_values(map_values, t0_6h)
    abx_6h = antibiotic_time if antibiotic_time and t0 <= antibiotic_time <= min(t0_6h, eval_time) else None
    blood_6h = blood_culture_time if blood_culture_time and t0 <= blood_culture_time <= min(t0_6h, eval_time) else None

    # cg 变体: 血气医嘱窗口内首个 orderTime（供 A1 判定）
    def _bg_first(end):
        if not blood_gas_times:
            return None
        lim = min(end, eval_time)
        return next((t for t in blood_gas_times if t0 <= t <= lim), None)

    bg_1h = _bg_first(t0_1h)
    bg_3h = _bg_first(t0_3h)
    bg_6h = _bg_first(t0_6h)

    patient_data = {
        't0': t0,
        'eval_time': eval_time,
        # 门控数据（全窗口，用于 S1-S4 / I1-I3 / K1-K2）
        'diagnosis_text': diagnosis_text,
        'has_antibiotic': has_antibiotic,
        'has_culture': has_culture,
        'has_vasopressor': has_vasopressor,
        'vasopressor_status': vasopressor_status,
        'site_confirmed': site_confirmed,
        'lactate_initial': lactate_initial,
        'gcs_min': gcs_min,
        'pf_ratio_min': pf_ratio_min,
        'map_min': map_min,
        # 1h 窗口数据（用于 A1/B1/B2/B3/C1/C2/C3）
        'w1h': {
            'lactate_initial': lac_1h[0]['value'] if lac_1h else None,
            'lactate_max': max((x['value'] for x in lac_1h), default=None),
            'map_min': min((x['value'] for x in map_1h), default=None),
            'antibiotic_time': abx_1h,
            'culture_time': blood_1h,
            'has_fluid': has_fluid_1h,
        },
        # 3h 窗口数据
        'w3h': {
            'lactate_initial': lac_3h[0]['value'] if lac_3h else None,
            'lactate_max': max((x['value'] for x in lac_3h), default=None),
            'map_min': min((x['value'] for x in map_3h), default=None),
            'antibiotic_time': abx_3h,
            'culture_time': blood_3h,
            'fluid_ml': fluid_3h_ml,
        },
        # 6h 窗口数据
        'w6h': {
            'lactate_initial': lac_6h[0]['value'] if lac_6h else None,
            'lactate_max': max((x['value'] for x in lac_6h), default=None),
            'map_min': min((x['value'] for x in map_6h), default=None),
            'antibiotic_time': abx_6h,
            'culture_time': blood_6h,
            'fluid_ml': fluid_6h_ml,
            'has_lactate_recheck': lactate_recheck_value is not None,
            'lactate_recheck_value': lactate_recheck_value,
        },
    }
    if BUNDLE_HOSPITAL_VARIANT == "cg":
        patient_data['w1h']['blood_gas_time'] = bg_1h
        patient_data['w3h']['blood_gas_time'] = bg_3h
        patient_data['w6h']['blood_gas_time'] = bg_6h

    result = judge_bundle_v3(patient_data) or {}

    # Gate fields are encounter-level evidence and remain available to the
    # existing detail schema.  Window-specific fields stay under bundle_1h /
    # bundle_3h and are never flattened into one ambiguous result.
    result.update(result.get('gate', {}) or {})

    # 附加原始数据明细
    result.update({
        't0': t0,
        'sc_pid': sc_pid,
        'dc_pid': dc_pid,
        'mrn': mrn,
        'pf_ratio_min': pf_ratio_min,
        'pf_ratio_time': pf_ratio_time,
        'gcs_min': gcs_min,
        'gcs_time': gcs_time,
        'map_min': map_min,
        'map_time': map_time,
        'lactate_initial': lactate_initial,
        'lactate_initial_time': lactate_initial_time,
        'lactate_max': lactate_max,
        'lactate_recheck_value': lactate_recheck_value,
        'lactate_recheck_time': lactate_recheck_time,
        'lactate_all': lactate_all,
        'antibiotic_time': antibiotic_time,
        'antibiotic_name': antibiotic_name,
        'culture_time': culture_time,
        'culture_name': culture_name,
        'blood_culture_time': blood_culture_time,
        'blood_culture_name': blood_culture_name,
        'has_blood_culture': has_blood_culture,
        'has_fluid_1h': has_fluid_1h,
        'fluid_3h_ml': fluid_3h_ml,
        'has_vasopressor': has_vasopressor,
        'vaso_name': vaso_name,
        'vaso_start_time': vaso_start_time,
    })
    if BUNDLE_HOSPITAL_VARIANT == "cg":
        # cg 变体: 血气识别明细（VI_ICU_ZYYZ 医嘱 orderTime 列表）
        result['blood_gas_times'] = blood_gas_times

    # ---- 正式 SOFA 评分 ----
    try:
        from scoring.sofa_bridge import compute_sofa_scores, build_clinical_layer
        from config.indicator_windows import SOFA_GATE_MODE

        sofa_result = compute_sofa_scores(
            sc_pid=sc_pid, mrn=mrn, dc_pid=dc_pid,
            t0=t0, eval_time=eval_time, weight_kg=None,
            batch_lab_cache=batch_lab_cache,
        )
        result['sofa'] = {
            'classic': sofa_result['classic'],
            'sofa2': sofa_result['sofa2'],
            'eval_time': fmt_bj(eval_time, "%Y-%m-%d %H:%M:%S"),
            't0': fmt_bj(t0, "%Y-%m-%d %H:%M:%S"),
            'version_meta': sofa_result.get('version_meta', {}),
            'data_complete': sofa_result.get('data_complete', True),
        }

        # ---- 临床识别层 ----
        clinical_layer = build_clinical_layer(
            sofa_result=sofa_result,
            infection_evidence={
                'has_infection': result.get('has_infection'),
                'i1': result.get('i1'),
                'i2': result.get('i2'),
                'i3': result.get('i3'),
            },
            has_vasopressor_wide=has_vasopressor,
            lactate_value=lactate_initial,
            map_value=map_min,
            has_fluid_resuscitation=has_fluid_1h,
            vaso_name=vaso_name,
        )
        # 填充 Bundle 完成结果
        clinical_layer['layer5_bundle'] = {
            '1h': result.get('bundle_1h'),
            '3h': result.get('bundle_3h'),
            '6h': None,
        }
        result['clinical_layer'] = clinical_layer

        # ---- 新旧门控对比 (shadow 模式) ----
        if SOFA_GATE_MODE == "shadow":
            old_gate = result.get('gate', {})
            sofa2_score = (sofa_result.get('sofa2') or {}).get('sofa2_score')
            new_organ_dys = (clinical_layer.get('layer2_organ_dysfunction') or {}).get('has_acute_organ_dysfunction')
            old_organ_dys = (old_gate or {}).get('has_organ_dysfunction')
            new_sepsis = (clinical_layer.get('layer3_sepsis') or {}).get('is_sepsis')
            old_shock = (old_gate or {}).get('is_septic_shock')
            new_shock = (clinical_layer.get('layer4_shock') or {}).get('shock_status')

            result['gate_comparison'] = {
                'mode': 'shadow',
                'sofa2_total': sofa2_score,
                'old_organ_dysfunction': old_organ_dys,
                'new_organ_dysfunction': new_organ_dys,
                'organ_dysfunction_changed': old_organ_dys != new_organ_dys,
                'old_is_septic_shock': old_shock,
                'new_shock_status': new_shock,
                'shock_changed': (old_shock is True and new_shock != 'confirmed') or
                                 (old_shock is not True and new_shock == 'confirmed'),
            }
    except Exception as e:
        logger.warning("SOFA scoring failed for sc_pid=%s: %s", sc_pid, e, exc_info=True)
        result['sofa'] = None
        result['clinical_layer'] = None
        result['gate_comparison'] = {'mode': 'shadow', 'error': str(e)}

    # ---- 候选引擎集成 (高召回四通道) ----
    try:
        from scoring.candidate_engine import extract_candidate
        cl = result.get('clinical_layer') or {}
        candidate_info = extract_candidate(
            v3_result=result,
            clinical_layer=cl,
            diagnosis_text=diagnosis_text,
            has_vasopressor_wide=has_vasopressor,
            vasopressor_status=vasopressor_status,
            lactate_value=lactate_initial,
            map_value=map_min,
            has_fluid_resuscitation=has_fluid_1h,
        )
        result['candidate_info'] = candidate_info
        result['candidate_status'] = candidate_info.get('candidate_status', 'not_candidate')
        result['is_septic_shock_candidate'] = candidate_info.get('is_septic_shock_candidate', False)
        result['candidate_pathways'] = candidate_info.get('candidate_pathways', [])
        result['candidate_reasons'] = candidate_info.get('candidate_reasons', [])
        result['clinical_confirmation_status'] = candidate_info.get('clinical_confirmation_status', 'insufficient')
        result['supporting_evidence'] = candidate_info.get('supporting_evidence', [])
        result['missing_evidence'] = candidate_info.get('missing_evidence', [])
        result['conflicting_evidence'] = candidate_info.get('conflicting_evidence', [])
    except Exception as e:
        logger.warning("Candidate engine failed for sc_pid=%s: %s", sc_pid, e, exc_info=True)
        result['candidate_info'] = {'candidate_status': 'not_candidate', 'is_septic_shock_candidate': False}
        result['candidate_status'] = 'not_candidate'
        result['is_septic_shock_candidate'] = False
        result['candidate_pathways'] = []
        result['candidate_reasons'] = []
        result['clinical_confirmation_status'] = 'insufficient'
        result['supporting_evidence'] = []
        result['missing_evidence'] = []
        result['conflicting_evidence'] = []

    return result


# ============================================================
# ICU-06：抗菌药物治疗前病原学送检率（治疗/预防三层判定）
# ============================================================

# ---- 可配置阈值常量 ----
FEVER_TEMP = 38.5               # 发热体温阈值 (℃)
WBC_HIGH = 10.0                 # WBC 升高阈值 (×10⁹/L)
WBC_LOW = 4.0                   # WBC 降低阈值 (×10⁹/L)
CRP_HIGH = 10.0                 # CRP 升高阈值 (mg/L)
PCT_HIGH = 0.5                  # PCT 升高阈值 (ng/mL)
INFLAM_WINDOW_H = 24            # 炎症指标时间窗口 (小时, 首剂前后)
PERIOP_PRE_H = 2                # 围术期术前窗口 (小时)
PERIOP_POST_H = 48              # 围术期术后/总疗程阈值 (小时)
SHORT_COURSE_H = 24             # 短疗程总跨度阈值 (小时)
SHORT_COURSE_MAX_DOSES = 2      # 短疗程最多给药次数
AI_CONFIDENCE_THRESHOLD = 0.6   # AI 低置信度阈值
CRP_AS_PATHOGEN = False         # CRP 默认不计入病原学送检

# ---- 治疗目的诊断关键词（A1 信号） ----
TREATMENT_DIAG_KEYWORDS = [
    "肺炎", "脓毒", "感染", "败血", "菌血", "腹膜炎", "脓肿", "化脓",
    "尿路感染", "胆管炎", "脑膜炎", "蜂窝织炎", "感染性休克",
]

# ---- 特殊使用级抗菌药关键词（A7 信号） ----
# ⚠️ configDrug 表无抗菌药物分级字段，只能用关键词硬编码清单
# 覆盖碳青霉烯类、糖肽类、噁唑烷酮类、环脂肽类、多黏菌素类、抗真菌类、四代头孢等
SPECIAL_GRADE_KEYWORDS = [
    # 碳青霉烯类
    "亚胺培南", "美罗培南", "比阿培南", "帕尼培南", "厄他培南",
    # 糖肽类
    "万古霉素", "去甲万古", "替考拉宁",
    # 噁唑烷酮类
    "利奈唑胺",
    # 甘氨酰环素类
    "替加环素",
    # 多黏菌素类
    "多黏菌素", "多粘菌素",
    # 抗真菌类 (三唑/棘白菌素/多烯)
    "两性霉素", "伏立康唑", "泊沙康唑", "卡泊芬净", "米卡芬净",
    # 环脂肽类
    "达托霉素",
    # 四代头孢
    "头孢吡肟",
    # 其他特殊级
    "夫西地酸",
]

# ---- 病原学送检关键词（源A：VI_ICU_ZYYZ, yaoType='检验'） ----
# ⚠️ 不含宽泛"培养"兜底，避免误命中非病原学检验
CULTURE_KEYWORDS_FULL = [
    "血培养", "痰培养", "尿培养", "细菌培养", "真菌培养",
    "分泌物培养", "引流液培养", "胸水培养", "腹水培养", "脑脊液培养",
    "导管培养", "涂片", "革兰染色", "抗酸染色", "G试验", "GM试验", "药敏",
    "内毒素", "隐球菌", "曲霉", "半乳甘露聚糖", "结核",
    "核酸", "微生物", "细菌",
]
PATHOGEN_REGEX = "|".join(CULTURE_KEYWORDS_FULL)

# ---- 检验结果清洗 ----
def _clean_test_value(raw) -> float | None:
    """
    清洗检验结果值。
    - 去除 > < ≥ ≤ 前缀
    - 跳过 "阴性" "未检出" "正常" "未见异常" 等非数值
    - 返回 float 或 None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    non_numeric = {"", "阴性", "未检出", "正常", "未见异常", "-", "—", "无", "弱阳性",
                   "阴性(-)", "阴性（-）", "(-)", "neg", "negative", "Neg"}
    if s in non_numeric:
        return None
    # 去除比较符号 (含组合如 >=, <=, ≥, ≤ 等)
    for ch in (">=", "<=", "≥", "≤", ">", "<", "="):
        s = s.replace(ch, "")
    # 去除常见单位后缀
    for suffix in ("℃", "°C", "mg/L", "ng/mL", "×10⁹/L", "x10⁹/L",
                   "g/L", "mmol/L", "μmol/L", "U/L", "%", "mm/h"):
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return None


# ---- 炎症指标查询 ----
def get_inflammation_signals(pat_pid: str, pat_hispid: str, first_dose,
                             db_sc, db_dc) -> dict:
    """
    查询首剂±INFLAM_WINDOW_H 内的炎症指标。

    数据源：
      - 体温: SmartCare.bedside, code='param_T', strVal 取数值
      - WBC/CRP/PCT: DataCenter.VI_ICU_EXAM (主表) + VI_ICU_EXAM_ITEM (子表)
        主表 collectTime 为检验时间，子表 itemCode 精确匹配取值。

    ⚠️ 字段陷阱：
      - PCT: 只认 itemCode=='PCT1' 或 itemName=='降钙素原'，itemCode='PCT' 是血小板比容
      - WBC: 只认 itemCode in {'WBC','WBCJS'}，不可用 itemName 模糊（会误命中尿白细胞 NYWBC）

    返回 {fever, fever_val, wbc_high, wbc_low, wbc_val,
           crp_high, crp_val, pct_high, pct_val, has_any_abnormal, details}
    """
    from datetime import timedelta

    window_start = first_dose - timedelta(hours=INFLAM_WINDOW_H)
    window_end = first_dose + timedelta(hours=INFLAM_WINDOW_H)

    result = {
        "fever": False, "fever_val": None,
        "wbc_high": False, "wbc_low": False, "wbc_val": None,
        "crp_high": False, "crp_val": None,
        "pct_high": False, "pct_val": None,
        "has_any_abnormal": False,
        "details": [],
    }

    # ---- 体温 (SmartCare.bedside) ----
    try:
        temp_docs = list(db_sc.bedside.find(
            {"pid": pat_pid, "code": "param_T", "valid": True,
             "time": {"$gte": window_start, "$lte": window_end}},
            {"strVal": 1, "time": 1},
        ).sort("time", -1).limit(5))
        for doc in temp_docs:
            val = _clean_test_value(doc.get("strVal"))
            if val is not None:
                if val >= FEVER_TEMP:
                    result["fever"] = True
                    result["fever_val"] = val
                    result["details"].append(f"体温{val}℃≥{FEVER_TEMP}℃")
                break  # 只取最近一条有效值
    except Exception as e:
        pass  # bedside 不可用则跳过

    # ---- WBC / CRP / PCT (DataCenter.EXAM + EXAM_ITEM) ----
    if pat_hispid and db_dc is not None:
        _query_exam_inflammation(db_dc, pat_hispid, window_start, window_end, result)

    result["has_any_abnormal"] = (
        result["fever"] or result["wbc_high"] or result["wbc_low"]
        or result["crp_high"] or result["pct_high"]
    )
    return result


def _query_exam_inflammation(db_dc, hispid: str, window_start, window_end, result: dict):
    """
    从 DataCenter 查询炎症相关检验指标。
    VI_ICU_EXAM (主表, collectTime) + VI_ICU_EXAM_ITEM (子表, itemCode/itemValue)。

    ✅ 字段实测结论（2026-10-10，两库全量验证 0 文档含 pid）：
      - 主表患者键 = hisPid（原查 "pid" 恒为空 → 检验链路曾整体静默失效）
      - 主表只有 reportID（无 examID），ITEM.examID 实际引用主表 reportID
      - VI_ICU_EXAM.collectTime 为主检验时间
    """
    try:
        # Step 1: 查主表，获取窗口内的检验记录
        exam_docs = list(db_dc["VI_ICU_EXAM"].find(
            # #fix: VI_ICU_EXAM 实际字段为 hisPid（原查 "pid" 恒为空）
            {"hisPid": hispid,
             "collectTime": {"$gte": window_start, "$lte": window_end}},
            {"examID": 1, "reportID": 1, "collectTime": 1},
        ))
        if not exam_docs:
            return

        # 构建 examID 集合 (优先 examID，兜底 reportID)
        exam_ids = []
        exam_time_by_id = {}  # exam_id → collectTime
        for e in exam_docs:
            eid = e.get("examID") or e.get("reportID")
            if eid:
                exam_ids.append(eid)
                exam_time_by_id[str(eid)] = e.get("collectTime")
        if not exam_ids:
            return

        # Step 2: 查子表，精确匹配 itemCode
        WBC_CODES = {"WBC", "WBCJS"}        # 血白细胞 — 绝不可用 itemName 模糊
        CRP_CODES = {"CRP", "sCRP"}          # C反应蛋白
        PCT_CODES = {"PCT1"}                 # ⚠️ 只认 PCT1 — "PCT" 是血小板比容

        target_codes = list(WBC_CODES | CRP_CODES | PCT_CODES)
        items = list(db_dc["VI_ICU_EXAM_ITEM"].find(
            {"examID": {"$in": exam_ids},
             "itemCode": {"$in": target_codes}},
            {"examID": 1, "itemCode": 1, "itemName": 1, "itemValue": 1, "authTime": 1},
        ))

        # Step 3: 逐条判定
        for item in items:
            code = item.get("itemCode", "")
            raw_val = item.get("itemValue")
            val = _clean_test_value(raw_val)
            if val is None:
                continue

            # 时间：优先主表 collectTime，无则子表 authTime
            eid = str(item.get("examID", ""))
            item_time = exam_time_by_id.get(eid) or item.get("authTime")

            if code in WBC_CODES:
                result["wbc_val"] = val
                if val > WBC_HIGH:
                    result["wbc_high"] = True
                    result["details"].append(f"WBC {val}>10×10⁹/L")
                elif val < WBC_LOW:
                    result["wbc_low"] = True
                    result["details"].append(f"WBC {val}<4×10⁹/L")

            elif code in CRP_CODES:
                result["crp_val"] = val
                if val > CRP_HIGH:
                    result["crp_high"] = True
                    result["details"].append(f"CRP {val}>10mg/L")

            elif code in PCT_CODES:
                # 双重保险：itemCode==PCT1 且 itemName 含"降钙素原"才认
                item_name = item.get("itemName", "")
                if "降钙素原" in str(item_name):
                    result["pct_val"] = val
                    if val > PCT_HIGH:
                        result["pct_high"] = True
                        result["details"].append(f"PCT {val}>0.5ng/mL")
                # 纯 itemCode==PCT1 但 itemName 无降钙素原也认（有的系统只存 code）
                elif code == "PCT1" and "血小板" not in str(item_name):
                    result["pct_val"] = val
                    if val > PCT_HIGH:
                        result["pct_high"] = True
                        result["details"].append(f"PCT {val}>0.5ng/mL")

    except Exception:
        pass  # 检验数据不可用则跳过，炎症指标只正向判治疗


# ---- 三层判定：classify_abx_purpose ----
def classify_abx_purpose(pat_doc: dict, first_dose, total_doses: int,
                         total_hours: float, drug_names: list,
                         abx_code_count: int = 0,
                         db_sc=None, db_dc=None,
                         inflammation_signals: dict | None = None) -> dict:
    """
    治疗 vs 预防三层漏斗判定。

    参数：
      pat_doc         - SmartCare.patient 文档 (含 clinicalDiagnosis, patientOperations, hisPid, _id)
      first_dose      - 首剂抗菌药时间 (datetime)
      total_doses     - 总给药次数
      total_hours     - 总疗程跨度 (小时)
      drug_names      - 抗菌药名称列表
      abx_code_count  - 不同抗菌药 code 去重计数 (≥2 即联合用药 A6)
      db_sc, db_dc    - SmartCare / DataCenter 数据库句柄
      inflammation_signals - 预查询的炎症指标 (避免重复查库)

    返回 {purpose: "治疗性"|"预防性", decided_by: "rule"|"ai"|"fallback",
           reason: str, confidence: float, need_review: bool}
    """
    from datetime import timedelta

    hispid = pat_doc.get("hisPid", "")
    pat_pid = str(pat_doc.get("_id", ""))
    diagnosis = pat_doc.get("clinicalDiagnosis", "") or ""
    operations = pat_doc.get("patientOperations") or []

    # ---- 第一层 A: 治疗信号（任一命中即判治疗） ----

    # A1: 临床诊断命中治疗关键词
    for kw in TREATMENT_DIAG_KEYWORDS:
        if kw in diagnosis:
            return {"purpose": "治疗性", "decided_by": "rule",
                    "reason": f"A1-诊断含「{kw}」", "confidence": 1.0,
                    "need_review": False}

    # A2–A5: 炎症指标（预查询结果或现场查询）
    if inflammation_signals is None:
        inflammation_signals = get_inflammation_signals(
            pat_pid, hispid, first_dose, db_sc, db_dc)

    inflam = inflammation_signals

    # A2: 体温 ≥ 38.5℃
    if inflam.get("fever"):
        return {"purpose": "治疗性", "decided_by": "rule",
                "reason": f"A2-首剂±{INFLAM_WINDOW_H}h体温{inflam.get('fever_val')}℃≥{FEVER_TEMP}℃",
                "confidence": 1.0, "need_review": False}

    # A3: WBC 异常 (>10 或 <4)
    if inflam.get("wbc_high"):
        return {"purpose": "治疗性", "decided_by": "rule",
                "reason": f"A3-WBC {inflam.get('wbc_val')}>10×10⁹/L",
                "confidence": 1.0, "need_review": False}
    if inflam.get("wbc_low"):
        return {"purpose": "治疗性", "decided_by": "rule",
                "reason": f"A3-WBC {inflam.get('wbc_val')}<4×10⁹/L",
                "confidence": 1.0, "need_review": False}

    # A4: CRP 升高 >10mg/L
    if inflam.get("crp_high"):
        return {"purpose": "治疗性", "decided_by": "rule",
                "reason": f"A4-CRP {inflam.get('crp_val')}>10mg/L",
                "confidence": 1.0, "need_review": False}

    # A5: PCT 升高 >0.5ng/mL (仅真降钙素原 PCT1)
    if inflam.get("pct_high"):
        return {"purpose": "治疗性", "decided_by": "rule",
                "reason": f"A5-PCT {inflam.get('pct_val')}>0.5ng/mL",
                "confidence": 1.0, "need_review": False}

    # A6: 联合用药（≥2 种不同抗菌药 code）
    if abx_code_count >= 2:
        return {"purpose": "治疗性", "decided_by": "rule",
                "reason": f"A6-联合用药({abx_code_count}种)",
                "confidence": 1.0, "need_review": False}

    # A7: 特殊使用级抗菌药
    for dname in drug_names:
        for kw in SPECIAL_GRADE_KEYWORDS:
            if kw in dname:
                return {"purpose": "治疗性", "decided_by": "rule",
                        "reason": f"A7-特殊级抗菌药:{kw}",
                        "confidence": 1.0, "need_review": False}

    # ---- 第二层 B: 围术期预防 ----
    # 首剂在术前2h~术后 + 总疗程≤48h
    for op in operations:
        op_start = op.get("startTime") or op.get("opTime") or op.get("surgeryTime")
        op_end = op.get("endTime") or op.get("opEndTime")
        if op_start is None:
            continue
        # 术前 2h 窗口
        pre_op = op_start - timedelta(hours=PERIOP_PRE_H)
        # 术后窗口（手术结束时间 or 手术开始+24h）— 用于判断"围术期"范围
        post_op = op_end if op_end else op_start

        if pre_op <= first_dose <= post_op and total_hours <= PERIOP_POST_H:
            return {"purpose": "预防性", "decided_by": "rule",
                    "reason": f"B-围术期预防(术前{PERIOP_PRE_H}h~术后,疗程{total_hours:.0f}h≤{PERIOP_POST_H}h)",
                    "confidence": 1.0, "need_review": False}

    # ---- 第二层 C: 短疗程预防 ----
    # 无 A 信号 + 总跨度≤24h + 次数≤2
    if total_hours <= SHORT_COURSE_H and total_doses <= SHORT_COURSE_MAX_DOSES:
        return {"purpose": "预防性", "decided_by": "rule",
                "reason": f"C-短疗程(跨度{total_hours:.0f}h≤{SHORT_COURSE_H}h, {total_doses}次≤{SHORT_COURSE_MAX_DOSES})",
                "confidence": 1.0, "need_review": False}

    # ---- 第三层: 灰区 AI ----
    # A1-A7 + B + C 均不命中 → AI 判定（结果缓存，不会重复调用）
    try:
        from ai_analyzer import classify_abx_with_ai
        diag = diagnosis[:200] if diagnosis else ""
        ops_summary = ",".join(
            o.get("name", o.get("code", ""))[:40] for o in operations[:3]
        ) if operations else "无"
        drug_summary = ",".join(d[:40] for d in drug_names[:5])
        inflam_summary = ";".join(inflam.get("details", [])) or "无异常"

        ai_result = classify_abx_with_ai({
            "hisPid": hispid,
            "diagnosis": diag,
            "surgery": ops_summary,
            "antibiotics": drug_summary,
            "course_hours": round(total_hours, 1),
            "dose_count": total_doses,
            "inflammation": inflam_summary,
        })
        if ai_result:
            return {
                "purpose": ai_result.get("purpose") or "未判定",
                "decided_by": ai_result.get("by", "ai"),
                "reason": ai_result.get("reason", ""),
                "confidence": ai_result.get("confidence", 0.0),
                "evaluated": ai_result.get("evaluated", ai_result.get("by") != "fallback"),
                "need_review": ai_result.get("need_review", False),
            }
    except Exception:
        pass

    # AI 不可用兜底（仍进 low_confidence 人工复核）
    return {"purpose": "未判定", "decided_by": "fallback",
            "reason": f"规则+AI均不可用兜底(疗程{total_hours:.0f}h/{total_doses}次/{','.join(drug_names[:2])[:30]})",
            "confidence": 0.0, "evaluated": False, "need_review": True}


# ---- 批量预查炎症指标（一次查询覆盖全部患者） ----
def _batch_get_inflammation(abx_by_pid: dict, pat_by_pid: dict,
                            db_sc, db_dc) -> dict:
    """
    批量查询所有候选患者的炎症指标，仅 3~4 次 MongoDB 往返。

    返回: {pid: inflammation_signals_dict}
    """
    from datetime import timedelta

    result = {}
    if not abx_by_pid:
        return result

    # 收集所有患者信息
    pid_hispid_map = {}   # pid → hispid
    pid_first_dose = {}   # pid → first_dose
    for pid in abx_by_pid:
        p = pat_by_pid.get(pid)
        if p:
            pid_hispid_map[pid] = p.get("hisPid", "")
            pid_first_dose[pid] = abx_by_pid[pid]["first_time"]

    if not pid_first_dose:
        return result

    # 计算全局时间窗口（覆盖所有患者）
    all_times = list(pid_first_dose.values())
    global_min = min(all_times) - timedelta(hours=INFLAM_WINDOW_H)
    global_max = max(all_times) + timedelta(hours=INFLAM_WINDOW_H)

    # 初始化空结果
    for pid in abx_by_pid:
        result[pid] = {
            "fever": False, "fever_val": None,
            "wbc_high": False, "wbc_low": False, "wbc_val": None,
            "crp_high": False, "crp_val": None,
            "pct_high": False, "pct_val": None,
            "has_any_abnormal": False, "details": [],
        }

    # ---- Query 1: 批量查 bedside 体温 (SmartCare) — 超时保护 ----
    all_pids = list(pid_first_dose.keys())
    try:
        temp_docs = list(db_sc.bedside.find(
            {"pid": {"$in": all_pids}, "code": "param_T", "valid": True,
             "time": {"$gte": global_min, "$lte": global_max}},
            {"pid": 1, "strVal": 1, "time": 1},
        ).sort("time", -1).max_time_ms(5000))  # 单次查询最多 5 秒

        # 按 pid 分组，取窗口内最高体温
        temp_by_pid = {}
        for doc in temp_docs:
            pid = doc.get("pid", "")
            t = doc.get("time")
            if pid not in pid_first_dose:
                continue
            fd = pid_first_dose[pid]
            if t is None or not (fd - timedelta(hours=INFLAM_WINDOW_H) <= t <= fd + timedelta(hours=INFLAM_WINDOW_H)):
                continue
            val = _clean_test_value(doc.get("strVal"))
            if val is not None:
                if pid not in temp_by_pid or val > temp_by_pid[pid]:
                    temp_by_pid[pid] = val

        for pid, val in temp_by_pid.items():
            if val >= FEVER_TEMP:
                result[pid]["fever"] = True
                result[pid]["fever_val"] = val
                result[pid]["details"].append(f"体温{val}℃≥{FEVER_TEMP}℃")
                result[pid]["has_any_abnormal"] = True
    except Exception:
        pass

    # ---- Query 2-3: 批量查 DataCenter 检验指标 ----
    if db_dc is not None:
        hispids = [h for h in pid_hispid_map.values() if h]
        if hispids:
            try:
                _batch_query_exam_inflammation(
                    db_dc, hispids, pid_hispid_map, pid_first_dose, result)
            except Exception:
                pass

    return result


def _batch_query_exam_inflammation(db_dc, hispids: list,
                                   pid_hispid_map: dict,
                                   pid_first_dose: dict,
                                   result: dict):
    """
    批量查询 DataCenter 检验指标。
    Query 2: VI_ICU_EXAM (主表)
    Query 3: VI_ICU_EXAM_ITEM (子表)
    """
    from datetime import timedelta

    # 计算全局窗口
    all_times = list(pid_first_dose.values())
    global_min = min(all_times) - timedelta(hours=INFLAM_WINDOW_H)
    global_max = max(all_times) + timedelta(hours=INFLAM_WINDOW_H)

    # hispid → pid 反向映射
    hispid_to_pid = {v: k for k, v in pid_hispid_map.items()}

    # Query 2: 批量查主表 — 超时保护 + 结果上限
    try:
        exam_docs = list(db_dc["VI_ICU_EXAM"].find(
            # #fix: VI_ICU_EXAM 实际字段为 hisPid（原查 "pid" 恒为空）
            {"hisPid": {"$in": hispids},
             "collectTime": {"$gte": global_min, "$lte": global_max}},
            {"hisPid": 1, "examID": 1, "reportID": 1, "collectTime": 1},
        ).max_time_ms(8000).limit(20000))
    except Exception:
        return  # 检验数据不可用则降级，炎症指标只正向判治疗
    if not exam_docs:
        return

    # 构建映射
    exam_ids = []
    exam_info = {}  # exam_id_str → {collectTime, hispid}
    for e in exam_docs:
        eid = e.get("examID") or e.get("reportID")
        if eid:
            eid_str = str(eid)
            exam_ids.append(eid)
            exam_info[eid_str] = {
                "collectTime": e.get("collectTime"),
                "hispid": e.get("hisPid", ""),  # #fix: VI_ICU_EXAM 字段为 hisPid
            }

    if not exam_ids:
        return

    # Query 3: 批量查子表
    WBC_CODES = {"WBC", "WBCJS"}
    CRP_CODES = {"CRP", "sCRP"}
    PCT_CODES = {"PCT1"}
    target_codes = list(WBC_CODES | CRP_CODES | PCT_CODES)

    items = list(db_dc["VI_ICU_EXAM_ITEM"].find(
        {"examID": {"$in": exam_ids},
         "itemCode": {"$in": target_codes}},
        {"examID": 1, "itemCode": 1, "itemName": 1, "itemValue": 1, "authTime": 1},
    ).max_time_ms(10000).limit(50000))

    # 按 (pid, itemCode) 分组，取窗口内最异常值
    for item in items:
        code = item.get("itemCode", "")
        raw_val = item.get("itemValue")
        val = _clean_test_value(raw_val)
        if val is None:
            continue

        eid_str = str(item.get("examID", ""))
        info = exam_info.get(eid_str)
        if not info:
            continue

        hispid = info.get("hispid", "")
        pid = hispid_to_pid.get(hispid)
        if pid not in pid_first_dose:
            continue

        # 时间窗口校验
        item_time = info.get("collectTime") or item.get("authTime")
        fd = pid_first_dose[pid]
        if item_time and not (fd - timedelta(hours=INFLAM_WINDOW_H) <= item_time <= fd + timedelta(hours=INFLAM_WINDOW_H)):
            continue

        res = result[pid]

        if code in WBC_CODES:
            res["wbc_val"] = val
            if val > WBC_HIGH:
                res["wbc_high"] = True
                res["has_any_abnormal"] = True
                res["details"].append(f"WBC {val}>10×10⁹/L")
            elif val < WBC_LOW:
                res["wbc_low"] = True
                res["has_any_abnormal"] = True
                res["details"].append(f"WBC {val}<4×10⁹/L")

        elif code in CRP_CODES:
            res["crp_val"] = val
            if val > CRP_HIGH:
                res["crp_high"] = True
                res["has_any_abnormal"] = True
                res["details"].append(f"CRP {val}>10mg/L")

        elif code in PCT_CODES:
            item_name = item.get("itemName", "")
            if "降钙素原" in str(item_name) or ("血小板" not in str(item_name) and code == "PCT1"):
                res["pct_val"] = val
                if val > PCT_HIGH:
                    res["pct_high"] = True
                    res["has_any_abnormal"] = True
                    res["details"].append(f"PCT {val}>0.5ng/mL")


# ---- ICU-06 主取数函数 ----
def get_icu06_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    ICU-06：抗菌药物治疗前病原学送检率。

    分母：统计期内本科室以治疗为目的用抗菌药的患者（按人数去重）。
          - 抗菌药：configDrug.classification=='抗生素' 的 code 集合
          - 治疗目的：三层漏斗判定 (A 治疗信号 → B/C 预防 → AI 灰区)
          - 预防性使用剔除出分母

    分子：分母中首剂抗菌药给药前完成病原学送检的患者（送检时间 ≤ 首剂时间）。
          - 源A：DataCenter.VI_ICU_ZYYZ, yaoType='检验', orderName 含培养类关键词
          - 首剂时间回溯全程（含入 ICU 前），不限统计窗口
          - 时间用 orderTime

    跨库对齐：SmartCare patient.hisPid ↔ DataCenter VI_ICU_ZYYZ.pid

    返回：{den_count, num_count, den_patients, num_patients,
           excluded_prophylaxis, low_confidence}
    """
    from datetime import datetime as dt, timedelta

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)

    result = {
        "den_count": 0, "num_count": 0,
        "den_patients": [], "num_patients": [],
        "excluded_prophylaxis": [],
        "low_confidence": [],
    }

    for db_name, db in iter_bed_dbs():
        try:

            # ---- 1. 取抗生素 drug codes (configDrug.classification='抗生素') ----
            abx_codes = [d["code"] for d in db.configDrug.find(
                {"classification": "抗生素"}, {"code": 1}
            )]
            if not abx_codes:
                continue

            # ---- 2. 在科患者 (统计期内在 ICU 的患者) ----
            patients = list(db.patient.find(
                {"deptCode": {"$in": dept_codes}, "status": {"$ne": "invalid"},
                 "icuAdmissionTime": {"$lte": end_dt_wide},
                 "$or": [{"icuDischargeTime": {"$gte": start_dt}}, {"icuDischargeTime": None},
                         {"icuDischargeTime": {"$exists": False}}]},
                {"_id": 1, "hisPid": 1, "mrn": 1, "name": 1,
                 "clinicalDiagnosis": 1, "patientOperations": 1},
            ))
            pid_set = {str(p["_id"]) for p in patients}
            pat_by_pid = {str(p["_id"]): p for p in patients}
            if not pid_set:
                continue

            # ---- 3. 分母候选：drugExe 统计期内有抗菌药执行记录 ----
            abx_docs = list(db.drugExe.find(
                {"pid": {"$in": list(pid_set)}, "status": "finished",
                 "drugList.code": {"$in": abx_codes},
                 "startTime": {"$gte": start_dt, "$lte": end_dt_wide}},
                {"pid": 1, "startTime": 1, "drugList.code": 1, "drugList.name": 1},
            ).sort("startTime", 1))

            if not abx_docs:
                continue

            # 按 pid 聚合首剂时间、总次数、总跨度、用药名称
            abx_by_pid = {}  # pid → {first_time, last_time, doses, drug_names}
            for d in abx_docs:
                pid = d.get("pid", "")
                t = d.get("startTime")
                if not pid or not t:
                    continue
                if pid not in abx_by_pid:
                    abx_by_pid[pid] = {"first_time": t, "last_time": t, "doses": 0,
                                       "drug_names": set(), "drug_codes": set()}
                entry = abx_by_pid[pid]
                if t < entry["first_time"]:
                    entry["first_time"] = t
                if t > entry["last_time"]:
                    entry["last_time"] = t
                entry["doses"] += 1
                for dl in d.get("drugList", []):
                    if dl.get("code") in abx_codes:
                        entry["drug_names"].add(dl.get("name", "")[:60])
                        entry["drug_codes"].add(dl.get("code"))

            # 回溯全程首剂：对每个候选患者，查统计期前的 drugExe 找真正首剂
            for pid in list(abx_by_pid.keys()):
                earliest = abx_by_pid[pid]["first_time"]
                earlier = list(db.drugExe.find(
                    {"pid": pid, "status": "finished",
                     "drugList.code": {"$in": abx_codes},
                     "startTime": {"$lt": earliest}},
                    {"startTime": 1, "drugList.code": 1, "drugList.name": 1},
                ).sort("startTime", 1).limit(1))
                if earlier:
                    abx_by_pid[pid]["first_time"] = earlier[0]["startTime"]
                    for dl in earlier[0].get("drugList", []):
                        if dl.get("code") in abx_codes:
                            abx_by_pid[pid]["drug_names"].add(dl.get("name", "")[:60])
                            abx_by_pid[pid]["drug_codes"].add(dl.get("code"))

            # ---- 4. 连接 DataCenter ----
            dc_db = None
            try:
                dc_db = get_datacenter_db()
            except Exception:
                pass

            empty_inflam = {"fever": False, "fever_val": None,
                           "wbc_high": False, "wbc_low": False, "wbc_val": None,
                           "crp_high": False, "crp_val": None,
                           "pct_high": False, "pct_val": None,
                           "has_any_abnormal": False, "details": []}

            # ---- 5. Pass 1: A1 诊断关键词 (纯内存, 零DB查询) ----
            den_patients = []
            excluded_prophylaxis = []
            low_confidence = []
            need_inflam_pids = set()  # A1 未命中, 需炎症查询

            for pid, info in abx_by_pid.items():
                p = pat_by_pid.get(pid)
                if not p:
                    continue
                diagnosis = p.get("clinicalDiagnosis", "") or ""
                a1_hit = any(kw in diagnosis for kw in TREATMENT_DIAG_KEYWORDS)
                if not a1_hit:
                    need_inflam_pids.add(pid)

            # ---- 5b. Pass 2: 批量查炎症 (仅 A1 未命中者) ----
            inflam_cache = {}
            if need_inflam_pids:
                need_abx = {pid: abx_by_pid[pid] for pid in need_inflam_pids}
                need_pat = {pid: pat_by_pid[pid] for pid in need_inflam_pids
                           if pid in pat_by_pid}
                inflam_cache = _batch_get_inflammation(need_abx, need_pat, db, dc_db)

            # ---- 5c. Pass 3: 逐患者 classify ----
            for pid, info in abx_by_pid.items():
                p = pat_by_pid.get(pid)
                if not p:
                    continue

                first_dose = info["first_time"]
                total_doses = info["doses"]
                total_hours = (info["last_time"] - info["first_time"]).total_seconds() / 3600.0
                # #fix: drug_names 是 set，list() 顺序随进程哈希种子变化；
                # >10 个药名时截断结果不同 → A7 特殊级判定/AI prompt 不稳定，排序保证确定性
                drug_names = sorted(info["drug_names"])[:10]
                abx_code_count = len(info.get("drug_codes", set()))
                hispid = p.get("hisPid", "")
                inflam = inflam_cache.get(pid, empty_inflam)

                classification = classify_abx_purpose(
                    p, first_dose, total_doses, total_hours, drug_names,
                    abx_code_count=abx_code_count,
                    db_sc=None, db_dc=None,  # 炎症已批量预查，不放句柄防逐条查库
                    inflammation_signals=inflam,
                )

                pat_entry = {
                    "pid": pid,
                    "mrn": p.get("mrn", "") or hispid,
                    "name": p.get("name", ""),
                    "patient_id": hispid,
                    "abx_time": first_dose,
                    "abx_drug": ", ".join(drug_names[:3]),
                    "total_doses": total_doses,
                    "total_hours": round(total_hours, 1),
                    "purpose": classification["purpose"],
                    "decided_by": classification["decided_by"],
                    "reason": classification["reason"],
                    "confidence": classification.get("confidence", 1.0),
                    "evaluated": classification.get("evaluated", classification.get("decided_by") != "fallback"),
                    "need_review": classification.get("need_review", False),
                    "inflammation": inflam.get("details", []),
                }

                if classification.get("decided_by") == "fallback":
                    low_confidence.append(pat_entry)
                    continue

                if classification["purpose"] == "治疗性":
                    den_patients.append(pat_entry)
                else:
                    excluded_prophylaxis.append(pat_entry)

                # 低置信度 / fallback / need_review → 待人工复核
                if (classification.get("need_review")
                        or classification.get("decided_by") == "fallback"
                        or (classification.get("decided_by") == "ai"
                            and classification.get("confidence", 0) < AI_CONFIDENCE_THRESHOLD)):
                    low_confidence.append(pat_entry)

            result["den_count"] = len(den_patients)
            result["den_patients"] = den_patients
            result["excluded_prophylaxis"] = excluded_prophylaxis
            result["low_confidence"] = low_confidence

            if not den_patients:
                continue

            den_pids = {p["pid"] for p in den_patients}

            # ---- 6. 分子：病原学送检 (源A: VI_ICU_ZYYZ) ----
            # hisPid 映射
            # #fix: 同一住院号可能对应多条在科记录（2026-09 分母 97 条含 7 个重复 hisPid）。
            # 原实现 dict 覆盖写 + den_pids(set) 迭代 → 跨进程哈希种子不同，
            # 培养医嘱落到不同 pid 上，分子出现 60↔59 漂移。
            # 改为 hisPid → [pid...]，该住院号的培养结果对所有在科记录生效。
            smart_pids_by_hispid = {}
            for pid in sorted(den_pids):
                p = pat_by_pid.get(pid)
                if p and p.get("hisPid"):
                    smart_pids_by_hispid.setdefault(p["hisPid"], []).append(pid)

            test_time = {}   # pid → earliest test time
            test_detail = {} # pid → {time, source, name}

            if dc_db is not None and smart_pids_by_hispid:
                hispids = list(smart_pids_by_hispid.keys())
                # 源A: VI_ICU_ZYYZ (培养类医嘱, yaoType='检验')
                culture_orders = list(dc_db["VI_ICU_ZYYZ"].find(
                    {"pid": {"$in": hispids}, "status": {"$in": EXECUTED_ORDER_STATUSES},
                     "yaoType": {"$in": LAB_ORDER_TYPES},
                     "orderName": {"$regex": _keyword_regex(CULTURE_KEYWORDS_FULL), "$options": "i"},
                     "orderTime": {"$lte": end_dt_wide}},  # 回溯全程，时间上限宽松
                    {"pid": 1, "orderTime": 1, "orderName": 1},
                ).sort("orderTime", 1))

                for o in culture_orders:
                    hp = o.get("pid", "")
                    t = o.get("orderTime")
                    if not t:
                        continue
                    for spid in smart_pids_by_hispid.get(hp, []):
                        if spid not in test_time or t < test_time[spid]:
                            test_time[spid] = t
                            test_detail[spid] = {
                                "time": t, "source": "检验医嘱",
                                "name": o.get("orderName", "")[:80],
                            }

            # ---- 7. 计算分子：送检时间 ≤ 首剂时间 ----
            num_pids = set()
            for p in den_patients:
                pid = p["pid"]
                tt = test_time.get(pid)
                first_dose_t = abx_by_pid.get(pid, {}).get("first_time")
                if tt and first_dose_t and tt <= first_dose_t:
                    num_pids.add(pid)

            result["num_count"] = min(len(num_pids), result["den_count"])

            result["num_patients"] = [
                {"pid": pid,
                 "mrn": pat_by_pid[pid].get("mrn", "") or pat_by_pid[pid].get("hisPid", ""),
                 "name": pat_by_pid[pid].get("name", ""),
                 "patient_id": pat_by_pid[pid].get("hisPid", ""),
                 "test_time": test_detail.get(pid, {}).get("time"),
                 "test_source": test_detail.get(pid, {}).get("source", ""),
                 "test_name": test_detail.get(pid, {}).get("name", ""),
                 "abx_time": abx_by_pid.get(pid, {}).get("first_time"),
                 }
                # #fix: set 迭代顺序不稳定 → 排序保证明细行序跨进程一致
                for pid in sorted(num_pids)
            ]

            break  # 拿到数据即退出库名循环

        except Exception as e:
            print(f"[ICU-06] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

    return result


# ============================================================
# ICU-09：镇痛评估率
# ============================================================

# ---- 镇痛评估量表白名单 ----
# bedside: pain-related codes (strVal 直接存评分结果如 CPOT-8)
BEDSIDE_PAIN_CODES = {"param_tengTong_score", "param_painscore_nicu_newborn"}

# bedside strVal 合规镇痛量表前缀白名单（提取 - 之前的部分）
# CPOT/NRS/BPS/VAS 为规范镇痛评估工具；其他前缀（如FRS/RASS等）非镇痛，已排除
PAIN_SCALE_PREFIXES = {"CPOT", "NRS", "BPS", "VAS", "FLACC", "PAIN"}

# score: 合规镇痛评估量表 scoreType
# ⚠️ nrs2002 / nrs2002Score 是营养风险筛查 NRS-2002，非镇痛评估，已排除
SCORE_PAIN_TYPES = {
    "bps", "cpotScore", "cpotScoreV2", "nrsScore",
    "painScore", "newBornPain", "xinShengErTengTong",
}

PAIN_SCALE_CN = {
    "BPS": "行为疼痛量表(BPS)",
    "CPOT": "重症监护疼痛观察工具(CPOT)",
    "FLACC": "FLACC疼痛评估量表",
    "NRS": "数字疼痛评分(NRS)",
    "PAIN": "疼痛评分",
    "VAS": "视觉模拟疼痛评分(VAS)",
    "bps": "行为疼痛量表(BPS)",
    "cpotScore": "重症监护疼痛观察工具(CPOT)",
    "cpotScoreV2": "重症监护疼痛观察工具(CPOT)",
    "nrsScore": "数字疼痛评分(NRS)",
    "painScore": "疼痛评分",
    "newBornPain": "新生儿疼痛评分",
    "xinShengErTengTong": "新生儿疼痛评分",
}

SEDATION_SCALE_CN = {
    "RASS": "Richmond躁动-镇静评分(RASS)",
    "rass": "Richmond躁动-镇静评分(RASS)",
}


def _split_scale_value(raw):
    text = str(raw or "").strip()
    if not text:
        return "", ""
    if "-" in text:
        name, value = text.split("-", 1)
        return name.strip(), value.strip()
    return text, ""


def _score_total(doc):
    for key in ("total", "score", "value", "fVal", "iVal", "strVal"):
        val = doc.get(key)
        if val not in (None, ""):
            return str(val).strip()
    return ""


def get_icu09_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    ICU-09：镇痛评估率。

    分母 = 统计期内本科室在科记录（**人次**口径，一条记录一人次，无排除）。
    分子 = 分母在科记录中，**本次住院期间**（窗口 [入科, 出科∧月末]）进行过
          ≥1 次镇痛评估的人次——「原有患者」上个月入科时做的评估同样计入。

    分子源 A（优先）：bedside 表，code ∈ BEDSIDE_PAIN_CODES 且 valid=True。
          命中患者不再回查源 B。
    分子源 B（兜底）：score 表，scoreType ∈ SCORE_PAIN_TYPES 且 valid=True。
          仅对源 A 未命中的患者补判。

    关联键：bedside.pid / score.pid ↔ str(patient._id)
      ⚠️ patient._id 是 ObjectId，bedside.pid/score.pid 是字符串，
         匹配时统一用 str(patient._id)。

    返回：{den_count, num_count, den_patients, num_patients}
    """
    from datetime import datetime as dt

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)

    result = {"den_count": 0, "num_count": 0,
              "den_patients": [], "num_patients": []}

    for db_name, db in iter_bed_dbs():
        try:

            # ---- 1. 分母：在科患者 ----
            patients = list(db.patient.find(
                {"deptCode": {"$in": dept_codes}, "status": {"$ne": "invalid"},
                 "icuAdmissionTime": {"$lte": end_dt_wide},
                 "$or": [{"icuDischargeTime": {"$gte": start_dt}},
                         {"icuDischargeTime": None},
                         {"icuDischargeTime": {"$exists": False}}]},
                {"_id": 1, "hisPid": 1, "mrn": 1, "name": 1, "hisBed": 1,
                 "icuAdmissionTime": 1, "icuDischargeTime": 1},
            ))
            if not patients:
                continue

            # pid 映射：ObjectId → str (统一为字符串用于关联)
            # 保留全部在科记录 —— 同一患者第二次入科做的评分也要能关联上
            den_pids_obj = set()
            pat_by_strpid = {}
            for p in patients:
                oid = p["_id"]
                spid = str(oid)
                den_pids_obj.add(spid)
                pat_by_strpid[spid] = p

            # 人次口径：分母一条在科记录 = 一人次，不按 mrn 去重
            result["den_count"] = len(patients)
            if not den_pids_obj:
                continue

            # ---- 2. 分子源 A：bedside 批量查 ----
            # 一次性查出所有分母患者中、命中镇痛 code 的 distinct pid
            den_pids_list = list(den_pids_obj)
            a_pids = set()
            a_detail = {}  # strpid → {scale_name, time}

            try:
                # 批量查 bedside，只取需要的字段；
                # 时间上界=月末、不设下界——评估窗口按每条在科记录自身
                # [入科, 出科∧月末] 判定，原有患者上个月入科时的评估也要命中
                bedside_docs = list(db.bedside.find(
                    {"pid": {"$in": den_pids_list},
                     "code": {"$in": list(BEDSIDE_PAIN_CODES)},
                     "valid": True,
                     "time": {"$lte": end_dt_wide}},
                    {"pid": 1, "strVal": 1, "history": 1, "time": 1},
                ).sort("time", 1).max_time_ms(10000).limit(100000))

                for doc in bedside_docs:
                    spid = doc.get("pid", "")
                    if spid not in den_pids_obj:
                        continue

                    # 时间窗口：评估必须在本次 ICU 住院期间
                    p = pat_by_strpid.get(spid)
                    at = doc.get("time")
                    if p and at:
                        w = _stay_window(p, end_dt_wide)
                        if not w or at < w[0] or at > w[1]:
                            continue

                    # 取评分值：优先 strVal，无则 history[].desc
                    score_val = (doc.get("strVal") or "").strip()
                    if not score_val:
                        hist = doc.get("history") or []
                        if hist:
                            score_val = (hist[0].get("desc") or "").strip()

                    # 前缀白名单校验：仅 CPOT/NRS/BPS/VAS/FLACC/PAIN 等合规量表
                    if score_val:
                        prefix, score_num = _split_scale_value(score_val)
                        if prefix not in PAIN_SCALE_PREFIXES:
                            continue  # 非镇痛量表脏值，跳过

                    if score_val:
                        if spid not in a_pids:
                            a_pids.add(spid)
                            a_detail[spid] = {
                                "source": "床旁评估记录",
                                "scale": PAIN_SCALE_CN.get(prefix, prefix),
                                "score_value": score_num,
                                "time": doc.get("time"),
                            }
            except Exception as e:
                print(f"[ICU-09] bedside query error: {e}")

            # ---- 3. 分子源 B：score 补判（仅 A 未命中者） ----
            b_pids = set()
            b_detail = {}  # strpid → {scale_name, time}

            need_b = [spid for spid in den_pids_list if spid not in a_pids]
            if need_b:
                try:
                    score_docs = list(db.score.find(
                        {"pid": {"$in": need_b},
                         "scoreType": {"$in": list(SCORE_PAIN_TYPES)},
                         "valid": True,
                         "time": {"$lte": end_dt_wide}},
                        {"pid": 1, "scoreType": 1, "total": 1, "score": 1,
                         "value": 1, "fVal": 1, "iVal": 1, "strVal": 1, "time": 1},
                    ).sort("time", 1).max_time_ms(10000).limit(50000))

                    for doc in score_docs:
                        spid = doc.get("pid", "")
                        if spid not in den_pids_obj:
                            continue
                        # 时间窗口：本次住院期间（原有患者上月入科时的评估也算）
                        p = pat_by_strpid.get(spid)
                        at = doc.get("time")
                        if p and at:
                            w = _stay_window(p, end_dt_wide)
                            if not w or at < w[0] or at > w[1]:
                                continue
                        stype = doc.get("scoreType", "painScore")
                        if spid not in b_pids:
                            b_pids.add(spid)
                            b_detail[spid] = {
                                "source": "量表评分记录",
                                "scale": PAIN_SCALE_CN.get(stype, stype),
                                "score_value": _score_total(doc),
                                "time": doc.get("time"),
                            }
                except Exception as e:
                    print(f"[ICU-09] score query error: {e}")

            # ---- 4. 分子 = A ∪ B ----
            num_pids = a_pids | b_pids

            # ---- 5. 构建明细（人次口径：一条在科记录一行）----
            result["den_patients"] = [
                {"pid": str(p["_id"]),
                 "mrn": p.get("mrn", "") or p.get("hisPid", ""),
                 "name": p.get("name", ""),
                 "patient_id": p.get("hisPid", ""),
                 "hisBed": p.get("hisBed", ""),
                 "icu_admit": p.get("icuAdmissionTime"),
                 "icu_discharge": p.get("icuDischargeTime"),
                 }
                for p in patients
            ]

            num_rows = []
            for spid in sorted(num_pids):          # sorted: 明细输出顺序稳定
                p = pat_by_strpid[spid]
                detail = a_detail.get(spid) or b_detail.get(spid) or {}
                num_rows.append({
                    "pid": spid,
                    "mrn": p.get("mrn", "") or p.get("hisPid", ""),
                    "name": p.get("name", ""),
                    "patient_id": p.get("hisPid", ""),
                    "assess_source": detail.get("source", ""),
                    "assess_scale": detail.get("scale", ""),
                    "assess_value": detail.get("score_value", ""),
                    "assess_time": detail.get("time"),
                })
            # 人次口径：一行一在科记录，不按 mrn 去重
            result["num_patients"] = num_rows
            result["num_count"] = len(num_rows)

            break  # 拿到数据即退出库名循环

        except Exception as e:
            print(f"[ICU-09] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

    return result


# ============================================================
# ICU-10：镇静评估率
# ============================================================

# bedside: RASS 镇静评分 code
BEDSIDE_SEDATION_CODE = "param_score_rass_obs"

# score: 镇静评估量表 scoreType
SCORE_SEDATION_TYPES = {"rass"}


def get_icu10_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    ICU-10：镇静评估率。

    分母 = 统计期内本科室在科记录（**人次**口径，一条记录一人次，无排除）。
    分子 = 分母在科记录中，**本次住院期间**（窗口 [入科, 出科∧月末]）进行过
          ≥1 次镇静评估（RASS）的人次——「原有患者」上个月入科时做的评估同样计入。

    分子源 A（优先）：bedside 表，code='param_score_rass_obs' 且 valid=True。
    分子源 B（兜底）：score 表，scoreType='rass' 且 valid=True。
          仅对源 A 未命中的患者补判。

    关联键：bedside.pid / score.pid ↔ str(patient._id)

    返回：{den_count, num_count, den_patients, num_patients}
    """
    from datetime import datetime as dt

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)

    result = {"den_count": 0, "num_count": 0,
              "den_patients": [], "num_patients": []}

    for db_name, db in iter_bed_dbs():
        try:

            # ---- 1. 分母 ----
            patients = list(db.patient.find(
                {"deptCode": {"$in": dept_codes}, "status": {"$ne": "invalid"},
                 "icuAdmissionTime": {"$lte": end_dt_wide},
                 "$or": [{"icuDischargeTime": {"$gte": start_dt}},
                         {"icuDischargeTime": None},
                         {"icuDischargeTime": {"$exists": False}}]},
                {"_id": 1, "hisPid": 1, "mrn": 1, "name": 1, "hisBed": 1,
                 "icuAdmissionTime": 1, "icuDischargeTime": 1},
            ))
            if not patients:
                continue

            # 保留全部在科记录 —— 同一患者第二次入科做的评分也要能关联上
            den_pids_obj = set()
            pat_by_strpid = {}
            for p in patients:
                spid = str(p["_id"])
                den_pids_obj.add(spid)
                pat_by_strpid[spid] = p

            # 人次口径：分母一条在科记录 = 一人次，不按 mrn 去重
            result["den_count"] = len(patients)
            if not den_pids_obj:
                continue
            den_pids_list = list(den_pids_obj)

            # ---- 2. 源 A：bedside ----
            a_pids = set()
            a_detail = {}

            try:
                # 时间上界=月末、不设下界——窗口按每条在科记录自身
                # [入科, 出科∧月末] 判定，原有患者上个月入科时的评估也要命中
                bedside_docs = list(db.bedside.find(
                    {"pid": {"$in": den_pids_list},
                     "code": BEDSIDE_SEDATION_CODE,
                     "valid": True,
                     "time": {"$lte": end_dt_wide}},
                    {"pid": 1, "strVal": 1, "fVal": 1, "time": 1},
                ).sort("time", 1).max_time_ms(10000).limit(100000))

                for doc in bedside_docs:
                    spid = doc.get("pid", "")
                    if spid not in den_pids_obj:
                        continue
                    # 时间窗口：评估必须在本次 ICU 住院期间
                    p = pat_by_strpid.get(spid)
                    at = doc.get("time")
                    if p and at:
                        w = _stay_window(p, end_dt_wide)
                        if not w or at < w[0] or at > w[1]:
                            continue
                    # RASS 值: strVal="-4"~"+4", fVal=-4.0~4.0
                    val = (doc.get("strVal") or "").strip()
                    if not val and doc.get("fVal") not in (None, ""):
                        val = str(doc.get("fVal")).strip()
                    if val and spid not in a_pids:
                        a_pids.add(spid)
                        a_detail[spid] = {
                            "source": "床旁评估记录",
                            "scale": SEDATION_SCALE_CN["RASS"],
                            "score_value": val,
                            "time": doc.get("time"),
                        }
            except Exception as e:
                print(f"[ICU-10] bedside query error: {e}")

            # ---- 3. 源 B：score 补判 ----
            b_pids = set()
            b_detail = {}

            need_b = [spid for spid in den_pids_list if spid not in a_pids]
            if need_b:
                try:
                    score_docs = list(db.score.find(
                        {"pid": {"$in": need_b},
                         "scoreType": {"$in": list(SCORE_SEDATION_TYPES)},
                         "valid": True,
                         "time": {"$lte": end_dt_wide}},
                        {"pid": 1, "scoreType": 1, "total": 1, "score": 1,
                         "value": 1, "fVal": 1, "iVal": 1, "strVal": 1, "time": 1},
                    ).sort("time", 1).max_time_ms(10000).limit(50000))

                    for doc in score_docs:
                        spid = doc.get("pid", "")
                        if spid not in den_pids_obj:
                            continue
                        # 时间窗口：本次住院期间（原有患者上月入科时的评估也算）
                        p = pat_by_strpid.get(spid)
                        at = doc.get("time")
                        if p and at:
                            w = _stay_window(p, end_dt_wide)
                            if not w or at < w[0] or at > w[1]:
                                continue
                        if spid not in b_pids:
                            b_pids.add(spid)
                            b_detail[spid] = {
                                "source": "量表评分记录",
                                "scale": SEDATION_SCALE_CN["rass"],
                                "score_value": _score_total(doc),
                                "time": doc.get("time"),
                            }
                except Exception as e:
                    print(f"[ICU-10] score query error: {e}")

            # ---- 4. 分子 = A ∪ B ----
            num_pids = a_pids | b_pids

            # ---- 5. 构建明细（人次口径：一条在科记录一行）----
            result["den_patients"] = [
                {"pid": str(p["_id"]),
                 "mrn": p.get("mrn", "") or p.get("hisPid", ""),
                 "name": p.get("name", ""),
                 "patient_id": p.get("hisPid", ""),
                 "hisBed": p.get("hisBed", ""),
                 "icu_admit": p.get("icuAdmissionTime"),
                 "icu_discharge": p.get("icuDischargeTime"),
                 }
                for p in patients
            ]

            num_rows = []
            for spid in sorted(num_pids):          # sorted: 明细输出顺序稳定
                p = pat_by_strpid[spid]
                detail = a_detail.get(spid) or b_detail.get(spid) or {}
                num_rows.append({
                    "pid": spid,
                    "mrn": p.get("mrn", "") or p.get("hisPid", ""),
                    "name": p.get("name", ""),
                    "patient_id": p.get("hisPid", ""),
                    "assess_source": detail.get("source", ""),
                    "assess_scale": detail.get("scale", ""),
                    "assess_value": detail.get("score_value", ""),
                    "assess_time": detail.get("time"),
                })
            # 人次口径：一行一在科记录，不按 mrn 去重
            result["num_patients"] = num_rows
            result["num_count"] = len(num_rows)

            break

        except Exception as e:
            print(f"[ICU-10] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

    return result


# ============================================================
# ICU-11：ICU患者标化病死指数(SMR)
# ============================================================

def _as_float_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_icu11_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    ICU-11：ICU患者标化病死指数(SMR)。

    分母人群 = 同期出科且 dischargedType 为“死亡/出院/非医嘱离院”，并有入科
    APACHE_WINDOW_HOURS（.env 可配，默认 24h）内首次 APACHE II 评分的完整病例。
    分母数值 = 每例首次 APACHE II apacheII.calDead.score 之和。
    分子 = 同一人群中 dischargedType 为“死亡/非医嘱离院”的人数。

    返回 {den_count, num_count, value, den_patients, num_patients}
    """
    from datetime import datetime as dt, timedelta

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)

    result = {
        "den_count": 0.0,
        "num_count": 0,
        "value": 0.0,
        "den_patients": [],
        "num_patients": [],
    }

    death_types = {"死亡", "非医嘱离院"}
    closed_types = {"出院", *death_types}

    for db_name, db in iter_bed_dbs():
        try:

            patients = list(db.patient.find(
                {
                    "deptCode": {"$in": dept_codes},
                    "status": {"$ne": "invalid"},
                    "icuDischargeTime": {"$gte": start_dt, "$lte": end_dt_wide},
                    "dischargedType": {"$in": list(closed_types)},
                },
                {
                    "_id": 1, "hisPid": 1, "mrn": 1, "name": 1,
                    "deptCode": 1, "icuAdmissionTime": 1, "icuDischargeTime": 1,
                    "dischargeTime": 1, "dischargedType": 1,
                },
            ).max_time_ms(10000).limit(200000))

            if not patients:
                continue

            pat_by_pid = {str(p["_id"]): p for p in patients}
            pids = list(pat_by_pid.keys())

            score_docs = list(db.score.find(
                {
                    "pid": {"$in": pids},
                    "scoreType": "apacheII",
                    "valid": True,
                },
                {
                    "pid": 1, "time": 1, "total": 1,
                    "apacheII.calDead.score": 1,
                },
            ).sort("time", 1).max_time_ms(15000).limit(300000))

            first_score_by_pid = {}
            for doc in score_docs:
                spid = doc.get("pid")
                if spid in first_score_by_pid:
                    continue
                pat = pat_by_pid.get(spid)
                if not pat:
                    continue
                score_time = doc.get("time")
                admit_time = pat.get("icuAdmissionTime")
                if not score_time or not admit_time:
                    continue
                if score_time < admit_time or score_time > admit_time + timedelta(hours=APACHE_WINDOW_HOURS):
                    continue
                cal_dead = (((doc.get("apacheII") or {}).get("calDead") or {}).get("score"))
                cal_dead = _as_float_or_none(cal_dead)
                if cal_dead is None or cal_dead < 0 or cal_dead > 1:
                    continue
                first_score_by_pid[spid] = {
                    "time": score_time,
                    "total": doc.get("total"),
                    "cal_dead": cal_dead,
                }

            den_patients = []
            num_patients = []
            expected_deaths = 0.0
            actual_deaths = 0

            for spid, score in first_score_by_pid.items():
                pat = pat_by_pid[spid]
                discharged_type = pat.get("dischargedType", "")
                expected_deaths += score["cal_dead"]
                if discharged_type in death_types:
                    actual_deaths += 1

                item = {
                    "pid": spid,
                    "mrn": pat.get("mrn", "") or pat.get("hisPid", ""),
                    "patient_id": pat.get("hisPid", "") or pat.get("mrn", ""),
                    "name": pat.get("name", ""),
                    "dept_code": pat.get("deptCode", ""),
                    "icu_admit": pat.get("icuAdmissionTime"),
                    "icu_discharge": pat.get("icuDischargeTime") or pat.get("dischargeTime"),
                    "dischargedType": discharged_type,
                    "apache_time": score["time"],
                    "apache_total": score.get("total"),
                    "apache_calDead": score["cal_dead"],
                }
                den_patients.append(item)
                if discharged_type in death_types:
                    num_patients.append(item)

            result["den_count"] = round(expected_deaths, 4)
            result["num_count"] = actual_deaths
            result["value"] = round(actual_deaths / expected_deaths, 2) if expected_deaths > 0 else 0.0
            result["den_patients"] = den_patients
            result["num_patients"] = num_patients
            break

        except Exception as e:
            print(f"[ICU-11] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

    return result


# ============================================================
# ICU-12/13：人工气道非计划拔管与48小时再置管
# ============================================================

AIRWAY_TUBE_TYPES = {"气管插管", "气插管", "气切套管"}


def _is_true(value) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _fmt_dt(value):
    # 库内/边界均为 UTC 帧 → 统一转北京时间展示（tzutil.fmt_bj）
    return fmt_bj(value)


def _get_airway_tube_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    共用取数：
    - 分母：统计期内拔除的气管插管/气插管/气切套管，排除 replace=true 换管。
    - ICU-12 分子：分母中 unPlannedEndTube 为 true 的非计划拔管。
    - ICU-13 分子：分母拔管后48小时内同患者再次气管插管/气插管/气切套管，
      再置管记录 replace=true 时不计；气管插管转气切套管计入。
    """
    from datetime import datetime as dt, timedelta

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)
    reinsert_until = end_dt_wide + timedelta(hours=48)

    result = {
        "den_count": 0,
        "icu12_num_count": 0,
        "icu13_num_count": 0,
        "den_patients": [],
        "icu12_num_patients": [],
        "icu13_num_patients": [],
    }

    for db_name, db in iter_bed_dbs():
        try:

            patients = list(db.patient.find(
                {"deptCode": {"$in": dept_codes}, "status": {"$ne": "invalid"}},
                {"_id": 1, "hisPid": 1, "mrn": 1, "name": 1, "deptCode": 1},
            ).max_time_ms(10000).limit(200000))
            if not patients:
                continue

            pat_by_pid = {str(p["_id"]): p for p in patients}
            pids = list(pat_by_pid.keys())

            ended_docs = list(db.tubeExe.find(
                {
                    "pid": {"$in": pids},
                    "type": {"$in": list(AIRWAY_TUBE_TYPES)},
                    "endTime": {"$gte": start_dt, "$lte": end_dt_wide},
                },
                {
                    "_id": 1, "pid": 1, "type": 1, "startTime": 1, "endTime": 1,
                    "unPlannedEndTube": 1, "replace": 1,
                },
            ).sort("endTime", 1).max_time_ms(15000).limit(300000))

            den_docs = [d for d in ended_docs if not _is_true(d.get("replace")) and d.get("endTime")]
            if not den_docs:
                result["den_patients"] = []
                break

            reinsert_docs = list(db.tubeExe.find(
                {
                    "pid": {"$in": pids},
                    "type": {"$in": list(AIRWAY_TUBE_TYPES)},
                    "startTime": {"$gte": start_dt, "$lte": reinsert_until},
                },
                {
                    "_id": 1, "pid": 1, "type": 1, "startTime": 1, "endTime": 1,
                    "replace": 1,
                },
            ).sort("startTime", 1).max_time_ms(15000).limit(300000))

            starts_by_pid = {}
            for doc in reinsert_docs:
                if _is_true(doc.get("replace")):
                    continue
                starts_by_pid.setdefault(doc.get("pid"), []).append(doc)

            den_patients = []
            icu12_num_patients = []
            icu13_num_patients = []

            for doc in den_docs:
                pid = doc.get("pid")
                pat = pat_by_pid.get(pid, {})
                end_time = doc.get("endTime")
                base_item = {
                    "pid": pid,
                    "mrn": pat.get("mrn", "") or pat.get("hisPid", ""),
                    "patient_id": pat.get("hisPid", "") or pat.get("mrn", ""),
                    "name": pat.get("name", ""),
                    "dept_code": pat.get("deptCode", ""),
                    "tube_id": str(doc.get("_id", "")),
                    "tube_type": doc.get("type", ""),
                    "tube_start": doc.get("startTime"),
                    "tube_end": end_time,
                    "unplanned": _is_true(doc.get("unPlannedEndTube")),
                    "replace": _is_true(doc.get("replace")),
                }
                den_patients.append(base_item)

                if base_item["unplanned"]:
                    icu12_num_patients.append(base_item)

                reinsert = None
                for next_doc in starts_by_pid.get(pid, []):
                    next_start = next_doc.get("startTime")
                    if not next_start or next_doc.get("_id") == doc.get("_id"):
                        continue
                    if end_time < next_start <= end_time + timedelta(hours=48):
                        reinsert = next_doc
                        break

                if reinsert:
                    item = dict(base_item)
                    item.update({
                        "reinsert_type": reinsert.get("type", ""),
                        "reinsert_start": reinsert.get("startTime"),
                        "reinsert_tube_id": str(reinsert.get("_id", "")),
                    })
                    icu13_num_patients.append(item)

            result["den_count"] = len(den_patients)
            result["icu12_num_count"] = len(icu12_num_patients)
            result["icu13_num_count"] = len(icu13_num_patients)
            result["den_patients"] = den_patients
            result["icu12_num_patients"] = icu12_num_patients
            result["icu13_num_patients"] = icu13_num_patients
            break

        except Exception as e:
            print(f"[ICU-12/13] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

    return result


def get_icu12_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    data = _get_airway_tube_data(dept_codes, start_date, end_date)
    return {
        "den_count": data["den_count"],
        "num_count": data["icu12_num_count"],
        "den_patients": data["den_patients"],
        "num_patients": data["icu12_num_patients"],
    }


def get_icu13_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    data = _get_airway_tube_data(dept_codes, start_date, end_date)
    return {
        "den_count": data["den_count"],
        "num_count": data["icu13_num_count"],
        "den_patients": data["den_patients"],
        "num_patients": data["icu13_num_patients"],
    }


# ============================================================
# ICU-14：非计划转入ICU率
# ============================================================

def _first_operation_name(patient: dict) -> str:
    operations = patient.get("patientOperations") or []
    if not operations or not isinstance(operations[0], dict):
        return ""
    return operations[0].get("name", "") or ""


def get_icu14_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    分母：同期转入ICU的手术患者，admissionType 同时包含“手术”和“转入”。
    分子：分母患者中 admissionPlan == “非计划转入”。
    空值/缺失/其他 admissionPlan 默认不计入分子。
    """
    from datetime import datetime as dt

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)

    result = {
        "den_count": 0,
        "num_count": 0,
        "value": 0.0,
        "den_patients": [],
        "num_patients": [],
    }

    for db_name, db in iter_bed_dbs():
        try:
            query = {
                "deptCode": {"$in": dept_codes},
                "status": {"$ne": "invalid"},
                "icuAdmissionTime": {"$gte": start_dt, "$lte": end_dt_wide},
                "$and": [
                    {"admissionType": {"$regex": "手术"}},
                    {"admissionType": {"$regex": "转入"}},
                ],
            }
            projection = {
                "_id": 1,
                "hisPid": 1,
                "mrn": 1,
                "name": 1,
                "deptCode": 1,
                "icuAdmissionTime": 1,
                "admissionType": 1,
                "admissionPlan": 1,
                "patientOperations.name": 1,
            }

            patients = list(
                db.patient.find(query, projection)
                .sort("icuAdmissionTime", 1)
                .max_time_ms(15000)
                .limit(200000)
            )
            if not patients:
                continue

            den_patients = []
            num_patients = []
            seen = set()
            for patient in patients:
                pid = str(patient.get("_id"))
                if pid in seen:
                    continue
                seen.add(pid)

                item = {
                    "pid": pid,
                    "mrn": patient.get("mrn", "") or patient.get("hisPid", ""),
                    "patient_id": patient.get("hisPid", "") or patient.get("mrn", "") or pid[-8:],
                    "name": patient.get("name", ""),
                    "dept_code": patient.get("deptCode", ""),
                    "icuAdmissionTime": patient.get("icuAdmissionTime"),
                    "admissionType": patient.get("admissionType", "") or "",
                    "admissionPlan": patient.get("admissionPlan", "") or "",
                    "operation_name": _first_operation_name(patient),
                }
                den_patients.append(item)
                if item["admissionPlan"] == "非计划转入":
                    num_patients.append(item)

            den_count = len(den_patients)
            num_count = len(num_patients)
            result.update({
                "den_count": den_count,
                "num_count": num_count,
                "value": round(num_count / den_count * 100, 1) if den_count > 0 else 0.0,
                "den_patients": den_patients,
                "num_patients": num_patients,
            })
            break

        except Exception as e:
            print(f"[ICU-14] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

    return result


# ============================================================
# ICU-16/17/CAUTI：三管院感发病率
# ============================================================

# ============================================================
# ICU-15: 48h readmission after ICU discharge
# ============================================================

def _icu15_patient_identity(patient: dict, fallback_pid: str = "") -> str:
    for key in ("hisPid", "mrn", "patientId", "inHospitalNo"):
        val = patient.get(key)
        if val not in (None, ""):
            return f"{key}:{val}"
    return f"pid:{fallback_pid or str(patient.get('_id', ''))}"


def _icu15_patient_item(patient: dict, pid: str, discharge_time, source: str) -> dict:
    return {
        "pid": pid,
        "mrn": patient.get("mrn", "") or patient.get("hisPid", ""),
        "patient_id": patient.get("hisPid", "") or patient.get("mrn", "") or pid[-8:],
        "name": patient.get("name", ""),
        "dept_code": patient.get("deptCode", ""),
        "icu_admit": patient.get("icuAdmissionTime"),
        "icu_discharge": discharge_time,
        "source": source,
        "basis": "同期转出ICU；依据：Patient记录" if source == "patient" else "同期转出ICU；依据：patInIcuHistoryList",
    }


def _icu15_event_key(identity: str, discharge_time) -> tuple:
    rounded = discharge_time.replace(second=0, microsecond=0) if discharge_time else None
    return identity, rounded


def _icu15_find_merge_key(events: dict, identity: str, discharge_time, merge_hours: int = 2):
    from datetime import timedelta

    if not discharge_time:
        return None
    for key, item in events.items():
        if key[0] != identity:
            continue
        existing_time = item.get("icu_discharge")
        if existing_time and abs(existing_time - discharge_time) <= timedelta(hours=merge_hours):
            return key
    return None


def _icu15_add_event(events: dict, identity: str, item: dict):
    discharge_time = item.get("icu_discharge")
    merge_key = _icu15_find_merge_key(events, identity, discharge_time)
    if merge_key:
        existing = events[merge_key]
        if item.get("history_re_admit") and not existing.get("history_re_admit"):
            existing["history_re_admit"] = item.get("history_re_admit")
        existing["source"] = "patient+patInIcuHistoryList" if existing.get("source") != item.get("source") else existing.get("source")
        existing["basis"] = "patient record and patInIcuHistoryList discharge merged"
        return existing
    key = _icu15_event_key(identity, discharge_time)
    events[key] = item
    return item


def get_icu15_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    ICU-15: transfer-out ICU then return to ICU within 48h.

    Denominator is discharge events in the period, including patient ICU discharge
    records and patInIcuHistoryList ICU discharge records. Numerator is the same
    events with a later ICU admission within 48 hours.
    """
    from datetime import datetime as dt, timedelta

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)

    result = {
        "den_count": 0,
        "num_count": 0,
        "value": 0.0,
        "value_type": "percent",
        "den_patients": [],
        "num_patients": [],
    }

    for db_name, db in iter_bed_dbs():
        try:
            projection = {
                "_id": 1, "hisPid": 1, "mrn": 1, "patientId": 1, "inHospitalNo": 1,
                "name": 1, "deptCode": 1, "icuAdmissionTime": 1, "icuDischargeTime": 1,
                "dischargeTime": 1, "patInIcuHistoryList": 1, "status": 1,
            }

            scoped_patients = list(db.patient.find(
                {
                    "deptCode": {"$in": dept_codes},
                    "status": {"$ne": "invalid"},
                    "$or": [
                        {"icuDischargeTime": {"$gte": start_dt, "$lte": end_dt_wide}},
                        {"dischargeTime": {"$gte": start_dt, "$lte": end_dt_wide}},
                        {"patInIcuHistoryList.icuDischargeTime": {"$gte": start_dt, "$lte": end_dt_wide}},
                    ],
                },
                projection,
            ).sort("icuDischargeTime", 1).max_time_ms(20000).limit(200000))

            if not scoped_patients:
                continue

            events = {}
            for patient in scoped_patients:
                pid = str(patient.get("_id", ""))
                identity = _icu15_patient_identity(patient, pid)
                discharge_time = patient.get("icuDischargeTime") or patient.get("dischargeTime")
                if discharge_time and start_dt <= discharge_time <= end_dt_wide:
                    _icu15_add_event(
                        events,
                        identity,
                        _icu15_patient_item(patient, pid, discharge_time, "patient"),
                    )

                for hist in patient.get("patInIcuHistoryList") or []:
                    hist_discharge = hist.get("icuDischargeTime")
                    if not hist_discharge or not (start_dt <= hist_discharge <= end_dt_wide):
                        continue
                    item = _icu15_add_event(
                        events,
                        identity,
                        _icu15_patient_item(patient, pid, hist_discharge, "patInIcuHistoryList"),
                    )
                    re_time = hist.get("reIcuAdmissionTime")
                    if re_time:
                        item["history_re_admit"] = re_time

            den_patients = []
            num_patients = []
            for (identity, _), item in sorted(events.items(), key=lambda kv: kv[1].get("icu_discharge")):
                discharge_time = item.get("icu_discharge")
                readmit = None
                history_re_admit = item.get("history_re_admit")
                if history_re_admit and discharge_time < history_re_admit <= discharge_time + timedelta(hours=48):
                    readmit = {
                        "time": history_re_admit,
                        "pid": item.get("pid", ""),
                        "dept_code": item.get("dept_code", ""),
                        "source": "patInIcuHistoryList",
                    }
                item["re_icu_admit"] = readmit.get("time") if readmit else None
                item["re_admit_dept_code"] = readmit.get("dept_code", "") if readmit else ""
                item["re_admit_source"] = readmit.get("source", "") if readmit else ""
                item["basis"] = (
                    "出科后48小时内重返ICU；依据：合并历史"
                    if readmit else item.get("basis", "")
                )
                den_patients.append(item)
                if readmit:
                    num_patients.append(item)

            den_count = len(den_patients)
            num_count = len(num_patients)
            result.update({
                "den_count": den_count,
                "num_count": num_count,
                "value": round(num_count / den_count * 100, 1) if den_count > 0 else 0.0,
                "den_patients": den_patients,
                "num_patients": num_patients,
            })
            break
        except Exception as e:
            print(f"[ICU-15] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

    return result


# ============================================================
# ICU-18: acute brain injury consciousness assessment rate
# ============================================================

# 急性脑损伤 ICD 编码清单（2026-10 院方口径：分母只按诊断 ICD 命中判定，
# 清单来源 VI_ICU_ICD 查询（患者侧另补 S06）；不再做诊断文本关键词、
# 影像报告(VI_ICU_REPORT)、diseaseDiagnosis 人工确认三条补充路径）。
# 匹配为前缀命中：类目码覆盖其下全部亚目（如 S06 命中 S06.900），
# 亚目码只命中自身前缀（如 K72.903 命中 K72.903x，不含 K72.901）。
ICU18_BRAIN_INJURY_ICD_PREFIXES = {
    "S00", "S01", "S02", "S03", "S04", "S05", "S06",
    "I60", "I61", "I62", "I63",
    "D43",
    "G40", "G41",
    "G06.006",
    # G93.403/G93.101 已含于 G93.4/G93.1 前缀，保留以对齐院方清单条目
    "G93.1", "G93.4", "G93.403", "G93.101",
    "K72.903", "T67.000",
}
# ICU-18 分子判定用评分类型：有 apacheII 评分即视为已完成意识(GCS)评估
# （2026-10 口径调整：不再逐条查 GCS/FOUR 床旁与量表记录，原常量见 git 历史）
ICU18_ASSESS_SCORE_TYPES = {"apacheII"}


def _normalize_icd(code) -> str:
    return str(code or "").strip().upper()


def _match_brain_injury_icd(codes: list[str]) -> tuple[str, str]:
    """患者 ICD 命中院方急性脑损伤清单则返回 (完整icd, 命中前缀)，否则 ("", "")。"""
    for code in codes:
        icd = _normalize_icd(code)
        if not icd:
            continue
        for prefix in ICU18_BRAIN_INJURY_ICD_PREFIXES:
            if icd.startswith(prefix):
                return icd, prefix
    return "", ""


def _brain_injury_den_item(patient: dict, pid: str, category: str, source: str, evidence: str, confidence=1.0):
    return {
        "pid": pid,
        "mrn": patient.get("mrn", "") or patient.get("hisPid", ""),
        "patient_id": patient.get("hisPid", "") or patient.get("mrn", "") or pid[-8:],
        "name": patient.get("name", ""),
        "dept_code": patient.get("deptCode", ""),
        "icu_admit": patient.get("icuAdmissionTime"),
        "category": category,
        "den_source": source,
        "evidence": evidence,
        "ai_confidence": confidence,
        "assessed": False,
        "first_assess_time": None,
        "assess_source": "",
    }


def _apply_icu18_assessments(db, den_by_pid: dict, start_dt, end_dt_wide):
    """分子判定：分母患者窗口 [入科, 月末] 内有 apacheII 评分即视为已评估意识(GCS)。

    2026-10 口径调整：不再逐条查 GCS/FOUR 床旁与量表记录——做 APACHEⅡ 评分时
    GCS 已一并录入，故「有 apacheII 评分」即标识该患者 GCS 已评估。
    """
    if not den_by_pid:
        return
    pids = list(den_by_pid.keys())

    def accept_assessment(doc, source):
        pid = doc.get("pid")
        item = den_by_pid.get(pid)
        if not item:
            return
        assess_time = doc.get("time")
        patient_admit = item.get("icu_admit") or start_dt
        if assess_time and (assess_time < patient_admit or assess_time > end_dt_wide):
            return
        old_time = item.get("first_assess_time")
        if not old_time or (assess_time and assess_time < old_time):
            item["assessed"] = True
            item["first_assess_time"] = assess_time
            item["assess_source"] = source

    score_query = {
        "pid": {"$in": pids},
        "valid": True,
        "scoreType": {"$in": list(ICU18_ASSESS_SCORE_TYPES)},
        "time": {"$gte": start_dt, "$lte": end_dt_wide},
    }
    for doc in db.score.find(
        score_query,
        {"pid": 1, "scoreType": 1, "time": 1, "total": 1},
    ).sort("time", 1).max_time_ms(30000).limit(300000):
        accept_assessment(doc, "APACHEⅡ评分")


def get_icu18_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    ICU-18: acute brain injury consciousness assessment rate.
    Denominator: patient diagnosis ICD codes prefix-hitting the hospital list
    ICU18_BRAIN_INJURY_ICD_PREFIXES（2026-10 院方口径：只按诊断 ICD 判定）。
    Numerator: denominator patients with an apacheII score in [ICU admit, month end]
    —— 有 APACHE 评分即视为已完成意识(GCS)评估（2026-10 口径调整，替代原 GCS/FOUR 判定）。
    """
    from datetime import datetime as dt

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)
    result = {
        "den_count": 0,
        "num_count": 0,
        "value": 0.0,
        "value_type": "percent",
        "den_patients": [],
        "num_patients": [],
        "note": "",
    }

    for db_name, db in iter_bed_dbs():
        try:
            patients = list(db.patient.find(
                {
                    "deptCode": {"$in": dept_codes},
                    "status": {"$ne": "invalid"},
                    "icuAdmissionTime": {"$gte": start_dt, "$lte": end_dt_wide},
                },
                {
                    "_id": 1, "hisPid": 1, "mrn": 1, "name": 1, "deptCode": 1,
                    "icuAdmissionTime": 1, "clinicalDiagnosisCodeList": 1,
                    "dischargedDiagnosisIcd": 1,
                },
            ).sort("icuAdmissionTime", 1).max_time_ms(30000).limit(200000))
            if not patients:
                continue

            den_by_pid = {}
            for patient in patients:
                pid = str(patient["_id"])
                codes = []
                for field in ("clinicalDiagnosisCodeList", "dischargedDiagnosisIcd"):
                    value = patient.get(field)
                    values = value if isinstance(value, list) else ([value] if value else [])
                    for item in values:
                        # 列表元素可能是 "|" 拼接的多码串（如 'R40.200x005|S02.900x002'），
                        # 拆开逐码判定，否则只有整串开头的编码能命中
                        codes.extend(seg.strip() for seg in str(item).split("|") if seg.strip())

                icd, prefix = _match_brain_injury_icd(codes)
                if icd:
                    evidence = icd if icd == prefix else f"{icd} (命中清单码 {prefix})"
                    den_by_pid[pid] = _brain_injury_den_item(
                        patient, pid, "急性脑损伤", "ICD编码", evidence, 1.0)

            _apply_icu18_assessments(db, den_by_pid, start_dt, end_dt_wide)

            den_patients = list(den_by_pid.values())
            num_patients = [p for p in den_patients if p.get("assessed")]
            den_count = len(den_patients)
            num_count = len(num_patients)
            result.update({
                "den_count": den_count,
                "num_count": num_count,
                "value": round(num_count / den_count * 100, 2) if den_count > 0 else 0.0,
                "den_patients": den_patients,
                "num_patients": num_patients,
                "note": "" if den_count > 0 else "无急性脑损伤患者",
            })
            break
        except Exception as e:
            print(f"[ICU-18] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

    return result


# ============================================================
# ICU-19：48h 内肠内营养(EN)启动率
# ============================================================

EN_BEDSIDE_ROUTE_CODES = {"param_肠内营养途径"}
EN_BEDSIDE_START_CODES = {"param_肠内营养措施", "param_营养输注方式"}
EN_BEDSIDE_START_VALUES = {"开始", "启动", "给予", "分次推注", "间接重力滴注", "持续泵入", "顿服"}
EN_ROUTE_KEYWORDS = {"经口", "口服", "胃管", "鼻胃管", "鼻空肠管", "鼻肠管", "造瘘", "空肠造瘘", "胃造瘘"}
EN_METHOD_KEYWORDS = {"口服", "鼻饲", "管饲", "胃管", "鼻胃管", "鼻肠管", "肠内", "经口", "造瘘"}
EN_NAME_WHITELIST = {
    "肠内营养", "能全力", "瑞素", "瑞能", "瑞代", "瑞先", "百普力", "百普素",
    "安素", "康全力", "佳维体", "益菲佳", "维沃", "全安素", "短肽型",
    "整蛋白型", "匀浆膳", "鼻饲营养",
}
PN_NAME_BLACKLIST = {
    "脂肪乳注射液", "中长链脂肪乳", "结构脂肪乳", "复方氨基酸注射液",
    "葡萄糖注射液", "卡文", "卡全", "全合一", "静脉营养", "肠外营养",
}
EN_CONTRAINDICATION_KEYWORDS = {
    "肠梗阻", "肠缺血", "消化道出血", "胃潴留", "肠瘘", "休克未纠正",
    "严重腹胀", "腹腔间隔室综合征",
}


def _first_time_from_drugexe(doc):
    t = doc.get("startTime") or doc.get("exeTime") or doc.get("orderTime")
    if t:
        return t
    his_start = doc.get("hisStartTime")
    if isinstance(his_start, dict):
        return his_start.get("exeTime") or his_start.get("startTime")
    return None


def _drug_names(doc) -> list[str]:
    names = []
    for item in doc.get("drugList", []) or []:
        name = str(item.get("name") or item.get("drugName") or "").strip()
        if name:
            names.append(name)
    for key in ("drugName", "orderName", "name"):
        name = str(doc.get(key) or "").strip()
        if name:
            names.append(name)
    return names


def _drug_codes(doc) -> set:
    codes = set()
    for item in doc.get("drugList", []) or []:
        code = str(item.get("code") or item.get("drugCode") or "").strip()
        if code:
            codes.add(code)
    for key in ("drugCode", "code"):
        code = str(doc.get(key) or "").strip()
        if code:
            codes.add(code)
    return codes


def _is_executed_drugexe(doc) -> bool:
    return (
        doc.get("status") == "finished"
        or doc.get("statusFlag") == "已执行"
        or doc.get("executeStatus") == "已执行"
    )


def _match_enteral_name(names: list[str]) -> tuple[bool, str, str]:
    text = " ".join(names)
    for kw in EN_NAME_WHITELIST:
        if kw in text:
            return True, kw, ""
    for kw in PN_NAME_BLACKLIST:
        if kw in text:
            return False, "", kw
    if ("注射液" in text or "静脉" in text) and "肠内营养" not in text:
        return False, "", "注射液/静脉"
    return False, "", ""


def _match_enteral_route(doc) -> tuple[bool, str]:
    vals = []
    for key in ("methodCode", "methodName", "route", "usage", "useWay"):
        val = str(doc.get(key) or "").strip()
        if val:
            vals.append(val)
    for item in doc.get("drugList", []) or []:
        for key in ("methodCode", "methodName", "route", "usage", "useWay"):
            val = str(item.get(key) or "").strip()
            if val:
                vals.append(val)
    text = " ".join(vals)
    for kw in EN_METHOD_KEYWORDS:
        if kw in text:
            return True, kw
    return False, ""


def _contraindication_evidence(patient: dict) -> list[str]:
    text = " ".join(
        str(patient.get(k) or "")
        for k in ("clinicalDiagnosis", "diagnosis", "admissionDiagnosis")
    )
    return [kw for kw in EN_CONTRAINDICATION_KEYWORDS if kw in text]


def _resolve_en_start_from_bedside(db, pids: list, start_dt, end_dt_wide) -> dict:
    result = {}
    docs = list(db.bedside.find(
        {
            "pid": {"$in": pids},
            "valid": True,
            "code": {"$in": list(EN_BEDSIDE_ROUTE_CODES | EN_BEDSIDE_START_CODES)},
            "time": {"$gte": start_dt, "$lte": end_dt_wide},
        },
        {"_id": 1, "pid": 1, "code": 1, "strVal": 1, "time": 1},
    ).sort("time", 1).max_time_ms(20000).limit(300000))

    latest_route = {}
    for doc in docs:
        pid = doc.get("pid")
        code = doc.get("code")
        val = str(doc.get("strVal") or "").strip()
        t = doc.get("time")
        if not pid or not t:
            continue
        if code in EN_BEDSIDE_ROUTE_CODES and val:
            latest_route[pid] = {"value": val, "time": t}
            continue
        if code not in EN_BEDSIDE_START_CODES:
            continue
        has_start = any(kw in val for kw in EN_BEDSIDE_START_VALUES)
        route = latest_route.get(pid, {})
        route_value = route.get("value", "")
        has_route = any(kw in route_value for kw in EN_ROUTE_KEYWORDS)
        if has_start and (has_route or "肠内" in code or "肠内" in val):
            if pid not in result or t < result[pid]["time"]:
                result[pid] = {
                    "time": t,
                    "source": "bedside",
                    "rule": "en_bedside_start",
                    "hit": val or "开始",
                    "route": route_value,
                    "record_id": str(doc.get("_id", "")),
                }
    return result


def _resolve_en_start_from_drugexe(db, pids: list, start_dt, end_dt_wide) -> dict:
    nutrition_codes = {
        str(d.get("code"))
        for d in db.configDrug.find({"classification": "营养"}, {"code": 1}).max_time_ms(10000)
        if d.get("code")
    }
    docs = list(db.drugExe.find(
        {
            "pid": {"$in": pids},
            "startTime": {"$gte": start_dt, "$lte": end_dt_wide},
            "$or": [
                {"status": "finished"},
                {"statusFlag": "已执行"},
                {"executeStatus": "已执行"},
            ],
        },
        {
            "_id": 1, "pid": 1, "startTime": 1, "exeTime": 1, "hisStartTime": 1,
            "status": 1, "statusFlag": 1, "executeStatus": 1,
            "methodCode": 1, "methodName": 1, "route": 1, "usage": 1, "useWay": 1,
            "drugName": 1, "orderName": 1, "name": 1,
            "drugList.code": 1, "drugList.name": 1, "drugList.methodCode": 1,
            "drugList.methodName": 1, "drugList.route": 1, "drugList.usage": 1,
            "drugList.useWay": 1,
        },
    ).sort("startTime", 1).max_time_ms(30000).limit(500000))

    result = {}
    for doc in docs:
        if not _is_executed_drugexe(doc):
            continue
        pid = doc.get("pid")
        t = _first_time_from_drugexe(doc)
        if not pid or not t:
            continue
        names = _drug_names(doc)
        codes = _drug_codes(doc)
        name_hit, name_kw, pn_kw = _match_enteral_name(names)
        route_hit, route_kw = _match_enteral_route(doc)
        class_hit = bool(nutrition_codes & codes)
        if name_hit:
            evidence = {"source": "name", "rule": "en_name_whitelist", "hit": name_kw}
        elif class_hit and route_hit:
            evidence = {"source": "classification", "rule": "nutrition_enteral", "hit": route_kw}
        elif class_hit and pn_kw:
            continue
        else:
            continue
        if pid not in result or t < result[pid]["time"]:
            result[pid] = {
                **evidence,
                "time": t,
                "route": route_kw,
                "drug_name": ", ".join(names[:3]),
                "record_id": str(doc.get("_id", "")),
            }
    return result


def get_icu19_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    """ICU-19: 48h 内肠内营养(EN)启动率。"""
    from datetime import datetime as dt, timedelta

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)
    result = {"den_count": 0, "num_count": 0, "den_patients": [], "num_patients": []}

    for db_name, db in iter_bed_dbs():
        try:
            patients = list(db.patient.find(
                {
                    "deptCode": {"$in": dept_codes},
                    "status": {"$ne": "invalid"},
                    "icuAdmissionTime": {"$gte": start_dt, "$lte": end_dt_wide},
                },
                {
                    "_id": 1, "hisPid": 1, "mrn": 1, "name": 1, "deptCode": 1,
                    "icuAdmissionTime": 1, "icuDischargeTime": 1, "dischargeTime": 1,
                    "clinicalDiagnosis": 1, "diagnosis": 1, "admissionDiagnosis": 1,
                },
            ).sort("icuAdmissionTime", 1).max_time_ms(20000).limit(200000))
            if not patients:
                continue

            den_patients = []
            pat_by_pid = {}
            for p in patients:
                pid = str(p["_id"])
                admit = p.get("icuAdmissionTime")
                if not admit:
                    continue
                stay_end = p.get("icuDischargeTime") or p.get("dischargeTime") or min(dt.utcnow(), end_dt_wide)
                if stay_end < admit:
                    continue
                if (stay_end - admit).total_seconds() <= 48 * 3600:
                    continue
                p["_pid"] = pid
                p["_stay_end"] = stay_end
                pat_by_pid[pid] = p
                den_patients.append(p)

            if not den_patients:
                break

            pids = list(pat_by_pid.keys())
            bedside_starts = _resolve_en_start_from_bedside(db, pids, start_dt, end_dt_wide)
            drug_starts = _resolve_en_start_from_drugexe(db, pids, start_dt, end_dt_wide)

            den_items = []
            num_items = []
            for pid, p in pat_by_pid.items():
                admit = p.get("icuAdmissionTime")
                candidates = [ev for ev in (bedside_starts.get(pid), drug_starts.get(pid)) if ev]
                ev = min(candidates, key=lambda x: x["time"]) if candidates else None
                en_start = ev.get("time") if ev else None
                within_48h = bool(en_start and en_start - admit <= timedelta(hours=48))
                contraindications = _contraindication_evidence(p)
                item = {
                    "pid": pid,
                    "mrn": p.get("mrn", "") or p.get("hisPid", ""),
                    "patient_id": p.get("hisPid", "") or p.get("mrn", "") or pid[-8:],
                    "name": p.get("name", ""),
                    "dept_code": p.get("deptCode", ""),
                    "icu_admit": admit,
                    "icu_discharge": p.get("_stay_end"),
                    "en_start_time": en_start,
                    "within_48h": within_48h,
                    "source": ev.get("source", "none") if ev else "none",
                    "rule": ev.get("rule", "") if ev else "",
                    "hit": ev.get("hit", "") if ev else "",
                    "route": ev.get("route", "") if ev else "",
                    "drug_name": ev.get("drug_name", "") if ev else "",
                    "contraindication": bool(contraindications),
                    "contraindication_hits": contraindications,
                    "basis": (
                        "48h内启动EN" if within_48h else
                        ("已启动EN但超过48h" if en_start else "未检出EN启动记录")
                    ),
                }
                den_items.append(item)
                if within_48h:
                    num_items.append(item)

            result.update({
                "den_count": len(den_items),
                "num_count": len(num_items),
                "den_patients": den_items,
                "num_patients": num_items,
            })
            break
        except Exception as e:
            print(f"[ICU-19] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue
    return result


TRI_TUBE_CONFIG = {
    "ICU-16": {
        "name": "VAP发病率",
        "diseaseType": "VAP呼吸机相关性肺炎",
        "propertyType": "respirator",
        "denominator": "ventilator",
    },
    "ICU-17": {
        "name": "CRBSI发病率",
        "diseaseType": "CRBSI血管内管道相关血流感染",
        "propertyType": "vascularInfection",
        "denominator": "vascular",
    },
    "CAUTI": {
        "name": "CAUTI尿管相关感染率",
        "diseaseType": "CAUTI尿管相关感染",
        "propertyType": "urinaryTractInfection",
        "denominator": "urinary",
    },
}

TRI_TUBE_EVENT_WINDOW_DAYS = 14
INVASIVE_VENT_VALUES = {"管辅", "切辅", "有创"}
INVASIVE_VENT_KEYWORDS = ("有创呼吸机(气辅)", "有创呼吸机(切辅)")
VASCULAR_TUBE_TYPES = {
    "中心静脉导管", "动脉导管", "PICC管", "PICCO管", "透析管", "血滤管",
    "CRRT管", "中长导管", "输液港",
}
CENTRAL_VASCULAR_TUBE_TYPES = {
    "中心静脉导管", "PICC管", "透析管", "血滤管", "CRRT管",
}
EXCLUDED_VASCULAR_TUBE_TYPES = {
    "动脉导管": "动脉导管-非中心导管",
    "PICCO管": "PICCO管-非中心导管",
    "中长导管": "中长导管-非中心导管",
    "输液港": "输液港-待确认中心属性，默认剔除",
}
URINARY_TUBE_TYPES = {"尿管"}


def _day_range(start_time, end_time):
    from datetime import timedelta

    if not start_time or not end_time:
        return
    # 按北京墙钟日切遍历（存储为 UTC，直接 .date() 会把北京 00-08 点事件归错日）
    day = to_bj_naive(start_time).date()
    last_day = to_bj_naive(end_time).date()
    while day <= last_day:
        yield day
        day += timedelta(days=1)


def _is_invasive_vent_value(value) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text in INVASIVE_VENT_VALUES:
        return True
    return any(keyword in text for keyword in INVASIVE_VENT_KEYWORDS)


def _tri_tube_patient_scope(db, dept_codes, start_dt, end_dt_wide):
    patients = list(db.patient.find(
        {
            "deptCode": {"$in": dept_codes},
            "status": {"$ne": "invalid"},
            "icuAdmissionTime": {"$lte": end_dt_wide},
            "$or": [
                {"icuDischargeTime": {"$gte": start_dt}},
                {"dischargeTime": {"$gte": start_dt}},
                {"icuDischargeTime": None},
                {"icuDischargeTime": {"$exists": False}},
            ],
        },
        {
            "_id": 1, "hisPid": 1, "mrn": 1, "name": 1, "deptCode": 1,
            "icuAdmissionTime": 1, "icuDischargeTime": 1, "dischargeTime": 1,
            "clinicalDiagnosis": 1, "diagnosis": 1, "admissionDiagnosis": 1,
            "dischargedDiagnosis": 1, "diagnosisHistoryList": 1,
        },
    ).max_time_ms(15000).limit(200000))
    return {str(p["_id"]): p for p in patients}


def _patient_item(patient, pid: str) -> dict:
    return {
        "pid": pid,
        "mrn": patient.get("mrn", "") or patient.get("hisPid", ""),
        "patient_id": patient.get("hisPid", "") or patient.get("mrn", "") or pid[-8:],
        "name": patient.get("name", ""),
        "dept_code": patient.get("deptCode", ""),
    }


def _dedup_tri_tube_diagnoses(docs: list) -> list:
    from datetime import timedelta

    docs = sorted(docs, key=lambda d: (d.get("pid", ""), d.get("diseaseType", ""), d.get("diagnosisTime")))
    events = []
    current = None
    window = timedelta(days=TRI_TUBE_EVENT_WINDOW_DAYS)
    for doc in docs:
        dtm = doc.get("diagnosisTime")
        key = (doc.get("pid"), doc.get("diseaseType"))
        if not dtm:
            continue
        if (
            current
            and current["key"] == key
            and dtm - current["last_time"] <= window
        ):
            current["last_time"] = dtm
            current["duplicate_count"] += 1
            current["duplicates"].append(doc)
            continue
        current = {
            "key": key,
            "first": doc,
            "last_time": dtm,
            "duplicate_count": 1,
            "duplicates": [doc],
        }
        events.append(current)
    return events


def _tri_tube_numerator(db, pat_by_pid: dict, cfg: dict, start_dt, end_dt_wide) -> list:
    docs = list(db.diseaseDiagnosis.find(
        {
            "pid": {"$in": list(pat_by_pid.keys())},
            "diagnosisTime": {"$gte": start_dt, "$lte": end_dt_wide},
            "$or": [
                {"diseaseType": cfg["diseaseType"]},
                {"propertyType": cfg["propertyType"]},
            ],
        },
        {
            "_id": 1, "pid": 1, "diseaseType": 1, "propertyType": 1,
            "diagnosisTime": 1, "notes": 1, "lastEditUserId": 1,
        },
    ).sort("diagnosisTime", 1).max_time_ms(15000).limit(100000))

    patients = []
    for event in _dedup_tri_tube_diagnoses(docs):
        doc = event["first"]
        pid = doc.get("pid")
        patient = pat_by_pid.get(pid, {})
        item = _patient_item(patient, pid)
        item.update({
            "diagnosis_id": str(doc.get("_id", "")),
            "diseaseType": doc.get("diseaseType", ""),
            "propertyType": doc.get("propertyType", ""),
            "diagnosisTime": doc.get("diagnosisTime"),
            "notes": doc.get("notes", "") or "",
            "lastEditUserId": doc.get("lastEditUserId", "") or "",
            "duplicate_count": event["duplicate_count"],
            "dedup_basis": f"同一患者同一感染类型{TRI_TUBE_EVENT_WINDOW_DAYS}天事件窗内去重",
        })
        patients.append(item)
    return patients


def _ventilator_days(db, pat_by_pid: dict, start_dt, end_dt_wide) -> tuple[int, list]:
    day_rows = {}
    cursor = db.bedside.find(
        {
            "pid": {"$in": list(pat_by_pid.keys())},
            "code": "param_XiYangTuJing",
            "valid": True,
            "time": {"$gte": start_dt, "$lte": end_dt_wide},
            "$or": [
                {"strVal": {"$in": list(INVASIVE_VENT_VALUES)}},
                {"strVal": {"$regex": "有创呼吸机"}},
            ],
        },
        {"_id": 1, "pid": 1, "strVal": 1, "time": 1},
    ).sort("time", 1).max_time_ms(30000).limit(500000)
    for doc in cursor:
        if not _is_invasive_vent_value(doc.get("strVal")):
            continue
        t = doc.get("time")
        pid = doc.get("pid")
        if not t or not pid:
            continue
        key = (pid, to_bj_naive(t).date())
        if key in day_rows:
            continue
        patient = pat_by_pid.get(pid, {})
        item = _patient_item(patient, pid)
        item.update({
            "device_type": "有创呼吸机",
            "device_value": doc.get("strVal", ""),
            "device_day": fmt_bj(t, "%Y-%m-%d"),
            "record_time": t,
            "basis": "当日氧疗途径为有创机械通气",
        })
        day_rows[key] = item
    return len(day_rows), list(day_rows.values())


def _tube_days(db, pat_by_pid: dict, tube_types: set, start_dt, end_dt_wide, label: str) -> tuple[int, list]:
    day_rows = {}
    query = {
        "pid": {"$in": list(pat_by_pid.keys())},
        "type": {"$in": list(tube_types)},
        "startTime": {"$lte": end_dt_wide},
        "$or": [
            {"endTime": {"$gte": start_dt}},
            {"endTime": None},
            {"endTime": {"$exists": False}},
        ],
    }
    cursor = db.tubeExe.find(
        query,
        {"_id": 1, "pid": 1, "type": 1, "startTime": 1, "endTime": 1, "replace": 1},
    ).sort("startTime", 1).max_time_ms(30000).limit(300000)
    for doc in cursor:
        pid = doc.get("pid")
        start_time = doc.get("startTime")
        if not pid or not start_time:
            continue
        patient = pat_by_pid.get(pid, {})
        fallback_end = patient.get("icuDischargeTime") or patient.get("dischargeTime") or end_dt_wide
        end_time = doc.get("endTime") or fallback_end
        span_start = max(start_time, start_dt)
        span_end = min(end_time, end_dt_wide)
        if span_end < span_start:
            continue
        for day in _day_range(span_start, span_end):
            key = (pid, day)
            if key in day_rows:
                continue
            item = _patient_item(patient, pid)
            item.update({
                "tube_id": str(doc.get("_id", "")),
                "device_type": label,
                "tube_type": doc.get("type", ""),
                "device_day": day.strftime("%Y-%m-%d"),
                "tube_start": start_time,
                "tube_end": end_time,
                "basis": f"当日存在{label}留置记录",
            })
            day_rows[key] = item
    return len(day_rows), list(day_rows.values())


def _central_line_days(db, pat_by_pid: dict, start_dt, end_dt_wide) -> tuple[int, list]:
    included = {}
    excluded_by_patient_day = {}
    all_vascular_types = CENTRAL_VASCULAR_TUBE_TYPES | set(EXCLUDED_VASCULAR_TUBE_TYPES)
    query = {
        "pid": {"$in": list(pat_by_pid.keys())},
        "type": {"$in": list(all_vascular_types)},
        "startTime": {"$lte": end_dt_wide},
        "$or": [
            {"endTime": {"$gte": start_dt}},
            {"endTime": None},
            {"endTime": {"$exists": False}},
        ],
    }
    cursor = db.tubeExe.find(
        query,
        {"_id": 1, "pid": 1, "type": 1, "startTime": 1, "endTime": 1, "replace": 1},
    ).sort("startTime", 1).max_time_ms(30000).limit(300000)

    for doc in cursor:
        pid = doc.get("pid")
        tube_type = doc.get("type", "")
        start_time = doc.get("startTime")
        if not pid or not start_time:
            continue
        patient = pat_by_pid.get(pid, {})
        fallback_end = patient.get("icuDischargeTime") or patient.get("dischargeTime") or end_dt_wide
        end_time = doc.get("endTime") or fallback_end
        span_start = max(start_time, start_dt)
        span_end = min(end_time, end_dt_wide)
        if span_end < span_start:
            continue

        is_central = tube_type in CENTRAL_VASCULAR_TUBE_TYPES
        exclude_reason = "" if is_central else EXCLUDED_VASCULAR_TUBE_TYPES.get(tube_type, "非中心血管导管")
        evidence = {
            "tube_id": str(doc.get("_id", "")),
            "tube_type": tube_type,
            "tube_start": start_time,
            "tube_end": end_time,
            "included": is_central,
            "exclude_reason": exclude_reason,
        }
        for day in _day_range(span_start, span_end):
            key = (pid, day)
            if not is_central:
                excluded_by_patient_day.setdefault(key, []).append(evidence)
                continue

            item = included.get(key)
            if not item:
                item = _patient_item(patient, pid)
                item.update({
                    "tube_id": evidence["tube_id"],
                    "device_type": "中心血管导管",
                    "tube_type": tube_type,
                    "device_day": day.strftime("%Y-%m-%d"),
                    "tube_start": start_time,
                    "tube_end": end_time,
                    "included": True,
                    "exclude_reason": "",
                    "tube_ids": [evidence["tube_id"]],
                    "tube_types": [tube_type],
                    "included_segments": [evidence],
                    "excluded_evidence": [],
                    "dedup_basis": "患者-中心导管日：同一患者同一日存在≥1根中心导管计1日",
                    "basis": "当日存在中心血管导管留置记录",
                })
                included[key] = item
                continue

            item["tube_ids"].append(evidence["tube_id"])
            if tube_type not in item["tube_types"]:
                item["tube_types"].append(tube_type)
            item["included_segments"].append(evidence)
            if start_time < item["tube_start"]:
                item["tube_start"] = start_time
            if end_time > item["tube_end"]:
                item["tube_end"] = end_time
            item["tube_type"] = "、".join(item["tube_types"])
            item["tube_id"] = ",".join(item["tube_ids"])
            item["basis"] = f"当日存在{len(item['tube_ids'])}根中心血管导管，按患者-日去重计1日"

    for key, evidence_list in excluded_by_patient_day.items():
        if key in included:
            included[key]["excluded_evidence"].extend(evidence_list)

    return len(included), list(included.values())


def get_tri_tube_infection_data(code: str, dept_codes: list, start_date: str, end_date: str) -> dict:
    from datetime import datetime as dt

    cfg = TRI_TUBE_CONFIG[code]
    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)
    result = {
        "num_count": 0,
        "den_count": 0,
        "value": 0.0,
        "value_type": "permille",
        "num_patients": [],
        "den_patients": [],
        "note": "",
    }

    for db_name, db in iter_bed_dbs():
        try:
            pat_by_pid = _tri_tube_patient_scope(db, dept_codes, start_dt, end_dt_wide)
            if not pat_by_pid:
                continue
            num_patients = _tri_tube_numerator(db, pat_by_pid, cfg, start_dt, end_dt_wide)
            denom = cfg["denominator"]
            if denom == "ventilator":
                den_count, den_patients = _ventilator_days(db, pat_by_pid, start_dt, end_dt_wide)
            elif denom == "vascular":
                den_count, den_patients = _central_line_days(db, pat_by_pid, start_dt, end_dt_wide)
            else:
                den_count, den_patients = _tube_days(db, pat_by_pid, URINARY_TUBE_TYPES, start_dt, end_dt_wide, "导尿管")

            num_count = len(num_patients)
            result.update({
                "num_count": num_count,
                "den_count": den_count,
                "value": round(num_count / den_count * 1000, 2) if den_count > 0 else 0.0,
                "num_patients": num_patients,
                "den_patients": den_patients,
                "note": "" if den_count > 0 else "无导管/通气使用记录",
            })
            break
        except Exception as e:
            print(f"[{code}] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue
    return result


def get_icu16_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    return get_tri_tube_infection_data("ICU-16", dept_codes, start_date, end_date)


def get_icu17_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    return get_tri_tube_infection_data("ICU-17", dept_codes, start_date, end_date)


def get_cauti_data(dept_codes: list, start_date: str, end_date: str) -> dict:
    return get_tri_tube_infection_data("CAUTI", dept_codes, start_date, end_date)


# ============================================================
# 三管感染疑似预警：仅作为医生确认线索，不计入正式分子
# ============================================================

TRI_TUBE_WARNING_TYPES = {
    "VAP": TRI_TUBE_CONFIG["ICU-16"],
    "CRBSI": TRI_TUBE_CONFIG["ICU-17"],
    "CAUTI": TRI_TUBE_CONFIG["CAUTI"],
}


def _safe_float(value):
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _confirmed_tri_tube_keys(db, pids: list, start_dt, end_dt_wide) -> set:
    docs = db.diseaseDiagnosis.find(
        {
            "pid": {"$in": pids},
            "diagnosisTime": {"$gte": start_dt, "$lte": end_dt_wide},
            "propertyType": {"$in": [cfg["propertyType"] for cfg in TRI_TUBE_WARNING_TYPES.values()]},
        },
        {"pid": 1, "propertyType": 1},
    ).max_time_ms(10000).limit(100000)
    return {(d.get("pid"), d.get("propertyType")) for d in docs}


def _fever_evidence(db, pids: list, start_dt, end_dt_wide) -> dict:
    evidence = {}
    if "temperatureData" not in db.list_collection_names():
        return evidence
    for doc in db.temperatureData.find(
        {
            "pid": {"$in": pids},
            "record_time": {"$gte": start_dt, "$lte": end_dt_wide},
        },
        {"pid": 1, "record_time": 1, "Temperature": 1},
    ).sort("record_time", -1).max_time_ms(15000).limit(200000):
        temp = _safe_float(doc.get("Temperature"))
        if temp is not None and temp >= 38:
            evidence.setdefault(doc.get("pid"), []).append({
                "type": "发热",
                "time": _fmt_dt(doc.get("record_time")),
                "value": f"T={temp}",
            })
    return evidence


def _wbc_evidence(db, pids: list, start_dt, end_dt_wide) -> dict:
    evidence = {}
    if "criticalValue" not in db.list_collection_names():
        return evidence
    # 仅按 pid + 时间范围查询（可走索引），文本过滤移到 Python 侧
    for doc in db.criticalValue.find(
        {
            "pid": {"$in": list(pids)},
            "publishTime": {"$gte": start_dt, "$lte": end_dt_wide},
        },
        {"pid": 1, "lisItem": 1, "value": 1, "publishTime": 1, "bigItemName": 1},
    ).sort("publishTime", -1).max_time_ms(15000).limit(20000):
        text = f"{doc.get('lisItem', '')} {doc.get('value', '')} {doc.get('bigItemName', '')}"
        if "白细胞" not in text and "WBC" not in text.upper():
            continue
        evidence.setdefault(doc.get("pid"), []).append({
            "type": "WBC异常危急值",
            "time": _fmt_dt(doc.get("publishTime")),
            "value": text[:80],
        })
    return evidence


def _sputum_evidence(db, pids: list, start_dt, end_dt_wide) -> dict:
    evidence = {}
    sputum_codes = ["param_痰液护理", "param_tanColor", "param_tanLiang", "param_tanYeFenJi"]
    param_names = {
        d.get("code"): d.get("name")
        for d in db.configParam.find(
            {"code": {"$in": sputum_codes}},
            {"code": 1, "name": 1, "_id": 0},
        ).max_time_ms(10000)
        if d.get("code") and d.get("name")
    }
    for doc in db.bedside.find(
        {
            "pid": {"$in": pids},
            "valid": True,
            "time": {"$gte": start_dt, "$lte": end_dt_wide},
            "code": {"$in": sputum_codes},
        },
        {"pid": 1, "code": 1, "strVal": 1, "time": 1},
    ).sort("time", -1).max_time_ms(15000).limit(100000):
        text = str(doc.get("strVal") or "").strip()
        if not text:
            continue
        param_name = param_names.get(doc.get("code")) or "痰液护理"
        evidence.setdefault(doc.get("pid"), []).append({
            "type": "痰液护理记录",
            "time": _fmt_dt(doc.get("time")),
            "value": f"{param_name}={text}"[:80],
        })
    return evidence


SEPSIS_INFECTION_KEYWORDS = {
    "感染", "肺炎", "脓毒", "败血", "菌血", "腹膜炎", "脓肿", "化脓",
    "胆管炎", "肾盂肾炎", "尿路感染", "泌尿系感染", "腹腔感染",
}
SEPSIS_BEDSIDE_NAME_RULES = {
    "resp_rate": ("呼吸频率", "呼吸次数", "呼吸"),
    "sbp": ("收缩压", "高压"),
    "map": ("平均动脉压", "MAP"),
    "consciousness": ("意识", "GCS", "格拉斯哥"),
    "urine_output": ("尿量",),
    "vasopressor": ("升压药", "去甲肾上腺素", "多巴胺", "肾上腺素", "血管活性"),
}
SEPSIS_EXAM_CODES = {
    "wbc": {"WBC", "WBCJS"},
    "crp": {"CRP", "sCRP"},
    "pct": {"PCT1"},
    "lactate": {"LAC", "LACT", "LAC1", "LAC2"},
}


def _patient_diagnosis_text(patient: dict) -> str:
    parts = []
    for key in ("clinicalDiagnosis", "diagnosis", "admissionDiagnosis", "dischargedDiagnosis"):
        if patient.get(key):
            parts.append(str(patient.get(key)))
    for item in patient.get("diagnosisHistoryList") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("diagnosis") or item.get("name") or ""))
        elif item:
            parts.append(str(item))
    return "；".join(p for p in parts if p)


def _latest_numeric_bedside(db, pids: list, codes: list, start_dt, end_dt_wide) -> dict:
    result = {}
    if not codes:
        return result
    for doc in db.bedside.find(
        {
            "pid": {"$in": pids},
            "code": {"$in": codes},
            "valid": True,
            "time": {"$gte": start_dt, "$lte": end_dt_wide},
        },
        {"pid": 1, "strVal": 1, "time": 1},
    ).sort("time", -1).max_time_ms(20000).limit(200000):
        pid = doc.get("pid")
        if pid in result:
            continue
        val = _safe_float(doc.get("strVal"))
        if val is not None:
            result[pid] = {"value": val, "time": doc.get("time")}
    return result


def _latest_text_bedside(db, pids: list, codes: list, start_dt, end_dt_wide) -> dict:
    result = {}
    if not codes:
        return result
    for doc in db.bedside.find(
        {
            "pid": {"$in": pids},
            "code": {"$in": codes},
            "valid": True,
            "time": {"$gte": start_dt, "$lte": end_dt_wide},
        },
        {"pid": 1, "strVal": 1, "time": 1},
    ).sort("time", -1).max_time_ms(20000).limit(200000):
        pid = doc.get("pid")
        text = str(doc.get("strVal") or "").strip()
        if pid not in result and text:
            result[pid] = {"value": text, "time": doc.get("time")}
    return result


_sepsis_codes_cache = None  # configParam 很少变化，内存缓存


def _sepsis_bedside_codes(db) -> dict:
    global _sepsis_codes_cache
    if _sepsis_codes_cache is not None:
        return _sepsis_codes_cache
    codes = {key: [] for key in SEPSIS_BEDSIDE_NAME_RULES}
    for doc in db.configParam.find(
        {},
        {"code": 1, "name": 1, "_id": 0},
    ).max_time_ms(10000).limit(20000):
        code = doc.get("code")
        text = f"{doc.get('name', '')} {code or ''}"
        if not code:
            continue
        for key, keywords in SEPSIS_BEDSIDE_NAME_RULES.items():
            if any(kw.lower() in text.lower() for kw in keywords):
                codes[key].append(code)
    codes["temperature"] = list(set(codes.get("temperature", []) + ["param_T"]))
    codes["consciousness"] = list(set(codes.get("consciousness", []) + ["param_score_gcs_obs"]))
    _sepsis_codes_cache = codes
    return codes


def _sepsis_exam_values(db_dc, hispids: list, start_dt, end_dt_wide) -> dict:
    result = {}
    if not hispids:
        return result
    try:
        exams = list(db_dc["VI_ICU_EXAM"].find(
            # #fix: VI_ICU_EXAM 实际字段为 hisPid（原查 "pid" 恒为空）
            {"hisPid": {"$in": hispids}, "collectTime": {"$gte": start_dt, "$lte": end_dt_wide}},
            {"hisPid": 1, "examID": 1, "reportID": 1, "collectTime": 1},
        ).max_time_ms(20000).limit(200000))
        exam_ids = []
        exam_info = {}
        for e in exams:
            eid = e.get("examID") or e.get("reportID")
            if not eid:
                continue
            exam_ids.append(eid)
            exam_info[str(eid)] = {"hisPid": e.get("hisPid"), "time": e.get("collectTime")}
        if not exam_ids:
            return result
        target_codes = set().union(*SEPSIS_EXAM_CODES.values())
        for item in db_dc["VI_ICU_EXAM_ITEM"].find(
            {"examID": {"$in": exam_ids}, "itemCode": {"$in": list(target_codes)}},
            {"examID": 1, "itemCode": 1, "itemName": 1, "itemValue": 1, "authTime": 1},
        ).sort("authTime", -1).max_time_ms(20000).limit(300000):
            info = exam_info.get(str(item.get("examID")))
            if not info:
                continue
            hispid = info.get("hisPid")
            val = _clean_test_value(item.get("itemValue"))
            if not hispid or val is None:
                continue
            code = item.get("itemCode")
            item_name = str(item.get("itemName") or "")
            metric = None
            for key, code_set in SEPSIS_EXAM_CODES.items():
                if code in code_set:
                    metric = key
                    break
            if metric == "pct" and "血小板" in item_name:
                continue
            if not metric:
                continue
            patient_values = result.setdefault(str(hispid), {})
            if metric not in patient_values:
                patient_values[metric] = {"value": val, "time": info.get("time") or item.get("authTime")}
    except Exception:
        return result
    return result


def _sepsis_lactate_from_bga(db, pids: list, start_dt, end_dt_wide) -> dict:
    result = {}
    collections = db.list_collection_names()
    coll_name = "BGATemp" if "BGATemp" in collections else "bGATemp"
    if coll_name not in collections:
        return result
    try:
        for doc in db[coll_name].find(
            {
                "eventExe.pid": {"$in": pids},
                "eventExe.startTime": {"$gte": start_dt, "$lte": end_dt_wide},
                "bedsides": {"$elemMatch": {"code": "param_bg_Lac"}},
            },
            {"eventExe.pid": 1, "eventExe.startTime": 1, "bedsides": 1},
        ).sort("eventExe.startTime", -1).max_time_ms(20000).limit(200000):
            pid = (doc.get("eventExe") or {}).get("pid")
            if not pid or pid in result:
                continue
            for item in doc.get("bedsides") or []:
                if item.get("code") != "param_bg_Lac":
                    continue
                val = _clean_test_value(item.get("strVal") or item.get("value") or item.get("val"))
                if val is not None:
                    result[pid] = {
                        "value": val,
                        "time": (doc.get("eventExe") or {}).get("startTime"),
                    }
                    break
    except Exception:
        return result
    return result


def _sepsis_rule_candidate(ctx: dict) -> bool:
    diagnosis = str(ctx.get("diagnosis") or "")
    infection = any(kw in diagnosis for kw in SEPSIS_INFECTION_KEYWORDS)
    temp = _safe_float(ctx.get("temperature"))
    rr = _safe_float(ctx.get("resp_rate"))
    sbp = _safe_float(ctx.get("sbp"))
    lactate = _safe_float(ctx.get("lactate"))
    wbc = _safe_float(ctx.get("wbc"))
    pct = _safe_float(ctx.get("pct"))
    crp = _safe_float(ctx.get("crp"))
    return (
        infection
        or (temp is not None and (temp >= 38.3 or temp < 36))
        or (rr is not None and rr >= 22)
        or (sbp is not None and sbp <= 100)
        or (lactate is not None and lactate >= 2)
        or (wbc is not None and (wbc > WBC_HIGH or wbc < WBC_LOW))
        or (pct is not None and pct > PCT_HIGH)
        or (crp is not None and crp > CRP_HIGH)
    )


def get_sepsis_alert_warnings(dept_codes: list, start_date: str, end_date: str, limit: int = 30) -> list:
    from ai_analyzer import classify_sepsis_alert_with_ai
    from datetime import datetime as dt

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)
    warnings = []

    for db_name, db in iter_bed_dbs():
        try:
            pat_by_pid = _tri_tube_patient_scope(db, dept_codes, start_dt, end_dt_wide)
            if not pat_by_pid:
                continue
            pids = list(pat_by_pid.keys())
            hispids = [str(p.get("hisPid")) for p in pat_by_pid.values() if p.get("hisPid")]
            codes = _sepsis_bedside_codes(db)
            temperature = _latest_numeric_bedside(db, pids, codes.get("temperature", []), start_dt, end_dt_wide)
            resp_rate = _latest_numeric_bedside(db, pids, codes.get("resp_rate", []), start_dt, end_dt_wide)
            sbp = _latest_numeric_bedside(db, pids, codes.get("sbp", []), start_dt, end_dt_wide)
            map_values = _latest_numeric_bedside(db, pids, codes.get("map", []), start_dt, end_dt_wide)
            consciousness = _latest_text_bedside(db, pids, codes.get("consciousness", []), start_dt, end_dt_wide)
            urine_output = _latest_text_bedside(db, pids, codes.get("urine_output", []), start_dt, end_dt_wide)
            vasopressor = _latest_text_bedside(db, pids, codes.get("vasopressor", []), start_dt, end_dt_wide)
            lactate_bga = _sepsis_lactate_from_bga(db, pids, start_dt, end_dt_wide)

            exam_values = {}
            try:
                db_dc = get_datacenter_client()["DataCenter"]
                exam_values = _sepsis_exam_values(db_dc, hispids, start_dt, end_dt_wide)
            except Exception:
                exam_values = {}

            scored = []
            for pid, patient in pat_by_pid.items():
                hispid = str(patient.get("hisPid") or "")
                exams = exam_values.get(hispid, {})
                ctx = {
                    "hisPid": hispid or patient.get("mrn") or pid,
                    "sample_time": _fmt_dt(end_dt_wide),
                    "diagnosis": _patient_diagnosis_text(patient) or "未知",
                    "temperature": (temperature.get(pid) or {}).get("value", "?"),
                    "resp_rate": (resp_rate.get(pid) or {}).get("value", "?"),
                    "sbp": (sbp.get(pid) or {}).get("value", "?"),
                    "map": (map_values.get(pid) or {}).get("value", "?"),
                    "consciousness": (consciousness.get(pid) or {}).get("value", "?"),
                    "wbc": (exams.get("wbc") or {}).get("value", "?"),
                    "pct": (exams.get("pct") or {}).get("value", "?"),
                    "crp": (exams.get("crp") or {}).get("value", "?"),
                    "lactate": (lactate_bga.get(pid) or exams.get("lactate") or {}).get("value", "?"),
                    "urine_output": (urine_output.get(pid) or {}).get("value", "?"),
                    "vasopressor": (vasopressor.get(pid) or {}).get("value", "?"),
                }
                if not _sepsis_rule_candidate(ctx):
                    continue
                priority = 0
                for key in ("lactate", "resp_rate", "sbp", "temperature", "wbc", "pct", "crp"):
                    priority += 1 if ctx.get(key) != "?" else 0
                scored.append((priority, pid, patient, ctx))

            for _, pid, patient, ctx in sorted(scored, reverse=True)[:limit]:
                result = classify_sepsis_alert_with_ai(ctx)
                if not result or result.get("risk") == "low":
                    continue
                item = _patient_item(patient, pid)
                qsofa = result.get("qsofa")
                evidence = [{"type": "判定依据", "value": result.get("reason", "")}]
                if qsofa is not None:
                    evidence.insert(0, {"type": "qSOFA", "value": str(qsofa)})
                else:
                    evidence.insert(0, {"type": "qSOFA", "value": "待评估 / 解析失败，请人工复核"})
                evidence.append({"type": "分诊建议", "value": result.get("action", "")})
                item.update({
                    "type": "脓毒症早期预警",
                    "risk": result.get("risk", "unknown"),
                    "qsofa": qsofa,
                    "suspect_sepsis": result.get("suspect_sepsis", False),
                    "basis": result.get("reason", ""),
                    "action": result.get("action", ""),
                    "by": result.get("by", "ai"),
                    "evaluated": result.get("evaluated", result.get("by") != "fallback"),
                    "confidence": 0.9 if result.get("risk") == "high" else 0.7,
                    "evidence": evidence,
                })
                warnings.append(item)
            break
        except Exception as e:
            print(f"[sepsis-alert] Error in db {db_name}: {e}")
            continue

    risk_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(warnings, key=lambda x: (risk_rank.get(x.get("risk"), 9), -float(x.get("confidence", 0))))


def _long_vent_patients(db, pat_by_pid: dict, start_dt, end_dt_wide, hours: int) -> dict:
    records = defaultdict(list)
    # 按 pid+code+time 查询（可走索引），regex 过滤移到 Python 侧
    for doc in db.bedside.find(
        {
            "pid": {"$in": list(pat_by_pid.keys())},
            "code": "param_XiYangTuJing",
            "valid": True,
            "time": {"$gte": start_dt, "$lte": end_dt_wide},
        },
        {"pid": 1, "strVal": 1, "time": 1},
    ).sort("time", 1).max_time_ms(30000).limit(300000):
        if _is_invasive_vent_value(doc.get("strVal")):
            records[doc.get("pid")].append(doc)
    result = {}
    for pid, docs in records.items():
        first = docs[0].get("time")
        last = docs[-1].get("time")
        if first and last and (last - first).total_seconds() >= hours * 3600:
            result[pid] = {
                "first_time": first,
                "last_time": last,
                "value": docs[0].get("strVal"),
            }
    return result


def _long_tube_patients(db, pat_by_pid: dict, tube_types: set, start_dt, end_dt_wide, hours: int) -> dict:
    result = {}
    for doc in db.tubeExe.find(
        {
            "pid": {"$in": list(pat_by_pid.keys())},
            "type": {"$in": list(tube_types)},
            "startTime": {"$lte": end_dt_wide},
            "$or": [
                {"endTime": {"$gte": start_dt}},
                {"endTime": None},
                {"endTime": {"$exists": False}},
            ],
        },
        {"pid": 1, "type": 1, "startTime": 1, "endTime": 1},
    ).sort("startTime", 1).max_time_ms(30000).limit(200000):
        pid = doc.get("pid")
        st = doc.get("startTime")
        if not pid or not st:
            continue
        patient = pat_by_pid.get(pid, {})
        et = doc.get("endTime") or patient.get("icuDischargeTime") or patient.get("dischargeTime") or end_dt_wide
        if et and (min(et, end_dt_wide) - max(st, start_dt)).total_seconds() >= hours * 3600:
            result.setdefault(pid, {
                "first_time": st,
                "last_time": et,
                "value": doc.get("type", ""),
            })
    return result


def get_tri_tube_suspected_warnings(dept_codes: list, start_date: str, end_date: str, min_hours: int = 48) -> list:
    from datetime import datetime as dt
    from collections import defaultdict

    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_dt_wide = utc_day_end_wide(end_date)
    warnings = []

    for db_name, db in iter_bed_dbs():
        try:
            pat_by_pid = _tri_tube_patient_scope(db, dept_codes, start_dt, end_dt_wide)
            if not pat_by_pid:
                continue
            pids = list(pat_by_pid.keys())
            confirmed = _confirmed_tri_tube_keys(db, pids, start_dt, end_dt_wide)
            fever = _fever_evidence(db, pids, start_dt, end_dt_wide)
            wbc = _wbc_evidence(db, pids, start_dt, end_dt_wide)
            sputum = _sputum_evidence(db, pids, start_dt, end_dt_wide)

            long_vent = _long_vent_patients(db, pat_by_pid, start_dt, end_dt_wide, min_hours)
            long_vascular = _long_tube_patients(db, pat_by_pid, VASCULAR_TUBE_TYPES, start_dt, end_dt_wide, min_hours)
            long_urinary = _long_tube_patients(db, pat_by_pid, URINARY_TUBE_TYPES, start_dt, end_dt_wide, min_hours)

            def append_warning(pid, warn_type, device_info, extra_evidence):
                cfg = TRI_TUBE_WARNING_TYPES[warn_type]
                if (pid, cfg["propertyType"]) in confirmed:
                    return
                patient = pat_by_pid.get(pid, {})
                evidence = [{
                    "type": "装置留置超过阈值",
                    "time": f"{_fmt_dt(device_info.get('first_time'))} ~ {_fmt_dt(device_info.get('last_time'))}",
                    "value": f"{device_info.get('value')} > {min_hours}h",
                }]
                evidence.extend(extra_evidence[:5])
                if len(evidence) < 2:
                    return
                confidence = min(0.95, 0.45 + 0.15 * (len(evidence) - 1))
                item = _patient_item(patient, pid)
                item.update({
                    "suspect_type": warn_type,
                    "diseaseType": cfg["diseaseType"],
                    "propertyType": cfg["propertyType"],
                    "confidence": round(confidence, 2),
                    "evidence": evidence,
                    "suggestion": "AI疑似预警，需医生确认后才写入正式诊断",
                })
                warnings.append(item)

            for pid, info in long_vent.items():
                append_warning(pid, "VAP", info, fever.get(pid, []) + wbc.get(pid, []) + sputum.get(pid, []))
            for pid, info in long_vascular.items():
                append_warning(pid, "CRBSI", info, fever.get(pid, []) + wbc.get(pid, []))
            for pid, info in long_urinary.items():
                append_warning(pid, "CAUTI", info, fever.get(pid, []) + wbc.get(pid, []))
            break
        except Exception as e:
            print(f"[tri-tube-warning] Error in db {db_name}: {e}")
            import traceback
            traceback.print_exc()
            continue
    return sorted(warnings, key=lambda x: x.get("confidence", 0), reverse=True)


def confirm_tri_tube_warning(pid: str, suspect_type: str, diagnosis_time, user_id: str, notes: str = "") -> dict:
    cfg = TRI_TUBE_WARNING_TYPES.get(suspect_type)
    if not cfg:
        raise ValueError("unsupported suspect_type")
    from bson import ObjectId
    for db_name, db in iter_bed_dbs():
        patient = None
        if len(pid) == 24:
            try:
                patient = db.patient.find_one({"_id": ObjectId(pid)})
            except Exception:
                patient = None
        if not patient:
            patient = db.patient.find_one({"hisPid": pid}) or db.patient.find_one({"mrn": pid})
        if not patient:
            continue
        normalized_pid = str(patient["_id"])
        doc = {
            "pid": normalized_pid,
            "diseaseType": cfg["diseaseType"],
            "propertyType": cfg["propertyType"],
            "diagnosisTime": diagnosis_time,
            "notes": notes or "AI疑似预警经医生确认录入",
            "lastEditUserId": user_id,
        }
        inserted = db.diseaseDiagnosis.insert_one(doc)
        return {"inserted_id": str(inserted.inserted_id), **doc}
    raise ValueError("patient not found")


# ============================================================
# 连接测试
# ============================================================
def get_patient_census(dept_codes: list, start_date: str, end_date: str,
                       distinct_by: str = "admission") -> dict:
    """
    carry_in (原有) : icuAdmissionTime <  期初0点 且 (未出科 或 icuDischargeTime >= 期初0点)
    new_admit(新入) : 期初0点 <= icuAdmissionTime <= 期末23:59:59
    discharge(出科) : 期初0点 <= icuDischargeTime  <= 期末23:59:59
    carry_out(期末在科) = carry_in + new_admit - discharge
    total(同期患者总数) = carry_in + new_admit
    distinct_by: "admission"=按住院次(patient文档) / "patient"=按 mrn 去重
    """
    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_wide = utc_day_end_wide(end_date)
    base = {"deptCode": {"$in": dept_codes}, "status": {"$ne": "invalid"}}
    open_ = [{"icuDischargeTime": None}, {"icuDischargeTime": {"$exists": False}}]

    q_carry_in = {**base, "icuAdmissionTime": {"$lt": start_dt},
                  "$or": open_ + [{"icuDischargeTime": {"$gte": start_dt}}]}
    q_new      = {**base, "icuAdmissionTime": {"$gte": start_dt, "$lte": end_wide}}
    q_out      = {**base, "icuDischargeTime": {"$gte": start_dt, "$lte": end_wide}}

    for db_name, db in iter_bed_dbs():
        try:
            if distinct_by == "patient":
                cnt = lambda q: len(db.patient.distinct("mrn", q))
            else:
                cnt = lambda q: db.patient.count_documents(q)
            carry_in, new_admit, discharge = cnt(q_carry_in), cnt(q_new), cnt(q_out)
            return {"carry_in": carry_in, "new_admit": new_admit,
                    "discharge": discharge,
                    "carry_out": carry_in + new_admit - discharge,
                    "total": carry_in + new_admit}
        except Exception as _exc:
            logger.warning("DB %s failed in get_patient_census: %s", db_name, _exc)
            continue
    return {"carry_in": 0, "new_admit": 0, "discharge": 0, "carry_out": 0, "total": 0}


def get_patient_census_detail(dept_codes: list, start_date: str, end_date: str,
                               part: str = "carry_in") -> list:
    """
    返回指定 part 的患者明细列表。
    part: carry_in / new_admit / discharge / carry_out (carry_out = carry_in + new_admit - discharge 中仍在科的)
          / total (分母口径：同期患者总数 = 原有 + 新入，与 get_patient_census 的 total 同口径)
    """
    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)
    end_wide = utc_day_end_wide(end_date)
    base = {"deptCode": {"$in": dept_codes}, "status": {"$ne": "invalid"}}
    open_ = [{"icuDischargeTime": None}, {"icuDischargeTime": {"$exists": False}}]

    q_carry_in = {**base, "icuAdmissionTime": {"$lt": start_dt},
                  "$or": open_ + [{"icuDischargeTime": {"$gte": start_dt}}]}
    q_new      = {**base, "icuAdmissionTime": {"$gte": start_dt, "$lte": end_wide}}
    q_out      = {**base, "icuDischargeTime": {"$gte": start_dt, "$lte": end_wide}}

    for db_name, db in iter_bed_dbs():
        try:
            if part == "carry_in":
                q = q_carry_in
                patient_type = "原有"
            elif part == "new_admit":
                q = q_new
                patient_type = "新入"
            elif part == "discharge":
                q = q_out
                patient_type = "出科"
            elif part == "carry_out":
                q = q_carry_in
                patient_type = None  # 需要排除出科的
            elif part == "total":
                q = None  # 原有 + 新入 两组查询后拼接，patient_type 逐患者标注
                patient_type = None
            else:
                return []

            fields = {"_id": 1, "mrn": 1, "name": 1, "hisBed": 1,
                      "icuAdmissionTime": 1, "icuDischargeTime": 1}
            if part == "total":
                # carry_in(入科<月初) 与 new_admit(入科>=月初) 按入科时间互斥，直接拼接无重复
                docs = list(db.patient.find(q_carry_in, fields)) + \
                       list(db.patient.find(q_new, fields))
            else:
                docs = list(db.patient.find(q, fields))
            if part == "carry_out":
                docs = [d for d in docs
                        if not d.get("icuDischargeTime") or d["icuDischargeTime"] >= start_dt]

            results = []
            for d in docs:
                admit = d.get("icuAdmissionTime")
                discharge = d.get("icuDischargeTime")
                los = None
                if admit:
                    ref = discharge or datetime.utcnow()
                    los = (ref - admit).days
                if part == "total":
                    ptype = "原有" if (admit and admit < start_dt) else "新入"
                else:
                    ptype = patient_type or ("原有" if part == "carry_out" else "")
                results.append({
                    "patient_id": str(d.get("_id", "")),
                    "mrn": d.get("mrn", ""),
                    "name": d.get("name", ""),
                    "hisBed": d.get("hisBed", ""),
                    "icuAdmissionTime": fmt_bj(admit) if admit else "",
                    "icuDischargeTime": fmt_bj(discharge) if discharge else "",
                    "patient_type": ptype,
                    "los_days": los,
                })
            return results
        except Exception as _exc:
            logger.warning("DB %s failed in get_patient_census_detail: %s", db_name, _exc)
            continue
    return []


# ============================================================
# Stage 6: 感染部位入库 (icu_infection_site)
# ============================================================

def save_infection_site(
    pid: str,
    infection_site: str,
    eval_time: datetime,
    operator: str = "system",
    evidence_type: str = "",
    notes: str = "",
    secondary_sites: list = None,
) -> dict:
    """
    保存感染部位到 icu_infection_site 集合 (V3 扩展版)。

    Args:
        pid: 患者 ID
        infection_site: 主要感染部位 (11 选 1)
        eval_time: 评估时间
        operator: 操作者
        evidence_type: 证据类型 (影像学/实验室/临床/病理)
        notes: 备注
        secondary_sites: 次要感染部位列表

    Returns:
        {"ok": True, "id": str} 或 {"ok": False, "error": str}
    """
    VALID_SITES = {
        "肺部", "腹腔", "泌尿道", "血流", "皮肤软组织",
        "中枢神经系统", "心血管", "骨关节", "五官",
        "其他", "不明确",
    }
    VALID_EVIDENCE = {"影像学", "实验室", "临床", "病理", "微生物学", ""}

    if infection_site not in VALID_SITES:
        return {"ok": False, "error": f"无效感染部位: {infection_site}"}
    if evidence_type not in VALID_EVIDENCE:
        return {"ok": False, "error": f"无效证据类型: {evidence_type}"}

    # 验证次要感染部位
    if secondary_sites:
        for site in secondary_sites:
            if site not in VALID_SITES:
                return {"ok": False, "error": f"无效次要感染部位: {site}"}

    try:
        sc = get_client("SmartCare")["SmartCare"]
    except Exception as exc:
        return {"ok": False, "error": f"SmartCare 连接失败: {exc}"}

    exclusion_key = build_exclusion_key(pid, eval_time)
    doc = {
        "pid": pid,
        "exclusion_key": exclusion_key,
        "infection_site": infection_site,
        "secondary_sites": secondary_sites or [],
        "evidence_type": evidence_type,
        "notes": notes,
        "eval_time": _aware(eval_time),
        "operator": operator,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        sc.icu_infection_site.update_one(
            {"exclusion_key": exclusion_key},
            {"$set": doc},
            upsert=True,
        )
        return {"ok": True, "id": exclusion_key}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_infection_site(pid: str) -> list:
    """
    获取患者的感染部位记录。

    Returns:
        [{"infection_site": str, "eval_time": datetime, "operator": str}, ...]
    """
    try:
        sc = get_client("SmartCare")["SmartCare"]
    except Exception:
        return []
    try:
        docs = list(sc.icu_infection_site.find(
            {"pid": pid},
            {"_id": 0},
        ).sort("created_at", -1))
        # 展示字段：存储为 UTC → 转北京墙钟
        for d in docs:
            for k in ("eval_time", "created_at", "confirmed_at"):
                if isinstance(d.get(k), datetime):
                    d[k] = to_bj_naive(d[k])
        return docs
    except Exception:
        return []


def delete_infection_site(pid: str, exclusion_key: str) -> dict:
    """删除感染部位记录。"""
    try:
        sc = get_client("SmartCare")["SmartCare"]
    except Exception as exc:
        return {"ok": False, "error": f"SmartCare 连接失败: {exc}"}
    try:
        result = sc.icu_infection_site.delete_one({"exclusion_key": exclusion_key})
        return {"ok": True, "deleted": result.deleted_count}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ============================================================
# Stage 7: 三层人工覆盖 (icu_manual_override)
# ============================================================

def save_override(override_doc: dict) -> dict:
    """
    保存三层人工覆盖记录。

    override_doc 必须包含:
        - pid: 患者 ID
        - level: "L1" / "L2" / "L3"
        - indicator_id: 指标 ID (L2/L3 必填)
        - override_value: 覆盖值
        - reason_code: 原因码
        - operator: 操作者

    Returns:
        {"ok": True, "id": str} 或 {"ok": False, "error": str}
    """
    try:
        sc = get_client("SmartCare")["SmartCare"]
    except Exception as exc:
        return {"ok": False, "error": f"SmartCare 连接失败: {exc}"}

    level = override_doc.get("level")
    if level not in ("L1", "L2", "L3"):
        return {"ok": False, "error": f"无效覆盖层级: {level}"}

    pid = override_doc.get("pid")
    if not pid:
        return {"ok": False, "error": "pid 不能为空"}

    doc = {
        "pid": pid,
        "level": level,
        "indicator_id": override_doc.get("indicator_id"),
        "override_value": override_doc.get("override_value"),
        "reason_code": override_doc.get("reason_code"),
        "reason_text": override_doc.get("reason_text", ""),
        "operator": override_doc.get("operator", "unknown"),
        "created_at": datetime.now(timezone.utc),
        "expires_at": override_doc.get("expires_at"),
    }

    # 构建唯一键
    if level == "L1":
        key = f"L1|{pid}"
    elif level == "L2":
        indicator_id = override_doc.get("indicator_id")
        eval_time = override_doc.get("eval_time")
        exclusion_key = build_exclusion_key(pid, eval_time) if eval_time else pid
        key = f"L2|{exclusion_key}|{indicator_id}"
    else:
        indicator_id = override_doc.get("indicator_id")
        eval_time = override_doc.get("eval_time")
        exclusion_key = build_exclusion_key(pid, eval_time) if eval_time else pid
        key = f"L3|{exclusion_key}|{indicator_id}"

    doc["override_key"] = key

    try:
        sc.icu_manual_override.update_one(
            {"override_key": key},
            {"$set": doc},
            upsert=True,
        )
        return {"ok": True, "id": key}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_overrides(pid: str, level: str = None) -> list:
    """
    获取患者的覆盖记录。

    Args:
        pid: 患者 ID
        level: 可选，筛选覆盖层级

    Returns:
        [{"level": str, "indicator_id": str, "override_value": Any, ...}, ...]
    """
    try:
        sc = get_client("SmartCare")["SmartCare"]
    except Exception:
        return []

    query = {"pid": pid}
    if level:
        query["level"] = level

    try:
        docs = list(sc.icu_manual_override.find(
            query, {"_id": 0}
        ).sort("created_at", -1))
        # 展示字段：存储为 UTC → 转北京墙钟（前端 new Date 按本地=北京解析）
        for d in docs:
            if isinstance(d.get("created_at"), datetime):
                d["created_at"] = to_bj_naive(d["created_at"])
        return docs
    except Exception:
        return []


def get_override_map(pid: str, eval_time: datetime) -> dict:
    """
    获取患者的覆盖映射 (合并三层)。

    返回:
        {
            "L1": dict or None,
            "L2": {indicator_id: override_doc, ...},
            "L3": {indicator_id: override_doc, ...},
        }
    """
    try:
        sc = get_client("SmartCare")["SmartCare"]
    except Exception:
        return {"L1": None, "L2": {}, "L3": {}}

    exclusion_key = build_exclusion_key(pid, eval_time)

    try:
        # L1: case-level override
        l1 = sc.icu_manual_override.find_one({"pid": pid, "level": "L1"})

        # L2: judgement-level overrides
        l2_docs = list(sc.icu_manual_override.find({
            "level": "L2",
            "override_key": {"$regex": f"^L2\\|{exclusion_key}\\|"},
        }))
        l2_map = {}
        for doc in l2_docs:
            indicator_id = doc.get("indicator_id")
            if indicator_id:
                l2_map[indicator_id] = doc

        # L3: sofa-component-level overrides
        l3_docs = list(sc.icu_manual_override.find({
            "level": "L3",
            "override_key": {"$regex": f"^L3\\|{exclusion_key}\\|"},
        }))
        l3_map = {}
        for doc in l3_docs:
            indicator_id = doc.get("indicator_id")
            if indicator_id:
                l3_map[indicator_id] = doc

        return {"L1": l1, "L2": l2_map, "L3": l3_map}
    except Exception:
        return {"L1": None, "L2": {}, "L3": {}}


def delete_override(override_key: str) -> dict:
    """删除覆盖记录。"""
    try:
        sc = get_client("SmartCare")["SmartCare"]
    except Exception as exc:
        return {"ok": False, "error": f"SmartCare 连接失败: {exc}"}
    try:
        result = sc.icu_manual_override.delete_one({"override_key": override_key})
        return {"ok": True, "deleted": result.deleted_count}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_override_stats(dept_codes: list, start_date: str, end_date: str) -> dict:
    """
    获取覆盖统计。

    Returns:
        {
            "total_overrides": int,
            "by_level": {"L1": int, "L2": int, "L3": int},
            "by_reason": {reason_code: count, ...},
        }
    """
    try:
        sc = get_client("SmartCare")["SmartCare"]
    except Exception:
        return {"total_overrides": 0, "by_level": {}, "by_reason": {}}

    from datetime import datetime as dt
    start_dt = utc_day_start(start_date)
    end_dt = utc_day_start(end_date)

    try:
        pipeline = [
            {"$match": {
                "created_at": {"$gte": start_dt, "$lte": utc_day_end_wide(end_date)},
            }},
            {"$group": {
                "_id": {"level": "$level", "reason": "$reason_code"},
                "count": {"$sum": 1},
            }},
        ]
        results = list(sc.icu_manual_override.aggregate(pipeline))

        by_level = {"L1": 0, "L2": 0, "L3": 0}
        by_reason = {}
        total = 0

        for r in results:
            level = r["_id"].get("level")
            reason = r["_id"].get("reason")
            count = r["count"]
            total += count
            if level in by_level:
                by_level[level] += count
            if reason:
                by_reason[reason] = by_reason.get(reason, 0) + count

        return {
            "total_overrides": total,
            "by_level": by_level,
            "by_reason": by_reason,
        }
    except Exception:
        return {"total_overrides": 0, "by_level": {}, "by_reason": {}}


def test_connections() -> dict:
    """测试所有数据库连接，返回状态"""
    results = {}
    for db_name in ["SmartCare", "DataCenter"]:
        try:
            client = get_client(db_name)
            client[db_name].command("ping")
            results[db_name] = "OK"
        except Exception as e:
            results[db_name] = str(e)
    return results
