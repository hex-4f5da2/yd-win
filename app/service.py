"""异动池与单票分析。

日线策略：落盘 + 每日用实时报价追加收盘价，平时几乎不请求日线接口；
确实要拉日线时（冷启动、新入池、除权、断档）统一走限速队列，腾讯被拦自动换新浪。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .engine import analyze, build_sim, limit_up_price, outlook, rule_for
from .store import Store
from .tencent import Blocked, Tencent
from .tradecal import next_trading_day, trading_days

log = logging.getLogger("yd")
TZ = ZoneInfo("Asia/Shanghai")
INDEXES = ["sh000002", "sz399107", "sz399102", "sh000688"]
INDEX_NAMES = {"sh000002": "上证A股指数", "sz399107": "深证A指", "sz399102": "创业板综指", "sh000688": "科创50"}
KLINE_N = 130
OUTLOOK_DAYS = 10
DB_PATH = Path(os.environ.get("YD_DB", Path(__file__).resolve().parent.parent / "data" / "yd.db"))

# 入池门槛（可调）
POOL_DEV10 = 0.50     # 10 日偏离值
POOL_DEV30 = 1.00     # 30 日偏离值
POOL_COUNT = 2        # 10 日内同向异常波动次数

# 日线请求限速（可调）
FETCH_INTERVAL = float(os.environ.get("YD_FETCH_INTERVAL", "1.0"))   # 两次日线请求的最小间隔（秒）
TENCENT_COOLDOWN = 900       # 腾讯被拦后多久再试（秒）
ALL_COOLDOWN = 120           # 两个源都失败后暂停多久（秒）
FINAL_HM = "15:05"           # 此后当日收盘价视为定稿


def _f(x: object) -> float:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def is_candidate(row: dict) -> bool:
    """用排行榜的 N 日涨幅做粗筛，宁宽勿漏；精确判断在引擎里做。"""
    d5, d10, d20, d60 = (_f(row.get(k)) for k in ("zdf_d5", "zdf_d10", "zdf_d20", "zdf_d60"))
    if "ST" in str(row.get("name", "")).upper():
        return d5 >= 8 or d10 >= 15 or d20 >= 30 or d60 >= 60
    return d5 >= 18 or d10 >= 30 or d20 >= 50 or d60 >= 90


class Service:
    def __init__(self) -> None:
        self.tx = Tencent()
        self.store = Store(DB_PATH)
        self.cands: Dict[str, str] = {}
        self.cands_at = 0.0
        self.pool_cache: Optional[tuple] = None
        self.lock = asyncio.Lock()
        # 日线队列
        self.gate = asyncio.Lock()
        self.next_at = 0.0
        self.queue: deque = deque()
        self.queued: set = set()
        self.worker: Optional[asyncio.Task] = None
        self.tencent_off_until = 0.0
        self.all_off_until = 0.0
        self.idx_tried = 0.0

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()
        await self.tx.close()

    # ---- 市场状态 ----
    async def market(self) -> dict:
        iq = await self.tx.quotes(INDEXES)
        ts = iq["sh000002"]["ts"]
        qdate = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        now = dt.datetime.now(TZ)
        today = now.date().isoformat()
        hm = now.strftime("%H:%M")
        preopen = today > qdate
        trading = (not preopen) and ("09:15" <= hm <= "11:31" or "12:59" <= hm <= "15:01")
        final = preopen or hm >= FINAL_HM
        t0 = next_trading_day(qdate) if preopen else qdate
        days = trading_days(t0, OUTLOOK_DAYS)
        return {"date": qdate, "time": f"{ts[8:10]}:{ts[10:12]}:{ts[12:14]}", "preopen": preopen,
                "trading": trading, "final": final, "idx": iq, "days": days}

    # ---- 日线：限速抓取 ----
    async def _fetch(self, code: str, mk: dict) -> bool:
        """抓一只票的日线并落盘。全程持有 gate，保证全局同一时刻只有一个日线请求。"""
        async with self.gate:
            if time.time() < self.all_off_until:
                return False
            wait = self.next_at - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            rows, src = None, ""
            try:
                if time.time() >= self.tencent_off_until:
                    try:
                        rows, src = await self.tx.kline(code, KLINE_N), "tencent"
                    except Exception as e:  # noqa: BLE001
                        self.tencent_off_until = time.time() + TENCENT_COOLDOWN
                        log.warning("腾讯日线不可用(%s)，%d 秒内改用新浪", e, TENCENT_COOLDOWN)
                if rows is None:
                    try:
                        rows, src = await self.tx.sina_kline(code, KLINE_N), "sina"
                    except Exception as e:  # noqa: BLE001
                        self.all_off_until = time.time() + ALL_COOLDOWN
                        log.warning("新浪日线也不可用(%s)，暂停 %d 秒", e, ALL_COOLDOWN)
                        return False
            finally:
                self.next_at = time.time() + FETCH_INTERVAL
            full = len(rows) >= KLINE_N - 2
            now = dt.datetime.now(TZ)
            if now.strftime("%H:%M") < FINAL_HM:      # 盘中的当日 K 线还没定稿，不入库
                today = now.date().isoformat()
                rows = [r for r in rows if r[0] != today]
            if not rows:
                return False
            self.store.save(code, rows, src, mk["date"], full)
            return True

    def _enqueue(self, code: str, mk: dict) -> None:
        if code in self.queued:
            return
        self.queued.add(code)
        self.queue.append(code)
        if not self.worker or self.worker.done():
            self.worker = asyncio.create_task(self._work(mk))

    async def _work(self, mk: dict) -> None:
        while self.queue:
            if time.time() < self.all_off_until:
                await asyncio.sleep(5)
                continue
            code = self.queue.popleft()
            try:
                ok = await self._fetch(code, mk)
            except Exception:  # noqa: BLE001
                log.exception("fetch %s", code)
                ok = False
            self.queued.discard(code)
            if ok:
                self.pool_cache = None

    # ---- 日线：是否可用 ----
    def _closed(self, code: str, mk: dict) -> List[tuple]:
        rows = self.store.bars.get(code) or []
        return rows if mk["preopen"] else [r for r in rows if r[0] < mk["date"]]

    def _index_ok(self, code: str, mk: dict) -> bool:
        rows = self._closed(code, mk)
        if not rows:
            return False
        if mk["preopen"]:
            return rows[-1][0] == mk["date"]
        prev = mk["idx"][code]["prev_close"]
        return abs(rows[-1][1] - prev) <= max(0.02, prev * 2e-5)

    async def _ensure_indexes(self, mk: dict) -> bool:
        missing = [c for c in INDEXES if not self._index_ok(c, mk)]
        if missing and time.time() - self.idx_tried > 30:
            self.idx_tried = time.time()
            for c in missing:
                await self._fetch(c, mk)
            missing = [c for c in INDEXES if not self._index_ok(c, mk)]
        return not missing

    def _ready(self, code: str, mk: dict, idx_last: str) -> bool:
        if self.store.meta.get(code, {}).get("checked") == mk["date"]:   # 今天已拉过，不再重拉（含上市太短的新股）
            return True
        rows = self._closed(code, mk)
        return len(rows) >= 2 and rows[-1][0] >= idx_last

    def _roll(self, mk: dict, quotes: Dict[str, dict]) -> None:
        """收盘定稿后，用实时报价把当日收盘追加进库，次日无需再拉日线。"""
        if mk["preopen"] or not mk["final"]:
            return
        d, items = mk["date"], []
        for c in INDEXES:
            rows = self.store.bars.get(c) or []
            if rows and rows[-1][0] < d and self._index_ok(c, mk):
                items.append((c, d, mk["idx"][c]["price"]))
        for c, q in quotes.items():
            rows = self.store.bars.get(c) or []
            if (rows and rows[-1][0] < d and q["price"] > 0 and q["amount"] > 0 and q["ts"][:8] == d.replace("-", "")
                    and abs(q["prev_close"] / rows[-1][1] - 1) < 1e-3):
                items.append((c, d, q["price"]))
        self.store.append(items)

    # ---- 单票 ----
    def compute(self, code: str, mk: dict, quote: dict) -> Optional[dict]:
        rule = rule_for(code, quote["name"])
        if rule is None:
            return {"code": code, "name": quote["name"], "unsupported": True}
        imap = dict(self._closed(rule.index, mk))
        rows = [r for r in self._closed(code, mk) if r[0] in imap]
        if len(rows) < 2:
            return None
        meta = self.store.meta.get(code, {})
        dates = [r[0] for r in rows]
        closes = [r[1] for r in rows]
        idx = [imap[d] for d in dates]
        adjusted = False
        if mk["preopen"]:
            prev = closes[-1]
            price, idx_now, pct = prev, idx[-1], 0.0
            lim = limit_up_price(prev, rule.limit)
        else:
            prev = quote["prev_close"] or closes[-1]
            if closes[-1] > 0 and abs(prev / closes[-1] - 1) > 1e-3:   # 今日除权除息，库里的日线尚未复权
                k = prev / closes[-1]
                closes = [c * k for c in closes]
                adjusted = True
            price = quote["price"] or prev
            pct = quote["pct"] if quote["price"] else 0.0
            idx_now = mk["idx"][rule.index]["price"] or idx[-1]
            lim = quote["limit_up"] or limit_up_price(prev, rule.limit)
        sim = build_sim(rule, dates, closes, idx, newly_listed=not meta.get("full", True))
        res = analyze(sim, price, idx_now, prev, lim)
        res["outlook"] = [dict(r, date=d) for r, d in zip(outlook(sim, price, idx_now, prev, lim, OUTLOOK_DAYS), mk["days"])]
        res.update(code=code, name=quote["name"], board=rule.board, st=rule.st, price=price, pct=pct,
                   prev_close=prev, at_limit=price >= lim - 1e-6, limit_pct=rule.limit,
                   index_pct=round((idx_now / idx[-1] - 1) * 100, 2), src=meta.get("src", ""),
                   index_code=rule.index, index_name=INDEX_NAMES[rule.index],
                   adjusted=adjusted)
        return res

    @staticmethod
    def in_pool(it: dict) -> bool:
        ln = it.get("lines") or {}
        return bool(
            (ln["d10"].get("dev") or 0) >= POOL_DEV10
            or (ln["d30"].get("dev") or 0) >= POOL_DEV30
            or ln["d3"]["count"] >= POOL_COUNT
            or it.get("monitor")
            or it["tomorrow"].get("today_severe")
        )

    # ---- 异动池 ----
    async def _refresh_cands(self, trading: bool) -> None:
        ttl = 300 if trading else 1800
        if self.cands and time.time() - self.cands_at < ttl:
            return
        try:
            rows = await self.tx.rank_all()
        except Exception:  # noqa: BLE001
            log.exception("rank list failed")
            if self.cands:
                self.cands_at = time.time() - ttl + 60
                return
            raise
        cands = {r["code"]: r["name"] for r in rows
                 if rule_for(r["code"], r.get("name", "")) and is_candidate(r)}
        self.cands, self.cands_at = cands, time.time()
        log.info("candidates: %d of %d", len(cands), len(rows))

    def _idx_last(self, mk: dict) -> str:
        return min(self._closed(c, mk)[-1][0] for c in INDEXES)

    async def pool(self) -> dict:
        async with self.lock:
            if self.pool_cache and time.time() - self.pool_cache[0] < 3:
                return self.pool_cache[1]
            mk = await self.market()
            view = self._mk_view(mk)
            if not await self._ensure_indexes(mk):
                return {"market": view, "items": [], "scanned": 0, "pending": 0,
                        "notice": "指数日线暂时取不到，稍后自动重试"}
            await self._refresh_cands(mk["trading"])
            codes = list(self.cands)
            quotes = await self.tx.quotes(codes)
            idx_last = self._idx_last(mk)
            tencent_ok = time.time() >= self.tencent_off_until
            items, pending = [], 0
            for code in codes:
                q = quotes.get(code)
                if not q:
                    continue
                meta = self.store.meta.get(code, {})
                if not self._ready(code, mk, idx_last):
                    pending += 1
                    self._enqueue(code, mk)
                    continue
                try:
                    it = self.compute(code, mk, q)
                except Exception:  # noqa: BLE001
                    log.exception("compute %s", code)
                    continue
                if not it or it.get("unsupported"):
                    continue
                # 新浪数据不复权、或今日除权：腾讯可用时每天后台换一次前复权数据
                if (it["src"] == "sina" or it["adjusted"]) and tencent_ok and meta.get("upg") != mk["date"]:
                    meta["upg"] = mk["date"]
                    self._enqueue(code, mk)
                if self.in_pool(it):
                    items.append(it)
            self._roll(mk, quotes)
            items.sort(key=lambda x: x["bind"]["space"] if x.get("bind") else 9e9)
            out = {"market": view, "items": items, "scanned": len(codes), "pending": pending, "notice": ""}
            self.pool_cache = (time.time(), out)
            return out

    async def stock(self, code: str) -> dict:
        code = code.lower()
        mk = await self.market()
        view = self._mk_view(mk)
        quote = (await self.tx.quotes([code])).get(code)
        if not quote:
            return {"market": view, "item": None}
        rule = rule_for(code, quote["name"])
        if rule is None:
            return {"market": view, "item": {"code": code, "name": quote["name"], "unsupported": True}}
        if not await self._ensure_indexes(mk):
            return {"market": view, "item": None, "notice": "指数日线暂时取不到，请稍后再试"}
        if not self._ready(code, mk, self._idx_last(mk)):
            await self._fetch(code, mk)          # 搜索的票插队，最多等一个在途请求
        item = self.compute(code, mk, quote)
        notice = "日线暂时取不到，请稍后再试"
        if item is None and self.store.meta.get(code, {}).get("checked") == mk["date"]:
            notice = "上市时间太短，还无法计算"
        return {"market": view, "item": item, "notice": notice}

    @staticmethod
    def _mk_view(mk: dict) -> dict:
        return {"date": mk["date"], "time": mk["time"], "preopen": mk["preopen"], "trading": mk["trading"],
                "indexes": [{"code": c, "name": mk["idx"][c]["name"], "pct": mk["idx"][c]["pct"]}
                            for c in INDEXES]}
