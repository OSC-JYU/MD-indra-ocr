# MD-indra-ocr Service

The **MD-indra-ocr** service uses **[Indra OCR](https://github.com/datalab-to/indra)**, a small and fast OCR model that runs on CPU, to turn images and PDFs into text. It reads printed text and handwriting in 80+ languages, and handles pages at any angle.

---

## Available Crunchers

### `ocr_markdown` — Image to Markdown
Recognises the text and the page layout, and writes Markdown with headings, lists and tables. Multi-page PDFs become one Markdown file.

* **PDF text source** — *Automatic* (default) uses the PDF's embedded text where it is reliable and OCRs the rest. *Always OCR* ignores the embedded text. *Embedded text only* never runs OCR. Images are always OCRed.
* **Math as LaTeX** — writes formulas as `$...$`. Slower on pages with math.
* **Keep page headers and footers** — off by default.
* **Max pages** — read only the first N pages of a PDF (0 = all).

### `ocr_text` — Image to text
Writes plain text, one line per detected line, in reading order.

### `ocr_json` — Image to OCR data (JSON)
Writes one JSON object with a `pages` list. Each page has `lines` (text, box, confidence), layout `blocks` (label, box, text), the Markdown, rotation and timings. Useful when you need positions or confidences.

## Limits
* Handwriting is supported but less accurate than printed text.
* Input files are limited to 100 MB per request.
* The models (about 1M parameters) are downloaded on the first run.

---

## External Resources
* [Indra OCR GitHub repository](https://github.com/datalab-to/indra)
