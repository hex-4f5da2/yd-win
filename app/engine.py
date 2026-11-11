"""异常波动 / 严重异常波动 计算引擎（纯函数，无 IO）。

规则依据：上交所交易规则(2023) 4.4.11、5.4.2、5.4.3、6.1.10、6.1.11，
深交所交易规则(2023) 5.4.3、5.4.4、5.4.5。

偏离值累计 = (期末收盘/期初前收盘 - 1) - (指数期末收盘/指数期初前收盘 - 1)
"N 个交易日内"包含更短的子窗口（已用真实公告校准，见 scripts/calibrate.py）。
指标在公布后的次一交易日起重新计算：
  - 3 日异常波动触发后，3 日窗口从次日重新起算；
  - 严重异常波动的 10 日 / 30 日 / 次数三条线各自独立，哪条触发哪条从次日重新起算
    （上纬新材 2025-07 公告"6 次异常波动、5 次严重异常波动"只有这种口径能对上）。
交易所按百分比保留两位小数后再相减比较（深中华A 2026-09-02：个股 +100.17%、
指数 +0.17%，精确差值 99.99998% 仍被认定触发），引擎同样先各自四舍五入。
未实现：主板"换手率 30 倍"那一条异常波动（很少见）。
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

EPS = 1e-9
UP10, DOWN10 = 1.00, -0.50
UP30, DOWN30 = 2.00, -0.70
MONITOR_DAYS = 10  # 严重异常波动后的重点监控期（交易日）


@dataclass(frozen=True)
class Rule:
    board: str        # 沪主板 / 深主板 / 创业板 / 科创板
    index: str        # 基准指数代码
    thr3: float       # 3 日异常波动阈值
    count_n: int      # 10 日内同向异常波动次数达到几次算严重
    limit: float      # 涨跌幅限制
    st: bool = False


def rule_for(code: str, name: str = "") -> Optional[Rule]:
    """code 形如 sh600000 / sz000001。不支持的品种返回 None。"""
    code = code.lower()
    mkt, num = code[:2], code[2:]
    st = "ST" in name.upper()
    if mkt == "sh" and num.startswith(("688", "689")):
        return Rule("科创板", "sh000688", 0.30, 3, 0.20, st)
    if mkt == "sz" and num.startswith(("300", "301", "302")):
        return Rule("创业板", "sz399102", 0.30, 3, 0.20, st)
    if mkt == "sh" and num.startswith("6"):
        return Rule("沪主板", "sh000002", 0.12 if st else 0.20, 4, 0.05 if st else 0.10, st)
    if mkt == "sz" and num.startswith(("000", "001", "002", "003", "004")):
        return Rule("深主板", "sz399107", 0.12 if st else 0.20, 4, 0.05 if st else 0.10, st)
    return None


def r4(x: float) -> float:
    """涨跌幅保留到 0.01%。"""
    return math.floor(x * 10000 + 0.5 + 1e-7) / 10000


def dev_of(price: float, base: float, idx: float, idx_base: float) -> float:
    return r4(price / base - 1) - r4(idx / idx_base - 1)


def trigger_price(base: float, idx: float, idx_base: float, thr: float) -> float:
    """使偏离值达到 thr 的最低价（精确到分）。"""
    p = math.floor(base * (1 + thr + r4(idx / idx_base - 1) - 0.0001) * 100) / 100 - 0.01
    for _ in range(50):
        if dev_of(p, base, idx, idx_base) >= thr - EPS:
            break
        p = round(p + 0.01, 2)
    return round(p, 2)


def round_price(x: float) -> float:
    return math.floor(x * 100 + 0.5 + 1e-6) / 100


def ceil_price(x: float) -> float:
    return math.ceil(x * 100 - 1e-6) / 100


def limit_up_price(prev_close: float, limit: float) -> float:
    return round_price(prev_close * (1 + limit))


@dataclass
class Sim:
    """按交易日逐日推进的状态机。C/I 为个股(前复权)与基准指数收盘价序列。"""
    rule: Rule
    C: List[float] = field(default_factory=list)
    I: List[float] = field(default_factory=list)
    D: List[str] = field(default_factory=list)
    first: int = 1          # 第一个可作为窗口起点的下标（新股前 5 日不计）
    s3: int = 1             # 3 日指标起算下标
    s10: int = 1            # 10 日线起算下标
    s30: int = 1            # 30 日线起算下标
    sc: int = 1             # 次数线起算下标
    ev3: List[Tuple[int, int]] = field(default_factory=list)       # (下标, 方向 ±1)
    severe: List[Tuple[int, int, List[str]]] = field(default_factory=list)  # (下标, 方向, 原因)

    def clone(self) -> "Sim":
        return copy.deepcopy(self)

    # ---- 基础 ----
    def dev(self, i: int, t: int) -> float:
        return dev_of(self.C[t], self.C[i - 1], self.I[t], self.I[i - 1])

    def _lo(self, t: int, n: int, start: int) -> int:
        return max(start, self.first, t - n + 1, 1)

    def max_dev(self, t: int, n: int, start: int) -> Optional[float]:
        lo = self._lo(t, n, start)
        return max((self.dev(i, t) for i in range(lo, t + 1)), default=None)

    def min_dev(self, t: int, n: int, start: int) -> Optional[float]:
        lo = self._lo(t, n, start)
        return min((self.dev(i, t) for i in range(lo, t + 1)), default=None)

    def up_count(self, t: int, direction: int = 1) -> int:
        """[t-9, t] 且不早于 sc 的同向异常波动次数。"""
        lo = max(self.sc, t - 9)
        return sum(1 for (k, d) in self.ev3 if d == direction and lo <= k <= t)

    # ---- 推进一天 ----
    def push(self, close: float, idx: float, date: str = "") -> dict:
        self.C.append(close)
        self.I.append(idx)
        self.D.append(date)
        t = len(self.C) - 1
        out = {"t": t, "ev3": 0, "severe": []}
        if t < max(self.first, 1):
            return out
        # 3 日异常波动
        hi, lo = self.max_dev(t, 3, self.s3), self.min_dev(t, 3, self.s3)
        d3 = 0
        if hi is not None and hi >= self.rule.thr3 - EPS:
            d3 = 1
        elif lo is not None and lo <= -self.rule.thr3 + EPS:
            d3 = -1
        if d3:
            self.ev3.append((t, d3))
            self.s3 = t + 1
            out["ev3"] = d3
        # 严重异常波动
        up, down = [], []
        h10, l10 = self.max_dev(t, 10, self.s10), self.min_dev(t, 10, self.s10)
        h30, l30 = self.max_dev(t, 30, self.s30), self.min_dev(t, 30, self.s30)
        if d3 and self.up_count(t, d3) >= self.rule.count_n:
            (up if d3 > 0 else down).append("次数")
            self.sc = t + 1
        if h10 is not None and h10 >= UP10 - EPS:
            up.append("10日")
            self.s10 = t + 1
        elif l10 is not None and l10 <= DOWN10 + EPS:
            down.append("10日")
            self.s10 = t + 1
        if h30 is not None and h30 >= UP30 - EPS:
            up.append("30日")
            self.s30 = t + 1
        elif l30 is not None and l30 <= DOWN30 + EPS:
            down.append("30日")
            self.s30 = t + 1
        if up or down:
            direction = 1 if up else -1
            reasons = up or down
            self.severe.append((t, direction, reasons))
            out["severe"] = reasons
            out["severe_dir"] = direction
        return out

    # ---- 下一交易日的触发价 ----
    def _trigger(self, n: int, start: int, thr: float, idx_next: float) -> Optional[float]:
        t = len(self.C)  # 下一交易日下标
        lo = self._lo(t, n, start)
        best = None
        for i in range(lo, t + 1):
            p = trigger_price(self.C[i - 1], idx_next, self.I[i - 1], thr)
            if best is None or p < best:
                best = p
        return best

    def next_day(self, idx_next: float, price: Optional[float] = None,
                 limit: Optional[float] = None) -> dict:
        """给定下一交易日的指数点位，返回各条线的触发价；
        给了 price 再算当前偏离值及其拆解（个股涨幅、指数涨幅、基准日），给了 limit 再算涨停时的偏离值。"""
        t = len(self.C)
        r = self.rule
        # 下一日窗口 [t-9, t]，已有事件需落在其中
        lo = max(self.sc, t - 9)
        cnt = sum(1 for (k, d) in self.ev3 if d == 1 and lo <= k < t)
        lines = {
            "d10": {"thr": UP10, "price": self._trigger(10, self.s10, UP10, idx_next)},
            "d30": {"thr": UP30, "price": self._trigger(30, self.s30, UP30, idx_next)},
            "d3": {"thr": r.thr3, "price": self._trigger(3, self.s3, r.thr3, idx_next),
                   "count": cnt, "n": r.count_n, "severe": cnt >= r.count_n - 1},
        }
        for k, ln in lines.items():
            if price:
                n, start = {"d10": (10, self.s10), "d30": (30, self.s30), "d3": (3, self.s3)}[k]
                lo_i = self._lo(t, n, start)
                best = None
                for i in range(lo_i, t + 1):
                    d = dev_of(price, self.C[i - 1], idx_next, self.I[i - 1])
                    if best is None or d > best[0]:
                        best = (d, i)
                ln["dev"] = best[0] if best else None
                if best:
                    i = best[1]
                    ln.update(base_date=self.D[i - 1], days=t - i + 1,
                              stock_pct=r4(price / self.C[i - 1] - 1),
                              index_pct=r4(idx_next / self.I[i - 1] - 1))
                if limit and best:
                    ln["dev_limit"] = max(dev_of(limit, self.C[i - 1], idx_next, self.I[i - 1])
                                          for i in range(lo_i, t + 1))
        cands = [(ln["price"], k) for k, ln in lines.items()
                 if ln["price"] is not None and (k != "d3" or ln["severe"])]
        bind = min(cands) if cands else None
        return {"lines": lines,
                "bind": {"price": bind[0], "line": bind[1]} if bind else None}


def build_sim(rule: Rule, dates: List[str], closes: List[float], idx: List[float],
              newly_listed: bool = False) -> Sim:
    sim = Sim(rule)
    if newly_listed:  # 上市前 5 个交易日无涨跌幅限制，不纳入计算
        sim.first = sim.s3 = sim.s10 = sim.s30 = sim.sc = 5
    for d, c, i in zip(dates, closes, idx):
        sim.push(c, i, d)
    return sim


def analyze(sim: Sim, price: float, idx_now: float, prev_close: float,
            limit_up: Optional[float] = None, max_boards: int = 20) -> dict:
    """sim 为截至上一交易日收盘的状态；price / idx_now 为今日现价与指数现值。"""
    r = sim.rule
    limit_up = limit_up or limit_up_price(prev_close, r.limit)
    today = sim.next_day(idx_now, price, limit_up)
    bind = today["bind"]
    res = {"lines": today["lines"], "bind": bind, "limit_up": limit_up}
    if bind:
        bind["space"] = bind["price"] / price - 1
        bind["limit_hits"] = limit_up >= bind["price"] - EPS
        bind["hit"] = price >= bind["price"] - EPS
    for ln in today["lines"].values():
        if ln["price"] is not None:
            ln["space"] = ln["price"] / price - 1

    # 明日：假设今日按现价收盘，明日指数持平
    s2 = sim.clone()
    r2 = s2.push(price, idx_now)
    tm = s2.next_day(idx_now)
    tb = tm["bind"]
    tomorrow = {"today_severe": r2["severe"], "today_ev3": r2["ev3"]}
    if tb:
        lu2 = limit_up_price(price, r.limit)
        tomorrow.update(price=tb["price"], line=tb["line"], space=tb["price"] / price - 1,
                        limit_hits=lu2 >= tb["price"] - EPS)
    res["tomorrow"] = tomorrow

    # 连板推演：从今日起每天涨停、指数持平，第几板触线
    s3 = sim.clone()
    p, hit, reasons = prev_close, None, []
    for k in range(1, max_boards + 1):
        p = limit_up if k == 1 else limit_up_price(p, r.limit)
        o = s3.push(p, idx_now)
        if o["severe"] and o.get("severe_dir") == 1:
            hit, reasons = k, o["severe"]
            break
    res["boards"] = {"hit": hit, "reasons": reasons}

    # 监管状态：最近一次向上的严重异常波动
    t = len(sim.C)
    mon = None
    for (k, d, why) in reversed(sim.severe):
        if d == 1:
            left = MONITOR_DAYS - (t - k) + 1  # 含今日在内剩余交易日
            if left > 0:
                mon = {"date": sim.D[k], "reasons": why, "days_left": left}
            break
    res["monitor"] = mon
    lo = max(sim.sc, t - 9)
    res["events"] = [sim.D[k] for (k, d) in sim.ev3 if d == 1 and lo <= k < t]
    return res


def outlook(sim: Sim, price: float, idx_now: float, prev_close: float, limit_today: float,
            days: int = 10) -> List[dict]:
    """未来逐日触发线：假设股价一直停在 price、指数一直停在 idx_now。

    第 0 行为今日。窗口逐日后滑，早期低位的基准价移出窗口，触发价随之抬高；
    若按 price 收盘会触发异常波动或严重异常波动，后续日子按规则清零重算。
    space 为相对现价的距离；day_pct 为当天相对前收所需涨幅（今日前收为实际前收，之后为 price）。
    limit_hits 表示当天一个涨停就能触线（次日起的涨停价按 price 计算）。
    """
    r = sim.rule
    s = sim.clone()
    rows = []
    for k in range(days):
        nd = s.next_day(idx_now)
        lim = limit_today if k == 0 else limit_up_price(price, r.limit)
        base = prev_close if k == 0 else price
        L = nd["lines"]
        row = {
            "lines": {key: {"price": ln["price"],
                            "space": (ln["price"] / price - 1) if ln["price"] is not None else None,
                            "day_pct": (ln["price"] / base - 1) if ln["price"] is not None else None}
                      for key, ln in L.items()},
            "count": L["d3"]["count"], "n": L["d3"]["n"], "count_severe": L["d3"]["severe"],
            "limit": lim, "bind": None,
        }
        b = nd["bind"]
        if b:
            row["bind"] = {"line": b["line"], "price": b["price"], "space": b["price"] / price - 1,
                           "day_pct": b["price"] / base - 1,
                           "limit_hits": lim >= b["price"] - EPS}
        rows.append(row)
        s.push(price, idx_now)
    return rows
