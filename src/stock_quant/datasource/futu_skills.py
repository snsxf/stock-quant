"""富途 Skills 官方 HTTP API 封装（资讯搜索 / 社区情绪 / 个股解读）。

数据源：`https://ai-news-search.futunn.com`
特点：
  - 官方维护，无需鉴权
  - 与富途牛牛 APP 头条同源
  - 跟逆向爬 futunn.com 网页相比稳定性高几个量级

当前实现的 Skill：
  - news_search          —— 对齐 futu-news-search       —— 资讯/公告/研报检索
  - stock_feed           —— 对齐 futu-comment-sentiment —— 社区/讨论帖检索
  - stock_digest_search  —— 对齐 futu-stock-digest      —— 个股新闻聚合（语义同 news_search）

约定：
  - 所有 Skill 函数失败一律返回 []，由调用方做降级链
  - 入参遵循富途官方 SKILL.md 中的字段命名（keyword / size / news_type / lang / sort_type）
"""
from __future__ import annotations

from typing import Any

import httpx

BASE_URL = "https://ai-news-search.futunn.com"
DEFAULT_TIMEOUT = 8.0

USER_AGENT_NEWS_SEARCH = "futunn-news-search/0.0.2 (Skill)"
USER_AGENT_STOCK_DIGEST = "futu-stock-digest/0.0.2 (Skill)"
USER_AGENT_COMMENT_SENTIMENT = "futunn-comment-sentiment/0.0.2 (Skill)"

# 兼容旧引用（v1 仅 news_search 时的常量名）
USER_AGENT = USER_AGENT_NEWS_SEARCH


def news_search(
    keyword: str,
    size: int = 10,
    lang: str = "zh-CN",
    sort_type: int = 2,
    news_type: int = 1,
) -> list[dict[str, Any]]:
    """资讯搜索（富途官方 Search Skill `futu-news-search`）。

    Args:
        keyword:    检索关键词，可以是 ticker / 公司名 / 中英文皆可
        size:       返回条数，最大 50
        lang:       语言（zh-CN / zh-HK / en）
        sort_type:  1=按热度，2=按时间，3=按关注度
        news_type:  1=News（资讯），2=Notice（公告），3=Research（研报）

    Returns:
        归一后的 list[dict]，每条包含：
          {
            "title":     str,                  # 标题（已剥离 <em> 高亮标签）
            "link":      str,                  # 原文链接
            "source":    "futu-skills",
            "published": int | str,            # Unix 秒级时间戳
            "news_id":   str,                  # 富途内部新闻 ID
            "news_type": int,                  # 1/2/3
          }

        失败时返回 []。
    """
    if not keyword:
        return []

    size = max(1, min(size, 50))

    params = {
        "keyword": keyword,
        "size": size,
        "news_type": news_type,
        "lang": lang,
        "sort_type": sort_type,
    }

    try:
        r = httpx.get(
            f"{BASE_URL}/news_search",
            params=params,
            headers={"User-Agent": USER_AGENT_NEWS_SEARCH},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return []
        payload = r.json()
    except Exception:
        return []

    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []

    raw_list = payload.get("data") or []
    out: list[dict[str, Any]] = []
    for item in raw_list:
        title = item.get("title") or ""
        if not title:
            continue
        title = title.replace("<em>", "").replace("</em>", "")

        out.append({
            "title": title,
            "link": item.get("url"),
            "source": "futu-skills",
            "published": item.get("publish_time"),
            "news_id": item.get("news_id"),
            "news_type": item.get("news_type"),
            "img_url": item.get("img_url"),
        })

    return out


# 与官方 SKILL.md 同义：futu-stock-digest 也是调 /news_search，只是建议把 size 上调到 10-20
def stock_digest_search(
    symbol: str,
    size: int = 12,
    lang: str = "zh-CN",
    news_type: int = 1,
) -> list[dict[str, Any]]:
    """个股新闻聚合（对齐 `futu-stock-digest` Skill）。

    本质上是 news_search 的语义封装：富途官方 Skill 流程为
    `parse → /news_search → event extraction → digest template`，
    本函数只覆盖前两步（拉数据），事件归因/方向判定由上层 `futu_news.py` 完成。
    """
    return news_search(
        keyword=symbol,
        size=max(3, min(size, 20)),
        lang=lang,
        sort_type=2,
        news_type=news_type,
    )


def stock_feed(
    keyword: str,
    size: int = 30,
    lang: str = "zh-CN",
) -> list[dict[str, Any]]:
    """社区/讨论帖检索（对齐 `futu-comment-sentiment` Skill）。

    富途社区帖子 = APP 的「讨论」/「评论」流，与新闻不同：
      - 字段重 desc（HTML 富文本）而非 url
      - 含用户原创观点，更适合做情绪温度计

    Args:
        keyword: ticker / 公司名
        size:    返回条数，clamp 到 1-50
        lang:    语言（zh-CN / zh-HK / en）

    Returns:
        list[dict]：
          {
            "id":           str,
            "title":        str,
            "desc":         str,        # 原始 HTML（含 <nnstock> 标签）
            "desc_text":    str,        # 已剥离 HTML 标签的纯文本
            "publish_time": int | str,  # Unix 秒
            "source":       "futu-skills-feed",
          }

        失败时返回 []。
    """
    if not keyword:
        return []

    size = max(1, min(size, 50))
    params = {"keyword": keyword, "size": size, "lang": lang}

    try:
        r = httpx.get(
            f"{BASE_URL}/stock_feed",
            params=params,
            headers={"User-Agent": USER_AGENT_COMMENT_SENTIMENT},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return []
        payload = r.json()
    except Exception:
        return []

    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []

    import re

    def _strip_html(s: str) -> str:
        if not s:
            return ""
        # 富途 desc 内嵌 <nnstock> / <p> / <span>，统一剥离
        no_tags = re.sub(r"<[^>]+>", " ", s)
        # HTML entity 简单还原
        no_tags = (
            no_tags.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
        )
        # 数字实体如 &#38463;
        no_tags = re.sub(
            r"&#(\d+);", lambda m: chr(int(m.group(1))), no_tags
        )
        return re.sub(r"\s+", " ", no_tags).strip()

    out: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        title = item.get("title") or ""
        desc = item.get("desc") or ""
        if not title and not desc:
            continue
        out.append({
            "id": item.get("id"),
            "title": title,
            "title_text": _strip_html(title),
            "desc": desc,
            "desc_text": _strip_html(desc),
            "publish_time": item.get("publish_time"),
            "source": "futu-skills-feed",
        })

    return out


def is_available() -> bool:
    """健康检查：富途 Skills HTTP 接口是否可用。

    用一次极小开销的请求探活，方便降级链做快速判断。
    """
    try:
        r = httpx.get(
            f"{BASE_URL}/news_search",
            params={"keyword": "AAPL", "size": 1, "news_type": 1, "lang": "en", "sort_type": 2},
            headers={"User-Agent": USER_AGENT_NEWS_SEARCH},
            timeout=3.0,
        )
        return r.status_code == 200 and (r.json() or {}).get("code") == 0
    except Exception:
        return False
