# COUNCIL V2 — Narrative Trend + Liquidity Sweep

## Bản nâng cấp dựa trên council.zip ngày 16/11/2026

**Chế độ:** chỉ QUÉT dữ liệu, đánh giá AI và **PAPER TRADING**. Không có endpoint tạo lệnh Binance thật.

### File được sửa

- `autoshadow.py`: triển khai V2 trực tiếp; `scan_signals()` đo Market Regime / Narrative / Sweep; `process_signals()` chặn sự kiện bằng code, ghi log đầy đủ và đưa tín hiệu đủ điều kiện vào Council.
- `pullback.py`: thêm `pullback_sweep_signal()`; giữ `pullback_signal()` nguyên vẹn để tham chiếu V1.
- `fill.py`: truyền Market Regime / Narrative / Sweep vào prompt và metadata kế hoạch paper.
- `council.py`: prompt cho 5 vai thẩm định nội dung mới; **giữ nguyên** 6 vai và các model trong bản gốc mới nhất, SearXNG/RSS; sửa các biến toàn cục về trạng thái per-request.
- `paper.py`: chặn mở mọi vị thế giả lập trong cửa sổ 24h sự kiện, kể cả `/open` thủ công; chống mở trùng theo `signal_id`; lưu context V2 trong position.
- `scanner.py`: dùng thư mục cache cạnh file script, không phụ thuộc working directory.
- `selftest_auto.py`: dry-run an toàn, không còn bypass cổng sự kiện.
- `start_bot.bat`: chạy trong thư mục thực tế chứa file BAT, không hardcode tên người dùng.

### File mới

- `liquidity.py`: xác nhận bullish sweep trên nến 4H đã đóng, pivot lịch sử được xác nhận trước sweep.
- `market_regime.py`: kiểm tra xu hướng BTC trên nến ngày đã đóng (`Close > MA50`, `MA20 > MA50`, `MA50` không giảm).
- `narrative.py`: điểm sức mạnh nhóm ngành dựa vào chênh lệch giá 7 ngày so với BTC (45%), độ rộng xu hướng (35%), khối lượng (20%).
- `narrative_map.json`: bảng ánh xạ thủ công các coin thuộc AI, DeFi, GameFi, RWA, Layer1, Layer2; cần cập nhật và kiểm chứng thường xuyên.
- `event_gate.py`: đọc `events.txt` theo **giờ địa phương của máy**, chặn nếu sự kiện còn không quá 24h; thiếu/hỏng/hết lịch thì fail-closed.
- `tests/test_upgrade.py`: bộ test offline.
- `research_signals_v2.jsonl`: file runtime **được tự tạo** khi chạy quét thật; không bao gồm trong ZIP.

### Cài đặt lên folder gốc Windows

1. Dừng bot đang chạy (`Ctrl+C`). **Sao lưu toàn bộ folder** `C:\Users\Bui Duc Loc\council` sang một thư mục dự phòng (bao gồm `paper_positions.json`, `journal.csv`, `events.txt`, `settings.json`, các file log và `klines_cache`).
2. Giải nén ZIP này, mở folder `council/`, **copy các file `.py` và `narrative_map.json`, `requirements.txt`** vào thư mục dự án gốc (ghi đè code cũ sau khi đã sao lưu). Giữ nguyên `events.txt`, `settings.json`, `paper_positions.json`, logs và cache trên máy.
3. Chạy ở PowerShell:
   ```powershell
   cd "C:\Users\Bui Duc Loc\council"
   py -m pip install -r requirements.txt
   py -m pytest -q tests
   py autoshadow.py status
   py autoshadow.py once --dry
   ```
