#!/usr/bin/env python3
"""
A股板块主线分析器 v2
数据来源：新浪财经
分析维度：主力资金净流入 + 5日涨幅 + 10日涨幅 + 20日涨幅
输出：HTML报告（含策略池 + 股池）
"""

import json
import re
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
    """
    获取板块资金流向
    fenlei=0: 行业板块, fenlei=1: 概念板块
    """
    url = (
        f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"MoneyFlow.ssl_bkzj_bk?page=1&num={num}&sort=netamount&asc=0&fenlei={fenlei}"
    )
    data = fetch_json(url, encoding="gbk")
    sectors = []
    for d in data:
        sectors.append({
            "category": "行业板块" if fenlei == 0 else "概念板块",
            "code": d["category"],
            "name": d["name"],
            "main_net": float(d["netamount"]),          # 主力净流入（元）
            "in_amount": float(d["inamount"]),           # 流入额
            "out_amount": float(d["outamount"]),          # 流出额
            "net_ratio": float(d["ratioamount"]),         # 净占比
            "turnover": float(d["turnover"]),             # 换手率
            "pct_chg": float(d["avg_changeratio"]) * 100, # 今日涨幅%
            "leading_symbol": d["ts_symbol"],              # 领涨股代码
            "leading_name": d["ts_name"],                  # 领涨股名称
        })
    return sectors


def get_sector_stocks(sector_code, num=30):
    """获取板块成分股列表"""
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
                "trade": float(d["trade"]),                # 最新价
                "changepercent": float(d["changepercent"]), # 涨跌幅%
                "volume": float(d.get("volume", 0)),       # 成交量
                "amount": float(d.get("amount", 0)),        # 成交额
                "mktcap": float(d.get("mktcap", 0)),        # 总市值
                "nmc": float(d.get("nmc", 0)),              # 流通市值
                "turnoverratio": float(d.get("turnoverratio", 0)),  # 换手率
            })
        return stocks
    except Exception:
        return []


def get_stock_kline(symbol, datalen=25):
    """获取个股K线数据（日线），默认取25根以覆盖20日涨幅计算"""
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


def get_stock_flow(symbol):
    """获取个股资金流向（主力净流入）"""
    # 新浪接口：stock_money_flow
    # symbol 格式：sh600000 或 sz000001
    url = (
        f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"MoneyFlow.ssl_bkzj_bk?page=1&num=1&sort=netamount&asc=0&fenlei=0"
    )
    # 个股资金流用另一个接口
    code = symbol.replace("sh", "").replace("sz", "")
    prefix = "sh" if symbol.startswith("sh") else "sz"
    url2 = (
        f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"MoneyFlow.ssl_bkzj_bk?page=1&num=1&sort=netamount&asc=0&fenlei=0"
    )
    # 用板块资金流接口无法直接获取个股，改用实时行情接口的字段
    return None


def calc_changes(closes):
    """从收盘价序列计算5日、10日和20日涨幅"""
    chg_5d = None
    chg_10d = None
    chg_20d = None
    if closes and len(closes) >= 6:
        chg_5d = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)
    if closes and len(closes) >= 11:
        chg_10d = round((closes[-1] - closes[-11]) / closes[-11] * 100, 2)
    if closes and len(closes) >= 21:
        chg_20d = round((closes[-1] - closes[-21]) / closes[-21] * 100, 2)
    return chg_5d, chg_10d, chg_20d


def filter_strategy_pool(ranked):
    """
    策略池1筛选条件：
    - 5日涨幅 >= 20%
    - 10日涨幅 >= 35%
    - 20日涨幅 >= 45%
    - 主力资金净流入 > 0
    """
    pool = []
    for s in ranked:
        chg_5d = s.get("chg_5d")
        chg_10d = s.get("chg_10d")
        chg_20d = s.get("chg_20d")
        main_net = s.get("main_net", 0)

        if (chg_5d is not None and chg_5d >= 20
                and chg_10d is not None and chg_10d >= 35
                and chg_20d is not None and chg_20d >= 45
                and main_net > 0):
            pool.append(s)

    pool.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    return pool


