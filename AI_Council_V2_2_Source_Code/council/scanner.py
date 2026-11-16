import os
import re
import time
import math
import json
import asyncio
import argparse
import datetime
import httpx
from council import run_council, ALIASES

BASE_URL = "https://api.binance.com"  # nếu bị chặn, thử https://data-api.binance.vision

# Cấu hình (đổi theo nhu cầu, hoặc dùng tham số dòng lệnh)
INTERVAL = "1h"
HISTORY_DAYS = 730              # độ dài lịch sử dùng để backtest
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "klines_cache")
TOP_BY_VOLUME = 40
MIN_QUOTE_VOLUME = 20_000_000   # khối lượng 24h tối thiểu, tính bằng USDT
ACCOUNT = 10000                 # đổi thành vốn thật của bạn
RISK_PCT = 1
ATR_STOP = 1.5
ATR_TARGET = 3.0
HORIZON = 48                    # tối đa số nến chờ chạm mục tiêu hoặc dừng lỗ khi backtest
TRAIN_FRAC = 0.7                # tỷ lệ dữ liệu dùng cho phần huấn luyện (phần còn lại để kiểm tra)
MIN_TRAIN_TRADES = 30
MIN_TEST_TRADES = 10
FEE_PCT = 0.10                  # phí mỗi chiều, đơn vị phần trăm
SLIP_PCT = 0.02                 # trượt giá mỗi chiều, đơn vị phần trăm
MIN_STOP_MULT = 3.0             # dừng lỗ phải xa giá vào ít nhất bấy nhiêu lần chi phí khứ hồi
COST_PER_SIDE = (FEE_PCT + SLIP_PCT) / 100

STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE", "EUR", "EURI",
    "AEUR", "XUSD", "USD1", "PYUSD", "UST", "USTC",
}
TOKENIZED = {  # token bám tài sản khác, cần tìm tin theo tài sản gốc
    "PAXG": ("commodity", "gold, PAXG"),
    "XAUT": ("commodity", "gold, XAUT"),
}
INTERVAL_MS = {
    "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}


# ---------- Chỉ báo ----------

def sma_series(x, n):
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi_series(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def atr_series(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) < n:
        return out
    tr = [h[0] - l[0]] + [
        max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        for i in range(1, len(c))
    ]
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def prepare(k):
    h = [float(x[2]) for x in k]
    l = [float(x[3]) for x in k]
    c = [float(x[4]) for x in k]
    v = [float(x[5]) for x in k]
    return {
        "h": h, "l": l, "c": c, "v": v,
        "sma20": sma_series(c, 20), "sma50": sma_series(c, 50),
        "rsi": rsi_series(c), "atr": atr_series(h, l, c),
        "vol20": sma_series(v, 20),
    }


# ---------- Quy tắc tín hiệu và backtest ----------

def has_signal(d, i):
    if i < 60:
        return False
    s20, s50, r, a = d["sma20"][i], d["sma50"][i], d["rsi"][i], d["atr"][i]
    vavg = d["vol20"][i - 1]
    if None in (s20, s50, r, a, vavg) or a <= 0 or vavg <= 0:
        return False
    c = d["c"][i]
    prior_high = max(d["h"][i - 10:i])
    return (
        c > s50 and s20 > s50 and 50 <= r <= 68
        and c > prior_high and d["v"][i] >= 1.2 * vavg
    )


def stop_ok(entry, atr):
    """Dừng lỗ phải đủ xa so với chi phí khứ hồi, nếu không phí nuốt hết lợi nhuận."""
    return entry > 0 and (ATR_STOP * atr / entry) >= MIN_STOP_MULT * 2 * COST_PER_SIDE


def backtest(d, lo, hi):
    """Backtest trên nến từ lo đến hi. Mọi lệnh phải kết thúc trong khoảng này.
    Kết quả tính bằng R (1R là mức lỗ nếu chạm dừng lỗ), đã trừ chi phí.
    Dừng lỗ và mục tiêu cùng nằm trong một nến thì tính là thua.
    Hết thời gian chờ thì thoát theo giá đóng cửa."""
    rs, hits = [], 0
    i = max(lo, 60)
    while i < hi - HORIZON - 1:
        entry, a = d["c"][i], d["atr"][i]
        if not (has_signal(d, i) and stop_ok(entry, a)):
            i += 1
            continue
        risk_u = ATR_STOP * a
        stop = entry - risk_u
        target = entry + ATR_TARGET * a
        cost_r = 2 * COST_PER_SIDE * entry / risk_u
        end = i + HORIZON
        exit_idx, r, hit = end, None, False
        for j in range(i + 1, end + 1):
            if d["l"][j] <= stop:
                r, exit_idx = -1 - cost_r, j
                break
            if d["h"][j] >= target:
                r, exit_idx, hit = ATR_TARGET / ATR_STOP - cost_r, j, True
                break
        if r is None:
            r = (d["c"][end] - entry) / risk_u - cost_r
        rs.append(r)
        hits += hit
        i = exit_idx + 1
    return rs, hits


def summarize(rs, hits):
    n = len(rs)
    if n == 0:
        return {"n": 0, "hit": None, "mean": None, "low": None}
    mean = sum(rs) / n
    low = None
    if n >= 2:
        var = sum((x - mean) ** 2 for x in rs) / (n - 1)
        low = mean - 1.96 * math.sqrt(var / n)
    return {"n": n, "hit": hits / n, "mean": mean, "low": low}


def stat_txt(s):
    if not s["n"]:
        return "không có lệnh"
    low = "?" if s["low"] is None else f"{s['low']:+.2f}R"
    return (
        f"{s['n']} lệnh, chạm mục tiêu {s['hit'] * 100:.0f}%, "
        f"kỳ vọng {s['mean']:+.2f}R (cận dưới 95%: {low})"
    )


# ---------- Lấy dữ liệu từ Binance (chỉ đọc) ----------

async def get_json(client, path, params=None):
    last = None
    for attempt in range(3):
        r = await client.get(BASE_URL + path, params=params)
        if r.status_code in (418, 429):
            raise RuntimeError(f"Binance giới hạn tốc độ (HTTP {r.status_code}). Đợi vài phút rồi chạy lại.")
        if r.status_code in (403, 451):
            raise RuntimeError(f"Binance chặn truy cập từ IP hoặc khu vực này (HTTP {r.status_code}).")
        if r.status_code == 200:
            return r.json()
        last = r
        await asyncio.sleep(1 + attempt)
    last.raise_for_status()


async def universe(client):
    tickers = await get_json(client, "/api/v3/ticker/24hr")
    out, total = [], 0
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if not base or base in STABLE_BASES:
            continue
        total += 1
        try:
            qv, price = float(t["quoteVolume"]), float(t["lastPrice"])
        except (KeyError, ValueError):
            continue
        if qv >= MIN_QUOTE_VOLUME and price > 0:
            out.append((base, s, qv))
    out.sort(key=lambda x: -x[2])
    return out[:TOP_BY_VOLUME], total, len(out)


async def load_klines(client, symbol):
    """Tải lịch sử nến theo từng đoạn 1.000 nến, lưu vào bộ nhớ đệm để lần sau chỉ tải phần mới."""
    step = INTERVAL_MS[INTERVAL]
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{symbol}_{INTERVAL}.json")
    rows, cached_days = [], 0
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                rows, cached_days = data.get("rows", []), data.get("days", 0)
            elif isinstance(data, list):
                rows, cached_days = data, 730  # bộ nhớ đệm bản cũ, mặc định 730 ngày
        except Exception:
            rows, cached_days = [], 0
    if cached_days < HISTORY_DAYS:
        rows = []  # bộ nhớ đệm không đủ dài, tải lại từ đầu
    now_ms = int(time.time() * 1000)
    start = rows[-1][0] + step if rows else now_ms - HISTORY_DAYS * 86_400_000
    while start < now_ms:
        batch = await get_json(
            client, "/api/v3/klines",
            {"symbol": symbol, "interval": INTERVAL, "startTime": start, "limit": 1000},
        )
        if not batch:
            break
        rows += [[int(b[0]), b[1], b[2], b[3], b[4], b[5]] for b in batch]
        start = int(batch[-1][0]) + step
        if len(batch) < 1000:
            break
        await asyncio.sleep(0.05)
    seen, clean = set(), []
    cutoff = now_ms - HISTORY_DAYS * 86_400_000
    for r in rows:
        if r[0] in seen or r[0] + step > now_ms or r[0] < cutoff:
            continue  # bỏ nến trùng, nến chưa đóng, nến quá cũ
        seen.add(r[0])
        clean.append(r)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"days": HISTORY_DAYS, "rows": clean}, fh)
    return clean


