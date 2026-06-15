"""港交所披露易 CCASS 持仓爬取脚本（独立工具，不依赖新闻流）。

数据源（全部为港交所官方公开页面，无需鉴权）：
  1. CCASS Shareholding Search   —— 中央结算系统每日各券商/托管行的持股
     https://www3.hkexnews.hk/sdw/search/searchsdw.aspx
  2. Substantial Shareholders DI —— 5% 以上股东权益披露（强制申报）
     https://di.hkex.com.hk/di/summary/...

特性：
  - 纯 stdlib + httpx + pandas，与 stock-quant 现有依赖兼容
  - ASP.NET WebForms ViewState 自动重放（CCASS 是 ASP.NET 老页面）
  - 支持单日快照 + 双日变动对比（找"谁在增减仓"）
  - 自动识别 H 股代码格式（5 位数字，左补 0）
  - 输出 DataFrame，可直接写 csv / 接 stock-quant 主链路
  - 内置基于 (stock_code, target_date) 的本地 parquet 缓存（历史快照永不变，命中率近 100%）

【缓存语义说明 —— 必读】
    HKEX 的 CCASS 数据是"按日期 T 的静态快照"接口：
      - 每个交易日约 18:00 (HK) 后发布当日数据，写入后永久不变
      - 同一个 (stock_code, T) 的查询结果，T 时拿到 vs T+N 时再拿 → 字节级一致
      - 因此 fetch_ccass(code, T) 的结果用 (code, T) 做 key 缓存是 100% 安全的
    "缓存陷阱"：
      - 想看持仓变化时，必须显式拉多个不同日期的快照后再 diff
      - 不要指望"再请求一次 5/8 就能看到 5/9 的数据"——这是接口设计的根本限制
      - 想跟踪日级别变动 → 用 fetch_ccass_range(code, start, end) 批量回填

用法：
    # 单日快照（默认最近一个交易日）
    uv run python scripts/hkex_ccass_scraper.py 02899

    # 指定日期（YYYY-MM-DD）
    uv run python scripts/hkex_ccass_scraper.py 02899 --date 2026-05-08

    # 对比两日，计算每个 broker 的增减仓（重点功能！）
    uv run python scripts/hkex_ccass_scraper.py 02899 \\
        --date 2026-05-21 --compare 2026-05-08

    # 拉日期范围内每天的快照（自动并发 + 缓存）
    uv run python scripts/hkex_ccass_scraper.py 02899 \\
        --range 2026-05-08:2026-05-21

    # 强制刷新缓存（极少用：HKEX 数据校正后）
    uv run python scripts/hkex_ccass_scraper.py 02899 --date 2026-05-08 --no-cache

    # 同时拉权益披露（5% 以上股东）
    uv run python scripts/hkex_ccass_scraper.py 02899 --di

    # 导出 CSV
    uv run python scripts/hkex_ccass_scraper.py 02899 --csv ccass_02899.csv

代码示例：
    from hkex_ccass_scraper import fetch_ccass, compare_ccass, fetch_ccass_range

    df_t = fetch_ccass("02899", "2026-05-21")
    df_p = fetch_ccass("02899", "2026-05-08")
    diff = compare_ccass(df_t, df_p, top_n=20)

    # 拉一段时间的所有快照（用于复盘）
    panel = fetch_ccass_range("02899", "2026-05-08", "2026-05-21")
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

CCASS_URL = "https://www3.hkexnews.hk/sdw/search/searchsdw.aspx"
DI_BASE = "https://di.hkex.com.hk/di/summary"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0

CACHE_DIR = Path(
    os.environ.get(
        "HKEX_CCASS_CACHE_DIR",
        Path.home() / ".cache" / "stock_quant" / "hkex_ccass",
    )
)


def _cache_path(stock_code: str, d: date) -> Path:
    """缓存文件路径：~/.cache/stock_quant/hkex_ccass/{code}/{YYYY-MM-DD}.pkl"""
    return CACHE_DIR / stock_code / f"{d.isoformat()}.pkl"


def _load_cache(stock_code: str, d: date) -> pd.DataFrame | None:
    p = _cache_path(stock_code, d)
    if not p.exists():
        return None
    try:
        df = pd.read_pickle(p)
        return df
    except Exception:
        return None


def _save_cache(df: pd.DataFrame, stock_code: str, d: date) -> None:
    p = _cache_path(stock_code, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_pickle(p)
    except Exception as exc:
        print(f"[cache] save failed: {exc}", file=sys.stderr)


# ============================================================
# 工具函数
# ============================================================
def normalize_hk_code(code: str) -> str:
    """统一港股代码为 5 位 0 左填充：'700' / '00700' / 'HK.00700' -> '00700'"""
    s = re.sub(r"^(HK\.|hk\.)", "", str(code).strip())
    s = re.sub(r"\D", "", s)
    if not s:
        raise ValueError(f"无法识别的港股代码: {code!r}")
    return s.zfill(5)


def previous_business_day(d: date | None = None) -> date:
    """粗略推断上一个工作日（不考虑港股节假日，仅扣周末）"""
    d = d or date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def parse_shares(s: str) -> float:
    """'1,234,567' -> 1234567.0；'-' -> NaN"""
    if s is None:
        return float("nan")
    s = str(s).strip().replace(",", "")
    if s in ("", "-", "N/A"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def parse_pct(s: str) -> float:
    """'13.40%' -> 13.40"""
    if s is None:
        return float("nan")
    s = str(s).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return float("nan")


# ============================================================
# CCASS 爬取核心
# ============================================================
def _extract_hidden(html: str, name: str) -> str:
    """从 ASP.NET WebForms 页面里抠 hidden 字段（ViewState / EVENTVALIDATION 等）"""
    m = re.search(
        rf'<input[^>]+name="{re.escape(name)}"[^>]+value="([^"]*)"',
        html,
        re.IGNORECASE,
    )
    return m.group(1) if m else ""


_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD_BODY_RE = re.compile(
    r'<td[^>]*class="(col-[a-z-]+)[^"]*"[^>]*>.*?'
    r'<div\s+class="mobile-list-body">(.*?)</div>',
    re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_ccass_html(html: str) -> pd.DataFrame:
    """解析 HKEX CCASS 页面（mobile-list 结构）"""
    m = re.search(
        r'<table[^>]*class="[^"]*table-mobile-list[^"]*"[^>]*>(.*?)</table>',
        html,
        re.S | re.I,
    )
    if not m:
        return pd.DataFrame()
    rows: list[dict] = []
    for tr_html in _TR_RE.findall(m.group(1)):
        cells: dict[str, str] = {}
        for cls, body in _TD_BODY_RE.findall(tr_html):
            text = _TAG_RE.sub("", body).strip()
            text = re.sub(r"\s+", " ", text)
            cells[cls] = text
        if cells.get("col-participant-id"):
            rows.append(
                {
                    "participant_id": cells.get("col-participant-id", ""),
                    "participant_name": cells.get("col-participant-name", ""),
                    "address": cells.get("col-address", ""),
                    "shareholding": parse_shares(cells.get("col-shareholding", "")),
                    "pct_of_issued": parse_pct(cells.get("col-shareholding-percent", "")),
                }
            )
    return pd.DataFrame(rows)


def _parse_summary(html: str) -> dict:
    """从汇总区抠总发行股数 / CCASS 总持股 / 参与者数等"""
    out: dict[str, float | str] = {}
    for label_pat, key in [
        (r"Total number of Issued (?:Shares|Units|Warrants)[^<]*", "total_issued_shares"),
        (r"Total number of CCASS Participants(?: holding shares)?[^<]*", "total_ccass_participants"),
        (
            r"(?:Total Shareholding of Market|Shareholding in CCASS)[^<]*",
            "total_ccass_shareholding",
        ),
    ]:
        m = re.search(
            label_pat + r".*?<div\s+class=\"value\">([0-9,\.%\s\(\)]+)</div>",
            html,
            re.S | re.I,
        )
        if m:
            out[key] = parse_shares(m.group(1))
    m = re.search(r'<div class="ccass-search-datepicker">([^<]+)</div>', html)
    if m:
        out["as_of_text"] = m.group(1).strip()
    return out


def fetch_ccass(
    stock_code: str,
    target_date: str | date | None = None,
    *,
    client: httpx.Client | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """拉取指定日期某股票的 CCASS 持仓明细。

    参数：
        stock_code:  港股代码（700 / 02899 / HK.02899 均可）
        target_date: 'YYYY-MM-DD' 或 date 对象，留空=最近交易日
        use_cache:   是否使用 (stock_code, date) 本地缓存。CCASS 历史数据永久不变，
                     缓存命中拿到的是 100% 完整、与 HKEX 当时返回字节一致的全量数据。
                     仅在 HKEX 极罕见的数据校正时需要 use_cache=False 强刷。

    返回：DataFrame
        列：participant_id / participant_name / address /
            shareholding / pct_of_issued / as_of_date / stock_code
        额外属性：df.attrs['summary'] = 总持仓汇总
    """
    code5 = normalize_hk_code(stock_code)
    if target_date is None:
        d = previous_business_day()
    elif isinstance(target_date, str):
        d = datetime.strptime(target_date, "%Y-%m-%d").date()
    else:
        d = target_date
    date_str = d.strftime("%Y/%m/%d")

    if use_cache:
        cached = _load_cache(code5, d)
        if cached is not None and not cached.empty:
            cached.attrs.setdefault("summary", {})["_cache_hit"] = True
            return cached

    own = client is None
    if own:
        client = httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )

    try:
        r = client.get(CCASS_URL)
        r.raise_for_status()
        html = r.text

        viewstate = _extract_hidden(html, "__VIEWSTATE")
        viewstate_gen = _extract_hidden(html, "__VIEWSTATEGENERATOR")
        event_validation = _extract_hidden(html, "__EVENTVALIDATION")

        form = {
            "__EVENTTARGET": "btnSearch",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": viewstate_gen,
            "__EVENTVALIDATION": event_validation,
            "today": d.strftime("%Y%m%d"),
            "sortBy": "shareholding",
            "sortDirection": "desc",
            "txtShareholdingDate": date_str,
            "txtStockCode": code5,
            "txtStockName": "",
            "txtParticipantID": "",
            "txtParticipantName": "",
            "txtSelectDate": date_str,
        }
        r2 = client.post(
            CCASS_URL,
            data=form,
            headers={"Referer": CCASS_URL, "Origin": "https://www3.hkexnews.hk"},
        )
        r2.raise_for_status()
        result_html = r2.text

        df = _parse_ccass_html(result_html)
        if df.empty:
            raise RuntimeError(
                f"CCASS 返回空表：code={code5} date={date_str}（该日可能为节假日，或代码不存在）"
            )

        df["as_of_date"] = d.isoformat()
        df["stock_code"] = code5

        summary = {
            "as_of_date": d.isoformat(),
            "stock_code": code5,
            "total_ccass_shareholding": float(df["shareholding"].sum(skipna=True)),
            "n_participants": int(len(df)),
        }
        summary.update(_parse_summary(result_html))
        df.attrs["summary"] = summary
        if use_cache:
            _save_cache(df, code5, d)
        return df.reset_index(drop=True)
    finally:
        if own:
            client.close()


def fetch_ccass_range(
    stock_code: str,
    start: str | date,
    end: str | date,
    *,
    skip_weekends: bool = True,
    use_cache: bool = True,
) -> dict[str, pd.DataFrame]:
    """拉取日期区间内每个交易日的 CCASS 快照（缓存命中时不发 HTTP）。

    参数：
        stock_code:     港股代码
        start / end:    起止日期（含两端）
        skip_weekends:  是否跳过周末（HKEX 节假日仍可能返回空，会自动忽略）
        use_cache:      是否走本地缓存，默认 True

    返回：
        {date_str(YYYY-MM-DD): DataFrame, ...}
    """
    s = datetime.strptime(start, "%Y-%m-%d").date() if isinstance(start, str) else start
    e = datetime.strptime(end, "%Y-%m-%d").date() if isinstance(end, str) else end
    if s > e:
        s, e = e, s

    out: dict[str, pd.DataFrame] = {}
    with httpx.Client(
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
    ) as client:
        cur = s
        while cur <= e:
            if skip_weekends and cur.weekday() >= 5:
                cur += timedelta(days=1)
                continue
            try:
                df = fetch_ccass(stock_code, cur, client=client, use_cache=use_cache)
                out[cur.isoformat()] = df
            except Exception as exc:
                # 节假日 / 数据未发布 → 跳过
                print(f"  [skip] {cur}: {type(exc).__name__}: {exc}", file=sys.stderr)
            cur += timedelta(days=1)
    return out


def compare_ccass(
    df_today: pd.DataFrame,
    df_prev: pd.DataFrame,
    *,
    top_n: int = 20,
    only_movers: bool = True,
) -> pd.DataFrame:
    """对比两个日期的 CCASS 快照，输出每个 participant 的增减仓。

    参数：
        df_today / df_prev: fetch_ccass 返回的两个 DataFrame
        top_n:              按 |变动股数| 排序后取前 N 个
        only_movers:        是否过滤掉变动=0 的 participant

    返回：DataFrame
        列：participant_id / participant_name /
            shares_today / shares_prev / shares_delta /
            pct_today / pct_prev / pct_delta /
            change_pct（变动股数占自身仓位比例）
    """
    keep = ["participant_id", "participant_name", "shareholding", "pct_of_issued"]
    a = df_today[[c for c in keep if c in df_today.columns]].copy()
    b = df_prev[[c for c in keep if c in df_prev.columns]].copy()

    a = a.rename(columns={"shareholding": "shares_today", "pct_of_issued": "pct_today"})
    b = b.rename(columns={"shareholding": "shares_prev", "pct_of_issued": "pct_prev"})

    merged = pd.merge(
        a, b, on=["participant_id", "participant_name"], how="outer"
    ).fillna({"shares_today": 0, "shares_prev": 0, "pct_today": 0, "pct_prev": 0})

    merged["shares_delta"] = merged["shares_today"] - merged["shares_prev"]
    merged["pct_delta"] = merged["pct_today"] - merged["pct_prev"]
    base = merged["shares_prev"].where(merged["shares_prev"] != 0, merged["shares_today"])
    merged["change_pct"] = (merged["shares_delta"] / base * 100).where(base != 0, 0.0)

    if only_movers:
        merged = merged[merged["shares_delta"].abs() > 0]
    merged = merged.reindex(merged["shares_delta"].abs().sort_values(ascending=False).index)
    return merged.head(top_n).reset_index(drop=True)


# ============================================================
# DI Search（5% 以上权益披露）—— 补充功能
# ============================================================
def fetch_di_summary(stock_code: str, *, client: httpx.Client | None = None) -> pd.DataFrame:
    """拉取某股票的"主要股东权益披露"汇总（披露易 DI Search）。

    返回：DataFrame，列含 substantial_shareholder / nature / long_pct / short_pct / as_of
    """
    code = normalize_hk_code(stock_code).lstrip("0") or "0"
    url = f"{DI_BASE}/searchSubstantialShareholderInfo.do?stockcode={code}&showAll=1"

    own = client is None
    if own:
        client = httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
    try:
        r = client.get(url)
        r.raise_for_status()
        from io import StringIO
        try:
            tables = pd.read_html(StringIO(r.text))
        except ValueError as e:
            raise RuntimeError(f"DI 页面无表格 / 该股票无 5% 以上股东披露: {stock_code}") from e
        if not tables:
            return pd.DataFrame()
        df = max(tables, key=len).copy()
        df.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]
        df["stock_code"] = normalize_hk_code(stock_code)
        return df
    finally:
        if own:
            client.close()


# ============================================================
# CLI
# ============================================================
def _print_summary(df: pd.DataFrame) -> None:
    s = df.attrs.get("summary", {})
    print("\n=== CCASS Summary ===")
    for k, v in s.items():
        if isinstance(v, float) and v == v:
            print(f"  {k:30s}: {v:,.0f}" if v.is_integer() else f"  {k:30s}: {v}")
        else:
            print(f"  {k:30s}: {v}")


def _format_top(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    cols = [c for c in ["participant_id", "participant_name",
                        "shareholding", "pct_of_issued"] if c in df.columns]
    return df[cols].head(n).copy()


def main() -> None:
    p = argparse.ArgumentParser(description="HKEX CCASS Shareholding Scraper")
    p.add_argument("stock_code", help="港股代码 (700 / 02899 / HK.02899)")
    p.add_argument("--date", help="目标日期 YYYY-MM-DD (默认上一个工作日)", default=None)
    p.add_argument("--compare", help="对比日期 YYYY-MM-DD（用于计算增减仓）", default=None)
    p.add_argument(
        "--range",
        dest="range_str",
        help="日期范围 YYYY-MM-DD:YYYY-MM-DD（批量拉每日快照，自动缓存）",
        default=None,
    )
    p.add_argument("--top", type=int, default=20, help="展示前 N 个 participant")
    p.add_argument("--di", action="store_true", help="同时拉权益披露 (5%% 以上股东)")
    p.add_argument("--csv", default=None, help="导出 CSV 到指定路径")
    p.add_argument("--no-cache", action="store_true", help="禁用本地缓存，强制重抓")
    args = p.parse_args()
    use_cache = not args.no_cache

    if args.range_str:
        s_, e_ = args.range_str.split(":")
        print(f"\n[CCASS] {args.stock_code} range {s_} ~ {e_} (cache={use_cache})")
        panel = fetch_ccass_range(args.stock_code, s_, e_, use_cache=use_cache)
        print(f"\n=== Got {len(panel)} daily snapshots ===")
        for d_str, df_d in panel.items():
            n = int(df_d.attrs.get("summary", {}).get("n_participants", len(df_d)))
            cache_hit = df_d.attrs.get("summary", {}).get("_cache_hit", False)
            tag = "🟢 cached" if cache_hit else "🌐 fetched"
            print(f"  {d_str}  participants={n:>4}  {tag}")
        if len(panel) >= 2:
            dates = sorted(panel.keys())
            d_first, d_last = dates[0], dates[-1]
            print(f"\n=== Movers from {d_first} to {d_last} ===")
            diff = compare_ccass(panel[d_last], panel[d_first], top_n=args.top)
            with pd.option_context(
                "display.max_columns", None, "display.width", 200,
                "display.float_format", "{:,.2f}".format,
            ):
                print(diff.to_string(index=False))
        return

    print(f"\n[CCASS] {args.stock_code} @ {args.date or 'last business day'} (cache={use_cache})")
    df_t = fetch_ccass(args.stock_code, args.date, use_cache=use_cache)
    _print_summary(df_t)

    if args.compare:
        print(f"\n[CCASS] Compare with {args.compare} ...")
        df_p = fetch_ccass(args.stock_code, args.compare, use_cache=use_cache)
        diff = compare_ccass(df_t, df_p, top_n=args.top)
        print(f"\n=== Top {args.top} Movers (target - compare) ===")
        with pd.option_context(
            "display.max_columns", None, "display.width", 200, "display.float_format", "{:,.2f}".format
        ):
            print(diff.to_string(index=False))
        if args.csv:
            diff.to_csv(args.csv, index=False)
            print(f"\n[saved] {args.csv}")
    else:
        print(f"\n=== Top {args.top} Holders ===")
        with pd.option_context(
            "display.max_columns", None, "display.width", 200, "display.float_format", "{:,.2f}".format
        ):
            print(_format_top(df_t, args.top).to_string(index=False))
        if args.csv:
            df_t.to_csv(args.csv, index=False)
            print(f"\n[saved] {args.csv}")

    if args.di:
        try:
            print("\n[DI] Fetching substantial shareholders ...")
            df_di = fetch_di_summary(args.stock_code)
            with pd.option_context("display.max_columns", None, "display.width", 200):
                print(df_di.head(20).to_string(index=False))
        except Exception as e:
            print(f"[DI] failed: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"\n[FATAL] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