def build_stock_pool(strategy_pool, core_sectors):
    """
    从策略池板块中提取成分股，按三个涨幅维度分别筛选：
    - pool_5d:  5日涨幅 ≥ 20%
    - pool_10d: 10日涨幅 ≥ 35%
    - pool_20d: 20日涨幅 ≥ 45%
    仅保留主板、非ST
    """
    seen = set()
    all_stocks = []
    core_sector_names = {s["name"] for s in core_sectors}

    total = len(strategy_pool)
    print(f"\n📡 正在从 {total} 个策略池板块中提取成分股...")

    for idx, sector in enumerate(strategy_pool):
        sector_code = sector["code"]
        sector_name = sector["name"]
        is_core = sector_name in core_sector_names

        stocks = get_sector_stocks(sector_code, num=30)
        print(f"  [{idx+1}/{total}] {sector_name}: {len(stocks)} 只成分股")

        for stock in stocks:
            sym = stock["symbol"]
            name = stock["name"]
            if sym in seen:
                continue
            seen.add(sym)

            # ---- 主板过滤 ----
            code = sym.replace("sh", "").replace("sz", "").replace("bj", "")
            is_main_board = (
                (sym.startswith("sh") and code.startswith("6")) or
                (sym.startswith("sz") and code.startswith("00"))
            )
            if not is_main_board:
                continue

            # ---- ST过滤 ----
            if "ST" in name.upper() or "*ST" in name:
                continue

            closes = get_stock_kline(sym, datalen=25)
            chg_5d, chg_10d, chg_20d = calc_changes(closes)

            if chg_5d is None:
                continue

            stock_info = {
                "symbol": sym,
                "name": stock["name"],
                "trade": stock["trade"],
                "chg_today": stock["changepercent"],
                "chg_5d": chg_5d,
                "chg_10d": chg_10d,
                "chg_20d": chg_20d,
                "amount": stock["amount"],
                "mktcap": stock["mktcap"],
                "turnover": stock["turnoverratio"],
                "from_sector": sector_name,
                "from_category": sector["category"],
                "is_core": is_core,
                "sector_score": sector.get("total_score", 0),
            }
            all_stocks.append(stock_info)

        if (idx + 1) % 5 == 0:
            time.sleep(0.5)

    # 按三个维度分别筛选，每个维度独立排序
    def sort_pool(lst):
        for s in lst:
            c20 = s["chg_20d"] if s["chg_20d"] is not None else 0
            s["composite"] = round(s["chg_5d"] * 0.3 + s["chg_10d"] * 0.3 + c20 * 0.4, 2)
        lst.sort(key=lambda x: (x["is_core"], x["composite"]), reverse=True)
        return lst

    pool_5d = sort_pool([s for s in all_stocks if s["chg_5d"] >= 20])
    pool_10d = sort_pool([s for s in all_stocks if s["chg_10d"] is not None and s["chg_10d"] >= 35])
    pool_20d = sort_pool([s for s in all_stocks if s["chg_20d"] is not None and s["chg_20d"] >= 45])

    print(f"  ✅ 5日≥20%: {len(pool_5d)} 只 | 10日≥35%: {len(pool_10d)} 只 | 20日≥45%: {len(pool_20d)} 只")

    return pool_5d, pool_10d, pool_20d


