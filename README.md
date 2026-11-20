# MD-indra-ocr

[MessyDesk](https://github.com/OSC-JYU/MessyDesk) service for [Indra OCR](https://github.com/datalab-to/indra): CPU-only OCR for images and PDFs.

Crunchers: `ocr_markdown`, `ocr_text`, `ocr_json` (see [help/index.md](help/index.md)).

Adapter: `elg` (HTTP). Port: 9013. Python 3.10+ is required by `indra-ocr`.

## Run locally

    python3.11 -m venv venv && ./venv/bin/pip install -r requirements.txt
    ./venv/bin/python api.py

## Run with MessyDesk (compose)

Start MessyDesk's local compose project first, then:

    podman-compose up -d --build

This starts the service and its consumer (see comments in [.env.example](.env.example) for overrides).
Models are cached in the `indra-models` volume.

## Configuration (env)

| Variable | Default | |
|---|---|---|
| `INDRA_THREADS` | CPU count | ONNX Runtime threads |
| `INDRA_DPI` | 200 | PDF render resolution |
| `MAX_CONCURRENT_JOBS` | 1 | simultaneous OCR jobs |
| `MAX_CONTENT_SIZE` | 100 MB | input size limit |
| `OUTPUT_MAX_AGE_SECONDS` | 3600 | output files older than this are swept |

Model license: modified OpenRAIL-M (see the Indra repository).
