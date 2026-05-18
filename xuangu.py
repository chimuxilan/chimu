"""
双逻辑股票筛选系统 - 顶级操盘手版 v3.1
数据源：腾讯行情API + baostock + akshare(龙虎榜)

v3.0 增强模块（10大模块）：
  ① 市场环境自适应：根据涨跌比/涨停数/跌幅中位数 动态调整阈值
  ② 相对强度分析：个股 vs 大盘/板块超额收益
  ③ 量价语义识别：区分放量突破/放量出货/缩量上涨/量价背离
  ④ 连板股特殊逻辑：首板/2板/3板+ 妖股识别
  ⑤ 均线收敛/发散度：预判大波动方向
  ⑥ 假突破惩罚：突破前高后跌回 → 扣分
  ⑦ 板块热度轮动：识别资金攻击/抛弃的板块
  ⑧ 多周期共振：日线/周线/月线三周期确认
  ⑨ 情绪周期位置：冰点→回暖→高潮→退潮
  ⑩ 增强版综合评分：以上所有因子融合

v3.1 新增（游资龙虎榜模块）：
  ⑪ 游资席位识别：30+核心游资席位自动匹配，含溢价权重
  ⑫ 游资信号评分：多路游资合力/拉萨天团反向指标/席位溢价
  ⑬ 涨停板质量评分：封板/振幅/跳空/量比/连板层级 综合评分
  ⑭ 板块首日爆发检测：捕捉游资平铺信号

使用方式：
    python stock_scanner.py                    # 默认筛选，输出前20
    python stock_scanner.py --top 50           # 输出前50
    python stock_scanner.py --min-score 65     # 最低分数阈值
    python stock_scanner.py --export result.csv  # 导出CSV

依赖安装：
    pip install pandas numpy baostock requests akshare
"""

import argparse
import sys
import time
import warnings
import threading
import numpy as np
import pandas as pd
import requests
import baostock as bs
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

warnings.filterwarnings("ignore")

# baostock 全局锁（bs 不是线程安全的）
_bs_lock = threading.Lock()

# v3.1: akshare 龙虎榜数据（可选）
try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


# ====================================================================== #
#  颜色输出
# ====================================================================== #

class C:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    END = "\033[0m"


def colored(text, color):
    return f"{color}{text}{C.END}"


# ====================================================================== #
#  数据结构
# ====================================================================== #

@dataclass
class StockData:
    """一只股票的完整数据"""
    code: str
    name: str
    close: float = 0
    pct_change: float = 0
    volume_ratio: float = 0
    turnover: float = 0
    amount: float = 0  # 成交额（元）

    # 资金面
    main_net_inflow: float = 0  # 主力净流入（万元）
    vwap_deviation: float = 0   # VWAP偏离度（%）
    flow_quality: float = 0     # 资金质量评分

    # 技术指标
    ma5: float = 0
    ma10: float = 0
    ma20: float = 0
    ma250: float = 0
    ma5_angle: float = 0
    ma10_angle: float = 0
    dif: float = 0
    dea: float = 0
    macd_bar: float = 0
    wr: float = 0

    # 基本面
    roe: Optional[float] = None
    revenue: Optional[float] = None  # 亿元
    debt_ratio: Optional[float] = None

    # 概念
    concepts: list = field(default_factory=list)

    # 是否板块内涨停龙头
    is_sector_limit_up: bool = False
    has_limit_up_in_sector: bool = False  # 同板块有涨停

    # 涨停阈值（动态，区分ST/普通/注册制）
    limit_up_threshold: float = 9.8

    # v3.0: 增强分析结果缓存
    enhanced: dict = field(default_factory=dict)


@dataclass
class Logic1Result:
    """逻辑一结果"""
    role: str = ""          # ⭐核心 / 📈潜力 / 📊跟涨
    raw_score: float = 0    # 10维度原始分
    enhanced_score: float = 0  # v3.0: 增强分（含10大模块）
    final_score: float = 0  # 确定性调整后最终分
    adjust_reason: str = "" # 调整原因
    detail: dict = field(default_factory=dict)
    enhanced_detail: dict = field(default_factory=dict)


@dataclass
class Logic2Result:
    """逻辑二结果"""
    role: str = ""          # 板块核心 / 板块龙头 / 跟涨
    tech_score: float = 0
    fundamental_score: float = 0
    capital_score: float = 0
    logic_score: float = 0
    total_score: float = 0
    detail: dict = field(default_factory=dict)


# ====================================================================== #
#  v3.0 新增模块 ①：市场环境自适应
# ====================================================================== #

class MarketRegime:
    """市场环境检测 — 所有后续分析的基础"""

    @staticmethod
    def detect(quote_df: pd.DataFrame) -> dict:
        pct = quote_df["pct_change"].astype(float)

        up_count = (pct > 0).sum()
        down_count = (pct < 0).sum()
        up_ratio = up_count / (up_count + down_count + 1)

        limit_up = (pct >= 9.5).sum()
        limit_down = (pct <= -9.5).sum()
        median_pct = pct.median()
        breadth = (pct > 3).sum() / len(pct)

        # 涨跌停比
        zt_ratio = limit_up / (limit_down + 1)

        if up_ratio > 0.7 and limit_up > 30 and median_pct > 1.5:
            regime = "强势普涨"
            score_adj = -5
            risk = "低"
        elif up_ratio > 0.55 and median_pct > 0:
            regime = "温和偏多"
            score_adj = 0
            risk = "中"
        elif up_ratio < 0.3 and median_pct < -2:
            regime = "恐慌杀跌"
            score_adj = 15
            risk = "极高"
        elif limit_down > 20:
            regime = "极端弱势"
            score_adj = 20
            risk = "极高"
        else:
            regime = "震荡整理"
            score_adj = 5
            risk = "中高"

        return {
            "regime": regime,
            "risk_level": risk,
            "score_adjustment": score_adj,
            "up_ratio": round(up_ratio, 3),
            "median_pct": round(median_pct, 2),
            "limit_up": limit_up,
            "limit_down": limit_down,
            "zt_ratio": round(zt_ratio, 2),
            "breadth": round(breadth, 3),
        }


# ====================================================================== #
#  v3.0 新增模块 ⑨：情绪周期
# ====================================================================== #

class EmotionCycle:
    """情绪周期：冰点→回暖→高潮→退潮"""

    @staticmethod
    def detect(stocks: list) -> dict:
        pct_list = [s.pct_change for s in stocks]
        limit_ups = sum(1 for p in pct_list if p >= 9.5)
        limit_downs = sum(1 for p in pct_list if p <= -9.5)
        avg_pct = np.mean(pct_list)
        zt_ratio = limit_ups / (limit_downs + 1)

        if zt_ratio > 10 and avg_pct > 2:
            stage = "🔥高潮期"
            advice = "追强不追高，注意退潮信号"
            aggression = 0.7
        elif zt_ratio > 3 and avg_pct > 0.5:
            stage = "📈回暖期"
            advice = "积极做多，重仓龙头"
            aggression = 0.9
        elif zt_ratio < 0.5 and avg_pct < -1:
            stage = "❄️冰点期"
            advice = "极度谨慎，反转往往在此孕育"
            aggression = 0.3
        elif zt_ratio < 1 and avg_pct < 0:
            stage = "📉退潮期"
            advice = "减仓为主，等待冰点"
            aggression = 0.4
        else:
            stage = "⚖️震荡期"
            advice = "精选个股，控制仓位"
            aggression = 0.6

        return {
            "stage": stage,
            "advice": advice,
            "aggression": aggression,
            "zt_ratio": round(zt_ratio, 2),
            "avg_pct": round(avg_pct, 2),
        }


# ====================================================================== #
#  v3.0 新增模块 ⑦：板块热度轮动
# ====================================================================== #

class SectorRotation:
    """板块轮动分析"""

    @staticmethod
    def analyze(stocks: list, code_concepts: dict) -> dict:
        sector_stats = {}

        for s in stocks:
            for concept in code_concepts.get(s.code, []):
                if concept not in sector_stats:
                    sector_stats[concept] = {
                        "count": 0, "total_pct": 0, "limit_ups": 0,
                        "total_vr": 0, "total_flow": 0, "stocks": [],
                    }
                stats = sector_stats[concept]
                stats["count"] += 1
                stats["total_pct"] += s.pct_change
                stats["total_vr"] += s.volume_ratio
                stats["total_flow"] += s.main_net_inflow
                if s.pct_change >= s.limit_up_threshold:
                    stats["limit_ups"] += 1
                stats["stocks"].append(s.code)

        sector_heat = {}
        for name, stats in sector_stats.items():
            if stats["count"] < 2:
                continue
            avg_pct = stats["total_pct"] / stats["count"]
            avg_vr = stats["total_vr"] / stats["count"]
            heat_score = (
                stats["limit_ups"] * 12
                + avg_pct * 3
                + avg_vr * 2
                + stats["total_flow"] / 10000
            )
            sector_heat[name] = {
                "heat_score": round(heat_score, 1),
                "avg_pct": round(avg_pct, 2),
                "limit_ups": stats["limit_ups"],
                "count": stats["count"],
                "avg_vr": round(avg_vr, 2),
            }

        sorted_sectors = sorted(sector_heat.items(), key=lambda x: x[1]["heat_score"], reverse=True)

        return {
            "hot_sectors": sorted_sectors[:5],
            "cold_sectors": sorted_sectors[-5:] if len(sorted_sectors) > 5 else [],
            "all": sector_heat,
        }

    @staticmethod
    def get_bonus(s: StockData, code_concepts: dict, sector_heat: dict) -> tuple:
        bonus = 0
        best_sector = ""
        for concept in code_concepts.get(s.code, []):
            if concept in sector_heat:
                heat = sector_heat[concept]["heat_score"]
                if heat > bonus:
                    bonus = heat
                    best_sector = concept

        if bonus > 50:
            return 8, best_sector
        elif bonus > 20:
            return 4, best_sector
        elif bonus < -10:
            return -3, best_sector
        return 0, best_sector


# ====================================================================== #
#  v3.0 新增模块 ②~⑥ ⑧：个股增强分析函数
# ====================================================================== #

def calc_relative_strength(s: StockData, market_median_pct: float, sector_avg_pct: float) -> dict:
    """② 相对强度：超额收益"""
    alpha_market = s.pct_change - market_median_pct
    alpha_sector = s.pct_change - sector_avg_pct

    rs_score = 0
    if alpha_market > 5:
        rs_score = 10
    elif alpha_market > 3:
        rs_score = 7
    elif alpha_market > 1:
        rs_score = 4
    elif alpha_market > 0:
        rs_score = 2
    elif alpha_market > -1:
        rs_score = 0
    else:
        rs_score = -3

    sector_score = 0
    if alpha_sector > 3:
        sector_score = 6
    elif alpha_sector > 1:
        sector_score = 3
    elif alpha_sector < -2:
        sector_score = -2

    return {
        "alpha_market": round(alpha_market, 2),
        "alpha_sector": round(alpha_sector, 2),
        "rs_score": rs_score,
        "sector_score": sector_score,
        "rs_total": rs_score + sector_score,
    }


