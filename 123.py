"""
双逻辑股票筛选系统 - 顶级操盘手版 v4.0
数据源：腾讯行情API + baostock

v3.0 模块（保留）：
  ① 市场环境自适应    ② 相对强度分析      ③ 量价语义识别
  ④ 连板股特殊逻辑    ⑤ 均线收敛/发散度   ⑥ 假突破惩罚
  ⑦ 板块热度轮动      ⑧ 多周期共振        ⑨ 情绪周期
  ⑩ 增强版综合评分

v4.0 新增（8大游资核心模块）：
  ⑪ 二板定龙头（赵老哥）：首板标记/二板确认龙头/三板+妖股逻辑
  ⑫ 分歧转一致检测（方新侠/赵老哥）：爆量分歧=买点，缩量加速=卖点
  ⑬ 筹码换手分析（游资共识）：连板期换手率趋势、筹码锁定度
  ⑭ 跟风股惩罚（章盟主/赵老哥）：同板块跟风股大幅扣分，只做龙头
  ⑮ MACD金叉细化：水上/水下金叉、二次金叉区分
  ⑯ 动态热门概念（赵老哥）：按近N日涨幅自动识别当前热点
  ⑰ 仓位管理建议（退学炒股/六一中路）：根据情绪周期给出仓位/止损
  ⑱ 止损止盈信号（涅盘重升）：输出建议止损价/止盈条件/持有天数

游资心法融合：
  - 章盟主：只做龙头，只做主升，只做惯性
  - 赵老哥：二板定龙头；缩量加速是卖点，爆量分歧是买点
  - 炒股养家：买在无人问津时，卖在人声鼎沸处
  - 方新侠：龙头死于加速，分歧才能走得更远
  - 退学炒股：会空仓的是祖师爷
  - 六一中路：控制回撤永远是最核心的
  - 涅盘重升：错误的交易，第二天集合竞价割

使用方式：
    python stock_scanner.py                    # 默认筛选，输出前20
    python stock_scanner.py --top 50           # 输出前50
    python stock_scanner.py --min-score 65     # 最低分数阈值
    python stock_scanner.py --export result.csv  # 导出CSV

依赖安装：
    pip install pandas numpy baostock requests
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
#  v4.0 新增模块 ⑪：二板定龙头（赵老哥核心逻辑）
# ====================================================================== #

class BoardConfirmation:
    """二板定龙头 — 赵老哥：一板能看出来个毛"""

    @staticmethod
    def evaluate(s: StockData, kline_df: pd.DataFrame,
                 zt_codes: set, code_concepts: dict,
                 emotion_stage: str) -> dict:
        if kline_df is None or kline_df.empty or len(kline_df) < 5:
            return {"streak": 0, "board_score": 0, "board_note": "无连板",
                    "is_leader_candidate": False}

        threshold = s.limit_up_threshold
        close = kline_df["close"].values.astype(float)
        volume = kline_df["volume"].values.astype(float)
        pct = np.diff(close) / close[:-1] * 100

        # 计算连板数
        streak = 0
        for p in reversed(pct):
            if p >= threshold:
                streak += 1
            else:
                break

        if streak == 0:
            return {"streak": 0, "board_score": 0, "board_note": "无连板",
                    "is_leader_candidate": False}

        # 统计同板块内有多少只连板股
        same_sector_boards = 0
        if code_concepts:
            my_sectors = set(code_concepts.get(s.code, []))
            for code in zt_codes:
                if code == s.code:
                    continue
                other_sectors = set(code_concepts.get(code, []))
                if my_sectors & other_sectors:
                    same_sector_boards += 1

        # 封板力度：尾盘是否封住（收盘价接近最高价）
        last_close = close[-1]
        last_high = kline_df["high"].values.astype(float)[-1]
        seal_ratio = last_close / (last_high + 1e-10)  # 越接近1封板越强

        # 二板时的换手量
        if len(volume) >= 2:
            board_vol_ratio = volume[-1] / (np.mean(volume[-6:-1]) + 1e-10)
        else:
            board_vol_ratio = 1.0

        is_leader = False
        if streak == 1:
            # 首板：仅标记，低分
            board_score = 3
            board_note = "首板"

        elif streak == 2:
            # 二板：赵老哥核心 — 确认龙头
            if same_sector_boards == 0:
                # 板块唯一二板 → 强龙头候选
                board_score = 18
                board_note = "⭐板块唯一二板·龙头确认"
                is_leader = True
            elif same_sector_boards <= 2:
                board_score = 13
                board_note = "📈二板·板块少数"
                is_leader = True
            elif same_sector_boards <= 5:
                board_score = 8
                board_note = "二板·板块内较多"
            else:
                board_score = 4
                board_note = "二板·板块过热"

            # 封板力度加成
            if seal_ratio > 0.995:
                board_score += 3
            elif seal_ratio < 0.97:
                board_score -= 3
                board_note += "(烂板)"

        elif streak == 3:
            # 三板：需要情绪配合
            if "回暖" in emotion_stage or "高潮" in emotion_stage:
                board_score = 15
                board_note = "🔥三板龙头·情绪配合"
                is_leader = True
            else:
                board_score = 8
                board_note = "⚠️三板·情绪不配合"
            if same_sector_boards == 0:
                board_score += 5
                is_leader = True

        elif streak >= 4:
            # 四板+：妖股逻辑
            if "回暖" in emotion_stage or "高潮" in emotion_stage:
                board_score = 20
                board_note = f"🔥{streak}板妖股"
                is_leader = True
            else:
                board_score = 10
                board_note = f"⚠️{streak}板妖股·高风险"

            # 逐板缩量 = 筹码锁定 = 更强
            if len(volume) >= streak + 1:
                board_vols = volume[-streak:]
                vol_trend = np.polyfit(range(len(board_vols)), board_vols, 1)[0]
                if vol_trend < 0:
                    board_score += 5
                    board_note += "·缩量锁仓"
        else:
            board_score = 0
            board_note = ""

        return {
            "streak": streak,
            "board_score": board_score,
            "board_note": board_note,
            "is_leader_candidate": is_leader,
            "seal_ratio": round(seal_ratio, 4),
            "same_sector_boards": same_sector_boards,
        }


# ====================================================================== #
#  v4.0 新增模块 ⑫：分歧转一致检测（方新侠/赵老哥）
# ====================================================================== #

class DivergenceConsensus:
    """分歧转一致 / 一致转分歧 — 游资最核心的买卖判断"""

    @staticmethod
    def detect(s: StockData, kline_df: pd.DataFrame) -> dict:
        if kline_df is None or kline_df.empty or len(kline_df) < 10:
            return {"pattern": "数据不足", "dc_score": 0, "signal": ""}

        close = kline_df["close"].values.astype(float)
        high = kline_df["high"].values.astype(float)
        low = kline_df["low"].values.astype(float)
        volume = kline_df["volume"].values.astype(float)

        vol_5_avg = np.mean(volume[-6:-1])

        # ---- 昨日特征 ----
        y_amp = (high[-2] - low[-2]) / (close[-2] + 1e-10) * 100
        y_vol_ratio = volume[-2] / (vol_5_avg + 1e-10)
        y_body_pos = (close[-2] - low[-2]) / (high[-2] - low[-2] + 1e-10)
        y_is_limit = close[-2] >= s.limit_up_threshold * close[-3] / 100 if len(close) > 2 else False

        # 昨日是否为分歧日：放量 + 大振幅 + 收盘偏低（或长上影线）
        is_y_divergent = (
            y_vol_ratio > 1.5 and y_amp > 4 and (y_body_pos < 0.5 or y_amp > 7)
        )

        # ---- 今日特征 ----
        t_vol_ratio = volume[-1] / (vol_5_avg + 1e-10)
        t_amp = (high[-1] - low[-1]) / (close[-1] + 1e-10) * 100
        t_is_limit = close[-1] >= s.limit_up_threshold * close[-2] / 100
        t_is_up = close[-1] > close[-2]

        # ---- 前日特征（用于检测连续缩量加速）----
        prev_shrink = False
        if len(volume) >= 3:
            prev_vol_ratio = volume[-2] / (volume[-3] + 1e-10)
            prev_shrink = prev_vol_ratio < 0.7 and close[-2] > close[-3]

        # ====== 核心判断 ======

        # 1. 分歧转一致 → 买点（赵老哥：爆量分歧是买点）
        if is_y_divergent and t_is_limit and t_vol_ratio < 0.85:
            return {
                "pattern": "🔥分歧转一致·强买点",
                "dc_score": 18,
                "signal": "BUY",
                "detail": f"昨日放量分歧(振幅{y_amp:.1f}%)→今日缩量涨停",
            }

        # 1b. 分歧后今日强势反包（未涨停但大涨+缩量）
        if is_y_divergent and t_is_up and t_vol_ratio < 0.9 and close[-1] > close[-2] * 1.05:
            return {
                "pattern": "📈分歧后反包·买点",
                "dc_score": 12,
                "signal": "BUY",
                "detail": f"昨日分歧→今日缩量反包涨{(close[-1]/close[-2]-1)*100:.1f}%",
            }

        # 2. 一致转分歧 → 卖点（方新侠：龙头死于加速）
        if t_vol_ratio > 2.0 and t_amp > 6 and close[-1] < high[-1] * 0.97:
            return {
                "pattern": "⚠️一致转分歧·卖点",
                "dc_score": -15,
                "signal": "SELL",
                "detail": f"放量{t_vol_ratio:.1f}倍+大振幅{t_amp:.1f}%+冲高回落",
            }

        # 3. 缩量加速 → 卖点（赵老哥：缩量加速是卖点）
        if prev_shrink and t_vol_ratio < 0.6 and t_is_up:
            return {
                "pattern": "⚠️连续缩量加速·卖点",
                "dc_score": -10,
                "signal": "SELL",
                "detail": "连续缩量上涨，随时可能见顶",
            }

        # 4. 放量滞涨 → 危险
        if t_vol_ratio > 2.5 and not t_is_up and t_amp > 5:
            return {
                "pattern": "❌放量滞涨·危险",
                "dc_score": -12,
                "signal": "RISK",
                "detail": f"放量{t_vol_ratio:.1f}倍但收跌",
            }

        # 5. 温和放量上涨 → 正常健康
        if 1.2 < t_vol_ratio < 2.0 and t_is_up:
            return {
                "pattern": "✅温和放量涨·健康",
                "dc_score": 5,
                "signal": "",
                "detail": "",
            }

        # 6. 缩量回调到位 → 潜在买点
        if t_vol_ratio < 0.5 and close[-1] > close[-3] and t_amp < 3:
            return {
                "pattern": "✅缩量企稳·蓄势",
                "dc_score": 3,
                "signal": "",
                "detail": "",
            }

        return {"pattern": "中性", "dc_score": 0, "signal": "", "detail": ""}


# ====================================================================== #
#  v4.0 新增模块 ⑬：筹码换手分析（游资共识）
# ====================================================================== #

class ChipTurnover:
    """筹码换手分析 — 游资：龙头股筹码供不应求"""

    @staticmethod
    def analyze(s: StockData, kline_df: pd.DataFrame) -> dict:
        if kline_df is None or kline_df.empty or len(kline_df) < 10:
            return {"chip_score": 0, "chip_note": "数据不足", "chip_trend": 0}

        volume = kline_df["volume"].values.astype(float)
        close = kline_df["close"].values.astype(float)

        # 近5日换手量趋势
        recent_5 = volume[-5:]
        if len(recent_5) >= 3:
            trend = np.polyfit(range(len(recent_5)), recent_5, 1)[0]
            trend_normalized = trend / (np.mean(recent_5) + 1e-10)
        else:
            trend_normalized = 0

        # 量能萎缩度（今日 vs 20日均量）
        vol_20_avg = np.mean(volume[-20:]) if len(volume) >= 20 else np.mean(volume)
        shrink_ratio = volume[-1] / (vol_20_avg + 1e-10)

        # 近3日量能标准差（越小越稳定）
        if len(volume) >= 3:
            vol_cv = np.std(volume[-3:]) / (np.mean(volume[-3:]) + 1e-10)
        else:
            vol_cv = 0

        chip_score = 0
        chip_note = ""

        # 筹码锁定信号：量能递减 + 价格不跌
        if trend_normalized < -0.15 and close[-1] >= close[-5] * 0.98:
            chip_score = 10
            chip_note = "✅筹码锁定·量缩价稳"
        elif trend_normalized < -0.08 and close[-1] > close[-3]:
            chip_score = 6
            chip_note = "✅量能温和萎缩·筹码集中"
        elif trend_normalized > 0.2 and close[-1] < close[-2]:
            chip_score = -8
            chip_note = "⚠️放量下跌·筹码松动"
        elif trend_normalized > 0.15:
            chip_score = -3
            chip_note = "⚠️量能递增·分歧加大"
        elif shrink_ratio < 0.4:
            chip_score = 4
            chip_note = "极度缩量·蓄势待发"
        else:
            chip_score = 0
            chip_note = "筹码状态中性"

        return {
            "chip_score": chip_score,
            "chip_note": chip_note,
            "chip_trend": round(trend_normalized, 4),
            "shrink_ratio": round(shrink_ratio, 3),
            "vol_cv": round(vol_cv, 3),
        }


# ====================================================================== #
#  v4.0 新增模块 ⑭：跟风股惩罚（章盟主/赵老哥）
# ====================================================================== #

class FollowerPenalty:
    """跟风股惩罚 — 章盟主：只做龙头；赵老哥：跟风盘不做不看不研究"""

    @staticmethod
    def evaluate(s: StockData, zt_codes: set, code_concepts: dict,
                 board_info: dict) -> dict:
        """
        判断是否为跟风股并给予惩罚
        """
        my_sectors = code_concepts.get(s.code, [])
        if not my_sectors:
            return {"is_follower": False, "follower_penalty": 0, "follower_note": ""}

        # 统计同板块涨停数
        sector_limit_up_count = 0
        sector_leader_code = None
        for code in zt_codes:
            if code == s.code:
                continue
            other_sectors = code_concepts.get(code, [])
            if set(my_sectors) & set(other_sectors):
                sector_limit_up_count += 1
                if sector_leader_code is None:
                    sector_leader_code = code

        # 自身不是涨停 + 同板块有涨停 = 跟风嫌疑
        is_limit_up = s.pct_change >= s.limit_up_threshold

        if is_limit_up:
            # 自身涨停 → 不算跟风
            return {"is_follower": False, "follower_penalty": 0, "follower_note": ""}

        if sector_limit_up_count == 0:
            return {"is_follower": False, "follower_penalty": 0, "follower_note": ""}

        # 同板块有涨停，自身不是 → 跟风嫌疑
        # 但如果自身是二板候选，从轻处理
        if board_info.get("is_leader_candidate", False):
            return {
                "is_follower": False,
                "follower_penalty": -2,
                "follower_note": "板块有涨停但自身为龙头候选",
            }

        # 涨幅远低于涨停 → 大概率跟风
        pct_gap = s.limit_up_threshold - s.pct_change
        if pct_gap > 5:
            penalty = -12
            note = f"❌跟风股·板块有{sector_limit_up_count}只涨停"
        elif pct_gap > 3:
            penalty = -8
            note = f"⚠️疑似跟风·涨幅落后板块龙头{pct_gap:.1f}%"
        elif pct_gap > 1:
            penalty = -4
            note = f"跟风嫌疑·距涨停仅差{pct_gap:.1f}%"
        else:
            penalty = -2
            note = "接近涨停但未封住"

        return {
            "is_follower": True,
            "follower_penalty": penalty,
            "follower_note": note,
            "sector_limit_up_count": sector_limit_up_count,
        }


# ====================================================================== #
#  v4.0 新增模块 ⑮：MACD金叉细化
# ====================================================================== #

class MACDRefined:
    """MACD细化 — 水上/水下金叉、二次金叉"""

    @staticmethod
    def evaluate(s: StockData, kline_df: pd.DataFrame) -> dict:
        if kline_df is None or kline_df.empty or len(kline_df) < 30:
            return {"macd_refined_score": 0, "macd_refined_note": ""}

        close = kline_df["close"].values.astype(float)

        ema12 = pd.Series(close).ewm(span=12).mean()
        ema26 = pd.Series(close).ewm(span=26).mean()
        dif = (ema12 - ema26).values
        dea = pd.Series(dif).ewm(span=9).mean().values
        macd_bar = 2 * (dif - dea)

        # 当前状态
        dif_now = dif[-1]
        dea_now = dea[-1]
        bar_now = macd_bar[-1]

        # 3日前状态（用于判断金叉）
        dif_3 = dif[-4] if len(dif) > 3 else dif[0]
        dea_3 = dea[-4] if len(dea) > 3 else dea[0]

        # 水上/水下判断
        above_zero = dif_now > 0 and dea_now > 0

        # 金叉判断（当前DIF>DEA 且 3日前DIF<DEA）
        is_golden_cross = dif_now > dea_now and dif_3 < dea_3

        # 二次金叉：MACD柱翻红后再次翻红
        recent_bars = macd_bar[-10:]
        red_count = 0
        for i in range(1, len(recent_bars)):
            if recent_bars[i] > 0 and recent_bars[i - 1] <= 0:
                red_count += 1
        is_second_cross = red_count >= 2

        score = 0
        note = ""

        if is_golden_cross and above_zero:
            score = 12
            note = "🔥水上金叉·强势信号"
        elif is_second_cross and above_zero:
            score = 10
            note = "🔥二次金叉·强势确认"
        elif is_golden_cross and not above_zero:
            score = 3
            note = "水下金叉·弱反弹"
        elif dif_now > dea_now and bar_now > 0:
            if bar_now > macd_bar[-2] if len(macd_bar) > 1 else True:
                score = 5
                note = "MACD柱放大·多头增强"
            else:
                score = 2
                note = "MACD多头"
        elif dif_now < dea_now and bar_now < 0:
            score = -3
            note = "MACD空头"
        else:
            score = 0
            note = ""

        return {
            "macd_refined_score": score,
            "macd_refined_note": note,
            "is_golden_cross": is_golden_cross,
            "above_zero": above_zero,
            "is_second_cross": is_second_cross,
        }


# ====================================================================== #
#  v4.0 新增模块 ⑯：动态热门概念
# ====================================================================== #

class DynamicHotConcepts:
    """动态热门概念 — 按近N日板块涨幅自动识别，替代硬编码"""

    @staticmethod
    def compute(stocks: list, code_concepts: dict) -> dict:
        """计算每个概念板块的综合热度（涨幅+涨停数+成交额）"""
        concept_stats = {}

        for s in stocks:
            for concept in code_concepts.get(s.code, []):
                if concept not in concept_stats:
                    concept_stats[concept] = {
                        "count": 0, "total_pct": 0, "limit_ups": 0,
                        "total_amount": 0, "total_flow": 0,
                    }
                cs = concept_stats[concept]
                cs["count"] += 1
                cs["total_pct"] += s.pct_change
                cs["total_amount"] += s.amount
                cs["total_flow"] += s.main_net_inflow
                if s.pct_change >= s.limit_up_threshold:
                    cs["limit_ups"] += 1

        concept_heat = {}
        for name, cs in concept_stats.items():
            if cs["count"] < 3:
                continue
            avg_pct = cs["total_pct"] / cs["count"]
            # 综合热度 = 涨停数×15 + 平均涨幅×3 + 成交额权重 + 资金流入权重
            heat = (
                cs["limit_ups"] * 15
                + avg_pct * 3
                + np.log10(cs["total_amount"] + 1) * 0.5
                + cs["total_flow"] / 50000
            )
            concept_heat[name] = {
                "heat": round(heat, 1),
                "avg_pct": round(avg_pct, 2),
                "limit_ups": cs["limit_ups"],
                "count": cs["count"],
            }

        # 排序取Top作为当前热门
        sorted_concepts = sorted(
            concept_heat.items(), key=lambda x: x[1]["heat"], reverse=True
        )

        # 热门概念集合（取前10%或热度>阈值的）
        hot_set = set()
        threshold_heat = 20
        for name, info in sorted_concepts:
            if info["heat"] > threshold_heat or len(hot_set) < max(5, len(sorted_concepts) // 10):
                hot_set.add(name)
            else:
                break

        return {
            "hot_concepts": hot_set,
            "concept_heat": concept_heat,
            "top_concepts": sorted_concepts[:10],
        }

    @staticmethod
    def get_concept_score(s: StockData, code_concepts: dict,
                          hot_concepts: set, concept_heat: dict) -> tuple:
        """给个股的概念加分"""
        score = 0
        hot_count = 0
        best_concept = ""

        for concept in code_concepts.get(s.code, []):
            if concept in hot_concepts:
                hot_count += 1
                heat = concept_heat.get(concept, {}).get("heat", 0)
                if heat > score:
                    score = heat
                    best_concept = concept

        if hot_count >= 3:
            return 10, f"🔥{hot_count}热门概念叠加({best_concept})"
        elif hot_count >= 2:
            return 7, f"📈双热门概念({best_concept})"
        elif hot_count >= 1:
            return 4, f"热门概念({best_concept})"
        elif len(s.concepts) >= 2:
            return 2, "普通概念"
        return 0, ""


# ====================================================================== #
#  v4.0 新增模块 ⑰：仓位管理（退学炒股/六一中路）
# ====================================================================== #

class PositionManager:
    """仓位管理 — 退学炒股：会空仓的是祖师爷；六一中路：控制回撤是核心"""

    POSITION_MAP = {
        "❄️冰点期": {"max_pos": 0.20, "stop_loss": -0.03, "max_hold": 1,
                     "advice": "极度谨慎，≤2成仓，快进快出"},
        "📉退潮期": {"max_pos": 0.30, "stop_loss": -0.05, "max_hold": 2,
                     "advice": "控制仓位，≤3成，严格止损"},
        "⚖️震荡期": {"max_pos": 0.50, "stop_loss": -0.05, "max_hold": 3,
                     "advice": "精选个股，≤5成仓"},
        "📈回暖期": {"max_pos": 0.70, "stop_loss": -0.07, "max_hold": 5,
                     "advice": "积极做多，可达7成"},
        "🔥高潮期": {"max_pos": 0.50, "stop_loss": -0.07, "max_hold": 3,
                     "advice": "逐步减仓，勿追高"},
    }

    @staticmethod
    def get_advice(emotion_stage: str) -> dict:
        for key, val in PositionManager.POSITION_MAP.items():
            if key in emotion_stage:
                return val
        return {"max_pos": 0.40, "stop_loss": -0.05, "max_hold": 3,
                "advice": "控制仓位，精选个股"}


# ====================================================================== #
#  v4.0 新增模块 ⑱：止损止盈信号（涅盘重升）
# ====================================================================== #

class StopSignal:
    """止损止盈信号 — 涅盘重升：错误的交易第二天集合竞价割"""

    @staticmethod
    def compute(s: StockData, kline_df: pd.DataFrame,
                position_advice: dict) -> dict:
        if kline_df is None or kline_df.empty or len(kline_df) < 5:
            return {"stop_loss_price": 0, "take_profit_note": "",
                    "max_hold_days": 3, "signals": []}

        close = kline_df["close"].values.astype(float)
        high = kline_df["high"].values.astype(float)

        # 止损价：取 MA5 和 position_advice 中的止损比例，取更紧的
        ma5 = np.mean(close[-5:])
        stop_loss_pct = position_advice.get("stop_loss", -0.05)
        stop_loss_price = max(
            s.close * (1 + stop_loss_pct),  # 百分比止损
            ma5 * 0.97,                       # 跌破5日均线
        )

        signals = []

        # 止盈条件
        # 1. 连板后首次放量阴线
        recent_bars = kline_df.tail(5)
        close_arr = recent_bars["close"].values.astype(float)
        volume_arr = recent_bars["volume"].values.astype(float)
        vol_avg = np.mean(volume_arr)

        if len(close_arr) >= 2:
            is_yin = close_arr[-1] < close_arr[-2]
            is_heavy_vol = volume_arr[-1] > 1.8 * vol_avg
            if is_yin and is_heavy_vol:
                signals.append("⚠️放量阴线·考虑止盈")

        # 2. 高位长上影线
        if len(high) >= 2:
            upper_shadow = (high[-1] - max(close[-1], float(kline_df["open"].values[-1]))) / close[-1] * 100
            if upper_shadow > 4:
                signals.append(f"⚠️长上影线{upper_shadow:.1f}%·压力大")

        # 3. 涨幅已大（近5日涨超20%）
        if len(close) >= 5:
            gain_5d = (close[-1] / close[-5] - 1) * 100
            if gain_5d > 25:
                signals.append(f"⚠️5日涨{gain_5d:.0f}%·高位风险")
            elif gain_5d > 15:
                signals.append(f"📈5日涨{gain_5d:.0f}%·注意止盈")

        take_profit_note = " | ".join(signals) if signals else "无止盈信号"

        return {
            "stop_loss_price": round(stop_loss_price, 2),
            "take_profit_note": take_profit_note,
            "max_hold_days": position_advice.get("max_hold", 3),
            "signals": signals,
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
                         aggression: float,
                         zt_codes: set = None,
                         emotion_stage: str = "",
                         hot_concepts: set = None,
                         concept_heat: dict = None) -> tuple:
        """
        v4.0: 增强评分 — 融合18大模块
        返回 (enhanced_bonus, enhanced_detail_dict)
        """
        bonus = 0
        ed = {}
        signals_extra = []  # 额外信号（止损止盈等）

        if zt_codes is None:
            zt_codes = set()
        if hot_concepts is None:
            hot_concepts = set()
        if concept_heat is None:
            concept_heat = {}

        # ====== v3.0 模块（保留） ======

        # ② 相对强度
        rs = calc_relative_strength(s, market_median, sector_avg)
        bonus += rs["rs_total"]
        if rs["rs_total"] != 0:
            ed["相对强度"] = rs["rs_total"]

        # ③ 量价语义
        vp = classify_volume_price(s, kline_df)
        bonus += vp["vp_score"]
        if vp["vp_score"] != 0:
            ed[f"量价:{vp['pattern']}"] = vp["vp_score"]

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

        # ====== v4.0 新增模块 ======

        # ⑪ 二板定龙头（赵老哥）
        board = BoardConfirmation.evaluate(s, kline_df, zt_codes, code_concepts, emotion_stage)
        bonus += board["board_score"]
        if board["board_score"] != 0:
            ed[board["board_note"]] = board["board_score"]

        # ⑫ 分歧转一致（方新侠/赵老哥）
        dc = DivergenceConsensus.detect(s, kline_df)
        bonus += dc["dc_score"]
        if dc["dc_score"] != 0:
            ed[dc["pattern"]] = dc["dc_score"]
        if dc.get("signal") == "BUY":
            signals_extra.append(dc["pattern"])
        elif dc.get("signal") in ("SELL", "RISK"):
            signals_extra.append(dc["pattern"])

        # ⑬ 筹码换手（游资共识）
        chip = ChipTurnover.analyze(s, kline_df)
        bonus += chip["chip_score"]
        if chip["chip_score"] != 0:
            ed[chip["chip_note"]] = chip["chip_score"]

        # ⑭ 跟风股惩罚（章盟主）
        follower = FollowerPenalty.evaluate(s, zt_codes, code_concepts, board)
        bonus += follower["follower_penalty"]
        if follower["follower_penalty"] != 0:
            ed[follower["follower_note"]] = follower["follower_penalty"]

        # ⑮ MACD金叉细化
        macd_r = MACDRefined.evaluate(s, kline_df)
        bonus += macd_r["macd_refined_score"]
        if macd_r["macd_refined_score"] != 0:
            ed[macd_r["macd_refined_note"]] = macd_r["macd_refined_score"]

        # ⑯ 动态热门概念（替代硬编码）
        concept_score, concept_note = DynamicHotConcepts.get_concept_score(
            s, code_concepts, hot_concepts, concept_heat
        )
        bonus += concept_score
        if concept_score != 0:
            ed[concept_note] = concept_score

        # ⑨ 情绪周期调整（aggression: 0.3~0.9）
        if bonus != 0:
            bonus = int(bonus * aggression)

        return bonus, ed, signals_extra


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
    """股票筛选器 v4.0"""

    def __init__(self, top_n=20, min_score=65, export_path=None, html_path=None):
        self.fetcher = DataFetcher()
        self.logic1 = LogicOne()
        self.logic2 = LogicTwo()
        self.top_n = top_n
        self.min_score = min_score
        self.export_path = export_path
        self.html_path = html_path or "stock_result.html"
        self.kline_cache = {}  # v3.0: K线缓存

    def run(self):
        print(colored("\n" + "=" * 70, C.CYAN))
        print(colored("  📊 双逻辑股票筛选系统 v4.0（顶级操盘手版）", C.BOLD))
        print(colored(f"  运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", C.DIM))
        print(colored("  数据源：腾讯行情 + baostock", C.DIM))
        print(colored("  增强：市场环境/相对强度/量价语义/连板/收敛/假突破/板块轮动/多周期/情绪", C.DIM))
        print(colored("  v4.0：二板定龙头/分歧一致/筹码换手/跟风惩罚/MACD细化/动态概念/仓位管理/止损止盈", C.DIM))
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

        # v4.0: 动态热门概念
        print(colored("  📡 计算动态热门概念...", C.YELLOW))
        dynamic_concepts = DynamicHotConcepts.compute(stocks, code_concepts)
        hot_concepts_set = dynamic_concepts["hot_concepts"]
        if dynamic_concepts["top_concepts"]:
            print(f"  🔥 当前动态热门: {', '.join(c[0] for c in dynamic_concepts['top_concepts'][:5])}")

        # v4.0: 仓位管理建议
        pos_advice = PositionManager.get_advice(emotion["stage"])
        print(colored(f"  💼 仓位建议: {pos_advice['advice']}", C.YELLOW))
        print(f"     最大仓位: {pos_advice['max_pos']*100:.0f}%  "
              f"止损线: {pos_advice['stop_loss']*100:.0f}%  "
              f"最长持有: {pos_advice['max_hold']}天")

        results = []
        for s in candidates:
            r1 = self.logic1.analyze(s)
            r2 = self.logic2.analyze(s)

            # v4.0: 增强评分（传入新模块参数）
            kline_df = self.kline_cache.get(s.code)
            sector_avg = get_sector_avg(s, code_concepts, sector_avg_pcts)
            enhanced_bonus, enhanced_detail, extra_signals = self.logic1.compute_enhanced(
                s, kline_df, market["median_pct"], sector_avg,
                code_concepts, sector_heat_result["all"],
                emotion["aggression"],
                zt_codes=zt_codes,
                emotion_stage=emotion["stage"],
                hot_concepts=hot_concepts_set,
                concept_heat=dynamic_concepts["concept_heat"],
            )

            r1.enhanced_score = r1.raw_score + enhanced_bonus
            r1.enhanced_detail = enhanced_detail
            r1.final_score = min(max(r1.enhanced_score, r1.final_score), 100)
            r1.final_score = max(r1.final_score, 0)

            # v4.0: 存储额外信号
            s.enhanced = {
                "rs": calc_relative_strength(s, market["median_pct"], sector_avg),
                "vp": classify_volume_price(s, kline_df) if kline_df is not None else {},
                "streak": detect_consecutive_limit_up(s, kline_df) if kline_df is not None else {},
                "cv": compute_ma_convergence(s),
                "fb": detect_false_breakout(s, kline_df) if kline_df is not None else {},
                "mtf": multi_timeframe_confirm(s, kline_df) if kline_df is not None else {},
                # v4.0 新增
                "board": BoardConfirmation.evaluate(s, kline_df, zt_codes, code_concepts, emotion["stage"]) if kline_df is not None else {},
                "dc": DivergenceConsensus.detect(s, kline_df) if kline_df is not None else {},
                "chip": ChipTurnover.analyze(s, kline_df) if kline_df is not None else {},
                "follower": FollowerPenalty.evaluate(s, zt_codes, code_concepts,
                    BoardConfirmation.evaluate(s, kline_df, zt_codes, code_concepts, emotion["stage"])) if kline_df is not None else {},
                "macd_r": MACDRefined.evaluate(s, kline_df) if kline_df is not None else {},
                "extra_signals": extra_signals,
                # v4.0 止损止盈
                "stop": StopSignal.compute(s, kline_df, pos_advice) if kline_df is not None else {},
            }

            results.append((s, r1, r2))

        self._print_results(results, market, emotion, effective_min, pos_advice)

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

    def _print_results(self, results: list, market: dict, emotion: dict, effective_min: float,
                       pos_advice: dict = None):
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
              f"{'逻辑二':^12} {'L2分':>6} {'主力(万)':>10}")
        print("  " + "-" * 85)

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

            print(f"  {i:<4} {s.code:<8} {name:<10} "
                  f"{colored(r1.role, r1c):^22} {r1.final_score:>6.1f} "
                  f"{colored(enhanced_str, enhanced_c):>5} "
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
                # v3.0 信号
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
                # v4.0 新增信号
                if eh.get("board", {}).get("board_note"):
                    signals.append(eh["board"]["board_note"])
                if eh.get("dc", {}).get("pattern") and eh["dc"]["pattern"] != "中性":
                    signals.append(eh["dc"]["pattern"])
                if eh.get("chip", {}).get("chip_note") and "中性" not in eh["chip"]["chip_note"]:
                    signals.append(eh["chip"]["chip_note"])
                if eh.get("follower", {}).get("follower_note"):
                    signals.append(eh["follower"]["follower_note"])
                if eh.get("macd_r", {}).get("macd_refined_note"):
                    signals.append(eh["macd_r"]["macd_refined_note"])
                if eh.get("extra_signals"):
                    signals.extend(eh["extra_signals"])
                if signals:
                    print(f"  信号: {' | '.join(signals)}")

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

            # v4.0: 止损止盈建议
            stop = eh.get("stop", {})
            if stop.get("stop_loss_price", 0) > 0:
                print(f"  ── 风控 ──")
                print(f"    止损价: {stop['stop_loss_price']:.2f}  "
                      f"最长持有: {stop.get('max_hold_days', 3)}天")
                if stop.get("take_profit_note"):
                    print(f"    止盈: {stop['take_profit_note']}")

        # v4.0: 输出仓位管理建议
        if pos_advice:
            print(colored("\n" + "-" * 70, C.DIM))
            print(colored("  💼 仓位管理建议（退学炒股/六一中路）", C.BOLD))
            print(colored("-" * 70, C.DIM))
            print(f"  情绪周期: {emotion['stage']}")
            print(f"  建议: {pos_advice['advice']}")
            print(f"  最大仓位: {pos_advice['max_pos']*100:.0f}%  "
                  f"止损线: {pos_advice['stop_loss']*100:.0f}%  "
                  f"最长持有: {pos_advice['max_hold']}天")

    def _export_csv(self, results: list):
        rows = []
        for s, r1, r2 in results:
            eh = s.enhanced
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
                "逻辑二角色": r2.role,
                "逻辑二总分": r2.total_score,
                "技术面": r2.tech_score,
                "基本面": r2.fundamental_score,
                "资金面": r2.capital_score,
                "逻辑面": r2.logic_score,
                # v3.0 字段
                "连板": eh.get("streak", {}).get("streak", 0),
                "量价语义": eh.get("vp", {}).get("pattern", ""),
                "均线收敛": eh.get("cv", {}).get("note", ""),
                "假突破": eh.get("fb", {}).get("note", ""),
                "多周期": eh.get("mtf", {}).get("mtf_note", ""),
                "超额收益": eh.get("rs", {}).get("rs_total", 0),
                # v4.0 新增字段
                "二板龙头": eh.get("board", {}).get("board_note", ""),
                "分歧一致": eh.get("dc", {}).get("pattern", ""),
                "DC信号": eh.get("dc", {}).get("signal", ""),
                "筹码状态": eh.get("chip", {}).get("chip_note", ""),
                "跟风标记": "是" if eh.get("follower", {}).get("is_follower") else "",
                "跟风惩罚": eh.get("follower", {}).get("follower_penalty", 0),
                "MACD细化": eh.get("macd_r", {}).get("macd_refined_note", ""),
                "止损价": eh.get("stop", {}).get("stop_loss_price", 0),
                "止盈信号": eh.get("stop", {}).get("take_profit_note", ""),
                "最长持有天": eh.get("stop", {}).get("max_hold_days", 3),
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
            # v4.0 badges
            if eh.get("board", {}).get("board_score", 0) >= 13:
                enhanced_badges += f'<span class="badge hot">{eh["board"]["board_note"]}</span>'
            if eh.get("dc", {}).get("signal") == "BUY":
                enhanced_badges += f'<span class="badge good">{eh["dc"]["pattern"]}</span>'
            elif eh.get("dc", {}).get("signal") in ("SELL", "RISK"):
                enhanced_badges += f'<span class="badge warn">{eh["dc"]["pattern"]}</span>'
            if eh.get("chip", {}).get("chip_score", 0) >= 6:
                enhanced_badges += f'<span class="badge good">{eh["chip"]["chip_note"]}</span>'
            if eh.get("follower", {}).get("is_follower"):
                enhanced_badges += f'<span class="badge warn">跟风股</span>'
            if eh.get("macd_r", {}).get("macd_refined_score", 0) >= 10:
                enhanced_badges += f'<span class="badge hot">{eh["macd_r"]["macd_refined_note"]}</span>'

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
              <td class="badges">{enhanced_badges}</td>
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
                               ("fb", "假突破"), ("mtf", "多周期"),
                               ("board", "二板"), ("dc", "分歧"), ("chip", "筹码"),
                               ("macd_r", "MACD")]:
                data = eh.get(key, {})
                if data:
                    note = data.get("note", data.get("pattern", data.get("board_note", data.get("chip_note", data.get("macd_refined_note", "")))))
                    if note:
                        signals_html += f'<span class="signal">{note}</span>'
            # v4.0: 跟风标记
            if eh.get("follower", {}).get("is_follower"):
                signals_html += f'<span class="signal warn">跟风股</span>'
            # v4.0: 止损止盈
            stop = eh.get("stop", {})
            if stop.get("stop_loss_price", 0) > 0:
                signals_html += f'<span class="signal">止损:{stop["stop_loss_price"]:.2f}</span>'

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
              </div>
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股票筛选结果 v3.0</title>
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
    <h1>📊 双逻辑股票筛选结果 v4.0</h1>
    <div class="sub">{run_time} · 顶级操盘手版 · 数据源：腾讯行情 + baostock</div>
    <div class="sub" style="margin-top:4px;color:#58a6ff;">游资核心：二板定龙头 · 分歧转一致 · 筹码锁定 · 跟风惩罚 · 仓位管理</div>
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
    <div class="stat"><div class="num">{sum(1 for s,_,_ in qualified if s.enhanced.get('board', dict()).get('is_leader_candidate',False))}</div><div class="label">龙头候选</div></div>
    <div class="stat"><div class="num">{sum(1 for s,_,_ in qualified if s.enhanced.get('dc', dict()).get('signal','')=='BUY')}</div><div class="label">分歧转一致</div></div>
  </div>

  <table>
    <thead>
      <tr>
        <th>#</th><th>代码</th><th>名称</th><th>现价</th><th>涨跌</th>
        <th>量比</th><th>主力(万)</th><th>逻辑一</th><th>L1分</th>
        <th>逻辑二</th><th>L2分</th><th>增强信号</th><th>概念</th>
      </tr>
    </thead>
    <tbody>{rows_html}
    </tbody>
  </table>

  <div class="section-title">🏆 TOP 3 详情</div>
  <div class="cards">{cards_html}
  </div>

  <div style="text-align:center;padding:24px 0;font-size:12px;color:#484f58;">
    Generated by 双逻辑股票筛选系统 v4.0 · min_score={self.min_score} (effective={effective_min})
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
    parser = argparse.ArgumentParser(description="双逻辑股票筛选系统 v4.0")
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
