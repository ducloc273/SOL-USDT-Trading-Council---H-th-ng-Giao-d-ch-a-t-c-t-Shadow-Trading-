import sys
import math
import time
import bisect
import asyncio
import argparse
import datetime
import httpx
import scanner
from scanner import prepare, fmt, INTERVAL_MS
from fill import load_settings

# ---------- Tham số của quy tắc: CHỐT TRƯỚC khi chạy backtest, đừng chỉnh theo kết quả ----------
K = 5                    # số nến nhìn lại để tìm nhịp hồi
PIVOT = 5                # số nến mỗi bên để xác nhận đáy xoay
SUPPORT_LOOKBACK = 120   # tìm hỗ trợ trong bấy nhiêu nến gần nhất
SLOPE_BARS = 10          # MA50 phải cao hơn so với bấy nhiêu nến trước
MA_TOUCH_UP = 0.5        # đáy nhịp hồi <= MA20 cộng 0.5 ATR
MA_TOUCH_DOWN = 1.0      # và >= MA20 trừ 1.0 ATR
SUP_BELOW = 0.3          # đáy nhịp hồi >= hỗ trợ trừ 0.3 ATR
SUP_ABOVE = 0.5          # và <= hỗ trợ cộng 0.5 ATR
RSI_MIN_LO, RSI_MIN_HI = 40.0, 50.0
RSI_MAX_NOW = 60.0
STOP_BUFFER = 0.2        # dừng lỗ dưới đáy nhịp hồi 0.2 ATR
MIN_STOP_ATR, MAX_STOP_ATR = 0.5, 3.0
RR = 2.0
HOLD_BARS = 30
SETUP_NAME = "pullback4h"

# Ngưỡng quyết định, cũng chốt trước khi chạy
GO_MIN_EXPECT = 0.05     # kỳ vọng ròng tối thiểu (đơn vị R) ở CẢ phần huấn luyện và phần kiểm tra
GO_MIN_BARS = 30         # số nến tín hiệu khác nhau tối thiểu ở mỗi phần


def prepare_full(rows):
    d = prepare(rows)
    d["o"] = [float(x[1]) for x in rows]
    d["t"] = [int(x[0]) for x in rows]
    n = len(rows)
    d["piv"] = [
        j for j in range(PIVOT, n - PIVOT)
        if d["l"][j] == min(d["l"][j - PIVOT: j + PIVOT + 1])
    ]
    return d


def pullback_signal(d, i):
    """Kiểm tra tín hiệu ở nến i, chỉ dùng dữ liệu tới nến i. Trả về dict hoặc None."""
    if i < 130:
        return None
    s20, s50, a = d["sma20"][i], d["sma50"][i], d["atr"][i]
    s50_prev = d["sma50"][i - SLOPE_BARS]
    if None in (s20, s50, a, s50_prev) or a <= 0:
        return None
    c, o = d["c"][i], d["o"][i]
    if not (c > s50 and s20 > s50 and s50 > s50_prev and c > o):
        return None

    idx = range(i - K + 1, i + 1)
    for m in idx:
        if d["sma50"][m] is None or d["c"][m] <= d["sma50"][m]:
            return None  # đóng dưới MA50 trong nhịp hồi: xu hướng bị phá

    rsi = d["rsi"]
    rs = [rsi[m] for m in idx]
    if any(x is None for x in rs) or rsi[i - 1] is None:
        return None
    rmin = min(rs)
    if not (RSI_MIN_LO <= rmin <= RSI_MIN_HI):
        return None
    if not (rsi[i] > rsi[i - 1] and rsi[i] >= rmin + 2 and rsi[i] <= RSI_MAX_NOW):
        return None

    j0 = min(idx, key=lambda m: d["l"][m])
    pl = d["l"][j0]
    m20, a0 = d["sma20"][j0], d["atr"][j0]
    if m20 is None or a0 is None:
        return None
    why = None
    if (m20 - MA_TOUCH_DOWN * a0) <= pl <= (m20 + MA_TOUCH_UP * a0):
        why = "retest MA20"
    else:
        piv = d["piv"]
        lo_k = bisect.bisect_left(piv, i - SUPPORT_LOOKBACK)
        hi_k = bisect.bisect_right(piv, i - PIVOT)
        for j in piv[lo_k:hi_k]:
            s = d["l"][j]
            if s < c and (s - SUP_BELOW * a) <= pl <= (s + SUP_ABOVE * a):
                why = f"retest hỗ trợ {fmt(s)}"
                break
    if why is None:
        return None

    entry = c
    stop = pl - STOP_BUFFER * a
    risk_u = entry - stop
    if risk_u < MIN_STOP_ATR * a or risk_u > MAX_STOP_ATR * a:
        return None
    if risk_u / entry < scanner.MIN_STOP_MULT * 2 * scanner.COST_PER_SIDE:
        return None
    return {
        "entry": entry, "stop": stop, "target": entry + RR * risk_u,
        "risk_u": risk_u, "rsi": rsi[i], "rsi_min": rmin, "why": why,
    }



