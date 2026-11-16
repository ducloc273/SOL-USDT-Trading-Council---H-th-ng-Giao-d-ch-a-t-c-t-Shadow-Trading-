# AI Council V2.2 — Sửa lỗi và quản lý độ trễ

Nguồn: vá từ **AI_Council_V2_1_Source_Code.zip** (bản đã bàn giao) và đối chiếu với `council.zip` gốc. Bản này CHỈ hỗ trợ paper trading, không đặt lệnh thật. Lưu ý: V2.2 là **bản sửa phần mềm**, không phải bằng chứng hiệu quả giao dịch.

## Các lỗi đã sửa

1. `diagnose.py replay`: dùng `autoshadow.signal_key(base, bar)` với hậu tố `STRATEGY_VERSION`. Không còn so sánh với dạng V1 `base:bar`; chỉ phản ánh trạng thái được lưu trong `autoshadow_state.json` (tối đa 2.000 key mới nhất). Event block, LIMIT hay timeout cũng tính là *đã xử lý tín hiệu* chứ KHÔNG có nghĩa đã mở vị thế.
2. `autoshadow.py`: loại bỏ `load_events()`, `event_text()`, `STALE_MS`, `GUARD_NEWS` và monkey-patch `council.gather_news` không cần thiết. Chỉ có `event_gate.assess()` kiểm tra lịch. SearXNG và News Analyst trong `council.py` giữ nguyên.
3. `research_compare_v21.py`: gọi `pullback.configure()` đọc `settings.json` trước khi tính kỳ vọng; `--fee` và `--slip` là tham số tùy chọn **đơn vị % mỗi chiều**, ghi rõ chi phí đã sử dụng. Không còn `defaultdict` không dùng.
4. `council.py`: hỗ trợ `COUNCIL_MODEL_PROFILE=original|compact|lite`. **Mặc định `original` để không âm thầm thay 5 model** trong bản ZIP gốc. `compact`: mọi vai dùng `qwen3:8b`; `lite`: mọi vai dùng `gemma3:4b`. Cả 6 vai trò, các prompts, logic `Risk+Chair`, và SearXNG vẫn được giữ. `COUNCIL_OLLAMA_CONCURRENCY=1` mặc định để không tải nhiều model đồng thời; chế độ original dùng `keep_alive=0` để giải phóng VRAM, compact/lite dùng 15 phút mặc định (tùy chỉnh `COUNCIL_KEEP_ALIVE`). Việc dùng chung model làm mất tính độc lập giữa các agent — cần ghi nhận khi so sánh kết quả thực nghiệm.
5. `autoshadow.py`: mặc định tối đa **3** ứng viên/chu kỳ (thay 10), timeout Council **360 s/tín hiệu**, ngân sách chu kỳ **480 s**, chặn paper khi quá **600 s kể từ lúc nến 4H đóng**. Phần dư được log `LIMIT`, phần timeout `COUNCIL_TIMEOUT`, tín hiệu cũ `LATE_SIGNAL` hoặc `LATE_AFTER_AI`. **Các trạng thái bị loại này được ghi processed, không có lệnh paper.** Người dùng có thể chỉnh các ngưỡng môi trường.
6. `paper.py`: kiểm tra lại ở bước ghi vị thế: tuổi nến, lệch giá hiện tại so với entry nến đóng (mặc định 0,35 ATR), ngoài cổng sự kiện. Vẫn lưu `signal_close_price` như entry *giả lập lý thuyết*; đây **không phải giá khớp thực**. Nếu lệch quá ngưỡng, không tạo paper position.
7. `telegram_bot.py`: biến `busy` chỉ áp dụng cho yêu cầu Council thủ công, không khóa suốt chu kỳ tự động. Các lệnh `/positions`, `/stats`, `/open` vốn không bị cờ `busy` chặn; yêu cầu AI có thể chờ tới lượt gọi Ollama, không bảo đảm phản hồi tức thì. Không chạy nhiều process bot.

## Cài đặt

