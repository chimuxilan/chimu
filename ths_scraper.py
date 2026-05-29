"""
ths_scraper.py — 同花顺 Selenium 爬虫（修复版 v2）
基于诊断结果修正：列顺序、详情页格式、板块解析、指数提取
"""

import time
import random
import re
import json
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ════════════════════════════════════════════════════
# 浏览器初始化
# ════════════════════════════════════════════════════

_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
]


def init_driver():
    """初始化 Selenium 浏览器（自动检测 Chrome/Edge）"""
    import shutil, os

    chrome_paths = [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ]
    edge_paths = [
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
    ]

    browser = None
    browser_path = None

    for p in chrome_paths:
        if os.path.exists(p):
            browser = "chrome"
            browser_path = p
            break
    if not browser:
        for p in edge_paths:
            if os.path.exists(p):
                browser = "edge"
                browser_path = p
                break

    if not browser:
        if shutil.which("chrome") or shutil.which("google-chrome"):
            browser = "chrome"
        elif shutil.which("msedge") or shutil.which("microsoft-edge"):
            browser = "edge"
        else:
            raise RuntimeError("未找到 Chrome 或 Edge 浏览器！")

    print(f"    检测到: {browser.upper()} ({browser_path or 'PATH'})")

    driver = None
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        from webdriver_manager.microsoft import EdgeChromiumManager
        from selenium.webdriver.chrome.service import Service as ChromeService
        from selenium.webdriver.edge.service import Service as EdgeService

        if browser == "chrome":
            opts = Options()
            if browser_path:
                opts.binary_location = browser_path
            _set_common_opts(opts)
            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
        else:
            from selenium.webdriver import EdgeOptions
            opts = EdgeOptions()
            if browser_path:
                opts.binary_location = browser_path
            _set_common_opts(opts)
            service = EdgeService(EdgeChromiumManager().install())
            driver = webdriver.Edge(service=service, options=opts)
        print("    ✅ webdriver_manager 自动配置成功")
    except ImportError:
        print("    ⚠ webdriver_manager 未安装，尝试直接启动...")
        if browser == "chrome":
            opts = Options()
            if browser_path:
                opts.binary_location = browser_path
            _set_common_opts(opts)
            driver = webdriver.Chrome(options=opts)
        else:
            from selenium.webdriver import EdgeOptions
            opts = EdgeOptions()
            if browser_path:
                opts.binary_location = browser_path
            _set_common_opts(opts)
            driver = webdriver.Edge(options=opts)
    except Exception as e:
        print(f"    ⚠ 自动下载失败({e})，尝试 Selenium 自带 driver...")
        if browser == "chrome":
            opts = Options()
            if browser_path:
                opts.binary_location = browser_path
            _set_common_opts(opts)
            driver = webdriver.Chrome(options=opts)
        else:
            from selenium.webdriver import EdgeOptions
            opts = EdgeOptions()
            if browser_path:
                opts.binary_location = browser_path
            _set_common_opts(opts)
            driver = webdriver.Edge(options=opts)

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    })
    driver.set_page_load_timeout(30)
    return driver


def _set_common_opts(opts):
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--user-agent={random.choice(_UAS)}")
    opts.add_argument("--log-level=3")
    opts.add_argument("--silent")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)


def close_driver(driver):
    try:
        driver.quit()
    except Exception:
        pass


def _delay(lo=0.5, hi=1.5):
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


def _wait_for_table(driver, timeout=15):
    """等待表格加载完成"""
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
        time.sleep(1)
        return True
    except Exception:
        return False


def _extract_table_rows(driver):
    """从当前页面提取表格所有行的 td 数据"""
    rows_data = []
    try:
        # 找到有数据的那个表格（跳过只有1行的空表格）
        tables = driver.find_elements(By.TAG_NAME, "table")
        target_table = None
        for table in tables:
            rows = table.find_elements(By.TAG_NAME, "tr")
            if len(rows) > 2:  # 至少3行（1表头+2数据）
                target_table = table
                break

        if not target_table:
            return rows_data

        rows = target_table.find_elements(By.TAG_NAME, "tr")
        for row in rows:
            tds = row.find_elements(By.TAG_NAME, "td")
            if len(tds) >= 4:
                row_text = [td.text.strip() for td in tds]
                # 同时获取链接中的代码
                links = row.find_elements(By.TAG_NAME, "a")
                link_codes = []
                for a in links:
                    href = a.get_attribute("href") or ""
                    code_m = re.search(r'/(\d{6})', href)
                    if code_m:
                        link_codes.append(code_m.group(1))
                rows_data.append({
                    "texts": row_text,
                    "link_codes": link_codes,
                })
    except Exception as e:
        print(f"    ⚠ 表格提取异常: {e}")
    return rows_data


