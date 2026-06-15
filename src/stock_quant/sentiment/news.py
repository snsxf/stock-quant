"""基于 Google News RSS 与 Yahoo 的免费新闻聚合（带超时保护）。"""
from __future__ import annotations

from urllib.parse import quote_plus

import feedparser
import httpx


def google_news(
    query: str, limit: int = 10, lang: str = "en-US", region: str = "US",
    timeout: float = 8.0,
) -> list[dict]:
    """Google News RSS（完全免费无 Key）。带 httpx 超时，避免 feedparser 卡住。"""
    q = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid={region}:en"
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {e}"}]
    return [
        {
            "title": e.get("title"),
            "link": e.get("link"),
            "published": e.get("published"),
            "source": e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None,
        }
        for e in feed.entries[:limit]
    ]


def yahoo_news_via_yfinance(symbol: str, limit: int = 10) -> list[dict]:
    """借 yfinance.Ticker.news 拿 Yahoo 新闻。"""
    import yfinance as yf

    items = yf.Ticker(symbol).news[:limit]
    out = []
    for n in items:
        content = n.get("content", n) or {}
        out.append(
            {
                "title": content.get("title") or n.get("title"),
                "link": (content.get("canonicalUrl") or {}).get("url") or n.get("link"),
                "provider": (content.get("provider") or {}).get("displayName") or n.get("publisher"),
                "published": content.get("pubDate") or n.get("providerPublishTime"),
            }
        )
    return out