def classify_volume_price(s: StockData, kline_df: pd.DataFrame) -> dict:
    """③ 量价语义识别"""
    if kline_df is None or kline_df.empty or len(kline_df) < 10:
        return {"pattern": "数据不足", "vp_score": 0, "vol_price_corr": 0}

    close = kline_df["close"].values.astype(float)
    volume = kline_df["volume"].values.astype(float)

    vol_5 = volume[-5:]
    vol_avg_20 = np.mean(volume[-20:]) if len(volume) >= 20 else np.mean(volume)

    # 量价相关性
    if len(close) >= 6 and len(volume) >= 6:
        price_ret = np.diff(close[-6:]) / (close[-6:-1] + 1e-10)
        vol_ret = np.diff(volume[-6:]) / (volume[-6:-1] + 1e-10)
        min_len = min(len(price_ret), len(vol_ret))
        if min_len >= 3:
            correlation = np.corrcoef(price_ret[:min_len], vol_ret[:min_len])[0, 1]
        else:
            correlation = 0
    else:
        correlation = 0

    recent_max_vol = np.max(vol_5)
    today_up = close[-1] > close[-2] if len(close) > 1 else False

    if recent_max_vol > 2 * vol_avg_20 and today_up:
        pattern = "✅放量突破"
        vp_score = 8
    elif recent_max_vol > 1.5 * vol_avg_20 and today_up:
        pattern = "✅温和放量涨"
        vp_score = 5
    elif not np.isnan(correlation) and correlation < -0.3 and close[-1] > close[-5]:
        pattern = "⚠️缩量上涨(量价背离)"
        vp_score = -3
    elif recent_max_vol > 2 * vol_avg_20 and not today_up:
        pattern = "⚠️放量下跌"
        vp_score = -5
    elif volume[-1] < 0.5 * vol_avg_20 and today_up:
        pattern = "✅缩量企稳"
        vp_score = 3
    elif volume[-1] < 0.5 * vol_avg_20 and not today_up:
        pattern = "缩量阴跌"
        vp_score = -2
    else:
        pattern = "中性"
        vp_score = 0

    return {
        "pattern": pattern,
        "vp_score": vp_score,
        "vol_price_corr": round(correlation if not np.isnan(correlation) else 0, 3),
    }


def detect_consecutive_limit_up(s: StockData, kline_df: pd.DataFrame) -> dict:
    """④ 连板股检测"""
    if kline_df is None or kline_df.empty or len(kline_df) < 5:
        return {"streak": 0, "streak_score": 0, "note": "无连板"}

    threshold = s.limit_up_threshold
    close = kline_df["close"].values.astype(float)
    pct = np.diff(close) / close[:-1] * 100

    streak = 0
    for p in reversed(pct):
        if p >= threshold:
            streak += 1
        else:
            break

    if streak >= 4:
        streak_score = 15
        note = "🔥4板+妖股"
    elif streak == 3:
        streak_score = 12
        note = "🔥3板龙头"
    elif streak == 2:
        streak_score = 8
        note = "📈2板确认"
    elif streak == 1:
        streak_score = 4
        note = "首板"
    else:
        streak_score = 0
        note = "无连板"

    return {"streak": streak, "streak_score": streak_score, "note": note}


def compute_ma_convergence(s: StockData) -> dict:
    """⑤ 均线收敛/发散度"""
    mas = [s.ma5, s.ma10, s.ma20]
    if any(m == 0 for m in mas):
        return {"convergence": 0, "direction": "unknown", "cv_score": 0, "note": "数据不足"}

    mean_ma = np.mean(mas)
    std_ma = np.std(mas)
    cv = std_ma / (mean_ma + 1e-10)

    direction = "up" if s.ma5 > s.ma10 > s.ma20 else (
        "down" if s.ma5 < s.ma10 < s.ma20 else "mixed"
    )

    cv_score = 0
    if cv < 0.01:
        if direction == "up":
            cv_score = 10
            note = "⚡均线极度收敛(多头)"
        elif direction == "down":
            cv_score = -3
            note = "⚠️均线极度收敛(空头)"
        else:
            cv_score = 5
            note = "⏳均线收敛待突破"
    elif cv < 0.03:
        cv_score = 3 if direction == "up" else 0
        note = "均线适度收敛"
    else:
        cv_score = 0
        note = "均线发散"

    return {
        "convergence": round(cv, 4),
        "direction": direction,
        "cv_score": cv_score,
        "note": note,
    }


def detect_false_breakout(s: StockData, kline_df: pd.DataFrame) -> dict:
    """⑥ 假突破惩罚"""
    if kline_df is None or kline_df.empty or len(kline_df) < 30:
        return {"false_breakout": False, "fb_penalty": 0, "note": ""}

    close = kline_df["close"].values.astype(float)
    high = kline_df["high"].values.astype(float)

    prev_high = np.max(high[-25:-5]) if len(high) > 25 else np.max(high[:-5])

    recent_highs = high[-5:]
    broke_above = np.any(recent_highs > prev_high)
    currently_below = close[-1] < prev_high

    if broke_above and currently_below:
        return {"false_breakout": True, "fb_penalty": -8, "note": "❌假突破跌回"}

    # 长上影线
    if len(high) > 1:
        open_val = kline_df["open"].values[-1] if "open" in kline_df.columns else close[-2]
        upper_shadow = (high[-1] - max(close[-1], float(open_val))) / close[-1] * 100
        if upper_shadow > 3:
            return {"false_breakout": False, "fb_penalty": -4, "note": "⚠️长上影线"}

    return {"false_breakout": False, "fb_penalty": 0, "note": ""}


def multi_timeframe_confirm(s: StockData, kline_df: pd.DataFrame) -> dict:
    """⑧ 多周期共振"""
    if kline_df is None or kline_df.empty or len(kline_df) < 60:
        return {"mtf_score": 0, "mtf_note": "数据不足", "resonance": 0}

    close = kline_df["close"].values.astype(float)

    daily_up = s.ma5 > s.ma10 and s.ma5 > 0

    weekly_ma5 = np.mean(close[-5:])
    weekly_ma20 = np.mean(close[-20:])
    weekly_up = weekly_ma5 > weekly_ma20

    # 月线用更长周期：取最近60日均线 vs 全量均线作为"月线趋势"
    monthly_ma5 = np.mean(close[-20:])   # ~1个月
    monthly_ma60 = np.mean(close[-60:]) if len(close) >= 60 else monthly_ma5
    monthly_up = monthly_ma5 > monthly_ma60

    resonance = sum([daily_up, weekly_up, monthly_up])

    if resonance == 3:
        return {"mtf_score": 10, "mtf_note": "🔥三周期共振向上", "resonance": 3}
    elif resonance == 2:
        return {"mtf_score": 5, "mtf_note": "📈双周期偏多", "resonance": 2}
    elif resonance == 1:
        return {"mtf_score": 1, "mtf_note": "单周期偏多", "resonance": 1}
    else:
        return {"mtf_score": -3, "mtf_note": "❌三周期全部偏空", "resonance": 0}


# ====================================================================== #
#  v3.1 新增模块 ⑪：游资核心席位库
# ====================================================================== #

# 席位 → (游资名称, 风格标签, 溢价权重)
# 权重 > 0: 正向信号  < 0: 反向指标  绝对值越大越强
YOUZI_SEATS = {
    # === 极活跃游资 ===
    "中信证券上海凯滨路": ("呼家楼", "板块引导型", 0.7),
    "中信建投证券北京中信大厦": ("呼家楼", "板块引导型", 0.7),
    "中信证券北京呼家楼": ("呼家楼", "板块引导型", 0.7),
    "南京证券绍兴人民东路": ("呼家楼", "板块引导型", 0.7),
    "招商证券福州六一中路": ("六一中路", "波段锁仓型", 0.9),
    "华泰证券天津东丽开发区二纬路": ("六一中路", "波段锁仓型", 0.9),
    "东北证券武汉香港路": ("六一中路", "波段锁仓型", 0.9),
    "国泰君安证券泰州鼓楼南路": ("92科比", "人气引导型", 0.6),
    "兴业证券南京天元东路": ("92科比", "人气引导型", 0.6),
    "财通证券杭州上塘路": ("上塘路", "扫板封板型", 0.5),
    "财通证券杭州体育馆路": ("上塘路", "扫板封板型", 0.5),
    "财通证券嘉兴分公司": ("上塘路", "扫板封板型", 0.5),
    "联储证券浙江分公司": ("上塘路", "扫板封板型", 0.5),
    "国盛证券宁波桑田路": ("桑田路", "弱转强接力型", 0.4),
    "兴业证券陕西分公司": ("方新侠", "趋势大票型", 0.6),
    "中信证券西安朱雀大街": ("方新侠", "趋势大票型", 0.6),
    "国泰君安证券宜昌珍珠路": ("消闲派", "首板高胜率型", 0.8),
    "中国银河证券大连黄河路": ("陈小群", "主动引导型", 0.7),
    "东亚前海证券苏州留园路": ("陈小群/腾得系", "主动引导型", 0.5),
    "中国银河证券大连金马路": ("陈小群", "主动引导型", 0.7),
    "东莞证券湖北分公司": ("思明南路", "平铺套利型", 0.7),
    "东亚前海证券上海分公司": ("思明南路", "平铺套利型", 0.7),
    # 腾得系（坐庄型，反向指标）
    "东吴证券苏州干将东路": ("腾得系", "坐庄型", -0.3),
    "湘财证券杭州五星路": ("腾得系", "坐庄型", -0.3),
    "申港证券广东分公司": ("腾得系", "坐庄型", -0.3),
    "申港证券深圳前海分公司": ("腾得系", "坐庄型", -0.3),
    # 炒股养家
    "华鑫证券宛平南路": ("炒股养家", "通道一字板型", 0.8),
    "华鑫证券上海茅台路": ("炒股养家", "通道一字板型", 0.8),
    "华鑫证券上海松江": ("炒股养家", "通道一字板型", 0.8),
    "华鑫证券西安西大街": ("炒股养家", "通道一字板型", 0.8),
    "华鑫证券海口海德路": ("炒股养家", "通道一字板型", 0.8),
    "华鑫证券上海红宝石路": ("炒股养家", "通道一字板型", 0.8),
    # 作手新一
    "国泰君安证券南京太平南路": ("作手新一", "重仓猛干型", 0.6),
    "国泰君安证券南京金融城": ("作手新一", "重仓猛干型", 0.6),
    # 小鳄鱼
    "中投证券南京太平南路": ("小鳄鱼", "趋势接力型", 0.5),
    "南京证券南京大钟亭": ("小鳄鱼", "趋势接力型", 0.5),
    "长江证券上海世纪大道": ("小鳄鱼", "趋势接力型", 0.5),
    # 佛山系
    "国泰君安证券三亚迎宾路": ("佛山系", "硬板型", 0.4),
    "光大证券绿景路": ("佛山系", "硬板型", 0.4),
    "湘财证券佛山祖庙路": ("佛山系", "硬板型", 0.4),
    "光大证券佛山季华路": ("佛山系", "硬板型", 0.4),
    # 章盟主
    "中信证券杭州延安路": ("章盟主", "大票重仓型", 0.5),
    "国泰君安上海江苏路": ("章盟主", "大票重仓型", 0.5),
    "海通证券上海建国西路": ("章盟主", "大票重仓型", 0.5),
    # 量化基金（反向参考）
    "华泰证券总部": ("量化基金", "量化平铺型", -0.2),
    "中国国际金融上海黄浦区湖滨路": ("量化基金", "量化平铺型", -0.2),
    "中国中金财富证券北京宋庄路": ("量化基金", "量化平铺型", -0.2),
    # 拉萨天团（强反向指标）
    "东方财富拉萨团结路第一营业部": ("拉萨天团", "散户接盘型", -0.8),
    "东方财富拉萨团结路第二营业部": ("拉萨天团", "散户接盘型", -0.8),
    "东方财富拉萨东环路第一营业部": ("拉萨天团", "散户接盘型", -0.8),
    "东方财富拉萨东环路第二营业部": ("拉萨天团", "散户接盘型", -0.8),
    # 赵老哥
    "银河证券绍兴营业部": ("赵老哥", "连板接力型", 0.7),
    "浙商证券绍兴分公司": ("赵老哥", "连板接力型", 0.7),
    "银河证券北京阜成路": ("赵老哥", "连板接力型", 0.7),
    "中信证券淮海中路": ("赵老哥/孙哥", "连板接力型", 0.6),
    # 孙哥
    "中信证券上海溧阳路": ("孙哥", "题材主升型", 0.5),
    "光大证券杭州庆春路": ("孙哥", "题材主升型", 0.5),
    # 金开大道
    "中信建投重庆涪陵广场路": ("金开大道", "重仓打板型", 0.6),
    "方正证券重庆金开大道": ("金开大道", "重仓打板型", 0.6),
    # 益田路（深圳帮）
    "华鑫证券深圳益田路": ("益田路(深圳帮)", "情绪格局型", 0.7),
    # 湖滨四季
    "申港证券江苏分公司": ("湖滨四季", "低位接力型", 0.6),
    "国联证券盐城解放南路": ("湖滨四季", "低位接力型", 0.6),
    # 著名刺客
    "海通证券北京阜外大街": ("著名刺客", "龙头锁仓型", 0.6),
    # 上海超短帮
    "国泰君安上海新闸路": ("上海超短帮", "机构合力型", 0.5),
    "国泰君安上海银城中路": ("上海超短帮", "机构合力型", 0.5),
    "东方证券上海浦东新区银城中路": ("上海超短帮", "机构合力型", 0.5),
    # 余哥
    "财通证券普陀山": ("余哥", "妖股高位型", -0.3),
    "申港证券湖北分公司": ("余哥", "妖股高位型", -0.3),
    "甬兴证券四川分公司": ("余哥", "妖股高位型", -0.3),
    # 毛老板
    "方正证券乐山龙游路": ("毛老板", "二波形态型", 0.5),
    "广发证券上海东方路": ("毛老板", "二波形态型", 0.5),
    # 流沙河
    "招商证券北京车公庄西路": ("流沙河", "题材潜伏型", 0.4),
    "华泰证券上海武定路": ("流沙河", "题材潜伏型", 0.4),
    # 涅槃重升
    "上海证券苏州太湖西路": ("涅槃重升", "题材接力型", 0.5),
    "上海证券苏州干将西路": ("涅槃重升", "题材接力型", 0.5),
    # 飞云江路
    "华鑫证券杭州飞云江路": ("飞云江路", "点火接力型", 0.4),
    # N周二
    "中信证券杭州凤起路": ("N周二", "波段趋势型", 0.5),
}


