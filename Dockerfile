FROM python:3.12-slim

# System dependencies mà OCRmyPDF cần:
#  - tesseract-ocr        : engine OCR
#  - tesseract-ocr-vie    : gói ngôn ngữ tiếng Việt
#  - ghostscript          : xử lý/ghép PDF
#  - unpaper              : cho --clean (khử nhiễu)
#  - pngquant             : cho --optimize
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-vie \
        tesseract-ocr-eng \
        ghostscript \
        unpaper \
        pngquant \
        libzbar0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py splitter.py ./

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
