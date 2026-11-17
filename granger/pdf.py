"""PDF adapter: born-digital PDFs only.

A page is rendered twice: once as printed, once with every text object
deleted. The second render is the artwork figures are cut from.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

import pymupdf
from PIL import Image

from .errors import DrmError, ScannedBookError, UnsupportedFormatError
from .model import Page, TextBlock
from .segment import join_lines


class PdfBook:
    layout = "paged"
    kind = "PDF"
    supports_captions = True

    def __init__(self, path: Path, dpi: int = 200):
        self.path = path
        self.dpi = dpi
        try:
            self.doc = pymupdf.open(path)
        except Exception as exc:
            raise UnsupportedFormatError(f"cannot open as PDF: {exc}") from exc
        if self.doc.needs_pass:
            raise DrmError("PDF is password protected")
        self.title = (self.doc.metadata or {}).get("title") or ""
        self.page_count = len(self.doc)
        self._reject_scans()

    def _reject_scans(self) -> None:
        """A scan is a page-sized image with no visible text drawn over it."""
        n = self.page_count
        sample = sorted({int(n * (i + 0.5) / 24) for i in range(24)})
        scanned = hidden_ocr = 0
        for i in sample:
            page = self.doc[i]
            area = abs(page.rect)
            covered = any(
                abs(pymupdf.Rect(info["bbox"]) & page.rect) >= 0.85 * area
                for info in page.get_image_info()
            )
            visible = hidden = 0
            for span in page.get_texttrace():
                if span["type"] == 3 or not span.get("opacity", 1):
                    hidden += len(span["chars"])
                else:
                    visible += len(span["chars"])
            if covered and visible < 20:
                scanned += 1
                hidden_ocr += hidden > 20
        if scanned > len(sample) / 2:
            what = (
                "the only text is an invisible OCR layer"
                if hidden_ocr > scanned / 2
                else "there is no text layer at all"
            )
            raise ScannedBookError(
                f"scanned PDF: {scanned} of {len(sample)} sampled pages are a single "
                f"page-sized photograph and {what}. Text is baked into the page image, "
                "so figures cannot be separated from it."
            )

    def pages(self, wanted: Iterable[int] | None, need_full: bool) -> Iterator[Page]:
        scale = self.dpi / 72
        for index in wanted or range(1, self.page_count + 1):
            if not 1 <= index <= self.page_count:
                continue
            page = self.doc[index - 1]
            if page.rotation:
                page.remove_rotation()
            blocks = _text_blocks(page, scale)
            full = _render(page, self.dpi) if need_full else None
            if blocks:
                page.add_redact_annot(page.rect)
                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE,
                )
            clean = _render(page, self.dpi)
            yield Page(index, page.get_label() or str(index), clean, full, blocks)

    def close(self) -> None:
        self.doc.close()


def _render(page: pymupdf.Page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csRGB)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _text_blocks(page: pymupdf.Page, scale: float) -> list[TextBlock]:
    blocks = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        lines, sizes = [], {}
        for line in block["lines"]:
            text = "".join(span["text"] for span in line["spans"])
            if text.strip():
                lines.append(text)
            for span in line["spans"]:
                sizes[span["size"]] = sizes.get(span["size"], 0) + len(span["text"].strip())
        text = join_lines(lines)
        if not text or not any(sizes.values()):
            continue
        x0, y0, x1, y1 = block["bbox"]
        blocks.append(
            TextBlock(
                (int(x0 * scale), int(y0 * scale), round(x1 * scale + 0.5), round(y1 * scale + 0.5)),
                text,
                max(sizes, key=sizes.get) * scale,
                len(lines),
            )
        )
    return blocks
