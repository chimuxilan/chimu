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

import json, os, sys, time, traceback, webbrowser, pickle, threading, gc
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from enum import Enum

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ========== Baostock 初始化 ==========
import baostock as bs

# 登录 Baostock（免费，无需账号）
def _init_baostock():
    """初始化 Baostock 登录（增加重试机制）"""
    import time
    max_retries = 3
    for attempt in range(max_retries):
        try:
            lg = bs.login()
            if lg.error_code == '0':
                print("[成功] [Baostock] 登录成功")
                return True
            else:
                print(f"[错误] [Baostock] 登录失败: {lg.error_msg}")
                if attempt < max_retries - 1:
                    print(f"等待 2 秒后重试... ({attempt+1}/{max_retries})")
                    time.sleep(2)
        except Exception as e:
            print(f"[错误] [Baostock] 登录异常: {e}")
            if attempt < max_retries - 1:
                print(f"等待 2 秒后重试... ({attempt+1}/{max_retries})")
                time.sleep(2)
    return False

# 启动时登录
_baostock_logged_in = _init_baostock()
_baostock_reconnect_count = 0  # 重连次数

# ========== Baostock K-line data fetch function ==========
def _baostock_reconnect():
    """Baostock 断连重连"""
    global _baostock_logged_in, _baostock_reconnect_count
    if _baostock_reconnect_count >= 3:
        return False
    try:
        bs.logout()
    except Exception:
        pass
    try:
        lg = bs.login()
        if lg.error_code == '0':
            _baostock_logged_in = True
            _baostock_reconnect_count += 1
            print(f"[Baostock] 重连成功 (第{_baostock_reconnect_count}次)")
            return True
    except Exception:
        pass
    _baostock_reconnect_count += 1
    print(f"[Baostock] 重连失败 (第{_baostock_reconnect_count}次)")
    return False


def _fetch_kline_from_baostock(code, start_date, end_date):
    """Get K-line data from Baostock（主数据源），失败时回退到新浪"""
    global _baostock_logged_in
    if not _baostock_logged_in:
        print(f"[警告] [Baostock] 未登录，跳过查询")
        return None
    
    try:
        bs_code = f"sz.{code}" if not code.startswith('6') else f"sh.{code}"
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2"
        )
        
        if rs.error_code != '0':
            # 连接可能断开，尝试重连一次
            if _baostock_reconnect():
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="2"
                )
                if rs.error_code != '0':
                    return None
            else:
                return None
        
        data_list = []
        while rs.next():
            data_list.append(rs.get_row_data())
        
        if not data_list:
            return None
        
        df = pd.DataFrame(data_list, columns=rs.fields)
        for col in ['open', 'high', 'low', 'close', 'preclose', 'volume', 'amount', 'turn', 'pctChg']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df = df.rename(columns={
            'close': '收盘', 'open': '开盘', 'high': '最高', 'low': '最低',
            'volume': '成交量', 'amount': '成交额', 'pctChg': '涨跌幅'
        })
        
        return df
        
    except Exception as e:
        # 连接异常，尝试重连
        if _baostock_reconnect():
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="2"
                )
                if rs.error_code == '0':
                    data_list = []
                    while rs.next():
                        data_list.append(rs.get_row_data())
                    if data_list:
                        df = pd.DataFrame(data_list, columns=rs.fields)
                        for col in ['open', 'high', 'low', 'close', 'preclose', 'volume', 'amount', 'turn', 'pctChg']:
                            if col in df.columns:
                                df[col] = pd.to_numeric(df[col], errors='coerce')
                        df = df.rename(columns={
                            'close': '收盘', 'open': '开盘', 'high': '最高', 'low': '最低',
                            'volume': '成交量', 'amount': '成交额', 'pctChg': '涨跌幅'
                        })
                        return df
            except Exception:
                pass
        print(f"[回退] [Baostock] 查询失败: {e}，回退到新浪 API")
        return _fetch_kline_from_sina(code, start_date, end_date)





# ========================================================================
# SHADOW PATTERN STOCK SELECTION STRATEGY v3.5

def _fetch_kline_from_sina(code: str, start_date: str, end_date: str, retries: int = 2) -> Optional[pd.DataFrame]:
    """
    从新浪财经获取K线数据（主数据源，不限频）
    code: 6位股票代码，如 '600519'
    start_date / end_date: YYYYMMDD 格式
    返回：统一中文列名的 DataFrame，按日期升序排列
    """
    sina_code = f"sh{code}" if code.startswith("6") else f"sz{code}"

    for attempt in range(retries):
        try:
            df = ak.stock_zh_a_daily(
                symbol=sina_code,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="qfq"
            )

            if df is None or df.empty:
                return None

            df = df.rename(columns={
                'date': '日期', 'close': '收盘', 'open': '开盘',
                'high': '最高', 'low': '最低', 'volume': '成交量',
                'amount': '成交额', 'turnover': '换手率'
            })

            for col in ['close', 'open', 'high', 'low', 'volume', 'amount', 'turnover']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            if '日期' in df.columns:
                df = df.sort_values('日期').reset_index(drop=True)
            else:
                return None

            if '收盘' in df.columns and len(df) >= 2:
                df['涨跌幅'] = df['收盘'].pct_change().fillna(0) * 100

            # 日期范围过滤
            start_iso = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}" if len(start_date) == 8 else start_date
            end_iso   = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"   if len(end_date)   == 8 else end_date

            def to_str(val):
                if hasattr(val, 'strftime'):
                    return val.strftime('%Y-%m-%d')
                return str(val)

            date_strs = df['日期'].apply(to_str)
            if start_iso:
                df = df[date_strs >= start_iso]
            if end_iso:
                df = df[date_strs <= end_iso]

            return df

        except Exception as e:
            if attempt < retries - 1:
                continue
            print(f"  -> [新浪K线] {code} 全部失败: {e}")
            return None


def _fetch_sina_fund_flow(code, page=1, num=1, sort='opendate', asc=0):
    """
    从新浪财经获取资金流向数据（最新一条）
    code: 6位股票代码，如 '600519'
    page: 页码（从1开始）
    num: 每页数据量（默认1，只取最新一条）
    sort: 排序字段（opendate=开盘日期）
    asc: 排序方式（0=降序，1=升序）
    返回：资金流向数据字典（最新的一条），或None
    """
    # 确保code有市场前缀
    if not code.startswith('sh') and not code.startswith('sz'):
        # 根据股票代码判断市场：6开头=上海，0/3开头=深圳
        if code.startswith('6'):
            code = 'sh' + code
        else:
            code = 'sz' + code
    
    # 新浪API URL（带参数）
    url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs?page={page}&num={num}&sort={sort}&asc={asc}&daima={code}"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None
        
        # 解析JSON
        data = response.json()
        if not data:
            return None
        
        # 返回最新的一条数据（第一条是最新的，因为asc=0降序）
        return data[0] if data else None
        
    except Exception as e:
        return None


# ========================================================================

def _fetch_kline_unified(code: str, start_ak: str, end_ak: str,
                         start_bs: str = None, end_bs: str = None) -> Optional[pd.DataFrame]:
    """
    统一K线获取：①新浪(akshare包装，不限频) → ②Baostock(免费) → ③akshare东财(兜底)
    start_ak/end_ak: YYYYMMDD 格式
    start_bs/end_bs: YYYY-MM-DD 格式（省略时自动从 start_ak/end_ak 转换）
    返回统一中文列名的 DataFrame，或 None
    """
    if start_bs is None:
        start_bs = (f"{start_ak[:4]}-{start_ak[4:6]}-{start_ak[6:8]}"
                    if len(start_ak) == 8 else start_ak)
    if end_bs is None:
        end_bs = (f"{end_ak[:4]}-{end_ak[4:6]}-{end_ak[6:8]}"
                  if len(end_ak) == 8 else end_ak)

    # ① Baostock（稳定，盘后可用，优先）
    df = _fetch_kline_from_baostock(code, start_bs, end_bs)
    if df is not None and len(df) > 2:
        return df

    # ② 新浪（盘中不限频，补充Baostock）
    df = _fetch_kline_from_sina(code, start_ak, end_ak)
    if df is not None and len(df) > 2:
        return df

    # ③ THS K线（TCP直连，不封IP）
    try:
        tc = _code6_to_thscode(code)
        from datetime import datetime as _dt
        r = ths.get_klines_raw(tc, start_bs, end_bs)
        if r is not None and len(r) > 2:
            records = []
            for row in r:
                records.append({
                    "日期": row.get("时间") if isinstance(row.get("时间"), str) else (row.get("时间").strftime("%Y-%m-%d") if hasattr(row.get("时间"), "strftime") else ""),
                    "开盘": float(row.get("开盘价", 0)),
                    "最高": float(row.get("最高价", 0)),
                    "最低": float(row.get("最低价", 0)),
                    "收盘": float(row.get("收盘价", 0)),
                    "成交量": float(row.get("成交量", 0)),
                    "成交额": float(row.get("总金额", 0)),
                })
            return pd.DataFrame(records)
    except Exception:
        pass
    return None


def _realtime_auction_from_ths(code6: str):
    """
    实时从 THS call_auction 获取竞价数据（用于补采/盘中实时分析）
    字段: {时间, 价格, 买2量, 卖2量, 当前量}
    筛选 09:20 后的买卖盘总量
    返回 dict: {open_price, bid_after_920, ask_after_920}
    """
    try:
        ths._ensure_connected()
        if not ths.connected or ths._ths is None:
            return None
        tc = _code6_to_thscode(code6)
        raw = ths._ths.call_auction(tc)
        records = raw.data if hasattr(raw, 'success') and raw.success and raw.data else []
        if not records:
            return None
        import datetime as _dt
        def _ts_to_min(ts_val):
            s = str(ts_val)
            if len(s) == 10:
                dt = _dt.datetime.fromtimestamp(int(s))
                return dt.hour * 60 + dt.minute
            elif len(s) >= 5:
                return int(s[:2]) * 60 + int(s[3:5])
            return 0
        def _gv(r, k, default=0):
            v = r.get(k) if isinstance(r, dict) else default
            if v is None: return default
            return float(v) if v != ths._INVALID else 0.0
        time_sorted = sorted(records, key=lambda x: str(x.get("时间", "")))
        after_920 = [x for x in time_sorted if _ts_to_min(x.get("时间", 0)) >= 560]
        bid920 = sum(_gv(x, "买2量") for x in after_920)
        ask920 = sum(_gv(x, "卖2量") for x in after_920)
        open_p = _gv(time_sorted[-1], "价格") if time_sorted else 0
        return {"open_price": open_p, "bid_after_920": bid920, "ask_after_920": ask920}
    except Exception:
        return None


def _get_market_cap(code: str) -> float:
    """Get total market cap (billion yuan) - 腾讯财经接口，不限频"""
    cache_key = f"mktcap_{code}"
    cached = cache_load(cache_key, ttl_hours=24)
    if cached is not None:
        return cached

    # ★ 极速路径：直接从已缓存的 spot_df 读（_tencent_batch_spot 已写 总市值亿 字段）
    try:
        df_sp = get_spot_df()
        if df_sp is not None and not df_sp.empty:
            rows = df_sp[df_sp["代码"] == code.zfill(6)]
            if not rows.empty:
                yi = float(rows.iloc[0].get("总市值亿", 0) or 0)
                if yi > 0:
                    cache_save(cache_key, yi)
                    return yi
    except Exception:
        pass

    # 数据源：腾讯财经接口（field[37]=总市值万元），不限频
    # 东财 stock_individual_info_em 已被IP封禁
    import re as _re
    prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    try:
        r = requests.get(url, timeout=5)
        m = _re.search(r'"([^"]+)"', r.text)
        if m:
            fields = m.group(1).split("~")
            # field[44]=总市值(亿元)，不存在时兜底
            if len(fields) > 44 and fields[44].strip():
                result = float(fields[44])
                cache_save(cache_key, result)
                print(f"  -> [market cap] {code} = {result:.1f}B (field44)")
                return result
            elif len(fields) > 37 and fields[37].strip():
                mktcap_wan = float(fields[37])
                result = mktcap_wan / 10000.0
                cache_save(cache_key, result)
                print(f"  -> [market cap] {code} = {result:.1f}B")
                return result
        print(f"  -> [market cap] {code} no data from Tencent")
        return 999.0
    except Exception as e:
        print(f"  -> [market cap] {code} Tencent error: {e}")
        return 999.0


def _find_limit_up_days(df: pd.DataFrame, lookback: int = 20) -> list:
    """Find limit-up days within N days"""
    limit_up_days = []
    recent = df.tail(lookback).copy()
    
    for _, row in recent.iterrows():
        chg = float(row.get("涨跌幅", 0) or 0)
        code = str(row.get("code", ""))
        is_limit_up = (chg >= 19.9) if code.startswith(("30", "68")) else (chg >= 9.9)
        
        if is_limit_up:
            limit_up_days.append({
                "date": row.get("date", ""),
                "close": float(row.get("收盘", 0)),
                "high": float(row.get("最高", 0)),
                "low": float(row.get("最低", 0)),
                "volume": float(row.get("成交量", 0)),
                "pct_chg": chg
            })
    
    return limit_up_days


def _find_volume_spikes(df: pd.DataFrame, lookback: int = 20, threshold: float = 2.0) -> list:
    """Find volume spike days (volume >= prev * threshold)"""
    spikes = []
    recent = df.tail(lookback).copy()
    
    for i in range(1, len(recent)):
        curr_vol = float(recent.iloc[i].get("成交量", 0) or 0)
        prev_vol = float(recent.iloc[i-1].get("成交量", 0) or 0)
        
        if prev_vol > 0 and curr_vol >= prev_vol * threshold:
            spikes.append({
                "date": recent.iloc[i].get("date", ""),
                "curr_vol": curr_vol,
                "prev_vol": prev_vol,
                "ratio": curr_vol / prev_vol,
                "close": float(recent.iloc[i].get("收盘", 0)),
                "high": float(recent.iloc[i].get("最高", 0)),
                "low": float(recent.iloc[i].get("最低", 0)),
                "pct_chg": float(recent.iloc[i].get("涨跌幅", 0) or 0)
            })
    
    return spikes


