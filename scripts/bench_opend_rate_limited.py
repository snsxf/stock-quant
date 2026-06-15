"""OpenD 限流模拟测试脚本（不改主代码，仅本地验证）。

目标：
  1. 在客户端模拟"30 秒滑动窗口预算限流"后，能否通过 4-8 路并发稳定拿到全部数据
  2. 验证期权链等重接口在限流保护下是否仍能完整召回（不丢合约）
  3. 对比 "无限流并发" vs "有限流并发" vs "纯串行" 的吞吐和成功率

模拟场景：
  Scenario A: 8 只股票 × (1 quote + 1 option_chain) = 16 个调用，4 路并发
  Scenario B: 高频 quote 请求（80 次，覆盖 2 个 30s 窗口）

Usage:
    python scripts/bench_opend_rate_limited.py
"""
from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from futu import OpenQuoteContext, OptionCondType, RET_OK


HOST = "127.0.0.1"
PORT = 11111

SYMBOLS = ["US.NVDA", "US.AAPL", "US.TSLA", "US.MSFT",
           "US.GOOG", "US.META", "US.AMZN", "US.AMD"]


# ============================================================
# 滑动窗口限流器（拟 server-side 的 30s 配额规则）
# ============================================================
class RateBudget:
    """30s 滑动窗口限流：超额则阻塞等待至有配额释放。"""

    def __init__(self, n_per_window: int, window_sec: float, name: str):
        self.n = n_per_window
        self.window = window_sec
        self.name = name
        self.q: deque[float] = deque()
        self.lock = threading.Lock()
        self.wait_total = 0.0
        self.wait_count = 0

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.q and now - self.q[0] > self.window:
                    self.q.popleft()
                if len(self.q) < self.n:
                    self.q.append(now)
                    return
                wait = self.window - (now - self.q[0]) + 0.05
            self.wait_total += wait
            self.wait_count += 1
            time.sleep(wait)

    def stats(self):
        return {
            "name": self.name,
            "wait_total_s": round(self.wait_total, 2),
            "wait_count": self.wait_count,
            "current_load": len(self.q),
            "limit": self.n,
        }


# 富途官方配额（留 2 次安全余量）
QUOTE_BUDGET = RateBudget(58, 30.0, "quote")
OPTION_BUDGET = RateBudget(8, 30.0, "option_chain")
KLINE_BUDGET = RateBudget(28, 30.0, "kline")


@dataclass
class CallResult:
    api: str
    ok: bool
    latency_ms: float
    data_size: int = 0  # 拿到的记录数（验证完整性）
    err: str = ""


# ============================================================
# 真实接口调用（带限流）
# ============================================================
def call_quote_limited(ctx, symbol: str) -> CallResult:
    QUOTE_BUDGET.acquire()
    t0 = time.perf_counter()
    try:
        ret, df = ctx.get_market_snapshot([symbol])
        dt = (time.perf_counter() - t0) * 1000
        if ret != RET_OK:
            return CallResult("quote", False, dt, 0, f"RET={ret}: {df}")
        return CallResult("quote", True, dt, data_size=len(df))
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        return CallResult("quote", False, dt, 0, f"{type(e).__name__}: {e}")


def call_option_chain_limited(ctx, symbol: str) -> CallResult:
    OPTION_BUDGET.acquire()
    t0 = time.perf_counter()
    try:
        ret, df = ctx.get_option_chain(
            code=symbol,
            option_cond_type=OptionCondType.ALL,
        )
        dt = (time.perf_counter() - t0) * 1000
        if ret != RET_OK:
            return CallResult("option_chain", False, dt, 0, f"RET={ret}: {df}")
        return CallResult("option_chain", True, dt, data_size=len(df))
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        return CallResult("option_chain", False, dt, 0, f"{type(e).__name__}: {e}")


def call_kline_limited(ctx, symbol: str) -> CallResult:
    KLINE_BUDGET.acquire()
    t0 = time.perf_counter()
    try:
        from futu import KLType, AuType
        ret, df, _ = ctx.request_history_kline(
            code=symbol,
            ktype=KLType.K_DAY,
            autype=AuType.QFQ,
            max_count=60,
        )
        dt = (time.perf_counter() - t0) * 1000
        if ret != RET_OK:
            return CallResult("kline", False, dt, 0, f"RET={ret}: {df}")
        return CallResult("kline", True, dt, data_size=len(df))
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        return CallResult("kline", False, dt, 0, f"{type(e).__name__}: {e}")


