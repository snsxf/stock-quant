"""期权 IV 期限结构（Term Structure）：跨多个到期日观察 ATM IV。

  - Contango: 远月 IV > 近月 IV（正常市场，平滑升）→ 无短期事件
  - Backwardation: 近月 IV > 远月 IV → 市场定价短期事件（财报、CPI、FOMC...）
                   常见于「事件驱动」+「事件后 IV crush」前夕，**慎做买方**
  - Steep front-month spike: 仅最近一个到期日 IV 突高 → 周内有事件
"""
from __future__ import annotations

from typing import Any

import pandas as pd


def build_term_structure(fs, symbol: str, n_expiries: int = 5) -> dict[str, Any]:
    """逐个到期日拉链，取 ATM IV，返回期限结构。

    Args:
        fs: FutuSource 实例
        symbol: 形如 'US.NVDA'
        n_expiries: 取最近 N 个到期日

    Returns:
        {
            "spot": 215.20,
            "term": [
                {"expiry": "2026-05-09", "dte": 1, "atm_iv": 1.21},
                {"expiry": "2026-05-16", "dte": 8, "atm_iv": 0.52},
                ...
            ],
            "shape": "backwardation" | "contango" | "flat" | "mixed",
            "front_back_diff": 0.69,  # term[0].iv - term[-1].iv
        }
    """
    from futu import RET_OK
    from ..datasource.futu import (
        OPTION_CHAIN_BUDGET,
        QUOTE_BUDGET,
    )

    out: dict[str, Any] = {"spot": None, "term": [], "shape": None}
    try:
        with fs._ctx() as ctx:
            QUOTE_BUDGET.acquire()
            ret_q, snap = ctx.get_market_snapshot([symbol])
            if ret_q == RET_OK and not snap.empty:
                out["spot"] = float(snap.iloc[0].get("last_price") or 0) or None

            OPTION_CHAIN_BUDGET.acquire()
            ret, exp_df = ctx.get_option_expiration_date(code=symbol)
            if ret != RET_OK or exp_df.empty:
                return out

            expiries = exp_df.head(n_expiries + 2).to_dict("records")
            spot = out["spot"]

            for er in expiries:
                expiry = er.get("strike_time")
                dte = er.get("option_expiry_date_distance")
                # 过滤已过期 / 0DTE（IV 数学上爆炸）
                if dte is None or dte < 1:
                    continue
                OPTION_CHAIN_BUDGET.acquire()
                ret_c, chain = ctx.get_option_chain(code=symbol, start=expiry, end=expiry)
                if ret_c != RET_OK or chain.empty:
                    continue

                if spot is None:
                    continue

                near_chain = chain.copy()
                strike_col = "option_strike_price" if "option_strike_price" in near_chain.columns else "strike_price"
                if strike_col not in near_chain.columns:
                    continue
                near_chain = near_chain.dropna(subset=[strike_col])
                if near_chain.empty:
                    continue
                near_chain = near_chain.assign(_dist=(near_chain[strike_col] - spot).abs())
                codes = near_chain.sort_values("_dist")["code"].tolist()[:12]
                if not codes:
                    continue

                QUOTE_BUDGET.acquire()
                ret_s, csnap = ctx.get_market_snapshot(codes)
                if ret_s != RET_OK or csnap.empty:
                    continue

                # 过滤无效 IV（0 / 异常高，可能是深度 OTM/ITM 噪声）
                csnap = csnap[(csnap.get("option_implied_volatility", 0) > 0) &
                              (csnap.get("option_implied_volatility", 0) < 500)]
                if csnap.empty:
                    continue

                csnap = csnap.assign(_dist=(csnap["option_strike_price"] - spot).abs())
                near = csnap.nsmallest(4, "_dist")
                atm_iv = float(near["option_implied_volatility"].mean())
                if atm_iv > 0:
                    out["term"].append({
                        "expiry": expiry,
                        "dte": int(dte),
                        "atm_iv": round(atm_iv, 4),
                    })
                if len(out["term"]) >= n_expiries:
                    break
    except Exception as e:
        out["_error"] = str(e)
        return out

    if len(out["term"]) >= 2:
        front = out["term"][0]["atm_iv"]
        back = out["term"][-1]["atm_iv"]
        diff = front - back
        out["front_back_diff"] = round(diff, 4)
        if abs(diff) < 0.02:
            out["shape"] = "flat"
        elif front > back * 1.05:
            out["shape"] = "backwardation"
        elif back > front * 1.05:
            out["shape"] = "contango"
        else:
            out["shape"] = "mixed"

    return out