# ════════════════════════════════════════════════════
# 1. 全A股代码列表
# ════════════════════════════════════════════════════

def fetch_all_stock_codes(driver) -> list[str]:
    """从同花顺行情页分页抓取沪深主板代码"""
    codes = []
    page = 1
    empty_count = 0

    while page <= 200:  # 最多200页
        url = f"https://q.10jqka.com.cn/index/index/board/all/field/199112/order/desc/page/{page}/"
        try:
            driver.get(url)
            _delay(1.0, 2.0)

            page_source = driver.page_source
            if "请输入验证码" in page_source or "访问过于频繁" in page_source:
                print(f"    ⚠ 第{page}页触发反爬验证码，停止翻页")
                break

            if not _wait_for_table(driver, timeout=10):
                print(f"    ⚠ 第{page}页表格未加载，停止")
                break

            rows = _extract_table_rows(driver)
            new_codes = []
            for row in rows:
                for code in row["link_codes"]:
                    if code.startswith(("60", "00")) and code not in codes:
                        new_codes.append(code)

            if not new_codes:
                empty_count += 1
                if empty_count >= 2:  # 连续2页无新数据才停止
                    print(f"    ℹ 连续{empty_count}页无新数据，停止翻页")
                    break
            else:
                empty_count = 0
                codes.extend(new_codes)

            if page % 10 == 0:
                print(f"    进度: 已翻到第{page}页，累计 {len(codes)} 只")
            page += 1
            _delay(0.3, 0.8)

        except Exception as e:
            print(f"    ⚠ 第{page}页异常: {e}")
            break

    return list(set(codes))


# ════════════════════════════════════════════════════
# 2. 实时行情（Selenium 直接渲染）
# ════════════════════════════════════════════════════

def fetch_quotes_batch(driver, codes: list[str]) -> dict:
    """
    从同花顺行情页批量抓取实时行情
    诊断结果：表格列顺序为：
      序号(0) | 代码(1) | 名称(2) | 现价(3) | 涨跌幅(4) | 涨跌(5) |
      换手率(6) | 量比(7) | 振幅(8) | 最高(9) | 最低(10) | 今开(11) | 昨收(12) | 成交量(13) | 成交额(14)
    """
    results = {}
    target_set = set(codes)
    page = 1
    empty_count = 0

    while page <= 200 and len(results) < len(codes):
        url = f"https://q.10jqka.com.cn/index/index/board/all/field/199112/order/desc/page/{page}/"
        try:
            driver.get(url)
            _delay(1.0, 2.0)

            page_source = driver.page_source
            if "请输入验证码" in page_source or "访问过于频繁" in page_source:
                print(f"    ⚠ 第{page}页触发反爬，停止")
                break

            if not _wait_for_table(driver, timeout=10):
                break

            rows = _extract_table_rows(driver)
            found_any = False

            for row in rows:
                texts = row["texts"]
                link_codes = row["link_codes"]

                if len(texts) < 6:
                    continue

                # 从链接获取代码
                code = None
                for c in link_codes:
                    if c in target_set:
                        code = c
                        break
                if not code:
                    for t in texts:
                        if re.match(r'^\d{6}$', t) and t in target_set:
                            code = t
                            break
                if not code:
                    continue

                # 列顺序: 序号 | 代码 | 名称 | 现价 | 涨跌幅 | 涨跌 | 换手率 | 量比 | 振幅 | 最高 | 最低 | 今开 | 昨收 | 成交量 | 成交额
                name = texts[2] if len(texts) > 2 else ""
                price = _safe_float(texts[3]) if len(texts) > 3 else 0
                change_pct = _safe_float(texts[4]) if len(texts) > 4 else 0
                turnover = _safe_float(texts[6]) if len(texts) > 6 else 0
                volume_ratio = _safe_float(texts[7]) if len(texts) > 7 else 0
                amplitude = _safe_float(texts[8]) if len(texts) > 8 else 0
                high = _safe_float(texts[9]) if len(texts) > 9 else 0
                low = _safe_float(texts[10]) if len(texts) > 10 else 0
                open_price = _safe_float(texts[11]) if len(texts) > 11 else 0
                prev_close = _safe_float(texts[12]) if len(texts) > 12 else 0
                volume = _parse_volume(texts[13]) if len(texts) > 13 else 0
                amount = _safe_float(texts[14]) if len(texts) > 14 else 0

                if price <= 0:
                    continue

                results[code] = {
                    "name": name,
                    "code": code,
                    "price": price,
                    "prev_close": prev_close,
                    "open": open_price,
                    "volume": volume,
                    "buy_vol": 0,
                    "sell_vol": 0,
                    "bid1_p": 0,
                    "bid1_v": 0,
                    "ask1_p": 0,
                    "ask1_v": 0,
                    "change_pct": change_pct,
                    "high": high,
                    "low": low,
                    "amount": amount,
                    "turnover": turnover,
                    "amplitude": amplitude,
                    "market_cap_yi": 0,
                    "volume_ratio_api": volume_ratio,
                    "quote_time": "",
                }
                found_any = True

            if not found_any:
                empty_count += 1
                if empty_count >= 2:
                    break
            else:
                empty_count = 0

            page += 1
            _delay(0.3, 0.8)

        except Exception as e:
            print(f"    ⚠ 行情第{page}页异常: {e}")
            break

    return results