def _find_consolidation_zones(df: pd.DataFrame, lookback: int = 60) -> list:
    """Identify consolidation zones: amplitude < 15%, duration >= 10 days"""
    zones = []
    recent = df.tail(lookback).copy()
    
    if len(recent) < 10:
        return zones
    
    window = 10
    for i in range(len(recent) - window + 1):
        window_data = recent.iloc[i:i+window]
        highs = window_data["最高"].astype(float)
        lows = window_data["最低"].astype(float)
        
        max_high = highs.max()
        min_low = lows.min()
        
        if min_low > 0:
            amplitude = (max_high - min_low) / min_low * 100
            
            if amplitude < 15:
                zones.append({
                    "start_idx": i,
                    "end_idx": i + window - 1,
                    "max_high": max_high,
                    "min_low": min_low,
                    "amplitude": amplitude,
                    "avg_volume": window_data["成交量"].astype(float).mean()
                })
    
    return zones


def _analyze_upper_shadow(df: pd.DataFrame, lookback: int = 60) -> list:
    """Analyze upper shadow characteristics for each trading day"""
    results = []
    recent = df.tail(lookback).copy()
    
    for i in range(len(recent)):
        row = recent.iloc[i]
        high = float(row.get("最高", 0))
        low = float(row.get("最低", 0))
        close = float(row.get("收盘", 0))
        open_p = float(row.get("开盘", 0))
        volume = float(row.get("成交量", 0))
        
        upper_shadow = high - max(close, open_p)
        lower_shadow = min(close, open_p) - low
        body = abs(close - open_p)
        total_range = high - low if high > low else 1
        
        results.append({
            "date": row.get("date", ""),
            "idx": i,
            "high": high, "low": low, "close": close, "open": open_p,
            "volume": volume,
            "upper_shadow": upper_shadow,
            "lower_shadow": lower_shadow,
            "body": body,
            "upper_ratio": upper_shadow / total_range,
            "lower_ratio": lower_shadow / total_range,
            "is_yang": close >= open_p,
            "lower_body_ratio": lower_shadow / body if body > 0 else 0
        })
    
    return results


def _check_limit_up_floor(df: pd.DataFrame, limit_up_days: list, lookback: int = 15) -> dict:
    """Check if limit-up floor price is broken (allow +/-2% tolerance)"""
    if not limit_up_days:
        return {"has_limit_up": False, "floor_intact": False}
    
    recent_lu = limit_up_days[0]
    floor_price = recent_lu["low"]
    lu_date = recent_lu["date"]
    
    recent = df.tail(lookback).copy()
    broken = False
    
    for _, row in recent.iterrows():
        row_date = str(row.get("date", ""))
        if row_date <= lu_date:
            continue
        low = float(row.get("最低", 0))
        if low < floor_price * 0.98:
            broken = True
            break
    
    return {
        "has_limit_up": True,
        "floor_intact": not broken,
        "floor_price": floor_price,
        "recent_lu": recent_lu
    }


def _check_breakout_high(df: pd.DataFrame, limit_up_days: list) -> dict:
    """Check if limit-up breaks 30-day high with volume >= 1.5x"""
    if not limit_up_days:
        return {"breaks_high": False, "high_volume_ok": False}
    
    recent_lu = limit_up_days[0]
    lu_high = recent_lu["high"]
    lu_volume = recent_lu["volume"]
    
    recent = df.tail(30).copy()
    max_high = 0
    max_high_vol = 0
    
    for _, row in recent.iterrows():
        h = float(row.get("最高", 0))
        v = float(row.get("成交量", 0))
        if h > max_high:
            max_high = h
            max_high_vol = v
    
    breaks_high = lu_high > max_high
    high_volume_ok = (lu_volume >= max_high_vol * 1.5) if (breaks_high and max_high_vol > 0) else False
    
    return {
        "breaks_high": breaks_high,
        "high_volume_ok": high_volume_ok,
        "lu_high": lu_high,
        "prev_high": max_high
    }


def _analyze_shadow_pattern(code: str) -> dict:
    """Comprehensive analysis of 7+5 shadow pattern conditions"""
    cache_key = f"shadow_{code}"
    cached = cache_load(cache_key, ttl_hours=24)
    if cached is not None:
        return cached
    
    try:
        # Baostock 需要 YYYY-MM-DD 格式
        today_bs = datetime.now().strftime("%Y-%m-%d")
        start_bs = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        # 第三兜底: THS klines (TCP直连)
        today_ak = datetime.now().strftime("%Y%m%d")
        start_ak = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

        df = _fetch_kline_from_baostock(code, start_bs, today_bs)
        if df is None or df.empty:df = _fetch_kline_from_ths(code, start_bs, today_bs)
        
        if df is None or df.empty or len(df) < 30:
            result = {"pass": False, "reason": "data_insufficient"}
            cache_save(cache_key, result)
            return result
        
        # 7 Shadow Conditions
        lu_20 = _find_limit_up_days(df, 20)
        cond_a = len(lu_20) > 0
        
        spikes = _find_volume_spikes(df, 20, 2.0)
        cond_b = len(spikes) > 0
        
        zones = _find_consolidation_zones(df, 60)
        shadows = _analyze_upper_shadow(df, 60)
        cond_c = False
        for z in zones:
            for s in shadows:
                if z["start_idx"] <= s["idx"] <= z["end_idx"] and s["upper_ratio"] > 0.60:
                    cond_c = True
                    break
            if cond_c:
                break
        
        cond_d = False
        for s in shadows[-20:]:
            for z in zones:
                if z["max_high"] > 0 and s["high"] >= z["max_high"] * 0.98:
                    if s["volume"] >= z["avg_volume"] * 1.5:
                        cond_d = True
                        break
            if cond_d:
                break
        
        cond_e = False
        if lu_20:
            lu_date = lu_20[0]["date"]
            for s in shadows[-10:]:
                if s["date"] > lu_date and s["upper_ratio"] > 0.40:
                    cond_e = True
                    break
        
        cond_f = False
        for s in shadows[-5:]:
            if s["is_yang"] and s["lower_body_ratio"] < 0.3:
                cond_f = True
                break
        
        cond_g = True
        for s in shadows[-5:]:
            if s["lower_ratio"] > 0.6:
                cond_g = False
                break
        
        sc = {
            "A_月内有涨停": cond_a,
            "B_有倍量交易": cond_b,
            "C_横盘影线长": cond_c,
            "D_打高点放量": cond_d,
            "E_涨停后回调": cond_e,
            "F_底部承接强": cond_f,
            "G_下影线短": cond_g
        }
        shadow_cnt = sum(sc.values())
        
        # 5 New Conditions
        mkt_cap = _get_market_cap(code)
        lu_15 = _find_limit_up_days(df, 15)
        floor_check = _check_limit_up_floor(df, lu_15 or lu_20, 15)
        breakout_check = _check_breakout_high(df, lu_15 or lu_20)
        
        cond_d_new = False
        if lu_15 or lu_20:
            lu_days = lu_15 or lu_20
            for lu in lu_days:
                for z in zones:
                    for s in shadows:
                        if s["date"] == lu["date"]:
                            if z["start_idx"] <= s["idx"] <= z["end_idx"]:
                                cond_d_new = True
                                break
                    if cond_d_new:
                        break
                if cond_d_new:
                    break
        
        nc = {
            "A_市值<=100亿": mkt_cap <= 100,
            "B_15日有涨停": len(lu_15) > 0,
            "C_未破涨停底": floor_check.get("floor_intact", False) or not floor_check.get("has_limit_up", True),
            "D_横盘突破涨停": cond_d_new,
            "E_涨停破前高": breakout_check.get("breaks_high", False) and breakout_check.get("high_volume_ok", False)
        }
        new_cnt = sum(nc.values())
        
        # Final: shadow >= 3 AND new >= 2
        final_pass = shadow_cnt >= 3 and new_cnt >= 2
        
        is_double = nc["E_涨停破前高"]
        is_single = nc["D_横盘突破涨停"]
        star = "★★" if is_double else ("★" if is_single else "")
        
        result = {
            "pass": final_pass,
            "shadow_count": shadow_cnt,
            "new_count": new_cnt,
            "shadow_conditions": sc,
            "new_conditions": nc,
            "market_cap": mkt_cap,
            "limit_up_days": (lu_15 or lu_20)[:2],
            "volume_spikes": spikes[:3],
            "consolidation_zones": zones[:2],
            "floor_check": floor_check,
            "breakout_check": breakout_check,
            "is_double_star": is_double,
            "is_single_star": is_single,
            "star_level": star
        }
        
        cache_save(cache_key, result)
        return result
        
    except Exception as e:
        cache_save(cache_key, {"pass": False, "reason": str(e)})
        return {"pass": False, "reason": str(e)}


def filter_by_shadow_pattern(pool: list) -> Tuple[list, dict]:
    """Apply shadow pattern stock selection (parallel with ThreadPoolExecutor)"""
    if not pool:
        return pool, {}
    
    print(f"[Shadow] Analyzing shadow patterns ({len(pool)} stocks)...")
    passed = []
    details = {}
    star_double = []
    star_single = []
    
    # 并行分析，4线程
    _shadow_results = {}
    _shadow_lock = threading.Lock()
    _shadow_done = [0]
    _consecutive_fail = [0]  # 连续失败计数（熔断用）
    _circuit_open = [False]  # 熔断标志

    def _shadow_worker(stock):
        if _circuit_open[0]:
            return stock["code"], {"pass": False, "reason": "circuit_breaker"}
        code = stock["code"]
        result = _analyze_shadow_pattern(code)
        with _shadow_lock:
            _shadow_done[0] += 1
            d = _shadow_done[0]
            name = stock.get("name", "")
            sc = result.get("shadow_count", 0)
            nc = result.get("new_count", 0)
            mc = result.get("market_cap", 999)
            star = result.get("star_level", "")
            status = "PASS" if result.get("pass", False) else "FAIL"
            if d % 10 == 0 or d == len(pool) or d <= 3:
                print(f"    [{d}/{len(pool)}] {code} {name:<6s} "
                      f"Shadow={sc}/7 New={nc}/5 Cap={mc:.0f}B {star} {status}")
            # 连续失败计数
            if not result.get("pass", False) and result.get("reason") in ("data_insufficient", "circuit_breaker"):
                _consecutive_fail[0] += 1
            else:
                _consecutive_fail[0] = 0
            # 连续15只数据不足 → 熔断
            if _consecutive_fail[0] >= 15:
                _circuit_open[0] = True
                print(f"  ⚠️  [Shadow] 连续{_consecutive_fail[0]}只数据不足，触发熔断，跳过剩余")
        return code, result

    max_workers = min(4, len(pool))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_shadow_worker, s): s for s in pool}
        for future in as_completed(futures):
            try:
                code, result = future.result()
                _shadow_results[code] = result
            except Exception:
                pass

    # 汇总结果（保持原始 pool 顺序）
    for i, stock in enumerate(pool):
        code = stock["code"]
        result = _shadow_results.get(code, {"pass": False, "reason": "no_result"})
        details[code] = result

        if result.get("pass", False):
            star = result.get("star_level", "")
            stock["shadow_result"] = result
            stock["star_level"] = star
            passed.append(stock)
            if result.get("is_double_star"):
                star_double.append(stock)
            elif result.get("is_single_star"):
                star_single.append(stock)

    print(f"[Shadow] Passed: {len(passed)}/{len(pool)}")
    print(f"  -> Double-Star (BREAK_HIGH): {len(star_double)}")
    print(f"  -> Single-Star (CONSOLIDATION_LU): {len(star_single)}")

    return passed, details


# 全局内存缓存（当次运行有效，零延迟）
_memory_cache = {}

# ========== 盘中/盘后模式检测 ==========
class TradingMode(Enum):
    PRE_MARKET_WITH_CACHE = "盘前(缓存)"  # 9:00~9:30
    PRE_MARKET = "盘前"              # 9:00~9:30（不缓存）
    INTRADAY = "盘中"                # 9:30~14:57
    CLOSING = "收盘竞价"             # 14:57~15:00
    AFTER_HOURS = "盘后"            # 15:00~次日9:00

def detect_trading_mode(now: datetime = None) -> TradingMode:
    """检测当前交易模式（自动判断）"""
    if now is None:
        now = datetime.now()
    
    # 周末判断
    if now.weekday() >= 5:  # 周六、周日
        return TradingMode.AFTER_HOURS
    
    # 时间判断
    if now.hour == 9 and now.minute < 30:
        return TradingMode.PRE_MARKET_WITH_CACHE  # 盘前+缓存模式
    elif (now.hour == 9 and now.minute >= 30) or (now.hour in [10, 11]):
        return TradingMode.INTRADAY
    elif (now.hour == 12) or (now.hour == 13 and now.minute < 57) or (now.hour == 14 and now.minute < 57):
        return TradingMode.INTRADAY
    elif (now.hour == 14 and now.minute >= 57) or (now.hour == 15 and now.minute == 0):
        return TradingMode.CLOSING
    else:
        return TradingMode.AFTER_HOURS

# 全局模式变量
_current_mode = detect_trading_mode()

def get_cache_ttl() -> float:
    """根据当前模式返回缓存TTL（小时）"""
    if _current_mode == TradingMode.INTRADAY:
        return 1/60  # 盘中：1分钟
    elif _current_mode == TradingMode.AFTER_HOURS:
        return 24  # 盘后：24小时
    elif _current_mode == TradingMode.PRE_MARKET_WITH_CACHE:
        return 12  # 盘前：12小时（复用昨日数据）
    else:
        return 0.5  # 其他：30分钟

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
    from thsdk import THS as _THS
    HAS_THS = True
except ImportError:
    _THS = None; HAS_THS = False
    print("[依赖] thsdk 未安装，同花顺数据不可用")


# ================================================================
# ★ 全局限频(线程安全) + 本地缓存
# ================================================================
_cache_lock = threading.Lock()
_last_ts = 0.0
_ts_lock = threading.Lock()
AKSHARE_MIN_GAP = 0.8  # 线程池模式下 0.8s 即可安全并行

