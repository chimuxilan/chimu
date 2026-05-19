#!/usr/bin/env python3
"""
筹码抢筹/出货信号回测
用历史日K模拟集合竞价判定，验证次日涨跌准确率
"""

import requests
import json
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from 分析筹码 import fetch_hist, analyze, AuctionResult

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def fetch_hist_long(code: str, days: int = 250) -> list[dict]:
    """获取较长历史日K（腾讯接口）"""
    try:
        sym = f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{sym},day,{start},{end},{days},qfq", "_var": "kline_dayqfq"},
            headers=HEADERS, timeout=15,
        )
        txt = r.text.split("=", 1)[1] if "=" in r.text else r.text
        data = json.loads(txt)
        klines = data.get("data", {}).get(sym, {})
        klines = klines.get("day") or klines.get("qfqday") or []
        result = []
        for k in klines:
            if len(k) >= 6:
                result.append({
                    "day": k[0],
                    "open": k[1],
                    "close": k[2],
                    "high": k[3],
                    "low": k[4],
                    "volume": k[5],
                })
        return result
    except Exception as e:
        print(f"  ⚠ 获取 {code} 历史数据失败: {e}")
        return []


def simulate_auction(day_data: dict, prev_data: dict, hist_before: list[dict]) -> dict:
    """
    用日K数据模拟集合竞价信号
    day_data: 当天K线
    prev_data: 前一天K线
    hist_before: 之前的K线（用于算量比等）
    返回模拟的 quote 字典
    """
    prev_close = float(prev_data["close"])
    open_price = float(day_data["open"])
    close_price = float(day_data["close"])
    high = float(day_data["high"])
    low = float(day_data["low"])
    volume = int(float(day_data["volume"]))

    # 模拟外盘比例：用收盘价 vs 开盘价估算
    # 收盘 > 开盘 → 买方占优 → 外盘偏高
    if close_price > open_price:
        # 涨的越多，外盘比例越高
        ratio = 50 + min((close_price - open_price) / open_price * 500, 20)
    elif close_price < open_price:
        ratio = 50 - min((open_price - close_price) / open_price * 500, 20)
    else:
        ratio = 50

    # 模拟买卖量
    buy_vol = int(volume * ratio / 100)
    sell_vol = volume - buy_vol

    return {
        "name": "模拟",
        "code": "000000",
        "price": close_price,
        "prev_close": prev_close,
        "open": open_price,
        "volume": volume,
        "amount": volume * (open_price + close_price) / 2 / 10000,  # 近似金额(万)
        "change_pct": (close_price - prev_close) / prev_close * 100,
        "high": high,
        "low": low,
        "amplitude": (high - low) / prev_close * 100 if prev_close > 0 else 0,
        "turnover": 0,  # 不知道流通盘，忽略
        "buy_vol": buy_vol,
        "sell_vol": sell_vol,
        "bid1_p": open_price,
        "bid1_v": 0,
        "ask1_p": open_price,
        "ask1_v": 0,
    }


def backtest_stock(code: str, name: str = "", lookback: int = 120) -> dict:
    """
    回测单只股票
    lookback: 回测天数
    """
    print(f"\n{'─'*50}")
    print(f"  回测: {name}({code})")
    print(f"{'─'*50}")

    all_klines = fetch_hist_long(code, days=lookback + 60)
    if len(all_klines) < 30:
        print(f"  ⚠ 数据不足 ({len(all_klines)} 条)，跳过")
        return {}

    # 从第 20 天开始（需要历史数据算量比）
    test_klines = all_klines[20:]
    results = []  # (日期, 判定, 次日实际涨跌)

    for i in range(1, len(test_klines)):
        day = test_klines[i]
        prev = test_klines[i - 1]
        hist_before = all_klines[max(0, i - 15):i]  # 前15天历史

        # 模拟集合竞价分析
        fake_quote = simulate_auction(day, prev, hist_before)
        result = analyze(code, fake_quote, hist_before)

        if not result:
            continue

        # 次日涨跌（如果有的话）
        next_day_data = test_klines[i + 1] if i + 1 < len(test_klines) else None
        if not next_day_data:
            continue

        next_close = float(next_day_data["close"])
        day_close = float(day["close"])
        next_change = (next_close - day_close) / day_close * 100

        results.append({
            "date": day["day"],
            "verdict": result.verdict,
            "bull_score": result.bull_score,
            "bear_score": result.bear_score,
            "net_score": result.bull_score - result.bear_score,
            "day_change": float(day["close"]) - float(prev["close"]) / float(prev["close"]) * 100,
            "next_change": next_change,
        })

    if not results:
        print("  ⚠ 无有效回测数据")
        return {}

    # 统计准确率
    bulls = [r for r in results if r["verdict"] == "真实抢筹"]
    bears = [r for r in results if r["verdict"] == "疑似出货"]
    normals = [r for r in results if r["verdict"] == "正常"]

    def calc_stats(group, label):
        if not group:
            return
        wins = [r for r in group if (label == "抢筹" and r["next_change"] > 0) or
                (label == "出货" and r["next_change"] < 0)]
        total = len(group)
        win_rate = len(wins) / total * 100
        avg_next = np.mean([r["next_change"] for r in group])
        avg_score = np.mean([r["net_score"] for r in group])
        max_win = max(r["next_change"] for r in group)
        max_loss = min(r["next_change"] for r in group)

        print(f"\n  {label}信号 ({len(group)} 次):")
        print(f"    胜率: {win_rate:.1f}% ({len(wins)}/{total})")
        print(f"    次日平均涨跌: {avg_next:+.2f}%")
        print(f"    平均净得分: {avg_score:+.1f}")
        print(f"    最大盈利: {max_win:+.2f}%  最大亏损: {max_loss:+.2f}%")
        return {
            "label": label, "total": total, "wins": len(wins),
            "win_rate": win_rate, "avg_next": avg_next,
            "avg_score": avg_score, "max_win": max_win, "max_loss": max_loss,
        }

    print(f"\n  📊 总测试天数: {len(results)}")
    bull_stats = calc_stats(bulls, "抢筹")
    bear_stats = calc_stats(bears, "出货")
    calc_stats(normals, "正常")

    # 分段准确率（按净得分分层）
    print(f"\n  📈 得分分层分析:")
    for lo, hi, label in [(20, 100, "强信号(>20分)"), (10, 20, "中信号(10-20分)"),
                           (0, 10, "弱信号(0-10分)"), (-10, 0, "弱信号(-10~0分)"),
                           (-100, -10, "强出货(<-10分)")]:
        subset = [r for r in results if lo <= r["net_score"] < hi]
        if not subset:
            continue
        up = len([r for r in subset if r["next_change"] > 0])
        avg = np.mean([r["next_change"] for r in subset])
        print(f"    {label}: {len(subset)}次, 次日涨率{up/len(subset)*100:.1f}%, 平均{avg:+.2f}%")

    return {
        "code": code, "name": name, "total_days": len(results),
        "bull_stats": bull_stats, "bear_stats": bear_stats,
        "results": results,
    }


