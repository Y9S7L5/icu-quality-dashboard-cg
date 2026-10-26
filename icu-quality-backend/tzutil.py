# -*- coding: utf-8 -*-
"""tzutil — 时区统一工具（内部真 UTC 架构）。

存储帧铁律（2026-10 实证：bedTime/collectTime/直方图均一致）：
    MongoDB naive datetime = 真 UTC 墙钟，勿改。

约定：
- 查询边界：北京墙钟日期串/naive → UTC 瞬间（−8h）
- 展示输出：UTC → 北京（+8h），统一走 fmt_bj
- aware 归一：as_utc()（naive 即 UTC，与原 db._aware 同义；
  scoring 层旧「naive=Asia/Shanghai」私有 _aware 全部收敛到此）
"""
from datetime import datetime, timedelta, timezone

BJ = timedelta(hours=8)


def as_utc(value):
    """naive → aware UTC；aware → 归一 UTC。None 透传。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_bj_naive(value):
    """UTC 帧（naive=UTC 墙钟，或 aware）→ 北京墙钟 naive。None 透传。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value + BJ


def utc_day_start(date_str: str) -> datetime:
    """北京墙钟日期（'YYYY-MM-DD'[ ' HH:MM[:SS]' ]）→ 该日起点的 UTC 瞬间。

    北京 D 00:00 → UTC D-1 16:00。入参必须是北京帧（period 拼出的日期串）。"""
    return datetime.fromisoformat(date_str) - BJ


def utc_day_end_wide(date_str: str) -> datetime:
    """北京墙钟日期串 → 该北京日 23:59:59 对应的 UTC 瞬间（D 15:59:59）。

    从「日期部分 +1 天 −8h −1s」构造。禁止先对字符串 −8 再拆 y/m/d 重组
    （会落到前一天，差一整天）——这是历史实现陷阱。"""
    d = datetime.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day) + timedelta(days=1) - BJ - timedelta(seconds=1)


def utc_end_wide_of(dt_val: datetime) -> datetime:
    """已解析的**北京帧** datetime（取其日期）→ 该北京日 23:59:59 的 UTC 瞬间。

    入参必须未做 −8；若值已是 UTC 帧请改用 utc_day_end_wide(date_str)。"""
    return datetime(dt_val.year, dt_val.month, dt_val.day) + timedelta(days=1) - BJ - timedelta(seconds=1)


def fmt_bj(value, fmt: str = "%Y-%m-%d %H:%M", default: str = "") -> str:
    """UTC 帧时间（datetime 或 ISO/空格分隔字符串）→ 北京时间字符串。

    None/'' → default；无法解析的字符串原样返回。
    与 main.py 既有 `(x + timedelta(hours=8)).strftime(...)` 补丁同构。"""
    if value is None or value == "":
        return default
    dt_val = value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            dt_val = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return value
    if not isinstance(dt_val, datetime):
        return default
    return to_bj_naive(dt_val).strftime(fmt)
