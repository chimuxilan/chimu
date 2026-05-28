"""
ths_scraper.py — 同花顺 Selenium 爬虫模块
替换所有 API 调用，通过模拟浏览器获取数据
"""

import time
import random
import re
import json
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ════════════════════════════════════════════════════
# 浏览器初始化
# ════════════════════════════════════════════════════

def init_driver():
    """初始化 Selenium Chrome 无头浏览器"""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # 自动检测 chromium 路径
    import shutil
    for p in ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]:
        if shutil.which(p) or __import__("os").path.exists(p):
            opts.binary_location = p
            break

    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    })
    driver.set_page_load_timeout(30)
    return driver


def _delay(lo=0.5, hi=1.5):
    """随机延迟，模拟人类操作"""
    time.sleep(random.uniform(lo, hi))


# ════════════════════════════════════════════════════
# 1. 全A股代码列表（同花顺行情页分页抓取）
# ════════════════════════════════════════════════════

def fetch_all_stock_codes(driver) -> list[str]:
    """
    从同花顺行情中心抓取全部沪深A股代码
    网址: q.10jqka.com.cn/index/index/board/all/
    """
    codes = []
    page = 1
    max_pages = 80  # 安全上限

    while page <= max_pages:
        url = f"https://q.10jqka.com.cn/index/index/board/all/field/199112/order/desc/page/{page}/"
        try:
            driver.get(url)
            _delay(1, 2)

            # 等待股票表格加载
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
            )

            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            if not rows:
                break

            found = 0
            for row in rows:
                try:
                    code_el = row.find_element(By.CSS_SELECTOR, "td a")
                    code = code_el.text.strip()
                    if len(code) == 6 and code.isdigit() and code.startswith(("60", "00")):
                        codes.append(code)
                        found += 1
                except Exception:
                    continue

            if found == 0:
                break

            page += 1
            _delay(0.8, 1.5)

        except Exception as e:
            print(f"    ⚠ 第{page}页异常: {e}")
            break

    return list(set(codes))


# ════════════════════════════════════════════════════
# 2. 实时行情（同花顺行情表批量抓取）
# ════════════════════════════════════════════════════

def fetch_quotes_batch(driver, codes: list[str]) -> dict:
    """
    从同花顺行情页批量抓取实时行情
    返回: {code: {name, code, price, change_pct, volume, ...}}
    """
    results = {}
    page = 1
    max_pages = 80
    target_set = set(codes)

    while page <= max_pages and len(results) < len(codes):
        url = f"https://q.10jqka.com.cn/index/index/board/all/field/199112/order/desc/page/{page}/"
        try:
            driver.get(url)
            _delay(1, 2)

            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
            )

            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            if not rows:
                break

            found_any = False
            for row in rows:
                try:
                    tds = row.find_elements(By.CSS_SELECTOR, "td")
                    if len(tds) < 6:
                        continue
                    code = tds[1].text.strip()
                    if code not in target_set:
                        continue

                    name = tds[2].text.strip()
                    price = tds[3].text.strip()
                    change_pct = tds[4].text.strip()
                    volume = tds[7].text.strip() if len(tds) > 7 else "0"

                    if not price or price == "—" or price == "-":
                        continue

                    results[code] = {
                        "name": name,
                        "code": code,
                        "price": _safe_float(price),
                        "prev_close": 0,
                        "open": 0,
                        "volume": _parse_volume(volume),
                        "buy_vol": 0,
                        "sell_vol": 0,
                        "bid1_p": 0,
                        "bid1_v": 0,
                        "ask1_p": 0,
                        "ask1_v": 0,
                        "change_pct": _safe_float(change_pct),
                        "high": 0,
                        "low": 0,
                        "amount": 0,
                        "turnover": 0,
                        "amplitude": 0,
                        "market_cap_yi": 0,
                        "volume_ratio_api": 0,
                        "quote_time": "",
                    }
                    found_any = True
                except Exception:
                    continue

            if not found_any:
                break

            page += 1
            _delay(0.8, 1.5)

        except Exception as e:
            print(f"    ⚠ 行情第{page}页异常: {e}")
            break

    return results


# ════════════════════════════════════════════════════
# 3. 股票详情（市值/量比/换手率 — 逐只抓取）
# ════════════════════════════════════════════════════

