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
    if net > 0:
        v = "🟢 真实抢筹"
    else:
        v = "🔴 疑似出货"

    return AuctionResult(
        code=code, name=quote["name"],
        prev_close=pc, open_price=op, price=quote["price"],
        volume=vol, amount=amt, change_pct=chg,
        volume_ratio=round(vr, 2), open_gap=round(gap, 2),
        amplitude=amp, turnover=to, buy_ratio=round(br, 1),
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


def save_html(results: list[AuctionResult], path: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cards = ""
    for r in results:
        ud = "up" if r.change_pct > 0 else ("down" if r.change_pct < 0 else "flat")
        vc = "bull" if "抢筹" in r.verdict else "bear"
        sigs = "".join(f'<div class="s">{s}</div>' for s in r.signals)
        cards += f"""
<div class="c"><div class="h"><div><span class="n">{r.name}</span> <span class="d">{r.code}</span></div>
<span class="b {vc}">{r.verdict}</span></div>
<div class="g">
<div class="m"><div class="ml">今开</div><div class="mv {ud}">{r.open_price:.2f}</div></div>
<div class="m"><div class="ml">涨跌</div><div class="mv {ud}">{r.change_pct:+.2f}%</div></div>
<div class="m"><div class="ml">跳空</div><div class="mv {ud}">{r.open_gap:+.2f}%</div></div>
<div class="m"><div class="ml">成交量</div><div class="mv">{r.volume:,}手</div></div>
<div class="m"><div class="ml">量比</div><div class="mv {"up" if r.volume_ratio>1.5 else ""}">{r.volume_ratio}x</div></div>
<div class="m"><div class="ml">外盘比</div><div class="mv">{r.buy_ratio:.1f}%</div></div></div>
<div class="brs">
<div class="br"><span class="bl">🟢 抢筹</span><div class="bt"><div class="bf bu" style="width:{r.bull_score}%"></div></div><span class="bsc" style="color:#3fb950">{r.bull_score}</span></div>
<div class="br"><span class="bl">🔴 出货</span><div class="bt"><div class="bf be" style="width:{r.bear_score}%"></div></div><span class="bsc" style="color:#f85149">{r.bear_score}</span></div></div>
<div class="sg"><div class="sh">📌 信号明细</div>{sigs}</div></div>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>集合竞价分析</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:-apple-system,"PingFang SC",sans-serif;background:#0d1117;color:#c9d1d9;padding:16px}}
.hd{{text-align:center;padding:20px 0}}.hd h1{{font-size:22px;color:#58a6ff}}.hd .t{{color:#8b949e;font-size:12px;margin-top:4px}}
.c{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px;margin:12px auto;max-width:660px}}
.h{{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:6px}}
.n{{font-size:17px;font-weight:700;color:#f0f6fc}}.d{{color:#8b949e;font-size:12px}}
.b{{padding:3px 10px;border-radius:14px;font-size:12px;font-weight:600}}
.b.bull{{background:rgba(46,160,67,.15);color:#3fb950}}.b.bear{{background:rgba(248,81,73,.15);color:#f85149}}.b.neutral{{background:rgba(139,148,158,.15);color:#8b949e}}
.g{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}}
.m{{background:#0d1117;border-radius:6px;padding:8px;text-align:center}}.ml{{font-size:10px;color:#8b949e}}.mv{{font-size:15px;font-weight:700;margin-top:2px;color:#c9d1d9}}
.mv.up{{color:#f85149}}.mv.down{{color:#3fb950}}
.brs{{margin:10px 0}}.br{{display:flex;align-items:center;margin:5px 0}}.bl{{width:65px;font-size:11px}}
.bt{{flex:1;height:16px;background:#0d1117;border-radius:8px;overflow:hidden}}.bf{{height:100%;border-radius:8px}}
.bf.bu{{background:linear-gradient(90deg,#238636,#3fb950)}}.bf.be{{background:linear-gradient(90deg,#da3633,#f85149)}}
.bsc{{width:35px;text-align:right;font-weight:700;font-size:12px}}
.sg{{margin-top:12px}}.sh{{font-size:12px;color:#8b949e;margin-bottom:4px}}.s{{padding:2px 0;font-size:12px;line-height:1.6}}
.ft{{text-align:center;color:#484f58;font-size:10px;padding:20px 0}}</style></head><body>
<div class="hd"><h1>📊 集合竞价分析</h1><div class="t">{now}</div></div>{cards}
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
    parser.add_argument("--quiet", "-q", action="store_true", help="静默模式")
    args = parser.parse_args()

    if not args.inputs and not args.image:
        parser.print_help()
        sys.exit(1)

    print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
