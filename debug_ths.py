"""
debug_ths.py — 同花顺 Selenium 诊断脚本
运行: python debug_ths.py
输出: debug_output/ 目录下的日志和截图
"""

import time
import os
import json

# 复用 ths_scraper 的浏览器初始化
from ths_scraper import init_driver, close_driver

os.makedirs("debug_output", exist_ok=True)

driver = None
try:
    print("🌐 启动浏览器...")
    driver = init_driver()
    print("✅ 浏览器就绪\n")

    # ═══════════════════════════════════════════
    # 测试1: 大盘指数
    # ═══════════════════════════════════════════
    print("=" * 50)
    print("📊 测试1: 大盘指数 (q.10jqka.com.cn)")
    print("=" * 50)
    driver.get("https://q.10jqka.com.cn/")
    time.sleep(3)

    page_source = driver.page_source
    with open("debug_output/01_index_page.html", "w", encoding="utf-8") as f:
        f.write(page_source)
    print(f"  页面大小: {len(page_source)} 字符")
    print(f"  已保存: debug_output/01_index_page.html")

    try:
        driver.save_screenshot("debug_output/01_index.png")
        print("  截图: debug_output/01_index.png")
    except Exception as e:
        print(f"  截图失败: {e}")

    # 检查页面文本中有没有指数数据
    body_text = driver.find_element("tag name", "body").text
    with open("debug_output/01_index_text.txt", "w", encoding="utf-8") as f:
        f.write(body_text)
    print(f"  页面文本大小: {len(body_text)} 字符")

    for keyword in ["上证指数", "深证成指", "创业板指", "3000", "10000"]:
        if keyword in body_text:
            # 找到关键词附近的内容
            idx = body_text.index(keyword)
            context = body_text[max(0, idx-20):idx+80]
            print(f"  ✅ 找到 '{keyword}': ...{context}...")
        else:
            print(f"  ❌ 未找到 '{keyword}'")

    # ═══════════════════════════════════════════
    # 测试2: 行情列表
    # ═══════════════════════════════════════════
    print("\n" + "=" * 50)
    print("📋 测试2: 行情列表 (第1页)")
    print("=" * 50)
    driver.get("https://q.10jqka.com.cn/index/index/board/all/field/199112/order/desc/page/1/")
    time.sleep(3)

    page_source = driver.page_source
    with open("debug_output/02_quotes_page.html", "w", encoding="utf-8") as f:
        f.write(page_source)
    print(f"  页面大小: {len(page_source)} 字符")

    try:
        driver.save_screenshot("debug_output/02_quotes.png")
        print("  截图: debug_output/02_quotes.png")
    except Exception as e:
        print(f"  截图失败: {e}")

    # 检查表格
    tables = driver.find_elements("tag name", "table")
    print(f"  表格数量: {len(tables)}")

    for i, table in enumerate(tables):
        rows = table.find_elements("tag name", "tr")
        print(f"  表格{i}: {len(rows)} 行")
        if rows and len(rows) > 1:
            # 显示前2行的td内容
            for j, row in enumerate(rows[:3]):
                tds = row.find_elements("tag name", "td")
                if tds:
                    texts = [td.text.strip()[:20] for td in tds[:10]]
                    print(f"    行{j}: {texts}")

    # 检查链接中的股票代码
    links = driver.find_elements("tag name", "a")
    codes_found = []
    import re
    for a in links:
        href = a.get_attribute("href") or ""
        m = re.search(r'/(\d{6})', href)
        if m and m.group(1).startswith(("60", "00")):
            codes_found.append(m.group(1))
    print(f"  从链接中找到的股票代码: {len(set(codes_found))} 个")
    if codes_found:
        print(f"  前10个: {list(set(codes_found))[:10]}")

    # ═══════════════════════════════════════════
    # 测试3: 个股详情页
    # ═══════════════════════════════════════════
    print("\n" + "=" * 50)
    print("📈 测试3: 个股详情页 (600519)")
    print("=" * 50)
    driver.get("https://stockpage.10jqka.com.cn/600519/")
    time.sleep(3)

    page_source = driver.page_source
    with open("debug_output/03_stock_page.html", "w", encoding="utf-8") as f:
        f.write(page_source)
    print(f"  页面大小: {len(page_source)} 字符")

    try:
        driver.save_screenshot("debug_output/03_stock.png")
        print("  截图: debug_output/03_stock.png")
    except Exception as e:
        print(f"  截图失败: {e}")

    if "forbidden" in page_source.lower() or "403" in page_source:
        print("  ❌ 被封IP了 (Nginx forbidden)")
    elif "请输入验证码" in page_source:
        print("  ❌ 触发验证码")
    elif "klineData" in page_source:
        print("  ✅ 找到 klineData 变量")
    else:
        print("  ⚠ 未找到 klineData，检查页面内容...")
        # 找所有 script 标签中的变量
        scripts = driver.find_elements("tag name", "script")
        for s in scripts:
            src = s.get_attribute("innerHTML") or ""
            if "var " in src and ("Data" in src or "data" in src):
                # 提取变量名
                vars_found = re.findall(r'var\s+(\w+)', src)
                if vars_found:
                    print(f"    JS变量: {vars_found[:10]}")

    body_text = driver.find_element("tag name", "body").text
    print(f"  页面文本: {len(body_text)} 字符")
    # 搜索关键数据
    for keyword in ["流通市值", "量比", "换手率", "贵州茅台"]:
        if keyword in body_text:
            idx = body_text.index(keyword)
            context = body_text[max(0, idx-10):idx+50]
            print(f"  ✅ 找到 '{keyword}': ...{context}...")
        else:
            print(f"  ❌ 未找到 '{keyword}'")

    # ═══════════════════════════════════════════
    # 测试4: 板块页
    # ═══════════════════════════════════════════
    print("\n" + "=" * 50)
    print("🏭 测试4: 行业板块页")
    print("=" * 50)
    driver.get("https://q.10jqka.com.cn/thshy/")
    time.sleep(3)

    page_source = driver.page_source
    with open("debug_output/04_sector_page.html", "w", encoding="utf-8") as f:
        f.write(page_source)
    print(f"  页面大小: {len(page_source)} 字符")

    try:
        driver.save_screenshot("debug_output/04_sector.png")
        print("  截图: debug_output/04_sector.png")
    except Exception as e:
        print(f"  截图失败: {e}")

    tables = driver.find_elements("tag name", "table")
    print(f"  表格数量: {len(tables)}")
    for i, table in enumerate(tables):
        rows = table.find_elements("tag name", "tr")
        print(f"  表格{i}: {len(rows)} 行")
        if rows and len(rows) > 1:
            for j, row in enumerate(rows[:3]):
                tds = row.find_elements("tag name", "td")
                if tds:
                    texts = [td.text.strip()[:20] for td in tds[:8]]
                    print(f"    行{j}: {texts}")

    # ═══════════════════════════════════════════
    # 测试5: 验证码检测
    # ═══════════════════════════════════════════
    print("\n" + "=" * 50)
    print("🔍 测试5: 连续访问检测反爬")
    print("=" * 50)
    for p in range(1, 4):
        url = f"https://q.10jqka.com.cn/index/index/board/all/field/199112/order/desc/page/{p}/"
        driver.get(url)
        time.sleep(2)
        ps = driver.page_source
        has_captcha = "请输入验证码" in ps or "访问过于频繁" in ps
        has_table = len(driver.find_elements("tag name", "table")) > 0
        print(f"  第{p}页: 验证码={has_captcha}, 有表格={has_table}, 大小={len(ps)}字符")

    print("\n" + "=" * 50)
    print("✅ 诊断完成! 请检查 debug_output/ 目录")
    print("=" * 50)

except Exception as e:
    print(f"\n❌ 异常: {e}")
    import traceback
    traceback.print_exc()

finally:
    if driver:
        close_driver(driver)
        print("🌐 浏览器已关闭")