# 腾讯 API 行情缓存（用于 akshare 全线阻塞时的兜底路径）
_GLOBAL_SPOT_CODES = []           # main() 中获取 pool 后设置
_tencent_spot_cache = None        # 腾讯行情 DataFrame 缓存


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


def akshare_call(func, *args, max_retries=1, **kwargs):
    """带限频 + 快速降级的 akshare 调用（优化版）"""
    for attempt in range(max_retries):
        try:
            _akshare_throttle()
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < max_retries - 1:
                # 不等待，立即重试（连接池会加速）
                continue
            else:
                # 失败后，立即返回None（让上层用缓存）
                print(f"  → akshare 失败({e})，使用缓存")
                return None
    return None


# ---------- 本地 pickle 缓存 ----------
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


def _cache_path(tag: str, use_yesterday: bool = False) -> str:
    """生成缓存文件路径（支持昨日缓存）"""
    if use_yesterday:
        # 使用昨日日期
        yesterday = date.today() - timedelta(days=1)
        date_str = yesterday.isoformat()
    else:
        # 使用今日日期
        date_str = date.today().isoformat()
    
    return os.path.join(_CACHE_DIR, f"{tag}_{date_str}.pkl")

def cache_save(tag: str, data, use_yesterday: bool = False):
    """保存到缓存（支持日期前缀）"""
    try:
        path = _cache_path(tag, use_yesterday)
        with open(path, "wb") as f:
            pickle.dump(data, f, protocol=4)
        print(f"  → [缓存] 已保存 {tag} 到 {os.path.basename(path)}")
    except Exception as e:
        print(f"  → [缓存] 保存失败: {e}")


def cache_load(tag: str, ttl_hours: int = 12, allow_stale: bool = False, try_yesterday: bool = False):
    """加载缓存（支持从昨日缓存加载）"""
    # 1. 尝试加载今日缓存
    path = _cache_path(tag, use_yesterday=False)
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if age_h <= ttl_hours:
            with open(path, "rb") as f:
                data = pickle.load(f)
            print(f"  → [缓存命中] {tag} ({age_h*60:.0f}分钟前)")
            return data
        elif allow_stale and age_h < 24:
            with open(path, "rb") as f:
                data = pickle.load(f)
            print(f"  → [降级缓存] {tag} ({age_h:.1f}h前, 已过期但可用)")
            return data
    
    # 2. 如果今日缓存不存在或过期，尝试加载昨日缓存
    if try_yesterday:
        path_yesterday = _cache_path(tag, use_yesterday=True)
        if os.path.exists(path_yesterday):
            print(f"  → [昨日缓存] 加载昨日 {tag} 数据")
            with open(path_yesterday, "rb") as f:
                return pickle.load(f)
    
    return None




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
def _ths_code_to_code6(ths_code: str) -> str:
    """把 THSCODE (USZA002354) 转成 6 位 (002354)"""
    return ths_code[-6:] if len(ths_code) >= 6 else ths_code

def _code6_to_thscode(code6: str) -> str:
    """6位股票代码 → THSCODE (USZA/USHA + 6位)"""
    c = code6.zfill(6)
    prefix = "USHA" if c.startswith(("6", "5", "9")) else "USZA"
    return prefix + c

# ── 集合竞价缓存 ──
_AUCTION_CACHE = None

def get_auction_cache() -> dict:
    """读取当日集合竞价缓存（9:20-9:25 采集）"""
    global _AUCTION_CACHE
    if _AUCTION_CACHE is not None:
        return _AUCTION_CACHE
    _AUCTION_CACHE = {}
    today_str = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(_CACHE_DIR, f"auction_{today_str}.pkl")
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                _AUCTION_CACHE = pickle.load(f)
            print(f"  → [竞价缓存] 加载成功: {len(_AUCTION_CACHE)} 只")
        except Exception as e:
            print(f"  → [竞价缓存] 读取失败: {e}")
    return _AUCTION_CACHE

class THSClientWrapper:
    """THS SDK 包装器 — 懒加载：构造时只保存配置，首次调用时连接"""
    _INVALID = 2147483648

    def __init__(self):
        self._ths = None
        self.connected = False
        self._ops = {}
        self._connect_attempted = False

        if not HAS_THS or _THS is None:
            try:
                print("[thsdk] thsdk 未安装，跳过")
            except OSError:
                pass
            return

        # 只保存账号配置，不连接
        try:
            import ths_cred as _cred
            if _cred.THS_USERNAME and _cred.THS_PASSWORD:
                self._ops["username"] = _cred.THS_USERNAME
                self._ops["password"] = _cred.THS_PASSWORD
        except (ImportError, AttributeError):
            pass
        if not self._ops.get("username"):
            import os as _os
            env_user = _os.environ.get("THS_USERNAME")
            env_pass = _os.environ.get("THS_PASSWORD")
            if env_user and env_pass:
                self._ops["username"] = env_user
                self._ops["password"] = env_pass

    def _ensure_connected(self):
        """按需延迟连接，只尝试一次"""
        if self.connected and self._ths is not None:
            return True
        if self._connect_attempted:
            return False
        self._connect_attempted = True

        if not HAS_THS or _THS is None:
            return False
        try:
            self._ths = _THS(self._ops if self._ops else None)
            result = self._ths.connect(max_retries=2)
            self.connected = result.success
            if self.connected:
                who = self._ops.get("username", "游客")
                try:
                    print(f"[thsdk] 已连接（{who}）")
                except OSError:
                    pass  # WinError 1: print 失败，忽略
            else:
                try:
                    print(f"[thsdk] 连接失败: {result.error}")
                except OSError:
                    pass
            return self.connected
        except Exception as e:
            try:
                print(f"[thsdk] 异常: {e}")
            except OSError:
                pass
            return False

    def _safe_call(self, func, label=""):
        self._ensure_connected()
        if not self.connected or self._ths is None:
            if label:
                print(f"  [THS] {label}: 未连接")
            return {}
        try:
            result = func()
            return result if result else {}
        except Exception as e:
            if label:
                print(f"  [THS] {label}: {type(e).__name__}: {e}")
            return {}

    _INVALID = 2147483648  # THS 无效值标记

    def get_fund_flow(self, code):
        """获取主力资金流向 — thsdk 汇总 query"""
        tc = _code6_to_thscode(code)
        r = self._safe_call(lambda: self._ths.market_data_cn(tc, "汇总"))
        if r and isinstance(r, list) and len(r) > 0:
            row = r[0]
            inflow = row.get("主力净流入", 0) or 0
            if inflow == self._INVALID:
                inflow = 0
            return {"main_net_inflow": float(inflow), "raw": row}
        return {}

    def get_extended(self, code):
        """获取扩展数据(PE/量比/振幅等) — thsdk 汇总 query"""
        tc = _code6_to_thscode(code)
        r = self._safe_call(lambda: self._ths.market_data_cn(tc, "汇总"))
        if r and isinstance(r, list) and len(r) > 0:
            row = r[0]
            pe = row.get("市盈率TTM", 0) or 0
            if pe == self._INVALID:
                pe = 0
            return {"pe_ttm": float(pe), "pe": float(pe), "raw": row}
        return {}

    def get_big_orders(self, code):
        """获取大单逐笔 — thsdk big_order_flow"""
        tc = _code6_to_thscode(code)
        r = self._safe_call(lambda: self._ths.big_order_flow(tc))
        if r and isinstance(r, list):
            return r
        if r and isinstance(r, dict):
            return [r]
        return []

    def get_klines_raw(self, code: str, start_bs: str, end_bs: str):
        """获取日K原始数据 — thsdk klines（TCP直连，不封IP）
        start_bs/end_bs: YYYY-MM-DD 格式
        """
        tc = _code6_to_thscode(code)
        from datetime import datetime as _dt
        try:
            start_dt = _dt.strptime(start_bs[:10], "%Y-%m-%d")
            end_dt = _dt.strptime(end_bs[:10], "%Y-%m-%d")
            self._ensure_connected()
            if not self.connected or self._ths is None:
                return None
            r = self._ths.klines(tc, start_dt, end_dt)
            if r and r.success and r.data and isinstance(r.data, list):
                return r.data
        except Exception:
            pass
        return None


ths = THSClientWrapper()


