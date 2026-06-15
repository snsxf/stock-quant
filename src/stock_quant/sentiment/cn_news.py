"""A 股资讯聚合 + 事件归因 + 情绪打分（对应美股 futu_news.py）。

数据源（Phase 2.1, 2026-05-20 落地）：
  1. **财联社电报**         akshare.stock_info_global_cls         ★ 主源（交易员信息源 No.1）
  2. **东方财富个股新闻**   akshare.stock_news_em(symbol=...)     ★ 个股粒度
  3. **新浪 7×24 全球财经** akshare.stock_info_global_sina        辅助：宏观快讯
  4. **巨潮个股公告**        akshare.stock_individual_notice_report 法定信披

特性：
  - 限流防护：每个 akshare 调用 timeout + try/except 单源失败不影响整体
  - 双关键词过滤：symbol（600519） + 公司名（贵州茅台）双向匹配，提升召回
  - 复用 futu_news.BULLISH/BEARISH_KEYWORDS 中文词典做情绪打分
  - 复用 futu_news.CATEGORY_KEYWORDS 做事件归因
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pandas as pd

from .futu_news import (
    BULLISH_KEYWORDS,  # noqa: F401  (re-used via _sentiment_score)
    BEARISH_KEYWORDS,  # noqa: F401
    CATEGORY_KEYWORDS,  # noqa: F401
    _categorize,
    _sentiment_label,
    _sentiment_score,
)
from .query_expansion import build_cn_aliases


def _safe_call(fn, *args, **kwargs) -> pd.DataFrame:
    """akshare 接口安全调用：异常时返回空 DataFrame。"""
    try:
        df = fn(*args, **kwargs)
        if df is None:
            return pd.DataFrame()
        return df
    except Exception:
        return pd.DataFrame()


def _normalize_cn_symbol(symbol: str) -> str:
    """统一 A 股代码格式：SH600519 / sh.600519 / 600519.SS / 600519 → 600519"""
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if s.startswith(("SH.", "SZ.", "BJ.")):
        return s.split(".", 1)[1]
    if s.startswith(("SH", "SZ", "BJ")) and s[2:].isdigit():
        return s[2:]
    if s.endswith((".SS", ".SZ", ".BJ")):
        return s[:-3]
    return s


def _match_symbol_or_name(text: str, symbol: str, name: str | None) -> bool:
    """文本是否提到目标 symbol 或公司名。"""
    if not text:
        return False
    if symbol and symbol in text:
        return True
    if name and name in text:
        return True
    return False


def _match_aliases(text: str, aliases: list[str]) -> str | None:
    """文本是否命中任一别名，命中则返回触发别名（用于诊断/降噪）。

    长别名优先匹配，避免短俗称（如 "宁德"）抢先命中包含长名（"宁德时代"）的文本。
    """
    if not text or not aliases:
        return None
    for token in sorted(aliases, key=len, reverse=True):
        if token and token in text:
            return token
    return None


# ============================================================
# 各源拉取
# ============================================================
def _fetch_cls(symbol: str, name: str | None, limit: int = 30,
               aliases: list[str] | None = None) -> list[dict]:
    """财联社电报：拉全市场流，按 symbol/name/别名 过滤。

    aliases 优先；不传则降级回 symbol/name 双关键词匹配。
    """
    import akshare as ak

    df = _safe_call(ak.stock_info_global_cls, symbol="全部")
    if df.empty:
        return []

    out: list[dict] = []
    for _, row in df.iterrows():
        title = str(row.get("标题") or "")
        content = str(row.get("内容") or "")
        text = title + " " + content
        if aliases:
            hit = _match_aliases(text, aliases)
            if not hit:
                continue
        else:
            if not _match_symbol_or_name(text, symbol, name):
                continue
            hit = name or symbol
        published = f"{row.get('发布日期', '')} {row.get('发布时间', '')}".strip()
        out.append({
            "title": title,
            "content": content,
            "published": published,
            "source": "cls",
            "link": None,
            "matched_alias": hit,
        })
        if len(out) >= limit:
            break
    return out


def _fetch_eastmoney_news(symbol: str, limit: int = 20) -> list[dict]:
    """东方财富个股新闻流：按 symbol 直接查询。"""
    import akshare as ak

    df = _safe_call(ak.stock_news_em, symbol=symbol)
    if df.empty:
        return []

    out: list[dict] = []
    for _, row in df.head(limit).iterrows():
        out.append({
            "title": str(row.get("新闻标题") or ""),
            "content": str(row.get("新闻内容") or "")[:400],
            "published": str(row.get("发布时间") or ""),
            "source": f"eastmoney/{row.get('文章来源') or 'em'}",
            "link": str(row.get("新闻链接") or ""),
        })
    return out


def _fetch_sina_global(symbol: str, name: str | None, limit: int = 20,
                       aliases: list[str] | None = None) -> list[dict]:
    """新浪 7×24 全球财经流，按 symbol/name/别名 过滤。"""
    import akshare as ak

    df = _safe_call(ak.stock_info_global_sina)
    if df.empty:
        return []

    out: list[dict] = []
    for _, row in df.iterrows():
        content = str(row.get("内容") or "")
        if aliases:
            hit = _match_aliases(content, aliases)
            if not hit:
                continue
        else:
            if not _match_symbol_or_name(content, symbol, name):
                continue
            hit = name or symbol
        # 新浪没有独立 title 字段，取内容前 60 字作为伪标题
        title = re.sub(r"^【([^】]+)】", r"\1", content[:80])
        out.append({
            "title": title,
            "content": content,
            "published": str(row.get("时间") or ""),
            "source": "sina-7x24",
            "link": None,
            "matched_alias": hit,
        })
        if len(out) >= limit:
            break
    return out


def _fetch_em_global(aliases: list[str], limit: int = 20) -> list[dict]:
    """东方财富全球财经快讯（200 条池子，召回最稳的全市场流）。"""
    import akshare as ak

    df = _safe_call(ak.stock_info_global_em)
    if df.empty or not aliases:
        return []

    out: list[dict] = []
    for _, row in df.iterrows():
        title = str(row.get("标题") or "")
        summary = str(row.get("摘要") or "")
        text = title + " " + summary
        hit = _match_aliases(text, aliases)
        if not hit:
            continue
        out.append({
            "title": title or summary[:80],
            "content": summary,
            "published": str(row.get("发布时间") or ""),
            "source": "em-global",
            "link": str(row.get("链接") or ""),
            "matched_alias": hit,
        })
        if len(out) >= limit:
            break
    return out


def _fetch_ths_global(aliases: list[str], limit: int = 10) -> list[dict]:
    """同花顺全球财经直播（20 条）。"""
    import akshare as ak

    df = _safe_call(ak.stock_info_global_ths)
    if df.empty or not aliases:
        return []

    out: list[dict] = []
    for _, row in df.iterrows():
        title = str(row.get("标题") or "")
        content = str(row.get("内容") or "")
        hit = _match_aliases(title + " " + content, aliases)
        if not hit:
            continue
        out.append({
            "title": title or content[:80],
            "content": content,
            "published": str(row.get("发布时间") or ""),
            "source": "ths-global",
            "link": str(row.get("链接") or ""),
            "matched_alias": hit,
        })
        if len(out) >= limit:
            break
    return out


def _fetch_futu_global_cn(aliases: list[str], limit: int = 10) -> list[dict]:
    """富途快讯（50 条，中文为主）。"""
    import akshare as ak

    df = _safe_call(ak.stock_info_global_futu)
    if df.empty or not aliases:
        return []

    out: list[dict] = []
    for _, row in df.iterrows():
        title = str(row.get("标题") or "")
        content = str(row.get("内容") or "")
        hit = _match_aliases(title + " " + content, aliases)
        if not hit:
            continue
        out.append({
            "title": title or content[:80],
            "content": content,
            "published": str(row.get("发布时间") or ""),
            "source": "futu-cn-global",
            "link": str(row.get("链接") or ""),
            "matched_alias": hit,
        })
        if len(out) >= limit:
            break
    return out


def _fetch_cninfo_notices(symbol: str, limit: int = 10) -> list[dict]:
    """巨潮个股公告（法定信披）。"""
    import akshare as ak

    df = _safe_call(ak.stock_individual_notice_report, security=symbol, symbol="全部")
    if df.empty:
        return []

    out: list[dict] = []
    for _, row in df.head(limit).iterrows():
        out.append({
            "title": str(row.get("公告标题") or ""),
            "content": "",
            "published": str(row.get("公告日期") or ""),
            "source": f"cninfo/{row.get('公告类型') or 'notice'}",
            "link": str(row.get("网址") or ""),
        })
    return out


# ============================================================
# 主入口
# ============================================================
def _published_sort_key(item: dict) -> float:
    """把任意格式的发布时间转为可比较的 timestamp（解析失败用 0）。"""
    p = (item.get("published") or "").strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(p[: len(fmt) + 4], fmt).timestamp()
        except Exception:
            continue
    return 0.0


def get_cn_stock_news_with_sentiment(
    symbol: str,
    name: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    """A 股个股资讯聚合 + 事件归因 + 情绪打分。

    Args:
        symbol: 股票代码（如 "600519" / "300750"，自动归一）
        name:   公司名（如 "贵州茅台"），用于全市场流匹配，强烈建议传
        limit:  最大返回条数

    Returns:
        与美股 get_stock_news_with_sentiment 输出结构兼容：
          {integrated_view, score, key_signals, categories, sources, _stats}
    """
    code = _normalize_cn_symbol(symbol)
    aliases = build_cn_aliases(code, name=name)
    raw: list[dict] = []
    stats = {
        "cls": 0, "eastmoney": 0, "sina": 0, "cninfo": 0,
        "em_global": 0, "ths_global": 0, "futu_global": 0,
    }

    # 1) 东方财富个股新闻（直接 symbol 查询，召回最稳）
    em_items = _fetch_eastmoney_news(code, limit=limit)
    raw.extend(em_items)
    stats["eastmoney"] = len(em_items)

    if aliases:
        # 2) 财联社电报（重要源，但池子只有 20 条；保留以备热点事件命中）
        cls_items = _fetch_cls(code, name, limit=limit, aliases=aliases)
        raw.extend(cls_items)
        stats["cls"] = len(cls_items)

        # 3) 东方财富全球财经快讯（200 条池，主力召回源）
        em_g_items = _fetch_em_global(aliases, limit=limit)
        raw.extend(em_g_items)
        stats["em_global"] = len(em_g_items)

        # 4) 同花顺全球财经直播（20 条池）
        ths_items = _fetch_ths_global(aliases, limit=10)
        raw.extend(ths_items)
        stats["ths_global"] = len(ths_items)

        # 5) 富途快讯中文流（50 条池）
        futu_items = _fetch_futu_global_cn(aliases, limit=10)
        raw.extend(futu_items)
        stats["futu_global"] = len(futu_items)

        # 6) 新浪 7×24（兜底，覆盖国际/港股联动消息）
        sina_items = _fetch_sina_global(code, name, limit=10, aliases=aliases)
        raw.extend(sina_items)
        stats["sina"] = len(sina_items)

    # 7) 巨潮公告（法定信披，0 噪声）
    cninfo_items = _fetch_cninfo_notices(code, limit=5)
    raw.extend(cninfo_items)
    stats["cninfo"] = len(cninfo_items)

    # 去重 + 按发布时间倒排
    seen: set[str] = set()
    items: list[dict] = []
    for it in sorted(raw, key=_published_sort_key, reverse=True):
        title = (it.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
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
            "_stats": {"total": 0, "aliases": aliases, **stats},
        }

    # 归因 + 打分（用标题 + 部分内容做匹配，提高中文召回）
    categories: dict[str, int] = {}
    sentiments: list[float] = []
    enriched: list[dict] = []
    key_signals: list[str] = []

    for it in items:
        title = it.get("title") or ""
        content_head = (it.get("content") or "")[:200]
        text_for_score = f"{title} {content_head}"

        cat = _categorize(text_for_score)
        sc = _sentiment_score(text_for_score)
        if cat:
            categories[cat] = categories.get(cat, 0) + 1
        sentiments.append(sc)
        enriched.append({
            **it,
            "category": cat,
            "sentiment": _sentiment_label(sc),
            "score": sc,
        })
        if abs(sc) >= 0.5 and len(key_signals) < 5:
            key_signals.append(title[:80])

    agg = round(sum(sentiments) / max(len(sentiments), 1), 3)

    return {
        "integrated_view": _sentiment_label(agg),
        "score": agg,
        "key_signals": key_signals,
        "categories": dict(sorted(categories.items(), key=lambda x: -x[1])),
        "sources": enriched,
        "_stats": {"total": len(items), "aliases": aliases, **stats},
    }
