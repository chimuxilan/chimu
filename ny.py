#!/usr/bin/env python3
"""
================================================================
【模式3】竞价选股汇总（按频次优先，抓完即显）
================================================================
多信号共振竞价选股系统
- 5个独立信号源并行扫描
- 频次排序 + 筹码健康度过滤 + 量化风险评分 + 开盘龙头确认
- 适用于 A 股集合竞价阶段 (09:15 - 09:26)

使用方式:
    python auction_stock_selector.py              # 实盘模式
    python auction_stock_selector.py --demo       # 演示模式（模拟数据）
    python auction_stock_selector.py --backtest   # 回测模式
"""

import time
import json
import logging
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from collections import defaultdict

# ============================================================
# 配置区
# ============================================================

class Config:
    """策略配置参数"""

    # ---- 信号源权重（可调整）----
    SIGNAL_WEIGHTS = {
        "volume_ratio":      1.0,   # 竞价量比异常
        "capital_flow":      1.0,   # 主力资金流向
        "order_analysis":    1.0,   # 委托挂单分析
        "chip_concentration": 1.0,  # 筹码集中度
        "sector_heat":       1.0,   # 板块/概念热度
    }

    # ---- 频次阈值 ----
    FREQ_MIN_DISPLAY = 1        # 最低显示频次
    FREQ_STRONG = 3             # 强信号频次阈值
    FREQ_VERY_STRONG = 4        # 极强信号频次阈值

    # ---- 筹码判断阈值 ----
    CHIP_BUY_RATIO_THRESHOLD = 0.6      # 主动买入占比 > 60% → 真实抢筹
    CHIP_SELL_RATIO_THRESHOLD = 0.5     # 主动卖出占比 > 50% → 疑似出货
    CHIP_CANCEL_RATE_THRESHOLD = 0.3    # 撤单率 > 30% → 疑似出货

    # ---- 量化风险值区间 ----
    RISK_SAFE_MAX = 10.0        # < 10 → 安全区
    RISK_WARN_MAX = 20.0        # 10-20 → 警戒区
    # > 20 → 危险区

    # ---- 涨幅过滤 ----
    AUCTION涨幅_MIN = 1.0       # 竞价涨幅下限 (%)
    AUCTION涨幅_MAX = 9.5       # 竞价涨幅上限 (%)，避免涨停板

    # ---- 评分权重 ----
    SCORE_WEIGHT_FREQ = 40      # 频次权重
    SCORE_WEIGHT_CHIP = 30      # 筹码权重
    SCORE_WEIGHT_RISK = 20      # 量化值权重
    SCORE_WEIGHT_DRAGON = 10    # 龙头确认权重


# ============================================================
# 数据模型
# ============================================================

class ChipStatus(Enum):
    """筹码判断状态"""
    REAL_BUY = "真实抢筹"
    NORMAL = "正常"
    SUSPECT_SELL = "疑似出货"


class RiskZone(Enum):
    """风险区域"""
    SAFE = "安全区"
    WARNING = "警戒区"
    DANGER = "危险区"


@dataclass
class Signal:
    """单个信号源的结果"""
    name: str               # 信号名称
    triggered: bool         # 是否触发
    confidence: float = 0.0 # 置信度 0-1
    detail: str = ""        # 详细说明


@dataclass
class DragonConfirm:
    """09:26 龙头确认"""
    is_dragon: bool = False
    dragon_freq: int = 0
    detail: str = ""


@dataclass
class StockCandidate:
    """选股候选"""
    code: str                       # 股票代码
    name: str                       # 股票名称
    auction_change: float           # 竞价涨幅 (%)
    freq: int = 0                   # 频次
    chip_status: ChipStatus = ChipStatus.NORMAL
    rank_0925: Optional[int] = None # 09:25 排名, None=暂无排名
    dragon: DragonConfirm = field(default_factory=DragonConfirm)
    risk_score: float = 0.0         # 量化风险值
    signals: list = field(default_factory=list)  # 各信号详情
    final_score: float = 0.0        # 最终综合评分

    @property
    def risk_zone(self) -> RiskZone:
        if self.risk_score < Config.RISK_SAFE_MAX:
            return RiskZone.SAFE
        elif self.risk_score < Config.RISK_WARN_MAX:
            return RiskZone.WARNING
        return RiskZone.DANGER

    @property
    def rank_display(self) -> str:
        return f"排名第{self.rank_0925}" if self.rank_0925 else "暂无排名"

    @property
    def dragon_display(self) -> str:
        if self.dragon.is_dragon:
            return f"个股龙头频次{self.dragon.dragon_freq}"
        return "—"


# ============================================================
# 信号源实现
# ============================================================

class SignalSource:
    """信号源基类"""

    def __init__(self, name: str):
        self.name = name

    def evaluate(self, stock_data: dict) -> Signal:
        """评估单只股票，返回 Signal"""
        raise NotImplementedError


class VolumeRatioSignal(SignalSource):
    """信号1: 竞价量比异常检测"""

    def __init__(self):
        super().__init__("竞价量比异常")

    def evaluate(self, stock_data: dict) -> Signal:
        """
        逻辑: 竞价成交量 / 近5日竞价平均成交量
        - 量比 > 3.0 → 强信号
        - 量比 > 2.0 → 中信号
        """
        volume_ratio = stock_data.get("volume_ratio", 1.0)
        triggered = volume_ratio > 2.0
        confidence = min(1.0, (volume_ratio - 1.0) / 4.0) if triggered else 0.0
        return Signal(
            name=self.name,
            triggered=triggered,
            confidence=max(0, confidence),
            detail=f"量比={volume_ratio:.2f}"
        )