# ================================================================
# ★★★ 实时达标信号（截图逻辑）
# ================================================================
def _detect_volume_breakout(df_hist, cur_price: float, cur_volume: float = 0) -> dict:
    """
    爆量突破放量检测
    返回 {is_breakout, vol_ratio, breakout_high, vol_surge, price_break, label}
    """
    result = {"is_breakout": False, "vol_ratio": 0.0, "breakout_high": 0.0,
              "vol_surge": False, "price_break": False, "label": ""}
    if df_hist is None or len(df_hist) < 10:
        return result

    volumes = df_hist["成交量"].tolist()
    highs   = df_hist["最高"].tolist()

    # ① 放量检测: 当日量 / 近5日均量
    avg_vol_5  = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else sum(volumes) / len(volumes)
    today_vol  = cur_volume if cur_volume > 0 else volumes[-1]

    if avg_vol_5 > 0:
        vol_ratio = round(today_vol / avg_vol_5, 2)
    else:
        vol_ratio = 0.0
    result["vol_ratio"] = vol_ratio

    # 放量标准: 量比 >= 2.0 (2倍以上算放量, 3倍以上算爆量)
    result["vol_surge"] = vol_ratio >= 2.0

    # ② 突破检测: 当前价 > 近10日最高价
    recent_high_10 = max(highs[-10:]) if len(highs) >= 10 else max(highs)
    result["breakout_high"] = recent_high_10
    result["price_break"] = cur_price > recent_high_10

    # ③ 综合判定
    if result["vol_surge"] and result["price_break"]:
        result["is_breakout"] = True
        if vol_ratio >= 3.0:
            result["label"] = f"🔥爆量突破(量比{vol_ratio:.1f}x, 突破{recent_high_10:.2f})"
        else:
            result["label"] = f"📈放量突破(量比{vol_ratio:.1f}x, 突破{recent_high_10:.2f})"
    elif result["vol_surge"]:
        if vol_ratio >= 3.0:
            result["label"] = f"💥爆量未突破(量比{vol_ratio:.1f}x, 高点{recent_high_10:.2f})"
        else:
            result["label"] = f"📊放量未突破(量比{vol_ratio:.1f}x, 高点{recent_high_10:.2f})"
    elif result["price_break"]:
        result["label"] = f"突破(缩量, 高点{recent_high_10:.2f})"

    return result


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

    # ★ 爆量突破放量检测
    today_volume = bid_m.get("volume", 0)
    vol_break = _detect_volume_breakout(df_hist, cur_price, today_volume)

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
            "vol_break": vol_break,
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
    vb = d.get("vol_break", {})
    vb_tag = ""
    if vb.get("is_breakout"):
        vb_tag = f" {vb['label']}"
    elif vb.get("vol_surge"):
        vb_tag = f" {vb['label']}"
    elif vb.get("price_break"):
        vb_tag = f" {vb['label']}"
    print(f"  {marker} [{signal['first_time']}] "
          f"{d['code']} {d['name']} "
          f"价格={d['price']:.2f} 涨幅={d['change_pct']:.2f}% "
          f"主力={d['main_net_wan']:+.0f}万 "
          f"EXPMA({d['expma10']:.2f}/{d['expma13']:.2f}){expma_tag} "
          f"中期={ms_tag}({d['mid_strength']:+.2f}) "
          f"5日均额={d['avg_trade']:.0f}万 "
          f"概念={d['concept'] or '无'}"
          f"{vb_tag}")


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
    带熔断器：连续失败过多时提前终止，用缓存兜底。
    """
    sector_hits  = {c: [] for c in pool_codes}
    concept_hits = {c: [] for c in pool_codes}
    total = len(pool_codes)
    done = [0]
    consecutive_fail = [0]  # 连续失败计数
    circuit_open = [False]  # 熔断标志

    def worker(code):
        if circuit_open[0]:
            return code, [], []
        sectors, concepts = _fetch_boards_for_stock(code)
        with _cache_lock:
            done[0] += 1
            d = done[0]
            # 检测是否为空结果（API失败）
            if not sectors and not concepts:
                consecutive_fail[0] += 1
            else:
                consecutive_fail[0] = 0
            # 连续10只无数据 → 熔断
            if consecutive_fail[0] >= 10:
                circuit_open[0] = True
                print(f"\n  ⚠️  [频次] 连续{consecutive_fail[0]}只查询失败，触发熔断")
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

    success_count = sum(1 for c in pool_codes if sector_hits[c] or concept_hits[c])
    print(f"    → 个股板块查询完成 {total} 只 (成功{success_count})      ")
    if circuit_open[0]:
        print(f"  ⚠️  [频次] 熔断已触发，{total - success_count}只缺失数据，尝试缓存降级")
    return sector_hits, concept_hits


def analyze_sector_concept_frequency(pool_codes: List[str]) -> Dict[str, Dict]:
    """
    ★ 频次 = 股票出现在多少个板块/概念中, 每进一个 +1, 不加权
    total_freq = sector_count + concept_count
    ★ 盘前模式：自动复用昨日缓存
    """
    result = {}
    for c in pool_codes:
        result[c] = {"sector_count": 0, "concept_count": 0,
                      "total_freq": 0, "sectors": [], "concepts": []}
    if not HAS_AKSHARE or not pool_codes:
        return result

    code_set = set(pool_codes)

    # ① 盘前模式：优先用昨日缓存（板块归属不每日变化）
    if _current_mode == TradingMode.PRE_MARKET_WITH_CACHE:
        yesterday_freq = cache_load("freq", try_yesterday=True)
        if yesterday_freq is not None and isinstance(yesterday_freq, dict):
            print(f"  → [盘前模式] 复用昨日板块频次数据")
            cached_data = yesterday_freq.get("data", {})
            for c in pool_codes:
                if c in cached_data:
                    result[c] = cached_data[c]
            _print_freq_preview(result, pool_codes)
            return result

    # ② 尝试今日缓存
    cache_ttl = get_cache_ttl()
    cached = cache_load("freq", ttl_hours=cache_ttl)
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

    # 检查成功比例，过低则降级到昨日缓存
    success_count = sum(1 for c in pool_codes if sector_hits.get(c) or concept_hits.get(c))
    success_rate = success_count / len(pool_codes) if pool_codes else 0
    if success_rate < 0.3:
        print(f"  ⚠️  [频次] 成功率过低({success_rate:.0%})，尝试降级到昨日缓存")
        yesterday_freq = cache_load("freq", try_yesterday=True)
        if yesterday_freq is not None and isinstance(yesterday_freq, dict):
            cached_data = yesterday_freq.get("data", {})
            for c in pool_codes:
                if c in cached_data:
                    result[c] = cached_data[c]
            print(f"  → [频次] 已降级使用昨日缓存")
            _print_freq_preview(result, pool_codes)
            return result
        else:
            print(f"  → [频次] 无昨日缓存可用，使用本次部分数据")

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
# 第三兜底: THS klines (TCP直连)
# ================================================================

def _tencent_batch_spot(codes: list) -> pd.DataFrame:
    """
    腾讯 qt.gtimg.cn 批量行情 — 不封 IP，零依赖，一次 HTTP GET 拿全。
    返回 DataFrame，列名与腾讯行情兼容。
    """
    if not codes:
        return None
    
    import urllib.request
    import re as _re
    
    # 构建批量查询 URL（~1KB / 150 只股票，远超池数量）
    code_args = []
    for c in codes:
        c6 = c.zfill(6)
        prefix = "sh" if c6.startswith(("6", "5", "9")) else "sz"
        code_args.append(f"{prefix}{c6}")
    
    url = "https://qt.gtimg.cn/q=" + ",".join(code_args)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read().decode("gbk")
    except Exception as e:
        print(f"  → [Tencent] 批量请求失败: {e}")
        return None
    
    rows = []
    for line in raw.strip().split(";"):
        if not line.strip() or "=" not in line:
            continue
        try:
            fields = line.split('"')[1].split("~")
        except (IndexError, ValueError):
            continue
        if len(fields) < 63:
            continue
        try:
            code = fields[2].strip()
            name = fields[1].strip()
            price = float(fields[3] or 0)
            last_close = float(fields[4] or 0)
            open_ = float(fields[5] or 0)
            volume_lot = float(fields[6] or 0)  # 手
            high = float(fields[33] or 0)
            low = float(fields[34] or 0)
            chg_pct = float(fields[32] or 0)    # %
            amount_wan = float(fields[37] or 0)  # 万元
            turnover = float(fields[38] or 0)    # %
            mcap_yi = float(fields[44] or 0)     # 亿元
            fund_flow = float(fields[62]) if len(fields) > 62 and fields[62].strip() else 0.0  # 主力净流入万元
        except (ValueError, IndexError):
            continue
        
        rows.append({
            "代码": code[-6:],      # 纯6位，与 pool 格式一致
            "名称": name,
            "最新价": price,
            "涨跌幅": chg_pct,
            "涨跌额": fields[31].strip() if len(fields) > 31 else 0,
            "成交量": volume_lot,              # 手，与 akshare 单位一致
            "成交额": amount_wan * 10000,       # 万元 → 元
            "振幅": float(fields[43] or 0) if len(fields) > 43 else 0,
            "最高": high,
            "最低": low,
            "今开": open_,
            "昨收": last_close,
            "换手率": turnover,
            "总市值": mcap_yi * 1e8,            # 亿 → 元
            "总市值亿": mcap_yi,                 # 单独存亿元（_get_market_cap直接读，零网络）
            "主力净流入": fund_flow * 10000,       # 万元 → 元（正=流入，负=流出）
        })
    
    if not rows:
        print("  → [Tencent] 无有效行情数据")
        return None
    
    df = pd.DataFrame(rows)
    print(f"  → [Tencent] 批量行情: {len(df)} 只股票")
    return df


# ================================================================
# 缓存版 akshare 封装(涨停池/快照/市场概况 一次拉, 全程复用)
# ================================================================
_df_spot_cache = None
_df_zt_cache = None


def get_spot_df():
    """极速版：内存缓存 + 连接池 + 快速降级 + 盘前缓存复用"""
    global _df_spot_cache
    
    # 1. 检查内存缓存（零耗时，<1ms）
    cache_key = "spot_df"
    if cache_key in _memory_cache:
        return _memory_cache[cache_key]
    
    # 2. 盘前模式：优先用昨日缓存（今日盘前分析需要）
    if _current_mode == TradingMode.PRE_MARKET_WITH_CACHE:
        yesterday_spot = cache_load("spot", try_yesterday=True)
        if yesterday_spot is not None:
            print(f"  → [盘前模式] 复用昨日行情数据 ({len(yesterday_spot)}只)")
            _memory_cache[cache_key] = yesterday_spot
            return yesterday_spot
    
    # 3. 根据模式调整缓存TTL
    cache_ttl = get_cache_ttl()
    
    # 4. 检查本地pickle缓存
    df = cache_load("spot", ttl_hours=cache_ttl)
    if df is not None:
        _df_spot_cache = df
        _memory_cache[cache_key] = df
        return df
    
    # 5. 腾讯API行情（不封IP，零依赖）
    if _GLOBAL_SPOT_CODES:
        print("  → 获取腾讯批量行情...")
        df = _tencent_batch_spot(_GLOBAL_SPOT_CODES)
        if df is not None and not df.empty:
            _df_spot_cache = df
            _memory_cache[cache_key] = df
            cache_save("spot", df)
            return df

    # 6. 快速降级：使用过期缓存（不失败）
    # 注：step 5 已是最新腾讯API查询，无需重复
    if df is not None and not df.empty:
        _df_spot_cache = df
        _memory_cache[cache_key] = df
        cache_save("spot", df)
        return df
    
    # 7. 快速降级：使用过期缓存（不失败）
    df = cache_load("spot", ttl_hours=24, allow_stale=True)
    if df is not None:
        print("⚠️ [降级] 使用过期缓存（API失败）")
        _df_spot_cache = df
        _memory_cache[cache_key] = df
        return df
    
    return None


def get_zt_df():
    """极速版：内存缓存 + 快速降级 + 盘前缓存复用"""
    global _df_zt_cache
    
    # 1. 检查内存缓存
    cache_key = "zt_df"
    if cache_key in _memory_cache:
        return _memory_cache[cache_key]
    
    # 2. 盘前模式：优先用昨日缓存（今日盘前分析需要）
    if _current_mode == TradingMode.PRE_MARKET_WITH_CACHE:
        yesterday_zt = cache_load("zt", try_yesterday=True)
        if yesterday_zt is not None:
            print(f"  → [盘前模式] 复用昨日涨停池 ({len(yesterday_zt)}只)")
            _memory_cache[cache_key] = yesterday_zt
            return yesterday_zt
    
    # 3. 根据模式调整缓存TTL
    cache_ttl = get_cache_ttl()
    
    # 4. 检查本地pickle缓存
    df = cache_load("zt", ttl_hours=cache_ttl)
    if df is not None:
        _df_zt_cache = df
        _memory_cache[cache_key] = df
        return df
    
    # 5. 从腾讯行情筛选涨停股（涨幅≥9.97%）
    df_spot = get_spot_df()
    if df_spot is not None and not df_spot.empty:
        zt_df = df_spot[df_spot["涨跌幅"] >= 9.97].copy()
        if not zt_df.empty:
            _df_zt_cache = zt_df
            _memory_cache[cache_key] = zt_df
            cache_save("zt", zt_df)
            return zt_df

    # 6. 快速降级
    if df is not None:
        _df_zt_cache = df
        _memory_cache[cache_key] = df
        cache_save("zt", df)
        return df
    
    # 6. 快速降级
    df = cache_load("zt", ttl_hours=24, allow_stale=True)
    if df is not None:
        print("⚠️ [降级] 使用过期涨停池缓存")
        _df_zt_cache = df
        _memory_cache[cache_key] = df
        return df
    
    return None


def fetch_bid_metrics_from_akshare() -> dict:
    """极速版：智能NaN处理 + 双模式支持"""
    df = get_spot_df()
    if df is None or df.empty:
        return {}
    
    # 智能NaN处理（根据模式调整策略）
    if _current_mode == TradingMode.INTRADAY:
        # 盘中模式：严格检查，跳过NaN行
        df = df.dropna(subset=["成交量", "最新价", "昨收"])
        print(f"  → [盘中模式] 跳过NaN数据, 剩余{len(df)}只")
    else:
        # 盘后/盘前模式：填充NaN为0
        df = df.fillna({
            "成交量": 0, "最新价": 0, "今开": 0,
            "最高": 0, "最低": 0, "成交额": 0, "昨收": 0,
        })
        print(f"  → [盘后/盘前模式] 填充NaN为0, 共{len(df)}只")
    
    result = {}
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).zfill(6)
        pre_close = float(row.get("昨收", 0))
        if pre_close <= 0: continue
        
        # 安全转换（防止NaN）
        volume = row.get("成交量", 0)
        if pd.isna(volume):
            volume = 0
        else:
            volume = int(float(volume))
        
        amount = row.get("成交额", 0)
        if pd.isna(amount):
            amount = 0.0
        else:
            amount = float(amount)
        
        result[code] = {
            "open": float(row.get("今开", 0) or 0), 
            "pre_close": pre_close,
            "price": float(row.get("最新价", 0) or 0),
            "high": float(row.get("最高", 0) or 0),
            "low": float(row.get("最低", 0) or 0),
            "volume": volume,
            "amount": amount,
            "bid1": 0, "bid1_vol": 0, "ask1": 0, "ask1_vol": 0,
            "main_net_inflow": float(row.get("主力净流入", 0) or 0),
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
def check_hard_reject(gap, vol, jzb, is_limit_up=False, skip_vol_check=False):
    now = datetime.now()
    is_pre = (now.hour == 9 and now.minute < 30) or now.hour < 9
    if not is_pre and not skip_vol_check and vol < 0.5:
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
    dt_count = 0  # 东财接口已删除，跌停数缺失时不过热惩罚（偏保守）
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
    zt_count = market_info.get("zt_count", 0)
    pct = abs(stock.get("涨跌幅", 0))
    if zt_count > 70 and pct < 9.97:   # 涨停股豁免过热惩罚
        s -= 15; notes.append(f"过热(涨停{zt_count}家)")
    if jzb > 25: s += 15; notes.append(f"主力积极(jzb={jzb:.1f}%)")
    elif jzb > 15: s += 10
    return s, "|".join(notes)


def _score_sector_relative(code: str, stock: dict) -> Tuple[int, str]:
    """
    相对评分：在全市场内对比涨幅/换手率排名，
    弥补盘后绝对数据缺失造成的评分集中化。
    top20% → +12分，top40% → +6分，top60% → +3分
    """
    s, note = 0, []
    df_spot = get_spot_df()
    if df_spot is None or df_spot.empty:
        return s, ""

    pct = float(stock.get("涨跌幅", 0) or 0)
    turnover = float(stock.get("换手率", 0) or 0)

    # 涨幅相对排名
    pct_series = df_spot["涨跌幅"].dropna()
    if len(pct_series) > 10 and pct != 0:
        rank = (pct_series < pct).sum() / len(pct_series)  # 越大表示越强
        if rank >= 0.80:  s += 10; note.append(f"涨幅领先前{int((1-rank)*100)}%")
        elif rank >= 0.60: s += 5;  note.append(f"涨幅偏强前{int((1-rank)*100)}%")
        elif rank >= 0.40: s += 2

    # 换手率相对排名
    turnover_series = df_spot["换手率"].dropna()
    if len(turnover_series) > 10 and turnover > 0:
        rank_v = (turnover_series < turnover).sum() / len(turnover_series)
        if rank_v >= 0.80:  s += 6;  note.append(f"换手活跃前{int((1-rank_v)*100)}%")
        elif rank_v >= 0.60: s += 3

    return s, ",".join(note)


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
    ths_score, ths_note = 0, "THS无数据"
    
    # ① THS 同花顺直取主力净流入（TCP官方通道，不封IP）
    fund = ths.get_fund_flow(code)
    ext = ths.get_extended(code)
    if fund:
        main_net = float(fund.get("main_net_inflow", 0))
        if main_net > 5e6:
            ths_score += 15; ths_note = f"主力净流入{main_net/1e4:.0f}万"
        elif main_net > 1e6:
            ths_score += 8; ths_note = f"主力小幅流入"
        elif main_net < -5e6:
            ths_score -= 12; ths_note = f"主力净流出{main_net/1e4:.0f}万"
        # PE 补充评分
        if ext:
            pe_ths = float(ext.get("pe_ttm", 0))
            if 5 < pe_ths < 30:
                ths_score += 8
                ths_note += f" PE合理({pe_ths:.1f})"
        return ths_score, ths_note
    
    # ② THS无数据→腾讯兜底（key名修复：主力净流入字段）
    main_net = float(stock.get("主力净流入", 0) or 0)
    if abs(main_net) > 1:
        if main_net > 5e6: ths_score += 15; ths_note = f"腾讯:主力净流入{main_net/1e4:.0f}万"
        elif main_net < -5e6: ths_score -= 12; ths_note = f"腾讯:主力净流出{main_net/1e4:.0f}万"
        return ths_score, ths_note
    
    # ③ 腾讯也无数据→新浪资金流向API第三兜底
    fund_sina = _fetch_sina_fund_flow(code)
    if fund_sina:
        netamount = float(fund_sina.get("netamount", 0))
        if netamount > 5e6:
            ths_score += 15
            ths_note = f"新浪:主力净流入{netamount/1e4:.0f}万"
        elif netamount < -5e6:
            ths_score -= 12
            ths_note = f"新浪:主力净流出{netamount/1e4:.0f}万"
        return ths_score, ths_note
    
    # 所有数据源都失败了
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
    """热点板块加分（东财接口已删除，暂不可用）"""
    return s, ""


def _adjust_for_big_orders(s, code):
    orders = ths.get_big_orders(code)
    if not orders: return s, ""
    net = orders.get("buy_large", 0) - orders.get("sell_large", 0)
    if net > 1e6: return s + 10, f"大单净买入{net/1e4:.0f}万"
    if net < -1e6: return s - 10, f"大单净卖出{net/1e4:.0f}万"
    return s, ""


def _adjust_for_divergence(s, code):
    try:
        now = datetime.now()
        today_ak = now.strftime("%Y%m%d")
        start_ak = now.replace(day=1).strftime("%Y%m%d")
        df = _fetch_kline_unified(code, start_ak, today_ak)
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
    pre_close = bid_m.get("pre_close", 0)

    # ★ 盘前模式：竞价量分析（不依赖今开，用昨日已筛池+竞价量判抢筹/出货）
    if _current_mode == TradingMode.PRE_MARKET_WITH_CACHE:
        auction = get_auction_cache().get(code)
        # ★ 缓存无数据或 bid=0 时，实时从 THS 补采（修复: 旧缓存因字段名错误全为0）
        if not auction or auction.get("bid_after_920", 0) <= 0:
            auction = _realtime_auction_from_ths(code)
        if not auction or auction.get("bid_after_920", 0) <= 0:
            return -100, "无竞价数据", {}
        bid_vol = auction["bid_after_920"]
        ask_vol = auction.get("ask_after_920", 0)
        open_price = auction.get("open_price", 0)
        cur = open_price if open_price > 0 else pre_close
        if pre_close <= 0:
            return -100, "无昨收", {}
        gap = (cur - pre_close) / pre_close * 100
        vol = (bid_vol + ask_vol) / 10000
        if bid_vol > ask_vol * 1.2:
            qiangchou, qc_note = True, f"抢筹(买{bid_vol:.0f}>卖{ask_vol:.0f})"
        elif ask_vol > bid_vol * 1.2:
            qiangchou, qc_note = False, f"出货(卖{ask_vol:.0f}>买{bid_vol:.0f})"
        else:
            qiangchou, qc_note = None, f"中性(买{bid_vol:.0f}/卖{ask_vol:.0f})"
        total_auction = bid_vol + ask_vol
        jzb = (bid_vol / total_auction * 100) if total_auction > 0 else 0
        s = 0
        notes = [qc_note]
        if qiangchou is True:
            s += 30; notes.append("真实抢筹")
        elif qiangchou is False:
            s -= 30; notes.append("出货嫌疑")
        if gap > 3: s += 15; notes.append(f"高开{gap:+.1f}%")
        elif gap > 0: s += 8
        elif gap < 0: s -= 10; notes.append(f"低开{gap:+.1f}%")
        if jzb > 60: s += 15; notes.append(f"主力占比{jzb:.0f}%")
        elif jzb > 50: s += 8
        if qiangchou is False and gap < -2:
            return -100, "硬否决: 出货+低开", {"reason": "出货+低开", "jzb": jzb, "qiangchou": qiangchou}
        all_notes = f"竞价:{qc_note}|占比:{jzb:.0f}%|缺口:{gap:+.1f}%|量:{vol:.1f}万手|" + "|".join(notes[1:])
        details = {
            "reason": all_notes, "jzb": jzb, "qiangchou": qiangchou,
            "bid_vol": bid_vol, "ask_vol": ask_vol, "gap": gap,
            "auction_used": True, "limit_up": False,
        }
        return s, all_notes, details

    # ★ 盘中/尾盘模式：原有逻辑（依赖今开）
    if not bid_m or pre_close <= 0:
        return -100, "无竞价数据", {}
    open_p = bid_m.get("open", 0)
    cur = bid_m.get("price", 0) or open_p
    vol = bid_m.get("volume", 0) / 10000
    # 优先用集合竞价缓存数据
    auction = get_auction_cache().get(code)
    # ★ 缓存bid=0时，仅在 PRE/INTRADAY 模式实时补采；POST_CLOSE 跳过THS调用（bid_metrics已足够）
    if not auction or auction.get("bid_after_920", 0) <= 0:
        if _current_mode in (TradingMode.PRE_MARKET_WITH_CACHE, TradingMode.INTRADAY, TradingMode.PRE_MARKET):
            auction = _realtime_auction_from_ths(code)
    if auction and auction.get("bid_after_920", 0) > 0:
        total_auction = auction["bid_after_920"] + auction.get("ask_after_920", 0)
        jzb = (auction["bid_after_920"] / total_auction * 100) if total_auction > 0 else 5.0
        # 竞价开盘价更准确
        if auction.get("open_price", 0) > 0 and cur == 0:
            cur = auction["open_price"]
    else:
        total = bid_m.get("bid1_vol", 0) + bid_m.get("ask1_vol", 0)
        jzb = (bid_m.get("bid1_vol", 0) / total * 100) if total > 0 else 5.0
    limit_up = is_limit_up(code, cur, pre_close)
    gap = (cur - pre_close) / pre_close * 100

    rej, reason = check_hard_reject(gap, vol, jzb, limit_up)
    if rej: return -100, f"硬否决: {reason}", {"reason": reason, "limit_up": limit_up, "jzb": jzb}

    rej2, reason2 = check_soft_reject(code, pre_close, open_p, cur, vol, stock, jzb)
    if rej2: return -80, f"柔性否决: {reason2}", {"reason": reason2, "limit_up": limit_up, "jzb": jzb}

    # 加载历史K线(用于EXPMA/中期强度/均额) — 统一数据源
    df_hist = None
    try:
        today_ak = datetime.now().strftime("%Y%m%d")
        month_ago = (datetime.now().replace(day=1)).strftime("%Y%m%d")
        df_hist = _fetch_kline_unified(code, month_ago, today_ak)
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

    # ★ 爆量突破放量加分
    if limit_signal:
        vb = limit_signal.get("达标", {}).get("vol_break", {})
        if vb.get("is_breakout"):
            if vb["vol_ratio"] >= 3.0:
                sig_bonus += 15; sig_notes.append(f"🔥爆量突破(量比{vb['vol_ratio']:.1f}x)")
            else:
                sig_bonus += 10; sig_notes.append(f"📈放量突破(量比{vb['vol_ratio']:.1f}x)")
        elif vb.get("vol_surge"):
            sig_bonus += 5; sig_notes.append(f"放量未突破(量比{vb['vol_ratio']:.1f}x)")
        elif vb.get("price_break"):
            sig_bonus += 3; sig_notes.append("缩量突破")

    s_val, note_val, extra_val = score_valuation_and_ths(code, stock)
    s_freq, note_freq = freq_to_score(freq_info)

    # ★ 相对评分：板块内排名（弥补盘后数据质量不足导致评分集中D级）
    s_rel, note_rel = _score_sector_relative(code, stock)

    base = s_mom + s_sen + s_val + s_freq + s_rel

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
        + (f"|相对:{note_rel}" if note_rel else "")
        + (f"|龙头:{dragon_note}" if dragon_note else "")
        + (f"|信号:{'; '.join(sig_notes)}" if sig_notes else "")
        + (f"|调节:{'; '.join(adj_notes)}" if adj_notes else "")
    )
    details = {
        "reason": all_notes, "base": base, "dragon_bonus": dragon_bonus,
        "adjustment": adj, "limit_up": limit_up, "jzb": jzb,
        "auction_used": auction is not None and auction.get("bid_after_920", 0) > 0,
        "freq_score": s_freq, "freq_note": note_freq,
        "rel_score": s_rel, "rel_note": note_rel,
        **extra_val,
        "limit_signal": limit_signal, "sig_bonus": sig_bonus, "sig_notes": sig_notes,
    }
    return s, all_notes, details


# ================================================================
# HTML 报告
# ================================================================
def generate_html(results, market_temp, market_info, freq_data, elapsed_scan, elapsed_total, shadow_details=None, pool=None, enriched=None):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    notes_html = "".join(f"<li>{r['notes']}</li>" for r in results if r.get("notes"))

    # ====== 主表：深度数据表格（匹配图片格式） ======
    rows = []
    intro_items = []  # 底部备注列表
    for r in results:
        code = r["code"]
        sig = r["signal"]
        cls = {"S": "signal-s", "A": "signal-a", "B": "signal-b",
               "C": "signal-c", "D": "signal-d"}.get(sig, "signal-d")
        en = (enriched or {}).get(code, {})

        # 基础字段
        name = r["name"]
        price = r.get("price", 0) or 0
        chg = 0.0  # 涨幅从 spot_df 获取（r中没有）
        mcap = r.get("flow_market_cap", 0) or 0

        # 涨幅
        chg_str = "0.00%"
        chg_cls = ""
        if pool:
            for s in pool:
                if s.get("code") == code:
                    chg = s.get("price", 0) or 0
                    pc = s.get("pre_close", 0) or 0
                    if pc > 0:
                        chg_pct = (chg - pc) / pc * 100
                        chg_str = f"{chg_pct:+.2f}%"
                        chg_cls = "chg-up" if chg_pct > 0 else ("chg-down" if chg_pct < 0 else "")
                    break

        # 主力净额（万元）
        ff = en.get("fund_flow", 0) or 0
        if ff > 10000:
            ff_str = f"+{ff/10000:.2f}亿"
            ff_cls = "chg-up"
        elif ff > 0:
            ff_str = f"+{ff:.2f}万"
            ff_cls = "chg-up"
        elif ff < -10000:
            ff_str = f"{ff/10000:.2f}亿"
            ff_cls = "chg-down"
        elif ff < 0:
            ff_str = f"{ff:.2f}万"
            ff_cls = "chg-down"
        else:
            ff_str = "-"
            ff_cls = ""

        # 资金比
        fr = en.get("fund_ratio", 0) or 0
        fr_str = f"{fr:+.2f}%" if abs(fr) > 0.001 else "-0.00%"
        fr_cls = "chg-up" if fr > 0 else ("chg-down" if fr < 0 else "")

        # 资金（成交额）
        amount_yuan = 0.0
        if pool:
            for s in pool:
                if s.get("code") == code:
                    amount_yuan = float(s.get("amount", 0) or 0)
                    break
        if amount_yuan >= 1e8:
            amount_str = f"{amount_yuan/1e8:.1f}亿"
        elif amount_yuan >= 1e4:
            amount_str = f"{amount_yuan/1e4:.1f}万"
        else:
            amount_str = f"{amount_yuan:.0f}"

        # 中线
        ml = en.get("midline", 50) or 50
        ml_str = f"{ml:.1f}"

        # 筹码
        chip = en.get("chip", 50) or 50
        chip_str = f"{chip:.0f}"

        # 人气
        pop = en.get("popularity", 0) or 0
        pop_str = f"{pop}"

        # 机构%
        inst = en.get("institution_pct", 0) or 0
        inst_str = f"{inst:.1f}%" if inst > 0 else "-"

        # 资金面
        ff_face = en.get("fund_face", 5) or 5
        ff_face_str = f"{ff_face:.1f}"

        # 概念
        concepts = en.get("concepts", []) or []
        concepts_str = "; ".join(concepts[:5]) if concepts else "-"
        if len(concepts) > 5:
            concepts_str += f" (+{len(concepts)-5})"

        rows.append(
            f"<tr class='{cls}'>"
            f"<td>{code}</td>"
            f"<td>{name}</td>"
            f"<td>{price:.2f}</td>"
            f"<td class='{chg_cls}'>{chg_str}</td>"
            f"<td class='{ff_cls}'>{ff_str}</td>"
            f"<td class='{fr_cls}'>{fr_str}</td>"
            f"<td>{amount_str}</td>"
            f"<td>{ml_str}</td>"
            f"<td>{chip_str}</td>"
            f"<td>{pop_str}</td>"
            f"<td>{inst_str}</td>"
            f"<td>{ff_face_str}</td>"
            f"<td class='concept-cell'>{concepts_str}</td>"
            f"</tr>"
        )

        # 底部备注
        intro = en.get("company_intro", "") or ""
        if intro:
            intro_text = intro.replace("\u3000", " ").strip()[:120]
            intro_items.append(f"<li><strong>{code} {name}</strong>：{intro_text}</li>")

    # ====== 频次/影子详情表（保留原有） ======
    freq_rows2 = []
    shadow_rows = []
    for code, info in sorted(freq_data.items(), key=lambda x: x[1].get("total_freq", 0), reverse=True):
        if info.get("total_freq", 0) <= 0 or "code_set" in str(info):
            continue
        name = next((s.get("name", "") for s in (pool or []) if s.get("code") == code), "")
        sectors_html = "".join(f"<span class='tag tag-s'>{s}</span>" for s in info.get("sectors", [])[:10])

        # Shadow pattern detail
        shadow_info = (shadow_details or {}).get(code, {})
        star = shadow_info.get("star_level", "")
        sc_val = shadow_info.get("shadow_count", 0)
        nc_val = shadow_info.get("new_count", 0)
        mc_val = shadow_info.get("market_cap", 0)
        sigs = []
        ssc = shadow_info.get("shadow_conditions", {})
        snc = shadow_info.get("new_conditions", {})
        if snc.get("E_涨停破前高"):
            sigs.append("BREAK_HIGH")
        if snc.get("D_横盘突破涨停"):
            sigs.append("CONSOLIDATION_LU")
        if ssc.get("A_月内有涨停"):
            sigs.append("HAS_LU")
        if ssc.get("B_有倍量交易"):
            sigs.append("HAS_SPIKE")
        sigs_str = ", ".join(sigs[:3])
        shadow_rows.append(
            f"<tr><td>{code}</td><td>{name}</td>"
            f"<td>{star}</td><td>{sc_val}/7</td><td>{nc_val}/5</td>"
            f"<td>{mc_val:.0f}</td><td>{sigs_str}</td></tr>"
        )

        concepts_html = "".join(f"<span class='tag tag-c'>{c}</span>" for c in info.get("concepts", [])[:15])
        more_s = f"<span class='tag-more'>+{len(info.get('sectors',[]))-10}个</span>" if len(info.get('sectors', [])) > 10 else ""
        more_c = f"<span class='tag-more'>+{len(info.get('concepts',[]))-15}个</span>" if len(info.get('concepts', [])) > 15 else ""
        freq_rows2.append(
            f"<tr><td>{code}</td><td>{name}</td>"
            f"<td class='freq-num'>{info.get('total_freq', 0)}</td>"
            f"<td>{info.get('sector_count', 0)}</td><td>{info.get('concept_count', 0)}</td>"
            f"<td class='tags-cell'>{sectors_html}{more_s}</td>"
            f"<td class='tags-cell'>{concepts_html}{more_c}</td></tr>"
        )

    intro_html = f"<div class='notes'><h3><span class='icon-bulb'>\ud83d\udca1</span> 公司简介及备注</h3><ul>{"".join(intro_items) if intro_items else '<li style="color:#484f58">暂无数据</li>'}</ul></div>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>尾盘竞价选股结果 {now_str}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Microsoft YaHei','PingFang SC',sans-serif; background:#0d1117; color:#c9d1d9; padding:24px 32px; }}
.header {{ display:flex; align-items:baseline; gap:16px; margin-bottom:4px; }}
h1 {{ color:#e3b341; font-size:20px; }}
.meta {{ color:#8b949e; margin-bottom:16px; font-size:12px; }}
.perf {{ background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px 16px; margin-bottom:20px; display:flex; gap:24px; font-size:12px; }}
.perf-item {{ color:#8b949e; }} .perf-item strong {{ color:#3fb950; font-size:14px; }}
table {{ border-collapse:collapse; width:100%; font-size:12px; }}
th {{ background:#1c2128; color:#e6edf3; padding:8px 6px; border:1px solid #30363d; position:sticky; top:0; z-index:10; white-space:nowrap; }}
td {{ padding:6px; border:1px solid #30363d; text-align:center; white-space:nowrap; }}
tr:nth-child(even) {{ background:#161b22; }}
tr:hover {{ background:#1f2937; }}
.signal-s {{ border-left:3px solid #f85149; }}
.signal-a {{ border-left:3px solid #f0883e; }}
.signal-b {{ border-left:3px solid #3fb950; }}
.signal-c {{ border-left:3px solid #58a6ff; }}
.signal-d {{ border-left:3px solid #484f58; opacity:0.6; }}
.chg-up {{ color:#f85149; font-weight:bold; }}
.chg-down {{ color:#3fb950; font-weight:bold; }}
.section-title {{ color:#e3b341; font-size:15px; margin:28px 0 10px; border-bottom:1px solid #30363d; padding-bottom:5px; }}
.freq-table th {{ color:#e3b341; }}
.tags-cell {{ max-width:400px; line-height:1.8; white-space:normal; }}
.concept-cell {{ max-width:220px; white-space:normal; text-align:left; font-size:11px; color:#8b949e; }}
.tag {{ display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px; margin:1px 2px; }}
.tag-s {{ background:#1c2333; color:#58a6ff; border:1px solid #30363d; }}
.tag-c {{ background:#1c2133; color:#d2a8ff; border:1px solid #3d2f55; }}
.tag-more {{ color:#484f58; font-size:9px; }}
.notes {{ margin-top:24px; background:#161b22; border-radius:8px; padding:16px; }}
.notes h3 {{ color:#e3b341; margin-bottom:8px; font-size:14px; }}
.notes .icon-bulb {{ font-size:18px; margin-right:6px; }}
.notes ul {{ padding-left:18px; font-size:12px; color:#8b949e; line-height:1.7; }}
.notes ul li {{ margin-bottom:4px; }}
.notes ul li strong {{ color:#58a6ff; }}
.footer {{ margin-top:24px; color:#484f58; font-size:11px; text-align:center; }}
.freq-num {{ font-weight:bold; color:#e3b341; }}
</style>
</head>
<body>
<div class="header">
  <h1>\u5c3e\u76d8\u7ade\u4ef7\u9009\u80a1\u7ed3\u679c</h1>
  <span style="color:#8b949e;font-size:12px">v3.5 + \u6df1\u5ea6\u5bcc\u5316</span>
</div>
<p class="meta">\u751f\u6210: {now_str} | \u6a21\u5f0f: {_current_mode.value} | \u6e29\u5ea6: {market_temp:.1f} | {json.dumps(market_info, ensure_ascii=False)}</p>
<div class="perf">
  <span class="perf-item">\u9891\u6b21\u626b\u63cf <strong>{elapsed_scan:.0f}s</strong></span>
  <span class="perf-item">\u603b\u8017\u65f6 <strong>{elapsed_total:.0f}s</strong></span>
  <span class="perf-item">\u80a1\u7968 <strong>{len(results)} \u53ea</strong></span>
</div>
<table>
<tr>
  <th>\u4ee3\u7801</th><th>\u540d\u79f0</th><th>\u73b0\u4ef7</th><th>\u6da8\u5e45</th>
  <th>\u4e3b\u529b\u51c0\u989d</th><th>\u8d44\u91d1\u6bd4</th><th>\u8d44\u91d1</th>
  <th>\u4e2d\u7ebf</th><th>\u7b79\u7801</th><th>\u4eba\u6c14</th>
  <th>\u673a\u6784%</th><th>\u8d44\u91d1\u9762</th><th>\u6240\u5c5e\u6982\u5ff5</th>
</tr>
{"".join(rows)}
</table>

<h2 class="section-title">Shadow Pattern Details</h2>
<table class="freq-table">
<tr><th>Code</th><th>Name</th><th>Level</th><th>Shadow7</th><th>New5</th><th>CapB</th><th>Signals</th></tr>
{"".join(shadow_rows)}
</table>

<h2 class="section-title">Mainline Frequency</h2>
<table class="freq-table">
<tr><th>Code</th><th>Name</th><th>TotalFreq</th><th>Sector</th><th>Concept</th><th>Sectors</th><th>Concepts</th></tr>
{"".join(freq_rows2)}
</table>

{intro_html}

<div class="notes" style="margin-top:12px"><h3>\u5206\u6790\u8fc7\u7a0b</h3><ul>{notes_html}</ul></div>
<p class="footer">v3.5 + \u6df1\u5ea6\u5bcc\u5316 | \u9891\u6b21=\u884c\u4e1a+\u6982\u5ff5\u6bcf\u6761\u7ebf+1 | \u5bcc\u5316\u6570\u636e\u4ec5\u5bf9\u9009\u80a1\u6c60\u83b7\u53d6</p>
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
    # 1. 尝试从缓存加载K线数据
    cache_key = f"kline_{code}"
    cached = cache_load(cache_key, ttl_hours=24)
    if cached is not None:
        return cached.get("passed", False), cached.get("ytd_pct", 0.0), cached.get("has_limit_up", False)
    
    try:
        # 日期格式：新浪/东财用 YYYYMMDD，baostock 用 YYYY-MM-DD
        today_ak      = datetime.now().strftime("%Y%m%d")
        year_start_ak = f"{datetime.now().year}0101"
        today_bs      = datetime.now().strftime("%Y-%m-%d")
        year_start_bs = f"{datetime.now().year}-01-01"

        # 数据源优先级：① Sina裸HTTP（~120ms，16线程） → ② Baostock → ③ THS
        # ① Sina 裸HTTP（无V8，支持并行，90%成功率）
        df = _fetch_kline_from_sina_raw(code)

        # ② Baostock 兜底（不限频，历史数据全）
        if df is None or len(df) < 2:
            df = _fetch_kline_from_baostock(code, year_start_bs, today_bs)

        # ③ THS 最终兜底（TCP直连）
        if df is None or len(df) < 2:
            df = _fetch_kline_from_ths(code, year_start_bs, today_bs)

        if df is None or df.empty or len(df) < 2:
            # 静默失败，不打印（预取阶段已处理过失败股票）
            result = (False, 0.0, False)
            result = (False, 0.0, False)
            cache_save(cache_key, {"passed": False, "ytd_pct": 0.0, "has_limit_up": False})
            return result

        # 年涨幅 = (最新收盘 - 年初收盘) / 年初收盘
        start_close = float(df.iloc[0]["收盘"])
        end_close   = float(df.iloc[-1]["收盘"])
        if start_close <= 0:
            result = (False, 0.0, False)
            cache_save(cache_key, {"passed": False, "ytd_pct": 0.0, "has_limit_up": False})
            return result
        ytd_pct = (end_close - start_close) / start_close * 100

        # 近期涨停: 最近 20 个交易日内，任一日涨幅 >= 9.9%（创业板/科创板 >= 19.9%）
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
        result = (passed, ytd_pct, has_limit_up)
        # ★ 盘前Sina无今日数据，不覆盖昨日有效缓存
        today_str = datetime.now().strftime("%Y-%m-%d")
        has_today = any(str(d)[:10] == today_str for d in df['日期'].values)
        if has_today:
            cache_save(cache_key, {"passed": passed, "ytd_pct": ytd_pct, "has_limit_up": has_limit_up})
        return result

    except Exception as e:
        print(f"  -> 筛选查询失败({code}): {e}")
        result = (False, 0.0, False)
        cache_save(cache_key, {"passed": False, "ytd_pct": 0.0, "has_limit_up": False})
        return result


def _fetch_kline_from_sina_raw(code: str) -> Optional[pd.DataFrame]:
    """
    直接调 Sina K线 JSON 接口（不经过 akshare V8 引擎，~120ms/只，支持16线程）
    字段: day, open, high, low, close, volume, ma_price5, ma_volume5
    返回 DataFrame（字段名兼容 akshare 格式）
    """
    import urllib.request, json
    s = 'sh' + code if code.startswith(('6', '5', '9')) else 'sz' + code
    url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php'
           f'/CN_MarketData.getKLineData?symbol={s}&scale=240&ma=5&datalen=200')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'})
        resp = urllib.request.urlopen(req, timeout=12)
        raw = json.loads(resp.read().decode('utf-8'))
        if not raw:
            return None
        df = pd.DataFrame(raw)
        # 统一列名（兼容原有逻辑）
        df = df.rename(columns={
            'day': '日期', 'close': '收盘', 'open': '开盘',
            'high': '最高', 'low': '最低',
            'volume': '成交量', 'ma_price5': 'MA5'
        })
        for col in ['收盘', '开盘', '最高', '最低', '成交量']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        # 涨跌幅需要自己算
        if '收盘' in df.columns and len(df) >= 2:
            df['涨跌幅'] = df['收盘'].pct_change() * 100
        df = df.sort_values('日期').reset_index(drop=True)
        return df
    except Exception:
        return None


def _build_bid_metrics_from_spot(spot_df: pd.DataFrame, pool_codes: list) -> dict:
    """
    从腾讯批量行情 spot_df 构建 bid_metrics（替代 akshare 东财接口）。
    spot_df 已有: 昨收(字段4), 今开(字段5), 最新价(字段3), 成交量(字段6)
    盘中/盘前均可正常工作。
    """
    if spot_df is None or spot_df.empty:
        return {}
    bm = {}
    for _, row in spot_df.iterrows():
        code = str(row.get('代码', '')).zfill(6)
        if code not in pool_codes:
            continue
        price = float(row.get('最新价', 0) or 0)
        prev_close = float(row.get('昨收', 0) or 0)
        open_p = float(row.get('今开', 0) or 0)
        vol = float(row.get('成交量', 0) or 0)
        if price <= 0 and prev_close > 0:
            price = prev_close
        bm[code] = {
            'price': price,
            'pre_close': prev_close,
            'open': open_p,
            'volume': vol * 100,
        }
    return bm


def _prefetch_klines_parallel(codes: list, max_workers: int = 16) -> None:
    """
    并行预取K线数据（多线程，Sina裸HTTP接口 ~120ms/只）
    16线程: 1247只 ≈ 2.6分钟 | 缓存命中率: 90%
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    _fetch_lock = threading.Lock()

    def _fetch_one(code: str):
        try:
            df = _fetch_kline_from_sina_raw(code)
            if df is None or len(df) < 2:
                return code, None

            # 计算年涨幅和近期涨停
            closes = df['收盘'].values
            if closes[-1] <= 0 or closes[0] <= 0:
                return code, None
            ytd_pct = (closes[-1] - closes[0]) / closes[0] * 100

            has_limit_up = False
            for chg in df['涨跌幅'].tail(20).dropna():
                chg = float(chg)
                if code.startswith(('30', '68')):
                    if chg >= 19.9: has_limit_up = True; break
                else:
                    if chg >= 9.9: has_limit_up = True; break

            passed = (ytd_pct > 30) and has_limit_up
            cache_key = f"kline_{code}"
            # ★ 盘前(09:15-09:25)不写今日缓存：Sina此时无今日数据，
            #   写空数据会毒化昨日有效缓存，导致 prev_close=0 全部D级
            today_str = datetime.now().strftime("%Y-%m-%d")
            has_today = any(str(d)[:10] == today_str for d in df['日期'].values)
            with _fetch_lock:
                if has_today:
                    # 有今日数据才更新缓存（盘中/盘后）
                    cache_save(cache_key, {
                        "passed": passed, "ytd_pct": ytd_pct,
                        "has_limit_up": has_limit_up, "df": df,
                        "cached_at": time.time()
                    })
                else:
                    # 盘前: 不覆盖昨日有效缓存，让筛选用历史数据
                    existing = cache_load(cache_key, ttl_hours=999)
                    if existing is None:
                        # 无历史缓存才写（容许全新环境）
                        cache_save(cache_key, {
                            "passed": passed, "ytd_pct": ytd_pct,
                            "has_limit_up": has_limit_up, "df": df,
                            "cached_at": time.time()
                        })
            return code, df
        except Exception:
            return code, None

    print(f"[预取] 并行获取K线数据 ({len(codes)} 只，{max_workers}线程，Sina裸接口)...")
    t0 = time.time()
    done = [0]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, c): c for c in codes}
        for future in as_completed(futures):
            code, _ = future.result()
            done[0] += 1
            d = done[0]
            if d % 200 == 0 or d == len(codes):
                print(f"  → 预取进度 {d}/{len(codes)}...", end="\r")

    elapsed = time.time() - t0
    hit = sum(1 for c in codes if cache_load(f"kline_{c}", ttl_hours=24) is not None)
    print(f"\n[预取] 完成: {done[0]}只 in {elapsed:.1f}s ({elapsed/done[0]*1000:.0f}ms/只), {hit}只缓存命中")