4. Kiểm tra `events.txt` là lịch sự kiện **thực tế**, thời gian được nhập theo giờ hệ điều hành Windows. Nếu lịch sai, bot có thể chặn sai. Nếu file không có sự kiện sắp tới, V2 mặc định **không mở vị thế** đến khi cập nhật lịch.
5. Kiểm tra `ollama list`, truy cập Ollama và SearXNG theo `council.py`; Telegram cần các biến môi trường `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `AUTOSHADOW=1` như bản gốc. Khởi động theo cách đang dùng (`start_bot.bat` hoặc `py telegram_bot.py`).

### Dòng điều phối

Nến đã đóng 4H → Pullback V1 + Liquidity Sweep → BTC daily regime → Narrative Score + RS coin → **event_gate** → `fill.build_request` → `council.run_council` (SearXNG + Ollama) → `fill.shadow_after` → `paper.open_position` (kiểm tra event lần nữa) → `journal.csv`.

### Các tên setup

- Chiến lược cũ: `pullback_a_ok` và `pullback_a_no` (dữ liệu trên máy bạn được giữ nguyên).
- Chiến lược mới: `pullback_v2_ok` và `pullback_v2_no`.
- **Lưu ý:** `NO_TRADE` do code từ chối **không** mở paper position; chỉ từ chối do ý kiến AI sau các cổng kiểm tra mới có thể vào `pullback_v2_no`.

### Lưu ý về nghiên cứu và giới hạn

- Narrative Score là **proxy từ giá/khối lượng**, không đo dòng tiền on-chain hoặc tiền luân chuyển giữa ngành thật.
- Bảng danh mục coin được ánh xạ thủ công; vì universe chỉ là Top 40 coin theo thanh khoản hiện tại, một nhóm có dưới 2 coin được phủ sẽ bị loại. Điều này có thể làm tần suất tín hiệu rất thấp.
- Cần làm backtest out-of-sample có kiểm soát look-ahead và survivorship bias, không khẳng định chiến lược có lãi.
- Chỉ nến 4H đóng và pivot đã xác nhận mới được dùng. Tín hiệu V2 yêu cầu **đồng thời** Pullback và Bullish Sweep (Sweep có thể ở 1–2 nến trước nến xác nhận).
- Sự kiện trong file `events.txt` là dữ liệu người dùng khai báo, không được SearXNG tự kiểm chứng/cập nhật; SearXNG vẫn chỉ làm nguồn **tin tức đánh giá bởi News Analyst** sau khi event gate cho phép.
- Nhiều tiến trình cùng sửa `paper_positions.json` có thể gây race condition; chỉ chạy **một** Telegram/autoshadow worker trên máy, không chạy song song các instance.
- `Entry` trong paper là giá đóng nến tín hiệu với kiểm tra độ lệch thời gian thực; đó là **mô phỏng lý thuyết**, không bảo đảm giá khớp thực tế.
- Trước khi đánh giá hiệu quả, cần đủ mẫu V1/V2 và phân tách tác động của Narrative / Sweep / AI bằng thiết kế đối chứng.

### Tham số nghiên cứu chỉnh tại đâu?

- `liquidity.py`: `sweep_window`, `min_depth_atr`, `max_depth_atr`, `min_close_position`.
- `narrative.py`: `MIN_GROUP_MEMBERS`, `MIN_SCORE`, `MIN_COIN_REL_PCT`, các trọng số trong `calculate_scores`.
- `market_regime.py`: điều kiện `classify`.
- `event_gate.py`: `BLOCK_BEFORE_HOURS` (mặc định 24), không thay bằng 0 để bypass.
- `autoshadow.py`: `SETUP`, `MAX_PER_CYCLE`, `HOLD_HOURS`.

### Kiểm tra nhanh các model đã giữ nguyên

- News: `qwen3:8b`; Technical: `gemma3:4b`; Quant: `phi4-mini:latest`; Devil: `llama3.2:3b`; Risk: `qwen3:4b`; Chair: `qwen3:8b`.
- Ollama `http://localhost:11434` và SearXNG `http://127.0.0.1:8080/search` giữ nguyên trong code. Nếu Ollama/SearXNG chạy ở container hoặc host khác, đổi URI đúng theo môi trường của bạn.