1. **Dừng bot**, sao lưu toàn bộ thư mục dự án hiện tại.
2. Chỉ sao chép source-code từ ZIP, không ghi đè `settings.json`, `events.txt`, `autoshadow_state.json`, `paper_positions.json`, `journal.csv`, `shadow_log.csv`, `research_signals_v21.jsonl`, `klines_cache`, `.env` hay các tệp dữ liệu khác.
3. Kiểm tra offline:

```powershell
cd "C:\Users\Bui Duc Loc\council"
py -m pip install -r requirements.txt
py -m pytest -q tests\test_upgrade.py
py autoshadow.py status
py autoshadow.py once --dry
py research_compare_v21.py
py diagnose.py replay --days 7
```

4. Muốn giảm nạp/xả model trên GPU 8GB, chọn **rõ ràng** chế độ compact trước khi chạy bot:

```powershell
$env:COUNCIL_MODEL_PROFILE = 'compact'       # dùng qwen3:8b cho 6 vai
$env:COUNCIL_OLLAMA_CONCURRENCY = '1'
$env:COUNCIL_MAX_PER_CYCLE = '3'            # 1..10
$env:COUNCIL_MAX_SIGNAL_AGE_S = '600'       # 10 phút từ lúc nến đóng
$env:COUNCIL_MAX_CYCLE_SECONDS = '480'
$env:COUNCIL_PER_SIGNAL_TIMEOUT_S = '360'
$env:COUNCIL_MAX_ENTRY_DRIFT_ATR = '0.35'
# Chạy bot theo quy trình cũ của bạn tại chính PowerShell này
```

Nếu muốn khôi phục đủ 5 model với 6 vai:

```powershell
$env:COUNCIL_MODEL_PROFILE = 'original'
```

**Các biến môi trường chỉ có hiệu lực với process khởi động sau khi thiết lập.** Không cần cài lại Ollama. Các model của chế độ compact/lite cần có trong `ollama list`.

## An toàn giao dịch & hạn chế

- `events.txt` vẫn được kiểm tra bởi `event_gate.py`: 24 giờ trước / 2 giờ sau sự kiện; thiếu hoặc hết lịch thì bot tự động không mở paper.
- V1/V2.1 signal-id **giữ nguyên** để không mở lại tín hiệu cũ sau cập nhật. Dòng log mới có `code_release = 2.2`; cần tách kết quả trước/sau thay đổi cấu hình trong phân tích.
- Mặc định cap 3 làm **giảm số mẫu được AI chấm** khi có nhiều tín hiệu; không tự động gọi bù đã bị bỏ qua. Ưu tiên an toàn và minh bạch; muốn thu thập nhiều mẫu hơn phải đo độ trễ rồi chỉnh cap/tuổi tín hiệu.
- Dù đã có giới hạn tuổi nến và drift, hệ thống chưa mô phỏng khớp lệnh/độ sâu sổ lệnh hoàn chỉnh; giá entry lưu vẫn là giá đóng nến, không phải báo giá giao dịch thực.
- `paper_positions.json` chưa có khóa ghi liên tiến trình; chỉ chạy **một bot worker**. Lệnh mở tay và tự động có thể tranh chấp ghi nếu thực hiện đúng cùng thời điểm; cần SQLite/transaction trước khi chạy nhiều worker.
- Chưa kiểm thử thực tế với máy Windows, Ollama, SearXNG, Binance hay Telegram. Các phép đo thời gian 30–90s/coin chỉ là ước đoán trước đó, không phải benchmark của V2.2.
- Các thay đổi profile model có thể làm khác kết quả GO/NO_TRADE; cần log/ghi lại chế độ model khi đánh giá thí nghiệm.

## Dữ liệu gửi kèm

Chỉ có mã nguồn, file cấu hình mẫu, hướng dẫn, bộ kiểm thử. Không kèm token Telegram, file lịch sự kiện thực, lịch sử lệnh, dữ liệu người dùng hoặc cache thị trường.
