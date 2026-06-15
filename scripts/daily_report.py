"""stock-quant 自检脚本（精简版）：仅富途数据，秒级出报告。

通过环境变量可启用扩展模块：
  ENABLE_SENTIMENT=1  打开 StockTwits
  ENABLE_NEWS=1       打开 Google News
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

from stock_quant import (
    FutuSource,
    get_source,
    greeks,
    latest_signals,
    logger,
    max_pain,
)


SYMBOL_US = "US.NVDA"
SYMBOL_HK = "HK.00700"
ENABLE_SENTIMENT = os.getenv("ENABLE_SENTIMENT") == "1"
ENABLE_NEWS = os.getenv("ENABLE_NEWS") == "1"


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}", flush=True)


@contextmanager
def timed(label: str):
    print(f"  ⏳ {label} ...", flush=True, end="")
    t0 = time.time()
    try:
        yield
        print(f" ✅ {time.time() - t0:.2f}s", flush=True)
    except Exception as e:
        print(f" ❌ {time.time() - t0:.2f}s | {type(e).__name__}: {e}", flush=True)


def _futu_alive() -> bool:
    print("🔌 探测富途 OpenD ...", flush=True)
    try:
        t0 = time.time()
        FutuSource().get_quote("HK.00700")
        print(f"   ✅ OpenD 在线 ({time.time() - t0:.2f}s)", flush=True)
        return True
    except Exception as e:
        logger.warning(f"   ❌ OpenD 不可达: {e}")
        return False


def analyze(symbol: str, futu_available: bool, with_options: bool = True) -> None:
    src, real_symbol = get_source(symbol, futu_available=futu_available)
    section(f"{symbol}  ({src.__class__.__name__} → {real_symbol})")

    with timed("Quote"):
        print("\n   ", src.get_quote(real_symbol), flush=True, end="")

    with timed("History 3mo"):
        hist = src.get_history(real_symbol, period="3mo")
        if not hist.empty:
            rename_map = {c: c.capitalize() for c in ["open", "high", "low", "close", "volume"] if c in hist.columns}
            if rename_map:
                hist = hist.rename(columns=rename_map)
            print("\n   ", latest_signals(hist), flush=True, end="")

    if with_options:
        with timed("Option chain (top 10)"):
            chain = src.get_option_chain(real_symbol) if hasattr(src, "get_option_chain") else None
            if chain is not None and not chain.empty:
                print("\n", chain.head(10), flush=True)
                if "strike" in chain.columns:
                    print("   📍 Max Pain:", max_pain(chain), flush=True)


def run() -> None:
    t_start = time.time()
    futu_ok = _futu_alive()

    analyze(SYMBOL_US, futu_ok, with_options=True)
    analyze(SYMBOL_HK, futu_ok, with_options=False)

    section("BSM Greeks 示例 (S=120, K=125, 30D, r=5%, σ=50%)")
    with timed("greeks"):
        g = greeks("call", S=120, K=125, T=30 / 365, r=0.05, sigma=0.5)
        print("\n   ", g, flush=True, end="")

    if ENABLE_SENTIMENT:
        from stock_quant import fetch_symbol_stream

        section("StockTwits 情绪 (NVDA)")
        with timed("StockTwits"):
            s = fetch_symbol_stream("NVDA", limit=20)
            print("\n   ", {k: v for k, v in s.items() if k != "messages"}, flush=True, end="")

    if ENABLE_NEWS:
        from stock_quant import google_news

        section("Google News (NVDA, top 5)")
        with timed("Google News"):
            for n in google_news("NVDA", limit=5):
                print(f"\n    - {n.get('title')} ({n.get('source')})", flush=True, end="")
        print()

    print(f"\n\n🎯 总耗时: {time.time() - t_start:.2f}s", flush=True)
    if not (ENABLE_SENTIMENT or ENABLE_NEWS):
        print("💡 如需情绪/新闻，运行: ENABLE_SENTIMENT=1 ENABLE_NEWS=1 uv run python scripts/daily_report.py", flush=True)


if __name__ == "__main__":
    run()
