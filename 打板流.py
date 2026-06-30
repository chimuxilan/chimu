#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
尾盘拉升选股器 v3.3 — 频次=每条线+1
======================================
v3.3 变更:
  ★ 频次含义: 每进入一个板块/概念 → 频次+1, 行业与概念平等计数
  ★ 去掉 ×2 / ×3 加权, total_freq = sector_count + concept_count
  保留 v3.2 全部加速: 线程池并行 + pickle缓存 + 滑动限频
"""

import json, os, sys, time, traceback, webbrowser, pickle, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

import requests

# ================================================================
# 依赖加载
# ================================================================
try:
    from mootdx.quotes import Quotes
    from pytdx.hq import TdxHq_API
    HAS_TDX = True
except ImportError:
    Quotes = None; TdxHq_API = None; HAS_TDX = False
    print("[依赖] pytdx 未安装，实时行情不可用")

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    ak = None; HAS_AKSHARE = False
    print("[依赖] akshare 未安装")

try:
    from thsdk import THSClient as _THSClient
    HAS_THS = True
except ImportError:
    _THSClient = None; HAS_THS = False
    print("[依赖] thsdk 未安装，同花顺数据不可用")


# ================================================================
# ★ 全局限频(线程安全) + 本地缓存
# ================================================================
_cache_lock = threading.Lock()
_last_ts = 0.0
_ts_lock = threading.Lock()
AKSHARE_MIN_GAP = 0.8  # 线程池模式下 0.8s 即可安全并行


def _akshare_throttle():
    """线程安全滑动限频，保证全局两次调用间隔 ≥ AKSHARE_MIN_GAP 秒"""
    global _last_ts
    wait = 0
    with _ts_lock:
        now = time.time()
        wait = AKSHARE_MIN_GAP - (now - _last_ts)
        if wait > 0:
            _last_ts = now + wait  # 预占时间槽，允许其他线程提前计算
        else:
            _last_ts = now
    if wait > 0:
        time.sleep(wait)  # 在锁外 sleep，不阻塞其他线程


def akshare_call(func, *args, max_retries=3, **kwargs):
    """带限频 + 指数退避重试的 akshare 调用"""
    for attempt in range(max_retries):
        try:
            _akshare_throttle()
            return func(*args, **kwargs)
        except Exception as e:
            wait = 2 ** attempt * 1.5
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                print(f"  → akshare 失败({e})，跳过")
                return None
    return None


# ---------- 本地 pickle 缓存 ----------
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


def _cache_path(tag: str) -> str:
    return os.path.join(_CACHE_DIR, f"{tag}_{date.today().isoformat()}.pkl")


def cache_load(tag: str, ttl_hours: int = 12):
    """加载当天缓存(存在且未过期则返回)"""
    path = _cache_path(tag)
    if not os.path.exists(path):
        return None
    try:
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if age_h > ttl_hours:
            return None
        with open(path, "rb") as f:
            data = pickle.load(f)
        print(f"  → [缓存命中] {tag} ({age_h*60:.0f}分钟前)")
        return data
    except Exception:
        return None


def cache_save(tag: str, data):
    """保存到当天缓存"""
    try:
        with open(_cache_path(tag), "wb") as f:
            pickle.dump(data, f, protocol=4)
    except Exception:
        pass


# ================================================================
# 通达信实时行情
# ================================================================
class TdxQuote:
    def __init__(self):
        self.api = TdxHq_API() if (HAS_TDX and TdxHq_API is not None) else None
        self.connected = False

    def connect(self):
        if not HAS_TDX or self.api is None:
            return
        try:
            self.api.connect("119.147.212.81", 7709)
            self.connected = True; print("[mootdx] 已连接")
        except Exception as e:
            self.connected = False; print(f"[mootdx] 连接失败: {e}")

    def get_quotes(self, codes: List[str]) -> Dict:
        if not self.connected or self.api is None:
            return {}
        try:
            params = [(1 if c.startswith(("6", "9")) else 0, c) for c in codes]
            data = self.api.get_security_quotes(params)
            if not data:
                return {}
            result = {}
            for d in data:
                code = d.get("code", "")
                result[code] = {
                    "open": d.get("open", 0), "high": d.get("high", 0),
                    "low": d.get("low", 0), "price": d.get("price", 0),
                    "volume": d.get("vol", 0) / 100, "amount": d.get("amount", 0),
                    "bid1": d.get("bid1", 0), "bid1_vol": d.get("bid_vol1", 0),
                    "ask1": d.get("ask1", 0), "ask1_vol": d.get("ask_vol1", 0),
                }
            return result
        except Exception as e:
            print(f"[mootdx] 获取行情失败: {e}")
            return {}

    def disconnect(self):
        if self.api is not None and self.connected:
            try: self.api.disconnect()
            except Exception: pass


tdx = TdxQuote()


# ================================================================
# 同花顺
# ================================================================
class THSClientWrapper:
    def __init__(self):
        self.client = None; self.connected = False
        if HAS_THS and _THSClient is not None:
            try:
                self.client = _THSClient(); self.connected = True
                print("[thsdk] 已连接")
            except Exception as e:
                print(f"[thsdk] 连接失败: {e}")

    def _safe_call(self, func):
        if not self.connected or self.client is None:
            return {}
        try:
            result = func()
            return result if result else {}
        except Exception:
            return {}

    def get_fund_flow(self, code):
        return self._safe_call(lambda: self.client.get_fund_flow(stock_code=code))
    def get_extended(self, code):
        return self._safe_call(lambda: self.client.get_extended(stock_code=code))
    def get_big_orders(self, code):
        return self._safe_call(lambda: self.client.get_big_orders(stock_code=code))


ths = THSClientWrapper()


# ================================================================
# ★★★ 实时达标信号（截图逻辑）
# ================================================================
def _calc_expma(closes: List[float], period: int) -> float:
    """计算 EXPMA（指数移动平均线）"""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2.0 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)


def _calc_mid_strength(df_hist) -> float:
    """
    中期强度指标: 综合量价关系、趋势惯性、波动率
    正值=中期偏强, 负值=中期偏弱
    """
    if df_hist is None or len(df_hist) < 10:
        return 0.0
    closes = df_hist["收盘"].tolist()
    volumes = df_hist["成交量"].tolist()

    # ① 趋势惯性: 近5日涨幅 vs 近20日涨幅
    if len(closes) >= 20:
        short_chg = (closes[-1] - closes[-6]) / closes[-6] * 100
        long_chg  = (closes[-1] - closes[-20]) / closes[-20] * 100
        trend = (short_chg - long_chg) * 2  # 短期加速为正
    else:
        trend = 0.0

    # ② 量价配合: 近5日量比
    if len(volumes) >= 10:
        recent_vol = sum(volumes[-5:]) / 5
        avg_vol    = sum(volumes[-10:]) / 10
        vol_ratio  = (recent_vol / avg_vol - 1) * 20 if avg_vol > 0 else 0
    else:
        vol_ratio = 0.0

    # ③ 波动率收敛: 近5日振幅 / 近20日振幅
    if len(closes) >= 20:
        recent_range = (max(closes[-5:]) - min(closes[-5:])) / min(closes[-5:]) * 100
        avg_range    = (max(closes[-20:]) - min(closes[-20:])) / min(closes[-20:]) * 100
        vol_converge = (avg_range - recent_range) * 1.5  # 收敛为正(蓄势)
    else:
        vol_converge = 0.0

    strength = trend + vol_ratio + vol_converge
    return round(strength, 2)


def build_limit_signal(code: str, stock: dict, bid_m: dict,
                        freq_info: dict, df_hist=None) -> dict:
    """
    构建达标信号字典（截图逻辑）
    当股票涨幅接近涨停时生成完整信号
    """
    cur_price = bid_m.get("price", 0) or bid_m.get("open", 0)
    pre_close = bid_m.get("pre_close", 0)
    if pre_close <= 0:
        return {}

    change_pct = (cur_price - pre_close) / pre_close * 100

    # 主力净流入(万) — 优先用同花顺, 否则从 bid1 盘口估算
    main_net_wan = 0.0
    if HAS_THS:
        fund = ths.get_fund_flow(code)
        if fund:
            main_net_wan = fund.get("main_net_inflow", 0) / 10000
    if main_net_wan == 0:
        bid_vol = bid_m.get("bid1_vol", 0)
        ask_vol = bid_m.get("ask1_vol", 0)
        if bid_vol + ask_vol > 0:
            main_net_wan = (bid_vol - ask_vol) / (bid_vol + ask_vol) * 1000  # 粗略估算

    # EXPMA
    expma10 = expma13 = 0.0
    if df_hist is not None and len(df_hist) >= 13:
        closes = df_hist["收盘"].tolist()
        expma10 = _calc_expma(closes, 10)
        expma13 = _calc_expma(closes, 13)

    # 中期强度
    mid_strength = _calc_mid_strength(df_hist)

    # 近5日平均成交额(万)
    avg_trade = 0.0
    if df_hist is not None and len(df_hist) >= 5:
        avg_trade = round(df_hist["成交额"].tail(5).mean() / 10000, 3)

    # 所属概念(取前3个)
    concepts = freq_info.get("concepts", [])
    concept_str = ",".join(concepts[:3]) if concepts else ""

    return {
        "first_time": datetime.now().strftime("%H:%M:%S"),
        "达标": {
            "code": code,
            "name": stock.get("name", ""),
            "price": round(cur_price, 2),
            "change_pct": round(change_pct, 4),
            "main_net_wan": round(main_net_wan, 3),
            "mid_strength": mid_strength,
            "concept": concept_str,
            "expma10": expma10,
            "expma13": expma13,
            "avg_trade": avg_trade,
        },
    }


def print_limit_signal(signal: dict):
    """终端打印信号（涨停标★，其他标●）"""
    if not signal:
        return
    d = signal["达标"]
    expma_tag = "↑" if d["expma10"] > d["expma13"] else "↓"
    ms_tag    = "强" if d["mid_strength"] > 0 else "弱"
    is_limit  = d["change_pct"] >= 9.5
    marker    = "★" if is_limit else "●"
    print(f"  {marker} [{signal['first_time']}] "
          f"{d['code']} {d['name']} "
          f"价格={d['price']:.2f} 涨幅={d['change_pct']:.2f}% "
          f"主力={d['main_net_wan']:+.0f}万 "
          f"EXPMA({d['expma10']:.2f}/{d['expma13']:.2f}){expma_tag} "
          f"中期={ms_tag}({d['mid_strength']:+.2f}) "
          f"5日均额={d['avg_trade']:.0f}万 "
          f"概念={d['concept'] or '无'}")


# ================================================================
# 钱龙经典指标
# ================================================================
class QianlongClassic:
    @staticmethod
    def kdj(high, low, close, n=9, m1=3, m2=3):
        if high == low: return 50.0, 50.0, 50.0
        rsv = (close - low) / (high - low) * 100
        k = rsv / m1 + 50 * (1 - 1 / m1)
        d = k / m2 + 50 * (1 - 1 / m2)
        return k, d, 3 * k - 2 * d

    @staticmethod
    def rsi6(closes):
        if len(closes) < 7: return 50.0
        gains = losses = 0.0
        for i in range(-6, 0):
            diff = closes[i] - closes[i - 1]
            if diff > 0: gains += diff
            else: losses -= diff
        avg = (gains + losses) / 6
        return (gains / 6 / avg * 100) if avg > 0 else 50.0


ql = QianlongClassic()


# ================================================================
# ★★★ 板块/概念频次 — 逐股查归属 + pickle 缓存
# ================================================================
def _fetch_boards_for_stock(code: str) -> Tuple[List[str], List[str]]:
    """
    调用东财 RPT_F10_CORETHEME_BOARDTYPE 接口，获取单只股票的全部板块归属。
    返回 (sectors: 行业列表, concepts: 概念列表)
    """
    import requests as _req
    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    params = {
        "sortColumns": "SECURITY_CODE",
        "sortTypes": "1",
        "pageSize": "200",
        "pageNumber": "1",
        "reportName": "RPT_F10_CORETHEME_BOARDTYPE",
        "columns": "ALL",
        "quoteColumns": "",
        "filter": f'(SECURITY_CODE="{code}")',
        "source": "HSF10",
        "client": "PC",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://emweb.securities.eastmoney.com/",
    }
    sectors, concepts = [], []
    for attempt in range(3):
        try:
            _akshare_throttle()
            r = _req.get(url, params=params, headers=headers, timeout=15)
            data = r.json()
            if not data.get("result") or not data["result"].get("data"):
                return sectors, concepts
            for b in data["result"]["data"]:
                bt = b.get("BOARD_TYPE", "")
                name = b.get("BOARD_NAME", "")
                if not name:
                    continue
                if bt == "行业":
                    sectors.append(name)
                elif bt == "板块":
                    pass  # 地域板块，跳过
                else:
                    concepts.append(name)
            return sectors, concepts
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt * 1.5)
            else:
                print(f"  → 个股板块查询失败({code}): {e}")
    return sectors, concepts


def _batch_stock_board_scan(
    pool_codes: List[str], max_workers: int = 2,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """
    逐股查询板块归属（替代原来的逐板块扫描）。
    返回 (sector_hits, concept_hits):
      sector_hits  = { code: [行业名, ...] }
      concept_hits = { code: [概念名, ...] }
    """
    sector_hits  = {c: [] for c in pool_codes}
    concept_hits = {c: [] for c in pool_codes}
    total = len(pool_codes)
    done = [0]

    def worker(code):
        sectors, concepts = _fetch_boards_for_stock(code)
        with _cache_lock:
            done[0] += 1
            d = done[0]
        if d % 10 == 0 or d == total:
            print(f"    → 个股板块 {d}/{total}...", end="\r")
        return code, sectors, concepts

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(worker, c): c for c in pool_codes}
        for future in as_completed(futures):
            try:
                code, sectors, concepts = future.result()
                sector_hits[code]  = sectors
                concept_hits[code] = concepts
            except Exception:
                pass

    print(f"    → 个股板块查询完成 {total} 只      ")
    return sector_hits, concept_hits


def analyze_sector_concept_frequency(pool_codes: List[str]) -> Dict[str, Dict]:
    """
    ★ 频次 = 股票出现在多少个板块/概念中, 每进一个 +1, 不加权
    total_freq = sector_count + concept_count
    """
    result = {}
    for c in pool_codes:
        result[c] = {"sector_count": 0, "concept_count": 0,
                      "total_freq": 0, "sectors": [], "concepts": []}
    if not HAS_AKSHARE or not pool_codes:
        return result

    code_set = set(pool_codes)

    # ① 尝试缓存
    cached = cache_load("freq")
    if cached and isinstance(cached, dict) and cached.get("code_set") == code_set:
        print("[频次] 使用本地缓存, 跳过 API 拉取")
        cached_data = cached.get("data", {})
        for c in pool_codes:
            if c in cached_data:
                result[c] = cached_data[c]
        _print_freq_preview(result, pool_codes)
        return result

    # ② ★ 逐股查询板块归属（替代原来的逐板块扫描）
    print(f"  → 逐股查询板块归属 ({len(pool_codes)} 只, 限频{AKSHARE_MIN_GAP}s)...")
    sector_hits, concept_hits = _batch_stock_board_scan(pool_codes, max_workers=2)

    for c in pool_codes:
        result[c]["sectors"]       = sector_hits.get(c, [])
        result[c]["sector_count"]  = len(result[c]["sectors"])
        result[c]["concepts"]      = concept_hits.get(c, [])
        result[c]["concept_count"] = len(result[c]["concepts"])
        result[c]["total_freq"]    = result[c]["sector_count"] + result[c]["concept_count"]

    # ③ 写入缓存
    cache_payload = {"code_set": code_set, "data": {c: result[c] for c in pool_codes}}
    cache_save("freq", cache_payload)

    _print_freq_preview(result, pool_codes)
    return result


def _print_freq_preview(result, pool_codes):
    ranked = sorted(
        [(c, result[c]) for c in pool_codes],
        key=lambda x: x[1]["total_freq"], reverse=True,
    )
    print("[频次] Top-10 (每条线+1):")
    for code, info in ranked[:10]:
        if info["total_freq"] == 0: continue
        print(f"  {code}  频次={info['total_freq']:>3d}条线  "
              f"(行业{info['sector_count']} + 概念{info['concept_count']})  "
              f"行业:{','.join(info['sectors'][:3])}  "
              f"概念:{','.join(info['concepts'][:3])}")


# ================================================================
# 缓存版 akshare 封装(涨停池/快照/市场概况 一次拉, 全程复用)
# ================================================================
_df_spot_cache = None
_df_zt_cache = None


def get_spot_df():
    global _df_spot_cache
    if _df_spot_cache is not None:
        return _df_spot_cache
    _df_spot_cache = cache_load("spot", ttl_hours=0.5)
    if _df_spot_cache is not None:
        return _df_spot_cache
    df = akshare_call(ak.stock_zh_a_spot_em)
    if df is not None and not df.empty:
        _df_spot_cache = df
        cache_save("spot", df)
    return _df_spot_cache


def get_zt_df():
    global _df_zt_cache
    if _df_zt_cache is not None:
        return _df_zt_cache
    _df_zt_cache = cache_load("zt", ttl_hours=0.5)
    if _df_zt_cache is not None:
        return _df_zt_cache
    df = akshare_call(ak.stock_zt_pool_em, date=datetime.now().strftime("%Y%m%d"))
    if df is not None:
        _df_zt_cache = df
        cache_save("zt", df)
    return _df_zt_cache


def fetch_bid_metrics_from_akshare() -> dict:
    df = get_spot_df()
    if df is None or df.empty:
        return {}
    result = {}
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).zfill(6)
        pre_close = float(row.get("昨收", 0) or 0)
        if pre_close <= 0: continue
        result[code] = {
            "open": float(row.get("今开", 0) or 0), "pre_close": pre_close,
            "price": float(row.get("最新价", 0) or 0),
            "high": float(row.get("最高", 0) or 0),
            "low": float(row.get("最低", 0) or 0),
            "volume": int(float(row.get("成交量", 0) or 0)),
            "amount": float(row.get("成交额", 0) or 0),
            "bid1": 0, "bid1_vol": 0, "ask1": 0, "ask1_vol": 0,
        }
    return result


# ================================================================
# 工具函数
# ================================================================
def calc_db_price(pre_close: float) -> float:
    return round(pre_close * 1.097, 2)


def is_limit_up(code, cur, pre_close):
    if pre_close <= 0: return False
    return cur >= round(pre_close * 1.1, 2) or (cur - pre_close) / pre_close * 100 > 9.97


def check_limit_up_continuation(code, pre_close, cur):
    if pre_close <= 0: return False
    return cur >= round(pre_close * 1.1, 2)


def _code_to_tdx(code):
    c = code.zfill(6)
    return f"1{c}" if c.startswith(("6", "9")) else f"0{c}"


# ================================================================
# 否决
# ================================================================
def check_hard_reject(gap, vol, jzb, is_limit_up=False):
    now = datetime.now()
    is_pre = (now.hour == 9 and now.minute < 30) or now.hour < 9
    if not is_pre and vol < 0.5:
        return True, f"量太小({vol:.2f}万手)"
    if gap < -1.0:
        return True, f"价格向下(gap={gap:+.2f}%)"
    if not is_pre:
        if jzb > 35: return True, f"竞价占比异常({jzb:.1f}%)"
        if gap < 0.5 and not is_limit_up and jzb < 3: return True, f"虚假信号"
        if gap > 1.0 and vol < 5: return True, f"薄里拉价"
        if jzb < 2: return True, f"无人气(jzb={jzb:.1f}%)"
    return False, "通过"


def check_soft_reject(code, pre_close, open_p, cur, vol, stock, jzb):
    if pre_close <= 0 or open_p <= 0: return True, "开盘价或昨收为0"
    gap = (cur - pre_close) / pre_close * 100
    prev_vol = stock.get("volume", 0) / 10000
    db_price = calc_db_price(pre_close)
    if cur > db_price and gap > 3 and vol < 15:
        return True, f"超过打板价({cur:.2f}>{db_price:.2f})"
    if gap > 5 and vol < 5:
        return True, f"开盘过于激进(gap={gap:+.1f}%)"
    if cur < open_p * 0.99 and vol > prev_vol * 0.7:
        return True, f"快速回落"
    if jzb < 8 and gap < 3.5:
        return True, f"弱势开盘(jzb={jzb:.1f}%)"
    if vol > 80 and jzb < 15:
        return True, f"异常放量"
    return False, "通过"


# ================================================================
# 三维度打分
# ================================================================
def score_momentum(code, stock):
    s, notes = 0, []
    cur = stock.get("price", 0)
    pre_close = stock.get("pre_close", 0)
    if pre_close > 0:
        pct = (cur - pre_close) / pre_close * 100
        if pct > 5: s += 20; notes.append(f"强势(涨{pct:.1f}%)")
        elif pct > 3: s += 15; notes.append(f"偏强(涨{pct:.1f}%)")
        elif pct > 1: s += 10; notes.append(f"温和(涨{pct:.1f}%)")
        elif pct < 0: s -= 10; notes.append(f"弱势(跌{pct:.1f}%)")
    vol = stock.get("volume", 0) / 10000
    if vol > 20: s += 15; notes.append(f"放量({vol:.1f}万)")
    elif vol > 5: s += 8; notes.append(f"适度量({vol:.1f}万)")
    return s, "|".join(notes)


def score_market_temperature():
    zt_count = 0
    df_zt = get_zt_df()
    if df_zt is not None:
        zt_count = len(df_zt)
    df_spot = get_spot_df()
    if df_spot is None or len(df_spot) == 0:
        return 50, "无数据", {"zt_count": 0, "dt_count": 0, "up_count": 0, "down_count": 0, "temp": "未知"}
    up_count = int((df_spot["涨跌幅"] > 0).sum())
    down_count = int((df_spot["涨跌幅"] < 0).sum())
    total = up_count + down_count
    ratio = up_count / total if total > 0 else 0.5
    df_dt = akshare_call(ak.stock_zt_pool_dtgc_em, date=datetime.now().strftime("%Y%m%d"))
    dt_count = len(df_dt) if df_dt is not None else 0
    s = 50 + (zt_count - 50) * 0.5 + (ratio - 0.5) * 40 - dt_count * 1.5
    s = max(0, min(100, s))
    temp = "过热" if s > 70 else ("冰点" if s < 20 else "中性")
    return s, f"涨停{zt_count}家/跌停{dt_count}家", {
        "zt_count": zt_count, "dt_count": dt_count,
        "up_count": up_count, "down_count": down_count, "temp": temp,
    }


def score_sentiment(code, stock, market_temp, market_info, jzb):
    s, notes = 0, []
    if market_temp > 65: s += 15; notes.append("情绪偏暖")
    elif market_temp < 35: s -= 10; notes.append("情绪偏冷")
    else: notes.append("情绪中性")
    if market_info.get("zt_count", 0) > 70:
        s -= 15; notes.append(f"过热(涨停{market_info['zt_count']}家)")
    if jzb > 25: s += 15; notes.append(f"主力积极(jzb={jzb:.1f}%)")
    elif jzb > 15: s += 10
    return s, "|".join(notes)


def score_valuation_and_ths(code, stock):
    s, notes = 0, []
    pe = stock.get("pe", 0)
    vol_ratio = stock.get("volume_ratio", 0)
    turnover = stock.get("turnover_rate", 0)
    total_mv = stock.get("total_mv", 0)
    if 5 < pe < 30: s += 15; notes.append(f"估值合理(PE={pe:.1f})")
    elif pe <= 0: s -= 5; notes.append(f"PE为负({pe:.1f})")
    else: notes.append(f"PE={pe:.1f}")
    if total_mv > 5e9: s += 8; notes.append("市值充裕")
    if vol_ratio > 2.0 and turnover > 3:
        s += 12; notes.append(f"量能活跃({vol_ratio:.1f}倍)")
    elif vol_ratio > 1.5: s += 6
    ths_score, ths_note = _ths_supplement(code, stock)
    s += ths_score; notes.append(ths_note)
    extra = {"ths_fund_score": ths_score, "ths_fund_note": ths_note,
             "fund_flow": ths.get_fund_flow(code),
             "extended": ths.get_extended(code),
             "big_orders": ths.get_big_orders(code)}
    return s, "|".join(notes), extra


def _ths_supplement(code, stock):
    fund = ths.get_fund_flow(code)
    ext = ths.get_extended(code)
    ths_score, ths_note = 0, "THS无数据"
    if fund:
        main_net = fund.get("main_net_inflow", 0)
        if main_net > 5e6: ths_score += 15; ths_note = f"主力净流入{main_net/1e4:.0f}万"
        elif main_net > 1e6: ths_score += 8; ths_note = f"主力小幅流入"
        elif main_net < -5e6: ths_score -= 12; ths_note = f"主力净流出{main_net/1e4:.0f}万"
    elif ext:
        pe_ths = ext.get("pe_ttm", stock.get("pe", 0))
        if 5 < pe_ths < 30: ths_score += 8; ths_note = f"THS估值合理(PE={pe_ths:.1f})"
    return ths_score, ths_note


# ================================================================
# 柔性调节
# ================================================================
def _adjust_for_overheating(s, code, jzb):
    df_zt = get_zt_df()
    zt = len(df_zt) if df_zt is not None else 0
    df_spot = get_spot_df()
    if df_spot is None or df_spot.empty: return s, ""
    row = df_spot[df_spot["代码"] == code.zfill(6)]
    if row.empty: return s, ""
    pct = float(row.iloc[0].get("涨跌幅", 0))
    if pct > 5 and zt > 60 and jzb > 30: return s + 10, f"情绪过热(涨停{zt}家)"
    if pct > 3 and zt > 40 and jzb > 20: return s + 5, "情绪偏暖"
    return s, ""


def _adjust_for_hotspot(s, code, market_info):
    if not HAS_AKSHARE: return s, ""
    try:
        df = akshare_call(ak.stock_board_industry_name_em)
        if df is None or df.empty: return s, ""
        hot_keywords = "电池|储能|光伏|半导体|AI"
        hot_rows = df[df["板块名称"].str.contains(hot_keywords, na=False)]
        if hot_rows.empty: return s, ""
        # 检查股票是否实际属于匹配的热点板块
        for _, row in hot_rows.iterrows():
            board_name = row["板块名称"]
            try:
                members = akshare_call(ak.stock_board_industry_cons_em, symbol=board_name)
                if members is not None and not members.empty:
                    member_codes = set(str(c).zfill(6) for c in members["代码"].tolist())
                    if code.zfill(6) in member_codes:
                        rise = float(row.get("板块指数", 0) or 0)
                        if rise > 0:
                            return s + 8, f"所在热点板块({board_name})当日上涨"
                        return s, ""
            except Exception:
                continue
        return s, ""
    except Exception:
        return s, ""


def _adjust_for_big_orders(s, code):
    orders = ths.get_big_orders(code)
    if not orders: return s, ""
    net = orders.get("buy_large", 0) - orders.get("sell_large", 0)
    if net > 1e6: return s + 10, f"大单净买入{net/1e4:.0f}万"
    if net < -1e6: return s - 10, f"大单净卖出{net/1e4:.0f}万"
    return s, ""


def _adjust_for_divergence(s, code):
    if not HAS_AKSHARE: return s, ""
    try:
        df = akshare_call(
            ak.stock_zh_a_hist, symbol=code, period="daily",
            start_date=datetime.now().replace(day=1).strftime("%Y%m%d"), adjust="",
        )
        if df is None or len(df) < 5: return s, ""
        diff_sign = df["收盘"].diff().fillna(0).apply(
            lambda x: 1 if x > 0 else (-1 if x < 0 else 0)
        )
        obv = (df["成交量"] * diff_sign).cumsum().iloc[-1]
        obv_ma = df["成交量"].tail(10).mean()
        price = df["收盘"].iloc[-1]
        if price > df["收盘"].iloc[-2] * 1.02 and obv < obv_ma: return s - 15, "出现顶背离信号"
        if price < df["收盘"].iloc[-2] * 0.98 and obv > obv_ma: return s + 10, "出现底背离信号"
        return s, ""
    except Exception:
        return s, ""


# ================================================================
# ★★★ 频次 → 评分: 每条线 +1, 不加权
# ================================================================
def freq_to_score(freq_info: Dict) -> Tuple[float, str]:
    """
    频次 = 股票进入的板块/概念总数, 每进一个板块或概念 +1, 行业与概念平等计数
    """
    sc = freq_info.get("sector_count", 0)
    cc = freq_info.get("concept_count", 0)
    tf = sc + cc  # ★ 简单累加, 每条线 +1

    if tf >= 30: return 40, f"多线龙头(共{tf}条线: {sc}行业+{cc}概念)"
    if tf >= 20: return 30, f"主线丰富(共{tf}条线: {sc}行业+{cc}概念)"
    if tf >= 12: return 20, f"主线较多(共{tf}条线: {sc}行业+{cc}概念)"
    if tf >= 6:  return 10, f"主线适中(共{tf}条线: {sc}行业+{cc}概念)"
    if tf >= 3:  return 5,  f"主线偏少(共{tf}条线: {sc}行业+{cc}概念)"
    return 0,     f"主线极少(共{tf}条线: {sc}行业+{cc}概念)"


# ================================================================
# 综合评分
# ================================================================
def score_tail_auction(code, stock, bid_metrics, market_temp, market_info, freq_info):
    bid_m = bid_metrics.get(code, {})
    if not bid_m or bid_m.get("pre_close", 0) <= 0:
        return -100, "无竞价数据", {}

    pre_close = bid_m["pre_close"]
    open_p = bid_m.get("open", 0)
    cur = bid_m.get("price", 0) or open_p
    vol = bid_m.get("volume", 0) / 10000
    total = bid_m.get("bid1_vol", 0) + bid_m.get("ask1_vol", 0)
    jzb = (bid_m.get("bid1_vol", 0) / total * 100) if total > 0 else 5.0
    limit_up = is_limit_up(code, cur, pre_close)
    gap = (cur - pre_close) / pre_close * 100

    rej, reason = check_hard_reject(gap, vol, jzb, limit_up)
    if rej: return -100, f"硬否决: {reason}", {"reason": reason, "limit_up": limit_up, "jzb": jzb}

    rej2, reason2 = check_soft_reject(code, pre_close, open_p, cur, vol, stock, jzb)
    if rej2: return -80, f"柔性否决: {reason2}", {"reason": reason2, "limit_up": limit_up, "jzb": jzb}

    # 加载历史K线(用于EXPMA/中期强度/均额)
    df_hist = None
    try:
        today_str = datetime.now().strftime("%Y%m%d")
        month_ago = (datetime.now().replace(day=1)).strftime("%Y%m%d")
        df_hist = akshare_call(
            ak.stock_zh_a_hist, symbol=code, period="daily",
            start_date=month_ago, end_date=today_str, adjust="qfq",
        )
    except Exception:
        pass

    s_mom, note_mom = score_momentum(code, stock)
    s_sen, note_sen = score_sentiment(code, stock, market_temp, market_info, jzb)

    # ★ 生成达标信号（每只股票都分析）
    limit_signal = build_limit_signal(code, stock, bid_m, freq_info, df_hist)
    print_limit_signal(limit_signal)

    # ★ 信号加分（EXPMA/中期强度/主力净流入）
    sig_bonus = 0; sig_notes = []
    if limit_signal:
        d = limit_signal.get("达标", {})
        ms = d.get("mid_strength", 0)
        e10 = d.get("expma10", 0)
        e13 = d.get("expma13", 0)
        mnet = d.get("main_net_wan", 0)
        if ms > 5:
            sig_bonus += 10; sig_notes.append(f"中期强势({ms:+.1f})")
        elif ms > 0:
            sig_bonus += 5; sig_notes.append(f"中期偏强({ms:+.1f})")
        elif ms < -10:
            sig_bonus -= 8; sig_notes.append(f"中期弱势({ms:+.1f})")
        if e10 > e13:
            sig_bonus += 5; sig_notes.append("EXPMA多头")
        elif e10 < e13 and e13 > 0:
            sig_bonus -= 3; sig_notes.append("EXPMA空头")
        if mnet > 500:
            sig_bonus += 8; sig_notes.append(f"主力强买({mnet:+.0f}万)")
        elif mnet > 100:
            sig_bonus += 4; sig_notes.append(f"主力买入({mnet:+.0f}万)")
        elif mnet < -500:
            sig_bonus -= 8; sig_notes.append(f"主力卖出({mnet:+.0f}万)")

    s_val, note_val, extra_val = score_valuation_and_ths(code, stock)
    s_freq, note_freq = freq_to_score(freq_info)
    base = s_mom + s_sen + s_val + s_freq

    dragon_bonus = 0; dragon_note = ""
    if check_limit_up_continuation(code, pre_close, cur):
        dragon_bonus += 15; dragon_note = "涨停接力"

    adj = 0; adj_notes = []
    for fn in [_adjust_for_overheating, _adjust_for_hotspot, _adjust_for_big_orders, _adjust_for_divergence]:
        extra_args = (code, jzb) if fn == _adjust_for_overheating else (code, market_info) if fn == _adjust_for_hotspot else (code,)
        a, n = fn(0, *extra_args)
        adj += a
        if n: adj_notes.append(n)

    s = base + dragon_bonus + adj + sig_bonus
    all_notes = (
        f"频次:{note_freq}|动量:{note_mom}|情绪:{note_sen}|价值:{note_val}"
        + (f"|龙头:{dragon_note}" if dragon_note else "")
        + (f"|信号:{'; '.join(sig_notes)}" if sig_notes else "")
        + (f"|调节:{'; '.join(adj_notes)}" if adj_notes else "")
    )
    details = {
        "reason": all_notes, "base": base, "dragon_bonus": dragon_bonus,
        "adjustment": adj, "limit_up": limit_up, "jzb": jzb,
        "freq_score": s_freq, "freq_note": note_freq, **extra_val,
        "limit_signal": limit_signal, "sig_bonus": sig_bonus, "sig_notes": sig_notes,
    }
    return s, all_notes, details


# ================================================================
# HTML 报告
# ================================================================
def generate_html(results, market_temp, market_info, freq_data, elapsed_scan, elapsed_total):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    notes_html = "".join(f"<li>{r['notes']}</li>" for r in results if r.get("notes"))

    rows = []
    for r in results:
        sig = r["signal"]
        cls = {"S": "signal-s", "A": "signal-a", "B": "signal-b",
               "C": "signal-c", "D": "signal-d"}.get(sig, "signal-d")
        freq = freq_data.get(r["code"], {})
        tf = freq.get("total_freq", 0)
        sc = freq.get("sector_count", 0); cc = freq.get("concept_count", 0)
        freq_bar_w = min(tf * 3, 100)
        rows.append(
            f"<tr class='{cls}'>"
            f"<td>{r['code']}</td><td>{r['name']}</td><td>{r['price']:.2f}</td>"
            f"<td>{r['score']:.0f}</td><td><span class='signal'>{sig}</span></td>"
            f"<td class='freq-cell'>"
            f"<span class='freq-num'>{tf}线</span>"
            f"<span class='freq-bar' style='width:{freq_bar_w}px'></span>"
            f"<span class='freq-detail'>{sc}行业+{cc}概念</span></td>"
            f"<td>{r.get('flow_market_cap', 0)/1e8:.1f}亿</td>"
            f"<td style='text-align:left;font-size:12px'>{r['notes']}</td></tr>"
        )

    freq_rows = []
    for code, info in sorted(freq_data.items(), key=lambda x: x[1].get("total_freq", 0), reverse=True):
        if info.get("total_freq", 0) <= 0 or "code_set" in str(info): continue
        name = next((r["name"] for r in results if r["code"] == code), "")
        sectors_html = "".join(f"<span class='tag tag-s'>{s}</span>" for s in info.get("sectors", [])[:10])
        concepts_html = "".join(f"<span class='tag tag-c'>{c}</span>" for c in info.get("concepts", [])[:15])
        more_s = f"<span class='tag-more'>+{len(info.get('sectors',[]))-10}个</span>" if len(info.get('sectors',[])) > 10 else ""
        more_c = f"<span class='tag-more'>+{len(info.get('concepts',[]))-15}个</span>" if len(info.get('concepts',[])) > 15 else ""
        freq_rows.append(
            f"<tr><td>{code}</td><td>{name}</td>"
            f"<td class='freq-num'>{info.get('total_freq', 0)}线</td>"
            f"<td>{info.get('sector_count', 0)}</td><td>{info.get('concept_count', 0)}</td>"
            f"<td class='tags-cell'>{sectors_html}{more_s}</td>"
            f"<td class='tags-cell'>{concepts_html}{more_c}</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>尾盘竞价选股结果 {now_str}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Microsoft YaHei','PingFang SC',sans-serif; background:#0a0e17; color:#c9d1d9; padding:24px 32px; }}
.header {{ display:flex; align-items:baseline; gap:16px; margin-bottom:4px; }}
h1 {{ color:#58a6ff; font-size:22px; }}
.meta {{ color:#8b949e; margin-bottom:20px; font-size:13px; }}
.perf {{ background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px 16px; margin-bottom:20px; display:flex; gap:24px; font-size:13px; }}
.perf-item {{ color:#8b949e; }} .perf-item strong {{ color:#3fb950; font-size:15px; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th {{ background:#161b22; color:#58a6ff; padding:10px 8px; border:1px solid #30363d; position:sticky; top:0; z-index:10; }}
td {{ padding:8px; border:1px solid #30363d; }}
tr:nth-child(even) {{ background:#161b22; }}
tr:hover {{ background:#1f2937; }}
.signal-s {{ border-left:4px solid #f85149; }}
.signal-a {{ border-left:4px solid #f0883e; }}
.signal-b {{ border-left:4px solid #3fb950; }}
.signal-c {{ border-left:4px solid #58a6ff; }}
.signal-d {{ border-left:4px solid #484f58; opacity:0.6; }}
.signal {{ font-weight:bold; font-size:1.3em; }}
.signal-s .signal {{ color:#f85149; }} .signal-a .signal {{ color:#f0883e; }}
.signal-b .signal {{ color:#3fb950; }} .signal-c .signal {{ color:#58a6ff; }}
.signal-d .signal {{ color:#484f58; }}
.freq-cell {{ white-space:nowrap; }}
.freq-num {{ font-weight:bold; color:#e3b341; margin-right:6px; }}
.freq-bar {{ display:inline-block; height:8px; background:linear-gradient(90deg,#e3b341,#f0883e); border-radius:4px; vertical-align:middle; margin-right:6px; }}
.freq-detail {{ color:#8b949e; font-size:11px; }}
.section-title {{ color:#e3b341; font-size:16px; margin:32px 0 12px; border-bottom:1px solid #30363d; padding-bottom:6px; }}
.freq-table th {{ color:#e3b341; }}
.tags-cell {{ max-width:420px; line-height:1.9; }}
.tag {{ display:inline-block; padding:1px 8px; border-radius:3px; font-size:11px; margin:1px 2px; }}
.tag-s {{ background:#1c2333; color:#58a6ff; border:1px solid #30363d; }}
.tag-c {{ background:#1c2133; color:#d2a8ff; border:1px solid #3d2f55; }}
.tag-more {{ color:#484f58; font-size:10px; }}
.notes {{ margin-top:24px; background:#161b22; border-radius:8px; padding:16px; }}
.notes h3 {{ color:#58a6ff; margin-bottom:8px; }}
.notes ul {{ padding-left:20px; font-size:12px; color:#8b949e; }}
.footer {{ margin-top:24px; color:#484f58; font-size:12px; text-align:center; }}
</style>
</head>
<body>
<div class="header">
  <h1>尾盘竞价选股结果</h1>
  <span style="color:#8b949e;font-size:13px">v3.3 · 频次=每条线+1</span>
</div>
<p class="meta">生成: {now_str} | 温度: {market_temp:.1f} | {json.dumps(market_info, ensure_ascii=False)}</p>
<div class="perf">
  <span class="perf-item">频次扫描 <strong>{elapsed_scan:.0f}s</strong></span>
  <span class="perf-item">总耗时 <strong>{elapsed_total:.0f}s</strong></span>
  <span class="perf-item">股票 <strong>{len(results)} 只</strong></span>
  <span class="perf-item">信号 S/A/B/C <strong>{sum(1 for r in results if r['signal'] in 'SABC')} 只</strong></span>
</div>
<table>
<tr><th>代码</th><th>名称</th><th>现价</th><th>得分</th><th>信号</th><th>主线频次(每线+1)</th><th>流通市值</th><th>分析</th></tr>
{"".join(rows)}
</table>
<h2 class="section-title">主线频次详情（每进一个板块/概念+1，按总频次降序）</h2>
<table class="freq-table">
<tr><th>代码</th><th>名称</th><th>总频次</th><th>行业板块数</th><th>概念板块数</th><th>所属行业板块</th><th>所属概念板块</th></tr>
{"".join(freq_rows)}
</table>
<div class="notes"><h3>分析过程</h3><ul>{notes_html}</ul></div>
<p class="footer">v3.3 · 频次=行业+概念每条线+1 | 线程池并行+缓存 | 全面加速</p>
</body>
</html>"""


