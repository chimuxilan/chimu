"""
双逻辑股票筛选系统 v5.0 升级模块
===================================

包含9大新增模块：
  ① RealCapitalFlow      — 真实资金流（东方财富API）
  ② EnhancedEmotionCycle  — 增强情绪周期（含炸板率）
  ③ IntradayAnalysis      — 日内分时分析（腾讯行情API）
  ④ SectorRotationV2      — 板块轮动节奏
  ⑤ MarketCorrelation     — 大盘联动分析
  ⑥ EnhancedFalseBreakout — 增强假突破检测
  ⑦ MarketPhase           — 主力行为阶段识别
  ⑧ NonLinearScoring      — 非线性评分系统
  ⑨ Backtester            — 选股回测验证

使用方式：
    from stock_scanner_v5_upgrade import *

依赖：
    pip install pandas numpy requests
"""

import time
import warnings
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

warnings.filterwarnings("ignore")


# ====================================================================== #
#  工具函数
# ====================================================================== #

def _code_to_secid(code: str) -> str:
    """将股票代码转换为东方财富secid格式
    沪市(6开头): 1.600000
    深市(0/3开头): 0.000001
    注意：000001 当个股（平安银行）处理，指数请用 _index_to_secid
    """
    code = str(code).strip()
    # 处理已带前缀的情况
    if code.startswith("sh"):
        return f"1.{code[2:]}"
    if code.startswith("sz"):
        return f"0.{code[2:]}"
    # 指数特殊处理（仅在明确传入指数代码时使用）
    # 注意：000001 既是上证指数也是平安银行，优先当个股处理
    if code in ("399001", "399006"):
        return f"0.{code}"  # 深成指/创业板指
    # 个股
    if code.startswith("6"):
        return f"1.{code}"
    # 0/3开头默认深市个股（包括000001平安银行）
    if code.startswith("0") or code.startswith("3"):
        return f"0.{code}"
    else:
        return f"0.{code}"


def _index_to_secid(code: str) -> str:
    """将指数代码转换为东方财富secid格式
    上证指数: 1.000001
    深成指: 0.399001
    创业板指: 0.399006
    """
    code = str(code).strip()
    # 上证系列指数（000开头）
    if code.startswith("000"):
        return f"1.{code}"
    # 深证系列指数（399开头）
    if code.startswith("399"):
        return f"0.{code}"
    # 默认
    return _code_to_secid(code)


def _code_to_tencent(code: str) -> str:
    """将股票代码转换为腾讯行情格式 sh600000 / sz000001"""
    code = str(code).strip()
    if code.startswith("sh") or code.startswith("sz"):
        return code
    if code.startswith("6"):
        return f"sh{code}"
    else:
        return f"sz{code}"


def _safe_float(v, default=0.0) -> float:
    """安全的浮点数转换"""
    try:
        if v is None or str(v).strip() in ("", "-", "--", "None"):
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


# ====================================================================== #
#  模块1: RealCapitalFlow — 真实资金流
# ====================================================================== #

