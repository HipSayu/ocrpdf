"""
Phân loại từng trang của một PDF: trang chứa mã QR, mã vạch 1D, trang trắng, hay trang nội dung.

Khác với `splitter.py` (phải ĐỌC ĐƯỢC nội dung mã mới coi là trang phân cách), module này chỉ cần
PHÁT HIỆN có mã trên trang — dựa vào đặc trưng hình học (QR: 3 ô định vị tỉ lệ 1:1:3:1:1).
Nhờ vậy vẫn nhận ra được mã bị mờ / nhoè / rách mà zbar không giải mã nổi.
Việc giải mã (nếu được) chỉ chạy trên các trang đã phát hiện có mã, để lấy nhãn hiển thị.
"""
import numpy as np
from typing import Callable

import cv2
import fitz  # PyMuPDF

try:
    from PIL import Image
    from pyzbar.pyzbar import decode as zbar_decode
    _HAS_ZBAR = True
except Exception:  # pragma: no cover - môi trường không có zbar vẫn chạy được, chỉ mất phần giải mã
    _HAS_ZBAR = False

# Bộ dò dùng chung — tạo một lần, không giữ trạng thái giữa các lần gọi detect().
_QR = cv2.QRCodeDetector()
_BAR = cv2.barcode.BarcodeDetector() if hasattr(cv2, "barcode") else None

# Tỉ lệ viền bị cắt bỏ trước khi đo độ "trắng" của trang: bỏ mép đen do máy scan,
# lỗ bấm ghim, vệt bóng gáy sách — mấy thứ này đủ làm một tờ trắng bị coi là có nội dung.
_MARGIN_RATIO = 0.04


def _render_gray(page: "fitz.Page", dpi: int) -> np.ndarray:
    """Render 1 trang ra ảnh xám, trả về mảng numpy liên tục (cv2 cần contiguous)."""
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    arr = arr.reshape(pix.height, pix.stride)[:, : pix.width]
    return np.ascontiguousarray(arr)


def _boxes_from_points(points, w: int, h: int) -> list[dict]:
    """Đổi các bộ 4 điểm góc mà OpenCV trả về thành bounding box chuẩn hoá 0..1."""
    boxes: list[dict] = []
    if points is None:
        return boxes
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 4, 2)
    for quad in pts:
        xs, ys = quad[:, 0], quad[:, 1]
        x0, x1 = float(xs.min()), float(xs.max())
        y0, y1 = float(ys.min()), float(ys.max())
        boxes.append({
            "x": round(max(0.0, x0 / w), 5),
            "y": round(max(0.0, y0 / h), 5),
            "w": round(min(1.0, (x1 - x0) / w), 5),
            "h": round(min(1.0, (y1 - y0) / h), 5),
        })
    return boxes


def _ink_ratio(img: np.ndarray) -> float:
    """
    Tỉ lệ pixel "có mực" trên trang, sau khi bỏ viền và khử hạt nhiễu.

    Ngưỡng đen được lấy theo nền thực tế của trang (phân vị 90) thay vì hằng số cứng,
    để không phán nhầm khi máy scan cho ra nền xám thay vì trắng.
    """
    h, w = img.shape
    m = int(min(h, w) * _MARGIN_RATIO)
    core = img[m:h - m, m:w - m] if h > 2 * m and w > 2 * m else img
    if core.size == 0:
        return 0.0

    core = cv2.medianBlur(core, 3)  # bụi trên kính scan đủ làm tờ trắng thành "có nội dung"
    bg = float(np.percentile(core, 90))
    thr = float(np.clip(bg - 50.0, 110.0, 200.0))
    return float((core < thr).sum()) / core.size


def _decode(img: np.ndarray) -> str | None:
    """Thử đọc nội dung mã trên trang. Chỉ gọi cho trang đã phát hiện có mã."""
    if not _HAS_ZBAR:
        return None
    pil = Image.fromarray(img)
    for angle in (0, 180, 90, 270):
        rotated = pil if angle == 0 else pil.rotate(angle, expand=True)
        for res in zbar_decode(rotated):
            data = res.data.decode("utf-8", "replace").strip()
            if data:
                return data
    return None


def classify_page(
    page: "fitz.Page",
    dpi: int = 150,
    blank_threshold: float = 0.002,
    marker: str = "TACH",
) -> dict:
    """
    Phân loại 1 trang.

    Trả về dict:
        type       : "qr" | "barcode" | "blank" | "content"
        boxes      : danh sách bounding box chuẩn hoá 0..1 của mã tìm được
        value      : nội dung mã nếu giải mã được, ngược lại None
        ink        : tỉ lệ pixel có mực (chỉ có nghĩa với trang blank/content)
        separator  : có nên coi đây là trang phân cách hay không
    """
    img = _render_gray(page, dpi)
    h, w = img.shape

    kind, boxes = "content", []

    ok, points = _QR.detectMulti(img)
    if ok:
        kind, boxes = "qr", _boxes_from_points(points, w, h)
    elif _BAR is not None:
        ok, corners = _BAR.detect(img)
        if ok:
            kind, boxes = "barcode", _boxes_from_points(corners, w, h)

    ink = None
    value = None
    if kind == "content":
        ink = round(_ink_ratio(img), 5)
        if ink < blank_threshold:
            kind = "blank"
    else:
        value = _decode(img)

    # Trang có mã và giải mã ra đúng marker -> chắc chắn là trang phân cách.
    # Trang có mã nhưng không giải mã nổi -> vẫn tính là phân cách (đây chính là phần
    # mà cách cũ bỏ sót). Trang có mã giải mã ra thứ khác -> là mã trong nội dung
    # văn bản (chữ ký số, mã tra cứu...), KHÔNG cắt ở đây.
    separator = kind in ("qr", "barcode") and (
        value is None or marker.upper() in value.upper()
    )

    return {
        "type": kind,
        "boxes": boxes,
        "value": value,
        "ink": ink,
        "separator": separator,
    }


def analyze_pdf(
    pdf_bytes: bytes,
    dpi: int = 150,
    blank_threshold: float = 0.002,
    marker: str = "TACH",
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Phân loại toàn bộ trang của một PDF. Trả về dict gồm `pages` và `summary`."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = doc.page_count
        pages: list[dict] = []
        summary = {"qr": 0, "barcode": 0, "blank": 0, "content": 0, "separator": 0}

        for i in range(total):
            info = classify_page(doc[i], dpi=dpi, blank_threshold=blank_threshold, marker=marker)
            info["page"] = i + 1
            pages.append(info)

            summary[info["type"]] += 1
            if info["separator"]:
                summary["separator"] += 1
            if progress:
                progress(i + 1, total)

        return {"page_count": total, "summary": summary, "pages": pages}
    finally:
        doc.close()