async def analyze(client, sem, base, symbol):
    async with sem:
        try:
            k = await load_klines(client, symbol)
        except Exception as e:
            return {"base": base, "error": f"{symbol}: {e}"}
    if len(k) < 300:
        return {"base": base, "reason": "ít dữ liệu"}
    d = prepare(k)
    n = len(k)
    split = int(n * TRAIN_FRAC)
    tr_r, tr_h = backtest(d, 0, split)
    te_r, te_h = backtest(d, split, n)
    train, test = summarize(tr_r, tr_h), summarize(te_r, te_h)
    out = {"base": base, "train": train, "test": test, "tr_r": tr_r, "te_r": te_r}

    if train["n"] < MIN_TRAIN_TRADES or test["n"] < MIN_TEST_TRADES:
        out["reason"] = "ít lệnh backtest"
        return out
    if train["mean"] <= 0 or test["mean"] <= 0:
        out["reason"] = "kỳ vọng không dương ở cả hai phần"
        return out
    i = n - 1
    if not has_signal(d, i):
        out["reason"] = "không có tín hiệu"
        return out
    entry, a = d["c"][i], d["atr"][i]
    if not stop_ok(entry, a):
        out["reason"] = "dừng lỗ hiện tại quá gần so với chi phí"
        return out
    stop, target = entry - ATR_STOP * a, entry + ATR_TARGET * a
    if stop <= 0:
        out["reason"] = "mức dừng lỗ không hợp lệ"
        return out
    out.update({
        "entry": entry, "stop": stop, "target": target,
        "stop_pct": ATR_STOP * a / entry * 100,
        "status": "PASS" if (test["low"] is not None and test["low"] > 0) else "WEAK",
        "rsi": d["rsi"][i], "volr": d["v"][i] / d["vol20"][i - 1],
        "s20": d["sma20"][i], "s50": d["sma50"][i],
        "sup": min(d["l"][-20:]), "res": max(d["h"][-100:]),
    })
    return out


# ---------- Soạn câu hỏi cho hội đồng ----------

def fmt(x):
    if x <= 0:
        return "0"
    d = max(2, min(10, 6 - int(math.floor(math.log10(x)))))
    return f"{x:.{d}f}"


def build_question(c):
    base = c["base"]
    market, kw = TOKENIZED.get(base, (None, None))
    if market is None:
        market = "crypto"
        name = ALIASES.get(base, base.lower())
        kw = f"{name}, {base}" if name != base.lower() else base
    res = (
        f"Resistance: {fmt(c['res'])}"
        if c["res"] > c["entry"] * 1.0005
        else "Resistance: none, price is at a 100-bar high"
    )
    t = c["test"]
    return "\n".join([
        f"Asset: {base}/USDT spot",
        f"Market: {market}",
        f"Keywords: {kw}",
        f"Entry: {fmt(c['entry'])}. Stop-loss: {fmt(c['stop'])}. Target: {fmt(c['target'])}",
        f"Account: {ACCOUNT:,.0f} USD. Max risk per trade: {RISK_PCT}%",
        f"Backtest of this exact setup (out-of-sample): {t['n']} trades, win rate {t['hit'] * 100:.0f}%",
        f"Costs assumed: {2 * COST_PER_SIDE * 100:.2f}% round trip. "
        f"Out-of-sample expectancy net of costs: {t['mean']:+.2f}R.",
        f"Trend: price above the 50-bar MA ({fmt(c['s50'])}) and the 20-bar MA ({fmt(c['s20'])}) "
        f"is above it ({INTERVAL} bars). RSI(14): {c['rsi']:.0f}. "
        f"Volume: {c['volr']:.1f}x the 20-bar average.",
        f"Support: {fmt(c['sup'])}. {res}",
    ])


async def judge(cands):
    verdicts = []
    for c in cands:
        print(f"\n=== Hội đồng phân tích {c['base']}/USDT ===", flush=True)
        pieces = []
        async for p in run_council(build_question(c)):
            pieces.append(p)
        final = next((x for x in reversed(pieces) if x.startswith("---\n**■")), "")
        text = final.replace("**", "").strip()
        print(text, flush=True)
        m = re.search(r"KẾT LUẬN:\s*([A-Z_]+)", text)
        verdicts.append((c["base"], m.group(1) if m else "?"))
    return verdicts


