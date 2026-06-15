"""期权决策引擎：综合 brief 数据自动给出今日推荐策略 + 具体合约参数。

核心逻辑：
  1. 方向得分（direction_score）∈ [-100, +100]
       - 技术面：MA 排列、RSI、MACD、近期涨跌幅
       - 资金流：主力净流入
       - 催化剂：评级共识、目标价上行空间、内部人交易
       - 市场环境：板块 ETF、VIX 状态
       → 综合后映射到 [-100(强空) ~ +100(强多)]

  2. IV 环境（iv_regime）：从 IV Rank 决定「买方 vs 卖方」
       - IV Rank > 50 → 卖方策略（信用价差 / Iron Condor / CSP）
       - IV Rank < 30 → 买方策略（直买 Call / Put / 借方价差）
       - IV 历史不足时 fallback 用 IV vs HV20 比较

  3. Term Structure 风险：
       - Backwardation → 短期事件，慎做买方
       - 财报 7 日内 → 强制退出建议在财报前

  4. Max Pain 钉价 → 优先选择靠近 Max Pain 的 strike

  5. 输出 1-3 个策略，每个含：
       - 名称、理由、具体合约（到期+strike+期权类型）
       - 预估权利金（mid 价）、最大盈/亏、盈亏平衡点
       - Greeks、退出计划
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..analysis.options import greeks
from ..datasource import FutuSource
from ..datasource.router import to_futu_symbol
from .daily_brief import build_brief


# ============================================================
# 评分模型
# ============================================================
@dataclass
class DirectionScore:
    score: float = 0.0  # [-100, +100]
    breakdown: list[tuple[str, float, str]] = field(default_factory=list)

    def add(self, name: str, value: float, note: str = "") -> None:
        self.score += value
        self.breakdown.append((name, round(value, 2), note))

    @property
    def label(self) -> str:
        if self.score >= 50:
            return "strong_bullish"
        if self.score >= 20:
            return "bullish"
        if self.score > -20:
            return "neutral"
        if self.score > -50:
            return "bearish"
        return "strong_bearish"


def evaluate_direction(brief: dict[str, Any]) -> DirectionScore:
    """根据 brief 综合各维度数据，给出方向得分。"""
    s = DirectionScore()

    tech = brief.get("technical") or {}
    if "_error" not in tech:
        # MA 排列：5d > 20d > 50d 加分
        ma5, ma20, ma50 = tech.get("ma5"), tech.get("ma20"), tech.get("ma50")
        if ma5 and ma20 and ma50:
            if ma5 > ma20 > ma50:
                s.add("MA 多头排列", +15, f"MA5({ma5:.2f})>MA20({ma20:.2f})>MA50({ma50:.2f})")
            elif ma5 < ma20 < ma50:
                s.add("MA 空头排列", -15, f"MA5<MA20<MA50")

        # RSI
        rsi = tech.get("rsi14")
        if rsi:
            if rsi > 75:
                s.add("RSI 严重超买", -10, f"RSI={rsi:.1f}")
            elif rsi > 65:
                s.add("RSI 偏强", +5, f"RSI={rsi:.1f}")
            elif rsi < 25:
                s.add("RSI 严重超卖", +10, f"RSI={rsi:.1f}")
            elif rsi < 35:
                s.add("RSI 偏弱", -5, f"RSI={rsi:.1f}")

        # MACD
        if tech.get("macd_cross") == "golden":
            s.add("MACD 金叉", +8, "MACD 由负转正")
        elif tech.get("macd_cross") == "death":
            s.add("MACD 死叉", -8, "MACD 由正转负")

        # 近期动能
        for n, k in [(5, "ret_5d"), (20, "ret_20d")]:
            r = tech.get(k)
            if r is None:
                continue
            if r > 10:
                s.add(f"{n}d 强势 +{r:.1f}%", +6)
            elif r > 3:
                s.add(f"{n}d 偏强 +{r:.1f}%", +3)
            elif r < -10:
                s.add(f"{n}d 弱势 {r:.1f}%", -6)
            elif r < -3:
                s.add(f"{n}d 偏弱 {r:.1f}%", -3)

    # 资金流
    cf = brief.get("capital_flow") or {}
    if "_error" not in cf and cf.get("in_flow") is not None:
        flow = float(cf["in_flow"])
        if flow > 100e6:
            s.add("主力净流入大", +10, f"+${flow/1e6:.1f}M")
        elif flow > 0:
            s.add("主力净流入", +4, f"+${flow/1e6:.1f}M")
        elif flow < -100e6:
            s.add("主力净流出大", -10, f"${flow/1e6:.1f}M")
        else:
            s.add("主力净流出", -4, f"${flow/1e6:.1f}M")

    # 聪明钱（super + big）：与散户（mid+sml）方向背离时给额外信号
    if "_error" not in cf and cf.get("smart_money") is not None:
        smart = float(cf["smart_money"])
        retail = float((cf.get("mid_in_flow") or 0) + (cf.get("sml_in_flow") or 0))
        if smart > 50e6 and retail < 0:
            s.add("聪明钱进/散户撤", +8, f"smart +${smart/1e6:.1f}M, retail ${retail/1e6:.1f}M")
        elif smart < -50e6 and retail > 0:
            s.add("聪明钱撤/散户进", -8, f"smart ${smart/1e6:.1f}M, retail +${retail/1e6:.1f}M")
        elif smart > 100e6 and retail > 0:
            s.add("聪明钱+散户共振多", +5, f"smart +${smart/1e6:.1f}M")
        elif smart < -100e6 and retail < 0:
            s.add("聪明钱+散户共振空", -5, f"smart ${smart/1e6:.1f}M")

    # 资讯情绪（融合富途/Google News 事件归因 + 关键词打分）
    ns = brief.get("news_sentiment") or {}
    if "_error" not in ns and ns.get("score") is not None:
        sent_score = float(ns["score"])  # [-1, +1]
        n_total = (ns.get("_stats") or {}).get("total") or 0
        if n_total >= 3:  # 至少 3 条新闻才纳入评分
            weight = round(sent_score * 8, 1)
            if abs(weight) >= 1:
                tag = "资讯偏多" if weight > 0 else "资讯偏空"
                s.add(tag, weight, f"score={sent_score:+.2f} ({n_total}条)")

    # 社区情绪（融合 StockTwits + Futunn 牛牛圈，bull/bear 占比）
    cs = brief.get("community_sentiment") or {}
    if "_error" not in cs and cs.get("n_posts", 0) >= 5:
        bull_pct = float(cs.get("bull_pct") or 0)
        bear_pct = float(cs.get("bear_pct") or 0)
        diff = bull_pct - bear_pct  # 范围 [-100, +100]
        # 映射：差距≥40→±6 分，≥20→±3 分，否则忽略
        if diff >= 40:
            s.add("社区情绪强多", +6, f"bull {bull_pct}% / bear {bear_pct}%")
        elif diff >= 20:
            s.add("社区情绪偏多", +3, f"bull {bull_pct}% / bear {bear_pct}%")
        elif diff <= -40:
            s.add("社区情绪强空", -6, f"bull {bull_pct}% / bear {bear_pct}%")
        elif diff <= -20:
            s.add("社区情绪偏空", -3, f"bull {bull_pct}% / bear {bear_pct}%")

    # 催化剂
    cat = brief.get("catalyst") or {}
    rating = cat.get("rating") or {}
    if rating:
        bull_bear = rating.get("bull_bear_score") or rating.get("bull_bear")
        if bull_bear is not None:
            # bull_bear 范围一般 [-1, 1]
            s.add(f"分析师共识 {bull_bear:+.2f}", round(bull_bear * 12, 1))

    pt = cat.get("price_target") or {}
    upside = pt.get("upside_pct") or pt.get("upside")
    if upside is not None:
        if upside > 30:
            s.add(f"目标价上行 +{upside:.1f}%", +12)
        elif upside > 10:
            s.add(f"目标价上行 +{upside:.1f}%", +6)
        elif upside < -20:
            s.add(f"目标价已透支 {upside:.1f}%", -12, "现价高于目标")
        elif upside < 0:
            s.add(f"目标价已超 {upside:.1f}%", -4)

    insider = cat.get("insider_90d") or {}
    net_raw = insider.get("net_value_usd", insider.get("net_amount"))
    if net_raw is not None:
        net = float(net_raw)
        if net < -50e6:
            s.add("内部人大幅净卖出", -8, f"${net/1e6:.1f}M")
        elif net > 50e6:
            s.add("内部人大幅净买入", +8, f"+${net/1e6:.1f}M")

    # 财报临近
    earn = cat.get("earnings_next") or {}
    days_to_earnings = earn.get("days_until")
    if earn.get("status") == "scheduled" and days_to_earnings is not None and 0 <= days_to_earnings <= 7:
        s.add(f"财报 {days_to_earnings} 天后", -5, "事件前不确定性增加")

    # 板块环境
    me = brief.get("market_env") or {}
    indices = me.get("indices") or {}
    sector_changes = []
    for tk in ["SMH", "SOXX", "XLK", "QQQ", "SPY"]:
        v = indices.get(tk)
        if v and v.get("change_rate") is not None:
            sector_changes.append(v["change_rate"])
    if sector_changes:
        avg = sum(sector_changes) / len(sector_changes)
        if avg > 2:
            s.add(f"板块/大盘强势 +{avg:.1f}%", +8)
        elif avg > 0.5:
            s.add(f"板块/大盘偏强 +{avg:.1f}%", +3)
        elif avg < -2:
            s.add(f"板块/大盘弱势 {avg:.1f}%", -8)
        elif avg < -0.5:
            s.add(f"板块/大盘偏弱 {avg:.1f}%", -3)

    # clamp
    s.score = max(-100, min(100, s.score))
    return s


# ============================================================
# IV 环境
# ============================================================
def evaluate_iv_regime(brief: dict[str, Any]) -> dict[str, Any]:
    opt = brief.get("options") or {}
    iv_rank = (opt.get("iv_rank") or {})
    tech = brief.get("technical") or {}
    hv20 = tech.get("hv20_pct")

    current_iv = iv_rank.get("current_iv")
    rank = iv_rank.get("iv_rank")

    regime = "neutral"
    if rank is not None:
        if rank > 60:
            regime = "high"
        elif rank < 30:
            regime = "low"
        else:
            regime = "medium"
    elif current_iv is not None and hv20:
        # fallback: 比较 IV 与 HV20
        ratio = current_iv / hv20
        if ratio > 1.3:
            regime = "high"
        elif ratio < 0.85:
            regime = "low"
        else:
            regime = "medium"

    return {
        "regime": regime,
        "iv_rank": rank,
        "current_iv": current_iv,
        "hv20": hv20,
        "iv_hv_ratio": round(current_iv / hv20, 2) if (current_iv and hv20) else None,
        "side": "sell" if regime == "high" else "buy" if regime == "low" else "either",
    }


def evaluate_term_structure(brief: dict[str, Any]) -> dict[str, Any]:
    ts = brief.get("term_structure") or {}
    shape = ts.get("shape")
    return {
        "shape": shape,
        "warn_buy_side": shape == "backwardation",  # 慎做买方
        "front_back_diff": ts.get("front_back_diff"),
        "term": ts.get("term") or [],
    }


# ============================================================
# 策略生成器
# ============================================================
@dataclass
class StrategyLeg:
    action: str          # 'BUY' / 'SELL'
    type: str            # 'CALL' / 'PUT'
    strike: float
    expiry: str
    qty: int = 1
    premium_mid: float = 0.0  # 单腿权利金（mid 价，正数）
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0


# ============================================================
# 中英文映射（用于提升可读性）
# ============================================================
_ACTION_CN = {"BUY": "买入", "SELL": "卖出"}
_TYPE_CN = {"CALL": "看涨", "PUT": "看跌"}
_DIRECTION_CN = {
    "strong_bullish": "强烈看多",
    "bullish": "看多",
    "neutral": "中性",
    "bearish": "看空",
    "strong_bearish": "强烈看空",
}
_RISK_CN = {"low": "低风险", "medium": "中风险", "high": "高风险"}
_EXIT_KEY_CN = {
    "take_profit": "止盈",
    "stop_loss": "止损",
    "time_stop": "时间止损",
    "roll": "移仓换月",
    "adjust": "调整方案",
}


def _leg_summary_cn(leg: dict) -> str:
    """生成一行人类可读的中文 leg 摘要。

    例: "买入 看涨期权 (Long Call) @ K=$225.00 到期 2026-05-22"
    """
    action_cn = _ACTION_CN.get(leg.get("action", ""), leg.get("action", ""))
    type_cn = _TYPE_CN.get(leg.get("type", ""), leg.get("type", ""))
    en = f"{'Long' if leg.get('action') == 'BUY' else 'Short'} {leg.get('type', '').title()}"
    return f"{action_cn}{type_cn}期权 ({en}) @ K=${leg.get('strike', 0):.2f} 到期 {leg.get('expiry', '')}"


@dataclass
class Strategy:
    name: str
    rationale: str
    legs: list[StrategyLeg]
    direction: str       # bullish/bearish/neutral
    risk_level: str      # low/medium/high
    net_debit: float = 0.0   # 正=付权利金，负=收权利金
    max_profit: float = 0.0
    max_loss: float = 0.0
    breakeven: list[float] = field(default_factory=list)
    exit_plan: dict[str, str] = field(default_factory=dict)


def _select_expiry(term: list[dict], dte_min: int = 5, dte_max: int = 35) -> dict | None:
    """从 term_structure 选出合适到期日（默认 5-35 DTE）。"""
    candidates = [t for t in term if dte_min <= (t.get("dte") or 0) <= dte_max]
    if not candidates:
        return term[0] if term else None
    return candidates[0]


def _fetch_chain_for_expiry(futu_sym: str, expiry: str) -> pd.DataFrame:
    """拉指定到期日的完整期权链（带快照报价）。"""
    fs = FutuSource()
    return fs.get_option_chain(futu_sym, expiry=expiry, max_contracts=None)


def _find_strike(chain: pd.DataFrame, target_strike: float, opt_type: str) -> dict | None:
    """从 chain 里找最接近 target_strike 的指定类型合约（CALL/PUT）。"""
    if chain.empty:
        return None
    df = chain[chain["option_type"] == opt_type].copy()
    if df.empty:
        return None
    df["dist"] = (df["option_strike_price"] - target_strike).abs()
    df = df.sort_values("dist").head(1)
    if df.empty:
        return None
    r = df.iloc[0]
    bid = float(r.get("bid_price") or 0)
    ask = float(r.get("ask_price") or 0)
    last = float(r.get("last_price") or 0)
    mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
    return {
        "code": r.get("code"),
        "strike": float(r.get("option_strike_price") or 0),
        "type": opt_type,
        "premium_mid": round(mid, 4),
        "bid": bid,
        "ask": ask,
        "last": last,
        "iv": float(r.get("option_implied_volatility") or 0),
        "delta": float(r.get("option_delta") or 0),
        "gamma": float(r.get("option_gamma") or 0),
        "theta": float(r.get("option_theta") or 0),
        "vega": float(r.get("option_vega") or 0),
        "oi": int(r.get("option_open_interest") or 0),
        "volume": int(r.get("volume") or 0),
    }


def _strategy_bull_call_spread(spot: float, expiry: str, chain: pd.DataFrame) -> Strategy | None:
    """牛市看涨价差：买近 ATM call，卖 OTM call。"""
    long_strike = round(spot)
    short_strike = round(spot * 1.025)  # +2.5%
    long_leg = _find_strike(chain, long_strike, "CALL")
    short_leg = _find_strike(chain, short_strike, "CALL")
    if not long_leg or not short_leg or long_leg["strike"] >= short_leg["strike"]:
        return None
    if long_leg["premium_mid"] <= 0 or short_leg["premium_mid"] <= 0:
        return None
    net_debit = long_leg["premium_mid"] - short_leg["premium_mid"]
    width = short_leg["strike"] - long_leg["strike"]
    max_profit = width - net_debit
    return Strategy(
        name="Bull Call Spread (牛市看涨价差)",
        rationale="方向偏多。买近 ATM Call、卖 OTM Call 降低权利金成本，限制最大亏损同时锁定 Theta 风险。",
        direction="bullish",
        risk_level="low",
        legs=[
            StrategyLeg("BUY", "CALL", long_leg["strike"], expiry, 1, long_leg["premium_mid"],
                        long_leg["delta"], long_leg["gamma"], long_leg["theta"], long_leg["vega"]),
            StrategyLeg("SELL", "CALL", short_leg["strike"], expiry, 1, short_leg["premium_mid"],
                        short_leg["delta"], short_leg["gamma"], short_leg["theta"], short_leg["vega"]),
        ],
        net_debit=round(net_debit, 2),
        max_profit=round(max_profit, 2),
        max_loss=round(net_debit, 2),
        breakeven=[round(long_leg["strike"] + net_debit, 2)],
        exit_plan={
            "take_profit": f"价差涨到 ${round(width * 0.8, 2)}（80% 最大利润）",
            "stop_loss": f"价差跌到 ${round(net_debit * 0.5, 2)}（损失 50%）",
            "time_stop": "到期前 2 个交易日强制平仓避免 Gamma 爆炸",
        },
    )


def _strategy_bear_put_spread(spot: float, expiry: str, chain: pd.DataFrame) -> Strategy | None:
    """熊市看跌价差：买近 ATM put，卖 OTM put。"""
    long_strike = round(spot)
    short_strike = round(spot * 0.975)
    long_leg = _find_strike(chain, long_strike, "PUT")
    short_leg = _find_strike(chain, short_strike, "PUT")
    if not long_leg or not short_leg or long_leg["strike"] <= short_leg["strike"]:
        return None
    if long_leg["premium_mid"] <= 0 or short_leg["premium_mid"] <= 0:
        return None
    net_debit = long_leg["premium_mid"] - short_leg["premium_mid"]
    width = long_leg["strike"] - short_leg["strike"]
    max_profit = width - net_debit
    return Strategy(
        name="Bear Put Spread (熊市看跌价差)",
        rationale="方向偏空且不愿全额付 Put 权利金，限制风险博下跌。",
        direction="bearish",
        risk_level="low",
        legs=[
            StrategyLeg("BUY", "PUT", long_leg["strike"], expiry, 1, long_leg["premium_mid"],
                        long_leg["delta"], long_leg["gamma"], long_leg["theta"], long_leg["vega"]),
            StrategyLeg("SELL", "PUT", short_leg["strike"], expiry, 1, short_leg["premium_mid"],
                        short_leg["delta"], short_leg["gamma"], short_leg["theta"], short_leg["vega"]),
        ],
        net_debit=round(net_debit, 2),
        max_profit=round(max_profit, 2),
        max_loss=round(net_debit, 2),
        breakeven=[round(long_leg["strike"] - net_debit, 2)],
        exit_plan={
            "take_profit": f"价差涨到 ${round(width * 0.8, 2)}（80% 最大利润）",
            "stop_loss": f"价差跌到 ${round(net_debit * 0.5, 2)}",
            "time_stop": "到期前 2 个交易日强制平仓",
        },
    )


def _strategy_iron_condor(spot: float, expiry: str, chain: pd.DataFrame) -> Strategy | None:
    """铁鹰：高 IV 中性观点。卖 OTM Call & Put，买更远 OTM 对冲。"""
    short_call_k = round(spot * 1.04)
    long_call_k = round(spot * 1.07)
    short_put_k = round(spot * 0.96)
    long_put_k = round(spot * 0.93)

    sc = _find_strike(chain, short_call_k, "CALL")
    lc = _find_strike(chain, long_call_k, "CALL")
    sp = _find_strike(chain, short_put_k, "PUT")
    lp = _find_strike(chain, long_put_k, "PUT")
    legs = [sc, lc, sp, lp]
    if any(x is None for x in legs):
        return None
    if any(x["premium_mid"] <= 0 for x in legs):
        return None

    net_credit = sc["premium_mid"] + sp["premium_mid"] - lc["premium_mid"] - lp["premium_mid"]
    if net_credit <= 0:
        return None
    width = max(lc["strike"] - sc["strike"], sp["strike"] - lp["strike"])
    max_loss = width - net_credit
    return Strategy(
        name="Iron Condor (铁鹰)",
        rationale="高 IV + 中性观点。卖 OTM Call & Put 收 Theta，远端对冲限制最大亏损。",
        direction="neutral",
        risk_level="medium",
        legs=[
            StrategyLeg("SELL", "CALL", sc["strike"], expiry, 1, sc["premium_mid"], sc["delta"], sc["gamma"], sc["theta"], sc["vega"]),
            StrategyLeg("BUY",  "CALL", lc["strike"], expiry, 1, lc["premium_mid"], lc["delta"], lc["gamma"], lc["theta"], lc["vega"]),
            StrategyLeg("SELL", "PUT",  sp["strike"], expiry, 1, sp["premium_mid"], sp["delta"], sp["gamma"], sp["theta"], sp["vega"]),
            StrategyLeg("BUY",  "PUT",  lp["strike"], expiry, 1, lp["premium_mid"], lp["delta"], lp["gamma"], lp["theta"], lp["vega"]),
        ],
        net_debit=round(-net_credit, 2),
        max_profit=round(net_credit, 2),
        max_loss=round(max_loss, 2),
        breakeven=[round(sp["strike"] - net_credit, 2), round(sc["strike"] + net_credit, 2)],
        exit_plan={
            "take_profit": f"收回 50% 权利金（约 ${round(net_credit * 0.5, 2)}）",
            "stop_loss": f"价差扩大到入场权利金的 2 倍",
            "time_stop": "到期前 5 天若未触发止盈，平仓避免 Gamma 暴雷",
        },
    )


def _strategy_cash_secured_put(spot: float, expiry: str, chain: pd.DataFrame, max_pain: float | None) -> Strategy | None:
    """卖出现金担保 Put：偏多 + IV 高，愿意以折扣价接货。"""
    target_strike = round(spot * 0.95)
    if max_pain and max_pain < spot:
        target_strike = round(min(target_strike, max_pain))
    leg = _find_strike(chain, target_strike, "PUT")
    if not leg or leg["premium_mid"] <= 0:
        return None
    net_credit = leg["premium_mid"]
    return Strategy(
        name="Cash-Secured Put (卖出现金担保 Put)",
        rationale="方向偏多 + IV 偏高。卖 OTM Put 收权利金，被指派则以低于现价 5% 的折扣接货。",
        direction="bullish",
        risk_level="medium",
        legs=[
            StrategyLeg("SELL", "PUT", leg["strike"], expiry, 1, leg["premium_mid"],
                        leg["delta"], leg["gamma"], leg["theta"], leg["vega"]),
        ],
        net_debit=round(-net_credit, 2),
        max_profit=round(net_credit, 2),
        max_loss=round(leg["strike"] - net_credit, 2),  # 极端情况股票归零
        breakeven=[round(leg["strike"] - net_credit, 2)],
        exit_plan={
            "take_profit": f"收回 50% 权利金后买回平仓（约 ${round(net_credit * 0.5, 2)}）",
            "stop_loss": f"标的跌破 strike 5% (${round(leg['strike'] * 0.95, 2)}) 或权利金涨到 2x 时平仓",
            "time_stop": "到期前 7 天若 ITM 概率高，考虑 roll 到下月",
        },
    )


# ============================================================
# 主入口
# ============================================================
def decide(symbol: str) -> dict[str, Any]:
    """生成完整决策报告。"""
    futu_sym = symbol if "." in symbol else to_futu_symbol(symbol)
    brief = build_brief(symbol)
    futu_quote = brief.get("quote", {}).get("futu") or {}
    spot = futu_quote.get("price")
    pre_market = futu_quote.get("pre_market") or {}
    after_market = futu_quote.get("after_market") or {}
    
    if not spot:
        return {"_error": "无法取到现价", "brief": brief}

    # 分时级盘中走势（15min K，约 5 个交易日）
    intraday = None
    try:
        from ..datasource.router import get_source
        from ..analysis.technical import intraday_signals
        src, src_sym = get_source(futu_sym)
        df_15m = src.get_history(src_sym, period="1mo", interval="15m")
        if df_15m is not None and not df_15m.empty:
            intraday = intraday_signals(df_15m)
    except Exception as e:
        intraday = {"available": False, "reason": f"分时拉取失败: {e}"}

    direction = evaluate_direction(brief)
    iv_env = evaluate_iv_regime(brief)
    ts_env = evaluate_term_structure(brief)
    max_pain = (brief.get("options") or {}).get("max_pain", {}).get("max_pain")

    # 选到期日（默认 5-35 DTE 区间）
    term = (brief.get("term_structure") or {}).get("term") or []
    expiry_pick = _select_expiry(term, dte_min=5, dte_max=35)
    if not expiry_pick:
        return {"_error": "无可用到期日"}
    expiry = expiry_pick["expiry"]
    dte = expiry_pick.get("dte")

    # 拉该到期日完整链
    chain = _fetch_chain_for_expiry(futu_sym, expiry)

    # 根据方向 + IV 环境组合，挑 1-3 个策略
    strategies: list[Strategy] = []
    label = direction.label
    iv_side = iv_env["side"]

    if label in ("strong_bullish", "bullish"):
        if iv_side in ("buy", "either"):
            s = _strategy_bull_call_spread(spot, expiry, chain)
            if s:
                strategies.append(s)
        if iv_side in ("sell", "either"):
            s = _strategy_cash_secured_put(spot, expiry, chain, max_pain)
            if s:
                strategies.append(s)
    elif label in ("strong_bearish", "bearish"):
        if iv_side in ("buy", "either"):
            s = _strategy_bear_put_spread(spot, expiry, chain)
            if s:
                strategies.append(s)
    else:  # neutral
        if iv_side in ("sell", "either"):
            s = _strategy_iron_condor(spot, expiry, chain)
            if s:
                strategies.append(s)
        # 中性 + 纯低 IV：观望

    # backwardation 警告：若是买方策略，加入 risk note
    if ts_env["warn_buy_side"]:
        for s in strategies:
            if s.net_debit > 0:
                s.exit_plan["⚠️ 重要"] = "Term Structure 倒挂提示短期事件，事件后 IV crush 严重，建议提前在事件前平仓。"

    return {
        "symbol": symbol,
        "futu_symbol": futu_sym,
        "_source": {
            "spot": "futu/get_market_snapshot via build_brief.quote",
            "pre_market/after_market": "futu/get_market_snapshot (pre/post session blocks)",
            "intraday": "computed: stock_quant.analysis.technical.intraday_signals (15m kline)",
            "direction": "computed: evaluate_direction (technical + capital_flow + catalyst + market_env)",
            "iv_env": "computed: build_brief.options.iv_rank + term_structure (IV regime classifier)",
            "term_structure": "futu option chain ATM IV across 5 expiries",
            "max_pain": "computed: stock_quant.flow.calc_max_pain",
            "selected_expiry": "computed: nearest expiry in 5-35 DTE window",
            "strategies": "computed: rule-based generator (Bull/Bear Spread, Iron Condor, CSP, Long Call/Put, etc.)",
        },
        "spot": spot,
        "pre_market": pre_market,
        "after_market": after_market,
        "intraday": intraday,
        "direction": {
            "score": round(direction.score, 2),
            "label": direction.label,
            "breakdown": direction.breakdown,
        },
        "iv_env": iv_env,
        "term_structure": ts_env,
        "max_pain": max_pain,
        "selected_expiry": {"expiry": expiry, "dte": dte},
        "strategies": [
            {
                "name": s.name,
                "rationale": s.rationale,
                "direction": s.direction,
                "direction_cn": _DIRECTION_CN.get(s.direction, s.direction),
                "risk_level": s.risk_level,
                "risk_level_cn": _RISK_CN.get(s.risk_level, s.risk_level),
                "legs": [
                    {
                        **vars(l),
                        "action_cn": _ACTION_CN.get(l.action, l.action),
                        "type_cn": _TYPE_CN.get(l.type, l.type),
                        "summary_cn": _leg_summary_cn(vars(l)),
                    }
                    for l in s.legs
                ],
                "net_debit": s.net_debit,
                "max_profit": s.max_profit,
                "max_loss": s.max_loss,
                "breakeven": s.breakeven,
                "exit_plan": s.exit_plan,
                "exit_plan_cn": {
                    _EXIT_KEY_CN.get(k, k): v for k, v in (s.exit_plan or {}).items()
                },
            }
            for s in strategies
        ],
    }


# ============================================================
# 终端格式化
# ============================================================
def format_decision(d: dict[str, Any]) -> str:
    if "_error" in d:
        return f"❌ {d['_error']}"

    lines: list[str] = []
    lines.append("")
    lines.append("=" * 72)
    lines.append(f"  🧠 {d['symbol']} 期权决策报告  现价 ${d['spot']:.2f}")
    if d.get("pre_market") and d["pre_market"].get("price"):
        lines.append(f"  🌅 盘前: ${d['pre_market']['price']:.2f} ({d['pre_market']['change_rate']:+.2f}%)")
    if d.get("after_market") and d["after_market"].get("price"):
        lines.append(f"  🌃 盘后: ${d['after_market']['price']:.2f} ({d['after_market']['change_rate']:+.2f}%)")
    lines.append("=" * 72)

    if d.get("intraday") and d["intraday"].get("available"):
        try:
            from ..analysis.technical import format_intraday_signals
            lines.append("")
            lines.append(format_intraday_signals(d["intraday"]))
        except Exception:
            pass

    # 方向
    dir_ = d["direction"]
    label_emoji = {
        "strong_bullish": "🟢🟢", "bullish": "🟢", "neutral": "🟡",
        "bearish": "🔴", "strong_bearish": "🔴🔴",
    }.get(dir_["label"], "")
    lines.append(f"\n📊 方向得分: {dir_['score']:+.1f} / 100  → {label_emoji} {dir_['label']}")
    lines.append("   评分明细:")
    for name, val, note in dir_["breakdown"]:
        lines.append(f"     {val:+6.1f}  {name}  {note}")

    # IV 环境
    iv = d["iv_env"]
    lines.append(f"\n🌡️  IV 环境: {iv['regime'].upper()}  → 倾向「{iv['side']}」侧策略")
    if iv.get("iv_rank") is not None:
        lines.append(f"   IV Rank {iv['iv_rank']}  | 当前 ATM IV {iv['current_iv']}%  | HV20 {iv['hv20']}%")
    elif iv.get("iv_hv_ratio"):
        lines.append(f"   IV/HV20 = {iv['iv_hv_ratio']}x  | 当前 IV {iv['current_iv']}%  | HV20 {iv['hv20']}%")

    # Term Structure
    ts = d["term_structure"]
    lines.append(f"\n📈 期限结构: {ts['shape']}  | 前后差 {ts.get('front_back_diff')}")
    if ts["warn_buy_side"]:
        lines.append("   ⚠️  Backwardation → 短期事件预期，慎做买方")

    # Max Pain
    if d.get("max_pain"):
        dev = (d["spot"] - d["max_pain"]) / d["max_pain"] * 100
        lines.append(f"\n🎯 Max Pain ${d['max_pain']:.2f}  | 现价偏离 {dev:+.2f}%")

    # 选定到期
    sel = d["selected_expiry"]
    lines.append(f"\n📅 选定到期日: {sel['expiry']} (DTE {sel['dte']})")

    # 策略推荐
    lines.append("\n" + "─" * 72)
    lines.append(f"  🎯 推荐策略 ({len(d['strategies'])} 个)")
    lines.append("─" * 72)
    if not d["strategies"]:
        lines.append("\n  💤 当前组合（方向+IV）建议观望，无明确策略推荐。")
    for i, s in enumerate(d["strategies"], 1):
        direction_cn = s.get("direction_cn") or _DIRECTION_CN.get(s.get("direction", ""), s.get("direction", ""))
        risk_cn = s.get("risk_level_cn") or _RISK_CN.get(s.get("risk_level", ""), s.get("risk_level", ""))
        lines.append(
            f"\n[策略 {i}] {s['name']}  "
            f"(方向: {direction_cn} / {s.get('direction', '')}  ·  风险: {risk_cn} / {s.get('risk_level', '')})"
        )
        lines.append(f"  理由: {s['rationale']}")
        lines.append(f"  合约腿:")
        for leg in s["legs"]:
            action_cn = leg.get("action_cn") or _ACTION_CN.get(leg.get("action", ""), leg.get("action", ""))
            type_cn = leg.get("type_cn") or _TYPE_CN.get(leg.get("type", ""), leg.get("type", ""))
            lines.append(
                f"    {action_cn}{type_cn}期权 [{leg['action']:4s} {leg['type']:4s}]  "
                f"K=${leg['strike']:>7.2f}  到期 {leg['expiry']}  数量={leg.get('qty', 1)}  "
                f"Mid=${leg['premium_mid']:.2f}  "
                f"Δ={leg['delta']:+.3f} Γ={leg['gamma']:.3f} Θ={leg['theta']:+.3f} ν={leg['vega']:.3f}"
            )
        if s["net_debit"] > 0:
            lines.append(f"  💰 净付权利金: ${s['net_debit']}/张  | 最大盈利 ${s['max_profit']}  | 最大亏损 ${s['max_loss']}")
        else:
            lines.append(f"  💰 净收权利金: ${-s['net_debit']}/张  | 最大盈利 ${s['max_profit']}  | 最大亏损 ${s['max_loss']}")
        if s["breakeven"]:
            be = ", ".join(f"${x}" for x in s["breakeven"])
            lines.append(f"  ⚖️  盈亏平衡点: {be}")
        if s.get("max_profit") and s.get("max_loss"):
            rr = round(s["max_profit"] / s["max_loss"], 2) if s["max_loss"] > 0 else None
            if rr:
                lines.append(f"  📐 风险收益比: 1 : {rr}")
        lines.append(f"  🚪 退出计划:")
        for k, v in s["exit_plan"].items():
            k_cn = _EXIT_KEY_CN.get(k, k)
            lines.append(f"     • {k_cn} ({k}): {v}")

    lines.append("")
    lines.append("=" * 72)
    lines.append("  ⚠️  本决策报告为算法自动生成，不构成投资建议。请结合自身风险偏好审慎决策。")
    lines.append("=" * 72)
    return "\n".join(lines)
