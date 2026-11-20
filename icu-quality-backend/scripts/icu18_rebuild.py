# -*- coding: utf-8 -*-
"""重建 icu_monthly_summary 中的 ICU-18 汇总行 + 刷新 ICU-18 明细缓存（口径修复后必跑）。

2026-10 口径（CACHE_VERSION 33）：ICU-18 分母只按院方 ICD 编码清单前缀命中判定，
去掉诊断文本关键词、影像报告(VI_ICU_REPORT)、diseaseDiagnosis 人工确认三条补充路径。

按 dept_code 分组逐组重建（每个 dept_code 单独调用一次 rebuild_summary），
明细缓存随 CACHE_VERSION 自动失效，本脚本按新版本重建。

用法:
    python scripts/icu18_rebuild.py            # 全部 dept_code × 全部周期
    python scripts/icu18_rebuild.py 3439       # 只重建指定 dept_code
"""
import sys, io, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_client, BED_DB_NAMES
from summary import rebuild_summary, SUMMARY_COLLECTION
import main as m

for db_name in BED_DB_NAMES:
    try:
        db = get_client(db_name)[db_name]
        coll = db[SUMMARY_COLLECTION]
        break
    except Exception:
        continue
else:
    print("无可用数据库")
    sys.exit(1)

rows = list(coll.find({"indicator": "ICU-18"},
                      {"dept_code": 1, "period": 1, "_id": 0}))
print(f"现有 ICU-18 汇总行: {len(rows)}")

# ---- 1. 决定重建范围 ----
group_keys = sorted({r.get("dept_code") for r in rows if r.get("dept_code")})
if len(sys.argv) > 1 and sys.argv[1] != "all":
    group_keys = [k for k in group_keys if k == sys.argv[1]]
    if not group_keys:
        print(f"未找到 dept_code={sys.argv[1]} 的 ICU-18 行")
        sys.exit(1)

all_periods = sorted({r["period"] for r in rows})

# ---- 2. 逐组重建汇总 + 明细 ----
detail_coll = m._get_detail_cache_collection()
if detail_coll is not None:
    n = detail_coll.delete_many({"code": "ICU-18",
                                 "cache_version": {"$ne": m.CACHE_VERSION}}).deleted_count
    print(f"清理旧版本 ICU-18 明细缓存 {n} 条 (当前 v{m.CACHE_VERSION})")

for key in group_keys:
    periods = sorted({r["period"] for r in rows
                      if r.get("dept_code") == key}) or all_periods
    # dept_code 是 ",".join(dept_codes) 的结果，反向 split 才能还原原始科室列表；
    # 否则整个含逗号的 key 会被当成一个"科室号"查不到任何在科记录 → 0/0。
    dept_list = key.split(",")
    print(f"\n=== 重建 dept_code = {key}  ({len(periods)} 个周期, {len(dept_list)} 个科室) ===")
    t0 = time.time()
    stats = rebuild_summary(dept_list, periods, indicators=["ICU-18"])
    print(f"  汇总 耗时 {time.time() - t0:.1f}s  total={stats['total']} "
          f"success={stats['success']} failed={stats['failed']}")
    for e in stats["errors"]:
        print("  ERROR:", e)

    icu_unit = dept_list[0] if len(dept_list) == 1 else "all"
    t0 = time.time()
    dstats = m.rebuild_detail_cache(dept_list, periods, indicators=["ICU-18"],
                                    icu_unit=icu_unit)
    print(f"  明细 耗时 {time.time() - t0:.1f}s  total={dstats['total']} "
          f"success={dstats['success']} failed={dstats['failed']}")
    for e in dstats["errors"]:
        print("  ERROR:", e)

# ---- 3. 回读校验 ----
print("\n重建后的 ICU-18 行:")
bad = 0
for r in sorted(coll.find({"indicator": "ICU-18"},
                          {"dept_code": 1, "period": 1, "numerator": 1,
                           "denominator": 1, "value": 1, "_id": 0}),
                key=lambda x: (str(x.get("dept_code")), str(x.get("period")))):
    n, d = r.get("numerator"), r.get("denominator")
    live = round(n / d * 100, 2) if d else 0.0
    if live != r.get("value"):
        bad += 1
    key = r.get("dept_code")
    label = "3439" if key == "3439" else (key[:24] + "…" if len(str(key)) > 24 else key)
    print(f"  {label:<28} {r.get('period')}  {n}/{d} = {r.get('value')}%")
print(f"\n校验: 存储值与 n/d 不一致的行 = {bad}")
