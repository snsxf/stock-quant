"""新闻检索 query 扩展：股票代码 + 公司中英文名 + 事件关键词。

目标：
  - 美股：MSFT / Microsoft / 微软 都能指向同一家公司
  - 港股：HK.00700 / 0700.HK / 00700 / 腾讯 / Tencent 都能指向同一家公司
  - A股：SH.600519 / 600519 / 贵州茅台 / Kweichow Moutai 都能指向同一家公司

注意：
  - 这里负责构造 query，不负责判断新闻是否相关；相关性由上层去重/排序/过滤处理。
  - 事件 query 只放强绑定的外溢催化剂，避免把无关宏观新闻拉进来。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuerySpec:
    query: str
    source: str
    kind: str  # symbol / alias / event / name


def _dedup(items: list[QuerySpec]) -> list[QuerySpec]:
    seen: set[str] = set()
    out: list[QuerySpec] = []
    for it in items:
        q = " ".join((it.query or "").split()).strip()
        key = q.lower()
        if not q or key in seen:
            continue
        seen.add(key)
        out.append(QuerySpec(query=q, source=it.source, kind=it.kind))
    return out


def _code_key(symbol: str) -> str:
    """把不同市场格式归一到别名表 key。"""
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if s.startswith(("US.", "HK.", "SH.", "SZ.")):
        market, code = s.split(".", 1)
        if market == "HK":
            return code.zfill(5)
        return code
    if s.endswith(".HK"):
        return s[:-3].zfill(5)
    if s.endswith((".SS", ".SZ")):
        return s[:-3]
    # 港股常见裸代码：700 / 0700 / 00700 都归一成 00700
    if s.isdigit() and len(s) <= 5:
        return s.zfill(5)
    return s


def _compact_hk_code(code: str) -> str | None:
    if not code.isdigit() or len(code) != 5:
        return None
    return code.lstrip("0").zfill(4)


# 常用核心票别名表。可持续扩展；没有命中的标的仍会使用 symbol + name。
COMPANY_ALIASES: dict[str, list[str]] = {
    # US mega-cap / AI
    "MSFT": ["Microsoft", "Microsoft Corporation", "微软"],
    "GOOGL": ["Google", "Alphabet", "Alphabet Inc", "谷歌", "谷歌母公司"],
    "GOOG": ["Google", "Alphabet", "Alphabet Inc", "谷歌", "谷歌母公司"],
    "META": ["Meta", "Meta Platforms", "Facebook", "脸书", "Meta平台"],
    "NVDA": ["Nvidia", "NVIDIA", "NVIDIA Corporation", "英伟达"],
    "TSLA": ["Tesla", "Tesla Inc", "特斯拉", "Elon Musk", "马斯克"],
    "AAPL": ["Apple", "Apple Inc", "苹果"],
    "AMZN": ["Amazon", "Amazon.com", "亚马逊"],
    "AMD": ["Advanced Micro Devices", "AMD", "超威半导体"],
    "AVGO": ["Broadcom", "Broadcom Inc", "博通"],
    "TSM": ["TSMC", "Taiwan Semiconductor", "台积电"],
    "NFLX": ["Netflix", "奈飞"],
    "PLTR": ["Palantir", "Palantir Technologies"],
    "ARM": ["Arm Holdings", "ARM Holdings", "Arm"],
    "RKLB": ["Rocket Lab", "Rocket Lab USA"],
    # HK
    "00700": ["0700", "Tencent", "Tencent Holdings", "腾讯", "腾讯控股"],
    "09988": ["9988", "Alibaba", "Alibaba Group", "阿里巴巴", "阿里巴巴集团"],
    "03690": ["3690", "Meituan", "美团"],
    "01810": ["1810", "Xiaomi", "小米", "小米集团"],
    "01299": ["1299", "AIA", "友邦保险"],
    # CN
    "600519": ["Kweichow Moutai", "贵州茅台", "茅台"],
    "000858": ["Wuliangye", "五粮液"],
    "300750": ["CATL", "Contemporary Amperex", "宁德时代"],
    "002594": ["BYD", "比亚迪"],
}


# A 股交易俗称 / 外号 / 雪球简称 / 财经媒体常用缩写。
# 这些词在财联社电报、新浪 7×24 里出现频率比正式公司名高得多。
# Key 为 6 位裸代码；Value 为别名列表（不含正式公司名，正式名走 COMPANY_ALIASES）。
COMPANY_ALIASES_CN: dict[str, list[str]] = {
    # 白酒
    "600519": ["茅台股份", "贵茅"],
    "000858": ["五粮液", "五粮"],
    "000568": ["泸州老窖", "老窖"],
    "600809": ["山西汾酒", "汾酒"],
    # 新能源 / 电池 / 电车
    "300750": ["宁王", "宁德", "CATL", "动力电池龙头"],
    "002594": ["迪王", "比亚迪股份", "BYD"],
    "300014": ["亿纬", "亿纬锂能"],
    "002074": ["国轩", "国轩高科"],
    # 半导体 / 芯片
    "688981": ["中芯", "中芯国际", "SMIC"],
    "002371": ["北方华创", "北华"],
    "688012": ["中微", "中微公司"],
    "603501": ["韦尔股份", "韦尔"],
    "002049": ["紫光", "紫光国微"],
    # AI / 算力 / 软件
    "300308": ["中际", "中际旭创"],
    "002115": ["三维通信"],
    "300033": ["同花顺"],
    "300059": ["东方财富", "东财"],
    # 新兴 / 卫星 / 航天
    "600118": ["中国卫星", "航天五院"],
    "688041": ["海光", "海光信息"],
    # 大金融
    "601318": ["中国平安", "平安集团"],
    "600036": ["招行", "招商银行"],
    # 医药
    "600276": ["恒瑞", "恒瑞医药"],
    "300760": ["迈瑞", "迈瑞医疗"],
    # 工业互联网 / 通信
    "601138": ["工业富联", "富联"],
    "002241": ["歌尔", "歌尔股份"],
}


def build_cn_aliases(symbol: str, name: str | None = None) -> list[str]:
    """A 股专用：返回 symbol + 公司名 + 通用别名 + 交易俗称的合并去重列表。

    用于在 akshare 全市场流（财联社/新浪）里做"标的相关"过滤。
    召回率提升的关键在于俗称——例如 "茅台" 比 "贵州茅台" 在电报里出现频率高得多。

    Args:
        symbol: 6 位裸代码 / SH600519 / 600519.SS 等任意 A 股格式
        name:   公司中文名（可选，但强烈建议传，例如 "贵州茅台"）

    Returns:
        list[str]: 用于子串匹配的关键词列表，已去重，长度通常 3-8 个。
    """
    key = _code_key(symbol)
    out: list[str] = []
    if symbol:
        out.append(symbol)
    if key and key != symbol:
        out.append(key)
    if name:
        out.append(name)
    out.extend(COMPANY_ALIASES.get(key, []))
    out.extend(COMPANY_ALIASES_CN.get(key, []))

    seen: set[str] = set()
    deduped: list[str] = []
    for t in out:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        deduped.append(t)
    return deduped


RELATED_EVENT_QUERIES: dict[str, list[str]] = {
    # TSLA 与 SpaceX / Elon Musk 强绑定，直接搜 TSLA 容易漏掉 SpaceX IPO 这类外溢催化剂。
    "TSLA": ["TSLA SpaceX IPO", "Tesla SpaceX IPO", "Elon Musk SpaceX IPO", "SpaceX IPO"],
    # NVDA 的新闻常被“中国芯片 / AI 数据中心 / Blackwell”这类主题承载。
    "NVDA": ["Nvidia China chip", "Nvidia Blackwell", "Nvidia AI data center"],
    # MSFT 常见外溢催化剂：OpenAI / Azure AI。
    "MSFT": ["Microsoft OpenAI", "Microsoft Azure AI"],
    # GOOGL 常见外溢催化剂：Gemini / Google Cloud / Waymo。
    "GOOGL": ["Google Gemini", "Alphabet Waymo", "Google Cloud AI"],
    "GOOG": ["Google Gemini", "Alphabet Waymo", "Google Cloud AI"],
    # META 常见外溢催化剂：AI capex / Llama / Reality Labs。
    "META": ["Meta AI capex", "Meta Llama", "Meta Reality Labs"],
}


def build_symbol_queries(symbol: str, market: str = "US", name: str | None = None) -> list[QuerySpec]:
    """构造股票代码/公司名 query，覆盖中英文名和市场代码变体。"""
    key = _code_key(symbol)
    specs: list[QuerySpec] = []
    if symbol:
        specs.append(QuerySpec(symbol, "query-expansion", "symbol"))
    if key and key != symbol:
        specs.append(QuerySpec(key, "query-expansion", "symbol"))

    compact_hk = _compact_hk_code(key)
    if market.upper() == "HK" or compact_hk:
        if compact_hk:
            specs.append(QuerySpec(compact_hk, "query-expansion", "symbol"))
            specs.append(QuerySpec(f"{compact_hk}.HK", "query-expansion", "symbol"))
        if key.isdigit():
            specs.append(QuerySpec(f"HK.{key}", "query-expansion", "symbol"))

    if name:
        specs.append(QuerySpec(name, "query-expansion", "name"))

    for alias in COMPANY_ALIASES.get(key, []):
        specs.append(QuerySpec(alias, "query-expansion", "alias"))

    return _dedup(specs)


def build_event_queries(symbol: str, name: str | None = None) -> list[QuerySpec]:
    """构造强绑定事件 query。"""
    key = _code_key(symbol)
    specs = [QuerySpec(q, "query-expansion", "event") for q in RELATED_EVENT_QUERIES.get(key, [])]
    # 如果调用方传入了更准确的公司名，把公司名和事件组合一次，提升中英文召回。
    if name and key == "TSLA":
        specs.append(QuerySpec(f"{name} SpaceX IPO", "query-expansion", "event"))
    return _dedup(specs)


def build_news_queries(
    symbol: str,
    market: str = "US",
    name: str | None = None,
    *,
    include_events: bool = True,
    max_alias_queries: int = 5,
) -> list[QuerySpec]:
    """新闻搜索用 query 列表：主代码优先，其次名称/别名，最后事件 query。"""
    base = build_symbol_queries(symbol, market=market, name=name)
    event = build_event_queries(symbol, name=name) if include_events else []

    # 保持 query 数量可控：首个 symbol 必保留，别名最多 max_alias_queries。
    first_symbol = [q for q in base if q.kind == "symbol"][:2]
    names = [q for q in base if q.kind in ("name", "alias")][:max_alias_queries]
    return _dedup(first_symbol + names + event)


def build_google_queries(symbol: str, market: str = "US", name: str | None = None) -> tuple[str, str | None]:
    """Google News 兜底 query：英文/中文各一条。"""
    specs = build_symbol_queries(symbol, market=market, name=name)
    ascii_parts = [q.query for q in specs if q.query.isascii()][:3]
    cn_parts = [q.query for q in specs if not q.query.isascii()][:3]
    q_en = " ".join(ascii_parts) if ascii_parts else (name or symbol)
    q_zh = " ".join(cn_parts) if cn_parts else None
    return q_en, q_zh