def filter_pool_by_ytd_and_limit_up(pool: list) -> list:
    """
    从股票池中筛选: 今年涨幅>30% 且 近期有涨停
    """
    if not pool:
        return pool

    codes = [s["code"] for s in pool]

    # ★ 并行预取K线（~1分钟），消除串行网络瓶颈
    _prefetch_klines_parallel(codes, max_workers=16)

    print(f"[筛选] 检查年涨幅>30% + 近期涨停 ({len(pool)} 只)...")
    filtered = []
    for i, stock in enumerate(pool):
        code = stock["code"]
        passed, ytd_pct, has_lu = _check_ytd_and_limit_up(code)
        status = "[通过]" if passed else "[失败]"
        print(f"    [{i+1}/{len(pool)}] {code} {stock.get('name',''):<6s} "
              f"年涨幅={ytd_pct:+.1f}%  涨停={'有' if has_lu else '无'}  {status}")
        if passed:
            stock["ytd_pct"] = ytd_pct
            filtered.append(stock)
        # 每500只主动GC一次，防止内存累积
        if (i + 1) % 500 == 0:
            gc.collect()

    print(f"[筛选] 通过: {len(filtered)}/{len(pool)} 只")
    return filtered


# ================================================================
# 股池加载
# ================================================================
def _fetch_hot_pool_via_tencent(min_chg_pct: float = 3.0) -> list:
    """
    腾讯API批量查价 → 筛选热门股（涨幅>=min_chg_pct）
    74批×80只≈10秒，完全替代akshare涨停池，不被封IP
    返回pool格式：[{"code", "name", "price", "pre_close", "volume", "amount", "涨跌幅"}]
    结果按涨幅降序排列。
    """
    # 1. 获取全量股票列表
    stocks = _load_all_stocks_from_baostock()
    if not stocks:
        return []
    codes = [s["code"] for s in stocks]
    print(f"[pool] Baostock 全量: {len(codes)} 只，开始腾讯批量查询...")

    import urllib.request

    pool = []
    batch_size = 80
    total_batches = (len(codes) + batch_size - 1) // batch_size

    for batch_idx in range(0, len(codes), batch_size):
        batch = codes[batch_idx:batch_idx + batch_size]
        code_args = []
        for c in batch:
            prefix = "sh" if c.startswith(("6", "5", "9")) else "sz"
            code_args.append(f"{prefix}{c}")

        url = "https://qt.gtimg.cn/q=" + ",".join(code_args)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            raw = resp.read().decode("gbk")
        except Exception as e:
            batch_num = batch_idx // batch_size + 1
            print(f"  → [腾讯] 第{batch_num}/{total_batches}批查询失败: {e}")
            continue

        for line in raw.strip().split(";"):
            if not line or "=" not in line:
                continue
            try:
                fields = line.split('"')[1].split("~")
                code = fields[2].strip()[-6:]
                name = fields[1].strip()
                price = float(fields[3] or 0)
                last_close = float(fields[4] or 0)
                chg_pct = float(fields[32] or 0)
                volume_lot = float(fields[6] or 0)
                amount_wan = float(fields[37] or 0) if len(fields) > 37 else 0
                mcap_yi = float(fields[44] or 0) if len(fields) > 44 else 0
                if chg_pct >= min_chg_pct:
                    # 排除科创板(688)/创业板(300)
                    if code.startswith('688') or code.startswith('300'):
                        continue
                    pool.append({
                        "code": code,
                        "name": name,
                        "price": price,
                        "pre_close": last_close,
                        "volume": int(volume_lot),
                        "amount": amount_wan * 10000,
                        "涨跌幅": chg_pct,
                        "market_cap": mcap_yi,
                    })
            except (ValueError, IndexError):
                continue

    pool.sort(key=lambda x: x["涨跌幅"], reverse=True)

    limit_up = sum(1 for s in pool if s["涨跌幅"] >= 9.8)
    strong = sum(1 for s in pool if 7.0 <= s["涨跌幅"] < 9.8)
    watch = sum(1 for s in pool if 3.0 <= s["涨跌幅"] < 7.0)
    print(f"[pool] 腾讯批量筛选完成: {len(pool)} 只（涨幅>={min_chg_pct}%）")
    print(f"  → 涨停(≥9.8%): {limit_up}只 / 强势(7-9.8%): {strong}只 / 关注(3-7%): {watch}只")
    if pool:
        top5 = [f"{s['name']}({s['涨跌幅']:+.1f}%)" for s in pool[:5]]
        print(f"  → 涨幅前5: {', '.join(top5)}")

    return pool


