#!/usr/bin/env python3
"""
NY3.0 — A股数据抓取 + 筹码分析一体化工具
═══════════════════════════════════════════════════
整合自:
  - nylo.py (A股数据爬虫)
  - 分析筹码_主板筛选V3.py (集合竞价筹码分析)
  - run.py (一键运行)

功能:
  1. 股票名称→代码搜索
  2. 实时行情（批量）
  3. 日K线历史（1000天）
  4. 行业板块列表 + 成分股
  5. 股票详情（市值/量比/换手率）
  6. 板块K线 / 大盘指数
  7. 集合竞价筹码分析（抢筹/出货判断）
  8. 多策略池筛选（基础池/量价池/趋势池/技术池）
  9. HTML报告生成

用法:
  python3 NY3.0.py                                    # 全市场抓取+分析（一键模式）
  python3 NY3.0.py 600519 000858                      # 分析指定股票
  python3 NY3.0.py 茅台 五粮液                         # 用名称也行
  python3 NY3.0.py --codes 600519 000858              # 只抓指定股票数据
  python3 NY3.0.py --search 茅台                      # 搜索股票代码
  python3 NY3.0.py --test                             # 测试各接口连通性
  python3 NY3.0.py --data-dir stock_data              # 从本地数据离线分析
  python3 NY3.0.py --data-dir stock_data --html r.html  # 离线分析+指定报告路径
"""

import requests
import re
import json
import time
import random
import os
import sys
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from dataclasses import dataclass, field
import argparse
import html as html_module
import glob as _glob
import subprocess
import threading
import asyncio
try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False


# ════════════════════════════════════════════════════
# NYLO — A股数据爬虫模块
# ════════════════════════════════════════════════════

# ════════════════════════════════════════════════════
# 浏览器模拟配置
# ════════════════════════════════════════════════════

# 多套 User-Agent 轮换，模拟不同浏览器/设备
USER_AGENTS = [
    # Chrome Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Chrome Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# 多套 Accept 头，与浏览器类型匹配
_ACCEPT_HEADERS = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "*/*",
]

def _random_ua():
    return random.choice(USER_AGENTS)

def _random_accept():
    return random.choice(_ACCEPT_HEADERS)

def _jitter_delay(base: float = 0.3, scale: float = 0.15) -> float:
    """指数分布延迟：均值=base，偶尔长停顿模拟真人"""
    return random.expovariate(1.0 / base) + scale

