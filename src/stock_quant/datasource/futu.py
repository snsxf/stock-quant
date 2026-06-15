import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from ..config import settings
from .base import DataSource

logger = logging.getLogger(__name__)


# ============================================================
# 时效性判定（防止把"周末/节假日 + 隔夜"的过期数据误判为实时）
# ============================================================
_US_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}


def _next_us_open_hint(now_et: datetime) -> str:
    """返回下次美股常规开盘的可读提示（仅供 LLM/用户参考，不做精确撮合）。"""
    from datetime import timedelta

    cur = now_et
    for _ in range(10):
        cur = cur + timedelta(days=1)
        d = cur.date().isoformat()
        if cur.weekday() < 5 and d not in _US_HOLIDAYS_2026:
            return f"{d} 09:30 ET"
    return "unknown"


def _compute_quote_freshness(update_time: str | None, sec_status: str | None) -> dict:
    """基于 wall-clock + sec_status 综合判断行情快照的"时效等级"。

    返回字段：
      - now_et:            当前美东时间（ISO）
      - snapshot_time:     富途 update_time 原值
      - data_age_minutes:  快照与现在的差值（分钟）
      - is_weekend:        当前是否为美股周末
      - is_us_holiday:     当前是否为美股法定假期
      - market_phase:      live | pre_market | after_market | overnight | closed_weekend | closed_holiday | closed_overnight
      - freshness_level:   live | recent | stale | expired
      - warn:              人类可读的告警（None 表示无问题）
      - next_open_hint:    下次开盘提示（仅 closed_* 阶段填充）

    目的：杜绝在周末/假期把"上个交易日的隔夜数据"当成"正在发生的实时回落"。
    """
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(tz=et)
    now_iso = now_et.isoformat(timespec="seconds")

    # 解析 update_time（富途返回字符串，无时区，按美东处理）
    snap_dt = None
    age_min = None
    if update_time:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                snap_dt = datetime.strptime(update_time, fmt).replace(tzinfo=et)
                break
            except ValueError:
                continue
        if snap_dt is not None:
            age_min = round((now_et - snap_dt).total_seconds() / 60.0, 1)

    today_iso = now_et.date().isoformat()
    is_weekend = now_et.weekday() >= 5
    is_holiday = today_iso in _US_HOLIDAYS_2026

    # 市场阶段（基于美东 wall-clock）
    minute_of_day = now_et.hour * 60 + now_et.minute
    if is_weekend:
        market_phase = "closed_weekend"
    elif is_holiday:
        market_phase = "closed_holiday"
    elif 240 <= minute_of_day < 570:           # 04:00-09:30 ET
        market_phase = "pre_market"
    elif 570 <= minute_of_day < 960:           # 09:30-16:00 ET
        market_phase = "live"
    elif 960 <= minute_of_day < 1200:          # 16:00-20:00 ET
        market_phase = "after_market"
    elif minute_of_day >= 1200 or minute_of_day < 240:
        market_phase = "overnight"
    else:
        market_phase = "closed_overnight"

    # 时效等级
    sec = (sec_status or "").upper()
    if market_phase == "live" and (age_min is None or age_min <= 5):
        level = "live"
    elif market_phase in ("pre_market", "after_market", "overnight") and (age_min or 0) <= 30:
        level = "recent"
    elif market_phase in ("closed_weekend", "closed_holiday") or (age_min or 0) > 60:
        level = "stale" if (age_min or 0) <= 1440 else "expired"
    else:
        level = "recent"

    warn = None
    if market_phase == "closed_weekend":
        warn = (
            f"WEEKEND_STALE: 当前是美股周末，pre/after/overnight 全部为上一交易日遗留快照；"
            f"快照时间={update_time}, 距今 {age_min} 分钟。隔夜成交量极低，价格信号不可靠，"
            f"禁止将其叙述为'正在发生的实时走势'。"
        )
    elif market_phase == "closed_holiday":
        warn = f"HOLIDAY_STALE: 当前为美股法定假期，所有 session 数据均为节前遗留。"
    elif level == "expired":
        warn = f"EXPIRED: 快照已过期 {age_min} 分钟，请重新拉取行情后再做决策。"
    elif market_phase == "overnight" and (age_min or 0) > 30:
        warn = f"OVERNIGHT_LOW_LIQUIDITY: 隔夜成交量稀薄，价格信号参考性低（age={age_min}min）。"

    out = {
        "now_et": now_iso,
        "snapshot_time": update_time,
        "data_age_minutes": age_min,
        "is_weekend": is_weekend,
        "is_us_holiday": is_holiday,
        "market_phase": market_phase,
        "freshness_level": level,
        "sec_status": sec or "UNKNOWN",
        "warn": warn,
    }
    if market_phase.startswith("closed"):
        out["next_open_hint"] = _next_us_open_hint(now_et)
    return out


