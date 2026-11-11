"""腾讯行情接口封装（实时报价 / 日线 / 排行 / 搜索）。"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

import httpx

class Blocked(Exception):
    """行情源拒绝服务（限流 / WAF）。"""


UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


class Tencent:
    def __init__(self) -> None:
        self.c = httpx.AsyncClient(timeout=10, headers=UA, trust_env=False)
        self.sem = asyncio.Semaphore(8)

    async def close(self) -> None:
        await self.c.aclose()

    async def _get(self, url: str, params: Optional[dict] = None, tries: int = 3) -> httpx.Response:
        last: Exception = RuntimeError("no try")
        for i in range(tries):
            try:
                async with self.sem:
                    r = await self.c.get(url, params=params)
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(0.3 * (i + 1))
        raise last

    async def quotes(self, codes: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}

        async def one(chunk: List[str]) -> None:
            r = await self._get("https://qt.gtimg.cn/q=" + ",".join(chunk))
            for line in r.content.decode("gbk", "ignore").split(";"):
                if '="' not in line:
                    continue
                key, val = line.strip().split('="', 1)
                f = val.rstrip('"').split("~")
                if len(f) < 49:
                    continue
                code = key[2:]

                def num(i: int) -> float:
                    try:
                        return float(f[i])
                    except ValueError:
                        return 0.0
                out[code] = {
                    "code": code, "name": f[1].replace(" ", ""), "price": num(3), "prev_close": num(4),
                    "open": num(5), "ts": f[30], "pct": num(32), "high": num(33), "low": num(34),
                    "amount": num(37), "turnover": num(38), "limit_up": num(47), "limit_down": num(48),
                }

        chunks = [codes[i:i + 60] for i in range(0, len(codes), 60)]
        await asyncio.gather(*(one(c) for c in chunks))
        return out

    KLINE_HOSTS = ("https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg")

    async def kline(self, code: str, n: int = 130) -> List[tuple]:
        """腾讯日线 [(date, close)]，前复权。每个域名只试一次，全部被拦时抛 Blocked。"""
        last: Exception = Blocked("no host")
        for host in self.KLINE_HOSTS:
            try:
                r = await self.c.get(host + "/appstock/app/fqkline/get", params={"param": f"{code},day,,,{n},qfq"})
            except httpx.HTTPError as e:
                last = e
                continue
            if r.status_code != 200 or not r.text.startswith("{"):
                last = Blocked(f"{host} {r.status_code}")
                continue
            d = (r.json().get("data") or {}).get(code) or {}
            rows = d.get("qfqday") or d.get("day") or []
            return [(x[0], float(x[2])) for x in rows]
        raise last

    async def sina_kline(self, code: str, n: int = 130) -> List[tuple]:
        """新浪日线 [(date, close)]，不复权。"""
        r = await self.c.get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                             params={"symbol": code, "scale": 240, "ma": "no", "datalen": n},
                             headers={"Referer": "https://finance.sina.com.cn/"})
        if r.status_code != 200 or not r.text.startswith("["):
            raise Blocked(f"sina {r.status_code}")
        return [(x["day"], float(x["close"])) for x in r.json()]

    async def rank_all(self) -> List[dict]:
        url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
        base = {"_appver": "11.17.0", "board_code": "aStock", "sort_type": "priceRatio",
                "direct": "down", "count": 200}
        first = (await self._get(url, {**base, "offset": 0})).json()["data"]
        rows = list(first["rank_list"])
        total = int(first.get("total", 0))

        async def page(off: int) -> List[dict]:
            return (await self._get(url, {**base, "offset": off})).json()["data"]["rank_list"]

        for part in await asyncio.gather(*(page(o) for o in range(200, total, 200))):
            rows.extend(part)
        return rows

    async def search(self, q: str) -> List[dict]:
        r = await self._get("https://proxy.finance.qq.com/ifzqgtimg/appstock/smartbox/search/get", {"q": q})
        rows = (r.json().get("data") or {}).get("stock") or []
        return [{"code": x[0] + x[1], "name": x[2]} for x in rows
                if len(x) >= 5 and x[0] in ("sh", "sz") and str(x[4]).startswith("GP-A")]