def _build_session():
    """构建带 Cookie 的 requests.Session，模拟真实浏览器会话"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _random_ua(),
        "Accept": _random_accept(),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=30, max=100",
    })
    # 连接池复用，减少握手开销
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=0,  # 由 safe_request 控制重试
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

# ════════════════════════════════════════════════════
# 限速器（防封IP核心）
# ════════════════════════════════════════════════════

class RateLimiter:
    """令牌桶限速器：确保两次请求之间有最小间隔（线程安全）"""
    def __init__(self, min_interval: float = 0.3):
        self._min_interval = min_interval
        self._last_time = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_time
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_time = time.monotonic()

# 每个域名独立限速
_limiter_sina = RateLimiter(0.3)      # 新浪：~3次/秒
_limiter_tencent = RateLimiter(0.2)   # 腾讯：~5次/秒
_limiter_eastmoney = RateLimiter(0.5)  # 东财：~2次/秒（防封核心）


def safe_request(url: str, limiter: RateLimiter, session: requests.Session = None,
                 max_retries: int = 3, encoding: str = None, **kwargs) -> Optional[requests.Response]:
    """
    带限速 + 指数退避 + 浏览器伪装的安全请求
    """
    if session is None:
        session = _build_session()

    kwargs.setdefault("timeout", 20)
    # 每次请求随机换 UA
    session.headers["User-Agent"] = _random_ua()

    last_err = None
    for attempt in range(max_retries):
        limiter.wait()
        try:
            r = session.request("GET", url, **kwargs)

            # 429 限流 → 指数退避
            if r.status_code == 429:
                wait = (2 ** attempt) + random.uniform(1, 3)
                print(f"    ⏳ 被限流(429)，等待 {wait:.1f}s...")
                time.sleep(wait)
                last_err = "HTTP 429"
                continue

            # 5xx 服务端错误 → 重试
            if r.status_code >= 500:
                wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(wait)
                last_err = f"HTTP {r.status_code}"
                continue

            # 成功：设置编码后返回
            # 注意：必须在访问 r.text 之前设置 encoding，否则 requests 会用
            # Content-Type 头的 charset 或 apparent_encoding 缓存解码结果
            if encoding:
                r.encoding = encoding
            return r

        except requests.exceptions.Timeout:
            last_err = "timeout"
            if attempt < max_retries - 1:
                time.sleep((2 ** attempt) + random.uniform(0.5, 1))
        except requests.exceptions.ConnectionError:
            last_err = "connection error"
            if attempt < max_retries - 1:
                time.sleep((2 ** (attempt + 2)) + random.uniform(2, 4))  # 更长退避: 4-8s, 8-12s
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries - 1:
                time.sleep(1)

    print(f"    ❌ 请求失败({max_retries}次重试): {last_err}")
    return None


# ════════════════════════════════════════════════════
# 1. 股票搜索（名称→代码）
# ════════════════════════════════════════════════════

def search_stock(keyword: str, session: requests.Session = None) -> Optional[dict]:
    """
    通过腾讯智能提示接口搜索股票
    返回: {"code": "600519", "name": "贵州茅台", "market": "sh"}
    """
    if session is None:
        session = _build_session()

    # 方案A: 腾讯智能提示
    try:
        r = safe_request(
            "https://smartbox.gtimg.cn/s3/",
            _limiter_tencent, session,
            params={"v": "2", "q": keyword, "t": "gp"},
        )
        if r:
            text = r.content.decode("gbk", errors="replace")
            m = re.search(r'v_hint="(.+)"', text)
            if m:
                raw = m.group(1)
                try:
                    decoded = json.loads('"' + raw.replace('"', '\\"') + '"')
                except Exception:
                    decoded = raw
                for item in decoded.split(";"):
                    parts = item.split("~")
                    if len(parts) >= 3 and parts[1].isdigit() and len(parts[1]) == 6:
                        return {"code": parts[1], "name": parts[2], "market": parts[0]}
    except Exception as e:
        print(f"    ⚠ 腾讯搜索异常: {e}")

    # 方案B: 新浪搜索（备用）
    try:
        r = safe_request(
            f"https://suggest3.sinajs.cn/suggest/key={keyword}",
            _limiter_sina, session,
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        if r:
            r.encoding = "utf-8"
            m = re.search(r'"(.+)"', r.text)
            if m:
                parts = m.group(1).split(",")
                for i in range(0, len(parts) - 1, 2):
                    tag = parts[i]
                    name = parts[i + 1]
                    if tag.startswith("gp"):
                        code = tag[2:]
                        market = "sh" if code.startswith(("6", "9")) else "sz"
                        return {"code": code, "name": name, "market": market}
    except Exception as e:
        print(f"    ⚠ 新浪搜索异常: {e}")

    return None


# ════════════════════════════════════════════════════
# 2. 实时行情（腾讯批量接口）
# ════════════════════════════════════════════════════

def fetch_quotes_batch(codes: list[str], session: requests.Session = None) -> dict:
    """
    批量获取腾讯实时行情（一次请求最多约80只）
    返回: {code: {name, price, prev_close, volume, ...}}
    """
    if session is None:
        session = _build_session()

    def _to_sym(code):
        return f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"

    results = {}
    batch_size = 80

    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        symbols = ",".join(_to_sym(c) for c in batch)

        r = safe_request(
            f"https://qt.gtimg.cn/q={symbols}",
            _limiter_tencent, session,
        )
        if not r:
            continue

        # 直接用 GBK 解码原始字节，绕过 requests 的编码检测（避免 Content-Type charset 干扰）
        text = r.content.decode("gbk", errors="replace")
        for line in text.strip().split("\n"):
            m = re.search(r'v_(\w+)="(.+)"', line)
            if not m:
                continue
            fields = m.group(2).split("~")
            if len(fields) < 50:
                continue

            code = fields[2]

            def _v(idx, t=float):
                try:
                    return t(fields[idx]) if fields[idx] else (t(0) if t != str else "")
                except (ValueError, IndexError):
                    return t(0) if t != str else ""

            results[code] = {
                "name": fields[1],
                "code": code,
                "price": _v(3),
                "prev_close": _v(4),
                "open": _v(5),
                "volume": _v(6, int),       # 成交量（手）
                "buy_vol": _v(7, int),      # 外盘（手）
                "sell_vol": _v(8, int),     # 内盘（手）
                "bid1_p": _v(9),            # 买一价
                "bid1_v": _v(10, int),      # 买一量
                "ask1_p": _v(19),           # 卖一价
                "ask1_v": _v(20, int),      # 卖一量
                "change_pct": _v(32),       # 涨跌幅%
                "high": _v(33),
                "low": _v(34),
                "amount": _v(37),           # 成交额（万元）
                "turnover": _v(38),         # 换手率%
                "amplitude": _v(43),        # 振幅%
                "market_cap_yi": _v(45),    # 流通市值（亿）
                "volume_ratio_api": _v(49), # 量比
                "quote_time": fields[31] if len(fields) > 31 else "",
            }

        # 指数分布延迟，模拟真人翻页节奏
        if i + batch_size < len(codes):
            time.sleep(_jitter_delay(0.3, 0.1))

    return results


# ════════════════════════════════════════════════════
# 3. 日K线历史（新浪 + 东财双源）
# ════════════════════════════════════════════════════

def fetch_kline_sina(code: str, days: int = 10, session: requests.Session = None) -> list[dict]:
    """新浪日K线（快速，适合少量请求）"""
    if session is None:
        session = _build_session()

    sym = f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"
    r = safe_request(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        _limiter_sina, session,
        params={"symbol": sym, "scale": "240", "ma": "no", "datalen": days},
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    if not r or not r.text.strip() or r.text.strip() == "null":
        return []
    try:
        data = json.loads(r.text)
        return data if data else []
    except json.JSONDecodeError:
        return []


def fetch_kline_tencent(code: str, days: int = 300, session: requests.Session = None) -> list[dict]:
    """
    腾讯日K线（web.ifzq.gtimg.cn，支持前复权）
    返回: [{"day", "open", "close", "high", "low", "volume"}, ...]
    """
    if session is None:
        session = _build_session()

    sym = f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")

    r = safe_request(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        _limiter_tencent, session,
        params={"param": f"{sym},day,{start},{end},{days},qfq", "_var": "kline_dayqfq"},
        headers={"Referer": "https://web.ifzq.gtimg.cn/"},
    )
    if not r:
        return []

    try:
        txt = r.text.split("=", 1)[1] if "=" in r.text else r.text
        data = json.loads(txt)
        klines = data.get("data", {}).get(sym, {})
        klines = klines.get("day") or klines.get("qfqday") or []
        if klines:
            return [{"day": k[0], "open": k[1], "close": k[2],
                     "high": k[3], "low": k[4], "volume": k[5]} for k in klines]
    except Exception:
        pass
    return []


def fetch_kline_batch(codes: list[str], days: int = 1000, max_workers: int = 4,
                      session: requests.Session = None) -> dict:
    """
    批量获取K线数据（新浪为主，腾讯补救）
    返回: {code: [kline_data]}
    """
    if session is None:
        session = _build_session()

    results = {}

    def _fetch_one(code):
        # 新浪K线（快速，支持最多约1024条）
        klines = fetch_kline_sina(code, days=min(days, 1000), session=session)
        if not klines:
            # 腾讯K线备用
            klines = fetch_kline_tencent(code, days=min(days, 300), session=session)
        return code, klines

    print(f"    📦 批量获取K线: {len(codes)} 只, {days}天, {max_workers}线程...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, c): c for c in codes}
        done = 0
        for future in as_completed(futures):
            code, klines = future.result()
            if klines:
                results[code] = klines
            done += 1
            if done % 100 == 0:
                print(f"    进度: {done}/{len(codes)} ({len(results)} 有数据)")

    print(f"    ✅ K线完成: {len(results)}/{len(codes)} 只有数据")
    return results


# ── 异步K线批量抓取（提速核心）──


async def _async_fetch_kline_sina(session, code: str,
                                   days: int, semaphore) -> list[dict]:
    """异步新浪K线"""
    sym = f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": sym, "scale": "240", "ma": "no", "datalen": days}
    headers = {"Referer": "https://finance.sina.com.cn/", "User-Agent": _random_ua(), "Accept": _random_accept()}

    async with semaphore:
        try:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    raise ConnectionAbortedError("429")  # 让自适应限速器检测到
                if resp.status != 200:
                    return []
                text = await resp.text()
                if not text.strip() or text.strip() == "null":
                    return []
                data = json.loads(text)
                return data if data else []
        except ConnectionAbortedError:
            raise  # 429错误向上抛出
        except Exception:
            return []


async def _async_fetch_kline_tencent(session, code: str,
                                      days: int, semaphore) -> list[dict]:
    """异步腾讯K线"""
    sym = f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{sym},day,{start},{end},{days},qfq", "_var": "kline_dayqfq"}
    headers = {"Referer": "https://web.ifzq.gtimg.cn/", "User-Agent": _random_ua(), "Accept": _random_accept()}

    async with semaphore:
        try:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    raise ConnectionAbortedError("429")  # 让自适应限速器检测到
                if resp.status != 200:
                    return []
                text = await resp.text()
                txt = text.split("=", 1)[1] if "=" in text else text
                data = json.loads(txt)
                klines = data.get("data", {}).get(sym, {})
                klines = klines.get("day") or klines.get("qfqday") or []
                if klines:
                    return [{"day": k[0], "open": k[1], "close": k[2],
                             "high": k[3], "low": k[4], "volume": k[5]} for k in klines]
        except ConnectionAbortedError:
            raise  # 429错误向上抛出
        except Exception:
            pass
        return []


async def _async_fetch_kline_batch(codes: list[str], days: int = 1000,
                                    concurrency: int = 100) -> dict:
    """
    异步批量K线抓取（双域名分流 + 自适应限速）
    ───────────────────────────────────────────
    策略：正常时跑满安全线，429限流自动降速，恢复后自动提速
    新浪：基础18 req/s，抖动±40%模拟真人，被限流降到3 req/s，恢复后逐步回升
    腾讯：基础30 req/s，同策略
    """
    semaphore = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    results = {}
    done_count = 0
    fail_count = 0
    t_start = time.time()

    class AdaptiveRateLimiter:
        """自适应令牌桶：正常加速，429降速，自动恢复"""
        def __init__(self, base_rate: float, name: str):
            self.base_rate = base_rate
            self.current_rate = base_rate
            self.name = name
            self.tokens = base_rate
            self.last_time = time.monotonic()
            self.lock = asyncio.Lock()
            self.backoff_until = 0.0

        async def acquire(self):
            while True:
                async with self.lock:
                    now = time.monotonic()
                    # 补充令牌
                    elapsed = now - self.last_time
                    self.tokens = min(self.current_rate, self.tokens + elapsed * self.current_rate)
                    self.last_time = now

                    if self.tokens >= 1:
                        self.tokens -= 1
                        # 抖动：±30%随机间隔，模拟真人节奏
                        jitter = random.uniform(0.7, 1.3)
                        await asyncio.sleep(0.01 * jitter)
                        return
                # 没有令牌，等一小段再试（锁外）
                await asyncio.sleep(0.03)

        def on_429(self):
            """被限流：速率减半，最低2 req/s"""
            self.current_rate = max(2.0, self.current_rate * 0.5)
            self.backoff_until = time.monotonic() + 5
            print(f"    ⚠ {self.name} 被限流，降速到 {self.current_rate:.0f} req/s")

        def on_success(self):
            """成功：逐步恢复到基础速率"""
            if time.monotonic() > self.backoff_until:
                self.current_rate = min(self.base_rate, self.current_rate * 1.05)

    sina_limiter = AdaptiveRateLimiter(base_rate=18, name="新浪")
    tencent_limiter = AdaptiveRateLimiter(base_rate=30, name="腾讯")

    # 双连接池
    sina_conn = aiohttp.TCPConnector(limit=6, limit_per_host=6, ttl_dns_cache=300)
    tencent_conn = aiohttp.TCPConnector(limit=10, limit_per_host=10, ttl_dns_cache=300)

    async with aiohttp.ClientSession(connector=sina_conn, timeout=timeout) as sina_sess, \
               aiohttp.ClientSession(connector=tencent_conn, timeout=timeout) as tencent_sess:

        async def _fetch_one(code):
            nonlocal done_count, fail_count

            await sina_limiter.acquire()
            try:
                klines = await _async_fetch_kline_sina(sina_sess, code, min(days, 1000), semaphore)
            except Exception:
                klines = []
                sina_limiter.on_429()

            if not klines:
                await tencent_limiter.acquire()
                try:
                    klines = await _async_fetch_kline_tencent(tencent_sess, code, min(days, 300), semaphore)
                except Exception:
                    klines = []

            done_count += 1
            if klines:
                results[code] = klines
                sina_limiter.on_success()
            else:
                fail_count += 1

            if done_count % 500 == 0:
                elapsed = time.time() - t_start
                speed = done_count / elapsed if elapsed > 0 else 0
                eta = (len(codes) - done_count) / speed if speed > 0 else 0
                print(f"    进度: {done_count}/{len(codes)} ({len(results)}有数据) | {speed:.0f}只/秒 | 剩余{eta:.0f}s | 新浪{sina_limiter.current_rate:.0f}/s 腾讯{tencent_limiter.current_rate:.0f}/s")
            return code, klines

        tasks = [_fetch_one(c) for c in codes]
        for coro in asyncio.as_completed(tasks):
            await coro

    elapsed = time.time() - t_start
    print(f"    📊 异步K线完成: {len(results)}/{len(codes)} 有数据, 失败{fail_count}, 耗时{elapsed:.1f}s ({len(codes)/elapsed:.0f}只/秒)")
    return results


async def _async_fetch_kline_120min_batch(codes: list[str]) -> dict:
    """
    异步批量120分钟K线抓取（与日K线并行，独立session+独立限速器）
    新浪60分钟K线 → 每2根合并为1根120分钟K线
    """
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    results = {}
    done_count = 0
    fail_count = 0
    t_start = time.time()
    semaphore = asyncio.Semaphore(50)

    class AdaptiveRateLimiter:
        def __init__(self, base_rate, name):
            self.base_rate = base_rate
            self.current_rate = base_rate
            self.name = name
            self.tokens = base_rate
            self.last_time = time.monotonic()
            self.lock = asyncio.Lock()
            self.backoff_until = 0.0

        async def acquire(self):
            while True:
                async with self.lock:
                    now = time.monotonic()
                    elapsed = now - self.last_time
                    self.tokens = min(self.current_rate, self.tokens + elapsed * self.current_rate)
                    self.last_time = now
                    if self.tokens >= 1:
                        self.tokens -= 1
                        jitter = random.uniform(0.7, 1.3)
                        await asyncio.sleep(0.01 * jitter)
                        return
                await asyncio.sleep(0.03)

        def on_429(self):
            self.current_rate = max(2.0, self.current_rate * 0.5)
            self.backoff_until = time.monotonic() + 5
            print(f"    ⚠ {self.name} 120min被限流，降速到 {self.current_rate:.0f} req/s")

        def on_success(self):
            if time.monotonic() > self.backoff_until:
                self.current_rate = min(self.base_rate, self.current_rate * 1.05)

    limiter = AdaptiveRateLimiter(base_rate=20, name="新浪120min")
    conn = aiohttp.TCPConnector(limit=10, limit_per_host=10, ttl_dns_cache=300)

    async with aiohttp.ClientSession(connector=conn, timeout=timeout) as sess:
        async def _fetch_one(code):
            nonlocal done_count, fail_count
            sym = f"sh{code}" if code.startswith("6") else f"sz{code}"
            url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
            params = {"symbol": sym, "scale": "60", "ma": "no", "datalen": 120}
            headers = {"Referer": "https://finance.sina.com.cn/", "User-Agent": _random_ua(), "Accept": _random_accept()}

            await limiter.acquire()
            async with semaphore:
                try:
                    async with sess.get(url, params=params, headers=headers) as resp:
                        if resp.status == 429:
                            limiter.on_429()
                            raise ConnectionAbortedError("429")
                        if resp.status != 200:
                            raise Exception(f"HTTP {resp.status}")
                        text = await resp.text()
                        if not text.strip() or text.strip() == "null":
                            raise Exception("empty")
                        m60 = json.loads(text)
                        if not m60 or len(m60) < 2:
                            raise Exception("too short")
                        m120 = []
                        for i in range(0, len(m60) - 1, 2):
                            a, b = m60[i], m60[i + 1]
                            m120.append({
                                "day": b["day"],
                                "open": a["open"],
                                "close": b["close"],
                                "high": str(max(float(a["high"]), float(b["high"]))),
                                "low": str(min(float(a["low"]), float(b["low"]))),
                                "volume": str(int(float(a.get("volume", 0)) + float(b.get("volume", 0)))),
                            })
                        results[code] = m120
                        limiter.on_success()
                except ConnectionAbortedError:
                    fail_count += 1
                except Exception:
                    fail_count += 1

            done_count += 1
            if done_count % 500 == 0:
                elapsed = time.time() - t_start
                speed = done_count / elapsed if elapsed > 0 else 0
                eta = (len(codes) - done_count) / speed if speed > 0 else 0
                print(f"    120min进度: {done_count}/{len(codes)} ({len(results)}有数据) | {speed:.0f}只/秒 | 剩余{eta:.0f}s")

        tasks = [_fetch_one(c) for c in codes]
        for coro in asyncio.as_completed(tasks):
            await coro

    elapsed = time.time() - t_start
    print(f"    📊 120min异步完成: {len(results)}/{len(codes)} 有数据, 失败{fail_count}, 耗时{elapsed:.1f}s ({len(codes)/elapsed:.0f}只/秒)")
    return results


async def _async_fetch_all_klines(codes: list[str], days: int = 1000,
                                   concurrency: int = 100) -> tuple[dict, dict]:
    """
    并行抓取日K线 + 120分钟K线（共享事件循环，独立限速器）
    返回: (daily_klines, kline_120min_map)
    """
    daily_task = _async_fetch_kline_batch(codes, days, concurrency)
    min120_task = _async_fetch_kline_120min_batch(codes)
    daily_klines, kline_120 = await asyncio.gather(daily_task, min120_task)
    return daily_klines, kline_120


def fetch_kline_batch_async(codes: list[str], days: int = 1000,
                             concurrency: int = 100) -> dict:
    """同步包装：调用异步批量K线抓取（无aiohttp时降级为多线程）"""
    if _HAS_AIOHTTP:
        print(f"    📦 异步批量获取K线: {len(codes)} 只, {days}天, {concurrency}并发, 双域名分流...")
        t0 = time.time()
        results = asyncio.run(_async_fetch_kline_batch(codes, days, concurrency))
        elapsed = time.time() - t0
        speed = len(codes) / elapsed if elapsed > 0 else 0
        print(f"    ⏱ 总耗时 {elapsed:.1f}s ({speed:.0f}只/秒)")
        return results
    else:
        print(f"    ⚠️ 未安装 aiohttp，使用多线程降级模式 (pip install aiohttp 可提速3-5倍)")
        return fetch_kline_batch(codes, days=days, max_workers=16)


def fetch_all_klines_async(codes: list[str], days: int = 1000,
                            concurrency: int = 100) -> tuple[dict, dict]:
    """
    同步包装：并行抓取日K线 + 120分钟K线
    返回: (daily_klines, kline_120min_map)
    """
    if _HAS_AIOHTTP:
        print(f"    📦 并行获取日K线 + 120minK线: {len(codes)} 只, {days}天...")
        t0 = time.time()
        daily, k120 = asyncio.run(_async_fetch_all_klines(codes, days, concurrency))
        elapsed = time.time() - t0
        speed = len(codes) / elapsed if elapsed > 0 else 0
        print(f"    ⏱ 并行K线总耗时 {elapsed:.1f}s ({speed:.0f}只/秒)")
        return daily, k120
    else:
        daily = fetch_kline_batch_async(codes, days, concurrency)
        return daily, {}


def fetch_kline_120min(code: str, count: int = 60, session: requests.Session = None,
                       limiter: RateLimiter = None) -> list[dict]:
    """
    获取120分钟K线数据（新浪60分钟K线每2根合并为1根）
    用于分析脚本的 pool_technical (120分钟MACD检查)
    返回: [{"day", "open", "close", "high", "low", "volume"}, ...]
    """
    if session is None:
        session = _build_session()

    sym = f"sh{code}" if code.startswith("6") else f"sz{code}"
    r = safe_request(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        limiter or _limiter_sina, session,
        params={"symbol": sym, "scale": "60", "ma": "no", "datalen": count * 2},
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    if not r:
        # safe_request 已打印错误信息
        return []
    if not r.text.strip() or r.text.strip() == "null":
        return []

    try:
        m60 = json.loads(r.text)
        if not m60 or len(m60) < 2:
            return []
        m120 = []
        for i in range(0, len(m60) - 1, 2):
            a, b = m60[i], m60[i + 1]
            m120.append({
                "day": b["day"],
                "open": a["open"],
                "close": b["close"],
                "high": str(max(float(a["high"]), float(b["high"]))),
                "low": str(min(float(a["low"]), float(b["low"]))),
                "volume": str(int(float(a.get("volume", 0)) + float(b.get("volume", 0)))),
            })
        return m120
    except Exception as e:
        # 打印解析错误以便排查，不再静默吞掉
        print(f"    ⚠️ 120min解析失败 {code}: {e} (text={r.text[:200]})")
        return []


def supplement_kline_amount(kline_map: dict, quotes: dict) -> dict:
    """
    补充K线数据中缺失的amount(成交额)字段。
    新浪K线不含amount，用 volume * 收盘价 近似估算。
    quotes: {code: {price, prev_close, amount, ...}}

    返回补充后的 kline_map（原地修改）
    """
    for code, klines in kline_map.items():
        q = quotes.get(code, {})
        for k in klines:
            if "amount" not in k or float(k.get("amount", 0)) <= 0:
                vol = float(k.get("volume", 0))
                close = float(k.get("close", 0))
                if vol > 0 and close > 0:
                    # volume单位是"手"(100股)，close单位是"元"
                    # amount单位与分析脚本一致（元）
                    k["amount"] = str(vol * 100 * close)
                else:
                    k["amount"] = "0"
    return kline_map


# ════════════════════════════════════════════════════
# 4. 全A股代码列表（新浪）
# ════════════════════════════════════════════════════

def fetch_all_stock_codes(session: requests.Session = None) -> list[str]:
    """获取全部A股代码列表（分页请求，每页100条）"""
    if session is None:
        session = _build_session()

    codes = []
    page = 1
    while True:
        r = safe_request(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
            _limiter_sina, session,
            params={
                "page": str(page), "num": "1000", "sort": "symbol",
                "asc": "1", "node": "hs_a", "symbol": "", "_s_r_a": "page",
            },
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        if not r:
            break
        try:
            data = json.loads(r.text)
            if not data:
                break
            for item in data:
                symbol = str(item.get("symbol", ""))
                code = str(item.get("code", ""))
                # 只要沪深主板（sh/sz开头），跳过北交所(bj)、创业板(300)、科创板(688)等
                if not symbol.startswith(("sh", "sz")):
                    continue
                if code and len(code) == 6 and code.isdigit():
                    codes.append(code)
            # API实际每页返回100条，不足100说明已到最后一页
            if len(data) < 100:
                break
            page += 1
        except json.JSONDecodeError:
            break

    return codes


# ════════════════════════════════════════════════════
# 5. 行业板块 + 成分股（东方财富）
# ════════════════════════════════════════════════════

def fetch_sectors(session: requests.Session = None) -> dict:
    """
    获取行业板块列表及各板块下的股票（新浪源，无封IP风险）
    返回: {sector_name: {"code": scode, "stocks": [...], "limit_up": N, ...}}
    """
    if session is None:
        session = _build_session()

    sectors = {}

    # 第一步：新浪板块列表
    r = safe_request(
        "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
        _limiter_sina, session, encoding="gbk",
    )
    if not r:
        print("    ❌ 板块列表获取失败")
        return sectors

    try:
        m = re.search(r'=\s*\{(.+)\}', r.text, re.DOTALL)
        if m:
            data = json.loads('{' + m.group(1) + '}')
            for key, val in data.items():
                parts = val.split(',')
                if len(parts) < 9:
                    continue
                sname = parts[1]
                stock_count = int(parts[2]) if parts[2].isdigit() else 0
                change_pct = float(parts[4]) if parts[4] else 0
                sectors[sname] = {
                    "code": key,       # 新浪板块代码，如 new_dlhy
                    "stocks": [],
                    "limit_up": 0,
                    "limit_down": 0,
                    "change_pct": round(change_pct, 2),
                    "stock_count": stock_count,
                }
    except Exception as e:
        print(f"    ❌ 板块列表解析失败: {e}")
        return sectors

    print(f"    ✅ 获取到 {len(sectors)} 个行业板块")

    # 第二步：并发获取每个板块的成分股（新浪接口）
    def _fetch_stocks(sname, scode):
        try:
            # 先获取数量
            r_count = safe_request(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount",
                _limiter_sina, session,
                params={"node": scode},
                encoding="gbk",
            )
            total = int(r_count.text.strip().strip('"')) if r_count else 0
            if total <= 0:
                return sname, []

            # 拉取全部成分股
            r_stocks = safe_request(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                _limiter_sina, session,
                params={
                    "num": str(total),
                    "sort": "symbol",
                    "asc": "1",
                    "node": scode,
                    "_s_r_a": "auto",
                },
                encoding="gbk",
            )
            if not r_stocks:
                return sname, []

            items = json.loads(r_stocks.text)
            stocks = []
            for it in items:
                sym = it.get("symbol", "")
                code = sym[2:] if len(sym) >= 8 else ""
                if code and len(code) == 6 and code[0].isdigit():
                    stocks.append({
                        "code": code,
                        "name": it.get("name", ""),
                        "symbol": sym,
                        "price": float(it.get("trade", 0) or 0),
                        "change_pct": float(it.get("changepercent", 0) or 0),
                    })
            return sname, stocks
        except Exception:
            return sname, []

    print(f"    📦 获取各板块成分股...")
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_stocks, sn, sectors[sn]["code"]): sn for sn in sectors}
        for future in as_completed(futures):
            sname, stocks = future.result()
            sectors[sname]["stocks"] = stocks

    total = sum(len(s["stocks"]) for s in sectors.values())
    print(f"    ✅ 成分股获取完成: 共 {total} 只")

    # 统计各板块涨停数（涨幅>=9.5%视为主板涨停）
    for sname, sdata in sectors.items():
        limit_cnt = 0
        for stk in sdata.get("stocks", []):
            chg = stk.get("change_pct", 0) or 0
            if chg >= 9.5:
                limit_cnt += 1
        sdata["limit_up"] = limit_cnt

    return sectors


# ════════════════════════════════════════════════════
# 6. 股票详情（东方财富批量接口）
# ════════════════════════════════════════════════════

def fetch_stock_details(codes: list[str], session: requests.Session = None,
                        quotes_ref: dict = None) -> dict:
    """
    批量获取股票详情（市值/量比/换手率/成交额）
    直接使用腾讯行情数据（稳定可靠，无封IP风险）
    """
    results = {}
    if not quotes_ref:
        return results

    for code in codes:
        if code in quotes_ref:
            q = quotes_ref[code]
            results[code] = {
                "market_cap_yi": round(float(q.get("market_cap_yi", 0) or 0), 2),
                "volume_ratio": round(float(q.get("volume_ratio_api", 0) or 0), 2),
                "turnover": round(float(q.get("turnover", 0) or 0), 4),
                "amount": round(float(q.get("amount", 0) or 0), 2),
                "volume": int(q.get("volume", 0) or 0),
            }

    print(f"    ✅ 腾讯行情数据: {len(results)} 只")
    return results


# ════════════════════════════════════════════════════
# 7. 大盘指数
# ════════════════════════════════════════════════════

def fetch_indices(session: requests.Session = None) -> list[dict]:
    """获取三大指数实时行情"""
    if session is None:
        session = _build_session()

    symbols = "sh000001,sz399001,sz399006"
    name_map = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}

    r = safe_request(
        f"https://qt.gtimg.cn/q={symbols}",
        _limiter_tencent, session,
    )
    if not r:
        return []

    # 直接用 GBK 解码原始字节
    text = r.content.decode("gbk", errors="replace")

    results = []
    for line in text.strip().split("\n"):
        m = re.search(r'v_(\w+)="(.+)"', line)
        if not m:
            continue
        symbol = m.group(1)
        fields = m.group(2).split("~")
        if len(fields) < 44:
            continue
        results.append({
            "name": name_map.get(symbol, fields[1]),
            "code": symbol,
            "price": float(fields[3]) if fields[3] else 0,
            "prev_close": float(fields[4]) if fields[4] else 0,
            "change_pct": float(fields[32]) if fields[32] else 0,
            "amount": float(fields[37]) if fields[37] else 0,
            "volume": int(fields[36]) if fields[36] else 0,
        })
    return results


# ════════════════════════════════════════════════════
# 8. 板块K线
# ════════════════════════════════════════════════════

def fetch_sector_kline(sector_code: str, days: int = 10, session: requests.Session = None) -> list[dict]:
    """获取板块日K线"""
    if session is None:
        session = _build_session()

    r = safe_request(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        _limiter_eastmoney, session,
        params={
            "secid": f"90.{sector_code}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101", "fqt": "0",
            "lmt": str(days), "end": "20500101",
            "ut": "fa5fd1943c7b386f172d6893dbbd4dc0",
        },
        headers={"Referer": "https://quote.eastmoney.com/center/boardlist.html"},
    )
    if not r:
        return []

    try:
        data = r.json().get("data", {})
        klines = []
        for line in data.get("klines", []):
            parts = line.split(",")
            if len(parts) >= 6:
                klines.append({
                    "day": parts[0], "open": parts[1], "close": parts[2],
                    "high": parts[3], "low": parts[4], "volume": parts[5],
                })
        return klines
    except Exception:
        return []


# ════════════════════════════════════════════════════
# 8a. 主线板块排名（基于股票数据聚合，无额外API依赖）
# ════════════════════════════════════════════════════

def rank_main_line_sectors(sectors: dict, quotes: dict = None,
                           details: dict = None, kline_map: dict = None,
                           session: requests.Session = None,
                           top_n: int = 10) -> list[dict]:
    """
    综合排名主线板块（基于已有股票数据聚合，不依赖外部API）
    维度：板块成交金额 + 板块5日平均涨幅 + 板块10日平均涨幅
    返回排名前N的板块列表
    """
    if quotes is None:
        quotes = {}
    if details is None:
        details = {}
    if kline_map is None:
        kline_map = {}

    # 构建 code -> sector 映射
    code_to_sector = {}
    for sn, sd in sectors.items():
        for stk in sd.get("stocks", []):
            code = stk.get("code", "") or stk.get("symbol", "")
            if code:
                code_to_sector[code] = sn

    # 按板块聚合数据
    sector_data = {}  # {sector_name: {amounts, changes_5d, changes_10d, up, down, today_changes}}
    for sn in sectors:
        sector_data[sn] = {
            "amounts": [],
            "changes_5d": [],
            "changes_10d": [],
            "today_changes": [],
            "up": 0,
            "down": 0,
        }

    for code, q in quotes.items():
        sn = code_to_sector.get(code)
        if not sn or sn not in sector_data:
            continue

        # 当日涨幅
        chg = float(q.get("change_pct", 0) or 0)
        sector_data[sn]["today_changes"].append(chg)
        if chg > 0:
            sector_data[sn]["up"] += 1
        elif chg < 0:
            sector_data[sn]["down"] += 1

        # 成交额(亿)
        amount = float(q.get("amount", 0) or 0)
        if amount > 0:
            sector_data[sn]["amounts"].append(amount)

        # K线计算5日/10日涨幅
        klines = kline_map.get(code, [])
        if klines and len(klines) >= 2:
            close_now = float(klines[-1].get("close", 0))
            if close_now <= 0:
                continue
            if len(klines) >= 6:
                close_5d = float(klines[-6].get("close", 0))
                if close_5d > 0:
                    sector_data[sn]["changes_5d"].append(
                        (close_now - close_5d) / close_5d * 100)
            if len(klines) >= 11:
                close_10d = float(klines[-11].get("close", 0))
                if close_10d > 0:
                    sector_data[sn]["changes_10d"].append(
                        (close_now - close_10d) / close_10d * 100)

    # 计算板块汇总
    import numpy as _np
    sector_scores = []
    for sn, sd in sectors.items():
        dd = sector_data.get(sn, {})
        amounts = dd.get("amounts", [])
        changes_5d = dd.get("changes_5d", [])
        changes_10d = dd.get("changes_10d", [])

        total_amount = sum(amounts) / 1e8 if amounts else 0  # 亿
        avg_5d = round(float(_np.mean(changes_5d)), 2) if changes_5d else 0
        avg_10d = round(float(_np.mean(changes_10d)), 2) if changes_10d else 0
        avg_today = round(float(_np.mean(dd.get("today_changes", [0]))), 2) if dd.get("today_changes") else 0

        # 只收录有数据的板块
        if total_amount <= 0 and not changes_5d:
            continue

        sector_scores.append({
            "name": sn,
            "code": sd.get("code", ""),
            "total_amount": round(total_amount, 2),
            "avg_5d": avg_5d,
            "avg_10d": avg_10d,
            "avg_today": avg_today,
            "up_count": dd.get("up", 0),
            "down_count": dd.get("down", 0),
            "stock_count": len(amounts),
        })

    if not sector_scores:
        return []

    # 各维度百分位排名打分
    def _percentile_rank(items, key):
        sorted_items = sorted(items, key=lambda x: -x[key])
        rank_map = {}
        for i, s in enumerate(sorted_items):
            rank_map[s["name"]] = round((1 - i / len(sorted_items)) * 100, 1)
        return rank_map

    money_rank = _percentile_rank(sector_scores, "total_amount")
    rank_5d = _percentile_rank(sector_scores, "avg_5d")
    rank_10d = _percentile_rank(sector_scores, "avg_10d")

    # 综合评分（成交金额40% + 五日涨幅30% + 十日涨幅30%）
    for s in sector_scores:
        sn = s["name"]
        s["score"] = round(
            money_rank.get(sn, 0) * 0.4 +
            rank_5d.get(sn, 0) * 0.3 +
            rank_10d.get(sn, 0) * 0.3, 1)

    sector_scores.sort(key=lambda x: -x["score"])
    for i, s in enumerate(sector_scores[:top_n], 1):
        s["rank"] = i

    return sector_scores[:top_n]


# ════════════════════════════════════════════════════
# 接口连通性测试
# ════════════════════════════════════════════════════

def test_connectivity():
    """测试所有数据接口的连通性"""
    session = _build_session()
    print("=" * 60)
    print("  🔌 接口连通性测试")
    print("=" * 60)

    tests = [
        ("腾讯行情", lambda: fetch_quotes_batch(["600519"], session)),
        ("腾讯搜索", lambda: search_stock("茅台", session)),
        ("新浪K线", lambda: fetch_kline_sina("600519", 5, session)),
        ("东财K线", lambda: fetch_kline_tencent("600519", 5, session)),
        ("大盘指数", lambda: fetch_indices(session)),
        ("东财股票详情", lambda: fetch_stock_details(["600519"], session, quotes_ref={})),
        ("新浪板块列表", lambda: fetch_sectors(session)),
    ]

    for name, fn in tests:
        try:
            t0 = time.time()
            result = fn()
            elapsed = time.time() - t0
            if result:
                if isinstance(result, dict):
                    ok = len(result) > 0
                elif isinstance(result, list):
                    ok = len(result) > 0
                else:
                    ok = result is not None
            else:
                ok = False
            status = "✅ 通" if ok else "⚠ 空"
            print(f"  {status}  {name:<16} {elapsed:.2f}s")
        except Exception as e:
            print(f"  ❌  {name:<16} 异常: {e}")

    print("=" * 60)


# ════════════════════════════════════════════════════
# 完整数据抓取流程
# ════════════════════════════════════════════════════

def scrape_all(output_dir: str = "stock_data", target_codes: list[str] = None):
    """
    完整抓取流程（纯HTTP，无Selenium依赖）：
    1. 获取全A代码列表（或使用指定代码）
    2. 批量获取实时行情（腾讯）
    3. 批量获取股票详情（腾讯兜底）
    4. 获取行业板块 + 成分股（新浪）
    5. 获取K线历史（新浪+腾讯）
    6. 全部保存为JSON
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session = _build_session()

    # 清理旧的时间戳文件，防止 glob 加载到旧数据
    for pattern in ("quotes_*.json", "details_*.json", "sectors_*.json",
                    "indices_*.json", "all_codes_*.json"):
        for old in _glob.glob(os.path.join(output_dir, pattern)):
            try:
                os.remove(old)
            except OSError:
                pass

    print(f"\n{'═' * 60}")
    print(f"  🕷️  NYLO — A股数据抓取 · 开始抓取（纯HTTP模式）")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  输出目录: {output_dir}/")
    print(f"{'═' * 60}\n")

    try:
        # ── 0. 大盘指数 ──
        print("📊 [1/6] 抓取大盘指数...")
        indices = fetch_indices(session)
        for idx in indices:
            sign = "+" if idx["change_pct"] > 0 else ""
            print(f"    {idx['name']}: {idx['price']:.2f} ({sign}{idx['change_pct']:.2f}%)")
        _save_json(indices, f"{output_dir}/indices_{timestamp}.json")

        # ── 1. 全A代码列表 ──
        if target_codes:
            all_codes = target_codes
            print(f"\n📋 [2/6] 使用指定代码: {len(all_codes)} 只")
        else:
            print(f"\n📋 [2/6] 抓取全A股代码列表（新浪）...")
            all_codes = fetch_all_stock_codes(session)
            print(f"    ✅ 共 {len(all_codes)} 只主板A股")
        _save_json(all_codes, f"{output_dir}/all_codes_{timestamp}.json")

        # ── 2. 批量实时行情（腾讯）──
        print(f"\n📊 [3/6] 抓取实时行情 ({len(all_codes)} 只)...")
        quotes = fetch_quotes_batch(all_codes, session)
        print(f"    ✅ 获取到 {len(quotes)} 只行情数据")
        _save_json(quotes, f"{output_dir}/quotes_{timestamp}.json")

        # ── 3. 股票详情（东财批量接口）──
        print(f"\n📊 [4/6] 抓取股票详情（市值/量比/换手率）...")
        details = fetch_stock_details(all_codes, session, quotes_ref=quotes)
        print(f"    ✅ 获取到 {len(details)} 只详情数据")
        _save_json(details, f"{output_dir}/details_{timestamp}.json")

        # 东财冷却：股票详情打完后等几秒再请求板块
        time.sleep(3)

        # ── 4. 行业板块 + 成分股（东财）──
        print(f"\n📊 [5/6] 抓取行业板块 + 成分股...")
        sectors = fetch_sectors(session)
        sectors_save = {}
        for sn, sd in sectors.items():
            sectors_save[sn] = {
                "code": sd.get("code", ""),
                "limit_up": sd.get("limit_up", 0),
                "limit_down": sd.get("limit_down", 0),
                "change_pct": sd.get("change_pct", 0),
                "stocks": sd.get("stocks", []),
            }
        _save_json(sectors_save, f"{output_dir}/sectors_{timestamp}.json")

        # ── 5. K线历史（新浪+腾讯双源，并行抓取120min）──
        kline_codes = [c for c in all_codes if c.startswith(("60", "00")) and c in quotes]
        if target_codes:
            kline_codes = target_codes

        print(f"\n📊 [6/6] 并行抓取日K线+120minK线 ({len(kline_codes)} 只主板, 1000天)...")
        klines, klines_120 = fetch_all_klines_async(kline_codes, days=1000, concurrency=100)
        klines = supplement_kline_amount(klines, quotes)

        kline_dir = f"{output_dir}/klines"
        os.makedirs(kline_dir, exist_ok=True)
        for code, data in klines.items():
            _save_json(data, f"{kline_dir}/{code}.json")
        print(f"    ✅ 日K线已保存到 {kline_dir}/ ({len(klines)} 个文件)")

        kline120_dir = f"{output_dir}/klines_120min"
        os.makedirs(kline120_dir, exist_ok=True)
        for code, data in klines_120.items():
            _save_json(data, f"{kline120_dir}/{code}.json")
        print(f"    ✅ 120minK线已保存到 {kline120_dir}/ ({len(klines_120)} 个文件)")

        # ── 汇总 ──
        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_codes": len(all_codes),
            "quotes_count": len(quotes),
            "details_count": len(details),
            "sectors_count": len(sectors),
            "klines_count": len(klines),
            "klines_120min_count": len(klines_120),
            "files": {
                "indices": f"indices_{timestamp}.json",
                "codes": f"all_codes_{timestamp}.json",
                "quotes": f"quotes_{timestamp}.json",
                "details": f"details_{timestamp}.json",
                "sectors": f"sectors_{timestamp}.json",
                "klines_dir": "klines/",
                "klines_120min_dir": "klines_120min/",
            },
        }
        _save_json(summary, f"{output_dir}/summary.json")

        print(f"\n{'═' * 60}")
        print(f"  ✅ 全部抓取完成!")
        print(f"  📂 数据目录: {os.path.abspath(output_dir)}/")
        print(f"  📊 汇总: {len(quotes)} 只行情 | {len(sectors)} 个板块 | {len(klines)} 只K线 | {len(klines_120)} 只120分钟K线")
        print(f"{'═' * 60}\n")

    except KeyboardInterrupt:
        print(f"\n⚠ 用户中断，已保存已获取的数据")

    return summary


