import os
import re
import sys
import csv
import json
import math
import asyncio
import argparse
import datetime
import httpx
from scanner import prepare, fmt, TOKENIZED, INTERVAL_MS
from council import run_council, ALIASES

SPOT_URL = "https://api.binance.com/api/v3/klines"  # nếu bị chặn, thử https://data-api.binance.vision/api/v3/klines
FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"  # mình chưa kiểm tra địa chỉ này từ máy bạn

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.json")
JOURNAL_FILE = os.path.join(HERE, "journal.csv")
FIELDS = ["date", "symbol", "setup", "r", "kind", "note"]
SHADOW_LOG = os.path.join(HERE, "shadow_log.csv")
SHADOW_FIELDS = ["time", "setup", "symbol", "group", "structure", "rsi", "verdict", "position_id", "note"]

DEFAULTS = {
    "account": 1000.0,   # vốn (USD)
    "risk_pct": 1.0,     # rủi ro tối đa mỗi lệnh, đơn vị phần trăm
    "fee_pct": 0.10,     # phí mỗi chiều, đơn vị phần trăm (hãy kiểm tra mức phí thật của bạn)
    "slip_pct": 0.02,    # trượt giá mỗi chiều, đơn vị phần trăm
    "interval": "4h",    # khung nến để tính chỉ báo
    "min_trades": 30,    # số lệnh nhật ký tối thiểu trước khi gọi hội đồng
    "atr_stop": 1.5,     # --auto: dừng lỗ bằng bấy nhiêu lần ATR
    "rr": 2.0,           # --auto: tỷ lệ lời/lỗ
}
DEMO_WIN, DEMO_TRADES = 40.0, 30


class ArgError(Exception):
    pass


class QuietParser(argparse.ArgumentParser):
    """Dùng cho Telegram: báo lỗi bằng ngoại lệ thay vì thoát chương trình."""

    def error(self, message):
        raise ArgError(message)

    def exit(self, status=0, message=None):
        raise ArgError(message or "")


def make_parser(quiet, **kw):
    return (QuietParser if quiet else argparse.ArgumentParser)(**kw)


# ---------- Cài đặt và nhật ký ----------

def load_settings():
    s = dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8-sig") as fh:
                s.update(json.load(fh))
        except Exception as e:
            print(f"Không đọc được settings.json ({e}). Dùng giá trị mặc định.")
    return s


def save_settings(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=2, ensure_ascii=False)


def norm_setup(x):
    return re.sub(r"[^a-z0-9_]+", "_", (x or "").lower()).strip("_")


def norm_base(s):
    s = re.sub(r"\s", "", (s or "").upper())
    return re.sub(r"[/\-_]?USDT$", "", s)


def clean_rule(text):
    """Bỏ dấu hai chấm và dấu bằng để quy tắc không bị code nhầm là nhãn số liệu."""
    t = re.sub(r"[:=\n\r]+", " ", text or "")
    return re.sub(r"\s+", " ", t).strip()[:300]


def read_journal(setup=None):
    rows = []
    if not os.path.exists(JOURNAL_FILE):
        return rows
    with open(JOURNAL_FILE, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                r = float(row["r"])
            except (KeyError, ValueError, TypeError):
                continue
            name = norm_setup(row.get("setup"))
            if setup and name != setup:
                continue
            rows.append({
                "date": row.get("date", ""), "symbol": row.get("symbol", ""), "setup": name,
                "r": r, "kind": (row.get("kind") or "paper").strip().lower(),
                "note": row.get("note", ""),
            })
    return rows


def append_journal(row):
    new = not os.path.exists(JOURNAL_FILE) or os.path.getsize(JOURNAL_FILE) == 0
    with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def journal_stats(rows):
    n = len(rows)
    if n == 0:
        return None
    rs = [x["r"] for x in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    mean = sum(rs) / n
    low = None
    if n >= 2:
        var = sum((r - mean) ** 2 for r in rs) / (n - 1)
        low = mean - 1.96 * math.sqrt(var / n)
    return {
        "n": n, "win": len(wins) / n * 100, "mean": mean, "low": low,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "paper": sum(1 for x in rows if x["kind"] != "real"),
        "real": sum(1 for x in rows if x["kind"] == "real"),
    }


def stat_line(st):
    if not st:
        return "chưa có dữ liệu thống kê"
    low = "?" if st["low"] is None else f"{st['low']:+.2f}R"
    aw = "-" if st["avg_win"] is None else f"{st['avg_win']:+.2f}R"
    al = "-" if st["avg_loss"] is None else f"{st['avg_loss']:+.2f}R"
    return (
        f"{st['n']} lệnh ({st['paper']} giả lập, {st['real']} thật), thắng {st['win']:.1f}%, "
        f"kỳ vọng {st['mean']:+.2f}R (cận dưới 95%: {low}), "
        f"thắng trung bình {aw}, thua trung bình {al}"
    )


# ---------- Dữ liệu thị trường ----------

async def fetch_klines(base, interval, perp):
    url = FUTURES_URL if perp else SPOT_URL
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "council-fill/4.0"}) as client:
        r = await client.get(url, params={"symbol": base + "USDT", "interval": interval, "limit": 300})
    if r.status_code in (403, 451):
        raise RuntimeError(f"Binance chặn truy cập từ IP hoặc khu vực này (HTTP {r.status_code})")
    if r.status_code in (418, 429):
        raise RuntimeError(f"Binance giới hạn tốc độ (HTTP {r.status_code}), đợi vài phút rồi chạy lại")
    if r.status_code == 400:
        where = "futures" if perp else "spot"
        raise RuntimeError(f"Không có cặp {base}USDT trên {where}. Thử thêm hoặc bỏ cờ --perp")
    r.raise_for_status()
    rows = r.json()[:-1]  # bỏ nến đang chạy dở
    if len(rows) < 120:
        raise RuntimeError("quá ít nến để tính chỉ báo")
    return rows


