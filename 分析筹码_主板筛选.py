#!/usr/bin/env python3
"""
A股集合竞价 · 抢筹/出货分析工具 v3
支持: 股票代码 / 股票名称 / 股票截图图片

用法:
  python3 auction_analyzer.py 600519 000858              # 代码
  python3 auction_analyzer.py 贵州茅台 宁德时代           # 名称
  python3 auction_analyzer.py --image stock_screenshot.png # 图片
  python3 auction_analyzer.py --image photo.jpg 600519 比亚迪  # 混合
"""

import requests
import re
import json
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
import argparse
import html as html_module
import os
import sys
import concurrent.futures

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


# ============================================================
# 股票名称 → 代码 查询
# ============================================================

def search_stock_code(keyword: str) -> Optional[dict]:
    """
    通过腾讯/新浪接口模糊搜索股票名称，返回 {code, name, market}
    """
    # 腾讯智能提示接口
    # 格式: v_hint="sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A"
    try:
        r = requests.get(
            "https://smartbox.gtimg.cn/s3/",
            params={"v": "2", "q": keyword, "t": "gp"},
            headers=HEADERS, timeout=10,
        )
        text = r.content.decode("gbk", errors="replace")
        for line in text.strip().split("\n"):
            m = re.search(r'v_hint="(.+)"', line)
            if not m:
                continue
            raw = m.group(1)
            try:
                decoded = json.loads('"' + raw.replace('"', '\\"') + '"')
            except Exception:
                decoded = raw
            items = decoded.split(";")
            for item in items:
                parts = item.split("~")
                if len(parts) >= 3:
                    market = parts[0]
                    code = parts[1]
                    name = parts[2]
                    if code and code.isdigit() and len(code) == 6:
                        return {"code": code, "name": name, "market": market}
    except Exception as e:
        print(f"  ⚠ 腾讯搜索接口异常: {e}")

    # 新浪搜索接口（备用）
    try:
        r = requests.get(
            "https://suggest3.sinajs.cn/suggest/key=" + keyword,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
            timeout=10,
        )
        r.encoding = "utf-8"
        # 格式: var suggest=",,,,,,,gp1600519,贵州茅台,gp000858,五粮液,..."
        m = re.search(r'"(.+)"', r.text)
        if m:
            parts = m.group(1).split(",")
            for i in range(0, len(parts) - 1, 2):
                tag = parts[i]
                name = parts[i + 1]
                if tag.startswith("gp"):
                    code = tag[2:]
                    return {"code": code, "name": name, "market": "sh" if code.startswith(("6", "9")) else "sz"}
    except Exception as e:
        print(f"  ⚠ 新浪搜索接口异常: {e}")

    return None


def resolve_stock_input(text: str) -> Optional[dict]:
    """
    解析输入: 自动判断是代码还是名称
    返回 {code, name}
    """
    text = text.strip()
    if not text:
        return None

    # 纯数字 → 当作代码
    if text.isdigit() and len(text) == 6:
        # 查一下名字
        result = search_stock_code(text)
        if result:
            return {"code": result["code"], "name": result["name"]}
        return {"code": text, "name": text}

    # 含中文 → 当作名称搜索
    result = search_stock_code(text)
    if result:
        return {"code": result["code"], "name": result["name"]}

    print(f"  ⚠ 无法识别: {text}")
    return None


# ============================================================
# 图片OCR提取股票
# ============================================================

def _get_mimo_api_key() -> str:
    """从环境变量获取 MiMo API Key"""
    return os.environ.get("MIMO_API_KEY", "")


def extract_stocks_from_image(image_path: str) -> list[str]:
    """
    识别图片中的股票名称或代码
    优先用 MiMo API（需 key），无 key 时用本地 OCR（无需 key）
    """
    # 方案1: MiMo API（更智能，能理解上下文）
    api_key = _get_mimo_api_key()
    if api_key:
        keywords = _ocr_via_mimo(image_path, api_key)
        if keywords:
            return keywords

    # 方案2: 本地 OCR（无需 API Key）
    keywords = _ocr_via_local(image_path)
    if keywords:
        return keywords

    print("  ✗ 无法进行图片识别，请手动输入股票名称")
    return []


def _ocr_via_mimo(image_path: str, api_key: str) -> list[str]:
    """用 MiMo 多模态 API 识别"""
    prompt = (
        "请识别这张图片中的所有A股股票名称或代码。"
        "只输出股票名称或代码，每行一个，不要其他内容。"
        "例如：贵州茅台 或 600519"
    )
    api_url = os.environ.get("MIMO_API_ENDPOINT", "https://api.xiaomimimo.com/v1/chat/completions")
    model = os.environ.get("MIMO_OMNI_MODEL", "clawm-alpha")
    try:
        import base64
        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
        mime = mime_map.get(ext, "image/jpeg")
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        image_url = f"data:{mime};base64,{b64}"

        body = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ]}],
            "max_tokens": 4096,
        }
        resp = requests.post(api_url, headers={"api-key": api_key, "Content-Type": "application/json"},
                             json=body, timeout=120)
        if resp.status_code == 200:
            text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            if text.strip():
                lines = text.strip().split("\n")
                keywords = [l.strip().lstrip("0123456789.、- ").strip() for l in lines if l.strip()]
                keywords = [k for k in keywords if k]
                if keywords:
                    print(f"  ✓ MiMo API 识别到 {len(keywords)} 个关键词")
                    return keywords
        else:
            print(f"  ⚠ MiMo API 失败: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  ⚠ MiMo API 异常: {e}")
    return []


