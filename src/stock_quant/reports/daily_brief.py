"""每日盘前/盘中简报生成器。

调用方式：
    from stock_quant.reports.daily_brief import build_brief, format_brief
    data = build_brief("NVDA")
    print(format_brief(data))

或 CLI:
    stock-quant brief NVDA
"""
from __future__ import annotations

import time
import traceback
from typing import Any

import pandas as pd

from ..analysis.technical import enrich_all
from ..catalyst import (
    price_target_summary,
    rating_consensus,
    recent_earnings_surprise,
    recent_insider_summary,
    upcoming_earnings,
)
from ..datasource import FutuSource
from ..datasource.finnhub import FinnhubSource
from ..datasource.market_index import (
    DEFAULT_TICKERS,
    get_market_snapshot,
    interpret_vix,
)
from ..datasource.router import to_futu_symbol
from ..datasource.yahoo_extras import forward_valuation as _forward_valuation
from ..flow import (
    _atm_iv_from_chain,
    build_term_structure,
    calc_iv_rank,
    calc_max_pain,
    chain_summary,
    scan_unusual,
)


_FUND_OR_INDEX_SYMBOLS = {
    "DIA", "GLD", "IWM", "NASA", "QQQ", "SMH", "SOXL", "SOXX", "SPY",
    "SPMO", "TLT", "VIX", "VVIX", "XLE", "XLF", "XLI", "XLK", "XLP",
    "XLRE", "XLU", "XLV", "XLY",
    "^DJI", "^GSPC", "^IXIC", "^RUT", "^TNX", "^TYX", "^VIX", "^VVIX",
}


def _base_symbol(symbol: str) -> str:
    """Normalize US.FOO / FOO into FOO for lightweight asset classification."""
    return symbol.upper().split(".", 1)[-1] if "." in symbol else symbol.upper()


def _is_fund_or_index(symbol: str) -> bool:
    """ETF/指数类不跑个股 fundamentals / earnings / forecast 路径。"""
    base = _base_symbol(symbol)
    return base in _FUND_OR_INDEX_SYMBOLS or base.startswith("^")


def _safe(fn, default=None, label: str = ""):
    """统一的容错执行：失败返回 default 并记录错误。"""
    try:
        return fn()
    except Exception as e:
        return {"_error": f"{label}: {type(e).__name__}: {e}"} if isinstance(default, dict) else default


def _normalize_kline(df: pd.DataFrame) -> pd.DataFrame:
    """Futu 列名（小写）→ ta 库期望的列名（大写首字母）。"""
    rename_map = {
        "open": "Open",
        "close": "Close",
        "high": "High",
        "low": "Low",
        "volume": "Volume",
    }
    cols = {k: v for k, v in rename_map.items() if k in df.columns}
    return df.rename(columns=cols)


# ============================================================
# 各板块构建函数
# ============================================================
def _section_quote(futu_sym: str, finnhub_sym: str, *, include_valuation_extras: bool = True) -> dict[str, Any]:
    """报价 + 估值（Futu 主源 + yfinance 前瞻估值补全）。

    2026-05 起：Finnhub `/quote` 已与 Futu `get_market_snapshot` 完全冗余，
    且 Futu 字段更全（pre/after/overnight/52w/估值），故移除 finnhub 副板。
    """
    fs = FutuSource()

    futu_q = _safe(lambda: fs.get_quote(futu_sym), default={}, label="futu_quote")

    if not include_valuation_extras:
        return {
            "_source": {
                "quote_primary": "futu/get_market_snapshot",
                "valuation_primary": "futu/get_market_snapshot",
                "valuation_extras": "skipped for ETF/index or lightweight scan",
            },
            "futu": futu_q,
        }

    # 补充 Forward PE / PEG（来自 yfinance.info）
    fwd_val = _safe(
        lambda: _forward_valuation(finnhub_sym), default=None, label="forward_pe"
    )
    if fwd_val and isinstance(futu_q, dict):
        val = futu_q.get("valuation") or {}
        # 尊重 Futu 已有的 pe_static / pe_ttm；仅补充 forward / peg / 衍生口径
        val.setdefault("pe_forward", fwd_val.get("pe_forward"))
        val.setdefault("pe_trailing_yf", fwd_val.get("pe_trailing"))
        val.setdefault("eps_forward", fwd_val.get("eps_forward"))
        val.setdefault("eps_trailing_yf", fwd_val.get("eps_trailing"))
        val.setdefault("peg_ratio", fwd_val.get("peg_ratio"))
        val.setdefault("price_to_sales_ttm", fwd_val.get("price_to_sales_ttm"))
        val.setdefault("price_to_book_yf", fwd_val.get("price_to_book"))
        # 修复 pe_static == pe_ttm 的 Futu 数据 Bug：
        # 若两者完全相等，且能拿到 yfinance 的 trailing_pe，认为 pe_ttm 更准
        try:
            pe_ttm = val.get("pe_ttm")
            pe_static = val.get("pe_static")
            yf_trailing = fwd_val.get("pe_trailing")
            if (
                pe_ttm is not None
                and pe_static is not None
                and abs(float(pe_ttm) - float(pe_static)) < 1e-6
                and yf_trailing is not None
                and abs(float(yf_trailing) - float(pe_ttm)) > 0.5
            ):
                val["_pe_warn"] = (
                    "futu pe_static == pe_ttm，疑似数据源未区分；"
                    f"yfinance trailingPE={yf_trailing}"
                )
        except Exception:
            pass
        futu_q["valuation"] = val

    return {
        "_source": {
            "quote_primary": "futu/get_market_snapshot",
            "valuation_primary": "futu/get_market_snapshot",
            "valuation_extras": "yfinance.info / stockanalysis (forward_pe / peg / ps / pb)",
        },
        "futu": futu_q,
    }