def pullback_sweep_signal(d, i):
    """V2: original pullback plus a previously-confirmed bullish liquidity sweep."""
    from liquidity import bullish_sweep
    base = pullback_signal(d, i)
    if base is None:
        return None
    sweep = bullish_sweep(d, i, pivot_confirm=PIVOT)
    if sweep is None:
        return None
    return {**base, "setup_version": "v2", "sweep": sweep}


def simulate(d, lo, hi):
    """Mô phỏng các lệnh có tín hiệu trong khoảng nến từ lo đến hi. Mọi lệnh kết thúc trong khoảng này.
    Kết quả tính bằng R, đã trừ chi phí. Dừng lỗ và mục tiêu cùng nằm trong một nến thì tính là thua."""
    trades = []
    i = max(lo, 130)
    while i < hi - HOLD_BARS - 1:
        sig = pullback_signal(d, i)
        if not sig:
            i += 1
            continue
        entry, stop, target, risk_u = sig["entry"], sig["stop"], sig["target"], sig["risk_u"]
        cost_r = 2 * scanner.COST_PER_SIDE * entry / risk_u
        end = i + HOLD_BARS
        exit_idx, r, hit = end, None, False
        for j in range(i + 1, end + 1):
            if d["l"][j] <= stop:
                r, exit_idx = -1 - cost_r, j
                break
            if d["h"][j] >= target:
                r, exit_idx, hit = RR - cost_r, j, True
                break
        if r is None:
            r = (d["c"][end] - entry) / risk_u - cost_r
        trades.append((d["t"][i], r, hit))
        i = exit_idx + 1
    return trades


def summarize(trades, weeks):
    n = len(trades)
    if n == 0:
        return {"n": 0, "per_week": 0.0, "bars": 0, "bar_mean": None}
    by = {}
    for t, r, _hit, _sym in trades:
        by.setdefault(t, []).append(r)
    means = [sum(v) / len(v) for v in by.values()]
    nb = len(means)
    bm = sum(means) / nb
    low = None
    if nb >= 2:
        var = sum((x - bm) ** 2 for x in means) / (nb - 1)
        low = bm - 1.96 * math.sqrt(var / nb)
    return {
        "n": n, "per_week": n / weeks,
        "hit": sum(1 for x in trades if x[2]) / n,
        "mean": sum(x[1] for x in trades) / n,
        "bars": nb, "bar_mean": bm, "bar_low": low,
        "clustered": sum(len(v) for v in by.values() if len(v) >= 3) / n,
    }


def line(name, s):
    if not s["n"]:
        return f"{name}: không có lệnh nào"
    low = "?" if s["bar_low"] is None else f"{s['bar_low']:+.2f}R"
    return (
        f"{name}: {s['n']} lệnh ({s['per_week']:.1f} lệnh mỗi tuần), chạm mục tiêu {s['hit'] * 100:.0f}%, "
        f"{s['bars']} nến tín hiệu khác nhau, {s['clustered'] * 100:.0f}% lệnh nằm trong nến có từ 3 mã trở lên cùng tín hiệu.\n"
        f"    Kỳ vọng mỗi lệnh {s['mean']:+.3f}R. Kỳ vọng sau khi gộp các mã cùng nến {s['bar_mean']:+.3f}R "
        f"(cận dưới 95%: {low})."
    )