class RealCapitalFlow:
    """
    真实资金流模块 — 替代VWAP估算

    使用东方财富免费API获取真实的大单/超大单/中单/小单净流入数据。

    数据来源：
        - 日K资金流: push2.eastmoney.com/api/qt/stock/fflow/daykline/get
        - 分钟资金流: push2.eastmoney.com/api/qt/stock/fflow/kline/get

    输出字段：
        - main_net_inflow: 主力净流入(万元) = 超大单+大单
        - super_large_net_inflow: 超大单净流入(万元)
        - large_net_inflow: 大单净流入(万元)
        - medium_net_inflow: 中单净流入(万元)
        - small_net_inflow: 小单净流入(万元)
        - flow_quality_score: 资金流质量评分(0~100)
        - consecutive_inflow_days: 连续主力净流入天数

    使用示例：
        flow = RealCapitalFlow()
        result = flow.get_single("600519")  # 获取茅台资金流
        results = flow.get_batch(["600519", "000001", "000858"])  # 批量获取
    """

    # 东方财富API地址
    DAYKLINE_URL = "http://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
    MIN_URL = "http://push2.eastmoney.com/api/qt/stock/fflow/kline/get"

    def __init__(self, request_interval: float = 0.2):
        """
        初始化资金流模块

        Args:
            request_interval: 请求间隔(秒)，默认0.2秒，防止被封IP
        """
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "http://quote.eastmoney.com/",
        })
        self.request_interval = request_interval
        self._last_request_time = 0

    def _rate_limit(self):
        """速率控制：确保两次请求之间有足够间隔"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_time = time.time()

    def get_single(self, code: str, days: int = 10,
                   quote_data: dict = None) -> Dict[str, Any]:
        """
        获取单只股票的资金流数据（多源降级：东财API → VWAP估算）

        Args:
            code: 股票代码 (如 "600519")
            days: 获取最近N天的日K资金流数据，默认10天
            quote_data: 可选，行情数据dict(含close/amount/volume/volume_ratio/turnover)
                       用于东财API失败时的VWAP估算降级

        Returns:
            dict: 包含资金流各项数据的字典
        """
        default_result = {
            "main_net_inflow": 0.0,
            "super_large_net_inflow": 0.0,
            "large_net_inflow": 0.0,
            "medium_net_inflow": 0.0,
            "small_net_inflow": 0.0,
            "flow_quality_score": 0.0,
            "consecutive_inflow_days": 0,
            "daily_flows": [],
            "success": False,
            "source": "none",
        }

        # ===== 方案A: 东方财富push2 API =====
        try:
            result = self._fetch_eastmoney(code, days)
            if result["success"]:
                result["source"] = "eastmoney"
                return result
        except Exception:
            pass

        # ===== 方案B: VWAP估算降级 =====
        if quote_data:
            result = self._estimate_by_vwap(quote_data)
            result["source"] = "vwap_estimate"
            return result

        return default_result

    def _fetch_eastmoney(self, code: str, days: int) -> Dict[str, Any]:
        """从东方财富API获取资金流数据"""
        default_result = {
            "main_net_inflow": 0.0, "super_large_net_inflow": 0.0,
            "large_net_inflow": 0.0, "medium_net_inflow": 0.0,
            "small_net_inflow": 0.0, "flow_quality_score": 0.0,
            "consecutive_inflow_days": 0, "daily_flows": [], "success": False,
        }
        secid = _code_to_secid(code)
        self._rate_limit()

        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "klt": "101", "lmt": str(days), "cb": "",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
        }

        resp = self.session.get(self.DAYKLINE_URL, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        if not data or data.get("data") is None:
            return default_result

        klines = data["data"].get("klines", [])
        if not klines:
            return default_result

        daily_flows = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                daily_flows.append({
                    "date": parts[0],
                    "main_net_inflow": _safe_float(parts[1]) / 10000,
                    "small_net_inflow": _safe_float(parts[2]) / 10000,
                    "medium_net_inflow": _safe_float(parts[3]) / 10000,
                    "large_net_inflow": _safe_float(parts[4]) / 10000,
                    "super_large_net_inflow": _safe_float(parts[5]) / 10000,
                })
            except (IndexError, ValueError):
                continue

        if not daily_flows:
            return default_result

        latest = daily_flows[-1]
        consecutive = 0
        for d in reversed(daily_flows):
            if d["main_net_inflow"] > 0:
                consecutive += 1
            else:
                break

        quality_score = self._calc_flow_quality(daily_flows)

        return {
            "main_net_inflow": round(latest["main_net_inflow"], 2),
            "super_large_net_inflow": round(latest["super_large_net_inflow"], 2),
            "large_net_inflow": round(latest["large_net_inflow"], 2),
            "medium_net_inflow": round(latest["medium_net_inflow"], 2),
            "small_net_inflow": round(latest["small_net_inflow"], 2),
            "flow_quality_score": round(quality_score, 1),
            "consecutive_inflow_days": consecutive,
            "daily_flows": daily_flows,
            "success": True,
        }

    def _estimate_by_vwap(self, quote_data: dict) -> Dict[str, Any]:
        """基于量价联动的资金流估算（降级方案）
        原理：
          - 量比>1 + 涨幅>0 = 资金流入
          - 量比>1 + 涨幅<0 = 资金流出（放量下跌）
          - 量比<1 + 涨幅>0 = 缩量上涨（散户推动）
          - 结合成交额估算主力净流入规模
        """
        close = _safe_float(quote_data.get("close", 0))
        amount = _safe_float(quote_data.get("amount", 0))   # 成交额（元）
        volume_ratio = _safe_float(quote_data.get("volume_ratio", 0))
        pct_change = _safe_float(quote_data.get("pct_change", 0))
        turnover = _safe_float(quote_data.get("turnover", 0))

        if close <= 0 or amount <= 0:
            return {"main_net_inflow": 0, "super_large_net_inflow": 0,
                    "large_net_inflow": 0, "medium_net_inflow": 0,
                    "small_net_inflow": 0, "flow_quality_score": 0,
                    "consecutive_inflow_days": 0, "daily_flows": [], "success": False}

        amount_wan = amount / 10000  # 转万元

        # 量价信号强度: 量比 × 涨幅方向
        # 量比>1表示有资金活跃，涨幅方向表示买卖方向
        if volume_ratio > 0:
            signal = (pct_change / 5.0) * min(volume_ratio, 5.0)  # 归一化到[-5, 5]范围
        else:
            signal = pct_change / 5.0

        # 信号夹板，避免极端值
        signal = max(-3.0, min(3.0, signal))

        # 主力净流入估算 = 成交额(万) × 信号 × 换手率系数
        # 换手率越高，主力参与度越高
        turnover_factor = min(turnover / 5.0, 2.0) if turnover > 0 else 0.5
        main_inflow = amount_wan * signal * turnover_factor * 0.1

        # 拆分
        if main_inflow > 0:
            super_large = main_inflow * 0.6
            large = main_inflow * 0.4
        else:
            super_large = main_inflow * 0.5
            large = main_inflow * 0.5

        # 质量评分 (0~100): 50为中性
        quality_score = max(0, min(100, 50 + signal * 15))

        return {
            "main_net_inflow": round(main_inflow, 2),
            "super_large_net_inflow": round(super_large, 2),
            "large_net_inflow": round(large, 2),
            "medium_net_inflow": 0.0,
            "small_net_inflow": round(-main_inflow, 2),
            "flow_quality_score": round(quality_score, 1),
            "consecutive_inflow_days": 0,
            "daily_flows": [],
            "success": True,
        }

    def get_minute_flow(self, code: str) -> Dict[str, Any]:
        """
        获取单只股票的分钟级资金流数据（今日）

        Args:
            code: 股票代码

        Returns:
            dict: 分钟级资金流数据
        """
        default_result = {
            "minute_flows": [],
            "morning_main_inflow": 0.0,
            "afternoon_main_inflow": 0.0,
            "success": False,
        }

        try:
            secid = _code_to_secid(code)
            self._rate_limit()

            params = {
                "secid": secid,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "klt": "1",  # 1分钟
                "lmt": "240",  # 4小时 = 240分钟
                "cb": "",
                "ut": "b2884a393a59ad64002292a3e90d46a5",
            }

            resp = self.session.get(self.MIN_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if not data or data.get("data") is None:
                return default_result

            klines = data["data"].get("klines", [])
            minute_flows = []
            for line in klines:
                parts = line.split(",")
                if len(parts) < 6:
                    continue
                try:
                    minute_flows.append({
                        "time": parts[0],
                        "main_net_inflow": _safe_float(parts[1]) / 10000,
                        "small_net_inflow": _safe_float(parts[2]) / 10000,
                        "medium_net_inflow": _safe_float(parts[3]) / 10000,
                        "large_net_inflow": _safe_float(parts[4]) / 10000,
                        "super_large_net_inflow": _safe_float(parts[5]) / 10000,
                    })
                except (IndexError, ValueError):
                    continue

            # 分上午/下午统计
            morning = [m for m in minute_flows if m["time"] < "13:00"]
            afternoon = [m for m in minute_flows if m["time"] >= "13:00"]

            morning_main = sum(m["main_net_inflow"] for m in morning)
            afternoon_main = sum(m["main_net_inflow"] for m in afternoon)

            return {
                "minute_flows": minute_flows,
                "morning_main_inflow": round(morning_main, 2),
                "afternoon_main_inflow": round(afternoon_main, 2),
                "success": True,
            }

        except Exception as e:
            print(f"  ⚠ 分钟资金流获取失败 {code}: {e}")
            return default_result

    def _calc_flow_quality(self, daily_flows: List[Dict]) -> float:
        """
        计算资金流质量评分 (0~100)

        评分维度：
        1. 连续净流入天数 (权重40%)
        2. 净流入金额趋势 (权重30%)
        3. 超大单/大单占比 (权重30%)

        Args:
            daily_flows: 最近N天的资金流数据列表

        Returns:
            float: 0~100的质量评分
        """
        if not daily_flows:
            return 0.0

        score = 0.0

        # 维度1: 连续净流入天数
        consecutive = 0
        for d in reversed(daily_flows):
            if d["main_net_inflow"] > 0:
                consecutive += 1
            else:
                break
        # 连续3天以上满分，1天20分
        continuity_score = min(consecutive / 3, 1.0) * 100
        score += continuity_score * 0.4

        # 维度2: 净流入趋势（最近3天 vs 之前）
        if len(daily_flows) >= 4:
            recent_avg = np.mean([d["main_net_inflow"] for d in daily_flows[-3:]])
            earlier_avg = np.mean([d["main_net_inflow"] for d in daily_flows[:-3]])
            if recent_avg > 0:
                trend_ratio = min(recent_avg / (abs(earlier_avg) + 1), 3.0) / 3.0
                trend_score = trend_ratio * 100
            else:
                trend_score = 0
            score += trend_score * 0.3
        else:
            score += 30 * 0.3  # 数据不足给中间分

        # 维度3: 超大单占比（机构/游资特征）
        latest = daily_flows[-1]
        total_inflow = abs(latest["super_large_net_inflow"]) + abs(latest["large_net_inflow"]) + \
                       abs(latest["medium_net_inflow"]) + abs(latest["small_net_inflow"])
        if total_inflow > 0:
            big_ratio = (abs(latest["super_large_net_inflow"]) + abs(latest["large_net_inflow"])) / total_inflow
            # 主力净流入为正且大单占比高 = 好信号
            if latest["main_net_inflow"] > 0:
                big_score = big_ratio * 100
            else:
                # 主力在流出但大单占比高 = 可能是出货
                big_score = max(0, (1 - big_ratio) * 50)
        else:
            big_score = 0
        score += big_score * 0.3

        return min(max(score, 0), 100)

    def get_batch(self, codes: List[str], days: int = 10,
                  quotes_dict: dict = None) -> Dict[str, Dict]:
        """
        批量获取多只股票的资金流数据（带速率控制，自动降级）

        Args:
            codes: 股票代码列表
            days: 获取最近N天数据
            quotes_dict: 可选，{code: {close, amount, volume, volume_ratio, turnover}}
                        东财API失败时用于VWAP估算降级

        Returns:
            dict: {code: 资金流数据字典}
        """
        results = {}
        total = len(codes)
        success_count = 0
        fallback_count = 0
        for i, code in enumerate(codes):
            qd = quotes_dict.get(code) if quotes_dict else None
            r = self.get_single(code, days, quote_data=qd)
            results[code] = r
            if r["success"]:
                if r.get("source") == "eastmoney":
                    success_count += 1
                elif r.get("source") == "vwap_estimate":
                    fallback_count += 1
            if (i + 1) % 50 == 0:
                print(f"  📊 资金流进度: {i + 1}/{total} "
                      f"(东财{success_count}+VWAP降级{fallback_count})")
        print(f"  ✅ 资金流完成: 东财{success_count} + VWAP降级{fallback_count} / {total}")
        return results

    def close(self):
        """关闭会话"""
        self.session.close()


# ====================================================================== #
#  模块2: EnhancedEmotionCycle — 增强情绪周期
# ====================================================================== #

class EnhancedEmotionCycle:
    """
    增强情绪周期模块 — 在原有情绪周期基础上增加炸板率等指标

    新增指标：
        - 炸板率: 曾涨停但收盘未封住的比例
        - 连板梯队高度: 市场最高连板数
        - 首板次日溢价率: 昨日首板股今日平均涨幅
        - 昨日涨停今日平均表现: 所有昨日涨停股今日涨跌幅
        - 跌停加速: 连续N天跌停数递增

    输出：
        - stage: 情绪阶段（冰点/回暖/高潮/退潮）
        - aggression: 攻击系数 (0.2~1.0)
        - 炸板率、连板高度等详细指标

    使用示例：
        emotion = EnhancedEmotionCycle()
        result = emotion.analyze(all_stocks, today_limit_ups, today_limit_downs,
                                  yesterday_limit_ups_today_pct)
    """

    def analyze(
        self,
        all_stocks: list,
        today_limit_up_count: int,
        today_limit_down_count: int,
        today_broken_limit_count: int = 0,
        yesterday_limit_up_today_avg_pct: float = 0.0,
        consecutive_board_height: int = 0,
        first_board_yesterday_today_avg_pct: float = None,
        limit_down_history: List[int] = None,
    ) -> Dict[str, Any]:
        """
        综合分析市场情绪周期

        Args:
            all_stocks: 所有股票数据列表（含pct_change属性）
            today_limit_up_count: 今日涨停数量
            today_limit_down_count: 今日跌停数量
            today_broken_limit_count: 今日炸板数量（曾涨停但收盘未封住）
            yesterday_limit_up_today_avg_pct: 昨日涨停股今日平均涨幅(%)
            consecutive_board_height: 当前市场最高连板数
            first_board_yesterday_today_avg_pct: 昨日首板股今日平均涨幅(%)
            limit_down_history: 最近N天的跌停数列表（从旧到新）

        Returns:
            dict: 情绪分析结果
        """
        # ---- 计算炸板率 ----
        total_attempted = today_limit_up_count + today_broken_limit_count
        broken_rate = today_broken_limit_count / max(total_attempted, 1)

        # ---- 跌停加速检测 ----
        limit_down_accelerating = False
        limit_down_accel_days = 0
        if limit_down_history and len(limit_down_history) >= 3:
            recent = limit_down_history[-3:]
            if all(recent[i] < recent[i + 1] for i in range(len(recent) - 1)):
                limit_down_accelerating = True
                limit_down_accel_days = len(recent)

        # ---- 基础情绪指标 ----
        all_pct = [_safe_float(getattr(s, "pct_change", 0)) for s in all_stocks]
        up_count = sum(1 for p in all_pct if p > 0)
        down_count = sum(1 for p in all_pct if p < 0)
        total = up_count + down_count + 1e-10
        up_ratio = up_count / total
        median_pct = np.median(all_pct) if all_pct else 0

        # ---- 涨跌停比 ----
        zt_ratio = today_limit_up_count / max(today_limit_down_count, 1)

        # ---- 综合情绪分数 (0~100) ----
        emotion_score = 0.0

        # 涨跌比贡献 (0~30分)
        emotion_score += min(up_ratio * 50, 30)

        # 涨停数贡献 (0~25分)
        emotion_score += min(today_limit_up_count / 80, 1.0) * 25

        # 跌停数惩罚 (扣0~20分)
        emotion_score -= min(today_limit_down_count / 30, 1.0) * 20

        # 涨跌停比贡献 (0~15分)
        emotion_score += min(zt_ratio / 5, 1.0) * 15

        # 连板高度贡献 (0~10分)
        if consecutive_board_height >= 7:
            emotion_score += 10
        elif consecutive_board_height >= 5:
            emotion_score += 7
        elif consecutive_board_height >= 3:
            emotion_score += 4

        # 炸板率惩罚 (扣0~15分)
        emotion_score -= broken_rate * 15

        # 跌停加速惩罚 (扣0~10分)
        if limit_down_accelerating:
            emotion_score -= min(limit_down_accel_days * 3, 10)

        # 昨日涨停今日表现 (0~10分调整)
        if yesterday_limit_up_today_avg_pct != 0:
            if yesterday_limit_up_today_avg_pct > 3:
                emotion_score += 10
            elif yesterday_limit_up_today_avg_pct > 0:
                emotion_score += 5
            elif yesterday_limit_up_today_avg_pct < -3:
                emotion_score -= 10
            elif yesterday_limit_up_today_avg_pct < 0:
                emotion_score -= 5

        emotion_score = max(0, min(100, emotion_score))

        # ---- 判断情绪阶段 ----
        if emotion_score >= 75:
            stage = "高潮"
            advice = "市场极度亢奋，注意风险，减仓为主"
        elif emotion_score >= 55:
            stage = "回暖"
            advice = "情绪回暖，可适度参与龙头股"
        elif emotion_score >= 35:
            stage = "震荡"
            advice = "情绪分歧，精选个股，控制仓位"
        elif emotion_score >= 20:
            stage = "退潮"
            advice = "退潮期，减少操作，等待企稳"
        else:
            stage = "冰点"
            advice = "冰点期，空仓观望为主，极端超跌可小仓试错"

        # ---- 攻击系数 (0.2~1.0) ----
        # 高潮期: 0.4~0.6 (减仓，不再激进)
        # 回暖期: 0.7~0.9 (最佳进攻窗口)
        # 震荡期: 0.5~0.7
        # 退潮期: 0.2~0.4
        # 冰点期: 0.2~0.3
        if stage == "高潮":
            aggression = 0.5
        elif stage == "回暖":
            aggression = 0.8
        elif stage == "震荡":
            aggression = 0.6
        elif stage == "退潮":
            aggression = 0.3
        else:  # 冰点
            aggression = 0.2

        # 根据具体指标微调
        if broken_rate > 0.5:
            aggression *= 0.8  # 炸板率高，降低攻击性
        if limit_down_accelerating:
            aggression *= 0.7  # 跌停加速，大幅降低
        if consecutive_board_height >= 7 and stage != "冰点":
            aggression *= 1.1  # 有高连板股，市场还有赚钱效应

        aggression = max(0.2, min(1.0, aggression))

        return {
            "stage": stage,
            "aggression": round(aggression, 2),
            "emotion_score": round(emotion_score, 1),
            "advice": advice,
            "broken_rate": round(broken_rate, 4),
            "consecutive_board_height": consecutive_board_height,
            "zt_ratio": round(zt_ratio, 2),
            "up_ratio": round(up_ratio, 4),
            "median_pct": round(float(median_pct), 2),
            "limit_down_accelerating": limit_down_accelerating,
            "yesterday_limit_up_today_avg_pct": round(yesterday_limit_up_today_avg_pct, 2),
            "first_board_yesterday_today_avg_pct": round(
                first_board_yesterday_today_avg_pct if first_board_yesterday_today_avg_pct is not None else 0, 2
            ),
        }


# ====================================================================== #
#  模块3: IntradayAnalysis — 日内分时分析
# ====================================================================== #

class IntradayAnalysis:
    """
    日内分时分析模块 — 腾讯行情API

    分析维度：
        - 集合竞价分析：竞价量、竞价价格趋势(9:20后)
        - 前30分钟走势分类：开盘急拉型/低开高走型/高开低走型/震荡型
        - 尾盘30分钟强度：尾盘拉升/下杀/横盘
        - 分时均价线(VWAP)偏离
        - 分时量能分布：前30分钟量能占比

    API:
        - 分时数据: https://ifzq.gtimg.cn/appstock/app/minute/query?code=sh600000

    使用示例：
        intra = IntradayAnalysis()
        result = intra.analyze("600519")
    """

    MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/minute/query"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })

    def analyze(self, code: str) -> Dict[str, Any]:
        """
        分析单只股票的日内分时走势

        Args:
            code: 股票代码

        Returns:
            dict: 分时分析结果
        """
        default_result = {
            "auction_volume": 0,
            "auction_price_trend": "neutral",
            "opening_pattern": "震荡型",
            "closing_strength": "横盘",
            "vwap_deviation": 0.0,
            "morning_volume_ratio": 0.0,
            "intraday_score": 0.0,
            "success": False,
        }

        try:
            tc_code = _code_to_tencent(code)
            url = f"{self.MINUTE_URL}?code={tc_code}"
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if not data or "data" not in data:
                return default_result

            stock_key = tc_code
            stock_data = data.get("data", {}).get(stock_key, {})
            minute_data = stock_data.get("data", {}).get("data", [])

            if not minute_data:
                return default_result

            # 解析分时数据: "0930 12.50 1000 125000" -> 时间 价格 成交量(手) 成交额(元)
            parsed = []
            for item in minute_data:
                parts = item.split(" ")
                if len(parts) < 4:
                    continue
                try:
                    parsed.append({
                        "time": parts[0],
                        "price": _safe_float(parts[1]),
                        "volume": _safe_float(parts[2]),  # 手
                        "amount": _safe_float(parts[3]),  # 元
                    })
                except (IndexError, ValueError):
                    continue

            if not parsed:
                return default_result

            # ---- 集合竞价分析 (09:25) ----
            auction_items = [p for p in parsed if p["time"] <= "0925"]
            if auction_items:
                auction_volume = sum(p["volume"] for p in auction_items)
                # 竞价价格趋势: 对比09:20后价格变化
                late_auction = [p for p in auction_items if p["time"] >= "0920"]
                if len(late_auction) >= 2:
                    price_start = late_auction[0]["price"]
                    price_end = late_auction[-1]["price"]
                    if price_end > price_start * 1.002:
                        auction_trend = "上行"
                    elif price_end < price_start * 0.998:
                        auction_trend = "下行"
                    else:
                        auction_trend = "平稳"
                else:
                    auction_trend = "neutral"
            else:
                auction_volume = 0
                auction_trend = "neutral"

            # ---- 前30分钟走势分类 ----
            first_30 = [p for p in parsed if p["time"] <= "1000"]
            opening_pattern = "震荡型"
            if len(first_30) >= 5:
                open_price = parsed[0]["price"]
                # 前30分钟最高/最低
                prices_30 = [p["price"] for p in first_30]
                high_30 = max(prices_30)
                low_30 = min(prices_30)
                last_30 = prices_30[-1]

                pct_change_30 = (last_30 - open_price) / max(open_price, 0.01) * 100

                if pct_change_30 > 3 and high_30 == prices_30[-1] or (
                    len(prices_30) > 2 and prices_30[-1] > prices_30[0] > prices_30[len(prices_30) // 2]
                ):
                    opening_pattern = "开盘急拉型"
                elif open_price > last_30 and last_30 > low_30 and pct_change_30 < -1:
                    # 高开后回落但有所企稳
                    if pct_change_30 > -3:
                        opening_pattern = "高开低走型"
                    else:
                        opening_pattern = "高开低走型"
                elif last_30 > open_price and (last_30 - low_30) / max(low_30, 0.01) > 0.02:
                    opening_pattern = "低开高走型"
                else:
                    # 判断是否在开盘价附近震荡
                    range_pct = (high_30 - low_30) / max(open_price, 0.01) * 100
                    if range_pct < 2:
                        opening_pattern = "窄幅震荡型"
                    else:
                        opening_pattern = "宽幅震荡型"

            # ---- 尾盘30分钟强度 (14:30之后) ----
            last_30_items = [p for p in parsed if p["time"] >= "1430"]
            closing_strength = "横盘"
            if len(last_30_items) >= 5:
                prices_tail = [p["price"] for p in last_30_items]
                vol_tail = sum(p["volume"] for p in last_30_items)
                tail_pct = (prices_tail[-1] - prices_tail[0]) / max(prices_tail[0], 0.01) * 100

                if tail_pct > 1.5:
                    closing_strength = "尾盘拉升"
                elif tail_pct < -1.5:
                    closing_strength = "尾盘下杀"
                else:
                    closing_strength = "横盘"

            # ---- VWAP偏离 ----
            total_amount = sum(p["amount"] for p in parsed)
            total_volume = sum(p["volume"] for p in parsed)
            if total_volume > 0:
                vwap = total_amount / (total_volume * 100)  # 每股均价
                current_price = parsed[-1]["price"]
                vwap_deviation = (current_price - vwap) / max(vwap, 0.01) * 100
            else:
                vwap = 0
                vwap_deviation = 0

            # ---- 量能分布 ----
            morning_volume = sum(p["volume"] for p in parsed if p["time"] <= "1130")
            morning_volume_ratio = morning_volume / max(total_volume, 1)

            # ---- 综合分时评分 (0~100) ----
            score = 50  # 基准分

            # 竞价上行加分
            if auction_trend == "上行":
                score += 10
            elif auction_trend == "下行":
                score -= 10

            # 走势形态加分
            if opening_pattern == "开盘急拉型":
                score += 15
            elif opening_pattern == "低开高走型":
                score += 10
            elif opening_pattern == "高开低走型":
                score -= 10

            # 尾盘强度
            if closing_strength == "尾盘拉升":
                score += 15
            elif closing_strength == "尾盘下杀":
                score -= 15

            # VWAP偏离
            if vwap_deviation > 2:
                score += 10
            elif vwap_deviation > 0:
                score += 5
            elif vwap_deviation < -2:
                score -= 10
            elif vwap_deviation < 0:
                score -= 5

            score = max(0, min(100, score))

            return {
                "auction_volume": auction_volume,
                "auction_price_trend": auction_trend,
                "opening_pattern": opening_pattern,
                "closing_strength": closing_strength,
                "vwap_deviation": round(vwap_deviation, 2),
                "morning_volume_ratio": round(morning_volume_ratio, 4),
                "intraday_score": round(score, 1),
                "success": True,
            }

        except Exception as e:
            print(f"  ⚠ 分时分析失败 {code}: {e}")
            return default_result

    def get_batch(self, codes: List[str]) -> Dict[str, Dict]:
        """批量获取分时分析结果"""
        results = {}
        for code in codes:
            results[code] = self.analyze(code)
            time.sleep(0.15)
        return results

    def close(self):
        self.session.close()


# ====================================================================== #
#  模块4: SectorRotationV2 — 板块轮动节奏
# ====================================================================== #

class SectorRotationV2:
    """
    板块轮动节奏分析模块

    分析维度：
        - 主线延续检测：昨天最热板块今天是否继续
        - 主线切换检测：新热点崛起替代旧热点
        - 板块内轮动：龙头→补涨→行情尾声
        - 退潮信号：无明确主线，热点散乱

    输出：
        - rotation_phase: 轮动阶段（主升/补涨/切换/退潮）
        - main_sector: 当前主线板块
        - rotation_direction: 轮动方向描述
        - sector_scores: 各板块评分

    使用示例：
        rotation = SectorRotationV2()
        result = rotation.analyze(stocks, code_concepts, yesterday_sector_data)
    """

    def analyze(
        self,
        stocks: list,
        code_concepts: Dict[str, List[str]],
        yesterday_hot_sectors: List[Dict] = None,
    ) -> Dict[str, Any]:
        """
        分析板块轮动节奏

        Args:
            stocks: 当前股票列表（含pct_change, amount等属性）
            code_concepts: {code: [概念1, 概念2, ...]} 映射
            yesterday_hot_sectors: 昨日热门板块列表 [{"name": "板块名", "avg_pct": 均涨幅, "count": 股数}]

        Returns:
            dict: 板块轮动分析结果
        """
        # ---- 计算今日各板块表现 ----
        sector_stats = {}  # {板块名: {pcts: [], amounts: [], limit_ups: 0, stocks: []}}

        for s in stocks:
            concepts = code_concepts.get(getattr(s, "code", ""), [])
            pct = _safe_float(getattr(s, "pct_change", 0))
            amount = _safe_float(getattr(s, "amount", 0))
            threshold = _safe_float(getattr(s, "limit_up_threshold", 9.8))

            for concept in concepts:
                if concept not in sector_stats:
                    sector_stats[concept] = {
                        "pcts": [],
                        "amounts": [],
                        "limit_ups": 0,
                        "limit_downs": 0,
                        "stocks": [],
                    }
                sector_stats[concept]["pcts"].append(pct)
                sector_stats[concept]["amounts"].append(amount)
                sector_stats[concept]["stocks"].append(getattr(s, "code", ""))
                if pct >= threshold:
                    sector_stats[concept]["limit_ups"] += 1
                if pct <= -threshold:
                    sector_stats[concept]["limit_downs"] += 1

        # 计算各板块统计
        sector_scores = {}
        for name, stats in sector_stats.items():
            if len(stats["pcts"]) < 3:
                continue

            avg_pct = np.mean(stats["pcts"])
            total_amount = sum(stats["amounts"])
            up_ratio = sum(1 for p in stats["pcts"] if p > 0) / len(stats["pcts"])

            # 板块热度评分
            heat_score = 0
            heat_score += min(avg_pct * 10, 40)  # 均涨幅贡献
            heat_score += stats["limit_ups"] * 8   # 涨停贡献
            heat_score -= stats["limit_downs"] * 5  # 跌停惩罚
            heat_score += up_ratio * 30             # 上涨比例贡献
            heat_score = max(0, min(100, heat_score))

            sector_scores[name] = {
                "avg_pct": round(avg_pct, 2),
                "count": len(stats["pcts"]),
                "limit_ups": stats["limit_ups"],
                "limit_downs": stats["limit_downs"],
                "up_ratio": round(up_ratio, 4),
                "total_amount": total_amount,
                "heat_score": round(heat_score, 1),
                "stocks": stats["stocks"],
            }

        # ---- 排序获取今日热门 ----
        today_hot = sorted(sector_scores.items(), key=lambda x: x[1]["heat_score"], reverse=True)

        # ---- 主线分析 ----
        main_sector = ""
        rotation_phase = "退潮"
        rotation_direction = ""

        if today_hot:
            main_sector = today_hot[0][0]
            main_heat = today_hot[0][1]["heat_score"]

            # 检测是否有明确主线（第一名大幅领先第二名）
            if len(today_hot) >= 2:
                second_heat = today_hot[1][1]["heat_score"]
                heat_gap = main_heat - second_heat
            else:
                heat_gap = main_heat

            # 主线延续检测
            yesterday_main = ""
            if yesterday_hot_sectors:
                yesterday_main = yesterday_hot_sectors[0].get("name", "")

            if main_heat >= 60:
                if yesterday_main == main_sector:
                    rotation_phase = "主升"
                    rotation_direction = f"主线延续: {main_sector} 连续领涨"
                elif heat_gap > 15:
                    rotation_phase = "切换"
                    rotation_direction = f"新主线崛起: {main_sector} (原主线: {yesterday_main})"
                else:
                    rotation_phase = "补涨"
                    rotation_direction = f"多线并行，{main_sector} 领衔"

            elif main_heat >= 40:
                # 检查板块内是否有龙头→补涨轮动
                if today_hot[0][1]["limit_ups"] >= 2:
                    rotation_phase = "补涨"
                    rotation_direction = f"{main_sector} 板块内轮动，涨停扩散"
                else:
                    rotation_phase = "切换"
                    rotation_direction = f"热点分散，主线不明"

            else:
                # 热度都不高
                if len(today_hot) >= 3:
                    top3_heat = [h[1]["heat_score"] for h in today_hot[:3]]
                    if max(top3_heat) - min(top3_heat) < 10:
                        rotation_phase = "退潮"
                        rotation_direction = "无明确主线，热点散乱，建议观望"
                    else:
                        rotation_phase = "切换"
                        rotation_direction = "弱轮动，需观察新主线"
                else:
                    rotation_phase = "退潮"
                    rotation_direction = "板块数据不足"

        return {
            "rotation_phase": rotation_phase,
            "main_sector": main_sector,
            "rotation_direction": rotation_direction,
            "today_hot_sectors": [(name, info) for name, info in today_hot[:10]],
            "sector_scores": sector_scores,
            "yesterday_main_sector": yesterday_hot_sectors[0]["name"] if yesterday_hot_sectors else "",
        }


# ====================================================================== #
#  模块5: MarketCorrelation — 大盘联动分析
# ====================================================================== #

class MarketCorrelation:
    """
    大盘联动分析模块

    功能：
        - 获取上证指数(000001)和深成指(399001)数据
        - 计算个股Beta系数
        - 检测"逆势股"：大盘跌它涨的股票
        - 检测"领涨股"：大盘见底时谁先反弹

    输出：
        - beta: Beta系数
        - inverse_score: 逆势强度评分 (越高=越逆势)
        - leader_score: 领涨强度评分

    使用示例：
        corr = MarketCorrelation()
        result = corr.analyze("600519", stock_kline_df, index_kline_df)
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })

    def get_index_kline(self, index_code: str, days: int = 120) -> pd.DataFrame:
        """
        获取指数K线数据（东方财富API）

        Args:
            index_code: 指数代码 ("000001"=上证, "399001"=深成)
            days: 获取天数

        Returns:
            DataFrame: 包含date, open, high, low, close, volume, pct_change的K线数据
        """
        try:
            secid = _index_to_secid(index_code)
            url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": "1",
                "end": "20500101",
                "lmt": str(days),
                "cb": "",
            }

            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if not data or data.get("data") is None:
                return pd.DataFrame()

            klines = data["data"].get("klines", [])
            rows = []
            for line in klines:
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                rows.append({
                    "date": parts[0],
                    "open": _safe_float(parts[1]),
                    "close": _safe_float(parts[2]),
                    "high": _safe_float(parts[3]),
                    "low": _safe_float(parts[4]),
                    "volume": _safe_float(parts[5]),
                    "amount": _safe_float(parts[6]),
                })

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            # 计算涨跌幅
            df["pct_change"] = df["close"].pct_change() * 100

            return df

        except Exception as e:
            print(f"  ⚠ 指数K线获取失败 {index_code}: {e}")
            return pd.DataFrame()

    def calculate_beta(
        self,
        stock_returns: np.ndarray,
        index_returns: np.ndarray,
    ) -> float:
        """
        计算Beta系数

        Beta = Cov(stock, index) / Var(index)

        Args:
            stock_returns: 个股日收益率序列
            index_returns: 指数日收益率序列

        Returns:
            float: Beta系数
        """
        if len(stock_returns) < 20 or len(index_returns) < 20:
            return 1.0

        # 对齐长度
        min_len = min(len(stock_returns), len(index_returns))
        stock_returns = stock_returns[-min_len:]
        index_returns = index_returns[-min_len:]

        # 去除NaN
        mask = ~(np.isnan(stock_returns) | np.isnan(index_returns))
        stock_returns = stock_returns[mask]
        index_returns = index_returns[mask]

        if len(stock_returns) < 20:
            return 1.0

        cov = np.cov(stock_returns, index_returns)
        var_index = cov[1, 1]

        if var_index < 1e-10:
            return 1.0

        beta = cov[0, 1] / var_index
        return round(float(beta), 4)

    def calculate_correlation(
        self,
        stock_returns: np.ndarray,
        index_returns: np.ndarray,
    ) -> float:
        """计算相关系数"""
        if len(stock_returns) < 10 or len(index_returns) < 10:
            return 0.0

        min_len = min(len(stock_returns), len(index_returns))
        sr = stock_returns[-min_len:]
        ir = index_returns[-min_len:]

        mask = ~(np.isnan(sr) | np.isnan(ir))
        if mask.sum() < 10:
            return 0.0

        corr = np.corrcoef(sr[mask], ir[mask])[0, 1]
        return round(float(corr), 4)

    def detect_inverse(
        self,
        stock_returns: np.ndarray,
        index_returns: np.ndarray,
        recent_days: int = 10,
    ) -> float:
        """
        检测逆势强度

        在大盘下跌的日子里，个股上涨的天数占比越高，逆势越强。

        Args:
            stock_returns: 个股日收益率
            index_returns: 指数日收益率
            recent_days: 检测最近N天

        Returns:
            float: 逆势强度评分 (0~100)
        """
        min_len = min(len(stock_returns), len(index_returns), recent_days)
        if min_len < 5:
            return 0.0

        sr = stock_returns[-min_len:]
        ir = index_returns[-min_len:]

        mask = ~(np.isnan(sr) | np.isnan(ir))
        sr = sr[mask]
        ir = ir[mask]

        if len(sr) < 3:
            return 0.0

        # 大盘跌的天
        index_down_days = ir < -0.5
        if index_down_days.sum() == 0:
            return 0.0  # 大盘没跌，无法判断

        # 大盘跌的日子里个股涨的天数
        inverse_days = (index_down_days & (sr > 0)).sum()
        inverse_ratio = inverse_days / index_down_days.sum()

        # 额外加权：大盘跌得越多个股涨得越多
        weighted_score = 0
        for i in range(len(sr)):
            if ir[i] < -0.5 and sr[i] > 0:
                weighted_score += abs(sr[i]) * abs(ir[i])

        score = inverse_ratio * 70 + min(weighted_score, 30)
        return round(min(score, 100), 1)

    def detect_leader(
        self,
        stock_returns: np.ndarray,
        index_returns: np.ndarray,
    ) -> float:
        """
        检测领涨强度

        在大盘见底反弹时，个股是否率先上涨。

        Returns:
            float: 领涨强度评分 (0~100)
        """
        if len(stock_returns) < 20 or len(index_returns) < 20:
            return 0.0

        min_len = min(len(stock_returns), len(index_returns))
        sr = stock_returns[-min_len:]
        ir = index_returns[-min_len:]

        mask = ~(np.isnan(sr) | np.isnan(ir))
        sr = sr[mask]
        ir = ir[mask]

        if len(sr) < 10:
            return 0.0

        # 寻找大盘拐点（连续下跌后开始反弹）
        index_pcts = ir
        leader_score = 0
        leader_count = 0

        for i in range(3, len(index_pcts) - 3):
            # 检测是否为局部低点（前后3天内最低）
            local_min = min(index_pcts[i - 3:i])
            if index_pcts[i] <= local_min and index_pcts[i] < 0:
                # 大盘在此处见底，检查个股是否提前反弹
                # 检查前2天个股是否已开始上涨
                pre_stock = sr[i - 2:i]
                post_stock = sr[i:i + 3]

                if len(pre_stock) > 0 and np.mean(pre_stock) > 0:
                    leader_count += 1
                    # 个股提前于大盘反弹
                    if len(post_stock) > 0 and np.mean(post_stock) > 0:
                        leader_score += 20  # 既提前又持续
                    else:
                        leader_score += 10

        return round(min(leader_score, 100), 1)

    def analyze(
        self,
        code: str,
        stock_kline: pd.DataFrame,
        index_kline: pd.DataFrame,
    ) -> Dict[str, Any]:
        """
        综合分析个股与大盘的联动关系

        Args:
            code: 股票代码
            stock_kline: 个股K线数据（需含close列）
            index_kline: 大盘K线数据（需含close列）

        Returns:
            dict: 联动分析结果
        """
        default_result = {
            "beta": 1.0,
            "correlation": 0.0,
            "inverse_score": 0.0,
            "leader_score": 0.0,
            "market_coupling": "正常联动",
            "success": False,
        }

        try:
            if stock_kline is None or stock_kline.empty:
                return default_result
            if index_kline is None or index_kline.empty:
                return default_result

            # 计算日收益率
            stock_returns = stock_kline["close"].pct_change().dropna().values * 100
            index_returns = index_kline["close"].pct_change().dropna().values * 100

            beta = self.calculate_beta(stock_returns, index_returns)
            correlation = self.calculate_correlation(stock_returns, index_returns)
            inverse_score = self.detect_inverse(stock_returns, index_returns)
            leader_score = self.detect_leader(stock_returns, index_returns)

            # 判断联动类型
            if inverse_score > 60:
                coupling = "强逆势"
            elif inverse_score > 40:
                coupling = "弱逆势"
            elif beta > 1.5:
                coupling = "高Beta领涨"
            elif beta < 0.5:
                coupling = "低Beta防守"
            elif correlation > 0.8:
                coupling = "高度联动"
            elif correlation > 0.5:
                coupling = "正常联动"
            else:
                coupling = "独立走势"

            return {
                "beta": round(beta, 4),
                "correlation": round(correlation, 4),
                "inverse_score": round(inverse_score, 1),
                "leader_score": round(leader_score, 1),
                "market_coupling": coupling,
                "success": True,
            }

        except Exception as e:
            print(f"  ⚠ 大盘联动分析失败 {code}: {e}")
            return default_result

    def close(self):
        self.session.close()


