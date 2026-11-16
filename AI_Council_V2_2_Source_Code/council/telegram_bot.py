import os
import shlex
import asyncio
import datetime
import httpx
from council import run_council
from fill import (
    ArgError, build_ask_parser, build_log_parser, build_stats_parser, build_compare_parser,
    build_request, do_log, do_stats, do_compare, shadow_after,
)
from paper import (
    build_open_parser, build_close_parser, open_position, close_position,
    list_positions, check_all, WATCH_SECONDS,
)
import autoshadow

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT = int(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"
LOG_FILE = "telegram_log.md"
AUTO = os.environ.get("AUTOSHADOW") == "1"

busy = False

HELP = (
    "Lệnh:\n"
    "/fill SOL --setup breakout4h --auto --event none\n"
    "  Hỏi hội đồng, bot tự điền dữ liệu Binance. Thêm --rule \"quy tắc vào lệnh\", --perp, "
    "--leverage 3, --short, --market, --keywords. --dry chỉ xem kế hoạch, --demo chỉ thử phần mềm.\n"
    "  Thêm --shadow để đo hội đồng có giá trị không: sau khi hội đồng kết luận, bot tự mở vị thế giả lập "
    "vào nhóm được duyệt hoặc nhóm bị từ chối. Hãy dùng --shadow cho MỌI lệnh bạn cân nhắc.\n"
    "/compare --setup breakout4h\n"
    "  So sánh kết quả hai nhóm (cần ít nhất 30 lệnh mỗi nhóm).\n"
    "/auto\n"
    "  Xem tình trạng thu thập tự động (chu kỳ gần nhất, sự kiện đang khai).\n"
    "/open SOL --setup breakout4h --auto\n"
    "  Mở vị thế giả lập theo giá thật. Bot tự theo dõi và ghi kết quả vào nhật ký khi lệnh đóng "
    "(mặc định giữ tối đa 48 giờ, đổi bằng --hold 24).\n"
    "/positions\n"
    "  Xem các vị thế giả lập đang mở.\n"
    "/close 1\n"
    "  Đóng tay vị thế số 1 theo giá hiện tại.\n"
    "/log --setup breakout4h --symbol SOL --entry 150 --stop 142 --exit 166\n"
    "  Ghi tay một lệnh đã đóng (thêm --real nếu là lệnh thật).\n"
    "/stats\n"
    "  Xem thống kê nhật ký.\n"
    "Hoặc gửi một khối dữ liệu có nhãn (Asset, Entry, Stop-loss, ...) để hỏi hội đồng theo cách cũ."
)


def plain(text):
    return text.replace("**", "").replace("`", "")


def normalize(text):
    """Đổi các ký tự mà bàn phím điện thoại tự thay (gạch dài, ngoặc kép cong)."""
    for ch in ("—", "–"):
        text = text.replace(ch, "--")
    for ch in ("“", "”", "„"):
        text = text.replace(ch, '"')
    for ch in ("‘", "’"):
        text = text.replace(ch, "'")
    return text


def write_log(question, answer):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n\n## {stamp}\n\n**Câu hỏi:**\n{question}\n\n{answer}\n")


async def send(client, chat_id, text):
    text = plain(text).strip()
    if not text:
        return
    for i in range(0, len(text), 4000):
        r = await client.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text[i:i + 4000]},
        )
        if r.status_code != 200:
            print("sendMessage lỗi:", r.status_code, r.text[:200], flush=True)


def parse(builder, rest):
    try:
        argv = shlex.split(normalize(rest))
    except ValueError as e:
        return None, f"Dấu ngoặc kép không khớp: {e}"
    try:
        return builder(True).parse_args(argv), None
    except ArgError as e:
        return None, f"Lỗi tham số: {e}. Gõ /help để xem ví dụ."


async def stream_council(client, chat_id, question):
    full, buf = "", ""
    try:
        async for piece in run_council(question):
            full += piece
            starts_new = piece.startswith("---\n**▶") or piece.startswith("---\n**■")
            if starts_new and buf.strip():
                await send(client, chat_id, buf)
                buf = ""
            buf += piece
        await send(client, chat_id, buf)
    except Exception as e:
        full += f"\n(lỗi: {e})"
        await send(client, chat_id, f"Lỗi: {e}")
    finally:
        try:
            write_log(question, full)
        except Exception as e:
            print("log error:", e, flush=True)
    return full


async def handle_question(client, chat_id, question):
    global busy
    try:
        await send(client, chat_id, "Hội đồng bắt đầu họp, mỗi đợt sẽ gửi khi xong...")
        await stream_council(client, chat_id, question)
    finally:
        busy = False


async def handle_fill(client, chat_id, rest):
    global busy
    try:
        args, err = parse(build_ask_parser, rest)
        if err:
            await send(client, chat_id, err)
            return
        res = await build_request(args)
        await send(client, chat_id, res["text"])
        if res["question"]:
            await send(client, chat_id, "Hội đồng bắt đầu họp, mỗi đợt sẽ gửi khi xong...")
            full = await stream_council(client, chat_id, res["question"])
            if res.get("shadow"):
                await send(client, chat_id, await shadow_after(res["plan"], full))
    except Exception as e:
        await send(client, chat_id, f"Lỗi: {e}")
    finally:
        busy = False


