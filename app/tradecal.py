"""交易日历：周末 + 交易所休市日。

休市日按国务院办公厅节假日安排推出（调休补班的周末交易所仍休市）。
每年 11 月左右公布次年安排，届时在 HOLIDAYS 里补上工作日中的休市日。
"""
from __future__ import annotations

import datetime as dt
from typing import List

HOLIDAYS = {
    # 2027（国办发明电〔2026〕10 号）
    "2027-01-01",
    "2027-02-05", "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11", "2027-02-12",
    "2027-04-05",
    "2027-05-03", "2027-05-04", "2027-05-05",
    "2027-06-09",
    "2027-09-15",
    "2027-10-01", "2027-10-04", "2027-10-05", "2027-10-06", "2027-10-07",
}
KNOWN_UNTIL = "2027-12-31"


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def trading_days(start: str, n: int) -> List[str]:
    """从 start（含）起的 n 个交易日。"""
    d, out = dt.date.fromisoformat(start), []
    while len(out) < n:
        if is_trading_day(d):
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def next_trading_day(after: str) -> str:
    d = dt.date.fromisoformat(after) + dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d.isoformat()
