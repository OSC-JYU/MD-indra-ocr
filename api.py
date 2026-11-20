import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

BASE_DIR = Path(__file__).resolve().parent
SERVICE_ID = "md-indra-ocr"
SERVICE_DESCRIPTOR_PATH = Path(os.getenv("SERVICE_DESCRIPTOR_PATH", BASE_DIR / "service.json")).resolve()
SERVICE_HELP_PATH = Path(os.getenv("SERVICE_HELP_PATH", BASE_DIR / "help" / "index.md")).resolve()
SERVICE_HELP_FALLBACK_PATH = Path(os.getenv("SERVICE_HELP_FALLBACK_PATH", BASE_DIR / "README.md")).resolve()
SERVICE_ID_OVERRIDE = os.getenv("SERVICE_ID")
SERVICE_NAME_OVERRIDE = os.getenv("SERVICE_NAME")
SERVICE_ADAPTER_OVERRIDE = os.getenv("SERVICE_ADAPTER")
SERVICE_LOCAL_URL_OVERRIDE = os.getenv("SERVICE_LOCAL_URL")

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output")).resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Fetched output isn't deleted on read (consumer retries could hit a 404), so a sweep is the
# only cleanup - anything older than this is assumed abandoned (fetched already or never claimed).
OUTPUT_MAX_AGE_SECONDS = int(os.getenv("OUTPUT_MAX_AGE_SECONDS", str(60 * 60)))
OUTPUT_CLEANUP_INTERVAL_SECONDS = int(os.getenv("OUTPUT_CLEANUP_INTERVAL_SECONDS", str(5 * 60)))

MAX_CONTENT_SIZE = int(os.getenv("MAX_CONTENT_SIZE", str(100 * 1024 * 1024)))
REQUEST_READ_CHUNK_SIZE = int(os.getenv("REQUEST_READ_CHUNK_SIZE", str(1024 * 1024)))

# ONNX Runtime threads per converter. Indra is CPU only (no PyTorch, no GPU).
THREADS = int(os.getenv("INDRA_THREADS", str(os.cpu_count() or 4)))
DEFAULT_DPI = float(os.getenv("INDRA_DPI", "200"))
# Pages are OCRed one document at a time; extra concurrent requests wait for their turn.
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))

MODES = ("auto", "ocr", "text")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(SERVICE_ID)


def log_event(level: str, event: str, **fields):
    line = json.dumps({"event": event, **fields}, default=str)
    getattr(logger, level, logger.info)(line)


def apply_service_descriptor_overrides(descriptor: Dict[str, Any]) -> Dict[str, Any]:
    overrides = {
        "id": SERVICE_ID_OVERRIDE,
        "name": SERVICE_NAME_OVERRIDE,
        "adapter": SERVICE_ADAPTER_OVERRIDE,
        "local_url": SERVICE_LOCAL_URL_OVERRIDE,
    }
    for key, value in overrides.items():
        if isinstance(value, str) and value.strip():
            descriptor[key] = value.strip()
    return descriptor


def load_service_descriptor() -> Dict[str, Any]:
    try:
        with SERVICE_DESCRIPTOR_PATH.open("r", encoding="utf-8") as handle:
            descriptor = json.load(handle)
    except FileNotFoundError as err:
        raise RuntimeError(f"Descriptor file not found: {SERVICE_DESCRIPTOR_PATH}") from err
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Descriptor file is not valid JSON: {err}") from err
    if not isinstance(descriptor, dict):
        raise RuntimeError("Descriptor root must be a JSON object")
    return apply_service_descriptor_overrides(descriptor)


def load_help_markdown() -> str:
    for candidate in (SERVICE_HELP_PATH, SERVICE_HELP_FALLBACK_PATH):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise RuntimeError("Help markdown file not found")