# ---------- Chạy ----------

async def scan():
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "council-scanner/2.0"}) as client:
        try:
            uni, total, passing = await universe(client)
        except Exception as e:
            print(f"Không lấy được danh sách từ Binance: {e}")
            return []
        print(
            f"Binance có {total} cặp USDT (đã bỏ stablecoin), {passing} cặp có khối lượng 24h "
            f"từ {MIN_QUOTE_VOLUME / 1e6:.0f} triệu USDT trở lên.\n"
            f"Quét {len(uni)} cặp đầu bảng, khung {INTERVAL}, lịch sử {HISTORY_DAYS} ngày, "
            f"chia {TRAIN_FRAC * 100:.0f}/{100 - TRAIN_FRAC * 100:.0f} theo thời gian "
            f"(huấn luyện/kiểm tra).\n"
            f"Chi phí giả định: {FEE_PCT + SLIP_PCT:.2f}% mỗi chiều, {2 * (FEE_PCT + SLIP_PCT):.2f}% khứ hồi. "
            f"Dừng lỗ tối thiểu: {MIN_STOP_MULT * 2 * (FEE_PCT + SLIP_PCT):.2f}% cách giá vào.\n"
            f"(Lần đầu tải sẽ chậm hơn vì phải tải toàn bộ lịch sử.)",
            flush=True,
        )
        sem = asyncio.Semaphore(4)
        results = await asyncio.gather(*(analyze(client, sem, b, s) for b, s, _ in uni))

    errors = [r["error"] for r in results if "error" in r]
    skips = {}
    for r in results:
        if "reason" in r:
            skips[r["reason"]] = skips.get(r["reason"], 0) + 1
    cands = [r for r in results if "status" in r]
    cands.sort(key=lambda r: (r["status"] != "PASS", -r["test"]["mean"]))

    # Thống kê tổng hợp trên mọi mã đủ lệnh, kể cả mã không có tín hiệu lúc này
    ev = [r for r in results if "train" in r and r.get("reason") != "ít lệnh backtest"]
    pos_train = [r for r in ev if r["train"]["mean"] > 0]
    pos_both = [r for r in pos_train if r["test"]["mean"] > 0]
    pool_tr = [x for r in ev for x in r["tr_r"]]
    pool_te = [x for r in ev for x in r["te_r"]]

    print(f"\nMã đủ lệnh để đánh giá: {len(ev)}. Bỏ qua: {skips or 'không có'}.")
    if ev:
        print(f"Kỳ vọng gộp mọi mã: huấn luyện {sum(pool_tr) / len(pool_tr):+.3f}R "
              f"({len(pool_tr)} lệnh), kiểm tra {sum(pool_te) / max(len(pool_te), 1):+.3f}R "
              f"({len(pool_te)} lệnh).")
        print(f"Mã có kỳ vọng dương ở huấn luyện: {len(pos_train)}. "
              f"Trong số đó còn dương ở kiểm tra: {len(pos_both)}.")
        if pos_train:
            ratio = len(pos_both) / len(pos_train)
            print(f"Tỷ lệ giữ được: {ratio * 100:.0f}%. Gần 50% nghĩa là phần huấn luyện gần như "
                  f"không dự đoán được phần kiểm tra (chỉ là nhiễu).")
    if errors:
        print(f"Lỗi {len(errors)} cặp, ví dụ: {errors[0]}")
    return cands


def show(cands):
    if not cands:
        print("\nKhông có mã nào vừa có tín hiệu lúc này vừa có kỳ vọng dương ở cả hai phần. "
              "Không vào lệnh cũng là một kết quả hợp lệ.")
        return
    print("\nỨng viên (PASS: cận dưới 95% ở phần kiểm tra vẫn dương. WEAK: kỳ vọng dương nhưng chưa chắc chắn):")
    for n, c in enumerate(cands, 1):
        print(
            f"{n:2d}. [{c['status']}] {c['base']}/USDT  vào {fmt(c['entry'])}  "
            f"dừng lỗ {fmt(c['stop'])} (cách {c['stop_pct']:.2f}%)  mục tiêu {fmt(c['target'])}\n"
            f"      Huấn luyện: {stat_txt(c['train'])}\n"
            f"      Kiểm tra:   {stat_txt(c['test'])}\n"
            f"      RSI {c['rsi']:.0f}, khối lượng {c['volr']:.1f}x"
        )
    print(
        "\nNhắc: (1) tự kiểm tra sự kiện sắp tới (Fed, mở khóa token, niêm yết). "
        "(2) Khi thử nhiều mã, một vài mã dương chỉ do may mắn. "
        "(3) Phần kiểm tra chỉ có ý nghĩa nếu bạn không sửa quy tắc sau khi đã xem nó."
    )


