#!/usr/bin/env python3
"""
A股主线分析器
逻辑：先判定主线行业板块 → 再从主线板块中筛选个股
数据来源：新浪财经
仅分析行业板块，不包含概念板块
"""

import json
import datetime
import os
import time
from urllib.request import urlopen, Request

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def fetch_json(url, encoding="utf-8"):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=15) as resp:
        text = resp.read().decode(encoding)
    return json.loads(text)


def get_sector_flow(num=100):
    """获取行业板块资金流向（仅行业板块）"""
    url = (
        f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"MoneyFlow.ssl_bkzj_bk?page=1&num={num}&sort=netamount&asc=0&fenlei=0"
    )
    data = fetch_json(url, encoding="gbk")
    sectors = []
    for d in data:
        sectors.append({
            "code": d["category"],
            "name": d["name"],
            "main_net": float(d["netamount"]),
            "net_ratio": float(d["ratioamount"]),
            "pct_chg": float(d["avg_changeratio"]) * 100,
            "leading_symbol": d["ts_symbol"],
            "leading_name": d["ts_name"],
        })
    return sectors


def get_stock_kline(symbol, datalen=25):
    """获取K线（日线）"""
    url = (
        f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )
    try:
        data = fetch_json(url)
        if not data:
            return None
        return [float(k["close"]) for k in data]
    except Exception:
        return None


def calc_changes(closes):
    """计算5日、10日、20日涨幅"""
    c5 = c10 = c20 = None
    if closes and len(closes) >= 6:
        c5 = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)
    if closes and len(closes) >= 11:
        c10 = round((closes[-1] - closes[-11]) / closes[-11] * 100, 2)
    if closes and len(closes) >= 21:
        c20 = round((closes[-1] - closes[-21]) / closes[-21] * 100, 2)
    return c5, c10, c20


def get_sector_stocks(sector_code, num=50):
    """获取板块成分股"""
    url = (
        f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"Market_Center.getHQNodeData?page=1&num={num}&sort=changepercent&asc=0"
        f"&node={sector_code}&symbol=&_s_r_a=page"
    )
    try:
        data = fetch_json(url, encoding="gbk")
        stocks = []
        for d in data:
            stocks.append({
                "symbol": d["symbol"],
                "name": d["name"],
                "trade": float(d["trade"]),
                "changepercent": float(d["changepercent"]),
                "amount": float(d.get("amount", 0)),
                "mktcap": float(d.get("mktcap", 0)),
                "turnover": float(d.get("turnoverratio", 0)),
            })
        return stocks
    except Exception:
        return []


def analyze_sectors():
    print("=" * 50)
    print("第一步：分析主线行业板块")
    print("=" * 50)
    print("\n📡 获取行业板块资金流向...")
    industry = get_sector_flow(num=100)
    print(f"  ✅ {len(industry)} 个行业板块")
    print(f"\n📊 获取 {len(industry)} 个板块涨幅数据...")
    valid = []
    for i, s in enumerate(industry):
        closes = get_stock_kline(s["leading_symbol"], datalen=25)
        c5, c10, c20 = calc_changes(closes)
        if c5 is not None and c10 is not None:
            s["chg_5d"] = c5
            s["chg_10d"] = c10
            s["chg_20d"] = c20
            valid.append(s)
        if (i + 1) % 20 == 0:
            print(f"  已处理 {i + 1}/{len(industry)}...")
        if (i + 1) % 10 == 0:
            time.sleep(0.3)
    print(f"  ✅ 有效板块: {len(valid)} 个")
    n = len(valid)
    by_flow = sorted(valid, key=lambda x: x["main_net"], reverse=True)
    for r, s in enumerate(by_flow):
        s["flow_rank"] = r + 1
    by_5d = sorted(valid, key=lambda x: x["chg_5d"], reverse=True)
    for r, s in enumerate(by_5d):
        s["rank_5d"] = r + 1
    by_10d = sorted(valid, key=lambda x: x["chg_10d"], reverse=True)
    for r, s in enumerate(by_10d):
        s["rank_10d"] = r + 1
    for s in valid:
        s["score"] = round(
            (1 - s["flow_rank"] / (n + 1)) * 100 * 0.4 +
            (1 - s["rank_5d"] / (n + 1)) * 100 * 0.3 +
            (1 - s["rank_10d"] / (n + 1)) * 100 * 0.3, 1
        )
    ranked = sorted(valid, key=lambda x: x["score"], reverse=True)
    return ranked


