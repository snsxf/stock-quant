"""期权链异动快扫：基于 Futu 期权链 + 历史 OI 比对，输出 Volume/OI 比值高的合约。"""
from __future__ import annotations

from typing import Any

import pandas as pd


def scan_unusual(chain: pd.DataFrame, min_vol_oi_ratio: float = 2.0, top_n: int = 5) -> list[dict[str, Any]]:
    """
    输入：Futu 期权链 DataFrame（含 volume, option_open_interest, option_strike_price, option_type, last_price, option_implied_volatility 等列）
    输出：Volume/OI > 阈值 的 top_n 合约
    """
    if chain is None or chain.empty:
        return []

    df = chain.copy()
    if "volume" not in df.columns or "option_open_interest" not in df.columns:
        return []

    df["vol_oi_ratio"] = df.apply(
        lambda r: (r["volume"] / r["option_open_interest"])
        if r.get("option_open_interest") and r["option_open_interest"] > 0
        else 0.0,
        axis=1,
    )
    df = df[df["vol_oi_ratio"] >= min_vol_oi_ratio]
    df = df.sort_values("vol_oi_ratio", ascending=False).head(top_n)

    out = []
    for _, r in df.iterrows():
        out.append({
            "code": r.get("code"),
            "type": r.get("option_type"),
            "strike": float(r.get("option_strike_price") or 0),
            "last": float(r.get("last_price") or 0),
            "iv": float(r.get("option_implied_volatility") or 0),
            "delta": float(r.get("option_delta") or 0),
            "volume": int(r.get("volume") or 0),
            "oi": int(r.get("option_open_interest") or 0),
            "vol_oi": round(float(r.get("vol_oi_ratio") or 0), 2),
        })
    return out


def chain_summary(chain: pd.DataFrame) -> dict[str, Any]:
    """期权链整体摘要：put/call ratio (按 volume) + ATM IV 中位 + 最大 OI strike (近似 max pain)。"""
    if chain is None or chain.empty:
        return {}

    df = chain.copy()
    chain_total_contracts = _meta_int(df, "_chain_total_contracts")
    snapshot_contracts = _meta_int(df, "_snapshot_contracts")
    is_truncated = bool(df["_is_truncated"].iloc[0]) if "_is_truncated" in df.columns and not df.empty else None
    calls = df[df.get("option_type", "") == "CALL"]
    puts = df[df.get("option_type", "") == "PUT"]

    call_vol = float(calls["volume"].sum()) if "volume" in calls.columns else 0.0
    put_vol = float(puts["volume"].sum()) if "volume" in puts.columns else 0.0
    pcr = round(put_vol / call_vol, 3) if call_vol > 0 else None

    # 用全链 OI 加权 strike 近似 max-pain
    max_oi_strike = None
    if "option_open_interest" in df.columns and "option_strike_price" in df.columns:
        gp = df.groupby("option_strike_price")["option_open_interest"].sum().sort_values(ascending=False)
        if not gp.empty:
            max_oi_strike = float(gp.index[0])

    iv_med = None
    if "option_implied_volatility" in df.columns:
        iv_series = df["option_implied_volatility"].dropna()
        if not iv_series.empty:
            iv_med = round(float(iv_series.median()), 2)

    result = {
        "call_volume": int(call_vol),
        "put_volume": int(put_vol),
        "put_call_ratio": pcr,
        "max_oi_strike": max_oi_strike,
        "iv_median": iv_med,
    }
    if chain_total_contracts is not None:
        result["chain_total_contracts"] = chain_total_contracts
    if snapshot_contracts is not None:
        result["snapshot_contracts"] = snapshot_contracts
    if is_truncated is not None:
        result["is_truncated"] = is_truncated
    return result


def _meta_int(df: pd.DataFrame, col: str) -> int | None:
    if col not in df.columns or df.empty:
        return None
    value = df[col].iloc[0]
    if pd.isna(value):
        return None
    return int(value)
