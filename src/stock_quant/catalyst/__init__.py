"""催化剂分析：把 Finnhub 原始数据加工成结构化、可读的字典。"""
from .earnings import upcoming_earnings, recent_earnings_surprise
from .ratings import rating_consensus, recent_rating_changes, price_target_summary
from .econ_calendar import today_events
from .insider import recent_insider_summary

__all__ = [
    "upcoming_earnings",
    "recent_earnings_surprise",
    "rating_consensus",
    "recent_rating_changes",
    "price_target_summary",
    "today_events",
    "recent_insider_summary",
]
