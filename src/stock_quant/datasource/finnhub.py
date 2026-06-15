"""Finnhub 数据源：评级 / 财报 / 新闻 / 经济日历 / 内部人交易。

免费额度：60 calls/min，足够每日盘前批量分析使用。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import finnhub
from finnhub.exceptions import FinnhubAPIException

from ..config import settings


def _safe(default):
    """装饰器：免费层 403/404 时返回 default 而不是抛错。"""
    def deco(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except FinnhubAPIException as e:
                code = getattr(e, "status_code", None) or 0
                if code in (401, 403, 404, 429):
                    return default() if callable(default) else default
                raise
        return wrapper
    return deco


class FinnhubSource:
    def __init__(self, api_key: str | None = None):
        key = api_key or settings.finnhub_api_key
        if not key:
            raise RuntimeError("FINNHUB_API_KEY 未配置，请在 .env 中填入。")
        self.client = finnhub.Client(api_key=key)

    # -------- 报价 / 公司基础 --------
    @_safe(default=dict)
    def quote(self, symbol: str) -> dict[str, Any]:
        return self.client.quote(symbol)

    @_safe(default=dict)
    def profile(self, symbol: str) -> dict[str, Any]:
        return self.client.company_profile2(symbol=symbol)

    # -------- 催化剂 --------
    @_safe(default=list)
    def recommendations(self, symbol: str) -> list[dict[str, Any]]:
        return self.client.recommendation_trends(symbol)

    @_safe(default=dict)
    def price_target(self, symbol: str) -> dict[str, Any]:
        """⚠️ 免费层无权限，返回空 dict。"""
        return self.client.price_target(symbol)

    @_safe(default=list)
    def upgrade_downgrade(
        self, symbol: str, days: int = 30
    ) -> list[dict[str, Any]]:
        """⚠️ 免费层无权限，返回空列表。"""
        end = date.today()
        start = end - timedelta(days=days)
        return self.client.upgrade_downgrade(
            symbol=symbol,
            _from=start.strftime("%Y-%m-%d"),
            to=end.strftime("%Y-%m-%d"),
        )

    @_safe(default=list)
    def earnings_calendar(
        self, symbol: str | None = None, days_ahead: int = 14
    ) -> list[dict[str, Any]]:
        start = date.today()
        end = start + timedelta(days=days_ahead)
        data = self.client.earnings_calendar(
            _from=start.strftime("%Y-%m-%d"),
            to=end.strftime("%Y-%m-%d"),
            symbol=symbol or "",
            international=False,
        )
        return data.get("earningsCalendar", []) if isinstance(data, dict) else []

    @_safe(default=list)
    def earnings_history(self, symbol: str) -> list[dict[str, Any]]:
        return self.client.company_earnings(symbol, limit=4)

    @_safe(default=list)
    def insider_transactions(self, symbol: str) -> list[dict[str, Any]]:
        """⚠️ 免费层可能受限。"""
        data = self.client.stock_insider_transactions(symbol, "", "")
        return data.get("data", []) if isinstance(data, dict) else []

    # -------- 关键事件 / 新闻 --------
    @_safe(default=list)
    def company_news(
        self, symbol: str, days: int = 3
    ) -> list[dict[str, Any]]:
        end = date.today()
        start = end - timedelta(days=days)
        return self.client.company_news(
            symbol,
            _from=start.strftime("%Y-%m-%d"),
            to=end.strftime("%Y-%m-%d"),
        )

    @_safe(default=list)
    def economic_calendar(self, days_ahead: int = 1) -> list[dict[str, Any]]:
        """⚠️ 免费层无权限。返回空列表，可改用其他源（如 te.com 爬虫）。"""
        data = self.client.calendar_economic()
        events = data.get("economicCalendar", []) if isinstance(data, dict) else []
        cutoff = date.today() + timedelta(days=days_ahead)
        return [
            e for e in events
            if str(e.get("time", ""))[:10] <= cutoff.strftime("%Y-%m-%d")
        ]

    # -------- 情绪 --------
    @_safe(default=dict)
    def news_sentiment(self, symbol: str) -> dict[str, Any]:
        return self.client.news_sentiment(symbol)

    @_safe(default=dict)
    def social_sentiment(
        self, symbol: str, days: int = 7
    ) -> dict[str, Any]:
        """Reddit + Twitter 综合社交情绪（Finnhub `/stock/social-sentiment`）。

        ⚠️ 免费层在 2022 年后逐步收紧，部分账户会返回 403/空。
        返回结构：
          {
            "symbol": "GOOGL",
            "reddit": [{"atTime", "mention", "positiveScore", "negativeScore",
                         "positiveMention", "negativeMention", "score"}, ...],
            "twitter": [...],
          }
        """
        end = date.today()
        start = end - timedelta(days=days)
        # finnhub-python 暴露的方法名为 stock_social_sentiment
        getter = getattr(self.client, "stock_social_sentiment", None)
        if getter is None:
            return {}
        return getter(
            symbol,
            _from=start.strftime("%Y-%m-%d"),
            to=end.strftime("%Y-%m-%d"),
        )