# ================================================================
# ★ 股池筛选: 年涨幅>30% + 近期涨停
# ================================================================
def _check_ytd_and_limit_up(code: str) -> Tuple[bool, float, bool]:
    """
    检查单只股票是否满足: 今年涨幅>30% 且 近期有涨停
    返回 (是否通过, 年涨幅%, 近期是否涨停)
    """
    try:
        today_str = datetime.now().strftime("%Y%m%d")
        year_start = f"{datetime.now().year}0101"
        df = akshare_call(
            ak.stock_zh_a_hist,
            symbol=code, period="daily",
            start_date=year_start, end_date=today_str,
            adjust="qfq", timeout=15,
        )
        if df is None or df.empty or len(df) < 2:
            return False, 0.0, False

        # 年涨幅 = (最新收盘 - 年初收盘) / 年初收盘
        start_close = float(df.iloc[0]["收盘"])
        end_close   = float(df.iloc[-1]["收盘"])
        if start_close <= 0:
            return False, 0.0, False
        ytd_pct = (end_close - start_close) / start_close * 100

        # 近期涨停: 最近 20 个交易日内，任一日涨幅 >= 9.9%
        # 创业板(30x)/科创板(68x) 涨跌幅限制 20%, 判断 >= 19.9%
        has_limit_up = False
        recent = df.tail(20)
        for _, row in recent.iterrows():
            chg = float(row.get("涨跌幅", 0) or 0)
            if code.startswith(("30", "68")):
                if chg >= 19.9:
                    has_limit_up = True; break
            else:
                if chg >= 9.9:
                    has_limit_up = True; break

        passed = (ytd_pct > 30) and has_limit_up
        return passed, ytd_pct, has_limit_up

    except Exception as e:
        print(f"  → 筛选查询失败({code}): {e}")
        return False, 0.0, False


