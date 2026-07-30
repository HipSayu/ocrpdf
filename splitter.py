"""
Tách 1 PDF gộp thành nhiều PDF, dựa vào trang phân cách có mã QR / mã vạch.

Trang phân cách = trang PHÁT HIỆN ĐƯỢC mã (không cần giải mã được nội dung).
Dùng chung bộ dò với `analyzer.py` nên kết quả tách luôn khớp với các dấu hiệu
mà `/analyze` đã vẽ lên bản xem trước.

Mặc định: cắt tại trang đó và LOẠI trang phân cách khỏi kết quả.
Truyền `marker` nếu chỉ muốn cắt ở những mã có nội dung chứa chuỗi cho trước.
"""
from typing import Callable

import fitz  # PyMuPDF

from analyzer import decode_image, detect_code, is_separator, render_gray


def _page_separator(page: "fitz.Page", marker: str, dpi: int) -> tuple[bool, str | None]:
    """
    Xét 1 trang. Trả về (có phải trang phân cách, nội dung mã nếu đọc được).

    Nội dung mã chỉ dùng để đặt tên tệp — và để lọc theo `marker` khi có yêu cầu.
    """
    img = render_gray(page, dpi)
    found = detect_code(img)
    if found is None:
        return False, None

    kind, boxes = found
    value = decode_image(img, boxes)
    return is_separator(kind, value, marker), value


def split_pdf(
    pdf_bytes: bytes,
    marker: str = "",
    drop_separator: bool = True,
    dpi: int = 200,
    progress: Callable[[int, int], None] | None = None,
    separators: list[int] | None = None,
    separator_values: dict[int, str] | None = None,
) -> list[dict]:
    """
    Tách PDF theo trang phân cách.

    `separators`: danh sách số trang (đếm từ 1) đã biết là trang phân cách. Truyền vào để
    BỎ QUA hoàn toàn bước dò mã — dùng khi client đã gọi `/analyze` trước đó. Vừa nhanh hơn
    hàng trăm lần, vừa bảo đảm kết quả tách đúng bằng những gì người dùng thấy trên bản xem trước.

    `separator_values`: nội dung mã của các trang đó ({số trang: chuỗi}), cũng lấy từ `/analyze`.
    Chỉ dùng để đặt tên tệp — nhờ nó mà đường nhanh vẫn cho tên tệp đẹp như đường dò lại.

    Trả về danh sách các tài liệu con, mỗi phần tử:
        { "index": số thứ tự, "pages": [chỉ số trang gốc], "bytes": nội dung PDF,
          "marker_data": chuỗi mã của separator ngay trước đó (nếu đọc được) }
    """
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = src.page_count

    # 1) Xác định trang nào là separator.
    #    None = trang thường; "" hoặc nội dung mã = trang phân cách.
    sep_flags: list[str | None]
    if separators is not None:
        given = {p - 1 for p in separators if 1 <= p <= total}
        vals = {p - 1: v for p, v in (separator_values or {}).items()}
        sep_flags = [(vals.get(i, "") if i in given else None) for i in range(total)]
        if progress:
            progress(total, total)
    else:
        sep_flags = []
        for i in range(total):
            is_sep, value = _page_separator(src[i], marker, dpi)
            sep_flags.append((value or "") if is_sep else None)
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
            last_marker = sep_flags[i] or None
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
