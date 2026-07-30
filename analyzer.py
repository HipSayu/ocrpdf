"""
Phát hiện mã QR / mã vạch trên trang PDF và phân loại trang.

Nguyên tắc: chỉ cần PHÁT HIỆN có mã (dựa vào đặc trưng hình học — QR có 3 ô định vị
tỉ lệ 1:1:3:1:1), KHÔNG cần giải mã được nội dung. Nhờ vậy vẫn nhận ra mã bị mờ / nhoè /
rách mà zbar chịu thua — và đó chính là các trang mà cách dò cũ (bắt buộc giải mã) bỏ sót.

Giải mã chỉ chạy thêm để lấy nhãn hiển thị và đặt tên tệp; nó KHÔNG quyết định
trang đó có phải điểm cắt hay không.
"""
import numpy as np
from typing import Callable

import cv2
import fitz  # PyMuPDF

try:
    from PIL import Image
    from pyzbar.pyzbar import decode as zbar_decode
    _HAS_ZBAR = True
except Exception:  # pragma: no cover - thiếu zbar vẫn chạy được, chỉ mất phần đọc nội dung mã
    _HAS_ZBAR = False

# Bộ dò dùng chung — tạo một lần, không giữ trạng thái giữa các lần gọi detect().
_QR = cv2.QRCodeDetector()
_BAR = cv2.barcode.BarcodeDetector() if hasattr(cv2, "barcode") else None

# Tỉ lệ viền bị cắt bỏ trước khi đo độ "trắng" của trang: bỏ mép đen do máy scan,
# lỗ bấm ghim, vệt bóng gáy sách — mấy thứ này đủ làm một tờ trắng bị coi là có nội dung.
_MARGIN_RATIO = 0.04


def render_gray(page: "fitz.Page", dpi: int) -> np.ndarray:
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


def detect_code(img: np.ndarray) -> tuple[str, list[dict]] | None:
    """
    Tìm mã trên ảnh trang. KHÔNG giải mã.
    Trả về ("qr" | "barcode", danh sách bounding box) hoặc None nếu trang không có mã.
    """
    h, w = img.shape

    ok, points = _QR.detectMulti(img)
    if ok:
        return "qr", _boxes_from_points(points, w, h)

    if _BAR is not None:
        ok, corners = _BAR.detect(img)
        if ok:
            return "barcode", _boxes_from_points(corners, w, h)

    return None


def _crops(img: np.ndarray, boxes: list[dict], pad: float = 0.06) -> list[np.ndarray]:
    """Cắt vùng quanh từng mã đã phát hiện (nới thêm `pad` để chừa quiet zone)."""
    h, w = img.shape
    out = []
    for b in boxes:
        x0 = int(max(0.0, b["x"] - pad) * w)
        x1 = int(min(1.0, b["x"] + b["w"] + pad) * w)
        y0 = int(max(0.0, b["y"] - pad) * h)
        y1 = int(min(1.0, b["y"] + b["h"] + pad) * h)
        if x1 - x0 > 16 and y1 - y0 > 16:
            out.append(np.ascontiguousarray(img[y0:y1, x0:x1]))
    return out


def decode_image(img: np.ndarray, boxes: list[dict] | None = None) -> str | None:
    """
    Thử đọc nội dung mã (chỉ để hiển thị / đặt tên tệp), trả None nếu không đọc được.

    Truyền `boxes` (kết quả của `detect_code`) để chỉ giải mã trong vùng có mã: nhanh hơn
    nhiều lần so với quét cả trang, và thường đọc được tốt hơn vì không bị chữ nghĩa xung quanh
    làm nhiễu. Chỉ thử 2 hướng 0°/90° — zbar tự đọc được mã lộn 180° nên thêm hướng là vô ích.
    """
    if not _HAS_ZBAR:
        return None

    regions = _crops(img, boxes) if boxes else []
    if not regions:
        regions = [img]

    for region in regions:
        # Bản scan nhạt/xám làm zbar không phân biệt nổi vạch với nền: ảnh xám thô đọc trượt
        # ở mọi DPI, nhị phân hoá Otsu thì đọc ra ngay. Thử cả hai, ưu tiên bản đã nhị phân hoá.
        _, binary = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        for variant in (binary, region):
            pil = Image.fromarray(variant)
            for angle in (0, 90):
                rotated = pil if angle == 0 else pil.rotate(angle, expand=True)
                for res in zbar_decode(rotated):
                    data = res.data.decode("utf-8", "replace").strip()
                    if data:
                        return data
    return None


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


def is_separator(kind: str | None, value: str | None, marker: str = "") -> bool:
    """
    Trang này có phải điểm cắt không.

    Mặc định (`marker` rỗng): CÓ MÃ LÀ CẮT — không cần giải mã được.
    Nếu truyền `marker`, chỉ cắt ở mã giải ra có chứa chuỗi đó; mã không giải mã nổi
    vẫn được tính là điểm cắt để không dính hai văn bản vào nhau.
    """
    if kind not in ("qr", "barcode"):
        return False
    if not marker:
        return True
    return value is None or marker.upper() in value.upper()


def classify_page(
    page: "fitz.Page",
    dpi: int = 150,
    blank_threshold: float = 0.002,
    marker: str = "",
) -> dict:
    """
    Phân loại 1 trang.

    Trả về dict:
        type       : "qr" | "barcode" | "blank" | "content"
        boxes      : danh sách bounding box chuẩn hoá 0..1 của mã tìm được
        value      : nội dung mã nếu giải mã được (chỉ để hiển thị), ngược lại None
        ink        : tỉ lệ pixel có mực (chỉ có nghĩa với trang blank/content)
        separator  : có phải điểm cắt không
    """
    img = render_gray(page, dpi)

    found = detect_code(img)
    ink = None
    value = None

    if found is None:
        kind, boxes = "content", []
        ink = round(_ink_ratio(img), 5)
        if ink < blank_threshold:
            kind = "blank"
    else:
        kind, boxes = found
        # chỉ để hiển thị / đặt tên tệp, không ảnh hưởng quyết định cắt
        value = decode_image(img, boxes)

    return {
        "type": kind,
        "boxes": boxes,
        "value": value,
        "ink": ink,
        "separator": is_separator(kind, value, marker),
    }


def analyze_pdf(
    pdf_bytes: bytes,
    dpi: int = 150,
    blank_threshold: float = 0.002,
    marker: str = "",
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
