# -*- coding: utf-8 -*-
"""ICU-04 旧编码下线：汇总 / 明细缓存迁移到 ICU-04-01..07。

映射（与看板 indicators.js、后端 INDICATORS_CONFIG 一致）:
  ICU-04  -> ICU-04-01   APACHEⅡ≥15分收治率
  ICU-04F -> ICU-04-02   APACHEⅡ＜15分收治率
  ICU-04A -> ICU-04-03   <10
  ICU-04B -> ICU-04-04   10-15
  ICU-04C -> ICU-04-05   15-20
  ICU-04D -> ICU-04-06   20-25
  ICU-04E -> ICU-04-07   >=25

规则（对每个旧行）:
  - 目标键已有新码行（同 dept_code+period[+part]）→ 旧行为完全重复，备份后删除；
  - 目标键没有 → 直接改名，保留全部历史字段（含 override / diag 字段）。

备份: 被删除的旧行整体写入 tools/out/icu04_recode_backup.json，可回滚。

用法: python scripts/icu04_recode_migration.py [--dry-run]
"""
import sys
import io
import json
import argparse
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_client
from summary import SUMMARY_COLLECTION

MAP = {
    "ICU-04": "ICU-04-01",
    "ICU-04F": "ICU-04-02",
    "ICU-04A": "ICU-04-03",
    "ICU-04B": "ICU-04-04",
    "ICU-04C": "ICU-04-05",
    "ICU-04D": "ICU-04-06",
    "ICU-04E": "ICU-04-07",
}
OLD = list(MAP)
DB_NAME = "SmartCare"
BACKUP_PATH = Path(__file__).resolve().parents[2] / "tools" / "out" / "icu04_recode_backup.json"


def _jsonable(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


def migrate_summary(coll, dry, deleted):
    renamed = 0
    dup_deleted = 0
    for doc in list(coll.find({"indicator": {"$in": OLD}})):
        old_code = doc["indicator"]
        new_code = MAP[old_code]
        target = coll.count_documents({
            "indicator": new_code,
            "dept_code": doc.get("dept_code"),
            "period": doc.get("period"),
        })
        if target:
            deleted.append({"collection": SUMMARY_COLLECTION, "doc": _jsonable(doc)})
            if not dry:
                coll.delete_one({"_id": doc["_id"]})
            dup_deleted += 1
            print(f"  删重复 {old_code} {doc.get('period')} dept={doc.get('dept_code')}"
                  f" (已有 {new_code})")
        else:
            if not dry:
                coll.update_one({"_id": doc["_id"]}, {"$set": {"indicator": new_code}})
            renamed += 1
            print(f"  改名   {old_code} -> {new_code}  {doc.get('period')}"
                  f" dept={doc.get('dept_code')}")
    return renamed, dup_deleted


def migrate_detail(coll, dry, deleted):
    renamed = 0
    dup_deleted = 0
    for doc in list(coll.find({"code": {"$in": OLD}})):
        old_code = doc["code"]
        new_code = MAP[old_code]
        target = coll.count_documents({
            "code": new_code,
            "dept_code": doc.get("dept_code"),
            "period": doc.get("period"),
            "part": doc.get("part"),
        })
        if target:
            deleted.append({"collection": "icu_indicator_detail_cache", "doc": _jsonable(doc)})
            if not dry:
                coll.delete_one({"_id": doc["_id"]})
            dup_deleted += 1
            print(f"  删重复 {old_code} {doc.get('period')}/{doc.get('part')}"
                  f" (已有 {new_code})")
        else:
            if not dry:
                coll.update_one({"_id": doc["_id"]}, {"$set": {"code": new_code}})
            renamed += 1
            print(f"  改名   {old_code} -> {new_code}  {doc.get('period')}/{doc.get('part')}")
    return renamed, dup_deleted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    args = ap.parse_args()
    dry = args.dry_run
    print(f"== ICU-04 旧编码迁移 {'(dry-run)' if dry else '(写库)'} ==")

    db = get_client(DB_NAME)[DB_NAME]
    deleted = []

    print("\n[1/3] icu_monthly_summary")
    s_renamed, s_deleted = migrate_summary(db[SUMMARY_COLLECTION], dry, deleted)

    print("\n[2/3] icu_indicator_detail_cache")
    d_renamed, d_deleted = migrate_detail(db["icu_indicator_detail_cache"], dry, deleted)

    print("\n[3/3] 校验")
    leftover = sorted(set(db[SUMMARY_COLLECTION].distinct("indicator")) & set(OLD))
    leftover_d = sorted(set(db["icu_indicator_detail_cache"].distinct("code")) & set(OLD))
    print(f"  summary 残留旧码: {leftover or '无'}")
    print(f"  detail  残留旧码: {leftover_d or '无'}")
    for new in MAP.values():
        per = sorted(db[SUMMARY_COLLECTION].distinct("period", {"indicator": new}))
        print(f"  {new}: {len(per)} 期"
              f" ({per[0]}..{per[-1]})" if per else f"  {new}: 无数据")

    if not dry and deleted:
        BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_PATH.write_text(
            json.dumps({"migrated_at": datetime.now().isoformat(timespec="seconds"),
                        "deleted": deleted},
                       ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
        print(f"\n已删除 {len(deleted)} 行，备份 -> {BACKUP_PATH}")

    print(f"\n完成: 改名 summary={s_renamed} detail={d_renamed}, "
          f"删重复 summary={s_deleted} detail={d_deleted}"
          + (" (dry-run 未写库)" if dry else ""))
    return 0 if not leftover and not leftover_d else 1


if __name__ == "__main__":
    sys.exit(main())
