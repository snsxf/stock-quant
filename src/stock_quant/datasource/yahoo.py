import pandas as pd
import yfinance as yf

from .base import DataSource


_YF_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

# yfinance 0.2.6x+ 强制使用 curl_cffi；requests_cache/requests_ratelimiter
# session 会触发 YFDataException。不要向 yf.Ticker 传 requests session。
_SESSION = None


class YahooSource(DataSource):
    """yfinance 数据源：免费、15min 延迟、美股优先。"""

    def _ticker(self, symbol: str) -> yf.Ticker:
        return yf.Ticker(symbol)

    def get_quote(self, symbol: str) -> dict:
        t = self._ticker(symbol)
        try:
            info = t.info
        except Exception:
            info = {}
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if price is None:
            try:
                hist = t.history(period="5d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
            except Exception:
                pass
        return {
            "symbol": symbol,
            "price": price,
            "market_cap": info.get("marketCap"),
            "pe": info.get("trailingPE"),
            "target_mean": info.get("targetMeanPrice"),
            "recommendation": info.get("recommendationKey"),
        }

    def get_history(
        self, symbol: str, period: str = "3mo", interval: str = "1d"
    ) -> pd.DataFrame:
        return self._ticker(symbol).history(period=period, interval=interval)

    def get_option_chain(
        self, symbol: str, expiry: str | None = None
    ) -> pd.DataFrame:
        t = self._ticker(symbol)
        if not t.options:
            return pd.DataFrame()
        expiry = expiry or t.options[0]
        chain = t.option_chain(expiry)
        calls = chain.calls.assign(option_type="CALL")
        puts = chain.puts.assign(option_type="PUT")
        df = pd.concat([calls, puts], ignore_index=True)
        df["expiry"] = expiry
        cols = [
            "expiry", "option_type", "strike", "lastPrice", "bid", "ask",
            "impliedVolatility", "openInterest", "volume",
        ]
        return df[[c for c in cols if c in df.columns]]
