from .config import settings
from .datasource import (
    DataSource, YahooSource, FutuSource,
    detect_market, to_yahoo_symbol, to_futu_symbol, get_source,
)
from .analysis import enrich_all, latest_signals, greeks, price_bs, iv_from_price, max_pain
from .sentiment import fetch_symbol_stream, google_news, yahoo_news_via_yfinance
from .utils import logger

__all__ = [
    "settings", "logger",
    "DataSource", "YahooSource", "FutuSource",
    "detect_market", "to_yahoo_symbol", "to_futu_symbol", "get_source",
    "enrich_all", "latest_signals",
    "greeks", "price_bs", "iv_from_price", "max_pain",
    "fetch_symbol_stream", "google_news", "yahoo_news_via_yfinance",
]
__version__ = "0.1.0"


def hello() -> str:
    return "Hello from stock-quant!"