def snapshot(rows):
    d = prepare(rows)
    i = len(rows) - 1
    return {
        "price": d["c"][i], "s20": d["sma20"][i], "s50": d["sma50"][i],
        "rsi": d["rsi"][i], "atr": d["atr"][i],
        "volr": d["v"][i] / d["vol20"][i - 1],
        "sup": min(d["l"][-20:]), "res": max(d["h"][-100:]),
    }


def auto_levels(entry, atr, short, atr_mult, rr, cost_frac):
    """Đề xuất dừng lỗ theo biên độ (ATR), không thấp hơn 3 lần chi phí khứ hồi, rồi mục tiêu theo R/R."""
    dist = max(atr_mult * atr, 3 * cost_frac * entry)
    widened = dist > atr_mult * atr * 1.000001
    if short:
        return entry + dist, entry - rr * dist, widened
    return entry - dist, entry + rr * dist, widened


# ---------- Cảnh báo và câu hỏi cho hội đồng ----------

def rel(price, ma, atr):
    d = (price - ma) / atr
    if abs(d) <= 0.1:
        return "at"
    return "above" if d > 0 else "below"


def structure(s):
    r50 = rel(s["price"], s["s50"], s["atr"])
    if r50 == "above" and s["s20"] > s["s50"]:
        return "uptrend"
    if r50 == "below" and s["s20"] < s["s50"]:
        return "downtrend"
    return "mixed, no clear trend"


def rsi_label(r):
    if r >= 70:
        return "overbought"
    if r <= 30:
        return "oversold"
    return "neutral"


def rule_mismatch(rule, s):
    w = []
    t = (rule or "").lower()
    if not t:
        return w
    st = structure(s)
    if re.search(r"uptrend|xu hướng tăng|trend up", t) and st != "uptrend":
        w.append(f"Quy tắc của bạn nói xu hướng tăng nhưng cấu trúc hiện tại là: {st}.")
    for name, key in (("MA20", "s20"), ("MA50", "s50")):
        if re.search(name[:2] + r"\s*" + name[2:], t, re.I):
            d = (s["price"] - s[key]) / s["atr"]
            if abs(d) > 1:
                w.append(
                    f"Quy tắc nhắc {name} nhưng giá đang cách {name} {abs(d):.1f} ATR "
                    f"({'trên' if d > 0 else 'dưới'})."
                )
    return w


def check(plan, s, jst, st, event, rule, demo):
    w = []
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    p, atr = s["price"], s["atr"]
    long_ = target > entry > stop
    gap = abs(entry - p) / atr
    if gap > 1:
        w.append(f"Entry cách giá hiện tại {gap:.1f} ATR (giá hiện tại {fmt(p)}). Kiểm tra lại mức vào.")
    stop_atr = abs(entry - stop) / atr
    if stop_atr < 0.7:
        w.append(f"Dừng lỗ chỉ cách {stop_atr:.2f} ATR, dễ bị quét bởi nhiễu giá thông thường.")
    cost_pct = 2 * (st["fee_pct"] + st["slip_pct"])
    stop_pct = abs(entry - stop) / entry * 100
    if stop_pct < 3 * cost_pct:
        w.append(f"Dừng lỗ cách giá vào {stop_pct:.2f}%, quá gần chi phí khứ hồi ước tính ({cost_pct:.2f}%).")
    tgt_atr = abs(target - entry) / atr
    if tgt_atr > 6:
        w.append(f"Mục tiêu xa {tgt_atr:.1f} ATR, ít khi đạt trong thời gian ngắn.")
    if long_ and target > s["res"] > p * 1.0005:
        w.append("Mục tiêu nằm trên vùng kháng cự 100 nến gần nhất, giá có thể bị cản.")
    if (not long_) and target < s["sup"]:
        w.append("Mục tiêu nằm dưới vùng hỗ trợ 20 nến gần nhất.")
    if jst:
        if jst["mean"] <= 0:
            w.append(
                f"Kỳ vọng trung bình trong nhật ký của thiết lập này là {jst['mean']:+.2f}R, không dương. "
                f"Dữ liệu của chính bạn đang nói thiết lập này chưa có lợi thế."
            )
        if jst["avg_win"] is not None and jst["avg_win"] < 0.8 * plan["rr"]:
            w.append(
                f"Lệnh thắng trong nhật ký trung bình chỉ {jst['avg_win']:+.2f}R, nhưng kế hoạch này kỳ vọng "
                f"{plan['rr']:.2f}R. Tỷ lệ thắng trong nhật ký có thể không áp dụng cho R/R này."
            )
        if jst["real"] == 0:
            w.append("Toàn bộ lệnh trong nhật ký là giả lập, thường lạc quan hơn lệnh thật (trượt giá, tâm lý).")
    if not rule and not demo:
        w.append("Bạn chưa khai --rule. Hội đồng sẽ đánh giá theo xu hướng chung thay vì quy tắc thiết lập của bạn.")
    w += rule_mismatch(rule, s)
    if not event:
        w.append("Bạn chưa khai --event. Tự kiểm tra sự kiện trong 24 giờ tới (Fed, CPI, mở khóa token, công bố lợi nhuận).")
    return w

