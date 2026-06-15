"""跨标的期权筛选器：mimics 富途 skill 的"卖 Put / 卖 Call 筛选"能力。

筛选维度：
  - DTE 范围（剩余天数）
  - Delta 范围（|Δ| 0.15~0.35 是常见卖方甜区）
  - OTM 程度（虚值百分比下限）
  - 最低买价 / 最低 OI / 最低成交
  - 年化收益率下限（基于权利金 / 行权价 × 365 / DTE 估算）

使用：
  from stock_quant.reports.option_screener import screen_options
  rows = screen_options(
      "NVDA",
      direction="sell_put",        # sell_put | sell_call | buy_call | buy_put
      dte_range=(21, 50),
      delta_range=(0.15, 0.35),
      min_otm_pct=1.0,
      min_yield_pct=15.0,
      min_oi=500,
      top_n=10,
  )

或 CLI：
  stock-quant screen NVDA --direction sell_put --dte 21-50 --delta 0.15-0.35 --min-yield 15
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Literal

import pandas as pd

from ..datasource import FutuSource
from ..datasource.router import to_futu_symbol


Direction = Literal["sell_put", "sell_call", "buy_call", "buy_put"]


@dataclass
class ScreenedContract:
    code: str
    expiry: str
    dte: int
    type: str            # CALL / PUT
    strike: float
    bid: float
    ask: float
    last: float
    mid: float
    iv: float
    delta: float
    abs_delta: float
    oi: int
    volume: int
    otm_pct: float       # 虚值百分比（正向）
    annualized_yield_pct: float | None  # 仅 sell_* 有意义


def _annualized_yield(premium: float, strike: float, dte: int) -> float | None:
    """卖方权利金年化估算（不含被指派后机会成本）。"""
    if not strike or not premium or not dte:
        return None
    return round(premium / strike * (365 / dte) * 100, 2)


def _list_expiries(futu_sym: str) -> list[dict]:
    """返回所有可用到期日（按 dte 升序）。"""
    from datetime import date
    from futu import RET_OK
    from ..datasource.futu import OPTION_CHAIN_BUDGET

    fs = FutuSource()
    today = date.today()
    with fs._ctx() as ctx:
        OPTION_CHAIN_BUDGET.acquire()
        ret, df = ctx.get_option_expiration_date(code=futu_sym)
        if ret != RET_OK or df.empty:
            return []
        out = []
        for _, r in df.iterrows():
            exp = str(r["strike_time"])[:10]
            try:
                d = (pd.Timestamp(exp).date() - today).days
            except Exception:
                continue
            out.append({"expiry": exp, "dte": d})
        return sorted(out, key=lambda x: x["dte"])


def _fetch_chain(futu_sym: str, expiry: str) -> pd.DataFrame:
    fs = FutuSource()
    return fs.get_option_chain(futu_sym, expiry=expiry, max_contracts=None)


def _get_spot(futu_sym: str) -> float | None:
    fs = FutuSource()
    q = fs.get_quote(futu_sym)
    return q.get("price")


def screen_options(
    symbol: str,
    *,
    direction: Direction = "sell_put",
    dte_range: tuple[int, int] = (21, 50),
    delta_range: tuple[float, float] = (0.15, 0.35),
    min_otm_pct: float = 1.0,
    min_yield_pct: float | None = None,
    min_oi: int = 100,
    min_volume: int = 0,
    min_bid: float = 0.05,
    top_n: int = 15,
) -> dict[str, Any]:
    """跨到期日 / 跨行权价的期权筛选。

    Returns: dict with `spot`, `direction`, `filters`, `results: list[ScreenedContract]`
    """
    futu_sym = symbol if "." in symbol else to_futu_symbol(symbol)
    spot = _get_spot(futu_sym)
    if not spot:
        return {"_error": "无法获取现价", "symbol": symbol}

    expiries = _list_expiries(futu_sym)
    valid_exp = [e for e in expiries if dte_range[0] <= e["dte"] <= dte_range[1]]
    if not valid_exp:
        return {
            "_error": f"DTE 区间 {dte_range} 无可用到期日",
            "symbol": symbol,
            "spot": spot,
            "available_expiries": expiries[:10],
        }

    # 决定要找 CALL 还是 PUT
    target_type = "PUT" if direction in ("sell_put", "buy_put") else "CALL"
    is_seller = direction.startswith("sell_")

    results: list[ScreenedContract] = []

    for exp_info in valid_exp:
        expiry = exp_info["expiry"]
        dte = exp_info["dte"]
        chain = _fetch_chain(futu_sym, expiry)
        if chain.empty:
            continue

        df = chain[chain["option_type"] == target_type].copy()
        if df.empty:
            continue

        for _, r in df.iterrows():
            strike = float(r.get("option_strike_price") or 0)
            if not strike:
                continue

            bid = float(r.get("bid_price") or 0)
            ask = float(r.get("ask_price") or 0)
            last = float(r.get("last_price") or 0)
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
            if mid <= 0 or bid < min_bid:
                continue

            iv = float(r.get("option_implied_volatility") or 0)
            delta = float(r.get("option_delta") or 0)
            abs_delta = abs(delta)
            oi = int(r.get("option_open_interest") or 0)
            vol = int(r.get("volume") or 0)

            # OTM 计算（虚值百分比，PUT/CALL 方向相反）
            if target_type == "PUT":
                otm_pct = round((spot - strike) / spot * 100, 2)
            else:
                otm_pct = round((strike - spot) / spot * 100, 2)

            # 过滤
            if otm_pct < min_otm_pct:
                continue
            if not (delta_range[0] <= abs_delta <= delta_range[1]):
                continue
            if oi < min_oi or vol < min_volume:
                continue

            ann_yield = _annualized_yield(mid, strike, dte) if is_seller else None
            if min_yield_pct is not None and is_seller and (ann_yield or 0) < min_yield_pct:
                continue

            results.append(ScreenedContract(
                code=r.get("code"),
                expiry=expiry,
                dte=dte,
                type=target_type,
                strike=strike,
                bid=round(bid, 4),
                ask=round(ask, 4),
                last=round(last, 4),
                mid=round(mid, 4),
                iv=round(iv, 2),
                delta=round(delta, 4),
                abs_delta=round(abs_delta, 4),
                oi=oi,
                volume=vol,
                otm_pct=otm_pct,
                annualized_yield_pct=ann_yield,
            ))

    # 排序：卖方按年化收益降序，买方按 |delta| 升序（更便宜）
    if is_seller:
        results.sort(key=lambda c: -(c.annualized_yield_pct or 0))
    else:
        results.sort(key=lambda c: (c.abs_delta, c.mid))

    return {
        "symbol": symbol,
        "futu_symbol": futu_sym,
        "_source": {
            "spot": "futu/get_market_snapshot",
            "chain": "futu/get_option_chain (full chain across DTE window) + per-contract get_market_snapshot",
            "filters/scoring": "computed: stock_quant.reports.option_screener (DTE/Δ/OTM%/OI/年化收益率)",
        },
        "spot": spot,
        "direction": direction,
        "filters": {
            "dte_range": list(dte_range),
            "delta_range": list(delta_range),
            "min_otm_pct": min_otm_pct,
            "min_yield_pct": min_yield_pct,
            "min_oi": min_oi,
            "min_volume": min_volume,
            "min_bid": min_bid,
        },
        "n_total": len(results),
        "results": [asdict(c) for c in results[:top_n]],
    }


def format_screen(data: dict[str, Any]) -> str:
    """终端友好输出（mimics 富途 skill 表格格式）。"""
    if "_error" in data:
        return f"⚠️  {data['_error']}\n  {data}"

    lines: list[str] = []
    sym = data["symbol"]
    spot = data["spot"]
    dirc = data["direction"]
    f = data["filters"]
    rows = data["results"]

    lines.append("=" * 110)
    lines.append(f"  🔍 {sym} 期权筛选 [{dirc}]   现价 ${spot:.2f}   命中 {data['n_total']} 条")
    lines.append("=" * 110)
    lines.append(
        f"  过滤：DTE {f['dte_range'][0]}-{f['dte_range'][1]} | "
        f"|Δ| {f['delta_range'][0]}-{f['delta_range'][1]} | "
        f"OTM≥{f['min_otm_pct']}% | "
        f"年化≥{f['min_yield_pct'] or '-'}% | OI≥{f['min_oi']}"
    )
    lines.append("-" * 110)
    if not rows:
        lines.append("  无命中合约。可放宽筛选条件。")
        return "\n".join(lines)

    header = f"  {'#':>2}  {'到期':10}  {'DTE':>4}  {'Strike':>8}  {'Bid/Ask':>14}  {'|Δ|':>5}  {'IV%':>6}  {'年化%':>7}  {'OI':>7}  {'Vol':>7}"
    lines.append(header)
    lines.append("  " + "-" * 106)
    for i, r in enumerate(rows, 1):
        ba = f"{r['bid']:.2f}/{r['ask']:.2f}"
        ann = f"{r['annualized_yield_pct']:.1f}" if r['annualized_yield_pct'] is not None else "-"
        lines.append(
            f"  {i:>2}  {r['expiry']:10}  {r['dte']:>4}  {r['strike']:>8.2f}  "
            f"{ba:>14}  {r['abs_delta']:>5.3f}  {r['iv']:>6.1f}  {ann:>7}  "
            f"{r['oi']:>7}  {r['volume']:>7}"
        )
    lines.append("=" * 110)
    return "\n".join(lines)