def _load_pool():
    # 涨停/热门池(走缓存) — TTL=24h，盘后缓存供明早竞价复用
    cached_zt = cache_load("pool_zt", ttl_hours=24)
    if cached_zt is not None:
        # 排除科创板(688)/创业板(300)，兼容旧缓存
        filtered = [s for s in cached_zt if not str(s.get("code","")).startswith(("688","300"))]
        removed = len(cached_zt) - len(filtered)
        print(f"[pool] 缓存池: {len(cached_zt)} 只, 去除科创/创业板: {removed} 只, 剩余: {len(filtered)} 只")
        return filtered

    # 腾讯API批量查价筛选热门股（涨停+强势+关注）
    pool = _fetch_hot_pool_via_tencent(min_chg_pct=3.0)
    if pool:
        cache_save("pool_zt", pool)
        print(f"  → [缓存] 已保存 pool_zt ({len(pool)} 只)")
        return pool

    # 最终兜底：Baostock全量（无价格筛选）
    print("[pool] 腾讯筛选失败，从 Baostock 获取全量A股兜底...")
    pool = _load_all_stocks_from_baostock()
    if pool:
        cache_save("pool_zt", pool)
    return pool



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


def _load_all_stocks_from_baostock():
    """从 Baostock 获取全量A股作为默认股票池
    优先用当日日期，没有数据则尝试最近交易日
    ⚠ 必须在每次 rs.next() 后立即调用 get_row_data() 否则会无限循环
    """
    global _baostock_logged_in
    if not _baostock_logged_in:
        _baostock_logged_in = _init_baostock()
    if not _baostock_logged_in:
        return []

    def _is_stock_code(code):
        code6 = code.split('.')[-1]
        if code.startswith('sh.') and code6[0] == '6':
            # 排除科创板(688)、保留主板(600/601/603/605)
            if code6.startswith('688'):
                return False
            return True
        if code.startswith('sz.') and not code6.startswith('399'):
            # 排除创业板(300)、保留主板(000/001/002)
            if code6.startswith('300'):
                return False
            return True
        return False

    try:
        now = datetime.now()
        stocks = []
        for i in range(0, 11):
            d = now - __import__('datetime').timedelta(days=i)
            date_str = d.strftime('%Y-%m-%d')
            rs = bs.query_all_stock(date_str)
            if rs is None or rs.error_code != '0':
                continue
            batch = []
            _safety = 0
            while rs.next() and _safety < 8000:
                row = rs.get_row_data()  # ★ 必须调用！否则无限循环
                _safety += 1
                code = row[0].strip()
                name = row[2].strip()
                if _is_stock_code(code):
                    code6 = code.split('.')[-1].zfill(6)
                    batch.append({
                        "code": code6, "name": name,
                        "price": 0, "pre_close": 0,
                        "volume": 0, "amount": 0
                    })
            if len(batch) > 3000:
                stocks = batch
                print(f"[pool] Baostock 全量股票: {len(stocks)} 只（日期: {date_str}）")
                break
            elif len(batch) > 0:
                print(f"[pool] Baostock 查询 {date_str}: {len(batch)} 只（不足，继续尝试）")

        if stocks:
            cache_save("pool_zt", stocks)
        else:
            print("[pool] Baostock 所有日期查询均无A股数据")
        return stocks
    except Exception as e:
        print(f"[pool] Baostock 全量股票获取失败: {e}")
        return []


