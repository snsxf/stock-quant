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
        price = None
        prev_close = None
        market_cap = None
        pe = None
        target_mean = None
        recommendation = None
        last_err = None

        # 1) fast_info — 对指数 / 期货 / 加密最稳，开销最小
        try:
            fi = t.fast_info
            price = getattr(fi, "last_price", None)
            prev_close = getattr(fi, "previous_close", None)
            market_cap = getattr(fi, "market_cap", None)
        except Exception as e:
            last_err = e

        # 2) info — 个股估值字段（指数/期货上多半缺失，允许失败）
        try:
            info = t.info or {}
            if price is None:
                price = info.get("currentPrice") or info.get("regularMarketPrice")
            if prev_close is None:
                prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
            market_cap = market_cap or info.get("marketCap")
            pe = info.get("trailingPE")
            target_mean = info.get("targetMeanPrice")
            recommendation = info.get("recommendationKey")
        except Exception as e:
            last_err = e

        # 3) history — 终极兜底
        if price is None:
            try:
                hist = t.history(period="5d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
                    if prev_close is None and len(hist) >= 2:
                        prev_close = float(hist["Close"].iloc[-2])
            except Exception as e:
                last_err = e

        if price is None:
            raise RuntimeError(
                f"yfinance 行情失败 ({symbol}): "
                f"fast_info/info/history 全部未返回价格"
                + (f"; last_err={type(last_err).__name__}: {last_err}" if last_err else "")
            )

        change_rate = None
        if prev_close and price:
            try:
                change_rate = round((price - prev_close) / prev_close * 100, 4)
            except Exception:
                change_rate = None

        return {
            "symbol": symbol,
            "price": price,
            "prev_close": prev_close,
            "change_rate": change_rate,
            "market_cap": market_cap,
            "pe": pe,
            "target_mean": target_mean,
            "recommendation": recommendation,
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