def configure(a):
    st = load_settings()
    fee = a.fee if a.fee is not None else st["fee_pct"]
    slip = a.slip if a.slip is not None else st["slip_pct"]
    scanner.INTERVAL = a.interval
    scanner.HISTORY_DAYS = a.days
    scanner.TOP_BY_VOLUME = a.top
    scanner.MIN_QUOTE_VOLUME = a.minvol * 1e6
    scanner.FEE_PCT, scanner.SLIP_PCT = fee, slip
    scanner.COST_PER_SIDE = (fee + slip) / 100
    scanner.MIN_STOP_MULT = 3.0
    return fee, slip


async def analyze_pair(client, sem, base, symbol, split_frac):
    async with sem:
        try:
            rows = await scanner.load_klines(client, symbol)
        except Exception as e:
            return {"base": base, "error": f"{symbol}: {e}"}
    if len(rows) < 300:
        return {"base": base, "skip": True}
    d = prepare_full(rows)
    n = len(rows)
    split = int(n * split_frac)
    return {"base": base, "train": simulate(d, 0, split), "test": simulate(d, split, n)}


def gather_trades(results, key):
    out = []
    for r in results:
        for t, rr, hit in r.get(key, []):
            out.append((t, rr, hit, r["base"]))
    return out


async def run_backtest(a):
    fee, slip = configure(a)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "council-pullback/1.0"}) as client:
        try:
            uni, total, passing = await scanner.universe(client)
        except Exception as e:
            print(f"Không lấy được danh sách từ Binance: {e}")
            return
        print(
            f"Binance có {total} cặp USDT, {passing} cặp có khối lượng 24h từ {a.minvol:g} triệu USDT trở lên. "
            f"Quét {len(uni)} cặp, khung {a.interval}, lịch sử {a.days} ngày, chia {a.split * 100:.0f}/{100 - a.split * 100:.0f} "
            f"(huấn luyện/kiểm tra).\nChi phí giả định: {fee + slip:.2f}% mỗi chiều. "
            f"Quy tắc {SETUP_NAME}: chốt trước, không chỉnh theo kết quả.\n",
            flush=True,
        )
        sem = asyncio.Semaphore(4)
        results = await asyncio.gather(
            *(analyze_pair(client, sem, b, s, a.split) for b, s, _ in uni)
        )

    errors = [r["error"] for r in results if "error" in r]
    used = [r for r in results if "train" in r]
    if errors:
        print(f"Lỗi {len(errors)} cặp, ví dụ: {errors[0]}")
    print(f"Số cặp đủ dữ liệu: {len(used)}.\n")

    tr = summarize(gather_trades(used, "train"), a.days * a.split / 7)
    te = summarize(gather_trades(used, "test"), a.days * (1 - a.split) / 7)
    print(line("Huấn luyện", tr))
    print(line("Kiểm tra  ", te))

    both = pos = 0
    for r in used:
        if len(r["train"]) >= 5 and len(r["test"]) >= 5:
            both += 1
            if sum(x[1] for x in r["train"]) > 0 and sum(x[1] for x in r["test"]) > 0:
                pos += 1
    print(f"\nTrong {both} cặp có từ 5 lệnh ở mỗi phần, {pos} cặp có kỳ vọng dương ở cả hai phần.")

    total_week = tr["per_week"] * a.split + te["per_week"] * (1 - a.split)
    print(f"Tần suất tín hiệu ước tính trên {len(used)} cặp: khoảng {total_week:.1f} lệnh mỗi tuần (mục tiêu của bạn: 5 đến 10).")

    reasons = []
    for name, s in (("huấn luyện", tr), ("kiểm tra", te)):
        if s["bar_mean"] is None or s["bar_mean"] <= GO_MIN_EXPECT:
            reasons.append(f"kỳ vọng gộp ở phần {name} không vượt {GO_MIN_EXPECT:+.2f}R")
        if s["bars"] < GO_MIN_BARS:
            reasons.append(f"phần {name} chỉ có {s['bars']} nến tín hiệu, dưới {GO_MIN_BARS}")
    print()
    if reasons:
        print("KẾT LUẬN: CHƯA ĐẠT ngưỡng đã chốt trước. Lý do: " + "; ".join(reasons) + ".")
        print("Không nên mất công giả lập quy tắc này. Cũng đừng chỉnh tham số cho tới khi con số đẹp lên: "
              "làm vậy là chỉnh theo dữ liệu, và kết quả mới sẽ không còn đáng tin.")
    else:
        print("KẾT LUẬN: ĐẠT ngưỡng đã chốt trước ở cả hai phần. Đây chưa phải bằng chứng quy tắc có lợi thế "
              "(chưa tính trượt giá thật, chỉ xét mã đang niêm yết hôm nay), nhưng đủ để đáng giả lập.")


