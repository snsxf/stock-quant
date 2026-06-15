"""OpenD (FutuOpenD :11111) 并发压测脚本

测试维度：
  1) 共享 ctx 串行     —— baseline
  2) 共享 ctx 并发     —— 多线程共用一个 OpenQuoteContext (官方推荐模式)
  3) 独立 ctx 并发     —— 每个线程独立 ctx (我们 stock-quant 当前模式)
  4) 期权链并发        —— 模拟真实工作负载的最坏场景

输出：成功率 / p50 / p95 / p99 / 吞吐 (req/s)。

Usage:
    python scripts/bench_opend_concurrency.py
"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass

from futu import OpenQuoteContext, RET_OK


HOST = "127.0.0.1"
PORT = 11111

SYMBOLS = ["US.NVDA", "US.AAPL", "US.TSLA", "US.MSFT", "US.GOOG",
           "US.META", "US.AMZN", "US.AMD", "US.ARM", "US.AVGO"]


@dataclass
class CallResult:
    ok: bool
    latency_ms: float
    err: str = ""


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
    ok = sum(1 for r in results if r.ok)
    fail = n - ok
    lats = [r.latency_ms for r in results if r.ok]
    p50 = _percentile(lats, 0.5) if lats else 0
    p95 = _percentile(lats, 0.95) if lats else 0
    p99 = _percentile(lats, 0.99) if lats else 0
    avg = statistics.mean(lats) if lats else 0
    qps = n / wall_time if wall_time > 0 else 0

    print(f"\n[{name}]")
    print(f"  total={n}  ok={ok}  fail={fail}  success_rate={ok/n*100:.1f}%")
    print(f"  wall_time={wall_time:.2f}s  qps={qps:.2f} req/s")
    print(f"  latency(ms): avg={avg:.0f}  p50={p50:.0f}  p95={p95:.0f}  p99={p99:.0f}")
    if fail > 0:
        errs = {}
        for r in results:
            if not r.ok:
                key = r.err.split("\n")[0][:80]
                errs[key] = errs.get(key, 0) + 1
        print("  errors:")
        for k, v in errs.items():
            print(f"    [{v}x] {k}")


def call_quote(ctx, symbol: str) -> CallResult:
    t0 = time.perf_counter()
    try:
        ret, df = ctx.get_market_snapshot([symbol])
        dt = (time.perf_counter() - t0) * 1000
        if ret != RET_OK:
            return CallResult(False, dt, f"RET={ret}: {df}")
        return CallResult(True, dt)
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        return CallResult(False, dt, f"{type(e).__name__}: {e}")


def call_option_chain(ctx, symbol: str) -> CallResult:
    """期权链拉取（模拟我们 daily_brief 里的最重负载）。"""
    from futu import OptionType, OptionCondType
    t0 = time.perf_counter()
    try:
        ret, df = ctx.get_option_chain(
            code=symbol,
            option_cond_type=OptionCondType.ALL,
        )
        dt = (time.perf_counter() - t0) * 1000
        if ret != RET_OK:
            return CallResult(False, dt, f"RET={ret}: {df}")
        return CallResult(True, dt)
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        return CallResult(False, dt, f"{type(e).__name__}: {e}")


# ============================================================
# 测试用例
# ============================================================
def test_serial_shared_ctx(n: int = 20):
    """Test 1: 共享 ctx 串行 (baseline)。"""
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        t0 = time.perf_counter()
        results = [call_quote(ctx, SYMBOLS[i % len(SYMBOLS)]) for i in range(n)]
        wall = time.perf_counter() - t0
    finally:
        ctx.close()
    report(f"Test 1: 共享 ctx 串行 (n={n})", results, wall)


def test_concurrent_shared_ctx(n: int = 20, workers: int = 8):
    """Test 2: 共享 ctx 并发 (官方推荐：一个 ctx 多线程复用)。"""
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(call_quote, ctx, SYMBOLS[i % len(SYMBOLS)]) for i in range(n)]
            results = [f.result() for f in as_completed(futs)]
        wall = time.perf_counter() - t0
    finally:
        ctx.close()
    report(f"Test 2: 共享 ctx 并发 (workers={workers}, n={n})", results, wall)


def test_concurrent_isolated_ctx(n: int = 20, workers: int = 8):
    """Test 3: 独立 ctx 并发（stock-quant 当前模式 — 每次 with-block 创建新 ctx）。"""
    def task(symbol):
        ctx = OpenQuoteContext(host=HOST, port=PORT)
        try:
            return call_quote(ctx, symbol)
        finally:
            ctx.close()

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(task, SYMBOLS[i % len(SYMBOLS)]) for i in range(n)]
        results = [f.result() for f in as_completed(futs)]
    wall = time.perf_counter() - t0
    report(f"Test 3: 独立 ctx 并发 (workers={workers}, n={n})", results, wall)


def test_concurrent_option_chain(n: int = 8, workers: int = 4):
    """Test 4: 期权链并发（最重负载，模拟 daily_brief 实战）。"""
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(call_option_chain, ctx, SYMBOLS[i % len(SYMBOLS)]) for i in range(n)]
            results = [f.result() for f in as_completed(futs)]
        wall = time.perf_counter() - t0
    finally:
        ctx.close()
    report(f"Test 4: 期权链并发 (workers={workers}, n={n})", results, wall)


def test_concurrent_mixed():
    """Test 5: 混合负载（quote + option_chain 同时打），模拟 4 个 MCP tool 并发调用。"""
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        t0 = time.perf_counter()
        results = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = []
            for sym in SYMBOLS[:4]:
                futs.append(ex.submit(call_quote, ctx, sym))
                futs.append(ex.submit(call_option_chain, ctx, sym))
            results = [f.result() for f in as_completed(futs)]
        wall = time.perf_counter() - t0
    finally:
        ctx.close()
    report("Test 5: 混合负载 (quote + option_chain x 4)", results, wall)


def main():
    print("=" * 70)
    print(f"OpenD 并发压测 @ {HOST}:{PORT}")
    print("=" * 70)

    # 先 warmup
    try:
        ctx = OpenQuoteContext(host=HOST, port=PORT)
        call_quote(ctx, "US.NVDA")
        ctx.close()
        print("✓ warmup ok\n")
    except Exception as e:
        print(f"✗ warmup failed: {e}")
        print("→ 请检查 FutuOpenD 是否运行在 11111 端口")
        return

    test_serial_shared_ctx(n=20)
    test_concurrent_shared_ctx(n=20, workers=2)
    test_concurrent_shared_ctx(n=20, workers=4)
    test_concurrent_shared_ctx(n=20, workers=8)
    test_concurrent_isolated_ctx(n=20, workers=4)
    test_concurrent_isolated_ctx(n=20, workers=8)
    test_concurrent_option_chain(n=8, workers=4)
    test_concurrent_mixed()


if __name__ == "__main__":
    main()
