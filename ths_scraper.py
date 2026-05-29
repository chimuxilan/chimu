"""
ths_scraper.py — 同花顺 Selenium 爬虫（修复版 v3）
基于实际运行结果修正所有问题
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

_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
]


def init_driver():
    import shutil, os
    chrome_paths = [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    ]
    edge_paths = [
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
        "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
    ]
    browser = browser_path = None
    for p in chrome_paths:
        if os.path.exists(p):
            browser, browser_path = "chrome", p; break
    if not browser:
        for p in edge_paths:
            if os.path.exists(p):
                browser, browser_path = "edge", p; break
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
            if browser_path: opts.binary_location = browser_path
            _set_common_opts(opts)
            driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=opts)
        else:
            from selenium.webdriver import EdgeOptions
            opts = EdgeOptions()
            if browser_path: opts.binary_location = browser_path
            _set_common_opts(opts)
            driver = webdriver.Edge(service=EdgeService(EdgeChromiumManager().install()), options=opts)
        print("    ✅ webdriver_manager 自动配置成功")
    except ImportError:
        print("    ⚠ webdriver_manager 未安装，尝试直接启动...")
        if browser == "chrome":
            opts = Options()
            if browser_path: opts.binary_location = browser_path
            _set_common_opts(opts)
            driver = webdriver.Chrome(options=opts)
        else:
            from selenium.webdriver import EdgeOptions
            opts = EdgeOptions()
            if browser_path: opts.binary_location = browser_path
            _set_common_opts(opts)
            driver = webdriver.Edge(options=opts)
    except Exception as e:
        print(f"    ⚠ 自动下载失败({e})，尝试 Selenium 自带 driver...")
        if browser == "chrome":
            opts = Options()
            if browser_path: opts.binary_location = browser_path
            _set_common_opts(opts)
            driver = webdriver.Chrome(options=opts)
        else:
            from selenium.webdriver import EdgeOptions
            opts = EdgeOptions()
            if browser_path: opts.binary_location = browser_path
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
    try: driver.quit()
    except: pass

def _delay(lo=0.5, hi=1.5):
    time.sleep(random.uniform(lo, hi))

def _safe_float(s):
    try: return float(str(s).replace("%", "").replace(",", "").replace(" ", "").replace("万", ""))
    except: return 0.0

def _parse_volume(s):
    s = str(s).strip()
    try:
        if "万" in s: return int(_safe_float(s.replace("万", "")) * 10000)
        return int(_safe_float(s))
    except: return 0


def _get_data_table(driver):
    """找到有数据的表格（行数>2），返回该 table element"""
    tables = driver.find_elements(By.TAG_NAME, "table")
    for table in tables:
        rows = table.find_elements(By.TAG_NAME, "tr")
        if len(rows) > 2:
            return table
    return None


def _extract_rows_from_table(table):
    """从指定表格提取所有行"""
    rows_data = []
    try:
        rows = table.find_elements(By.TAG_NAME, "tr")
        for row in rows:
            tds = row.find_elements(By.TAG_NAME, "td")
            if len(tds) >= 4:
                row_text = [td.text.strip() for td in tds]
                links = row.find_elements(By.TAG_NAME, "a")
                link_codes = []
                for a in links:
                    href = a.get_attribute("href") or ""
                    code_m = re.search(r'/(\d{6})', href)
                    if code_m:
                        link_codes.append(code_m.group(1))
                rows_data.append({"texts": row_text, "link_codes": link_codes})
    except Exception as e:
        print(f"    ⚠ 表格提取异常: {e}")
    return rows_data


def _check_blocked(driver):
    """检查页面是否被反爬拦截"""
    try:
        ps = driver.page_source
        if "请输入验证码" in ps or "访问过于频繁" in ps:
            return "captcha"
        if "forbidden" in ps.lower() or "Nginx forbidden" in ps:
            return "blocked"
    except:
        pass
    return None


# ════════════════════════════════════════════════════
# 1. 全A股代码列表
# ════════════════════════════════════════════════════

def fetch_all_stock_codes(driver) -> list[str]:
    """
    从同花顺行情页分页抓取沪深主板代码
    每页20条，用排序字段和方向尝试获取更多数据
    """
    codes = []
    empty_count = 0

    # 尝试不同的排序方向获取更多股票
    sort_configs = [
        ("199112", "desc"),  # 默认排序
        ("199112", "asc"),   # 反向排序
        ("symbol", "asc"),   # 按代码正序
        ("symbol", "desc"),  # 按代码倒序
    ]

    for sort_field, sort_dir in sort_configs:
        if empty_count >= 2 and len(codes) > 0:
            break

        page = 1
        while page <= 200:
            url = f"https://q.10jqka.com.cn/index/index/board/all/field/{sort_field}/order/{sort_dir}/page/{page}/"
            try:
                driver.get(url)
                _delay(1.0, 2.0)

                block = _check_blocked(driver)
                if block:
                    print(f"    ⚠ 第{page}页被拦截({block})，停止")
                    break

                table = _get_data_table(driver)
                if not table:
                    break

                rows = _extract_rows_from_table(table)
                new_codes = []
                for row in rows:
                    for code in row["link_codes"]:
                        if code.startswith(("60", "00")) and code not in codes:
                            new_codes.append(code)

                if not new_codes:
                    empty_count += 1
                    if empty_count >= 2:
                        break
                    page += 1
                    continue
                else:
                    empty_count = 0
                    codes.extend(new_codes)

                if page % 10 == 0:
                    print(f"    进度: 第{page}页，累计 {len(codes)} 只")
                page += 1
                _delay(0.3, 0.8)

            except Exception as e:
                print(f"    ⚠ 第{page}页异常: {e}")
                break

    return list(set(codes))


# ════════════════════════════════════════════════════
# 2. 实时行情
# ════════════════════════════════════════════════════

def fetch_quotes_batch(driver, codes: list[str]) -> dict:
    """
    从同花顺行情页批量抓取实时行情
    列顺序（诊断确认）: 序号|代码|名称|现价|涨跌幅|涨跌|换手率|量比|振幅|最高|最低|今开|昨收|成交量|成交额
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

            block = _check_blocked(driver)
            if block:
                print(f"    ⚠ 第{page}页被拦截({block})，停止")
                break

            table = _get_data_table(driver)
            if not table:
                break

            rows = _extract_rows_from_table(table)
            found_any = False

            for row in rows:
                texts = row["texts"]
                link_codes = row["link_codes"]
                if len(texts) < 6:
                    continue

                code = None
                for c in link_codes:
                    if c in target_set:
                        code = c; break
                if not code:
                    for t in texts:
                        if re.match(r'^\d{6}$', t) and t in target_set:
                            code = t; break
                if not code:
                    continue

                # 列: 序号(0) 代码(1) 名称(2) 现价(3) 涨跌幅(4) 涨跌(5) 换手率(6) 量比(7) 振幅(8) 最高(9) 最低(10) 今开(11) 昨收(12) 成交量(13) 成交额(14)
                price = _safe_float(texts[3]) if len(texts) > 3 else 0
                if price <= 0:
                    continue

                results[code] = {
                    "name": texts[2] if len(texts) > 2 else "",
                    "code": code,
                    "price": price,
                    "prev_close": _safe_float(texts[12]) if len(texts) > 12 else 0,
                    "open": _safe_float(texts[11]) if len(texts) > 11 else 0,
                    "volume": _parse_volume(texts[13]) if len(texts) > 13 else 0,
                    "buy_vol": 0, "sell_vol": 0,
                    "bid1_p": 0, "bid1_v": 0, "ask1_p": 0, "ask1_v": 0,
                    "change_pct": _safe_float(texts[4]) if len(texts) > 4 else 0,
                    "high": _safe_float(texts[9]) if len(texts) > 9 else 0,
                    "low": _safe_float(texts[10]) if len(texts) > 10 else 0,
                    "amount": _safe_float(texts[14]) if len(texts) > 14 else 0,
                    "turnover": _safe_float(texts[6]) if len(texts) > 6 else 0,
                    "amplitude": _safe_float(texts[8]) if len(texts) > 8 else 0,
                    "market_cap_yi": 0,
                    "volume_ratio_api": _safe_float(texts[7]) if len(texts) > 7 else 0,
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
# 3. 股票详情
# ════════════════════════════════════════════════════

def fetch_stock_details(driver, codes: list[str]) -> dict:
    """
    逐个抓取个股详情页（市值/量比/换手率）
    页面文本格式: "量比\n0.87\n低\n1,271.00\n流通\n15950.79亿\n换\n0.37%"
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

            block = _check_blocked(driver)
            if block == "blocked":
                print(f"    ❌ stockpage 被封IP，跳过详情抓取")
                blocked = True; break
            if block == "captcha":
                print(f"    ⚠ 触发验证码，停止详情抓取")
                break

            body_text = ""
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except: pass

            detail = {"code": code}

            m = re.search(r'流通[^\d]*?([\d,.]+)\s*亿', body_text)
            if m: detail["market_cap_yi"] = _safe_float(m.group(1))

            m = re.search(r'量比\s*([\d.]+)', body_text)
            if m: detail["volume_ratio_api"] = _safe_float(m.group(1))

            m = re.search(r'换[手率]?\s*([\d.]+)%?', body_text)
            if m: detail["turnover"] = _safe_float(m.group(1))

            m = re.search(r'市值\s*([\d,.]+)\s*亿', body_text)
            if m: detail["market_cap_total_yi"] = _safe_float(m.group(1))

            if len(detail) > 1:
                results[code] = detail

        except: pass

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
    诊断确认列顺序: 序号|名称|涨跌幅|市值|换手|领涨股涨幅|涨家数|跌家数
    """
    sectors = {}
    page = 1

    while page <= 10:
        url = f"https://q.10jqka.com.cn/thshy/field/199112/order/desc/page/{page}/"
        try:
            driver.get(url)
            _delay(1.0, 2.0)

            block = _check_blocked(driver)
            if block:
                print(f"    ⚠ 第{page}页被拦截({block})，停止")
                break

            table = _get_data_table(driver)
            if not table:
                break

            rows = _extract_rows_from_table(table)
            if not rows:
                break

            # 提取板块链接（从整个页面的 a 标签中找）
            all_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/thshy/detail/']")
            sector_links = {}
            for a in all_links:
                href = a.get_attribute("href") or ""
                text = a.text.strip()
                m = re.search(r'/code/(\d+)/', href)
                if m and text:
                    sector_links[text] = {"code": m.group(1), "url": href}

            new_count = 0
            for row in rows:
                texts = row["texts"]
                if len(texts) < 2:
                    continue

                # 名称在第1列
                sname = texts[1] if len(texts) > 1 else ""
                if not sname or sname in sectors:
                    continue

                # 涨跌幅在第2列
                chg = _safe_float(texts[2]) if len(texts) > 2 else 0

                # 从链接获取板块代码和URL
                scode = ""
                sector_url = ""
                if sname in sector_links:
                    scode = sector_links[sname]["code"]
                    sector_url = sector_links[sname]["url"]

                # 如果没找到链接，从行内的链接获取
                if not scode:
                    for c in row["link_codes"]:
                        if len(c) >= 4:
                            scode = c; break

                # 抓取成分股
                stocks = []
                if sector_url:
                    stocks = _fetch_sector_stocks(driver, sector_url)
                elif scode:
                    stocks = _fetch_sector_stocks(driver, f"https://q.10jqka.com.cn/thshy/detail/code/{scode}/")

                sectors[sname] = {
                    "code": scode,
                    "limit_up": 0, "limit_down": 0,
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

        block = _check_blocked(driver)
        if block:
            return stocks

        table = _get_data_table(driver)
        if not table:
            return stocks

        rows = _extract_rows_from_table(table)
        for row in rows:
            texts = row["texts"]
            link_codes = row["link_codes"]
            if len(texts) >= 3:
                code = ""
                for c in link_codes:
                    if c.startswith(("60", "00")):
                        code = c; break
                if not code:
                    for t in texts:
                        if re.match(r'^[036]\d{5}$', t) and t.startswith(("60", "00")):
                            code = t; break
                if code:
                    name = texts[2] if len(texts) > 2 else ""
                    stocks.append({"code": code, "name": name})
    except: pass
    return stocks


# ════════════════════════════════════════════════════
# 5. K线数据
# ════════════════════════════════════════════════════

def fetch_kline_batch(driver, codes: list[str], days: int = 1000) -> dict:
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

            block = _check_blocked(driver)
            if block == "blocked":
                print(f"    ❌ stockpage 被封IP，跳过K线抓取")
                blocked = True; break
            if block == "captcha":
                print(f"    ⚠ 触发验证码，停止K线抓取")
                break

            ps = driver.page_source
            klines = []

            # 方法1: HTML中的JS变量
            m = re.search(r'var\s+klineData\s*=\s*(\[.*?\]);', ps, re.DOTALL)
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
                                "day": str(item[0]), "open": str(item[1]),
                                "close": str(item[2]), "high": str(item[3]),
                                "low": str(item[4]), "volume": str(item[5]),
                            })
                except: pass

            # 方法2: Selenium JS执行
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
                except: pass

            # 方法3: 表格
            if not klines:
                table_m = re.search(r'class="histroyTrade"[^>]*>(.*?)</table>', ps, re.DOTALL)
                if table_m:
                    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', table_m.group(1), re.DOTALL):
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

        except: pass

        done += 1
        if done % 50 == 0:
            print(f"    进度: {done}/{len(codes)} ({len(kline_map)} 有数据)")
        _delay(0.3, 0.6)

    return kline_map