# ════════════════════════════════════════════════════
# 3. 股票详情（Selenium 直接渲染个股页面）
# ════════════════════════════════════════════════════

def fetch_stock_details(driver, codes: list[str]) -> dict:
    """
    逐个抓取个股详情页
    诊断结果：页面文本格式为：
      "量比\n0.87\n低\n1,271.00\n流通\n15950.79亿\n换\n0.37%\n开\n1,290.00"
    需要从纯文本中用正则提取
    """
    results = {}
    done = 0
    blocked = False

    for code in codes:
        if blocked:
            break

        url = f"https://stockpage.10jqka.com.cn/{code}/"
        try:
            driver.get(url)
            _delay(0.5, 1.0)

            page_source = driver.page_source

            # 检查是否被封
            if "forbidden" in page_source.lower() or "Nginx forbidden" in page_source:
                print(f"    ❌ stockpage 被封IP，跳过详情抓取")
                blocked = True
                break

            if "请输入验证码" in page_source:
                print(f"    ⚠ 触发验证码，停止详情抓取")
                break

            # 从页面文本提取（诊断证明有效）
            body_text = ""
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                pass

            detail = {"code": code}

            # 流通市值: "流通\n15950.79亿" 或 "流通市值 15950.79亿"
            m = re.search(r'流通[^\d]*?([\d,.]+)\s*亿', body_text)
            if m:
                detail["market_cap_yi"] = _safe_float(m.group(1))

            # 量比: "量比\n0.87"
            m = re.search(r'量比\s*([\d.]+)', body_text)
            if m:
                detail["volume_ratio_api"] = _safe_float(m.group(1))

            # 换手率: "换\n0.37%" 或 "换手率 0.37%"
            m = re.search(r'换[手率]?\s*([\d.]+)%?', body_text)
            if m:
                detail["turnover"] = _safe_float(m.group(1))

            # 市值（总市值）: "市值\n12345.67亿"
            m = re.search(r'市值\s*([\d,.]+)\s*亿', body_text)
            if m:
                detail["market_cap_total_yi"] = _safe_float(m.group(1))

            if len(detail) > 1:
                results[code] = detail

        except Exception:
            pass

        done += 1
        if done % 50 == 0:
            print(f"    进度: {done}/{len(codes)} ({len(results)} 有数据)")
        _delay(0.3, 0.6)

    return results


# ════════════════════════════════════════════════════
# 4. 行业板块 + 成分股
# ════════════════════════════════════════════════════