def _ocr_via_local(image_path: str) -> list[str]:
    """用本地 OCR 识别（rapidocr-onnxruntime，无需 API Key）"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        print("  ✗ 未安装本地 OCR 库，请手动运行: pip install rapidocr-onnxruntime")
        return []

    try:
        print("  📷 使用本地 OCR 识别中...")
        ocr = RapidOCR()
        result, _ = ocr(image_path)
        if not result:
            print("  ⚠ OCR 未识别到文字")
            return []

        # 拼接所有识别到的文本
        all_text = " ".join([item[1] for item in result])
        print(f"  OCR 原文: {all_text[:120]}...")

        # 从文本中提取股票名称或6位代码
        keywords = []
        # 提取6位数字代码
        codes = re.findall(r'\b(\d{6})\b', all_text)
        keywords.extend(codes)
        # 提取中文股票名称（2-4个汉字，排除常见非股票词）
        exclude = {"集合竞价", "抢筹", "出货", "股票", "代码", "名称", "涨跌", "成交量",
                    "换手率", "振幅", "量比", "今开", "昨收", "最新", "买入", "卖出",
                    "时间", "序号", "类型", "市场", "行业", "板块", "概念", "自选"}
        names = re.findall(r'[\u4e00-\u9fa5]{2,4}', all_text)
        for name in names:
            if name not in exclude and name not in keywords:
                keywords.append(name)

        if keywords:
            print(f"  ✓ 本地 OCR 识别到 {len(keywords)} 个关键词")
        return keywords
    except Exception as e:
        print(f"  ⚠ 本地 OCR 异常: {e}")
    return []


# ============================================================
# 行情数据获取
# ============================================================

def _to_tencent(code: str) -> str:
    return f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"


def fetch_quotes(codes: list[str]) -> dict:
    """批量获取腾讯实时行情"""
    symbols = ",".join(_to_tencent(c) for c in codes)
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={symbols}", headers=HEADERS, timeout=15)
        r.encoding = "gbk"
    except Exception as e:
        print(f"  ⚠ 行情请求失败: {e}")
        return {}

    results = {}
    for line in r.text.strip().split("\n"):
        m = re.search(r'v_\w+="(.+)"', line)
        if not m:
            continue
        f = m.group(1).split("~")
        if len(f) < 50:
            continue
        code = f[2]

        def _v(i, t=float):
            try:
                return t(f[i]) if f[i] else (t(0) if t != str else "")
            except (ValueError, IndexError):
                return t(0) if t != str else ""

        results[code] = {
            "name": f[1], "code": code,
            "price": _v(3), "prev_close": _v(4), "open": _v(5),
            "volume": _v(6, int), "buy_vol": _v(7, int), "sell_vol": _v(8, int),
            "bid1_p": _v(9), "bid1_v": _v(10, int),
            "ask1_p": _v(19), "ask1_v": _v(20, int),
            "change_pct": _v(32), "high": _v(33), "low": _v(34),
            "amount": _v(37),          # 腾讯接口返回单位：万元
            "turnover": _v(38),
            "amplitude": _v(43),
        }
    return results


def fetch_hist(code: str, days: int = 10) -> list[dict]:
    """获取近期日K线（带重试）"""
    # 尝试新浪接口（带重试）
    for attempt in range(2):
        try:
            r = requests.get(
                "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                params={"symbol": _to_tencent(code), "scale": "240", "ma": "no", "datalen": days},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
                timeout=15,
            )
            if r.text and r.text.strip():
                data = json.loads(r.text)
                if data:
                    return data
        except Exception:
            pass
        if attempt == 0:
            import time; time.sleep(0.3)

    # 备用：腾讯接口
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{sym},day,{start},{end},{days},qfq", "_var": "kline_dayqfq"},
            headers=HEADERS, timeout=15,
        )
        txt = r.text.split("=", 1)[1] if "=" in r.text else r.text
        data = json.loads(txt)
        klines = data.get("data", {}).get(sym, {})
        klines = klines.get("day") or klines.get("qfqday") or []
        return [{"day": k[0], "open": k[1], "close": k[2], "high": k[3], "low": k[4], "volume": k[5]} for k in klines]
    except Exception as e:
        print(f"  ⚠ 腾讯K线接口异常({code}): {e}")
    return []


# ============================================================
# 分析逻辑
# ============================================================

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
    bv = quote["buy_vol"]
    sv = quote["sell_vol"]
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
    if net > 15:
        v = "真实抢筹"
    elif net < -10:
        v = "疑似出货"
    else:
        v = "正常"

    return AuctionResult(
        code=code, name=quote["name"],
        prev_close=pc, open_price=op, price=quote["price"],
        volume=vol, amount=amt, change_pct=chg,
        volume_ratio=round(vr, 2), open_gap=round(gap, 2),
        amplitude=amp, turnover=to, buy_ratio=round(br, 1),
        buy_vol=bv, sell_vol=sv,
        bull_score=bs, bear_score=rs, verdict=v, signals=sigs,
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


def save_html(results: list[AuctionResult], path: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 按涨幅降序
    sorted_results = sorted(results, key=lambda x: x.change_pct, reverse=True)

    rows = ""
    for i, r in enumerate(sorted_results, 1):
        chg_color = "#f85149" if r.change_pct > 0 else ("#3fb950" if r.change_pct < 0 else "#c9d1d9")
        chg_str = f"{r.change_pct:+.2f}%" if r.change_pct != 999 else "涨停"

        # 筹码判断颜色
        if r.verdict == "真实抢筹":
            v_color = "#f85149"
            v_bg = "rgba(248,81,73,.12)"
        elif r.verdict == "疑似出货":
            v_color = "#3fb950"
            v_bg = "rgba(63,185,80,.12)"
        else:
            v_color = "#d29922"
            v_bg = "rgba(210,153,34,.12)"

        freq = len([s for s in r.signals if any(k in s for k in ["高开", "低开", "放量", "缩量", "买盘", "卖盘", "连涨", "连跌", "平开"])])
        strategy = _compute_strategy(r)
        vol_fmt = _format_volume(r.volume)

        # 竞昨比（今开 vs 昨收）
        if r.prev_close > 0:
            comp_ratio = f"{(r.open_price - r.prev_close) / r.prev_close * 100:.1f}%"
        else:
            comp_ratio = "-"

        rows += f"""<tr>