def levels_line(q, s):
    """Code kết luận vị trí dừng lỗ và mục tiêu so với hỗ trợ, kháng cự và biên độ. Không đưa số để mô hình khỏi chép sai."""
    e, st, tg = q["entry"], q["stop"], q["target"]
    sup, res, atr = s["sup"], s["res"], s["atr"]
    long_ = tg > e > st
    notes, ok = [], True
    if long_:
        if st >= sup and sup < e:
            notes.append("stop-loss is above the support level, so price may hit it before support is tested (risk)")
            ok = False
        else:
            notes.append("stop-loss is placed below support, which is normal for a long trade")
        if tg > res > e:
            notes.append("target is beyond the resistance level, so resistance may block price (risk)")
            ok = False
        else:
            notes.append("target is below the resistance level, which is not a concern")
    else:
        if st <= res and res > e:
            notes.append("stop-loss is below the resistance level, so price may hit it before resistance is tested (risk)")
            ok = False
        else:
            notes.append("stop-loss is placed above resistance, which is normal for a short trade")
        if tg < sup < e:
            notes.append("target is beyond the support level, so support may block price (risk)")
            ok = False
        else:
            notes.append("target is above the support level, which is not a concern")
    if abs(e - st) / atr < 0.7:
        notes.append("the stop-loss distance is very tight compared with normal price movement (risk)")
        ok = False
    else:
        notes.append("the stop-loss distance is adequate and not too tight")
    head = "all level placements are acceptable" if ok else "some level placements carry risk"
    return f"Levels (verified by code, do not dispute): {head}: " + "; ".join(notes) + "."


def build_question(q):
    base, p, s = q["base"], q["price"], q["snap"]
    market = q["market"] or TOKENIZED.get(base, ("crypto", None))[0]
    kw = q["keywords"] or TOKENIZED.get(base, (None, None))[1]
    if not kw:
        name = ALIASES.get(base, base.lower())
        kw = f"{name}, {base}" if name != base.lower() else base
    asset = f"{base}USDT perpetual" if q["perp"] else f"{base}/USDT spot"
    res = (
        f"Resistance: {fmt(s['res'])}"
        if s["res"] > p * 1.0005
        else "Resistance: none, price is at a 100-bar high"
    )
    pos20 = (p - s["s20"]) / s["atr"]
    lines = [f"Asset: {asset}", f"Market: {market}", f"Keywords: {kw}"]
    lines.append(levels_line(q, s))
    if q["leverage"]:
        lines.append(f"Leverage: {q['leverage']:g}x")
    note = ""
    if q["demo"]:
        note = " (ASSUMED for a software test, not real evidence)"
    elif q["shadow"]:
        note = " (fixed neutral assumption for an experiment, not real evidence)"
    lines += [
        f"Entry: {fmt(q['entry'])}. Stop-loss: {fmt(q['stop'])}. Target: {fmt(q['target'])}",
        f"Account: {q['account']:,.0f} USD. Max risk per trade: {q['risk']:g}%",
        f"Journal of this exact setup (my own trades): {q['trades']} trades, win rate {q['win']:.1f}%{note}",
    ]
    if q["shadow"]:
        lines.append(
            "Experiment mode: the trade statistics above are a fixed neutral assumption and are not part "
            "of the judgment. Judge the market context, news, levels and risk only."
        )
    j = q["jst"]
    if j:
        lines.append(
            f"Journal expectancy net of costs: {j['mean']:+.2f}R per trade "
            f"({j['paper']} paper trades, {j['real']} real trades)."
        )
    lines += [
        f"Current price: {fmt(p)}",
        f"Trend (verified by code): structure is {structure(s)}. Price is {rel(p, s['s50'], s['atr'])} "
        f"the 50-bar MA ({fmt(s['s50'])}) and {abs(pos20):.1f} ATR {'above' if pos20 >= 0 else 'below'} "
        f"the 20-bar MA ({fmt(s['s20'])}). The 20-bar MA is {'above' if s['s20'] > s['s50'] else 'below'} "
        f"the 50-bar MA ({q['interval']} bars). "
        f"RSI(14): {s['rsi']:.0f} ({rsi_label(s['rsi'])}; overbought only above 70, oversold only below 30). "
        f"Volume: {s['volr']:.1f}x the 20-bar average. ATR(14): {s['atr'] / p * 100:.2f}% of price.",
        f"Support: {fmt(s['sup'])}. {res}",
    ]
    context = q.get('context') or {}
    if context:
        reg = context.get('market_regime', {})
        nar = context.get('narrative', {})
        sw = context.get('liquidity_sweep', {})
        lines.append(f"Market regime (code): {reg.get('regime','UNKNOWN')}; BTC daily MA20={reg.get('btc_ma20','?')}, MA50={reg.get('btc_ma50','?')}; data as-of {reg.get('as_of_ms','?')} ms.")
        lines.append(f"Narrative trend (price/volume proxy, NOT verified capital flow): group {nar.get('name','UNKNOWN')}, score {nar.get('score','?')}/100, members {nar.get('members','?')}, coin return vs BTC over 7d {nar.get('relative_7d_pct','?')}%, status {nar.get('status','UNKNOWN')}.")
        lines.append(f"Liquidity sweep (optional annotation): confirmed={sw.get('confirmed',False)}, level={sw.get('level','?')}, low={sw.get('sweep_low','?')}, depth={sw.get('depth_atr','?')} ATR, bars_ago={sw.get('bars_ago','?')}.")
        lines.append("Research rule: Pullback is the ONLY technical entry condition. An absent sweep, weak/unknown narrative or BTC regime is NOT an automatic rejection. Do not assume these factors independently predict returns; assess all evidence.")
    if q["event"]:
        lines.append(f"Event: {q['event']}")
    if q["rule"]:
        lines.append(f"Setup rule: {q['rule']}")  # luôn đặt ở dòng cuối
    return "\n".join(lines)


