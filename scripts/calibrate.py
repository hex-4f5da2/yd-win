"""用真实公告校准引擎：打印指定区间内引擎判定的异常波动 / 严重异常波动日期。
用法: python scripts/calibrate.py sh688585 2025-07-01 2025-08-15 [名称]
"""
import json, sys, urllib.request
sys.path.insert(0, ".")
from app.engine import build_sim, rule_for

def kline(code, start, end, n=400):
    u = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,{start},{end},{n},qfq"
    d = json.load(urllib.request.urlopen(u, timeout=15))["data"][code]
    return [(r[0], float(r[2])) for r in (d.get("qfqday") or d.get("day"))]

def main():
    code, start, end = sys.argv[1:4]
    name = sys.argv[4] if len(sys.argv) > 4 else ""
    rule = rule_for(code, name)
    # 往前多取 60 个自然日做预热
    import datetime as dt
    warm = (dt.date.fromisoformat(start) - dt.timedelta(days=90)).isoformat()
    ks = kline(code, warm, end)
    im = dict(kline(rule.index, warm, end))
    ks = [(d, c) for d, c in ks if d in im]
    sim = build_sim(rule, [d for d, _ in ks], [c for _, c in ks], [im[d] for d, _ in ks])
    print(code, name, rule.board, f"thr3={rule.thr3} n={rule.count_n}")
    ev = {sim.D[k]: d for k, d in sim.ev3}
    sv = {sim.D[k]: (d, why) for k, d, why in sim.severe}
    for t, d in enumerate(sim.D):
        if d < start or t == 0:
            continue
        pct = sim.C[t] / sim.C[t - 1] - 1
        tags = []
        if d in ev: tags.append("异常波动" + ("↑" if ev[d] > 0 else "↓"))
        if d in sv: tags.append("严重异常波动(" + "+".join(sv[d][1]) + ")")
        print(f"  {d} {sim.C[t]:8.2f} {pct*100:+6.2f}%  {' '.join(tags)}")

main()
