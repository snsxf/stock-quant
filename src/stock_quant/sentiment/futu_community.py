"""富途社区（牛牛圈）情绪聚合 — mimics 富途 skill 的 "看多 74% / 看空 8% / 中性 18%" 输出。

数据源优先级（v2.4 - 2026-05-20 调整默认开关）：
  1. **Futu Skills 官方 stock_feed API** —— 与 APP 社区流同源，无需鉴权 ★ 主源（默认开）
     对齐官方 `futu-comment-sentiment` Skill 的 `stock_feed` 上游契约
  2. StockTwits 美股流                  —— 默认**关闭**（2025 后强制 OAuth，匿名 403）
  3. Reddit (PRAW)                     —— 默认开，但需 client_id/secret 才出数据
  4. Finnhub social_sentiment          —— 默认**关闭**（免费层 403）
  5. 逆向爬 futunn.com 个股页（兜底）   —— 仅当主源空时启用

输出结构（保持与 v1 兼容）：
  {
    "integrated_view": "bullish",
    "bull_pct": 74, "bear_pct": 8, "neutral_pct": 18,
    "n_posts": 18,
    "top_themes": ["一根大阳转三观", "AI产业链围城", "抄底成功"],
    "sources": ["futu-skills"],
    "breakdown_by_source": {...},
    "_warning": None,           # n_posts < 5 时给出"样本不足"提醒
  }
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

import httpx

from .stocktwits import fetch_symbol_stream
from .futu_news import _sentiment_label, _sentiment_score


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


# ============================================================
# 主题/高频短语挖掘（中文 4-grams + 英文 bigrams）
# ============================================================
_STOPWORDS = {
    "的", "了", "和", "是", "在", "我", "也", "都", "就", "不", "有", "着", "啊",
    "the", "and", "for", "this", "that", "are", "you", "with", "have", "but",
    "stock", "market", "today", "now", "still",
}


def _extract_top_themes(texts: list[str], top_k: int = 5) -> list[str]:
    """从评论 / 标题中挖掘 top-k 主题短语。"""
    counter: Counter = Counter()
    for t in texts:
        if not t:
            continue
        # 中文短语：连续中文 ≥ 4 字
        for m in re.findall(r"[\u4e00-\u9fff]{4,12}", t):
            if m not in _STOPWORDS:
                counter[m] += 1
        # 英文 bigram
        words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", t) if w.lower() not in _STOPWORDS]
        for i in range(len(words) - 1):
            bigram = f"{words[i]} {words[i+1]}"
            counter[bigram] += 1
    # 过滤出现 < 2 次
    return [k for k, v in counter.most_common(top_k * 3) if v >= 2][:top_k]


# ============================================================
# Futunn 社区抓取（best-effort）
# ============================================================
def _post_mentions_symbol(raw_html: str, target_ticker: str) -> bool:
    """判断一条富途 stock_feed 帖子是否真的 @ 了目标 ticker。

    富途帖子里的 cashtag 形如：
      <nnstock stocksymbol="NVDA.US" stockcode="NVDA" stockname="英伟达" ... >
    我们解析所有 stocksymbol，前缀匹配（NVDA ↔ NVDA.US / NVDA.HK 等都算命中）。
    """
    if not raw_html or not target_ticker:
        return False
    target = target_ticker.upper().split(".")[0]
    syms = re.findall(r'stocksymbol="([^"]+)"', raw_html)
    for s in syms:
        head = s.upper().split(".")[0]
        if head == target:
            return True
    # fallback：原文里可能直接出现 $TICKER ...$
    if re.search(rf"\${re.escape(target)}\b", raw_html, flags=re.IGNORECASE):
        return True
    return False


def _fetch_futu_skills_feed(symbol: str, market: str = "US", limit: int = 30) -> list[dict]:
    """从富途官方 stock_feed Skill 拉取社区/讨论帖（v2 主源）。

    注意：富途 stock_feed 的 `title` 字段也是 HTML 富文本（与 desc 内容高度重复），
    必须使用 `_strip_html` 后的 `title_text` / `desc_text`，否则原始 HTML 标签
    （如 <span dir="auto"> / <nnstock>）会污染 bigram 主题挖掘。

    召回精度问题（v2.3 - 2026-05-20 再次优化）：
      - 富途 stock_feed 本质是平台实时流，主流是港 A 股；美股 ticker 单次召回
        50-100 条里通常只有 1-2 条真的 @ 目标。
      - v2.2 阈值 < 3 直接返回空，导致美股社区源彻底空转。
      - v2.3：
          1) 用 COMPANY_ALIASES 扩展 query（NVDA → Nvidia / 英伟达），多 query 合并去重
          2) 单次池子从 30 拉大到 100
          3) 阈值从 3 降到 1（命中 1 条也比完全空好，至少能给 mention 热度信号）
    """
    from ..datasource.futu_skills import stock_feed
    from .query_expansion import COMPANY_ALIASES

    target_ticker = symbol.split(".", 1)[-1] if "." in symbol else symbol
    lang = "zh-CN" if market.upper() in ("HK", "CN") else "en"

    queries: list[tuple[str, str]] = [(symbol, lang)]
    for alias in COMPANY_ALIASES.get(target_ticker.upper(), []):
        is_zh = any("\u4e00" <= ch <= "\u9fff" for ch in alias)
        queries.append((alias, "zh-CN" if is_zh else "en"))

    pool: list[dict] = []
    seen_ids: set[Any] = set()
    pool_size = max(limit * 3, 100)
    for q, q_lang in queries:
        items = stock_feed(keyword=q, size=pool_size, lang=q_lang)
        for it in items:
            iid = it.get("id")
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            pool.append(it)

    strict: list[dict] = []
    for it in pool:
        raw_html = (it.get("title") or "") + " " + (it.get("desc") or "")
        if _post_mentions_symbol(raw_html, target_ticker):
            strict.append(it)

    if not strict:
        return []

    out: list[dict] = []
    for it in strict:
        # title 和 desc 在富途 stock_feed 中通常是同一段 HTML，取更长的那个即可
        desc_text = (it.get("desc_text") or "").strip()
        title_text = (it.get("title_text") or "").strip()
        text = desc_text if len(desc_text) >= len(title_text) else title_text
        if not text:
            continue
        out.append({"text": text, "source": "futu-skills"})
    return out


def _fetch_futunn_community(symbol: str, market: str = "US", limit: int = 30) -> list[dict]:
    """从 futunn.com 个股页面抓社区帖子标题（v1 兜底，反爬升级后召回 ≈ 0）。"""
    code = symbol.split(".", 1)[-1] if "." in symbol else symbol
    url = f"https://www.futunn.com/stock/{code}-{market.upper()}"
    try:
        r = httpx.get(url, timeout=8.0, follow_redirects=True, headers={"User-Agent": UA})
        if r.status_code != 200:
            return []
        html = r.text
    except Exception:
        return []

    posts: list[dict] = []
    seen: set[str] = set()
    # 抓社区帖子标题模式（class 包含 community-post / nnq- 等富途惯用前缀）
    for m in re.finditer(
        r'class="[^"]*(?:post|feed|community)[^"]*"[^>]*>\s*<[^>]*>([^<]{6,200})<',
        html, flags=re.IGNORECASE,
    ):
        text = m.group(1).strip()
        if text in seen or len(text) < 6:
            continue
        seen.add(text)
        posts.append({"text": text, "source": "futunn"})
        if len(posts) >= limit:
            break
    return posts


# ============================================================
# 主入口
# ============================================================
def _fetch_reddit_breakdown(symbol: str, hours: int = 24) -> dict[str, Any]:
    """Reddit 散户提及聚合。仅统计 mention/热度，**不参与 bull/bear 计票**。

    返回 None 表示未配置或失败；上层据此跳过本源。
    """
    try:
        from .reddit import fetch_ticker_mentions

        data = fetch_ticker_mentions(symbol, hours=hours)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}
    if data.get("_error") or data.get("total_mentions", 0) == 0:
        return data
    return data


def _fetch_finnhub_social(symbol: str, days: int = 7) -> dict[str, Any]:
    """Finnhub 社交情绪（Reddit/Twitter 综合）。免费层可能 403。"""
    try:
        from ..datasource.finnhub import FinnhubSource

        src = FinnhubSource()
        data = src.social_sentiment(symbol, days=days)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}
    if not isinstance(data, dict) or not data:
        return {"_error": "无 Finnhub social 数据（可能权限受限）"}

    # 聚合 reddit + twitter 两个数组的 positive/negative mention 总和
    out: dict[str, Any] = {"reddit": {}, "twitter": {}}
    for key in ("reddit", "twitter"):
        items = data.get(key) or []
        if not items:
            continue
        pos = sum(int(x.get("positiveMention", 0) or 0) for x in items)
        neg = sum(int(x.get("negativeMention", 0) or 0) for x in items)
        total = sum(int(x.get("mention", 0) or 0) for x in items)
        out[key] = {
            "positive_mentions": pos,
            "negative_mentions": neg,
            "total_mentions": total,
            "n_buckets": len(items),
        }
    return out


def get_community_sentiment(
    symbol: str,
    market: str = "US",
    *,
    include_stocktwits: bool = False,
    include_reddit: bool = True,
    include_finnhub_social: bool = False,
) -> dict[str, Any]:
    """聚合多源社区情绪，返回看多/看空/中性占比 + 热门主题 + 分源 breakdown。

    各源默认开关（2026-05-20 v2.4 调整）：
      - futu-skills    : 始终启用（主源，无需鉴权）
      - stocktwits     : **默认关闭** —— 2025 起匿名 API 全 403，需 OAuth token；
                          国内访问 signup 页常被 Cloudflare 拦截，未配 token 前
                          关掉避免每次调用都污染日志。需要时显式传 True。
      - reddit         : 默认启用，但需配置 reddit_client_id/secret 才会真正出数据
      - finnhub-social : **默认关闭** —— 免费层 social_sentiment 端点 403。
      - futunn-html    : 仅当 futu-skills 完全空时启用（best-effort 兜底）
    """
    bull = bear = neutral = 0
    posts_for_themes: list[str] = []
    sources_used: list[str] = []
    breakdown_by_source: dict[str, dict[str, Any]] = {}

    # 1) 富途 Skills 官方 stock_feed（主源，与 APP 社区同源）
    skill_posts = _fetch_futu_skills_feed(symbol, market=market, limit=30)
    if skill_posts:
        sources_used.append("futu-skills")
        sub_bull = sub_bear = sub_neu = 0
        for p in skill_posts:
            text = p["text"]
            sc = _sentiment_score(text)
            label = _sentiment_label(sc)
            if label == "bullish":
                bull += 1
                sub_bull += 1
            elif label == "bearish":
                bear += 1
                sub_bear += 1
            else:
                neutral += 1
                sub_neu += 1
            posts_for_themes.append(text)
        breakdown_by_source["futu-skills"] = {
            "n_posts": len(skill_posts),
            "bull": sub_bull, "bear": sub_bear, "neutral": sub_neu,
        }

    # 2) StockTwits（仅美股有意义）
    if include_stocktwits and market == "US":
        try:
            st = fetch_symbol_stream(symbol, limit=30)
            if not st.get("error"):
                st_bull = st.get("bull_count", 0)
                st_bear = st.get("bear_count", 0)
                bull += st_bull
                bear += st_bear
                # StockTwits 未打标的视为中性
                untagged = max(st.get("total", 0) - st_bull - st_bear, 0)
                neutral += untagged
                posts_for_themes.extend(
                    [m.get("body") or "" for m in st.get("messages") or []]
                )
                if st.get("total", 0) > 0:
                    sources_used.append("stocktwits")
                    breakdown_by_source["stocktwits"] = {
                        "n_posts": st.get("total", 0),
                        "bull": st_bull, "bear": st_bear, "neutral": untagged,
                    }
        except Exception:
            pass

    # 3) Reddit 散户提及（仅美股；不参与 bull/bear 计票，作为热度指标单独输出）
    if include_reddit and market == "US":
        rd = _fetch_reddit_breakdown(symbol)
        if rd and not rd.get("_error") and rd.get("total_mentions", 0) > 0:
            sources_used.append("reddit")
            breakdown_by_source["reddit"] = {
                "total_mentions": rd.get("total_mentions"),
                "by_sub": rd.get("by_sub"),
                "score_sum": rd.get("score_sum"),
                "comments_sum": rd.get("comments_sum"),
                "top_posts": rd.get("top_posts", [])[:5],
                "note": "仅热度指标，不参与 bull/bear 计票",
            }

    # 4) Finnhub social_sentiment（Reddit + Twitter 综合；免费层可能受限）
    if include_finnhub_social and market == "US":
        fh = _fetch_finnhub_social(symbol)
        if fh and not fh.get("_error"):
            has_data = any(
                isinstance(v, dict) and v.get("total_mentions", 0) > 0
                for v in fh.values()
            )
            if has_data:
                sources_used.append("finnhub-social")
                breakdown_by_source["finnhub-social"] = {
                    "reddit": fh.get("reddit") or {},
                    "twitter": fh.get("twitter") or {},
                    "note": "Finnhub 聚合 7 日数据，作辅助参考",
                }

    # 5) Futunn 社区（HTML 抓取，best-effort 兜底；反爬升级后通常 0）
    if not skill_posts:
        futu_posts = _fetch_futunn_community(symbol, market=market, limit=30)
        if futu_posts:
            sources_used.append("futunn-html")
            sub_bull = sub_bear = sub_neu = 0
            for p in futu_posts:
                text = p["text"]
                sc = _sentiment_score(text)
                label = _sentiment_label(sc)
                if label == "bullish":
                    bull += 1
                    sub_bull += 1
                elif label == "bearish":
                    bear += 1
                    sub_bear += 1
                else:
                    neutral += 1
                    sub_neu += 1
                posts_for_themes.append(text)
            breakdown_by_source["futunn-html"] = {
                "n_posts": len(futu_posts),
                "bull": sub_bull, "bear": sub_bear, "neutral": sub_neu,
            }

    n_posts = bull + bear + neutral
    if n_posts == 0:
        return {
            "integrated_view": "neutral",
            "bull_pct": 0, "bear_pct": 0, "neutral_pct": 0,
            "n_posts": 0,
            "top_themes": [],
            "sources": sources_used,
            "breakdown_by_source": breakdown_by_source,
            "_error": "无社区数据" if not breakdown_by_source else None,
        }

    bull_pct = round(bull / n_posts * 100, 1)
    bear_pct = round(bear / n_posts * 100, 1)
    neu_pct = round(100 - bull_pct - bear_pct, 1)

    # 样本不足保护：< 5 条样本不做强多/强空判定，避免 1 条帖子打成 100% bear
    if n_posts < 5:
        view = "insufficient_sample"
    elif bull_pct >= 60:
        view = "bullish"
    elif bear_pct >= 60:
        view = "bearish"
    elif bull_pct - bear_pct >= 20:
        view = "lean_bullish"
    elif bear_pct - bull_pct >= 20:
        view = "lean_bearish"
    else:
        view = "neutral"

    return {
        "integrated_view": view,
        "bull_pct": bull_pct,
        "bear_pct": bear_pct,
        "neutral_pct": neu_pct,
        "n_posts": n_posts,
        "top_themes": _extract_top_themes(posts_for_themes, top_k=5),
        "sources": sources_used,
        "breakdown_by_source": breakdown_by_source,
        "_warning": "样本量 < 5，仅供参考" if n_posts < 5 else None,
    }