def fetch_sectors(driver) -> dict:
    """
    从同花顺行业板块页抓取板块列表及成分股
    诊断结果：表格列顺序为：
      序号(0) | 名称(1) | 涨跌幅(2) | 市值(3) | 换手(4) | 领涨股涨幅(5) | 涨家数(6) | 跌家数(7)
    """
    sectors = {}
    page = 1

    while page <= 10:
        url = f"https://q.10jqka.com.cn/thshy/field/199112/order/desc/page/{page}/"
        try:
            driver.get(url)
            _delay(1.0, 2.0)

            page_source = driver.page_source
            if "请输入验证码" in page_source or "访问过于频繁" in page_source:
                print(f"    ⚠ 第{page}页触发反爬，停止")
                break

            if not _wait_for_table(driver, timeout=10):
                break

            rows = _extract_table_rows(driver)
            if not rows:
                break

            new_count = 0
            for row in rows:
                texts = row["texts"]
                link_codes = row["link_codes"]

                if len(texts) < 3:
                    continue

                # 从链接获取板块代码
                scode = ""
                for a_code in link_codes:
                    if len(a_code) >= 4:  # 板块代码通常较长
                        scode = a_code
                        break

                # 名称在第1列
                sname = texts[1] if len(texts) > 1 else ""
                if not sname or sname in sectors:
                    continue

                # 涨跌幅在第2列
                chg = _safe_float(texts[2]) if len(texts) > 2 else 0

                # 抓取成分股
                # 从链接中获取板块详情URL
                sector_url = ""
                links = driver.find_elements(By.TAG_NAME, "a")
                for a in links:
                    href = a.get_attribute("href") or ""
                    if "/thshy/detail/" in href and sname in (a.text or ""):
                        sector_url = href
                        break

                # 如果没找到URL，用代码构造
                if not sector_url and scode:
                    sector_url = f"https://q.10jqka.com.cn/thshy/detail/code/{scode}/"

                stocks = []
                if sector_url:
                    stocks = _fetch_sector_stocks(driver, sector_url)

                sectors[sname] = {
                    "code": scode,
                    "limit_up": 0,
                    "limit_down": 0,
                    "change_pct": chg,
                    "stocks": stocks,
                }
                print(f"    ✓ {sname}: {len(stocks)} 只 (涨跌:{chg:+.2f}%)")
                new_count += 1
                _delay(0.5, 1.0)

            if new_count == 0:
                break

            page += 1
            _delay(0.5, 1.5)

        except Exception as e:
            print(f"    ⚠ 板块第{page}页异常: {e}")
            break

    return sectors


def _fetch_sector_stocks(driver, url):
    """抓取单个板块的成分股"""
    stocks = []
    try:
        driver.get(url)
        _delay(1.0, 2.0)

        page_source = driver.page_source
        if "请输入验证码" in page_source or "forbidden" in page_source.lower():
            return stocks

        if not _wait_for_table(driver, timeout=10):
            return stocks

        rows = _extract_table_rows(driver)
        for row in rows:
            texts = row["texts"]
            link_codes = row["link_codes"]
            if len(texts) >= 3:
                code = ""
                for c in link_codes:
                    if c.startswith(("60", "00")):
                        code = c
                        break
                if not code:
                    for t in texts:
                        if re.match(r'^[036]\d{5}$', t) and t.startswith(("60", "00")):
                            code = t
                            break
                if code:
                    name = texts[2] if len(texts) > 2 else ""
                    stocks.append({"code": code, "name": name})
    except Exception:
        pass
    return stocks


# ════════════════════════════════════════════════════
# 5. K线数据（Selenium 直接渲染）
# ════════════════════════════════════════════════════

