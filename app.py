"""
OCR API — bọc OCRmyPDF thành REST endpoint.
Đầu vào: 1 file PDF (scan). Đầu ra: PDF 2 lớp (ảnh gốc + lớp text ẩn).
"""
import os
import io
import re
import zipfile
import tempfile
import uuid
import logging

from fastapi import FastAPI, UploadFile, File, Query, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

from splitter import split_pdf

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ocr-api")

app = FastAPI(
    title="OCR API",
    description="Chuyển PDF scan thành PDF 2 lớp (searchable) bằng OCRmyPDF.",
    version="1.0.0",
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


def _safe_name(s: str) -> str:
    """Làm sạch chuỗi barcode để dùng làm tên file."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "doc"


@app.post("/split")
async def split(
    background: BackgroundTasks,
    file: UploadFile = File(..., description="PDF gộp nhiều văn bản, ngăn nhau bằng trang phân cách có barcode"),
    marker: str = Query("TACH", description="Chuỗi con phải có trong barcode để coi là trang phân cách"),
    drop_separator: bool = Query(True, description="True: bỏ trang phân cách khỏi kết quả"),
    dpi: int = Query(200, ge=100, le=400, description="Độ phân giải render trang khi dò barcode"),
    ocr: bool = Query(False, description="True: OCR (tạo PDF 2 lớp) cho từng văn bản sau khi tách"),
    lang: str = Query("vie+eng", description="Ngôn ngữ OCR (chỉ dùng khi ocr=true)"),
):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(415, "Chỉ nhận file PDF.")

    data = await file.read()
    if MAX_BYTES is not None and len(data) > MAX_BYTES:
        raise HTTPException(413, f"File vượt quá {MAX_BYTES // (1024*1024)}MB.")

    try:
        docs = split_pdf(data, marker=marker, drop_separator=drop_separator, dpi=dpi)
    except Exception as e:
        log.exception("Tách thất bại")
        raise HTTPException(500, f"Tách thất bại: {e}")

    if not docs:
        raise HTTPException(422, "Không tách được văn bản nào (kiểm tra lại marker hoặc chất lượng scan).")

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
        import anyio
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