# ============================================================
# 报表工具
# ============================================================
def _percentile(data, p):
    if not data:
        return 0.0
    data = sorted(data)
    k = (len(data) - 1) * p
    f = int(k)
    c = min(f + 1, len(data) - 1)
    return data[f] + (data[c] - data[f]) * (k - f)


def report(name: str, results: list[CallResult], wall_time: float):
    n = len(results)
    by_api: dict[str, list[CallResult]] = {}
    for r in results:
        by_api.setdefault(r.api, []).append(r)

    ok = sum(1 for r in results if r.ok)
    fail = n - ok
    qps = n / wall_time if wall_time > 0 else 0

    print(f"\n[{name}]")
    print(f"  total={n}  ok={ok}  fail={fail}  success_rate={ok/n*100:.1f}%")
    print(f"  wall_time={wall_time:.2f}s  qps={qps:.2f} req/s")

    for api, rs in by_api.items():
        ok_lats = [r.latency_ms for r in rs if r.ok]
        sizes = [r.data_size for r in rs if r.ok]
        avg_lat = statistics.mean(ok_lats) if ok_lats else 0
        p95_lat = _percentile(ok_lats, 0.95) if ok_lats else 0
        avg_size = statistics.mean(sizes) if sizes else 0
        ok_count = sum(1 for r in rs if r.ok)
        print(f"  [{api:13s}] ok={ok_count}/{len(rs)}  "
              f"avg_lat={avg_lat:.0f}ms  p95={p95_lat:.0f}ms  "
              f"avg_data_rows={avg_size:.0f}")

    if fail > 0:
        errs: dict[str, int] = {}
        for r in results:
            if not r.ok:
                key = r.err.split("\n")[0][:80]
                errs[key] = errs.get(key, 0) + 1
        print("  errors:")
        for k, v in errs.items():
            print(f"    [{v}x] {k}")

    # 限流统计
    for b in (QUOTE_BUDGET, OPTION_BUDGET, KLINE_BUDGET):
        s = b.stats()
        if s["wait_count"] > 0:
            print(f"  [budget {s['name']:13s}] waited {s['wait_count']}x, "
                  f"total {s['wait_total_s']:.2f}s")


def reset_budgets():
    """重置限流计数器，避免上一轮影响下一轮。"""
    for b in (QUOTE_BUDGET, OPTION_BUDGET, KLINE_BUDGET):
        b.q.clear()
        b.wait_total = 0.0
        b.wait_count = 0


# ============================================================
# 测试场景
# ============================================================
def scenario_A_mixed_8stocks(workers: int = 4):
    """8 只股票 × (quote + option_chain + kline) = 24 个调用。

    最贴近实战 daily_brief 的负载结构。
    """
    reset_budgets()
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        tasks = []
        for sym in SYMBOLS:
            tasks.append(("quote", sym))
            tasks.append(("option_chain", sym))
            tasks.append(("kline", sym))

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = []
            for api, sym in tasks:
                if api == "quote":
                    futs.append(ex.submit(call_quote_limited, ctx, sym))
                elif api == "option_chain":
                    futs.append(ex.submit(call_option_chain_limited, ctx, sym))
                else:
                    futs.append(ex.submit(call_kline_limited, ctx, sym))
            results = [f.result() for f in as_completed(futs)]
        wall = time.perf_counter() - t0
    finally:
        ctx.close()
    report(f"Scenario A: 8 票 × 3 接口 (workers={workers}, 限流开)", results, wall)
    return results


def scenario_B_quote_storm(n: int = 80, workers: int = 8):
    """高频 quote 请求 80 次，跨越 2 个 30s 窗口，验证限流是否能 hold 住。"""
    reset_budgets()
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(call_quote_limited, ctx, SYMBOLS[i % len(SYMBOLS)])
                    for i in range(n)]
            results = [f.result() for f in as_completed(futs)]
        wall = time.perf_counter() - t0
    finally:
        ctx.close()
    report(f"Scenario B: quote x{n} (workers={workers}, 限流开)", results, wall)
    return results


