#!/usr/bin/env python3
"""
A股主线分析器
逻辑：先判定主线行业板块 → 再从主线板块中筛选个股
数据来源：新浪财经
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


def get_sector_flow(fenlei=0, num=100):
    """获取板块资金流向 fenlei=0行业 fenlei=1概念"""
    url = (
        f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"MoneyFlow.ssl_bkzj_bk?page=1&num={num}&sort=netamount&asc=0&fenlei={fenlei}"
    )
    data = fetch_json(url, encoding="gbk")
    sectors = []
    for d in data:
        sectors.append({
            "category": "行业" if fenlei == 0 else "概念",
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


# ============================================================
# 第一步：分析主线行业板块
# ============================================================
def analyze_sectors():
    print("=" * 50)
    print("第一步：分析主线行业板块")
    print("=" * 50)

    print("\n📡 获取行业板块资金流向...")
    industry = get_sector_flow(fenlei=0, num=100)
    print(f"  ✅ {len(industry)} 个行业板块")

    print("📡 获取概念板块资金流向...")
    concept = get_sector_flow(fenlei=1, num=100)
    print(f"  ✅ {len(concept)} 个概念板块")

    all_sectors = industry + concept

    # 获取领涨股K线，计算板块涨幅
    print(f"\n📊 获取 {len(all_sectors)} 个板块涨幅数据...")
    valid = []
    for i, s in enumerate(all_sectors):
        closes = get_stock_kline(s["leading_symbol"], datalen=25)
        c5, c10, c20 = calc_changes(closes)
        if c5 is not None and c10 is not None:
            s["chg_5d"] = c5
            s["chg_10d"] = c10
            s["chg_20d"] = c20
            valid.append(s)
        if (i + 1) % 20 == 0:
            print(f"  已处理 {i + 1}/{len(all_sectors)}...")
        if (i + 1) % 10 == 0:
            time.sleep(0.3)

    print(f"  ✅ 有效板块: {len(valid)} 个")

    # 综合排名：资金流入权重0.4 + 5日涨幅0.3 + 10日涨幅0.3
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


# ============================================================
# 第二步：从主线板块筛选个股
# ============================================================
def pick_stocks(main_sectors):
    """从主线板块中筛选满足策略池1条件的主板非ST个股"""
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

            # 主板过滤
            code = sym.replace("sh", "").replace("sz", "").replace("bj", "")
            if not ((sym.startswith("sh") and code.startswith("6")) or
                    (sym.startswith("sz") and code.startswith("00"))):
                continue
            # ST过滤
            if "ST" in name.upper() or "*ST" in name:
                continue

            closes = get_stock_kline(sym, datalen=25)
            c5, c10, c20 = calc_changes(closes)
            if c5 is None:
                continue

            results.append({
                "symbol": sym,
                "name": name,
                "trade": st["trade"],
                "chg_today": st["changepercent"],
                "chg_5d": c5,
                "chg_10d": c10,
                "chg_20d": c20,
                "mktcap": st["mktcap"],
                "turnover": st["turnover"],
                "sector_name": sector["name"],
                "sector_category": sector["category"],
                "sector_score": sector["score"],
            })

        if (idx + 1) % 5 == 0:
            time.sleep(0.5)

    # 按三个维度分别筛选
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


# ============================================================
# 主流程
# ============================================================
def main():
    print("🔥 A股主线分析器")
    print("=" * 50)

    # 第一步：找主线板块
    ranked = analyze_sectors()
    if not ranked:
        print("❌ 无数据"); return

    # 策略池1：5日≥20% & 10日≥35% & 20日≥45% & 资金流入>0
    strategy_pool = [
        s for s in ranked
        if s["chg_5d"] >= 20 and s["chg_10d"] >= 35
        and s.get("chg_20d") is not None and s["chg_20d"] >= 45
        and s["main_net"] > 0
    ]

    print("\n" + "=" * 50)
    print(f"📊 主线板块排名 TOP 20（综合评分）")
    print("=" * 50)
    print(f"  {'#':>3s} {'板块':10s} {'分类':4s} {'评分':>5s} {'主力净流入':>10s} {'5日':>8s} {'10日':>8s} {'20日':>8s} {'领涨股':8s}")
    for i, s in enumerate(ranked[:20]):
        c20 = f"{s['chg_20d']:+6.2f}%" if s.get("chg_20d") is not None else "    —  "
        flow = fmt_amount(s["main_net"])
        print(f"  {i+1:3d} {s['name']:10s} {s['category']:4s} {s['score']:5.1f} {flow:>10s} {s['chg_5d']:+6.2f}% {s['chg_10d']:+6.2f}% {c20} {s['leading_name']}")

    if strategy_pool:
        print(f"\n🎯 策略池1 入选 {len(strategy_pool)} 个板块（5日≥20% & 10日≥35% & 20日≥45% & 资金流入>0）")
    else:
        print("\n⚠️ 策略池1 无符合条件的板块")

    # 第二步：从主线板块筛个股
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

    # 生成HTML
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


# ============================================================
# HTML报告
# ============================================================
def generate_html(ranked, strategy_pool, pool_5d, pool_10d, pool_20d, output_path):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def color(val):
        return "#e74c3c" if val > 0 else "#27ae60" if val < 0 else "#888"

    # ---- 板块排名表 ----
    def sector_rows(items, limit=30):
        rows = ""
        for i, s in enumerate(items[:limit]):
            c20 = f'<td style="color:{color(s["chg_20d"])}">{s["chg_20d"]:+.2f}%</td>' if s.get("chg_20d") is not None else '<td>—</td>'
            rows += f'<tr><td style="color:#999">{i+1}</td>'
            rows += f'<td><b>{s["name"]}</b></td><td>{s["category"]}</td>'
            rows += f'<td style="color:{color(s["main_net"])}">{fmt_amount(s["main_net"])}</td>'
            rows += f'<td style="color:{color(s["chg_5d"])};font-weight:600">{s["chg_5d"]:+.2f}%</td>'
            rows += f'<td style="color:{color(s["chg_10d"])};font-weight:600">{s["chg_10d"]:+.2f}%</td>'
            rows += c20
            rows += f'<td>{s["net_ratio"]:.1f}%</td>'
            rows += f'<td>{s["leading_name"]}</td>'
            rows += f'<td style="color:#e74c3c;font-weight:600">{s["score"]}</td></tr>'
        return rows

    sth = '<thead><tr><th>#</th><th>板块</th><th>分类</th><th>主力净流入</th><th>5日涨幅</th><th>10日涨幅</th><th>20日涨幅</th><th>净占比</th><th>领涨股</th><th>评分</th></tr></thead>'

    # ---- 个股表 ----
    def stock_rows(items):
        if not items:
            return '<tr><td colspan="8" style="text-align:center;padding:24px;color:#999">暂无符合条件的股票</td></tr>'
        rows = ""
        for s in items:
            c20 = f'<td style="color:{color(s["chg_20d"])}">{s["chg_20d"]:+.2f}%</td>' if s.get("chg_20d") is not None else '<td>—</td>'
            rows += f'<tr><td><b>{s["name"]}</b> <small style="color:#999">{s["symbol"]}</small></td>'
            rows += f'<td style="color:{color(s["chg_5d"])};font-weight:600">{s["chg_5d"]:+.2f}%</td>'
            rows += f'<td style="color:{color(s["chg_10d"])}">{s["chg_10d"]:+.2f}%</td>'
            rows += c20
            rows += f'<td>{s["trade"]:.2f}</td><td>{fmt_mktcap(s["mktcap"])}</td>'
            rows += f'<td>{s["turnover"]:.1f}%</td>'
            rows += f'<td style="color:#888">{s["sector_name"]}</td></tr>'
        return rows

    stockh = '<thead><tr><th>股票</th><th>5日涨幅</th><th>10日涨幅</th><th>20日涨幅</th><th>现价</th><th>市值</th><th>换手率</th><th>所属板块</th></tr></thead>'

    n5, n10, n20 = len(pool_5d), len(pool_10d), len(pool_20d)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股主线分析 {now}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#fafafa;color:#333;line-height:1.5;padding:20px;font-size:14px}}
.wrap{{max-width:1100px;margin:0 auto}}
h1{{font-size:1.5em;margin-bottom:4px;color:#222}}
.sub{{color:#999;font-size:.8em;margin-bottom:24px}}
.sec{{font-size:1.15em;font-weight:600;margin:28px 0 10px;color:#222;padding-bottom:6px;border-bottom:2px solid #e74c3c}}
.sec2{{font-size:1.15em;font-weight:600;margin:28px 0 10px;color:#222;padding-bottom:6px;border-bottom:2px solid #3b82f6}}
table{{width:100%;border-collapse:collapse;margin-bottom:8px}}
th{{background:#f5f5f5;padding:8px 10px;text-align:left;font-size:.8em;color:#666;border-bottom:2px solid #eee;white-space:nowrap}}
td{{padding:7px 10px;border-bottom:1px solid #f0f0f0;white-space:nowrap;font-size:.88em}}
tr:hover{{background:#f8f8f8}}
.tabs{{display:flex;gap:0;margin:0}}
.tab{{padding:8px 18px;background:#f5f5f5;border:1px solid #e0e0e0;cursor:pointer;border-radius:6px 6px 0 0;color:#888;font-weight:500;font-size:.85em;transition:.15s;border-bottom:none}}
.tab.active{{background:#fff;color:#333;border-bottom-color:#fff;font-weight:600}}
.tc{{display:none;background:#fff;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 8px 8px;padding:10px;overflow-x:auto}}
.tc.active{{display:block}}
.note{{margin-top:28px;padding:12px 14px;background:#fff;border:1px solid #eee;border-radius:8px;font-size:.78em;color:#999;line-height:1.8}}
.note b{{color:#666}}
@media(max-width:768px){{body{{padding:10px}}table{{font-size:.78em}}th,td{{padding:5px 6px}}}}
</style>
</head>
<body>
<div class="wrap">
<h1>A股主线分析</h1>
<p class="sub">{now} · 数据来源：新浪财经</p>

<!-- 第一步：主线板块 -->
<h2 class="sec">一、主线行业板块（TOP 30）</h2>
<p style="font-size:.82em;color:#888;margin-bottom:8px">综合排名 = 主力净流入×0.4 + 5日涨幅×0.3 + 10日涨幅×0.3</p>
<table>{sth}<tbody>{sector_rows(ranked, 30)}</tbody></table>

<!-- 第二步：从主线板块筛个股 -->
<h2 class="sec2">二、主线板块个股筛选</h2>
<p style="font-size:.82em;color:#888;margin-bottom:8px">条件：仅主板 · 非ST · 来自TOP板块</p>
<div class="tabs">
  <div class="tab active" onclick="sw('s5')">5日涨幅≥20%（{n5}）</div>
  <div class="tab" onclick="sw('s10')">10日涨幅≥35%（{n10}）</div>
  <div class="tab" onclick="sw('s20')">20日涨幅≥45%（{n20}）</div>
</div>
<div id="t-s5" class="tc active"><table>{stockh}<tbody>{stock_rows(pool_5d)}</tbody></table></div>
<div id="t-s10" class="tc"><table>{stockh}<tbody>{stock_rows(pool_10d)}</tbody></table></div>
<div id="t-s20" class="tc"><table>{stockh}<tbody>{stock_rows(pool_20d)}</tbody></table></div>

<div class="note">
<b>分析逻辑：</b>① 先看行业板块资金流入+涨幅 → 判定主线行业 → ② 从主线行业中筛选个股<br>
<b>板块涨幅：</b>取各板块领涨股K线数据作为板块趋势代理<br>
⚠️ 数据仅供参考，不构成投资建议
</div>
</div>
<script>
function sw(n){{
  var t=event.target,grp=t.parentElement;
  grp.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  var p=grp.parentElement;
  p.querySelectorAll('.tc').forEach(x=>x.classList.remove('active'));
  document.getElementById('t-'+n).classList.add('active');
}}
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