def parse_bool_param(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_int_param(value: Any, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def parse_request_payload(raw: bytes) -> Dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as err:
        raise HTTPException(400, "Invalid JSON payload") from err
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request payload must be a JSON object")
    return payload


# Converters hold the ONNX sessions, so they are loaded on first use (keeps /health and /config
# fast; the models download on first load) and reused per option combination.
_converters: Dict[tuple, Any] = {}
MAX_CACHED_CONVERTERS = 4


def get_converter(mode: str, math: bool, furniture: bool, dpi: float):
    key = (mode, math, furniture, dpi)
    if key not in _converters:
        import indra_ocr
        if len(_converters) >= MAX_CACHED_CONVERTERS:
            _converters.pop(next(iter(_converters)))
        log_event("info", "model_load_start", mode=mode, math=math, furniture=furniture, dpi=dpi)
        _converters[key] = indra_ocr.load(threads=THREADS, dpi=dpi, mode=mode, furniture=furniture, math=math)
        log_event("info", "model_load_done")
    return _converters[key]


def run_ocr(path: Path, fmt: str, mode: str, math: bool, furniture: bool, dpi: float, max_pages: int) -> str:
    converter = get_converter(mode, math, furniture, dpi)
    chunks = []
    pages_json = []
    for index, page in converter.convert(path, max_pages=max_pages):
        if page.source == "error":
            log_event("warning", "page_failed", page=index + 1, note=page.note)
        if fmt == "json":
            pages_json.append({
                "page": index,
                "source": page.source,
                "rotation": page.rotation,
                "skew": page.skew,
                "vertical": page.vertical,
                "note": page.note,
                "lines": [asdict(line) for line in page.lines],
                "markdown": page.markdown,
                "blocks": page.blocks,
                "timings": page.timings,
            })
        elif fmt == "md":
            chunks.append(page.markdown if page.markdown is not None else page.text)
        else:
            chunks.append(page.text)
    if fmt == "json":
        return json.dumps({"pages": pages_json}, ensure_ascii=False)
    return "\n\n".join(chunks) + "\n"


async def read_upload_with_limit(upload: UploadFile, max_size: int, dest: Path = None) -> bytes:
    """Reads an upload in chunks, rejecting it above max_size. Streams to dest if given."""
    chunks = []
    total = 0
    handle = dest.open("wb") if dest else None
    try:
        while True:
            chunk = await upload.read(REQUEST_READ_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise HTTPException(413, f"Input exceeds the {max_size} byte limit")
            if handle:
                handle.write(chunk)
            else:
                chunks.append(chunk)
    finally:
        if handle:
            handle.close()
    return b"".join(chunks)


# task id -> (indra output format, output file name)
TASKS = {
    "ocr_markdown": ("md", "ocr.md"),
    "ocr_text": ("text", "ocr.txt"),
    "ocr_json": ("json", "ocr.json"),
}


def cleanup_stale_output(max_age_seconds: int) -> None:
    cutoff = time.time() - max_age_seconds
    try:
        entries = list(OUTPUT_DIR.iterdir())
    except FileNotFoundError:
        return
    for entry in entries:
        try:
            if not entry.is_dir() or entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            log_event("info", "output_cleanup_removed", dir=str(entry))
        except FileNotFoundError:
            continue
        except Exception as err:
            log_event("warning", "output_cleanup_failed", dir=str(entry), error=str(err))


async def output_cleanup_loop():
    while True:
        await asyncio.sleep(OUTPUT_CLEANUP_INTERVAL_SECONDS)
        cleanup_stale_output(OUTPUT_MAX_AGE_SECONDS)


_job_slots: asyncio.Semaphore = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _job_slots
    _job_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    cleanup_stale_output(OUTPUT_MAX_AGE_SECONDS)  # clear anything left over from a previous run
    task = asyncio.create_task(output_cleanup_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(
    title="Indra OCR API",
    description="OCR for images and PDFs with Indra OCR",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/files", StaticFiles(directory=str(OUTPUT_DIR)), name="files")


@app.get("/")
async def root():
    return {"message": "Indra OCR API for MessyDesk"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_ID}


@app.get("/config")
async def config():
    try:
        return load_service_descriptor()
    except RuntimeError as err:
        raise HTTPException(500, str(err))


@app.get("/help", response_class=PlainTextResponse)
async def help_markdown():
    try:
        return load_help_markdown()
    except RuntimeError as err:
        raise HTTPException(404, str(err))


@app.post("/process")
async def process(
    message: UploadFile = File(...),
    content: UploadFile = File(...),
):
    work_dir = Path(tempfile.mkdtemp(prefix="indra-"))
    try:
        request_json = parse_request_payload(await read_upload_with_limit(message, 1024 * 1024))

        task = request_json.get("task") if isinstance(request_json.get("task"), dict) else {}
        task_id = task.get("id")
        params = task.get("params") if isinstance(task.get("params"), dict) else {}
        if task_id not in TASKS:
            raise HTTPException(400, f"Unsupported task: {task_id}")
        fmt, output_name = TASKS[task_id]

        mode = str(params.get("mode") or "auto").strip()
        if mode not in MODES:
            mode = "auto"
        math = parse_bool_param(params.get("math"), default=False)
        furniture = parse_bool_param(params.get("keep_furniture"), default=False)
        max_pages = parse_int_param(params.get("max_pages"), default=0)

        input_path = work_dir / "input"
        await read_upload_with_limit(content, MAX_CONTENT_SIZE, dest=input_path)

        log_event("info", "process_start", task=task_id, mode=mode, math=math,
                  keep_furniture=furniture, max_pages=max_pages)
        async with _job_slots:
            result = await run_in_threadpool(
                run_ocr, input_path, fmt, mode, math, furniture, DEFAULT_DPI, max_pages
            )

        output_id = uuid.uuid4().hex
        output_dir = OUTPUT_DIR / output_id
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / output_name).write_text(result, encoding="utf-8")

        log_event("info", "process_done", task=task_id, output=str(output_dir / output_name))
        return {"response": {"uri": f"/files/{output_id}/{output_name}"}}
    except HTTPException:
        raise
    except Exception as err:
        log_event("error", "process_failed", error=str(err))
        raise HTTPException(500, f"Processing failed: {err}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "9013")))