def _section_technical(futu_sym: str) -> dict[str, Any]:
    fs = FutuSource()
    df = _safe(lambda: fs.get_history(futu_sym, period="6mo", interval="1d"), default=pd.DataFrame(), label="kline")
    if df is None or df.empty:
        return {"_error": "K线数据为空"}

    df = _normalize_kline(df)
    enriched = enrich_all(df)
    last = enriched.iloc[-1]
    prev = enriched.iloc[-2] if len(enriched) > 1 else last

    # 历史波动率 HV20
    returns = df["Close"].pct_change().dropna().tail(20)
    hv20 = float(returns.std() * (252 ** 0.5) * 100) if len(returns) > 1 else None

    # 简单支撑/阻力：近 60 日的 0.25/0.75 分位
    recent = df["Close"].tail(60)
    support = float(recent.quantile(0.25))
    resistance = float(recent.quantile(0.75))

    # 涨跌幅
    def _pct(n):
        if len(df) <= n:
            return None
        return round((df["Close"].iloc[-1] / df["Close"].iloc[-1 - n] - 1) * 100, 2)

    return {
        "_source": {
            "kline": "futu/request_history_kline (6mo daily)",
            "indicators": "stock_quant.analysis.technical (ta lib)",
            "support_resistance": "computed: 60d Close quantile(0.25/0.75)",
            "hv20": "computed: 20d Close pct_change std × √252",
        },
        "close": float(last["Close"]),
        "ma5": float(last.get("MA5")) if pd.notna(last.get("MA5")) else None,
        "ma20": float(last.get("MA20")) if pd.notna(last.get("MA20")) else None,
        "ma50": float(last.get("MA50")) if pd.notna(last.get("MA50")) else None,
        "ma200": float(last.get("MA200")) if pd.notna(last.get("MA200")) else None,
        "rsi14": round(float(last.get("RSI14")), 2) if pd.notna(last.get("RSI14")) else None,
        "macd_hist": round(float(last.get("MACD_HIST")), 4) if pd.notna(last.get("MACD_HIST")) else None,
        "macd_cross": (
            "golden" if last.get("MACD_HIST", 0) > 0 and prev.get("MACD_HIST", 0) <= 0
            else "death" if last.get("MACD_HIST", 0) < 0 and prev.get("MACD_HIST", 0) >= 0
            else "none"
        ),
        "bb_position": (
            "upper" if last["Close"] >= last.get("BB_HIGH", float("inf"))
            else "lower" if last["Close"] <= last.get("BB_LOW", -float("inf"))
            else "middle"
        ),
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "ret_5d": _pct(5),
        "ret_20d": _pct(20),
        "ret_60d": _pct(60),
        "hv20_pct": round(hv20, 2) if hv20 else None,
    }


def _section_options(futu_sym: str, spot: float | None = None) -> dict[str, Any]:
    fs = FutuSource()
    chain = _safe(
        lambda: fs.get_option_chain(futu_sym, max_contracts=None),
        default=pd.DataFrame(),
        label="option_chain",
    )
    if chain is None or chain.empty:
        return {"_error": "期权链为空（可能不是可期权标的）"}

    summary = chain_summary(chain)
    unusual = scan_unusual(chain, min_vol_oi_ratio=2.0, top_n=5)

    # Max Pain
    max_pain = _safe(lambda: calc_max_pain(chain), default={}, label="max_pain") or {}
    if spot and max_pain.get("max_pain"):
        max_pain["spot_to_max_pain_pct"] = round((spot - max_pain["max_pain"]) / max_pain["max_pain"] * 100, 2)

    # IV Rank（基于本地 SQLite 历史）
    iv_rank: dict[str, Any] = {}
    if spot is not None:
        atm_iv = _atm_iv_from_chain(chain, spot)
        if atm_iv:
            iv_rank = _safe(
                lambda: calc_iv_rank(futu_sym, atm_iv),
                default={},
                label="iv_rank",
            ) or {}

    return {
        "_source": {
            "chain": "futu/get_option_chain + get_market_snapshot (per contract IV/Greeks)",
            "summary": "computed: stock_quant.flow.chain_summary",
            "unusual": "computed: vol/oi ratio scan",
            "max_pain": "computed: stock_quant.flow.calc_max_pain",
            "iv_rank": "stock_quant.flow.iv_rank (local SQLite history)",
        },
        "summary": summary,
        "unusual": unusual,
        "max_pain": max_pain,
        "iv_rank": iv_rank,
        "n_contracts": len(chain),
        "option_chain_scope": {
            "snapshot_contracts": int(summary.get("snapshot_contracts") or len(chain)),
            "chain_total_contracts": int(summary.get("chain_total_contracts") or len(chain)),
            "is_truncated": bool(summary.get("is_truncated")) if summary.get("is_truncated") is not None else False,
        },
    }


def _section_term_structure(futu_sym: str) -> dict[str, Any]:
    """期权 IV 期限结构（前 5 个到期日）。"""
    fs = FutuSource()
    out = _safe(
        lambda: build_term_structure(fs, futu_sym, n_expiries=5),
        default={"_error": "term_structure 不可用"},
        label="term_structure",
    ) or {}
    if isinstance(out, dict) and "_error" not in out:
        out.setdefault("_source", "futu/get_option_expiration_date + get_option_chain + get_market_snapshot (ATM IV)")
    return out