<td>{html_module.escape(r.code)}</td>
<td style="text-align:left;font-weight:600">{html_module.escape(r.name)}</td>
<td>{r.open_price:.2f}</td>
<td>{r.price:.2f}</td>
<td>{vol_fmt}</td>
<td>{comp_ratio}</td>
<td>{r.buy_ratio:.1f}%</td>
<td style="color:{chg_color}">{chg_str}</td>
<td style="color:{v_color};background:{v_bg};border-radius:4px;font-weight:600">{r.verdict}</td>
<td>{freq}</td>
<td style="text-align:left;font-size:12px">{strategy}</td>
</tr>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>集合竞价 - 股票筛选</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#0d1117;color:#c9d1d9;padding:16px}}
.hd{{text-align:center;padding:16px 0}}
.hd h1{{font-size:20px;color:#58a6ff}}
.hd .t{{color:#8b949e;font-size:12px;margin-top:4px}}
.tbl-wrap{{overflow-x:auto;margin:0 auto;max-width:1100px}}
table{{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}}
th{{background:#161b22;color:#8b949e;font-weight:600;padding:8px 10px;text-align:center;border-bottom:2px solid #30363d;position:sticky;top:0}}
td{{padding:6px 10px;text-align:center;border-bottom:1px solid #21262d}}
tr:hover{{background:#161b22}}
.ft{{text-align:center;color:#484f58;font-size:10px;padding:20px 0}}
@media(max-width:768px){{table{{font-size:11px}}th,td{{padding:4px 6px}}}}
</style></head><body>
<div class="hd"><h1>📊 集合竞价 - 股票筛选</h1><div class="t">更新时间: {now}</div></div>
<div class="tbl-wrap">
<table>
<thead><tr>
<th>代码</th><th>名称</th><th>09:25</th><th>09:26</th><th>搓合量</th>
<th>竞昨比</th><th>剩余率</th><th>09:26涨幅</th>
<th>筹码判断</th><th>频次</th><th>策略</th>
</tr></thead>
<tbody>{rows}</tbody>
</table></div>
<div class="ft">⚠️ 仅供学习参考，不构成投资建议</div></body></html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.abspath(path)


# ============================================================
# 核心接口
# ============================================================

def run(codes: list[str], html_path: str = None) -> list[AuctionResult]:
    """
    分析指定股票代码列表
    """
    quotes = fetch_quotes(codes)
    if not quotes:
        return []
    results = []
    for code in codes:
        if code not in quotes:
            continue
        hist = fetch_hist(code, days=10)
        r = analyze(code, quotes[code], hist)
        if r:
            results.append(r)
    if html_path and results:
        save_html(results, html_path)
    return results


def run_by_names(names: list[str], html_path: str = None) -> list[AuctionResult]:
    """
    通过股票名称查询代码并分析
    """
    print("🔍 正在查询股票代码...")
    codes = []
    name_map = {}
    for name in names:
        info = resolve_stock_input(name)
        if info:
            codes.append(info["code"])
            name_map[info["code"]] = info["name"]
            print(f"  ✓ {name} → {info['code']} ({info['name']})")
        else:
            print(f"  ✗ {name} → 未找到")

    if not codes:
        return []

    results = run(codes, html_path=html_path)
    # 补回名称映射
    for r in results:
        if r.code in name_map:
            r.name = name_map[r.code]
    return results


def run_by_image(image_path: str, html_path: str = None) -> list[AuctionResult]:
    """
    从截图中识别股票并分析
    """
    if not os.path.exists(image_path):
        print(f"❌ 图片不存在: {image_path}")
        return []

    print(f"📸 正在识别图片: {image_path}")
    keywords = extract_stocks_from_image(image_path)
    if not keywords:
        print("  ✗ 未能从图片中识别出股票")
        return []

    print(f"  识别到 {len(keywords)} 个关键词: {', '.join(keywords)}")
    return run_by_names(keywords, html_path=html_path)


# ============================================================
# CLI
# ============================================================

# ============================================================
# 主板筛选策略
# ============================================================

def fetch_market_indices() -> list[dict]:
    """
    获取大盘指数实时行情
    返回: [{name, code, price, change_pct, amount, volume, amplitude}, ...]
    """
    index_map = [
        ("上证指数", "sh000001"),
        ("深证成指", "sz399001"),
        ("创业板指", "sz399006"),
    ]
    results = []
    for name, symbol in index_map:
        try:
            r = requests.get(
                f"https://qt.gtimg.cn/q={symbol}",
                headers=HEADERS, timeout=10,
            )
            r.encoding = "gbk"
            m = re.search(r'v_\w+="(.+)"', r.text)
            if m:
                f = m.group(1).split("~")
                if len(f) > 38:
                    results.append({
                        "name": name,
                        "code": symbol,
                        "price": float(f[3]) if f[3] else 0,
                        "prev_close": float(f[4]) if f[4] else 0,
                        "change_pct": float(f[32]) if f[32] else 0,
                        "amount": float(f[37]) if f[37] else 0,       # 万元
                        "volume": int(f[36]) if f[36] else 0,         # 手
                        "amplitude": float(f[43]) if f[43] else 0,
                    })
        except Exception as e:
            print(f"  ⚠ 获取{name}失败: {e}")
    return results


def fetch_all_ashare_codes() -> list[str]:
    """
    获取全部A股代码列表（沪深主板+创业板+科创板）
    使用新浪接口分页获取
    """
    codes = []
    page = 1
    while True:
        try:
            r = requests.get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                params={
                    "page": str(page), "num": "1000", "sort": "symbol",
                    "asc": "1", "node": "hs_a", "symbol": "", "_s_r_a": "page",
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://finance.sina.com.cn/",
                },
                timeout=20,
            )
            data = json.loads(r.text)
            if not data:
                break
            for item in data:
                code = str(item.get("code", ""))
                symbol = str(item.get("symbol", ""))
                if code and len(code) == 6 and code.isdigit():
                    codes.append(code)
                elif symbol:
                    # symbol格式: sh600519 或 sz000001
                    c = symbol[2:] if len(symbol) > 2 else ""
                    if c and len(c) == 6 and c.isdigit():
                        codes.append(c)
            if len(data) < 1000:
                break
            page += 1
        except Exception as e:
            print(f"  ⚠ 新浪接口第{page}页失败: {e}")
            break
    return codes


def _is_mainboard_a(code: str) -> bool:
    """判断是否为沪深主板A股（排除北交所等）"""
    return code.startswith(("60", "00", "30", "68"))


def _fetch_kline_concurrent(codes: list[str], days: int = 5) -> dict:
    """
    并发获取多只股票的日K线
    返回: {code: [kline_data]}
    """
    result = {}

    def _fetch_one(code):
        try:
            return code, fetch_hist(code, days=days)
        except Exception:
            return code, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_fetch_one, c) for c in codes]
        for future in concurrent.futures.as_completed(futures):
            code, hist = future.result()
            result[code] = hist
    return result


def _check_has_limit_up_in_days(code: str, days: int = 120, cached_klines: list[dict] = None) -> bool:
    """
    检查股票在最近N个交易日内是否有涨停记录
    通过逐日K线检查涨幅是否接近涨停阈值
    """
    try:
        klines = cached_klines if cached_klines is not None else fetch_hist(code, days=days + 30)
        if not klines or len(klines) < 5:
            return False
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


def fetch_kline_120min(code: str, count: int = 60) -> list[dict]:
    """
    获取120分钟K线数据（腾讯接口）
    """
    sym = _to_tencent(code)
    try:
        r = requests.get(
            f"https://web.ifzq.gtimg.cn/appstock/app/kline/mkline",
            params={"param": f"{sym},m120,,{count}", "_var": "kline_m120"},
            headers=HEADERS, timeout=15,
        )
        txt = r.text.split("=", 1)[1] if "=" in r.text else r.text
        data = json.loads(txt)
        klines = data.get("data", {}).get(sym, {}).get("m120", [])
        if klines:
            return [{"day": k[0], "open": k[1], "close": k[2], "high": k[3], "low": k[4], "volume": k[5]} for k in klines]
    except Exception as e:
        print(f"  ⚠ 腾讯120分钟K线异常({code}): {e}")

    # 备用：新浪接口
    try:
        r = requests.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": sym, "scale": "120", "ma": "no", "datalen": count},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
            timeout=15,
        )
        data = json.loads(r.text)
        if data:
            return data
    except Exception as e:
        print(f"  ⚠ 新浪120分钟K线异常({code}): {e}")
    return []


def _check_macd_120min_up(code: str) -> bool:
    """
    检查120分钟MACD是否向上（MACD线当前 > 前一根 且 MACD > 0）
    """
    klines = fetch_kline_120min(code, count=60)
    if not klines or len(klines) < 35:
        return False
    closes = [float(k.get("close", 0)) for k in klines if float(k.get("close", 0)) > 0]
    if len(closes) < 35:
        return False
    macd_line, _, _ = _compute_macd(closes)
    if len(macd_line) < 2:
        return False
    # MACD线向上：当前 > 前一根，且 MACD > 0
    return macd_line[-1] > macd_line[-2] and macd_line[-1] > 0


def _check_weekly_macd_red_growing(code: str, cached_klines: list[dict] = None) -> bool:
    """
    检查周MACD红柱变大
    用日K线按真实日历周聚合为周K线，计算MACD，检查最近一根红柱 > 前一根
    """
    klines = cached_klines if cached_klines is not None else fetch_hist(code, days=1000)
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
    klines = cached_klines if cached_klines is not None else fetch_hist(code, days=1000)
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


def _screen_unified(candidates: list[dict], tencent_map: dict) -> list[dict]:
    """
    统一筛选策略 —— 以下全部条件必须同时满足才能入选：
      1.  主板
      2.  非ST
      3.  集合竞价涨幅 > 3% 且 < 10%
      4.  市值 < 400亿
      5.  价格 < 120元
      6.  前一日涨停取反（昨日未涨停）
      7.  非盘中下跌（竞价价 >= 昨收）
      8.  开盘跳空高开（open > prev_close）
      9.  今日竞价金额 / 昨日竞价金额 > 1.5倍
      10. 集合竞价换手率 > 0.11%
      11. 集合竞价量比 > 5
      12. 3日涨幅 < 15%
      13. 集合竞价现手量 > 40000手
      14. 2个月内有过涨停
    """
    filtered = []
    _diag = {}  # 诊断计数器
    for c in candidates:
        code = c["code"]
        tc = tencent_map.get(code, {})

        # ---- 条件3: 集合竞价涨幅 > 3% 且 < 10% ----
        if c["auction_gain"] <= 3 or c["auction_gain"] >= 10:
            _diag["涨幅3-10%"] = _diag.get("涨幅3-10%", 0) + 1
            continue

        # ---- 条件4: 市值 < 400亿 ----
        if c["market_cap_yi"] >= 400:
            _diag["市值<400亿"] = _diag.get("市值<400亿", 0) + 1
            continue

        # ---- 条件5: 价格 < 120元 ----
        if c["price"] >= 120:
            _diag["价格<120"] = _diag.get("价格<120", 0) + 1
            continue

        # ---- 条件8: 开盘跳空高开 ----
        if c["open_price"] <= c["prev_close"]:
            _diag["高开"] = _diag.get("高开", 0) + 1
            continue

        # ---- 竞价量 ----
        auction_vol = tc.get("volume", c["volume"])
        if auction_vol <= 0:
            _diag["竞价量>0"] = _diag.get("竞价量>0", 0) + 1
            continue

        # ---- 条件13: 集合竞价现手量 > 40000手 ----
        if auction_vol <= 40000:
            _diag["竞价量>4万手"] = _diag.get("竞价量>4万手", 0) + 1
            continue

        # ---- 昨成交量（股→手）----
        yesterday_vol_shares = c["volume_shares"]
        yesterday_vol_lots = yesterday_vol_shares // 100
        if yesterday_vol_lots <= 0:
            _diag["昨成交量>0"] = _diag.get("昨成交量>0", 0) + 1
            continue

        # ---- 条件9: 今日竞价金额/昨日竞价金额 > 1.5倍 ----
        today_auction_amount = auction_vol * c["price"] * 100      # 手→股 × 价格
        yesterday_auction_amount = yesterday_vol_lots * c["prev_close"] * 100
        if yesterday_auction_amount <= 0:
            _diag["金额比"] = _diag.get("金额比", 0) + 1
            continue
        amount_ratio = today_auction_amount / yesterday_auction_amount
        if amount_ratio <= 1.5:
            _diag["金额比>1.5"] = _diag.get("金额比>1.5", 0) + 1
            continue

        # ---- 条件10: 集合竞价换手率 > 0.11% ----
        turnover = tc.get("turnover", 0)
        if turnover <= 0:
            turnover = c.get("turnover_sina", 0)
        if turnover <= 0.11:
            _diag["换手率>0.11%"] = _diag.get("换手率>0.11%", 0) + 1
            continue

        # ---- 条件11: 集合竞价量比 > 5 ----
        volume_ratio = tc.get("volume_ratio_api", 0)
        if volume_ratio <= 0:
            volume_ratio = auction_vol / yesterday_vol_lots if yesterday_vol_lots > 0 else 0
        if volume_ratio <= 5:
            _diag["量比>5"] = _diag.get("量比>5", 0) + 1
            continue

        # ---- 通过全部检查，记录 ----
        c["auction_vol"] = auction_vol
        c["vol_ratio_yesterday"] = round(auction_vol / yesterday_vol_lots * 100, 2)
        c["volume_ratio"] = round(volume_ratio, 2)
        c["turnover"] = round(turnover, 4)
        c["yesterday_vol"] = yesterday_vol_lots
        c["amount_ratio"] = round(amount_ratio, 2)
        filtered.append(c)

    # 输出诊断信息
    if _diag:
        print(f"  📋 过滤诊断 (共淘汰 {sum(_diag.values())} 只):")
        for reason, cnt in sorted(_diag.items(), key=lambda x: -x[1]):
            print(f"    ❌ {reason}: {cnt} 只")

    return filtered


def _fetch_sectors_with_stocks() -> dict:
    """
    通过东方财富 push2test 接口获取行业板块列表及各板块下的股票
    返回: {sector_name: {"code": sector_code, "stocks": [stock_items]}}
    """
    EM_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/center/boardlist.html",
    }
    sectors = {}

    # 第一步：获取行业板块列表（涨停数倒序，取前50个板块）
    try:
        r = requests.get(
            "https://push2test.eastmoney.com/api/qt/clist/get",
            params={
                "cb": "jQuery", "pn": "1", "pz": "50", "po": "1", "np": "1",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": "2", "invt": "2", "fid": "f3",
                "fs": "m:90+t:2+f:!50",
                "fields": "f2,f3,f12,f14,f104,f105",
            },
            headers=EM_HEADERS, timeout=15,
        )
        m = re.search(r"jQuery\((.+)\);", r.text)
        if m:
            data = json.loads(m.group(1))
            items = data.get("data", {}).get("diff", [])
            for item in items:
                scode = item.get("f12", "")
                sname = item.get("f14", "")
                limit_up = item.get("f104", 0)
                limit_down = item.get("f105", 0)
                change_pct = item.get("f3", 0)
                if scode and sname:
                    sectors[sname] = {
                        "code": scode,
                        "stocks": [],
                        "limit_up": limit_up,
                        "limit_down": limit_down,
                        "change_pct": change_pct,
                    }
    except Exception as e:
        print(f"  ⚠ 东方财富板块列表获取失败: {e}")
        return sectors

    if not sectors:
        return sectors

    # 第二步：并发获取每个板块的成分股
    def _fetch_sector_stocks(sname, scode):
        try:
            r = requests.get(
                "https://push2test.eastmoney.com/api/qt/clist/get",
                params={
                    "cb": "jQuery", "pn": "1", "pz": "1000", "po": "1", "np": "1",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": "2", "invt": "2", "fid": "f3",
                    "fs": f"b:{scode}",
                    "fields": "f2,f3,f12,f14",
                },
                headers=EM_HEADERS, timeout=15,
            )
            m = re.search(r"jQuery\((.+)\);", r.text)
            if m:
                data = json.loads(m.group(1))
                items = data.get("data", {}).get("diff", [])
                stocks = []
                for it in items:
                    code = str(it.get("f12", ""))
                    name = it.get("f14", "")
                    price = it.get("f2", 0)
                    change_pct = it.get("f3", 0)
                    if code and len(code) == 6 and code[0].isdigit():
                        stocks.append({
                            "code": code, "name": name,
                            "symbol": ("sh" if code.startswith(("6", "9")) else "sz") + code,
                            "trade": str(price), "settlement": "0",
                            "changepercent": str(change_pct),
                        })
                return sname, stocks
        except Exception:
            pass
        return sname, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_fetch_sector_stocks, sn, sectors[sn]["code"]) for sn in sectors]
        for future in concurrent.futures.as_completed(futures):
            sname, stocks = future.result()
            sectors[sname]["stocks"] = stocks

    return sectors

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


def screen_mainboard_strategy() -> list[dict]:
    """
    板块优先统一筛选策略（东方财富 push2test 接口）：
    1. 实时分析大盘哪个板块涨停最多
    2. 优先在涨停最多的板块里筛选
    3. 结果按板块涨停数 + 频次排列

    统一筛选条件（全部满足才能入选）：
      1.  主板  2.  非ST  3.  涨幅>3%且<10%  4.  市值<400亿
      5.  价格<120元  6.  前一日涨停取反  7.  非盘中下跌
      8.  开盘跳空高开  9.  竞价金额比>1.5倍  10. 换手率>0.11%
      11. 量比>5  12. 3日涨幅<15%  13. 现手量>40000手
      14. 2个月内有涨停  15. 120分钟MACD向上
      16. 月MACD红柱向上  17. 周MACD红柱变大
    """
    # ========== 第一步：获取板块及股票 ==========
    # 检查是否在竞价时段
    now = datetime.now()
    h, m = now.hour, now.minute
    is_auction = (h == 9 and 15 <= m <= 25)
    if not is_auction:
        print(f"\n⚠️  当前 {h:02d}:{m:02d} 非集合竞价时段 (09:15-09:25)")
        print("   腾讯接口返回的是全天数据而非竞价数据，量比/换手率可能不符合竞价条件")
        print("   建议在 09:15-09:25 运行以获得准确的竞价筛选结果\n")

    print("\n📊 获取行业板块数据（东方财富）...")
    sectors = _fetch_sectors_with_stocks()
    if not sectors:
        print("  ⚠ 板块API不可用，回退到全市场筛选")
        return _screen_fallback_all_market()

    print(f"  共 {len(sectors)} 个行业板块")

    # ========== 第二步：统计每个板块的涨停数 ==========
    print("📊 统计各板块涨停数...")
    sector_limit_counts = {}
    sector_all_stocks = {}
    total_stocks = 0

    for sname, sdata in sectors.items():
        stocks = sdata.get("stocks", [])
        limit_count = sdata.get("limit_up", 0)
        if not stocks:
            continue

        valid_stocks = []
        for item in stocks:
            code = str(item.get("code", ""))
            name = str(item.get("name", ""))
            symbol = str(item.get("symbol", ""))
            if not code or len(code) != 6 or not code[0].isdigit():
                continue
            if symbol.startswith("bj"):
                continue
            if not code.startswith(("60", "00", "30", "68")):
                continue
            if "ST" in name.upper():
                continue
            valid_stocks.append(item)

        sector_all_stocks[sname] = valid_stocks
        sector_limit_counts[sname] = limit_count
        total_stocks += len(valid_stocks)

    sorted_sectors = sorted(sector_limit_counts.items(), key=lambda x: x[1], reverse=True)
    print(f"  板块涨停排名 (Top 10):")
    for i, (sname, cnt) in enumerate(sorted_sectors[:10], 1):
        print(f"    {i}. {sname}: {cnt} 只涨停")

    # ========== 第三步：按板块优先级逐板块筛选 ==========
    print(f"\n📊 按板块优先级筛选 ({total_stocks} 只股票)...")

    all_candidates = []

    for sname, limit_cnt in sorted_sectors:
        stocks = sector_all_stocks.get(sname, [])
        if not stocks:
            continue

        for item in stocks:
            code = str(item.get("code", ""))
            name = str(item.get("name", ""))
            symbol = str(item.get("symbol", ""))
            if not code or len(code) != 6:
                continue

            # 东方财富返回的涨跌幅是百分比
            change_pct = 0
            try:
                change_pct = float(item.get("changepercent", item.get("change_pct", 0)))
            except (ValueError, TypeError):
                pass

            # 价格
            try:
                price = float(item.get("trade", 0))
            except (ValueError, TypeError):
                continue

            # 东方财富成分股接口没有 settlement（昨收），需要从 trade 和 changepercent 反算
            if change_pct != 0 and price > 0:
                prev_close = price / (1 + change_pct / 100)
            else:
                prev_close = price  # fallback

            if prev_close <= 0 or price <= 0 or price >= 120:
                continue

            auction_gain = change_pct if change_pct != 0 else 0
            if auction_gain <= 3 or auction_gain >= 10:
                continue

            if price < prev_close * 0.999:  # 允许0.1%误差
                continue

            mktcap = 0  # 东方财富成分股接口没有市值字段，后续由腾讯接口补充

            cand = {
                "code": code, "name": name, "symbol": symbol,
                "price": price, "prev_close": round(prev_close, 2),
                "auction_gain": round(auction_gain, 2),
                "open_price": price,
                "volume_shares": 0,
                "volume": 0,
                "market_cap_yi": mktcap,
                "turnover_sina": 0,
                "sector": sname,
                "sector_limit_count": limit_cnt,
            }
            all_candidates.append(cand)

    print(f"  初筛候选: {len(all_candidates)} 只")

    if not all_candidates:
        return []

    # ========== 第四步：批量获取腾讯实时数据 ==========
    print("📊 获取竞价详细数据...")
    tencent_map = {}
    batch_size = 50
    for i in range(0, len(all_candidates), batch_size):
        batch = all_candidates[i:i + batch_size]
        symbols = ",".join(c["symbol"] for c in batch)
        try:
            r = requests.get(
                f"https://qt.gtimg.cn/q={symbols}",
                headers=HEADERS, timeout=15,
            )
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
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

                # 补充市值信息
                mktcap = _v(45)  # 万亿→亿
                for c in all_candidates:
                    if c["code"] == code and c["market_cap_yi"] <= 0:
                        c["market_cap_yi"] = round(mktcap, 2) if mktcap > 0 else 0

                tencent_map[code] = {
                    "volume": _v(6, int),
                    "buy_vol": _v(7, int),
                    "sell_vol": _v(8, int),
                    "amount": _v(37),
                    "turnover": _v(38),
                    "amplitude": _v(43),
                    "volume_ratio_api": _v(49),
                }
        except Exception as e:
            print(f"  ⚠ 腾讯接口批次{i//batch_size+1}失败: {e}")

    # ========== 第五步：获取K线数据（用于补充成交量 + 后续MACD检查）==========
    print(f"📊 获取K线数据（{len(all_candidates)} 只候选，1000天）...")
    kline_map_all = _fetch_kline_concurrent([c["code"] for c in all_candidates], days=1000)
    for c in all_candidates:
        klines = kline_map_all.get(c["code"], [])
        if klines and len(klines) >= 2:
            try:
                yesterday_vol = int(float(klines[-2].get("volume", 0)))
                if yesterday_vol > 0:
                    c["volume_shares"] = yesterday_vol
            except (ValueError, TypeError):
                pass
    vol_filled = sum(1 for c in all_candidates if c["volume_shares"] > 0)
    print(f"  ✅ 已补充昨日成交量: {vol_filled}/{len(all_candidates)} 只")

    # ========== 第六步：统一策略筛选 ==========
    print("📊 执行统一策略筛选...")
    filtered = _screen_unified(all_candidates, tencent_map)
    print(f"  通过筛选: {len(filtered)} 只")

    if not filtered:
        return []

    # ========== 第七步：K线 + MACD 检查（复用第五步数据）==========
    print("📊 执行K线+MACD检查...")

    final = []
    for c in filtered:
        code = c["code"]
        klines = kline_map_all.get(code, [])
        if klines and len(klines) >= 4:
            closes = [float(k.get("close", 0)) for k in klines[-4:]]
            if closes[0] > 0:
                change_3d = (closes[-1] - closes[0]) / closes[0] * 100
                if change_3d >= 15:
                    continue
                c["change_3d"] = round(change_3d, 2)
            else:
                c["change_3d"] = 0
            if len(klines) >= 2:
                c["yesterday_vol_hist"] = int(float(klines[-2].get("volume", 0)))
            else:
                c["yesterday_vol_hist"] = 0

            # 用K线数据修正昨收价
            if len(klines) >= 2:
                try:
                    real_prev_close = float(klines[-2].get("close", 0))
                    if real_prev_close > 0:
                        c["prev_close"] = real_prev_close
                        c["auction_gain"] = round((c["price"] - real_prev_close) / real_prev_close * 100, 2)
                except (ValueError, TypeError):
                    pass

            if code.startswith(("30", "68")):
                limit_pct = 20
            else:
                limit_pct = 10
            if len(klines) >= 3:
                try:
                    dby_close = float(klines[-3].get("close", 0))
                    y_close = float(klines[-2].get("close", 0))
                    if dby_close > 0:
                        y_chg = (y_close - dby_close) / dby_close * 100
                        if y_chg >= limit_pct * 0.95:
                            continue
                except (ValueError, TypeError):
                    pass
        else:
            c["change_3d"] = 0
            c["yesterday_vol_hist"] = 0

        if not _check_has_limit_up_in_days(code, days=60, cached_klines=klines):
            continue
        if not _check_macd_120min_up(code):
            continue
        if not _check_monthly_macd_red_up(code, cached_klines=klines):
            continue
        if not _check_weekly_macd_red_growing(code, cached_klines=klines):
            continue

        c["change_pct"] = c["auction_gain"]
        final.append(c)

    # ========== 第八步：计算展示字段 ==========
    for c in final:
        code = c["code"]
        tc = tencent_map.get(code, {})
        c["auction_price"] = c["open_price"]
        c["price_0926"] = c["price"]
        c["chg_0926"] = round((c["price_0926"] - c["prev_close"]) / c["prev_close"] * 100, 2) if c["prev_close"] > 0 else 0

        auction_vol = c.get("auction_vol", c["volume"])
        yesterday_vol = c.get("yesterday_vol_hist", 0) or c.get("yesterday_vol", 1)
        c["comp_ratio"] = round(auction_vol / yesterday_vol * 100, 1) if yesterday_vol > 0 else 0

        buy_vol = tc.get("buy_vol", 0)
        sell_vol = tc.get("sell_vol", 0)
        c["remaining_rate"] = round(buy_vol / (buy_vol + sell_vol) * 100, 1) if (buy_vol + sell_vol) > 0 else 50.0

        chg = c["auction_gain"]
        vr = c.get("volume_ratio", 0)
        rr = c["remaining_rate"]
        cr = c.get("comp_ratio", 0)

        if chg >= 9:
            c["verdict"] = "真实抢筹"
        else:
            score = 0
            if chg >= 7: score += 3
            elif chg >= 5: score += 2
            elif chg >= 3: score += 1
            if vr >= 10: score += 3
            elif vr >= 7: score += 2
            elif vr >= 5: score += 1
            if rr >= 65: score += 3
            elif rr >= 55: score += 1
            elif rr < 40: score -= 2
            elif rr < 45: score -= 1
            if cr >= 10: score += 2
            elif cr >= 5: score += 1
            c["verdict"] = "真实抢筹" if score >= 4 else ("疑似出货" if score <= 0 else "正常")

        freq = 0
        if vr >= 5: freq += 1
        if chg >= 5: freq += 1
        if rr >= 60: freq += 1
        if cr >= 3: freq += 1
        c["frequency"] = freq
        c["strategy"] = _compute_screen_strategy(c)

    # ========== 第九步：排序（板块涨停数降序 → 频次降序 → 涨幅降序）==========
    final.sort(key=lambda x: (-x.get("sector_limit_count", 0), -x.get("frequency", 0), -x["auction_gain"]))

    print(f"\n  ✅ 最终筛选: {len(final)} 只股票")

    # 输出板块分布统计
    sector_result = {}
    for c in final:
        sn = c.get("sector", "未知")
        sector_result[sn] = sector_result.get(sn, 0) + 1
    if sector_result:
        print("  📊 入选股票板块分布:")
        for sn, cnt in sorted(sector_result.items(), key=lambda x: -x[1]):
            lc = sector_limit_counts.get(sn, 0)
            print(f"    {sn}: {cnt} 只入选, {lc} 只涨停")

    # ========== 第十步：板块龙头识别 + 出现次数统计 ==========
    sector_groups = {}
    for c in final:
        sn = c.get("sector", "未知")
        sector_groups.setdefault(sn, []).append(c)

    leader_scores = {}
    for sn, stocks_in_sector in sector_groups.items():
        best_code = None
        best_score = -999
        for s in stocks_in_sector:
            chg = s.get("auction_gain", 0)
            vr = s.get("volume_ratio", 0)
            freq = s.get("frequency", 0)
            comp = chg * 3 + vr * 2 + freq * 10
            if comp > best_score:
                best_score = comp
                best_code = s["code"]
        if best_code:
            leader_scores[best_code] = best_score

    leader_freq_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leader_frequency.json")
    try:
        with open(leader_freq_path, "r", encoding="utf-8") as f:
            leader_freq_history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        leader_freq_history = {}

    today_str = datetime.now().strftime("%Y-%m-%d")
    for code in leader_scores:
        if code not in leader_freq_history:
            leader_freq_history[code] = {"count": 0, "dates": []}
        if today_str not in leader_freq_history[code]["dates"]:
            leader_freq_history[code]["count"] += 1
            leader_freq_history[code]["dates"].append(today_str)
            leader_freq_history[code]["dates"] = leader_freq_history[code]["dates"][-60:]

    try:
        with open(leader_freq_path, "w", encoding="utf-8") as f:
            json.dump(leader_freq_history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    for c in final:
        code = c["code"]
        c["is_leader"] = code in leader_scores
        c["leader_count"] = leader_freq_history.get(code, {}).get("count", 0)

    final.sort(key=lambda x: (-int(x.get("is_leader", False)), -x.get("leader_count", 0),
                               -x.get("sector_limit_count", 0), -x.get("frequency", 0)))

    leader_stats = [(c["code"], c["name"], c.get("sector", ""), c.get("leader_count", 0))
                    for c in final if c.get("is_leader")]
    if leader_stats:
        leader_stats.sort(key=lambda x: -x[3])
        print(f"\n  🏆 板块龙头出现次数统计:")
        for code, name, sector, cnt in leader_stats:
            print(f"    {name}({code}) [{sector}] — 出现 {cnt} 次")

    return final

def _screen_fallback_all_market() -> list[dict]:
    """
    板块API不可用时的回退方案：全市场筛选（保留兼容性）
    """
    print("📊 全市场回退筛选...")
    all_stocks = []
    page = 1
    while True:
        try:
            r = requests.get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                params={"page": str(page), "num": "80", "sort": "symbol",
                         "asc": "1", "node": "hs_a", "symbol": "", "_s_r_a": "page"},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
                timeout=20,
            )
            data = json.loads(r.text)
            if not data:
                break
            all_stocks.extend(data)
            if len(data) < 80:
                break
            page += 1
        except Exception as e:
            print(f"  ⚠ 第{page}页失败: {e}")
            break

    if not all_stocks:
        return []

    candidates = []
    for item in all_stocks:
        code = str(item.get("code", ""))
        name = str(item.get("name", ""))
        symbol = str(item.get("symbol", ""))
        if not code or len(code) != 6:
            continue
        if symbol.startswith("bj") or not code.startswith(("60", "00", "30", "68")):
            continue
        if "ST" in name.upper():
            continue

        mktcap = item.get("mktcap", 0)
        try:
            mktcap = float(mktcap) if mktcap else 0
        except (ValueError, TypeError):
            continue
        if mktcap <= 0:
            continue
        market_cap_yi = mktcap / 10000
        if market_cap_yi >= 400:
            continue

        trade = item.get("trade", "0")
        settlement = item.get("settlement", "0")
        try:
            price = float(trade) if trade else 0
            prev_close = float(settlement) if settlement else 0
        except (ValueError, TypeError):
            continue
        if prev_close <= 0 or price <= 0 or price >= 120:
            continue

        auction_gain = (price - prev_close) / prev_close * 100
        if auction_gain <= 3 or auction_gain >= 10:
            continue

        open_price = item.get("open", "0")
        try:
            open_price = float(open_price) if open_price else price
        except (ValueError, TypeError):
            open_price = price
        if open_price < prev_close:
            continue

        volume = item.get("volume", 0)
        try:
            volume = int(volume) if volume else 0
        except (ValueError, TypeError):
            continue
        if volume <= 0:
            continue

        turnover = item.get("turnoverratio", "0")
        try:
            turnover = float(turnover) if turnover else 0
        except (ValueError, TypeError):
            turnover = 0

        candidates.append({
            "code": code, "name": name, "symbol": symbol,
            "price": price, "prev_close": prev_close,
            "auction_gain": round(auction_gain, 2),
            "open_price": open_price,
            "volume_shares": volume,
            "volume": volume // 100,
            "market_cap_yi": round(market_cap_yi, 2),
            "turnover_sina": turnover,
            "sector": _get_board_type(code),
            "sector_limit_count": 0,
        })

    if not candidates:
        return []

    tencent_map = {}
    batch_size = 50
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        symbols = ",".join(c["symbol"] for c in batch)
        try:
            r = requests.get(f"https://qt.gtimg.cn/q={symbols}", headers=HEADERS, timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
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
                tencent_map[code] = {"volume": _v(6, int), "buy_vol": _v(7, int), "sell_vol": _v(8, int),
                                      "amount": _v(37), "turnover": _v(38), "amplitude": _v(43), "volume_ratio_api": _v(49)}
        except Exception:
            pass

    filtered = _screen_unified(candidates, tencent_map)
    if not filtered:
        return []

    kline_map = _fetch_kline_concurrent([c["code"] for c in filtered], days=1000)
    final = []
    for c in filtered:
        code = c["code"]
        klines = kline_map.get(code, [])
        if klines and len(klines) >= 4:
            closes = [float(k.get("close", 0)) for k in klines[-4:]]
            if closes[0] > 0 and (closes[-1] - closes[0]) / closes[0] * 100 >= 15:
                continue
            c["change_3d"] = round((closes[-1] - closes[0]) / closes[0] * 100, 2) if closes[0] > 0 else 0
            c["yesterday_vol_hist"] = int(float(klines[-2].get("volume", 0))) if len(klines) >= 2 else 0
            if code.startswith(("30", "68")):
                lp = 20
            else:
                lp = 10
            if len(klines) >= 3:
                try:
                    dby = float(klines[-3].get("close", 0))
                    yc = float(klines[-2].get("close", 0))
                    if dby > 0 and (yc - dby) / dby * 100 >= lp * 0.95:
                        continue
                except (ValueError, TypeError):
                    pass
        else:
            c["change_3d"] = 0
            c["yesterday_vol_hist"] = 0

        if not _check_has_limit_up_in_days(code, days=60, cached_klines=klines):
            continue
        if not _check_macd_120min_up(code):
            continue
        if not _check_monthly_macd_red_up(code, cached_klines=klines):
            continue
        if not _check_weekly_macd_red_growing(code, cached_klines=klines):
            continue
        c["change_pct"] = c["auction_gain"]
        final.append(c)

    for c in final:
        tc = tencent_map.get(c["code"], {})
        c["auction_price"] = c["open_price"]
        c["price_0926"] = c["price"]
        c["chg_0926"] = round((c["price_0926"] - c["prev_close"]) / c["prev_close"] * 100, 2) if c["prev_close"] > 0 else 0
        av = c.get("auction_vol", c["volume"])
        yv = c.get("yesterday_vol_hist", 0) or c.get("yesterday_vol", 1)
        c["comp_ratio"] = round(av / yv * 100, 1) if yv > 0 else 0
        bv, sv = tc.get("buy_vol", 0), tc.get("sell_vol", 0)
        c["remaining_rate"] = round(bv / (bv + sv) * 100, 1) if (bv + sv) > 0 else 50.0
        chg, vr, rr, cr = c["auction_gain"], c.get("volume_ratio", 0), c["remaining_rate"], c.get("comp_ratio", 0)
        if chg >= 9: c["verdict"] = "真实抢筹"
        else:
            s = 0
            if chg >= 7: s += 3
            elif chg >= 5: s += 2
            elif chg >= 3: s += 1
            if vr >= 10: s += 3
            elif vr >= 7: s += 2
            elif vr >= 5: s += 1
            if rr >= 65: s += 3
            elif rr >= 55: s += 1
            elif rr < 40: s -= 2
            elif rr < 45: s -= 1
            if cr >= 10: s += 2
            elif cr >= 5: s += 1
            c["verdict"] = "真实抢筹" if s >= 4 else ("疑似出货" if s <= 0 else "正常")
        freq = (1 if vr >= 5 else 0) + (1 if chg >= 5 else 0) + (1 if rr >= 60 else 0) + (1 if cr >= 3 else 0)
        c["frequency"] = freq
        c["strategy"] = _compute_screen_strategy(c)

    # 按板块类型分组，识别龙头
    board_groups = {}
    for c in final:
        bt = c.get("sector", _get_board_type(c["code"]))
        c["sector"] = bt
        board_groups.setdefault(bt, []).append(c)

    leader_scores = {}
    for bt, stocks_in_board in board_groups.items():
        best_code = None
        best_score = -999
        for s in stocks_in_board:
            chg = s.get("auction_gain", 0)
            vr = s.get("volume_ratio", 0)
            freq = s.get("frequency", 0)
            comp = chg * 3 + vr * 2 + freq * 10
            if comp > best_score:
                best_score = comp
                best_code = s["code"]
        if best_code:
            leader_scores[best_code] = best_score

    # 读取历史龙头出现次数
    leader_freq_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leader_frequency.json")
    try:
        with open(leader_freq_path, "r", encoding="utf-8") as f:
            leader_freq_history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        leader_freq_history = {}

    today_str = datetime.now().strftime("%Y-%m-%d")
    for code in leader_scores:
        if code not in leader_freq_history:
            leader_freq_history[code] = {"count": 0, "dates": []}
        if today_str not in leader_freq_history[code]["dates"]:
            leader_freq_history[code]["count"] += 1
            leader_freq_history[code]["dates"].append(today_str)
            leader_freq_history[code]["dates"] = leader_freq_history[code]["dates"][-60:]

    try:
        with open(leader_freq_path, "w", encoding="utf-8") as f:
            json.dump(leader_freq_history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    for c in final:
        code = c["code"]
        c["is_leader"] = code in leader_scores
        c["leader_count"] = leader_freq_history.get(code, {}).get("count", 0)

    final.sort(key=lambda x: (-int(x.get("is_leader", False)), -x.get("leader_count", 0),
                               -x.get("frequency", 0), -x["auction_gain"]))

    # 输出龙头统计
    leader_stats = [(c["code"], c["name"], c.get("sector", ""), c.get("leader_count", 0))
                    for c in final if c.get("is_leader")]
    if leader_stats:
        leader_stats.sort(key=lambda x: -x[3])
        print(f"\n  🏆 板块龙头出现次数统计:")
        for code, name, sector, cnt in leader_stats:
            print(f"    {name}({code}) [{sector}] — 出现 {cnt} 次")

    return final


def _compute_screen_strategy(s: dict) -> str:
    """根据筛选结果推断策略（阈值考虑筛选前提：chg>1%, vr>3）"""
    chg = s.get("auction_gain", 0)
    vr = s.get("volume_ratio", 0)
    rr = s.get("remaining_rate", 50)

    if chg >= 9.5:
        return "一字板/涨停"
    if chg >= 7 and vr >= 5:
        return "5w首板、新首板"
    if chg >= 5 and vr >= 5 and rr >= 55:
        return "5w首板、新首板"
    if chg >= 5:
        return "新首板"
    if chg >= 3 and vr >= 5 and rr >= 55:
        return "新首板"
    if chg >= 3:
        return "四万首板"
    return "三万首板"


def save_screen_html(stocks: list[dict], indices: list[dict], path: str) -> str:
    """保存主板筛选策略HTML报告（与截图一致的格式）"""
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

        rows += f"""<tr>
<td>{html_module.escape(s["code"])}</td>
<td style="text-align:left;font-weight:600">{html_module.escape(s["name"])}</td>
<td style="text-align:left;font-size:12px">{html_module.escape(sector)}<span style="color:#8b949e;font-size:10px">({sector_lc}涨停)</span></td>
<td style="color:{leader_color};font-weight:{'700' if is_leader else '400'}">{leader_mark}{leader_count}</td>
<td>{s["auction_price"]:.2f}</td>
<td>{s.get("price_0926", s["price"]):.2f}</td>
<td>{vol_fmt}</td>
<td>{comp_ratio:.1f}%</td>
<td>{remaining:.1f}%</td>
<td style="color:{chg_color};font-weight:600">{chg_0926:+.2f}%</td>
<td style="color:{v_color};background:{v_bg};border-radius:4px;font-weight:600;padding:4px 8px">{verdict}</td>
<td>{freq}</td>
<td style="text-align:left;font-size:12px">{strategy}</td>
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
    leader_data = [(s["code"], s["name"], s.get("sector", ""), s.get("leader_count", 0))
                   for s in stocks if s.get("is_leader")]
    if leader_data:
        leader_data.sort(key=lambda x: -x[3])
        leader_cells = ""
        for code, name, sector, cnt in leader_data:
            leader_cells += f'<span style="background:#161b22;border:1px solid #f0883e;border-radius:4px;padding:4px 8px;margin:2px;display:inline-block;font-size:12px">🏆 {html_module.escape(name)}({html_module.escape(code)}) <span style="color:#f0883e;font-weight:700">{cnt}次</span> <span style="color:#8b949e;font-size:10px">{html_module.escape(sector)}</span></span> '
        leader_html = f'<div style="text-align:center;margin-bottom:12px;padding:8px;background:rgba(240,136,62,.08);border-radius:8px"><div style="color:#f0883e;font-size:13px;font-weight:600;margin-bottom:6px">🏆 板块龙头出现次数统计</div>{leader_cells}</div>'

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>集合竞价 - 板块优先筛选</title>
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
<div class="hd"><h1>📊 集合竞价 - 板块优先筛选</h1><div class="t">更新时间: {now}</div></div>
{idx_html}
{leader_html}
{sector_html}
<div class="tbl-wrap">
<table>
<thead><tr>
<th>代码</th><th>名称</th><th>板块(涨停数)</th><th>龙头次数</th><th>09:25</th><th>09:26</th><th>搓合量</th>
<th>竞昨比</th><th>剩余率</th><th>09:26涨幅</th>
<th>筹码判断</th><th>频次</th><th>策略</th>
</tr></thead>
<tbody>{rows}</tbody>
</table></div>
<div class="ft">⚠️ 仅供学习参考，不构成投资建议</div>
</body></html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.abspath(path)


def main():
    parser = argparse.ArgumentParser(
        description="A股集合竞价 抢筹/出货分析",
        epilog="示例:\n"
               "  python3 auction_analyzer.py 600519 000858\n"
               "  python3 auction_analyzer.py 贵州茅台 宁德时代\n"
               "  python3 auction_analyzer.py --image stock.png\n"
               "  python3 auction_analyzer.py --image stock.png 600519 比亚迪",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", help="股票代码或名称（可多个）")
    parser.add_argument("--image", "-i", help="股票截图图片路径")
    parser.add_argument("--html", "-o", default="auction_report.html", help="HTML报告保存路径")
    parser.add_argument("--screen", "-s", action="store_true", default=True, help="主板筛选策略模式（默认开启）")
    parser.add_argument("--no-screen", dest="screen", action="store_false", help="关闭主板筛选，使用传统分析模式")
    parser.add_argument("--quiet", "-q", action="store_true", help="静默模式")
    args = parser.parse_args()

    # 没有指定股票/图片且没关闭screen → 自动进入筛选模式
    if not args.inputs and not args.image:
        args.screen = True

    print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ---- 主板筛选策略模式 ----
    if args.screen:
        # 获取大盘数据
        print("\n📈 获取大盘数据...")
        indices = fetch_market_indices()
        for idx in indices:
            sign = "+" if idx["change_pct"] > 0 else ""
            print(f"  {idx['name']}: {idx['price']:.2f} ({sign}{idx['change_pct']:.2f}%)")

        # 执行筛选
        stocks = screen_mainboard_strategy()

        if not stocks:
            print("\n❌ 未找到符合条件的股票")
            sys.exit(1)

        # 输出结果
        if not args.quiet:
            print(f"\n{'─'*120}")
            print(f"  {'#':>3}  {'代码':<8} {'名称':<8} {'板块':<10} {'龙头':>4} {'09:25':>7} {'09:26':>7} {'搓合量':>8} {'竞昨比':>7} {'剩余率':>7} {'涨幅':>7} {'筹码':<6} {'频次':>4} {'策略':<12}")
            print(f"{'─'*120}")
            for i, s in enumerate(stocks, 1):
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
                print(f"  {i:>3}  {s['code']:<8} {s['name']:<8} {sector:<10} {leader_mark:>4} {s.get('auction_price', s['price']):>7.2f} {s.get('price_0926', s['price']):>7.2f} {vol_fmt:>8} {comp_ratio:>6.1f}% {remaining:>6.1f}% {chg_0926:>+6.2f}% {verdict:<6} {freq:>4} {strategy:<12}")
            print(f"{'─'*120}")

        # 保存HTML
        html_path = args.html if args.html != "auction_report.html" else "screen_report.html"
        path = save_screen_html(stocks, indices, html_path)
        print(f"\n✅ 筛选报告: {path}")
        return

    # ---- 原有分析模式 ----
    now = datetime.now()
    h, m = now.hour, now.minute
    if h == 9 and 15 <= m <= 25:
        print("🟢 集合竞价中")
    elif h == 9 and m < 15:
        print("🟡 集合竞价未开始")
    else:
        print("ℹ️  非集合竞价时段")

    # 收集所有输入
    all_results = []

    # 图片输入
    if args.image:
        img_results = run_by_image(args.image, html_path=None)
        all_results.extend(img_results)

    # 文本输入（代码或名称）
    if args.inputs:
        # 分离代码和名称
        codes = []
        names = []
        for inp in args.inputs:
            inp = inp.strip()
            if inp.isdigit() and len(inp) == 6:
                codes.append(inp)
            else:
                names.append(inp)

        if codes:
            print(f"\n📋 直接代码: {', '.join(codes)}")
            code_results = run(codes, html_path=None)
            all_results.extend(code_results)

        if names:
            name_results = run_by_names(names, html_path=None)
            all_results.extend(name_results)

    if not all_results:
        print("❌ 无有效分析结果")
        sys.exit(1)

    # 去重
    seen = set()
    unique = []
    for r in all_results:
        if r.code not in seen:
            seen.add(r.code)
            unique.append(r)
    all_results = unique

    # 输出
    if not args.quiet:
        for r in all_results:
            print_result(r)

        bulls = sorted([r for r in all_results if "抢筹" in r.verdict], key=lambda x: -x.bull_score)
        bears = sorted([r for r in all_results if "出货" in r.verdict], key=lambda x: -x.bear_score)

        print(f"\n{'='*56}")
        if bulls:
            print("  🟢 真实抢筹:")
            for i, s in enumerate(bulls, 1):
                print(f"    {i}. {s.name}({s.code}) 抢筹分:{s.bull_score}")
        if bears:
            print("  🔴 疑似出货:")
            for i, s in enumerate(bears, 1):
                print(f"    {i}. {s.name}({s.code}) 出货分:{s.bear_score}")
        print(f"  ⚠️  仅供参考")
        print(f"{'='*56}")

    path = save_html(all_results, args.html)
    print(f"\n✅ HTML报告: {path}")


if __name__ == "__main__":
    main()
