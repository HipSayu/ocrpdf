"""
OCR API — bọc OCRmyPDF thành REST endpoint.
Đầu vào: 1 file PDF (scan). Đầu ra: PDF 2 lớp (ảnh gốc + lớp text ẩn).
"""
import os
import tempfile
import uuid
import logging

import ocrmypdf
from fastapi import FastAPI, UploadFile, File, Query, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ocr-api")

app = FastAPI(
    title="OCR API",
    description="Chuyển PDF scan thành PDF 2 lớp (searchable) bằng OCRmyPDF.",
    version="1.0.0",
)

# Thư mục tạm để chứa file trong lúc xử lý
WORK_DIR = tempfile.gettempdir()
MAX_BYTES = 100 * 1024 * 1024  # giới hạn 100MB, chỉnh theo nhu cầu


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

    job_id = uuid.uuid4().hex
    in_path = os.path.join(WORK_DIR, f"{job_id}_in.pdf")
    out_path = os.path.join(WORK_DIR, f"{job_id}_out.pdf")

    # Đọc & lưu file upload, kiểm tra dung lượng
    data = await file.read()
    if len(data) > MAX_BYTES:
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