def _section_market_env() -> dict[str, Any]:
    """市场环境：VIX + SPY/QQQ/SOXX/SMH 等板块 ETF。"""
    snap = _safe(
        lambda: get_market_snapshot(DEFAULT_TICKERS),
        default={},
        label="market_env",
    ) or {}
    vix_price = (snap.get("VIX") or {}).get("price")
    return {
        "_source": "per-ticker._source (futu primary, yfinance fallback); vix_regime: computed",
        "indices": snap,
        "vix_regime": interpret_vix(vix_price),
    }


def _section_capital_flow(futu_sym: str) -> dict[str, Any]:
    """Futu 资金流：调用 FutuSource.get_capital_flow（含 smart_money 聚合）。"""
    out = _safe(
        lambda: FutuSource().get_capital_flow(futu_sym) or {},
        default={"_error": "capital_flow 不可用"},
        label="capital_flow",
    )
    if isinstance(out, dict) and "_error" not in out:
        out.setdefault("_source", "futu/get_capital_flow")
    return out


def _section_catalyst(finnhub_sym: str) -> dict[str, Any]:
    return {
        "_source": {
            "rating": "finnhub/recommendation_trends (fallback: yahoo + stockanalysis)",
            "price_target": "stockanalysis.com (fallback: yahoo finance)",
            "earnings_next": "yahoo/Ticker.calendar (scheduled only; fallback: finnhub/earnings_calendar untrusted expected date)",
            "earnings_history": "yahoo/Ticker.earnings_dates (fallback: stockanalysis, then finnhub with warning)",
            "insider_90d": "yahoo/Ticker.insider_transactions (SEC Form 4, fallback: finnhub)",
        },
        "rating": _safe(lambda: rating_consensus(finnhub_sym), default={}, label="rating"),
        "price_target": _safe(lambda: price_target_summary(finnhub_sym), default={}, label="price_target"),
        "earnings_next": _safe(lambda: upcoming_earnings(finnhub_sym, days_ahead=120), default={}, label="earnings_next"),
        "earnings_history": _safe(lambda: recent_earnings_surprise(finnhub_sym), default=[], label="earnings_history"),
        "insider_90d": _safe(lambda: recent_insider_summary(finnhub_sym, days=90), default={}, label="insider"),
    }


def _skipped_section(reason: str) -> dict[str, Any]:
    return {"_skipped": True, "reason": reason}


def _section_news(finnhub_sym: str, limit: int = 8) -> list[dict[str, Any]]:
    """公司新闻（yfinance 主源 + finnhub fallback）。

    yfinance.Ticker.news 直接拉 Yahoo Finance 头条，免费无限速；
    Finnhub `/company-news` 作为兜底（免费层可用但有 1-2 小时延迟）。
    """
    out: list[dict[str, Any]] = []
    try:
        import yfinance as yf
        yf_news = yf.Ticker(finnhub_sym).news or []
        for n in yf_news[:limit]:
            content = n.get("content") if isinstance(n.get("content"), dict) else n
            title = content.get("title") or n.get("title") or ""
            provider = (
                (content.get("provider") or {}).get("displayName")
                if isinstance(content.get("provider"), dict)
                else n.get("publisher")
            )
            url = (
                (content.get("canonicalUrl") or {}).get("url")
                if isinstance(content.get("canonicalUrl"), dict)
                else n.get("link")
            )
            ts = content.get("pubDate") or n.get("providerPublishTime")
            out.append({
                "headline": (title or "")[:120],
                "source": provider,
                "url": url,
                "datetime": ts,
                "_source": "yahoo/Ticker.news",
            })
    except Exception:
        out = []

    if out:
        return out

    fh = FinnhubSource()
    items = _safe(lambda: fh.company_news(finnhub_sym, days=2), default=[], label="news") or []
    for n in items[:limit]:
        out.append({
            "headline": (n.get("headline") or "")[:120],
            "source": n.get("source"),
            "url": n.get("url"),
            "datetime": n.get("datetime"),
            "_source": "finnhub/company_news (fallback)",
        })
    return out


