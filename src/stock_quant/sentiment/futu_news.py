"""富途资讯 + 事件归因 + 情绪打分（模仿 futunn AI skill 输出格式）。

数据源优先级（逐级降级，保证任意环境都能出结果）：
  1. Futu Skills 官方 HTTP API  —— 与富途牛牛 APP 头条同源，无需鉴权 ★ 主源
  2. Yahoo Finance 新闻         —— 通过 yfinance，美股覆盖优良
  3. Google News RSS（多语）    —— 最后兜底，保证非零召回

历史改动：
  - v1: 逆向爬 futunn.com 的 quote-api / HTML（已于 2026-05 后失效，富途反爬升级）
  - v2: 改用富途 Skills 官方 HTTP API（ai-news-search.futunn.com/news_search）

在此之上做：
  - 事件归因（财报 / 回购 / 合作 / 监管 / 产品 / 高管 / 评级 / AI / 并购 / 分红）
  - 看多 / 看空 / 中性 三态打分（关键词匹配 + 加权，支持中英文）
  - 输出 "AI-ready" 结构化摘要：integrated_view + key_signals + sources
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .news import google_news
from .query_expansion import build_google_queries, build_news_queries


# ============================================================
# 事件归因关键词（中英文双语）
# ============================================================
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "财报": ["earnings", "revenue", "EPS", "季报", "年报", "财报", "营收", "净利"],
    "回购": ["buyback", "repurchase", "回购", "buy back"],
    "合作": ["partnership", "deal", "agreement", "合作", "携手", "签约"],
    "监管": ["SEC", "lawsuit", "investigation", "fine", "诉讼", "监管", "处罚"],
    "产品": ["launch", "release", "unveil", "发布", "推出", "上线"],
    "高管": ["CEO", "CFO", "resign", "appoint", "辞任", "任命", "高管"],
    "评级": ["upgrade", "downgrade", "target price", "评级", "目标价", "上调", "下调"],
    "AI": ["AI", "artificial intelligence", "人工智能", "大模型", "GPT"],
    "并购": ["acquisition", "merger", "M&A", "收购", "并购"],
    "分红": ["dividend", "分红", "派息"],
    "IPO": ["IPO", "prospectus", "debut", "listing", "上市", "招股书"],
}

# ============================================================
# 情绪关键词（带权重）
# ============================================================
BULLISH_KEYWORDS: dict[str, float] = {
    # 英文 - 业绩 / 盈利
    "beat": 1.5, "beats": 1.5, "tops": 1.2, "exceed": 1.2, "exceeds": 1.2,
    "outperform": 1.2, "outperforms": 1.2, "record": 1.0, "record high": 1.5,
    "all-time high": 1.5, "ath": 1.0, "blowout": 1.5,
    # 英文 - 价格动作
    "surge": 1.2, "surges": 1.2, "soar": 1.3, "soars": 1.3, "rally": 1.0,
    "rallies": 1.0, "jump": 1.0, "jumps": 1.0, "rise": 0.6, "rises": 0.6,
    "gain": 0.6, "gains": 0.6, "climb": 0.8, "climbs": 0.8, "rocket": 1.3,
    "skyrocket": 1.3, "spike": 1.0, "breakout": 1.2, "breaks out": 1.2,
    # 英文 - 评级 / 资金
    "upgrade": 1.5, "upgrades": 1.5, "raised": 0.8, "raises": 0.8,
    "buy rating": 1.2, "overweight": 1.0, "bullish": 1.2, "bull": 0.8,
    "buyback": 1.0, "buybacks": 1.0, "repurchase": 1.0, "dividend hike": 1.2,
    "boost": 0.8, "boosts": 0.8, "lifts": 0.8,
    # 英文 - 业务正面
    "strong": 0.8, "robust": 1.0, "solid": 0.6, "growth": 0.6, "expand": 0.6,
    "expands": 0.6, "expansion": 0.6, "partnership": 0.6, "deal": 0.5,
    "wins": 1.0, "win": 0.8, "secures": 0.8, "approves": 0.8, "approval": 0.8,
    "launches": 0.6, "unveils": 0.6, "innovation": 0.6, "milestone": 0.8,
    "lead": 0.5, "leads": 0.5, "leading": 0.5, "dominates": 1.0,
    # 中文 - 业绩
    "超预期": 1.5, "超出预期": 1.5, "超市场预期": 1.5, "业绩亮眼": 1.3,
    "业绩超预期": 1.5, "盈利超预期": 1.5, "营收创新高": 1.3, "净利大增": 1.3,
    # 中文 - 价格
    "大涨": 1.2, "暴涨": 1.3, "飙升": 1.3, "拉升": 1.0, "走高": 0.8,
    "上涨": 0.6, "涨停": 1.5, "涨幅": 0.4, "新高": 1.0, "创新高": 1.3,
    "破位上行": 1.0, "突破": 1.0, "突围": 1.0, "强势": 0.8, "强劲": 0.8,
    "暴拉": 1.3, "猛涨": 1.2, "跃升": 1.2, "跃居": 0.8,
    # 中文 - 评级 / 资金
    "上调": 1.0, "上调评级": 1.5, "上调目标价": 1.5, "买入评级": 1.2,
    "增持": 1.0, "重仓": 1.0, "加仓": 1.0, "建仓": 0.8, "回购": 0.8,
    "派息": 0.6, "分红": 0.6, "看多": 1.5, "看好": 1.0, "利好": 1.2,
    # 中文 - 业务正面
    "合作": 0.5, "签约": 0.6, "中标": 1.0, "获批": 1.0, "批准": 0.8,
    "发布": 0.4, "推出": 0.4, "投产": 0.6, "扩产": 0.8, "增产": 0.8,
    "订单": 0.6, "大单": 1.0, "巨单": 1.2, "斩获": 1.0, "拿下": 0.8,
    "领先": 0.5, "领跑": 0.8, "碾压": 1.0, "霸主": 0.8, "龙头": 0.4,
    "受益": 0.6, "提振": 0.8,
}

BEARISH_KEYWORDS: dict[str, float] = {
    # 英文 - 业绩 / 盈利
    "miss": 1.5, "misses": 1.5, "missed": 1.5, "disappoint": 1.3,
    "disappoints": 1.3, "disappointing": 1.3, "shortfall": 1.2,
    "below estimates": 1.5, "below expectations": 1.5, "weak guidance": 1.5,
    "warns": 1.2, "warning": 1.2, "profit warning": 1.5,
    # 英文 - 价格动作
    "plunge": 1.3, "plunges": 1.3, "tumble": 1.2, "tumbles": 1.2,
    "crash": 1.3, "crashes": 1.3, "slump": 1.2, "slumps": 1.2,
    "drop": 0.8, "drops": 0.8, "fall": 0.6, "falls": 0.6, "fell": 0.6,
    "decline": 0.8, "declines": 0.8, "sink": 1.0, "sinks": 1.0,
    "slide": 0.8, "slides": 0.8, "selloff": 1.2, "sell-off": 1.2,
    "rout": 1.3, "bloodbath": 1.5, "low": 0.4, "all-time low": 1.5,
    # 英文 - 评级 / 资金
    "downgrade": 1.5, "downgrades": 1.5, "cut": 0.8, "cuts": 0.8,
    "lowered": 0.8, "lowers": 0.8, "sell rating": 1.2, "underweight": 1.0,
    "bearish": 1.2, "bear": 0.8, "short": 0.5, "shorts": 0.5,
    "outflow": 0.8, "outflows": 0.8, "dumps": 1.0, "dumping": 1.0,
    # 英文 - 业务负面
    "lawsuit": 1.0, "sue": 1.0, "sued": 1.0, "investigation": 1.2,
    "probe": 1.0, "fine": 1.0, "fined": 1.0, "penalty": 1.0,
    "weak": 0.8, "weakness": 0.8, "concern": 0.6, "concerns": 0.6,
    "worry": 0.6, "worries": 0.6, "fear": 0.8, "fears": 0.8, "risk": 0.4,
    "risks": 0.4, "underperform": 1.2, "underperforms": 1.2,
    "layoff": 1.0, "layoffs": 1.0, "cuts jobs": 1.0, "recall": 1.0,
    "recalls": 1.0, "delay": 0.8, "delays": 0.8, "halt": 1.0, "halts": 1.0,
    "scandal": 1.3, "fraud": 1.5, "bankruptcy": 1.5,
    # 中文 - 业绩
    "不及预期": 1.5, "低于预期": 1.5, "增收不增利": 1.5, "盈利下滑": 1.3,
    "亏损": 1.2, "巨亏": 1.5, "业绩下滑": 1.3, "业绩爆雷": 1.5,
    # 中文 - 价格
    "大跌": 1.2, "暴跌": 1.3, "重挫": 1.3, "崩盘": 1.5, "下挫": 1.0,
    "下跌": 0.8, "走低": 0.8, "回落": 0.6, "跳水": 1.3, "杀跌": 1.2,
    "破位": 1.0, "跌破": 1.0, "跌停": 1.5, "跌幅": 0.4, "新低": 1.0,
    "创新低": 1.3, "急跌": 1.2, "闪崩": 1.5,
    # 中文 - 评级 / 资金
    "下调": 1.0, "下调评级": 1.5, "下调目标价": 1.5, "卖出评级": 1.2,
    "减持": 1.0, "清仓": 1.2, "减仓": 0.8, "抛售": 1.2, "套现": 0.8,
    "看空": 1.5, "看淡": 1.0, "利空": 1.2, "做空": 1.0,
    # 中文 - 业务负面
    "诉讼": 1.0, "起诉": 1.0, "调查": 0.8, "处罚": 1.0, "罚款": 1.0,
    "反垄断": 1.0, "监管": 0.4, "监管压力": 1.0, "下架": 1.0, "停产": 1.2,
    "停售": 1.0, "召回": 1.0, "裁员": 1.0, "辞职": 0.8, "辞任": 0.8,
    "下台": 1.0, "丑闻": 1.3, "造假": 1.5, "退市": 1.5, "破产": 1.5,
    "担忧": 0.8, "忧虑": 0.8, "风险": 0.4, "警告": 0.8, "警示": 0.8,
    "拖累": 0.8, "承压": 0.8, "失利": 0.8, "失守": 1.0, "受挫": 1.0,
    "落败": 1.0, "失宠": 1.0, "受阻": 0.8, "遇冷": 1.0,
}


def _categorize(title: str) -> str | None:
    t = title.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k.lower() in t for k in kws):
            return cat
    return None


def _sentiment_score(title: str) -> float:
    """单条标题情绪打分，范围 [-1, +1]。"""
    t = title.lower()
    bull = sum(w for k, w in BULLISH_KEYWORDS.items() if k.lower() in t)
    bear = sum(w for k, w in BEARISH_KEYWORDS.items() if k.lower() in t)
    if bull == 0 and bear == 0:
        return 0.0
    raw = (bull - bear) / max(bull + bear, 1.0)
    return round(max(-1.0, min(1.0, raw)), 3)


def _sentiment_label(score: float) -> str:
    if score > 0.2:
        return "bullish"
    if score < -0.2:
        return "bearish"
    return "neutral"


# ============================================================
# Futu Skills 官方 HTTP API（取代失效的逆向爬虫）
# ============================================================
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _fetch_futu_skills(
    keyword: str, market: str = "US", limit: int = 10
) -> list[dict]:
    """调用富途 Skills 官方资讯搜索接口。

    与 APP 头条同源，覆盖中英双语新闻。失败返回 []。

    Args:
        keyword: 关键词（ticker 或公司名）
        market:  "US" / "HK" / "CN"，影响默认语言选择
        limit:   返回条数（最大 50）
    """
    from ..datasource.futu_skills import news_search

    lang = "zh-CN" if market.upper() in ("HK", "CN") or re.search(r"[\u4e00-\u9fff]", keyword) else "en"

    items = news_search(
        keyword=keyword,
        size=limit,
        lang=lang,
        sort_type=2,
        news_type=1,
    )
    if not items and lang != "zh-CN":
        items = news_search(
            keyword=keyword,
            size=limit,
            lang="zh-CN",
            sort_type=2,
            news_type=1,
        )

    out: list[dict] = []
    for it in items:
        out.append({
            "title": it.get("title"),
            "link": it.get("link"),
            "source": "futu-skills",
            "published": it.get("published"),
        })
    return out


def _published_sort_key(item: dict) -> int:
    try:
        return int(item.get("published") or item.get("datetime") or 0)
    except Exception:
        return 0


def _fetch_yahoo_news(symbol: str, limit: int = 10) -> list[dict]:
    """Yahoo 新闻（yfinance），美股覆盖较全。失败返回 []。"""
    try:
        from .news import yahoo_news_via_yfinance
        raw = yahoo_news_via_yfinance(symbol, limit=limit)
    except Exception:
        return []
    out: list[dict] = []
    for n in raw:
        title = n.get("title")
        if not title:
            continue
        out.append({
            "title": title,
            "link": n.get("link"),
            "source": f"yahoo/{n.get('provider')}" if n.get("provider") else "yahoo",
            "published": n.get("published"),
        })
    return out


# ============================================================
# 主入口：聚合 + 归因 + 打分
# ============================================================
def get_stock_news_with_sentiment(
    symbol: str,
    market: str = "US",
    name: str | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    """获取个股资讯并做事件归因 + 情绪打分。

    Args:
        symbol: 标的代码（不含市场前缀，如 "TSLA" / "00700"）
        market: "US" / "HK" / "CN"
        name:   公司名（用于增强 Google News 检索召回）
        limit:  最多返回多少条

    Returns:
        {
          integrated_view, score, key_signals, categories, sources, _stats
        }
    """
    raw: list[dict] = []

    # 1) 富途 Skills 官方 HTTP API（主源，与 APP 头条同源）
    #    用 query_expansion 同时覆盖 ticker / 英文名 / 中文名 / 事件关键词。
    futu_query_specs = build_news_queries(symbol, market=market, name=name)
    for i, spec in enumerate(futu_query_specs):
        size = limit if i == 0 else 4
        futu_items = _fetch_futu_skills(spec.query, market=market, limit=size)
        if spec.kind == "event":
            source = "futu-skills-related"
        elif spec.kind in ("alias", "name"):
            source = "futu-skills-alias"
        else:
            source = "futu-skills"
        for it in futu_items:
            raw.append({**it, "source": source, "_query": spec.query, "_query_kind": spec.kind})

    # 2) Yahoo Finance（仅美股，覆盖国际财经媒体）
    if len(raw) < limit and market.upper() == "US":
        yahoo_items = _fetch_yahoo_news(symbol, limit=limit - len(raw))
        seen_titles = {it.get("title") for it in raw}
        for it in yahoo_items:
            if it.get("title") not in seen_titles:
                raw.append(it)
                seen_titles.add(it.get("title"))

    # 4) Google News 兜底（多语言）
    if len(raw) < limit:
        # 英文 / 中文 query
        q_en, q_zh = build_google_queries(symbol, market=market, name=name)
        for it in google_news(q_en, limit=limit, lang="en-US", region="US")[: limit - len(raw)]:
            if "error" in it:
                continue
            raw.append({**it, "source": it.get("source") or "google-en"})

        # 中文 query（港股/A股或带中文名时）
        if q_zh and (market in ("HK", "CN") or re.search(r"[\u4e00-\u9fff]", q_zh)):
            for it in google_news(q_zh, limit=limit, lang="zh-CN", region="HK")[: limit - len(raw)]:
                if "error" in it:
                    continue
                raw.append({**it, "source": it.get("source") or "google-zh"})

    # 去重（按 title）并按发布时间重排；事件扩展召回可能比 ticker 主 query 更新
    seen = set()
    items: list[dict] = []
    for it in sorted(raw, key=_published_sort_key, reverse=True):
        t = (it.get("title") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        items.append(it)
        if len(items) >= limit:
            break

    if not items:
        return {
            "integrated_view": "neutral",
            "score": 0.0,
            "key_signals": [],
            "categories": {},
            "sources": [],
            "_stats": {"total": 0, "futu_skills": 0, "yahoo": 0, "google": 0},
        }

    # 归因 + 打分
    categories: dict[str, int] = {}
    sentiments: list[float] = []
    enriched: list[dict] = []
    key_signals: list[str] = []

    for it in items:
        title = it.get("title") or ""
        cat = _categorize(title)
        sc = _sentiment_score(title)
        if cat:
            categories[cat] = categories.get(cat, 0) + 1
        sentiments.append(sc)
        enriched.append({
            **it,
            "category": cat,
            "sentiment": _sentiment_label(sc),
            "score": sc,
        })
        # 强信号入选 key_signals
        if abs(sc) >= 0.5 and len(key_signals) < 5:
            key_signals.append(title[:80])

    # 整体情绪得分：均值（剔除 0 分噪音权重一半）
    if sentiments:
        weighted = [s if s != 0 else 0.0 for s in sentiments]
        agg = sum(weighted) / max(len(weighted), 1)
    else:
        agg = 0.0
    agg = round(agg, 3)

    return {
        "integrated_view": _sentiment_label(agg),
        "score": agg,
        "key_signals": key_signals,
        "categories": dict(sorted(categories.items(), key=lambda x: -x[1])),
        "sources": enriched,
        "_stats": {
            "total": len(items),
            "futu_skills": sum(1 for it in items if (it.get("source") or "").startswith("futu-skills")),
            "yahoo": sum(1 for it in items if (it.get("source") or "").startswith("yahoo")),
            "google": sum(1 for it in items if "google" in (it.get("source") or "")),
        },
    }
