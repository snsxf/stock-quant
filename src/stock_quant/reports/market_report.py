"""大盘模式（Market Report）：大盘情绪 + 股票池扫描与期权推荐。"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from typing import Any
import pandas as pd

from ..datasource.market_index import get_market_snapshot, interpret_vix
from .daily_brief import build_brief
from .option_decision import evaluate_direction, decide


WATCHLIST = [
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA",
    "AMD", "AVGO", "TSM", "NFLX", "PLTR"
]

LIGHTWEIGHT_THRESHOLD = 7
LIGHTWEIGHT_MAX_WORKERS = 4
FULL_MAX_WORKERS = 2
SCAN_BUDGET_SEC = 55


def _scan_symbol(sym: str, shared_market_env: dict[str, Any], *, lightweight: bool) -> dict[str, Any]:
    brief = build_brief(sym, lightweight=lightweight, shared_market_env=shared_market_env)
    spot = (brief.get("quote", {}).get("futu") or {}).get("price")
    pre = (brief.get("quote", {}).get("futu") or {}).get("pre_market") or {}
    after = (brief.get("quote", {}).get("futu") or {}).get("after_market") or {}

    direction = evaluate_direction(brief)
    return {
        "symbol": sym,
        "spot": spot,
        "pre_market": pre,
        "after_market": after,
        "score": direction.score,
        "label": direction.label,
        "breakdown": direction.breakdown,
        "elapsed_sec": brief.get("_elapsed_sec"),
        "lightweight": lightweight,
    }


def build_market_report(watchlist: list[str] | None = None) -> dict[str, Any]:
    """生成大盘报告与个股推荐。"""
    t0 = time.time()
    watchlist = watchlist or WATCHLIST
    lightweight = len(watchlist) > LIGHTWEIGHT_THRESHOLD
    
    # 1. 大盘指数
    indices = get_market_snapshot()
    vix_val = (indices.get("VIX") or {}).get("price")
    vix_regime = interpret_vix(vix_val)
    shared_market_env = {
        "_source": "shared market_report snapshot; reused by per-symbol scans",
        "indices": indices,
        "vix_regime": vix_regime,
    }
    
    # 2. 个股横向扫描：大池子默认轻量模式，避免 news/fundamentals/options 串行 IO。
    stocks_info = []
    scan_errors = []
    max_workers = LIGHTWEIGHT_MAX_WORKERS if lightweight else FULL_MAX_WORKERS
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="market-report")
    futures = {
        executor.submit(_scan_symbol, sym, shared_market_env, lightweight=lightweight): sym
        for sym in watchlist
    }
    try:
        for fut in as_completed(futures, timeout=SCAN_BUDGET_SEC):
            sym = futures[fut]
            try:
                stocks_info.append(fut.result())
            except Exception as e:
                scan_errors.append({"symbol": sym, "error": f"{type(e).__name__}: {e}"})
    except TimeoutError:
        pending = [sym for fut, sym in futures.items() if not fut.done()]
        scan_errors.append({
            "type": "scan_timeout",
            "budget_sec": SCAN_BUDGET_SEC,
            "pending": pending,
        })
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
            
    # 按多空打分排序
    stocks_info.sort(key=lambda x: x["score"], reverse=True)
    
    top_bullish = [s for s in stocks_info if s["score"] > 0][:3]
    top_bearish = [s for s in stocks_info if s["score"] < 0][-3:]
    
    # 3. 针对最强股票给出期权策略建议；大池轻量扫描时默认跳过。
    recommended_strategy = {}
    if lightweight:
        recommended_strategy["_skipped"] = {
            "reason": f"watchlist size {len(watchlist)} > {LIGHTWEIGHT_THRESHOLD}; "
                      "skip option_decision by default to avoid heavy IO",
        }
    else:
        if top_bullish:
            best_bull = top_bullish[0]["symbol"]
            try:
                recommended_strategy[best_bull] = decide(best_bull)
            except Exception:
                pass

        if top_bearish:
            best_bear = top_bearish[-1]["symbol"]
            try:
                recommended_strategy[best_bear] = decide(best_bear)
            except Exception:
                pass

    return {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_elapsed_sec": round(time.time() - t0, 2),
        "mode": "lightweight" if lightweight else "full",
        "watchlist_size": len(watchlist),
        "scan_errors": scan_errors,
        "_source": {
            "indices": "futu (primary) + yfinance (fallback) via get_market_snapshot",
            "vix_regime": "computed: VIX threshold mapping",
            "stocks": (
                "lightweight build_brief(quote + signals + shared market_env) → DirectionScore"
                if lightweight
                else "per-symbol build_brief(shared market_env) → DirectionScore"
            ),
            "top_bullish/top_bearish": "computed: stocks sorted by DirectionScore",
            "recommended_strategy": (
                "skipped in lightweight mode"
                if lightweight
                else "computed: option_decision.decide() — IV regime + Max Pain + term structure"
            ),
        },
        "indices": indices,
        "vix_regime": vix_regime,
        "stocks": stocks_info,
        "top_bullish": top_bullish,
        "top_bearish": top_bearish,
        "recommended_strategy": recommended_strategy,
    }


def _fmt_pct(v) -> str:
    return f"{v:+.2f}%" if v is not None else "-"

def _fmt_money(v) -> str:
    return f"${v:.2f}" if v is not None else "-"

def format_market_report(data: dict[str, Any]) -> str:
    lines = []
    lines.append("=" * 80)
    lines.append(f"  🌍 市场全景报告   {data['generated_at']}   (耗时 {data['_elapsed_sec']}s)")
    lines.append("=" * 80)
    
    # 大盘
    lines.append("\n📈 【大盘指数】")
    idx = data.get("indices", {})
    vix = idx.get("VIX", {})
    lines.append(f"  VIX: {_fmt_money(vix.get('price'))} ({_fmt_pct(vix.get('change_rate'))}) → 情绪: {data.get('vix_regime')}")
    
    for tk in ["SPY", "QQQ", "DIA", "IWM", "SMH", "SOXX"]:
        v = idx.get(tk)
        if v and v.get("price"):
            lines.append(f"  {tk:5s} {_fmt_money(v.get('price')):>8}  涨跌 {_fmt_pct(v.get('change_rate'))}")
            
    # 股票扫描
    lines.append("\n📊 【科技核心票扫描】(按多空动能排序)")
    for s in data["stocks"]:
        spot_str = _fmt_money(s['spot'])
        pre_str = f"盘前 {_fmt_money(s['pre_market'].get('price'))} ({_fmt_pct(s['pre_market'].get('change_rate'))})" if s.get('pre_market') and s['pre_market'].get('price') else ""
        after_str = f"盘后 {_fmt_money(s['after_market'].get('price'))} ({_fmt_pct(s['after_market'].get('change_rate'))})" if s.get('after_market') and s['after_market'].get('price') else ""
        ext_str = pre_str or after_str
        
        emoji = "🟢" if s['score'] >= 20 else ("🔴" if s['score'] <= -20 else "🟡")
        lines.append(f"  {s['symbol']:5s} | 现价 {spot_str:>8} {ext_str:25s} | 得分 {s['score']:+5.1f} {emoji} {s['label']}")
        
    # 期权推荐
    lines.append("\n💡 【系统期权策略精选】")
    for sym, st in data.get("recommended_strategy", {}).items():
        if sym == "_skipped" or st.get("_skipped"):
            lines.append(f"  (已跳过：{st.get('reason', 'lightweight mode')})")
            continue
        if "_error" in st:
            continue
        lines.append("-" * 80)
        lines.append(f"  🔥 {sym} 推荐策略 (方向得分 {st['direction']['score']})")
        strats = st.get("strategies", [])
        if not strats:
            lines.append("  (无合适期权策略)")
        for i, strat in enumerate(strats, 1):
            lines.append(f"  [策略{i}] {strat['name']}")
            for leg in strat['legs']:
                lines.append(f"     {leg['action']:4s} {leg['type']:4s} K={leg['strike']} 到期={leg['expiry']} Mid={leg['premium_mid']:.2f}")
            lines.append(f"     净收/付: {abs(strat['net_debit']):.2f} | 盈亏平衡: {strat['breakeven']}")

    lines.append("=" * 80)
    return "\n".join(lines)
