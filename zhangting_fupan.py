#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股涨停复盘日报生成器
======================
根据 akshare 数据自动生成每日涨停复盘报告，格式参考专业财经媒体。

用法:
    python zhangting_fupan.py                    # 默认生成今天的报告
    python zhangting_fupan.py --date 20260625    # 指定日期
    python zhangting_fupan.py --output report.md # 指定输出文件

依赖:
    pip install akshare pandas
"""

import argparse
import datetime
import time
import sys
from collections import defaultdict, Counter

import akshare as ak
import pandas as pd


# ============================================================
# 配置
# ============================================================

# 连板判定：涨幅 >= 9.5% 视为涨停（考虑四舍五入）
ZT_THRESHOLD = 9.5
# 创业板/科创板涨停阈值 19.5%
ZT_THRESHOLD_20CM = 19.5
# ST 涨停阈值 4.5%
ZT_THRESHOLD_ST = 4.5

# 板块关键词映射（用于自动归类涨停股）
SECTOR_KEYWORDS = {
    "化工": ["化工", "化学", "材料", "氟", "硅", "磷", "钛", "钾", "锂电材料", "橡胶", "塑料", "涂料", "纤维"],
    "医药": ["医药", "生物", "药业", "制药", "医疗", "基因", "疫苗", "诊断", "CXO"],
    "机器人/自动化": ["机器人", "自动化", "智能装备", "工业母机", "数控", "伺服"],
    "AI/算力": ["AI", "人工智能", "算力", "数据中心", "服务器", "光模块", "CPO", "GPU", "芯片"],
    "电力/能源": ["电力", "能源", "光伏", "风电", "储能", "电池", "核电", "热电", "供电"],
    "军工": ["军工", "国防", "航天", "航空", "兵器", "船舶", "导弹"],
    "汽车/新能源车": ["汽车", "新能源车", "锂电", "充电桩", "智能驾驶"],
    "房地产": ["地产", "房产", "物业", "建筑", "装饰"],
    "消费": ["食品", "饮料", "白酒", "服装", "纺织", "家电", "零售", "商业"],
    "金融": ["银行", "证券", "保险", "信托", "期货"],
    "传媒/游戏": ["传媒", "游戏", "影视", "广告", "出版", "文化"],
    "通信": ["通信", "5G", "6G", "光纤", "卫星", "导航"],
    "半导体": ["半导体", "集成电路", "封测", "晶圆", "EDA", "IP"],
    "ST板块": [],  # 特殊处理
}


# ============================================================
# 数据获取
# ============================================================

def get_trade_date(date_str=None):
    """获取交易日，如果非交易日则回退到最近的交易日"""
    if date_str:
        return date_str
    today = datetime.date.today()
    return today.strftime("%Y%m%d")


def fetch_zt_data(date_str):
    """获取涨停板数据"""
    print(f"📊 正在获取 {date_str} 涨停数据...")
    try:
        df = ak.stock_zt_pool_em(date=date_str)
        print(f"  ✅ 涨停股数量: {len(df)}")
        return df
    except Exception as e:
        print(f"  ❌ 获取涨停数据失败: {e}")
        return pd.DataFrame()


def fetch_zb_data(date_str):
    """获取炸板数据"""
    print(f"📊 正在获取 {date_str} 炸板数据...")
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date_str)
        print(f"  ✅ 炸板股数量: {len(df)}")
        return df
    except Exception as e:
        print(f"  ❌ 获取炸板数据失败: {e}")
        return pd.DataFrame()


def fetch_market_overview():
    """获取大盘概览数据"""
    print("📊 正在获取大盘数据...")
    try:
        # 上证指数
        sh = ak.stock_zh_index_daily(symbol="sh000001")
        # 深证成指
        sz = ak.stock_zh_index_daily(symbol="sz399001")
        print("  ✅ 大盘数据获取成功")
        return sh, sz
    except Exception as e:
        print(f"  ❌ 获取大盘数据失败: {e}")
        return None, None


def fetch_north_flow():
    """获取北向资金数据（兼容多版本 akshare）"""
    print("📊 正在获取北向资金数据...")
    apis = [
        lambda: ak.stock_hsgt_north_net_flow_in_em(symbol="北上"),
        lambda: ak.stock_hsgt_north_acc_flow_in_em(symbol="北上"),
        lambda: ak.stock_em_hsgt_north_net_flow_in(),
    ]
    for api_fn in apis:
        try:
            df = api_fn()
            if df is not None and not df.empty:
                print("  ✅ 北向资金数据获取成功")
                return df
        except Exception:
            continue
    print("  ⚠️ 北向资金数据获取失败（所有 API 均不可用）")
    return None


def fetch_stock_realtime():
    """获取全市场实时行情（用于统计涨跌家数），带重试"""
    print("📊 正在获取全市场行情...")
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                print(f"  ✅ 全市场数据: {len(df)} 只")
                return df
        except Exception as e:
            print(f"  ⚠️ 第{attempt+1}次获取失败: {e}")
            if attempt < 2:
                time.sleep(3)
    print("  ❌ 全市场数据获取失败，将跳过涨跌家数统计")
    return pd.DataFrame()


# ============================================================
# 数据处理
# ============================================================

def classify_sector(name):
    """根据股票名称自动归类板块"""
    if "ST" in name.upper():
        return "ST板块"
    for sector, keywords in SECTOR_KEYWORDS.items():
        if sector == "ST板块":
            continue
        for kw in keywords:
            if kw in name:
                return sector
    return "其他"


def get_zt_threshold(code, name):
    """根据股票代码和名称返回涨停阈值"""
    if "ST" in name.upper():
        return ZT_THRESHOLD_ST
    # 创业板 300xxx, 科创板 688xxx
    if code.startswith("30") or code.startswith("688"):
        return ZT_THRESHOLD_20CM
    return ZT_THRESHOLD


def analyze_zt_stocks(zt_df):
    """分析涨停股数据，返回结构化结果"""
    if zt_df.empty:
        return {}

    result = {
        "total": len(zt_df),
        "stocks": [],
        "sectors": defaultdict(list),
        "lianban": defaultdict(list),
        "max_lianban": 0,
    }

    for _, row in zt_df.iterrows():
        code = str(row.get("代码", ""))
        name = str(row.get("名称", ""))
        price = row.get("最新价", 0)
        change_pct = row.get("涨跌幅", 0)
        turnover = row.get("换手率", 0)
        amount = row.get("成交额", 0)
        lianban = row.get("连板数", 1)
        reason = row.get("所属行业", "") or row.get("涨停原因", "")

        # 判断是否一字板
        is_yizi = False
        try:
            open_price = row.get("开盘价", 0)
            high_price = row.get("最高价", 0)
            if open_price and high_price and abs(float(open_price) - float(high_price)) < 0.01:
                is_yizi = True
        except:
            pass

        stock_info = {
            "code": code,
            "name": name,
            "price": float(price) if price else 0,
            "change_pct": float(change_pct) if change_pct else 0,
            "turnover": float(turnover) if turnover else 0,
            "amount": float(amount) if amount else 0,
            "lianban": int(lianban) if lianban else 1,
            "reason": reason,
            "is_yizi": is_yizi,
        }

        result["stocks"].append(stock_info)

        # 板块归类
        if reason:
            result["sectors"][reason].append(stock_info)
        else:
            sector = classify_sector(name)
            result["sectors"][sector].append(stock_info)

        # 连板统计
        lb = stock_info["lianban"]
        result["lianban"][lb].append(stock_info)
        if lb > result["max_lianban"]:
            result["max_lianban"] = lb

    return result


def analyze_zb_stocks(zb_df):
    """分析炸板股数据"""
    if zb_df.empty:
        return {"total": 0, "stocks": []}

    stocks = []
    for _, row in zb_df.iterrows():
        code = str(row.get("代码", ""))
        name = str(row.get("名称", ""))
        stocks.append({"code": code, "name": name})

    return {"total": len(zb_df), "stocks": stocks}


def calc_market_sentiment(realtime_df, zt_count, zb_count):
    """计算市场情绪指标"""
    # 封板率不依赖全市场数据，先算好
    seal_rate = zt_count / (zt_count + zb_count) * 100 if (zt_count + zb_count) > 0 else 0

    if realtime_df.empty:
        return {
            "up_count": "-",
            "down_count": "-",
            "flat_count": "-",
            "total_stocks": "-",
            "total_amount": "-",
            "seal_rate": round(seal_rate, 1),
            "zt_ratio": "-",
            "up_5": "-",
            "down_5": "-",
        }

    # 涨跌家数
    up_count = len(realtime_df[realtime_df["涨跌幅"] > 0])
    down_count = len(realtime_df[realtime_df["涨跌幅"] < 0])
    flat_count = len(realtime_df[realtime_df["涨跌幅"] == 0])

    # 成交额（亿元）
    total_amount = realtime_df["成交额"].sum() / 1e8

    # 涨停占比
    zt_ratio = zt_count / len(realtime_df) * 100 if len(realtime_df) > 0 else 0

    # 涨幅分布
    up_5 = len(realtime_df[realtime_df["涨跌幅"] > 5])
    down_5 = len(realtime_df[realtime_df["涨跌幅"] < -5])

    return {
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "total_stocks": len(realtime_df),
        "total_amount": round(total_amount, 2),
        "seal_rate": round(seal_rate, 1),
        "zt_ratio": round(zt_ratio, 2),
        "up_5": up_5,
        "down_5": down_5,
    }


# ============================================================
# 报告生成
# ============================================================

def generate_headline(date_str, zt_count, zb_count, max_lb, seal_rate):
    """生成报告标题"""
    # 格式化日期
    dt = datetime.datetime.strptime(date_str, "%Y%m%d")
    date_display = f"{dt.month}.{dt.day}"

    # 根据市场情况生成标题描述
    if seal_rate >= 85:
        mood = "市场情绪高涨"
    elif seal_rate >= 75:
        mood = "市场情绪修复"
    elif seal_rate >= 60:
        mood = "市场情绪一般"
    else:
        mood = "市场情绪低迷"

    title = f"{date_display} 涨停复盘：{mood}，共{zt_count}股涨停"
    return title


def generate_report(date_str, zt_analysis, zb_analysis, sentiment, north_flow=None):
    """生成完整的复盘报告"""
    dt = datetime.datetime.strptime(date_str, "%Y%m%d")
    date_display = f"{dt.year}年{dt.month}月{dt.day}日"

    lines = []

    # ---- 标题 ----
    headline = generate_headline(
        date_str,
        zt_analysis.get("total", 0),
        zb_analysis.get("total", 0),
        zt_analysis.get("max_lianban", 0),
        sentiment.get("seal_rate", 0),
    )
    lines.append(f"# {headline}")
    lines.append("")
    lines.append(f"> 数据来源：akshare | 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # ---- 核心数据卡片 ----
    lines.append("## 📊 核心数据")
    lines.append("")
    total_zt = zt_analysis.get("total", 0)
    total_zb = zb_analysis.get("total", 0)
    seal_rate = sentiment.get("seal_rate", 0)
    max_lb = zt_analysis.get("max_lianban", 0)
    lianban_count = sum(1 for stocks in zt_analysis.get("lianban", {}).values() for s in stocks if s["lianban"] >= 2)

    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 涨停股数 | **{total_zt}** 只 |")
    lines.append(f"| 炸板股数 | **{total_zb}** 只 |")
    lines.append(f"| 封板率 | **{seal_rate}%** |")
    lines.append(f"| 连板股数 | **{lianban_count}** 只 |")
    lines.append(f"| 最高连板 | **{max_lb}** 连板 |")
    lines.append(f"| 上涨家数 | **{sentiment.get('up_count', '-')}** |")
    lines.append(f"| 下跌家数 | **{sentiment.get('down_count', '-')}** |")
    lines.append(f"| 成交额 | **{sentiment.get('total_amount', '-')}** 亿元 |")
    if north_flow is not None:
        lines.append(f"| 北向资金 | 见下方详情 |")
    lines.append("")

    # ---- 连板龙头 ----
    if max_lb >= 2:
        lines.append("## 🏆 连板龙头")
        lines.append("")
        # 按连板数倒序
        sorted_lb = sorted(
            zt_analysis.get("lianban", {}).items(),
            key=lambda x: x[0],
            reverse=True,
        )
        for lb, stocks in sorted_lb:
            if lb < 2:
                break
            stock_strs = []
            for s in stocks:
                yizi_tag = "（一字板）" if s.get("is_yizi") else ""
                stock_strs.append(f"**{s['name']}**（{s['code']}）{yizi_tag}")
            lines.append(f"- **{lb}连板**：{'、'.join(stock_strs)}")
        lines.append("")

    # ---- 市场情绪 ----
    lines.append("## 📈 市场情绪")
    lines.append("")
    lines.append(f"- **涨跌家数**：上涨 {sentiment.get('up_count', '-')} 家，下跌 {sentiment.get('down_count', '-')} 家，平盘 {sentiment.get('flat_count', '-')} 家")
    lines.append(f"- **成交额**：{sentiment.get('total_amount', '-')} 亿元")
    lines.append(f"- **涨停占比**：{sentiment.get('zt_ratio', '-')}%")
    lines.append(f"- **涨幅>5%**：{sentiment.get('up_5', '-')} 只 | **跌幅>5%**：{sentiment.get('down_5', '-')} 只")

    if north_flow is not None and not north_flow.empty:
        try:
            latest = north_flow.iloc[-1]
            flow_val = latest.iloc[1] if len(latest) > 1 else None
            if flow_val is not None:
                flow_str = f"{float(flow_val):.2f}亿"
                direction = "净流入" if float(flow_val) > 0 else "净流出"
                lines.append(f"- **北向资金**：当日{direction} {flow_str}")
        except:
            pass
    lines.append("")

    # ---- 涨停板块分析 ----
    lines.append("## 🔥 涨停板块分析")
    lines.append("")

    sectors = zt_analysis.get("sectors", {})
    # 按涨停数排序
    sorted_sectors = sorted(sectors.items(), key=lambda x: len(x[1]), reverse=True)

    for sector_name, stocks in sorted_sectors:
        if sector_name == "其他" and len(stocks) < 3:
            continue  # 跳过少量未分类的
        count = len(stocks)
        pct = round(count / total_zt * 100, 1) if total_zt > 0 else 0

        lines.append(f"### {sector_name}（{count}股涨停，占比{pct}%）")
        lines.append("")

        # 代表个股（最多列10个）
        display_stocks = stocks[:10]
        stock_lines = []
        for s in display_stocks:
            lb_tag = f" {s['lianban']}连板" if s["lianban"] >= 2 else ""
            yizi = " 一字板" if s.get("is_yizi") else ""
            stock_lines.append(f"{s['name']}（{s['code']}）{lb_tag}{yizi}")
        lines.append(f"代表个股：{'、'.join(stock_lines)}")

        if len(stocks) > 10:
            lines.append(f"及其他 {len(stocks) - 10} 只")
        lines.append("")

    # ---- 连板梯队 ----
    lines.append("## 📋 连板梯队")
    lines.append("")
    lines.append("| 连板数 | 股票 |")
    lines.append("|--------|------|")

    sorted_lb = sorted(
        zt_analysis.get("lianban", {}).items(),
        key=lambda x: x[0],
        reverse=True,
    )
    for lb, stocks in sorted_lb:
        if lb < 2:
            continue
        stock_strs = [f"{s['name']}（{s['code']}）" for s in stocks]
        lines.append(f"| {lb} 连板 | {'、'.join(stock_strs)} |")
    lines.append("")

    # ---- 首板股列表 ----
    first_board = zt_analysis.get("lianban", {}).get(1, [])
    if first_board:
        lines.append("## 📝 首板涨停股")
        lines.append("")
        lines.append("| 代码 | 名称 | 涨幅 | 换手率 | 板块 |")
        lines.append("|------|------|------|--------|------|")
        for s in first_board:
            sector = classify_sector(s["name"])
            reason = s.get("reason", "") or sector
            lines.append(
                f"| {s['code']} | {s['name']} | {s['change_pct']:.2f}% | "
                f"{s['turnover']:.1f}% | {reason} |"
            )
        lines.append("")

    # ---- 炸板股 ----
    if zb_analysis.get("total", 0) > 0:
        lines.append("## ⚠️ 炸板股")
        lines.append("")
        for s in zb_analysis.get("stocks", []):
            lines.append(f"- {s['name']}（{s['code']}）")
        lines.append("")

    # ---- 尾部声明 ----
    lines.append("---")
    lines.append("")
    lines.append("*本报告由 AI 自动生成，仅供参考，不构成投资建议。数据来源于 akshare，可能存在延迟或误差。*")

    return "\n".join(lines)


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="A股涨停复盘日报生成器")
    parser.add_argument("--date", type=str, default=None, help="日期，格式 YYYYMMDD，默认今天")
    parser.add_argument("--output", type=str, default=None, help="输出文件路径，默认 zhangting_fupan_YYYYMMDD.md")
    args = parser.parse_args()

    date_str = get_trade_date(args.date)
    print(f"{'='*60}")
    print(f"  A股涨停复盘日报生成器")
    print(f"  日期: {date_str}")
    print(f"{'='*60}")
    print()

    # 1. 获取涨停数据
    zt_df = fetch_zt_data(date_str)
    time.sleep(1)

    # 2. 获取炸板数据
    zb_df = fetch_zb_data(date_str)
    time.sleep(1)

    # 3. 获取全市场行情
    realtime_df = fetch_stock_realtime()
    time.sleep(1)

    # 4. 获取北向资金
    north_flow = fetch_north_flow()

    # 5. 数据分析
    print("\n🔍 正在分析数据...")
    zt_analysis = analyze_zt_stocks(zt_df)
    zb_analysis = analyze_zb_stocks(zb_df)
    sentiment = calc_market_sentiment(
        realtime_df,
        zt_analysis.get("total", 0),
        zb_analysis.get("total", 0),
    )

    # 6. 生成报告
    print("📝 正在生成报告...")
    report = generate_report(date_str, zt_analysis, zb_analysis, sentiment, north_flow)

    # 7. 输出
    output_file = args.output or f"zhangting_fupan_{date_str}.md"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n✅ 报告已生成: {output_file}")
    print(f"   涨停 {zt_analysis.get('total', 0)} 只 | 炸板 {zb_analysis.get('total', 0)} 只 | "
          f"封板率 {sentiment.get('seal_rate', 0)}%")

    # 同时输出到控制台
    print("\n" + "=" * 60)
    print(report)

    return report


if __name__ == "__main__":
    main()
