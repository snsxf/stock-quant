"""分析师评级 + 目标价。

策略：
- Finnhub 优先（数据更结构化）
- 失败/空时自动 fallback 到 yfinance（免费层补缺）
"""
from __future__ import annotations

from typing import Any

from ..datasource.finnhub import FinnhubSource
from ..datasource import yahoo_extras, stockanalysis


def rating_consensus(symbol: str) -> dict[str, Any] | None:
    """最近一期的买卖共识（Finnhub 主，Yahoo 辅）。"""
    fh = FinnhubSource()
    items = fh.recommendations(symbol)
    if items:
        latest = items[0]
        total = (
            latest.get("strongBuy", 0) + latest.get("buy", 0)
            + latest.get("hold", 0) + latest.get("sell", 0)
            + latest.get("strongSell", 0)
        )
        bullish = latest.get("strongBuy", 0) + latest.get("buy", 0)
        bearish = latest.get("sell", 0) + latest.get("strongSell", 0)
        score = round((bullish - bearish) / total, 2) if total > 0 else None
        return {
            "source": "finnhub",
            "period": latest.get("period"),
            "strong_buy": latest.get("strongBuy"),
            "buy": latest.get("buy"),
            "hold": latest.get("hold"),
            "sell": latest.get("sell"),
            "strong_sell": latest.get("strongSell"),
            "total": total,
            "bull_bear_score": score,
        }

    rows = yahoo_extras.recommendations(symbol)
    if rows:
        latest = rows[0]
        total = sum(latest.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
        bullish = latest.get("strongBuy", 0) + latest.get("buy", 0)
        bearish = latest.get("sell", 0) + latest.get("strongSell", 0)
        score = round((bullish - bearish) / total, 2) if total > 0 else None
        return {
            "source": "yahoo",
            "period": latest.get("period"),
            "strong_buy": latest.get("strongBuy"),
            "buy": latest.get("buy"),
            "hold": latest.get("hold"),
            "sell": latest.get("sell"),
            "strong_sell": latest.get("strongSell"),
            "total": total,
            "bull_bear_score": score,
        }

    return stockanalysis.rating_consensus(symbol)


def price_target_summary(symbol: str) -> dict[str, Any] | None:
    """目标价（Finnhub 免费层 403 → Yahoo fallback）。"""
    fh = FinnhubSource()
    pt = fh.price_target(symbol)
    if pt and pt.get("targetMean"):
        return {
            "source": "finnhub",
            "target_high": pt.get("targetHigh"),
            "target_low": pt.get("targetLow"),
            "target_mean": pt.get("targetMean"),
            "target_median": pt.get("targetMedian"),
            "n_analysts": pt.get("numberOfAnalysts"),
            "last_updated": pt.get("lastUpdated"),
        }

    yp = yahoo_extras.price_target(symbol)
    if yp:
        cur = yp.get("current_price")
        upside = None
        if cur and yp.get("target_mean"):
            upside = round((yp["target_mean"] - cur) / cur * 100, 2)
        return {
            "source": "yahoo",
            "target_high": yp.get("target_high"),
            "target_low": yp.get("target_low"),
            "target_mean": yp.get("target_mean"),
            "target_median": yp.get("target_median"),
            "n_analysts": yp.get("n_analysts"),
            "current_price": cur,
            "upside_pct": upside,
            "recommendation_key": yp.get("recommendation"),
            "recommendation_mean": yp.get("recommendation_mean"),
        }

    return stockanalysis.price_target(symbol)


def recent_rating_changes(symbol: str, limit: int = 20) -> list[dict[str, Any]]:
    """评级上下调（Finnhub 免费层 403 → Yahoo fallback）。"""
    fh = FinnhubSource()
    rows = fh.upgrade_downgrade(symbol, days=60) or []
    if rows:
        return [{
            "source": "finnhub",
            "date": r.get("gradeTime"),
            "company": r.get("company"),
            "from_grade": r.get("fromGrade"),
            "to_grade": r.get("toGrade"),
            "action": r.get("action"),
        } for r in rows[:limit]]

    return [{"source": "yahoo", **r} for r in yahoo_extras.upgrades_downgrades(symbol, limit=limit)]