async def run(text):
    chunks = []
    async for piece in run_council(text):
        chunks.append(piece)
        print(piece.replace("**", "").replace("`", ""), end="", flush=True)
    return "".join(chunks)


# ---------- Chuẩn bị yêu cầu (dùng chung cho dòng lệnh và Telegram) ----------

def build_ask_parser(quiet=False):
    ap = make_parser(
        quiet, prog="fill.py",
        description="Điền dữ liệu thị trường từ Binance cho thiết lập do bạn chọn, rồi gửi cho hội đồng.",
    )
    ap.add_argument("symbol", help="mã, ví dụ SOL, SOLUSDT hoặc XAU")
    ap.add_argument("--setup", help="tên thiết lập trong nhật ký, ví dụ breakout4h")
    ap.add_argument("--rule", default=None, help="quy tắc vào lệnh của thiết lập, viết trong một câu")
    ap.add_argument("--entry", type=float, help="giá vào lệnh (với --auto, mặc định là giá đóng cửa nến gần nhất)")
    ap.add_argument("--stop", type=float, help="giá dừng lỗ")
    ap.add_argument("--target", type=float, help="giá mục tiêu")
    ap.add_argument("--auto", action="store_true",
                    help="tự đề xuất dừng lỗ và mục tiêu theo biên độ ATR. Không chọn hướng hay thời điểm vào lệnh")
    ap.add_argument("--short", action="store_true", help="hướng SHORT cho --auto (cần --perp)")
    ap.add_argument("--atr-stop", type=float, dest="atr_stop", help="--auto: dừng lỗ bằng bấy nhiêu lần ATR")
    ap.add_argument("--rr", type=float, help="--auto: tỷ lệ lời/lỗ")
    ap.add_argument("--account", type=float, help="ghi đè vốn trong settings.json")
    ap.add_argument("--risk", type=float, help="ghi đè rủi ro tối đa mỗi lệnh, tính theo phần trăm")
    ap.add_argument("--interval", choices=list(INTERVAL_MS), help="ghi đè khung nến")
    ap.add_argument("--leverage", type=float, help="đòn bẩy, ví dụ 3")
    ap.add_argument("--perp", action="store_true", help="dùng dữ liệu và nhãn hợp đồng vĩnh cửu futures")
    ap.add_argument("--market", help="crypto, stock, forex hoặc commodity")
    ap.add_argument("--keywords", help="từ khóa tìm tin, ví dụ Apple, AAPL")
    ap.add_argument("--event", help="sự kiện sắp tới, ví dụ US CPI tomorrow. Ghi none nếu chắc là không có")
    ap.add_argument("--demo", action="store_true",
                    help="chế độ thử phần mềm: dùng tỷ lệ thắng giả định, KHÔNG dùng để vào lệnh thật")
    ap.add_argument("--shadow", action="store_true",
                    help="chế độ bóng: sau khi hội đồng kết luận, tự mở vị thế giả lập vào nhóm duyệt hoặc nhóm từ chối")
    ap.add_argument("--hold", type=float, default=None,
                    help="số giờ giữ vị thế giả lập tối đa khi dùng --shadow (mặc định 48)")
    ap.add_argument("--dry", action="store_true", help="chỉ in dữ liệu, kế hoạch và cảnh báo, không gọi hội đồng")
    return ap


