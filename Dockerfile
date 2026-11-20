# indra-ocr requires Python 3.10+ (tested 3.10 - 3.13)
FROM python:3.11-slim

WORKDIR /app

# CPU only: Indra runs on ONNX Runtime, no PyTorch or GPU needed
# Models download from Hugging Face on first use into HF_HOME; mount a volume there to keep them
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf

RUN mkdir -p /app/output /models

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py service.json ./
COPY help/index.md ./help/index.md

EXPOSE 9013

CMD ["python", "api.py"]