# ====================================================================== #
#  模块6: EnhancedFalseBreakout — 增强假突破检测
# ====================================================================== #

class EnhancedFalseBreakout:
    """
    增强假突破检测模块

    在原有假突破检测基础上增加：
        - 突破时成交量确认（放量突破更可信）
        - 缩量回踩 vs 放量跌回区分
        - "洗盘"识别：突破后缩量回落不破均线
        - 多次突破同一前高的累积效应

    输出：
        - breakout_type: "真突破" / "假突破" / "洗盘" / "无突破"
        - breakout_score: 评分 (0~100，越高越可信)
        - volume_confirm: 量能确认 (True/False)
        - pullback_type: 回踩类型

    使用示例：
        efb = EnhancedFalseBreakout()
        result = efb.analyze(kline_df)
    """

    def analyze(
        self,
        kline_df: pd.DataFrame,
        lookback_days: int = 60,
        resistance_window: int = 20,
    ) -> Dict[str, Any]:
        """
        分析突破行为

        Args:
            kline_df: K线数据 (需含 open, high, low, close, volume 列)
            lookback_days: 回看天数
            resistance_window: 阻力位计算窗口

        Returns:
            dict: 突破分析结果
        """
        default_result = {
            "breakout_type": "无突破",
            "breakout_score": 0.0,
            "volume_confirm": False,
            "pullback_type": "无",
            "resistance_price": 0.0,
            "breakout_count": 0,
            "success": False,
        }

        try:
            if kline_df is None or len(kline_df) < lookback_days:
                return default_result

            df = kline_df.tail(lookback_days).copy().reset_index(drop=True)
            close = df["close"].values.astype(float)
            high = df["high"].values.astype(float)
            low = df["low"].values.astype(float)
            volume = df["volume"].values.astype(float)

            current_price = close[-1]
            current_volume = volume[-1]

            # ---- 寻找阻力位（前高）----
            resistance_prices = self._find_resistance_levels(high, close, resistance_window)
            if not resistance_prices:
                return default_result

            # 取最接近当前价的阻力位
            nearest_resistance = min(resistance_prices, key=lambda x: abs(x - current_price))

            # ---- 判断是否突破 ----
            # 突破条件：收盘价站上阻力位，且近期曾触及过阻力位
            was_at_resistance = any(
                abs(high[i] - nearest_resistance) / max(nearest_resistance, 0.01) < 0.02
                for i in range(-30, -1)
            )

            if not was_at_resistance:
                default_result["resistance_price"] = nearest_resistance
                return default_result

            breakout_pct = (current_price - nearest_resistance) / max(nearest_resistance, 0.01) * 100

            if breakout_pct < 0:
                # 未突破
                default_result["resistance_price"] = nearest_resistance
                default_result["pullback_type"] = "跌回阻力位下方"
                return default_result

            # ---- 量能确认 ----
            avg_volume_20 = np.mean(volume[-20:])
            volume_ratio = current_volume / max(avg_volume_20, 1)
            volume_confirm = volume_ratio > 1.3  # 放量30%以上算放量突破

            # ---- 多次突破检测 ----
            breakout_count = 0
            for i in range(len(high) - 10, len(high)):
                if high[i] > nearest_resistance * 1.01:
                    breakout_count += 1

            # ---- 回踩分析 ----
            # 检查突破后是否有回踩
            recent_high = max(high[-5:])
            pullback_from_high = (recent_high - current_price) / max(recent_high, 0.01) * 100

            # 均线位置
            ma20 = np.mean(close[-20:]) if len(close) >= 20 else current_price

            pullback_type = "无回踩"
            if pullback_from_high > 2:  # 从近期高点回落超过2%
                if volume_ratio < 0.8:
                    # 缩量回落
                    if current_price > ma20:
                        pullback_type = "缩量洗盘"
                    else:
                        pullback_type = "缩量回踩"
                else:
                    if current_price < nearest_resistance:
                        pullback_type = "放量跌回"
                    else:
                        pullback_type = "放量回踩"

            # ---- 综合评分 ----
            score = 0.0

            # 基础分：是否站稳阻力位
            if breakout_pct > 3:
                score += 30
            elif breakout_pct > 1:
                score += 20
            elif breakout_pct > 0:
                score += 10

            # 量能确认加分
            if volume_confirm:
                score += 25
            elif volume_ratio > 1.0:
                score += 10

            # 多次突破加分（确认有效突破）
            if breakout_count >= 3:
                score += 15
            elif breakout_count >= 2:
                score += 10

            # 回踩类型调整
            if pullback_type == "缩量洗盘":
                score += 20  # 洗盘是好事
            elif pullback_type == "缩量回踩":
                score += 10
            elif pullback_type == "放量跌回":
                score -= 30  # 假突破信号
            elif pullback_type == "放量回踩":
                score -= 10

            # 判断突破类型
            if score >= 60 and volume_confirm:
                breakout_type = "真突破"
            elif pullback_type == "放量跌回" or score < 30:
                breakout_type = "假突破"
            elif pullback_type == "缩量洗盘":
                breakout_type = "洗盘"
            elif breakout_pct > 0:
                breakout_type = "待确认突破"
            else:
                breakout_type = "无突破"

            score = max(0, min(100, score))

            return {
                "breakout_type": breakout_type,
                "breakout_score": round(score, 1),
                "volume_confirm": volume_confirm,
                "pullback_type": pullback_type,
                "resistance_price": round(nearest_resistance, 2),
                "breakout_count": breakout_count,
                "breakout_pct": round(breakout_pct, 2),
                "volume_ratio": round(volume_ratio, 2),
                "success": True,
            }

        except Exception as e:
            print(f"  ⚠ 增强假突破分析异常: {e}")
            return default_result

    def _find_resistance_levels(
        self,
        high: np.ndarray,
        close: np.ndarray,
        window: int = 20,
    ) -> List[float]:
        """
        寻找阻力位（前高）

        通过局部极大值法寻找阻力位。

        Args:
            high: 最高价序列
            close: 收盘价序列
            window: 搜索窗口

        Returns:
            list: 阻力位价格列表
        """
        levels = []
        n = len(high)

        for i in range(window, n - 5):
            # 检查是否为局部高点
            local_max = max(high[i - window:i + 1])
            if high[i] >= local_max * 0.99:
                # 检查是否与已有阻力位接近（合并相近的阻力位）
                is_new = True
                for j, existing in enumerate(levels):
                    if abs(high[i] - existing) / max(existing, 0.01) < 0.03:
                        # 取平均值
                        levels[j] = (high[i] + existing) / 2
                        is_new = False
                        break
                if is_new:
                    levels.append(float(high[i]))

        # 只保留最近的有效阻力位
        if len(levels) > 5:
            # 取离当前价最近的5个
            current = close[-1]
            levels.sort(key=lambda x: abs(x - current))
            levels = levels[:5]

        return levels