def experiment_win(rr):
    """Tỷ lệ thắng giả định cố định cho chế độ bóng: điểm hòa vốn cộng 10 điểm phần trăm."""
    return round(min(90.0, 100.0 / (1.0 + rr) + 10.0), 1)


async def build_request(a):
    """Trả về {'text', 'question', 'plan', 'shadow'}. question là None nếu chưa gọi hội đồng."""
    st = load_settings()
    account = a.account if a.account else st["account"]
    risk = a.risk if a.risk else st["risk_pct"]
    interval = a.interval or st["interval"]
    cost_frac = 2 * (st["fee_pct"] + st["slip_pct"]) / 100
    base = norm_base(a.symbol)
    setup = norm_setup(a.setup)
    rule = clean_rule(a.rule)

    def fail(msg):
        return {"text": msg, "question": None}

    if not base:
        return fail("Thiếu mã giao dịch.")
    if not a.demo and not setup:
        return fail("Cần --setup ten_thiet_lap (hoặc --demo để thử phần mềm).")
    if a.shadow and a.demo:
        return fail("Không dùng --shadow cùng --demo.")
    if a.short and not a.perp:
        return fail("Lệnh SHORT cần futures. Thêm --perp.")

    try:
        rows = await fetch_klines(base, interval, a.perp)
    except Exception as e:
        return fail(f"Không lấy được dữ liệu từ Binance: {e}")
    s = snapshot(rows)
    price = s["price"]

    entry = a.entry if a.entry else (price if a.auto else None)
    stop, target, widened = a.stop, a.target, False
    if a.auto and entry:
        a_stop, a_target, widened = auto_levels(
            entry, s["atr"], a.short, a.atr_stop or st["atr_stop"], a.rr or st["rr"], cost_frac
        )
        stop = stop if stop else a_stop
        target = target if target else a_target
    if not (entry and stop and target):
        return fail("Thiếu mức giá. Dùng --auto hoặc nhập đủ --entry --stop --target.")
    if min(entry, stop, target, account, risk) <= 0:
        return fail("Giá, vốn và rủi ro phải lớn hơn 0 (mục tiêu tự tính có thể âm nếu biên độ quá lớn so với giá).")
    long_ = target > entry > stop
    short_ = target < entry < stop
    if not (long_ or short_):
        return fail("Entry, stop và target không nhất quán (thứ tự các mức giá sai).")

    risk_u = abs(entry - stop)
    rr = abs(target - entry) / risk_u
    max_loss = account * risk / 100
    size = max_loss / risk_u
    notional = size * entry
    stop_pct = risk_u / entry * 100

    out = [
        f"Dữ liệu Binance ({interval}, {base}USDT{' perp' if a.perp else ''}): giá {fmt(price)}, "
        f"MA20 {fmt(s['s20'])}, MA50 {fmt(s['s50'])}, RSI {s['rsi']:.0f}, "
        f"ATR {s['atr'] / price * 100:.2f} phần trăm giá, khối lượng {s['volr']:.1f}x trung bình.",
        "",
        "Kế hoạch (đề xuất theo biên độ ATR, không phải tín hiệu):" if a.auto else "Kế hoạch (do bạn nhập):",
        f"- Hướng: {'LONG' if long_ else 'SHORT'}",
        f"- Vào {fmt(entry)}" + (" (giá đóng cửa nến gần nhất)" if a.auto and not a.entry else ""),
        f"- Dừng lỗ {fmt(stop)}, cách {stop_pct:.2f}% (bằng {risk_u / s['atr']:.1f} ATR)"
        + (" (đã nới ra để vượt ngưỡng chi phí)" if widened and not a.stop else ""),
        f"- Mục tiêu {fmt(target)}, R/R {rr:.2f}",
        f"- Với vốn {account:,.0f} USD và rủi ro {risk:g}%: lỗ tối đa {max_loss:,.2f} USD, "
        f"vị thế tối đa khoảng {size:g} ({notional:,.0f} USD)",
    ]
    if rule:
        out.append(f"- Quy tắc thiết lập: {rule}")
    out.append("")

    if a.demo:
        win, trades, jst = DEMO_WIN, DEMO_TRADES, None
        out.append(
            "=== CHẾ ĐỘ THỬ: tỷ lệ thắng 40% với 30 lệnh là GIẢ ĐỊNH để thử phần mềm. "
            "Kết quả không có giá trị làm bằng chứng và KHÔNG dùng để vào lệnh thật. ==="
        )
        out.append("")
    elif a.shadow:
        win, trades, jst = experiment_win(rr), 100, None
        out.append(
            f"=== CHẾ ĐỘ BÓNG: hội đồng nhận tỷ lệ thắng giả định cố định {win:.1f}% (hòa vốn cộng 10 điểm phần trăm), "
            f"chỉ để đánh giá thị trường, tin tức và rủi ro. Sau khi hội đồng kết luận, một vị thế giả lập "
            f"sẽ tự mở vào nhóm {setup}_ok hoặc {setup}_no. ==="
        )
        out.append("")
    else:
        jst = journal_stats(read_journal(setup))
        win, trades = (jst["win"], jst["n"]) if jst else (None, 0)

    plan = {"entry": entry, "stop": stop, "target": target, "rr": rr}
    warns = check(plan, s, jst, st, a.event, rule, a.demo)
    if warns:
        out.append("Cảnh báo của code (không đưa cho hội đồng):")
        out += [f"- {x}" for x in warns]
        out.append("")

    if not a.demo and not a.shadow and trades < st["min_trades"]:
        out.append(
            f"Chưa đủ nhật ký cho thiết lập '{setup}': {trades}/{st['min_trades']} lệnh. "
            f"Hội đồng chưa được gọi vì tỷ lệ thắng tính từ quá ít lệnh không đáng tin.\n"
            f"Ghi một lệnh đã đóng bằng: log --setup {setup} --symbol {base} --entry ... --stop ... --exit ...\n"
            f"Hoặc dùng --demo để chỉ thử phần mềm, hay --shadow để so sánh nhóm được duyệt và bị từ chối."
        )
        return {"text": "\n".join(out), "question": None}
    if a.dry:
        out.append("(--dry: chưa gọi hội đồng)")
        return {"text": "\n".join(out), "question": None}

    q = build_question({
        "base": base, "perp": a.perp, "market": a.market, "keywords": a.keywords,
        "leverage": a.leverage, "entry": entry, "stop": stop, "target": target,
        "account": account, "risk": risk, "win": win, "trades": trades,
        "jst": jst, "snap": s, "price": price, "interval": interval,
        "event": a.event, "demo": a.demo, "shadow": a.shadow, "rule": rule,
        "context": getattr(a, "context", None),
    })
    shadow_plan = {
        "base": base, "perp": bool(a.perp), "setup": setup, "entry": entry, "stop": stop,
        "target": target, "account": account, "risk": risk, "leverage": a.leverage,
        "interval": interval, "hold": a.hold,
        "structure": structure(s), "rsi": round(s["rsi"], 1),
        "context": getattr(a, "context", None), "signal_id": getattr(a, "signal_id", None),
    }
    return {"text": "\n".join(out), "question": q, "plan": shadow_plan, "shadow": bool(a.shadow)}


