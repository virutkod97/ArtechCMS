# ArtechCMS

Phần mềm web quản lý **nhiều máy ghi âm Artech** (dòng AJ/AK/AQ, giao thức TCP 6066) — tự động tải file ghi âm về máy, nghe lại ngay trên web, không cần phần mềm PhoneRec gốc.

## Tính năng
- Quản lý **nhiều thiết bị Artech** cùng lúc (thêm/sửa/xóa, test kết nối).
- **Tự động tải** file ghi âm theo chu kỳ (mặc định 15 phút, cấu hình từng thiết bị).
- **Không tải lại** file đã có (dedup theo CDR index, lưu trong SQLite).
- **Nghe ngay trên web** — tự giải mã IMA ADPCM → PCM (không cần ffmpeg).
- **Duyệt + tải thủ công** mọi file đã lưu.
- **Cập nhật online** từ GitHub (nút ⟳ Update) hoặc `update.bat`.
- **1-click deploy** trên Windows.

## Cài đặt nhanh (Windows)
1. Cài **Python 3.9+** (nhớ tick *Add to PATH*) và **Git** (để update online).
2. Lấy source:
   ```
   git clone https://github.com/virutkod97/ArtechCMS
   cd ArtechCMS
   ```
3. Chạy **`deploy_windows.bat`** (double-click). Nó tự tạo venv, cài thư viện, khởi động.
4. Mở trình duyệt: **http://localhost:8787**

> Cổng web mặc định `8787`. Đổi bằng biến môi trường `ARTECHCMS_PORT`.

## Thêm thiết bị
Tab **Thiết bị** → **+ Thêm thiết bị**:
- **IP thiết bị**: IP máy ghi âm Artech (vd `10.43.12.240`)
- **User/Password**: mặc định `admin` / `1111`
- **Client IP / MAC**: IP + MAC của máy chạy app (giúp khớp định dạng gói login; để trống vẫn thường chạy)
- **Chu kỳ tải**: số phút giữa 2 lần tải tự động
- **Số ngày lùi**: tải file của N ngày gần nhất mỗi lần quét

Bấm **Lưu** → **Test kết nối** để kiểm tra, hoặc **⬇ Tải ngay**.

## Cấu trúc
```
app.py                Flask app + scheduler + API
artech/protocol.py    Giao thức 6066: login, pull, giải mã IMA ADPCM, carve WAV
artech/downloader.py  Job tải + dedup theo index
artech/db.py          SQLite: devices / recordings / settings
templates/ static/    Giao diện web
recordings/dev<N>/     File .wav theo từng thiết bị
data/artechcms.db     Cơ sở dữ liệu
```

## Giao thức Artech (6066) — tóm tắt kỹ thuật
- **Login**: `40 01 01 00` + struct 320 byte, chuỗi UTF‑16LE: user@0x04, pass@0x24, version@0x44, clientIP@0x64, MAC@0x84, deviceIP@0xC0. Reply bắt đầu `62 00 01 02`.
- **Chọn ngày**: `04 00 0C 13 00 00` rồi `42 00 0C 11` + SYSTEMTIME(ngày).
- **Kéo dữ liệu**: client gửi `06 00 0C 07 00 10 00 00` lặp lại; thiết bị đẩy từng chunk 4096 byte, mỗi chunk có marker `06 10 0C 07 00 10 00 00` ở đầu.
- **Khung file**: `<delim 0D0009363A00002D3A0000> <u32 CDR_index> 06 00 0C 0A <u32 size> 00 00 <payload>`. Payload là WAV IMA ADPCM (mono 8000 Hz) đã bị chèn marker — lọc marker để ra WAV sạch.

## Lưu ý về chế độ tải tự động
Hàm `protocol.download_stream()` tải theo cách: login → chọn ngày → vòng lặp kéo chunk đến khi hết. Cadence vòng lặp đã dựng từ capture thật; nếu với thiết bị/firmware khác mà bị dừng sớm hoặc thiếu file, bắt thêm 1 capture (pktmon lọc theo IP thiết bị) và điều chỉnh tham số `idle`/số lần lặp trong `download_stream`. Việc **ghi file** luôn an toàn: đã dedup theo index nên không bao giờ ghi trùng.

## License
Nội bộ EVNNPC. Dùng cho thiết bị do bạn sở hữu/quản trị.
