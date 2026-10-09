import json
import tempfile
import unittest
from pathlib import Path

import ingest
import parsers
import textutil


class PDFOffsetTests(unittest.TestCase):
    def test_real_text_pdf_page_can_be_parsed_into_multiple_chunks(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "synthetic.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for line in range(50):
                    page.insert_text((30, 30 + line * 14), f"Line {line:02d}: " + "family document " * 4, fontsize=9)
                doc.save(path)
            parsed = parsers.parse_pdf(path)
            self.assertGreater(len(parsed.segments[0].text), ingest.CHUNK_TARGET)
            chunks, pages, kinds, positions = ingest._chunk(parsed)
            self.assertGreater(len(chunks), 1)
            for chunk, position in zip(chunks, positions):
                start, end = json.loads(position)["char_range"]
                self.assertEqual(parsed.segments[0].text[start:end], chunk)

    def test_long_nonrepeating_and_repeated_pages_have_exact_overlapping_spans(self):
        samples = ["".join(chr(0x4e00 + i) for i in range(2400)), "repeated text " * 240,
                   ("line " * 150 + "\n") * 4]
        for page, text in enumerate(samples):
            with self.subTest(page=page):
                parsed = parsers.Parsed(segments=[parsers.Segment(text=text, page=page, position={"page": page})])
                chunks, pages, kinds, positions = ingest._chunk(parsed)
                self.assertGreater(len(chunks), 1)
                covered_end = 0
                previous_start = -1
                for chunk, chunk_page, position in zip(chunks, pages, positions):
                    marker = json.loads(position)
                    start, end = marker["char_range"]
                    self.assertEqual(text[start:end], chunk)
                    self.assertEqual(marker["page"], page)
                    self.assertEqual(chunk_page, page)
                    self.assertGreater(start, previous_start)
                    self.assertLessEqual(start, covered_end)
                    previous_start, covered_end = start, end
                self.assertEqual(covered_end, len(text))
                self.assertEqual(chunks, textutil.split_text(text, target=ingest.CHUNK_TARGET, overlap=ingest.CHUNK_OVERLAP))

    def test_non_pdf_markers_are_preserved(self):
        marker = {"sheet": "Bills", "rows": [1, 20]}
        parsed = parsers.Parsed(segments=[parsers.Segment(text="line " * 500, kind="table", position=marker)])
        chunks, pages, kinds, positions = ingest._chunk(parsed)
        self.assertTrue(all(json.loads(p) == marker for p in positions))

    def test_newline_backoff_always_advances_and_invalid_settings_fail(self):
        spans = textutil.split_text_spans("abcdefg\n" * 4, target=10, overlap=9)
        self.assertTrue(all(b[0] > a[0] for a, b in zip(spans, spans[1:])))
        for target, overlap in ((0, 0), (10, 10), (10, -1)):
            with self.assertRaises(ValueError):
                textutil.split_text_spans("text", target=target, overlap=overlap)
