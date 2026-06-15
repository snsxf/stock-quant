"""Yahoo Finance 补充数据：分析师目标价 / 推荐评级 / 上下调记录 / 财报历史 / 内部人交易。

填补 Finnhub 免费层缺失项：
- price_target（目标价）
- upgrade_downgrade（评级变动）
- earnings_dates（季度财报历史 actual vs estimate，GAAP 口径稳定）
- next_earnings_date（下次财报日期 + EPS estimate）
- insider_transactions（SEC Form 4 内部人交易）

注意：yfinance 这些字段不稳定（依赖 Yahoo 页面爬取），失败时返回 None / [] 优雅降级。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

import yfinance as yf


def _ticker(symbol: str) -> yf.Ticker:
    # yfinance now manages its curl_cffi session internally.
    return yf.Ticker(symbol)


def price_target(symbol: str) -> dict[str, Any] | None:
    """目标价共识：取自 ticker.info 的 targetXxxPrice 字段。"""
    try:
        info = _ticker(symbol).info or {}
    except Exception:
        return None
    if not info.get("targetMeanPrice"):
        return None
    return {
        "target_high": info.get("targetHighPrice"),
        "target_low": info.get("targetLowPrice"),
        "target_mean": info.get("targetMeanPrice"),
        "target_median": info.get("targetMedianPrice"),
        "n_analysts": info.get("numberOfAnalystOpinions"),
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "recommendation": info.get("recommendationKey"),
        "recommendation_mean": info.get("recommendationMean"),
    }


def recommendations(symbol: str) -> list[dict[str, Any]]:
    """近几个月的评级共识 (period / strongBuy / buy / hold / sell / strongSell)。"""
    try:
        df: pd.DataFrame | None = _ticker(symbol).recommendations
    except Exception:
        return []
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")


def upgrades_downgrades(symbol: str, limit: int = 20) -> list[dict[str, Any]]:
    """评级上下调记录：firm / toGrade / fromGrade / action / date。"""
    try:
        df = _ticker(symbol).upgrades_downgrades
    except Exception:
        return []
    if df is None or df.empty:
        return []
    df = df.reset_index().head(limit)
    out = []
    for _, r in df.iterrows():
        out.append({
            "date": str(r.get("GradeDate", r.get("Date", ""))),
            "firm": r.get("Firm"),
            "to_grade": r.get("ToGrade"),
            "from_grade": r.get("FromGrade"),
            "action": r.get("Action"),
        })
    return out


def earnings_dates(symbol: str, limit: int = 8) -> list[dict[str, Any]]:
    """季度财报历史 actual vs estimate（yfinance 取自 Yahoo Finance Earnings 页）。

    返回字段：period / actual / estimate / surprise_pct / _source

    优势 vs Finnhub：
      - GAAP 口径稳定，与 Yahoo 终端 / 富途 APP 一致
      - GOOGL FY26 Q1 实测：actual=5.11, estimate=2.64, +93.84% beat（Finnhub 错配返回 2.62）
    """
    try:
        df = _ticker(symbol).earnings_dates
    except Exception:
        return []
    if df is None or df.empty:
        return []

    out: list[dict[str, Any]] = []
    df = df.dropna(subset=["Reported EPS"]).head(limit)
    for idx, row in df.iterrows():
        try:
            period = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
        except Exception:
            period = str(idx)[:10]

        actual = row.get("Reported EPS")
        estimate = row.get("EPS Estimate")
        surprise = row.get("Surprise(%)")

        out.append({
            "period": period,
            "actual": float(actual) if pd.notna(actual) else None,
            "estimate": float(estimate) if pd.notna(estimate) else None,
            "surprise_pct": round(float(surprise), 2) if pd.notna(surprise) else None,
            "_source": "yahoo/Ticker.earnings_dates",
        })
    return out


def next_earnings_date(symbol: str) -> dict[str, Any] | None:
    """下次财报日期 + EPS estimate（取自 yfinance.Ticker.calendar）。

    返回字段：date / eps_estimate / revenue_estimate_low / revenue_estimate_high
    """
    try:
        cal = _ticker(symbol).calendar
    except Exception:
        return None
    if not cal:
        return None

    if isinstance(cal, dict):
        date_val = cal.get("Earnings Date")
        if isinstance(date_val, list) and date_val:
            date_val = date_val[0]
        eps_est = cal.get("Earnings Average") or cal.get("EPS Estimate")
        rev_lo = cal.get("Revenue Low")
        rev_hi = cal.get("Revenue High")
    else:
        try:
            row = cal.iloc[:, 0] if hasattr(cal, "iloc") else None
            date_val = row.get("Earnings Date") if row is not None else None
            eps_est = row.get("Earnings Average") if row is not None else None
            rev_lo = row.get("Revenue Low") if row is not None else None
            rev_hi = row.get("Revenue High") if row is not None else None
        except Exception:
            return None

    if not date_val:
        return None

    try:
        if hasattr(date_val, "strftime"):
            date_str = date_val.strftime("%Y-%m-%d")
        else:
            date_str = str(date_val)[:10]
    except Exception:
        date_str = str(date_val)[:10]

    return {
        "date": date_str,
        "eps_estimate": float(eps_est) if eps_est is not None and pd.notna(eps_est) else None,
        "revenue_estimate_low": float(rev_lo) if rev_lo is not None and pd.notna(rev_lo) else None,
        "revenue_estimate_high": float(rev_hi) if rev_hi is not None and pd.notna(rev_hi) else None,
        "_source": "yahoo/Ticker.calendar",
    }


def insider_transactions(symbol: str, days: int = 90) -> list[dict[str, Any]]:
    """内部人交易（来自 SEC Form 4，yfinance 直接拉取，免费无限速）。

    返回字段：date / insider / position / transaction / shares / value / _source
    """
    try:
        df = _ticker(symbol).insider_transactions
    except Exception:
        return []
    if df is None or df.empty:
        return []

    cutoff = datetime.now() - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        date_raw = r.get("Start Date") or r.get("Date")
        try:
            d = pd.to_datetime(date_raw)
            if pd.notna(d) and d.to_pydatetime().replace(tzinfo=None) < cutoff:
                continue
            date_str = d.strftime("%Y-%m-%d") if pd.notna(d) else str(date_raw)[:10]
        except Exception:
            date_str = str(date_raw)[:10] if date_raw else None

        shares = r.get("Shares")
        value = r.get("Value")
        out.append({
            "date": date_str,
            "insider": r.get("Insider"),
            "position": r.get("Position"),
            "transaction": r.get("Transaction"),
            "shares": int(shares) if pd.notna(shares) else None,
            "value": float(value) if pd.notna(value) else None,
            "_source": "yahoo/Ticker.insider_transactions",
        })
    return out


def forward_valuation(symbol: str) -> dict[str, Any] | None:
    """Forward PE / PEG / Forward EPS 等前瞻估值字段。

    降级链（实测 2025-2026 年 yfinance 经常被 Yahoo 限速）：
      1. StockAnalysis.com /statistics/  ← 主源（命中率 ~100%，免费免 key）
      2. yfinance ticker.info             ← 兜底（限速时全部失败）

    返回 None 表示两个数据源都拿不到 Forward PE。
    返回 dict 中的 _source 字段标记数据来源，便于调试。
    """
    primary = _from_stockanalysis(symbol)
    if primary and primary.get("pe_forward") is not None:
        return primary

    fallback = _from_yfinance(symbol)
    if fallback and fallback.get("pe_forward") is not None:
        return fallback

    return primary or fallback


def _from_stockanalysis(symbol: str) -> dict[str, Any] | None:
    """主源：StockAnalysis.com /statistics/ 页面爬虫。"""
    try:
        from .stockanalysis import forward_valuation as _sa_forward
        raw = _sa_forward(symbol)
    except Exception:
        return None
    if not raw:
        return None

    return {
        "pe_forward": raw.get("pe_forward"),
        "pe_trailing": raw.get("pe_trailing"),
        "eps_forward": raw.get("eps_forward"),
        "eps_trailing": raw.get("eps_trailing"),
        "peg_ratio": raw.get("peg_ratio"),
        "price_to_sales_ttm": raw.get("ps_trailing"),
        "price_to_book": raw.get("price_to_book"),
        "_source": "stockanalysis",
    }


def _from_yfinance(symbol: str) -> dict[str, Any] | None:
    """兜底源：yfinance ticker.info（Yahoo 限速时常年返回空）。"""
    try:
        info = _ticker(symbol).info or {}
    except Exception:
        return None
    if not info:
        return None

    fwd_pe = info.get("forwardPE")
    trailing_pe = info.get("trailingPE")
    if fwd_pe is None and trailing_pe is None:
        return None

    return {
        "pe_forward": fwd_pe,
        "pe_trailing": trailing_pe,
        "eps_forward": info.get("forwardEps"),
        "eps_trailing": info.get("trailingEps"),
        "peg_ratio": info.get("pegRatio") or info.get("trailingPegRatio"),
        "price_to_sales_ttm": info.get("priceToSalesTrailing12Months"),
        "price_to_book": info.get("priceToBook"),
        "_source": "yfinance.info",
    }