def fetch_stock_details(driver, codes: list[str]) -> dict:
    """
    从同花顺个股详情页抓取市值/量比/换手率
    网址: stockpage.10jqka.com.cn/{code}/
    """
    results = {}
    total = len(codes)

    for i, code in enumerate(codes):
        url = f"https://stockpage.10jqka.com.cn/{code}/"
        try:
            driver.get(url)
            _delay(0.5, 1.0)

            # 等待数据加载
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#main"))
            )
            time.sleep(0.5)  # 额外等待JS渲染

            page_text = driver.find_element(By.CSS_SELECTOR, "body").text

            detail = {"code": code}

            # 解析流通市值
            m = re.search(r"流通市值[:\s]*([\d.]+)\s*亿", page_text)
            if m:
                detail["market_cap_yi"] = _safe_float(m.group(1))

            # 解析量比
            m = re.search(r"量比[:\s]*([\d.]+)", page_text)
            if m:
                detail["volume_ratio_api"] = _safe_float(m.group(1))

            # 解析换手率
            m = re.search(r"换手率[:\s]*([\d.]+)%?", page_text)
            if m:
                detail["turnover"] = _safe_float(m.group(1))

            if len(detail) > 1:
                results[code] = detail

            if (i + 1) % 50 == 0:
                print(f"    进度: {i+1}/{total}")

        except Exception:
            continue

    return results


# ════════════════════════════════════════════════════
# 4. 行业板块 + 成分股
# ════════════════════════════════════════════════════

def fetch_sectors(driver) -> dict:
    """
    从同花顺行业板块页抓取板块列表及成分股
    网址: q.10jqka.com.cn/thshy/
    """
    sectors = {}
    page = 1
    max_pages = 10

    while page <= max_pages:
        url = f"https://q.10jqka.com.cn/thshy/field/199112/order/desc/page/{page}/"
        try:
            driver.get(url)
            _delay(1, 2)

            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
            )

            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            if not rows:
                break

            sector_links = []
            for row in rows:
                try:
                    tds = row.find_elements(By.CSS_SELECTOR, "td")
                    if len(tds) < 4:
                        continue
                    link = tds[1].find_element(By.CSS_SELECTOR, "a")
                    name = link.text.strip()
                    href = link.get_attribute("href")
                    change_pct = tds[3].text.strip() if len(tds) > 3 else "0"
                    if name and href:
                        sector_links.append((name, href, change_pct))
                except Exception:
                    continue

            for sname, href, chg in sector_links:
                try:
                    driver.get(href)
                    _delay(0.8, 1.5)

                    WebDriverWait(driver, 8).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
                    )

                    stocks = []
                    srows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                    for sr in srows:
                        try:
                            stds = sr.find_elements(By.CSS_SELECTOR, "td")
                            if len(stds) < 2:
                                continue
                            scode = stds[1].text.strip()
                            sname2 = stds[2].text.strip() if len(stds) > 2 else ""
                            if scode and len(scode) == 6 and scode.isdigit():
                                stocks.append({"code": scode, "name": sname2})
                        except Exception:
                            continue

                    sectors[sname] = {
                        "code": "",
                        "limit_up": 0,
                        "limit_down": 0,
                        "change_pct": _safe_float(chg),
                        "stocks": stocks,
                    }
                    print(f"    ✓ {sname}: {len(stocks)} 只")
                    _delay(0.5, 1.0)

                except Exception:
                    continue

            page += 1
            _delay(0.8, 1.5)

        except Exception as e:
            print(f"    ⚠ 板块第{page}页异常: {e}")
            break

    return sectors


# ════════════════════════════════════════════════════
# 5. K线数据（同花顺历史行情页）
# ════════════════════════════════════════════════════

def fetch_kline_batch(driver, codes: list[str], days: int = 1000) -> dict:
    """
    从同花顺历史行情页批量抓取日K线
    网址: stockpage.10jqka.com.cn/{code}/
    """
    kline_map = {}
    total = len(codes)

    for i, code in enumerate(codes):
        url = f"https://stockpage.10jqka.com.cn/{code}/"
        try:
            driver.get(url)
            _delay(0.5, 1.0)

            # 点击"历史行情"tab
            try:
                hist_tab = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//a[contains(text(),'历史行情')]"))
                )
                hist_tab.click()
                _delay(1, 2)
            except Exception:
                pass

            # 通过页面JS获取K线数据
            try:
                js_data = driver.execute_script(
                    "return typeof quotationData !== 'undefined' ? quotationData : null;"
                )
                if js_data and "data" in js_data:
                    klines = _parse_ths_kline(js_data["data"])
                    if klines:
                        kline_map[code] = klines[-days:]
                        continue
            except Exception:
                pass

            # 备用：解析页面表格
            klines = _parse_kline_from_table(driver)
            if klines:
                kline_map[code] = klines[-days:]

            if (i + 1) % 50 == 0:
                print(f"    进度: {i+1}/{total}")

        except Exception:
            continue

    return kline_map