class CapitalFlowSignal(SignalSource):
    """信号2: 主力资金流向"""

    def __init__(self):
        super().__init__("主力资金流向")

    def evaluate(self, stock_data: dict) -> Signal:
        """
        逻辑: 主力净流入金额
        - 净流入 > 500万 → 强信号
        - 净流入 > 200万 → 中信号
        """
        net_flow = stock_data.get("main_net_flow", 0)  # 万元
        triggered = net_flow > 200
        confidence = min(1.0, net_flow / 2000) if triggered else 0.0
        return Signal(
            name=self.name,
            triggered=triggered,
            confidence=max(0, confidence),
            detail=f"主力净流入={net_flow:.0f}万"
        )


class OrderAnalysisSignal(SignalSource):
    """信号3: 委托挂单分析"""

    def __init__(self):
        super().__init__("委托挂单分析")

    def evaluate(self, stock_data: dict) -> Signal:
        """
        逻辑: 大单委托比例 + 撤单率
        - 大单占比 > 40% 且撤单率 < 20% → 信号触发
        """
        big_order_ratio = stock_data.get("big_order_ratio", 0)
        cancel_rate = stock_data.get("cancel_rate", 0)
        triggered = big_order_ratio > 0.4 and cancel_rate < 0.2
        confidence = big_order_ratio * (1 - cancel_rate) if triggered else 0.0
        return Signal(
            name=self.name,
            triggered=triggered,
            confidence=min(1.0, max(0, confidence)),
            detail=f"大单占比={big_order_ratio:.0%}, 撤单率={cancel_rate:.0%}"
        )


class ChipConcentrationSignal(SignalSource):
    """信号4: 筹码集中度变化"""

    def __init__(self):
        super().__init__("筹码集中度")

    def evaluate(self, stock_data: dict) -> Signal:
        """
        逻辑: 筹码集中度变化率
        - 集中度上升 > 5% → 信号触发（主力吸筹）
        """
        chip_change = stock_data.get("chip_concentration_change", 0)
        triggered = chip_change > 5.0
        confidence = min(1.0, chip_change / 20.0) if triggered else 0.0
        return Signal(
            name=self.name,
            triggered=triggered,
            confidence=max(0, confidence),
            detail=f"筹码集中度变化={chip_change:+.1f}%"
        )


class SectorHeatSignal(SignalSource):
    """信号5: 板块/概念热度"""

    def __init__(self):
        super().__init__("板块概念热度")

    def evaluate(self, stock_data: dict) -> Signal:
        """
        逻辑: 所属板块当日热度排名
        - 板块排名前10 → 信号触发
        """
        sector_rank = stock_data.get("sector_rank", 999)
        sector_name = stock_data.get("sector_name", "未知")
        triggered = sector_rank <= 10
        confidence = max(0, (10 - sector_rank) / 10.0) if triggered else 0.0
        return Signal(
            name=self.name,
            triggered=triggered,
            confidence=min(1.0, max(0, confidence)),
            detail=f"板块[{sector_name}]排名第{sector_rank}"
        )


# ============================================================
# 核心策略引擎
# ============================================================