def pick_stocks(main_sectors):
    """从主线板块中筛选满足条件的主板非ST个股"""
    seen = set()
    results = []
    total = len(main_sectors)
    print(f"\n📡 从 {total} 个主线板块中筛选个股...")
    for idx, sector in enumerate(main_sectors):
        stocks = get_sector_stocks(sector["code"], num=50)
        print(f"  [{idx+1}/{total}] {sector['name']}: {len(stocks)} 只成分股")
        for st in stocks:
            sym = st["symbol"]
            name = st["name"]
            if sym in seen:
                continue
            seen.add(sym)
            code = sym.replace("sh", "").replace("sz", "").replace("bj", "")
            if not ((sym.startswith("sh") and code.startswith("6")) or
                    (sym.startswith("sz") and code.startswith("00"))):
                continue
            if "ST" in name.upper() or "*ST" in name:
                continue
            closes = get_stock_kline(sym, datalen=25)
            c5, c10, c20 = calc_changes(closes)
            if c5 is None:
                continue
            results.append({
                "symbol": sym, "name": name, "trade": st["trade"],
                "chg_today": st["changepercent"], "chg_5d": c5,
                "chg_10d": c10, "chg_20d": c20,
                "mktcap": st["mktcap"], "turnover": st["turnover"],
                "sector_name": sector["name"], "sector_score": sector["score"],
            })
        if (idx + 1) % 5 == 0:
            time.sleep(0.5)

    def sort_pool(lst):
        for s in lst:
            c20 = s["chg_20d"] if s["chg_20d"] is not None else 0
            s["composite"] = round(s["chg_5d"] * 0.3 + s["chg_10d"] * 0.3 + c20 * 0.4, 2)
        lst.sort(key=lambda x: x["composite"], reverse=True)
        return lst

    pool_5d = sort_pool([s for s in results if s["chg_5d"] >= 20])
    pool_10d = sort_pool([s for s in results if s["chg_10d"] is not None and s["chg_10d"] >= 35])
    pool_20d = sort_pool([s for s in results if s["chg_20d"] is not None and s["chg_20d"] >= 45])
    print(f"\n  ✅ 个股筛选结果：5日≥20% {len(pool_5d)} 只 | 10日≥35% {len(pool_10d)} 只 | 20日≥45% {len(pool_20d)} 只")
    return pool_5d, pool_10d, pool_20d


def main():
    print("🔥 A股主线分析器（仅行业板块）")
    print("=" * 50)
    ranked = analyze_sectors()
    if not ranked:
        print("❌ 无数据"); return
    strategy_pool = [
        s for s in ranked
        if s["chg_5d"] >= 20 and s["chg_10d"] >= 35
        and s.get("chg_20d") is not None and s["chg_20d"] >= 45
        and s["main_net"] > 0
    ]
    print("\n" + "=" * 50)
    print(f"📊 主线行业板块排名 TOP 20（综合评分）")
    print("=" * 50)
    print(f"  {'#':>3s} {'板块':10s} {'评分':>5s} {'主力净流入':>10s} {'5日涨幅':>8s} {'10日涨幅':>8s} {'20日涨幅':>8s} {'领涨股':8s}")
    for i, s in enumerate(ranked[:20]):
        c20 = f"{s['chg_20d']:+6.2f}%" if s.get("chg_20d") is not None else "    —  "
        flow = fmt_amount(s["main_net"])
        print(f"  {i+1:3d} {s['name']:10s} {s['score']:5.1f} {flow:>10s} {s['chg_5d']:+6.2f}% {s['chg_10d']:+6.2f}% {c20} {s['leading_name']}")
    if strategy_pool:
        print(f"\n🎯 策略池1 入选 {len(strategy_pool)} 个板块")
    else:
        print("\n⚠️ 策略池1 无符合条件的板块")
    pool_5d, pool_10d, pool_20d = pick_stocks(ranked[:20])

    def print_pool(title, pool):
        if pool:
            print(f"\n{'=' * 50}")
            print(f"📦 {title}（{len(pool)} 只）")
            print("=" * 50)
            for i, s in enumerate(pool):
                c20 = f" 20日:{s['chg_20d']:+.2f}%" if s.get("chg_20d") is not None else ""
                print(f"  {i+1:2d}. {s['name']:8s} {s['symbol']:8s} | 5日:{s['chg_5d']:+6.2f}% | 10日:{s['chg_10d']:+6.2f}%{c20} | 现价:{s['trade']:.2f} | 来自:{s['sector_name']}")

    print_pool("5日涨幅≥20%", pool_5d)
    print_pool("10日涨幅≥35%", pool_10d)
    print_pool("20日涨幅≥45%", pool_20d)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output = os.path.join(script_dir, "sector_analysis.html")
    generate_html(ranked, strategy_pool, pool_5d, pool_10d, pool_20d, output)
    print(f"\n✅ HTML报告: {output}")


