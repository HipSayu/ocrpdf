"""
Tách 1 PDF gộp thành nhiều PDF, dựa vào trang phân cách chứa mã vạch.
Trang phân cách = trang có barcode mà nội dung chứa chuỗi marker (vd "TACH").
Mặc định: cắt tại trang đó và LOẠI trang phân cách khỏi kết quả.
"""
import io
from typing import Callable

import fitz  # PyMuPDF
from PIL import Image
from pyzbar.pyzbar import decode as zbar_decode


def _page_has_marker(page: "fitz.Page", marker: str, dpi: int = 200) -> str | None:
    """
    Render 1 trang thành ảnh và tìm barcode chứa `marker`.
    Trả về chuỗi barcode nếu khớp, ngược lại None.
    Thử cả 4 hướng xoay để chịu được trang scan ngược.
    """
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")

    for angle in (0, 180, 90, 270):
        rotated = img if angle == 0 else img.rotate(angle, expand=True)
        for res in zbar_decode(rotated):
            data = res.data.decode("utf-8", "replace")
            if marker.upper() in data.upper():
                return data
    return None


def split_pdf(
    pdf_bytes: bytes,
    marker: str = "TACH",
    drop_separator: bool = True,
    dpi: int = 200,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """
    Tách PDF theo trang phân cách.

    Trả về danh sách các tài liệu con, mỗi phần tử:
        { "index": số thứ tự, "pages": [chỉ số trang gốc], "bytes": nội dung PDF,
          "marker_data": chuỗi barcode của separator ngay trước đó (nếu có) }
    """
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = src.page_count

    # 1) Quét: đánh dấu trang nào là separator
    sep_flags: list[str | None] = []
    for i in range(total):
        data = _page_has_marker(src[i], marker, dpi=dpi)
        sep_flags.append(data)
        if progress:
            progress(i + 1, total)

    # 2) Gom các trang không-phải-separator thành từng nhóm,
    #    cắt mỗi khi gặp separator.
    groups: list[dict] = []
    current: list[int] = []
    last_marker: str | None = None

    def flush():
        if current:
            groups.append({"pages": current.copy(), "marker_data": last_marker})
            current.clear()

    for i in range(total):
        if sep_flags[i] is not None:
            # gặp trang phân cách -> chốt nhóm hiện tại
            flush()
            last_marker = sep_flags[i]
            if not drop_separator:
                # nếu muốn giữ trang phân cách, đưa nó vào đầu nhóm kế
                current.append(i)
        else:
            current.append(i)
    flush()

    # 3) Xuất từng nhóm thành PDF riêng
    docs: list[dict] = []
    for idx, g in enumerate(groups, start=1):
        out = fitz.open()
        for p in g["pages"]:
            out.insert_pdf(src, from_page=p, to_page=p)
        buf = out.tobytes()
        out.close()
        docs.append({
            "index": idx,
            "pages": g["pages"],
            "bytes": buf,
            "marker_data": g["marker_data"],
        })

    src.close()
    return docs
