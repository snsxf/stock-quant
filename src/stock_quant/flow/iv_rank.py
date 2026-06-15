"""IV Rank / IV Percentile 计算 + SQLite 历史落库。

IV Rank      = (current_iv - 1y_low) / (1y_high - 1y_low) * 100
IV Percentile = % of days in past 1y where IV < current_iv

  - IV Rank > 50  → 当前 IV 偏高，适合「卖方」策略（Iron Condor / 信用价差 / Cash-Secured Put）
  - IV Rank < 30  → 当前 IV 偏低，适合「买方」策略（直买 Call/Put / 借方价差）

历史落库使用 SQLite（项目根目录 data/iv_history.db），每次跑都自动 upsert 当日 ATM IV。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import settings


def _db_path() -> Path:
    base = Path(settings.cache_dir) if hasattr(settings, "cache_dir") else Path.home() / ".cache" / "stock_quant"
    base.mkdir(parents=True, exist_ok=True)
    return base / "iv_history.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS iv_history (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            atm_iv REAL NOT NULL,
            PRIMARY KEY (symbol, date)
        )
        """
    )
    return conn


def _atm_iv_from_chain(chain: pd.DataFrame, spot: float) -> float | None:
    """从期权链取 ATM IV：找到最接近 spot 的 strike 上 Call/Put 的 IV 平均。
    过滤掉 IV<=0 / IV>500（深度 OTM/ITM/0DTE 数学爆炸）。
    """
    if chain is None or chain.empty or spot is None:
        return None
    if "option_strike_price" not in chain.columns or "option_implied_volatility" not in chain.columns:
        return None

    df = chain.dropna(subset=["option_strike_price", "option_implied_volatility"]).copy()
    df = df[(df["option_implied_volatility"] > 0) & (df["option_implied_volatility"] < 500)]
    if df.empty:
        return None

    df["dist"] = (df["option_strike_price"] - spot).abs()
    df = df.sort_values("dist").head(4)
    iv_avg = float(df["option_implied_volatility"].mean())
    return iv_avg if iv_avg > 0 else None


def upsert_today_iv(symbol: str, atm_iv: float) -> None:
    if atm_iv is None or atm_iv <= 0:
        return
    today = date.today().strftime("%Y-%m-%d")
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO iv_history (symbol, date, atm_iv) VALUES (?, ?, ?)",
            (symbol, today, float(atm_iv)),
        )
        conn.commit()


def load_iv_history(symbol: str, days: int = 365) -> pd.DataFrame:
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as conn:
        df = pd.read_sql_query(
            "SELECT date, atm_iv FROM iv_history WHERE symbol = ? AND date >= ? ORDER BY date",
            conn, params=(symbol, cutoff),
        )
    return df


def calc_iv_rank(symbol: str, current_iv: float, lookback_days: int = 365) -> dict[str, Any]:
    """返回 IV Rank / Percentile + 1y 高低。

    注意：current_iv 单位为「百分点」（如 50 表示 50%），与 Futu 返回口径一致。
    历史不足时仍返回当前值，标记数据不足。
    """
    upsert_today_iv(symbol, current_iv)

    hist = load_iv_history(symbol, days=lookback_days)
    n = len(hist)
    if n < 5:
        return {
            "current_iv": round(current_iv, 2) if current_iv else None,
            "iv_rank": None,
            "iv_percentile": None,
            "iv_1y_high": None,
            "iv_1y_low": None,
            "history_days": n,
            "_note": "历史数据不足 5 天，IV Rank 暂不可用。每天跑一次 brief 即可累积。",
        }

    iv_high = float(hist["atm_iv"].max())
    iv_low = float(hist["atm_iv"].min())
    iv_rank = (current_iv - iv_low) / (iv_high - iv_low) * 100 if iv_high > iv_low else 50.0
    iv_pct = (hist["atm_iv"] < current_iv).sum() / n * 100

    return {
        "current_iv": round(current_iv, 2),
        "iv_rank": round(iv_rank, 1),
        "iv_percentile": round(iv_pct, 1),
        "iv_1y_high": round(iv_high, 2),
        "iv_1y_low": round(iv_low, 2),
        "history_days": n,
        "regime": (
            "high (sell premium)" if iv_rank > 50 else
            "low (buy premium)" if iv_rank < 30 else
            "neutral"
        ),
    }