def main():
    global INTERVAL, TOP_BY_VOLUME, ACCOUNT, RISK_PCT, HISTORY_DAYS, MIN_QUOTE_VOLUME
    global MIN_TRAIN_TRADES, MIN_TEST_TRADES, FEE_PCT, SLIP_PCT, COST_PER_SIDE, TRAIN_FRAC, MIN_STOP_MULT
    ap = argparse.ArgumentParser(description="Quét các cặp /USDT trên Binance (chỉ đọc dữ liệu).")
    ap.add_argument("--council", type=int, default=0, help="số ứng viên đầu bảng gửi cho hội đồng")
    ap.add_argument("--interval", default=INTERVAL, help="khung nến, ví dụ 1h hoặc 4h")
    ap.add_argument("--top", type=int, default=TOP_BY_VOLUME, help="số cặp quét, theo khối lượng")
    ap.add_argument("--account", type=float, default=ACCOUNT, help="vốn (USD)")
    ap.add_argument("--risk", type=float, default=RISK_PCT, help="rủi ro tối đa mỗi lệnh, tính theo phần trăm")
    ap.add_argument("--days", type=int, default=HISTORY_DAYS, help="số ngày lịch sử dùng để backtest")
    ap.add_argument("--minvol", type=float, default=MIN_QUOTE_VOLUME / 1e6,
                    help="khối lượng 24h tối thiểu, đơn vị triệu USDT")
    ap.add_argument("--minbt", type=int, default=MIN_TRAIN_TRADES, help="số lệnh tối thiểu ở phần huấn luyện")
    ap.add_argument("--mintest", type=int, default=MIN_TEST_TRADES, help="số lệnh tối thiểu ở phần kiểm tra")
    ap.add_argument("--fee", type=float, default=FEE_PCT, help="phí mỗi chiều, đơn vị phần trăm")
    ap.add_argument("--slip", type=float, default=SLIP_PCT, help="trượt giá mỗi chiều, đơn vị phần trăm")
    ap.add_argument("--split", type=float, default=TRAIN_FRAC, help="tỷ lệ dữ liệu cho huấn luyện, từ 0.5 đến 0.9")
    ap.add_argument("--minstop", type=float, default=MIN_STOP_MULT,
                    help="dừng lỗ phải xa giá vào bao nhiêu lần chi phí khứ hồi")
    args = ap.parse_args()

    INTERVAL, TOP_BY_VOLUME, ACCOUNT, RISK_PCT = args.interval, args.top, args.account, args.risk
    HISTORY_DAYS, MIN_QUOTE_VOLUME = args.days, args.minvol * 1e6
    MIN_TRAIN_TRADES, MIN_TEST_TRADES = args.minbt, args.mintest
    FEE_PCT, SLIP_PCT = args.fee, args.slip
    COST_PER_SIDE = (FEE_PCT + SLIP_PCT) / 100
    TRAIN_FRAC = min(0.9, max(0.5, args.split))
    MIN_STOP_MULT = args.minstop

    if INTERVAL not in INTERVAL_MS:
        print(f"Khung nến {INTERVAL} chưa được hỗ trợ. Dùng một trong: {', '.join(INTERVAL_MS)}")
        return

    cands = asyncio.run(scan())
    show(cands)

    verdicts = []
    if args.council and cands:
        verdicts = asyncio.run(judge(cands[: args.council]))
        print("\nTóm tắt hội đồng: " + ", ".join(f"{b}: {v}" for b, v in verdicts))

    try:
        slim = [{k: v for k, v in c.items() if k not in ("tr_r", "te_r")} for c in cands]
        with open("scan_results.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "time": datetime.datetime.now().isoformat(timespec="seconds"),
                "interval": INTERVAL, "fee_pct": FEE_PCT, "slip_pct": SLIP_PCT,
                "train_frac": TRAIN_FRAC, "candidates": slim, "verdicts": verdicts,
            }, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print("log error:", e)


if __name__ == "__main__":
    main()