# ====================================================================== #
#  模块7: MarketPhase — 主力行为阶段识别
# ====================================================================== #

class MarketPhase:
    """
    主力行为阶段识别模块

    识别四个阶段：
        - 吸筹期: 长期横盘+间歇性放量+筹码集中
        - 拉升期: 放量突破+均线多头+量价齐升
        - 派发期: 高位放量震荡+长上影线+换手率飙升
        - 下跌期: 缩量阴跌+均线空头

    需要60日以上K线数据。

    输出：
        - phase: 当前阶段 (吸筹/拉升/派发/下跌)
        - phase_score: 阶段评分 (0~100)
        - phase_detail: 阶段细节分析
        - signal_modifier: 信号修正系数（同样的信号在不同阶段含义不同）

    使用示例：
        mp = MarketPhase()
        result = mp.analyze(kline_df)
    """

    def analyze(self, kline_df: pd.DataFrame) -> Dict[str, Any]:
        """
        分析主力行为阶段

        Args:
            kline_df: K线数据 (需含 open, high, low, close, volume 列)

        Returns:
            dict: 阶段分析结果
        """
        default_result = {
            "phase": "未知",
            "phase_score": 0.0,
            "phase_detail": {},
            "signal_modifier": 1.0,
            "success": False,
        }

        try:
            if kline_df is None or len(kline_df) < 60:
                return default_result

            df = kline_df.tail(120).copy().reset_index(drop=True)
            close = df["close"].values.astype(float)
            high = df["high"].values.astype(float)
            low = df["low"].values.astype(float)
            volume = df["volume"].values.astype(float)
            opens = df["open"].values.astype(float)

            # ---- 计算技术指标 ----
            ma5 = pd.Series(close).rolling(5).mean().values
            ma10 = pd.Series(close).rolling(10).mean().values
            ma20 = pd.Series(close).rolling(20).mean().values
            ma60 = pd.Series(close).rolling(60).mean().values if len(close) >= 60 else None

            current_price = close[-1]
            avg_vol_20 = np.mean(volume[-20:])
            avg_vol_60 = np.mean(volume[-60:]) if len(volume) >= 60 else avg_vol_20

            # ---- 横盘度检测 ----
            # 用60日价格变异系数(CV)来衡量
            if len(close) >= 60:
                cv_60 = np.std(close[-60:]) / np.mean(close[-60:])
            else:
                cv_60 = np.std(close) / np.mean(close)

            is_sideways = cv_60 < 0.08  # 价格变异系数<8%算横盘

            # ---- 均线多头/空头排列 ----
            ma_bull = (ma5[-1] > ma10[-1] > ma20[-1]) if not np.isnan(ma20[-1]) else False
            ma_bear = (ma5[-1] < ma10[-1] < ma20[-1]) if not np.isnan(ma20[-1]) else False

            # ---- 量能分析 ----
            # 间歇性放量：是否有突然的成交量高峰
            vol_spikes = 0
            for i in range(-60, 0):
                if i + len(volume) >= 0:
                    idx = i + len(volume)
                    if idx > 0 and volume[idx] > avg_vol_20 * 2:
                        vol_spikes += 1

            # ---- 上影线检测 ----
            upper_shadow_ratio = 0
            if len(high) >= 5:
                for i in range(-5, 0):
                    body = abs(close[i] - opens[i])
                    shadow = high[i] - max(close[i], opens[i])
                    if body > 0:
                        upper_shadow_ratio += shadow / body
                upper_shadow_ratio /= 5

            # ---- 趋势方向 ----
            if len(close) >= 20:
                trend_20 = (close[-1] - close[-20]) / max(close[-20], 0.01) * 100
            else:
                trend_20 = 0

            if len(close) >= 60:
                trend_60 = (close[-1] - close[-60]) / max(close[-60], 0.01) * 100
            else:
                trend_60 = 0

            # ---- 阶段判定 ----
            scores = {
                "吸筹": 0,
                "拉升": 0,
                "派发": 0,
                "下跌": 0,
            }

            # 吸筹期特征
            if is_sideways:
                scores["吸筹"] += 30
            if vol_spikes >= 3 and is_sideways:
                scores["吸筹"] += 20
            if not ma_bull and not ma_bear:
                scores["吸筹"] += 10
            if abs(trend_60) < 10:
                scores["吸筹"] += 10

            # 拉升期特征
            if ma_bull:
                scores["拉升"] += 30
            if trend_20 > 10:
                scores["拉升"] += 20
            if volume[-1] > avg_vol_20 * 1.5 and close[-1] > close[-2]:
                scores["拉升"] += 20
            if trend_60 > 15:
                scores["拉升"] += 10

            # 派发期特征
            if not is_sideways and abs(trend_20) < 5 and trend_60 > 20:
                scores["派发"] += 20  # 高位横盘
            if upper_shadow_ratio > 2:
                scores["派发"] += 25  # 长上影线
            if volume[-1] > avg_vol_60 * 2 and close[-1] < close[-2]:
                scores["派发"] += 20  # 高位放量阴线
            if trend_60 > 30 and trend_20 < 5:
                scores["派发"] += 15  # 高位滞涨

            # 下跌期特征
            if ma_bear:
                scores["下跌"] += 30
            if trend_20 < -10:
                scores["下跌"] += 20
            if volume[-1] < avg_vol_20 * 0.7 and close[-1] < close[-2]:
                scores["下跌"] += 15  # 缩量阴跌
            if trend_60 < -15:
                scores["下跌"] += 10

            # 确定主要阶段
            phase = max(scores, key=scores.get)
            phase_score = scores[phase]

            # 如果分数太低，说明阶段不明确
            if phase_score < 20:
                phase = "过渡期"

            # 信号修正系数
            signal_modifiers = {
                "吸筹": 1.1,   # 吸筹期突破信号更值得重视
                "拉升": 1.0,   # 正常
                "派发": 0.6,   # 派发期所有信号打折
                "下跌": 0.5,   # 下跌期大幅打折
                "过渡期": 0.8,
            }
            signal_modifier = signal_modifiers.get(phase, 1.0)

            return {
                "phase": phase,
                "phase_score": round(phase_score, 1),
                "phase_detail": {
                    "cv_60": round(cv_60, 4),
                    "is_sideways": is_sideways,
                    "ma_bull": ma_bull,
                    "ma_bear": ma_bear,
                    "vol_spikes": vol_spikes,
                    "upper_shadow_ratio": round(upper_shadow_ratio, 2),
                    "trend_20": round(trend_20, 2),
                    "trend_60": round(trend_60, 2),
                    "all_scores": {k: round(v, 1) for k, v in scores.items()},
                },
                "signal_modifier": round(signal_modifier, 2),
                "success": True,
            }

        except Exception as e:
            print(f"  ⚠ 主力阶段分析异常: {e}")
            return default_result


