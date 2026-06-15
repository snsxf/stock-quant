"""Max Pain (最大痛点) 计算。

最大痛点 = 在到期日，期权多头总损失（call holders + put holders）最小的 strike。
直觉：临近到期，标的价格大概率被「钉」在 max pain 附近（理论 + 实证常见）。

公式：
    对每个候选 strike S：
        Total_Pain(S) = Σ_call(max(S - K_call, 0) * OI_call) + Σ_put(max(K_put - S, 0) * OI_put)
    Max Pain = argmin_S Total_Pain(S)
"""
from __future__ import annotations

from typing import Any

import pandas as pd


def calc_max_pain(chain: pd.DataFrame) -> dict[str, Any]:
    """输入 Futu 期权链 DataFrame（含 option_type / option_strike_price / option_open_interest）。

    返回:
        {
            "max_pain": 215.0,              # 最大痛点 strike
            "spot_to_max_pain_pct": -2.3,   # 现价偏离 max pain 的百分比（需要外部传入）
            "pain_curve": [(strike, pain), ...],  # 全部 strike 的 pain 曲线（前 10）
            "total_call_oi": 12345,
            "total_put_oi": 8901,
            "pcr_oi": 0.72,
        }
    """
    if chain is None or chain.empty:
        return {}
    if "option_type" not in chain.columns or "option_strike_price" not in chain.columns:
        return {}
    if "option_open_interest" not in chain.columns:
        return {}

    df = chain.copy()
    df = df.dropna(subset=["option_strike_price", "option_open_interest"])
    if df.empty:
        return {}

    calls = df[df["option_type"] == "CALL"][["option_strike_price", "option_open_interest"]].rename(
        columns={"option_strike_price": "K", "option_open_interest": "OI"}
    )
    puts = df[df["option_type"] == "PUT"][["option_strike_price", "option_open_interest"]].rename(
        columns={"option_strike_price": "K", "option_open_interest": "OI"}
    )

    strikes = sorted(set(df["option_strike_price"].dropna().tolist()))
    if not strikes:
        return {}

    pain_curve: list[tuple[float, float]] = []
    for S in strikes:
        call_pain = float(((S - calls["K"]).clip(lower=0) * calls["OI"]).sum())
        put_pain = float(((puts["K"] - S).clip(lower=0) * puts["OI"]).sum())
        pain_curve.append((float(S), call_pain + put_pain))

    pain_curve.sort(key=lambda x: x[1])
    max_pain_strike = pain_curve[0][0]

    total_call_oi = int(calls["OI"].sum())
    total_put_oi = int(puts["OI"].sum())
    pcr_oi = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else None
    chain_total_contracts = _meta_int(df, "_chain_total_contracts")
    snapshot_contracts = _meta_int(df, "_snapshot_contracts")
    is_truncated = bool(df["_is_truncated"].iloc[0]) if "_is_truncated" in df.columns and not df.empty else None
    warnings: list[str] = []
    if is_truncated:
        warnings.append(
            f"期权链快照被截断：仅使用 {snapshot_contracts} / {chain_total_contracts} 张合约，PCR_OI/Max Pain 可能失真"
        )
    elif chain_total_contracts and snapshot_contracts and snapshot_contracts < chain_total_contracts:
        warnings.append(
            f"期权链快照未覆盖全量：仅使用 {snapshot_contracts} / {chain_total_contracts} 张合约"
        )

    pain_curve_sorted = sorted(pain_curve, key=lambda x: x[0])
    result = {
        "max_pain": max_pain_strike,
        "min_pain_value": round(pain_curve[0][1], 2),
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "pcr_oi": pcr_oi,
        "pain_curve_top5_low": pain_curve[:5],
        "pain_curve_full": pain_curve_sorted,
    }
    if chain_total_contracts is not None:
        result["chain_total_contracts"] = chain_total_contracts
    if snapshot_contracts is not None:
        result["snapshot_contracts"] = snapshot_contracts
    if is_truncated is not None:
        result["is_truncated"] = is_truncated
    if warnings:
        result["_warnings"] = warnings
    return result


def _meta_int(df: pd.DataFrame, col: str) -> int | None:
    if col not in df.columns or df.empty:
        return None
    value = df[col].iloc[0]
    if pd.isna(value):
        return None
    return int(value)