# ============================================================
# 客户端限流：30s 滑动窗口预算（对齐 FutuOpenD 服务端配额规则）
# ============================================================
class RateBudget:
    """30s 滑动窗口限流，超额则阻塞等待至有配额释放。

    解决问题：富途 OpenD 对每类接口有独立的 30s 配额：
        - get_market_snapshot:        60 次 / 30s
        - get_option_chain:           10 次 / 30s
        - request_history_kline:      ~30 次 / 30s
        - get_capital_flow / 异动等:   各自独立但 IP 维度共享

    超额时 OpenD 直接拒绝（RET=-1 频控错误），导致并发请求大面积失败。
    本限流器在客户端预算化排队，**不丢请求**：超额时 sleep 至窗口滑出再重试。
    """

    def __init__(
        self,
        n_per_window: int,
        window_sec: float = 30.0,
        name: str = "",
        max_wait_sec: float = 90.0,
    ):
        self.n = n_per_window
        self.window = window_sec
        self.name = name
        self.max_wait_sec = max_wait_sec
        self._q: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """阻塞等待至获得配额。最长等待 max_wait_sec，超过则放行（避免死锁）。"""
        deadline = time.monotonic() + self.max_wait_sec
        while True:
            with self._lock:
                now = time.monotonic()
                while self._q and now - self._q[0] > self.window:
                    self._q.popleft()
                if len(self._q) < self.n:
                    self._q.append(now)
                    return
                wait = self.window - (now - self._q[0]) + 0.05
            if time.monotonic() + wait > deadline:
                logger.warning(
                    "[RateBudget %s] wait %.1fs exceeds max_wait %.1fs, force pass",
                    self.name, wait, self.max_wait_sec,
                )
                with self._lock:
                    self._q.append(time.monotonic())
                return
            logger.debug(
                "[RateBudget %s] quota full (%d/%d), wait %.2fs",
                self.name, len(self._q), self.n, wait,
            )
            time.sleep(min(wait, 5.0))


# 富途官方配额（留 2 次安全余量；可经环境变量覆盖）
QUOTE_BUDGET = RateBudget(58, 30.0, "quote")
OPTION_CHAIN_BUDGET = RateBudget(8, 30.0, "option_chain")
KLINE_BUDGET = RateBudget(28, 30.0, "kline")
CAPITAL_FLOW_BUDGET = RateBudget(28, 30.0, "capital_flow")
PLATE_BUDGET = RateBudget(28, 30.0, "plate")
ORDER_BOOK_BUDGET = RateBudget(28, 30.0, "order_book")
UNUSUAL_BUDGET = RateBudget(28, 30.0, "unusual")


def _patch_futu_logging() -> None:
    """猴补丁：在 import futu 前把它的 TimedRotatingFileHandler 替换成 NullHandler，
    避免 macOS 上对 ~/.com.futunn.FutuOpenD/Log/ 无写权限导致 PermissionError。
    """
    import logging
    import logging.handlers
    import sys

    if getattr(_patch_futu_logging, "_done", False):
        return

    class _NoopHandler(logging.NullHandler):
        def __init__(self, *args, **kwargs):
            super().__init__()

    if "futu" not in sys.modules:
        logging.handlers.TimedRotatingFileHandler = _NoopHandler
    _patch_futu_logging._done = True


_patch_futu_logging()