# ====================================================================== #
#  模块8: NonLinearScoring — 非线性评分系统
# ====================================================================== #

class NonLinearScoring:
    """
    非线性评分系统

    核心理念：
        - 一票否决制：某些信号组合直接排除
        - 核心信号乘法放大
        - 矛盾信号惩罚
        - 情绪周期乘数

    使用示例：
        scorer = NonLinearScoring()
        result = scorer.compute(base_score, signals, emotion_stage, aggression)
    """

    def compute(
        self,
        base_score: float,
        signals: Dict[str, Any],
        emotion_stage: str = "震荡",
        aggression: float = 0.6,
        market_phase: str = "未知",
        market_phase_modifier: float = 1.0,
    ) -> Dict[str, Any]:
        """
        非线性评分计算

        Args:
            base_score: 基础分数（来自原有评分系统）
            signals: 各模块的信号字典，包含以下可能的key：
                - is_leader: 是否龙头
                - divergence_consensus: 分歧转一致信号 (BUY/SELL/NONE)
                - volume_price: 量价关系 (背离/齐升/缩量等)
                - follower_penalty: 是否跟风股
                - broken_rate: 炸板率
                - board_height: 连板高度
                - capital_flow_quality: 资金流质量
                - breakout_type: 突破类型
                - inverse_score: 逆势强度
            emotion_stage: 情绪阶段
            aggression: 情绪攻击系数
            market_phase: 主力阶段
            market_phase_modifier: 阶段修正系数

        Returns:
            dict: 评分结果
        """
        score = base_score
        adjustments = []  # 记录所有调整
        veto = False
        veto_reason = ""

        # ========== 一票否决 ==========
        # 组合1: 跟风 + 分歧 + 缩量 → 直接排除
        is_follower = signals.get("follower_penalty", False)
        is_divergence_sell = signals.get("divergence_consensus", "") == "SELL"
        is_volume_shrink = signals.get("volume_price", "") in ("缩量", "量价背离")

        if is_follower and is_divergence_sell and is_volume_shrink:
            veto = True
            veto_reason = "跟风+分歧卖出+缩量 → 一票否决"

        # 组合2: 派发期 + 高炸板率 + 跟风
        if market_phase == "派发" and signals.get("broken_rate", 0) > 0.4 and is_follower:
            veto = True
            veto_reason = "派发期+高炸板+跟风 → 一票否决"

        # 组合3: 下跌期 + 假突破
        if market_phase == "下跌" and signals.get("breakout_type") == "假突破":
            veto = True
            veto_reason = "下跌期+假突破 → 一票否决"

        if veto:
            return {
                "final_score": 0,
                "adjusted_score": 0,
                "adjustments": [veto_reason],
                "veto": True,
                "veto_reason": veto_reason,
                "multiplier": 0,
            }

        # ========== 核心信号乘法放大 ==========

        multiplier = 1.0

        # 信号1: 龙头 + 分歧转一致 → 1.5x
        is_leader = signals.get("is_leader", False)
        dc_signal = signals.get("divergence_consensus", "")
        if is_leader and dc_signal == "BUY":
            multiplier *= 1.5
            adjustments.append("龙头+分歧转一致 ×1.5")

        # 信号2: 资金流质量高 + 量价齐升 → 1.3x
        flow_quality = signals.get("capital_flow_quality", 0)
        vp_relation = signals.get("volume_price", "")
        if flow_quality > 70 and vp_relation == "量价齐升":
            multiplier *= 1.3
            adjustments.append("优质资金流+量价齐升 ×1.3")

        # 信号3: 真突破 + 放量 → 1.3x
        if signals.get("breakout_type") == "真突破" and signals.get("volume_confirm", False):
            multiplier *= 1.3
            adjustments.append("真突破+放量确认 ×1.3")

        # 信号4: 逆势走强 + 龙头 → 1.2x
        inverse = signals.get("inverse_score", 0)
        if inverse > 50 and is_leader:
            multiplier *= 1.2
            adjustments.append("逆势龙头 ×1.2")

        # ========== 矛盾信号惩罚 ==========

        # 惩罚1: 相对强度强但量价背离 → 0.7x
        rs_strong = signals.get("rs_score", 0) > 70
        if rs_strong and vp_relation == "量价背离":
            multiplier *= 0.7
            adjustments.append("强度高但量价背离 ×0.7")

        # 惩罚2: 资金流入但跟风 → 0.8x
        if flow_quality > 50 and is_follower:
            multiplier *= 0.8
            adjustments.append("资金流入但跟风 ×0.8")

        # 惩罚3: 吸筹期的突破可信度低（除非放量）
        if market_phase == "吸筹" and signals.get("breakout_type") in ("待确认突破",):
            multiplier *= 0.85
            adjustments.append("吸筹期未确认突破 ×0.85")

        # 惩罚4: 高炸板率环境
        broken_rate = signals.get("broken_rate", 0)
        if broken_rate > 0.5:
            multiplier *= 0.7
            adjustments.append(f"高炸板率({broken_rate:.0%}) ×0.7")
        elif broken_rate > 0.3:
            multiplier *= 0.85
            adjustments.append(f"中炸板率({broken_rate:.0%}) ×0.85")

        # ========== 情绪周期乘数 ==========

        # 情绪乘数表
        emotion_multipliers = {
            "冰点": 0.6,
            "退潮": 0.75,
            "震荡": 0.9,
            "回暖": 1.1,
            "高潮": 0.85,  # 高潮期反而要谨慎
        }
        emotion_mult = emotion_multipliers.get(emotion_stage, 1.0)

        # 用aggression进一步调整
        emotion_mult = emotion_mult * (0.5 + aggression * 0.5)
        multiplier *= emotion_mult
        adjustments.append(f"情绪周期({emotion_stage},agg={aggression}) ×{emotion_mult:.2f}")

        # ========== 市场阶段乘数 ==========
        multiplier *= market_phase_modifier
        if market_phase_modifier != 1.0:
            adjustments.append(f"市场阶段({market_phase}) ×{market_phase_modifier:.2f}")

        # ========== 最终计算 ==========
        adjusted_score = score * multiplier
        final_score = max(0, min(100, adjusted_score))

        return {
            "final_score": round(final_score, 1),
            "adjusted_score": round(adjusted_score, 1),
            "base_score": round(score, 1),
            "multiplier": round(multiplier, 4),
            "adjustments": adjustments,
            "veto": False,
            "veto_reason": "",
        }


