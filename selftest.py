"""Assert-based self-checks, model-free. CI runs `python -m selftest`.

Grows with the codebase; keep every check here runnable without a GPU,
Ollama server, or real family data.
"""

import json
import sqlite3


def check_dep_imports() -> None:
    import jieba
    import ollama
    import openpyxl
    import onnxruntime
    import PIL
    import plotly
    import pymupdf  # pymupdf (fitz API deprecated)
    import rapidocr
    import sqlite_vec
    import streamlit
    from docx import Document  # python-docx

    # silence "imported but unused" style lints; this check is about importability
    assert all(
        x is not None
        for x in (jieba, ollama, openpyxl, onnxruntime, PIL, plotly, pymupdf, rapidocr, sqlite_vec, streamlit, Document)
    )


def check_sqlite_vec_roundtrip() -> None:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(db)
    db.enable_load_extension(False)

    db.execute("CREATE VIRTUAL TABLE vec USING vec0(embedding float[4])")
    rows = [(1, json.dumps([0.1, 0.2, 0.3, 0.4])), (2, json.dumps([0.9, 0.8, 0.7, 0.6]))]
    db.executemany("INSERT INTO vec(rowid, embedding) VALUES (?, ?)", rows)
    got = db.execute(
        "SELECT rowid, distance FROM vec WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
        (json.dumps([0.1, 0.2, 0.3, 0.4]),),
    ).fetchall()
    assert got[0][0] == 1, f"kNN roundtrip failed: {got}"


def check_ocr_roundtrip() -> None:
    import numpy as np
    from PIL import Image, ImageDraw
    from rapidocr import RapidOCR

    img = Image.new("RGB", (320, 100), "white")
    ImageDraw.Draw(img).text((20, 30), "Family 1234", fill="black", font_size=32)
    result = RapidOCR()(np.asarray(img))
    text = " ".join(result.txts)
    assert "Family" in text and "1234" in text, f"unexpected OCR text: {text}"


def main() -> None:
    check_dep_imports()
    check_sqlite_vec_roundtrip()
    check_ocr_roundtrip()
    print("selftest: all checks passed")


if __name__ == "__main__":
    main()