# ====================================================================== #
#  v3.1 新增模块 ⑫：龙虎榜数据获取 + 游资信号评分
# ====================================================================== #

class LongHuBangFetcher:
    """龙虎榜数据获取器（基于akshare）"""

    def __init__(self):
        self._cache = {}

    def get_daily_data(self, date: str = None) -> pd.DataFrame:
        """获取龙虎榜个股数据"""
        if not HAS_AKSHARE:
            return pd.DataFrame()
        cache_key = f"lhb_{date or 'latest'}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            df = ak.stock_lhb_detail_daily_sina(date=date or datetime.now().strftime("%Y%m%d"))
            if df is not None and not df.empty:
                self._cache[cache_key] = df
                return df
        except Exception as e:
            print(f"  ⚠ 龙虎榜获取失败: {e}")
        return pd.DataFrame()

    def get_stock_detail(self, code: str, date: str = None) -> dict:
        """获取个股龙虎榜买卖详情（营业部级别）"""
        if not HAS_AKSHARE:
            return {"buy_seats": [], "sell_seats": [], "youzi_signals": []}
        try:
            df = ak.stock_lhb_stock_detail_em(
                symbol=code,
                date=date or datetime.now().strftime("%Y%m%d"),
                flag="买入"
            )
            if df is None or df.empty:
                return {"buy_seats": [], "sell_seats": [], "youzi_signals": []}

            buy_seats, sell_seats, youzi_signals = [], [], []
            for _, row in df.iterrows():
                seat_name = str(row.get("营业部名称", ""))
                buy_amt = float(row.get("买入金额", 0) or 0)
                sell_amt = float(row.get("卖出金额", 0) or 0)
                net = buy_amt - sell_amt
                seat_info = {"name": seat_name, "buy": buy_amt, "sell": sell_amt, "net": net}
                if net > 0:
                    buy_seats.append(seat_info)
                else:
                    sell_seats.append(seat_info)
                for pattern, (youzi_name, style, weight) in YOUZI_SEATS.items():
                    if pattern in seat_name:
                        youzi_signals.append({
                            "youzi": youzi_name, "style": style, "weight": weight,
                            "seat": seat_name, "buy": buy_amt, "sell": sell_amt,
                            "net": net, "is_buy": net > 0,
                        })
                        break
            return {
                "buy_seats": sorted(buy_seats, key=lambda x: x["net"], reverse=True),
                "sell_seats": sorted(sell_seats, key=lambda x: x["net"]),
                "youzi_signals": youzi_signals,
            }
        except Exception as e:
            print(f"  ⚠ 龙虎榜详情失败 {code}: {e}")
            return {"buy_seats": [], "sell_seats": [], "youzi_signals": []}


class YouziSignalScorer:
    """游资信号综合评分器"""

    @staticmethod
    def score(code: str, lhb_detail: dict, emotion_stage: str = "") -> dict:
        youzi_signals = lhb_detail.get("youzi_signals", [])
        if not youzi_signals:
            return {"youzi_score": 0, "signals": [], "warnings": [],
                    "buy_youzi": [], "sell_youzi": []}

        score = 0
        signals = []
        warnings = []
        buy_youzi = [s for s in youzi_signals if s["is_buy"]]
        sell_youzi = [s for s in youzi_signals if not s["is_buy"]]

        for sig in buy_youzi:
            weight, youzi_name, style = sig["weight"], sig["youzi"], sig["style"]
            net = sig["net"]
            if weight < 0:
                score += int(weight * 10)
                warnings.append(f"⚠️{youzi_name}({style})买入{net / 10000:.0f}万")
            else:
                bonus = int(weight * min(net / 5000, 5))
                score += bonus
                signals.append(f"✅{youzi_name}({style})买入{net / 10000:.0f}万")

        for sig in sell_youzi:
            weight, youzi_name = sig["weight"], sig["youzi"]
            net = abs(sig["net"])
            if weight > 0.6:
                penalty = int(weight * min(net / 5000, 5))
                score -= penalty
                warnings.append(f"🔴{youzi_name}卖出{net / 10000:.0f}万")

        if len(buy_youzi) >= 3:
            score += 5
            signals.append(f"🔥{len(buy_youzi)}路游资合力买入")

        lasa_count = sum(1 for s in youzi_signals if "拉萨" in s["youzi"])
        if lasa_count >= 2:
            score -= 10
            warnings.append(f"💀拉萨天团{lasa_count}席入场，次日大概率下行")

        if "冰点" in emotion_stage or "退潮" in emotion_stage:
            score = int(score * 1.3)
        elif "高潮" in emotion_stage:
            score = int(score * 0.7)

        return {
            "youzi_score": max(-20, min(25, score)),
            "signals": signals, "warnings": warnings,
            "buy_youzi": [s["youzi"] for s in buy_youzi],
            "sell_youzi": [s["youzi"] for s in sell_youzi],
        }


# ====================================================================== #
#  v3.1 新增模块 ⑬：涨停板质量评分
# ====================================================================== #

class LimitUpQualityScorer:
    """涨停板质量评分 — 区分好板烂板"""

    @staticmethod
    def score(kline_df: pd.DataFrame, today_data: dict) -> dict:
        if kline_df is None or kline_df.empty:
            return {"quality_score": 50, "quality_label": "数据不足", "details": {}}

        score = 0
        details = {}
        close = float(today_data.get("close", 0))
        high = float(today_data.get("high", 0))
        low = float(today_data.get("low", 0))
        open_val = float(today_data.get("open", 0))
        amount = float(today_data.get("amount", 0))
        if close <= 0:
            return {"quality_score": 0, "quality_label": "数据异常", "details": {}}

        # 封板判断
        if abs(close - high) / close < 0.001:
            score += 20; details["封板"] = "✅封住"
        else:
            score -= 15; details["封板"] = "❌炸板"

        # 振幅
        amplitude = (high - low) / close * 100 if close > 0 else 0
        if amplitude < 3:
            score += 15; details["振幅"] = f"极小({amplitude:.1f}%)→一字板/T字板"
        elif amplitude < 5:
            score += 10; details["振幅"] = f"小({amplitude:.1f}%)→封板坚决"
        elif amplitude < 8:
            score += 5; details["振幅"] = f"中等({amplitude:.1f}%)"
        else:
            score -= 5; details["振幅"] = f"大({amplitude:.1f}%)→分歧大"

        # 跳空
        if len(kline_df) > 0:
            prev_c = float(kline_df["close"].iloc[-1])
            gap = (open_val - prev_c) / prev_c * 100 if prev_c > 0 else 0
            if gap > 5:
                score += 10; details["跳空"] = f"高开{gap:.1f}%→强势"
            elif gap > 2:
                score += 5; details["跳空"] = f"高开{gap:.1f}%"
            elif gap < -1:
                score -= 5; details["跳空"] = f"低开{gap:.1f}%→弱"

        # 量比
        if len(kline_df) >= 5:
            avg_vol = kline_df["volume"].tail(5).mean()
            today_vol = float(today_data.get("volume", 0))
            vol_ratio = today_vol / (avg_vol + 1) if avg_vol > 0 else 1
            if 1.5 < vol_ratio < 4:
                score += 10; details["量比"] = f"温和放量({vol_ratio:.1f}倍)"
            elif vol_ratio >= 4:
                score += 3; details["量比"] = f"巨量({vol_ratio:.1f}倍)→分歧"
            elif vol_ratio < 0.8:
                score += 8; details["量比"] = f"缩量({vol_ratio:.1f}倍)→一致看多"
            else:
                score += 5; details["量比"] = f"平量({vol_ratio:.1f}倍)"

        quality_score = max(0, min(100, 50 + score))
        if quality_score >= 80:
            quality_label = "🟢优质板"
        elif quality_score >= 60:
            quality_label = "🟡合格板"
        elif quality_score >= 40:
            quality_label = "🟠一般板"
        else:
            quality_label = "🔴烂板"

        return {"quality_score": quality_score, "quality_label": quality_label, "details": details}


# ====================================================================== #
#  v3.1 新增模块 ⑭：板块首日爆发检测
# ====================================================================== #

class SectorBreakoutDetector:
    """板块首日爆发检测 — 捕捉游资平铺信号"""

    @staticmethod
    def detect(sector_heat: dict, sector_avg_pcts: dict) -> list:
        breakouts = []
        for sector_name, heat_info in sector_heat.items():
            heat_score = heat_info.get("heat_score", 0)
            avg_pct = heat_info.get("avg_pct", 0)
            limit_ups = heat_info.get("limit_ups", 0)
            count = heat_info.get("count", 0)
            if limit_ups >= 2 and avg_pct > 5 and count >= 3:
                breakouts.append({
                    "sector": sector_name, "heat": heat_score,
                    "avg_pct": avg_pct, "limit_ups": limit_ups, "count": count,
                    "reason": f"{limit_ups}只涨停,均涨{avg_pct:.1f}%,{count}只个股",
                })
        return sorted(breakouts, key=lambda x: x["heat"], reverse=True)[:5]


# ====================================================================== #
#  逻辑一：v3.0 增强版
# ====================================================================== #