def _infer_session_freshness(
    *,
    sec_status: str | None,
    update_time: str | None,
    pre_price: float | None,
    after_price: float | None,
    overnight_price: float | None,
) -> dict[str, str]:
    """根据顶层 sec_status + update_time 推断 pre/after/overnight 三段数据是
    "本场次最新" 还是 "上一场次遗留"。

    背景：富途 get_market_snapshot 没有为 pre/after/overnight 单独提供 update_time
    字段，但所有数据都跟随顶层 update_time 一起刷新。我们用 sec_status 来推断
    用户当前看到的每一段数据到底"代表哪一天"：

      - sec_status = PRE_MARKET_BEGIN     → pre 是当天本场，after/overnight 是昨日遗留
      - sec_status = NORMAL               → pre 是当天本场，after/overnight 是昨夜遗留
      - sec_status = AFTER_MARKET_BEGIN   → pre/after 都是当天本场，overnight 是昨夜遗留
      - sec_status = OVERNIGHT/CLOSED 等  → pre/after 是当天本场，overnight 是当天最新

    返回 {"pre_market": "current"|"stale", "after_market": "...", "overnight": "..."}
    """
    sec = (sec_status or "").upper()

    def _tag(price, is_current: bool) -> str:
        if price is None:
            return "n/a"
        return "current" if is_current else "stale"

    if sec.startswith("PRE_MARKET"):
        pre_cur, after_cur, overnight_cur = True, False, False
    elif sec == "NORMAL" or sec.startswith("MIDDLE"):
        pre_cur, after_cur, overnight_cur = True, False, False
    elif sec.startswith("AFTER_MARKET"):
        pre_cur, after_cur, overnight_cur = True, True, False
    elif "OVERNIGHT" in sec:
        pre_cur, after_cur, overnight_cur = True, True, True
    elif sec in ("CLOSED", "REST", ""):
        pre_cur, after_cur, overnight_cur = True, True, True
    else:
        pre_cur, after_cur, overnight_cur = True, True, True

    return {
        "pre_market": _tag(pre_price, pre_cur),
        "after_market": _tag(after_price, after_cur),
        "overnight": _tag(overnight_price, overnight_cur),
        "snapshot_time": update_time or "",
        "sec_status": sec or "UNKNOWN",
    }


