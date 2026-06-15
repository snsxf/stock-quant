"""stock-quant MCP Server.

把富途 OpenD + 技术指标 + 期权 Greeks 暴露给 Trae / Claude Desktop / Cursor。

启动方式：
    uv run python -m stock_quant.mcp_server
或在 mcp.json 中配置（见 README）。
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
import functools
import threading
from typing import Any
import numpy as np

from mcp.server.fastmcp import FastMCP

from .analysis import greeks as _greeks
from .analysis import latest_signals as _latest_signals
from .analysis import max_pain as _max_pain
from .datasource import FutuSource, YahooSource, get_source
from .flow import calc_max_pain


# ============================================================
# Logging：所有日志走 stderr，避免污染 MCP stdio (stdout 是 JSON-RPC 通道)
# ============================================================
def _setup_logging() -> logging.Logger:
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr for h in root.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    return logging.getLogger("stock_quant.mcp_server")


logger = _setup_logging()


# 兜底：把任何子线程未捕获的异常打到 stderr 而不是让进程崩溃
def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    logger.error(
        "Uncaught exception in thread %s: %s",
        args.thread.name if args.thread else "<unknown>",
        args.exc_value,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


threading.excepthook = _thread_excepthook


mcp = FastMCP("stock-quant")


_FUTU_LOCK = threading.RLock()


def _convert_numpy(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return _convert_numpy(obj.tolist())
    elif isinstance(obj, dict):
        return {k: _convert_numpy(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_numpy(v) for v in obj]
    return obj


def _is_transport_closed_error(exc: BaseException) -> bool:
    """Detect expected MCP stdio disconnect errors without hard-importing anyio."""
    name = type(exc).__name__
    if name in {"BrokenResourceError", "ClosedResourceError", "EndOfStream"}:
        return True
    msg = str(exc)
    if "BrokenResourceError" in msg or "Connection closed" in msg:
        return True
    nested = getattr(exc, "exceptions", None)
    if nested:
        return any(_is_transport_closed_error(e) for e in nested)
    return False


def safe_tool(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            with _FUTU_LOCK:
                res = func(*args, **kwargs)
            return _convert_numpy(res)
        except Exception as e:
            err_msg = f"Error in {func.__name__}: {type(e).__name__}: {str(e)}"
            # 必须打到 stderr，方便排查为何工具失败；同时永远不让异常 propagate
            logger.error("[tool=%s] failed: %s", func.__name__, e, exc_info=True)
            return {
                "_error": err_msg,
                "traceback": traceback.format_exc(limit=5)
            }
    return wrapper


def _df_to_records(df, limit: int = 50) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return json.loads(df.head(limit).to_json(orient="records"))


def _normalize_hist(df):
    if df is None or df.empty:
        return df
    rename_map = {
        c: c.capitalize()
        for c in ["open", "high", "low", "close", "volume"]
        if c in df.columns
    }
    return df.rename(columns=rename_map) if rename_map else df


# ---------------- Tools ---------------- #

@mcp.tool()
@safe_tool
def get_quote(symbol: str) -> dict:
    """
    获取实时（或延迟15min）行情快照。
    symbol 支持：
        - 富途风格: 'US.NVDA' / 'HK.00700' / 'SH.600519' / 'SZ.000001'
        - yfinance 风格: 'NVDA' / '0700.HK' / '600519.SS'
    港股/A股自动走富途，美股优先富途、回退 yfinance。
    """
    src, real = get_source(symbol)
    src_name = src.__class__.__name__
    return {
        "_source": f"{src_name} (futu/get_market_snapshot or yfinance.fast_info)",
        "source": src_name,
        "symbol": real,
        **src.get_quote(real),
    }


@mcp.tool()
@safe_tool
def get_history(symbol: str, period: str = "3mo", interval: str = "1d") -> dict:
    """
    获取历史 K 线。
    period: '1mo' / '3mo' / '6mo' / '1y' / '2y'
    interval: '1d' / '1h' / '30m' / '15m' / '5m' / '1m'
    """
    src, real = get_source(symbol)
    df = _normalize_hist(src.get_history(real, period=period, interval=interval))
    src_name = src.__class__.__name__
    return {
        "_source": f"{src_name} (futu/request_history_kline or yfinance.history)",
        "source": src_name,
        "symbol": real,
        "rows": len(df),
        "tail": _df_to_records(df.tail(20)),
    }


@mcp.tool()
@safe_tool
def get_signals(symbol: str, period: str = "6mo") -> dict:
    """
    返回最新一根K线的技术指标摘要：
    收盘价 / MA20 / MA50 / 金叉死叉 / RSI14 / RSI 状态 / MACD 柱 / 布林带位置。
    """
    src, real = get_source(symbol)
    df = _normalize_hist(src.get_history(real, period=period))
    src_name = src.__class__.__name__
    return {
        "_source": f"{src_name} kline + computed (stock_quant.analysis.technical / ta lib)",
        "source": src_name,
        "symbol": real,
        **_latest_signals(df),
    }


@mcp.tool()
@safe_tool
def get_option_chain(symbol: str, expiry: str | None = None, max_contracts: int | None = 20) -> dict:
    """
    获取期权链（含 IV / Greeks，富途数据源）。
    expiry: 'YYYY-MM-DD'，留空取最近到期日。
    """
    src, real = get_source(symbol)
    if isinstance(src, YahooSource):
        chain = src.get_option_chain(real, expiry=expiry)
        mp_chain = chain
    else:
        chain = src.get_option_chain(real, expiry=expiry, max_contracts=max_contracts)
        mp_chain = chain if max_contracts is None else src.get_option_chain(real, expiry=expiry, max_contracts=None)
    mp = None
    if not mp_chain.empty:
        mp = _max_pain(mp_chain) if isinstance(src, YahooSource) else calc_max_pain(mp_chain)
    src_name = src.__class__.__name__
    return {
        "_source": {
            "chain": f"{src_name} (futu/get_option_chain + per-contract get_market_snapshot, or yfinance option_chain)",
            "max_pain": "computed: stock_quant.flow.calc_max_pain (full chain, untruncated)",
        },
        "source": src_name,
        "symbol": real,
        "expiry": expiry,
        "max_pain": mp,
        "contracts": _df_to_records(chain, limit=max_contracts or len(chain)),
    }


@mcp.tool()
@safe_tool
def calc_greeks(
    option_type: str, S: float, K: float, T_days: float,
    r: float = 0.05, sigma: float = 0.4,
) -> dict:
    """
    Black-Scholes 期权定价 + Greeks。
    option_type: 'call' / 'put'
    S 现价, K 行权价, T_days 距到期天数, r 无风险利率, sigma 隐含波动率（小数）。
    """
    g = _greeks(option_type, S=S, K=K, T=T_days / 365, r=r, sigma=sigma)
    return {
        "_source": "computed: Black-Scholes (stock_quant.analysis.greeks)",
        "price": float(g.price), "delta": float(g.delta), "gamma": float(g.gamma),
        "theta": float(g.theta), "vega": float(g.vega), "rho": float(g.rho),
        "iv": float(g.iv) if g.iv is not None else None,
    }


@mcp.tool()
@safe_tool
def futu_capital_flow(symbol: str) -> dict:
    """获取标的资金流（富途专属，仅港美股部分支持）。"""
    from futu import RET_OK
    from .datasource.router import to_futu_symbol
    from .datasource.futu import CAPITAL_FLOW_BUDGET

    futu_sym = to_futu_symbol(symbol)
    src = FutuSource()
    CAPITAL_FLOW_BUDGET.acquire()
    with src._ctx() as ctx:
        ret, df = ctx.get_capital_flow(futu_sym)
    if ret != RET_OK:
        return {"_source": "futu/get_capital_flow", "error": str(df), "symbol": futu_sym}
    return {
        "_source": "futu/get_capital_flow",
        "symbol": futu_sym,
        "rows": len(df),
        "tail": _df_to_records(df.tail(10)),
    }


@mcp.tool()
@safe_tool
def daily_brief(symbol: str) -> dict:
    """
    生成 6 段集成式每日简报（推荐盘前/盘中/盘后任意时间使用）。

    包含：
      1. 基本面 + 估值（PE_TTM/PB/市值/股息/52w 区间）
      2. 实时报价 + 盘前/盘中/盘后/夜盘 4 段行情
      3. 技术信号（MA/RSI/MACD/布林带 + 多空判断）
      4. 期权链摘要（活跃合约/Max Pain/Put-Call Ratio/IV Rank）
      5. 催化剂（最近评级 + 目标价 + 内部人交易 + 财报临近度）
      6. 市场环境（VIX 状态 + 板块 ETF 强弱）
      + IV Term Structure（5 个最近到期日的隐波曲线）

    参数：
        symbol: 'NVDA' / 'US.NVDA' / '0700.HK' / 'HK.00700' 均可
    """
    import traceback

    from .reports.daily_brief import build_brief

    try:
        return build_brief(symbol)
    except Exception as e:
        return {
            "_error": f"daily_brief failed: {type(e).__name__}: {e}",
            "symbol": symbol,
            "traceback": traceback.format_exc(limit=5),
        }


@mcp.tool()
@safe_tool
def option_decision(symbol: str) -> dict:
    """
    给定标的输出今日推荐期权策略（含具体 Strike + 到期日 + Greeks + 退出计划）。

    决策流程：
      1. 方向得分 ∈ [-100, +100]：技术 + 资金流 + 催化剂 + 板块综合打分
      2. IV 环境：高 IV 走卖方（Iron Condor / Cash-Secured Put）；
         低 IV 走买方（Bull Call / Bear Put Spread）
      3. Term Structure 诊断：backwardation 时给买方策略加风险旗
      4. Max Pain 钉价：选 strike 时考虑回归倾向
      5. 自动选 5-35 DTE 范围内最近的到期日

    返回字段：
      - direction: {score, label, breakdown[]}
      - iv_regime: {regime, side, ...}
      - term_structure: {shape, warn_buy_side, ...}
      - max_pain, spot, expiry_pick
      - strategies: [
          {name, rationale, direction, risk_level,
           legs: [{action, type, strike, expiry, qty, premium_mid, delta, gamma, theta, vega}],
           net_debit/credit, max_profit, max_loss, breakeven, exit_plan}
        ]

    参数：
        symbol: 'NVDA' / 'US.NVDA' 等
    """
    from .reports.option_decision import decide

    return decide(symbol)


@mcp.tool()
@safe_tool
def market_env(tickers: list[str] | None = None) -> dict:
    """
    获取市场宏观环境快照：VIX + 主流指数/板块 ETF。

    默认拉：VIX, SPY, QQQ, DIA, IWM, SMH, SOXX, XLK, XLF
    含 VIX 状态判定（extreme_low / low / medium / high / extreme_high）。

    参数：
        tickers: 可选，自定义 ticker 列表（富途风格如 'US.SPY'，留空走默认池）
    """
    from .datasource.market_index import (
        DEFAULT_TICKERS,
        get_market_snapshot,
        interpret_vix,
    )

    if tickers:
        tk_dict = {t.split(".")[-1]: t if "." in t else f"US.{t}" for t in tickers}
    else:
        tk_dict = DEFAULT_TICKERS
    snapshot = get_market_snapshot(tk_dict)
    vix_data = snapshot.get("VIX", {}) or {}
    vix_price = vix_data.get("price")
    return {
        "_source": {
            "snapshot": "per-ticker._source (futu primary, yfinance fallback)",
            "vix_regime": "computed: VIX threshold mapping",
        },
        "snapshot": snapshot,
        "vix_regime": interpret_vix(vix_price) if vix_price else None,
    }


# ============================================================
# P0 新增：市场全景 / 期权筛选 / 分时分析 / 资讯&情绪
# ============================================================

@mcp.tool()
@safe_tool
def market_report(watchlist: list[str] | None = None) -> dict:
    """
    大盘全景 + 核心股票池多空扫描 + TOP 标的自动期权策略推荐。

    输出：
      - indices: VIX/SPY/QQQ/SMH 等大盘指数
      - vix_regime: 市场情绪判定
      - stocks[]: 股票池逐个的 DirectionScore + 盘前盘后行情
      - top_bullish / top_bearish: 多空 TOP 3
      - recommended_strategy: 对最强多/空票自动跑 option_decision

    参数：
        watchlist: 自定义股票池，留空使用默认 12 只科技核心票
                  （NVDA/AAPL/MSFT/AMZN/META/GOOGL/TSLA/AMD/AVGO/TSM/NFLX/PLTR）
    """
    from .reports.market_report import build_market_report
    return build_market_report(watchlist)


@mcp.tool()
@safe_tool
def screen_options(
    symbol: str,
    direction: str = "sell_put",
    dte_min: int = 21,
    dte_max: int = 50,
    delta_min: float = 0.15,
    delta_max: float = 0.35,
    min_otm_pct: float = 1.0,
    min_yield_pct: float | None = 15.0,
    min_oi: int = 100,
    top_n: int = 15,
) -> dict:
    """
    跨到期日 / 跨行权价的期权筛选（参考富途 sell-put 选合约示例）。

    direction 取值：
      - 'sell_put'        卖 Put（CSP/Bull Put Spread 用）
      - 'sell_call'       卖 Call（Covered Call/Bear Call Spread 用）
      - 'buy_call'        买 Call（看多博弈）
      - 'buy_put'         买 Put（对冲/看空）

    返回：按年化收益率排序的合约清单，含 K/DTE/Bid/Ask/|Δ|/IV/年化%/OI。
    """
    from .reports.option_screener import screen_options as _screen
    return _screen(
        symbol,
        direction=direction,
        dte_range=(dte_min, dte_max),
        delta_range=(delta_min, delta_max),
        min_otm_pct=min_otm_pct,
        min_yield_pct=min_yield_pct,
        min_oi=min_oi,
        top_n=top_n,
    )


@mcp.tool()
@safe_tool
def intraday_analysis(symbol: str, interval: str = "15m") -> dict:
    """
    分时级盘中走势分析：识别盘中节奏 / 关键区间 / 量价主导方。

    包含：
      - day_open/high/low/close + 日内涨跌幅
      - volume_ratio（今日 vs 5日均量，识别放量/缩量）
      - trend: EMA9/EMA21/VWAP 综合多空打分（-4 ~ +4）
      - opening_range: 30min 开盘区间 + 突破状态
      - volume_profile: POC + Value Area 70% 区间

    参数：
        symbol:   标的代码
        interval: '15m' / '5m' / '30m'，建议 15m
    """
    from .analysis.technical import intraday_signals
    src, real = get_source(symbol)
    df = src.get_history(real, period="1mo", interval=interval)
    src_name = src.__class__.__name__
    return {
        "_source": {
            "kline": f"{src_name} (futu intraday or yfinance interval)",
            "indicators": "computed: stock_quant.analysis.technical.intraday_signals (EMA9/EMA21/VWAP + opening_range + volume_profile)",
        },
        "symbol": real,
        "interval": interval,
        **intraday_signals(df),
    }


@mcp.tool()
@safe_tool
def sentiment_summary(symbol: str, market: str = "US") -> dict:
    """
    资讯 + 社区情绪一站式聚合（对标富途 News Search + Comment Sentiment）。

    输出：
      - news: {integrated_view, score, key_signals, categories, sources}
      - community: {
            integrated_view, bull/bear/neutral 占比, top_themes, sources,
            breakdown_by_source: {
                futu-skills:    {n_posts, bull, bear, neutral},
                stocktwits:     {n_posts, bull, bear, neutral},
                reddit:         {total_mentions, by_sub, score_sum, top_posts, ...},
                finnhub-social: {reddit:{...}, twitter:{...}},
            }
        }

    数据源（社区，按市场）：
      - 美股：futu-skills + StockTwits + Reddit (PRAW) + Finnhub social
      - 港股：futu-skills（社区流）
      - A 股：东方财富股吧 + 雪球热度 + 千股千评（akshare）
              新闻：财联社电报 + 东财个股 + 新浪 7×24 + 巨潮公告（akshare）

    参数：
        symbol: 标的代码（如 TSLA / 00700 / 600519）
        market: 'US' / 'HK' / 'CN'
    """
    out: dict = {"symbol": symbol, "market": market}

    if (market or "").upper() == "CN":
        from .sentiment.cn_news import get_cn_stock_news_with_sentiment, _normalize_cn_symbol
        from .sentiment.cn_community import get_cn_community_sentiment

        out["_source"] = {
            "news": "akshare: stock_telegraph_cls (财联社) + stock_news_em (东财) + sina-7x24 + cninfo (巨潮公告)",
            "community": "akshare: stock_comment_em (千股千评) + xueqiu-heat + eastmoney-guba",
        }
        code = _normalize_cn_symbol(symbol)

        # 自动查公司中文名（提升 cls / sina 全市场流匹配召回）
        name: str | None = None
        try:
            import akshare as ak
            df = ak.stock_info_a_code_name()
            row = df[df["code"] == code]
            if not row.empty:
                name = str(row.iloc[0]["name"])
        except Exception:
            name = None

        try:
            out["news"] = get_cn_stock_news_with_sentiment(code, name=name, limit=15)
        except Exception as e:
            out["news"] = {"_error": str(e)}
        try:
            out["community"] = get_cn_community_sentiment(code)
        except Exception as e:
            out["community"] = {"_error": str(e)}
        if name:
            out["name"] = name
        return out

    from .sentiment.futu_news import get_stock_news_with_sentiment
    from .sentiment.futu_community import get_community_sentiment

    out["_source"] = {
        "news": "futu-skills + yahoo (US) / google-news; sentiment: rule-based event scoring",
        "community": "US: futu-skills + stocktwits + reddit (PRAW) + finnhub-social; HK: futu-skills + futunn-html",
    }

    try:
        out["news"] = get_stock_news_with_sentiment(symbol, market=market)
    except Exception as e:
        out["news"] = {"_error": str(e)}
    try:
        out["community"] = get_community_sentiment(symbol, market=market)
    except Exception as e:
        out["community"] = {"_error": str(e)}
    return out


# ============================================================
# P1 新增：选股 / 板块 / 期权简称解析 / 盘口
# ============================================================

@mcp.tool()
@safe_tool
def parse_option_code(code: str) -> dict:
    """
    解析期权简称或富途/OCC 代码。

    支持格式：
      - 'JPM 260320 267.50C'      (富途简称)
      - 'NVDA260424P170000'       (OCC 风格：标的+YYMMDD+C/P+8位strike*1000)
      - 'AAPL 2026-03-20 267.5 C' (空格分隔)

    返回：{symbol, expiry, strike, option_type}
    """
    import re
    s = code.strip().upper()
    _SRC = "computed: stock_quant.mcp_server.parse_option_code (regex parser, OCC + Futu shorthand)"

    # 格式 1: 标准 OCC "NVDA260424P00170000"（含前导零，strike 恰好 8 位）
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", s)
    if m:
        sym, yymmdd, cp, strike_raw = m.groups()
        return {
            "_source": _SRC,
            "symbol": sym,
            "expiry": f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
            "strike": int(strike_raw) / 1000.0,
            "option_type": "CALL" if cp == "C" else "PUT",
        }

    # 格式 1b: 富途/紧凑风格 "TSLA250620C300" —— 整数 strike，直接当实际价
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{1,7})$", s)
    if m:
        sym, yymmdd, cp, strike_raw = m.groups()
        return {
            "_source": _SRC,
            "symbol": sym,
            "expiry": f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
            "strike": float(strike_raw),
            "option_type": "CALL" if cp == "C" else "PUT",
        }

    # 格式 2: "JPM 260320 267.50C"
    m = re.match(r"^([A-Z]+)\s+(\d{6})\s+([\d.]+)\s*([CP])$", s)
    if m:
        sym, yymmdd, strike, cp = m.groups()
        return {
            "_source": _SRC,
            "symbol": sym,
            "expiry": f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
            "strike": float(strike),
            "option_type": "CALL" if cp == "C" else "PUT",
        }

    # 格式 2b: "AAPL 240315 C 175" —— C/P 在中间
    m = re.match(r"^([A-Z]+)\s+(\d{6})\s+([CP])\s+([\d.]+)$", s)
    if m:
        sym, yymmdd, cp, strike = m.groups()
        return {
            "_source": _SRC,
            "symbol": sym,
            "expiry": f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
            "strike": float(strike),
            "option_type": "CALL" if cp == "C" else "PUT",
        }

    # 格式 3: "AAPL 2026-03-20 267.5 C"
    m = re.match(r"^([A-Z]+)\s+(\d{4}-\d{2}-\d{2})\s+([\d.]+)\s*([CP])$", s)
    if m:
        sym, exp, strike, cp = m.groups()
        return {
            "_source": _SRC,
            "symbol": sym,
            "expiry": exp,
            "strike": float(strike),
            "option_type": "CALL" if cp == "C" else "PUT",
        }

    # 格式 3b: "AAPL 2026-03-20 C 267.5"
    m = re.match(r"^([A-Z]+)\s+(\d{4}-\d{2}-\d{2})\s+([CP])\s+([\d.]+)$", s)
    if m:
        sym, exp, cp, strike = m.groups()
        return {
            "_source": _SRC,
            "symbol": sym,
            "expiry": exp,
            "strike": float(strike),
            "option_type": "CALL" if cp == "C" else "PUT",
        }

    return {"_source": _SRC, "_error": f"无法解析期权代码: {code}"}


@mcp.tool()
@safe_tool
def sector_analysis(symbol: str, with_peers: bool = True, peer_limit: int = 20) -> dict:
    """
    板块联动分析：获取个股所属行业/概念板块 + 成分股。

    返回：
      - owner_plates: [{plate_code, plate_name, plate_type}]
      - peers[plate_name]: 同板块前 N 只成分股（用于横向对比）

    参数：
        symbol: 富途代码或裸代码（自动补 US./HK.）
        with_peers: 是否拉同板块成分股
        peer_limit: 每个板块最多返回多少只
    """
    from .datasource.futu import FutuSource
    from .datasource.router import to_futu_symbol

    futu_sym = symbol if "." in symbol else to_futu_symbol(symbol)
    src = FutuSource()
    plates = src.get_owner_plate(futu_sym)
    out = {
        "_source": {
            "owner_plates": "futu/get_owner_plate",
            "peers": "futu/get_plate_stock (top-N constituents)",
        },
        "symbol": futu_sym,
        "owner_plates": plates,
    }
    if with_peers and plates:
        peers = {}
        for p in plates[:5]:
            plate_code = p.get("plate_code") or p.get("code")
            plate_name = p.get("plate_name") or p.get("name") or plate_code
            if not plate_code:
                continue
            try:
                peers[plate_name] = src.get_plate_stock(plate_code, limit=peer_limit)
            except Exception:
                peers[plate_name] = []
        out["peers"] = peers
    return out


@mcp.tool()
@safe_tool
def screen_stocks(
    market: str = "US",
    market_cap_min: float | None = None,
    pe_max: float | None = None,
    pe_min: float | None = None,
    price_change_30d_min_pct: float | None = None,
    price_change_30d_max_pct: float | None = None,
    top_n: int = 30,
) -> dict:
    """
    条件选股（对标富途 Stock Screening）：按市值/PE/30日涨跌筛选。

    参数：
        market:                        'US' / 'HK' / 'CN'
        market_cap_min:                最小市值（USD/HKD/CNY，按市场）
        pe_max / pe_min:               PE 区间
        price_change_30d_min_pct:      30日涨幅下限（%）
        price_change_30d_max_pct:      30日涨幅上限（%）
        top_n:                         返回前 N 只

    返回：
        results[]: 排序后股票清单（按市值降序）
    """
    from futu import (
        FinancialFilter, SimpleFilter, StockField,
    )
    from .datasource.futu import FutuSource

    src = FutuSource()
    market_map = {"US": "US", "HK": "HK", "CN": "SH"}
    m = market_map.get(market.upper(), "US")

    filter_list = []
    if market_cap_min is not None:
        f = SimpleFilter()
        f.filter_min = market_cap_min
        f.stock_field = StockField.MARKET_VAL
        f.is_no_filter = False
        filter_list.append(f)
    if pe_max is not None or pe_min is not None:
        f = FinancialFilter()
        f.stock_field = StockField.PE_ANNUAL
        f.is_no_filter = False
        if pe_max is not None:
            f.filter_max = pe_max
        if pe_min is not None:
            f.filter_min = pe_min
        filter_list.append(f)
    if price_change_30d_min_pct is not None or price_change_30d_max_pct is not None:
        f = SimpleFilter()
        f.stock_field = StockField.CHANGE_RATE
        f.is_no_filter = False
        if price_change_30d_min_pct is not None:
            f.filter_min = price_change_30d_min_pct
        if price_change_30d_max_pct is not None:
            f.filter_max = price_change_30d_max_pct
        filter_list.append(f)

    try:
        from .datasource.futu import PLATE_BUDGET
        PLATE_BUDGET.acquire()
        with src._ctx() as ctx:
            ret, data = ctx.get_stock_filter(
                market=m,
                filter_list=filter_list,
                begin=0,
                num=top_n,
            )
            if ret != 0:
                return {"_error": str(data)}
            last_page, all_count, ls = data
            results = [
                {
                    "code": s.stock_code,
                    "name": s.stock_name,
                    "market_cap": getattr(s, "market_val", None),
                    "pe": getattr(s, "pe_annual", None),
                    "change_rate": getattr(s, "change_rate", None),
                }
                for s in ls
            ]
        return {
            "_source": "futu/get_stock_filter (FinancialFilter + SimpleFilter)",
            "market": market,
            "total_matched": all_count,
            "returned": len(results),
            "results": results,
        }
    except Exception as e:
        return {"_error": f"选股失败: {e}"}


@mcp.tool()
@safe_tool
def order_book(symbol: str, depth: int = 10) -> dict:
    """
    获取实时买卖盘 / 盘口深度。

    参数：
        symbol: 标的代码
        depth:  档位数（1-10，视账户权限）
    """
    from .datasource.futu import FutuSource
    from .datasource.router import to_futu_symbol

    futu_sym = symbol if "." in symbol else to_futu_symbol(symbol)
    src = FutuSource()
    return {
        "_source": "futu/get_order_book (real-time L2 depth)",
        "symbol": futu_sym,
        **src.get_order_book(futu_sym, num=depth),
    }


# ============================================================
# Layer 3: 富途异动信号（对齐 anomaly-skills 三件套）
# ============================================================
@mcp.tool()
@safe_tool
def capital_anomaly(
    symbol: str,
    time_range: int = 7,
    analysis_dimensions: list[str] | None = None,
    language: str = "en",
) -> dict:
    """
    资金面异动检测（对齐富途 `futu-capital-anomaly` Skill，调 OpenD `get_financial_unusual`）。

    参数：
        symbol:              US.NVDA / HK.00700 / 裸 ticker（自动补 US.）
        time_range:          自然日窗口，默认 7
        analysis_dimensions: 子维度过滤，可选值（留空=全维度扫）：
            funds_distribution, funds_broker, funds_flow,
            short_sell_number, short_sell_ratio, short_sell_number_and_ratio
        language:            zh-CN / zh-HK / en，默认 en
    """
    from .analysis.anomaly import capital_anomaly as _capital
    out = _capital(symbol, time_range=time_range,
                   analysis_dimensions=analysis_dimensions,
                   language_id=language)
    if isinstance(out, dict):
        out.setdefault("_source", "futu/get_financial_unusual (OpenD anomaly Skill)")
    return out


@mcp.tool()
@safe_tool
def derivatives_anomaly(
    symbol: str,
    time_range: int = 7,
    analysis_dimensions: list[str] | None = None,
    language: str = "en",
) -> dict:
    """
    衍生品异动检测（对齐 `futu-derivatives-anomaly`，调 OpenD `get_derivative_unusual`）。

    analysis_dimensions 可选值：
        warrant_ratio, warrant_price_distribution（仅港股）,
        option_unusual, option_volatility, option_volume_price,
        option_sentiment, option_comprehensive
    """
    from .analysis.anomaly import derivatives_anomaly as _deriv
    out = _deriv(symbol, time_range=time_range,
                 analysis_dimensions=analysis_dimensions,
                 language_id=language)
    if isinstance(out, dict):
        out.setdefault("_source", "futu/get_derivative_unusual (OpenD anomaly Skill)")
    return out


@mcp.tool()
@safe_tool
def technical_anomaly(
    symbol: str,
    time_range: int = 7,
    indicator_filters: list[str] | None = None,
    language: str = "en",
) -> dict:
    """
    技术面异动检测（对齐 `futu-technical-anomaly`，调 OpenD `get_technical_unusual`）。

    indicator_filters 可选值：
        CCI / KDJ / BIAS / AR / BR / VR / PSY / OSC / WMSR /
        MACD / BOLL / MA / RSI6 / RSI12 / RSI24
    """
    from .analysis.anomaly import technical_anomaly as _tech
    out = _tech(symbol, time_range=time_range,
                indicator_filters=indicator_filters,
                language_id=language)
    if isinstance(out, dict):
        out.setdefault("_source", "futu/get_technical_unusual (OpenD anomaly Skill)")
    return out


@mcp.tool()
@safe_tool
def full_anomaly_scan(symbol: str, time_range: int = 7, language: str = "en") -> dict:
    """
    一键全维度异动扫描：并发跑资金/衍生品/技术三件套。

    返回结构：
        {
          stock_symbol, time_range,
          capital:     {ok, data: {time_range, content}, ...},
          derivatives: {ok, data: {time_range, content}, ...},
          technical:   {ok, data: {time_range, content}, ...},
        }
    """
    from .analysis.anomaly import full_anomaly_scan as _scan
    out = _scan(symbol, time_range=time_range, language_id=language)
    if isinstance(out, dict):
        out.setdefault(
            "_source",
            "futu OpenD anomaly Skills 三件套：get_financial_unusual + get_derivative_unusual + get_technical_unusual",
        )
    return out


def main() -> None:
    """以 stdio 方式启动 MCP Server。

    增加多层异常守护：任何 transport 层 / 工具层异常都不应让进程崩溃，
    全部打到 stderr 由进程管理者（mcp.json / supervisor）观察。
    """
    logger.info("stock-quant MCP server starting (pid=%d)...", _os_pid())
    try:
        mcp.run()
    except KeyboardInterrupt:
        logger.info("stock-quant MCP server stopped by KeyboardInterrupt")
    except SystemExit as e:
        logger.info("stock-quant MCP server SystemExit: %s", e)
        raise
    except Exception as e:
        if _is_transport_closed_error(e):
            logger.warning("stock-quant MCP transport closed; exiting cleanly: %s", e)
            return
        # 不让进程在 transport 层异常时静默死亡 —— 至少留下完整 traceback
        logger.exception("stock-quant MCP server crashed in mcp.run()")
        # 退出码 1 让上层 supervisor / IDE 知道是异常退出，可触发自动重启
        sys.exit(1)


def _os_pid() -> int:
    import os
    return os.getpid()


if __name__ == "__main__":
    main()
