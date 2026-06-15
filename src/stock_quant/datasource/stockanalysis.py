"""StockAnalysis.com 公开页面爬虫：免费、无需 API Key、数据较新。

用作 Finnhub 免费层缺失项的兜底：
- 分析师目标价（high / low / median / average + upside%）
- 评级共识（Strong Buy / Buy / Hold / Sell / Strong Sell）
- 前瞻估值（Forward PE / Forward PS / PEG / PB）
"""
from __future__ import annotations

import re
from typing import Any

import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _fetch(symbol: str, page: str = "forecast") -> str:
    # 兼容带 query string 的 page，例如 "financials/?p=quarterly"
    if "?" in page:
        path, query = page.split("?", 1)
        path = path.rstrip("/")
        url = f"https://stockanalysis.com/stocks/{symbol.lower()}/{path}/?{query}"
    else:
        url = f"https://stockanalysis.com/stocks/{symbol.lower()}/{page}/"
    r = httpx.get(url, timeout=10.0, headers={"User-Agent": UA}, follow_redirects=True)
    if r.status_code != 200:
        return ""
    return r.text


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _parse_number(raw: str) -> float | None:
    """容错解析 '278.91' / '1,234.5' / 'n/a' / '-'。"""
    if not raw:
        return None
    s = raw.strip().replace(",", "")
    if s.lower() in ("n/a", "na", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def price_target(symbol: str) -> dict[str, Any] | None:
    """返回平均/最高/最低/中位目标价 + 上行空间。"""
    html = _fetch(symbol, "forecast")
    if not html:
        return None

    out: dict[str, Any] = {"source": "stockanalysis"}

    # "Price Target: $270.73 (+25.80%)"
    m = re.search(
        r"Price Target:\s*\$([\d,]+\.\d+)\s*\(\s*([+\-]?[\d.]+)%",
        _strip(html),
    )
    if m:
        out["target_mean"] = float(m.group(1).replace(",", ""))
        out["upside_pct"] = float(m.group(2))

    # "average price target of $270.73, which forecasts a 25.80% increase"
    if "Strong Buy" in html:
        out["recommendation"] = "Strong Buy"
    elif "consensus rating of \"Buy\"" in html or "rating of Buy" in html:
        out["recommendation"] = "Buy"
    elif "Hold" in html and "consensus rating" in html:
        out["recommendation"] = "Hold"

    # 表格行 "Price $195 $270.73 $265 $360"
    text = _strip(html)
    m = re.search(
        r"Price\s+\$([\d,]+\.?\d*)\s+\$([\d,]+\.?\d*)\s+\$([\d,]+\.?\d*)\s+\$([\d,]+\.?\d*)",
        text,
    )
    if m:
        out["target_low"] = float(m.group(1).replace(",", ""))
        out["target_mean"] = float(m.group(2).replace(",", ""))
        out["target_median"] = float(m.group(3).replace(",", ""))
        out["target_high"] = float(m.group(4).replace(",", ""))

    # 分析师数量 "based on N analysts"
    m = re.search(r"based on\s+(\d+)\s+analyst", text, re.IGNORECASE)
    if m:
        out["n_analysts"] = int(m.group(1))

    return out if len(out) > 1 else None


def rating_consensus(symbol: str) -> dict[str, Any] | None:
    """评级共识：Strong Buy/Buy/Hold/Sell/Strong Sell 计数。"""
    html = _fetch(symbol, "forecast")
    if not html:
        return None
    text = _strip(html)

    out: dict[str, Any] = {"source": "stockanalysis"}
    # "Buy 47 Hold 4 Sell 1" 之类的聚合
    for label, key in [
        ("Strong Buy", "strong_buy"),
        ("Buy", "buy"),
        ("Hold", "hold"),
        ("Sell", "sell"),
        ("Strong Sell", "strong_sell"),
    ]:
        m = re.search(rf"\b{re.escape(label)}\b\s+(\d+)\b", text)
        if m:
            out[key] = int(m.group(1))

    if any(k in out for k in ("strong_buy", "buy", "hold", "sell", "strong_sell")):
        total = sum(out.get(k, 0) for k in ("strong_buy", "buy", "hold", "sell", "strong_sell"))
        bullish = out.get("strong_buy", 0) + out.get("buy", 0)
        bearish = out.get("sell", 0) + out.get("strong_sell", 0)
        out["total"] = total
        out["bull_bear_score"] = round((bullish - bearish) / total, 2) if total else None
        return out

    return None


def forward_valuation(symbol: str) -> dict[str, Any] | None:
    """前瞻估值：Forward PE / Forward PS / PEG / PB / TTM PE / TTM PS。

    数据源：stockanalysis.com 的 /statistics/ 页面（也兜底 /financials/ratios/）。
    返回 None 表示页面抓不到或所有字段都解析失败。

    实测可用字段（截至 2025-2026）：
      - "PE Ratio 278.91 Forward PE 96.74 PS Ratio 47.64 Forward PS 37.xx"
      - "PEG Ratio 1.5 Price to Book 12.34"
    """
    pages = ["statistics", "financials/ratios"]
    text = ""
    used_url = ""
    for page in pages:
        html = _fetch(symbol, page)
        if html:
            text = _strip(html)
            used_url = f"https://stockanalysis.com/stocks/{symbol.lower()}/{page}/"
            break
    if not text:
        return None

    out: dict[str, Any] = {"_source": "stockanalysis", "_url": used_url}

    field_patterns = [
        ("pe_forward", [
            r"PE\s+Ratio\s*\(Forward\)\s*([\-\d\.,]+|n/a|-)",
            r"Forward\s+PE\s+Ratio\s*([\-\d\.,]+|n/a|-)",
            r"Forward\s+PE(?!G)(?!\s+\([0-9])\s*([\-\d\.,]+|n/a|-)",
            r"P/E\s+\(Forward\)\s*([\-\d\.,]+|n/a|-)",
        ]),
        ("pe_trailing", [
            r"PE\s+Ratio(?!\s*\(Forward\))\s+([\-\d\.,]+|n/a|-)",
            r"P/E\s+Ratio\s+([\-\d\.,]+|n/a|-)",
        ]),
        ("ps_forward", [
            r"Forward\s+PS\s+([\-\d\.,]+|n/a|-)",
            r"PS\s+Ratio\s*\(Forward\)\s*([\-\d\.,]+|n/a|-)",
        ]),
        ("ps_trailing", [
            r"PS\s+Ratio(?!\s*\(Forward\))\s+([\-\d\.,]+|n/a|-)",
        ]),
        ("peg_ratio", [
            r"PEG\s+Ratio\s+([\-\d\.,]+|n/a|-)",
        ]),
        ("price_to_book", [
            r"PB\s+Ratio\s+([\-\d\.,]+|n/a|-)",
            r"Price[\s/]to[\s/]Book\s+([\-\d\.,]+|n/a|-)",
        ]),
        ("eps_forward", [
            r"EPS\s*\(FWD\)\s+\$?([\-\d\.,]+|n/a|-)",
            r"Forward\s+EPS\s+\$?([\-\d\.,]+|n/a|-)",
        ]),
        ("eps_trailing", [
            r"EPS\s*\(TTM\)\s+\$?([\-\d\.,]+|n/a|-)",
            r"Earnings\s+Per\s+Share\s+\(EPS\)\s+\$?([\-\d\.,]+|n/a|-)",
        ]),
    ]

    for key, pats in field_patterns:
        for pat in pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                val = _parse_number(m.group(1))
                if val is not None:
                    out[key] = val
                    break

    has_data = any(
        k in out for k in ("pe_forward", "pe_trailing", "ps_forward", "peg_ratio")
    )
    return out if has_data else None


def earnings_history(symbol: str, limit: int = 8) -> list[dict[str, Any]]:
    """季度 EPS 历史（解析 stockanalysis.com /financials/?p=quarterly）。

    用作 yfinance 被限流时的二级 fallback。
    免费版只有 actual 没有 estimate，但 actual 是 GAAP-Diluted 口径，准确。

    返回结构（最新季度优先）：
        [{"period": "2026-03-31", "actual": 5.11, "estimate": None,
          "surprise_pct": None, "_source": "stockanalysis.com/financials"}, ...]
    """
    html = _fetch(symbol, "financials/?p=quarterly")
    if not html:
        return []

    # 1) 表头列出现的财季：<th id="2026-03-31">Q1 2026</th>
    periods = re.findall(r'<th id="(\d{4}-\d{2}-\d{2})"', html)
    if not periods:
        return []

    # 2) 提取 EPS (Diluted) 行，最新→最旧
    text = _strip(html)
    m = re.search(r"EPS\s*\(Diluted\)\s+([\-\d\.\s,]+?)(?=[A-Za-z%\(])", text)
    if not m:
        return []
    raw_values = m.group(1).split()
    values = [_parse_number(v) for v in raw_values]

    rows: list[dict[str, Any]] = []
    for period, val in zip(periods[:limit], values[:limit]):
        if val is None:
            continue
        rows.append({
            "period": period,
            "actual": val,
            "estimate": None,
            "surprise_pct": None,
            "_source": "stockanalysis.com/financials?p=quarterly",
        })
    return rows