# ====================================================================== #
#  模块9: Backtester — 选股回测验证
# ====================================================================== #

class Backtester:
    """
    选股回测验证模块

    功能：
        - 输入选出的股票列表 + 日期
        - 获取T+1到T+5的实际表现
        - 计算：胜率、平均收益、最大回撤、盈亏比
        - 按模块归因：哪些模块选出的股票表现最好

    使用示例：
        bt = Backtester()
        report = bt.run(selected_stocks, "2024-01-15")
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })

    def _get_future_kline(self, code: str, from_date: str, days: int = 10) -> pd.DataFrame:
        """
        获取指定日期之后的K线数据

        Args:
            code: 股票代码
            from_date: 起始日期 (YYYY-MM-DD)
            days: 获取天数

        Returns:
            DataFrame: K线数据
        """
        try:
            secid = _code_to_secid(code)
            url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": "1",
                "end": "20500101",
                "lmt": str(days + 10),  # 多获取一些
                "cb": "",
            }

            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if not data or data.get("data") is None:
                return pd.DataFrame()

            klines = data["data"].get("klines", [])
            rows = []
            for line in klines:
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                rows.append({
                    "date": parts[0],
                    "open": _safe_float(parts[1]),
                    "close": _safe_float(parts[2]),
                    "high": _safe_float(parts[3]),
                    "low": _safe_float(parts[4]),
                    "volume": _safe_float(parts[5]),
                    "amount": _safe_float(parts[6]),
                })

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])

            # 筛选from_date之后的数据
            from_dt = pd.to_datetime(from_date)
            df = df[df["date"] > from_dt].head(days)

            return df.reset_index(drop=True)

        except Exception as e:
            print(f"  ⚠ 回测数据获取失败 {code}: {e}")
            return pd.DataFrame()

    def _calculate_max_drawdown(self, prices: List[float]) -> float:
        """
        计算最大回撤

        Args:
            prices: 价格序列

        Returns:
            float: 最大回撤百分比（正数）
        """
        if not prices or len(prices) < 2:
            return 0.0

        peak = prices[0]
        max_dd = 0.0

        for p in prices:
            if p > peak:
                peak = p
            dd = (peak - p) / max(peak, 0.01) * 100
            if dd > max_dd:
                max_dd = dd

        return round(max_dd, 2)

    def run(
        self,
        selected_stocks: List[Dict[str, Any]],
        select_date: str,
        hold_days: int = 5,
    ) -> Dict[str, Any]:
        """
        运行回测

        Args:
            selected_stocks: 选出的股票列表，每项为字典:
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "score": 85.0,
                    "select_reason": "龙头+分歧转一致",
                    "modules": ["board", "dc", "capital_flow"],  # 哪些模块贡献
                }
            select_date: 选股日期 (YYYY-MM-DD)
            hold_days: 持有天数（T+1到T+N）

        Returns:
            dict: 回测报告
        """
        print(f"\n  📈 回测开始: 选股日期={select_date}, 持有天数={hold_days}")
        print(f"  📊 共 {len(selected_stocks)} 只股票")

        stock_results = []
        module_performance = {}  # {模块名: [收益列表]}

        for i, stock_info in enumerate(selected_stocks):
            code = stock_info.get("code", "")
            name = stock_info.get("name", "")
            score = stock_info.get("score", 0)
            modules = stock_info.get("modules", [])

            time.sleep(0.2)  # 速率控制

            future_df = self._get_future_kline(code, select_date, hold_days)
            if future_df.empty:
                print(f"  ⚠ {code} {name} 无后续数据")
                continue

            # 获取选股日的收盘价（作为买入参考价）
            # 实际应该是T+1开盘价买入
            buy_price = future_df.iloc[0]["open"] if len(future_df) > 0 else 0
            if buy_price <= 0:
                continue

            # T+1 到 T+N 的每日收益
            daily_returns = []
            close_prices = future_df["close"].values.tolist()
            for cp in close_prices:
                ret = (cp - buy_price) / buy_price * 100
                daily_returns.append(round(ret, 2))

            # 最终收益
            final_return = daily_returns[-1] if daily_returns else 0

            # 最大回撤
            price_series = [buy_price] + close_prices
            max_dd = self._calculate_max_drawdown(price_series)

            # 记录
            stock_result = {
                "code": code,
                "name": name,
                "score": score,
                "buy_price": round(buy_price, 2),
                "final_return": round(final_return, 2),
                "max_drawdown": max_dd,
                "daily_returns": daily_returns,
                "modules": modules,
                "select_reason": stock_info.get("select_reason", ""),
            }
            stock_results.append(stock_result)

            # 按模块归因
            for mod in modules:
                if mod not in module_performance:
                    module_performance[mod] = []
                module_performance[mod].append(final_return)

            if (i + 1) % 20 == 0:
                print(f"  📊 回测进度: {i + 1}/{len(selected_stocks)}")

        # ---- 汇总统计 ----
        if not stock_results:
            return {
                "total_stocks": 0,
                "error": "无有效回测数据",
            }

        returns = [r["final_return"] for r in stock_results]
        win_count = sum(1 for r in returns if r > 0)
        lose_count = sum(1 for r in returns if r < 0)
        win_rate = win_count / max(len(returns), 1)

        avg_return = np.mean(returns)
        avg_win = np.mean([r for r in returns if r > 0]) if win_count > 0 else 0
        avg_lose = np.mean([r for r in returns if r < 0]) if lose_count > 0 else 0

        # 盈亏比
        profit_loss_ratio = abs(avg_win / avg_lose) if avg_lose != 0 else float("inf")

        max_dd_all = max(r["max_drawdown"] for r in stock_results)

        # 按模块归因统计
        module_stats = {}
        for mod, rets in module_performance.items():
            mod_returns = np.array(rets)
            module_stats[mod] = {
                "count": len(rets),
                "avg_return": round(float(np.mean(mod_returns)), 2),
                "win_rate": round(sum(1 for r in rets if r > 0) / max(len(rets), 1), 4),
                "best_return": round(float(np.max(mod_returns)), 2),
                "worst_return": round(float(np.min(mod_returns)), 2),
            }

        # 按分数段统计
        score_bands = {
            "high (>80)": [r for r in stock_results if r["score"] > 80],
            "medium (60-80)": [r for r in stock_results if 60 <= r["score"] <= 80],
            "low (<60)": [r for r in stock_results if r["score"] < 60],
        }
        score_band_stats = {}
        for band_name, band_stocks in score_bands.items():
            if band_stocks:
                band_returns = [s["final_return"] for s in band_stocks]
                score_band_stats[band_name] = {
                    "count": len(band_stocks),
                    "avg_return": round(float(np.mean(band_returns)), 2),
                    "win_rate": round(sum(1 for r in band_returns if r > 0) / len(band_returns), 4),
                }

        # 排序：按收益从高到低
        stock_results.sort(key=lambda x: x["final_return"], reverse=True)

        report = {
            "select_date": select_date,
            "hold_days": hold_days,
            "total_stocks": len(stock_results),
            "win_count": win_count,
            "lose_count": lose_count,
            "win_rate": round(win_rate, 4),
            "avg_return": round(float(avg_return), 2),
            "avg_win": round(float(avg_win), 2),
            "avg_lose": round(float(avg_lose), 2),
            "profit_loss_ratio": round(float(min(profit_loss_ratio, 99)), 2),
            "max_drawdown": max_dd_all,
            "best_stock": stock_results[0] if stock_results else {},
            "worst_stock": stock_results[-1] if stock_results else {},
            "module_attribution": module_stats,
            "score_band_stats": score_band_stats,
            "stock_details": stock_results,
        }

        # 打印报告
        self._print_report(report)

        return report

    def _print_report(self, report: Dict):
        """打印回测报告"""
        print(f"\n  {'=' * 50}")
        print(f"  📈 回测报告")
        print(f"  {'=' * 50}")
        print(f"  选股日期: {report['select_date']}")
        print(f"  持有天数: T+1 ~ T+{report['hold_days']}")
        print(f"  股票数量: {report['total_stocks']}")
        print(f"  胜率: {report['win_rate']:.1%} ({report['win_count']}胜/{report['lose_count']}负)")
        print(f"  平均收益: {report['avg_return']:+.2f}%")
        print(f"  平均盈利: {report['avg_win']:+.2f}%")
        print(f"  平均亏损: {report['avg_lose']:+.2f}%")
        print(f"  盈亏比: {report['profit_loss_ratio']:.2f}")
        print(f"  最大回撤: {report['max_drawdown']:.2f}%")

        if report.get("best_stock"):
            best = report["best_stock"]
            print(f"  最佳: {best['code']} {best['name']} {best['final_return']:+.2f}%")

        if report.get("worst_stock"):
            worst = report["worst_stock"]
            print(f"  最差: {worst['code']} {worst['name']} {worst['final_return']:+.2f}%")

        if report.get("module_attribution"):
            print(f"\n  📊 模块归因:")
            sorted_modules = sorted(
                report["module_attribution"].items(),
                key=lambda x: x[1]["avg_return"],
                reverse=True,
            )
            for mod, stats in sorted_modules:
                print(f"    {mod}: 均收{stats['avg_return']:+.2f}% "
                      f"胜率{stats['win_rate']:.0%} ({stats['count']}只)")

        print(f"  {'=' * 50}\n")

    def close(self):
        self.session.close()


# ====================================================================== #
#  V5Integration — 集成到现有StockScanner
# ====================================================================== #

class V5Integration:
    """
    v5.0 模块集成器

    展示如何将9大新增模块集成到现有StockScanner的_do_run方法中。

    使用方式：
        # 在StockScanner.__init__中
        self.v5 = V5Integration()

        # 在_do_run方法中，原有分析之后
        v5_results = self.v5.run_all(candidates, stock_list, emotion, market, code_concepts, zt_codes)

        # v5_results 包含所有模块结果，可用于最终评分

    集成顺序：
        1. RealCapitalFlow — 替代VWAP估算（先执行，后续模块可能依赖）
        2. EnhancedEmotionCycle — 增强情绪（先执行，评分系统需要）
        3. SectorRotationV2 — 板块轮动（不需要K线）
        4. MarketPhase — 主力阶段（需要K线，批量执行）
        5. EnhancedFalseBreakout — 增强假突破（需要K线）
        6. MarketCorrelation — 大盘联动（需要K线+指数K线）
        7. IntradayAnalysis — 分时分析（可选，API请求较多）
        8. NonLinearScoring — 非线性评分（最后执行，汇总所有信号）
        9. Backtester — 回测（可选，用于验证）
    """

    def __init__(self, enable_intraday: bool = False, enable_backtest: bool = False):
        """
        初始化v5集成器

        Args:
            enable_intraday: 是否启用分时分析（每只股票一次API请求，较慢）
            enable_backtest: 是否在筛选后自动回测
        """
        self.capital_flow = RealCapitalFlow()
        self.emotion_v2 = EnhancedEmotionCycle()
        self.sector_rotation = SectorRotationV2()
        self.market_phase = MarketPhase()
        self.false_breakout = EnhancedFalseBreakout()
        self.market_corr = MarketCorrelation()
        self.intraday = IntradayAnalysis() if enable_intraday else None
        self.backtester = Backtester() if enable_backtest else None
        self.nonlinear = NonLinearScoring()

        self.enable_intraday = enable_intraday
        self.enable_backtest = enable_backtest

        # 缓存
        self._index_kline_cache = {}

    def run_all(
        self,
        candidates: list,
        stock_list_df: pd.DataFrame,
        emotion_result: Dict,
        market_result: Dict,
        code_concepts: Dict[str, List[str]],
        zt_codes: set,
        kline_cache: Dict[str, pd.DataFrame] = None,
    ) -> Dict[str, Any]:
        """
        执行所有v5模块分析

        Args:
            candidates: 候选股票列表（StockData对象列表）
            stock_list_df: 全部股票行情DataFrame
            emotion_result: 原有情绪分析结果
            market_result: 原有市场环境结果
            code_concepts: 板块概念映射
            zt_codes: 今日涨停代码集合
            kline_cache: K线缓存 {code: DataFrame}

        Returns:
            dict: 所有v5模块的分析结果
        """
        v5_results = {}

        # ========== 模块1: 真实资金流 ==========
        print("  📊 [V5-1] 获取真实资金流（东方财富API，失败自动VWAP降级）...")
        codes = [getattr(s, "code", "") for s in candidates]
        # 构建行情数据字典，用于东财API失败时的VWAP估算降级
        quotes_dict = {}
        for s in candidates:
            code = getattr(s, "code", "")
            quotes_dict[code] = {
                "close": getattr(s, "close", 0),
                "amount": getattr(s, "amount", 0),
                "volume_ratio": getattr(s, "volume_ratio", 0),
                "pct_change": getattr(s, "pct_change", 0),
                "turnover": getattr(s, "turnover", 0),
            }
        flow_data = self.capital_flow.get_batch(codes, quotes_dict=quotes_dict)
        v5_results["capital_flow"] = flow_data

        # ========== 模块2: 增强情绪周期 ==========
        print("  🌡️ [V5-2] 增强情绪周期分析...")
        today_lu = len(zt_codes)
        today_ld = market_result.get("limit_down", 0)
        # 注意：炸板数需要从外部传入或计算，这里用默认值
        emotion_v2_result = self.emotion_v2.analyze(
            all_stocks=stock_list_df.to_dict("records") if hasattr(stock_list_df, "to_dict") else candidates,
            today_limit_up_count=today_lu,
            today_limit_down_count=today_ld,
            today_broken_limit_count=emotion_result.get("broken_count", 0),
            consecutive_board_height=emotion_result.get("board_height", 0),
            limit_down_history=emotion_result.get("limit_down_history", None),
        )
        v5_results["emotion_v2"] = emotion_v2_result
        print(f"  ✅ 情绪阶段: {emotion_v2_result['stage']} "
              f"攻击系数: {emotion_v2_result['aggression']:.2f} "
              f"炸板率: {emotion_v2_result['broken_rate']:.1%}")

        # ========== 模块4: 板块轮动节奏 ==========
        print("  🔄 [V5-4] 板块轮动分析...")
        rotation_result = self.sector_rotation.analyze(
            candidates, code_concepts,
            yesterday_hot_sectors=None,  # 需要外部传入昨日数据
        )
        v5_results["sector_rotation"] = rotation_result
        print(f"  ✅ 轮动阶段: {rotation_result['rotation_phase']} "
              f"主线: {rotation_result['main_sector']}")

        # ========== 模块5/6/7: 需要K线的分析 ==========
        print("  📈 [V5-5/6/7] K线深度分析（大盘联动/假突破/主力阶段）...")

        # 获取指数K线（缓存）
        if "000001" not in self._index_kline_cache:
            self._index_kline_cache["000001"] = self.market_corr.get_index_kline("000001", 120)
            time.sleep(0.3)
        if "399001" not in self._index_kline_cache:
            self._index_kline_cache["399001"] = self.market_corr.get_index_kline("399001", 120)

        index_kline = self._index_kline_cache.get("000001")

        stock_v5_details = {}
        for i, s in enumerate(candidates):
            code = getattr(s, "code", "")
            kline = kline_cache.get(code) if kline_cache else None

            detail = {}

            # 真实资金流
            detail["capital_flow"] = flow_data.get(code, {})

            # 主力阶段
            if kline is not None and len(kline) >= 60:
                detail["market_phase"] = self.market_phase.analyze(kline)

            # 增强假突破
            if kline is not None and len(kline) >= 60:
                detail["false_breakout"] = self.false_breakout.analyze(kline)

            # 大盘联动
            if kline is not None and index_kline is not None and len(index_kline) > 20:
                detail["market_corr"] = self.market_corr.analyze(code, kline, index_kline)

            # 分时分析（可选，较慢）
            if self.intraday and self.enable_intraday:
                detail["intraday"] = self.intraday.analyze(code)
                time.sleep(0.15)

            stock_v5_details[code] = detail

            if (i + 1) % 50 == 0:
                print(f"  📊 K线分析进度: {i + 1}/{len(candidates)}")

        v5_results["stock_details"] = stock_v5_details
        print(f"  ✅ K线深度分析完成")

        # ========== 模块8: 非线性评分 ==========
        print("  🎯 [V5-8] 非线性评分计算...")
        v5_results["scoring_inputs"] = {
            "emotion_stage": emotion_v2_result["stage"],
            "aggression": emotion_v2_result["aggression"],
            "rotation_phase": rotation_result["rotation_phase"],
            "broken_rate": emotion_v2_result["broken_rate"],
            "board_height": emotion_v2_result["consecutive_board_height"],
        }

        return v5_results

    def score_single_stock(
        self,
        base_score: float,
        code: str,
        v5_results: Dict[str, Any],
        enhanced_signals: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        对单只股票进行v5非线性评分

        Args:
            base_score: 原有系统评分
            code: 股票代码
            v5_results: run_all返回的v5结果
            enhanced_signals: 原有增强模块的信号

        Returns:
            dict: 包含最终分数和调整详情
        """
        stock_detail = v5_results.get("stock_details", {}).get(code, {})
        scoring_inputs = v5_results.get("scoring_inputs", {})

        # 构建信号字典
        signals = {}

        # 从原有系统提取
        if enhanced_signals:
            signals["is_leader"] = enhanced_signals.get("board", {}).get("is_leader_candidate", False)
            signals["divergence_consensus"] = enhanced_signals.get("dc", {}).get("signal", "NONE")
            signals["follower_penalty"] = enhanced_signals.get("follower", {}).get("is_follower", False)
            signals["volume_price"] = enhanced_signals.get("vp", {}).get("relation", "")
            signals["rs_score"] = enhanced_signals.get("rs", {}).get("score", 0)

        # 从v5模块提取
        cap_flow = stock_detail.get("capital_flow", {})
        if cap_flow.get("success"):
            signals["capital_flow_quality"] = cap_flow.get("flow_quality_score", 0)

        fb = stock_detail.get("false_breakout", {})
        if fb.get("success"):
            signals["breakout_type"] = fb.get("breakout_type", "")
            signals["volume_confirm"] = fb.get("volume_confirm", False)

        corr = stock_detail.get("market_corr", {})
        if corr.get("success"):
            signals["inverse_score"] = corr.get("inverse_score", 0)

        mp = stock_detail.get("market_phase", {})
        market_phase = mp.get("phase", "未知") if mp.get("success") else "未知"
        market_phase_modifier = mp.get("signal_modifier", 1.0) if mp.get("success") else 1.0

        signals["broken_rate"] = scoring_inputs.get("broken_rate", 0)
        signals["board_height"] = scoring_inputs.get("board_height", 0)

        # 执行非线性评分
        result = self.nonlinear.compute(
            base_score=base_score,
            signals=signals,
            emotion_stage=scoring_inputs.get("emotion_stage", "震荡"),
            aggression=scoring_inputs.get("aggression", 0.6),
            market_phase=market_phase,
            market_phase_modifier=market_phase_modifier,
        )

        # 添加各模块原始结果
        result["v5_detail"] = stock_detail
        result["signals_used"] = signals

        return result

    def run_backtest(
        self,
        selected_stocks: List[Dict[str, Any]],
        select_date: str,
        hold_days: int = 5,
    ) -> Dict[str, Any]:
        """
        运行回测验证

        Args:
            selected_stocks: 选出的股票列表
            select_date: 选股日期
            hold_days: 持有天数

        Returns:
            dict: 回测报告
        """
        if not self.backtester:
            print("  ⚠ 回测模块未启用")
            return {}
        return self.backtester.run(selected_stocks, select_date, hold_days)

    def close(self):
        """关闭所有会话"""
        self.capital_flow.close()
        self.market_corr.close()
        if self.intraday:
            self.intraday.close()
        if self.backtester:
            self.backtester.close()


