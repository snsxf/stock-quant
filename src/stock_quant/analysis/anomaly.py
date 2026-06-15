"""富途异动信号统一入口（对齐 Layer 3 Anomaly Skills）。

对齐富途官方 anomaly-skills 三件套：
  - futu-capital-anomaly       → capital_anomaly()       → get_financial_unusual
  - futu-derivatives-anomaly   → derivatives_anomaly()   → get_derivative_unusual
  - futu-technical-anomaly     → technical_anomaly()     → get_technical_unusual

设计原则：
  1. 命名/参数完全对齐富途 SKILL.md，避免再次出现"双 API 系统"的概念混淆
  2. 接口失败时返回 {"ok": False, "error": ...}，而不是抛异常，便于 daily_brief 集成
  3. 所有响应都标准化为 {ok, method, stock_symbol, time_range, ..., data: {time_range, content}}
  4. content 是富途服务端预渲染好的自然语言总结，直接展示即可，无需二次解析
"""
from __future__ import annotations

from typing import Any


# ============================================================
# 维度白名单（与富途 SKILL.md 完全一致，便于校验/IDE 提示）
# ============================================================
CAPITAL_DIMENSIONS = {
    "funds_distribution",
    "funds_broker",
    "funds_flow",
    "short_sell_number",
    "short_sell_ratio",
    "short_sell_number_and_ratio",
}

DERIVATIVES_DIMENSIONS = {
    "warrant_ratio",
    "warrant_price_distribution",
    "option_unusual",
    "option_volatility",
    "option_volume_price",
    "option_sentiment",
    "option_comprehensive",
}

TECHNICAL_INDICATORS = {
    "CCI", "KDJ", "BIAS", "AR", "BR", "VR", "PSY", "OSC", "WMSR",
    "MACD", "BOLL", "MA", "RSI6", "RSI12", "RSI24",
}

LANG_MAP = {
    "zh-CN": 0, "zh": 0, "cn": 0,
    "zh-TW": 1, "zh-HK": 1, "tw": 1, "hk": 1,
    "en": 2, "en-US": 2,
    "th": 4, "ja": 5,
}


def _normalize_symbol(symbol: str, default_market: str = "US") -> str:
    """裸 ticker → US.XXX；保持已有前缀不变。"""
    if "." in symbol:
        return symbol.upper()
    return f"{default_market.upper()}.{symbol.upper()}"


def _resolve_lang(lang: str | int | None) -> int:
    if lang is None:
        return 0
    if isinstance(lang, int):
        return lang
    return LANG_MAP.get(lang, 0)


def _futu_source_with_method(method_name: str):
    """Lazy import avoids stale class bindings in long-running MCP workers."""
    import inspect

    from ..datasource.futu import FutuSource

    if hasattr(FutuSource, method_name):
        return FutuSource()

    available = [name for name in dir(FutuSource) if "unusual" in name]
    return {
        "ok": False,
        "method": method_name,
        "error": (
            f"FutuSource missing {method_name}; imported_from={inspect.getfile(FutuSource)}; "
            f"available_unusual_methods={available}"
        ),
    }


# ============================================================
# 1. 资金面异动（capital）
# ============================================================
def capital_anomaly(
    symbol: str,
    time_range: int = 7,
    analysis_dimensions: list[str] | None = None,
    language_id: str | int | None = "en",
) -> dict[str, Any]:
    """资金面异动检测。

    See: docs/futu_skills_reference futu-capital-anomaly/SKILL.md
    """
    if analysis_dimensions:
        bad = [d for d in analysis_dimensions if d not in CAPITAL_DIMENSIONS]
        if bad:
            return {
                "ok": False,
                "method": "get_financial_unusual",
                "error": f"unknown analysis_dimensions: {bad} (valid: {sorted(CAPITAL_DIMENSIONS)})",
            }

    src = _futu_source_with_method("get_financial_unusual")
    if isinstance(src, dict):
        return src
    return src.get_financial_unusual(
        symbol=_normalize_symbol(symbol),
        time_range=time_range,
        analysis_dimensions=analysis_dimensions,
        language_id=_resolve_lang(language_id),
    )