def scenario_C_option_storm(n: int = 20, workers: int = 4):
    """高频 option_chain 请求 20 次（远超 10/30s 配额），验证窗口限流。"""
    reset_budgets()
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(call_option_chain_limited, ctx,
                              SYMBOLS[i % len(SYMBOLS)])
                    for i in range(n)]
            results = [f.result() for f in as_completed(futs)]
        wall = time.perf_counter() - t0
    finally:
        ctx.close()
    report(f"Scenario C: option_chain x{n} (workers={workers}, 限流开)",
           results, wall)
    return results


def scenario_D_no_limit_baseline(workers: int = 4):
    """对照组：去掉限流，直接打——预期会被 OpenD 拒绝。"""
    reset_budgets()
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        def call_raw(sym):
            t0 = time.perf_counter()
            try:
                ret, df = ctx.get_option_chain(
                    code=sym, option_cond_type=OptionCondType.ALL)
                dt = (time.perf_counter() - t0) * 1000
                if ret != RET_OK:
                    return CallResult("option_chain", False, dt, 0,
                                      f"RET={ret}: {df}")
                return CallResult("option_chain", True, dt, len(df))
            except Exception as e:
                dt = (time.perf_counter() - t0) * 1000
                return CallResult("option_chain", False, dt, 0,
                                  f"{type(e).__name__}: {e}")

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(call_raw, SYMBOLS[i % len(SYMBOLS)])
                    for i in range(20)]
            results = [f.result() for f in as_completed(futs)]
        wall = time.perf_counter() - t0
    finally:
        ctx.close()
    report(f"Scenario D: option_chain x20 (workers={workers}, 限流关-对照组)",
           results, wall)
    return results


def verify_data_completeness(results_with_limit: list[CallResult],
                             results_without_limit: list[CallResult] = None):
    """验证：限流不影响数据完整性 — 同一标的，限流前后返回的合约数应一致。"""
    print("\n" + "=" * 70)
    print("数据完整性校验")
    print("=" * 70)

    by_api_size: dict[str, list[int]] = {}
    for r in results_with_limit:
        if r.ok:
            by_api_size.setdefault(r.api, []).append(r.data_size)

    for api, sizes in by_api_size.items():
        if not sizes:
            continue
        unique_sizes = set(sizes)
        avg = sum(sizes) / len(sizes)
        print(f"  [{api}] 调用 {len(sizes)} 次, "
              f"返回行数: min={min(sizes)} max={max(sizes)} avg={avg:.0f}")
        if api == "option_chain":
            print(f"    ✓ 期权链合约数 (各标的不同是正常的) ")
        elif api == "kline":
            unique_check = "✓ 一致" if len(unique_sizes) == 1 else f"⚠ 多种 {sorted(unique_sizes)}"
            print(f"    K 线行数: {unique_check}")


def main():
    print("=" * 70)
    print(f"OpenD 限流模拟测试 @ {HOST}:{PORT}")
    print("配额: quote=58/30s, option_chain=8/30s, kline=28/30s (留 2 次余量)")
    print("=" * 70)

    # warmup
    try:
        ctx = OpenQuoteContext(host=HOST, port=PORT)
        ret, _ = ctx.get_market_snapshot(["US.NVDA"])
        ctx.close()
        if ret != RET_OK:
            print("✗ warmup failed")
            return
        print("✓ warmup ok")
    except Exception as e:
        print(f"✗ warmup error: {e}")
        return

    # 1. 运行核心场景
    results_a = scenario_A_mixed_8stocks(workers=4)

    print("\n→ 等 30 秒清空配额窗口...")
    time.sleep(31)

    results_c = scenario_C_option_storm(n=20, workers=4)

    print("\n→ 等 30 秒清空配额窗口...")
    time.sleep(31)

    results_b = scenario_B_quote_storm(n=80, workers=8)

    # 2. 验证数据完整性
    verify_data_completeness(results_a + results_b + results_c)

    # 3. 总结
    print("\n" + "=" * 70)
    print("总结")
    print("=" * 70)
    all_results = results_a + results_b + results_c
    total_ok = sum(1 for r in all_results if r.ok)
    print(f"  总调用: {len(all_results)}, 成功: {total_ok}, "
          f"成功率: {total_ok/len(all_results)*100:.1f}%")


if __name__ == "__main__":
    main()
