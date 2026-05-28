"""
ths_scraper.py — 同花顺 Selenium + 并发requests 混合爬虫
Selenium 只用于初始化会话/反爬，实际数据用并发requests抓取
"""

import time
import random
import re
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ════════════════════════════════════════════════════
# 浏览器初始化（仅用于获取cookie绕过反爬）
# ════════════════════════════════════════════════════

_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
]


def init_driver():
    """初始化 Selenium Chrome 无头浏览器"""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--user-agent={random.choice(_UAS)}")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    import shutil, os
    for p in ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
              "C:/Program Files/Google/Chrome/Application/chrome.exe",
              "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"]:
        if shutil.which(p) or os.path.exists(p):
            opts.binary_location = p
            break

    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    })
    driver.set_page_load_timeout(20)
    return driver


def _build_session(driver=None):
    """构建 requests Session，从 Selenium 复制 cookie"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(_UAS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Referer": "https://www.10jqka.com.cn/",
    })
    if driver:
        for c in driver.get_cookies():
            s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    return s


def _delay(lo=0.1, hi=0.3):
    time.sleep(random.uniform(lo, hi))


def _safe_float(s):
    try:
        return float(str(s).replace("%", "").replace(",", "").replace(" ", "").replace("万", ""))
    except (ValueError, TypeError):
        return 0.0


def _parse_volume(s):
    s = str(s).strip()
    try:
        if "万" in s:
            return int(_safe_float(s.replace("万", "")) * 10000)
        return int(_safe_float(s))
    except (ValueError, TypeError):
        return 0


def close_driver(driver):
    try:
        driver.quit()
    except Exception:
        pass


# ════════════════════════════════════════════════════
# 1. 全A股代码列表
# ════════════════════════════════════════════════════

def fetch_all_stock_codes(driver) -> list[str]:
    """从同花顺行情页分页抓取沪深主板代码"""
    session = _build_session(driver)
    codes = []
    page = 1

    while page <= 80:
        url = f"https://q.10jqka.com.cn/index/index/board/all/field/199112/order/desc/page/{page}/"
        try:
            r = session.get(url, timeout=15, headers={
                "Referer": "https://q.10jqka.com.cn/",
                "X-Requested-With": "XMLHttpRequest",
            })
            r.encoding = "utf-8"

            # 解析股票代码
            found = re.findall(r'<a[^>]*href="[^"]*[/](\d{6})[/"]', r.text)
            new_codes = [c for c in found if c.startswith(("60", "00"))]

            if not new_codes:
                # 尝试从表格解析
                for m in re.finditer(r'<td[^>]*>\s*<a[^>]*>(\d{6})</a>', r.text):
                    c = m.group(1)
                    if c.startswith(("60", "00")):
                        new_codes.append(c)

            if not new_codes:
                break

            codes.extend(new_codes)
            page += 1
            _delay(0.1, 0.3)

        except Exception as e:
            print(f"    ⚠ 第{page}页异常: {e}")
            break

    return list(set(codes))


# ════════════════════════════════════════════════════
# 2. 实时行情（并发抓取行情页）
# ════════════════════════════════════════════════════

def fetch_quotes_batch(driver, codes: list[str]) -> dict:
    """从同花顺行情页批量抓取实时行情（并发）"""
    session = _build_session(driver)
    results = {}
    page = 1
    target_set = set(codes)

    while page <= 80 and len(results) < len(codes):
        url = f"https://q.10jqka.com.cn/index/index/board/all/field/199112/order/desc/page/{page}/"
        try:
            r = session.get(url, timeout=15, headers={
                "Referer": "https://q.10jqka.com.cn/",
            })
            r.encoding = "utf-8"
            html = r.text

            # 解析表格行
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
            found_any = False
            for row in rows:
                tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                if len(tds) < 6:
                    continue

                # 提取代码
                code_m = re.search(r'(\d{6})', tds[1])
                if not code_m:
                    continue
                code = code_m.group(1)
                if code not in target_set:
                    continue

                # 提取各字段
                name = re.sub(r'<[^>]+>', '', tds[2]).strip()
                price = re.sub(r'<[^>]+>', '', tds[3]).strip()
                change_pct = re.sub(r'<[^>]+>', '', tds[4]).strip()

                if not price or price in ("—", "-", ""):
                    continue

                # 成交量在第8列（索引7）
                volume = re.sub(r'<[^>]+>', '', tds[7]).strip() if len(tds) > 7 else "0"

                results[code] = {
                    "name": name,
                    "code": code,
                    "price": _safe_float(price),
                    "prev_close": 0,
                    "open": 0,
                    "volume": _parse_volume(volume),
                    "buy_vol": 0, "sell_vol": 0,
                    "bid1_p": 0, "bid1_v": 0,
                    "ask1_p": 0, "ask1_v": 0,
                    "change_pct": _safe_float(change_pct),
                    "high": 0, "low": 0,
                    "amount": 0, "turnover": 0,
                    "amplitude": 0,
                    "market_cap_yi": 0,
                    "volume_ratio_api": 0,
                    "quote_time": "",
                }
                found_any = True

            if not found_any and page > 1:
                break

            page += 1
            _delay(0.1, 0.3)

        except Exception as e:
            print(f"    ⚠ 行情第{page}页异常: {e}")
            break

    return results


# ════════════════════════════════════════════════════
# 3. 股票详情（并发抓取）
# ════════════════════════════════════════════════════

def fetch_stock_details(driver, codes: list[str]) -> dict:
    """并发抓取个股详情页（市值/量比/换手率）"""
    session = _build_session(driver)
    results = {}

    def _fetch_one(code):
        url = f"https://stockpage.10jqka.com.cn/{code}/"
        try:
            r = session.get(url, timeout=12)
            r.encoding = "utf-8"
            text = r.text
            detail = {"code": code}

            # 流通市值
            m = re.search(r'流通市值[：:\s]*<[^>]*>([\d.]+)</[^>]*>\s*亿', text)
            if not m:
                m = re.search(r'流通市值[：:\s]*([\d.]+)\s*亿', text)
            if m:
                detail["market_cap_yi"] = _safe_float(m.group(1))

            # 量比
            m = re.search(r'量比[：:\s]*<[^>]*>([\d.]+)</[^>]*>', text)
            if not m:
                m = re.search(r'量比[：:\s]*([\d.]+)', text)
            if m:
                detail["volume_ratio_api"] = _safe_float(m.group(1))

            # 换手率
            m = re.search(r'换手率[：:\s]*<[^>]*>([\d.]+)%?</[^>]*>', text)
            if not m:
                m = re.search(r'换手率[：:\s]*([\d.]+)%?', text)
            if m:
                detail["turnover"] = _safe_float(m.group(1))

            if len(detail) > 1:
                return code, detail
        except Exception:
            pass
        return code, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_one, c): c for c in codes}
        done = 0
        for f in as_completed(futures):
            code, detail = f.result()
            if detail:
                results[code] = detail
            done += 1
            if done % 200 == 0:
                print(f"    进度: {done}/{len(codes)}")

    return results


# ════════════════════════════════════════════════════
# 4. 行业板块 + 成分股
# ════════════════════════════════════════════════════

def fetch_sectors(driver) -> dict:
    """从同花顺行业板块页抓取板块列表及成分股"""
    session = _build_session(driver)
    sectors = {}
    page = 1

    while page <= 10:
        url = f"https://q.10jqka.com.cn/thshy/field/199112/order/desc/page/{page}/"
        try:
            r = session.get(url, timeout=15, headers={"Referer": "https://q.10jqka.com.cn/thshy/"})
            r.encoding = "utf-8"
            html = r.text

            # 提取板块链接
            sector_links = re.findall(
                r'<a[^>]*href="(https?://q\.10jqka\.com\.cn/thshy/detail/code/(\d+)/)"[^>]*>([^<]+)</a>',
                html
            )
            if not sector_links:
                break

            # 提取涨跌幅
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

            for href, scode, sname in sector_links:
                sname = sname.strip()
                if not sname:
                    continue

                # 从板块列表页提取涨跌幅
                chg = 0
                for row in rows:
                    if scode in row:
                        tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                        if len(tds) >= 4:
                            chg = _safe_float(re.sub(r'<[^>]+>', '', tds[3]))
                        break

                # 抓取成分股
                stocks = _fetch_sector_stocks(session, href)
                sectors[sname] = {
                    "code": scode,
                    "limit_up": 0,
                    "limit_down": 0,
                    "change_pct": chg,
                    "stocks": stocks,
                }
                print(f"    ✓ {sname}: {len(stocks)} 只")
                _delay(0.1, 0.3)

            page += 1
            _delay(0.1, 0.3)

        except Exception as e:
            print(f"    ⚠ 板块第{page}页异常: {e}")
            break

    return sectors


def _fetch_sector_stocks(session, url):
    """抓取单个板块的成分股列表"""
    stocks = []
    try:
        r = session.get(url, timeout=12, headers={"Referer": "https://q.10jqka.com.cn/thshy/"})
        r.encoding = "utf-8"

        # 解析成分股表格
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL)
        for row in rows:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(tds) >= 3:
                code_m = re.search(r'(\d{6})', tds[1])
                if code_m:
                    code = code_m.group(1)
                    name = re.sub(r'<[^>]+>', '', tds[2]).strip()
                    stocks.append({"code": code, "name": name})
    except Exception:
        pass
    return stocks


# ════════════════════════════════════════════════════
# 5. K线数据（并发抓取）
# ════════════════════════════════════════════════════

def fetch_kline_batch(driver, codes: list[str], days: int = 1000) -> dict:
    """并发抓取日K线数据"""
    session = _build_session(driver)
    kline_map = {}

    def _fetch_one(code):
        url = f"https://stockpage.10jqka.com.cn/{code}/"
        try:
            r = session.get(url, timeout=12)
            r.encoding = "utf-8"
            html = r.text

            # 方法1: 从页面JS变量提取K线数据
            m = re.search(r'var\s+klineData\s*=\s*(\[.*?\]);', html, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    klines = []
                    for item in data:
                        if isinstance(item, dict):
                            klines.append({
                                "day": item.get("date", item.get("day", "")),
                                "open": str(item.get("open", 0)),
                                "close": str(item.get("close", 0)),
                                "high": str(item.get("high", 0)),
                                "low": str(item.get("low", 0)),
                                "volume": str(item.get("volume", 0)),
                            })
                        elif isinstance(item, (list, tuple)) and len(item) >= 6:
                            klines.append({
                                "day": str(item[0]),
                                "open": str(item[1]),
                                "close": str(item[2]),
                                "high": str(item[3]),
                                "low": str(item[4]),
                                "volume": str(item[5]),
                            })
                    if klines:
                        return code, klines[-days:]
                except json.JSONDecodeError:
                    pass

            # 方法2: 从页面提取历史行情表格
            klines = []
            table_m = re.search(r'class="histroyTrade"[^>]*>(.*?)</table>', html, re.DOTALL)
            if table_m:
                rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_m.group(1), re.DOTALL)
                for row in rows:
                    tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                    if len(tds) >= 6:
                        klines.append({
                            "day": re.sub(r'<[^>]+>', '', tds[0]).strip(),
                            "open": re.sub(r'<[^>]+>', '', tds[1]).strip(),
                            "close": re.sub(r'<[^>]+>', '', tds[2]).strip(),
                            "high": re.sub(r'<[^>]+>', '', tds[3]).strip(),
                            "low": re.sub(r'<[^>]+>', '', tds[4]).strip(),
                            "volume": re.sub(r'<[^>]+>', '', tds[5]).strip(),
                        })
            if klines:
                return code, klines[-days:]

        except Exception:
            pass
        return code, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_one, c): c for c in codes}
        done = 0
        for f in as_completed(futures):
            code, klines = f.result()
            if klines:
                kline_map[code] = klines
            done += 1
            if done % 200 == 0:
                print(f"    进度: {done}/{len(codes)} ({len(kline_map)} 有数据)")

    return kline_map


def fetch_kline_120min(driver, code: str, count: int = 60) -> list[dict]:
    """抓取120分钟K线"""
    session = _build_session(driver)
    url = f"https://stockpage.10jqka.com.cn/{code}/"
    try:
        r = session.get(url, timeout=10)
        r.encoding = "utf-8"

        # 尝试从JS提取分时数据
        m = re.search(r'var\s+minData\s*=\s*(\[.*?\]);', r.text, re.DOTALL)
        if m:
            data = json.loads(m.group(1))
            result = []
            for item in data:
                if isinstance(item, dict):
                    result.append({
                        "day": item.get("time", item.get("day", "")),
                        "open": str(item.get("open", 0)),
                        "close": str(item.get("close", 0)),
                        "high": str(item.get("high", 0)),
                        "low": str(item.get("low", 0)),
                        "volume": str(item.get("volume", 0)),
                    })
            return result[-count:] if result else []

    except Exception:
        pass
    return []


# ════════════════════════════════════════════════════
# 6. 大盘指数
# ════════════════════════════════════════════════════

def fetch_indices(driver) -> list[dict]:
    """从同花顺首页抓取大盘指数"""
    session = _build_session(driver)
    indices = []
    try:
        r = session.get("https://q.10jqka.com.cn/", timeout=15)
        r.encoding = "utf-8"
        text = r.text

        for name in ["上证指数", "深证成指", "创业板指"]:
            m = re.search(rf'{name}[：:\s]*<[^>]*>([\d.]+)</[^>]*>[^%]*?([+-]?[\d.]+)%', text)
            if not m:
                m = re.search(rf'{name}[：:\s]*([\d.]+)\s*([+-]?[\d.]+)%', text)
            if m:
                indices.append({
                    "name": name,
                    "price": _safe_float(m.group(1)),
                    "change_pct": _safe_float(m.group(2)),
                })
    except Exception as e:
        print(f"    ⚠ 大盘指数异常: {e}")

    return indices