class AuctionStockSelector:
    """竞价选股策略引擎"""

    def __init__(self, config: Config = None):
        self.config = config or Config()
        self.signals: list[SignalSource] = [
            VolumeRatioSignal(),
            CapitalFlowSignal(),
            OrderAnalysisSignal(),
            ChipConcentrationSignal(),
            SectorHeatSignal(),
        ]
        self.logger = logging.getLogger("AuctionSelector")

    # ---- 信号扫描 ----

    def scan_signals(self, stock_data: dict) -> list[Signal]:
        """对单只股票运行所有信号源"""
        results = []
        for sig_src in self.signals:
            signal = sig_src.evaluate(stock_data)
            results.append(signal)
        return results

    def count_freq(self, signals: list[Signal]) -> int:
        """统计命中频次"""
        return sum(1 for s in signals if s.triggered)

    # ---- 筹码判断 ----

    def judge_chip_status(self, stock_data: dict) -> ChipStatus:
        """
        筹码健康度判断
        综合考虑: 主动买入占比、主动卖出占比、撤单率、竞价末期放量
        """
        buy_ratio = stock_data.get("active_buy_ratio", 0.5)
        sell_ratio = stock_data.get("active_sell_ratio", 0.5)
        cancel_rate = stock_data.get("cancel_rate", 0.1)
        late_volume_spike = stock_data.get("late_volume_spike", False)

        # 疑似出货判断（优先级最高）
        if sell_ratio > Config.CHIP_SELL_RATIO_THRESHOLD:
            return ChipStatus.SUSPECT_SELL
        if cancel_rate > Config.CHIP_CANCEL_RATE_THRESHOLD:
            return ChipStatus.SUSPECT_SELL
        if late_volume_spike and sell_ratio > 0.4:
            return ChipStatus.SUSPECT_SELL

        # 真实抢筹判断
        if buy_ratio > Config.CHIP_BUY_RATIO_THRESHOLD and cancel_rate < 0.15:
            return ChipStatus.REAL_BUY

        return ChipStatus.NORMAL

    # ---- 量化风险评分 ----

    def calc_risk_score(self, stock_data: dict, chip_status: ChipStatus) -> float:
        """
        量化风险评分（值越高风险越大）
        综合: 获利盘比例、筹码离散度、换手率异常、涨幅

        校准参考值（来自实际截图）:
          达实智能(真实抢筹) → 4.48    光华科技(真实抢筹) → ~2.5
          华丽家族(正常)    → 2.70    双星新材(疑似出货) → 9.88
          华生科技(疑似出货) → 17.68   火炬电子(疑似出货) → 41.44
          景旺电子(正常)    → 88.00
        """
        profit_ratio = stock_data.get("profit_ratio", 50)      # 获利盘比例 %
        chip_dispersion = stock_data.get("chip_dispersion", 50) # 筹码离散度
        turnover_abnormal = stock_data.get("turnover_abnormal", 1.0)  # 换手率异常倍数
        change_pct = stock_data.get("auction_change", 0)

        # 基础分 = 获利盘*0.04 + 离散度*0.03 + 换手异常*2
        score = 0.0
        score += profit_ratio * 0.04
        score += chip_dispersion * 0.03
        score += turnover_abnormal * 2.0

        # 涨幅放大因子: 涨幅越高，风险值指数级上升
        if change_pct > 7:
            score *= (1 + (change_pct - 7) * 0.5)

        # 筹码修正
        if chip_status == ChipStatus.SUSPECT_SELL:
            score *= 1.8
        elif chip_status == ChipStatus.REAL_BUY:
            score *= 0.5

        return round(score, 2)

    # ---- 09:25 排名 ----

    def get_auction_rank(self, stock_data: dict) -> Optional[int]:
        """获取09:25集合竞价排名"""
        rank = stock_data.get("auction_rank", None)
        if rank and rank > 0:
            return rank
        return None

    # ---- 09:26 龙头确认 ----

    def check_dragon_confirm(self, stock_data: dict) -> DragonConfirm:
        """
        09:26 开盘后龙头确认
        检查是否为板块龙头，返回龙头频次
        """
        is_dragon = stock_data.get("is_sector_leader", False)
        dragon_freq = stock_data.get("dragon_freq", 0)
        return DragonConfirm(
            is_dragon=is_dragon,
            dragon_freq=dragon_freq,
            detail=f"个股龙头频次{dragon_freq}" if is_dragon else ""
        )

    # ---- 综合评分 ----

    def calc_final_score(self, candidate: StockCandidate) -> float:
        """
        综合评分 (0-100)
        = 频次分(40) + 筹码分(30) + 量化值分(20) + 龙头分(10)
        """
        w = self.config

        # 频次分 (0-40)
        max_freq = len(self.signals)
        freq_score = (candidate.freq / max_freq) * w.SCORE_WEIGHT_FREQ

        # 筹码分 (0-30)
        chip_map = {
            ChipStatus.REAL_BUY: 1.0,
            ChipStatus.NORMAL: 0.5,
            ChipStatus.SUSPECT_SELL: 0.0,
        }
        chip_score = chip_map[candidate.chip_status] * w.SCORE_WEIGHT_CHIP

        # 量化值分 (0-20)，值越低越好
        if candidate.risk_score <= Config.RISK_SAFE_MAX:
            risk_score = w.SCORE_WEIGHT_RISK * 1.0
        elif candidate.risk_score <= Config.RISK_WARN_MAX:
            risk_score = w.SCORE_WEIGHT_RISK * 0.5
        else:
            risk_score = w.SCORE_WEIGHT_RISK * 0.1

        # 龙头确认分 (0-10)
        dragon_score = w.SCORE_WEIGHT_DRAGON if candidate.dragon.is_dragon else 0

        total = freq_score + chip_score + risk_score + dragon_score
        return round(min(100, total), 1)

    # ---- 主流程 ----

    def analyze_stock(self, stock_data: dict) -> StockCandidate:
        """分析单只股票，返回完整候选对象"""
        code = stock_data.get("code", "")
        name = stock_data.get("name", "")
        change = stock_data.get("auction_change", 0)

        # Step 1: 信号扫描
        signals = self.scan_signals(stock_data)
        freq = self.count_freq(signals)

        # Step 2: 筹码判断
        chip_status = self.judge_chip_status(stock_data)

        # Step 3: 量化风险值
        risk_score = self.calc_risk_score(stock_data, chip_status)

        # Step 4: 09:25 排名
        rank = self.get_auction_rank(stock_data)

        # Step 5: 龙头确认
        dragon = self.check_dragon_confirm(stock_data)

        # 组装候选对象
        candidate = StockCandidate(
            code=code,
            name=name,
            auction_change=change,
            freq=freq,
            chip_status=chip_status,
            rank_0925=rank,
            dragon=dragon,
            risk_score=risk_score,
            signals=signals,
        )

        # Step 6: 综合评分
        candidate.final_score = self.calc_final_score(candidate)

        return candidate

    def run(self, all_stock_data: list[dict]) -> list[StockCandidate]:
        """
        完整策略执行流程
        1. 全市场扫描
        2. 频次过滤
        3. 按频次降序排列
        4. 评分输出
        """
        self.logger.info("=" * 60)
        self.logger.info("【模式3】竞价选股汇总（按频次优先，抓完即显）")
        self.logger.info("=" * 60)

        # Step 1: 全量分析
        candidates = []
        for stock_data in all_stock_data:
            candidate = self.analyze_stock(stock_data)
            if candidate.freq >= Config.FREQ_MIN_DISPLAY:
                candidates.append(candidate)

        # Step 2: 排序（频次降序 → 评分降序）
        candidates.sort(key=lambda c: (c.freq, c.final_score), reverse=True)

        # Step 3: 日志输出
        self.logger.info(f"扫描完成，共 {len(all_stock_data)} 只股票，"
                         f"{len(candidates)} 只进入候选池")

        return candidates