# ---------- Lệnh bóng ----------

def verdict_of(full):
    """Trả về (nhóm, kết luận). Nhóm là 'ok', 'no' hoặc None nếu không đưa vào phép so sánh."""
    t = full.replace("**", "")
    m = re.search(r"KẾT LUẬN:\s*([A-Z_]+)", t)
    if not m:
        return None, "không đọc được kết luận của hội đồng"
    v = m.group(1)
    tail = t[m.start(): m.start() + 400]
    if "code từ chối" in tail or "thiếu dữ liệu" in tail:
        return None, "code từ chối hoặc thiếu dữ liệu nên hội đồng AI không được hỏi"
    if v == "NO_TRADE" and "Lỗi hoặc thiếu kết quả" in tail:
        return None, "một vai AI bị lỗi nên kết luận NO_TRADE không phản ánh ý kiến hội đồng"
    if v in ("LONG", "SHORT"):
        return "ok", v
    if v == "NO_TRADE":
        return "no", v
    return None, f"kết luận lạ: {v}"


def log_shadow(plan, group, verdict, pid, note=""):
    """Ghi mọi lần chạy --shadow, kể cả khi không mở được lệnh, để sau này phân tích theo cấu trúc xu hướng."""
    try:
        new = not os.path.exists(SHADOW_LOG) or os.path.getsize(SHADOW_LOG) == 0
        with open(SHADOW_LOG, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=SHADOW_FIELDS)
            if new:
                w.writeheader()
            w.writerow({
                "time": datetime.datetime.now().isoformat(timespec="seconds"),
                "setup": plan["setup"], "symbol": plan["base"], "group": group,
                "structure": plan.get("structure", ""), "rsi": plan.get("rsi", ""),
                "verdict": verdict, "position_id": pid, "note": note,
            })
    except Exception as e:
        print("shadow log lỗi:", e, flush=True)


