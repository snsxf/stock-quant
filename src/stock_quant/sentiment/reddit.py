"""Reddit 散户情绪源（基于 PRAW）。

监控核心 sub：
  - r/wallstreetbets：散户抱团 / meme 股策源地
  - r/stocks：偏理性的散户讨论
  - r/options：期权策略讨论

本模块按 Phase 1 设计：**仅统计 mention 数与热度**，不做 NLP 情感打分；
后续如需打分可叠加 FinBERT / Claude。

凭证读取：
  - 环境变量 REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT
  - 也可以通过 stock_quant.config.settings 注入

未配置或失败时统一返回空结构（不抛错），由调用方降级。
"""
from __future__ import annotations

import re
import time
from typing import Any

from ..config import settings


_DEFAULT_SUBS = ("wallstreetbets", "stocks", "options")

# 仅匹配大写 ticker（2-5 字母）、$cashtag、以及 $TICKER 形式
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
_BARE_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")

# 去掉常见误报词：英语中频出现的全大写缩写
_NOISE_TICKERS = {
    "I", "A", "U", "USA", "EU", "UK", "CEO", "CFO", "AI", "GPU", "CPU",
    "IPO", "ETF", "FED", "DD", "YOLO", "FOMO", "TLDR", "EOD", "ATH",
    "PE", "PB", "EPS", "ROI", "API", "SaaS", "SEC", "FBI", "CIA", "FAA",
    "USD", "EUR", "JPY", "RMB", "GDP", "CPI", "PMI", "ISM",
    "TLDR", "ELI5", "IMO", "IMHO", "AMA", "ASAP", "EOL", "IIRC",
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "WAS", "GET", "HAS",
    "PUT", "CALL", "OTM", "ITM", "ATM", "DTE", "IV", "HV",
}


def _build_reddit_client():
    """构建 PRAW 客户端，未配置时返回 None。"""
    cid = settings.reddit_client_id
    csec = settings.reddit_client_secret
    ua = settings.reddit_user_agent
    if not cid or not csec:
        return None
    try:
        import praw

        return praw.Reddit(
            client_id=cid,
            client_secret=csec,
            user_agent=ua,
            check_for_async=False,
        )
    except Exception:
        return None


def _extract_tickers(text: str) -> set[str]:
    """从一段文本中提取潜在 ticker 集合（含 $cashtag 与裸大写词，已去噪）。"""
    if not text:
        return set()
    cands = set(_CASHTAG_RE.findall(text))
    cands |= set(_BARE_TICKER_RE.findall(text))
    return {t for t in cands if t not in _NOISE_TICKERS}


def fetch_ticker_mentions(
    symbol: str,
    hours: int = 24,
    subs: tuple[str, ...] = _DEFAULT_SUBS,
    limit_per_sub: int = 200,
) -> dict[str, Any]:
    """统计指定 ticker 在 Reddit 核心 sub 中近 N 小时的提及次数与热度。

    Args:
        symbol:        如 'GOOGL' / 'NVDA'，仅取 ticker 主体（自动剥离 US./HK. 前缀）
        hours:         回溯时窗（小时），默认 24
        subs:          监控的 subreddit 元组
        limit_per_sub: 每个 sub 拉取多少条 new/hot 帖子

    Returns:
        {
          "symbol": "GOOGL",
          "hours": 24,
          "total_mentions": 18,
          "by_sub": {"wallstreetbets": 12, "stocks": 5, "options": 1},
          "top_posts": [{"title", "score", "num_comments", "url", "sub", "created_utc"}, ...],
          "score_sum": 4567,                    # 所有 mention 帖的 upvote 总和
          "comments_sum": 1234,                 # 所有 mention 帖的评论总和
          "sources": ["reddit"],
          "_error": None | "未配置 token" | "...",
        }
    """
    target = symbol.upper().split(".", 1)[-1]

    empty = {
        "symbol": target,
        "hours": hours,
        "total_mentions": 0,
        "by_sub": {},
        "top_posts": [],
        "score_sum": 0,
        "comments_sum": 0,
        "sources": [],
    }

    reddit = _build_reddit_client()
    if reddit is None:
        return {**empty, "_error": "Reddit 未配置 client_id/secret"}

    cutoff = time.time() - hours * 3600
    by_sub: dict[str, int] = {}
    top_posts: list[dict] = []
    score_sum = 0
    comments_sum = 0

    for sub in subs:
        try:
            subreddit = reddit.subreddit(sub)
            # new() 拿最近的，避免热门帖的时间偏差
            for post in subreddit.new(limit=limit_per_sub):
                if post.created_utc < cutoff:
                    continue
                text = f"{post.title or ''}\n{post.selftext or ''}"
                tickers = _extract_tickers(text)
                if target not in tickers:
                    continue

                by_sub[sub] = by_sub.get(sub, 0) + 1
                score_sum += int(post.score or 0)
                comments_sum += int(post.num_comments or 0)
                top_posts.append({
                    "title": post.title,
                    "score": int(post.score or 0),
                    "num_comments": int(post.num_comments or 0),
                    "url": f"https://reddit.com{post.permalink}",
                    "sub": sub,
                    "created_utc": int(post.created_utc),
                })
        except Exception as e:
            return {**empty, "_error": f"{type(e).__name__}: {e}"}

    top_posts.sort(key=lambda x: x["score"], reverse=True)
    total = sum(by_sub.values())
    return {
        "symbol": target,
        "hours": hours,
        "total_mentions": total,
        "by_sub": by_sub,
        "top_posts": top_posts[:10],
        "score_sum": score_sum,
        "comments_sum": comments_sum,
        "sources": ["reddit"] if total > 0 else [],
    }