# ============================================================
# 2. 衍生品异动（derivatives）
# ============================================================
def derivatives_anomaly(
    symbol: str,
    time_range: int = 7,
    analysis_dimensions: list[str] | None = None,
    language_id: str | int | None = "en",
) -> dict[str, Any]:
    """衍生品异动检测（期权大单 / IV / PCR / 牛熊证街货等）。

    See: docs/futu_skills_reference futu-derivatives-anomaly/SKILL.md
    """
    if analysis_dimensions:
        bad = [d for d in analysis_dimensions if d not in DERIVATIVES_DIMENSIONS]
        if bad:
            return {
                "ok": False,
                "method": "get_derivative_unusual",
                "error": f"unknown analysis_dimensions: {bad} (valid: {sorted(DERIVATIVES_DIMENSIONS)})",
            }

    src = _futu_source_with_method("get_derivative_unusual")
    if isinstance(src, dict):
        return src
    return src.get_derivative_unusual(
        symbol=_normalize_symbol(symbol),
        time_range=time_range,
        analysis_dimensions=analysis_dimensions,
        language_id=_resolve_lang(language_id),
    )


# ============================================================
# 3. 技术面异动（technical）
# ============================================================
def technical_anomaly(
    symbol: str,
    time_range: int = 7,
    indicator_filters: list[str] | None = None,
    language_id: str | int | None = "en",
) -> dict[str, Any]:
    """技术面异动检测（K 线形态 + 14 种指标信号）。

    See: docs/futu_skills_reference futu-technical-anomaly/SKILL.md
    """
    if indicator_filters:
        bad = [d for d in indicator_filters if d not in TECHNICAL_INDICATORS]
        if bad:
            return {
                "ok": False,
                "method": "get_technical_unusual",
                "error": f"unknown indicator_filters: {bad} (valid: {sorted(TECHNICAL_INDICATORS)})",
            }

    src = _futu_source_with_method("get_technical_unusual")
    if isinstance(src, dict):
        return src
    return src.get_technical_unusual(
        symbol=_normalize_symbol(symbol),
        time_range=time_range,
        indicator_filters=indicator_filters,
        language_id=_resolve_lang(language_id),
    )


# ============================================================
# 4. 一键三连（broad anomaly check）
# ============================================================
def full_anomaly_scan(
    symbol: str,
    time_range: int = 7,
    language_id: str | int | None = "en",
) -> dict[str, Any]:
    """全维度异动扫描，并发跑三件套。

    返回：
      {
        "stock_symbol":  "US.NVDA",
        "time_range":    7,
        "capital":       {ok, content, ...},
        "derivatives":   {ok, content, ...},
        "technical":     {ok, content, ...},
      }

    异常隔离：任意一类失败不影响其他类，统一在结果中以 ok=False 体现。
    """
    from concurrent.futures import ThreadPoolExecutor

    sym = _normalize_symbol(symbol)
    lang = _resolve_lang(language_id)

    tasks = {
        "capital": lambda: capital_anomaly(sym, time_range, language_id=lang),
        "derivatives": lambda: derivatives_anomaly(sym, time_range, language_id=lang),
        "technical": lambda: technical_anomaly(sym, time_range, language_id=lang),
    }

    out: dict[str, Any] = {"stock_symbol": sym, "time_range": time_range}
    # OpenD 并发受全局 RLock 串行化，这里 max_workers=3 不会真正并发执行，
    # 但能让三个调用排队，main 线程不阻塞过久；并保留接口语义。
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {key: ex.submit(fn) for key, fn in tasks.items()}
        for key, fut in futures.items():
            try:
                out[key] = fut.result(timeout=20)
            except Exception as e:
                out[key] = {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                }
    return out