def fetch_kline_batch(driver, codes: list[str], days: int = 1000) -> dict:
    """
    逐个抓取日K线数据
    诊断结果：stockpage 被封IP（Nginx forbidden）
    尝试从行情页或其他途径获取K线
    """
    kline_map = {}
    done = 0
    blocked = False

    for code in codes:
        if blocked:
            break

        url = f"https://stockpage.10jqka.com.cn/{code}/"
        try:
            driver.get(url)
            _delay(0.5, 1.0)

            page_source = driver.page_source

            # 检查是否被封
            if "forbidden" in page_source.lower() or "Nginx forbidden" in page_source:
                print(f"    ❌ stockpage 被封IP，跳过K线抓取")
                blocked = True
                break

            if "请输入验证码" in page_source:
                print(f"    ⚠ 触发验证码，停止K线抓取")
                break

            klines = []

            # 方法1: 从页面JS变量提取
            m = re.search(r'var\s+klineData\s*=\s*(\[.*?\]);', page_source, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
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
                except json.JSONDecodeError:
                    pass

            # 方法2: Selenium 执行 JS
            if not klines:
                try:
                    js_data = driver.execute_script(
                        "try { return JSON.stringify(klineData); } catch(e) { return null; }"
                    )
                    if js_data:
                        data = json.loads(js_data)
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
                except Exception:
                    pass

            # 方法3: 从表格提取
            if not klines:
                table_m = re.search(r'class="histroyTrade"[^>]*>(.*?)</table>', page_source, re.DOTALL)
                if table_m:
                    trs = re.findall(r'<tr[^>]*>(.*?)</tr>', table_m.group(1), re.DOTALL)
                    for tr in trs:
                        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
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
                kline_map[code] = klines[-days:]

        except Exception:
            pass

        done += 1
        if done % 50 == 0:
            print(f"    进度: {done}/{len(codes)} ({len(kline_map)} 有数据)")
        _delay(0.3, 0.6)

    return kline_map


def fetch_kline_120min(driver, code: str, count: int = 60) -> list:
    """抓取120分钟K线"""
    url = f"https://stockpage.10jqka.com.cn/{code}/"
    try:
        driver.get(url)
        _delay(0.5, 1.0)

        page_source = driver.page_source
        if "forbidden" in page_source.lower():
            return []

        m = re.search(r'var\s+minData\s*=\s*(\[.*?\]);', page_source, re.DOTALL)
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

        try:
            js_data = driver.execute_script(
                "try { return JSON.stringify(minData); } catch(e) { return null; }"
            )
            if js_data:
                data = json.loads(js_data)
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

    except Exception:
        pass
    return []


# ════════════════════════════════════════════════════
# 6. 大盘指数
# ════════════════════════════════════════════════════

def fetch_indices(driver) -> list[dict]:
    """
    从同花顺首页抓取大盘指数
    诊断结果：页面文本2694字符，未找到"上证指数"
    可能是JS懒加载，需要等待更长时间或滚动页面
    """
    indices = []
    try:
        driver.get("https://q.10jqka.com.cn/")
        # 等待更长时间让JS加载
        time.sleep(5)

        # 尝试滚动页面触发懒加载
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)

        page_source = driver.page_source
        body_text = ""
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            pass

        # 从纯文本中提取
        for name in ["上证指数", "深证成指", "创业板指"]:
            # 格式: "上证指数\n3,283.59\n-0.62%"
            m = re.search(rf'{name}\s*([\d,.]+)\s*([+-]?[\d.]+)%', body_text)
            if m:
                indices.append({
                    "name": name,
                    "price": _safe_float(m.group(1)),
                    "change_pct": _safe_float(m.group(2)),
                })
                continue

            # 也尝试从HTML提取
            m = re.search(rf'{name}[：:\s]*<[^>]*>([\d.]+)</[^>]*>[^%]*?([+-]?[\d.]+)%', page_source)
            if m:
                indices.append({
                    "name": name,
                    "price": _safe_float(m.group(1)),
                    "change_pct": _safe_float(m.group(2)),
                })

        # 如果首页没拿到，尝试从子页面
        if not indices:
            try:
                driver.get("https://q.10jqka.com.cn/zs000001/")
                time.sleep(3)
                body_text = driver.find_element(By.TAG_NAME, "body").text
                m = re.search(r'([\d,.]+)\s*([+-]?[\d.]+)%', body_text)
                if m:
                    indices.append({
                        "name": "上证指数",
                        "price": _safe_float(m.group(1)),
                        "change_pct": _safe_float(m.group(2)),
                    })
            except Exception:
                pass

    except Exception as e:
        print(f"    ⚠ 大盘指数异常: {e}")

    return indices