def _section_stock_digest(symbol: str, market: str, limit: int = 10) -> dict[str, Any]:
    """富途个股解读（对齐 `futu-stock-digest`）：官方 Skills 新闻聚合主源。"""
    from ..datasource.futu_skills import stock_digest_search
    from ..sentiment.query_expansion import build_news_queries

    lang = "zh-CN" if market.upper() in ("HK", "CN") else "en"
    items: list[dict[str, Any]] = []
    for i, spec in enumerate(build_news_queries(symbol, market=market, include_events=True)):
        q_lang = "zh-CN" if any("\u4e00" <= ch <= "\u9fff" for ch in spec.query) else lang
        batch = _safe(
            lambda q=spec.query, size=limit if i == 0 else 4, query_lang=q_lang: stock_digest_search(
                q, size=size, lang=query_lang
            ),
            default=[],
            label=f"stock_digest:{spec.query}",
        ) or []
        source = "futu-stock-digest-related" if spec.kind == "event" else "futu-stock-digest-alias" if spec.kind in ("alias", "name") else "futu-stock-digest"
        for it in batch:
            items.append({**it, "source": source, "_query": spec.query, "_query_kind": spec.kind})

    def _published_key(item: dict[str, Any]) -> int:
        try:
            return int(item.get("published") or 0)
        except Exception:
            return 0

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for it in sorted(items, key=_published_key, reverse=True):
        title = (it.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        deduped.append(it)
        if len(deduped) >= limit:
            break

    return {
        "_source": "futu-stock-digest (alias/related-event query expansion)",
        "source": "futu-stock-digest",
        "lang": lang,
        "items": deduped,
        "n": len(deduped),
    }


def _section_news_sentiment(symbol: str, market: str, name: str | None = None) -> dict[str, Any]:
    """富途风格资讯 + 情绪打分（融合 futunn / Google News，事件归因 + 看多/看空判定）。"""
    from ..sentiment.futu_news import get_stock_news_with_sentiment

    out = _safe(
        lambda: get_stock_news_with_sentiment(symbol, market=market, name=name, limit=12),
        default={"_error": "news_sentiment 不可用"},
        label="news_sentiment",
    )
    if isinstance(out, dict) and "_error" not in out:
        out.setdefault(
            "_source",
            "futu-skills + yahoo-finance (US) / google-news (zh+en); sentiment: rule-based event scoring",
        )
    return out


def _section_community_sentiment(symbol: str, market: str) -> dict[str, Any]:
    """富途社区 + StockTwits 情绪聚合：看多/看空/中性 + 热门主题。"""
    from ..sentiment.futu_community import get_community_sentiment

    out = _safe(
        lambda: get_community_sentiment(symbol, market=market),
        default={"_error": "community_sentiment 不可用"},
        label="community_sentiment",
    )
    if isinstance(out, dict) and "_error" not in out:
        out.setdefault(
            "_source",
            "US: futu-skills + stocktwits + reddit (PRAW) + finnhub-social; "
            "HK: futu-skills + futunn-html; "
            "CN: eastmoney-guba + xueqiu-heat + em-comment (akshare)",
        )
    return out


def _section_anomaly_scan(symbol: str, time_range: int = 7, language: str = "en") -> dict[str, Any]:
    """富途 Layer 3 异动信号三件套：资金面 / 衍生品 / 技术面。"""
    from ..analysis.anomaly import full_anomaly_scan

    out = _safe(
        lambda: full_anomaly_scan(symbol, time_range=time_range, language_id=language),
        default={"_error": "anomaly_scan 不可用"},
        label="anomaly_scan",
    )
    if isinstance(out, dict) and "_error" not in out:
        out.setdefault(
            "_source",
            "futu/get_financial_unusual + get_derivative_unusual + get_technical_unusual",
        )
    return out


# ============================================================
# 主入口
# ============================================================
def build_brief(
    symbol: str,
    *,
    lightweight: bool = False,
    shared_market_env: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    生成简报数据（结构化 dict，便于后续 JSON 化或喂给 LLM）。

    symbol: 既支持 'NVDA' 也支持 'US.NVDA' / '0700.HK' 等格式
    """
    t0 = time.time()
    futu_sym = symbol if "." in symbol else to_futu_symbol(symbol)
    finnhub_sym = symbol.split(".")[-1] if "." in symbol else symbol
    is_fund_or_index = _is_fund_or_index(finnhub_sym) or _is_fund_or_index(futu_sym)

    data: dict[str, Any] = {
        "symbol": symbol,
        "futu_symbol": futu_sym,
        "finnhub_symbol": finnhub_sym,
        "asset_class_hint": "fund_or_index" if is_fund_or_index else "equity",
        "lightweight": lightweight,
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    data["quote"] = _section_quote(
        futu_sym,
        finnhub_sym,
        include_valuation_extras=not lightweight and not is_fund_or_index,
    )
    spot_price = (data["quote"].get("futu") or {}).get("price")

    data["market_env"] = shared_market_env if shared_market_env is not None else _section_market_env()
    data["technical"] = _section_technical(futu_sym)

    if lightweight:
        reason = "lightweight market_report scan: quote + signals + shared market_env only"
        data["options"] = _skipped_section(reason)
        data["term_structure"] = _skipped_section(reason)
        data["capital_flow"] = _skipped_section(reason)
        data["catalyst"] = _skipped_section(reason)
        data["news"] = []
        data["stock_digest"] = _skipped_section(reason)
        data["news_sentiment"] = _skipped_section(reason)
        data["community_sentiment"] = _skipped_section(reason)
        data["anomaly_scan"] = _skipped_section(reason)
        data["_elapsed_sec"] = round(time.time() - t0, 2)
        data["_sources_overview"] = {
            "quote": "futu only; valuation extras skipped",
            "technical": "futu kline + ta lib",
            "market_env": "shared market_report snapshot",
            "skipped": reason,
        }
        return data

    data["options"] = _section_options(futu_sym, spot=spot_price)
    data["term_structure"] = _section_term_structure(futu_sym)

    # 兜底：如果 options.iv_rank 因 0DTE 没拿到 IV，从 term_structure 取第一个有效 IV 重算
    opt = data["options"]
    if isinstance(opt, dict) and not (opt.get("iv_rank") or {}).get("current_iv"):
        terms = (data["term_structure"] or {}).get("term") or []
        if terms:
            atm_iv = terms[0].get("atm_iv")
            if atm_iv:
                opt["iv_rank"] = _safe(
                    lambda: calc_iv_rank(futu_sym, atm_iv),
                    default={},
                    label="iv_rank_fallback",
                ) or {}

    data["capital_flow"] = _section_capital_flow(futu_sym)
    if is_fund_or_index:
        data["catalyst"] = _skipped_section("ETF/index: skip ratings, price targets, earnings, insider paths")
    else:
        data["catalyst"] = _section_catalyst(finnhub_sym)
    market = futu_sym.split(".", 1)[0] if "." in futu_sym else "US"
    name = (data["quote"].get("futu") or {}).get("name")
    data["news"] = _section_news(finnhub_sym)
    data["stock_digest"] = _section_stock_digest(finnhub_sym, market=market)

    # 富途风格资讯 + 情绪聚合（事件归因 + 看多/看空判定）
    data["news_sentiment"] = _section_news_sentiment(finnhub_sym, market=market, name=name)
    data["community_sentiment"] = _section_community_sentiment(finnhub_sym, market=market)
    data["anomaly_scan"] = _section_anomaly_scan(futu_sym, time_range=7, language="en")

    data["_elapsed_sec"] = round(time.time() - t0, 2)
    data["_sources_overview"] = {
        "quote": "futu (primary) + yfinance.info / stockanalysis (forward valuation)",
        "market_env": "futu (primary) + yfinance (fallback)",
        "technical": "futu kline + ta lib",
        "options": "futu option_chain + computed (max_pain / iv_rank / unusual)",
        "term_structure": "futu option chain ATM IV across 5 expiries",
        "capital_flow": "futu/get_capital_flow",
        "catalyst": (
            "rating: finnhub→yahoo→stockanalysis | "
            "price_target: stockanalysis→yahoo | "
            "earnings: yfinance.calendar/earnings_dates→stockanalysis→finnhub (scheduled/expected only unless actuals present) | "
            "insider: yfinance SEC Form 4→finnhub"
        ),
        "news": "yahoo/Ticker.news (primary) + finnhub/company_news (fallback)",
        "stock_digest": "futu-stock-digest (Skills)",
        "news_sentiment": "futu-skills + yahoo + google-news",
        "community_sentiment": "futu-skills + stocktwits + reddit + finnhub-social (US) / akshare (CN)",
        "anomaly_scan": "futu unusual API trio (financial / derivative / technical)",
    }
    return data


# ============================================================
# 文本格式化（终端友好）
# ============================================================
def _hr(title: str = "", w: int = 64) -> str:
    if not title:
        return "─" * w
    pad = w - len(title) - 4
    return f"── {title} " + "─" * max(pad, 4)


def _fmt_money(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, (int, float)) and abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if isinstance(v, (int, float)) and abs(v) >= 1e6:
        return f"${v / 1e6:.2f}M"
    if isinstance(v, (int, float)):
        return f"${v:,.2f}"
    return str(v)


def _fmt_pct(v) -> str:
    if v is None:
        return "-"
    return f"{v:+.2f}%"


def format_brief(data: dict[str, Any]) -> str:
    sym = data["symbol"]
    lines: list[str] = []

    lines.append("")
    lines.append("=" * 64)
    lines.append(f"  📊 {sym} 每日简报   {data['generated_at']}   (耗时 {data['_elapsed_sec']}s)")
    lines.append("=" * 64)

    # ───────── 行情 ─────────
    q_futu = data["quote"].get("futu") or {}
    lines.append(_hr("📈 行情（Futu 实时 + 盘前/盘后/隔夜 + 估值）"))
    if q_futu:
        # 时效性硬告警（防止 LLM 把周末/隔夜过期数据当作"正在发生的实时走势"）
        fresh = q_futu.get("quote_freshness") or {}
        if fresh:
            phase = fresh.get("market_phase", "?")
            level = fresh.get("freshness_level", "?")
            age = fresh.get("data_age_minutes")
            now_et = fresh.get("now_et", "")
            lines.append(
                f"  ⏱️ 时效   phase={phase}  level={level}  age={age}min  "
                f"now_et={now_et}  snapshot={fresh.get('snapshot_time')}"
            )
            if fresh.get("warn"):
                lines.append(f"  ⚠️  {fresh['warn']}")
            if fresh.get("next_open_hint"):
                lines.append(f"  ⏭  下次开盘 {fresh['next_open_hint']}（当前为休市，禁止用过期 session 推断实时走势）")

        vol = q_futu.get("volume")
        vol_str = f"{vol/1e6:.2f}M" if vol and vol > 1e6 else f"{vol:,}" if vol else "-"
        lines.append(
            f"  Futu     现价 {_fmt_money(q_futu.get('price'))}  涨跌 {_fmt_pct(q_futu.get('change_rate'))}  "
            f"上日收 {_fmt_money(q_futu.get('prev_close'))}  量 {vol_str}  时间 {q_futu.get('update_time')}"
        )
        lines.append(
            f"           开 {_fmt_money(q_futu.get('open'))}  高 {_fmt_money(q_futu.get('high'))}  "
            f"低 {_fmt_money(q_futu.get('low'))}  量比 {q_futu.get('volume_ratio')}  换手率 {q_futu.get('turnover_rate')}%"
        )

        # 4 段行情
        pre = q_futu.get("pre_market") or {}
        after = q_futu.get("after_market") or {}
        overnight = q_futu.get("overnight") or {}
        if pre.get("price"):
            lines.append(
                f"  🌅 盘前  现价 {_fmt_money(pre.get('price'))}  涨跌 {_fmt_pct(pre.get('change_rate'))}  "
                f"高/低 {pre.get('high')}/{pre.get('low')}  量 {pre.get('volume')}"
            )
        if after.get("price"):
            lines.append(
                f"  🌃 盘后  现价 {_fmt_money(after.get('price'))}  涨跌 {_fmt_pct(after.get('change_rate'))}  "
                f"高/低 {after.get('high')}/{after.get('low')}  量 {after.get('volume')}"
            )
        if overnight.get("price"):
            lines.append(
                f"  🌙 隔夜  现价 {_fmt_money(overnight.get('price'))}  涨跌 {_fmt_pct(overnight.get('change_rate'))}  "
                f"高/低 {overnight.get('high')}/{overnight.get('low')}  量 {overnight.get('volume')}"
            )

        # 估值 + 52周
        val = q_futu.get("valuation") or {}
        r52 = q_futu.get("range_52w") or {}
        if val.get("pe_ttm") is not None or val.get("market_cap"):
            mc = val.get("market_cap")
            mc_str = f"${mc/1e12:.2f}T" if mc and mc > 1e12 else f"${mc/1e9:.2f}B" if mc else "-"
            lines.append(
                f"  💰 估值  PE_TTM {val.get('pe_ttm')}  PE_静态 {val.get('pe_static')}  "
                f"PE_Forward {val.get('pe_forward')}  PB {val.get('pb')}  "
                f"EPS_TTM {val.get('eps')}  EPS_Fwd {val.get('eps_forward')}  "
                f"PEG {val.get('peg_ratio')}  市值 {mc_str}  股息率 {val.get('dividend_yield_ttm')}%"
            )
            if val.get("_pe_warn"):
                lines.append(f"     ⚠️  {val['_pe_warn']}")
        if r52.get("high"):
            lines.append(
                f"  📅 52周  高/低 {_fmt_money(r52.get('high'))} / {_fmt_money(r52.get('low'))}  "
                f"历史高 {_fmt_money(r52.get('high_history'))}"
            )

    # ───────── 技术 ─────────
    tech = data["technical"] or {}
    lines.append("")
    lines.append(_hr("🔧 技术面（日线，6 个月数据）"))
    if "_error" in tech:
        lines.append(f"  ⚠️  {tech['_error']}")
    else:
        lines.append(
            f"  收盘 {_fmt_money(tech.get('close'))}  "
            f"5d {_fmt_pct(tech.get('ret_5d'))}  20d {_fmt_pct(tech.get('ret_20d'))}  60d {_fmt_pct(tech.get('ret_60d'))}"
        )
        lines.append(
            f"  MA5/20/50/200: {tech.get('ma5'):.2f} / {tech.get('ma20'):.2f} / "
            f"{tech.get('ma50'):.2f} / {tech.get('ma200') if tech.get('ma200') else '-'}"
            if tech.get("ma5") else "  MA: 数据不足"
        )
        rsi = tech.get("rsi14")
        rsi_tag = "超买🔥" if rsi and rsi > 70 else "超卖❄️" if rsi and rsi < 30 else "中性"
        lines.append(f"  RSI14 {rsi}  ({rsi_tag})  | MACD柱 {tech.get('macd_hist')}  ({tech.get('macd_cross')})  | 布林位 {tech.get('bb_position')}")
        lines.append(f"  支撑 {_fmt_money(tech.get('support'))}   阻力 {_fmt_money(tech.get('resistance'))}   HV20 {tech.get('hv20_pct')}%")

    # ───────── 期权 ─────────
    opt = data["options"] or {}
    lines.append("")
    lines.append(_hr("🎯 期权（Futu 期权链，最近到期日）"))
    if "_error" in opt:
        lines.append(f"  ⚠️  {opt['_error']}")
    else:
        s = opt.get("summary", {})
        lines.append(
            f"  合约数 {opt.get('n_contracts')}  | Call量 {s.get('call_volume'):,}  Put量 {s.get('put_volume'):,}  "
            f"PCR {s.get('put_call_ratio')}"
        )
        lines.append(f"  最大OI行权价 ≈ {_fmt_money(s.get('max_oi_strike'))}   IV中位 {s.get('iv_median')}%")

        # Max Pain
        mp = opt.get("max_pain") or {}
        if mp.get("max_pain"):
            dev = mp.get("spot_to_max_pain_pct")
            dev_str = f"  现价偏离 {dev:+.2f}%" if dev is not None else ""
            lines.append(
                f"  🎯 Max Pain {_fmt_money(mp.get('max_pain'))}{dev_str}  "
                f"PCR_OI {mp.get('pcr_oi')}  全链 OI Call/Put {mp.get('total_call_oi'):,}/{mp.get('total_put_oi'):,}"
            )

        # IV Rank
        ivr = opt.get("iv_rank") or {}
        if ivr.get("current_iv") is not None:
            if ivr.get("iv_rank") is not None:
                lines.append(
                    f"  📊 ATM IV {ivr.get('current_iv')}%  IV Rank {ivr.get('iv_rank')}  "
                    f"Percentile {ivr.get('iv_percentile')}%  1y [{ivr.get('iv_1y_low')}%, {ivr.get('iv_1y_high')}%]  "
                    f"→ {ivr.get('regime')}"
                )
            else:
                lines.append(
                    f"  📊 ATM IV {ivr.get('current_iv')}%  ⏳ {ivr.get('_note', '')}（已落库 {ivr.get('history_days')} 天）"
                )

        if opt.get("unusual"):
            lines.append(f"  ⚡ 异动合约 (Vol/OI ≥ 2.0)  Top {len(opt['unusual'])}:")
            for u in opt["unusual"]:
                lines.append(
                    f"     {u['type']:4s} K={u['strike']:>8.2f}  Last={u['last']:>7.2f}  "
                    f"Vol/OI={u['vol_oi']:>5.2f}  Vol={u['volume']:>7,}  OI={u['oi']:>6,}  IV={u['iv']:.2f}"
                )

    # ───────── 期限结构 Term Structure ─────────
    ts = data.get("term_structure") or {}
    lines.append("")
    lines.append(_hr("📈 IV 期限结构（前 5 个到期日 ATM IV）"))
    if "_error" in ts:
        lines.append(f"  ⚠️  {ts['_error']}")
    else:
        terms = ts.get("term") or []
        if terms:
            for t in terms:
                lines.append(
                    f"  {t.get('expiry')}  DTE={t.get('dte'):>3}  ATM IV {t.get('atm_iv'):>8.2f}%"
                )
            shape = ts.get("shape") or "-"
            diff = ts.get("front_back_diff")
            tip = {
                "backwardation": "近月>远月，市场定价短期事件，⚠️ 慎做买方（事件后 IV crush）",
                "contango": "远月>近月，正常市场结构，✅ 适合常规策略",
                "flat": "前后 IV 几乎一致",
                "mixed": "结构混合，需个案判断",
            }.get(shape, "")
            lines.append(f"  ▶ 形态: {shape}  | 前后差 {diff:+.4f}  | {tip}")
        else:
            lines.append("  无可用到期日数据")

    # ───────── 市场环境（VIX + 板块 ETF） ─────────
    me = data.get("market_env") or {}
    lines.append("")
    lines.append(_hr("🌐 市场环境（VIX + 板块 ETF）"))
    idx = me.get("indices") or {}
    if idx:
        # VIX 单独一行
        vix = idx.get("VIX") or {}
        if vix.get("price"):
            lines.append(
                f"  VIX  {vix.get('price'):>6.2f}  涨跌 {_fmt_pct(vix.get('change_rate'))}  → {me.get('vix_regime')}"
            )
        # 大盘 + 板块 ETF
        for tk in ["SPY", "QQQ", "DIA", "IWM", "SMH", "SOXX", "XLK", "XLF"]:
            v = idx.get(tk)
            if v and v.get("price"):
                lines.append(
                    f"  {tk:5s}{v.get('price'):>8.2f}  涨跌 {_fmt_pct(v.get('change_rate'))}  日内[{v.get('low')}, {v.get('high')}]"
                )
    else:
        lines.append("  无数据")

    # ───────── 资金流 ─────────
    cf = data["capital_flow"] or {}
    lines.append("")
    lines.append(_hr("💰 资金流（Futu，单位:USD）"))
    if "_error" in cf:
        lines.append(f"  ⚠️  {cf['_error']}")
    elif not cf:
        lines.append("  无数据")
    else:
        lines.append(f"  时间 {cf.get('capital_flow_item_time')}")
        lines.append(
            f"  净流入: 总 {_fmt_money(cf.get('in_flow'))}  主力 {_fmt_money(cf.get('main_in_flow'))}  "
            f"超大 {_fmt_money(cf.get('super_in_flow'))}"
        )
        lines.append(
            f"        大单 {_fmt_money(cf.get('big_in_flow'))}  中单 {_fmt_money(cf.get('mid_in_flow'))}  "
            f"小单 {_fmt_money(cf.get('sml_in_flow'))}"
        )

    # ───────── 催化剂 ─────────
    cat = data["catalyst"] or {}
    lines.append("")
    lines.append(_hr("🔥 催化剂（评级 / 目标价 / 财报 / 内部人）"))

    rating = cat.get("rating") or {}
    if rating and "_error" not in rating:
        lines.append(
            f"  评级共识 [{rating.get('source')}]  StrongBuy={rating.get('strong_buy')} "
            f"Buy={rating.get('buy')} Hold={rating.get('hold')} Sell={rating.get('sell')} "
            f"StrongSell={rating.get('strong_sell')}  | 多空分 {rating.get('bull_bear_score')}"
        )

    pt = cat.get("price_target") or {}
    if pt and "_error" not in pt:
        cur = q_futu.get("price") or pt.get("current_price")
        upside = None
        if cur and pt.get("target_mean"):
            upside = round((pt["target_mean"] - cur) / cur * 100, 2)
        lines.append(
            f"  目标价 [{pt.get('source')}]  均值 {_fmt_money(pt.get('target_mean'))}  "
            f"区间 [{_fmt_money(pt.get('target_low'))}, {_fmt_money(pt.get('target_high'))}]  "
            f"上行空间 {_fmt_pct(upside or pt.get('upside_pct'))}"
        )

    eh = cat.get("earnings_next") or {}
    if eh:
        status = eh.get("status") or "tbd"
        tone_word = eh.get("tone_word") or "unknown"
        days_until = eh.get("days_until")
        day_note = f" 距今 {days_until} 天" if days_until is not None else ""
        lines.append(
            f"  下次财报[{status}/{tone_word}]  {eh.get('date')} ({eh.get('hour','-')}){day_note}  "
            f"EPS预期 {eh.get('eps_estimate')}  营收预期 {_fmt_money(eh.get('revenue_estimate'))}"
        )
        if eh.get("_warn"):
            lines.append(f"     ⚠️ {eh.get('_warn')}")

    eh_hist = cat.get("earnings_history") or []
    if eh_hist:
        beats = [r for r in eh_hist if r.get("surprise_pct") and r["surprise_pct"] > 0]
        lines.append(f"  近 4 季 EPS Beat 率: {len(beats)}/{len(eh_hist)}")
        for r in eh_hist[:2]:
            lines.append(f"     {r['period']}  实际 {r['actual']}  预期 {r['estimate']}  Surprise {_fmt_pct(r['surprise_pct'])}")

    insider = cat.get("insider_90d") or {}
    if insider and "_error" not in insider:
        net = insider.get("net_value_usd", 0)
        emoji = "🔴" if net < 0 else "🟢"
        lines.append(
            f"  {emoji} 内部人 90d  买 {insider.get('buy_count')}笔/{_fmt_money(insider.get('buy_value_usd'))}  "
            f"卖 {insider.get('sell_count')}笔/{_fmt_money(insider.get('sell_value_usd'))}  "
            f"净 {_fmt_money(net)}"
        )

    # ───────── 新闻 ─────────
    news = data.get("news") or []
    lines.append("")
    lines.append(_hr("📰 公司新闻（近 2 天 Top 8）"))
    if not news:
        lines.append("  无新闻")
    else:
        for n in news:
            ts = pd.to_datetime(n["datetime"], unit="s").strftime("%m-%d %H:%M") if n.get("datetime") else "-"
            lines.append(f"  [{ts}] {n['headline']}")

    # ───────── 个股解读（futu-stock-digest）─────────
    digest = data.get("stock_digest") or {}
    lines.append("")
    lines.append(_hr("🧠 个股解读（futu-stock-digest 官方聚合）"))
    if not digest or digest.get("_error"):
        lines.append(f"  {digest.get('_error', '不可用')}")
    elif not digest.get("items"):
        lines.append("  无个股解读数据")
    else:
        lines.append(
            f"  来源 {digest.get('source')}  语言 {digest.get('lang')}  样本 {digest.get('n', 0)} 条"
        )
        for it in (digest.get("items") or [])[:8]:
            ts = "-"
            if it.get("published"):
                try:
                    ts = pd.to_datetime(int(it["published"]), unit="s").strftime("%m-%d %H:%M")
                except Exception:
                    ts = str(it.get("published"))
            title = (it.get("title") or "")[:96]
            lines.append(f"  [{ts}] {title}")

    # ───────── 富途资讯 + 情绪打分 ─────────
    ns = data.get("news_sentiment") or {}
    lines.append("")
    lines.append(_hr("🗞️  富途资讯情绪（Skills 官方源 + 事件归因）"))
    if not ns or "_error" in ns:
        lines.append(f"  {ns.get('_error', '不可用')}")
    else:
        view_emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(ns.get("integrated_view"), "⚪")
        stats = ns.get("_stats") or {}
        lines.append(
            f"  {view_emoji} 整体情绪: {ns.get('integrated_view','-')}  "
            f"score {ns.get('score',0):+.3f}  "
            f"样本 {stats.get('total',0)} 条 "
            f"(futu={stats.get('futu_skills',0)} / yahoo={stats.get('yahoo',0)} / google={stats.get('google',0)})"
        )
        cats = ns.get("categories") or {}
        if cats:
            cat_str = "  ".join(f"{k}×{v}" for k, v in cats.items())
            lines.append(f"  事件分布: {cat_str}")
        for sig in (ns.get("key_signals") or [])[:5]:
            lines.append(f"   ⚡ {sig}")
        for it in (ns.get("sources") or [])[:6]:
            tag = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(it.get("sentiment"), "⚪")
            cat = it.get("category") or "-"
            src = it.get("source") or "-"
            title = (it.get("title") or "")[:80]
            lines.append(f"   {tag} [{cat}|{src}] {title}")

    # ───────── 社区情绪 ─────────
    cs = data.get("community_sentiment") or {}
    lines.append("")
    lines.append(_hr("💬 社区情绪（富途/StockTwits）"))
    if not cs or "_error" in cs:
        lines.append(f"  {cs.get('_error', '不可用')}")
    elif cs.get("n_posts", 0) == 0:
        lines.append("  无社区帖子")
    else:
        view_emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(cs.get("integrated_view"), "⚪")
        lines.append(
            f"  {view_emoji} 整体: {cs.get('integrated_view','-')}  "
            f"看多 {cs.get('bull_pct',0)}% / 看空 {cs.get('bear_pct',0)}% / 中性 {cs.get('neutral_pct',0)}%  "
            f"(n={cs.get('n_posts',0)}, 源={'+'.join(cs.get('sources') or []) or '-'})"
        )
        themes = cs.get("top_themes") or []
        if themes:
            lines.append(f"  热门主题: {' / '.join(themes)}")

    # ───────── Layer 3 异动扫描（资金/衍生品/技术） ─────────
    aly = data.get("anomaly_scan") or {}
    lines.append("")
    lines.append(_hr("🚨 富途异动扫描（近 7 天 · capital/derivatives/technical）"))
    if not aly or "_error" in aly:
        lines.append(f"  {aly.get('_error', '不可用')}")
    else:
        for cat_label, key, emoji in [
            ("资金面", "capital", "💰"),
            ("衍生品", "derivatives", "📈"),
            ("技术面", "technical", "📊"),
        ]:
            block = aly.get(key) or {}
            if not block.get("ok"):
                err = (block.get("error") or "")[:80]
                lines.append(f"  {emoji} {cat_label}: ⚠️ 不可用 ({err})" if err else f"  {emoji} {cat_label}: ⚠️ 不可用")
                continue
            payload = block.get("data") or {}
            content = (payload.get("content") if isinstance(payload, dict) else None) or "无异常"
            tr = payload.get("time_range") if isinstance(payload, dict) else None
            header = f"  {emoji} {cat_label}" + (f"（{tr}）" if tr else "")
            lines.append(header)
            for ln in str(content).splitlines():
                ln = ln.strip()
                if ln:
                    lines.append(f"     · {ln}")

    lines.append("")
    lines.append("=" * 64)
    lines.append("  ⚠️  本简报为算法自动生成，不构成投资建议。")
    lines.append("=" * 64)
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    try:
        d = build_brief(sym)
        print(format_brief(d))
    except Exception:
        traceback.print_exc()