# ================================================================
# 深度数据富化（对选股池补充主力净额/概念/机构%/中线等字段）
# ================================================================

def _batch_wencai_enrich(codes: list) -> dict:
    """
    批量问财查询：概念、机构%、公司简介 分批调用（每批最多50只）
    返回 {code6: {concepts, institution_pct, company_intro}}
    """
    if not codes:
        return {}
    result = {}
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        try:
            ths._ensure_connected()
            if not ths.connected or ths._ths is None:
                continue
            codes_str = ",".join(batch)
            raw = ths._ths.wencai_nlp(f"{codes_str} 所属概念,机构持股比例,公司简介")
            if not (hasattr(raw, 'success') and raw.success and raw.data):
                continue
            for item in raw.data:
                scode = str(item.get("股票代码", "")).split(".")[0].zfill(6)
                concepts_raw = item.get("所属概念", "")
                inst_pct = 0.0
                for k, v in item.items():
                    if "机构持股" in k and v is not None:
                        try:
                            inst_pct = float(v)
                        except (ValueError, TypeError):
                            pass
                intro = item.get("公司简介", "")
                result[scode] = {
                    "concepts": [c.strip() for c in concepts_raw.split(";") if c.strip()] if concepts_raw else [],
                    "institution_pct": inst_pct,
                    "company_intro": intro,
                }
        except Exception as e:
            print(f"[问财] 批次{i//batch_size+1}查询失败: {e}")
    return result


def _get_fund_flow(code: str) -> float:
    """
    THS big_order_flow 计算主力净流入（万元）
    通过 os.dup2 屏蔽 THS SDK 的 stderr WARNING 输出
    """
    import os
    try:
        ths._ensure_connected()
        if not ths.connected or ths._ths is None:
            return 0.0
        tc = _code6_to_thscode(code)
        # 临时重定向 stderr/stdout 到 /devnull，屏蔽 THS SDK 的 WARNING
        devnull = os.open(os.devnull, os.O_WRONLY)
        old_stderr = os.dup(2)
        old_stdout = os.dup(1)
        os.dup2(devnull, 2)
        os.dup2(devnull, 1)
        try:
            raw = ths._ths.big_order_flow(tc)
        finally:
            os.dup2(old_stderr, 2)
            os.dup2(old_stdout, 1)
            os.close(devnull)
            os.close(old_stderr)
            os.close(old_stdout)
        if not (hasattr(raw, 'success') and raw.success and raw.data):
            return 0.0
        total_in = 0.0
        total_out = 0.0
        for r in raw.data:
            direction = r.get('成交方向', 0)
            amount = float(r.get('总金额', 0) or 0)
            if direction > 0:
                total_in += amount
            elif direction < 0:
                total_out += amount
        net = total_in - total_out
        return net / 10000.0  # 元 -> 万元
    except Exception as e:
        return 0.0


# 全局中线缓存（进程内，避免重复计算）
_MIDLINE_CACHE = {}


def _calc_midline_from_kline(code: str) -> float:
    """
    从K线缓存计算中线指标（MA20偏离度得分 0-100）
    50为基准，偏离+5% = 55，偏离-5% = 45
    优先用专用 midline 缓存（ttl=24h），其次K线缓存兜底
    """
    if code in _MIDLINE_CACHE:
        return _MIDLINE_CACHE[code]
    try:
        # 优先：专用中线缓存
        cached = cache_load(f"midline_{code}", ttl_hours=24)
        if cached is not None and isinstance(cached, (int, float)):
            _MIDLINE_CACHE[code] = float(cached)
            return _MIDLINE_CACHE[code]
        kline_df = None
        # 其次：K线缓存
        kline_df = cache_load(f"kline_{code}", ttl_hours=24)
        if kline_df is None or (hasattr(kline_df, 'empty') and kline_df.empty) or (hasattr(kline_df, '__len__') and len(kline_df) < 20):
            # 尝试从新浪拉取
            from datetime import datetime as _dt, timedelta as _td
            end_s = _dt.now().strftime('%Y-%m-%d')
            start_s = (_dt.now() - _td(days=60)).strftime('%Y-%m-%d')
            kline_df = _fetch_kline_from_sina(code, start_s.replace('-',''), end_s.replace('-',''))
            if kline_df is None or (hasattr(kline_df, 'empty') and kline_df.empty) or (hasattr(kline_df, '__len__') and len(kline_df) < 20):
                kline_df = _fetch_kline_from_baostock(code, start_s, end_s)
        if kline_df is None:
            score = 50.0
        elif hasattr(kline_df, 'empty') and kline_df.empty:
            score = 50.0
        else:
            closes = []
            if isinstance(kline_df, pd.DataFrame):
                closes = kline_df['收盘'].tail(20).tolist()
            elif isinstance(kline_df, list):
                for r in kline_df[-20:]:
                    if isinstance(r, dict):
                        closes.append(float(r.get('收盘', r.get('收盘价', 0)) or 0))
            if len(closes) < 20:
                score = 50.0
            else:
                ma20 = sum(closes) / 20
                cur = closes[-1]
                if ma20 <= 0:
                    score = 50.0
                else:
                    deviation = (cur / ma20 - 1) * 100
                    score = max(0.0, min(100.0, 50 + deviation))
        _MIDLINE_CACHE[code] = score
        cache_save(f"midline_{code}", score)
        return score
    except Exception:
        _MIDLINE_CACHE[code] = 50.0
        return 50.0


class _SuppressTHS:
    """上下文管理器：临时将 stderr/stdout 重定向到 devnull，屏蔽 THS SDK 的 WARNING 输出"""
    def __enter__(self):
        import os
        self._devnull = os.open(os.devnull, os.O_WRONLY)
        self._old_err = os.dup(2)
        self._old_out = os.dup(1)
        os.dup2(self._devnull, 2)
        os.dup2(self._devnull, 1)
        return self
    def __exit__(self, *args):
        import os
        os.dup2(self._old_err, 2)
        os.dup2(self._old_out, 1)
        os.close(self._devnull)
        os.close(self._old_err)
        os.close(self._old_out)