async def shadow_after(plan, full):
    group, info = verdict_of(full)
    if group is None:
        log_shadow(plan, "", "", "", f"không mở lệnh bóng: {info}")
        return f"Không mở lệnh bóng: {info}."
    from paper import open_position, load_db  # nhập muộn để tránh vòng nhập giữa hai file
    both = (plan["setup"] + "_ok", plan["setup"] + "_no")
    for p in load_db()["positions"]:
        if p["status"] == "open" and p["base"] == plan["base"] and p["setup"] in both:
            log_shadow(plan, group, info, p["id"], "bỏ qua vì đã có vị thế mở cho mã này")
            return (
                f"Không mở lệnh bóng: đã có vị thế giả lập #{p['id']} cho {plan['base']} "
                f"(nhóm {p['setup']}). Mỗi tín hiệu chỉ nên được ghi nhận một lần, "
                f"nếu không cùng một đợt thị trường sẽ bị đếm hai lần."
            )
    name = f"{plan['setup']}_{group}"
    ns = argparse.Namespace(
        symbol=plan["base"], setup=name, entry=plan["entry"], stop=plan["stop"],
        target=plan["target"], auto=False, short=False, atr_stop=None, rr=None,
        account=plan["account"], risk=plan["risk"], interval=plan["interval"],
        leverage=plan["leverage"], perp=plan["perp"], hold=plan["hold"], force=False,
        context=plan.get("context"), signal_id=plan.get("signal_id"),
    )
    label = "được hội đồng duyệt" if group == "ok" else "bị hội đồng từ chối"
    msg = await open_position(ns)
    m = re.search(r"#(\d+)", msg) if msg.startswith("Đã mở") else None
    log_shadow(plan, group, info, m.group(1) if m else "", "đã mở" if m else "không mở được: " + msg[:80])
    return f"Lệnh bóng ({label}, nhóm {name}):\n{msg}"

def build_compare_parser(quiet=False):
    ap = make_parser(quiet, prog="fill.py compare",
                     description="So sánh kết quả nhóm được hội đồng duyệt và nhóm bị từ chối.")
    ap.add_argument("--setup", required=True, help="tên thiết lập gốc, ví dụ pullback4h")
    return ap


def mean_sd(rs):
    n = len(rs)
    m = sum(rs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (n - 1)) if n > 1 else None
    return m, sd


def do_compare(a):
    base = norm_setup(a.setup)
    ok_rows = read_journal(base + "_ok")
    no_rows = read_journal(base + "_no")
    j_ok, j_no = journal_stats(ok_rows), journal_stats(no_rows)
    need = load_settings()["min_trades"]
    lines = [
        f"So sánh nhóm hội đồng duyệt và nhóm bị từ chối của thiết lập {base}:",
        "- Được duyệt: " + (stat_line(j_ok) if j_ok else "chưa có lệnh"),
        "- Bị từ chối: " + (stat_line(j_no) if j_no else "chưa có lệnh"),
    ]
    if not j_ok or not j_no or j_ok["n"] < 2 or j_no["n"] < 2:
        lines.append("\nChưa đủ lệnh ở một trong hai nhóm để so sánh.")
        return "\n".join(lines)
    a_r = [x["r"] for x in ok_rows]
    b_r = [x["r"] for x in no_rows]
    ma, sa = mean_sd(a_r)
    mb, sb = mean_sd(b_r)
    se = math.sqrt(sa ** 2 / len(a_r) + sb ** 2 / len(b_r))
    d = ma - mb
    lo, hi = d - 1.96 * se, d + 1.96 * se
    lines.append(
        f"\nChênh lệch kỳ vọng (duyệt trừ từ chối): {d:+.2f}R, khoảng tin cậy 95% xấp xỉ từ {lo:+.2f}R đến {hi:+.2f}R."
    )
    if min(len(a_r), len(b_r)) < need:
        lines.append(
            f"Chưa đủ lệnh: cần ít nhất {need} lệnh mỗi nhóm (hiện {len(a_r)} và {len(b_r)}). "
            f"Với ít lệnh như vậy mọi chênh lệch đều có thể chỉ là nhiễu."
        )
    elif lo > 0:
        lines.append(
            "Nhóm được duyệt tốt hơn rõ rệt: hội đồng có vẻ lọc được lệnh. "
            "Hãy kiểm tra lại trên giai đoạn mới trước khi tin."
        )
    elif hi < 0:
        lines.append(
            "Nhóm bị từ chối lại tốt hơn: hội đồng đang loại nhầm lệnh tốt, hoặc đang chọn ngược."
        )
    else:
        lines.append(
            "Chưa phân biệt được hai nhóm: hội đồng chưa chứng minh được giá trị lọc lệnh. "
            "Nó vẫn có thể hữu ích như bảng kiểm tra rủi ro."
        )
    lines.append(
        "\nLưu ý: các lệnh trong cùng một đợt thị trường tương quan với nhau nên khoảng tin cậy thật rộng hơn con số này. "
        "Chỉ so sánh khi bạn đã chạy --shadow cho mọi lệnh bạn cân nhắc, không chỉ lệnh trông đẹp."
    )
    return "\n".join(lines)


# ---------- Nhật ký và thống kê ----------

def build_log_parser(quiet=False):
    ap = make_parser(quiet, prog="fill.py log", description="Ghi một lệnh đã đóng vào journal.csv.")
    ap.add_argument("--setup", required=True, help="tên thiết lập, ví dụ breakout4h")
    ap.add_argument("--symbol", required=True, help="mã, ví dụ SOL")
    ap.add_argument("--r", type=float, default=None,
                    help="kết quả đã tính ra R (dùng dấu bằng nếu âm, ví dụ --r=-1). Nếu bỏ qua, nhập --entry --stop --exit")
    ap.add_argument("--entry", type=float, help="giá vào thực tế")
    ap.add_argument("--stop", type=float, help="giá dừng lỗ ban đầu")
    ap.add_argument("--exit", type=float, dest="exit_", help="giá thoát thực tế")
    ap.add_argument("--real", action="store_true", help="đánh dấu là lệnh thật (mặc định là giả lập)")
    ap.add_argument("--note", default="", help="ghi chú ngắn")
    ap.add_argument("--date", default=None, help="ngày, mặc định là hôm nay (YYYY-MM-DD)")
    return ap