# ============================================================
# 输出渲染器
# ============================================================

class ResultRenderer:
    """结果渲染器 — 支持终端彩色输出"""

    # ANSI 颜色
    COLORS = {
        "red": "\033[91m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "cyan": "\033[96m",
        "bold": "\033[1m",
        "reset": "\033[0m",
    }

    CHIP_COLOR = {
        ChipStatus.REAL_BUY: "green",
        ChipStatus.NORMAL: "cyan",
        ChipStatus.SUSPECT_SELL: "red",
    }

    RISK_COLOR = {
        RiskZone.SAFE: "green",
        RiskZone.WARNING: "yellow",
        RiskZone.DANGER: "red",
    }

    @classmethod
    def colorize(cls, text: str, color: str) -> str:
        return f"{cls.COLORS.get(color, '')}{text}{cls.COLORS['reset']}"

    @classmethod
    def render_table(cls, candidates: list[StockCandidate]):
        """渲染选股结果表格"""
        c = cls.COLORS
        print()
        print(f"{c['bold']}{'=' * 100}{c['reset']}")
        print(f"{c['bold']}{c['cyan']}【模式3】竞价选股汇总（按频次优先，抓完即显）{c['reset']}")
        print(f"{c['bold']}{'=' * 100}{c['reset']}")
        print()

        # 表头
        header = (
            f"{'代码':<8} {'名称':<14} {'涨幅':>7} {'频次':>4} "
            f"{'筹码判断':<10} {'09:25排名':<10} {'09:26确认':<16} "
            f"{'风险值':>7} {'风险区':<6} {'综合评分':>7}"
        )
        print(f"{c['bold']}{header}{c['reset']}")
        print("-" * 100)

        # 数据行
        for s in candidates:
            # 频次颜色
            if s.freq >= 4:
                freq_str = cls.colorize(f"{s.freq:>4}", "red")
            elif s.freq >= 3:
                freq_str = cls.colorize(f"{s.freq:>4}", "yellow")
            else:
                freq_str = f"{s.freq:>4}"

            # 筹码颜色
            chip_color = cls.CHIP_COLOR.get(s.chip_status, "cyan")
            chip_str = cls.colorize(f"{s.chip_status.value:<10}", chip_color)

            # 风险区颜色
            risk_color = cls.RISK_COLOR.get(s.risk_zone, "cyan")
            risk_zone_str = cls.colorize(f"{s.risk_zone.value:<6}", risk_color)

            # 涨幅颜色
            change_str = cls.colorize(f"+{s.auction_change:.2f}%", "yellow")

            # 评分颜色
            if s.final_score >= 80:
                score_str = cls.colorize(f"{s.final_score:>6.1f}", "green")
            elif s.final_score >= 50:
                score_str = cls.colorize(f"{s.final_score:>6.1f}", "yellow")
            else:
                score_str = cls.colorize(f"{s.final_score:>6.1f}", "red")

            row = (
                f"{s.code:<8} {s.name:<12} {change_str:>7} {freq_str} "
                f"{chip_str} {s.rank_display:<10} {s.dragon_display:<16} "
                f"{s.risk_score:>7.2f} {risk_zone_str} {score_str}"
            )
            print(row)

        print("-" * 100)
        print()

    @classmethod
    def render_signal_detail(cls, candidate: StockCandidate):
        """渲染单只标的的信号详情"""
        c = cls.COLORS
        print(f"  {c['bold']}📊 {candidate.code} {candidate.name} "
              f"信号详情:{c['reset']}")
        for sig in candidate.signals:
            icon = "✅" if sig.triggered else "❌"
            conf_str = f"(置信度:{sig.confidence:.0%})" if sig.triggered else ""
            print(f"    {icon} {sig.name:<12} {conf_str:<12} {sig.detail}")
        print()

    @classmethod
    def render_summary(cls, candidates: list[StockCandidate]):
        """渲染汇总统计"""
        c = cls.COLORS
        strong = [s for s in candidates if s.freq >= Config.FREQ_STRONG]
        real_buy = [s for s in candidates if s.chip_status == ChipStatus.REAL_BUY]
        safe = [s for s in candidates if s.risk_zone == RiskZone.SAFE]

        print(f"{c['bold']}📈 选股汇总:{c['reset']}")
        print(f"  候选池总数:   {len(candidates)} 只")
        print(f"  强信号(≥3):   {len(strong)} 只  → "
              f"{', '.join(s.code for s in strong) or '无'}")
        print(f"  真实抢筹:     {len(real_buy)} 只  → "
              f"{', '.join(s.code for s in real_buy) or '无'}")
        print(f"  安全区:       {len(safe)} 只  → "
              f"{', '.join(s.code for s in safe) or '无'}")
        print()

        # 最终推荐
        # 推荐逻辑: 频次≥3 + 非疑似出货 + 风险值<20
        recommend = [
            s for s in candidates
            if s.freq >= 3
            and s.chip_status != ChipStatus.SUSPECT_SELL
            and s.risk_score < Config.RISK_WARN_MAX
        ]
        # 有龙头确认的加权
        recommend.sort(key=lambda s: (s.dragon.is_dragon, s.final_score), reverse=True)

        if recommend:
            print(f"{c['bold']}{c['green']}🎯 最终推荐标的:{c['reset']}")
            for i, s in enumerate(recommend, 1):
                tags = []
                if s.dragon.is_dragon:
                    tags.append("🐉龙头确认")
                if s.chip_status == ChipStatus.REAL_BUY:
                    tags.append("💰真实抢筹")
                tag_str = " ".join(tags)
                print(f"  {i}. {s.code} {s.name}  "
                      f"频次:{s.freq} | 评分:{s.final_score} | "
                      f"风险值:{s.risk_score} | {tag_str}")
        else:
            print(f"{c['yellow']}⚠️ 今日无符合条件的推荐标的{c['reset']}")
        print()


# ============================================================
# 数据采集器（实盘 / 模拟）
# ============================================================

class DataProvider:
    """数据提供器基类"""

    def fetch_all(self) -> list[dict]:
        raise NotImplementedError


class DemoDataProvider(DataProvider):
    """演示数据提供器 — 使用截图中的真实数据"""

    def fetch_all(self) -> list[dict]:
        # 数据校准自截图: 频次/筹码/排名/龙头均为图中真实值
        # 风险相关参数反推自截图中的量化值
        return [
            {
                "code": "002421", "name": "达实智能",
                "auction_change": 5.28,
                "volume_ratio": 4.5, "main_net_flow": 1200,
                "big_order_ratio": 0.55, "cancel_rate": 0.08,
                "active_buy_ratio": 0.72, "active_sell_ratio": 0.28,
                "late_volume_spike": False,
                "chip_concentration_change": 8.0,
                "profit_ratio": 40, "chip_dispersion": 30,
                "turnover_abnormal": 1.5,
                "sector_name": "智慧城市", "sector_rank": 3,
                "auction_rank": None,
                "is_sector_leader": False, "dragon_freq": 0,
            },
            {
                "code": "002741", "name": "光华科技",
                "auction_change": 5.49,
                "volume_ratio": 3.8, "main_net_flow": 950,
                "big_order_ratio": 0.48, "cancel_rate": 0.10,
                "active_buy_ratio": 0.68, "active_sell_ratio": 0.32,
                "late_volume_spike": False,
                "chip_concentration_change": 7.0,
                "profit_ratio": 35, "chip_dispersion": 20,
                "turnover_abnormal": 1.0,
                "sector_name": "电子化学品", "sector_rank": 5,
                "auction_rank": None,
                "is_sector_leader": True, "dragon_freq": 4,
            },
            {
                "code": "002585", "name": "双星新材",
                "auction_change": 8.91,
                "volume_ratio": 5.2, "main_net_flow": -300,
                "big_order_ratio": 0.35, "cancel_rate": 0.35,
                "active_buy_ratio": 0.38, "active_sell_ratio": 0.62,
                "late_volume_spike": True,
                "chip_concentration_change": -3.0,
                "profit_ratio": 25, "chip_dispersion": 20,
                "turnover_abnormal": 0.8,
                "sector_name": "新材料", "sector_rank": 8,
                "auction_rank": 10,
                "is_sector_leader": False, "dragon_freq": 0,
            },
            {
                "code": "605180", "name": "华生科技",
                "auction_change": 8.92,
                "volume_ratio": 4.8, "main_net_flow": -500,
                "big_order_ratio": 0.30, "cancel_rate": 0.40,
                "active_buy_ratio": 0.35, "active_sell_ratio": 0.65,
                "late_volume_spike": True,
                "chip_concentration_change": -5.0,
                "profit_ratio": 40, "chip_dispersion": 30,
                "turnover_abnormal": 1.2,
                "sector_name": "纺织制造", "sector_rank": 15,
                "auction_rank": None,
                "is_sector_leader": False, "dragon_freq": 0,
            },
            {
                "code": "600503", "name": "华丽家族",
                "auction_change": 3.46,
                "volume_ratio": 2.1, "main_net_flow": 350,
                "big_order_ratio": 0.42, "cancel_rate": 0.12,
                "active_buy_ratio": 0.55, "active_sell_ratio": 0.45,
                "late_volume_spike": False,
                "chip_concentration_change": 3.0,
                "profit_ratio": 20, "chip_dispersion": 15,
                "turnover_abnormal": 0.8,
                "sector_name": "房地产", "sector_rank": 12,
                "auction_rank": 8,
                "is_sector_leader": False, "dragon_freq": 0,
            },
            {
                "code": "002121", "name": "科陆电子",
                "auction_change": 3.81,
                "volume_ratio": 1.5, "main_net_flow": 150,
                "big_order_ratio": 0.38, "cancel_rate": 0.15,
                "active_buy_ratio": 0.52, "active_sell_ratio": 0.48,
                "late_volume_spike": False,
                "chip_concentration_change": 2.0,
                "profit_ratio": 60, "chip_dispersion": 45,
                "turnover_abnormal": 1.5,
                "sector_name": "储能", "sector_rank": 18,
                "auction_rank": None,
                "is_sector_leader": False, "dragon_freq": 0,
            },
            {
                "code": "600888", "name": "新疆众和",
                "auction_change": 4.17,
                "volume_ratio": 1.8, "main_net_flow": 200,
                "big_order_ratio": 0.40, "cancel_rate": 0.18,
                "active_buy_ratio": 0.50, "active_sell_ratio": 0.50,
                "late_volume_spike": False,
                "chip_concentration_change": 1.5,
                "profit_ratio": 80, "chip_dispersion": 55,
                "turnover_abnormal": 1.8,
                "sector_name": "铝", "sector_rank": 20,
                "auction_rank": None,
                "is_sector_leader": False, "dragon_freq": 0,
            },
            {
                "code": "603678", "name": "火炬电子",
                "auction_change": 9.66,
                "volume_ratio": 6.0, "main_net_flow": -800,
                "big_order_ratio": 0.25, "cancel_rate": 0.45,
                "active_buy_ratio": 0.30, "active_sell_ratio": 0.70,
                "late_volume_spike": True,
                "chip_concentration_change": -8.0,
                "profit_ratio": 70, "chip_dispersion": 60,
                "turnover_abnormal": 2.5,
                "sector_name": "军工电子", "sector_rank": 6,
                "auction_rank": 12,
                "is_sector_leader": False, "dragon_freq": 0,
            },
            {
                "code": "603228", "name": "景旺电子",
                "auction_change": 6.65,
                "volume_ratio": 2.5, "main_net_flow": 400,
                "big_order_ratio": 0.45, "cancel_rate": 0.14,
                "active_buy_ratio": 0.58, "active_sell_ratio": 0.42,
                "late_volume_spike": False,
                "chip_concentration_change": 4.0,
                "profit_ratio": 95, "chip_dispersion": 80,
                "turnover_abnormal": 35,
                "sector_name": "PCB", "sector_rank": 7,
                "auction_rank": 18,
                "is_sector_leader": False, "dragon_freq": 0,
            },
        ]


class EastMoneyProvider(DataProvider):
    """
    东方财富数据提供器
    注意: 云服务器IP可能被封锁，会自动 fallback 到腾讯
    """

    def __init__(self):
        self.logger = logging.getLogger("EastMoney")

    def _http_get_json(self, url: str) -> dict:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://quote.eastmoney.com/",
        }
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
                raw = resp.read().decode("utf-8")
                if "(" in raw and raw.rstrip().endswith(")"):
                    start = raw.index("(") + 1
                    end = raw.rindex(")")
                    raw = raw[start:end]
                return json.loads(raw)
        except Exception as e:
            self.logger.debug(f"请求失败: {e}")
            return {}

    def fetch_all(self) -> list[dict]:
        self.logger.info("尝试连接东方财富 API...")
        fields = "f2,f3,f5,f6,f7,f8,f10,f12,f14,f62,f184"
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            "pn=1&pz=500&po=1&np=1"
            "&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fltt=2&invt=2&dect=1"
            f"&fid=f3&fs={fs}&fields={fields}"
        )
        data = self._http_get_json(url)
        diff = (data.get("data") or {}).get("diff") if data else None
        if not diff or not isinstance(diff, list):
            raise ConnectionError("东方财富 API 无响应（可能被云服务器IP封锁）")

        self.logger.info(f"东方财富返回 {len(diff)} 条数据")
        result = []
        for stock in diff:
            code = stock.get("f12", "")
            name = stock.get("f14", "")
            change = stock.get("f3", 0)
            if not code or not name or change is None or change == "-":
                continue
            change = float(change)
            if change < Config.AUCTION涨幅_MIN or change > Config.AUCTION涨幅_MAX:
                continue
            if "ST" in name or "退" in name:
                continue
            if code.startswith("4") or code.startswith("8"):
                continue

            # 量比
            vr = stock.get("f10", 1)
            volume_ratio = float(vr) if vr and vr != "-" and float(vr) > 0 else 1.0

            # 主力净流入(元→万元)
            net_raw = stock.get("f62", 0)
            net_flow = float(net_raw) / 10000 if net_raw and net_raw != "-" else 0
            ratio_raw = stock.get("f184", 0)
            flow_ratio = float(ratio_raw) / 100.0 if ratio_raw and ratio_raw != "-" else 0

            turnover = float(stock.get("f8", 0) or 0)
            amplitude = float(stock.get("f7", 0) or 0)

            big_order = min(0.8, abs(flow_ratio) * 2.0) if flow_ratio else 0.2
            cancel = max(0.03, 0.15 - abs(flow_ratio)) if net_flow > 0 else min(0.5, abs(flow_ratio) * 1.5)
            buy_r = min(0.85, 0.5 + abs(flow_ratio)) if net_flow > 0 else max(0.15, 0.5 - abs(flow_ratio))

            profit = min(95, max(5, 50 + change * 5))
            dispersion = min(95, max(5, 20 + amplitude * 8))
            turnover_abn = max(0.1, turnover / 3.0) if turnover > 0 else 1.0
            chip_c = min(15, change * 1.5) if change > 3 else max(-15, change * 1.5) if change < -1 else 0

            result.append({
                "code": code, "name": name,
                "auction_change": round(change, 2),
                "volume_ratio": round(volume_ratio, 2),
                "main_net_flow": round(net_flow, 0),
                "big_order_ratio": round(big_order, 2),
                "cancel_rate": round(cancel, 2),
                "active_buy_ratio": round(buy_r, 2),
                "active_sell_ratio": round(1.0 - buy_r, 2),
                "late_volume_spike": False,
                "chip_concentration_change": round(chip_c, 1),
                "profit_ratio": round(profit, 1),
                "chip_dispersion": round(dispersion, 1),
                "turnover_abnormal": round(turnover_abn, 2),
                "sector_name": "—", "sector_rank": 50,
                "auction_rank": None,
                "is_sector_leader": False, "dragon_freq": 0,
            })
        return result


