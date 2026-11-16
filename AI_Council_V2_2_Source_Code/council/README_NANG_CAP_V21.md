# AI Council V2.1 — Pullback + Context (không lọc cứng)

**Nguồn:** sửa từ bộ `council_v2_upgrade.zip`, vốn xuất phát từ `council.zip` người dùng gửi ngày 16/11/2026.
**Chỉ PAPER TRADING:** không có endpoint đặt lệnh giao dịch thật tại Binance.

## Tại sao có V2.1?

V2 yêu cầu đồng thời Pullback, Sweep, BTC 1D RISK_ON và Narrative STRONG. Điều này làm số cơ hội quá ít và khó tách tác động của từng bộ lọc. V2.1 khôi phục **chỉ Pullback gốc** làm điều kiện kỹ thuật tạo ứng viên. Sweep, BTC regime, Narrative vẫn được tính và đưa cho Council như **dữ kiện**, không được tự động veto chỉ vì vắng mặt hoặc `UNKNOWN`.

| Khía cạnh | V2.1 |
|---|---|
| Tín hiệu ứng viên | `pullback.pullback_signal` nguyên bản, nến 4H **đã đóng** |
| Liquidity sweep | `liquidity.bullish_sweep` tùy chọn; vắng mặt -> `confirmed: false` |
| Narrative | sức mạnh tương đối 7 ngày so với BTC (60%), thay đổi tương đối động lượng (25%), gia tốc khối lượng (15%). Không dùng breadth MA20/MA50. Đây là **proxy giả thuyết** |
| Coin không có trong map | `UNKNOWN`, không loại khỏi quá trình gọi AI; bảng có thêm Meme, Payments, Infrastructure |
| BTC daily regime | chỉ cung cấp thông tin `RISK_ON / RISK_OFF / UNKNOWN` cho AI; không gate |
| Event gate | BLOCK 24h **trước** và 2h **sau** sự kiện đã khai, cả mốc biên; kiểm tra lại trước paper |
| Lịch events.txt thiếu/hỏng/hết | auto BLOCK; manual paper chỉ được xác nhận bỏ qua **OUTDATED**, xem dưới |
| SearXNG & Ollama | Giữ 6 vai AI, các model và URI của ZIP gốc mới nhất; không thay đổi dịch vụ |
| Nhóm kết quả | `pullback_v21_ok` / `pullback_v21_no`; không gộp thống kê với V1 hoặc V2 |
| Chống mở trùng | Giữ `signal_id`, ghi `context` theo vị thế và file `research_signals_v21.jsonl` |

## Chạy trên Windows

1. Dừng bot và sao lưu **TOÀN BỘ** thư mục gốc (đặc biệt `paper_positions.json`, `journal.csv`, `events.txt`, `settings.json`, `klines_cache/`, `.env`, log). Không ghi đè các file này.
2. Giải nén `council_v21_upgrade.zip`; copy **các file code** trong `council/` vào thư mục thực tế, ghi đè đúng các tên `.py`/`.json` của mã. *KHÔNG* copy `events.example.txt` thành lịch thật nếu chưa kiểm chứng.
3. Trong PowerShell:
   ```powershell
   cd "C:\Users\Bui Duc Loc\council"
   py -m pip install -r requirements.txt
   py -m pytest -q tests\test_upgrade.py
   py autoshadow.py status
   py autoshadow.py once --dry
   ```
4. Đảm bảo `events.txt` theo `YYYY-MM-DD HH:MM | tên sự kiện`, **giờ Asia/Ho_Chi_Minh**. Nếu dùng múi giờ khác, đặt biến môi trường `COUNCIL_EVENT_TZ` bằng IANA zone tương ứng (và có `tzdata`).
5. Kiểm tra lại model từ `ollama list`, API `http://localhost:11434`, SearXNG `http://127.0.0.1:8080/search` đúng với Windows/Docker; không tự động đổi URL.
6. Sau khi `--dry` khớp kỳ vọng và chạy thử Ollama/SearXNG, mới bật tự động (`AUTOSHADOW=1`) **với một worker duy nhất**.

