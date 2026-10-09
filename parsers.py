"""parsers.py — one parser per format; extension → parser dispatch.

Parsers return Segments with stable position markers (page, char range, sheet+rows,
paragraph range, OCR bbox) so extraction evidence is chunk-id based, never name search.
"""

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

KIND_BY_EXT = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".json": "json",
    ".txt": "txt",
    ".md": "md",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".tiff": "image",
    ".webp": "image",
}
WHITELIST = frozenset(KIND_BY_EXT)
SKIP_EXTS = frozenset({".xls", ".xlsm"})  # macro formats: never parsed, logged and skipped

TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "cp1252")


@dataclass(frozen=True)
class Segment:
    text: str
    page: int | None = None
    kind: str = "body"  # body|caption|table
    position: dict | None = None  # {"page", "char_range"} | {"sheet", "rows"} | {"para_range"} | {"bboxes"}


@dataclass
class Parsed:
    segments: list[Segment]
    exif_date: str | None = None
    meta: dict = field(default_factory=dict)


def read_text_with_sniff(path: Path) -> str:
    """Read text file, sniffing common encodings (incl. gb18030 for CN bank exports)."""
    data = path.read_bytes()
    for enc in TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_ocr_engine = None  # lazy singleton: import + model load costs ~1s


def _ocr(np_img) -> str:
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr import RapidOCR

        _ocr_engine = RapidOCR()
    result = _ocr_engine(np_img)
    return "\n".join(result.txts)


def _ocr_boxes(np_img) -> tuple[str, list]:
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr import RapidOCR

        _ocr_engine = RapidOCR()
    result = _ocr_engine(np_img)
    return "\n".join(result.txts), result.boxes.tolist()


def _exif_date(img) -> str | None:
    exif = img.getexif()
    for tag in (36867, 306):  # DateTimeOriginal, DateTime — GPS deliberately never read
        val = exif.get(tag)
        if val:
            return str(val).strip() or None
    return None


def parse_pdf(path: Path) -> Parsed:
    import numpy as np
    import pymupdf

    doc = pymupdf.open(path)
    segments: list[Segment] = []
    for n, page in enumerate(doc):
        text = page.get_text()
        if len(text.strip()) < 50:  # scanned page: OCR the render
            pix = page.get_pixmap(dpi=150)
            text = _ocr(np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n))
        segments.append(Segment(text=text, page=n, position={"page": n}))
    return Parsed(segments=segments, meta={"page_count": doc.page_count})


def parse_docx(path: Path) -> Parsed:
    from docx import Document

    doc = Document(path)
    segments: list[Segment] = []
    para_i = 0
    buf: list[str] = []
    buf_start = 0
    for block in doc.element.body:
        if block.tag.endswith("}p"):
            text = block.text or ""
            if not buf:
                buf_start = para_i
            buf.append(text)
            para_i += 1
            if sum(len(t) for t in buf) > 2000:
                segments.append(Segment(text="\n".join(buf), position={"para_range": [buf_start, para_i - 1]}))
                buf = []
        elif block.tag.endswith("}tbl"):
            from docx.table import Table

            table = Table(block, doc)
            rows = [" | ".join(c.text for c in row.cells) for row in table.rows]
            segments.append(Segment(text="\n".join(rows), kind="table", position={"table_index": len(segments)}))
    if buf:
        segments.append(Segment(text="\n".join(buf), position={"para_range": [buf_start, para_i - 1]}))
    return Parsed(segments=segments)


def parse_xlsx(path: Path) -> Parsed:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    segments: list[Segment] = []
    for sheet in wb.worksheets:
        rows: list[str] = []
        start = None
        for r, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            cells = [str(v) for v in row if v is not None]
            if not cells:
                continue
            if start is None:
                start = r
            rows.append(f"{r}: " + " | ".join(cells))
            if len(rows) >= 50:
                segments.append(Segment(text="\n".join(rows), kind="table",
                                        position={"sheet": sheet.title, "rows": [start, r]}))
                rows, start = [], None
        if rows:
            segments.append(Segment(text="\n".join(rows), kind="table",
                                    position={"sheet": sheet.title, "rows": [start, sheet.max_row]}))
    wb.close()
    return Parsed(segments=segments, meta={"sheets": wb.sheetnames if hasattr(wb, "sheetnames") else None})


def parse_csv(path: Path) -> Parsed:
    text = read_text_with_sniff(path)
    rows = list(csv.reader(text.splitlines()))
    lines = [" | ".join(cells) for cells in rows if any(cells)]
    segments = [
        Segment(text="\n".join(lines[i:i + 50]), kind="table", position={"rows": [i + 1, i + 50]})
        for i in range(0, len(lines), 50)
    ]
    return Parsed(segments=segments)


def parse_json(path: Path) -> Parsed:
    text = read_text_with_sniff(path)
    data = json.loads(text)  # raises on invalid JSON -> run marked error
    normalized = json.dumps(data, ensure_ascii=False, default=str)
    from textutil import split_text

    return Parsed(segments=[Segment(text=p) for p in split_text(normalized, target=2000)])


def parse_image(path: Path) -> Parsed:
    import numpy as np
    from PIL import Image

    with Image.open(path) as img:
        exif = _exif_date(img)
        np_img = np.asarray(img.convert("RGB"))
    text, boxes = _ocr_boxes(np_img)
    return Parsed(segments=[Segment(text=text, kind="body", position={"bboxes": boxes})],
                  exif_date=exif, meta={"ocr": True})


def parse_text(path: Path) -> Parsed:
    return Parsed(segments=[Segment(text=read_text_with_sniff(path))])


_PARSERS = {
    "pdf": parse_pdf,
    "docx": parse_docx,
    "xlsx": parse_xlsx,
    "csv": parse_csv,
    "json": parse_json,
    "image": parse_image,
    "txt": parse_text,
    "md": parse_text,
}


def parse(path: Path) -> Parsed:
    kind = KIND_BY_EXT[path.suffix.lower()]
    return _PARSERS[kind](path)