async def reply_paper(client, chat_id, work):
    try:
        await send(client, chat_id, await work)
    except Exception as e:
        await send(client, chat_id, f"Lỗi: {e}")


async def handle_update(client, upd):
    global busy
    msg = upd.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    print(f"nhận tin từ chat_id={chat_id}: {text[:50]}", flush=True)
    if chat_id != ALLOWED_CHAT or not text:
        if text:
            print(f"bỏ qua tin này: chat_id {chat_id} khác với TELEGRAM_CHAT_ID đang đặt ({ALLOWED_CHAT})", flush=True)
        return

    parts = text.split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        await send(client, chat_id, HELP)
    elif cmd == "/auto":
        await send(client, chat_id, autoshadow.describe_status(AUTO))
    elif cmd == "/stats":
        args, err = parse(build_stats_parser, rest)
        await send(client, chat_id, err or do_stats(args))
    elif cmd == "/compare":
        args, err = parse(build_compare_parser, rest)
        await send(client, chat_id, err or do_compare(args))
    elif cmd == "/log":
        args, err = parse(build_log_parser, rest)
        await send(client, chat_id, err or do_log(args))
    elif cmd == "/open":
        args, err = parse(build_open_parser, rest)
        if err:
            await send(client, chat_id, err)
        else:
            await reply_paper(client, chat_id, open_position(args))
    elif cmd == "/close":
        args, err = parse(build_close_parser, rest)
        if err:
            await send(client, chat_id, err)
        else:
            await reply_paper(client, chat_id, close_position(args))
    elif cmd == "/positions":
        await reply_paper(client, chat_id, list_positions())
    elif cmd == "/fill":
        if busy:
            await send(client, chat_id, "Hội đồng đang bận, đợi xong rồi gửi lại.")
            return
        busy = True
        asyncio.create_task(handle_fill(client, chat_id, rest))
    elif cmd.startswith("/"):
        await send(client, chat_id, "Lệnh không rõ. Gõ /help để xem danh sách.")
    else:
        if busy:
            await send(client, chat_id, "Hội đồng đang bận, đợi xong rồi gửi lại.")
            return
        busy = True
        asyncio.create_task(handle_question(client, chat_id, text))


async def paper_loop(client):
    """Theo dõi các vị thế giả lập và báo khi có lệnh đóng."""
    await asyncio.sleep(10)
    while True:
        try:
            for text in await check_all():
                await send(client, ALLOWED_CHAT, text)
        except Exception as e:
            print("paper loop lỗi:", e, flush=True)
        await asyncio.sleep(WATCH_SECONDS)


async def auto_loop(client):
    """Sau mỗi nến 4h đóng, quét tín hiệu pullback và chấm bằng hội đồng ở chế độ bóng."""
    global busy

    async def notify(text):
        await send(client, ALLOWED_CHAT, text)

    catch, wait = autoshadow.needs_catchup()
    while True:
        if catch:
            await asyncio.sleep(wait)
            catch = False
        else:
            await asyncio.sleep(autoshadow.seconds_until_next_cycle())
        waited = 0
        while busy and waited < 1800:
            await asyncio.sleep(10)
            waited += 10
        if busy:
            await notify("Bỏ qua chu kỳ tự động vì bot đang bận quá 30 phút.")
            continue
        # "busy" is for one manual Council request at a time, not the full auto
        # cycle.  Automatic and manual model calls share the Ollama limiter;
        # basic Telegram commands remain responsive while auto scoring runs.
        try:
            await autoshadow.process_signals(notify)
        except Exception as e:
            print("autoshadow lỗi:", e, flush=True)
            await notify(f"Chu kỳ tự động gặp lỗi: {e}")


async def main():
    offset = None
    async with httpx.AsyncClient(timeout=70) as client:
        # Bỏ qua các tin cũ tồn đọng khi bot khởi động
        r = await client.get(f"{API}/getUpdates", params={"offset": -1})
        old = r.json().get("result", [])
        if old:
            offset = old[-1]["update_id"] + 1

        asyncio.create_task(paper_loop(client))
        if AUTO:
            asyncio.create_task(auto_loop(client))
            print("Thu thập tự động (autoshadow): BẬT", flush=True)
        else:
            print("Thu thập tự động (autoshadow): TẮT (đặt biến môi trường AUTOSHADOW=1 để bật)", flush=True)
        print("Telegram bot đang chạy. Nhấn Ctrl+C để dừng.", flush=True)
        while True:
            try:
                r = await client.get(
                    f"{API}/getUpdates",
                    params={"timeout": 50, "offset": offset},
                )
                data = r.json()
                if not data.get("ok", True):
                    print("getUpdates lỗi:", data.get("error_code"), data.get("description"), flush=True)
                    await asyncio.sleep(5)
                    continue
                updates = data.get("result", [])
            except Exception as e:
                print("poll error:", e, flush=True)
                await asyncio.sleep(5)
                continue

            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    await handle_update(client, upd)
                except Exception as e:
                    print("handle error:", e, flush=True)


asyncio.run(main())
