"""Shift-window arithmetic.

A shift is named for the day it STARTS. "Monday night" is Monday 1800 to Tuesday 0600, never Sunday into
Monday. So times are placed on an operational day that begins at 0600: minutes run 360 (0600) to 1800
(0600 the next calendar morning). A clock time before 0600 is "after midnight" and belongs to the
previous shift date, so it is shifted by 24 hours.

    0600 -> 360    1800 -> 1080    0000 -> 1440    0200 -> 1560    0600 as an end after a night start -> 1800

Nights run 1800-0600 every day; days run 0600-1800 on Saturday and Sunday only.
"""

from datetime import date

DAY_START = 360  # 0600
MINUTES_PER_DAY = 1440
Interval = tuple[int, int]


def clock_minutes(hhmm: str) -> int:
    """'1800' or '18:00' -> minutes since midnight."""
    digits = hhmm.replace(":", "")
    if len(digits) != 4 or not digits.isdigit():
        raise ValueError(f"not a time: {hhmm!r}")
    h, m = int(digits[:2]), int(digits[2:])
    if h > 23 or m > 59:
        raise ValueError(f"not a time: {hhmm!r}")
    return h * 60 + m


def op_minute(clock: int) -> int:
    return clock if clock >= DAY_START else clock + MINUTES_PER_DAY


def interval(start: str, end: str) -> Interval:
    """The operational interval for a start and end time on one shift date. An end at or before the start is the next morning."""
    s, e = op_minute(clock_minutes(start)), op_minute(clock_minutes(end))
    if e <= s:
        e += MINUTES_PER_DAY
    return s, e


def to_hhmm(op: int) -> str:
    m = op % MINUTES_PER_DAY
    return f"{m // 60:02d}{m % 60:02d}"


def merge(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract(intervals: list[Interval], cut: Interval) -> list[Interval]:
    out: list[Interval] = []
    for s, e in intervals:
        if cut[1] <= s or cut[0] >= e:
            out.append((s, e))
            continue
        if s < cut[0]:
            out.append((s, cut[0]))
        if cut[1] < e:
            out.append((cut[1], e))
    return out


def overlap_minutes(intervals: list[Interval], target: Interval) -> int:
    return sum(max(0, min(e, target[1]) - max(s, target[0])) for s, e in intervals)


def valid_windows(shift_date: date) -> list[Interval]:
    """The times a shift can exist on this shift date, merged where they touch."""
    windows = [(1080, 1800)]  # night: 1800 to 0600
    if shift_date.weekday() >= 5:  # Saturday, Sunday
        windows.append((360, 1080))  # day: 0600 to 1800
    return merge(windows)


def within(windows: list[Interval], target: Interval) -> bool:
    return any(s <= target[0] and target[1] <= e for s, e in windows)