def analyze():
    print("=" * 60)
    print("🔥 A股板块主线分析器 v2（含策略池+股池）")
    print("=" * 60)

    # 1. 获取行业板块
    print("\n📡 获取行业板块资金流向...")
    industry = get_sector_flow(fenlei=0, num=100)
    print(f"  ✅ 行业板块: {len(industry)} 个")

    # 2. 获取概念板块
    print("📡 获取概念板块资金流向...")
    concept = get_sector_flow(fenlei=1, num=100)
    print(f"  ✅ 概念板块: {len(concept)} 个")

    all_sectors = industry + concept

    # 3. 获取各板块领涨股K线，计算5/10/20日涨幅
    print(f"\n📊 正在获取 {len(all_sectors)} 个板块的K线数据...")
    valid = []
    for i, s in enumerate(all_sectors):
        sym = s["leading_symbol"]
        closes = get_stock_kline(sym, datalen=25)
        chg_5d, chg_10d, chg_20d = calc_changes(closes)

        if chg_5d is not None and chg_10d is not None:
            s["chg_5d"] = chg_5d
            s["chg_10d"] = chg_10d
            s["chg_20d"] = chg_20d
            valid.append(s)

        if (i + 1) % 20 == 0:
            print(f"  已处理 {i + 1}/{len(all_sectors)}...")
        if (i + 1) % 10 == 0:
            time.sleep(0.3)

    print(f"  ✅ 有效数据: {len(valid)} 个板块")

    # 4. 排名评分
    n = len(valid)
    if n == 0:
        print("❌ 无有效数据")
        return [], [], []

    by_flow = sorted(valid, key=lambda x: x["main_net"], reverse=True)
    for rank, s in enumerate(by_flow):
        s["flow_rank"] = rank + 1

    by_5d = sorted(valid, key=lambda x: x["chg_5d"], reverse=True)
    for rank, s in enumerate(by_5d):
        s["rank_5d"] = rank + 1

    by_10d = sorted(valid, key=lambda x: x["chg_10d"], reverse=True)
    for rank, s in enumerate(by_10d):
        s["rank_10d"] = rank + 1

    for s in valid:
        s["flow_score"] = round((1 - s["flow_rank"] / (n + 1)) * 100, 1)
        s["score_5d"] = round((1 - s["rank_5d"] / (n + 1)) * 100, 1)
        s["score_10d"] = round((1 - s["rank_10d"] / (n + 1)) * 100, 1)
        s["total_score"] = round(
            s["flow_score"] * 0.4 + s["score_5d"] * 0.3 + s["score_10d"] * 0.3, 1
        )

    ranked = sorted(valid, key=lambda x: x["total_score"], reverse=True)

    for i, s in enumerate(ranked):
        if i < 5:
            s["level"] = "🔴 核心主线"
        elif i < 12:
            s["level"] = "🟠 强势支线"
        elif i < 20:
            s["level"] = "🟡 潜力支线"
        else:
            s["level"] = ""

    # 5. 策略池筛选
    print("\n🎯 筛选策略池（5日≥20% & 10日≥35% & 20日≥45% & 资金流入>0）...")
    strategy_pool = filter_strategy_pool(ranked)
    print(f"  ✅ 策略池入选: {len(strategy_pool)} 个板块")

    # 6. 构建股池（三个维度）
    core_sectors = [s for s in ranked if s["level"] == "🔴 核心主线"]
    pool_5d, pool_10d, pool_20d = build_stock_pool(strategy_pool, core_sectors)

    return ranked, strategy_pool, pool_5d, pool_10d, pool_20d


def fmt_amount(val):
    """格式化金额（亿元）"""
    abs_val = abs(val)
    if abs_val >= 1e8:
        return f"{val / 1e8:+.2f}亿"
    elif abs_val >= 1e4:
        return f"{val / 1e4:+.2f}万"
    else:
        return f"{val:+.0f}"


def fmt_mktcap(val):
    """格式化市值（亿元）"""
    if val >= 1e8:
        return f"{val / 1e8:.0f}亿"
    elif val >= 1e4:
        return f"{val / 1e4:.0f}万"
    else:
        return f"{val:.0f}"