# ====================================================================== #
#  集成示例代码
# ====================================================================== #

"""
以下展示如何在现有StockScanner._do_run方法中集成v5模块。

在原有代码的适当位置插入以下代码段即可完成集成。

# ============================================================
# 在 StockScanner.__init__ 中添加：
# ============================================================

    # v5.0: 初始化集成器
    self.v5 = V5Integration(enable_intraday=False, enable_backtest=False)

# ============================================================
# 在 _do_run 方法中，原有 Step 6 深度分析之后添加：
# ============================================================

    # ---- v5.0: 新增9大模块 ----
    print(colored("\n  🚀 v5.0: 执行9大增强模块...", C.CYAN))
    v5_results = self.v5.run_all(
        candidates=candidates,
        stock_list_df=stock_list,
        emotion_result=emotion,
        market_result=market,
        code_concepts=code_concepts,
        zt_codes=zt_codes,
        kline_cache=self.kline_cache,
    )

    # 更新情绪数据（使用v5增强版）
    emotion_v2 = v5_results.get("emotion_v2", {})
    aggression = emotion_v2.get("aggression", emotion.get("aggression", 0.6))

    # ---- 对每只候选股进行v5非线性评分 ----
    for s, r1, r2 in results:
        code = s.code
        original_score = r1.final_score

        v5_score_result = self.v5.score_single_stock(
            base_score=original_score,
            code=code,
            v5_results=v5_results,
            enhanced_signals=s.enhanced,
        )

        # 如果被一票否决
        if v5_score_result.get("veto"):
            r1.final_score = 0
            r1.adjust_reason += f" [V5否决: {v5_score_result['veto_reason']}]"
        else:
            # 应用v5调整后的分数
            r1.final_score = v5_score_result["final_score"]
            if v5_score_result["multiplier"] != 1.0:
                r1.adjust_reason += f" [V5乘数: ×{v5_score_result['multiplier']:.2f}]"

        # 存储v5详情到enhanced
        s.enhanced["v5"] = v5_score_result.get("v5_detail", {})
        s.enhanced["v5_score"] = v5_score_result

    # ---- v5板块轮动信息输出 ----
    rotation = v5_results.get("sector_rotation", {})
    if rotation.get("today_hot_sectors"):
        print(colored(f"\n  🔄 板块轮动: {rotation['rotation_phase']} — {rotation['rotation_direction']}", C.CYAN))
        for name, info in rotation["today_hot_sectors"][:3]:
            print(f"    {name}: 热度{info['heat_score']:.0f} 涨停{info['limit_ups']} 均涨{info['avg_pct']:+.2f}%")

    # ---- 输出v5新增指标汇总 ----
    print(colored(f"\n  📊 V5增强指标汇总:", C.CYAN))
    print(f"    情绪阶段: {emotion_v2.get('stage', '?')}  "
          f"攻击系数: {aggression:.2f}  "
          f"炸板率: {emotion_v2.get('broken_rate', 0):.1%}")
    print(f"    板块轮动: {rotation.get('rotation_phase', '?')}  "
          f"主线: {rotation.get('main_sector', '?')}")

    # 统计v5否决和加分情况
    veto_count = sum(1 for s, _, _ in results if s.enhanced.get("v5_score", {}).get("veto", False))
    boosted = sum(1 for s, _, _ in results
                  if not s.enhanced.get("v5_score", {}).get("veto", False)
                  and s.enhanced.get("v5_score", {}).get("multiplier", 1.0) > 1.1)
    print(f"    V5否决: {veto_count}只  V5大幅加分(×1.1+): {boosted}只")

# ============================================================
# 在 StockScanner.close() 中添加：
# ============================================================

    self.v5.close()
"""


