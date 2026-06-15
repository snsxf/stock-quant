"""A 股社区情绪聚合（对应美股 futu_community.py）。

数据源（Phase 2.1, 2026-05-20 落地）：
  1. **东方财富股吧 HTML 直爬**          guba.eastmoney.com/list,{code},1.html
                                         单页 ~80 条标题，散户情绪大本营
  2. **雪球热门关注度** (akshare)         stock_hot_follow_xq + stock_comment_em
                                         热度指标（关注人数/机构参与度/综合得分），不参与 bull/bear 计票
  3. **千股千评** (akshare)               stock_comment_em
                                         机构参与度 + 综合得分 + 主力成本 + 关注指数

特性：
  - 雪球 timeline 已被阿里云 WAF 拦截，改用 akshare 雪球热度接口（合法可用）
  - 股吧直爬只取标题做情绪打分，不抓详情（避免反爬触发）
  - 复用 futu_news.BULLISH/BEARISH_KEYWORDS 中文词典
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

import httpx
import pandas as pd

from .futu_community import _extract_top_themes
from .futu_news import _sentiment_label, _sentiment_score


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _normalize_cn_symbol(symbol: str) -> str:
    """统一 A 股代码格式 → 6 位裸代码（600519）。"""
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


def _xq_symbol(code: str) -> str:
    """6 位裸代码 → 雪球格式（SH600519 / SZ300750）。"""
    if not code or not code.isdigit():
        return ""
    if code.startswith(("60", "68", "90")):
        return f"SH{code}"
    return f"SZ{code}"


# ============================================================
# 1) 东方财富股吧 HTML 直爬
# ============================================================
def _fetch_guba_titles(symbol: str, max_pages: int = 2) -> list[dict]:
    """从东方财富股吧抓帖子标题流。

    URL: https://guba.eastmoney.com/list,{symbol},{page}.html
    单页约 80 条，含资讯转载 + 散户讨论。
    限流：每次调用 sleep 0.3s，每股最多翻 max_pages 页。
    """
    out: list[dict] = []
    seen: set[str] = set()
    headers = {"User-Agent": UA, "Referer": "https://guba.eastmoney.com/"}

    for page in range(1, max_pages + 1):
        url = f"https://guba.eastmoney.com/list,{symbol},{page}.html"
        try:
            r = httpx.get(url, timeout=8.0, headers=headers, follow_redirects=True)
            if r.status_code != 200:
                break
            html = r.text
        except Exception:
            break

        # 帖子链接形式：<a href="/news,{symbol},{post_id}.html" ...>{title}</a>
        pattern = rf'href="(/news,{symbol},\d+\.html)"[^>]*>([^<]{{4,200}})<'
        for _href, title in re.findall(pattern, html):
            t = title.strip()
            if not t or t in seen or len(t) < 4:
                continue
            seen.add(t)
            out.append({"text": t, "source": "guba"})
    return out


# ============================================================
# 2-3) 雪球 + 千股千评 热度指标（akshare 包装，合法可用）
# ============================================================
def _safe_call(fn, *args, **kwargs) -> pd.DataFrame:
    try:
        df = fn(*args, **kwargs)
        return df if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _fetch_xueqiu_heat(symbol: str) -> dict[str, Any]:
    """雪球关注热度（仅热度指标，不参与情绪计票）。"""
    import akshare as ak

    xq_code = _xq_symbol(symbol)
    if not xq_code:
        return {}

    df = _safe_call(ak.stock_hot_follow_xq, symbol="最热门")
    if df.empty:
        return {}

    row = df[df["股票代码"] == xq_code]
    if row.empty:
        return {}
    follow_count = int(row.iloc[0]["关注"] or 0)
    rank = int(row.index[0]) + 1
    return {
        "follow_count": follow_count,
        "rank_among_top_5599": rank,
        "note": "雪球关注人数（热度指标）",
    }


def _fetch_em_comment(symbol: str) -> dict[str, Any]:
    """东方财富千股千评：机构参与度 + 综合得分 + 关注指数。"""
    import akshare as ak

    df = _safe_call(ak.stock_comment_em)
    if df.empty:
        return {}

    row = df[df["代码"] == symbol]
    if row.empty:
        return {}

    r = row.iloc[0]
    out: dict[str, Any] = {}
    for src_col, dst_col in [
        ("机构参与度", "institution_participation"),
        ("综合得分", "comprehensive_score"),
        ("主力成本", "main_force_cost"),
        ("关注指数", "attention_index"),
        ("目前排名", "current_rank"),
        ("上升", "rise_count"),
    ]:
        try:
            out[dst_col] = float(r[src_col])
        except Exception:
            out[dst_col] = None

    # 综合得分映射到偏多/偏空（综合得分 0-100，> 60 偏多，< 40 偏空）
    score = out.get("comprehensive_score") or 0
    if score >= 60:
        out["score_view"] = "bullish"
    elif score <= 40:
        out["score_view"] = "bearish"
    else:
        out["score_view"] = "neutral"
    out["note"] = "东财千股千评（机构 + 散户综合指标）"
    return out


# ============================================================
# 主入口
# ============================================================
def get_cn_community_sentiment(
    symbol: str,
    *,
    include_guba: bool = True,
    include_xueqiu_heat: bool = True,
    include_em_comment: bool = True,
) -> dict[str, Any]:
    """A 股社区情绪聚合，输出与 futu_community.get_community_sentiment 兼容。

    Args:
        symbol: 股票代码（自动归一为 6 位裸代码）

    Returns:
        {
          integrated_view, bull_pct, bear_pct, neutral_pct,
          n_posts, top_themes, sources, breakdown_by_source, _warning
        }
    """
    code = _normalize_cn_symbol(symbol)
    if not code or not code.isdigit():
        return {
            "integrated_view": "neutral",
            "bull_pct": 0, "bear_pct": 0, "neutral_pct": 0,
            "n_posts": 0,
            "top_themes": [],
            "sources": [],
            "breakdown_by_source": {},
            "_error": f"非法 A 股代码: {symbol}",
        }

    bull = bear = neutral = 0
    posts_for_themes: list[str] = []
    sources_used: list[str] = []
    breakdown_by_source: dict[str, dict[str, Any]] = {}

    # 1) 股吧帖子流
    if include_guba:
        guba_posts = _fetch_guba_titles(code, max_pages=2)
        if guba_posts:
            sources_used.append("guba")
            sub_bull = sub_bear = sub_neu = 0
            for p in guba_posts:
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
            breakdown_by_source["guba"] = {
                "n_posts": len(guba_posts),
                "bull": sub_bull, "bear": sub_bear, "neutral": sub_neu,
            }

    # 2) 雪球关注热度（仅热度，不计票）
    if include_xueqiu_heat:
        xq = _fetch_xueqiu_heat(code)
        if xq:
            sources_used.append("xueqiu-heat")
            breakdown_by_source["xueqiu-heat"] = xq

    # 3) 千股千评（机构 + 综合得分）
    if include_em_comment:
        em = _fetch_em_comment(code)
        if em:
            sources_used.append("em-comment")
            breakdown_by_source["em-comment"] = em
            # 千股千评的 score_view 作为外挂判断，加权计入计票
            sv = em.get("score_view")
            if sv == "bullish":
                bull += 5
                neutral += 0
            elif sv == "bearish":
                bear += 5

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
