from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .service import Service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
WEB = Path(__file__).resolve().parent.parent / "web"
svc = Service()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await svc.close()


app = FastAPI(title="yd-win", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(Exception)
async def on_error(_, exc: Exception):
    logging.getLogger("yd").exception("unhandled: %s", exc)
    return JSONResponse({"detail": "行情源暂时不可用，稍后自动重试"}, status_code=502)


@app.get("/api/pool")
async def api_pool():
    return await svc.pool()


@app.get("/api/stock/{code}")
async def api_stock(code: str):
    if not re.fullmatch(r"(sh|sz)\d{6}", code.lower()):
        raise HTTPException(400, "代码格式应为 sh600000 / sz000001")
    r = await svc.stock(code)
    if not r["item"]:
        raise HTTPException(503, r.get("notice") or "没有找到这只股票的数据")
    return r


@app.get("/api/search")
async def api_search(q: str):
    q = q.strip()
    if not q:
        return []
    return await svc.tx.search(q)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})
