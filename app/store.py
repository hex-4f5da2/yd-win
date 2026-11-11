"""日线落盘（SQLite）。数据量很小，全部读进内存使用。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS bars(code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));"
            "CREATE TABLE IF NOT EXISTS meta(code TEXT PRIMARY KEY, src TEXT, checked TEXT, full INTEGER);")
        self.bars: Dict[str, List[Tuple[str, float]]] = {}
        self.meta: Dict[str, dict] = {}
        for code, date, close in self.db.execute("SELECT code, date, close FROM bars ORDER BY code, date"):
            self.bars.setdefault(code, []).append((date, close))
        for code, src, checked, full in self.db.execute("SELECT code, src, checked, full FROM meta"):
            self.meta[code] = {"src": src, "checked": checked, "full": bool(full)}

    def save(self, code: str, rows: List[Tuple[str, float]], src: str, checked: str, full: bool) -> None:
        """整体替换一只票的日线。full=False 表示上市不足一个完整窗口（新股）。"""
        with self.db:
            self.db.execute("DELETE FROM bars WHERE code=?", (code,))
            self.db.executemany("INSERT INTO bars VALUES(?,?,?)", [(code, d, c) for d, c in rows])
            self.db.execute("REPLACE INTO meta VALUES(?,?,?,?)", (code, src, checked, int(full)))
        self.bars[code] = list(rows)
        self.meta[code] = {"src": src, "checked": checked, "full": full}

    def append(self, items: List[Tuple[str, str, float]]) -> None:
        """追加当日收盘 [(code, date, close)]。"""
        if not items:
            return
        with self.db:
            self.db.executemany("REPLACE INTO bars VALUES(?,?,?)", items)
        for code, d, c in items:
            rows = self.bars.setdefault(code, [])
            if rows and rows[-1][0] == d:
                rows[-1] = (d, c)
            elif not rows or rows[-1][0] < d:
                rows.append((d, c))

    def mark(self, code: str, checked: str) -> None:
        m = self.meta.get(code)
        if m:
            m["checked"] = checked
            with self.db:
                self.db.execute("UPDATE meta SET checked=? WHERE code=?", (checked, code))
