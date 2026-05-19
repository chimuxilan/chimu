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

# 大盘指数代码
MARKET_INDICES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
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


def fetch_market_indices() -> dict:
    """
    获取大盘指数实时行情
    返回 {指数代码: {name, price, change_pct, amount, volume, high, low, open, prev_close}}
    """
    symbols = ",".join(MARKET_INDICES.keys())
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={symbols}", headers=HEADERS, timeout=15)
        r.encoding = "gbk"
    except Exception as e:
        print(f"  ⚠ 大盘行情请求失败: {e}")
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
            "name": f[1],
            "code": code,
            "price": _v(3),
            "prev_close": _v(4),
            "open": _v(5),
            "volume": _v(6, int),
            "change_pct": _v(32),
            "high": _v(33),
            "low": _v(34),
            "amount": _v(37),
        }
    return results


def judge_market_env(indices: dict) -> dict:
    """
    根据三大指数判断大盘环境
    返回 {trend, label, color, score_adj, desc}
      score_adj: 对个股抢筹/出货分的修正值
    """
    if not indices:
        return {"trend": "unknown", "label": "未知", "color": "#8b949e",
                "score_adj": 0, "desc": "未获取到大盘数据"}

    sh = indices.get("000001", {}).get("change_pct", 0)
    cy = indices.get("399006", {}).get("change_pct", 0)
    avg = (sh + cy) / 2

    if avg > 1.5:
        return {"trend": "strong_up", "label": "大盘强势", "color": "#f85149",
                "score_adj": 8, "desc": f"大盘强势上攻，做多氛围浓厚（上证{sh:+.2f}% 创业板{cy:+.2f}%）"}
    elif avg > 0.5:
        return {"trend": "up", "label": "大盘偏多", "color": "#f85149",
                "score_adj": 4, "desc": f"大盘温和上涨，情绪偏暖（上证{sh:+.2f}% 创业板{cy:+.2f}%）"}
    elif avg > -0.5:
        return {"trend": "neutral", "label": "大盘震荡", "color": "#d29922",
                "score_adj": 0, "desc": f"大盘窄幅震荡，多空平衡（上证{sh:+.2f}% 创业板{cy:+.2f}%）"}
    elif avg > -1.5:
        return {"trend": "down", "label": "大盘偏空", "color": "#3fb950",
                "score_adj": -5, "desc": f"大盘温和下跌，情绪偏冷（上证{sh:+.2f}% 创业板{cy:+.2f}%）"}
    else:
        return {"trend": "strong_down", "label": "大盘弱势", "color": "#3fb950",
                "score_adj": -10, "desc": f"大盘大幅下挫，恐慌情绪蔓延（上证{sh:+.2f}% 创业板{cy:+.2f}%）"}


def print_market_overview(indices: dict, env: dict):
    """终端打印大盘概览"""
    print(f"\n{'━'*56}")
    print(f"  📈 大盘实时概况  {env['label']}")
    print(f"{'━'*56}")
    for full_code, label in MARKET_INDICES.items():
        code = full_code[2:]
        idx = indices.get(code)
        if not idx:
            continue
        chg = idx["change_pct"]
        arrow = "🔴" if chg > 0 else ("🟢" if chg < 0 else "⚪")
        print(f"  {arrow} {label:　<6s} {idx['price']:>10.2f}  {chg:+.2f}%")
    print(f"\n  💡 {env['desc']}")
    print(f"{'━'*56}")


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
    confidence: str = "低"
    signals: list = field(default_factory=list)


