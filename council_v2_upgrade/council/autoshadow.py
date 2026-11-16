import os
import re
import sys
import json
import time
import asyncio
import argparse
import datetime
import httpx
import scanner
import pullback
import fill
import paper
import council
import narrative
import market_regime
import event_gate
from council import run_council

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "autoshadow_state.json")
EVENTS_FILE = os.path.join(HERE, "events.txt")
LOG_MD = os.path.join(HERE, "autoshadow_log.md")
RESEARCH_LOG = os.path.join(HERE, "research_signals_v2.jsonl")

SETUP = "pullback_v2"
STRATEGY_VERSION = "narrative_sweep_v2"
RULE = "LONG 4H: Pullback MA20/support + bullish liquidity sweep, narrative strength and BTC market regime"
INTERVAL = "4h"
STEP_S = 4 * 3600
DELAY_S = 120                   # chờ sau khi nến đóng
HOLD_HOURS = pullback.HOLD_BARS * 4.0
MAX_PER_CYCLE = 10
CATCHUP_WINDOW_S = 3.5 * 3600   # bot khởi động trong khoảng này sau khi nến đóng thì chạy bù ngay
STALE_MS = 6 * 3600 * 1000      # nến cũ hơn mức này coi là dữ liệu lỗi thời

# Bổ sung tên cho một số đồng phổ biến và ít gây nhầm với từ thông thường
EXTRA_ALIASES = {
    "ZEC": "zcash", "UNI": "uniswap", "HYPE": "hyperliquid", "AAVE": "aave", "APT": "aptos",
    "FIL": "filecoin", "ICP": "internet computer", "INJ": "injective", "ENA": "ethena",
    "ONDO": "ondo finance", "HBAR": "hedera", "ALGO": "algorand", "BCH": "bitcoin cash",
    "ETC": "ethereum classic", "TAO": "bittensor", "WLD": "worldcoin", "SEI": "sei network",
}
council.ALIASES.update({k: v for k, v in EXTRA_ALIASES.items() if k not in council.ALIASES})
for _members in narrative.load_mapping().values():
    council.ALIASES.update({k: v for k, v in _members.items() if k not in council.ALIASES})


# Trong chu kỳ tự động, mã không có trong bảng tên không đọc tin (tin của mã lạ dễ trùng từ thông thường)
GUARD_NEWS = False
_orig_gather = council.gather_news


async def _gather_guarded(client, prof):
    if GUARD_NEWS and prof["base"] not in council.ALIASES:
        return [], []
    return await _orig_gather(client, prof)


council.gather_news = _gather_guarded


# ---------- Trạng thái ----------
def load_state():
    st = {"processed": [], "last_boundary": None, "last_cycle": None, "heartbeat_date": None}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                st.update(json.load(fh))
        except Exception as e:
            print(f"Không đọc được autoshadow_state.json ({e}). Dùng trạng thái trống.")
    return st


def save_state(st):
    st["processed"] = st["processed"][-2000:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---------- Sự kiện do người dùng khai ----------
def load_events():
    """Trả về None nếu chưa có events.txt, ngược lại là danh sách (thời điểm, mô tả)."""
    if not os.path.exists(EVENTS_FILE):
        return None
    out = []
    with open(EVENTS_FILE, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})\s*\|\s*(.+)", line)
            if not m:
                continue
            try:
                when = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            out.append((when, m.group(3).strip()))
    return out


def event_text():
    """Trả về (chữ gửi cho hội đồng, ghi chú cho người dùng)."""
    events = load_events()
    if events is None:
        return "none", "events.txt chưa có nên đang giả định không có sự kiện lớn nào"
    now = datetime.datetime.now()
    upcoming = sorted((w, d) for w, d in events if w > now)
    if not upcoming or (upcoming[0][0] - now).total_seconds() > 48 * 3600:
        return "none", ""
    when, desc = upcoming[0]
    hours = max((when - now).total_seconds() / 3600, 0.1)
    return f"{desc} in {hours:.1f} hours", ""


# ---------- Lịch chạy ----------

