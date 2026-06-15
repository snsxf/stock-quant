"""内部人交易摘要。

数据源策略（2026-05 起调整）：
  1. **yfinance.Ticker.insider_transactions**   ← 主源（来自 SEC Form 4，免费无限速）
  2. **finnhub.insider_transactions**           ← fallback（免费层常 403）

教训：Finnhub 免费层 `/stock/insider-transactions` 通常返回 403，
yfinance 直接拉 SEC Form 4，更稳定可用。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..datasource import yahoo_extras
from ..datasource.finnhub import FinnhubSource


def _summarize_yahoo(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """对 yfinance.insider_transactions 的行做买卖聚合。

    Yahoo 的 transaction 字段值常见：
      - "Sale" / "Sell" / "Disposition"          → 卖出
      - "Purchase" / "Buy"                        → 买入
      - "Exercise" / "Conversion"                 → 通常视为中性（行权产生）
    """
    buy_shares = sell_shares = 0
    buy_value = sell_value = 0.0
    n_buy = n_sell = 0
    for r in rows:
        tx = (r.get("transaction") or "").lower()
        shares = r.get("shares") or 0
        value = r.get("value") or 0.0
        if any(k in tx for k in ("sale", "sell", "disposition")):
            sell_shares += abs(int(shares))
            sell_value += float(value)
            n_sell += 1
        elif any(k in tx for k in ("purchase", "buy", "acquire")):
            buy_shares += abs(int(shares))
            buy_value += float(value)
            n_buy += 1

    return {
        "buy_count": n_buy,
        "buy_shares": buy_shares,
        "buy_value_usd": round(buy_value, 0),
        "sell_count": n_sell,
        "sell_shares": sell_shares,
        "sell_value_usd": round(sell_value, 0),
        "net_value_usd": round(buy_value - sell_value, 0),
    }


def recent_insider_summary(symbol: str, days: int = 90) -> dict[str, Any]:
    """近 N 天内部人买卖摘要（yfinance 主，finnhub fallback）。"""
    yf_rows = yahoo_extras.insider_transactions(symbol, days=days)
    if yf_rows:
        agg = _summarize_yahoo(yf_rows)
        return {
            "window_days": days,
            **agg,
            "_source": "yahoo/Ticker.insider_transactions (SEC Form 4)",
            "n_records": len(yf_rows),
        }

    fh = FinnhubSource()
    rows = fh.insider_transactions(symbol)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    buy_shares = sell_shares = 0
    buy_value = sell_value = 0.0
    n_buy = n_sell = 0
    for r in rows:
        d = (r.get("transactionDate") or "")[:10]
        if d < cutoff:
            continue
        change = r.get("change") or 0
        price = r.get("transactionPrice") or 0
        value = abs(change) * price
        if change > 0:
            buy_shares += change
            buy_value += value
            n_buy += 1
        elif change < 0:
            sell_shares += abs(change)
            sell_value += value
            n_sell += 1
    return {
        "window_days": days,
        "buy_count": n_buy,
        "buy_shares": buy_shares,
        "buy_value_usd": round(buy_value, 0),
        "sell_count": n_sell,
        "sell_shares": sell_shares,
        "sell_value_usd": round(sell_value, 0),
        "net_value_usd": round(buy_value - sell_value, 0),
        "_source": "finnhub/stock_insider_transactions (fallback)",
        "_warn": "Finnhub 免费层 insider 端点常返回 403，建议 yfinance 优先",
    }