def analyze(code: str, quote: dict, hist: list[dict], market_env: dict = None) -> Optional[AuctionResult]:
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

    # ═══════════════════════════════════════════════
    # 第一层：单项信号（基于回测数据校准）
    # ═══════════════════════════════════════════════

    # 1. 跳空幅度
    # 回测发现：大幅低开次日涨率67%，温和高开均涨+0.52%
    if gap > 5:
        sigs.append(f"🔴 强势高开 {gap:+.2f}% → 注意追高风险"); bull += 15; bear += 10
    elif gap > 3:
        sigs.append(f"🔴 明显高开 {gap:+.2f}% → 强势延续"); bull += 20
    elif gap > 1:
        sigs.append(f"🟠 温和高开 {gap:+.2f}% → 看涨"); bull += 18
    elif gap > 0:
        sigs.append(f"🟡 微幅高开 {gap:+.2f}%"); bull += 3
    elif gap > -1:
        sigs.append(f"🟡 微幅低开 {gap:+.2f}%"); bear += 3
    elif gap > -3:
        sigs.append(f"🟠 温和低开 {gap:+.2f}%"); bear += 5
    else:
        # 大幅低开：回测次日涨率67%，典型反弹信号
        sigs.append(f"🟢 大幅低开 {gap:+.2f}% → 超跌反弹概率高"); bull += 25; bear += 5

    # 2. 量比
    # 回测发现：极度放量(5x+)次日涨率仅20%，极度缩量反而最好
    if vr > 5:
        sigs.append(f"📊 量比 {vr:.2f}x → 极度放量 ⚠️ 筹码松动!"); bear += 25
    elif vr > 3:
        sigs.append(f"📊 量比 {vr:.2f}x → 大幅放量，分歧加大"); bear += 10
    elif vr > 1.5:
        sigs.append(f"📊 量比 {vr:.2f}x → 温和放量"); bull += 5
    elif vr > 0.8:
        sigs.append(f"📊 量比 {vr:.2f}x → 量能正常")
    elif vr > 0.5:
        sigs.append(f"📊 量比 {vr:.2f}x → 温和缩量"); bull += 3
    else:
        # 极度缩量：回测涨率50%，抛压衰竭
        sigs.append(f"📊 量比 {vr:.2f}x → 极度缩量，抛压衰竭"); bull += 8

    # 3. 外盘比例
    # 回测发现：外盘高(60%+)反而次日跌，主力拉高出货；外盘低(<40%)次日微涨
    if br > 60:
        sigs.append(f"⚠️ 外盘 {br:.1f}% → 买入拥挤，警惕拉高出货!"); bear += 12
    elif br > 55:
        sigs.append(f"💪 外盘 {br:.1f}% → 买盘略强"); bull += 3
    elif br < 40:
        sigs.append(f"🔻 外盘 {br:.1f}% → 恐慌抛售后，关注反弹"); bull += 5
    elif br < 45:
        sigs.append(f"🔻 外盘 {br:.1f}% → 卖盘略强"); bear += 3
    else:
        sigs.append(f"⚖️ 外盘 {br:.1f}% → 多空平衡")

    # 4. 振幅
    if amp > 8:
        sigs.append(f"📈 振幅 {amp:.2f}% → 剧烈波动，多空激战"); bear += 8
    elif amp > 5:
        sigs.append(f"📈 振幅 {amp:.2f}% → 波动较大"); bear += 3

    # 5. 换手率
    if to > 10:
        sigs.append(f"🔄 换手 {to:.2f}% → 筹码大换手，方向待定"); bear += 8
    elif to > 5:
        sigs.append(f"🔄 换手 {to:.2f}% → 活跃"); bull += 3
    elif to < 0.5:
        sigs.append(f"🔄 换手 {to:.2f}% → 交投清淡")

    # ═══════════════════════════════════════════════
    # 第二层：组合信号（回测验证的高胜率模式）
    # ═══════════════════════════════════════════════

    combo_hit = 0  # 命中组合信号数

    # 低开+缩量/正常量 → 回测涨率54-57%，均涨+0.3%
    if gap < -1 and vr < 1.2:
        sigs.append("✅ 低开+缩量 → 恐慌释放后反弹概率高")
        bull += 15; combo_hit += 1

    # 大幅低开+缩量 → 最强反弹组合
    if gap < -3 and vr < 0.8:
        sigs.append("🔥 大幅低开+缩量 → 强烈反弹信号!")
        bull += 20; combo_hit += 1

    # 高开+放量 → 回测均涨+0.57%，强势延续
    if gap > 1 and vr > 1.5:
        sigs.append("🚀 高开+放量 → 强势延续，主力加仓")
        bull += 15; combo_hit += 1

    # 高开+极度放量 → 过热信号
    if gap > 1 and vr > 3:
        sigs.append("⚠️ 高开+极度放量 → 短期过热，注意回调")
        bear += 10; combo_hit += 1

    # 平开+放量 → 回测涨率仅38%，出货特征
    if abs(gap) < 0.5 and vr > 1.5:
        sigs.append("👀 平开+放量 → 主力暗中出货?")
        bear += 10; combo_hit += 1

    # 高开+外盘拥挤 → 拉高出货
    if gap > 1 and br > 60:
        sigs.append("🚨 高开+外盘拥挤 → 拉高出货概率大!")
        bear += 15; combo_hit += 1

    # 低开+外盘低 → 恐慌抛售后的反弹
    if gap < -1 and br < 40:
        sigs.append("💡 低开+恐慌抛售 → 反弹在即")
        bull += 10; combo_hit += 1

    # 极度放量+外盘高 → 最危险组合
    if vr > 3 and br > 60:
        sigs.append("☠️ 放巨量+外盘高 → 主力大规模出货!")
        bear += 20; combo_hit += 1

    # ═══════════════════════════════════════════════
    # 第三层：近期走势修正
    # ═══════════════════════════════════════════════

    if hist and len(hist) >= 3:
        cs = [float(h.get("close", 0)) for h in hist[-3:]]
        changes = [(cs[i] - cs[i-1]) / cs[i-1] * 100 for i in range(1, len(cs)) if cs[i-1] > 0]
        if changes:
            avg_chg = np.mean(changes)
            if avg_chg > 2 and gap > 1:
                sigs.append("📈 连涨+高开 → 强势延续，注意追高风险")
                bull += 8; bear += 8
            elif avg_chg < -2 and gap < -1:
                sigs.append("📉 连跌+低开 → 弱势延续，观望为主")
                bear += 10
            elif avg_chg < -2 and gap > 1:
                sigs.append("🔄 连跌+高开 → 止跌反弹信号!")
                bull += 18
            elif avg_chg > 2 and gap < -1:
                sigs.append("⚡ 连涨+低开 → 高位获利了结!")
                bear += 15

    # ═══════════════════════════════════════════════
    # 第四层：大盘环境修正
    # ═══════════════════════════════════════════════

    if market_env and market_env["trend"] != "unknown":
        adj = market_env["score_adj"]
        if adj > 0:
            sigs.append(f"📈 {market_env['label']} → 大盘助涨 +{adj}分")
            bull += adj
        elif adj < 0:
            sigs.append(f"📉 {market_env['label']} → 大盘拖累 {adj}分")
            bear += abs(adj)
        else:
            sigs.append(f"⚖️ {market_env['label']} → 大盘中性")

        # 逆大盘是强势信号
        if market_env["trend"] in ("strong_up", "up") and gap < -1:
            sigs.append("⚡ 逆大盘低开 → 主力刻意打压，关注反包")
            bull += 10
        elif market_env["trend"] in ("strong_down", "down") and gap > 2:
            sigs.append("🔥 逆大盘高开 → 极度强势!")
            bull += 15
        elif market_env["trend"] in ("strong_down", "down") and gap > 0:
            sigs.append("💪 逆大盘微高 → 相对强势，可关注")
            bull += 5

    # ═══════════════════════════════════════════════
    # 综合判定
    # ═══════════════════════════════════════════════

    bs = min(bull, 100)
    rs = min(bear, 100)
    net = bs - rs

    # 判定阈值（回测校准：提高抢筹门槛，降低出货门槛）
    if net > 12 and combo_hit >= 1:
        v = "真实抢筹"
    elif net > 20:
        v = "真实抢筹"
    elif net < -8 and combo_hit >= 1:
        v = "疑似出货"
    elif net < -15:
        v = "疑似出货"
    else:
        v = "正常"

    # 置信度
    sig_count = len([s for s in sigs if any(k in s for k in ["✅","🔥","🚀","🚨","☠️","💡","👀","⚡","📈","📉","🔄","💪","⚠️","🟢"])])
    if combo_hit >= 2 or abs(net) > 30:
        confidence = "高"
    elif combo_hit >= 1 or abs(net) > 15:
        confidence = "中"
    else:
        confidence = "低"

    return AuctionResult(
        code=code, name=quote["name"],
        prev_close=pc, open_price=op, price=quote["price"],
        volume=vol, amount=amt, change_pct=chg,
        volume_ratio=round(vr, 2), open_gap=round(gap, 2),
        amplitude=amp, turnover=to, buy_ratio=round(br, 1),
        buy_vol=bv, sell_vol=sv,
        bull_score=bs, bear_score=rs, verdict=v,
        confidence=confidence, signals=sigs,
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
    print(f"\n  🔮 {r.verdict} (置信度: {r.confidence})")
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


def save_html(results: list[AuctionResult], path: str, market_indices: dict = None, market_env: dict = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 大盘概览 HTML
    market_html = ""
    if market_indices and market_env:
        env_color = market_env.get("color", "#8b949e")
        env_label = market_env.get("label", "未知")
        env_desc = market_env.get("desc", "")
        idx_rows = ""
        for full_code, label in MARKET_INDICES.items():
            code = full_code[2:]
            idx = market_indices.get(code)
            if not idx:
                continue
            chg = idx["change_pct"]
            chg_color = "#f85149" if chg > 0 else ("#3fb950" if chg < 0 else "#c9d1d9")
            idx_rows += f"""<div class="idx-card">
<div class="idx-name">{label}</div>
<div class="idx-price" style="color:{chg_color}">{idx['price']:.2f}</div>
<div class="idx-chg" style="color:{chg_color}">{chg:+.2f}%</div>
</div>"""
        market_html = f"""<div class="market-box">
<div class="market-title">📈 大盘实时概况 <span class="env-tag" style="background:{env_color};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;margin-left:8px">{env_label}</span></div>
<div class="idx-row">{idx_rows}</div>
<div class="market-desc">{env_desc}</div>
</div>"""

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
<td style="color:{v_color};background:{v_bg};border-radius:4px;font-weight:600">{r.verdict}({r.confidence})</td>
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
.market-box{{max-width:1100px;margin:0 auto 16px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px}}
.market-title{{font-size:14px;font-weight:600;color:#58a6ff;margin-bottom:10px}}
.idx-row{{display:flex;gap:12px;flex-wrap:wrap;justify-content:center}}
.idx-card{{flex:1;min-width:120px;text-align:center;padding:8px;background:#0d1117;border-radius:6px}}
.idx-name{{font-size:12px;color:#8b949e;margin-bottom:4px}}
.idx-price{{font-size:16px;font-weight:700}}
.idx-chg{{font-size:13px;font-weight:600;margin-top:2px}}
.market-desc{{font-size:12px;color:#8b949e;margin-top:8px;text-align:center}}
.tbl-wrap{{overflow-x:auto;margin:0 auto;max-width:1100px}}
table{{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}}
th{{background:#161b22;color:#8b949e;font-weight:600;padding:8px 10px;text-align:center;border-bottom:2px solid #30363d;position:sticky;top:0}}
td{{padding:6px 10px;text-align:center;border-bottom:1px solid #21262d}}
tr:hover{{background:#161b22}}
.ft{{text-align:center;color:#484f58;font-size:10px;padding:20px 0}}
@media(max-width:768px){{table{{font-size:11px}}th,td{{padding:4px 6px}}}}
</style></head><body>
<div class="hd"><h1>📊 集合竞价 - 股票筛选</h1><div class="t">更新时间: {now}</div></div>
{market_html}
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

def run(codes: list[str], html_path: str = None, market_indices: dict = None, market_env: dict = None, quiet: bool = False) -> list[AuctionResult]:
    """
    分析指定股票代码列表
    market_indices/market_env: 若传入则跳过重复获取
    quiet: 传入 True 时跳过大盘打印（由调用方负责）
    """
    if market_indices is None:
        print("📈 获取大盘指数...")
        market_indices = fetch_market_indices()
        market_env = judge_market_env(market_indices)
        if market_indices:
            print_market_overview(market_indices, market_env)
        else:
            print("  ⚠ 未获取到大盘数据，跳过大盘修正")
    elif not quiet:
        print_market_overview(market_indices, market_env)

    quotes = fetch_quotes(codes)
    if not quotes:
        return []
    results = []
    for code in codes:
        if code not in quotes:
            continue
        hist = fetch_hist(code, days=10)
        r = analyze(code, quotes[code], hist, market_env=market_env)
        if r:
            results.append(r)
    if html_path and results:
        save_html(results, html_path, market_indices=market_indices, market_env=market_env)
    return results


def run_by_names(names: list[str], html_path: str = None, market_indices: dict = None, market_env: dict = None, quiet: bool = False) -> list[AuctionResult]:
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

    results = run(codes, html_path=html_path, market_indices=market_indices, market_env=market_env, quiet=quiet)
    # 补回名称映射
    for r in results:
        if r.code in name_map:
            r.name = name_map[r.code]
    return results


def run_by_image(image_path: str, html_path: str = None, market_indices: dict = None, market_env: dict = None, quiet: bool = False) -> list[AuctionResult]:
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
    return run_by_names(keywords, html_path=html_path, market_indices=market_indices, market_env=market_env, quiet=quiet)


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

    # 获取大盘数据（只获取一次）
    print("📈 获取大盘指数...")
    market_indices = fetch_market_indices()
    market_env = judge_market_env(market_indices)
    if market_indices:
        print_market_overview(market_indices, market_env)
    else:
        print("  ⚠ 未获取到大盘数据")

    # 图片输入
    if args.image:
        img_results = run_by_image(args.image, html_path=None,
                                    market_indices=market_indices, market_env=market_env, quiet=True)
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
            code_results = run(codes, html_path=None,
                               market_indices=market_indices, market_env=market_env, quiet=True)
            all_results.extend(code_results)

        if names:
            name_results = run_by_names(names, html_path=None,
                                        market_indices=market_indices, market_env=market_env, quiet=True)
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

    path = save_html(all_results, args.html, market_indices=market_indices, market_env=market_env)
    print(f"\n✅ HTML报告: {path}")


if __name__ == "__main__":
    main()
