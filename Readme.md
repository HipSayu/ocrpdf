# OCR API (OCRmyPDF)

Ba chức năng:
1. **`/ocr`** — POST một PDF scan → nhận về PDF 2 lớp (ảnh gốc + lớp text ẩn, searchable).
2. **`/split`** — POST một PDF gộp nhiều văn bản (ngăn nhau bằng trang phân cách có barcode)
   → nhận về file `.zip` chứa từng văn bản riêng. Tùy chọn OCR luôn từng file.
3. **`/analyze`** — POST một PDF → nhận về JSON phân loại từng trang: có mã QR, có mã vạch,
   trang trắng hay trang nội dung, kèm khung bao của mã. Không tách file, không OCR.
   Bản **`/analyze/stream`** trả NDJSON theo từng trang (`{"progress":{"page":5,"total":169}}` …
   rồi `{"result":…}`) để client vẽ được thanh tiến trình thật.

## Chạy bằng Docker (khuyến nghị)

Docker lo sẵn Tesseract, Ghostscript, gói tiếng Việt — không phải cài tay.

```bash
docker build -t ocr-api .
docker run -p 8000:8000 ocr-api
```

## Chạy trực tiếp trên Windows (không Docker)

Cài các phần mềm hệ thống rồi thêm PATH:
- **Tesseract OCR** (UB Mannheim build) — nhớ tick chọn gói ngôn ngữ **Vietnamese** khi cài.
- **Ghostscript** (bản Windows 64-bit).
- (Tùy chọn) **unpaper**, **pngquant** nếu dùng `--clean` / `--optimize`.

`pyzbar` trên Windows đã kèm sẵn DLL của zbar (không cần cài libzbar riêng),
nhưng có thể cần **Visual C++ Redistributable 2013** nếu báo lỗi thiếu DLL.

Sau đó:

```bat
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Nếu Tesseract/Ghostscript không nằm trong PATH, thêm biến môi trường hoặc trỏ
đường dẫn cho OCRmyPDF trước khi chạy.

## Chạy trực tiếp (Linux/macOS, không Docker)

Cần cài system deps trước:

```bash
# Ubuntu/Debian
sudo apt install tesseract-ocr tesseract-ocr-vie ghostscript unpaper pngquant libzbar0 libglib2.0-0

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

`libglib2.0-0` là dependency của OpenCV headless (dùng cho `/analyze`).

## Dùng thử

Swagger UI có sẵn tại: http://localhost:8000/docs

Gọi bằng curl (`-OJ` để lưu file trả về theo đúng tên):

```bash
curl -X POST "http://localhost:8000/ocr?lang=vie+eng&deskew=true" \
     -F "file=@input.pdf" \
     -OJ
```

### Tách văn bản theo trang phân cách

```bash
# Tách, không OCR -> ra zip các văn bản
curl -X POST "http://localhost:8000/split" -F "file=@merged.pdf" -OJ

# Tách + OCR luôn từng văn bản thành PDF 2 lớp
curl -X POST "http://localhost:8000/split?ocr=true&lang=vie+eng" \
     -F "file=@merged.pdf" -OJ
```

Trang phân cách = trang **phát hiện được** mã QR / mã vạch, không cần giải mã được
nội dung mã. Nhờ vậy vẫn cắt đúng ở những tờ phân cách bị scan mờ/nhoè. Mặc định
trang phân cách bị loại khỏi kết quả. Bộ dò bất biến với góc xoay nên trang scan
ngược vẫn nhận ra.

Tham số `/split`: `marker` (**để trống = cắt tại mọi trang có mã**; điền chuỗi để chỉ
cắt ở mã chứa chuỗi đó — dùng khi văn bản có sẵn QR chữ ký số / mã tra cứu không muốn
cắt), `drop_separator` (bỏ trang phân cách, mặc định true), `dpi` (độ phân giải dò mã,
mặc định 200), `ocr` (OCR từng file, mặc định false), `lang` (ngôn ngữ OCR),
`separators` + `separator_values` (xem dưới).

Nếu client đã gọi `/analyze` trước thì truyền lại danh sách trang phân cách qua
`separators=2,4,6…`, máy chủ bỏ hẳn bước dò mã. Đo trên file thật 169 trang / 65.7 MB:
**19.5s → 0.1s**. Quan trọng hơn: tránh được việc `/analyze` và `/split` chạy song song
tranh CPU (cả hai đều CPU-bound), và kết quả tách đúng bằng bản xem trước.

### Phân loại trang (không tách file)

```bash
curl -X POST "http://localhost:8000/analyze?dpi=150" -F "file=@merged.pdf"
```

Trả về JSON: mỗi trang có `type` (`qr` / `barcode` / `blank` / `content`), khung bao `boxes`
của mã (toạ độ chuẩn hoá 0..1), `value` (nội dung mã nếu đọc được — chỉ để hiển thị) và
`separator` (có phải điểm cắt không).

Mã được tìm bằng đặc trưng hình học — QR qua `cv2.QRCodeDetector`, mã vạch 1D qua
`cv2.barcode` — nên thấy được cả mã mờ/nhoè mà zbar không giải mã nổi. Trang trắng nhận ra
bằng tỉ lệ điểm ảnh có mực sau khi cắt viền (bỏ mép đen máy scan, lỗ bấm ghim) và khử hạt nhiễu.

`/analyze` và `/split` dùng chung module `analyzer.py` cho bước dò mã và chung hàm
`is_separator()`, nên chỗ `/analyze` báo "sẽ cắt" đúng bằng chỗ `/split` cắt thật.

Tham số: `dpi` (mặc định 150), `marker` (để trống = mọi trang có mã đều là điểm cắt),
`blank_threshold` (mặc định 0.002 = 0.2%). Xem chi tiết trong [`../api.md`](../api.md).

## Tham số /ocr (query string)

| Tham số   | Mặc định   | Ý nghĩa |
|-----------|------------|---------|
| `lang`    | `vie+eng`  | Mã ngôn ngữ Tesseract. Gộp bằng dấu `+` |
| `deskew`  | `true`     | Làm thẳng trang nghiêng |
| `clean`   | `false`    | Khử nhiễu trước khi OCR (cần unpaper) |
| `rotate`  | `true`     | Tự xoay trang bị lật |
| `optimize`| `1`        | Nén ảnh: 0 (tắt) → 3 (mạnh nhất) |
| `mode`    | `skip-text`| `skip-text` (bỏ qua trang đã có text) / `force-ocr` (ép OCR lại tất cả) / `redo-ocr` (OCR lại lớp do máy tạo) |

## Lưu ý về hiệu năng

- OCR là tác vụ CPU-bound. Với tải cao, nên chạy nhiều worker:
  `uvicorn app:app --workers 4` (hoặc scale số container).
- Muốn chịu tải lớn và không chặn request: chuyển sang mô hình hàng đợi
  (Celery/RQ + Redis) — API nhận job trả về `job_id`, client hỏi kết quả sau.