def _save_json(data, path):
    """保存JSON文件（统一UTF-8编码）"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    size = os.path.getsize(path)
    if size > 1024 * 1024:
        print(f"    💾 {path} ({size / 1024 / 1024:.1f}MB)")
    else:
        print(f"    💾 {path} ({size / 1024:.0f}KB)")


def _load_json(path):
    """加载JSON文件，自动检测编码（兼容 UTF-8 / UTF-8-BOM / GBK）"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return json.load(f)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"无法解码文件: {path}")


# ════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════


# ════════════════════════════════════════════════════
# 分析筹码_主板筛选V3 — 集合竞价分析模块
# ════════════════════════════════════════════════════

# 全局缓存
_macd_120min_cache = {}
_sector_kline_cache = {}

@dataclass
class AuctionResult:
    code: str
    name: str
    prev_close: float
    open_price: float
    price: float
    volume: int
    amount: float
    change_pct: float
    volume_ratio: float
    open_gap: float
    amplitude: float
    turnover: float
    buy_ratio: float
    buy_vol: int
    sell_vol: int
    bull_score: int
    bear_score: int
    verdict: str
    signals: list = field(default_factory=list)
    macd_dif: float = 0
    macd_dea: float = 0
    macd_bar: float = 0
    macd_trend: str = "-"
    # 尾段竞价判定
    tail_score: int = 0
    tail_verdict: str = "-"
    tail_signals: list = field(default_factory=list)


def tail_segment_verdict(gap: float, vol: int, buy_vol: int, sell_vol: int,
                         is_limit_up: bool = False,
                         mid_buy_all: bool = False,
                         mid_sell_vol: float = 0, tail_sell_vol: float = 0,
                         vol_min: int = 5000) -> tuple[int, str, list]:
    """
    尾段竞价判定规则（新规则三维评分）
    ─────────────────────────────────────
    第一层：新规则三维评分
      硬否决规则（任一触发→判定"不看多"）
      正面计分规则（总分≥5分才判定"看多"）

    参数:
      gap: 开盘跳空百分比 (op - prev_close) / prev_close * 100
      vol: 成交量（手）
      buy_vol: 尾段买量（手）
      sell_vol: 尾段卖量（手）
      is_limit_up: 是否涨停
      mid_buy_all: 中段是否全买单
      mid_sell_vol: 中段卖量（手）
      tail_sell_vol: 尾段卖量（手）（用于计算中→尾卖盘变化）
      vol_min: 成交量最低阈值（手），开盘竞价默认5000，收盘竞价用500

    返回:
      (score, verdict, signals)
      score: 综合得分（≥5=看多, <5=不看多, 硬否决时为负数）
      verdict: "看多" / "不看多"
      signals: 信号列表
    """
    signals = []

    # ═══════════════════════════════════════════
    # 硬否决规则（任一触发→直接判定"不看多"）
    # ═══════════════════════════════════════════

    # 1. 成交量维度：vol < vol_min → "量太小"
    if vol < vol_min:
        signals.append("❌ 量太小 (<5000手)")
        return -1, "不看多", signals

    # 2. 价格差值维度：gap < -1.0% → "价格向下"
    if gap < -1.0:
        signals.append("❌ 价格向下 (gap<-1.0%)")
        return -2, "不看多", signals

    # 3. jzb指标维度：jzb > 35% → "异常高（华塑陷阱）"
    #    jzb = 尾段买量占比 = buy_vol / (buy_vol + sell_vol) * 100
    total_tail = buy_vol + sell_vol
    jzb = (buy_vol / total_tail * 100) if total_tail > 0 else None
    if jzb is not None and jzb > 35:
        signals.append(f"❌ 异常高/华塑陷阱 (jzb={jzb:.1f}%>35%)")
        return -3, "不看多", signals

    # 4. 组合条件：尾段全买单 + gap<0.5% + 非涨停 → "虚假信号"
    if buy_vol > 0 and sell_vol == 0 and gap < 0.5 and not is_limit_up:
        signals.append("❌ 虚假信号 (全买单+gap<0.5%+非涨停)")
        return -4, "不看多", signals

    # 5. 价格+量组合：gap>1% + vol<5万 → "薄里拉价（太阳能陷阱）"
    if gap > 1 and vol < 50000:
        signals.append("❌ 薄里拉价/太阳能陷阱 (gap>1%+vol<5万)")
        return -5, "不看多", signals

    # 6. jzb指标维度：jzb < 2% → "无人气"
    if jzb is not None and jzb < 2:
        signals.append(f"❌ 无人气 (jzb={jzb:.1f}%<2%)")
        return -6, "不看多", signals

    # ═══════════════════════════════════════════
    # 正面计分规则（总分≥5分才判定"看多"）
    # ═══════════════════════════════════════════
    score = 0

    # 1. gap指标计分
    if gap > 1.5:
        score += 3
        signals.append(f"✅ gap>{gap:.1f}%>1.5% → +3")
    elif gap > 0.8:
        score += 2
        signals.append(f"✅ gap={gap:.1f}%∈(0.8,1.5] → +2")
    elif gap > 0.3:
        score += 1
        signals.append(f"✅ gap={gap:.1f}%∈(0.3,0.8] → +1")

    # 2. 尾段买卖量计分
    if buy_vol > sell_vol and buy_vol > 50:
        score += 2
        signals.append(f"✅ 尾段买>卖且>50手 → +2")
    elif buy_vol > sell_vol and buy_vol > 10:
        score += 1
        signals.append(f"✅ 尾段买>卖且>10手 → +1")

    # 3. 中→尾卖盘变化计分
    if mid_sell_vol > 0 and tail_sell_vol >= 0:
        sell_decrease = (mid_sell_vol - tail_sell_vol) / mid_sell_vol * 100
        if sell_decrease > 80:
            score += 2
            signals.append(f"✅ 卖盘减少{sell_decrease:.0f}%>80% → +2")
        elif sell_decrease > 50:
            score += 1
            signals.append(f"✅ 卖盘减少{sell_decrease:.0f}%>50% → +1")

    # 4. vol成交量计分
    if vol > 100000:
        score += 2
        signals.append(f"✅ vol={vol:,}>10万 → +2")
    elif vol > 50000:
        score += 1
        signals.append(f"✅ vol={vol:,}>5万 → +1")

    # 5. jzb指标计分：3% ≤ jzb ≤ 20%
    if jzb is not None and 3 <= jzb <= 20:
        score += 1
        signals.append(f"✅ jzb={jzb:.1f}%∈[3%,20%] → +1")

    # 6. 扣分项：中段全买单 + 非涨停
    if mid_buy_all and not is_limit_up:
        score -= 2
        signals.append(f"⚠️ 中段全买单+非涨停 → -2")

    verdict = "看多" if score >= 5 else "不看多"
    return score, verdict, signals


# ════════════════════════════════════════════════════
# 收盘集合竞价监控（14:57-15:00）
# ════════════════════════════════════════════════════

@dataclass
class CloseAuctionSnapshot:
    """收盘竞价快照"""
    time: str
    price: float
    bid1_v: int
    ask1_v: int
    volume: int
    amount: float


@dataclass
class CloseAuctionResult:
    """收盘竞价分析结果"""
    code: str
    name: str
    prev_close: float
    close_price: float
    # 尾段增量（最后两次快照差值）
    tail_bid_delta: int     # 尾段买一量变化
    tail_ask_delta: int     # 尾段卖一量变化
    tail_vol_delta: int     # 尾段搓合量变化
    # 全程增量（14:57→15:00）
    total_bid_delta: int    # 全程买一量变化
    total_ask_delta: int    # 全程卖一量变化
    total_vol_delta: int    # 全程搓合量变化
    # 判定
    gap: float              # 收盘相对昨收涨跌幅
    tail_score: int
    tail_verdict: str
    tail_signals: list
    snapshots: list         # 所有快照


