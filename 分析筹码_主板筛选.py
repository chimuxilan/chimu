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
import subprocess
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
import argparse
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
    except Exception:
        pass

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
    except Exception:
        pass

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
    """从环境变量或 openclaw 配置中获取 MiMo API Key"""
    key = os.environ.get("MIMO_API_KEY", "")
    if key:
        return key
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            key = cfg.get("models", {}).get("providers", {}).get("xiaomi", {}).get("apiKey", "")
            if key:
                return key
        except Exception:
            pass
    return ""


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
        print("  ⚠ 未安装本地 OCR 库，正在自动安装...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "rapidocr-onnxruntime", "-q"],
                           capture_output=True, timeout=120)
            from rapidocr_onnxruntime import RapidOCR
        except Exception:
            print("  ✗ 安装失败，请手动运行: pip install rapidocr-onnxruntime")
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
            "amount": _v(37), "turnover": _v(38),
            "amplitude": _v(43),
        }
    return results


def fetch_hist(code: str, days: int = 10) -> list[dict]:
    """获取近期日K线"""
    try:
        r = requests.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": _to_tencent(code), "scale": "240", "ma": "no", "datalen": days},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
            timeout=15,
        )
        data = json.loads(r.text)
        if data:
            return data
    except Exception:
        pass

    try:
        sym = _to_tencent(code)
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
    except Exception:
        pass
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

    # 2. 量比
    if vr > 5:
        sigs.append(f"📊 量比 {vr:.2f}x → 极度放量"); bull += 15; bear += 20
    elif vr > 3:
        sigs.append(f"📊 量比 {vr:.2f}x → 大幅放量"); bull += 20; bear += 10
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


