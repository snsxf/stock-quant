"""StockTwits 情绪：公开 REST API（带浏览器 UA 绕过 403）。"""
from __future__ import annotations

import httpx

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def fetch_symbol_stream(symbol: str, limit: int = 30, timeout: float = 8.0) -> dict:
    """
    拉取某标的最新讨论流，包含每条消息的 bullish/bearish 情绪标签。
    """
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
    try:
        r = httpx.get(url, timeout=timeout, headers=_HEADERS, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {
            "error": f"{type(e).__name__}: {e}",
            "messages": [], "bull_count": 0, "bear_count": 0, "total": 0,
        }

    messages = data.get("messages", [])[:limit]
    bull = bear = 0
    for m in messages:
        s = (m.get("entities", {}) or {}).get("sentiment", {}) or {}
        tag = (s.get("basic") or "").lower()
        if tag == "bullish":
            bull += 1
        elif tag == "bearish":
            bear += 1
    return {
        "symbol": symbol,
        "total": len(messages),
        "bull_count": bull,
        "bear_count": bear,
        "bull_ratio": bull / max(bull + bear, 1),
        "messages": [
            {
                "body": m.get("body"),
                "created_at": m.get("created_at"),
                "sentiment": (m.get("entities", {}) or {}).get("sentiment", {}).get("basic"),
            }
            for m in messages
        ],
    }