def close_auction_monitor(codes: list[str], poll_interval: int = 10,
                          tail_window: int = 2, html_path: str = None) -> list[CloseAuctionResult]:
    """
    收盘集合竞价监控（14:57-15:00）

    在收盘竞价期间定时轮询腾讯接口，采集快照，
    竞价结束后用最后 tail_window 次快照的差值作为"尾段数据"，
    再调用 tail_segment_verdict 做判定。

    参数:
      codes: 股票代码列表
      poll_interval: 采样间隔（秒），默认10秒
      tail_window: 尾段窗口大小（取最后N次快照的差值），默认2

    返回:
      CloseAuctionResult 列表
    """
    from datetime import datetime as _dt
    import time as _time

    print(f"\n{'═'*56}")
    print(f"  📊 收盘集合竞价监控（14:57-15:00）")
    print(f"  监控 {len(codes)} 只股票，每 {poll_interval} 秒采样")
    print(f"  尾段窗口: 最后 {tail_window} 次快照差值")
    print(f"{'═'*56}\n")

    # 检查时间窗口
    now = _dt.now()
    target_start = now.replace(hour=14, minute=57, second=0, microsecond=0)
    target_end = now.replace(hour=15, minute=0, second=0, microsecond=0)

    if now >= target_end:
        print(f"  ❌ 当前 {now.strftime('%H:%M')}，已过 15:00，收盘竞价已结束")
        return []

    if now < target_start:
        wait_sec = (target_start - now).total_seconds()
        print(f"  ⏳ 等待 14:57 开始...（{int(wait_sec)}秒后启动）")
        _time.sleep(wait_sec)

    print(f"  🚀 开始采集 @ {_dt.now().strftime('%H:%M:%S')}")

    # 采集快照
    all_snapshots: dict[str, list[CloseAuctionSnapshot]] = {c: [] for c in codes}
    session = _build_session()

    while True:
        now = _dt.now()
        if now.hour >= 15:
            break  # 15:00 结束

        # 批量获取行情
        quotes = fetch_quotes_batch(codes, session=session)
        ts = now.strftime('%H:%M:%S')

        for code in codes:
            q = quotes.get(code, {})
            if not q:
                continue
            snap = CloseAuctionSnapshot(
                time=ts,
                price=float(q.get("price", 0)),
                bid1_v=int(q.get("bid1_v", 0)),
                ask1_v=int(q.get("ask1_v", 0)),
                volume=int(q.get("volume", 0)),
                amount=float(q.get("amount", 0)),
            )
            all_snapshots[code].append(snap)

        print(f"  ⏱ {ts} | 已采 {len(all_snapshots[codes[0]])} 次快照")
        _time.sleep(poll_interval)

    print(f"\n  ✅ 采集结束 @ {_dt.now().strftime('%H:%M:%S')}")

    # 分析每个股票
    results = []
    for code in codes:
        snaps = all_snapshots.get(code, [])
        if len(snaps) < 2:
            print(f"  ⚠️ {code} 快照不足（{len(snaps)}次），跳过")
            continue

        # 尾段 = 最后 tail_window 次快照的差值
        tail_snaps = snaps[-tail_window:]
        tail_bid_delta = tail_snaps[-1].bid1_v - tail_snaps[0].bid1_v
        tail_ask_delta = tail_snaps[-1].ask1_v - tail_snaps[0].ask1_v
        tail_vol_delta = tail_snaps[-1].volume - tail_snaps[0].volume

        # 全程 = 第一次 → 最后一次
        total_bid_delta = snaps[-1].bid1_v - snaps[0].bid1_v
        total_ask_delta = snaps[-1].ask1_v - snaps[0].ask1_v
        total_vol_delta = snaps[-1].volume - snaps[0].volume

        # 收盘价涨跌幅
        close_price = snaps[-1].price
        # 从腾讯接口拿昨收
        quotes = fetch_quotes_batch([code], session=session)
        q = quotes.get(code, {})
        prev_close = float(q.get("prev_close", 0))
        gap = (close_price - prev_close) / prev_close * 100 if prev_close > 0 else 0

        # 尾段判定：用尾段买一量增量作为 buy_vol，卖一量增量作为 sell_vol
        # 增量为正说明尾段在加仓/加卖，为负说明在撤单
        buy_vol = max(tail_bid_delta, 0)   # 尾段买一增加量
        sell_vol = max(tail_ask_delta, 0)  # 尾段卖一增加量
        is_limit_up = gap >= 9.5

        # vol = 收盘竞价期间搓合量增量（14:57→15:00 的成交量）
        close_auction_vol = max(snaps[-1].volume - snaps[0].volume, 0)

        ts_score, ts_verdict, ts_signals = tail_segment_verdict(
            gap=gap,
            vol=close_auction_vol,
            vol_min=500,  # 收盘竞价3分钟，阈值降低到500手
            buy_vol=buy_vol,
            sell_vol=sell_vol,
            is_limit_up=is_limit_up,
        )

        # 补充额外信号
        if tail_bid_delta < 0:
            ts_signals.append(f"⚠️ 尾段买一撤单 {tail_bid_delta}手")
        if tail_ask_delta < 0:
            ts_signals.append(f"⚠️ 尾段卖一撤单 {tail_ask_delta}手")
        if total_bid_delta > total_ask_delta * 2:
            ts_signals.append(f"✅ 全程买盘主导 (买+{total_bid_delta} vs 卖+{total_ask_delta})")
        elif total_ask_delta > total_bid_delta * 2:
            ts_signals.append(f"🔴 全程卖盘主导 (卖+{total_ask_delta} vs 买+{total_bid_delta})")

        result = CloseAuctionResult(
            code=code,
            name=q.get("name", code),
            prev_close=prev_close,
            close_price=close_price,
            tail_bid_delta=tail_bid_delta,
            tail_ask_delta=tail_ask_delta,
            tail_vol_delta=tail_vol_delta,
            total_bid_delta=total_bid_delta,
            total_ask_delta=total_ask_delta,
            total_vol_delta=total_vol_delta,
            gap=round(gap, 2),
            tail_score=ts_score,
            tail_verdict=ts_verdict,
            tail_signals=ts_signals,
            snapshots=snaps,
        )
        results.append(result)

    # 输出结果
    print(f"\n{'─'*72}")
    print(f"  📊 收盘竞价尾段判定结果")
    print(f"{'─'*72}")
    print(f"  {'代码':<8} {'名称':<8} {'收盘':>7} {'涨幅':>7} {'尾段买':>8} {'尾段卖':>8} {'全程买':>8} {'全程卖':>8} {'判定':<8} {'得分':>4}")
    print(f"  {'─'*8} {'─'*8} {'─'*7} {'─'*7} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*4}")
    for r in results:
        icon = "🟢" if r.tail_verdict == "看多" else "🔴"
        print(f"  {r.code:<8} {r.name:<8} {r.close_price:>7.2f} {r.gap:>+6.2f}% {r.tail_bid_delta:>+8} {r.tail_ask_delta:>+8} {r.total_bid_delta:>+8} {r.total_ask_delta:>+8} {icon}{r.tail_verdict:<6} {r.tail_score:>4}")
        for sig in r.tail_signals:
            print(f"    {sig}")
    print(f"{'─'*72}")

    # 生成 HTML 报告
    if not html_path:
        html_path = "close_auction_report.html"
    save_close_auction_html(results, html_path)
    print(f"\n📄 报告: {html_path}")

    return results


def save_close_auction_html(results: list[CloseAuctionResult], path: str) -> str:
    """保存收盘竞价尾段判定HTML报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows = ""
    for i, r in enumerate(results, 1):
        chg_color = "#f85149" if r.gap > 0 else ("#3fb950" if r.gap < 0 else "#c9d1d9")

        # 尾段判定颜色
        if r.tail_verdict == "看多":
            ts_color = "#f85149"
            ts_verdict_html = "🟢看多"
        else:
            ts_color = "#3fb950"
            ts_verdict_html = "🔴不看多"

        # 尾段买/卖颜色
        bid_color = "#f85149" if r.tail_bid_delta > 0 else ("#3fb950" if r.tail_bid_delta < 0 else "#c9d1d9")
        ask_color = "#3fb950" if r.tail_ask_delta > 0 else ("#f85149" if r.tail_ask_delta < 0 else "#c9d1d9")
        total_bid_color = "#f85149" if r.total_bid_delta > 0 else ("#3fb950" if r.total_bid_delta < 0 else "#c9d1d9")
        total_ask_color = "#3fb950" if r.total_ask_delta > 0 else ("#f85149" if r.total_ask_delta < 0 else "#c9d1d9")

        # 信号列表
        sigs_html = "<br>".join(html_module.escape(s) for s in r.tail_signals)

        rows += f"""<tr>