def fmt_amount(val):
    abs_val = abs(val)
    if abs_val >= 1e8:
        return f"{val / 1e8:+.2f}亿"
    elif abs_val >= 1e4:
        return f"{val / 1e4:+.2f}万"
    return f"{val:+.0f}"


def fmt_mktcap(val):
    if val >= 1e8:
        return f"{val / 1e8:.0f}亿"
    elif val >= 1e4:
        return f"{val / 1e4:.0f}万"
    return f"{val:.0f}"


def generate_html(ranked, strategy_pool, pool_5d, pool_10d, pool_20d, output_path):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def color(val):
        if val is None:
            return "#888"
        return "#e74c3c" if val > 0 else "#27ae60" if val < 0 else "#888"

    def sector_rows(items, limit=30):
        rows = ""
        for i, s in enumerate(items[:limit]):
            c20 = f'<td style="color:{color(s["chg_20d"])}">{s["chg_20d"]:+.2f}%</td>' if s.get("chg_20d") is not None else '<td>—</td>'
            rows += f'<tr><td style="color:#999">{i+1}</td>'
            rows += f'<td><b>{s["name"]}</b></td>'
            rows += f'<td style="color:{color(s["main_net"])}">{fmt_amount(s["main_net"])}</td>'
            rows += f'<td style="color:{color(s["chg_5d"])};font-weight:600">{s["chg_5d"]:+.2f}%</td>'
            rows += f'<td style="color:{color(s["chg_10d"])};font-weight:600">{s["chg_10d"]:+.2f}%</td>'
            rows += c20
            rows += f'<td>{s["net_ratio"]:.1f}%</td>'
            rows += f'<td>{s["leading_name"]}</td>'
            rows += f'<td style="color:#e74c3c;font-weight:600">{s["score"]}</td></tr>'
        return rows

    sth = '<thead><tr><th>#</th><th>板块</th><th>主力净流入</th><th>5日涨幅</th><th>10日涨幅</th><th>20日涨幅</th><th>净占比</th><th>领涨股</th><th>评分</th></tr></thead>'

    def stock_rows_5d(items):
        if not items:
            return '<tr><td colspan="7" style="text-align:center;padding:24px;color:#999">暂无符合条件的股票</td></tr>'
        rows = ""
        for s in items:
            rows += f'<tr><td><b>{s["name"]}</b> <small style="color:#999">{s["symbol"]}</small></td>'
            rows += f'<td style="color:{color(s["chg_5d"])};font-weight:600">{s["chg_5d"]:+.2f}%</td>'
            rows += f'<td>{s["trade"]:.2f}</td><td>{fmt_mktcap(s["mktcap"])}</td>'
            rows += f'<td>{s["turnover"]:.1f}%</td>'
            rows += f'<td style="color:#888">{s["sector_name"]}</td></tr>'
        return rows

    stockh_5d = '<thead><tr><th>股票</th><th>5日涨幅</th><th>现价</th><th>市值</th><th>换手率</th><th>所属板块</th></tr></thead>'

    def stock_rows_10d(items):
        if not items:
            return '<tr><td colspan="7" style="text-align:center;padding:24px;color:#999">暂无符合条件的股票</td></tr>'
        rows = ""
        for s in items:
            rows += f'<tr><td><b>{s["name"]}</b> <small style="color:#999">{s["symbol"]}</small></td>'
            rows += f'<td style="color:{color(s["chg_10d"])};font-weight:600">{s["chg_10d"]:+.2f}%</td>'
            rows += f'<td>{s["trade"]:.2f}</td><td>{fmt_mktcap(s["mktcap"])}</td>'
            rows += f'<td>{s["turnover"]:.1f}%</td>'
            rows += f'<td style="color:#888">{s["sector_name"]}</td></tr>'
        return rows

    stockh_10d = '<thead><tr><th>股票</th><th>10日涨幅</th><th>现价</th><th>市值</th><th>换手率</th><th>所属板块</th></tr></thead>'

    def stock_rows_20d(items):
        if not items:
            return '<tr><td colspan="7" style="text-align:center;padding:24px;color:#999">暂无符合条件的股票</td></tr>'
        rows = ""
        for s in items:
            c20 = s["chg_20d"] if s["chg_20d"] is not None else 0
            rows += f'<tr><td><b>{s["name"]}</b> <small style="color:#999">{s["symbol"]}</small></td>'
            rows += f'<td style="color:{color(c20)};font-weight:600">{c20:+.2f}%</td>'
            rows += f'<td>{s["trade"]:.2f}</td><td>{fmt_mktcap(s["mktcap"])}</td>'
            rows += f'<td>{s["turnover"]:.1f}%</td>'
            rows += f'<td style="color:#888">{s["sector_name"]}</td></tr>'
        return rows

    stockh_20d = '<thead><tr><th>股票</th><th>20日涨幅</th><th>现价</th><th>市值</th><th>换手率</th><th>所属板块</th></tr></thead>'

    n5, n10, n20 = len(pool_5d), len(pool_10d), len(pool_20d)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股主线行业分析 {now}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#fafafa;color:#333;line-height:1.5;padding:20px;font-size:14px}}
