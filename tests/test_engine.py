"""离线自测：python tests/test_engine.py"""
import sys
sys.path.insert(0, ".")
from app.engine import analyze, build_sim, dev_of, limit_up_price, rule_for, trigger_price


def series(start, pcts):
    out = [start]
    for p in pcts:
        out.append(round(out[-1] * (1 + p) + 1e-9, 2))
    return out


def run(code, pcts, name=""):
    rule = rule_for(code, name)
    c = series(10.0, [0.0] * 40 + pcts)
    sim = build_sim(rule, [str(i) for i in range(len(c))], c, [1000.0] * len(c))
    base = 40
    return sim, [k - base for k, d in sim.ev3], [(k - base, why) for k, d, why in sim.severe]


# 科创板连续 20cm：对应上纬新材 2025-07 的节奏（第 2、4、6、8 板异常波动；第 4 板 10 日线、第 6 板次数线）
_, ev, sv = run("sh688585", [0.2] * 8)
assert ev == [2, 4, 6, 8], ev
assert [k for k, _ in sv] == [4, 6, 7, 8], sv  # 第 7 板为 30 日线
assert "10日" in sv[0][1] and "次数" in sv[1][1]

# 主板连续 10cm：第 2、4、6、8 板异常波动，第 8 板同时触发次数线与 10 日线
_, ev, sv = run("sh600000", [0.1] * 8)
assert ev == [2, 4, 6, 8], ev
assert sv and sv[0][0] == 8 and set(sv[0][1]) == {"次数", "10日"}, sv

# ST：12% 阈值，5% 涨停，第 3 板一次异常波动
_, ev, sv = run("sz000711", [0.05] * 12, "ST京蓝")
assert ev == [3, 6, 9, 12], ev
assert sv[0] == (12, ["次数"]), sv

# 四舍五入口径：深中华A 2026-09-02，个股 +100.17%，指数 +0.17%
assert dev_of(11.75, 5.87, 2628.01, 2623.54) >= 1.0 - 1e-9
assert trigger_price(5.87, 2628.01, 2623.54, 1.0) == 11.75

# analyze：主板 7 连板后，今日涨停即触发，连板第 1 板触线
sim, _, _ = run("sh600000", [0.1] * 7)
prev = sim.C[-1]
lim = limit_up_price(prev, 0.1)
r = analyze(sim, prev, 1000.0, prev, lim)
assert r["bind"]["limit_hits"] and r["boards"]["hit"] == 1, r
assert r["bind"]["price"] <= lim
# 指数大涨时触发价抬高
r2 = analyze(sim, prev, 1050.0, prev, lim)
assert r2["bind"]["price"] >= r["bind"]["price"]
print("ok")

# outlook：主板 5 连板后股价不动，触发价逐日抬高；第 0 行与今日分析一致
from app.engine import outlook
sim, _, _ = run("sh600000", [0.1] * 5)
prev = sim.C[-1]
rows = outlook(sim, prev, 1000.0, prev, limit_up_price(prev, 0.1), 10)
r0 = analyze(sim, prev, 1000.0, prev)
assert rows[0]["bind"]["price"] == r0["bind"]["price"]
ps = [r["lines"]["d10"]["price"] for r in rows]
assert all(b >= a for a, b in zip(ps, ps[1:])), ps
assert ps[-1] > ps[0]
print("outlook ok")
