# -*- coding: utf-8 -*-
"""
人数口径落地：重建 ICU-04-01/07/09/10 的汇总 + 明细缓存。

背景：INDICATORS_CONFIG 里这四个指标的分子分母定义都是「患者数」，
     而 SmartCare patient 表一行 = 一次 ICU 入住，此前 len() 得到的是人次。
     本脚本把按口径重算的结果写回 icu_monthly_summary / icu_indicator_detail_cache。

用法: python -u scripts/icu_person_rebuild.py [--summary-only] [--detail-only]
"""
import sys, io, time, argparse
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_client, BED_DB_NAMES
import main as m
from summary import rebuild_summary, SUMMARY_COLLECTION

IND = ["ICU-04-01", "ICU-07", "ICU-09", "ICU-10"]
ap = argparse.ArgumentParser()
ap.add_argument("--summary-only", action="store_true")
ap.add_argument("--detail-only", action="store_true")
args = ap.parse_args()

db = get_client(BED_DB_NAMES[0])[BED_DB_NAMES[0]]

# 1) 枚举 dept_code 分组（多科室键必须 split 后逐个传入，否则会写坏）
keys = sorted(db[SUMMARY_COLLECTION].distinct("dept_code"))
groups = []
for k in keys:
    depts = [x for x in k.split(",") if x]
    periods = sorted(db[SUMMARY_COLLECTION].distinct("period", {"dept_code": k}))
    if depts and periods:
        groups.append((k, depts, periods))
print(f"待重建科室分组 {len(groups)} 个，指标 {IND}", flush=True)
for k, d, p in groups:
    print(f"   {k[:60]!r}  {len(d)} 科室 × {len(p)} 期", flush=True)

N = {"n": 0, "t0": time.time(), "last": time.time()}


def prog(total, success, failed, period, item, event, *a):
    """每 10 个任务或每 30 秒打一行，避免长任务看起来像卡死。

    rebuild_summary 回调 6 参: (total, success, failed, period, indicator, event)
    rebuild_detail_cache 回调 7 参: (..., period, code, part, event)
    两种都要认，否则 detail 阶段的 event 会错位成 part，进度行全被吞掉。
    """
    if len(a):
        event = a[-1]                      # 7 参形态：真实 event 在最后
    if event != "finished":
        return
    N["n"] += 1
    now = time.time()
    if N["n"] % 10 == 0 or now - N["last"] > 30:
        N["last"] = now
        print(f"    [{N['n']:4d}] {period} {item}  ok={success} fail={failed} "
              f"{now - N['t0']:.0f}s", flush=True)


ok = True

# 2) 汇总
if not args.detail_only:
    N.update(n=0, t0=time.time(), last=time.time())
    for k, depts, periods in groups:
        print(f"\n=== 汇总 {k[:50]} ({len(periods)} 期) ===", flush=True)
        st = rebuild_summary(depts, periods, indicators=IND, progress_callback=prog)
        print(f"  -> total={st['total']} success={st['success']} failed={st['failed']}"
              f" 累计 {time.time()-N['t0']:.0f}s", flush=True)
        for e in st["errors"]:
            print("  ERROR:", e, flush=True)
        ok = ok and st["failed"] == 0
    print(f"\n汇总重建完成，耗时 {time.time()-N['t0']:.0f}s", flush=True)

# 3) 明细缓存：先清旧版本
if not args.summary_only:
    coll = m._get_detail_cache_collection()
    if coll is not None:
        n = coll.delete_many({"cache_version": {"$ne": m.CACHE_VERSION}}).deleted_count
        print(f"\n清理旧版本明细缓存 {n} 条 (当前 v{m.CACHE_VERSION})", flush=True)

    N.update(n=0, t0=time.time(), last=time.time())
    for k, depts, periods in groups:
        icu_unit = depts[0] if len(depts) == 1 else "all"
        print(f"\n=== 明细缓存 {k[:50]} (icu_unit={icu_unit}) ===", flush=True)
        st = m.rebuild_detail_cache(depts, periods, indicators=IND,
                                    icu_unit=icu_unit, progress_callback=prog)
        print(f"  -> total={st['total']} success={st['success']} failed={st['failed']}"
              f" 累计 {time.time()-N['t0']:.0f}s", flush=True)
        for e in st["errors"]:
            print("  ERROR:", e, flush=True)
        ok = ok and st["failed"] == 0
    print(f"\n明细缓存重建完成，耗时 {time.time()-N['t0']:.0f}s", flush=True)

print("\n" + ("重建成功 ✔" if ok else "重建有失败 ✘"), flush=True)
sys.exit(0 if ok else 1)