def _compute_frequency(r: AuctionResult) -> int:
    """频次：信号命中数"""
    return len([s for s in r.signals if any(k in s for k in ["高开", "低开", "放量", "缩量", "买盘", "卖盘", "连涨", "连跌", "平开"])])


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

        freq = _compute_frequency(r)
        strategy = _compute_strategy(r)
        vol_fmt = _format_volume(r.volume)

        # 竞昨比（今开 vs 昨收）
        if r.prev_close > 0:
            comp_ratio = f"{(r.open_price - r.prev_close) / r.prev_close * 100:.1f}%"
        else:
            comp_ratio = "-"

        rows += f"""<tr>
<td>{r.code}</td>
<td style="text-align:left;font-weight:600">{r.name}</td>
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(_fetch_one, c) for c in codes]
        for future in concurrent.futures.as_completed(futures):
            code, hist = future.result()
            result[code] = hist
    return result


def screen_mainboard_strategy() -> list[dict]:
    """
    主板筛选策略（使用新浪+腾讯接口）：
    1. 非ST
    2. 集合竞价涨幅 > 1%
    3. 市值 < 400亿
    4. 前一日涨停取反（昨日涨停，今日竞价涨幅>1%但未封涨停）
    5. 非盘中下跌（竞价价 >= 昨收）
    6. 今日竞价量/昨日成交量 > 2%
    7. 集合竞价换手率 > 0.13%
    8. 集合竞价量比 > 3
    9. 3日涨幅 < 15%
    10. 集合竞价现手量 > 30000手
    """
    # ========== 第一步：获取全部A股 ==========
    print("\n📊 获取A股列表...")
    all_stocks = []
    page = 1
    while True:
        try:
            r = requests.get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                params={
                    "page": str(page), "num": "80", "sort": "symbol",
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
            all_stocks.extend(data)
            if len(data) < 80:
                break
            page += 1
        except Exception as e:
            print(f"  ⚠ 第{page}页失败: {e}")
            break

    if not all_stocks:
        print("  ✗ 无法获取股票列表")
        return []
    print(f"  共 {len(all_stocks)} 只A股")

    # ========== 第二步：初筛 ==========
    print("📊 初筛中...")
    candidates = []
    for item in all_stocks:
        code = str(item.get("code", ""))
        name = str(item.get("name", ""))
        symbol = str(item.get("symbol", ""))
        if not code or not name or len(code) != 6:
            continue

        # 只要沪深A股（排除北交所 bj 开头）
        if symbol.startswith("bj"):
            continue
        if not code.startswith(("60", "00", "30", "68")):
            continue

        # 排除ST
        if "ST" in name.upper():
            continue

        # 市值（亿元）: mktcap 单位是万元
        mktcap = item.get("mktcap", 0)
        try:
            mktcap = float(mktcap) if mktcap else 0
        except (ValueError, TypeError):
            continue
        if mktcap <= 0:
            continue
        market_cap_yi = mktcap / 10000  # 万元 → 亿元
        if market_cap_yi >= 400:
            continue

        # 当前价 / 昨收
        trade = item.get("trade", "0")
        settlement = item.get("settlement", "0")
        try:
            price = float(trade) if trade else 0
            prev_close = float(settlement) if settlement else 0
        except (ValueError, TypeError):
            continue
        if prev_close <= 0 or price <= 0:
            continue

        # 集合竞价涨幅 > 1%
        auction_gain = (price - prev_close) / prev_close * 100
        if auction_gain < 1:
            continue

        # 前一日涨停取反
        if code.startswith("68"):
            limit_pct = 20
        elif code.startswith("30"):
            limit_pct = 20
        else:
            limit_pct = 10
        limit_price = round(prev_close * (1 + limit_pct / 100), 2)
        if prev_close >= limit_price * 0.99:
            continue  # 昨日涨停

        # 非盘中下跌（竞价价 >= 昨收）
        open_price = item.get("open", "0")
        try:
            open_price = float(open_price) if open_price else price
        except (ValueError, TypeError):
            open_price = price
        if open_price < prev_close:
            continue

        # 成交量（新浪接口volume单位是股）
        volume = item.get("volume", 0)
        try:
            volume = int(volume) if volume else 0
        except (ValueError, TypeError):
            continue
        if volume <= 0:
            continue

        # 换手率
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
            "volume_shares": volume,  # 股
            "volume": volume // 100,  # 转换为手
            "market_cap_yi": round(market_cap_yi, 2),
            "turnover_sina": turnover,
        })

    print(f"  初筛: {len(candidates)} 只")

    if not candidates:
        return []

    # ========== 第三步：获取集合竞价详细数据（腾讯接口） ==========
    print("📊 获取竞价详细数据...")

    # 用腾讯接口批量获取实时数据（含量比等）
    tencent_map = {}
    batch_size = 50
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
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
                sym = m.group(1)
                fields = m.group(2).split("~")
                if len(fields) < 50:
                    continue
                code = fields[2]
                def _v(idx, t=float):
                    try:
                        return t(fields[idx]) if fields[idx] else (t(0) if t != str else "")
                    except (ValueError, IndexError):
                        return t(0) if t != str else ""
                tencent_map[code] = {
                    "volume": _v(6, int),       # 手
                    "amount": _v(37),            # 万元
                    "turnover": _v(38),          # 换手率
                    "amplitude": _v(43),         # 振幅
                    "volume_ratio_api": _v(49),  # 量比
                }
        except Exception as e:
            print(f"  ⚠ 腾讯接口批次{i//batch_size+1}失败: {e}")

    # ========== 第四步：应用量比/换手/竞价量过滤 ==========
    print("📊 应用策略过滤...")
    filtered = []
    for c in candidates:
        code = c["code"]
        tc = tencent_map.get(code, {})

        # 竞价量（优先用腾讯接口，单位：手）
        auction_vol = tc.get("volume", c["volume"])
        if auction_vol <= 0:
            continue

        # 昨成交量（股→手）
        # 新浪volume是股，需要转换
        yesterday_vol_shares = c["volume_shares"]
        yesterday_vol_lots = yesterday_vol_shares // 100  # 近似（非竞价时段=全天成交量）
        if yesterday_vol_lots <= 0:
            continue

        # 今日竞价量/昨日成交量 > 2%
        vol_ratio_yesterday = auction_vol / yesterday_vol_lots * 100
        if vol_ratio_yesterday <= 2:
            continue

        # 换手率（优先用腾讯接口）
        turnover = tc.get("turnover", 0)
        if turnover <= 0:
            turnover = c.get("turnover_sina", 0)
        if turnover <= 0.13:
            continue

        # 量比（优先用腾讯接口的量比字段）
        volume_ratio = tc.get("volume_ratio_api", 0)
        if volume_ratio <= 0:
            # 手动计算：竞价量 / 近5日均量
            avg_vol = yesterday_vol_lots
            volume_ratio = auction_vol / avg_vol if avg_vol > 0 else 0
        if volume_ratio <= 3:
            continue

        # 竞价现手量 > 30000手
        if auction_vol <= 30000:
            continue

        c["auction_vol"] = auction_vol
        c["vol_ratio_yesterday"] = round(vol_ratio_yesterday, 2)
        c["volume_ratio"] = round(volume_ratio, 2)
        c["turnover"] = round(turnover, 4)
        c["yesterday_vol"] = yesterday_vol_lots
        filtered.append(c)

    print(f"  量比/换手筛选: {len(filtered)} 只")

    if not filtered:
        return []

    # ========== 第五步：3日涨幅 < 15% ==========
    print("📊 获取3日涨幅...")
    codes_to_fetch = [c["code"] for c in filtered]
    kline_map = _fetch_kline_concurrent(codes_to_fetch, days=5)

    final = []
    for c in filtered:
        code = c["code"]
        klines = kline_map.get(code, [])
        if klines and len(klines) >= 4:
            closes = [float(k.get("close", 0)) for k in klines[-4:]]
            if closes[0] > 0:
                change_3d = (closes[-1] - closes[0]) / closes[0] * 100
                if change_3d >= 15:
                    continue
                c["change_3d"] = round(change_3d, 2)
            else:
                c["change_3d"] = 0
        else:
            c["change_3d"] = 0

        c["change_pct"] = c["auction_gain"]
        final.append(c)

    # 按竞价涨幅从大到小排序
    final.sort(key=lambda x: x["auction_gain"], reverse=True)

    print(f"  ✅ 最终筛选: {len(final)} 只股票")
    return final


def save_screen_html(stocks: list[dict], indices: list[dict], path: str) -> str:
    """保存主板筛选策略HTML报告"""
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
        chg = s["auction_gain"]
        color = "#f85149" if chg > 3 else ("#ffa657" if chg > 1 else "#c9d1d9")
        chg_3d = s.get("change_3d", 0)
        chg_3d_color = "#f85149" if chg_3d > 5 else ("#ffa657" if chg_3d > 0 else "#3fb950")

        rows += f"""<tr>