.wrap{{max-width:1200px;margin:0 auto}}
h1{{font-size:1.5em;margin-bottom:4px;color:#222}}
.sub{{color:#999;font-size:.8em;margin-bottom:24px}}
.sec{{font-size:1.15em;font-weight:600;margin:28px 0 10px;color:#222;padding-bottom:6px;border-bottom:2px solid #e74c3c}}
.sec2{{font-size:1.15em;font-weight:600;margin:28px 0 10px;color:#222;padding-bottom:6px;border-bottom:2px solid #3b82f6}}
.sec3{{font-size:1.15em;font-weight:600;margin:28px 0 10px;color:#222;padding-bottom:6px;border-bottom:2px solid #f59e0b}}
.sec4{{font-size:1.15em;font-weight:600;margin:28px 0 10px;color:#222;padding-bottom:6px;border-bottom:2px solid #10b981}}
table{{width:100%;border-collapse:collapse;margin-bottom:8px}}
th{{background:#f5f5f5;padding:8px 10px;text-align:left;font-size:.8em;color:#666;border-bottom:2px solid #eee;white-space:nowrap}}
td{{padding:7px 10px;border-bottom:1px solid #f0f0f0;white-space:nowrap;font-size:.88em}}
tr:hover{{background:#f8f8f8}}
.badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75em;font-weight:600;margin-left:8px}}
.badge-red{{background:#fee2e2;color:#e74c3c}}
.badge-amber{{background:#fef3c7;color:#f59e0b}}
.badge-green{{background:#d1fae5;color:#10b981}}
.note{{margin-top:28px;padding:12px 14px;background:#fff;border:1px solid #eee;border-radius:8px;font-size:.78em;color:#999;line-height:1.8}}
.note b{{color:#666}}
@media(max-width:768px){{body{{padding:10px}}table{{font-size:.78em}}th,td{{padding:5px 6px}}}}
</style>
</head>
<body>
<div class="wrap">
<h1>🔥 A股主线行业分析</h1>
<p class="sub">{now} · 数据来源：新浪财经 · 仅行业板块</p>
<h2 class="sec">一、主线行业板块排名（TOP 30）</h2>
<p style="font-size:.82em;color:#888;margin-bottom:8px">综合排名 = 主力净流入×0.4 + 5日涨幅×0.3 + 10日涨幅×0.3</p>
<table>{sth}<tbody>{sector_rows(ranked, 30)}</tbody></table>
<h2 class="sec2">二、5日涨幅≥20% 个股 <span class="badge badge-red">{n5} 只</span></h2>
<p style="font-size:.82em;color:#888;margin-bottom:8px">条件：仅主板 · 非ST · 来自TOP行业板块</p>
<table>{stockh_5d}<tbody>{stock_rows_5d(pool_5d)}</tbody></table>
<h2 class="sec3">三、10日涨幅≥35% 个股 <span class="badge badge-amber">{n10} 只</span></h2>
<p style="font-size:.82em;color:#888;margin-bottom:8px">条件：仅主板 · 非ST · 来自TOP行业板块</p>
<table>{stockh_10d}<tbody>{stock_rows_10d(pool_10d)}</tbody></table>
<h2 class="sec4">四、20日涨幅≥45% 个股 <span class="badge badge-green">{n20} 只</span></h2>
<p style="font-size:.82em;color:#888;margin-bottom:8px">条件：仅主板 · 非ST · 来自TOP行业板块</p>
<table>{stockh_20d}<tbody>{stock_rows_20d(pool_20d)}</tbody></table>
<div class="note">
<b>分析逻辑：</b>① 先看行业板块资金流入+涨幅 → 判定主线行业 → ② 从主线行业中筛选个股<br>
<b>板块涨幅：</b>取各板块领涨股K线数据作为板块趋势代理<br>
⚠️ 数据仅供参考，不构成投资建议
</div>
</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
