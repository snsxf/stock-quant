"""stock-quant 命令行入口。

支持的子命令：
    stock-quant brief <SYMBOL>      # 生成每日简报
    stock-quant quote <SYMBOL>      # 仅拉实时报价
    stock-quant signals <SYMBOL>    # 仅拉技术信号
    stock-quant decide <SYMBOL>     # 期权决策建议（含具体 Strike/到期日）
    stock-quant screen <SYMBOL>     # 跨到期/行权价的期权筛选器
    stock-quant sentiment <SYMBOL>  # 资讯 + 社区情绪聚合（看多/看空）
    stock-quant market              # 大盘模式：全景情绪与核心池扫描推荐

也支持 JSON 输出：
    stock-quant brief NVDA --json
    stock-quant decide NVDA --json
"""
from __future__ import annotations

import argparse
import json
import sys


def _cmd_brief(args: argparse.Namespace) -> int:
    from .reports.daily_brief import build_brief, format_brief

    data = build_brief(args.symbol)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_brief(data))
    return 0


def _cmd_quote(args: argparse.Namespace) -> int:
    from .datasource import FutuSource
    from .datasource.router import to_futu_symbol

    sym = args.symbol if "." in args.symbol else to_futu_symbol(args.symbol)
    fs = FutuSource()
    q = fs.get_quote(sym)
    print(json.dumps(q, indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_signals(args: argparse.Namespace) -> int:
    import pandas as pd
    from .analysis.technical import latest_signals
    from .datasource import FutuSource
    from .datasource.router import to_futu_symbol

    sym = args.symbol if "." in args.symbol else to_futu_symbol(args.symbol)
    fs = FutuSource()
    df = fs.get_history(sym, period="6mo", interval="1d")
    rename = {"open": "Open", "close": "Close", "high": "High", "low": "Low", "volume": "Volume"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    print(json.dumps(latest_signals(df), indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_decide(args: argparse.Namespace) -> int:
    from .reports.option_decision import decide, format_decision

    data = decide(args.symbol)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_decision(data))
    return 0


def _parse_range(s: str, cast=float) -> tuple:
    """支持 '21-50' 或 '0.15,0.35' 格式。"""
    sep = "-" if "-" in s else ","
    parts = s.split(sep)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"无效区间: {s}，请用 'a-b' 或 'a,b'")
    return cast(parts[0]), cast(parts[1])


def _cmd_screen(args: argparse.Namespace) -> int:
    from .reports.option_screener import screen_options, format_screen

    data = screen_options(
        args.symbol,
        direction=args.direction,
        dte_range=_parse_range(args.dte, int),
        delta_range=_parse_range(args.delta, float),
        min_otm_pct=args.min_otm,
        min_yield_pct=args.min_yield,
        min_oi=args.min_oi,
        min_volume=args.min_volume,
        top_n=args.top,
    )
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_screen(data))
    return 0


def _cmd_sentiment(args: argparse.Namespace) -> int:
    from .sentiment.futu_news import get_stock_news_with_sentiment
    from .sentiment.futu_community import get_community_sentiment

    sym = args.symbol
    market = "HK" if sym.endswith(".HK") or sym.startswith("HK.") else (
        "CN" if sym.endswith((".SS", ".SZ")) or sym.startswith(("SH.", "SZ.")) else "US"
    )
    code = sym.split(".", 1)[-1] if "." in sym else sym

    news = get_stock_news_with_sentiment(code, market=market, limit=12)
    community = get_community_sentiment(code, market=market)

    out = {"symbol": sym, "market": market, "news": news, "community": community}
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    # 终端友好输出
    print("=" * 70)
    print(f"  📰 {sym} 资讯 + 社区情绪聚合")
    print("=" * 70)
    print(f"\n📊 资讯情绪: {news.get('integrated_view','-')}  (score={news.get('score','-')})")
    cats = news.get("categories") or {}
    if cats:
        print("   事件分布:", "  ".join(f"{k}×{v}" for k, v in cats.items()))
    for sig in (news.get("key_signals") or [])[:5]:
        print(f"   • {sig}")

    print(f"\n💬 社区情绪: {community.get('integrated_view','-')}  "
          f"(看多 {community.get('bull_pct',0)}% / 看空 {community.get('bear_pct',0)}% / "
          f"中性 {community.get('neutral_pct',0)}%, n={community.get('n_posts',0)})")
    for tw in (community.get("top_themes") or [])[:3]:
        print(f"   • {tw}")
    print("=" * 70)
    return 0


def _cmd_market(args: argparse.Namespace) -> int:
    from .reports.market_report import build_market_report, format_market_report

    watchlist = args.watchlist.split(",") if args.watchlist else None
    data = build_market_report(watchlist)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_market_report(data))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="stock-quant", description="stock-quant 命令行")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_brief = sub.add_parser("brief", help="生成每日简报")
    p_brief.add_argument("symbol", help="标的代码，如 NVDA / 0700.HK / US.NVDA")
    p_brief.add_argument("--json", action="store_true", help="输出 JSON 而非终端友好格式")
    p_brief.set_defaults(func=_cmd_brief)

    p_quote = sub.add_parser("quote", help="实时报价")
    p_quote.add_argument("symbol")
    p_quote.set_defaults(func=_cmd_quote)

    p_sig = sub.add_parser("signals", help="技术信号")
    p_sig.add_argument("symbol")
    p_sig.set_defaults(func=_cmd_signals)

    p_dec = sub.add_parser("decide", help="期权决策建议（含具体 Strike/到期日）")
    p_dec.add_argument("symbol", help="标的代码，如 NVDA / US.NVDA")
    p_dec.add_argument("--json", action="store_true", help="输出 JSON 而非终端友好格式")
    p_dec.set_defaults(func=_cmd_decide)

    p_scr = sub.add_parser("screen", help="跨到期/行权价的期权筛选器")
    p_scr.add_argument("symbol", help="标的代码")
    p_scr.add_argument(
        "--direction", default="sell_put",
        choices=["sell_put", "sell_call", "buy_call", "buy_put"],
        help="筛选方向（默认 sell_put）",
    )
    p_scr.add_argument("--dte", default="21-50", help="DTE 区间，格式 'a-b'，默认 21-50")
    p_scr.add_argument("--delta", default="0.15-0.35", help="|Delta| 区间，默认 0.15-0.35")
    p_scr.add_argument("--min-otm", type=float, default=1.0, help="最小虚值百分比，默认 1.0")
    p_scr.add_argument("--min-yield", type=float, default=None, help="最小年化收益%（仅卖方）")
    p_scr.add_argument("--min-oi", type=int, default=100, help="最小未平仓数，默认 100")
    p_scr.add_argument("--min-volume", type=int, default=0, help="最小成交量")
    p_scr.add_argument("--top", type=int, default=15, help="返回前 N 条，默认 15")
    p_scr.add_argument("--json", action="store_true")
    p_scr.set_defaults(func=_cmd_screen)

    p_sent = sub.add_parser("sentiment", help="资讯 + 社区情绪聚合")
    p_sent.add_argument("symbol", help="标的代码")
    p_sent.add_argument("--json", action="store_true")
    p_sent.set_defaults(func=_cmd_sentiment)

    p_market = sub.add_parser("market", help="大盘全景报告与期权推荐")
    p_market.add_argument("--watchlist", help="自定义股票池，逗号分隔 (如 NVDA,TSLA,AAPL)")
    p_market.add_argument("--json", action="store_true")
    p_market.set_defaults(func=_cmd_market)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
