import os
import json
import time
import random
import bisect
import asyncio
import argparse
import datetime
import httpx
import scanner
import pullback
from autoshadow import STRATEGY_VERSION, signal_key
from scanner import fmt

INTERVAL = "4h"
STEP_MS = scanner.INTERVAL_MS[INTERVAL]
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "autoshadow_state.json")


def configure():
    ns = argparse.Namespace(fee=None, slip=None, interval=INTERVAL, days=730, top=40, minvol=5.0)
    pullback.configure(ns)


def new_client():
    return httpx.AsyncClient(timeout=30, headers={"User-Agent": "council-diagnose/1.0"})


def local(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%d/%m %H:%M")


def parse_local(text):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp() * 1000


def index_at(rows, at_ms):
    """Nến đã đóng gần nhất tại hoặc trước thời điểm at_ms."""
    for i in range(len(rows) - 1, -1, -1):
        if rows[i][0] + STEP_MS <= at_ms:
            return i
    return None


# ---------- Các điều kiện của quy tắc, viết lại để in số liệu ----------

def gates(d, i):
    G = []

    def add(name, ok, detail):
        G.append({"name": name, "ok": bool(ok), "detail": detail})

    P = pullback
    if i < 130:
        add("0. Đủ dữ liệu (từ nến thứ 131)", False, f"chỉ có {i + 1} nến")
        return G
    s20, s50, a = d["sma20"][i], d["sma50"][i], d["atr"][i]
    sp = d["sma50"][i - P.SLOPE_BARS]
    if None in (s20, s50, a, sp) or a <= 0:
        add("0. Chỉ báo tính được", False, "thiếu MA hoặc ATR")
        return G
    c, o = d["c"][i], d["o"][i]
    add(
        "1. Xu hướng tăng (giá trên MA50, MA20 trên MA50, MA50 dốc lên)",
        c > s50 and s20 > s50 and s50 > sp,
        f"giá {fmt(c)}, MA20 {fmt(s20)}, MA50 {fmt(s50)}, MA50 cách đây {P.SLOPE_BARS} nến là {fmt(sp)}",
    )
    add("2. Nến tín hiệu là nến xanh", c > o, f"mở {fmt(o)}, đóng {fmt(c)}")
    idx = list(range(i - P.K + 1, i + 1))
    below = [m for m in idx if d["sma50"][m] is None or d["c"][m] <= d["sma50"][m]]
    add(
        f"3. Không nến nào trong {P.K} nến gần nhất đóng dưới MA50",
        not below, f"{len(below)} nến đóng dưới MA50",
    )
    rsi = d["rsi"]
    rs = [rsi[m] for m in idx]
    if any(x is None for x in rs) or rsi[i - 1] is None:
        add("4. RSI", False, "thiếu giá trị RSI")
        return G
    rmin = min(rs)
    add(
        f"4. RSI thấp nhất của {P.K} nến nằm trong khoảng {P.RSI_MIN_LO:g} đến {P.RSI_MIN_HI:g}",
        P.RSI_MIN_LO <= rmin <= P.RSI_MIN_HI, f"RSI thấp nhất {rmin:.1f}",
    )
    add(
        f"5. RSI đang đi lên (hơn nến trước, hơn đáy ít nhất 2 điểm, không quá {P.RSI_MAX_NOW:g})",
        rsi[i] > rsi[i - 1] and rsi[i] >= rmin + 2 and rsi[i] <= P.RSI_MAX_NOW,
        f"RSI hiện tại {rsi[i]:.1f}, nến trước {rsi[i - 1]:.1f}, đáy {rmin:.1f}",
    )
    j0 = min(idx, key=lambda m: d["l"][m])
    pl = d["l"][j0]
    m20, a0 = d["sma20"][j0], d["atr"][j0]
    why, detail = None, f"đáy nhịp hồi {fmt(pl)}"
    if m20 is not None and a0 is not None:
        lo_b, hi_b = m20 - P.MA_TOUCH_DOWN * a0, m20 + P.MA_TOUCH_UP * a0
        detail += f"; vùng retest MA20 từ {fmt(lo_b)} đến {fmt(hi_b)}"
        if lo_b <= pl <= hi_b:
            why = "retest MA20"
        else:
            piv = d["piv"]
            lo_k = bisect.bisect_left(piv, i - P.SUPPORT_LOOKBACK)
            hi_k = bisect.bisect_right(piv, i - P.PIVOT)
            near = []
            for j in piv[lo_k:hi_k]:
                s = d["l"][j]
                if s < c:
                    dist = (pl - s) / a
                    near.append((abs(dist), s, dist))
                    if (s - P.SUP_BELOW * a) <= pl <= (s + P.SUP_ABOVE * a):
                        why = f"retest hỗ trợ {fmt(s)}"
                        break
            if why is None:
                near.sort()
                if near:
                    parts = [f"{fmt(s)} (đáy nhịp hồi cách {dist:+.2f} ATR)" for _, s, dist in near[:2]]
                    detail += (
                        "; hỗ trợ xoay gần nhất: " + ", ".join(parts)
                        + f"; cần từ {-P.SUP_BELOW:+.1f} đến {P.SUP_ABOVE:+.1f} ATR"
                    )
                else:
                    detail += "; không có hỗ trợ xoay nào đã xác nhận dưới giá trong khoảng tìm kiếm"
    else:
        detail += "; thiếu MA20 hoặc ATR tại đáy"
    add("6. Đáy nhịp hồi chạm MA20 hoặc hỗ trợ cứng", why is not None, detail + (f" => {why}" if why else ""))
    stop = pl - P.STOP_BUFFER * a
    risk_u = c - stop
    min_pct = scanner.MIN_STOP_MULT * 2 * scanner.COST_PER_SIDE * 100
    add(
        f"7. Khoảng dừng lỗ hợp lệ ({P.MIN_STOP_ATR:g} đến {P.MAX_STOP_ATR:g} ATR và từ {min_pct:.2f}% giá vào)",
        P.MIN_STOP_ATR * a <= risk_u <= P.MAX_STOP_ATR * a and risk_u / c >= min_pct / 100,
        f"khoảng dừng {risk_u / a:.2f} ATR, tức {risk_u / c * 100:.2f}% giá vào",
    )
    return G


def show_gates(G):
    for g in G:
        print(f"  [{'ĐẠT ' if g['ok'] else 'KHÔNG'}] {g['name']}\n          {g['detail']}")


def norm_base(s):
    s = s.upper().replace("/", "").replace("-", "")
    return s[:-4] if s.endswith("USDT") else s


# ---------- Lệnh why ----------

async def cmd_why(a):
    configure()
    base = norm_base(a.symbol)
    async with new_client() as client:
        rows = await scanner.load_klines(client, base + "USDT")
    d = pullback.prepare_full(rows)
    if a.at:
        i = index_at(rows, parse_local(a.at))
        if i is None:
            print("Không có nến nào trước thời điểm đó trong dữ liệu đã tải.")
            return
    else:
        i = len(rows) - 1
    t0 = rows[i][0]
    print(f"{base}/USDT, nến 4h mở lúc {local(t0)}, đóng lúc {local(t0 + STEP_MS)} (giờ máy):")
    G = gates(d, i)
    show_gates(G)
    sig = pullback.pullback_signal(d, i)
    print("\nKết luận của code: " + ("CÓ TÍN HIỆU" if sig else "không có tín hiệu"))
    if sig:
        print(f"  vào {fmt(sig['entry'])}, dừng lỗ {fmt(sig['stop'])}, mục tiêu {fmt(sig['target'])}. {sig['why']}.")
    if bool(G) and all(g["ok"] for g in G) != (sig is not None):
        print("CẢNH BÁO: phần chẩn đoán này lệch với hàm pullback_signal. Hãy báo lại, đừng tin chẩn đoán.")


# ---------- Lệnh funnel ----------

async def cmd_funnel(a):
    configure()
    async with new_client() as client:
        uni, total, passing = await scanner.universe(client)
        sem = asyncio.Semaphore(4)

        async def one(base, symbol):
            async with sem:
                try:
                    rows = await scanner.load_klines(client, symbol)
                except Exception as e:
                    return base, None, str(e)
            if len(rows) < 200:
                return base, None, "ít dữ liệu"
            return base, gates(pullback.prepare_full(rows), len(rows) - 1), ""

        results = await asyncio.gather(*(one(b, s) for b, s, _ in uni))
    stats, near, n_ok, n_used, bad = {}, [], 0, 0, 0
    for base, G, err in results:
        if G is None:
            bad += 1
            continue
        n_used += 1
        fails = [g for g in G if not g["ok"]]
        if not fails:
            n_ok += 1
            continue
        stats[fails[0]["name"]] = stats.get(fails[0]["name"], 0) + 1
        if len(fails) == 1:
            near.append((base, fails[0]))
    print(f"Quét {n_used} cặp ở nến 4h đã đóng gần nhất ({bad} cặp bị bỏ vì thiếu dữ liệu). Có tín hiệu: {n_ok}.\n")
    print("Số cặp bị chặn ở điều kiện đầu tiên không đạt:")
    for name in sorted(stats):
        print(f"  {stats[name]:3d}  {name}")
    print(f"\nCặp chỉ thiếu đúng một điều kiện ({len(near)}):")
    for base, g in near:
        print(f"- {base}: {g['name']}\n    {g['detail']}")
    print("\nNếu một điều kiện chặn gần như mọi cặp ở cả lúc thị trường tăng, hãy kiểm tra điều kiện đó bằng lệnh why.")


# ---------- Lệnh check ----------

async def cmd_check(a):
    configure()
    random.seed(1)
    problems = 0
    async with new_client() as client:
        uni, _, _ = await scanner.universe(client)
        for base, symbol, _ in uni[: a.pairs]:
            try:
                rows = await scanner.load_klines(client, symbol)
            except Exception as e:
                print(f"{base}: không tải được ({e})")
                problems += 1
                continue
            gaps = sum(1 for k in range(1, len(rows)) if rows[k][0] - rows[k - 1][0] != STEP_MS)
            try:
                fresh = await scanner.get_json(
                    client, "/api/v3/klines", {"symbol": symbol, "interval": INTERVAL, "limit": 121}
                )
                fresh = {int(k[0]): float(k[4]) for k in fresh[:-1]}
                cached = {int(k[0]): float(k[4]) for k in rows}
                diff = sum(
                    1 for t, c in fresh.items()
                    if t in cached and abs(cached[t] - c) > 1e-9 * max(1.0, abs(c))
                )
                missing = sum(1 for t in fresh if t not in cached)
            except Exception as e:
                diff, missing = -1, -1
                print(f"{base}: không so được với dữ liệu mới tải ({e})")
            d = pullback.prepare_full(rows)
            n = len(rows)
            lo = max(131, n - 600)
            pos = [i for i in range(lo, n) if pullback.pullback_signal(d, i)]
            pos_set = set(pos)
            pool = [i for i in range(lo, n) if i not in pos_set]
            test = pos[: a.samples] + random.sample(pool, min(a.samples, len(pool)))
            bad = []
            for i in test:
                s1 = pullback.pullback_signal(d, i)
                s2 = pullback.pullback_signal(pullback.prepare_full(rows[: i + 1]), i)
                same = (s1 is None) == (s2 is None) and (
                    s1 is None
                    or (abs(s1["entry"] - s2["entry"]) < 1e-9 and abs(s1["stop"] - s2["stop"]) < 1e-9)
                )
                if not same:
                    bad.append(local(rows[i][0]))
            flag = gaps > 0 or diff > 0 or missing > 0 or bad
            problems += 1 if flag else 0
            print(
                f"{base}: {n} nến, khoảng trống {gaps}, lệch so với dữ liệu mới {diff}, thiếu {missing}; "
                f"kiểm tra {len(test)} nến ({len(pos)} tín hiệu trong 600 nến gần nhất), lệch giữa bản đầy đủ và bản cắt cụt: {len(bad)}"
                + (f" ({', '.join(bad[:3])})" if bad else "")
                + ("  <-- XEM LẠI" if flag else "")
            )
    print("\nTẤT CẢ ĐỀU KHỚP." if problems == 0 else f"\nCó {problems} cặp cần xem lại.")


# ---------- Lệnh replay ----------

async def cmd_replay(a):
    configure()
    processed = set()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                processed = set(json.load(fh).get("processed", []))
        except Exception as e:
            print(f"Không đọc được autoshadow_state.json ({e}).")
    since_ms = parse_local(a.since) if a.since else time.time() * 1000 - a.days * 86400 * 1000
    found = []
    async with new_client() as client:
        uni, _, _ = await scanner.universe(client)
        for base, symbol, _ in uni:
            try:
                rows = await scanner.load_klines(client, symbol)
            except Exception as e:
                print(f"{base}: không tải được ({e})")
                continue
            if len(rows) < 200:
                continue
            d = pullback.prepare_full(rows)
            for i in range(131, len(rows)):
                if rows[i][0] + STEP_MS < since_ms:
                    continue
                if pullback.pullback_signal(d, i):
                    found.append((rows[i][0], base, signal_key(base, rows[i][0]) in processed))
    found.sort()
    print(f"Từ {local(since_ms)} đến nay, quy tắc cho ra {len(found)} tín hiệu trên danh sách 40 cặp hiện tại (phiên bản {STRATEGY_VERSION}):")
    for t, base, ok in found:
        print(f"- {base}, nến mở {local(t)}: {'bot đã xử lý' if ok else 'bot CHƯA xử lý'}")
    miss = sum(1 for _, _, ok in found if not ok)
    if miss:
        print(
            f"\n{miss} tín hiệu chưa được xử lý. Hãy xem bot có tắt vào lúc đó không, "
            f"hoặc cặp đó có nằm trong top 40 vào thời điểm ấy không. Kết quả dựa vào processed state hiện lưu, chỉ giữ 2000 key gần nhất."
        )
    elif found:
        print("\nMọi tín hiệu đều đã được bot xử lý.")
    else:
        print("\nKhông có tín hiệu nào trong khoảng này, khớp với kết quả bot đã báo.")


def main():
    ap = argparse.ArgumentParser(description="Chẩn đoán bộ quét pullback (chỉ đọc dữ liệu).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("why", help="vì sao một cặp có hoặc không có tín hiệu")
    p.add_argument("symbol", help="ví dụ SOL")
    p.add_argument("--at", help='thời điểm theo giờ máy, ví dụ "2026-11-10 08:00" (mặc định là nến mới nhất)')
    sub.add_parser("funnel", help="điều kiện nào chặn nhiều nhất trên 40 cặp hiện tại")
    p = sub.add_parser("check", help="kiểm tra bản quét trực tiếp có khớp bản backtest và dữ liệu có đúng không")
    p.add_argument("--pairs", type=int, default=15, help="số cặp kiểm tra")
    p.add_argument("--samples", type=int, default=15, help="số nến lấy mẫu mỗi nhóm")
    p = sub.add_parser("replay", help="tín hiệu lẽ ra đã có kể từ một thời điểm, so với những gì bot đã xử lý")
    p.add_argument("--since", help='giờ máy, ví dụ "2026-11-15 11:00"')
    p.add_argument("--days", type=float, default=7, help="số ngày nhìn lại nếu không có --since")
    a = ap.parse_args()
    {"why": cmd_why, "funnel": cmd_funnel, "check": cmd_check, "replay": cmd_replay}[a.cmd]
    asyncio.run({"why": cmd_why, "funnel": cmd_funnel, "check": cmd_check, "replay": cmd_replay}[a.cmd](a))


if __name__ == "__main__":
    main()
