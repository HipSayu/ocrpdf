"""
OCR API — bọc OCRmyPDF thành REST endpoint.
Đầu vào: 1 file PDF (scan). Đầu ra: PDF 2 lớp (ảnh gốc + lớp text ẩn).
"""
import asyncio
import os
import io
import json
import queue
import re
import zipfile
import tempfile
import uuid
import logging

import anyio
from fastapi import FastAPI, UploadFile, File, Form, Query, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse

from analyzer import analyze_pdf
from splitter import split_pdf

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ocr-api")

app = FastAPI(
    title="OCR API",
    description="OCR PDF scan thành PDF 2 lớp, tách PDF theo mã QR/barcode, "
                "và phân loại từng trang (mã QR / mã vạch / trang trắng).",
    version="1.1.0",
)

# Thư mục tạm để chứa file trong lúc xử lý
WORK_DIR = tempfile.gettempdir()

# Giới hạn dung lượng upload (MB). Đặt qua biến môi trường OCR_MAX_MB.
# Mặc định 0 = không giới hạn. Đặt số > 0 để bật giới hạn (vd OCR_MAX_MB=500).
_max_mb = int(os.environ.get("OCR_MAX_MB", "0"))
MAX_BYTES = _max_mb * 1024 * 1024 if _max_mb > 0 else None


def _cleanup(*paths: str) -> None:
    """Xóa file tạm sau khi đã trả response về client."""
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError as e:
            log.warning("Không xóa được %s: %s", p, e)


@app.get("/health")
def health():
    return {"status": "ok"}


def _ocr_bytes(pdf_bytes: bytes, lang: str) -> bytes:
    """OCR một PDF (dạng bytes) -> trả PDF 2 lớp (bytes). Dùng cho từng file sau khi tách."""
    import ocrmypdf
    tmp_in = os.path.join(WORK_DIR, f"{uuid.uuid4().hex}_in.pdf")
    tmp_out = os.path.join(WORK_DIR, f"{uuid.uuid4().hex}_out.pdf")
    try:
        with open(tmp_in, "wb") as f:
            f.write(pdf_bytes)
        ocrmypdf.ocr(
            tmp_in, tmp_out,
            language=lang, deskew=True, rotate_pages=True,
            skip_text=True, progress_bar=False, output_type="pdf",
        )
        with open(tmp_out, "rb") as f:
            return f.read()
    finally:
        _cleanup(tmp_in, tmp_out)