async def run_scan(a):
    fee, slip = configure(a)
    hold_hours = HOLD_BARS * INTERVAL_MS[a.interval] / 3_600_000
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "council-pullback/1.0"}) as client:
        try:
            uni, total, passing = await scanner.universe(client)
        except Exception as e:
            print(f"Không lấy được danh sách từ Binance: {e}")
            return
        sem = asyncio.Semaphore(4)

        async def one(base, symbol):
            async with sem:
                try:
                    rows = await scanner.load_klines(client, symbol)
                except Exception as e:
                    return {"error": f"{symbol}: {e}"}
            if len(rows) < 200:
                return None
            d = prepare_full(rows)
            i = len(rows) - 1
            sig = pullback_signal(d, i)
            if not sig:
                return None
            sig.update(base=base, bar=d["t"][i])
            return sig

        results = await asyncio.gather(*(one(b, s) for b, s, _ in uni))

    sigs = [r for r in results if r and "base" in r]
    errors = [r["error"] for r in results if r and "error" in r]
    when = datetime.datetime.now().strftime("%d/%m %H:%M")
    print(f"Quét {len(uni)} cặp lúc {when}, khung {a.interval}, nến đã đóng gần nhất.")
    if errors:
        print(f"Lỗi {len(errors)} cặp, ví dụ: {errors[0]}")
    if not sigs:
        print("Không có tín hiệu pullback nào lúc này.")
        return
    print(f"Có {len(sigs)} tín hiệu (nhớ rằng các mã thường điều chỉnh cùng lúc, nên chúng không độc lập):\n")
    for s in sigs:
        cut = (s["entry"] - s["stop"]) / s["entry"] * 100
        print(
            f"- {s['base']}/USDT: vào {fmt(s['entry'])}, dừng lỗ {fmt(s['stop'])} (cách {cut:.2f}%), "
            f"mục tiêu {fmt(s['target'])}. {s['why']}, RSI thấp nhất {s['rsi_min']:.0f} rồi lên {s['rsi']:.0f}.\n"
            f"  Lệnh mở giả lập: /open {s['base']} --setup {SETUP_NAME} --entry {fmt(s['entry'])} "
            f"--stop {fmt(s['stop'])} --target {fmt(s['target'])} --hold {hold_hours:g}"
        )
    print("\nNhớ: thiết lập chỉ có giá trị khi bạn mở MỌI tín hiệu, không chọn lọc lệnh trông đẹp.")


def main():
    ap = argparse.ArgumentParser(description="Kiểm tra và quét quy tắc pullback (chỉ đọc dữ liệu, không đặt lệnh).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("backtest", "kiểm tra quy tắc trên lịch sử, chia huấn luyện và kiểm tra theo thời gian"),
        ("scan", "tìm tín hiệu ở nến đã đóng gần nhất"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--interval", default="4h", choices=list(INTERVAL_MS), help="khung nến")
        p.add_argument("--days", type=int, default=730, help="số ngày lịch sử")
        p.add_argument("--top", type=int, default=40, help="số cặp quét, theo khối lượng")
        p.add_argument("--minvol", type=float, default=5, help="khối lượng 24h tối thiểu, đơn vị triệu USDT")
        p.add_argument("--fee", type=float, default=None, help="phí mỗi chiều, theo phần trăm (mặc định lấy từ settings.json)")
        p.add_argument("--slip", type=float, default=None, help="trượt giá mỗi chiều, theo phần trăm (mặc định lấy từ settings.json)")
        p.add_argument("--split", type=float, default=0.7, help="tỷ lệ dữ liệu cho huấn luyện")
    a = ap.parse_args()
    a.split = min(0.9, max(0.5, a.split))
    if a.cmd == "backtest":
        asyncio.run(run_backtest(a))
    else:
        asyncio.run(run_scan(a))


if __name__ == "__main__":
    main()
