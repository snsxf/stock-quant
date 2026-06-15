"""期权定价与 Greeks：基于 mibian（纯 Python，无 C 依赖，兼容 3.11+）。

注意 mibian 的习惯：
- 波动率传百分比（如 50 而非 0.5）
- 利率传百分比（如 5 而非 0.05）
- 时间单位是 **天**（如 30 天到期直接传 30）
"""
from __future__ import annotations

from dataclasses import dataclass

import mibian


@dataclass
class OptionGreeks:
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    iv: float | None = None


def _bs(S: float, K: float, r_pct: float, T_days: float, sigma_pct: float) -> mibian.BS:
    return mibian.BS([S, K, r_pct, T_days], volatility=sigma_pct)


def price_bs(option_type: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Black-Scholes 理论价格。
    :param option_type: 'call' / 'put'
    :param T: 到期年化时间（小数，如 30/365）
    :param r: 无风险利率小数（如 0.05）
    :param sigma: 波动率小数（如 0.5 表示 50%）
    """
    bs = _bs(S, K, r * 100, T * 365, sigma * 100)
    return bs.callPrice if option_type.lower().startswith("c") else bs.putPrice


def iv_from_price(
    option_type: str, price: float, S: float, K: float, T: float, r: float
) -> float:
    """从市场价反推隐含波动率（返回小数，如 0.45）。"""
    is_call = option_type.lower().startswith("c")
    kwargs = {"callPrice": price} if is_call else {"putPrice": price}
    bs = mibian.BS([S, K, r * 100, T * 365], **kwargs)
    return bs.impliedVolatility / 100.0


def greeks(
    option_type: str, S: float, K: float, T: float, r: float, sigma: float
) -> OptionGreeks:
    """返回完整 Greeks（全部以小数/美元表示）。"""
    bs = _bs(S, K, r * 100, T * 365, sigma * 100)
    is_call = option_type.lower().startswith("c")
    return OptionGreeks(
        price=bs.callPrice if is_call else bs.putPrice,
        delta=bs.callDelta if is_call else bs.putDelta,
        gamma=bs.gamma,
        theta=bs.callTheta if is_call else bs.putTheta,
        vega=bs.vega,
        rho=bs.callRho if is_call else bs.putRho,
        iv=sigma,
    )


def max_pain(chain_df) -> float | None:
    """估算期权最大痛点（Max Pain）。chain_df 需含 strike / openInterest / option_type。"""
    if chain_df.empty or "strike" not in chain_df.columns:
        return None
    strikes = sorted(chain_df["strike"].unique())
    losses = []
    for k in strikes:
        call_loss = (
            chain_df.loc[(chain_df["option_type"] == "CALL") & (chain_df["strike"] < k)]
            .assign(pain=lambda d: (k - d["strike"]) * d["openInterest"].fillna(0))
            ["pain"].sum()
        )
        put_loss = (
            chain_df.loc[(chain_df["option_type"] == "PUT") & (chain_df["strike"] > k)]
            .assign(pain=lambda d: (d["strike"] - k) * d["openInterest"].fillna(0))
            ["pain"].sum()
        )
        losses.append((k, call_loss + put_loss))
    return min(losses, key=lambda x: x[1])[0] if losses else None