def generate_html(ranked, strategy_pool, pool_5d, pool_10d, pool_20d, output_path):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def color(val):
        return "#e74c3c" if val > 0 else "#27ae60" if val < 0 else "#888"

    def c20(s):
        if s.get("chg_20d") is not None:
            return f'<td style="color:{color(s["chg_20d"])}">{s["chg_20d"]:+.2f}%</td>'
        return '<td>—</td>'

    # ---- 股池表格（共用） ----
    def stock_tbody(items):
        if not items:
            return '<tr><td colspan="8" style="text-align:center;padding:24px;color:#666">暂无符合条件的股票</td></tr>'
        rows = ""
        for s in items:
            star = '<span style="color:#e74c3c">⭐</span>' if s["is_core"] else ""
            rows += f'<tr{"" if not s["is_core"] else " class=core"}>'
            rows += f'<td>{star}</td>'
            rows += f'<td><b>{s["name"]}</b> <small style="color:#666">{s["symbol"]}</small></td>'
            rows += f'<td style="color:{color(s["chg_5d"])};font-weight:600">{s["chg_5d"]:+.2f}%</td>'
            rows += f'<td style="color:{color(s["chg_10d"])}">{s["chg_10d"]:+.2f}%</td>'
            rows += c20(s)
            rows += f'<td>{s["trade"]:.2f}</td>'
            rows += f'<td>{fmt_mktcap(s["mktcap"])}</td>'
            rows += f'<td style="color:#888">{s["from_sector"]}</td>'
            rows += '</tr>'
        return rows

    sth = '<thead><tr><th></th><th>股票</th><th>5日涨幅</th><th>10日涨幅</th><th>20日涨幅</th><th>现价</th><th>市值</th><th>板块</th></tr></thead>'

    # ---- 策略池板块表格 ----
    def pool_tbody():
        if not strategy_pool:
            return '<tr><td colspan="9" style="text-align:center;padding:24px;color:#666">暂无</td></tr>'
        rows = ""
        for s in strategy_pool:
            rows += '<tr>'
            rows += f'<td><b>{s["name"]}</b></td>'
            rows += f'<td style="color:{color(s["main_net"])}">{fmt_amount(s["main_net"])}</td>'
            rows += f'<td style="color:{color(s["chg_5d"])};font-weight:600">{s["chg_5d"]:+.2f}%</td>'
            rows += f'<td style="color:{color(s["chg_10d"])};font-weight:600">{s["chg_10d"]:+.2f}%</td>'
            rows += c20(s)
            rows += f'<td>{s["net_ratio"]:.1f}%</td>'
            rows += f'<td>{s["leading_name"]}</td>'
            rows += f'<td class="g">{s["total_score"]}</td>'
            rows += '</tr>'
        return rows

    pth = '<thead><tr><th>板块</th><th>主力净流入</th><th>5日</th><th>10日</th><th>20日</th><th>净占比</th><th>领涨股</th><th>评分</th></tr></thead>'

    count_5d = len(pool_5d)
    count_10d = len(pool_10d)
    count_20d = len(pool_20d)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股分析 {now}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#fafafa;color:#333;line-height:1.5;padding:20px;font-size:14px}}