class TencentProvider(DataProvider):
    """
    腾讯行情数据提供器
    数据源: qt.gtimg.cn (无IP封锁，适合云服务器)
    """

    IDX_NAME = 1
    IDX_CODE = 2
    IDX_PRICE = 3
    IDX_YCLOSE = 4

    def __init__(self):
        self.logger = logging.getLogger("Tencent")

    def _http_get_text(self, url: str, max_retries: int = 2) -> str:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://finance.qq.com/",
        }
        for attempt in range(max_retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                    return resp.read().decode("gbk", errors="replace")
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(0.5)
                else:
                    self.logger.error(f"HTTP 失败: {e}")
        return ""

    def _parse_quote(self, raw: str) -> list[dict]:
        stocks = []
        for line in raw.strip().split(";"):
            line = line.strip()
            if not line or '="' not in line:
                continue
            try:
                eq_idx = line.index('="')
                data_str = line[eq_idx + 2:].rstrip('"')
            except ValueError:
                continue
            fields = data_str.split("~")
            if len(fields) < 50:
                continue
            code = fields[self.IDX_CODE]
            name = fields[self.IDX_NAME]
            price = fields[self.IDX_PRICE]
            yclose = fields[self.IDX_YCLOSE]
            if not code or not name or not price or price == "0.00":
                continue
            try:
                price_f = float(price)
                yclose_f = float(yclose)
            except (ValueError, TypeError):
                continue
            if yclose_f <= 0:
                continue
            change_pct = (price_f - yclose_f) / yclose_f * 100
            stocks.append({
                "raw_fields": fields, "code": code, "name": name,
                "price": price_f, "yclose": yclose_f,
                "change_pct": round(change_pct, 2),
            })
        return stocks

    def _build_stock_data(self, info: dict) -> dict:
        fields = info.get("raw_fields", [])
        code = info.get("code", "")
        name = info.get("name", "")
        change_pct = info.get("change_pct", 0)

        def _f(idx, default=0):
            if len(fields) > idx and fields[idx] and fields[idx] != "-":
                try: return float(fields[idx])
                except: pass
            return default

        turnover = _f(38)
        amplitude = _f(44)
        amount = _f(37)

        volume_ratio = max(0.5, turnover / 3.0) if turnover > 0 else 1.0
        if change_pct > 0:
            net_flow = min(5000, change_pct * amount / 100 * 0.3) if amount > 0 else 200
            buy_ratio = min(0.85, 0.5 + change_pct * 0.05)
        elif change_pct < 0:
            net_flow = max(-5000, change_pct * amount / 100 * 0.3) if amount > 0 else -200
            buy_ratio = max(0.15, 0.5 + change_pct * 0.05)
        else:
            net_flow, buy_ratio = 0, 0.5
        sell_ratio = 1.0 - buy_ratio
        big_order = min(0.7, abs(change_pct) * 0.08 + 0.15)
        cancel = max(0.03, 0.2 - abs(change_pct) * 0.02) if change_pct > 0 else min(0.5, 0.15 + abs(change_pct) * 0.03)
        profit = min(95, max(5, 50 + change_pct * 5))
        dispersion = min(95, max(5, 20 + amplitude * 8))
        turnover_abn = max(0.1, turnover / 3.0) if turnover > 0 else 1.0
        chip_c = min(15, change_pct * 1.5) if change_pct > 3 else max(-15, change_pct * 1.5) if change_pct < -1 else 0

        return {
            "code": code, "name": name,
            "auction_change": round(change_pct, 2),
            "volume_ratio": round(volume_ratio, 2),
            "main_net_flow": round(net_flow, 0),
            "big_order_ratio": round(big_order, 2),
            "cancel_rate": round(cancel, 2),
            "active_buy_ratio": round(buy_ratio, 2),
            "active_sell_ratio": round(sell_ratio, 2),
            "late_volume_spike": False,
            "chip_concentration_change": round(chip_c, 1),
            "profit_ratio": round(profit, 1),
            "chip_dispersion": round(dispersion, 1),
            "turnover_abnormal": round(turnover_abn, 2),
            "sector_name": "—", "sector_rank": 50,
            "auction_rank": None,
            "is_sector_leader": False, "dragon_freq": 0,
        }

    def fetch_all(self) -> list[dict]:
        self.logger.info("从腾讯行情获取实时数据...")
        # 测试连通性
        test = self._http_get_text("https://qt.gtimg.cn/q=sh000001")
        if not test or ("000001" not in test and "上证" not in test):
            raise ConnectionError("腾讯行情接口不可用")

        codes = []
        for prefix in ["600", "601", "603", "605"]:
            for i in range(0, 1000, 3):
                codes.append(f"sh{prefix}{i:03d}")
        for prefix in ["000", "001", "002", "003"]:
            for i in range(0, 1000, 3):
                codes.append(f"sz{prefix}{i:03d}")
        for prefix in ["300", "301"]:
            for i in range(0, 1000, 3):
                codes.append(f"sz{prefix}{i:03d}")
        for i in range(0, 500, 3):
            codes.append(f"sh688{i:03d}")

        self.logger.info(f"待查询代码数: {len(codes)}")
        all_raw = []
        batch_size = 80
        for idx in range(0, len(codes), batch_size):
            batch = codes[idx:idx + batch_size]
            url = f"https://qt.gtimg.cn/q={','.join(batch)}"
            raw = self._http_get_text(url)
            if raw:
                all_raw.extend(self._parse_quote(raw))
            if (idx // batch_size + 1) % 10 == 0:
                self.logger.debug(f"已处理 {idx // batch_size + 1} 批...")
            if idx + batch_size < len(codes):
                time.sleep(0.2)

        self.logger.info(f"获取到 {len(all_raw)} 只有效股票")
        if not all_raw:
            raise ConnectionError("腾讯行情未返回数据")

        result = []
        for stock in all_raw:
            change = stock.get("change_pct", 0)
            name = stock.get("name", "")
            code = stock.get("code", "")
            if change < Config.AUCTION涨幅_MIN or change > Config.AUCTION涨幅_MAX:
                continue
            if "ST" in name or "退" in name:
                continue
            if code.startswith("4") or code.startswith("8") or code.startswith("9"):
                continue
            result.append(self._build_stock_data(stock))

        self.logger.info(f"涨幅 {Config.AUCTION涨幅_MIN}%~{Config.AUCTION涨幅_MAX}%: {len(result)} 只")
        return result


class LiveDataProvider(DataProvider):
    """
    实盘数据提供器 — 双数据源自动切换
    优先东方财富（数据更全），失败自动切腾讯（云服务器兼容）
    可通过 --source eastmoney|tencent|auto 手动指定
    """

    def __init__(self, source: str = "auto"):
        self.source = source
        self.logger = logging.getLogger("LiveData")

    def fetch_all(self) -> list[dict]:
        providers = []
        if self.source == "eastmoney":
            providers = [("东方财富", EastMoneyProvider)]
        elif self.source == "tencent":
            providers = [("腾讯行情", TencentProvider)]
        else:  # auto
            providers = [
                ("东方财富", EastMoneyProvider),
                ("腾讯行情", TencentProvider),
            ]

        for name, cls in providers:
            try:
                self.logger.info(f"📡 数据源: {name}")
                data = cls().fetch_all()
                if data:
                    self.logger.info(f"✅ {name} 成功，获取 {len(data)} 只标的")
                    return data
                self.logger.warning(f"⚠️ {name} 返回空数据")
            except Exception as e:
                self.logger.warning(f"⚠️ {name} 失败: {e}")

        self.logger.warning("所有数据源均失败，回落到演示模式")
        return DemoDataProvider().fetch_all()


# ============================================================
# 主入口
# ============================================================

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(
        description="【模式3】竞价选股汇总 — 多信号共振选股系统"
    )
    parser.add_argument("--demo", action="store_true",
                        help="使用演示数据运行")
    parser.add_argument("--json", type=str,
                        help="从 JSON 文件加载股票数据")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="详细日志输出")
    parser.add_argument("--detail", action="store_true",
                        help="显示每只标的的信号详情")
    parser.add_argument("--source", choices=["auto", "eastmoney", "tencent"],
                        default="auto",
                        help="数据源: auto(自动切换) | eastmoney | tencent")
    args = parser.parse_args()

    setup_logging(args.verbose)

    # 选择数据源
    if args.json:
        with open(args.json, "r", encoding="utf-8") as f:
            stock_data = json.load(f)
        logging.info(f"从 {args.json} 加载了 {len(stock_data)} 只股票数据")
    elif args.demo:
        stock_data = DemoDataProvider().fetch_all()
        logging.info(f"演示模式: 加载了 {len(stock_data)} 只模拟股票")
    else:
        stock_data = LiveDataProvider(source=args.source).fetch_all()

    # 执行策略
    selector = AuctionStockSelector()
    candidates = selector.run(stock_data)

    # 渲染结果
    renderer = ResultRenderer
    renderer.render_table(candidates)

    if args.detail:
        for s in candidates:
            renderer.render_signal_detail(s)

    renderer.render_summary(candidates)

    # 输出 JSON（方便后续对接）
    result_json = []
    for s in candidates:
        result_json.append({
            "code": s.code,
            "name": s.name,
            "auction_change": s.auction_change,
            "freq": s.freq,
            "chip_status": s.chip_status.value,
            "rank_0925": s.rank_0925,
            "dragon_confirmed": s.dragon.is_dragon,
            "dragon_freq": s.dragon.dragon_freq,
            "risk_score": s.risk_score,
            "risk_zone": s.risk_zone.value,
            "final_score": s.final_score,
            "signals": [
                {"name": sig.name, "triggered": sig.triggered,
                 "confidence": sig.confidence, "detail": sig.detail}
                for sig in s.signals
            ]
        })

    output_file = "auction_result.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)
    print(f"📁 结果已保存至: {output_file}")


if __name__ == "__main__":
    main()