def fetch_kline_120min(driver, code: str, count: int = 60) -> list:
    url = f"https://stockpage.10jqka.com.cn/{code}/"
    try:
        driver.get(url)
        _delay(0.5, 1.0)

        block = _check_blocked(driver)
        if block:
            return []

        ps = driver.page_source
        m = re.search(r'var\s+minData\s*=\s*(\[.*?\]);', ps, re.DOTALL)
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
        except: pass
    except: pass
    return []


# ════════════════════════════════════════════════════
# 6. 大盘指数
# ════════════════════════════════════════════════════

def fetch_indices(driver) -> list[dict]:
    """
    从同花顺首页抓取大盘指数
    诊断结果：页面文本2694字符，未找到指数名
    解决：等待更久 + 滚动 + 尝试多种选择器
    """
    indices = []
    try:
        driver.get("https://q.10jqka.com.cn/")

        # 等待JS渲染（多等几秒）
        for _ in range(3):
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.5)
            driver.execute_script("window.scrollTo(0, 300);")
            time.sleep(0.5)

        # 从整个页面HTML中提取（比body.text更可靠）
        ps = driver.page_source

        # 尝试从HTML中提取（同花顺可能把指数放在特定div中）
        for name in ["上证指数", "深证成指", "创业板指"]:
            # 模式1: "上证指数</a>...3283.59...-0.62%"
            m = re.search(rf'{name}.*?([\d,.]+).*?([+-]?[\d.]+)%', ps, re.DOTALL)
            if m:
                price = _safe_float(m.group(1))
                chg = _safe_float(m.group(2))
                if 1000 < price < 50000:  # 合理范围
                    indices.append({"name": name, "price": price, "change_pct": chg})
                    continue

            # 模式2: 从纯文本
            body_text = ""
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except: pass
            m = re.search(rf'{name}\s*([\d,.]+)\s*([+-]?[\d.]+)%', body_text)
            if m:
                price = _safe_float(m.group(1))
                chg = _safe_float(m.group(2))
                if 1000 < price < 50000:
                    indices.append({"name": name, "price": price, "change_pct": chg})
                    continue

            # 模式3: 尝试CSS选择器
            try:
                elems = driver.find_elements(By.XPATH, f"//*[contains(text(), '{name}')]/..")
                for elem in elems:
                    text = elem.text
                    m = re.search(r'([\d,.]+)\s*([+-]?[\d.]+)%', text)
                    if m:
                        price = _safe_float(m.group(1))
                        chg = _safe_float(m.group(2))
                        if 1000 < price < 50000:
                            indices.append({"name": name, "price": price, "change_pct": chg})
                            break
            except: pass

    except Exception as e:
        print(f"    ⚠ 大盘指数异常: {e}")

    # 如果同花顺首页拿不到，用新浪备用接口
    if not indices:
        try:
            import requests
            s = requests.Session()
            s.headers["User-Agent"] = random.choice(_UAS)
            # 新浪指数接口
            r = s.get("https://hq.sinajs.cn/list=s_sh000001,s_sz399001,s_sz399006",
                      headers={"Referer": "https://finance.sina.com.cn/"}, timeout=10)
            r.encoding = "gbk"
            name_map = {"s_sh000001": "上证指数", "s_sz399001": "深证成指", "s_sz399006": "创业板指"}
            for line in r.text.strip().split("\n"):
                m = re.search(r'hq_str_(\w+)="(.+)"', line)
                if m:
                    symbol = m.group(1)
                    fields = m.group(2).split(",")
                    if len(fields) >= 4:
                        name = name_map.get(symbol, fields[0])
                        price = _safe_float(fields[1])
                        chg = _safe_float(fields[3])
                        if price > 0:
                            indices.append({"name": name, "price": price, "change_pct": chg})
        except Exception as e:
            print(f"    ⚠ 新浪指数备用接口也失败: {e}")

    return indices