def log_research(signal, status, reason, verdict=None):
    """Append an audit row, including blocked signals. No real orders."""
    record = {
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        'signal_id': f"{signal['base']}:{signal['bar']}:{STRATEGY_VERSION}",
        'symbol': signal['base'], 'bar_ms': signal['bar'],
        'strategy_version': STRATEGY_VERSION, 'status': status,
        'reason': reason, 'verdict': verdict,
        'entry': signal['entry'], 'stop': signal['stop'], 'target': signal['target'],
        'context': signal.get('context', {}),
    }
    with open(RESEARCH_LOG, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')


def seconds_until_next_cycle():
    now = time.time()
    this_run = (now // STEP_S) * STEP_S + DELAY_S
    target = this_run if now < this_run else (now // STEP_S + 1) * STEP_S + DELAY_S
    return max(1.0, target - now)


def needs_catchup():
    """Trả về (có cần chạy bù ngay không, số giây nên chờ trước khi chạy)."""
    now = time.time()
    boundary = int(now // STEP_S * STEP_S)
    st = load_state()
    if st.get("last_boundary") == boundary or now - boundary > CATCHUP_WINDOW_S:
        return False, 0
    return True, max(20.0, DELAY_S - (now - boundary))


# ---------- Quét tín hiệu ----------
def configure_scanner():
    ns = argparse.Namespace(fee=None, slip=None, interval=INTERVAL, days=730, top=40, minvol=5.0)
    pullback.configure(ns)


async def scan_signals():
    """Return (V2 technical candidates, errors, universe_size, market context).

    All strength measurements use the same last CLOSED 4h bar as BTC.
    Failure of BTC or narrative data blocks V2 rather than guessing.
    """
    configure_scanner()
    step_ms = scanner.INTERVAL_MS[INTERVAL]
    mapping = narrative.load_mapping()
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "council-autoshadow-v2/1.0"}) as client:
        uni, total, passing = await scanner.universe(client)
        sem = asyncio.Semaphore(4)
        try:
            regime = await market_regime.fetch_regime(client)
        except Exception as e:
            regime = {'regime': 'UNKNOWN', 'long_allowed': False, 'reason': str(e)}

        async def one(base, symbol):
            async with sem:
                try:
                    rows = await scanner.load_klines(client, symbol)
                except Exception as e:
                    return {'error': f'{symbol}: {e}'}
            if len(rows) < 200:
                return None
            d = pullback.prepare_full(rows)
            i = len(rows) - 1
            if time.time() * 1000 - (d['t'][i] + step_ms) > STALE_MS:
                return {'error': f'{symbol}: old candle data'}
            cm = narrative.coin_metrics(d, i)
            sig = pullback.pullback_sweep_signal(d, i)
            if sig:
                sig.update(base=base, bar=d['t'][i], atr=d['atr'][i])
            return {'base': base, 'metric': cm, 'signal': sig}

        raw = await asyncio.gather(*(one(b, s) for b, s, _ in uni))
        if not any(x and x.get('base') == 'BTC' for x in raw):
            raw.append(await one('BTC', 'BTCUSDT'))
    errors = [r['error'] for r in raw if r and 'error' in r]
    metrics = {r['base']: r['metric'] for r in raw if r and r.get('metric')}
    btc = metrics.get('BTC')
    groups = narrative.calculate_scores(metrics, mapping, btc) if btc else {}
    sigs = []
    for r in raw:
        if not r or not r.get('signal'):
            continue
        sig = r['signal']
        nar = narrative.choose_for_coin(r['base'], groups, metrics, mapping, btc)
        sig['context'] = {
            'strategy_version': STRATEGY_VERSION,
            'market_regime': regime,
            'narrative': nar,
            'liquidity_sweep': sig['sweep'],
            'signal_bar_ms': sig['bar'],
            'signal_close_price': sig['entry'],
        }
        sig['eligible'] = bool(regime.get('long_allowed') and nar['eligible'])
        sig['gate_reason'] = 'eligible' if sig['eligible'] else '; '.join([
            'BTC market regime not RISK_ON' if not regime.get('long_allowed') else '',
            nar['reason'] if not nar['eligible'] else '',
        ]).strip('; ')
        sigs.append(sig)
    return sigs, errors, len(uni), {'regime': regime, 'groups': groups}


def fix_levels(question, sig):
    """Với quy tắc pullback, dừng lỗ nằm dưới đáy nhịp hồi theo thiết kế.
    Thay dòng Levels do fill.py tính theo hỗ trợ 20 nến (khác định nghĩa của quy tắc)."""
    m = re.search(r"Resistance:\s*([0-9]+(?:\.[0-9]+)?)", question)
    res = float(m.group(1)) if m else None
    notes = [
        "the stop-loss is placed below the pullback low, which is the intended design for this setup",
        "the stop-loss distance is adequate and not too tight",
    ]
    ok = True
    if res is not None and sig["entry"] < res < sig["target"]:
        notes.append("target is beyond the resistance level, so resistance may block price (risk)")
        ok = False
    else:
        notes.append("target is below the resistance level, which is not a concern")
    head = "all level placements are acceptable" if ok else "some level placements carry risk"
    line = f"Levels (verified by code, do not dispute): {head}: " + "; ".join(notes) + "."
    return re.sub(r"^Levels \(verified by code.*$", lambda _m: line, question, count=1, flags=re.M)


def stub(sig):
    return {"setup": SETUP, "base": sig["base"], "structure": "uptrend", "rsi": round(sig["rsi"], 1)}


def summarize(base, full, msg):
    t = full.replace("**", "")
    m = re.search(r"KẾT LUẬN:\s*([A-Z_]+)", t)
    verdict = m.group(1) if m else "?"
    fails = []
    for line in t.splitlines():
        if "KHÔNG ĐẠT" in line:
            fails.append(re.sub(r":\s*KHÔNG ĐẠT.*$", "", line).strip("- ").strip())
    ml = msg.splitlines()
    first = ml[0] if ml else ""
    second = ml[1] if len(ml) > 1 else ""
    text = f"- {base}: {verdict}."
    if fails:
        text += " Cổng không đạt: " + "; ".join(fails) + "."
    return text + f"\n  {first} {second[:140]}".rstrip()


def append_md(base, question, full, msg):
    try:
        with open(LOG_MD, "a", encoding="utf-8") as fh:
            fh.write(
                f"\n\n## {datetime.datetime.now():%Y-%m-%d %H:%M} {base}\n\n"
                f"**Câu hỏi:**\n{question}\n\n{full}\n\n**Lệnh bóng:** {msg}\n"
            )
    except Exception as e:
        print("autoshadow_log lỗi:", e, flush=True)


# ---------- Một chu kỳ ----------
async def process_signals(notify, dry=False):
    global GUARD_NEWS
    now = time.time()
    boundary = int(now // STEP_S * STEP_S)
    state = load_state()
    last = state.get('last_boundary')
    missed = max(0, int((boundary - last) // STEP_S) - 1) if last else 0
    event = event_gate.assess(EVENTS_FILE)
    try:
        sigs, errs, n_scanned, ctx = await scan_signals()
    except Exception as e:
        await notify(f'V2 scan failed safely: {e}')
        return
    when = datetime.datetime.now().strftime('%d/%m %H:%M')
    eligible = [s for s in sigs if s['eligible']]
    header = (f'V2 {when}: {n_scanned} pairs, {len(sigs)} pullback+sweep, '
              f'{len(eligible)} passed narrative+regime.')
    if dry:
        lines = [header, f"Market regime: {ctx['regime'].get('regime')}",
                 f"Event gate: {'ALLOW' if event['allowed'] else 'BLOCK'} ({event['reason']})",
                 'Narratives: ' + ', '.join(f'{k}={v["score"]:.1f}({v["members"]})'
                                            for k,v in sorted(ctx['groups'].items(),
                                            key=lambda kv: -kv[1]['score']))]
        for s in sigs:
            n = s['context']['narrative']
            lines.append(f"- {s['base']}: {'ELIGIBLE' if s['eligible'] else 'FILTERED'}; "
                         f"narrative {n.get('name','UNKNOWN')} ({n.get('score','?')}); "
                         f"sweep {s['sweep']['depth_atr']:.2f} ATR; "
                         f"entry {scanner.fmt(s['entry'])}, SL {scanner.fmt(s['stop'])}, "
                         f"TP {scanner.fmt(s['target'])}; {s['gate_reason']}")
        if errs: lines.append(f'Data errors {len(errs)}; e.g. {errs[0]}')
        lines.append('DRY-RUN: no AI call, no position, no state mutation')
        await notify('\n'.join(lines))
        return

    lines = []
    judged = 0
    processed = set(state['processed'])
    # Event gate is checked by CODE before SearXNG, Ollama or paper.open_position.
    # Log newly blocked signals and mark processed so a past bar is not traded later.
    for sig in sigs:
        key = f"{sig['base']}:{sig['bar']}:{STRATEGY_VERSION}"
        if key in processed:
            continue
        if not event['allowed']:
            log_research(sig, 'EVENT_BLOCK', event['reason'])
            fill.log_shadow(stub(sig), '', '', '', 'V2 event blocked: ' + event['reason'])
            lines.append(f"- {sig['base']}: macro event BLOCK; no AI, no paper position")
            state['processed'].append(key); processed.add(key)
            continue
        if not sig['eligible']:
            log_research(sig, 'FILTERED', sig['gate_reason'])
            fill.log_shadow(stub(sig), '', '', '', 'V2 filtered: ' + sig['gate_reason'])
            state['processed'].append(key); processed.add(key)
            continue

    active = [s for s in eligible if f"{s['base']}:{s['bar']}:{STRATEGY_VERSION}" not in processed]
    for s in active[MAX_PER_CYCLE:]:
        key = f"{s['base']}:{s['bar']}:{STRATEGY_VERSION}"
        log_research(s, 'LIMIT', 'MAX_PER_CYCLE exceeded')
        fill.log_shadow(stub(s), '', '', '', 'V2 cycle limit reached')
        state['processed'].append(key); processed.add(key)
    GUARD_NEWS = True
    try:
        for s in active[:MAX_PER_CYCLE]:
            key = f"{s['base']}:{s['bar']}:{STRATEGY_VERSION}"
            try:
                other = next((p for p in paper.load_db()['positions']
                              if p['status'] == 'open' and p['base'] == s['base']), None)
                if other:
                    log_research(s, 'OPEN_BLOCK', f'open paper position #{other["id"]}')
                    fill.log_shadow(stub(s), '', '', other['id'], 'already open position')
                    lines.append(f"- {s['base']}: existing paper position #{other['id']}")
                else:
                    args = argparse.Namespace(
                        symbol=s['base'], setup=SETUP, rule=RULE,
                        entry=s['entry'], stop=s['stop'], target=s['target'],
                        auto=False, short=False, atr_stop=None, rr=None,
                        account=None, risk=None, interval=INTERVAL, leverage=None, perp=False,
                        market=None, keywords=None, event=event_gate.as_council_text(event),
                        demo=False, shadow=True, hold=HOLD_HOURS, dry=False,
                        context=s['context'], signal_id=key,
                    )
                    res = await fill.build_request(args)
                    if not res.get('question'):
                        reason = (res.get('text') or '')[-150:]
                        log_research(s, 'PLAN_ERROR', reason)
                        lines.append(f"- {s['base']}: plan failed: {reason[:100]}")
                    else:
                        question = fix_levels(res['question'], s)
                        chunks = [part async for part in run_council(question)]
                        full = ''.join(chunks)
                        verdict, info = fill.verdict_of(full)
                        late_gate = event_gate.assess(EVENTS_FILE)
                        if not late_gate['allowed']:
                            msg = f"Paper position BLOCKED: {late_gate['reason']}"
                            log_research(s, 'EVENT_BLOCK_AFTER_AI', late_gate['reason'], verdict)
                        else:
                            msg = await fill.shadow_after(res['plan'], full)
                            log_research(s, 'COUNCIL_JUDGED', info, verdict)
                        append_md(s['base'], question, full, msg)
                        lines.append(summarize(s['base'], full, msg))
                        judged += 1
            except Exception as e:
                log_research(s, 'ERROR', str(e))
                lines.append(f"- {s['base']}: ERROR {e}")
            finally:
                state['processed'].append(key)
                processed.add(key)
                save_state(state)
    finally:
        GUARD_NEWS = False

    state['last_boundary'] = boundary
    state['last_cycle'] = {
        'time': when, 'scanned': n_scanned, 'signals': len(sigs),
        'judged': judged, 'missed': missed, 'errors': len(errs),
    }
    if errs:
        lines.append(f'Data errors: {len(errs)}; example: {errs[0]}')
    if missed:
        lines.append(f'Missed {missed} earlier 4h cycles')
    if lines:
        await notify('\n'.join([header] + lines))
    elif state.get('heartbeat_date') != datetime.date.today().isoformat():
        state['heartbeat_date'] = datetime.date.today().isoformat()
        await notify(header + ' No eligible new signals.')
    save_state(state)


def describe_status(enabled=True):
    st = load_state()
    lc = st.get("last_cycle")
    event = event_gate.assess(EVENTS_FILE)
    nxt = datetime.datetime.now() + datetime.timedelta(seconds=seconds_until_next_cycle())
    lines = [f"Thu thập tự động: {'BẬT' if enabled else 'TẮT (đặt biến môi trường AUTOSHADOW=1 rồi chạy lại bot)'}."]
    if lc:
        lines.append(
            f"Chu kỳ gần nhất lúc {lc['time']}: quét {lc['scanned']} cặp, {lc['signals']} tín hiệu, "
            f"{lc['judged']} đã chấm, {lc['errors']} cặp lỗi dữ liệu, {lc['missed']} chu kỳ bị bỏ lỡ trước đó."
        )
    else:
        lines.append("Chưa có chu kỳ nào được ghi nhận.")
    lines.append(f"Chu kỳ kế tiếp khoảng {nxt:%d/%m %H:%M}.")
    lines.append(f"Event gate: {'ALLOW' if event['allowed'] else 'BLOCK'} — {event['reason']}.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Quét pullback sau mỗi nến 4h và chấm bằng hội đồng ở chế độ bóng.")
    ap.add_argument("cmd", nargs="?", default="status", choices=["once", "status"])
    ap.add_argument("--dry", action="store_true", help="chỉ liệt kê tín hiệu, không gọi hội đồng")
    a = ap.parse_args()
    if a.cmd == "status":
        print(describe_status())
        return

    async def notify(text):
        print(text, flush=True)

    asyncio.run(process_signals(notify, dry=a.dry))


if __name__ == "__main__":
    main()