class FutuSource(DataSource):
    """富途 OpenD 数据源：港/美/A 股 + 期权 Greeks（需本地启动 OpenD）。"""

    def __init__(self, host: str | None = None, port: int | None = None):
        self.host = host or settings.futu_host
        self.port = port or settings.futu_port

    @contextmanager
    def _ctx(self):
        from futu import OpenQuoteContext

        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            yield ctx
        finally:
            ctx.close()

    def get_quote(self, symbol: str) -> dict:
        from futu import RET_OK

        QUOTE_BUDGET.acquire()
        with self._ctx() as ctx:
            ret, df = ctx.get_market_snapshot([symbol])
            if ret != RET_OK:
                raise RuntimeError(f"富途行情失败: {df}")
            row = df.iloc[0]

            def _g(key, default=None):
                v = row.get(key, default)
                try:
                    import math
                    if isinstance(v, float) and math.isnan(v):
                        return None
                except Exception:
                    pass
                return v

            prev_close = _g("prev_close_price")
            last_price = _g("last_price")
            change_rate = None
            if prev_close and last_price:
                try:
                    change_rate = round((last_price - prev_close) / prev_close * 100, 4)
                except Exception:
                    change_rate = None

            return {
                "symbol": symbol,
                "name": _g("name"),
                "price": last_price,
                "prev_close": prev_close,
                "change_rate": change_rate,
                "open": _g("open_price"),
                "high": _g("high_price"),
                "low": _g("low_price"),
                "volume": _g("volume"),
                "turnover": _g("turnover"),
                "volume_ratio": _g("volume_ratio"),
                "turnover_rate": _g("turnover_rate"),
                "update_time": _g("update_time"),
                "sec_status": _g("sec_status"),
                "session_freshness": _infer_session_freshness(
                    sec_status=_g("sec_status"),
                    update_time=_g("update_time"),
                    pre_price=_g("pre_price"),
                    after_price=_g("after_price"),
                    overnight_price=_g("overnight_price"),
                ),
                "quote_freshness": _compute_quote_freshness(
                    update_time=_g("update_time"),
                    sec_status=_g("sec_status"),
                ),

                "valuation": {
                    "pe_ttm": _g("pe_ttm_ratio"),
                    "pe_static": _g("pe_ratio"),
                    "pb": _g("pb_ratio"),
                    "eps": _g("earning_per_share"),
                    "net_asset_per_share": _g("net_asset_per_share"),
                    "net_profit": _g("net_profit"),
                    "market_cap": _g("total_market_val"),
                    "circular_market_cap": _g("circular_market_val"),
                    "issued_shares": _g("issued_shares"),
                    "outstanding_shares": _g("outstanding_shares"),
                    "dividend_ttm": _g("dividend_ttm"),
                    "dividend_yield_ttm": _g("dividend_ratio_ttm"),
                    "dividend_lfy": _g("dividend_lfy"),
                    "dividend_yield_lfy": _g("dividend_lfy_ratio"),
                },

                "range_52w": {
                    "high": _g("highest52weeks_price"),
                    "low": _g("lowest52weeks_price"),
                    "high_history": _g("highest_history_price"),
                    "low_history": _g("lowest_history_price"),
                },

                "pre_market": {
                    "price": _g("pre_price"),
                    "high": _g("pre_high_price"),
                    "low": _g("pre_low_price"),
                    "volume": _g("pre_volume"),
                    "turnover": _g("pre_turnover"),
                    "change": _g("pre_change_val"),
                    "change_rate": _g("pre_change_rate"),
                    "amplitude": _g("pre_amplitude"),
                },

                "after_market": {
                    "price": _g("after_price"),
                    "high": _g("after_high_price"),
                    "low": _g("after_low_price"),
                    "volume": _g("after_volume"),
                    "turnover": _g("after_turnover"),
                    "change": _g("after_change_val"),
                    "change_rate": _g("after_change_rate"),
                    "amplitude": _g("after_amplitude"),
                },

                "overnight": {
                    "price": _g("overnight_price"),
                    "high": _g("overnight_high_price"),
                    "low": _g("overnight_low_price"),
                    "volume": _g("overnight_volume"),
                    "turnover": _g("overnight_turnover"),
                    "change": _g("overnight_change_val"),
                    "change_rate": _g("overnight_change_rate"),
                    "amplitude": _g("overnight_amplitude"),
                    "update_time": _g("overnight_update_time"),
                    "status": _g("overnight_market_status"),
                },
            }

    def get_history(
        self, symbol: str, period: str = "3mo", interval: str = "1d"
    ) -> pd.DataFrame:
        from datetime import date, timedelta

        from futu import KLType, RET_OK

        ktype_map = {
            "1d": KLType.K_DAY,
            "1h": KLType.K_60M,
            "30m": KLType.K_30M,
            "15m": KLType.K_15M,
            "5m": KLType.K_5M,
            "1m": KLType.K_1M,
        }
        days_map = {"1mo": 30, "3mo": 95, "6mo": 190, "1y": 370, "2y": 740}
        days = days_map.get(period, 95)

        end = date.today()
        start = end - timedelta(days=days)

        KLINE_BUDGET.acquire()
        with self._ctx() as ctx:
            ret, df, _ = ctx.request_history_kline(
                symbol,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                ktype=ktype_map.get(interval, KLType.K_DAY),
                max_count=days,
            )
            if ret != RET_OK:
                raise RuntimeError(f"富途K线失败: {df}")
            return df

    def get_capital_flow(self, symbol: str) -> dict:
        """资金流：返回最新一笔聚合 + 聪明钱（super+big）净额。

        字段说明（金额单位 USD/HKD/CNY，由富途按标的市场决定）：
          in_flow         总净流入（含所有档位）
          main_in_flow    主力净流入（仅港/A 股有效，美股 N/A）
          super_in_flow   超大单
          big_in_flow     大单
          mid_in_flow     中单
          sml_in_flow     小单
          smart_money     super_in_flow + big_in_flow（"聪明钱"近似）
        """
        from futu import RET_OK

        CAPITAL_FLOW_BUDGET.acquire()
        with self._ctx() as ctx:
            ret, df = ctx.get_capital_flow(symbol)
            if ret != RET_OK:
                raise RuntimeError(f"富途资金流失败: {df}")
            if df is None or df.empty:
                return {}
            row = df.iloc[-1].to_dict()

            def _num(k):
                v = row.get(k)
                if v in (None, "N/A", "") or (isinstance(v, float) and v != v):
                    return None
                try:
                    return float(v)
                except Exception:
                    return None

            super_in = _num("super_in_flow")
            big_in = _num("big_in_flow")
            smart = None
            if super_in is not None or big_in is not None:
                smart = (super_in or 0) + (big_in or 0)

            return {
                "in_flow": _num("in_flow"),
                "main_in_flow": _num("main_in_flow"),
                "super_in_flow": super_in,
                "big_in_flow": big_in,
                "mid_in_flow": _num("mid_in_flow"),
                "sml_in_flow": _num("sml_in_flow"),
                "smart_money": smart,
                "capital_flow_item_time": str(row.get("capital_flow_item_time")),
            }

    def get_owner_plate(self, symbol: str) -> list[dict]:
        """获取标的所属板块（行业/概念/地区）。"""
        from futu import RET_OK

        PLATE_BUDGET.acquire()
        with self._ctx() as ctx:
            ret, df = ctx.get_owner_plate([symbol])
            if ret != RET_OK or df is None or df.empty:
                return []
            return df.to_dict(orient="records")

    def get_plate_stock(self, plate_code: str, limit: int = 50) -> list[dict]:
        """获取指定板块的成分股。"""
        from futu import RET_OK

        PLATE_BUDGET.acquire()
        with self._ctx() as ctx:
            ret, df = ctx.get_plate_stock(plate_code)
            if ret != RET_OK or df is None or df.empty:
                return []
            return df.head(limit).to_dict(orient="records")

    def get_order_book(self, symbol: str, num: int = 10) -> dict:
        """获取实时买卖盘（L1/L2 视账户权限而定）。"""
        from futu import RET_OK

        ORDER_BOOK_BUDGET.acquire()
        with self._ctx() as ctx:
            ret, data = ctx.get_order_book(symbol, num=num)
            if ret != RET_OK:
                return {"error": str(data)}
            return data if isinstance(data, dict) else {"raw": str(data)}

    def get_option_chain(
        self, symbol: str, expiry: str | None = None, max_contracts: int | None = 20
    ) -> pd.DataFrame:
        from futu import RET_OK

        with self._ctx() as ctx:
            OPTION_CHAIN_BUDGET.acquire()
            ret, exp_df = ctx.get_option_expiration_date(code=symbol)
            if ret != RET_OK:
                raise RuntimeError(f"富途期权到期日失败: {exp_df}")
            if exp_df.empty:
                return pd.DataFrame()

            expiry = expiry or exp_df["strike_time"].iloc[0]
            OPTION_CHAIN_BUDGET.acquire()
            ret, chain = ctx.get_option_chain(
                code=symbol, start=expiry, end=expiry
            )
            if ret != RET_OK:
                raise RuntimeError(f"富途期权链失败: {chain}")

            codes = chain["code"].tolist()
            total_contracts = len(codes)
            if max_contracts is not None:
                codes = codes[:max_contracts]
            if not codes:
                return chain

            snaps = []
            for i in range(0, len(codes), 200):
                QUOTE_BUDGET.acquire()
                ret, snap = ctx.get_market_snapshot(codes[i:i + 200])
                if ret != RET_OK:
                    continue
                if snap is not None and not snap.empty:
                    snaps.append(snap)
            if not snaps:
                return chain
            snap = pd.concat(snaps, ignore_index=True)
            snap["_chain_total_contracts"] = total_contracts
            snap["_snapshot_contracts"] = len(codes)
            snap["_is_truncated"] = max_contracts is not None and len(codes) < total_contracts

            cols = [
                "code", "option_strike_price", "option_type",
                "option_implied_volatility", "option_delta",
                "option_gamma", "option_theta", "option_vega",
                "option_open_interest", "volume", "last_price",
                "_chain_total_contracts", "_snapshot_contracts", "_is_truncated",
            ]
            return snap[[c for c in cols if c in snap.columns]]

    # ============================================================
    # Layer 3: Anomaly Skills 三件套（对齐 futu-{capital,derivatives,technical}-anomaly）
    # ============================================================
    def get_financial_unusual(
        self,
        symbol: str,
        time_range: int = 7,
        analysis_dimensions: list[str] | None = None,
        language_id: int = 0,
    ) -> dict:
        """资金面异动（对齐 `futu-capital-anomaly` Skill）。

        Args:
            symbol:              标准带前缀代码，如 US.NVDA / HK.00700
            time_range:          自然日窗口，默认 7
            analysis_dimensions: 子维度过滤，可选值见富途 SKILL.md：
                - funds_distribution           资金分布
                - funds_broker                 买卖经纪商
                - funds_flow                   资金流向
                - short_sell_number            卖空数量
                - short_sell_ratio             卖空比例
                - short_sell_number_and_ratio  卖空数量+比例同时异动
            language_id:         0=zh-CN / 1=zh-TW / 2=en / 4=th / 5=ja

        Returns:
            {
              "ok":                  bool,
              "method":              "get_financial_unusual",
              "stock_symbol":        str,
              "time_range":          int,
              "analysis_dimensions": list[str],
              "language_id":         int,
              "data":                dict | list,   # 富途原始返回（含 content 文本）
            }
        """
        from futu import RET_OK

        UNUSUAL_BUDGET.acquire()
        with self._ctx() as ctx:
            ret, data = ctx.get_financial_unusual(
                symbol,
                time_range=time_range,
                analysis_dimensions=analysis_dimensions,
                language_id=language_id,
            )
            return {
                "ok": ret == RET_OK,
                "method": "get_financial_unusual",
                "stock_symbol": symbol,
                "time_range": time_range,
                "analysis_dimensions": analysis_dimensions or [],
                "language_id": language_id,
                "data": _normalize_unusual(data) if ret == RET_OK else None,
                "error": None if ret == RET_OK else str(data),
            }

    def get_derivative_unusual(
        self,
        symbol: str,
        time_range: int = 7,
        analysis_dimensions: list[str] | None = None,
        language_id: int = 0,
    ) -> dict:
        """衍生品异动（对齐 `futu-derivatives-anomaly` Skill）。

        analysis_dimensions 可选值（见富途 SKILL.md）：
          - warrant_ratio              牛熊证街货比例（仅港股）
          - warrant_price_distribution 牛熊证街货价格区间（仅港股）
          - option_unusual             期权大单
          - option_volatility          期权波动率
          - option_volume_price        期权量价
          - option_sentiment           期权情绪（PCR）
          - option_comprehensive       综合信号
        """
        from futu import RET_OK

        UNUSUAL_BUDGET.acquire()
        with self._ctx() as ctx:
            ret, data = ctx.get_derivative_unusual(
                symbol,
                time_range=time_range,
                analysis_dimensions=analysis_dimensions,
                language_id=language_id,
            )
            return {
                "ok": ret == RET_OK,
                "method": "get_derivative_unusual",
                "stock_symbol": symbol,
                "time_range": time_range,
                "analysis_dimensions": analysis_dimensions or [],
                "language_id": language_id,
                "data": _normalize_unusual(data) if ret == RET_OK else None,
                "error": None if ret == RET_OK else str(data),
            }

    def get_technical_unusual(
        self,
        symbol: str,
        time_range: int = 7,
        indicator_filters: list[str] | None = None,
        language_id: int = 0,
    ) -> dict:
        """技术面异动（对齐 `futu-technical-anomaly` Skill）。

        indicator_filters 可选值（见富途 SKILL.md）：
          CCI / KDJ / BIAS / AR / BR / VR / PSY / OSC / WMSR
          MACD / BOLL / MA / RSI6 / RSI12 / RSI24
        """
        from futu import RET_OK

        UNUSUAL_BUDGET.acquire()
        with self._ctx() as ctx:
            ret, data = ctx.get_technical_unusual(
                symbol,
                time_range=time_range,
                indicator_filters=indicator_filters,
                language_id=language_id,
            )
            return {
                "ok": ret == RET_OK,
                "method": "get_technical_unusual",
                "stock_symbol": symbol,
                "time_range": time_range,
                "indicator_filters": indicator_filters or [],
                "language_id": language_id,
                "data": _normalize_unusual(data) if ret == RET_OK else None,
                "error": None if ret == RET_OK else str(data),
            }


def _normalize_unusual(value):
    """将 futu unusual 接口返回的 DataFrame / dict / list 全部统一成 JSON-friendly 结构。"""
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict(orient="records")
        except TypeError:
            return value.to_dict()
    if isinstance(value, dict):
        return {k: _normalize_unusual(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_unusual(v) for v in value]
    return value
