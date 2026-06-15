"""Forward PE 多源回归测试脚本（不动主代码，仅本地验证）。

验证四个候选源能否拿到 ARM / NVDA / CRCL 的 Forward PE：
  1. yfinance.info.forwardPE              （现有兜底）
  2. Finnhub stock_basic_financials       （metric.peForward / peNormalizedAnnual）
  3. StockAnalysis.com /financials/ HTML  （Forward PE 表格行）
  4. FMP analyst-estimates 自算           （spot / mean(EPS_next_FY)，需 FMP_API_KEY）

用法：
    cd stock-quant
    uv run python scripts/test_forward_pe.py
    # 或
    uv run python scripts/test_forward_pe.py NVDA AAPL TSLA

输出：
    ┌────────┬─────────┬─────────┬──────────────┬────────┐
    │ symbol │ yfinance│ finnhub │ stockanalysis│ fmp    │
    ├────────┼─────────┼─────────┼──────────────┼────────┤
    │ ARM    │   None  │  29.41  │    29.40     │ 28.95  │
    └────────┴─────────┴─────────┴──────────────┴────────┘
"""
from __future__ import annotations

import os
import re
import sys
import time
import traceback
from typing import Any

import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DEFAULT_SYMBOLS = ["ARM", "NVDA", "CRCL"]


# ─────────────────────────────────────────────────────────────
# Source 1: yfinance
# ─────────────────────────────────────────────────────────────
def from_yfinance(symbol: str) -> dict[str, Any]:
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
    except Exception as e:
        return {"forward_pe": None, "error": f"{type(e).__name__}: {e}"}

    if not info:
        return {"forward_pe": None, "error": "info dict 为空"}

    return {
        "forward_pe": info.get("forwardPE"),
        "forward_eps": info.get("forwardEps"),
        "trailing_pe": info.get("trailingPE"),
        "info_keys": len(info),
    }


# ─────────────────────────────────────────────────────────────
# Source 2: Finnhub
# ─────────────────────────────────────────────────────────────
def from_finnhub(symbol: str) -> dict[str, Any]:
    try:
        from stock_quant.config import settings
        api_key = settings.finnhub_api_key
    except Exception:
        api_key = os.environ.get("FINNHUB_API_KEY", "")

    if not api_key:
        return {"forward_pe": None, "error": "FINNHUB_API_KEY 未配置"}

    url = "https://finnhub.io/api/v1/stock/metric"
    try:
        r = httpx.get(
            url,
            params={"symbol": symbol, "metric": "all", "token": api_key},
            timeout=10.0,
        )
        if r.status_code != 200:
            return {"forward_pe": None, "error": f"HTTP {r.status_code}"}
        data = r.json()
    except Exception as e:
        return {"forward_pe": None, "error": f"{type(e).__name__}: {e}"}

    metric = data.get("metric", {}) if isinstance(data, dict) else {}

    fwd = metric.get("peForward")

    pe_related = {
        k: v for k, v in metric.items()
        if ("pe" in k.lower() or "peg" in k.lower()) and v is not None
    }

    return {
        "forward_pe": fwd,
        "pe_ttm": metric.get("peBasicExclExtraTTM") or metric.get("peTTM"),
        "peg": metric.get("pegRatio"),
        "available_pe_fields": list(pe_related.keys())[:10],
    }