### Ngoại lệ paper MỞ TAY khi events.txt đã hết lịch

Chỉ có hiệu lực nếu tệp có các dòng hợp lệ nhưng **không còn sự kiện nào tương lai** và **không còn nằm trong cửa sổ 2 giờ sau sự kiện vừa qua**. Không bao giờ vượt qua EVENT_WINDOW, MISSING, INVALID hay EMPTY.

```powershell
py paper.py open SOL --setup manual_check --auto --ack-stale-events
```

Tương tự Telegram `/open SOL --setup manual_check --auto --ack-stale-events` nếu bot đã xác thực chat. Đây chỉ là paper trading, bị gắn `macro_event_status=OUTDATED_MANUAL_ACK` và log `manual_event_override.jsonl`. **Không dùng cho tự động**; với lịch không cập nhật, có thể bỏ sót sự kiện thật.

## Kiểm tra trên cache offline

```powershell
py research_compare_v21.py
```

Tạo `research_compare_v21.csv` từ `klines_cache/*_4h.json`; chỉ so sánh Pullback V1 và thuộc tính Sweep, giả lập tối đa 30 nến, chi phí theo `scanner.COST_PER_SIDE`. **Không** kiểm chứng AI, Narrative, Regime, event data hay giá fill thực tế; sử dụng universe lịch sử đang lưu, có survivorship bias.

Trên bản cache đã cung cấp, lượt kiểm tra hiện tại tìm 1.075 giao dịch Pullback không chồng lấn trong từng coin: 262 có Sweep, 813 không Sweep; trung bình ròng lần lượt -0,2381R và -0,1132R, toàn bộ -0,1437R. Đây là **phép đo khác** so với đếm mọi tín hiệu thô (2.311) nên không được coi là tái hiện độc lập đúng cùng phương pháp nghiên cứu cũ. Đây cũng không phải bằng chứng AI hiệu quả.

## Điểm chưa thể loại bỏ chỉ bằng sửa mã

- Không có bằng chứng Narrative Score đo được dòng tiền on-chain / dòng vốn thực. Hệ số 60/25/15 là giả thuyết.
- Cần backtest/out-of-sample và giai đoạn forward paper để đo tác động thực. Council chưa được chạy tích hợp thật trên máy bạn.
- SearXNG có thể thiếu tin/cập nhật chậm; `events.txt` chỉ chứa sự kiện người dùng tự khai.
- Giới hạn `MAX_PER_CYCLE=10`, tác vụ AI chậm và công suất Ollama có thể làm giảm số lệnh được xét. Log `LIMIT` khi vượt ngưỡng.
- `paper_positions.json` chưa được thiết kế cho nhiều tiến trình ghi đồng thời; chỉ chạy một bot worker. Vẫn dùng giá đóng nến làm entry giả lập, không phải báo giá khớp thật.
- So sánh `ok/no` vẫn là nghiên cứu quan sát, không thay cho thí nghiệm ngẫu nhiên; yêu cầu tối thiểu 30 mẫu mỗi nhóm có thể mất nhiều thời gian.

## File quan trọng

- `autoshadow.py`: orchestration trực tiếp V2.1; `SETUP=pullback_v21`.
- `event_gate.py`: pre/post macro window, timezone, manual-only stale acknowledgment.
- `narrative.py`, `narrative_map.json`: informational narrative.
- `liquidity.py`: kiểm tra sweep optional.
- `market_regime.py`: informational BTC 1d.
- `pullback.py`: giữ nguyên `pullback_signal`; hàm `pullback_sweep_signal` của V2 vẫn tồn tại để so sánh offline nhưng **không** được gọi trong `autoshadow.py` của V2.1.
- `fill.py`, `council.py`: context gửi vào AI, giữ nguyên mô hình/SearXNG.
- `paper.py`: cổng kiểm tra sự kiện hai lần, manual acknowledgment, metadata/audit.
- `research_compare_v21.py`, `tests/test_upgrade.py`: đối chiếu và kiểm tra không dùng mạng.
