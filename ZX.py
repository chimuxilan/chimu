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
    core = [s for s in ranked if s["level"] == "🔴 核心主线"]
    strong = [s for s in ranked if s["level"] == "🟠 强势支线"]
    potential = [s for s in ranked if s["level"] == "🟡 潜力支线"]

    def color(val):
        return "#e74c3c" if val > 0 else "#27ae60" if val < 0 else "#999"

    def make_card(s, idx):
        chg_20d_html = ""
        if s.get("chg_20d") is not None:
            chg_20d_html = f'<div><small>20日涨幅</small><b style="color:{color(s["chg_20d"])}">{s["chg_20d"]:+.2f}%</b></div>'
        return f"""
    <div class="card">
      <div class="card-head">
        <span class="r">#{idx}</span>
        <span class="nm">{s['name']}</span>
        <span class="tag">{s['category']}</span>
      </div>
      <div class="card-grid">
        <div><small>综合得分</small><b class="gold">{s['total_score']}</b></div>
        <div><small>主力净流入</small><b style="color:{color(s['main_net'])}">{fmt_amount(s['main_net'])}</b></div>
        <div><small>今日涨幅</small><b style="color:{color(s['pct_chg'])}">{s['pct_chg']:+.2f}%</b></div>
        <div><small>5日涨幅</small><b style="color:{color(s['chg_5d'])}">{s['chg_5d']:+.2f}%</b></div>
        <div><small>10日涨幅</small><b style="color:{color(s['chg_10d'])}">{s['chg_10d']:+.2f}%</b></div>
        {chg_20d_html}
        <div><small>领涨股</small><b>{s['leading_name']}</b></div>
      </div>
      <div class="card-foot">
        <span>流入#{s['flow_rank']}</span><span>5日#{s['rank_5d']}</span><span>10日#{s['rank_10d']}</span>
      </div>
    </div>"""

    cards = "".join(make_card(s, i + 1) for i, s in enumerate(core))

    # 策略池卡片
    def make_pool_card(s, idx):
        return f"""
    <div class="pool-card">
      <div class="card-head" style="background:linear-gradient(135deg,rgba(231,76,60,.15),rgba(241,196,15,.1))">
        <span class="r">#{idx}</span>
        <span class="nm">{s['name']}</span>
        <span class="tag" style="background:rgba(231,76,60,.2);color:#ff6b6b">策略池</span>
      </div>
      <div class="card-grid">
        <div><small>综合得分</small><b class="gold">{s['total_score']}</b></div>
        <div><small>主力净流入</small><b style="color:{color(s['main_net'])}">{fmt_amount(s['main_net'])}</b></div>
        <div><small>今日涨幅</small><b style="color:{color(s['pct_chg'])}">{s['pct_chg']:+.2f}%</b></div>
        <div><small>5日涨幅</small><b style="color:{color(s['chg_5d'])};font-size:1.1em">{s['chg_5d']:+.2f}%</b></div>
        <div><small>10日涨幅</small><b style="color:{color(s['chg_10d'])};font-size:1.1em">{s['chg_10d']:+.2f}%</b></div>
        <div><small>20日涨幅</small><b style="color:{color(s['chg_20d'])};font-size:1.1em">{s['chg_20d']:+.2f}%</b></div>
        <div><small>领涨股</small><b>{s['leading_name']}</b></div>
        <div><small>净占比</small><b>{s['net_ratio']:.2f}%</b></div>
      </div>
      <div class="card-foot" style="background:rgba(231,76,60,.08)">
        <span>流入#{s['flow_rank']}</span><span>5日#{s['rank_5d']}</span><span>10日#{s['rank_10d']}</span>
      </div>
    </div>"""

    pool_cards = "".join(make_pool_card(s, i + 1) for i, s in enumerate(strategy_pool))
    pool_empty = '<div class="note" style="text-align:center;padding:30px"><b>暂无符合条件的板块</b><br>筛选条件：5日≥20% & 10日≥35% & 20日≥45% & 主力净流入>0</div>' if not strategy_pool else ""

    # ---- 股池表格 ----
    def make_stock_rows(items):
        rows = ""
        for s in items:
            core_badge = '<span class="core-badge">⭐ 主线</span>' if s["is_core"] else ""
            chg_20d_val = f"{s['chg_20d']:+.2f}%" if s.get("chg_20d") is not None else "—"
            chg_20d_clr = color(s["chg_20d"]) if s.get("chg_20d") is not None else "#999"
            rows += f"""<tr class="{'stock-core' if s['is_core'] else ''}">
<td>{core_badge}</td>
<td><b>{s['name']}</b><br><small style="color:#8b949e">{s['symbol']}</small></td>
<td style="color:{color(s['chg_today'])}">{s['chg_today']:+.2f}%</td>
<td style="color:{color(s['chg_5d'])};font-weight:700;font-size:.95em">{s['chg_5d']:+.2f}%</td>
<td style="color:{color(s['chg_10d'])};font-weight:700;font-size:.95em">{s['chg_10d']:+.2f}%</td>
<td style="color:{chg_20d_clr};font-weight:700;font-size:.95em">{chg_20d_val}</td>
<td>{s['trade']:.2f}</td>
<td>{fmt_mktcap(s['mktcap'])}</td>
<td>{s['turnover']:.1f}%</td>
<td><small>{s['from_sector']}</small></td>
<td class="gold">{s['composite']}</td></tr>"""
        return rows

    stock_thead = """<thead><tr>
<th>标记</th><th>股票</th><th>今日</th>
<th>5日涨幅</th><th>10日涨幅</th><th>20日涨幅</th>
<th>现价</th><th>总市值</th><th>换手率</th>
<th>来源板块</th><th>综合分</th>
</tr></thead>"""

    def make_stock_table(items, empty_msg="暂无符合条件的股票"):
        if not items:
            return f'<tr><td colspan="11" style="text-align:center;padding:20px;color:#8b949e">{empty_msg}</td></tr>'
        return make_stock_rows(items)

    stock_5d_rows = make_stock_table(pool_5d, "暂无5日涨幅≥20%的股票")
    stock_10d_rows = make_stock_table(pool_10d, "暂无10日涨幅≥35%的股票")
    stock_20d_rows = make_stock_table(pool_20d, "暂无20日涨幅≥45%的股票")

    # ---- 板块排名表 ----
    def make_rows(items, cls=""):
        rows = ""
        for s in items:
            chg_20d_val = f"{s['chg_20d']:+.2f}%" if s.get("chg_20d") is not None else "—"
            chg_20d_clr = color(s["chg_20d"]) if s.get("chg_20d") is not None else "#999"
            rows += f"""<tr class="{cls}">
<td>{s['level']}</td><td><b>{s['name']}</b></td><td>{s['category']}</td>
<td class="gold">{s['total_score']}</td>
<td style="color:{color(s['main_net'])}">{fmt_amount(s['main_net'])}</td>
<td style="color:{color(s['pct_chg'])}">{s['pct_chg']:+.2f}%</td>
<td style="color:{color(s['chg_5d'])}">{s['chg_5d']:+.2f}%</td>
<td style="color:{color(s['chg_10d'])}">{s['chg_10d']:+.2f}%</td>
<td style="color:{chg_20d_clr}">{chg_20d_val}</td>
<td>{s['net_ratio']:.2f}%</td>
<td>{s['leading_name']}</td>
<td>{s['flow_rank']}</td><td>{s['rank_5d']}</td><td>{s['rank_10d']}</td></tr>"""
        return rows

    def make_pool_rows(items):
        rows = ""
        for s in items:
            rows += f"""<tr class="pool-row">
<td><b class="gold">{s['name']}</b></td><td>{s['category']}</td>
<td class="gold">{s['total_score']}</td>
<td style="color:{color(s['main_net'])}">{fmt_amount(s['main_net'])}</td>
<td style="color:{color(s['pct_chg'])}">{s['pct_chg']:+.2f}%</td>
<td style="color:{color(s['chg_5d'])};font-weight:700">{s['chg_5d']:+.2f}%</td>
<td style="color:{color(s['chg_10d'])};font-weight:700">{s['chg_10d']:+.2f}%</td>
<td style="color:{color(s['chg_20d'])};font-weight:700">{s['chg_20d']:+.2f}%</td>
<td>{s['net_ratio']:.2f}%</td>
<td>{s['leading_name']}</td>
<td>{s['flow_rank']}</td></tr>"""
        return rows

    thead = """<thead><tr>
<th>级别</th><th>板块</th><th>分类</th><th>综合分</th>
<th>主力净流入</th><th>今日</th><th>5日</th><th>10日</th><th>20日</th>
<th>净占比</th><th>领涨股</th><th>流入#</th><th>5日#</th><th>10日#</th>
</tr></thead>"""

    pool_thead = """<thead><tr>
<th>板块</th><th>分类</th><th>综合分</th>
<th>主力净流入</th><th>今日</th><th>5日</th><th>10日</th><th>20日</th>
<th>净占比</th><th>领涨股</th><th>流入#</th>
</tr></thead>"""

    count_5d = len(pool_5d)
    count_10d = len(pool_10d)
    count_20d = len(pool_20d)
    core_5d = len([s for s in pool_5d if s["is_core"]])
    core_10d = len([s for s in pool_10d if s["is_core"]])
    core_20d = len([s for s in pool_20d if s["is_core"]])

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股板块主线分析 {now}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#0d1117;color:#c9d1d9;line-height:1.6;padding:16px}}
.wrap{{max-width:1400px;margin:0 auto}}
h1{{text-align:center;font-size:1.8em;margin:16px 0 4px;background:linear-gradient(90deg,#e74c3c,#f1c40f,#e74c3c);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
.sub{{text-align:center;color:#8b949e;font-size:.85em;margin-bottom:20px}}
.bar{{display:flex;justify-content:center;gap:16px;flex-wrap:wrap;margin:16px 0 24px}}
.bar .it{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 20px;text-align:center;min-width:140px}}
.bar .it .n{{font-size:1.8em;font-weight:700}}
.bar .it .l{{color:#8b949e;font-size:.8em}}
.sec{{font-size:1.3em;margin:24px 0 12px;padding-left:10px;border-left:4px solid #f1c40f}}
.sec.pool{{border-left-color:#e74c3c}}
.sec.stock{{border-left-color:#58a6ff}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin:12px 0}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden;transition:.2s}}
.card:hover{{transform:translateY(-2px);box-shadow:0 6px 20px rgba(231,76,60,.12)}}
.card-head{{padding:10px 14px;background:linear-gradient(135deg,rgba(231,76,60,.1),rgba(241,196,15,.06));display:flex;align-items:center;gap:6px}}
.r{{font-size:1.2em;font-weight:700;color:#f1c40f;min-width:28px}}
.nm{{font-weight:700;font-size:1.05em;flex:1}}
.tag{{font-size:.7em;background:rgba(56,139,253,.15);color:#58a6ff;padding:2px 7px;border-radius:8px}}
.card-grid{{padding:10px 14px;display:grid;grid-template-columns:1fr 1fr;gap:6px}}
.card-grid div{{display:flex;flex-direction:column}}
.card-grid small{{font-size:.7em;color:#8b949e}}
.card-grid b{{font-size:.95em}}
.gold{{color:#f1c40f!important}}
.card-foot{{padding:6px 14px;background:rgba(0,0,0,.25);display:flex;justify-content:space-between;font-size:.7em;color:#8b949e}}

.pool-cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;margin:12px 0}}
.pool-card{{background:#161b22;border:2px solid rgba(231,76,60,.35);border-radius:10px;overflow:hidden;transition:.2s;position:relative}}
.pool-card:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(231,76,60,.18)}}
.pool-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#e74c3c,#f1c40f,#e74c3c)}}
.pool-row{{background:rgba(231,76,60,.04)}}
.pool-badge{{display:inline-flex;align-items:center;gap:4px;background:linear-gradient(135deg,rgba(231,76,60,.15),rgba(241,196,15,.08));border:1px solid rgba(231,76,60,.3);border-radius:20px;padding:4px 12px;font-size:.8em;color:#ff6b6b;margin:8px 0}}

/* 股池样式 */
.stock-badge{{display:inline-flex;align-items:center;gap:4px;background:linear-gradient(135deg,rgba(56,139,253,.15),rgba(231,76,60,.08));border:1px solid rgba(56,139,253,.3);border-radius:20px;padding:4px 12px;font-size:.8em;color:#58a6ff;margin:8px 0}}
.core-badge{{display:inline-flex;align-items:center;gap:2px;background:rgba(231,76,60,.12);border:1px solid rgba(231,76,60,.3);border-radius:6px;padding:1px 6px;font-size:.7em;color:#ff6b6b;white-space:nowrap}}
.stock-core{{background:rgba(231,76,60,.04)}}

.tabs{{display:flex;gap:0;margin:20px 0 0;flex-wrap:wrap}}
.tab{{padding:8px 20px;background:#161b22;border:1px solid #30363d;cursor:pointer;border-radius:8px 8px 0 0;color:#8b949e;font-weight:500;font-size:.9em;transition:.2s}}
.tab.active{{background:#30363d;color:#c9d1d9;border-bottom-color:#30363d}}
.tc{{display:none;background:#161b22;border:1px solid #30363d;border-top:none;border-radius:0 0 10px 10px;padding:12px;overflow-x:auto}}
.tc.active{{display:block}}
table{{width:100%;border-collapse:collapse;font-size:.82em}}
th{{background:#0d1117;padding:8px 6px;text-align:left;border-bottom:2px solid #30363d;position:sticky;top:0;white-space:nowrap}}
td{{padding:6px;border-bottom:1px solid #21262d;white-space:nowrap}}
tr:hover{{background:rgba(255,255,255,.02)}}
.core{{background:rgba(231,76,60,.06)}}
.strong{{background:rgba(230,126,34,.04)}}
.pot{{background:rgba(241,196,15,.02)}}
.note{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;margin:20px 0;font-size:.85em;color:#8b949e}}
.note b{{color:#c9d1d9}}
@media(max-width:768px){{.cards,.pool-cards{{grid-template-columns:1fr}}.bar{{gap:8px}}.bar .it{{min-width:110px;padding:8px 12px}}}}
</style>
</head>
<body>
<div class="wrap">
<h1>🔥 A股板块主线分析</h1>
<p class="sub">{now} · 数据来源：新浪财经 · 领涨股5/10/20日涨幅作为板块趋势代理</p>

<div class="bar">
  <div class="it"><div class="n" style="color:#e74c3c">{len(core)}</div><div class="l">核心主线</div></div>
  <div class="it"><div class="n" style="color:#e67e22">{len(strong)}</div><div class="l">强势支线</div></div>
  <div class="it"><div class="n" style="color:#f1c40f">{len(potential)}</div><div class="l">潜力支线</div></div>
  <div class="it"><div class="n" style="color:#ff6b6b">{len(strategy_pool)}</div><div class="l">🎯 策略池</div></div>
  <div class="it"><div class="n" style="color:#58a6ff">{count_5d}</div><div class="l">5日≥20%</div></div>
  <div class="it"><div class="n" style="color:#58a6ff">{count_10d}</div><div class="l">10日≥35%</div></div>
  <div class="it"><div class="n" style="color:#58a6ff">{count_20d}</div><div class="l">20日≥45%</div></div>
  <div class="it"><div class="n" style="color:#58a6ff">{len(ranked)}</div><div class="l">分析总数</div></div>
</div>

<h2 class="sec stock">📦 股池 · 策略池1选股</h2>
<p style="color:#8b949e;font-size:.85em;margin-bottom:12px">入选条件：<b style="color:#ff6b6b">5日≥20%</b> · <b style="color:#ff6b6b">10日≥35%</b> · <b style="color:#ff6b6b">20日≥45%</b> · 来自策略池板块 · <b style="color:#58a6ff">仅主板</b> · <b style="color:#27ae60">非ST</b></p>

<div class="tabs" style="margin-bottom:0">
  <div class="tab active" onclick="sw('s5')">📈 5日≥20%（{count_5d}）</div>
  <div class="tab" onclick="sw('s10')">📈 10日≥35%（{count_10d}）</div>
  <div class="tab" onclick="sw('s20')">📈 20日≥45%（{count_20d}）</div>
</div>
<div id="t-s5" class="tc active" style="border-top:none;border-radius:0 0 10px 10px">
  <div class="stock-badge">📈 5日涨幅≥20% · 共 {count_5d} 只 · 主线 <b style="color:#ff6b6b">{core_5d}</b> 只 ⭐</div>
  <table>{stock_thead}<tbody>{stock_5d_rows}</tbody></table>
</div>
<div id="t-s10" class="tc">
  <div class="stock-badge">📈 10日涨幅≥35% · 共 {count_10d} 只 · 主线 <b style="color:#ff6b6b">{core_10d}</b> 只 ⭐</div>
  <table>{stock_thead}<tbody>{stock_10d_rows}</tbody></table>
</div>
<div id="t-s20" class="tc">
  <div class="stock-badge">📈 20日涨幅≥45% · 共 {count_20d} 只 · 主线 <b style="color:#ff6b6b">{core_20d}</b> 只 ⭐</div>
  <table>{stock_thead}<tbody>{stock_20d_rows}</tbody></table>
</div>

<h2 class="sec pool">🎯 策略池 · 强势板块</h2>
<p style="color:#8b949e;font-size:.85em;margin-bottom:12px">筛选条件：<b style="color:#ff6b6b">5日≥20%</b> · <b style="color:#ff6b6b">10日≥35%</b> · <b style="color:#ff6b6b">20日≥45%</b> · <b style="color:#27ae60">主力净流入>0</b></p>
<div class="pool-badge">⚡ 符合条件: {len(strategy_pool)} 个板块</div>
{pool_empty}
<div class="pool-cards">{pool_cards}</div>

{"<h2 class='sec'>🏆 核心主线 TOP 5</h2>" if core else ""}
<div class="cards">{cards}</div>

<h2 class="sec">📋 完整排名</h2>
<div class="tabs">
  <div class="tab active" onclick="sw('all')">全部 TOP50</div>
  <div class="tab" onclick="sw('core')">核心主线</div>
  <div class="tab" onclick="sw('strong')">强势支线</div>
  <div class="tab" onclick="sw('pot')">潜力支线</div>
  <div class="tab" onclick="sw('pool')" style="border-bottom:2px solid #e74c3c">🎯 策略池</div>
</div>
<div id="t-all" class="tc active"><table>{thead}<tbody>{make_rows(ranked[:50])}</tbody></table></div>
<div id="t-core" class="tc"><table>{thead}<tbody>{make_rows(core,'core')}</tbody></table></div>
<div id="t-strong" class="tc"><table>{thead}<tbody>{make_rows(strong,'strong')}</tbody></table></div>
<div id="t-pot" class="tc"><table>{thead}<tbody>{make_rows(potential,'pot')}</tbody></table></div>
<div id="t-pool" class="tc"><table>{pool_thead}<tbody>{make_pool_rows(strategy_pool)}</tbody></table></div>

<div class="note">
<h3 style="margin-bottom:8px">📐 分析方法</h3>
<p><b>数据来源：</b>新浪财经板块资金流向 + 领涨股/成分股K线</p>
<p><b>综合评分 =</b> 主力净流入排名分 × 0.4 + 5日涨幅排名分 × 0.3 + 10日涨幅排名分 × 0.3</p>
<p><b>核心主线：</b>综合得分 TOP 5 · <b>强势支线：</b>6-12名 · <b>潜力支线：</b>13-20名</p>
<p><b>🎯 策略池：</b>5日涨幅≥20% + 10日涨幅≥35% + 20日涨幅≥45% + 主力资金净流入>0</p>
<p><b>📦 股池：</b>来自策略池板块的成分股，按涨幅维度分三列展示 · ⭐标记为核心主线板块股票 · 仅主板 · 排除ST</p>
<p style="margin-top:6px">⚠️ 数据仅供参考，不构成投资建议。主线判定需结合政策面、消息面综合判断。</p>
</div>
</div>
<script>
function sw(n){{
  var t=event.target;
  var group=t.parentElement;
  group.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  var container=group.nextElementSibling;
  while(container&&container.classList.contains('tc')){{
    container.classList.remove('active');
    container=container.nextElementSibling;
  }}
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