def fetch_kline_120min(driver, code: str, count: int = 60) -> list[dict]:
    """抓取120分钟K线"""
    url = f"https://stockpage.10jqka.com.cn/{code}/"
    try:
        driver.get(url)
        _delay(0.5, 1.0)

        # 尝试通过JS获取分时数据
        js_data = driver.execute_script(
            "return typeof quotationData !== 'undefined' ? quotationData : null;"
        )
        if js_data:
            min_data = js_data.get("120min") or js_data.get("min") or []
            if min_data:
                result = []
                for item in min_data:
                    if isinstance(item, dict):
                        result.append({
                            "day": item.get("time", ""),
                            "open": str(item.get("open", 0)),
                            "high": str(item.get("high", 0)),
                            "low": str(item.get("low", 0)),
                            "close": str(item.get("close", 0)),
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
    """
    从同花顺首页抓取大盘指数
    """
    indices = []
    url = "https://q.10jqka.com.cn/"
    try:
        driver.get(url)
        _delay(1, 2)

        body = driver.find_element(By.CSS_SELECTOR, "body").text

        # 解析上证指数
        m = re.search(r"上证指数[:\s]*([\d.]+)\s*([+-]?[\d.]+)%", body)
        if m:
            indices.append({
                "name": "上证指数",
                "price": _safe_float(m.group(1)),
                "change_pct": _safe_float(m.group(2)),
            })

        # 解析深证成指
        m = re.search(r"深证成指[:\s]*([\d.]+)\s*([+-]?[\d.]+)%", body)
        if m:
            indices.append({
                "name": "深证成指",
                "price": _safe_float(m.group(1)),
                "change_pct": _safe_float(m.group(2)),
            })

        # 解析创业板指
        m = re.search(r"创业板指[:\s]*([\d.]+)\s*([+-]?[\d.]+)%", body)
        if m:
            indices.append({
                "name": "创业板指",
                "price": _safe_float(m.group(1)),
                "change_pct": _safe_float(m.group(2)),
            })

    except Exception as e:
        print(f"    ⚠ 大盘指数异常: {e}")

    return indices


# ════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════

def _safe_float(s):
    try:
        return float(str(s).replace("%", "").replace(",", "").replace(" ", ""))
    except (ValueError, TypeError):
        return 0.0


def _parse_volume(s):
    """解析成交量（带单位：万手/手）"""
    s = str(s).strip()
    try:
        if "万" in s:
            return int(_safe_float(s.replace("万", "")) * 10000)
        return int(_safe_float(s))
    except (ValueError, TypeError):
        return 0


def _parse_ths_kline(data_str) -> list[dict]:
    """解析同花顺K线数据字符串"""
    klines = []
    try:
        if isinstance(data_str, str):
            for item in data_str.split(";"):
                parts = item.split(",")
                if len(parts) >= 6:
                    klines.append({
                        "day": parts[0],
                        "open": parts[1],
                        "close": parts[2],
                        "high": parts[3],
                        "low": parts[4],
                        "volume": parts[5],
                    })
        elif isinstance(data_str, list):
            for item in data_str:
                if isinstance(item, (list, tuple)) and len(item) >= 6:
                    klines.append({
                        "day": str(item[0]),
                        "open": str(item[1]),
                        "close": str(item[2]),
                        "high": str(item[3]),
                        "low": str(item[4]),
                        "volume": str(item[5]),
                    })
    except Exception:
        pass
    return klines


def _parse_kline_from_table(driver) -> list[dict]:
    """从历史行情表格解析K线数据"""
    klines = []
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            tds = row.find_elements(By.CSS_SELECTOR, "td")
            if len(tds) >= 6:
                klines.append({
                    "day": tds[0].text.strip(),
                    "open": tds[1].text.strip(),
                    "close": tds[2].text.strip(),
                    "high": tds[3].text.strip(),
                    "low": tds[4].text.strip(),
                    "volume": tds[5].text.strip(),
                })
    except Exception:
        pass
    return klines


def close_driver(driver):
    """关闭浏览器"""
    try:
        driver.quit()
    except Exception:
        pass