def do_log(a):
    setup = norm_setup(a.setup)
    if not setup:
        return "Tên thiết lập không hợp lệ."
    st = load_settings()
    if a.r is not None:
        r = a.r
        detail = f"R nhập tay: {r:+.2f}"
    else:
        if None in (a.entry, a.stop, a.exit_) or a.entry == a.stop:
            return "Cần --r, hoặc đủ --entry --stop --exit (entry khác stop)."
        gross = (a.exit_ - a.entry) / (a.entry - a.stop)
        cost_r = 2 * (st["fee_pct"] + st["slip_pct"]) / 100 * a.entry / abs(a.entry - a.stop)
        r = gross - cost_r
        detail = f"R trước chi phí {gross:+.2f}, trừ chi phí {cost_r:.2f}, R ròng {r:+.2f}"
    row = {
        "date": a.date or datetime.date.today().isoformat(),
        "symbol": norm_base(a.symbol), "setup": setup, "r": f"{r:.4f}",
        "kind": "real" if a.real else "paper", "note": a.note,
    }
    append_journal(row)
    j = journal_stats(read_journal(setup))
    return (
        f"Đã ghi vào journal.csv ({row['kind']}): {row['symbol']}, thiết lập {setup}. {detail}\n"
        f"Thiết lập {setup}: {stat_line(j)}. Cần {st['min_trades']} lệnh để gọi hội đồng."
    )


def build_stats_parser(quiet=False):
    ap = make_parser(quiet, prog="fill.py stats", description="Thống kê nhật ký theo từng thiết lập.")
    ap.add_argument("--setup", default=None, help="chỉ xem một thiết lập")
    return ap


def do_stats(a):
    st = load_settings()
    rows = read_journal(norm_setup(a.setup) if a.setup else None)
    if not rows:
        return "Nhật ký trống. Ghi lệnh bằng: log --setup ten --symbol SOL --entry ... --stop ... --exit ..."
    groups = {}
    for r in rows:
        groups.setdefault(r["setup"], []).append(r)
    lines = []
    for name, rs in sorted(groups.items()):
        j = journal_stats(rs)
        status = "đủ lệnh" if j["n"] >= st["min_trades"] else f"chưa đủ ({j['n']}/{st['min_trades']})"
        lines.append(f"- {name}: {stat_line(j)} | {status}")
    lines.append("\nNếu bạn đổi quy tắc của một thiết lập, hãy dùng tên mới để không trộn dữ liệu cũ và mới.")
    return "\n".join(lines)


# ---------- Dòng lệnh ----------

def cmd_init(argv):
    ap = argparse.ArgumentParser(prog="fill.py init", description="Tạo hoặc cập nhật settings.json (làm một lần).")
    ap.add_argument("--account", type=float, help="vốn (USD)")
    ap.add_argument("--risk", type=float, help="rủi ro tối đa mỗi lệnh, tính theo phần trăm")
    ap.add_argument("--fee", type=float, help="phí mỗi chiều, tính theo phần trăm")
    ap.add_argument("--slip", type=float, help="trượt giá mỗi chiều, tính theo phần trăm")
    ap.add_argument("--interval", choices=list(INTERVAL_MS), help="khung nến để tính chỉ báo")
    ap.add_argument("--min-trades", type=int, dest="min_trades", help="số lệnh nhật ký tối thiểu trước khi gọi hội đồng")
    a = ap.parse_args(argv)
    s = load_settings()
    for key, val in (
        ("account", a.account), ("risk_pct", a.risk), ("fee_pct", a.fee),
        ("slip_pct", a.slip), ("interval", a.interval), ("min_trades", a.min_trades),
    ):
        if val is not None:
            s[key] = val
    save_settings(s)
    print(f"Đã lưu {SETTINGS_FILE}:")
    for k, v in s.items():
        print(f"- {k}: {v}")


def cmd_ask(argv):
    a = build_ask_parser().parse_args(argv)
    res = asyncio.run(build_request(a))
    print(res["text"])
    if res["question"]:
        print("\nCâu hỏi gửi cho hội đồng:\n" + res["question"] + "\n")
        full = asyncio.run(run(res["question"]))
        if res.get("shadow"):
            print("\n" + asyncio.run(shadow_after(res["plan"], full)))


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "init":
        cmd_init(argv[1:])
    elif argv and argv[0] == "log":
        print(do_log(build_log_parser().parse_args(argv[1:])))
    elif argv and argv[0] == "stats":
        print(do_stats(build_stats_parser().parse_args(argv[1:])))
    elif argv and argv[0] == "compare":
        print(do_compare(build_compare_parser().parse_args(argv[1:])))
    else:
        cmd_ask(argv)


if __name__ == "__main__":
    main()