def _safe_name(s: str, max_len: int = 40) -> str:
    """
    Làm sạch chuỗi mã để dùng làm tên file.

    Cắt ngắn vì nội dung mã có thể rất dài (URL, vCard…) — nối vào tên gốc dễ vượt
    giới hạn 260 ký tự đường dẫn của Windows.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")
    return cleaned[:max_len].strip("_") or "doc"


@app.post("/analyze")
async def analyze(
    file: UploadFile = File(..., description="PDF cần phân loại từng trang"),
    dpi: int = Query(150, ge=72, le=400, description="Độ phân giải render trang khi dò mã"),
    marker: str = Query(
        "",
        description="Để trống (mặc định): mọi trang có mã QR/mã vạch đều là trang phân cách. "
                    "Điền chuỗi nếu chỉ muốn cắt ở mã có nội dung chứa chuỗi đó.",
    ),
    blank_threshold: float = Query(
        0.002, ge=0.0, le=0.5,
        description="Tỉ lệ pixel có mực tối đa để coi là trang trắng (0.002 = 0.2%)",
    ),
):
    """
    Quét từng trang và phân loại: `qr`, `barcode`, `blank` (trang trắng) hoặc `content`.
    Không tách file, không OCR — chỉ trả về JSON để client đánh dấu lên bản xem trước.
    """
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(415, "Chỉ nhận file PDF.")

    data = await file.read()
    if MAX_BYTES is not None and len(data) > MAX_BYTES:
        raise HTTPException(413, f"File vượt quá {MAX_BYTES // (1024*1024)}MB.")

    try:
        # Dò mã là tác vụ CPU-bound -> đẩy sang threadpool để không chặn event loop.
        result = await anyio.to_thread.run_sync(
            lambda: analyze_pdf(data, dpi=dpi, blank_threshold=blank_threshold, marker=marker)
        )
    except Exception as e:
        log.exception("Phân tích thất bại")
        raise HTTPException(500, f"Phân tích thất bại: {e}")

    result["filename"] = file.filename or ""
    return result


@app.post("/analyze/stream")
async def analyze_stream(
    file: UploadFile = File(..., description="PDF cần phân loại từng trang"),
    dpi: int = Query(150, ge=72, le=400, description="Độ phân giải render trang khi dò mã"),
    marker: str = Query("", description="Để trống: mọi trang có mã đều là trang phân cách"),
    blank_threshold: float = Query(0.002, ge=0.0, le=0.5, description="Ngưỡng coi là trang trắng"),
):
    """
    Giống `/analyze` nhưng trả về **NDJSON theo dòng** để client vẽ được thanh tiến trình thật:

        {"progress": {"page": 5, "total": 169}}
        ...
        {"result": { ...giống hệt /analyze... }}

    Nếu lỗi giữa đường thì dòng cuối là `{"error": "..."}` (HTTP vẫn 200 vì header đã gửi đi rồi).
    """
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(415, "Chỉ nhận file PDF.")

    data = await file.read()
    if MAX_BYTES is not None and len(data) > MAX_BYTES:
        raise HTTPException(413, f"File vượt quá {MAX_BYTES // (1024*1024)}MB.")

    name = file.filename or ""
    # Hàng đợi nối luồng worker (CPU-bound) với generator async đang stream ra client.
    q: "queue.Queue[dict | None]" = queue.Queue()

    def work():
        try:
            res = analyze_pdf(
                data, dpi=dpi, blank_threshold=blank_threshold, marker=marker,
                progress=lambda done, total: q.put({"progress": {"page": done, "total": total}}),
            )
            res["filename"] = name
            q.put({"result": res})
        except Exception as e:
            log.exception("Phân tích thất bại")
            q.put({"error": f"Phân tích thất bại: {e}"})
        finally:
            q.put(None)

    async def stream():
        worker = asyncio.create_task(anyio.to_thread.run_sync(work))
        try:
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.05)
                    continue
                if item is None:
                    break
                yield json.dumps(item, ensure_ascii=False) + "\n"
        finally:
            await worker

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/split")
async def split(
    background: BackgroundTasks,
    file: UploadFile = File(..., description="PDF gộp nhiều văn bản, ngăn nhau bằng trang phân cách có mã"),
    marker: str = Query(
        "",
        description="Để trống (mặc định): cắt tại MỌI trang phát hiện được mã QR/mã vạch, "
                    "không cần giải mã được nội dung. Điền chuỗi để chỉ cắt ở mã chứa chuỗi đó.",
    ),
    drop_separator: bool = Query(True, description="True: bỏ trang phân cách khỏi kết quả"),
    dpi: int = Query(200, ge=100, le=400, description="Độ phân giải render trang khi dò barcode"),
    ocr: bool = Query(False, description="True: OCR (tạo PDF 2 lớp) cho từng văn bản sau khi tách"),
    lang: str = Query("vie+eng", description="Ngôn ngữ OCR (chỉ dùng khi ocr=true)"),
    separators: str = Query(
        "",
        description="Danh sách số trang phân cách đã biết, cách nhau bằng dấu phẩy (vd \"2,4,6\"). "
                    "Truyền vào để bỏ qua bước dò mã — dùng khi client đã gọi /analyze trước đó.",
    ),
    separator_values: str = Form(
        "",
        description="JSON {\"<số trang>\": \"<nội dung mã>\"} — nội dung mã của các trang phân cách, "
                    "chỉ dùng để đặt tên tệp. Đi kèm 'separators'.",
    ),
):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(415, "Chỉ nhận file PDF.")

    data = await file.read()
    if MAX_BYTES is not None and len(data) > MAX_BYTES:
        raise HTTPException(413, f"File vượt quá {MAX_BYTES // (1024*1024)}MB.")

    sep_pages: list[int] | None = None
    if separators.strip():
        try:
            sep_pages = [int(x) for x in separators.replace(" ", "").split(",") if x]
        except ValueError:
            raise HTTPException(422, "Tham số 'separators' phải là các số trang cách nhau bằng dấu phẩy.")

    sep_values: dict[int, str] | None = None
    # lstrip BOM: một số client ghi UTF-8 kèm BOM, json.loads sẽ chết vì ký tự đó.
    if separator_values.strip().lstrip("﻿"):
        try:
            raw = json.loads(separator_values.lstrip("﻿"))
            sep_values = {int(k): str(v) for k, v in raw.items() if v}
        except Exception:
            raise HTTPException(422, "Tham số 'separator_values' phải là JSON dạng {\"số trang\": \"nội dung mã\"}.")

    try:
        # Dò mã là tác vụ CPU-bound -> threadpool, để không chặn các request khác.
        docs = await anyio.to_thread.run_sync(
            lambda: split_pdf(
                data, marker=marker, drop_separator=drop_separator,
                dpi=dpi, separators=sep_pages, separator_values=sep_values,
            )
        )
    except Exception as e:
        log.exception("Tách thất bại")
        raise HTTPException(500, f"Tách thất bại: {e}")

    if not docs:
        raise HTTPException(
            422,
            "Không tách được văn bản nào — không phát hiện được mã QR/mã vạch nào trên tài liệu "
            "(thử tăng dpi, hoặc kiểm tra lại chất lượng bản scan).",
        )

    base = os.path.splitext(file.filename or "output")[0]
    zip_path = os.path.join(WORK_DIR, f"{uuid.uuid4().hex}.zip")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in docs:
            content = _ocr_bytes(d["bytes"], lang) if ocr else d["bytes"]
            # tên file: ưu tiên dùng nội dung barcode nếu có, kèm số thứ tự
            tag = _safe_name(d["marker_data"]) if d.get("marker_data") else "part"
            name = f"{base}_{d['index']:03d}_{tag}.pdf"
            zf.writestr(name, content)

    background.add_task(_cleanup, zip_path)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{base}_split.zip",
        background=background,
    )


@app.post("/ocr")
async def ocr_pdf(
    background: BackgroundTasks,
    file: UploadFile = File(..., description="File PDF cần OCR"),
    lang: str = Query("vie+eng", description="Mã ngôn ngữ Tesseract, vd: vie, eng, vie+eng"),
    deskew: bool = Query(True, description="Tự làm thẳng trang bị nghiêng"),
    clean: bool = Query(False, description="Khử nhiễu trước khi OCR (cần unpaper)"),
    rotate: bool = Query(True, description="Tự phát hiện & xoay trang bị lật"),
    optimize: int = Query(1, ge=0, le=3, description="Mức nén ảnh: 0=tắt ... 3=mạnh nhất"),
    mode: str = Query(
        "skip-text",
        description="skip-text: bỏ qua trang đã có text | force-ocr: ép OCR lại tất cả | redo-ocr: OCR lại chỉ lớp do máy tạo",
    ),
):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(415, "Chỉ nhận file PDF.")

    import ocrmypdf
    job_id = uuid.uuid4().hex
    in_path = os.path.join(WORK_DIR, f"{job_id}_in.pdf")
    out_path = os.path.join(WORK_DIR, f"{job_id}_out.pdf")

    # Đọc & lưu file upload, kiểm tra dung lượng
    data = await file.read()
    if MAX_BYTES is not None and len(data) > MAX_BYTES:
        raise HTTPException(413, f"File vượt quá {MAX_BYTES // (1024*1024)}MB.")
    with open(in_path, "wb") as f:
        f.write(data)

    # Map "mode" sang tham số của ocrmypdf
    kwargs = dict(
        language=lang,
        deskew=deskew,
        clean=clean,
        rotate_pages=rotate,
        optimize=optimize,
        output_type="pdfa",       # xuất chuẩn PDF/A để lưu trữ; đổi thành "pdf" nếu không cần
        progress_bar=False,
    )
    if mode == "force-ocr":
        kwargs["force_ocr"] = True
    elif mode == "redo-ocr":
        kwargs["redo_ocr"] = True
    else:  # skip-text (mặc định)
        kwargs["skip_text"] = True

    try:
        # ocrmypdf.ocr là CPU-bound & blocking -> chạy trong threadpool
        await anyio.to_thread.run_sync(
            lambda: ocrmypdf.ocr(in_path, out_path, **kwargs)
        )
    except ocrmypdf.exceptions.PriorOcrFoundError:
        _cleanup(in_path, out_path)
        raise HTTPException(422, "PDF đã có sẵn lớp text. Dùng mode=force-ocr hoặc redo-ocr để OCR lại.")
    except ocrmypdf.exceptions.EncryptedPdfError:
        _cleanup(in_path, out_path)
        raise HTTPException(422, "PDF đang bị mã hóa/đặt mật khẩu. Hãy giải mã trước.")
    except Exception as e:
        _cleanup(in_path, out_path)
        log.exception("OCR thất bại")
        raise HTTPException(500, f"OCR thất bại: {e}")

    # Lên lịch xóa file tạm sau khi response được gửi xong
    background.add_task(_cleanup, in_path, out_path)

    out_name = (os.path.splitext(file.filename or "document")[0]) + "_ocr.pdf"
    return FileResponse(
        out_path,
        media_type="application/pdf",
        filename=out_name,
        background=background,
    )