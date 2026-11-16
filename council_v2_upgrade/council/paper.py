import os
import sys
import json
import time
import asyncio
import argparse
import datetime
import httpx
import event_gate
from scanner import fmt, INTERVAL_MS
from fill import (
    ArgError, make_parser, load_settings, norm_base, norm_setup,
    fetch_klines, snapshot, auto_levels, append_journal, read_journal,
    journal_stats, stat_line, SPOT_URL, FUTURES_URL,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER_FILE = os.path.join(HERE, "paper_positions.json")
HEADERS = {"User-Agent": "council-paper/1.0"}
CHECK_STEP = "5m"
CHECK_MS = INTERVAL_MS[CHECK_STEP]
DEFAULT_HOLD_HOURS = 48
WATCH_SECONDS = 300
REASONS = {
    "stop": "chạm dừng lỗ",
    "target": "chạm mục tiêu",
    "timeout": "hết thời gian giữ",
    "manual": "đóng tay",
}


# ---------- Lưu trữ ----------

def load_db():
    if not os.path.exists(PAPER_FILE):
        return {"next_id": 1, "positions": []}
    try:
        with open(PAPER_FILE, encoding="utf-8") as fh:
            db = json.load(fh)
    except Exception as e:
        raise RuntimeError(f"paper_positions.json bị hỏng ({e}). Hãy kiểm tra hoặc đổi tên file này.")
    db.setdefault("next_id", 1)
    db.setdefault("positions", [])
    return db


def save_db(db):
    tmp = PAPER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(db, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, PAPER_FILE)


def now_ms():
    return int(time.time() * 1000)


def fmt_time(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%d/%m %H:%M")


# ---------- Dữ liệu Binance (chỉ đọc) ----------

async def get_klines(client, base, perp, interval, start_ms=None, limit=1000):
    url = FUTURES_URL if perp else SPOT_URL
    params = {"symbol": base + "USDT", "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    r = await client.get(url, params=params)
    if r.status_code in (403, 451):
        raise RuntimeError(f"Binance chặn truy cập từ IP hoặc khu vực này (HTTP {r.status_code})")
    if r.status_code in (418, 429):
        raise RuntimeError(f"Binance giới hạn tốc độ (HTTP {r.status_code}), thử lại sau vài phút")
    if r.status_code == 400:
        where = "futures" if perp else "spot"
        raise RuntimeError(f"Không có cặp {base}USDT trên {where}")
    r.raise_for_status()
    return r.json()


# ---------- Tính kết quả ----------

def calc_r(p, reason, price):
    risk_u = abs(p["entry"] - p["stop"])
    cost_r = p["cost_frac"] * p["entry"] / risk_u
    if reason == "stop":
        gross = -1.0
    elif reason == "target":
        gross = p["rr"]
    else:
        sign = 1 if p["direction"] == "LONG" else -1
        gross = sign * (price - p["entry"]) / risk_u
    return gross - cost_r, gross, cost_r


def finalize(p, reason, price, when_ms, both=False):
    """Đóng vị thế, ghi vào journal.csv và trả về nội dung thông báo."""
    r, gross, cost_r = calc_r(p, reason, price)
    p.update(status="closed", exit_reason=reason, exit_price=price, closed_ms=when_ms, r=r)
    note = f"auto #{p['id']}: {REASONS[reason]}"
    extra = ""
    if both:
        note += " (cùng nến với mục tiêu, tính là thua)"
        extra = " (dừng lỗ và mục tiêu cùng nằm trong một nến 5 phút, tính là thua)"
    append_journal({
        "date": datetime.date.fromtimestamp(when_ms / 1000).isoformat(),
        "symbol": p["base"], "setup": p["setup"], "r": f"{r:.4f}",
        "kind": "paper", "note": note,
    })
    j = journal_stats(read_journal(p["setup"]))
    return (
        f"Lệnh giả lập #{p['id']} {p['base']} ({p['setup']}) đã đóng: {REASONS[reason]}{extra}, "
        f"giá thoát {fmt(price)}.\n"
        f"R trước chi phí {gross:+.2f}, trừ chi phí {cost_r:.2f}, R ròng {r:+.2f}. Đã ghi vào journal.csv.\n"
        f"Thiết lập {p['setup']}: {stat_line(j)}."
    )


def describe(p, cur=None):
    d = p["direction"]
    kind = f"{p['base']}USDT perp" if p["perp"] else f"{p['base']}/USDT spot"
    line = (
        f"#{p['id']} {kind} {d}, thiết lập {p['setup']}: vào {fmt(p['entry'])}, "
        f"dừng lỗ {fmt(p['stop'])}, mục tiêu {fmt(p['target'])}"
    )
    if cur is not None:
        risk_u = abs(p["entry"] - p["stop"])
        sign = 1 if d == "LONG" else -1
        r_now = sign * (cur - p["entry"]) / risk_u
        line += f" | giá hiện tại {fmt(cur)} ({r_now:+.2f}R, chưa tính chi phí)"
    left = p["opened_ms"] + p["hold_hours"] * 3_600_000 - now_ms()
    line += f" | mở {fmt_time(p['opened_ms'])}, còn tối đa {max(left, 0) / 3_600_000:.1f} giờ"
    return line


# ---------- Theo dõi vị thế ----------

def hit_flags(p, hi, lo):
    if p["direction"] == "LONG":
        return lo <= p["stop"], hi >= p["target"]
    return hi >= p["stop"], lo <= p["target"]


async def evaluate_position(client, p):
    hold_ms = int(p["hold_hours"] * 3_600_000)
    start = int(p["next_ms"])
    while start + CHECK_MS <= now_ms():
        batch = await get_klines(client, p["base"], p["perp"], CHECK_STEP, start, 1000)
        if not batch:
            break
        for k in batch:
            t = int(k[0])
            if t + CHECK_MS > now_ms():  # nến chưa đóng, xử lý lần sau
                return {"close": False, "next_ms": t}
            hi, lo, cl = float(k[2]), float(k[3]), float(k[4])
            stop_hit, target_hit = hit_flags(p, hi, lo)
            when = t + CHECK_MS
            if stop_hit:
                return {"close": True, "reason": "stop", "price": p["stop"], "when": when, "both": target_hit}
            if target_hit:
                return {"close": True, "reason": "target", "price": p["target"], "when": when, "both": False}
            if when - p["opened_ms"] >= hold_ms:
                return {"close": True, "reason": "timeout", "price": cl, "when": when, "both": False}
            start = when
        if len(batch) < 1000:
            break
    return {"close": False, "next_ms": start}


async def check_all():
    """Kiểm tra mọi vị thế đang mở. Trả về danh sách thông báo cho các lệnh vừa đóng."""
    db = load_db()
    opens = [p for p in db["positions"] if p["status"] == "open"]
    if not opens:
        return []
    outcomes = {}
    async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
        for p in opens:
            try:
                outcomes[p["id"]] = await evaluate_position(client, p)
            except Exception as e:
                print(f"[paper] lỗi khi kiểm tra #{p['id']}: {e}", flush=True)
    # Nạp lại sau khi đã lấy xong dữ liệu để không ghi đè thay đổi của lệnh khác
    db = load_db()
    events = []
    for p in db["positions"]:
        res = outcomes.get(p["id"])
        if p["status"] != "open" or res is None:
            continue
        if res["close"]:
            events.append(finalize(p, res["reason"], res["price"], res["when"], res.get("both", False)))
        else:
            p["next_ms"] = res["next_ms"]
    save_db(db)
    return events


# ---------- Mở, đóng, liệt kê ----------

def build_open_parser(quiet=False):
    ap = make_parser(quiet, prog="paper.py open", description="Mở một vị thế giả lập theo giá thật của Binance.")
    ap.add_argument("symbol", help="mã, ví dụ SOL hoặc SOLUSDT")
    ap.add_argument("--setup", help="tên thiết lập trong nhật ký, ví dụ breakout4h")
    ap.add_argument("--entry", type=float, help="giá vào (với --auto là giá hiện tại)")
    ap.add_argument("--stop", type=float, help="giá dừng lỗ")
    ap.add_argument("--target", type=float, help="giá mục tiêu")
    ap.add_argument("--auto", action="store_true", help="vào ở giá hiện tại, dừng lỗ theo ATR, mục tiêu theo R/R")
    ap.add_argument("--short", action="store_true", help="hướng SHORT cho --auto (cần --perp)")
    ap.add_argument("--atr-stop", type=float, dest="atr_stop", help="--auto: dừng lỗ bằng bấy nhiêu lần ATR")
    ap.add_argument("--rr", type=float, help="--auto: tỷ lệ lời/lỗ")
    ap.add_argument("--account", type=float, help="ghi đè vốn trong settings.json")
    ap.add_argument("--risk", type=float, help="ghi đè rủi ro tối đa mỗi lệnh, theo phần trăm")
    ap.add_argument("--interval", choices=list(INTERVAL_MS), help="khung nến để tính ATR")
    ap.add_argument("--leverage", type=float, help="đòn bẩy, ví dụ 3")
    ap.add_argument("--perp", action="store_true", help="dùng giá hợp đồng vĩnh cửu futures")
    ap.add_argument("--hold", type=float, help="số giờ giữ lệnh tối đa (mặc định 48)")
    ap.add_argument("--force", action="store_true", help="cho phép vào dù giá hiện tại cách xa giá vào")
    return ap


def build_close_parser(quiet=False):
    ap = make_parser(quiet, prog="paper.py close", description="Đóng tay một vị thế giả lập theo giá hiện tại.")
    ap.add_argument("pid", type=int, help="số thứ tự vị thế, xem bằng positions")
    return ap


async def open_position(a):
    # Applies to automatic AND manual paper opens. Never places an exchange order.
    gate = event_gate.assess(os.path.join(HERE, 'events.txt'))
    if not gate['allowed']:
        return f"Không mở vị thế giả lập: event gate BLOCK ({gate['reason']})."
    st = load_settings()
    base = norm_base(a.symbol)
    setup = norm_setup(a.setup)
    if not base:
        return "Thiếu mã giao dịch."
    if not setup:
        return "Cần --setup ten_thiet_lap."
    if a.short and not a.perp:
        return "Lệnh SHORT cần futures. Thêm --perp."
    interval = a.interval or st["interval"]
    account = a.account if a.account else st["account"]
    risk = a.risk if a.risk else st["risk_pct"]
    hold = a.hold if a.hold else st.get("max_hold_hours", DEFAULT_HOLD_HOURS)
    cost_frac = 2 * (st["fee_pct"] + st["slip_pct"]) / 100

    # Idempotency across restarts and *closed* positions as well.
    signal_id = getattr(a, 'signal_id', None)
    if signal_id:
        try:
            prior = next((p for p in load_db()['positions'] if p.get('signal_id') == signal_id), None)
        except RuntimeError as e:
            return str(e)
        if prior is not None:
            return f"Tín hiệu {signal_id} đã được ghi bằng vị thế #{prior['id']}; không mở trùng."

    try:
        rows = await fetch_klines(base, interval, a.perp)
        async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
            cur_rows = await get_klines(client, base, a.perp, CHECK_STEP, None, 2)
    except Exception as e:
        return f"Không lấy được dữ liệu từ Binance: {e}"
    s = snapshot(rows)
    cur = float(cur_rows[-1][4])

    entry = a.entry if a.entry else (cur if a.auto else None)
    stop, target = a.stop, a.target
    if a.auto and entry:
        a_stop, a_target, _ = auto_levels(
            entry, s["atr"], a.short, a.atr_stop or st["atr_stop"], a.rr or st["rr"], cost_frac
        )
        stop = stop if stop else a_stop
        target = target if target else a_target
    if not (entry and stop and target):
        return "Thiếu mức giá. Dùng --auto hoặc nhập đủ --entry --stop --target."
    if min(entry, stop, target, account, risk) <= 0:
        return "Giá, vốn và rủi ro phải lớn hơn 0."
    long_ = target > entry > stop
    short_ = target < entry < stop
    if not (long_ or short_):
        return "Entry, stop và target không nhất quán (thứ tự các mức giá sai)."
    if short_ and not a.perp:
        return "Lệnh SHORT cần futures. Thêm --perp."
    gap = abs(entry - cur) / s["atr"]
    if gap > 1 and not a.force:
        return (
            f"Giá hiện tại {fmt(cur)} cách giá vào {fmt(entry)} tới {gap:.1f} ATR, lệnh có thể chưa khớp ngoài thực tế. "
            f"Dùng --auto để vào ở giá hiện tại, hoặc thêm --force."
        )

    risk_u = abs(entry - stop)
    rr = abs(target - entry) / risk_u
    max_loss = account * risk / 100
    size = max_loss / risk_u
    allowed = account * (a.leverage or 1)
    capped = size * entry > allowed
    if capped:
        size = allowed / entry
    risk_usd = size * risk_u

    try:
        db = load_db()
    except RuntimeError as e:
        return str(e)
    for q in db["positions"]:
        if q["status"] == "open" and q["base"] == base and q["setup"] == setup:
            return f"Đã có vị thế giả lập #{q['id']} cho {base} (thiết lập {setup}). Đóng nó trước khi mở lệnh mới."

    t = now_ms()
    pos = {
        "id": db["next_id"], "base": base, "perp": bool(a.perp), "setup": setup,
        "direction": "LONG" if long_ else "SHORT",
        "entry": entry, "stop": stop, "target": target, "rr": rr,
        "size": size, "risk_usd": risk_usd, "cost_frac": cost_frac,
        "interval": interval, "opened_ms": t, "hold_hours": float(hold),
        "next_ms": (t // CHECK_MS + 1) * CHECK_MS,  # bắt đầu từ nến kế tiếp
        "status": "open",
        "signal_id": signal_id,
        "context": getattr(a, 'context', None) or {},
    }
    db["next_id"] += 1
    db["positions"].append(pos)
    save_db(db)

    note = " (vị thế bị giới hạn bởi vốn, rủi ro thực nhỏ hơn mức tối đa)" if capped else ""
    return (
        f"Đã mở vị thế giả lập. {describe(pos)}\n"
        f"R/R {rr:.2f}, vị thế khoảng {size:g} ({size * entry:,.0f} USD), rủi ro khoảng {risk_usd:,.2f} USD{note}.\n"
        f"Giá hiện tại {fmt(cur)}. Bot (hoặc paper.py watch) sẽ kiểm tra mỗi {WATCH_SECONDS // 60} phút "
        f"và tự ghi kết quả vào journal.csv khi lệnh đóng."
    )


async def close_position(a):
    events = await check_all()  # xử lý các nến đã bỏ lỡ trước khi đóng tay
    try:
        db = load_db()
    except RuntimeError as e:
        return str(e)
    p = next((x for x in db["positions"] if x["id"] == a.pid), None)
    if p is None:
        return f"Không có vị thế #{a.pid}."
    if p["status"] != "open":
        return "\n\n".join(events) if events else f"Vị thế #{a.pid} đã đóng."
    try:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
            rows = await get_klines(client, p["base"], p["perp"], CHECK_STEP, None, 2)
    except Exception as e:
        return f"Không lấy được giá: {e}"
    price = float(rows[-1][4])
    db = load_db()
    p = next((x for x in db["positions"] if x["id"] == a.pid), None)
    if p is None or p["status"] != "open":
        return "Vị thế vừa thay đổi trong lúc lấy giá, hãy thử lại."
    text = finalize(p, "manual", price, now_ms())
    save_db(db)
    return text


async def list_positions():
    try:
        db = load_db()
    except RuntimeError as e:
        return str(e)
    opens = [p for p in db["positions"] if p["status"] == "open"]
    closed = [p for p in db["positions"] if p["status"] == "closed"][-5:]
    lines = []
    if opens:
        lines.append("Vị thế giả lập đang mở:")
        async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
            for p in opens:
                cur = None
                try:
                    rows = await get_klines(client, p["base"], p["perp"], CHECK_STEP, None, 2)
                    cur = float(rows[-1][4])
                except Exception:
                    pass
                lines.append("- " + describe(p, cur))
    else:
        lines.append("Không có vị thế giả lập nào đang mở.")
    if closed:
        lines.append("\nĐã đóng gần đây:")
        for p in closed:
            lines.append(
                f"- #{p['id']} {p['base']} ({p['setup']}): {REASONS[p['exit_reason']]}, R ròng {p['r']:+.2f}"
            )
    return "\n".join(lines)


# ---------- Dòng lệnh ----------

def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "positions"
    rest = argv[1:]
    if cmd == "open":
        print(asyncio.run(open_position(build_open_parser().parse_args(rest))))
    elif cmd == "close":
        print(asyncio.run(close_position(build_close_parser().parse_args(rest))))
    elif cmd == "positions":
        print(asyncio.run(list_positions()))
    elif cmd == "check":
        events = asyncio.run(check_all())
        print("\n\n".join(events) if events else "Không có lệnh nào đóng.")
    elif cmd == "watch":
        print(f"Theo dõi vị thế giả lập, kiểm tra mỗi {WATCH_SECONDS // 60} phút. Nhấn Ctrl+C để dừng.")
        print("Đừng chạy lệnh này cùng lúc với bot Telegram, vì cả hai đều theo dõi cùng các vị thế.")
        while True:
            try:
                events = asyncio.run(check_all())
                for text in events:
                    print("\n" + text, flush=True)
            except Exception as e:
                print("lỗi khi kiểm tra:", e, flush=True)
            time.sleep(WATCH_SECONDS)
    else:
        print("Lệnh: open, close, positions, check, watch")


if __name__ == "__main__":
    main()