# ─────────────────────────────────────────────────────────────
# Source 3: StockAnalysis.com（爬 /financials/ratios/ 页面）
# ─────────────────────────────────────────────────────────────
def from_stockanalysis(symbol: str) -> dict[str, Any]:
    pages_to_try = [
        f"https://stockanalysis.com/stocks/{symbol.lower()}/statistics/",
        f"https://stockanalysis.com/stocks/{symbol.lower()}/financials/ratios/",
    ]
    last_err = None
    for url in pages_to_try:
        try:
            r = httpx.get(
                url,
                timeout=12.0,
                headers={"User-Agent": UA},
                follow_redirects=True,
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code} @ {url}"
                continue
            html = r.text
        except Exception as e:
            last_err = f"{type(e).__name__}: {e} @ {url}"
            continue

        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

        patterns = [
            r"PE\s+Ratio\s*\(Forward\)\s*([\-\d\.,]+)",
            r"Forward\s+PE\s+Ratio\s*([\-\d\.,]+)",
            r"Forward\s+PE\s+\(1y\)\s*([\-\d\.,]+)",
            r"Forward\s+PE(?!G)\s*([\-\d\.,]+)",
            r"P/E\s+\(Forward\)\s*([\-\d\.,]+)",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                raw = m.group(1).replace(",", "")
                try:
                    val = float(raw)
                except ValueError:
                    continue
                start = max(0, m.start() - 30)
                end = min(len(text), m.end() + 30)
                context = text[start:end]
                return {
                    "forward_pe": val,
                    "url": url,
                    "matched_pattern": pat,
                    "context": context,
                }

    return {"forward_pe": None, "error": last_err or "页面抓到了但没匹配到 Forward PE"}


# ─────────────────────────────────────────────────────────────
# Source 4: FMP（自算 = spot / forward_eps_consensus）
# ─────────────────────────────────────────────────────────────
def from_fmp(symbol: str) -> dict[str, Any]:
    api_key = os.environ.get("FMP_API_KEY", "")
    if not api_key:
        return {"forward_pe": None, "error": "FMP_API_KEY 未配置（可选源，跳过）"}

    try:
        spot_url = f"https://financialmodelingprep.com/api/v3/quote/{symbol}"
        r1 = httpx.get(spot_url, params={"apikey": api_key}, timeout=10.0)
        spot_data = r1.json() if r1.status_code == 200 else []
        if not spot_data:
            return {"forward_pe": None, "error": "quote 接口无数据"}
        spot = spot_data[0].get("price")

        est_url = (
            f"https://financialmodelingprep.com/api/v3/analyst-estimates/{symbol}"
        )
        r2 = httpx.get(est_url, params={"apikey": api_key, "limit": 4}, timeout=10.0)
        if r2.status_code != 200:
            return {"forward_pe": None, "error": f"estimates HTTP {r2.status_code}"}
        estimates = r2.json() or []
        if not estimates:
            return {"forward_pe": None, "error": "无分析师预期数据"}

        next_fy = estimates[0]
        eps_avg = next_fy.get("estimatedEpsAvg")

        if not (spot and eps_avg and eps_avg > 0):
            return {
                "forward_pe": None,
                "error": f"spot={spot} eps_avg={eps_avg} 无法计算",
            }

        return {
            "forward_pe": round(spot / eps_avg, 2),
            "spot": spot,
            "eps_forward_consensus": eps_avg,
            "estimate_date": next_fy.get("date"),
        }
    except Exception as e:
        return {"forward_pe": None, "error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────
# Source 5: 项目内 yahoo_extras.forward_valuation（重构后的降级链入口）
# ─────────────────────────────────────────────────────────────
def from_project_chain(symbol: str) -> dict[str, Any]:
    try:
        from stock_quant.datasource.yahoo_extras import forward_valuation
    except Exception as e:
        return {"forward_pe": None, "error": f"import 失败: {e}"}
    try:
        res = forward_valuation(symbol)
    except Exception as e:
        return {"forward_pe": None, "error": f"{type(e).__name__}: {e}"}
    if not res:
        return {"forward_pe": None, "error": "降级链全部失败"}
    return {
        "forward_pe": res.get("pe_forward"),
        "trailing_pe": res.get("pe_trailing"),
        "forward_eps": res.get("eps_forward"),
        "_source": res.get("_source"),
    }


# ─────────────────────────────────────────────────────────────
# 汇总执行
# ─────────────────────────────────────────────────────────────
SOURCES = [
    ("yfinance", from_yfinance),
    ("finnhub", from_finnhub),
    ("stockanalysis", from_stockanalysis),
    ("fmp", from_fmp),
    ("project_chain", from_project_chain),
]


def fmt(v: Any) -> str:
    if v is None:
        return "  None  "
    if isinstance(v, float):
        return f"{v:7.2f} "
    return f"{str(v)[:8]:>8s}"


def run(symbols: list[str]) -> None:
    print("=" * 90)
    print(f"  Forward PE 多源回归测试  |  标的: {', '.join(symbols)}")
    print("=" * 90)

    results: dict[str, dict[str, Any]] = {}

    for sym in symbols:
        print(f"\n▶ {sym}")
        print("-" * 90)
        results[sym] = {}
        for name, fn in SOURCES:
            t0 = time.time()
            try:
                res = fn(sym)
            except Exception as e:
                res = {"forward_pe": None, "error": f"未捕获异常: {e}"}
                traceback.print_exc()
            elapsed = (time.time() - t0) * 1000

            results[sym][name] = res
            fpe = res.get("forward_pe")
            err = res.get("error", "")
            tag = "✅" if fpe is not None else "❌"
            extra = ""
            if fpe is not None:
                if "trailing_pe" in res and res.get("trailing_pe") is not None:
                    extra += f"  TTM={res['trailing_pe']}"
                if "forward_eps" in res and res.get("forward_eps"):
                    extra += f"  fwdEPS={res['forward_eps']}"
                if "eps_forward_consensus" in res:
                    extra += f"  fwdEPS={res['eps_forward_consensus']}"
                if "_source" in res:
                    extra += f"  via={res['_source']}"
                if "context" in res:
                    extra += f"\n        ↳ HTML 上下文: ...{res['context']}..."
            else:
                extra = f"  ← {err}"
                if name == "finnhub" and "available_pe_fields" in res:
                    extra += f"\n        ↳ 可用字段: {res['available_pe_fields']}"
            print(
                f"  {tag} {name:14s} forward_pe = {fmt(fpe)}  "
                f"({elapsed:6.0f}ms){extra}"
            )

    # 总结表
    print("\n" + "=" * 90)
    print("  汇总（√ 表示拿到非空 Forward PE）")
    print("=" * 90)
    header = f"  {'symbol':8s} | " + " | ".join(f"{n:14s}" for n, _ in SOURCES)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for sym in symbols:
        row = [f"{sym:8s}"]
        for name, _ in SOURCES:
            fpe = results[sym][name].get("forward_pe")
            row.append(f"{fpe:14.2f}" if fpe is not None else f"{'  ─':14s}")
        print("  " + " | ".join(row))

    # 推荐降级链
    print("\n" + "=" * 90)
    print("  推荐降级链（按各源命中率从高到低）")
    print("=" * 90)
    hit_rate = {
        name: sum(
            1 for sym in symbols if results[sym][name].get("forward_pe") is not None
        )
        for name, _ in SOURCES
    }
    ranked = sorted(hit_rate.items(), key=lambda x: -x[1])
    for i, (name, hits) in enumerate(ranked, 1):
        print(f"  {i}. {name:14s} 命中 {hits}/{len(symbols)}")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_SYMBOLS
    run([s.upper() for s in syms])
