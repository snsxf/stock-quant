"""财报相关：即将公布的财报 + 历史超预期率。

数据源策略（2026-05 起调整）：
  - 历史财报 actual vs estimate：
      1. **yfinance.Ticker.earnings_dates**     ← 主源（GAAP 口径，与 Yahoo / 富途 APP 一致）
      2. **stockanalysis.com /earnings/**       ← 二级 fallback
      3. **finnhub.company_earnings**           ← 最后兜底（标 _warn：GAAP/Non-GAAP 错配风险）
  - 下次财报日期：
      1. **yfinance.Ticker.calendar**           ← 主源
      2. **finnhub.earnings_calendar**          ← fallback

教训：Finnhub 免费层 `/stock/earnings` 在 GAAP/Non-GAAP 之间会错配
（GOOGL FY26 Q1 实测：actual=2.62, 真实 5.11，误差 95%）。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..datasource import stockanalysis, yahoo_extras
from ..datasource.finnhub import FinnhubSource


def _days_until(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        target = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (target - date.today()).days


def _scheduled_status(date_str: str | None) -> str:
    days = _days_until(date_str)
    if days is None:
        return "tbd"
    return "scheduled" if days >= 0 else "tbd"


def upcoming_earnings(symbol: str, days_ahead: int = 14) -> dict[str, Any] | None:
    """下次财报日期 + EPS estimate（yfinance 主，finnhub fallback）。

    只要没有 actual EPS/revenue，就不能标记为 released。
    """
    yf_next = yahoo_extras.next_earnings_date(symbol)
    if yf_next and yf_next.get("date"):
        date_str = yf_next.get("date")
        return {
            "symbol": symbol,
            "date": date_str,
            "hour": None,
            "status": _scheduled_status(date_str),
            "tone_word": "scheduled",
            "days_until": _days_until(date_str),
            "eps_estimate": yf_next.get("eps_estimate"),
            "revenue_estimate": None,
            "revenue_estimate_low": yf_next.get("revenue_estimate_low"),
            "revenue_estimate_high": yf_next.get("revenue_estimate_high"),
            "actual_eps": None,
            "actual_revenue": None,
            "_source": yf_next.get("_source", "yahoo/Ticker.calendar"),
            "_source_official": None,
            "_source_secondary": yf_next.get("_source", "yahoo/Ticker.calendar"),
            "_warn": (
                "yfinance calendar is a secondary/aggregated source; "
                "treat date as scheduled, not released, until official IR/SEC/HKEX notice and actuals are present"
            ),
        }

    fh = FinnhubSource()
    items = fh.earnings_calendar(symbol=symbol, days_ahead=days_ahead)
    if not items:
        return None
    nxt = items[0]
    date_str = nxt.get("date")
    return {
        "symbol": nxt.get("symbol"),
        "date": date_str,
        "hour": nxt.get("hour"),
        "status": _scheduled_status(date_str),
        "tone_word": "expected",
        "days_until": _days_until(date_str),
        "eps_estimate": nxt.get("epsEstimate"),
        "revenue_estimate": nxt.get("revenueEstimate"),
        "actual_eps": None,
        "actual_revenue": None,
        "year": nxt.get("year"),
        "quarter": nxt.get("quarter"),
        "_source": "finnhub/earnings_calendar (fallback)",
        "_source_official": None,
        "_source_secondary": "finnhub/earnings_calendar (fallback)",
        "_warn": (
            "Finnhub earnings_calendar is an untrusted fallback for date discovery; "
            "do not treat as official or released without an official notice and actual EPS/revenue"
        ),
    }


def recent_earnings_surprise(symbol: str) -> list[dict[str, Any]]:
    """近 4-8 个季度的财报超预期记录（yfinance 主 → stockanalysis fallback → finnhub 兜底）。"""
    yf_rows = yahoo_extras.earnings_dates(symbol, limit=8)
    if yf_rows:
        return yf_rows

    # 二级 fallback：stockanalysis.com（actual EPS GAAP-Diluted，无 estimate）
    sa_rows = stockanalysis.earnings_history(symbol, limit=8)
    if sa_rows:
        return sa_rows

    fh = FinnhubSource()
    rows = fh.earnings_history(symbol)
    out: list[dict[str, Any]] = []
    for r in rows:
        actual = r.get("actual")
        est = r.get("estimate")
        surprise_pct = None
        if actual is not None and est not in (None, 0):
            surprise_pct = round((actual - est) / abs(est) * 100, 2)
        out.append({
            "period": r.get("period"),
            "actual": actual,
            "estimate": est,
            "surprise_pct": surprise_pct,
            "_source": "finnhub/company_earnings (fallback)",
            "_warn": "Finnhub 免费层 GAAP/Non-GAAP 错配风险，建议 yfinance 优先",
        })
    return out