def filter_pool_by_ytd_and_limit_up(pool: list) -> list:
    """
    从股票池中筛选: 今年涨幅>30% 且 近期有涨停
    """
    if not pool:
        return pool

    print(f"[筛选] 检查年涨幅>30% + 近期涨停 ({len(pool)} 只)...")
    filtered = []
    for i, stock in enumerate(pool):
        code = stock["code"]
        passed, ytd_pct, has_lu = _check_ytd_and_limit_up(code)
        status = "✓" if passed else "✗"
        print(f"    [{i+1}/{len(pool)}] {code} {stock.get('name',''):<6s} "
              f"年涨幅={ytd_pct:+.1f}%  涨停={'有' if has_lu else '无'}  {status}")
        if passed:
            stock["ytd_pct"] = ytd_pct
            filtered.append(stock)
        time.sleep(0.8)  # 限频

    print(f"[筛选] 通过: {len(filtered)}/{len(pool)} 只")
    return filtered


# ================================================================
# 股池加载
# ================================================================
def _load_pool():
    if not HAS_AKSHARE:
        return _load_pool_from_file()

    # 涨停池(走缓存)
    cached_zt = cache_load("pool_zt", ttl_hours=1)
    if cached_zt is not None:
        return cached_zt
    try:
        df = get_zt_df()
        if df is not None and not df.empty:
            pool = []
            for _, row in df.iterrows():
                code = str(row.get("代码", "")).zfill(6)
                pool.append({
                    "code": code, "name": str(row.get("名称", "")),
                    "price": float(row.get("最新价", 0) or 0),
                    "pre_close": float(row.get("昨收", 0) or 0),
                    "volume": int(float(row.get("成交量", 0) or 0)),
                    "amount": float(row.get("成交额", 0) or 0),
                })
            if pool:
                print(f"[pool] akshare 涨停池: {len(pool)} 只")
                cache_save("pool_zt", pool)
                return pool
    except Exception as e:
        print(f"[pool] akshare 涨停池获取失败: {e}")

    # 热门股(走缓存)
    try:
        cached_hot = cache_load("pool_hot", ttl_hours=1)
        if cached_hot is not None:
            return cached_hot
        df = akshare_call(ak.stock_hot_rank_em)
        if df is not None and not df.empty:
            pool = []
            for _, row in df.head(50).iterrows():
                code = str(row.get("股票代码", "")).zfill(6)
                pool.append({"code": code, "name": str(row.get("股票简称", "")),
                             "price": 0, "pre_close": 0, "volume": 0, "amount": 0})
            if pool:
                print(f"[pool] akshare 热门股: {len(pool)} 只")
                cache_save("pool_hot", pool)
                return pool
    except Exception as e:
        print(f"[pool] akshare 热门股获取失败: {e}")

    return _load_pool_from_file()