class LogicOne:
    """逻辑一实现（v3.0 增强版）"""

    @staticmethod
    def classify_role(s: StockData) -> str:
        main = s.main_net_inflow
        amount = s.amount if s.amount > 0 else 1
        flow_ratio = main * 10000 / amount

        if flow_ratio < -0.05:
            return "📊跟涨"
        if flow_ratio < 0:
            return "📈潜力"
        if s.has_limit_up_in_sector:
            return "📊跟涨"

        if flow_ratio >= 0.08:
            if main >= 10000:
                return "⭐核心"
            if s.revenue and s.revenue >= 50:
                return "⭐核心"
            if s.roe and s.roe >= 15:
                return "⭐核心"
            if len(s.concepts) >= 3:
                return "⭐核心"
            if s.ma5 > s.ma10 and s.ma5 > 0:
                return "⭐核心"
            return "📈潜力"

        if flow_ratio >= 0.03:
            return "📈潜力"
        return "📊跟涨"

    @staticmethod
    def score_10d(s: StockData) -> tuple:
        """10维度基础打分"""
        score = 0
        detail = {}

        # 1. 均线多头排列
        if s.ma5 > s.ma10 > s.ma20 and s.ma5 > 0:
            score += 15; detail["均线多头排列"] = 15
        elif s.ma5 > s.ma10 and s.ma5 > 0:
            score += 8; detail["均线短期多头"] = 8
        else:
            detail["均线多头"] = 0

        # 2. 5日线陡升
        if s.ma5_angle > 60:
            score += 8; detail["5日线陡升"] = 8
        elif s.ma5_angle > 30:
            score += 4; detail["5日线上翘"] = 4
        else:
            detail["5日线角度"] = 0

        # 3. 10日线上翘
        if s.ma10_angle > 30:
            score += 5; detail["10日线上翘"] = 5
        else:
            detail["10日线上翘"] = 0

        # 4. MACD合并评分
        if s.dif > 0 and s.dea > 0 and s.macd_bar > 0:
            macd_s = 12
        elif s.dif > s.dea and s.macd_bar > 0:
            macd_s = 8
        elif s.macd_bar > 0:
            macd_s = 4
        elif s.dif > s.dea:
            macd_s = 2
        else:
            macd_s = 0
        score += macd_s; detail["MACD"] = macd_s

        # 5. 量比
        vr = s.volume_ratio
        if vr >= 3.0:
            vr_s = 12
        elif vr >= 2.0:
            vr_s = 10
        elif vr >= 1.5:
            vr_s = 6
        elif vr >= 1.0:
            vr_s = 2
        else:
            vr_s = 0
        score += vr_s; detail["量比"] = vr_s

        # 6. 主力净流入
        main = s.main_net_inflow
        if main >= 10000:
            m_s = 15
        elif main >= 5000:
            m_s = 12
        elif main >= 2000:
            m_s = 8
        elif main >= 500:
            m_s = 4
        elif main > 0:
            m_s = 1
        else:
            m_s = 0
        score += m_s; detail["主力净流入"] = m_s

        # 7. WR趋势确认
        if -50 <= s.wr <= -20:
            wr_s = 6
        elif -80 < s.wr < -50:
            wr_s = 3
        elif s.wr <= -80:
            wr_s = 1
        else:
            wr_s = 0
        score += wr_s; detail["WR"] = wr_s

        # 8. 年线位置
        if not np.isnan(s.ma250) and s.ma250 > 0:
            above_pct = (s.close - s.ma250) / s.ma250 * 100
            if above_pct > 30:
                yr_s = 8
            elif above_pct > 15:
                yr_s = 6
            elif above_pct > 5:
                yr_s = 4
            elif above_pct > 0:
                yr_s = 2
            else:
                yr_s = 0
        else:
            yr_s = 0
        score += yr_s; detail["年线位置"] = yr_s

        # 9. 换手率
        if s.turnover > 15:
            tr_s = 5
        elif s.turnover > 8:
            tr_s = 3
        elif s.turnover > 3:
            tr_s = 1
        else:
            tr_s = 0
        score += tr_s; detail["换手率"] = tr_s

        # 10. 涨幅催化
        if s.pct_change >= s.limit_up_threshold:
            zt_s = 8
        elif s.pct_change >= 7:
            zt_s = 4
        elif s.pct_change >= 5:
            zt_s = 2
        else:
            zt_s = 0
        score += zt_s; detail["涨幅催化"] = zt_s

        return min(score, 100), detail

    @staticmethod
    def adjust_score(role: str, raw_score: float, s: StockData) -> tuple:
        if role == "⭐核心":
            if raw_score < 70:
                bonus = min(int(s.main_net_inflow / 2000), 15)
                final = min(raw_score + bonus, 90)
                return final, f"核心补偿+{bonus}(主力{s.main_net_inflow:+,.0f}万)"
            return raw_score, "核心无调整"
        if role == "📈潜力" and raw_score >= 80:
            return raw_score, "高分潜力"
        return raw_score, "无需调整"

    def analyze(self, s: StockData) -> Logic1Result:
        role = self.classify_role(s)
        raw_score, detail = self.score_10d(s)
        final_score, reason = self.adjust_score(role, raw_score, s)
        return Logic1Result(
            role=role, raw_score=raw_score,
            final_score=final_score, adjust_reason=reason,
            detail=detail,
        )

    @staticmethod
    def compute_enhanced(s: StockData, kline_df: pd.DataFrame,
                         market_median: float, sector_avg: float,
                         code_concepts: dict, sector_heat: dict,
                         aggression: float) -> tuple:
        """
        v3.0: 增强评分 — 融合10大模块
        返回 (enhanced_bonus, enhanced_detail_dict)
        """
        bonus = 0
        ed = {}

        # ② 相对强度
        rs = calc_relative_strength(s, market_median, sector_avg)
        bonus += rs["rs_total"]
        if rs["rs_total"] != 0:
            ed[f"相对强度"] = rs["rs_total"]

        # ③ 量价语义
        vp = classify_volume_price(s, kline_df)
        bonus += vp["vp_score"]
        if vp["vp_score"] != 0:
            ed[f"量价:{vp['pattern']}"] = vp["vp_score"]

        # ④ 连板检测
        streak = detect_consecutive_limit_up(s, kline_df)
        bonus += streak["streak_score"]
        if streak["streak_score"] > 0:
            ed[streak["note"]] = streak["streak_score"]

        # ⑤ 均线收敛
        cv = compute_ma_convergence(s)
        bonus += cv["cv_score"]
        if cv["cv_score"] != 0:
            ed[cv["note"]] = cv["cv_score"]

        # ⑥ 假突破惩罚
        fb = detect_false_breakout(s, kline_df)
        bonus += fb["fb_penalty"]
        if fb["fb_penalty"] != 0:
            ed[fb["note"]] = fb["fb_penalty"]

        # ⑦ 板块热度
        sector_b, best_sec = SectorRotation.get_bonus(s, code_concepts, sector_heat)
        bonus += sector_b
        if sector_b != 0:
            ed[f"板块:{best_sec}"] = sector_b

        # ⑧ 多周期共振
        mtf = multi_timeframe_confirm(s, kline_df)
        bonus += mtf["mtf_score"]
        if mtf["mtf_score"] != 0:
            ed[mtf["mtf_note"]] = mtf["mtf_score"]

        # ⑨ 情绪周期调整（aggression: 0.3~0.9）
        # 在冰点期降低激进分，在回暖期放大（正负分都调整）
        if bonus != 0:
            bonus = int(bonus * aggression)

        return bonus, ed


# ====================================================================== #
#  逻辑二：v2.0 版（保持不变）
# ====================================================================== #

class LogicTwo:
    """逻辑二实现"""

    @staticmethod
    def score_tech(s: StockData) -> tuple:
        score = 0
        detail = {}

        if s.ma5_angle > 60 and s.ma5_angle > s.ma10_angle * 2:
            score += 10; detail["均线加速"] = 10
        elif s.ma5_angle > 30 and s.ma5_angle > s.ma10_angle:
            score += 7; detail["均线加速"] = 7
        else:
            detail["均线加速"] = 0

        if s.ma5_angle > 30:
            score += 4; detail["5日线上翘"] = 4
        else:
            detail["5日线上翘"] = 0

        if s.ma5 > s.ma10 > s.ma20 and s.ma5 > 0:
            score += 8; detail["均线多头排列"] = 8
        elif s.ma5 > s.ma10 and s.ma5 > 0:
            score += 4; detail["均线短期多头"] = 4
        else:
            detail["均线多头"] = 0

        if s.ma10_angle > 30:
            score += 4; detail["10日线上翘"] = 4
        else:
            detail["10日线上翘"] = 0

        amount = s.amount if s.amount > 0 else 1
        dde = (s.main_net_inflow * 10000) / amount
        if dde > 0.08:
            dde_s = 8
        elif dde > 0.05:
            dde_s = 5
        elif dde > 0.02:
            dde_s = 3
        else:
            dde_s = 0
        score += dde_s; detail["DDE"] = dde_s

        return min(score, 30), detail

    @staticmethod
    def score_fundamental(s: StockData) -> tuple:
        score = 0
        detail = {}

        roe = s.roe if s.roe is not None else 0
        if roe >= 20:
            score += 20; detail["ROE"] = 20
        elif roe >= 15:
            score += 17; detail["ROE"] = 17
        elif roe >= 10:
            score += 14; detail["ROE"] = 14
        elif roe >= 5:
            score += 10; detail["ROE"] = 10
        elif roe >= 0:
            score += 5; detail["ROE"] = 5
        elif roe >= -5:
            score += 4; detail["ROE"] = 4
        elif roe >= -10:
            score += 2; detail["ROE"] = 2
        else:
            detail["ROE"] = 0

        rev = s.revenue if s.revenue is not None else 0
        if rev >= 100:
            score += 5; detail["营收"] = 5
        elif rev >= 30:
            score += 3; detail["营收"] = 3
        else:
            detail["营收"] = 0

        dr = s.debt_ratio if s.debt_ratio is not None else 100
        if 0 <= dr <= 30:
            score += 5; detail["负债率"] = 5
        elif 30 < dr <= 50:
            score += 3; detail["负债率"] = 3
        else:
            detail["负债率"] = 0

        return min(score, 30), detail

    @staticmethod
    def score_capital(s: StockData) -> tuple:
        main = s.main_net_inflow
        detail = {}

        if main >= 30000:
            main_s = 14
        elif main >= 10000:
            main_s = 11
        elif main >= 5000:
            main_s = 8
        elif main >= 2000:
            main_s = 5
        elif main >= 500:
            main_s = 3
        elif main > 0:
            main_s = 1
        else:
            main_s = 0
        detail["主力净流入"] = main_s

        vwap_bonus = 0
        if s.vwap_deviation > 2:
            vwap_bonus = 4
        elif s.vwap_deviation > 0.5:
            vwap_bonus = 2
        elif s.vwap_deviation > 0:
            vwap_bonus = 1
        detail["VWAP偏离"] = vwap_bonus

        return min(main_s + vwap_bonus, 20), detail

    @staticmethod
    def score_logic(s: StockData) -> tuple:
        score = 0
        detail = {}

        hot_concepts = {"新能源", "人工智能", "芯片", "光伏", "锂电", "军工",
                        "数字经济", "机器人", "算力", "储能", "半导体", "汽车",
                        "医药", "消费电子", "数据要素", "低空经济", "卫星"}
        hot_count = sum(1 for c in s.concepts if any(h in c for h in hot_concepts))

        if hot_count >= 2:
            c_s = 8; detail["热门概念叠加"] = 8
        elif hot_count >= 1:
            c_s = 5; detail["热门概念"] = 5
        elif len(s.concepts) >= 2:
            c_s = 3; detail["概念"] = 3
        elif len(s.concepts) >= 1:
            c_s = 1; detail["概念"] = 1
        else:
            c_s = 0; detail["概念"] = 0
        score += c_s

        if s.amount >= 5e9:
            l_s = 5; detail["巨额成交"] = 5
        elif s.amount >= 1e9:
            l_s = 3; detail["大成交"] = 3
        else:
            l_s = 0; detail["成交"] = 0
        score += l_s

        if s.pct_change >= s.limit_up_threshold:
            z_s = 4; detail["涨停催化"] = 4
        elif s.pct_change >= 5:
            z_s = 2; detail["涨幅催化"] = 2
        else:
            z_s = 0; detail["催化"] = 0
        score += z_s

        return min(score, 20), detail

    @staticmethod
    def classify_role(total: float, main: float) -> str:
        if total >= 75 and main >= 5000:
            return "板块核心"
        if total >= 65:
            return "板块核心"
        if total >= 50:
            return "板块龙头"
        return "跟涨"

    def analyze(self, s: StockData) -> Logic2Result:
        tech, d1 = self.score_tech(s)
        fund, d2 = self.score_fundamental(s)
        cap, d3 = self.score_capital(s)
        logic, d4 = self.score_logic(s)
        total = tech + fund + cap + logic
        role = self.classify_role(total, s.main_net_inflow)

        return Logic2Result(
            role=role,
            tech_score=tech, fundamental_score=fund,
            capital_score=cap, logic_score=logic,
            total_score=total,
            detail={**d1, **d2, **d3, **d4},
        )