<td>{i}</td>
<td>{html_module.escape(r.code)}</td>
<td style="text-align:left;font-weight:600">{html_module.escape(r.name)}</td>
<td>{r.prev_close:.2f}</td>
<td>{r.close_price:.2f}</td>
<td style="color:{chg_color};font-weight:600">{r.gap:+.2f}%</td>
<td style="color:{bid_color};font-weight:600">{r.tail_bid_delta:+d}</td>
<td style="color:{ask_color};font-weight:600">{r.tail_ask_delta:+d}</td>
<td style="color:{total_bid_color}">{r.total_bid_delta:+d}</td>
<td style="color:{total_ask_color}">{r.total_ask_delta:+d}</td>
<td style="color:{ts_color};font-weight:700">{ts_verdict_html}</td>
<td style="font-weight:700">{r.tail_score}</td>
<td style="text-align:left;font-size:11px;line-height:1.6">{sigs_html}</td>
</tr>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>收盘竞价 - 尾段判定报告</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#0d1117;color:#c9d1d9;padding:16px}}
.hd{{text-align:center;padding:16px 0}}
.hd h1{{font-size:20px;color:#58a6ff}}
.hd .t{{color:#8b949e;font-size:12px;margin-top:4px}}
.tbl-wrap{{overflow-x:auto;margin:0 auto;max-width:1400px}}
table{{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}}
th{{background:#161b22;color:#8b949e;font-weight:600;padding:8px 10px;text-align:center;border-bottom:2px solid #30363d;position:sticky;top:0}}
td{{padding:6px 10px;text-align:center;border-bottom:1px solid #21262d}}
tr:hover{{background:rgba(88,166,255,.06)}}
.legend{{text-align:center;margin:12px 0;font-size:12px;color:#8b949e}}
.legend span{{margin:0 8px}}
</style></head><body>
<div class="hd">
<h1>📊 收盘集合竞价 · 尾段判定报告</h1>
<div class="t">{now} · 监控 {len(results)} 只股票 · 14:57-15:00 采集</div>
</div>
<div class="legend">
<span>🟢看多 = 尾段买盘增强</span>
<span>🔴不看多 = 尾段卖盘增强或数据异常</span>
<span style="color:#58a6ff">尾段 = 最后两次快照差值</span>
<span style="color:#f0883e">全程 = 14:57→15:00 总差值</span>
</div>
<div class="tbl-wrap">
<table>
<tr>
<th>#</th><th>代码</th><th>名称</th><th>昨收</th><th>收盘</th><th>涨幅</th>
<th>尾段买</th><th>尾段卖</th><th>全程买</th><th>全程卖</th>
<th>判定</th><th>得分</th><th>信号</th>
</tr>
{rows}
</table>
</div>
</body></html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def analyze(code: str, quote: dict, hist: list[dict]) -> Optional[AuctionResult]:
    pc = quote["prev_close"]
    op = quote["open"]
    if pc <= 0 or op <= 0:
        return None

    vol = quote["volume"]
    amt = quote["amount"]
    chg = quote["change_pct"]
    amp = quote["amplitude"]
    to = quote["turnover"]
    bv = quote.get("bid1_v", 0)    # 竞价买一量（尾段真实买盘）
    sv = quote.get("ask1_v", 0)    # 竞价卖一量（尾段真实卖盘）
    gap = (op - pc) / pc * 100

    avg_vol = 0
    if hist:
        vs = [float(h.get("volume", 0)) for h in hist[-5:] if float(h.get("volume", 0)) > 0]
        if vs:
            avg_vol = np.mean(vs)
    vr = vol / avg_vol if avg_vol > 0 else 1.0

    tv = bv + sv
    br = (bv / tv * 100) if tv > 0 else 50

    sigs = []
    bull = 0
    bear = 0

    # 1. 跳空
    if gap > 5:
        sigs.append(f"🔴 强势高开 {gap:+.2f}%"); bull += 30
    elif gap > 3:
        sigs.append(f"🔴 明显高开 {gap:+.2f}%"); bull += 25
    elif gap > 1:
        sigs.append(f"🟠 温和高开 {gap:+.2f}%"); bull += 15
    elif gap > 0:
        sigs.append(f"🟡 微幅高开 {gap:+.2f}%"); bull += 5
    elif gap > -1:
        sigs.append(f"🟡 微幅低开 {gap:+.2f}%"); bear += 5
    elif gap > -3:
        sigs.append(f"🟠 温和低开 {gap:+.2f}%"); bear += 15
    else:
        sigs.append(f"🔴 强势低开 {gap:+.2f}%"); bear += 25

    # 2. 量比（根据涨跌方向判断多空倾向）
    if vr > 5:
        sigs.append(f"📊 量比 {vr:.2f}x → 极度放量")
        if gap > 0:
            bull += 25  # 高开放量 → 抢筹
        elif gap < 0:
            bear += 25  # 低开放量 → 出货
        else:
            bull += 10; bear += 10  # 平开放量 → 多空分歧
    elif vr > 3:
        sigs.append(f"📊 量比 {vr:.2f}x → 大幅放量")
        if gap > 0:
            bull += 20
        elif gap < 0:
            bear += 20
        else:
            bull += 10; bear += 5
    elif vr > 1.5:
        sigs.append(f"📊 量比 {vr:.2f}x → 温和放量"); bull += 15
    elif vr > 0.8:
        sigs.append(f"📊 量比 {vr:.2f}x → 量能正常")
    elif vr > 0.5:
        sigs.append(f"📊 量比 {vr:.2f}x → 温和缩量"); bear += 10
    else:
        sigs.append(f"📊 量比 {vr:.2f}x → 严重缩量"); bear += 15

    # 3. 买卖盘
    if br > 60:
        sigs.append(f"💪 外盘 {br:.1f}% → 主动买入占优"); bull += 15
    elif br > 55:
        sigs.append(f"💪 外盘 {br:.1f}% → 买盘略强"); bull += 8
    elif br < 40:
        sigs.append(f"🔻 外盘 {br:.1f}% → 主动卖出占优"); bear += 15
    elif br < 45:
        sigs.append(f"🔻 外盘 {br:.1f}% → 卖盘略强"); bear += 8
    else:
        sigs.append(f"⚖️ 外盘 {br:.1f}% → 多空平衡")

    # 4. 组合
    if gap > 1 and vr > 1.5 and br > 55:
        sigs.append("✅ 高开+放量+买盘强 → 强烈抢筹!"); bull += 25
    if gap > 2 and vr < 0.8:
        sigs.append("⚠️ 高开+缩量 → 疑似诱多!"); bear += 25
    if gap < -1 and vr > 1.5 and br < 45:
        sigs.append("🚨 低开+放量+卖盘强 → 强烈出货!"); bear += 30
    if gap < -1 and vr < 0.8:
        sigs.append("💡 低开+缩量 → 可能洗盘"); bull += 10
    if gap > 1 and vr > 2 and br < 45:
        sigs.append("⚡ 高开+放量但卖压重 → 多空分歧"); bear += 15
    if abs(gap) < 0.5 and vr > 3:
        sigs.append("👀 平开+大幅放量 → 有大资金动作"); bull += 10; bear += 10

    # 5. 振幅
    if amp > 5:
        sigs.append(f"📈 振幅 {amp:.2f}% → 波动剧烈"); bear += 10

    # 6. 换手率
    if to > 10:
        sigs.append(f"🔄 换手 {to:.2f}% → 极度活跃"); bull += 10; bear += 15
    elif to > 5:
        sigs.append(f"🔄 换手 {to:.2f}% → 高度活跃"); bull += 10; bear += 5
    elif to < 0.5:
        sigs.append(f"🔄 换手 {to:.2f}% → 交投清淡")

    # 7. 近期走势
    if hist and len(hist) >= 3:
        cs = [float(h.get("close", 0)) for h in hist[-3:]]
        changes = [(cs[i] - cs[i-1]) / cs[i-1] * 100 for i in range(1, len(cs)) if cs[i-1] > 0]
        if changes:
            avg = np.mean(changes)
            if avg > 2 and gap > 1:
                sigs.append("📈 连涨+高开 → 强势延续，注意追高"); bull += 10; bear += 10
            elif avg < -2 and gap < -1:
                sigs.append("📉 连跌+低开 → 弱势延续"); bear += 15
            elif avg < -2 and gap > 1:
                sigs.append("🔄 连跌+高开 → 可能止跌反弹!"); bull += 20

    bs = min(bull, 100)
    rs = min(bear, 100)
    net = bs - rs
    if net > 25:
        v = "真实抢筹"
    elif net < -10:
        v = "疑似出货"
    else:
        v = "正常"

    # ---- 尾段竞价判定 ----
    is_limit_up = chg >= 9.5
    ts_score, ts_verdict, ts_signals = tail_segment_verdict(
        gap=gap, vol=vol, buy_vol=bv, sell_vol=sv,
        is_limit_up=is_limit_up,
    )

    return AuctionResult(
        code=code, name=quote["name"],
        prev_close=pc, open_price=op, price=quote["price"],
        volume=vol, amount=amt, change_pct=chg,
        volume_ratio=round(vr, 2), open_gap=round(gap, 2),
        amplitude=amp, turnover=to, buy_ratio=round(br, 1),
        buy_vol=bv, sell_vol=sv,
        bull_score=bs, bear_score=rs, verdict=v, signals=sigs,
        tail_score=ts_score, tail_verdict=ts_verdict, tail_signals=ts_signals,
    )


# ============================================================
# 输出
# ============================================================

def print_result(r: AuctionResult):
    arrow = "↑" if r.open_gap > 0 else ("↓" if r.open_gap < 0 else "→")
    print(f"\n{'─'*56}")
    print(f"  {r.name} ({r.code})")
    print(f"{'─'*56}")
    print(f"  昨收:{r.prev_close:.2f} 今开:{r.open_price:.2f} 最新:{r.price:.2f}")
    print(f"  涨跌:{r.change_pct:+.2f}% 跳空:{r.open_gap:+.2f}% {arrow}")
    print(f"  成交:{r.volume:,}手 金额:{r.amount:,.0f}万")
    print(f"  量比:{r.volume_ratio}x 换手:{r.turnover:.2f}% 振幅:{r.amplitude:.2f}%")
    print(f"  外盘:{r.buy_ratio:.1f}% 内盘:{100-r.buy_ratio:.1f}%")
    print()
    for s in r.signals:
        print(f"    {s}")
    print()
    bb = "█" * (r.bull_score // 5) + "░" * (20 - r.bull_score // 5)
    rb = "█" * (r.bear_score // 5) + "░" * (20 - r.bear_score // 5)
    print(f"  抢筹 [{bb}] {r.bull_score}/100")
    print(f"  出货 [{rb}] {r.bear_score}/100")
    print(f"\n  🔮 {r.verdict}")
    # 尾段竞价判定
    if r.tail_verdict and r.tail_verdict != "-":
        ts_icon = "🟢" if r.tail_verdict == "看多" else "🔴"
        print(f"  {ts_icon} 尾段判定: {r.tail_verdict} (得分:{r.tail_score})")
        for ts in r.tail_signals:
            print(f"    {ts}")
    print(f"{'─'*56}")


def _format_volume(vol: int) -> str:
    """格式化搓合量：万手/手"""
    if vol >= 10000:
        return f"{vol / 10000:.1f}万"
    return f"{vol:,}手"


def _compute_strategy(r: AuctionResult) -> str:
    """根据信号推断策略"""
    net = r.bull_score - r.bear_score
    sig_text = " ".join(r.signals)
    if "连跌" in sig_text and "高开" in sig_text:
        return "1进2，止跌反弹"
    if "连涨" in sig_text and "高开" in sig_text:
        return "强势延续"
    if r.change_pct >= 9.5:
        return "一字板/涨停"
    if net > 20 and r.volume_ratio > 2:
        return "5w首板、新首板"
    if net > 15:
        return "新首板"
    if net > 5:
        return "四万首板"
    if net < -10:
        return "三万首板"
    if r.change_pct > 3:
        return "新首板"
    return "观望"


def _auction_result_to_screen_dict(r: AuctionResult) -> dict:
    """将 AuctionResult 转换为 save_screen_html 所需的 dict 格式"""
    buy_vol = r.buy_vol
    sell_vol = r.sell_vol
    remaining_rate = round(buy_vol / (buy_vol + sell_vol) * 100, 1) if (buy_vol + sell_vol) > 0 else 50.0
    return {
        "code": r.code,
        "name": r.name,
        "price": r.price,
        "prev_close": r.prev_close,
        "auction_price": r.open_price,
        "price_0926": r.price,
        "chg_0926": r.change_pct,
        "auction_vol": r.volume,
        "volume": r.volume,
        "comp_ratio": 0,
        "remaining_rate": remaining_rate,
        "verdict": r.verdict,
        "frequency": len([s for s in r.signals if any(k in s for k in ["高开", "低开", "放量", "缩量", "买盘", "卖盘", "连涨", "连跌", "平开"])]),
        "freq_signals": [s for s in r.signals if any(k in s for k in ["高开", "低开", "放量", "缩量", "买盘", "卖盘", "连涨", "连跌", "平开"])],
        "strategy": _compute_strategy(r),
        "passed_pools": [],
        "sector": "-",
        "sector_limit_count": 0,
        "is_leader": False,
        "leader_count": 0,
        "macd_dif": r.macd_dif,
        "macd_dea": r.macd_dea,
        "macd_bar": r.macd_bar,
        "macd_trend": r.macd_trend,
        "tail_score": r.tail_score,
        "tail_verdict": r.tail_verdict,
        "tail_signals": r.tail_signals,
    }


# ============================================================
# 核心接口
# ============================================================

def _is_mainboard_a(code: str) -> bool:
    """判断是否为沪深主板A股（排除创业板、科创板、北交所等）"""
    return code.startswith(("60", "00"))


def _check_has_limit_up_in_days(code: str, days: int = 120, cached_klines: list[dict] = None) -> bool:
    """
    检查股票在最近N个交易日内是否有涨停记录
    通过逐日K线检查涨幅是否接近涨停阈值
    """
    try:
        klines = cached_klines if cached_klines is not None else []
        if not klines or len(klines) < 5:
            return False
        # 只检查最近 N 天的数据
        klines = klines[-days:]
        # 主板涨停10%，创业板/科创板20%
        if code.startswith(("30", "68")):
            limit_pct = 20
        else:
            limit_pct = 10
        threshold = limit_pct * 0.95  # 9.5% 或 19.5%
        for i in range(1, len(klines)):
            try:
                prev_c = float(klines[i - 1].get("close", 0))
                curr_c = float(klines[i].get("close", 0))
                if prev_c > 0:
                    chg = (curr_c - prev_c) / prev_c * 100
                    if chg >= threshold:
                        return True
            except (ValueError, TypeError):
                continue
    except Exception as e:
        print(f"  ⚠ 涨停检查异常({code}): {e}")
    return False


# ============================================================
# MACD 计算
# ============================================================

def _ema(data: list, period: int) -> list[float]:
    """模块级EMA计算（供策略池4深度分析使用）"""
    if not data:
        return []
    ema = [0.0] * len(data)
    k = 2.0 / (period + 1)
    ema[0] = data[0]
    for i in range(1, len(data)):
        ema[i] = data[i] * k + ema[i - 1] * (1 - k)
    return ema


def _compute_macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float], list[float]]:
    """
    计算 MACD
    返回: (macd_line, signal_line, histogram)
    """
    if len(closes) < slow + signal:
        return [], [], []

    def _ema(data, period):
        ema = [0.0] * len(data)
        k = 2.0 / (period + 1)
        ema[0] = data[0]
        for i in range(1, len(data)):
            ema[i] = data[i] * k + ema[i - 1] * (1 - k)
        return ema

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    signal_line = _ema(macd_line, signal)
    histogram = [macd_line[i] - signal_line[i] for i in range(len(closes))]
    return macd_line, signal_line, histogram


# ---- 板块5日涨幅检查 ----
_sector_kline_cache = {}  # {sector_code: [kline_data]}

def _check_sector_5day_rise(sector_code: str, threshold: float = 5.0,
                                 cached_sector_klines: dict = None) -> bool:
    """检查板块近5日涨幅是否超过阈值（无数据时默认通过）"""
    klines = (cached_sector_klines or {}).get(sector_code, [])
    if len(klines) < 6:
        return True  # 无数据时默认通过，不阻断筛选
    # 取最近6根K线，用第1根的开盘和第6根的收盘计算5日涨幅
    close_now = float(klines[-1]["close"])
    close_5ago = float(klines[-6]["close"])
    if close_5ago <= 0:
        return False
    rise_pct = (close_now - close_5ago) / close_5ago * 100
    return rise_pct >= threshold


def _check_macd_120min_up(code: str, cached_klines: list[dict] = None,
                          cached_klines_120min: list[dict] = None) -> bool:
    """
    检查120分钟MACD是否向上（MACD线当前 > 前一根 且 MACD > 0）
    结果缓存：同一code不会重复请求。
    若120分钟数据不可用，用日K线MACD做代理
    """
    # 检查缓存
    if code in _macd_120min_cache:
        return _macd_120min_cache[code]

    klines = cached_klines_120min if cached_klines_120min else []
    if klines and len(klines) >= 35:
        closes = [float(k.get("close", 0)) for k in klines if float(k.get("close", 0)) > 0]
        if len(closes) >= 35:
            macd_line, _, _ = _compute_macd(closes)
            if len(macd_line) >= 2:
                result = macd_line[-1] > macd_line[-2] and macd_line[-1] > 0
                _macd_120min_cache[code] = result
                return result

    # fallback: 用日K线MACD做代理（120分钟数据不可用时）
    day_klines = cached_klines if cached_klines is not None else []
    if not day_klines or len(day_klines) < 35:
        _macd_120min_cache[code] = False
        return False
    closes = [float(k.get("close", 0)) for k in day_klines if float(k.get("close", 0)) > 0]
    if len(closes) < 35:
        _macd_120min_cache[code] = False
        return False
    macd_line, _, _ = _compute_macd(closes)
    if len(macd_line) < 2:
        _macd_120min_cache[code] = False
        return False
    result = macd_line[-1] > macd_line[-2] and macd_line[-1] > 0
    _macd_120min_cache[code] = result
    return result


def _check_weekly_macd_red_growing(code: str, cached_klines: list[dict] = None) -> bool:
    """
    检查周MACD红柱变大
    用日K线按真实日历周聚合为周K线，计算MACD，检查最近一根红柱 > 前一根
    """
    klines = cached_klines if cached_klines is not None else []
    if not klines or len(klines) < 60:
        return False

    # 日K线按真实日历周聚合为周K线（每周最后一个交易日的收盘价）
    weekly_closes = []
    last_week = -1
    last_close_in_week = 0.0
    for k in klines:
        try:
            c = float(k.get("close", 0))
            day_str = k.get("day", "")
        except (ValueError, TypeError):
            continue
        if c <= 0 or not day_str:
            continue
        try:
            dt = datetime.strptime(day_str[:10], "%Y-%m-%d")
            iso_week = dt.isocalendar()[1]
            iso_year = dt.year
            week_key = iso_year * 100 + iso_week
        except (ValueError, IndexError):
            continue
        if last_week >= 0 and week_key != last_week:
            # 上一周结束，追加上一周最后一个交易日的收盘价
            weekly_closes.append(last_close_in_week)
        last_week = week_key
        last_close_in_week = c
    # 追加最后一周的收盘价
    if last_close_in_week > 0:
        if not weekly_closes or weekly_closes[-1] != last_close_in_week:
            weekly_closes.append(last_close_in_week)

    if len(weekly_closes) < 35:
        return False

    _, _, hist = _compute_macd(weekly_closes)
    if len(hist) < 2:
        return False
    # 红柱 > 0 且变大（当前 > 前一根）
    return hist[-1] > 0 and hist[-1] > hist[-2]


def _check_monthly_macd_red_up(code: str, cached_klines: list[dict] = None) -> bool:
    """
    检查月MACD红柱向上
    用日K线按真实日历月聚合为月K线，计算MACD，检查最近红柱 > 0 且向上
    """
    klines = cached_klines if cached_klines is not None else []
    if not klines or len(klines) < 60:
        return False

    # 日K线按真实日历月聚合为月K线（每月最后一个交易日的收盘价）
    monthly_closes = []
    last_month = -1
    last_close_in_month = 0.0
    for k in klines:
        try:
            c = float(k.get("close", 0))
            day_str = k.get("day", "")
        except (ValueError, TypeError):
            continue
        if c <= 0 or not day_str:
            continue
        try:
            dt = datetime.strptime(day_str[:10], "%Y-%m-%d")
            month_key = dt.year * 100 + dt.month
        except (ValueError, IndexError):
            continue
        if last_month >= 0 and month_key != last_month:
            monthly_closes.append(last_close_in_month)
        last_month = month_key
        last_close_in_month = c
    # 追加最后一个月的收盘价
    if last_close_in_month > 0:
        if not monthly_closes or monthly_closes[-1] != last_close_in_month:
            monthly_closes.append(last_close_in_month)

    if len(monthly_closes) < 35:
        return False

    _, _, hist = _compute_macd(monthly_closes)
    if len(hist) < 2:
        return False
    # 红柱 > 0 且向上（当前 > 前一根）
    return hist[-1] > 0 and hist[-1] > hist[-2]


def pool_base(code: str, name: str, price: float, market_cap_yi: float,
              avg_price: float = 0, yesterday_amount: float = 0,
              last_limit_amount: float = 0, amount_5min: float = 0) -> tuple[bool, str]:
    """
    策略池1 · 基础池（前置硬性门槛）
    ─────────────────────────────────
    条件（非K线部分）:
      1.  主板（沪主板60 / 深主板00，排除创业板/科创板/北交所）
      2.  去除ST
      3.  流通市值 > 35.99亿
      4.  流通市值 < 999.99亿
      5.  股价 < 60元
      6.  当前股价在均价线之上（price > VWAP）
      7.  昨日成交金额 > 上次涨停日成交金额（需外部传入）
      8.  开盘5分钟成交金额 > 3000万（需外部传入）
    所有策略的前置条件，不通过则直接淘汰
    """
    # 1. 主板
    if not code.startswith(("60", "00")):
        return False, "非主板"
    # 2. 去除ST
    if "ST" in name.upper():
        return False, "ST"
    # 3-4. 流通市值 35.99亿 ~ 999.99亿
    if market_cap_yi <= 35.99:
        return False, "流通市值≤35.99亿"
    if market_cap_yi >= 999.99:
        return False, "流通市值≥999.99亿"
    # 5. 股价 < 60
    if price >= 60:
        return False, "股价≥60"
    # 6. 当前股价在均价线之上
    if avg_price > 0 and price <= avg_price:
        return False, "股价低于均价线"
    # 7. 昨日成交金额 > 上次涨停日成交金额
    if last_limit_amount > 0 and yesterday_amount > 0:
        if yesterday_amount <= last_limit_amount:
            return False, "昨额≤涨停额"
    # 8. 开盘5分钟成交金额 > 3000万
    if amount_5min > 0 and amount_5min <= 30000000:
        return False, "5分钟额≤3000万"
    return True, ""


def pool_base_post_kline(code: str, price: float, klines: list[dict],
                         prev_close: float, yesterday_amount: float) -> tuple[bool, str]:
    """
    策略池1 · 基础池（K线依赖部分）
    ─────────────────────────────────
    条件（需K线数据）:
      9.  昨日成交金额 > 上次涨停日成交金额
      10. 7天涨幅 < 34.99%
      11. 去除昨日连板（前天也涨停则排除）
      12. 最低价 < 昨日收盘价
      13. 股价 > 昨日开盘价
      14. 当前股价 > 昨日收盘价
    """
    if not klines or len(klines) < 2:
        return False, "K线数据不足"

    # 获取昨日前一天的K线（用于判断连板和获取昨日开盘价）
    yesterday_kline = klines[-2] if len(klines) >= 2 else None
    dby_kline = klines[-3] if len(klines) >= 3 else None

    if not yesterday_kline:
        return False, "缺少昨日K线"

    y_open = float(yesterday_kline.get("open", 0))
    y_close = float(yesterday_kline.get("close", 0))
    y_low = float(yesterday_kline.get("low", 0))
    y_high = float(yesterday_kline.get("high", 0))
    y_amount = float(yesterday_kline.get("amount", 0))  # 昨日成交额

    # 9. 昨日成交金额 > 上次涨停日成交金额
    # 找最近一次涨停日（排除昨日），比较昨额与涨停额
    limit_pct = 10  # 主板
    threshold = limit_pct * 0.95
    last_limit_amount = 0
    for i in range(len(klines) - 2, max(0, len(klines) - 62), -1):  # 最近60天内找
        if i < 1:
            break
        prev_c = float(klines[i - 1].get("close", 0))
        curr_c = float(klines[i].get("close", 0))
        if prev_c > 0 and (curr_c - prev_c) / prev_c * 100 >= threshold:
            last_limit_amount = float(klines[i].get("amount", 0))
            break
    if last_limit_amount > 0 and y_amount <= last_limit_amount:
        return False, "昨额≤涨停额"

    # 10. 7天涨幅 < 34.99%
    if len(klines) >= 8:
        close_7d_ago = float(klines[-8].get("close", 0))
        if close_7d_ago > 0:
            gain_7d = (y_close - close_7d_ago) / close_7d_ago * 100
            if gain_7d >= 34.99:
                return False, f"7天涨幅{gain_7d:.1f}%≥35%"

    # 11. 去除昨日连板（前天也涨停则排除）
    if dby_kline:
        dby_close = float(dby_kline.get("close", 0))
        if dby_close > 0:
            y_chg = (y_close - dby_close) / dby_close * 100
            if y_chg >= threshold:
                # 前天收盘→昨日涨停，检查再前一天是否也涨停
                if len(klines) >= 4:
                    dby_prev = float(klines[-4].get("close", 0))
                    if dby_prev > 0:
                        dby_chg = (dby_close - dby_prev) / dby_prev * 100
                        if dby_chg >= threshold:
                            return False, "昨日连板"

    # 12. 最低价 < 昨日收盘价（今日最低价低于昨收，表示有回踩）
    # 注意：集合竞价/开盘前阶段 low 就是撮合价，高开股必然 low >= prev_close，
    # 此规则仅在开盘后有分时数据时才适用
    _now = datetime.now()
    _before_market_open = _now.hour < 9 or (_now.hour == 9 and _now.minute < 30)
    if not _before_market_open:
        today_low = float(klines[-1].get("low", 0)) if len(klines) >= 1 else 0
        if today_low > 0 and prev_close > 0:
            if today_low >= prev_close:
                return False, "最低价≥昨收"

    # 13. 股价 > 昨日开盘价
    if y_open > 0 and price <= y_open:
        return False, "股价≤昨开"

    # 14. 当前股价 > 昨日收盘价
    if y_close > 0 and price <= y_close:
        return False, "股价≤昨收"

    return True, ""


def pool_volume_price(c: dict, tc: dict, em: dict, klines: list = None) -> tuple[bool, str]:
    """
    策略池2 · 量价池（集合竞价量价信号）
    ─────────────────────────────────────
    条件:
      1.  主板（沪60/深00）
      2.  非ST
      3.  集合竞价涨幅 > 1%
      4.  市值 < 700亿
      5.  前一日涨停取反（昨日未涨停）
      6.  非盘中下跌（竞价价 >= 昨收）
      7.  今日竞价金额/昨日竞价金额 > 1.5倍
      8.  集合竞价换手率 > 0.11%
      9.  集合竞价量比 > 5
      10. 3日涨幅 < 15%（需K线数据）
      11. 集合竞价现手量 > 40000手
    核心逻辑：筛选出竞价阶段资金明显抢筹的标的
    """
    code = c["code"]
    name = c.get("name", "")

    # 1. 主板
    if not code.startswith(("60", "00")):
        return False, "非主板"
    # 2. 非ST
    if "ST" in name.upper():
        return False, "ST"
    # 3. 涨幅 > 1%
    if c["auction_gain"] <= 1:
        return False, "涨幅≤1%"
    # 4. 市值 < 700亿
    market_cap_yi = tc.get("market_cap_yi", 0) or em.get("market_cap_yi", 0) or c.get("market_cap_yi", 0)
    if market_cap_yi >= 700:
        return False, "市值≥700亿"
    # 5. 前一日涨停取反（昨日未涨停）
    if klines and len(klines) >= 3:
        try:
            dby_close = float(klines[-3].get("close", 0))
            y_close = float(klines[-2].get("close", 0))
            if dby_close > 0:
                y_chg = (y_close - dby_close) / dby_close * 100
                if y_chg >= 9.5:  # 主板涨停阈值
                    return False, "前一日涨停"
        except (ValueError, TypeError):
            pass
    # 6. 非盘中下跌（竞价价 >= 昨收）
    if c["open_price"] < c["prev_close"]:
        return False, "盘中下跌"
    # 竞价量（手）
    auction_vol = tc.get("volume", 0) or em.get("volume", 0) or c.get("volume", 0)
    if auction_vol <= 0:
        return False, "竞价量为0"
    # 昨成交量（股→手）
    yesterday_vol_shares = c.get("volume_shares", 0)
    yesterday_vol_lots = yesterday_vol_shares // 100
    if yesterday_vol_lots <= 0:
        return False, "昨成交量为0"
    # 7. 今日竞价金额/昨日竞价金额 > 1.5倍
    today_amount = auction_vol * c["price"] * 100
    yesterday_amount = yesterday_vol_shares * c["prev_close"]
    if yesterday_amount <= 0:
        return False, "昨金额为0"
    amount_ratio = today_amount / yesterday_amount
    if amount_ratio <= 1.5:
        return False, "金额比≤1.5"
    # 8. 换手率 > 0.11%
    turnover = em.get("turnover", 0) or tc.get("turnover", 0) or c.get("turnover_sina", 0)
    if turnover <= 0.11:
        return False, "换手率≤0.11%"
    # 9. 量比 > 5
    est_auction_avg = yesterday_vol_lots * (10 / 240)
    volume_ratio = auction_vol / est_auction_avg if est_auction_avg > 0 else 0
    if volume_ratio <= 5:
        return False, "量比≤5"
    # 10. 3日涨幅 < 15%
    if klines and len(klines) >= 4:
        try:
            closes = [float(k.get("close", 0)) for k in klines[-4:]]
            if closes[0] > 0:
                change_3d = (closes[-1] - closes[0]) / closes[0] * 100
                if change_3d >= 15:
                    return False, f"3日涨幅≥15%"
        except (ValueError, TypeError):
            pass
    # 11. 竞价量 > 40000手
    if auction_vol <= 40000:
        return False, "竞价量≤4万手"

    # 写回计算字段
    c["auction_vol"] = auction_vol
    c["vol_ratio_yesterday"] = round(auction_vol / yesterday_vol_lots * 100, 2)
    c["volume_ratio"] = round(volume_ratio, 2)
    c["turnover"] = round(turnover, 4)
    c["yesterday_vol"] = yesterday_vol_lots
    c["amount_ratio"] = round(amount_ratio, 2)
    if market_cap_yi > 0:
        c["market_cap_yi"] = market_cap_yi

    return True, ""


def pool_trend(c: dict, klines: list[dict], tc: dict = None, em: dict = None) -> tuple[bool, str]:
    """
    策略池3 · 趋势池（竞价趋势信号）
    ─────────────────────────────────
    条件:
      1.  主板（沪60/深00）
      2.  非ST
      3.  集合竞价涨幅 > 3% 且 < 10%
      4.  120日内有涨停
      5.  市值 < 1000亿
      6.  前一日涨停取反（昨日未涨停）
      7.  非盘中下跌（竞价价 >= 昨收）
      8.  开盘跳空高开（open > prev_close）
      9.  集合竞价量比 > 3
      10. 集合竞价换手率 > 0.1%
    排序：竞价涨幅从大到小（在主流程排序）
    """
    code = c["code"]
    name = c.get("name", "")
    if tc is None:
        tc = {}
    if em is None:
        em = {}

    # 1. 主板
    if not code.startswith(("60", "00")):
        return False, "非主板"
    # 2. 非ST
    if "ST" in name.upper():
        return False, "ST"
    # 3. 涨幅 > 3% 且 < 10%
    if c["auction_gain"] <= 3 or c["auction_gain"] >= 10:
        return False, "涨幅不在3-10%"
    # 4. 120日内有涨停
    if not _check_has_limit_up_in_days(code, days=120, cached_klines=klines):
        return False, "120日内无涨停"
    # 5. 市值 < 1000亿
    market_cap_yi = tc.get("market_cap_yi", 0) or em.get("market_cap_yi", 0) or c.get("market_cap_yi", 0)
    if market_cap_yi >= 1000:
        return False, "市值≥1000亿"
    # 6. 前一日涨停取反
    if klines and len(klines) >= 3:
        try:
            dby_close = float(klines[-3].get("close", 0))
            y_close = float(klines[-2].get("close", 0))
            if dby_close > 0:
                y_chg = (y_close - dby_close) / dby_close * 100
                if y_chg >= 9.5:
                    return False, "前一日涨停"
        except (ValueError, TypeError):
            pass
    # 7. 非盘中下跌
    if c["open_price"] < c["prev_close"]:
        return False, "盘中下跌"
    # 8. 开盘跳空高开
    if c["open_price"] <= c["prev_close"]:
        return False, "未高开"
    # 9. 集合竞价量比 > 3
    auction_vol = tc.get("volume", 0) or em.get("volume", 0) or c.get("volume", 0)
    yesterday_vol_shares = c.get("volume_shares", 0)
    yesterday_vol_lots = yesterday_vol_shares // 100
    if yesterday_vol_lots > 0 and auction_vol > 0:
        est_auction_avg = yesterday_vol_lots * (10 / 240)
        volume_ratio = auction_vol / est_auction_avg if est_auction_avg > 0 else 0
        if volume_ratio <= 3:
            return False, "量比≤3"
    # 10. 集合竞价换手率 > 0.1%
    turnover = em.get("turnover", 0) or tc.get("turnover", 0) or c.get("turnover_sina", 0)
    if turnover <= 0.1:
        return False, "换手率≤0.1%"

    return True, ""


def pool_technical(code: str, klines: list[dict], name: str = "",
                   price: float = 0, market_cap_yi: float = 0,
                   sector: str = "", sector_code: str = "",
                   kline120_map: dict = None, sector_klines: dict = None) -> tuple[bool, str]:
    """
    策略池4 · 技术池（多周期MACD共振）
    ─────────────────────────────────
    条件:
      1.  主板（沪60/深00）
      2.  非ST
      3.  120分钟MACD向上
      4.  市值 < 400亿
      5.  价格 < 120元
      6.  月MACD红柱向上
      7.  周MACD红柱变大
      8.  2个月内有过涨停
      9.  板块5日涨幅 > 5%
    核心逻辑：多周期MACD共振确认趋势向上，排除假突破
    """
    # 1. 主板
    if not code.startswith(("60", "00")):
        return False, "非主板"
    # 2. 非ST
    if "ST" in name.upper():
        return False, "ST"
    # 3. 120分钟MACD向上
    k120 = (kline120_map or {}).get(code, [])
    if not _check_macd_120min_up(code, cached_klines=klines, cached_klines_120min=k120):
        return False, "120分钟MACD未向上"
    # 4. 市值 < 400亿
    if market_cap_yi >= 400:
        return False, "市值≥400亿"
    # 5. 价格 < 120元
    if price >= 120:
        return False, "价格≥120"
    # 6. 月MACD红柱向上
    if not _check_monthly_macd_red_up(code, cached_klines=klines):
        return False, "月MACD红柱未向上"
    # 7. 周MACD红柱变大
    if not _check_weekly_macd_red_growing(code, cached_klines=klines):
        return False, "周MACD红柱未变大"
    # 8. 2个月内有过涨停
    if not _check_has_limit_up_in_days(code, days=60, cached_klines=klines):
        return False, "2个月内无涨停"
    # 9. 板块5日涨幅 > 5%
    if sector_code and not _check_sector_5day_rise(sector_code, threshold=5.0,
                                                       cached_sector_klines=sector_klines):
        return False, f"板块5日涨幅<5%({sector})"
    return True, ""


# ============================================================
# 旧的统一筛选（已重构为调用策略池）
# ============================================================

def _screen_unified(candidates: list[dict], tencent_map: dict, em_data: dict = None,
                    kline_map: dict = None) -> list[dict]:
    """
    统一筛选策略 —— 策略池OR逻辑
    ─────────────────────────────
    策略池1(基础池) 为硬性前置门槛，不通过直接淘汰。
    通过基础池后，策略池2(量价)、策略池3(趋势)、策略池4(技术) 任一通过即可入选。
    （策略池3/4在此阶段仅做非K线预检，完整检查在后续K线阶段补充）

    策略池1 · 基础池：主板 / 去ST / 流通市值35.99~999.99亿 / 股价<60 / 均价线之上
    策略池2 · 量价池：主板 / 非ST / 涨幅>1% / 市值<700亿 / 前一日未涨停 / 非盘中下跌
              / 金额比>1.5 / 换手率>0.11% / 量比>5 / 3日涨幅<15% / 竞价量>4万手
    策略池3 · 趋势池：主板 / 非ST / 涨幅3-10% / 120日内有涨停 / 市值<1000亿 / 前一日未涨停
              / 非盘中下跌 / 高开 / 量比>3 / 换手率>0.1%（涨幅从大到小排名）
    策略池4 · 技术池：主板 / 非ST / 120分钟MACD↑ / 市值<400亿 / 价格<120 / 月MACD红柱↑ / 周MACD红柱↑ / 2月内有涨停 / 板块5日涨>5%

    最终流程：任一策略池通过 → 初选池 → 频次分析 → 按频次排名TOP10 → 龙头识别
    """
    if em_data is None:
        em_data = {}
    filtered = []
    _diag = {}  # 诊断计数器
    for c in candidates:
        code = c["code"]
        tc = tencent_map.get(code, {})
        em = em_data.get(code, {})
        name = c.get("name", "")
        market_cap_yi = tc.get("market_cap_yi", 0) or em.get("market_cap_yi", 0) or c.get("market_cap_yi", 0)

        # 均价线（VWAP）：腾讯字段32为均价，或用竞价金额/竞价量计算
        avg_price = 0
        try:
            avg_price = float(tc.get("avg_price", 0)) if tc.get("avg_price") else 0
        except (ValueError, TypeError):
            pass
        if avg_price <= 0:
            auction_vol = tc.get("volume", 0) or c.get("volume", 0)
            auction_amount = tc.get("amount", 0) or em.get("amount", 0)
            if auction_vol > 0 and auction_amount > 0:
                avg_price = auction_amount / auction_vol / 100  # 手→股

        # 昨日成交金额（K线数据在后续阶段补充，此处为0）
        yesterday_amount = 0

        # 上次涨停日成交金额（K线数据在后续阶段补充，此处为0）
        last_limit_amount = 0

        # 开盘5分钟成交金额（实时数据，腾讯/东财接口可能提供）
        amount_5min = 0

        # ---- 策略池1 · 基础池（非K线部分）—— 硬性前置门槛 ----
        ok, reason = pool_base(code, name, c["price"], market_cap_yi,
                               avg_price=avg_price, yesterday_amount=yesterday_amount,
                               last_limit_amount=last_limit_amount, amount_5min=amount_5min)
        if not ok:
            _diag[f"基础池:{reason}"] = _diag.get(f"基础池:{reason}", 0) + 1
            continue

        # ---- 策略池2 · 量价池 ----
        klines_for_pool2 = (kline_map or {}).get(code, [])
        ok2, reason2 = pool_volume_price(c, tc, em, klines=klines_for_pool2)

        # ---- 策略池3 · 趋势池（非K线预检部分）----
        # 完整的趋势池检查需要K线数据，在后续阶段做；此处做基础条件预检
        ok3_pre = True
        reason3_pre = ""
        if not code.startswith(("60", "00")):
            ok3_pre = False; reason3_pre = "非主板"
        elif "ST" in name.upper():
            ok3_pre = False; reason3_pre = "ST"
        elif c["auction_gain"] <= 3 or c["auction_gain"] >= 10:
            ok3_pre = False; reason3_pre = "涨幅不在3-10%"
        elif c["open_price"] < c["prev_close"]:
            ok3_pre = False; reason3_pre = "盘中下跌"
        elif c["open_price"] <= c["prev_close"]:
            ok3_pre = False; reason3_pre = "未高开"

        # ---- 策略池4 · 技术池（非K线预检部分）----
        # 完整的技术池检查需要K线数据，在后续阶段做；此处做基础条件预检
        ok4_pre = True
        reason4_pre = ""
        if not code.startswith(("60", "00")):
            ok4_pre = False; reason4_pre = "非主板"
        elif "ST" in name.upper():
            ok4_pre = False; reason4_pre = "ST"
        elif c["price"] >= 120:
            ok4_pre = False; reason4_pre = "价格≥120"
        elif market_cap_yi >= 400:
            ok4_pre = False; reason4_pre = "市值≥400亿"

        # ---- OR逻辑：量价池/趋势池/技术池 任一通过即可 ----
        passed_pools = []
        if ok2:
            passed_pools.append("量价池")
        if ok3_pre:
            passed_pools.append("趋势池")
        if ok4_pre:
            passed_pools.append("技术池")

        if not passed_pools:
            # 三个策略池都不通过，记录诊断
            if not ok2:
                _diag[f"量价池:{reason2}"] = _diag.get(f"量价池:{reason2}", 0) + 1
            # 趋势池和技术池的完整诊断在K线阶段输出
            continue

        # 补充 market_cap_yi
        if market_cap_yi > 0:
            c["market_cap_yi"] = market_cap_yi

        # 记录通过的策略池
        c["passed_pools"] = passed_pools
        filtered.append(c)

    # 输出诊断信息
    if _diag:
        print(f"  📋 过滤诊断 (共淘汰 {sum(_diag.values())} 只):")
        for reason, cnt in sorted(_diag.items(), key=lambda x: -x[1]):
            print(f"    ❌ {reason}: {cnt} 只")

    return filtered


def _get_board_type(code: str) -> str:
    """根据股票代码前缀判断板块类型"""
    if code.startswith("60"):
        return "沪主板"
    elif code.startswith("00"):
        return "深主板"
    elif code.startswith("30"):
        return "创业板"
    elif code.startswith("68"):
        return "科创板"
    return "其他"


def _compute_screen_strategy(s: dict) -> str:
    """根据筛选结果推断策略（阈值考虑筛选前提：chg>1%, vr>3）"""
    chg = s.get("auction_gain", 0)
    vr = s.get("volume_ratio", 0)
    rr = s.get("remaining_rate", 50)
    pools = s.get("passed_pools", [])

    # 基础策略标签
    if chg >= 9.5:
        base = "一字板/涨停"
    elif chg >= 7 and vr >= 5:
        base = "5w首板、新首板"
    elif chg >= 5 and vr >= 5 and rr >= 55:
        base = "5w首板、新首板"
    elif chg >= 5:
        base = "新首板"
    elif chg >= 3 and vr >= 5 and rr >= 55:
        base = "新首板"
    elif chg >= 3:
        base = "四万首板"
    else:
        base = "三万首板"

    # 追加策略池来源标记
    if pools:
        pool_tags = "/".join(pools)
        return f"{base} [{pool_tags}]"
    return base


def save_screen_html(stocks: list[dict], indices: list[dict], path: str, main_line_sectors: list = None) -> str:
    """保存主板筛选策略HTML报告（与截图一致的格式）"""
    if main_line_sectors is None:
        main_line_sectors = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 大盘指数板块
    idx_html = ""
    if indices:
        idx_cells = ""
        for idx in indices:
            chg = idx["change_pct"]
            color = "#f85149" if chg > 0 else ("#3fb950" if chg < 0 else "#c9d1d9")
            sign = "+" if chg > 0 else ""
            idx_cells += f"""<div style="background:#161b22;border-radius:8px;padding:12px 20px;text-align:center;min-width:160px">
<div style="color:#8b949e;font-size:12px">{idx["name"]}</div>
<div style="color:#f0f6fc;font-size:18px;font-weight:700;margin:4px 0">{idx["price"]:.2f}</div>
<div style="color:{color};font-size:14px;font-weight:600">{sign}{chg:.2f}%</div>
</div>"""
        idx_html = f"""
<div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-bottom:20px">
{idx_cells}
</div>"""

    # 股票表格
    rows = ""
    for i, s in enumerate(stocks, 1):
        chg_0926 = s.get("chg_0926", 0)
        chg_color = "#f85149" if chg_0926 > 0 else ("#3fb950" if chg_0926 < 0 else "#c9d1d9")

        # 筹码判断颜色
        verdict = s.get("verdict", "正常")
        if verdict == "真实抢筹":
            v_color = "#f85149"
            v_bg = "rgba(248,81,73,.12)"
        elif verdict == "疑似出货":
            v_color = "#3fb950"
            v_bg = "rgba(63,185,80,.12)"
        else:
            v_color = "#d29922"
            v_bg = "rgba(210,153,34,.12)"

        vol_fmt = _format_volume(s.get("auction_vol", s.get("volume", 0)))
        comp_ratio = s.get("comp_ratio", 0)
        remaining = s.get("remaining_rate", 50)
        freq = s.get("frequency", 0)
        strategy = s.get("strategy", "观望")
        sector = s.get("sector", "-")
        sector_lc = s.get("sector_limit_count", 0)
        is_leader = s.get("is_leader", False)
        leader_count = s.get("leader_count", 0)
        leader_mark = "🏆" if is_leader else ""
        leader_color = "#f0883e" if is_leader else "#8b949e"

        pools = "/".join(s.get("passed_pools", []))
        freq_sigs = ", ".join(s.get("freq_signals", []))

        # 尾段判定
        ts_verdict = s.get("tail_verdict", "-")
        ts_score = s.get("tail_score", 0)
        if ts_verdict == "看多":
            ts_color = "#f85149"
            ts_verdict_html = "🟢看多"
        elif ts_verdict == "不看多":
            ts_color = "#3fb950"
            ts_verdict_html = "🔴不看多"
        else:
            ts_color = "#8b949e"
            ts_verdict_html = "⚪-"

        # 主线板块标记
        main_line_1_name = main_line_sectors[0]["name"] if main_line_sectors else ""
        is_main_line = main_line_1_name and sector == main_line_1_name
        main_line_mark = "🔥主线" if is_main_line else ""
        main_line_color = "#f0883e" if is_main_line else "#8b949e"

        rows += f"""<tr>
<td>{html_module.escape(s["code"])}</td>
<td style="text-align:left;font-weight:600">{html_module.escape(s["name"])}</td>
<td style="text-align:left;font-size:12px">{html_module.escape(sector)}<span style="color:#8b949e;font-size:10px">({sector_lc}涨停)</span></td>
<td style="color:{main_line_color};font-weight:{'700' if is_main_line else '400'}">{main_line_mark}</td>
<td style="color:{leader_color};font-weight:{'700' if is_leader else '400'}">{leader_mark}{leader_count}</td>
<td>{s["auction_price"]:.2f}</td>
<td>{s.get("price_0926", s["price"]):.2f}</td>
<td>{vol_fmt}</td>
<td>{comp_ratio:.1f}%</td>
<td>{remaining:.1f}%</td>
<td style="color:{chg_color};font-weight:600">{chg_0926:+.2f}%</td>
<td style="color:{v_color};background:{v_bg};border-radius:4px;font-weight:600;padding:4px 8px">{verdict}</td>
<td style="font-weight:700">{freq}</td>
<td style="color:{ts_color};font-weight:600">{ts_verdict_html}({ts_score})</td>
<td style="text-align:left;font-size:11px">{html_module.escape(pools)}</td>
<td style="text-align:left;font-size:12px">{html_module.escape(strategy)}</td>
<td style="font-size:11px">{s.get("macd_dif", 0):.3f}</td>
<td style="font-size:11px">{s.get("macd_dea", 0):.3f}</td>
<td style="font-size:11px;color:{'#f85149' if s.get('macd_bar', 0) > 0 else '#3fb950'}">{s.get("macd_bar", 0):.3f}</td>
<td>{s.get("macd_trend", "-")}</td>
<td style="text-align:left;font-size:11px">{html_module.escape(freq_sigs)}</td>
</tr>"""

    # 板块分布统计
    sector_stats = {}
    for s in stocks:
        sn = s.get("sector", "未知")
        sector_stats[sn] = sector_stats.get(sn, 0) + 1
    sector_html = ""
    if sector_stats:
        sector_cells = ""
        for sn, cnt in sorted(sector_stats.items(), key=lambda x: -x[1]):
            sector_cells += f'<span style="background:#161b22;border-radius:4px;padding:4px 8px;margin:2px;display:inline-block;font-size:12px">{html_module.escape(sn)}: <b>{cnt}</b>只</span> '
        sector_html = f'<div style="text-align:center;margin-bottom:12px">{sector_cells}</div>'

    # 板块龙头出现次数统计（HTML）
    leader_html = ""
    leader_data = [(s["code"], s["name"], s.get("sector", ""), s.get("leader_count", 0), s.get("frequency", 0))
                   for s in stocks if s.get("is_leader")]
    if leader_data:
        leader_data.sort(key=lambda x: -x[3])
        leader_cells = ""
        for code, name, sector, cnt, freq in leader_data:
            leader_cells += f'<span style="background:#161b22;border:1px solid #f0883e;border-radius:4px;padding:4px 8px;margin:2px;display:inline-block;font-size:12px">🏆 {html_module.escape(name)}({html_module.escape(code)}) <span style="color:#f0883e;font-weight:700">频次{freq}</span> <span style="color:#8b949e;font-size:10px">{html_module.escape(sector)}({cnt}只)</span></span> '
        leader_html = f'<div style="text-align:center;margin-bottom:12px;padding:8px;background:rgba(240,136,62,.08);border-radius:8px"><div style="color:#f0883e;font-size:13px;font-weight:600;margin-bottom:6px">🏆 龙头股（板块内频次最高）</div>{leader_cells}</div>'

    # 主线板块排名HTML
    main_line_html = ""
    if main_line_sectors:
        ml_cells = ""
        for s in main_line_sectors[:10]:
            rank = s["rank"]
            rank_icon = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else f"#{rank}"))
            border = "border:1px solid #f0883e" if rank == 1 else "border:1px solid #30363d"
            bg = "rgba(240,136,62,.08)" if rank == 1 else "#161b22"
            sign_5d = "+" if s["avg_5d"] > 0 else ""
            sign_10d = "+" if s["avg_10d"] > 0 else ""
            ml_cells += f"""<div style="background:{bg};{border};border-radius:8px;padding:10px 14px;text-align:center;min-width:140px">
<div style="color:#f0883e;font-size:13px;font-weight:700">{rank_icon} {html_module.escape(s["name"])}</div>
<div style="color:#58a6ff;font-size:16px;font-weight:700;margin:4px 0">{s["score"]:.1f}分</div>
<div style="color:#8b949e;font-size:11px">成交 {s["total_amount"]:.2f}亿</div>
<div style="color:{'#f85149' if s['avg_5d']>0 else '#3fb950'};font-size:11px">5日 {sign_5d}{s["avg_5d"]:.2f}%</div>
<div style="color:{'#f85149' if s['avg_10d']>0 else '#3fb950'};font-size:11px">10日 {sign_10d}{s["avg_10d"]:.2f}%</div>
<div style="color:#8b949e;font-size:10px">{s["up_count"]}涨 {s["down_count"]}跌</div>
</div>"""
        main_line_html = f"""
<div style="text-align:center;margin-bottom:16px;padding:12px;background:rgba(88,166,255,.06);border-radius:10px;border:1px solid #1f2937">
<div style="color:#58a6ff;font-size:14px;font-weight:600;margin-bottom:8px">📊 主线板块排名（成交额40% + 五日涨幅30% + 十日涨幅30%）</div>
<div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">{ml_cells}</div>
</div>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>集合竞价 - 策略池OR筛选 · 频次排名</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#0d1117;color:#c9d1d9;padding:16px}}
.hd{{text-align:center;padding:16px 0}}
.hd h1{{font-size:20px;color:#58a6ff}}
.hd .t{{color:#8b949e;font-size:12px;margin-top:4px}}
.tbl-wrap{{overflow-x:auto;margin:0 auto;max-width:1200px}}
table{{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}}
th{{background:#161b22;color:#8b949e;font-weight:600;padding:8px 10px;text-align:center;border-bottom:2px solid #30363d;position:sticky;top:0}}
td{{padding:6px 10px;text-align:center;border-bottom:1px solid #21262d}}
tr:hover{{background:#161b22}}
.ft{{text-align:center;color:#484f58;font-size:10px;padding:20px 0}}
@media(max-width:768px){{table{{font-size:11px}}th,td{{padding:4px 6px}}}}
</style></head><body>
<div class="hd"><h1>📊 集合竞价 - 策略池OR筛选 · 频次排名TOP10</h1><div class="t">更新时间: {now}</div></div>
{idx_html}
{main_line_html}
{leader_html}
{sector_html}
<div class="tbl-wrap">
<table>
<thead><tr>
<th>代码</th><th>名称</th><th>板块(涨停数)</th><th>主线</th><th>龙头</th><th>09:25</th><th>09:26</th><th>搓合量</th>
<th>竞昨比</th><th>剩余率</th><th>09:26涨幅</th>
<th>筹码判断</th><th>频次</th><th>尾段判定</th><th>策略池</th><th>策略</th>
<th>DIF</th><th>DEA</th><th>BAR</th><th>趋势</th><th>信号</th>
</tr></thead>
<tbody>{rows}</tbody>
</table></div>
<div class="ft">⚠️ 仅供学习参考，不构成投资建议</div>
</body></html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.abspath(path)


def run_from_data_dir(data_dir: str, html_path: str = None, quiet: bool = False) -> list[dict]:
    """
    从 NYLO 抓取的 JSON 数据目录加载数据并执行分析（无需联网）
    data_dir: NYLO 输出目录，包含 quotes_*.json, klines/, details_*.json, sectors_*.json 等
    """
    import glob as _glob

    if not os.path.isdir(data_dir):
        print(f"❌ 数据目录不存在: {data_dir}")
        return []

    print(f"📂 从本地数据加载: {os.path.abspath(data_dir)}")

    # ---- 1. 加载行情数据 ----
    quote_files = _glob.glob(os.path.join(data_dir, "quotes_*.json"))
    if not quote_files:
        print("❌ 未找到行情数据 (quotes_*.json)")
        return []
    quote_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)  # 最新文件优先
    quotes = _load_json(quote_files[0])
    print(f"  ✅ 行情: {len(quotes)} 只 (文件: {os.path.basename(quote_files[0])})")

    # ---- 2. 加载股票详情（市值/量比/换手率）----
    detail_files = _glob.glob(os.path.join(data_dir, "details_*.json"))
    details = {}
    if detail_files:
        detail_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        details = _load_json(detail_files[0])
        print(f"  ✅ 详情: {len(details)} 只")

    # ---- 3. 加载板块数据 ----
    sector_files = _glob.glob(os.path.join(data_dir, "sectors_*.json"))
    sectors = {}
    if sector_files:
        sector_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        sectors = _load_json(sector_files[0])
        print(f"  ✅ 板块: {len(sectors)} 个")

    # ---- 4. 加载大盘指数 ----
    index_files = _glob.glob(os.path.join(data_dir, "indices_*.json"))
    indices = []
    if index_files:
        index_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        indices = _load_json(index_files[0])
        for idx in indices:
            sign = "+" if idx["change_pct"] > 0 else ""
            print(f"  📈 {idx['name']}: {idx['price']:.2f} ({sign}{idx['change_pct']:.2f}%)")

    # ---- 5. 加载K线数据 ----
    kline_dir = os.path.join(data_dir, "klines")
    kline_map = {}
    if os.path.isdir(kline_dir):
        for kf in _glob.glob(os.path.join(kline_dir, "*.json")):
            code = os.path.splitext(os.path.basename(kf))[0]
            kline_map[code] = _load_json(kf)
        print(f"  ✅ K线: {len(kline_map)} 只")

    # ---- 6. 加载120分钟K线 ----
    kline120_dir = os.path.join(data_dir, "klines_120min")
    kline120_map = {}
    if os.path.isdir(kline120_dir):
        for kf in _glob.glob(os.path.join(kline120_dir, "*.json")):
            code = os.path.splitext(os.path.basename(kf))[0]
            kline120_map[code] = _load_json(kf)
        print(f"  ✅ 120分钟K线: {len(kline120_map)} 只")

    # 板块K线（目前未提供，默认空）
    sector_klines = {}

    # ---- 6a. 主线板块排名（基于股票数据聚合）----
    main_line_sectors = []
    if sectors:
        print(f"\n📊 [5.5/6] 排名主线板块...")
        try:
            main_line_sectors = rank_main_line_sectors(
                sectors, quotes=quotes, details=details,
                kline_map=kline_map, top_n=10)
            if main_line_sectors:
                print(f"\n{'─'*110}")
                print(f"  {'排名':>4}  {'板块':<12} {'综合分':>6}  {'成交额(亿)':>10}  {'5日涨幅':>8}  {'10日涨幅':>9}  {'今日涨幅':>8}  {'上涨':>4}  {'下跌':>4}  {'个股数':>4}")
                print(f"{'─'*110}")
                for s in main_line_sectors:
                    sign_5d = "+" if s["avg_5d"] > 0 else ""
                    sign_10d = "+" if s["avg_10d"] > 0 else ""
                    sign_today = "+" if s["avg_today"] > 0 else ""
                    rank_mark = "🥇" if s["rank"] == 1 else ("🥈" if s["rank"] == 2 else ("🥉" if s["rank"] == 3 else f" {s['rank']}"))
                    print(f"  {rank_mark:>4}  {s['name']:<12} {s['score']:>6.1f}  {s['total_amount']:>10.2f}  {sign_5d}{s['avg_5d']:>6.2f}%  {sign_10d}{s['avg_10d']:>7.2f}%  {sign_today}{s['avg_today']:>6.2f}%  {s['up_count']:>4}  {s['down_count']:>4}  {s['stock_count']:>4}")
                print(f"{'─'*110}")
                print(f"  📌 主线板块 #1: {main_line_sectors[0]['name']} (综合分 {main_line_sectors[0]['score']})")
        except Exception as e:
            print(f"    ⚠️ 主线板块排名失败: {e}")

    # ---- 7a. 直接分析模式（当quotes中股票数≤20时，直接分析每只）----
    if len(quotes) <= 20:
        print(f"\n📊 直接分析模式 ({len(quotes)} 只股票)...")
        direct_results = []
        for code, q in quotes.items():
            if not code or len(code) != 6:
                continue
            klines = kline_map.get(code, [])
            # 补充K线amount
            for k in klines:
                if "amount" not in k or float(k.get("amount", 0)) <= 0:
                    vol = float(k.get("volume", 0))
                    close = float(k.get("close", 0))
                    k["amount"] = str(vol * 100 * close) if vol > 0 and close > 0 else "0"

            r = analyze(code, q, klines[-10:] if klines else [])
            if r:
                # 补充技术指标
                if klines and len(klines) >= 60:
                    try:
                        closes = [float(k.get("close", 0)) for k in klines]
                        ema12 = _ema(closes, 12)
                        ema26 = _ema(closes, 26)
                        dif = [a - b for a, b in zip(ema12, ema26)]
                        dea = _ema(dif, 9)
                        macd_bar = [(d - e) * 2 for d, e in zip(dif, dea)]
                        r.macd_dif = round(dif[-1], 3)
                        r.macd_dea = round(dea[-1], 3)
                        r.macd_bar = round(macd_bar[-1], 3)
                        r.macd_trend = "↑" if len(macd_bar) >= 2 and macd_bar[-1] > macd_bar[-2] else "↓"
                    except Exception:
                        r.macd_dif = r.macd_dea = r.macd_bar = 0
                        r.macd_trend = "-"
                else:
                    r.macd_dif = r.macd_dea = r.macd_bar = 0
                    r.macd_trend = "-"
                direct_results.append(r)

        if direct_results:
            if not quiet:
                for r in direct_results:
                    print_result(r)
                    # 额外输出技术指标
                    print(f"    DIF={r.macd_dif:.3f} DEA={r.macd_dea:.3f} BAR={r.macd_bar:.3f} 趋势={r.macd_trend}")

            if html_path:
                screen_dicts = [_auction_result_to_screen_dict(r) for r in direct_results]
                path = save_screen_html(screen_dicts, [], html_path)
                print(f"\n✅ 分析报告: {path}")

            # 转为dict列表返回
            return [{"code": r.code, "name": r.name, "verdict": r.verdict,
                     "bull_score": r.bull_score, "bear_score": r.bear_score} for r in direct_results]
        else:
            print("  ⚠ 无有效分析结果")
            return []

    # ---- 7b. 筛选模式（从板块数据中提取主板+非ST+涨幅>1%）----
    all_candidates = []
    seen_codes = set()

    for sname, sdata in sectors.items():
        limit_cnt = sdata.get("limit_up", 0)
        stocks = sdata.get("stocks", [])
        for item in stocks:
            code = str(item.get("code", ""))
            name = str(item.get("name", ""))
            if not code or len(code) != 6 or not code[0].isdigit():
                continue
            if not code.startswith(("60", "00")):
                continue
            if "ST" in name.upper():
                continue
            if code in seen_codes:
                continue

            q = quotes.get(code, {})
            price = q.get("price", 0) or float(item.get("price", 0))
            prev_close = q.get("prev_close", 0)
            if not prev_close or prev_close <= 0:
                continue
            if price <= 0 or price >= 60:
                continue

            auction_gain = (price - prev_close) / prev_close * 100
            if auction_gain <= 1:
                continue

            seen_codes.add(code)
            em = details.get(code, {})
            all_candidates.append({
                "code": code, "name": name or q.get("name", ""),
                "symbol": ("sh" if code.startswith(("6", "9")) else "sz") + code,
                "price": price, "prev_close": prev_close,
                "auction_gain": round(auction_gain, 2),
                "open_price": q.get("open", price),
                "volume_shares": q.get("volume", 0) * 100 if q.get("volume", 0) else 0,
                "volume": q.get("volume", 0),
                "market_cap_yi": em.get("market_cap_yi", 0) or q.get("market_cap_yi", 0),
                "turnover_sina": q.get("turnover", 0),
                "sector": sname,
                "sector_code": sdata.get("code", ""),
                "sector_limit_count": limit_cnt,
            })

    print(f"\n📊 候选股票: {len(all_candidates)} 只")

    if not all_candidates:
        print("❌ 无候选股票")
        return []

    # ---- 8. 执行策略筛选 ----
    # 构建 tencent_map (模拟腾讯行情数据)
    tencent_map = {}
    for code in seen_codes:
        q = quotes.get(code, {})
        em = details.get(code, {})
        tencent_map[code] = {
            "volume": q.get("volume", 0),
            "buy_vol": q.get("buy_vol", 0),
            "sell_vol": q.get("sell_vol", 0),
            "bid1_v": q.get("bid1_v", 0),    # 竞价买一量
            "ask1_v": q.get("ask1_v", 0),    # 竞价卖一量
            "amount": q.get("amount", 0),
            "turnover": q.get("turnover", 0),
            "amplitude": q.get("amplitude", 0),
            "market_cap_yi": q.get("market_cap_yi", 0) or em.get("market_cap_yi", 0),
            "volume_ratio_api": q.get("volume_ratio_api", 0) or em.get("volume_ratio", 0),
        }

    # 补充K线amount字段
    for code, klines in kline_map.items():
        for k in klines:
            if "amount" not in k or float(k.get("amount", 0)) <= 0:
                vol = float(k.get("volume", 0))
                close = float(k.get("close", 0))
                if vol > 0 and close > 0:
                    k["amount"] = str(vol * 100 * close)
                else:
                    k["amount"] = "0"

    # 补充昨日成交量
    for c in all_candidates:
        klines = kline_map.get(c["code"], [])
        if klines and len(klines) >= 2:
            try:
                yesterday_vol = int(float(klines[-2].get("volume", 0)))
                if yesterday_vol > 0:
                    c["volume_shares"] = yesterday_vol
            except (ValueError, TypeError):
                pass

    # 统一策略筛选
    print("📊 执行统一策略筛选...")
    filtered = _screen_unified(all_candidates, tencent_map, em_data=details, kline_map=kline_map)
    print(f"  通过筛选: {len(filtered)} 只")

    if not filtered:
        return []

    # ---- 9. 策略池OR逻辑检查（任一策略池通过即可入选初选池）----
    print("📊 执行策略池OR逻辑检查（任一池通过即入选）...")
    final = []
    for c in filtered:
        code = c["code"]
        klines = kline_map.get(code, [])

        # 修正昨收价
        if klines and len(klines) >= 2:
            try:
                real_prev_close = float(klines[-2].get("close", 0))
                if real_prev_close > 0:
                    c["prev_close"] = real_prev_close
                    c["auction_gain"] = round((c["price"] - real_prev_close) / real_prev_close * 100, 2)
            except (ValueError, TypeError):
                pass

        # 基础池(K线部分) —— 基本面过滤，非策略池
        ok_base, reason_base = pool_base_post_kline(code, c["price"], klines,
                                                     c.get("prev_close", 0),
                                                     c.get("yesterday_amount", 0))
        if not ok_base:
            continue

        tc = tencent_map.get(code, {})
        em = details.get(code, {})

        # 量价池
        ok_vp, reason_vp = pool_volume_price(c, tc, em, klines=klines)
        # 趋势池
        ok_trend, reason_trend = pool_trend(c, klines, tc=tc, em=em)
        # 技术池
        ok_tech, reason_tech = pool_technical(code, klines, name=c.get("name", ""),
                                              price=c["price"], market_cap_yi=c.get("market_cap_yi", 0),
                                              sector=c.get("sector", ""), sector_code=c.get("sector_code", ""),
                                              kline120_map=kline120_map, sector_klines=sector_klines)

        passed_pools = []
        if ok_vp:
            passed_pools.append("量价池")
        if ok_trend:
            passed_pools.append("趋势池")
        if ok_tech:
            passed_pools.append("技术池")

        # OR逻辑：任一策略池通过即入选初选池
        if not passed_pools:
            continue

        c["passed_pools"] = passed_pools
        c["change_pct"] = c["auction_gain"]
        c["tech_pool_pass"] = ok_tech
        c["tech_pool_reason"] = reason_tech if not ok_tech else ""
        c["trend_pool_pass"] = ok_trend
        c["vp_pool_pass"] = ok_vp

        # MACD技术指标
        if klines and len(klines) >= 60:
            try:
                closes = [float(k.get("close", 0)) for k in klines]
                ema12 = _ema(closes, 12)
                ema26 = _ema(closes, 26)
                dif = [a - b for a, b in zip(ema12, ema26)]
                dea = _ema(dif, 9)
                macd_bar = [(d - e) * 2 for d, e in zip(dif, dea)]
                c["macd_dif"] = round(dif[-1], 3) if dif else 0
                c["macd_dea"] = round(dea[-1], 3) if dea else 0
                c["macd_bar"] = round(macd_bar[-1], 3) if macd_bar else 0
                c["macd_trend"] = "↑" if len(macd_bar) >= 2 and macd_bar[-1] > macd_bar[-2] else "↓"

                # 2个月内涨停次数
                limit_up_count = 0
                for i in range(max(0, len(klines) - 42), len(klines)):
                    if i < 1:
                        continue
                    pc = float(klines[i-1].get("close", 0))
                    cc = float(klines[i].get("close", 0))
                    if pc > 0 and (cc - pc) / pc * 100 >= 9.5:
                        limit_up_count += 1
                c["limit_up_60d"] = limit_up_count
            except Exception:
                pass

        final.append(c)

    if not final:
        print("❌ 策略池过滤后无股票剩余")
        return []

    print(f"  初选池: {len(final)} 只（任一策略池通过）")

    # ---- 10. 抢筹/出货深度分析 + 频次统计 ----
    print("📊 执行抢筹/出货深度分析...")
    for c in final:
        code = c["code"]
        tc = tencent_map.get(code, {})
        em = details.get(code, {})
        klines = kline_map.get(code, [])

        c["auction_price"] = c["open_price"]
        c["price_0926"] = c["price"]
        # 涨幅 = 相对竞价额的变化（而非昨收）
        base_price = c["auction_price"] if c["auction_price"] > 0 else c["prev_close"]
        c["chg_0926"] = round((c["price_0926"] - base_price) / base_price * 100, 2) if base_price > 0 else 0

        auction_vol = c.get("auction_vol", c["volume"])
        yesterday_vol = c.get("yesterday_vol_hist", 0) or c.get("yesterday_vol", 1)
        c["comp_ratio"] = round(auction_vol / yesterday_vol * 100, 1) if yesterday_vol > 0 else 0

        buy_vol = tc.get("bid1_v", 0)    # 竞价买一量（尾段真实买盘）
        sell_vol = tc.get("ask1_v", 0)   # 竞价卖一量（尾段真实卖盘）
        c["remaining_rate"] = round(buy_vol / (buy_vol + sell_vol) * 100, 1) if (buy_vol + sell_vol) > 0 else 50.0

        chg = c["auction_gain"]
        vr = c.get("volume_ratio", 0)
        rr = c["remaining_rate"]
        cr = c.get("comp_ratio", 0)

        # ---- 频次分析：6维度信号统计（收紧阈值）----
        # 每个维度满足条件计1分，总分=频次
        freq_signals = []
        freq = 0

        # 1. 量比 ≥ 8 → 资金高度关注
        if vr >= 8:
            freq += 1
            freq_signals.append("量比≥8")
        # 2. 竞价涨幅 ≥ 7% → 极强势高开
        if chg >= 7:
            freq += 1
            freq_signals.append("涨幅≥7%")
        # 3. 剩余率 ≥ 70% → 买盘绝对主导
        if rr >= 70:
            freq += 1
            freq_signals.append("剩余率≥70%")
        # 4. 竞昨比 ≥ 5% → 显著资金放大
        if cr >= 5:
            freq += 1
            freq_signals.append("竞昨比≥5%")
        # 5. 板块涨停数 ≥ 5 → 板块强势
        if c.get("sector_limit_count", 0) >= 5:
            freq += 1
            freq_signals.append("板块涨停≥5")
        # 6. 通过多个策略池 → 多维共振
        if len(c.get("passed_pools", [])) >= 3:
            freq += 1
            freq_signals.append("多池共振")

        c["frequency"] = freq
        c["freq_signals"] = freq_signals

        # ---- 抢筹/出货综合判断（收紧评分体系）----
        # 取消涨停直通，涨停也需要评分验证
        score = 0
        # 涨幅维度（最高3分）
        if chg >= 9.5: score += 3       # 涨停强信号
        elif chg >= 7: score += 2
        elif chg >= 5: score += 1
        # 量比维度（最高3分）
        if vr >= 15: score += 3          # 超级放量
        elif vr >= 10: score += 2
        elif vr >= 8: score += 1
        # 剩余率维度（最高3分，最低-2分）
        if rr >= 75: score += 3
        elif rr >= 65: score += 2
        elif rr >= 55: score += 1
        elif rr < 35: score -= 2         # 强卖压
        elif rr < 45: score -= 1
        # 注意：无数据时rr默认50%，不加分也不扣分
        # 竞昨比维度（最高2分）
        if cr >= 10: score += 2
        elif cr >= 7: score += 1
        # 频次加权（最高3分）
        if freq >= 5: score += 3
        elif freq >= 4: score += 2
        elif freq >= 3: score += 1
        # 多池共振加权（最高2分）
        if len(c.get("passed_pools", [])) >= 4: score += 2
        elif len(c.get("passed_pools", [])) >= 3: score += 1
        # 涨停额外惩罚：涨停但其他信号弱 → 减分（防止一字板/缩量板误判）
        if chg >= 9.5:
            if vr < 5: score -= 3        # 涨停但缩量，疑似一字板
            if rr < 50: score -= 2       # 涨停但卖压重
            if freq < 2: score -= 2      # 涨停但其他维度弱

        c["verdict"] = "真实抢筹" if score >= 9 else ("疑似出货" if score <= 0 else "正常")

        # ---- 尾段竞价判定（新规则三维评分）----
        is_limit_up = chg >= 9.5
        ts_score, ts_verdict, ts_signals = tail_segment_verdict(
            gap=chg, vol=c.get("auction_vol", c.get("volume", 0)),
            buy_vol=buy_vol, sell_vol=sell_vol,
            is_limit_up=is_limit_up,
        )
        c["tail_score"] = ts_score
        c["tail_verdict"] = ts_verdict
        c["tail_signals"] = ts_signals

        c["strategy"] = _compute_screen_strategy(c)

    # ---- 11. 按频次排序，保留前10 ----
    final.sort(key=lambda x: (
        -x.get("frequency", 0),          # 频次最高排第一
        -x.get("auction_gain", 0),        # 同频次按涨幅
        -x.get("sector_limit_count", 0),  # 再按板块热度
    ))
    if len(final) > 10:
        final = final[:10]

    # ---- 12. 龙头股识别 ----
    # 在前10中，按板块分组，每个板块内频次最高者为龙头
    sector_groups = {}
    for c in final:
        sn = c.get("sector", "未知")
        sector_groups.setdefault(sn, []).append(c)

    leader_codes = set()
    leader_sector = ""      # 龙头所在板块
    leader_sector_count = 0 # 该板块在前10中的股票数

    for sn, stocks_in_sector in sector_groups.items():
        # 板块内频次最高 + 涨幅最大 = 龙头
        best = max(stocks_in_sector, key=lambda s: (
            s.get("frequency", 0),
            s.get("auction_gain", 0),
            s.get("volume_ratio", 0),
        ))
        best["is_leader"] = True
        best["leader_count"] = len(stocks_in_sector)  # 板块内入选数量
        leader_codes.add(best["code"])

        # 统计龙头板块（入选股票最多的板块）
        if len(stocks_in_sector) > leader_sector_count:
            leader_sector_count = len(stocks_in_sector)
            leader_sector = sn

    for c in final:
        if c["code"] not in leader_codes:
            c["is_leader"] = False
            c["leader_count"] = 0

    print(f"\n✅ 最终筛选: {len(final)} 只股票")
    if leader_sector:
        print(f"🏆 龙头板块: {leader_sector} ({leader_sector_count}只入选)")

    # 主线板块 #1 名称（用于标记）
    main_line_1_name = main_line_sectors[0]["name"] if main_line_sectors else ""

    # 输出结果
    if not quiet:
        print(f"\n{'─'*180}")
        print(f"  {'#':>3}  {'代码':<8} {'名称':<8} {'板块':<10} {'主线':>4} {'龙头':>4} {'09:25':>7} {'09:26':>7} {'搓合量':>8} {'竞昨比':>7} {'剩余率':>7} {'涨幅':>7} {'筹码':<6} {'频次':>4} {'尾段':>6} {'策略池':<12} {'策略':<14} {'DIF':>7} {'DEA':>7} {'BAR':>7} {'趋势':>4} {'信号':<20}")
        print(f"{'─'*180}")

        for i, s in enumerate(final, 1):
            vol_fmt = _format_volume(s.get("auction_vol", s.get("volume", 0)))
            chg_0926 = s.get("chg_0926", 0)
            comp_ratio = s.get("comp_ratio", 0)
            remaining = s.get("remaining_rate", 50)
            verdict = s.get("verdict", "正常")
            freq = s.get("frequency", 0)
            strategy = s.get("strategy", "观望")
            sector = s.get("sector", "-")[:8]
            is_leader = s.get("is_leader", False)
            leader_count = s.get("leader_count", 0)
            leader_mark = f"🏆{leader_count}" if is_leader else f"  {leader_count}"
            # 主线板块标记
            is_main_line = main_line_1_name and s.get("sector", "") == main_line_1_name
            main_mark = "🔥" if is_main_line else "  "
            dif = s.get("macd_dif", 0)
            dea = s.get("macd_dea", 0)
            bar = s.get("macd_bar", 0)
            trend = s.get("macd_trend", "-")
            pools = "/".join(s.get("passed_pools", []))
            freq_sigs = ",".join(s.get("freq_signals", []))[:18]
            # 尾段判定
            ts_verdict = s.get("tail_verdict", "-")
            ts_score = s.get("tail_score", 0)
            ts_icon = "🟢" if ts_verdict == "看多" else ("🔴" if ts_verdict == "不看多" else "⚪")
            tail_mark = f"{ts_icon}{ts_score:>2}"
            print(f"  {i:>3}  {s['code']:<8} {s['name']:<8} {sector:<10} {main_mark:>4} {leader_mark:>4} {s.get('auction_price', s['price']):>7.2f} {s.get('price_0926', s['price']):>7.2f} {vol_fmt:>8} {comp_ratio:>6.1f}% {remaining:>6.1f}% {chg_0926:>+6.2f}% {verdict:<6} {freq:>4} {tail_mark:>6} {pools:<12} {strategy:<14} {dif:>7.3f} {dea:>7.3f} {bar:>7.3f} {trend:>4} {freq_sigs:<20}")

        print(f"{'─'*170}")

    # 保存HTML
    if html_path:
        path = save_screen_html(final, indices, html_path, main_line_sectors=main_line_sectors)
        print(f"\n✅ 筛选报告: {path}")

    return final



# ════════════════════════════════════════════════════
# run.py — 一键运行（抓取+分析）
# ════════════════════════════════════════════════════

def run_oneclick():
    inputs = sys.argv[1:]
    session = _build_session()

    # ---- 解析输入：代码或名称 ----
    codes = []
    if inputs:
        for inp in inputs:
            inp = inp.strip()
            if inp.isdigit() and len(inp) == 6:
                codes.append(inp)
            else:
                # 名称搜索
                result = search_stock(inp, session)
                if result:
                    codes.append(result["code"])
                    print(f"  🔍 {inp} → {result['code']} {result['name']}")
                else:
                    print(f"  ⚠ 未找到: {inp}")
        if not codes:
            print("❌ 没有有效股票代码")
            return

    # ---- 临时目录存放抓取数据 ----
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_nylo_cache")
    os.makedirs(data_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"\n{'═'*50}")
    print(f"  NYLO — A股数据抓取 + 分析（纯HTTP模式）")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*50}\n")

    try:
        # ---- 1. 大盘指数 ----
        print("📊 获取大盘...")
        indices = fetch_indices(session)
        for idx in indices:
            sign = "+" if idx["change_pct"] > 0 else ""
            print(f"  {idx['name']}: {idx['price']:.2f} ({sign}{idx['change_pct']:.2f}%)")
        _save_json(indices, f"{data_dir}/indices_{timestamp}.json")

        # ---- 2. 行情 ----
        if codes:
            print(f"\n📊 获取行情 ({len(codes)} 只)...")
            quotes = fetch_quotes_batch(codes, session)
        else:
            print("\n📊 获取板块数据...")
            sectors = fetch_sectors(session)
            _save_json(sectors, f"{data_dir}/sectors_{timestamp}.json")

            all_codes = set()
            for sdata in sectors.values():
                for item in sdata.get("stocks", []):
                    c = str(item.get("code", ""))
                    if c and len(c) == 6 and c.startswith(("60", "00")):
                        all_codes.add(c)
            codes = list(all_codes)
            print(f"  共 {len(codes)} 只主板股票")

            print(f"\n📊 获取行情 ({len(codes)} 只)...")
            quotes = fetch_quotes_batch(codes, session)

        print(f"  ✅ 行情: {len(quotes)} 只")
        _save_json(quotes, f"{data_dir}/quotes_{timestamp}.json")

        # ---- 3. 详情（东财批量接口）----
        print(f"\n📊 获取详情...")
        details = fetch_stock_details(codes, session, quotes_ref=quotes)
        print(f"  ✅ 详情: {len(details)} 只")
        _save_json(details, f"{data_dir}/details_{timestamp}.json")

        # ---- 4. 板块（如果还没拿）----
        sector_files = [f for f in os.listdir(data_dir) if f.startswith("sectors_")]
        if not sector_files:
            print(f"\n📊 获取板块...")
            sectors = fetch_sectors(session)
            _save_json(sectors, f"{data_dir}/sectors_{timestamp}.json")

        # ---- 5. K线（并行抓取日K线+120minK线）----
        print(f"\n📊 并行获取K线 ({len(codes)} 只)...")
        klines, klines_120 = fetch_all_klines_async(codes, days=1000, concurrency=100)
        klines = supplement_kline_amount(klines, quotes)
        kline_dir = f"{data_dir}/klines"
        os.makedirs(kline_dir, exist_ok=True)
        for code, data in klines.items():
            _save_json(data, f"{kline_dir}/{code}.json")
        print(f"  ✅ 日K线: {len(klines)} 只")

        kline120_dir = f"{data_dir}/klines_120min"
        os.makedirs(kline120_dir, exist_ok=True)
        for code, data in klines_120.items():
            _save_json(data, f"{kline120_dir}/{code}.json")
        print(f"  ✅ 120minK线: {len(klines_120)} 只")

        # ---- 6. 运行分析 ----
        html_out = f"{data_dir}/report.html"
        print(f"\n{'═'*50}")
        print(f"  🚀 开始分析...")
        print(f"{'═'*50}\n")

        run_from_data_dir(data_dir, html_path=html_out)

        print(f"\n📄 报告: {html_out}")

    except KeyboardInterrupt:
        print(f"\n⚠ 用户中断")


# ════════════════════════════════════════════════════
# 统一入口 main()
# ════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="NY3.0 — A股数据抓取 + 筹码分析一体化工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 NY3.0.py                                    # 全市场抓取+分析（一键模式）
  python3 NY3.0.py 600519 000858                      # 分析指定股票
  python3 NY3.0.py 茅台 五粮液                         # 用名称搜索并分析
  python3 NY3.0.py --codes 600519 000858              # 只抓指定股票数据
  python3 NY3.0.py --search 茅台                      # 搜索股票代码
  python3 NY3.0.py --test                             # 测试接口连通性
  python3 NY3.0.py --data-dir stock_data              # 从本地数据离线分析
        """,
    )
    parser.add_argument("inputs", nargs="*", help="股票代码或名称（一键模式：抓取+分析）")
    parser.add_argument("--codes", nargs="*", help="指定股票代码（只抓数据）")
    parser.add_argument("--search", "-s", help="搜索股票名称/代码")
    parser.add_argument("--test", "-t", action="store_true", help="测试接口连通性")
    parser.add_argument("--output", "-o", default="stock_data", help="输出目录")
    parser.add_argument("--data-dir", "-d", help="从NYLO数据目录离线分析（无需联网）")
    parser.add_argument("--html", default=None, help="HTML报告保存路径")
    parser.add_argument("--analyze", "-a", action="store_true", help="抓取完成后自动运行分析")
    parser.add_argument("--quiet", "-q", action="store_true", help="静默模式")
    parser.add_argument("--screen", action="store_true", default=True, help="主板筛选策略模式（默认开启）")
    parser.add_argument("--no-screen", dest="screen", action="store_false", help="关闭主板筛选")
    parser.add_argument("--close-auction", nargs="*", metavar="CODE",
                        help="收盘集合竞价监控（14:57-15:00），指定代码或留空自动取筛选结果")

    args = parser.parse_args()

    # ---- 测试模式 ----
    if args.test:
        test_connectivity()
        return

    # ---- 搜索模式 ----
    if args.search:
        session = _build_session()
        result = search_stock(args.search, session)
        if result:
            print(f"  ✅ {args.search} → {result['code']} ({result['name']}) [{result['market']}]")
        else:
            print(f"  ❌ 未找到: {args.search}")
        return

    # ---- 收盘竞价监控模式（命令行指定代码）----
    if args.close_auction is not None:
        codes = args.close_auction
        if not codes:
            print("  ⚠️ 请用 --close-auction 600519 000858 指定股票代码")
            return
        html_out = args.html or "close_auction_report.html"
        close_auction_monitor(codes, html_path=html_out)
        return

    # ---- 交互菜单模式（无参数时显示）----
    if not args.inputs and not args.data_dir and not args.search and not args.test and not args.codes:
        print(f"\n{'═'*40}")
        print(f"  📊 NY3.0 集合竞价分析")
        print(f"{'═'*40}")
        print(f"  1. 早盘集合竞价分析（09:15-09:25）")
        print(f"  2. 尾盘集合竞价分析（14:57-15:00）")
        print(f"{'═'*40}")
        choice = input("  请选择 (1/2): ").strip()

        if choice == "1":
            run_oneclick()
            return
        elif choice == "2":
            raw = input("  请输入股票代码（空格分隔，如 600519 000858）: ").strip()
            if not raw:
                print("  ❌ 未输入代码，退出")
                return
            codes = raw.split()
            html_out = args.html or "close_auction_report.html"
            close_auction_monitor(codes, html_path=html_out)
            return
        else:
            print("  ❌ 无效选择，退出")
            return

    # ---- 离线分析模式 ----
    if args.data_dir:
        html_path = args.html or "auction_report.html"
        stocks = run_from_data_dir(args.data_dir, html_path=html_path, quiet=args.quiet)
        if not stocks:
            print("\n❌ 未找到符合条件的股票")
            sys.exit(1)
        return

    # ---- 一键模式（有位置参数：代码或名称）----
    if args.inputs:
        run_oneclick()
        return

    # ---- 纯抓取模式（--codes 或 全市场）----
    try:
        scrape_all(output_dir=args.output, target_codes=args.codes)
    except Exception as e:
        print(f"\n⚠ 抓取出错: {e}")
        print("  继续尝试分析已有数据...\n")

    # 无论抓取是否完全成功，都尝试分析已有数据
    html_out = args.html or os.path.join(args.output, "screen_report.html")
    print(f"\n{'═'*60}")
    print(f"  🚀 自动运行分析...")
    print(f"{'═'*60}\n")
    run_from_data_dir(args.output, html_path=html_out)


if __name__ == "__main__":
    main()