def _enrich_pool(pool: list, spot_df) -> dict:
    """
    对影子筛选后的股票池补充深度数据（只对选股池中的股票）
    返回 enriched: {code: {fund_flow, fund_ratio, turnover, midline, chip, popularity, inst_pct, fund_face, concepts, intro}}
    """
    import os

    codes = [s["code"] for s in pool]
    if not codes:
        return {}

    enriched = {}

    # 1. spot_df 索引（含换手率）
    spot_dict = {}
    if spot_df is not None and hasattr(spot_df, 'iterrows'):
        for _, row in spot_df.iterrows():
            c = str(row.get("代码", "")).zfill(6)
            spot_dict[c] = row

    # 2. 批量问财：概念 + 机构% + 简介（分批，每批50只）— 屏蔽 THS SDK 输出
    print(f"[富化] 批量问财 {len(codes)} 只 ...")
    t0 = time.time()
    with _SuppressTHS():
        wencai_data = _batch_wencai_enrich(codes)
    print(f"[富化] 问财完成: {len(wencai_data)} 只 ({time.time()-t0:.1f}s)")

    # 3. 中线(MA20) — 有进程内缓存，重复调用极快（不涉及 THS）
    print(f"[富化] 中线计算 {len(codes)} 只 ...")
    t0 = time.time()
    midline_scores = {}
    for code in codes:
        midline_scores[code] = _calc_midline_from_kline(code)
    print(f"[富化] 中线完成 ({time.time()-t0:.1f}s)")

    # 4. 主力净额（big_order_flow）
    # ★ AFTER_HOURS 模式：big_order_flow 全部超时，返回0（昨日盘后无资金流数据）
    skip_fund_flow = (_current_mode == TradingMode.AFTER_HOURS)
    if skip_fund_flow:
        print(f"[富化] 主力净额: 盘后模式跳过（big_order_flow 无历史数据）")
        fund_flows = {code: 0.0 for code in codes}
    else:
        print(f"[富化] 主力净额采集 {len(codes)} 只 ...")
        t0 = time.time()
        fund_flows = {}
        for i, code in enumerate(codes):
            with _SuppressTHS():
                fund_flows[code] = _get_fund_flow(code)
            if (i + 1) % 10 == 0:
                print(f"  -> {i+1}/{len(codes)}")
        print(f"[富化] 主力净额完成 ({time.time()-t0:.1f}s)")

    # 5. 人气排序
    volumes = {}
    for code in codes:
        if code in spot_dict:
            sp = spot_dict[code]
            volumes[code] = float(sp.get("成交量", 0) or 0)
        else:
            volumes[code] = 0
    ranked = sorted(volumes, key=volumes.get, reverse=True)
    popularity_map = {code: rank + 1 for rank, code in enumerate(ranked)}

    # 6. 组装
    for code in codes:
        wc = wencai_data.get(code, {})
        sp = spot_dict.get(code, {})
        ff = fund_flows.get(code, 0.0)
        turnover_pct = float(sp.get("换手率", 0) or 0) if code in spot_dict else 0.0
        amount_yuan = float(sp.get("成交额", 0) or 0) if code in spot_dict else 0.0

        # 资金比 = 主力净额(万元*10000) / 成交额(元) * 100
        fund_ratio = (ff * 10000 / amount_yuan * 100) if amount_yuan > 0 else 0.0

        # 资金面 0-10 分段评分
        abs_ff = abs(ff)
        if abs_ff > 10000:
            fund_face = 9.0 if ff > 0 else 1.0
        elif abs_ff > 5000:
            fund_face = 7.5 if ff > 0 else 2.5
        elif abs_ff > 1000:
            fund_face = 6.0 if ff > 0 else 4.0
        elif ff > 0:
            fund_face = 5.5
        elif ff < 0:
            fund_face = 4.5
        else:
            fund_face = 5.0

        # 筹码 = 100 - 换手率（换手率越高筹码越分散）
        chip = max(0, min(100, 100 - turnover_pct))

        enriched[code] = {
            "fund_flow": ff,                  # 主力净额 万元
            "fund_ratio": fund_ratio,         # 资金比 %
            "turnover": turnover_pct,         # 换手率 %
            "midline": midline_scores.get(code, 50.0),  # 中线得分
            "chip": chip,                     # 筹码
            "popularity": popularity_map.get(code, 0),  # 人气排名
            "institution_pct": wc.get("institution_pct", 0),  # 机构%
            "fund_face": fund_face,           # 资金面
            "concepts": wc.get("concepts", []),  # 概念列表
            "company_intro": wc.get("company_intro", ""),  # 备注
        }

    print(f"[富化] 完成: {len(enriched)} 只")
    return enriched


# ================================================================
# 主流程
# ================================================================
def main():
    print("=" * 64)
    print("  尾盘拉升选股器 v3.5 — 盘中/盘后双模式")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  模式: {_current_mode.value}")
    print("=" * 64)
    
    # 东财连接池已删除（全部改用腾讯/Baostock/THS）
    
    # 显示缓存策略
    cache_ttl = get_cache_ttl()
    print(f"  → [缓存策略] TTL={cache_ttl*60:.0f}分钟")
    if _current_mode == TradingMode.PRE_MARKET_WITH_CACHE:
        print(f"  → [盘前模式] 将复用昨日缓存数据")
    
    t_all = time.time()

    # 1. 股票池
    pool = _load_pool()
    if not pool:
        print("[错误] 无可用股票池，退出"); return

    # ★ 过滤退市股/停牌股：不在实时行情中的股票直接排除
    _df_spot_filter = get_spot_df()
    if _df_spot_filter is not None and not _df_spot_filter.empty:
        _spot_dict = {_row["代码"]: _row for _, _row in _df_spot_filter.iterrows()}
        pool_before = len(pool)
        pool = [s for s in pool if s["code"] in _spot_dict]
        removed = pool_before - len(pool)
        if removed > 0:
            print(f"[筛选] 去除退市/停牌/无行情股票: {removed} 只（剩余 {len(pool)} 只）")
        del _df_spot_filter, _spot_dict
    gc.collect()

    # ★ 年涨幅>30% + 近期涨停 筛选（盘后模式跳过）
    if _current_mode == TradingMode.AFTER_HOURS:
        print(f"[筛选] 盘后模式，跳过K线筛选（API不可用）")
        print(f"[筛选] 保留全部 {len(pool)} 只股票（将使用其他筛选条件）")
    else:
        pool = filter_pool_by_ytd_and_limit_up(pool)
        if not pool:
            print("[筛选] 无符合条件的股票，退出"); return
    pool_codes = [s["code"] for s in pool]
    global _GLOBAL_SPOT_CODES
    _GLOBAL_SPOT_CODES = pool_codes  # 供 get_spot_df() 腾讯兜底使用

    # 2. ★ 板块/概念频次(带缓存 + 并行, 每条线+1)
    print("[1/5] 板块/概念频次统计...")
    t_scan = time.time()
    freq_data = analyze_sector_concept_frequency(pool_codes)
    elapsed_scan = time.time() - t_scan
    print(f"  → 频次扫描耗时: {elapsed_scan:.1f}s")

    # 3. 实时行情(走缓存快照)
    print("[2/5] 获取实时行情...")

    # bid_metrics 从腾讯批量行情构建（昨收/今开/现价全部来自字段[4][5][3]，akshare被封不再影响）
    spot_df = get_spot_df()
    # ★ 合并 market_cap 回 pool（fix: spot_df["总市值亿"] 拿到了但从未写入 pool，导致主表市值全=0）
    _pool_code_set = {s['code'] for s in pool}
    for _, row in spot_df.iterrows():
        code = str(row.get('代码', '')).zfill(6)
        if code in _pool_code_set:
            for s in pool:
                if s['code'] == code:
                    s['market_cap'] = float(row.get('总市值亿', 0) or 0)
                    break
    bid_metrics = _build_bid_metrics_from_spot(spot_df, pool_codes)
    print(f"  -> 腾讯实时行情: {len(bid_metrics)} 只")

    # 4. 市场概况
    print("[3/5] 市场概况...")
    market_temp, _, market_info = score_market_temperature()
    print(f"  → 温度: {market_temp:.1f}")

    # ★ 集合竞价采集（09:15-09:25 持续采集）
    auction_cache = get_auction_cache()
    if not auction_cache:
        now_h = datetime.now().hour
        now_m = datetime.now().minute
        now_min = now_h * 60 + now_m
        if 9*60+15 <= now_min <= 9*60+25:
            print(f"[3.5/5] 集合竞价持续采集（{len(pool_codes)} 只, 窗口09:20-09:25）...")
            while True:
                cur_min = datetime.now().hour * 60 + datetime.now().minute
                if cur_min < 9*60+20:
                    wait_sec = (9*60+20 - cur_min) * 60
                    print(f"  → 等待至09:20（约{wait_sec}s）")
                    time.sleep(min(wait_sec, 30))
                    continue
                if cur_min >= 9*60+25:
                    break
                # 采集一轮
                collected = 0
                codes_list = list(pool_codes)
                for i, code6 in enumerate(codes_list):
                    tc = _code6_to_thscode(code6)
                    try:
                        ths._ensure_connected()
                        if not ths.connected or ths._ths is None:
                            continue
                        raw = ths._ths.call_auction(tc)
                        records = (raw.data if hasattr(raw, 'success') and raw.success and raw.data
                                   else raw if isinstance(raw, list) else [])
                        if records and isinstance(records, list) and len(records) > 0:
                            time_sorted = sorted(records, key=lambda x: str(x.get("时间", x[0] if isinstance(x, (list,tuple)) else "")))
                            def _g(r, k): return float(r[k]) if isinstance(r, dict) and k in r else float(r[{"时间":0,"价格":1,"买2量":2,"卖2量":3,"当前量":4}.get(k,1)]) if isinstance(r, (list,tuple)) and len(r) > 4 else 0
                            def _ts_to_min(ts_val):
                                s = str(ts_val)
                                if len(s) == 10:  # Unix timestamp
                                    import datetime as _dt
                                    dt = _dt.datetime.fromtimestamp(int(s))
                                    return dt.hour * 60 + dt.minute
                                elif len(s) >= 5:  # HH:MM or HH:MM:SS string
                                    return int(s[:2]) * 60 + int(s[3:5])
                                return 0
                            after_920 = [x for x in time_sorted if _ts_to_min(x.get("时间",0)) >= 560]  # 560=9*60+20
                            bid920 = sum(_g(x,"买2量") for x in after_920)
                            ask920 = sum(_g(x,"卖2量") for x in after_920)
                            open_p = _g(time_sorted[-1], "价格")
                            auction_cache[code6] = {
                                "open_price": open_p,
                                "bid_after_920": bid920,
                                "ask_after_920": ask920,
                            }
                            collected += 1
                            if (i + 1) % 10 == 0:
                                time.sleep(0.3)
                    except Exception:
                        pass
                # 保存到磁盘
                if collected > 0:
                    cache_path = os.path.join(_CACHE_DIR, f"auction_{datetime.now().strftime('%Y-%m-%d')}.pkl")
                    with open(cache_path, "wb") as f:
                        pickle.dump(auction_cache, f)
                    global _AUCTION_CACHE
                    _AUCTION_CACHE = auction_cache
                    print(f"  → 本轮采集: {collected}/{len(codes_list)} 只")
                else:
                    print(f"  → 本轮无数据（THS可能未推送）")
                cur_min = datetime.now().hour * 60 + datetime.now().minute
                if cur_min >= 9*60+25:
                    break
                time.sleep(60)  # 每60秒采集一轮
        else:
            print(f"[3.5/5] 跳过竞价采集（非9:15-9:25窗口, 当前{now_h:02d}:{now_m:02d}）")
    else:
        print(f"[3.5/5] 竞价缓存已存在（{len(auction_cache)} 只）")

    # Shadow pattern stock selection
    if _current_mode == TradingMode.PRE_MARKET_WITH_CACHE:
        # 严格用昨日已筛池：优先加载单独缓存的筛选结果
        cached_filtered = cache_load("pool_filtered", ttl_hours=12)
        if cached_filtered is not None and len(cached_filtered) > 0:
            print(f"[4/5] 复用昨日已筛池: {len(cached_filtered)} 只（跳过重筛）")
            pool = cached_filtered
            shadow_details = {s["code"]: s.get("shadow_result", {}) for s in pool}
        else:
            print(f"[4/5] Shadow pattern analysis ({len(pool)} stocks)...")
            pool, shadow_details = filter_by_shadow_pattern(pool)
            if pool:
                cache_save("pool_filtered", pool)
    else:
        print(f"[4/5] Shadow pattern analysis ({len(pool)} stocks)...")
        pool, shadow_details = filter_by_shadow_pattern(pool)
        if pool:
            cache_save("pool_filtered", pool)
    if not pool:
        print("[Filter] No stocks passed shadow filter, exit"); return

    # 释放不再需要的大对象
    _memory_cache.clear()
    del auction_cache
    gc.collect()

    # ★ 4.5/5 深度数据富化（只对选股池中的股票）
    print(f"[4.5/5] 深度数据富化 ({len(pool)} 只)...")
    enriched = _enrich_pool(pool, spot_df)

    # 5. Scoring
    print(f"[5/5] Scoring ({len(pool)} stocks)...")
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
            "flow_market_cap": stock.get("market_cap", 0), **details,
        })

    results.sort(key=lambda x: (
        freq_data.get(x["code"], {}).get("total_freq", 0), x["score"],
    ), reverse=True)

    # 6. 报告
    elapsed_total = time.time() - t_all
    print(f"[6/6] Generating report... (total {elapsed_total:.1f}s)")
    html = generate_html(results, market_temp, market_info, freq_data, elapsed_scan, elapsed_total, shadow_details, pool, enriched)
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tail_auction_result.html")
    try:
        with open(html_path, "w", encoding="utf-8", errors='replace') as f:
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
    bs.logout()
    print("[成功] 已退出 Baostock")


if __name__ == "__main__":
    main()

def _fetch_kline_from_ths(code, start_date, end_date):
    """THS klines 获取K线（TCP直连，不封IP）"""
    try:
        raw = ths.get_klines_raw(code, start_date, end_date)
        if raw and isinstance(raw, list) and len(raw) > 0:
            # 转 DataFrame
            import pandas as _pd
            df = _pd.DataFrame(raw)
            # 映射列名
            col_map = {"时间": "date", "开盘": "open", "收盘": "close",
                       "最高": "high", "最低": "low",
                       "成交量": "volume", "成交额": "amount"}
            df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)
            if "date" in df.columns:
                df["date"] = df["date"].astype(str)
            return df
    except Exception:
        pass
    return None

