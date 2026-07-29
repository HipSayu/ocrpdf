# OCR API (OCRmyPDF)

REST API: POST một PDF scan vào → nhận về PDF 2 lớp (ảnh gốc + lớp text ẩn, searchable).

## Chạy bằng Docker (khuyến nghị)

Docker lo sẵn Tesseract, Ghostscript, gói tiếng Việt — không phải cài tay.

```bash
docker build -t ocr-api .
docker run -p 8000:8000 ocr-api
```

## Chạy trực tiếp (local, không Docker)

Cần cài system deps trước:

```bash
# Ubuntu/Debian
sudo apt install tesseract-ocr tesseract-ocr-vie ghostscript unpaper pngquant

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Dùng thử

Swagger UI có sẵn tại: http://localhost:8000/docs

Gọi bằng curl (`-OJ` để lưu file trả về theo đúng tên):

```bash
curl -X POST "http://localhost:8000/ocr?lang=vie+eng&deskew=true" \
     -F "file=@input.pdf" \
     -OJ
```

## Tham số (query string)

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