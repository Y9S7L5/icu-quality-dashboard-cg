# -*- coding: utf-8 -*-
"""
时区修复 + ICU-04 重编号：历史汇总全量重建 + 明细缓存刷新。

背景:
  1. 时区修复（查询边界北京−8h、日切北京墙钟）后全期口径统一重写；
  2. ICU-04-02~07 历史月份缺行补齐（重编号迁移未覆盖全部期）。
  实测 2026-10/09/05/01 数值零 diff，重建主要为口径统一 + 补行。

用法: python -u scripts/tz_full_rebuild.py [--summary-only] [--detail-only]
流程与 scripts/icu_person_rebuild.py 同构（科室分组枚举 + 6/7 参进度回调）。
"""
import sys, io, time, argparse
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_client, BED_DB_NAMES
import main as m
from summary import rebuild_summary, SUMMARY_COLLECTION

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
print(f"待重建科室分组 {len(groups)} 个，指标 = 全部（indicators=None）", flush=True)
for k, d, p in groups:
    print(f"   {k[:60]!r}  {len(d)} 科室 × {len(p)} 期: {p[0]}~{p[-1]}", flush=True)

N = {"n": 0, "t0": time.time(), "last": time.time()}


def prog(total, success, failed, period, item, event, *a):
    """6 参(rebuild_summary) / 7 参(rebuild_detail_cache) 进度回调，event 恒取末位。"""
    if len(a):
        event = a[-1]
    if event != "finished":
        return
    N["n"] += 1
    now = time.time()
    if N["n"] % 10 == 0 or now - N["last"] > 30:
        N["last"] = now
        print(f"    [{N['n']:4d}] {period} {item}  ok={success} fail={failed} "
              f"{now - N['t0']:.0f}s", flush=True)


ok = True

# 2) 汇总全量重建
if not args.detail_only:
    N.update(n=0, t0=time.time(), last=time.time())
    for k, depts, periods in groups:
        print(f"\n=== 汇总 {k[:50]} ({len(periods)} 期) ===", flush=True)
        st = rebuild_summary(depts, periods, indicators=None, progress_callback=prog)
        print(f"  -> total={st['total']} success={st['success']} failed={st['failed']}"
              f" 累计 {time.time()-N['t0']:.0f}s", flush=True)
        for e in st["errors"]:
            print("  ERROR:", e, flush=True)
        ok = ok and st["failed"] == 0
    print(f"\n汇总重建完成，耗时 {time.time()-N['t0']:.0f}s", flush=True)

# 3) 明细缓存：先清旧版本，再全量重建
if not args.summary_only:
    coll = m._get_detail_cache_collection()
    if coll is not None:
        n = coll.delete_many({"cache_version": {"$ne": m.CACHE_VERSION}}).deleted_count
        print(f"\n清理旧版本明细缓存 {n} 条 (当前 v{m.CACHE_VERSION})", flush=True)

    N.update(n=0, t0=time.time(), last=time.time())
    for k, depts, periods in groups:
        icu_unit = depts[0] if len(depts) == 1 else "all"
        print(f"\n=== 明细缓存 {k[:50]} (icu_unit={icu_unit}) ===", flush=True)
        st = m.rebuild_detail_cache(depts, periods, indicators=None,
                                    icu_unit=icu_unit, progress_callback=prog)
        print(f"  -> total={st['total']} success={st['success']} failed={st['failed']}"
              f" 累计 {time.time()-N['t0']:.0f}s", flush=True)
        for e in st["errors"]:
            print("  ERROR:", e, flush=True)
        ok = ok and st["failed"] == 0
    print(f"\n明细缓存重建完成，耗时 {time.time()-N['t0']:.0f}s", flush=True)

print("\n" + ("重建成功 ✔" if ok else "重建有失败 ✘"), flush=True)
sys.exit(0 if ok else 1)