def main():
    # 测试一组代表性股票
    stocks = [
        ("600519", "贵州茅台"),
        ("000858", "五粮液"),
        ("300750", "宁德时代"),
        ("002594", "比亚迪"),
        ("601318", "中国平安"),
        ("000001", "平安银行"),
        ("600036", "招商银行"),
        ("000725", "京东方A"),
        ("601899", "紫金矿业"),
        ("002475", "立讯精密"),
    ]

    if len(sys.argv) > 1:
        # 支持命令行指定
        stocks = [(c, c) for c in sys.argv[1:]]

    print("=" * 60)
    print("  📊 筹码信号回测 - 验证抢筹/出货判定准确率")
    print("=" * 60)

    all_stats = []
    for code, name in stocks:
        stats = backtest_stock(code, name, lookback=120)
        if stats:
            all_stats.append(stats)

    if not all_stats:
        print("\n❌ 无有效回测数据")
        return

    # 汇总
    print(f"\n{'='*60}")
    print(f"  📋 汇总 ({len(all_stats)} 只股票)")
    print(f"{'='*60}")

    total_bull_wins = sum(s["bull_stats"]["wins"] for s in all_stats if s.get("bull_stats"))
    total_bull = sum(s["bull_stats"]["total"] for s in all_stats if s.get("bull_stats"))
    total_bear_wins = sum(s["bear_stats"]["wins"] for s in all_stats if s.get("bear_stats"))
    total_bear = sum(s["bear_stats"]["total"] for s in all_stats if s.get("bear_stats"))

    if total_bull > 0:
        print(f"\n  🟢 抢筹信号汇总: {total_bull_wins}/{total_bull} 胜率 {total_bull_wins/total_bull*100:.1f}%")
    else:
        print(f"\n  🟢 抢筹信号: 无数据")

    if total_bear > 0:
        print(f"  🔴 出货信号汇总: {total_bear_wins}/{total_bear} 胜率 {total_bear_wins/total_bear*100:.1f}%")
    else:
        print(f"  🔴 出货信号: 无数据")

    # 逐只股票胜率表
    print(f"\n  {'股票':　<8s} {'抢筹次':>6s} {'抢筹胜率':>8s} {'出货次':>6s} {'出货胜率':>8s}")
    print(f"  {'─'*42}")
    for s in all_stats:
        bs = s.get("bull_stats", {})
        brs = s.get("bear_stats", {})
        b_total = bs.get("total", 0)
        b_wr = f"{bs.get('win_rate', 0):.1f}%" if b_total > 0 else "-"
        r_total = brs.get("total", 0)
        r_wr = f"{brs.get('win_rate', 0):.1f}%" if r_total > 0 else "-"
        label = f"{s['name'][:4]}"
        print(f"  {label:　<8s} {b_total:>6d} {b_wr:>8s} {r_total:>6d} {r_wr:>8s}")

    print(f"\n  ⚠️  注意: 回测用日K模拟集合竞价，与真实竞价数据有差异")
    print(f"  ⚠️  外盘比例为估算值，实际集合竞价更极端")
    print(f"  ⚠️  仅供验证信号逻辑，不构成投资建议")


if __name__ == "__main__":
    main()
