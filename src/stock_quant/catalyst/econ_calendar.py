"""今日 / 明日的全球宏观经济事件。

数据源策略（2026-05 起调整）：
  1. **akshare.tool_trade_date_hist_sina + akshare.macro_** ← 主源
     （akshare 已是项目依赖，覆盖中美主要经济数据发布）
  2. **finnhub.economic_calendar**                          ← fallback（免费层常 403）

教训：
  - Finnhub 免费层 `/calendar/economic` 通常返回 403。
  - akshare 的 macro_xxx 接口列结构统一为 `商品 / 日期 / 今值 / 预测值 / 前值`，
    其中 `日期` 是 datetime.date 对象（不是字符串），`商品` 才是事件名。
    历史 bug：曾把 `商品` 当成日期字段，导致 `time="美国失业率"` 漏馅。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..datasource.finnhub import FinnhubSource


# akshare 宏观接口名 → 事件描述（中美高影响力事件）
# 仅保留实测存在且列结构为 商品/日期/今值/预测值/前值 的接口
_AKSHARE_MACRO_EVENTS: list[tuple[str, str, str]] = [
    # (akshare 接口名, country, event_name)
    ("macro_usa_cpi_monthly", "US", "美国 CPI 月率"),
    ("macro_usa_cpi_yoy", "US", "美国 CPI 同比"),
    ("macro_usa_core_cpi_monthly", "US", "美国核心 CPI 月率"),
    ("macro_usa_core_pce_price", "US", "美国核心 PCE 物价"),
    ("macro_usa_ppi", "US", "美国 PPI"),
    ("macro_usa_unemployment_rate", "US", "美国失业率"),
    ("macro_usa_non_farm", "US", "美国非农就业"),
    ("macro_usa_initial_jobless", "US", "美国初请失业金"),
    ("macro_usa_adp_employment", "US", "美国 ADP 就业"),
    ("macro_usa_gdp_monthly", "US", "美国 GDP"),
    ("macro_usa_retail_sales", "US", "美国零售销售"),
    ("macro_usa_ism_pmi", "US", "美国 ISM 制造业 PMI"),
    ("macro_usa_ism_non_pmi", "US", "美国 ISM 非制造业 PMI"),
    ("macro_china_cpi_monthly", "CN", "中国 CPI 月率"),
    ("macro_china_cpi_yearly", "CN", "中国 CPI 年率"),
    ("macro_china_ppi_yearly", "CN", "中国 PPI 年率"),
    ("macro_china_gdp_yearly", "CN", "中国 GDP 年率"),
    ("macro_china_cx_pmi_yearly", "CN", "中国财新制造业 PMI"),
    ("macro_china_cx_services_pmi_yearly", "CN", "中国财新服务业 PMI"),
]


def _to_date(val: Any) -> date | None:
    """把 akshare `日期` 列的多种可能类型统一成 date。"""
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str) and len(val) >= 10:
        try:
            return datetime.strptime(val[:10], "%Y-%m-%d").date()
        except ValueError:
            try:
                return datetime.strptime(val[:10], "%Y/%m/%d").date()
            except ValueError:
                return None
    # pandas Timestamp 兼容
    try:
        return val.to_pydatetime().date()  # type: ignore[attr-defined]
    except Exception:
        return None


def _today_events_akshare(days_ahead: int = 1) -> list[dict[str, Any]]:
    """从 akshare 拉取近期发布的宏观经济事件。

    akshare 的 macro_xxx 接口列结构通常是：
        商品 / 日期 / 今值 / 预测值 / 前值
    其中 `日期` 列是 datetime.date 对象（不是字符串）。
    最后一行通常是最近一次发布或下一期预告（今值=NaN）。
    """
    try:
        import akshare as ak
    except ImportError:
        return []

    today = date.today()
    cutoff_lo = today - timedelta(days=2)
    cutoff_hi = today + timedelta(days=days_ahead)

    out: list[dict[str, Any]] = []
    for fn_name, country, event_name in _AKSHARE_MACRO_EVENTS:
        fn = getattr(ak, fn_name, None)
        if fn is None:
            continue
        try:
            df = fn()
        except Exception:
            continue
        if df is None or df.empty:
            continue

        try:
            tail = df.tail(1).to_dict(orient="records")[0]
        except Exception:
            continue

        # 日期字段：标准列名是 `日期`，兜底兼容 `公布日期 / 发布时间 / 时间`
        raw_date = (
            tail.get("日期") or tail.get("公布日期")
            or tail.get("发布时间") or tail.get("时间")
        )
        evt_date = _to_date(raw_date)
        if evt_date is None:
            continue
        if evt_date < cutoff_lo or evt_date > cutoff_hi:
            continue

        # 数值字段（`商品` 永远是事件中文名，不会作为数值）
        actual = (
            tail.get("今值") if tail.get("今值") is not None else
            tail.get("公布") if tail.get("公布") is not None else
            tail.get("实际值") if tail.get("实际值") is not None else
            tail.get("最新值")
        )
        estimate = tail.get("预测值") if tail.get("预测值") is not None else tail.get("预期")
        prev = tail.get("前值") if tail.get("前值") is not None else tail.get("上次")

        # NaN -> None 处理
        def _nan_to_none(v: Any) -> Any:
            try:
                import math
                if isinstance(v, float) and math.isnan(v):
                    return None
            except Exception:
                pass
            return v

        out.append({
            "time": evt_date.isoformat(),
            "country": country,
            "event": event_name,
            "impact": "high",
            "actual": _nan_to_none(actual),
            "estimate": _nan_to_none(estimate),
            "prev": _nan_to_none(prev),
            "unit": None,
            "_source": f"akshare/{fn_name}",
        })

    # 按日期升序
    out.sort(key=lambda e: e.get("time") or "")
    return out


def today_events(days_ahead: int = 1, only_high_impact: bool = True) -> list[dict[str, Any]]:
    """今日 / 明日的宏观经济事件（akshare 主源，finnhub fallback）。"""
    ak_events = _today_events_akshare(days_ahead=days_ahead)
    if ak_events:
        return ak_events

    fh = FinnhubSource()
    raw = fh.economic_calendar(days_ahead=days_ahead)
    out = []
    for e in raw:
        impact = (e.get("impact") or "").lower()
        if only_high_impact and impact not in ("high", "medium"):
            continue
        out.append({
            "time": e.get("time"),
            "country": e.get("country"),
            "event": e.get("event"),
            "impact": impact,
            "actual": e.get("actual"),
            "estimate": e.get("estimate"),
            "prev": e.get("prev"),
            "unit": e.get("unit"),
            "_source": "finnhub/economic_calendar (fallback)",
            "_warn": "Finnhub 免费层 economic_calendar 端点常返回 403，建议 akshare 优先",
        })
    return out