# ====================================================================== #
#  快速测试入口
# ====================================================================== #

def _quick_test():
    """快速测试各模块基本功能"""
    print("=" * 60)
    print("  V5 模块快速测试")
    print("=" * 60)

    # 测试1: 资金流
    print("\n[1] RealCapitalFlow 测试...")
    flow = RealCapitalFlow()
    result = flow.get_single("600519")
    if result["success"]:
        print(f"  ✅ 茅台主力净流入: {result['main_net_inflow']:.0f}万  质量分: {result['flow_quality_score']:.1f}")
    else:
        print(f"  ⚠ 资金流获取失败（可能是非交易时间）")
    flow.close()

    # 测试2: 增强情绪
    print("\n[2] EnhancedEmotionCycle 测试...")
    emotion = EnhancedEmotionCycle()
    result = emotion.analyze(
        all_stocks=[],
        today_limit_up_count=45,
        today_limit_down_count=8,
        today_broken_limit_count=12,
        consecutive_board_height=5,
        limit_down_history=[3, 5, 6, 8, 8],
    )
    print(f"  ✅ 情绪: {result['stage']}  攻击系数: {result['aggression']:.2f}  "
          f"炸板率: {result['broken_rate']:.1%}")

    # 测试3: 增强假突破
    print("\n[3] EnhancedFalseBreakout 测试...")
    # 构造测试数据
    np.random.seed(42)
    prices = 10 + np.cumsum(np.random.randn(100) * 0.2)
    test_df = pd.DataFrame({
        "open": prices + np.random.randn(100) * 0.1,
        "high": prices + abs(np.random.randn(100) * 0.3),
        "low": prices - abs(np.random.randn(100) * 0.3),
        "close": prices,
        "volume": np.random.randint(10000, 100000, 100).astype(float),
    })
    efb = EnhancedFalseBreakout()
    result = efb.analyze(test_df)
    print(f"  ✅ 突破类型: {result['breakout_type']}  评分: {result['breakout_score']:.1f}")

    # 测试4: 主力阶段
    print("\n[4] MarketPhase 测试...")
    mp = MarketPhase()
    result = mp.analyze(test_df)
    print(f"  ✅ 阶段: {result['phase']}  评分: {result['phase_score']:.1f}  "
          f"信号修正: {result['signal_modifier']:.2f}")

    # 测试5: 非线性评分
    print("\n[5] NonLinearScoring 测试...")
    scorer = NonLinearScoring()
    result = scorer.compute(
        base_score=75,
        signals={
            "is_leader": True,
            "divergence_consensus": "BUY",
            "volume_price": "量价齐升",
            "capital_flow_quality": 80,
            "breakout_type": "真突破",
            "volume_confirm": True,
            "follower_penalty": False,
            "broken_rate": 0.2,
            "inverse_score": 30,
            "rs_score": 65,
        },
        emotion_stage="回暖",
        aggression=0.8,
        market_phase="拉升",
        market_phase_modifier=1.0,
    )
    print(f"  ✅ 基础分: 75  最终分: {result['final_score']:.1f}  "
          f"乘数: ×{result['multiplier']:.2f}  否决: {result['veto']}")
    for adj in result["adjustments"]:
        print(f"    - {adj}")

    print("\n" + "=" * 60)
    print("  测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    _quick_test()