# ====================================================================== #
#  数据获取模块
# ====================================================================== #

class DataFetcher:
    """股票数据获取器（腾讯行情 + baostock）"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        self._bs_logged_in = False

    def _ensure_bs(self):
        if not self._bs_logged_in:
            try:
                lg = bs.login()
                if lg.error_code != '0':
                    print(f"  ⚠ baostock登录失败: {lg.error_msg}")
                    return False
                self._bs_logged_in = True
            except Exception as e:
                print(f"  ⚠ baostock登录异常: {e}")
                return False
        return True

    def _tencent_quote(self, codes: list) -> dict:
        tc_codes = []
        for c in codes:
            prefix = "sh" if c.startswith("6") else "sz"
            tc_codes.append(f"{prefix}{c}")

        result = {}
        for i in range(0, len(tc_codes), 80):
            batch = tc_codes[i:i + 80]
            url = f"https://qt.gtimg.cn/q={','.join(batch)}"
            try:
                r = self.session.get(url, timeout=10)
                r.encoding = "gbk"
                for line in r.text.strip().split("\n"):
                    if "~" not in line:
                        continue
                    parts = line.split("~")
                    if len(parts) < 45:
                        continue
                    code_raw = line.split("=")[0].split("_")[-1]
                    code = code_raw[2:]
                    try:
                        result[code] = {
                            "name": parts[1],
                            "close": self._safe_float(parts[3]),
                            "pct_change": self._safe_float(parts[32]),
                            "volume": self._safe_float(parts[6]),
                            "amount": self._safe_float(parts[37]),
                            "turnover": self._safe_float(parts[38]),
                            "volume_ratio": self._safe_float(parts[49]) if len(parts) > 49 and self._safe_float(parts[49]) > 0 else 0,
                            "high": self._safe_float(parts[33]),
                            "low": self._safe_float(parts[34]),
                            "open": self._safe_float(parts[5]),
                        }
                    except (IndexError, ValueError):
                        continue
            except Exception as e:
                print(f"  ⚠ 腾讯行情批次失败: {e}")
            time.sleep(0.2)
        return result

    @staticmethod
    def _safe_float(v):
        try:
            return float(v) if v and v.strip() not in ("", "-", "--") else 0.0
        except (ValueError, TypeError):
            return 0.0

    def get_stock_list(self) -> pd.DataFrame:
        if not self._ensure_bs():
            print("  ❌ baostock 未登录，无法获取股票列表")
            return pd.DataFrame()

        rs = bs.query_stock_basic()
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=rs.fields)

        df = df[df["type"] == "1"]
        df = df[df["status"] == "1"]
        df["code"] = df["code"].str.split(".").str[1]
        df = df[df["code"].str[:2].isin(["60", "00"])]

        print(f"  📡 获取 {len(df)} 只股票实时行情...")
        quotes = self._tencent_quote(df["code"].tolist())
        print(f"  ✅ 成功获取 {len(quotes)} 只行情")

        quote_df = pd.DataFrame.from_dict(quotes, orient="index")
        quote_df.index.name = "code"
        quote_df = quote_df.reset_index()

        quote_df = quote_df[quote_df["close"] > 0]
        quote_df = quote_df[~quote_df["name"].str.contains("ST|退", na=False)]

        return quote_df.reset_index(drop=True)

    def get_kline(self, code: str, days: int = 300) -> pd.DataFrame:
        if not self._ensure_bs():
            return pd.DataFrame()
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days + 60)).strftime("%Y-%m-%d")

        bs_code = f"{'sh' if code.startswith('6') else 'sz'}.{code}"
        try:
            with _bs_lock:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount",
                    start_date=start, end_date=end,
                    frequency="d", adjustflag="2",
                )
                rows = []
                while rs.error_code == '0' and rs.next():
                    rows.append(rs.get_row_data())
            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows, columns=rs.fields)
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").tail(days).reset_index(drop=True)
            return df
        except Exception as e:
            print(f"  ⚠ K线获取失败 {code}: {e}")
            return pd.DataFrame()

    @staticmethod
    def compute_technicals(df: pd.DataFrame) -> dict:
        if df.empty or len(df) < 60:
            return {}

        close = df["close"].values.astype(float)
        volume = df["volume"].values.astype(float)

        ma5 = pd.Series(close).rolling(5).mean().iloc[-1]
        ma10 = pd.Series(close).rolling(10).mean().iloc[-1]
        ma20 = pd.Series(close).rolling(20).mean().iloc[-1]
        ma250 = pd.Series(close).rolling(250).mean().iloc[-1] if len(close) >= 250 else np.nan

        def slope_angle(series, window=5):
            if len(series) < window:
                return 0
            tail = series[-window:]
            x = np.arange(window)
            slope = np.polyfit(x, tail, 1)[0]
            return np.degrees(np.arctan(slope / (abs(tail[-1]) + 1e-10)))

        ma5_series = pd.Series(close).rolling(5).mean().dropna().values
        ma10_series = pd.Series(close).rolling(10).mean().dropna().values
        ma5_angle = slope_angle(ma5_series)
        ma10_angle = slope_angle(ma10_series)

        ema12 = pd.Series(close).ewm(span=12).mean()
        ema26 = pd.Series(close).ewm(span=26).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9).mean()
        macd_bar = 2 * (dif - dea)

        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        h14 = pd.Series(high).rolling(14).max()
        l14 = pd.Series(low).rolling(14).min()
        h14_last = h14.iloc[-1]
        l14_last = l14.iloc[-1]
        wr = (h14_last - close[-1]) / (h14_last - l14_last + 1e-10) * -100

        vol_ratio = volume[-1] / (np.mean(volume[-6:-1]) + 1e-10)

        return {
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma250": ma250,
            "ma5_angle": round(ma5_angle, 2),
            "ma10_angle": round(ma10_angle, 2),
            "dif": dif.iloc[-1], "dea": dea.iloc[-1],
            "macd_bar": macd_bar.iloc[-1],
            "wr": round(wr, 2),
            "vol_ratio": round(vol_ratio, 2),
            "close": close[-1],
        }

    def get_capital_flow_from_quote(self, quote_df: pd.DataFrame) -> pd.DataFrame:
        df = quote_df.copy()
        volume = df["volume"].astype(float)
        amount = df["amount"].astype(float)
        close = df["close"].astype(float)

        vwap = amount / (volume * 100 + 1e-10)
        vwap_deviation = (close - vwap) / (vwap + 1e-10) * 100
        vr = df["volume_ratio"].astype(float)
        flow_quality = vwap_deviation * vr
        main_net_inflow = (amount * flow_quality / 10000 * 0.05).round(2)

        result = df[["code"]].copy()
        result["main_net_inflow"] = main_net_inflow
        result["vwap_deviation"] = vwap_deviation.round(4)
        result["flow_quality"] = flow_quality.round(4)

        return result

    def get_financial(self, code: str) -> dict:
        if not self._ensure_bs():
            return {"roe": None, "revenue": None, "debt_ratio": None}
        result = {"roe": None, "revenue": None, "debt_ratio": None}
        bs_code = f"{'sh' if code.startswith('6') else 'sz'}.{code}"

        try:
            year = datetime.now().year
            quarter = (datetime.now().month - 1) // 3 + 1
            rows2 = []
            with _bs_lock:
                for q in range(quarter, quarter - 4, -1):
                    y = year
                    qq = q
                    if qq <= 0:
                        qq += 4
                        y -= 1
                    rs = bs.query_profit_data(code=bs_code, year=y, quarter=qq)
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    if rows:
                        df = pd.DataFrame(rows, columns=rs.fields)
                        if "roeAvg" in df.columns:
                            val = pd.to_numeric(df["roeAvg"].iloc[0], errors="coerce")
                            if not np.isnan(val):
                                result["roe"] = round(val * 100, 2) if val < 1 else round(val, 2)
                        if "totalOperatingRevenue" in df.columns:
                            val = pd.to_numeric(df["totalOperatingRevenue"].iloc[0], errors="coerce")
                            if not np.isnan(val):
                                result["revenue"] = round(val / 1e8, 2)
                        break

                rs2 = bs.query_balance_data(code=bs_code, year=year, quarter=quarter)
                while rs2.next():
                    rows2.append(rs2.get_row_data())
            if rows2:
                df2 = pd.DataFrame(rows2, columns=rs2.fields)
                if "liabilityToAsset" in df2.columns:
                    val = pd.to_numeric(df2["liabilityToAsset"].iloc[0], errors="coerce")
                    if not np.isnan(val):
                        result["debt_ratio"] = round(val * 100, 2) if val < 1 else round(val, 2)
        except Exception:
            pass
        return result

    def get_concepts_batch(self) -> dict:
        code_concepts = {}
        try:
            if not self._ensure_bs():
                return code_concepts
            rs = bs.query_stock_industry()
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                df = pd.DataFrame(rows, columns=rs.fields)
                for _, row in df.iterrows():
                    code = row.get("code", "").split(".")[-1]
                    industry = row.get("industry", "")
                    if code and industry:
                        if code not in code_concepts:
                            code_concepts[code] = []
                        code_concepts[code].append(industry)
        except Exception as e:
            print(f"  ⚠ 行业分类获取失败: {e}")
        return code_concepts

    def close(self):
        if self._bs_logged_in:
            bs.logout()


# ====================================================================== #
#  涨停阈值工具
# ====================================================================== #

def get_limit_up_threshold(code: str, name: str) -> float:
    if "ST" in name:
        return 4.8
    if code.startswith("30") or code.startswith("68"):
        return 19.5
    return 9.8


# ====================================================================== #
#  板块平均涨幅计算（用于相对强度）
# ====================================================================== #

def calc_sector_avg_pcts(stocks: list, code_concepts: dict) -> dict:
    """计算每个板块的平均涨幅"""
    sector_pcts = {}
    for s in stocks:
        for concept in code_concepts.get(s.code, []):
            if concept not in sector_pcts:
                sector_pcts[concept] = []
            sector_pcts[concept].append(s.pct_change)
    return {k: np.mean(v) for k, v in sector_pcts.items() if len(v) >= 2}


def get_sector_avg(s: StockData, code_concepts: dict, sector_avg_pcts: dict) -> float:
    """获取某只股票所属板块的平均涨幅"""
    avgs = []
    for concept in code_concepts.get(s.code, []):
        if concept in sector_avg_pcts:
            avgs.append(sector_avg_pcts[concept])
    return np.mean(avgs) if avgs else 0


# ====================================================================== #
#  主流程
# ====================================================================== #

class StockScanner:
    """股票筛选器 v3.0"""

    def __init__(self, top_n=20, min_score=65, export_path=None, html_path=None):
        self.fetcher = DataFetcher()
        self.logic1 = LogicOne()
        self.logic2 = LogicTwo()
        self.top_n = top_n
        self.min_score = min_score
        self.export_path = export_path
        self.html_path = html_path or "stock_result.html"
        self.kline_cache = {}  # v3.0: K线缓存
        # v3.1: 游资模块
        self.lhb_fetcher = LongHuBangFetcher()
        self.youzi_scorer = YouziSignalScorer()
        self.quality_scorer = LimitUpQualityScorer()
        self.sector_detector = SectorBreakoutDetector()
        self.youzi_data = {}  # 龙虎榜数据缓存

    def run(self):
        print(colored("\n" + "=" * 70, C.CYAN))
        print(colored("  📊 双逻辑股票筛选系统 v3.1（顶级操盘手版）", C.BOLD))
        print(colored(f"  运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", C.DIM))
        print(colored("  数据源：腾讯行情 + baostock + akshare(龙虎榜)", C.DIM))
        print(colored("  增强：环境/强度/量价/连板/收敛/假突破/板块/多周期/情绪 + 游资席位/涨停质量", C.DIM))
        print(colored("=" * 70 + "\n", C.CYAN))

        try:
            self._do_run()
        finally:
            self.fetcher.close()

    def _do_run(self):
        # Step 1: 获取股票列表 + 实时行情
        print(colored("[1/6] 获取A股列表 + 实时行情...", C.YELLOW))
        stock_list = self.fetcher.get_stock_list()
        if stock_list.empty:
            print(colored("  ❌ 获取股票列表失败，无法继续", C.RED))
            return
        print(f"  ✅ 共 {len(stock_list)} 只有效股票")

        # Step 2: VWAP资金流向估算
        print(colored("[2/6] 估算主力资金流向（VWAP偏离法）...", C.YELLOW))
        capital_df = self.fetcher.get_capital_flow_from_quote(stock_list)
        stock_list = stock_list.merge(capital_df, on="code", how="left")
        for col in ["main_net_inflow", "vwap_deviation", "flow_quality"]:
            stock_list[col] = stock_list[col].fillna(0)
        print(f"  ✅ 资金流向估算完成")

        # Step 3: 识别涨停股
        print(colored("[3/6] 识别涨停股（动态阈值）...", C.YELLOW))
        zt_codes = set()
        for _, row in stock_list.iterrows():
            threshold = get_limit_up_threshold(row["code"], row.get("name", ""))
            if float(row.get("pct_change", 0)) >= threshold:
                zt_codes.add(row["code"])
        print(f"  ✅ 今日涨停 {len(zt_codes)} 只")

        # Step 3.5: 龙虎榜游资数据（v3.1）
        print(colored("[3.5/7] 获取龙虎榜游资数据...", C.YELLOW))
        if HAS_AKSHARE:
            try:
                lhb_df = self.lhb_fetcher.get_daily_data()
                if not lhb_df.empty:
                    print(f"  ✅ 龙虎榜 {len(lhb_df)} 条")
                    for code in zt_codes:
                        detail = self.lhb_fetcher.get_stock_detail(code)
                        if detail.get("youzi_signals"):
                            self.youzi_data[code] = detail
                            print(f"  📌 {code} → {len(detail['youzi_signals'])} 个游资信号")
                else:
                    print(f"  ⚠ 龙虎榜数据为空（非交易日或接口异常）")
            except Exception as e:
                print(f"  ⚠ 游资模块异常: {e}")
        else:
            print(f"  ⚠ akshare未安装，跳过龙虎榜模块（pip install akshare）")

        # Step 4: 获取行业板块
        print(colored("[4/6] 获取行业分类...", C.YELLOW))
        code_concepts = self.fetcher.get_concepts_batch()
        print(f"  ✅ 覆盖 {len(code_concepts)} 只股票")

        # Step 5: 构建 StockData
        print(colored("[5/6] 构建数据模型...", C.YELLOW))
        stocks = []
        for _, row in stock_list.iterrows():
            code = row["code"]
            name = row.get("name", "")
            sd = StockData(
                code=code,
                name=name,
                close=float(row.get("close", 0) or 0),
                pct_change=float(row.get("pct_change", 0) or 0),
                volume_ratio=float(row.get("volume_ratio", 0) or 0),
                turnover=float(row.get("turnover", 0) or 0),
                amount=float(row.get("amount", 0) or 0) * 10000,
                main_net_inflow=float(row.get("main_net_inflow", 0) or 0),
                vwap_deviation=float(row.get("vwap_deviation", 0) or 0),
                flow_quality=float(row.get("flow_quality", 0) or 0),
                concepts=code_concepts.get(code, []),
                is_sector_limit_up=code in zt_codes,
                limit_up_threshold=get_limit_up_threshold(code, name),
            )
            stocks.append(sd)

        stocks.sort(key=lambda x: x.amount, reverse=True)
        # 剔除涨停股票
        stocks = [s for s in stocks if s.pct_change < s.limit_up_threshold]
        candidates = stocks[:200]
        print(f"  📊 取成交额前200只做深度分析（已剔除创业板、科创板、涨停股）")

        # Step 6: 深度分析
        print(colored("[6/6] 深度分析（K线+技术+增强模块）...", C.YELLOW))
        self._enrich_technicals(candidates)
        self._mark_sector_limit_up(candidates, zt_codes, code_concepts)

        # ---- v3.0: 全局分析 ----
        print(colored("\n  🌡️ 市场环境分析...", C.YELLOW))
        market = MarketRegime.detect(stock_list)
        emotion = EmotionCycle.detect(stocks)
        effective_min = self.min_score + market["score_adjustment"]

        print(f"  {colored(market['regime'], C.BOLD)}  风险: {market['risk_level']}")
        print(f"  涨跌比: {market['up_ratio']:.1%}  涨停: {market['limit_up']}  跌停: {market['limit_down']}  中位数: {market['median_pct']:+.2f}%")
        print(f"  情绪周期: {emotion['stage']}  {emotion['advice']}")
        print(f"  有效最低分: {effective_min} (基准{self.min_score} + 环境调整{market['score_adjustment']:+d})")

        print(colored("\n  📊 板块热度分析...", C.YELLOW))
        sector_heat_result = SectorRotation.analyze(stocks, code_concepts)
        sector_avg_pcts = calc_sector_avg_pcts(stocks, code_concepts)

        if sector_heat_result["hot_sectors"]:
            print(f"  🔥 最热板块:")
            for name, info in sector_heat_result["hot_sectors"][:3]:
                print(f"    {name}: 热度{info['heat_score']:.0f}  涨停{info['limit_ups']}  "
                      f"均涨{info['avg_pct']:+.2f}%  {info['count']}只")

        # 双逻辑 + 增强分析
        print(colored("\n  🔍 双逻辑 + 增强分析中...", C.YELLOW))
        results = []
        for s in candidates:
            r1 = self.logic1.analyze(s)
            r2 = self.logic2.analyze(s)

            # v3.0: 增强评分
            kline_df = self.kline_cache.get(s.code)
            sector_avg = get_sector_avg(s, code_concepts, sector_avg_pcts)
            enhanced_bonus, enhanced_detail = self.logic1.compute_enhanced(
                s, kline_df, market["median_pct"], sector_avg,
                code_concepts, sector_heat_result["all"],
                emotion["aggression"],
            )

            r1.enhanced_score = r1.raw_score + enhanced_bonus
            r1.enhanced_detail = enhanced_detail
            r1.final_score = min(max(r1.enhanced_score, r1.final_score), 100)
            # 用增强分和基础分的较大值作为最终分
            r1.final_score = max(r1.final_score, 0)

            # v3.1: 游资信号评分
            youzi_result = self.youzi_scorer.score(
                s.code, self.youzi_data.get(s.code, {}),
                emotion.get("stage", ""),
            )
            youzi_bonus = youzi_result["youzi_score"]

            # v3.1: 涨停板质量评分（对涨停股）
            if s.pct_change >= s.limit_up_threshold:
                kline_for_q = self.kline_cache.get(s.code)
                quality = self.quality_scorer.score(kline_for_q, {
                    "close": s.close, "high": s.close,
                    "low": s.close * 0.95, "open": s.close * 0.98,
                    "volume": 0, "amount": s.amount,
                    "pct_change": s.pct_change,
                })
                quality_bonus = (quality["quality_score"] - 50) / 10
            else:
                quality = {"quality_score": 0, "quality_label": "", "details": {}}
                quality_bonus = 0

            # 融合到最终分
            r1.enhanced_score += youzi_bonus + quality_bonus
            r1.final_score = max(0, min(100, r1.final_score + youzi_bonus + quality_bonus))

            s.enhanced["youzi"] = youzi_result
            s.enhanced["quality"] = quality

            s.enhanced = {
                "rs": calc_relative_strength(s, market["median_pct"], sector_avg),
                "vp": classify_volume_price(s, kline_df) if kline_df is not None else {},
                "streak": detect_consecutive_limit_up(s, kline_df) if kline_df is not None else {},
                "cv": compute_ma_convergence(s),
                "fb": detect_false_breakout(s, kline_df) if kline_df is not None else {},
                "mtf": multi_timeframe_confirm(s, kline_df) if kline_df is not None else {},
            }

            results.append((s, r1, r2))

        # v3.1: 板块首日爆发检测
        breakouts = self.sector_detector.detect(sector_heat_result["all"], sector_avg_pcts)
        if breakouts:
            print(colored("\n  🔥 板块首日爆发信号:", C.RED))
            for bo in breakouts:
                print(f"    🚀 {bo['sector']}: {bo['reason']}")

        self._print_results(results, market, emotion, effective_min)

        if self.export_path:
            self._export_csv(results)

        self._export_html(results, market, emotion)

    def _enrich_technicals(self, stocks: list):
        """获取K线并计算技术指标（多线程并发），同时缓存K线"""
        total = len(stocks)

        def _fetch_one(s):
            try:
                kline = self.fetcher.get_kline(s.code, days=300)
                # 缓存K线供增强模块使用（需加锁，多线程写入）
                if kline is not None and not kline.empty:
                    with done_lock:
                        self.kline_cache[s.code] = kline
                tech = self.fetcher.compute_technicals(kline)
                if tech:
                    s.ma5 = tech.get("ma5", 0)
                    s.ma10 = tech.get("ma10", 0)
                    s.ma20 = tech.get("ma20", 0)
                    s.ma250 = tech.get("ma250", 0)
                    s.ma5_angle = tech.get("ma5_angle", 0)
                    s.ma10_angle = tech.get("ma10_angle", 0)
                    s.dif = tech.get("dif", 0)
                    s.dea = tech.get("dea", 0)
                    s.macd_bar = tech.get("macd_bar", 0)
                    s.wr = tech.get("wr", 0)
                fin = self.fetcher.get_financial(s.code)
                s.roe = fin.get("roe")
                s.revenue = fin.get("revenue")
                s.debt_ratio = fin.get("debt_ratio")
            except Exception as e:
                print(f"  ⚠ {s.code} 数据获取异常: {e}")

        done = [0]
        done_lock = threading.Lock()
        def _fetch_with_progress(s):
            _fetch_one(s)
            with done_lock:
                done[0] += 1
                if done[0] % 20 == 0:
                    print(f"  ⏳ {done[0]}/{total} ...")

        max_workers = min(8, total)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            pool.map(_fetch_with_progress, stocks)

        print(f"  ✅ 技术指标计算完成 ({total} 只, 缓存K线 {len(self.kline_cache)} 只)")

    def _mark_sector_limit_up(self, stocks: list, zt_codes: set, code_concepts: dict):
        zt_industries = set()
        for code in zt_codes:
            for ind in code_concepts.get(code, []):
                zt_industries.add(ind)

        for s in stocks:
            # 涨停股自身也标记（它是板块龙头），非涨停股看同板块有无涨停
            for ind in code_concepts.get(s.code, []):
                if ind in zt_industries:
                    s.has_limit_up_in_sector = True
                    break

    def _print_results(self, results: list, market: dict, emotion: dict, effective_min: float):
        results.sort(key=lambda x: x[1].final_score, reverse=True)

        print(colored("\n" + "=" * 70, C.GREEN))
        print(colored("  🏆 筛选结果", C.BOLD))
        print(colored("=" * 70, C.GREEN))

        qualified = [
            (s, r1, r2) for s, r1, r2 in results
            if r1.final_score >= effective_min or r2.total_score >= effective_min
        ][:self.top_n]

        if not qualified:
            print(colored("  ❌ 无达标股票", C.RED))
            return

        print(f"\n  {'#':<4} {'代码':<8} {'名称':<10} "
              f"{'逻辑一':^12} {'L1分':>6} {'增强':>5} "
              f"{'游资':>6} {'质量':>6} "
              f"{'逻辑二':^12} {'L2分':>6} {'主力(万)':>10}")
        print("  " + "-" * 95)

        for i, (s, r1, r2) in enumerate(qualified, 1):
            role1_color = {"⭐核心": C.RED, "📈潜力": C.YELLOW, "📊跟涨": C.DIM}
            role2_color = {"板块核心": C.RED, "板块龙头": C.YELLOW, "跟涨": C.DIM}

            r1c = role1_color.get(r1.role, "")
            r2c = role2_color.get(r2.role, "")

            main_str = f"{s.main_net_inflow:+,.0f}"
            main_c = C.GREEN if s.main_net_inflow > 0 else C.RED
            name = s.name[:4] if len(s.name) > 4 else s.name

            enhanced_str = f"+{sum(r1.enhanced_detail.values()):.0f}" if r1.enhanced_detail else "0"
            enhanced_c = C.GREEN if sum(r1.enhanced_detail.values()) > 0 else C.RED

            # v3.1: 游资信号
            yz = s.enhanced.get("youzi", {})
            youzi_str = f"{yz.get('youzi_score', 0):+d}" if yz.get("youzi_score", 0) != 0 else "—"
            youzi_c = C.GREEN if yz.get("youzi_score", 0) > 0 else (C.RED if yz.get("youzi_score", 0) < 0 else C.DIM)

            # v3.1: 涨停质量
            qt = s.enhanced.get("quality", {})
            quality_str = qt.get("quality_label", "—")[:4]

            print(f"  {i:<4} {s.code:<8} {name:<10} "
                  f"{colored(r1.role, r1c):^22} {r1.final_score:>6.1f} "
                  f"{colored(enhanced_str, enhanced_c):>5} "
                  f"{colored(youzi_str, youzi_c):>6} {quality_str:>6} "
                  f"{colored(r2.role, r2c):^22} {r2.total_score:>6.1f} "
                  f"{colored(main_str, main_c):>20}")

        # TOP3 详情
        print(colored("\n" + "-" * 70, C.DIM))
        print(colored("  📋 TOP 3 详情", C.BOLD))
        print(colored("-" * 70, C.DIM))

        for i, (s, r1, r2) in enumerate(qualified[:3], 1):
            eh = s.enhanced
            print(f"\n  {colored(f'#{i} {s.code} {s.name}', C.BOLD)}")
            print(f"  现价: {s.close:.2f}  涨跌: {s.pct_change:+.2f}%  "
                  f"量比: {s.volume_ratio:.2f}  主力: {s.main_net_inflow:+,.0f}万")
            print(f"  VWAP偏离: {s.vwap_deviation:+.2f}%  "
                  f"行业: {', '.join(s.concepts[:5]) if s.concepts else '无'}")

            # 增强模块信号
            if eh:
                signals = []
                if eh.get("streak", {}).get("streak", 0) > 0:
                    signals.append(eh["streak"]["note"])
                if eh.get("vp", {}).get("vp_score", 0) != 0:
                    signals.append(eh["vp"]["pattern"])
                if eh.get("cv", {}).get("cv_score", 0) != 0:
                    signals.append(eh["cv"]["note"])
                if eh.get("fb", {}).get("fb_penalty", 0) != 0:
                    signals.append(eh["fb"]["note"])
                if eh.get("mtf", {}).get("mtf_score", 0) != 0:
                    signals.append(eh["mtf"]["mtf_note"])
                if eh.get("rs", {}).get("rs_total", 0) != 0:
                    rs_val = eh["rs"]["rs_total"]
                    signals.append(f"超额收益{rs_val:+d}")
                # v3.1: 游资信号
                yz = eh.get("youzi", {})
                for sig in yz.get("signals", []):
                    signals.append(sig)
                for warn in yz.get("warnings", []):
                    signals.append(warn)
                if signals:
                    print(f"  信号: {' | '.join(signals)}")

            # v3.1: 涨停质量
            qt = eh.get("quality", {})
            if qt.get("quality_label"):
                print(f"  涨停质量: {qt['quality_label']} ({qt['quality_score']}分)")
                for k, v in qt.get("details", {}).items():
                    print(f"    {k}: {v}")

            print(f"\n  ── 逻辑一 ──")
            print(f"  角色: {r1.role}  基础分: {r1.raw_score:.1f}  "
                  f"增强分: {r1.enhanced_score:.1f}  最终分: {r1.final_score:.1f}")
            for k, v in r1.detail.items():
                if v > 0:
                    print(f"    {k}: +{v}")
            if r1.enhanced_detail:
                print(f"  ── 增强模块 ──")
                for k, v in r1.enhanced_detail.items():
                    if v != 0:
                        sign = "+" if v > 0 else ""
                        print(f"    {k}: {sign}{v}")

            print(f"\n  ── 逻辑二 ──")
            print(f"  角色: {r2.role}  总分: {r2.total_score:.1f}")
            print(f"  技术面: {r2.tech_score}/30  基本面: {r2.fundamental_score}/30  "
                  f"资金面: {r2.capital_score}/20  逻辑面: {r2.logic_score}/20")

    def _export_csv(self, results: list):
        rows = []
        for s, r1, r2 in results:
            eh = s.enhanced
            yz = eh.get("youzi", {})
            qt = eh.get("quality", {})
            rows.append({
                "代码": s.code, "名称": s.name,
                "现价": s.close, "涨跌幅%": s.pct_change,
                "主力净流入(万)": s.main_net_inflow,
                "VWAP偏离%": s.vwap_deviation,
                "量比": s.volume_ratio,
                "逻辑一角色": r1.role,
                "逻辑一基础分": r1.raw_score,
                "逻辑一增强分": r1.enhanced_score,
                "逻辑一最终分": r1.final_score,
                "增强加分": sum(r1.enhanced_detail.values()),
                "游资信号分": yz.get("youzi_score", 0),
                "游资买入": "|".join(yz.get("buy_youzi", [])),
                "游资卖出": "|".join(yz.get("sell_youzi", [])),
                "游资警告": "|".join(yz.get("warnings", [])),
                "涨停质量": qt.get("quality_label", ""),
                "涨停质量分": qt.get("quality_score", 0),
                "逻辑二角色": r2.role,
                "逻辑二总分": r2.total_score,
                "技术面": r2.tech_score,
                "基本面": r2.fundamental_score,
                "资金面": r2.capital_score,
                "逻辑面": r2.logic_score,
                "连板": eh.get("streak", {}).get("streak", 0),
                "量价语义": eh.get("vp", {}).get("pattern", ""),
                "均线收敛": eh.get("cv", {}).get("note", ""),
                "假突破": eh.get("fb", {}).get("note", ""),
                "多周期": eh.get("mtf", {}).get("mtf_note", ""),
                "超额收益": eh.get("rs", {}).get("rs_total", 0),
                "ROE": s.roe,
                "营收(亿)": s.revenue,
                "负债率%": s.debt_ratio,
                "概念": "|".join(s.concepts[:5]),
            })
        df = pd.DataFrame(rows)
        df.to_csv(self.export_path, index=False, encoding="utf-8-sig")
        print(colored(f"\n  💾 已导出到: {self.export_path}", C.GREEN))

    def _export_html(self, results: list, market: dict, emotion: dict):
        effective_min = self.min_score + market["score_adjustment"]
        qualified = [
            (s, r1, r2) for s, r1, r2 in results
            if r1.final_score >= effective_min or r2.total_score >= effective_min
        ][:self.top_n]

        run_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        rows_html = ""
        for i, (s, r1, r2) in enumerate(qualified, 1):
            pct_cls = "up" if s.pct_change >= 0 else "down"
            pct_sign = "+" if s.pct_change >= 0 else ""
            main_cls = "up" if s.main_net_inflow >= 0 else "down"
            main_sign = "+" if s.main_net_inflow >= 0 else ""
            r1_cls = "core" if "核心" in r1.role else ("potential" if "潜力" in r1.role else "follow")
            r2_cls = "core" if "核心" in r2.role else ("leader" if "龙头" in r2.role else "follow")
            l1_pct = min(r1.final_score, 100)
            l2_pct = min(r2.total_score, 100)
            concepts_str = "、".join(s.concepts[:3]) if s.concepts else "-"

            eh = s.enhanced
            enhanced_badges = ""
            if eh.get("streak", {}).get("streak", 0) >= 2:
                enhanced_badges += f'<span class="badge hot">{eh["streak"]["note"]}</span>'
            if eh.get("vp", {}).get("vp_score", 0) > 0:
                enhanced_badges += f'<span class="badge good">{eh["vp"]["pattern"]}</span>'
            elif eh.get("vp", {}).get("vp_score", 0) < 0:
                enhanced_badges += f'<span class="badge warn">{eh["vp"]["pattern"]}</span>'
            if eh.get("mtf", {}).get("resonance", 0) == 3:
                enhanced_badges += f'<span class="badge hot">三周期共振</span>'

            # v3.1: 游资信号
            yz = eh.get("youzi", {})
            youzi_badges = ""
            if yz.get("youzi_score", 0) > 0:
                youzi_badges += f'<span class="badge good">游资+{yz["youzi_score"]}</span>'
            elif yz.get("youzi_score", 0) < 0:
                youzi_badges += f'<span class="badge warn">游资{yz["youzi_score"]}</span>'
            for sig in yz.get("signals", [])[:2]:
                youzi_badges += f'<span class="badge good">{sig[:20]}</span>'
            for warn in yz.get("warnings", [])[:1]:
                youzi_badges += f'<span class="badge warn">{warn[:20]}</span>'

            # v3.1: 涨停质量
            qt = eh.get("quality", {})
            quality_html = qt.get("quality_label", "—") if qt.get("quality_label") else "—"

            rows_html += f"""
            <tr>
              <td class="rank">{i}</td>
              <td class="code">{s.code}</td>
              <td class="name">{s.name}</td>
              <td class="price">{s.close:.2f}</td>
              <td class="{pct_cls}">{pct_sign}{s.pct_change:.2f}%</td>
              <td>{s.volume_ratio:.2f}</td>
              <td class="{main_cls}">{main_sign}{s.main_net_inflow:,.0f}</td>
              <td><span class="tag {r1_cls}">{r1.role}</span></td>
              <td>
                <div class="score-bar"><div class="score-fill l1" style="width:{l1_pct}%"></div></div>
                <span class="score-num">{r1.final_score:.0f}</span>
              </td>
              <td><span class="tag {r2_cls}">{r2.role}</span></td>
              <td>
                <div class="score-bar"><div class="score-fill l2" style="width:{l2_pct}%"></div></div>
                <span class="score-num">{r2.total_score:.0f}</span>
              </td>
              <td class="badges">{enhanced_badges}{youzi_badges}</td>
              <td class="quality">{quality_html}</td>
              <td class="concepts">{concepts_str}</td>
            </tr>"""

        cards_html = ""
        for i, (s, r1, r2) in enumerate(qualified[:3], 1):
            medal = ["🥇", "🥈", "🥉"][i - 1]
            pct_cls = "up" if s.pct_change >= 0 else "down"
            main_cls = "up" if s.main_net_inflow >= 0 else "down"

            l1_items = "".join(
                f'<div class="detail-item"><span>{k}</span><span class="up">+{v}</span></div>'
                for k, v in r1.detail.items() if v > 0
            )
            eh_items = "".join(
                f'<div class="detail-item"><span>{k}</span><span class="{"up" if v > 0 else "down"}">{"+" if v > 0 else ""}{v}</span></div>'
                for k, v in r1.enhanced_detail.items() if v != 0
            )
            eh = s.enhanced
            signals_html = ""
            for key, label in [("streak", "连板"), ("vp", "量价"), ("cv", "收敛"),
                               ("fb", "假突破"), ("mtf", "多周期")]:
                data = eh.get(key, {})
                if data:
                    note = data.get("note", data.get("pattern", ""))
                    if note:
                        signals_html += f'<span class="signal">{note}</span>'
            # v3.1: 游资信号
            yz = eh.get("youzi", {})
            for sig in yz.get("signals", []):
                signals_html += f'<span class="signal good">{sig[:30]}</span>'
            for warn in yz.get("warnings", []):
                signals_html += f'<span class="signal warn">{warn[:30]}</span>'

            # v3.1: 涨停质量
            qt = eh.get("quality", {})
            quality_card = ""
            if qt.get("quality_label"):
                quality_card = f"""
                <div class="card-section">
                  <h4>涨停质量 · {qt['quality_label']}</h4>
                  <div class="score-row"><span>评分</span><strong>{qt['quality_score']}</strong></div>
                  <div class="detail-grid">{''.join(f'<div class="detail-item"><span>{k}</span><span>{v}</span></div>' for k, v in qt.get("details", {}).items())}</div>
                </div>"""

            cards_html += f"""
            <div class="card">
              <div class="card-header">
                <span class="medal">{medal}</span>
                <span class="card-code">{s.code}</span>
                <span class="card-name">{s.name}</span>
                <span class="card-price">{s.close:.2f}</span>
                <span class="{pct_cls}">{s.pct_change:+.2f}%</span>
              </div>
              <div class="card-body">
                <div class="card-section">
                  <h4>逻辑一 · {r1.role}</h4>
                  <div class="score-row">
                    <span>基础</span><strong>{r1.raw_score:.0f}</strong>
                    <span>增强</span><strong>{r1.enhanced_score:.0f}</strong>
                    <span>最终</span><strong>{r1.final_score:.0f}</strong>
                  </div>
                  <div class="detail-grid">{l1_items}</div>
                </div>
                <div class="card-section">
                  <h4>增强模块信号</h4>
                  <div class="signals">{signals_html if signals_html else '<span class="signal dim">无特殊信号</span>'}</div>
                  <div class="detail-grid">{eh_items}</div>
                </div>
                <div class="card-section">
                  <h4>逻辑二 · {r2.role}</h4>
                  <div class="score-row">
                    <span>技术</span><strong>{r2.tech_score}/30</strong>
                    <span>基本</span><strong>{r2.fundamental_score}/30</strong>
                    <span>资金</span><strong>{r2.capital_score}/20</strong>
                    <span>逻辑</span><strong>{r2.logic_score}/20</strong>
                  </div>
                </div>
                <div class="card-section">
                  <h4>基本面</h4>
                  <div class="score-row">
                    <span>ROE</span><strong>{s.roe if s.roe is not None else '-'}</strong>
                    <span>营收</span><strong>{s.revenue if s.revenue is not None else '-'}亿</strong>
                    <span>负债率</span><strong>{s.debt_ratio if s.debt_ratio is not None else '-'}%</strong>
                    <span>主力</span><strong class="{main_cls}">{s.main_net_inflow:+,.0f}万</strong>
                  </div>
                </div>
                {quality_card}
              </div>
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股票筛选结果 v3.1</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#0f1117; color:#e1e4e8; padding:20px; }}
  .header {{ text-align:center; padding:24px 0 16px; }}
  .header h1 {{ font-size:22px; color:#58a6ff; }}
  .header .sub {{ font-size:13px; color:#8b949e; margin-top:6px; }}
  .market-bar {{ display:flex; justify-content:center; gap:16px; margin:12px 0; flex-wrap:wrap; }}
  .market-bar .chip {{ background:#161b22; border:1px solid #30363d; border-radius:20px; padding:6px 14px; font-size:12px; }}
  .market-bar .chip .label {{ color:#8b949e; }}
  .market-bar .chip .val {{ font-weight:600; margin-left:4px; }}
  .summary {{ display:flex; justify-content:center; gap:24px; margin:16px 0 24px; flex-wrap:wrap; }}
  .summary .stat {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px 20px; text-align:center; }}
  .summary .stat .num {{ font-size:24px; font-weight:700; color:#58a6ff; }}
  .summary .stat .label {{ font-size:12px; color:#8b949e; margin-top:2px; }}
  table {{ width:100%; border-collapse:collapse; background:#161b22; border-radius:8px; overflow:hidden; font-size:13px; }}
  th {{ background:#1c2128; color:#8b949e; font-weight:600; text-align:left; padding:10px 12px; white-space:nowrap; position:sticky; top:0; }}
  td {{ padding:10px 12px; border-bottom:1px solid #21262d; white-space:nowrap; }}
  tr:hover {{ background:#1c2128; }}
  .rank {{ color:#8b949e; font-weight:600; text-align:center; }}
  .code {{ color:#58a6ff; font-family:monospace; }}
  .name {{ font-weight:600; }}
  .price {{ font-family:monospace; }}
  .up {{ color:#3fb950; }}
  .down {{ color:#f85149; }}
  .concepts {{ color:#8b949e; max-width:120px; overflow:hidden; text-overflow:ellipsis; }}
  .badges {{ max-width:200px; }}
  .badge {{ display:inline-block; padding:1px 6px; border-radius:3px; font-size:11px; margin:1px 2px; }}
  .badge.hot {{ background:#f8514922; color:#f85149; }}
  .badge.good {{ background:#3fb95022; color:#3fb950; }}
  .badge.warn {{ background:#d2992222; color:#d29922; }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:600; }}
  .tag.core {{ background:#f8514922; color:#f85149; }}
  .tag.potential {{ background:#d29922; color:#000; }}
  .tag.leader {{ background:#d29922aa; color:#000; }}
  .tag.follow {{ background:#30363d; color:#8b949e; }}
  .score-bar {{ display:inline-block; width:60px; height:6px; background:#21262d; border-radius:3px; vertical-align:middle; margin-right:6px; }}
  .score-fill {{ height:100%; border-radius:3px; }}
  .score-fill.l1 {{ background:#58a6ff; }}
  .score-fill.l2 {{ background:#d29922; }}
  .score-num {{ font-size:12px; font-weight:600; color:#e1e4e8; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); gap:16px; margin-top:24px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; overflow:hidden; }}
  .card-header {{ padding:14px 16px; display:flex; align-items:center; gap:10px; border-bottom:1px solid #21262d; }}
  .medal {{ font-size:22px; }}
  .card-code {{ font-family:monospace; color:#58a6ff; font-weight:600; }}
  .card-name {{ font-weight:700; flex:1; }}
  .card-price {{ font-family:monospace; font-size:16px; font-weight:600; }}
  .card-body {{ padding:14px 16px; }}
  .card-section {{ margin-bottom:12px; }}
  .card-section:last-child {{ margin-bottom:0; }}
  .card-section h4 {{ font-size:13px; color:#8b949e; margin-bottom:8px; }}
  .score-row {{ display:flex; gap:12px; flex-wrap:wrap; font-size:13px; }}
  .score-row span {{ color:#8b949e; }}
  .score-row strong {{ color:#e1e4e8; }}
  .signals {{ display:flex; flex-wrap:wrap; gap:4px; margin-bottom:6px; }}
  .signal {{ background:#21262d; padding:2px 8px; border-radius:4px; font-size:11px; }}
  .signal.dim {{ color:#484f58; }}
  .signal.good {{ background:#3fb95022; color:#3fb950; }}
  .signal.warn {{ background:#d2992222; color:#d29922; }}
  .quality {{ font-size:12px; white-space:nowrap; }}
  .detail-grid {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }}
  .detail-item {{ background:#21262d; padding:3px 8px; border-radius:4px; font-size:12px; display:flex; gap:6px; }}
  .section-title {{ font-size:16px; font-weight:700; color:#e1e4e8; margin:28px 0 12px; padding-left:4px; }}
  @media(max-width:768px) {{
    table {{ font-size:11px; }}
    th,td {{ padding:6px 8px; }}
    .cards {{ grid-template-columns:1fr; }}
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>📊 双逻辑股票筛选结果 v3.1</h1>
    <div class="sub">{run_time} · 顶级操盘手版 · 数据源：腾讯行情 + baostock + akshare(龙虎榜)</div>
  </div>

  <div class="market-bar">
    <div class="chip"><span class="label">市场</span><span class="val">{market['regime']}</span></div>
    <div class="chip"><span class="label">情绪</span><span class="val">{emotion['stage']}</span></div>
    <div class="chip"><span class="label">涨跌比</span><span class="val">{market['up_ratio']:.0%}</span></div>
    <div class="chip"><span class="label">涨停</span><span class="val up">{market['limit_up']}</span></div>
    <div class="chip"><span class="label">跌停</span><span class="val down">{market['limit_down']}</span></div>
    <div class="chip"><span class="label">中位数</span><span class="val {'up' if market['median_pct'] >= 0 else 'down'}">{market['median_pct']:+.2f}%</span></div>
    <div class="chip"><span class="label">建议</span><span class="val">{emotion['advice']}</span></div>
  </div>

  <div class="summary">
    <div class="stat"><div class="num">{len(qualified)}</div><div class="label">入选股票</div></div>
    <div class="stat"><div class="num">{sum(1 for s,_,_ in qualified if s.main_net_inflow > 0)}</div><div class="label">主力净流入</div></div>
    <div class="stat"><div class="num">{sum(1 for s,_,_ in qualified if s.pct_change >= 5)}</div><div class="label">强势股(≥5%)</div></div>
    <div class="stat"><div class="num">{len([1 for s,r1,_ in qualified if '核心' in r1.role])}</div><div class="label">⭐核心</div></div>
    <div class="stat"><div class="num">{sum(1 for s,_,_ in qualified if s.enhanced.get('youzi', dict()).get('youzi_score',0) > 0)}</div><div class="label">游资正面</div></div>
    <div class="stat"><div class="num">{sum(1 for s,_,_ in qualified if s.enhanced.get('streak', dict()).get('streak',0) >= 2)}</div><div class="label">连板</div></div>
  </div>

  <table>
    <thead>
      <tr>
        <th>#</th><th>代码</th><th>名称</th><th>现价</th><th>涨跌</th>
        <th>量比</th><th>主力(万)</th><th>逻辑一</th><th>L1分</th>
        <th>逻辑二</th><th>L2分</th><th>增强+游资信号</th><th>涨停质量</th><th>概念</th>
      </tr>
    </thead>
    <tbody>{rows_html}
    </tbody>
  </table>

  <div class="section-title">🏆 TOP 3 详情</div>
  <div class="cards">{cards_html}
  </div>

  <div style="text-align:center;padding:24px 0;font-size:12px;color:#484f58;">
    Generated by 双逻辑股票筛选系统 v3.1 · min_score={self.min_score} (effective={effective_min})
  </div>
</body>
</html>"""

        with open(self.html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(colored(f"\n  🌐 HTML报告已生成: {self.html_path}", C.GREEN))


# ====================================================================== #
#  入口
# ====================================================================== #

def main():
    parser = argparse.ArgumentParser(description="双逻辑股票筛选系统 v3.1")
    parser.add_argument("--top", type=int, default=20, help="输出前N只（默认20）")
    parser.add_argument("--min-score", type=int, default=65, help="最低分数阈值（默认65）")
    parser.add_argument("--export", type=str, default=None, help="导出CSV路径")
    parser.add_argument("--html", type=str, default="stock_result.html", help="导出HTML路径")
    args = parser.parse_args()

    scanner = StockScanner(
        top_n=args.top,
        min_score=args.min_score,
        export_path=args.export,
        html_path=args.html,
    )
    scanner.run()


if __name__ == "__main__":
    main()