.wrap{{max-width:1200px;margin:0 auto}}
h1{{font-size:1.5em;margin-bottom:4px;color:#222}}
.sub{{color:#999;font-size:.8em;margin-bottom:24px}}
.sec{{font-size:1.15em;font-weight:600;margin:28px 0 10px;color:#222;padding-bottom:6px;border-bottom:2px solid #e74c3c}}
table{{width:100%;border-collapse:collapse;margin-bottom:8px}}
th{{background:#f5f5f5;padding:8px 10px;text-align:left;font-size:.8em;color:#666;border-bottom:2px solid #eee;white-space:nowrap}}
td{{padding:7px 10px;border-bottom:1px solid #f0f0f0;white-space:nowrap;font-size:.88em}}
tr:hover{{background:#f8f8f8}}
tr.core{{background:#fff5f5}}
.g{{color:#e74c3c;font-weight:600}}
.tabs{{display:flex;gap:0;margin:0}}
.tab{{padding:8px 18px;background:#f5f5f5;border:1px solid #e0e0e0;cursor:pointer;border-radius:6px 6px 0 0;color:#888;font-weight:500;font-size:.85em;transition:.15s;border-bottom:none}}
.tab.active{{background:#fff;color:#333;border-bottom-color:#fff;font-weight:600}}
.tc{{display:none;background:#fff;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 8px 8px;padding:10px;overflow-x:auto}}
.tc.active{{display:block}}
.badge{{display:inline-block;background:#f0f0f0;border-radius:12px;padding:2px 10px;font-size:.78em;color:#666;margin:6px 0}}
.empty{{text-align:center;padding:30px;color:#aaa}}
.note{{margin-top:28px;padding:12px 14px;background:#fff;border:1px solid #eee;border-radius:8px;font-size:.78em;color:#999;line-height:1.8}}
.note b{{color:#666}}
@media(max-width:768px){{body{{padding:10px}}table{{font-size:.78em}}th,td{{padding:5px 6px}}}}
</style>
</head>
<body>
<div class="wrap">
<h1>A股板块主线分析</h1>
<p class="sub">{now} · 数据来源：新浪财经</p>

<!-- 股池 -->
<h2 class="sec">股池 · 三维度选股</h2>
<p style="font-size:.82em;color:#888;margin-bottom:8px">条件：来自策略池板块 · 仅主板 · 非ST · ⭐=核心主线板块</p>
<div class="tabs">
  <div class="tab active" onclick="sw('s5')">5日≥20%（{count_5d}）</div>
  <div class="tab" onclick="sw('s10')">10日≥35%（{count_10d}）</div>
  <div class="tab" onclick="sw('s20')">20日≥45%（{count_20d}）</div>
</div>
<div id="t-s5" class="tc active"><table>{sth}<tbody>{stock_tbody(pool_5d)}</tbody></table></div>
<div id="t-s10" class="tc"><table>{sth}<tbody>{stock_tbody(pool_10d)}</tbody></table></div>
<div id="t-s20" class="tc"><table>{sth}<tbody>{stock_tbody(pool_20d)}</tbody></table></div>

<!-- 策略池板块 -->
<h2 class="sec">策略池 · 强势板块（{len(strategy_pool)}）</h2>
<p style="font-size:.82em;color:#888;margin-bottom:8px">5日≥20% · 10日≥35% · 20日≥45% · 主力净流入>0</p>
<table>{pth}<tbody>{pool_tbody()}</tbody></table>

<div class="note">
<b>评分方法：</b>主力净流入排名×0.4 + 5日涨幅排名×0.3 + 10日涨幅排名×0.3<br>
<b>股池：</b>策略池板块成分股 · ⭐=核心主线板块 · 仅主板 · 非ST<br>
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


def main():
    ranked, strategy_pool, pool_5d, pool_10d, pool_20d = analyze()
    if not ranked:
        print("❌ 分析失败")
        return

    print("\n" + "=" * 60)
    print("🏆 核心主线 TOP 5:")
    print("=" * 60)
    for i, s in enumerate(ranked[:5]):
        chg_20d_str = f" | 20日:{s['chg_20d']:+6.2f}%" if s.get("chg_20d") is not None else ""
        print(f"  #{i+1} {s['name']:10s} | 得分:{s['total_score']:5.1f} | "
              f"主力:{fmt_amount(s['main_net']):>8s} | 5日:{s['chg_5d']:+6.2f}% | "
              f"10日:{s['chg_10d']:+6.2f}%{chg_20d_str} | 领涨:{s['leading_name']}")

    if strategy_pool:
        print("\n" + "=" * 60)
        print("🎯 策略池入选板块:")
        print("=" * 60)
        for i, s in enumerate(strategy_pool):
            chg_20d_str = f" | 20日:{s['chg_20d']:+6.2f}%" if s.get("chg_20d") is not None else ""
            print(f"  #{i+1} {s['name']:10s} | 得分:{s['total_score']:5.1f} | "
                  f"主力:{fmt_amount(s['main_net']):>8s} | 5日:{s['chg_5d']:+6.2f}% | "
                  f"10日:{s['chg_10d']:+6.2f}%{chg_20d_str} | 领涨:{s['leading_name']}")
    else:
        print("\n⚠️ 策略池无符合条件的板块")

    def print_stock_pool(title, pool):
        if pool:
            print(f"\n{'=' * 60}")
            print(f"📦 {title}（共 {len(pool)} 只）:")
            print("=" * 60)
            for i, s in enumerate(pool):
                core_mark = "⭐" if s["is_core"] else "  "
                chg_20d_str = f" | 20日:{s['chg_20d']:+6.2f}%" if s.get("chg_20d") is not None else ""
                print(f"  {core_mark}#{i+1:2d} {s['name']:8s} {s['symbol']:8s} | "
                      f"5日:{s['chg_5d']:+6.2f}% | 10日:{s['chg_10d']:+6.2f}%{chg_20d_str} | "
                      f"现价:{s['trade']:.2f} | 来源:{s['from_sector']}")
        else:
            print(f"\n⚠️ {title} 无符合条件的股票")

    print_stock_pool("5日涨幅≥20%", pool_5d)
    print_stock_pool("10日涨幅≥35%", pool_10d)
    print_stock_pool("20日涨幅≥45%", pool_20d)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output = os.path.join(script_dir, "sector_analysis.html")
    generate_html(ranked, strategy_pool, pool_5d, pool_10d, pool_20d, output)
    print(f"\n✅ HTML报告已生成: {output}")


if __name__ == "__main__":
    main()