<td>{i}</td>
<td>{s["code"]}</td>
<td style="text-align:left;font-weight:600">{s["name"]}</td>
<td style="color:{color};font-weight:600">{chg:+.2f}%</td>
<td>{s["price"]:.2f}</td>
<td>{s["prev_close"]:.2f}</td>
<td>{s["market_cap_yi"]:.0f}亿</td>
<td>{s["volume"]:,}手</td>
<td>{s.get("vol_ratio_yesterday", 0):.1f}%</td>
<td>{s.get("turnover", 0):.3f}%</td>
<td>{s.get("volume_ratio", 0):.1f}x</td>
<td style="color:{chg_3d_color}">{chg_3d:+.2f}%</td>
</tr>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>主板筛选策略 - 集合竞价</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#0d1117;color:#c9d1d9;padding:16px}}
.hd{{text-align:center;padding:16px 0}}
.hd h1{{font-size:22px;color:#58a6ff}}
.hd .t{{color:#8b949e;font-size:12px;margin-top:4px}}
.criteria{{background:#161b22;border-radius:8px;padding:12px 16px;margin:12px auto;max-width:900px;font-size:12px;color:#8b949e;line-height:1.8}}
.criteria b{{color:#ffa657}}
.tbl-wrap{{overflow-x:auto;margin:0 auto;max-width:1200px}}
table{{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}}
th{{background:#161b22;color:#8b949e;font-weight:600;padding:8px 10px;text-align:center;border-bottom:2px solid #30363d;position:sticky;top:0}}
td{{padding:6px 10px;text-align:center;border-bottom:1px solid #21262d}}
tr:hover{{background:#161b22}}
.ft{{text-align:center;color:#484f58;font-size:10px;padding:20px 0}}
.badge{{display:inline-block;background:#238636;color:#fff;border-radius:10px;padding:2px 8px;font-size:11px;margin-left:6px}}
@media(max-width:768px){{table{{font-size:11px}}th,td{{padding:4px 6px}}}}
</style></head><body>
<div class="hd">
<h1>📊 主板筛选策略 <span class="badge">集合竞价</span></h1>
<div class="t">更新时间: {now} | 共筛选出 {len(stocks)} 只股票</div>
</div>
{idx_html}
<div class="criteria">
<b>筛选条件：</b>非ST | 竞价涨幅&gt;1% | 市值&lt;400亿 | 前一日涨停取反 | 非盘中下跌 |
竞价量/昨成交量&gt;2% | 竞价换手率&gt;0.13% | 竞价量比&gt;3 | 3日涨幅&lt;15% | 现手量&gt;30000手
</div>
<div class="tbl-wrap">
<table>
<thead><tr>
<th>#</th><th>代码</th><th>名称</th><th>竞价涨幅</th><th>竞价价</th>
<th>昨收</th><th>市值</th><th>竞价量</th><th>竞/昨量比</th>
<th>换手率</th><th>量比</th><th>3日涨幅</th>
</tr></thead>
<tbody>{rows}</tbody>
</table></div>
<div class="ft">⚠️ 仅供学习参考，不构成投资建议 | 数据来源: 东方财富</div>
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
    parser.add_argument("--screen", "-s", action="store_true", help="主板筛选策略模式")
    parser.add_argument("--quiet", "-q", action="store_true", help="静默模式")
    args = parser.parse_args()

    if not args.inputs and not args.image and not args.screen:
        parser.print_help()
        sys.exit(1)

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
            print(f"\n{'─'*80}")
            print(f"  {'#':>3}  {'代码':<8} {'名称':<8} {'竞价涨幅':>8} {'市值':>8} {'竞价量':>10} {'竞/昨量':>8} {'换手率':>8} {'量比':>6} {'3日涨幅':>8}")
            print(f"{'─'*80}")
            for i, s in enumerate(stocks, 1):
                print(f"  {i:>3}  {s['code']:<8} {s['name']:<8} {s['auction_gain']:>+7.2f}% {s['market_cap_yi']:>7.0f}亿 {s['volume']:>9,}手 {s.get('vol_ratio_yesterday',0):>7.1f}% {s.get('turnover',0):>7.3f}% {s.get('volume_ratio',0):>5.1f}x {s.get('change_3d',0):>+7.2f}%")
            print(f"{'─'*80}")

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