def _load_pool_from_file():
    pool_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool.json")
    if os.path.exists(pool_file):
        try:
            with open(pool_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            pool = [{"code": str(s.get("code", "")).zfill(6), "name": s.get("name", ""),
                      "price": float(s.get("price", 0)), "pre_close": float(s.get("pre_close", 0)),
                      "volume": int(s.get("volume", 0)), "amount": float(s.get("amount", 0))}
                     for s in data]
            print(f"[pool] 本地 pool.json: {len(pool)} 只")
            return pool
        except Exception as e:
            print(f"[pool] 本地加载失败: {e}")
    return []


# ================================================================
# 主流程
# ================================================================
def main():
    print("=" * 64)
    print("  尾盘拉升选股器 v3.3 — 频次=每条线+1")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 64)
    t_all = time.time()

    # 1. 股票池
    pool = _load_pool()
    if not pool:
        print("[错误] 无可用股票池，退出"); return
    # ★ 年涨幅>30% + 近期涨停 筛选
    pool = filter_pool_by_ytd_and_limit_up(pool)
    if not pool:
        print("[筛选] 无符合条件的股票，退出"); return
    pool_codes = [s["code"] for s in pool]

    # 2. ★ 板块/概念频次(带缓存 + 并行, 每条线+1)
    print("[1/5] 板块/概念频次统计...")
    t_scan = time.time()
    freq_data = analyze_sector_concept_frequency(pool_codes)
    elapsed_scan = time.time() - t_scan
    print(f"  → 频次扫描耗时: {elapsed_scan:.1f}s")

    # 3. 实时行情(走缓存快照)
    print("[2/5] 获取实时行情...")
    bid_metrics = fetch_bid_metrics_from_akshare()
    print(f"  → akshare 快照: {len(bid_metrics)} 只")

    # mootdx 补充盘口
    if not tdx.connected: tdx.connect()
    if tdx.connected:
        codes6 = [c for c in pool_codes if c in bid_metrics]
        for i in range(0, len(codes6), 80):
            batch = codes6[i:i+80]
            quotes = tdx.get_quotes([_code_to_tdx(c) for c in batch])
            code_map = {c: c for c in batch}  # TDX API 返回裸代码，直接用裸代码做 key
            for tdx_code, q in quotes.items():
                code6 = code_map.get(tdx_code, "")
                if code6 in bid_metrics:
                    bid_metrics[code6].update({
                        "bid1_vol": q.get("bid1_vol", 0),
                        "ask1_vol": q.get("ask1_vol", 0),
                        "bid1": q.get("bid1", 0), "ask1": q.get("ask1", 0),
                    })
                    if q.get("price", 0) > 0: bid_metrics[code6]["price"] = q["price"]
                    if q.get("volume", 0) > 0:
                        bid_metrics[code6]["volume"] = q["volume"] * 100  # TDX返回手，转为股
            time.sleep(0.2)
        print("  → mootdx 盘口补充完成")

    # 4. 市场概况
    print("[3/5] 市场概况...")
    market_temp, _, market_info = score_market_temperature()
    print(f"  → 温度: {market_temp:.1f}")

    # 5. 打分
    print(f"[4/5] 打分中 ({len(pool)} 只)...")
    results = []
    for stock in pool:
        code = stock["code"]
        fi = freq_data.get(code, {"total_freq": 0, "sector_count": 0, "concept_count": 0})
        score, notes, details = score_tail_auction(
            code, stock, bid_metrics, market_temp, market_info, fi,
        )
        if score >= 85: sig = "S"
        elif score >= 75: sig = "A"
        elif score >= 65: sig = "B"
        elif score >= 55: sig = "C"
        else: sig = "D"

        results.append({
            "code": code, "name": stock["name"], "price": stock.get("price", 0),
            "score": score, "signal": sig, "notes": notes,
            "flow_market_cap": stock.get("amount", 0), **details,
        })

    results.sort(key=lambda x: (
        freq_data.get(x["code"], {}).get("total_freq", 0), x["score"],
    ), reverse=True)

    # 6. 报告
    elapsed_total = time.time() - t_all
    print(f"[5/5] 生成报告... (总耗时 {elapsed_total:.1f}s)")
    html = generate_html(results, market_temp, market_info, freq_data, elapsed_scan, elapsed_total)
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tail_auction_result.html")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  → 已保存: {html_path}")
        webbrowser.open(f"file://{html_path}")
    except Exception as e:
        print(f"[HTML] 写入失败: {e}")

    # 7. 终端输出
    print("\n" + "-" * 90)
    for r in results:
        fi = freq_data.get(r["code"], {})
        tf = fi.get("total_freq", 0); sc = fi.get("sector_count", 0); cc = fi.get("concept_count", 0)
        print(f"  {r['signal']}  {r['code']}  {r['name']:<8s}  "
              f"价格={r['price']:>8.2f}  分={r['score']:>5.0f}  "
              f"频次={tf:>3d}线({sc}行业+{cc}概念)  {r['notes']}")
    print("-" * 90)
    sigs = {}
    for r in results: sigs.setdefault(r["signal"], []).append(r)
    for sig in ["S", "A", "B", "C", "D"]:
        if sig in sigs: print(f"  {sig} 级: {len(sigs[sig])} 只")
    print(f"\n  频次扫描: {elapsed_scan:.1f}s | 总耗时: {elapsed_total:.1f}s")
    tdx.disconnect()


if __name__ == "__main__":
    main()
