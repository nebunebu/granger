"""Open a book, pull its figures out, write them under a directory named after it."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .errors import CaptionsUnavailableError, NoImagesError, UnsupportedFormatError
from .segment import Params, assign_captions, captioned_box, detect_figures

Image.MAX_IMAGE_PIXELS = None

MANIFEST = "manifest.json"
PDF_TYPES = {".pdf"}
EPUB_TYPES = {".epub"}
KINDLE_TYPES = {".azw3", ".azw", ".kf8", ".mobi"}
REJECTED = {
    ".djvu": "DjVu stores a page as a low-resolution background plus a high-resolution "
    "foreground mask, and the mask holds the text and the line artwork together. "
    "Neither layer is a text-free picture, so figures cannot be separated.",
    ".djv": "see .djvu",
    ".cbz": "comic archives are bare page scans with no text layer.",
    ".cbr": "comic archives are bare page scans with no text layer.",
    ".txt": "plain text has no images.",
    ".fb2": "not implemented.",
    ".docx": "not implemented.",
}
BOOK_TYPES = PDF_TYPES | EPUB_TYPES | KINDLE_TYPES | {".djvu", ".djv", ".cbz", ".cbr", ".fb2"}


@dataclass
class Options:
    out: Path = Path("out")
    captions: str = "without"  # without | with | both
    dpi: int = 200
    format: str = "jpg"
    quality: int = 90
    pages: list[int] | None = None
    min_pixels: int = 150
    overwrite: bool = False
    debug: bool = False
    params: Params = field(default_factory=Params)


@dataclass
class Result:
    title: str
    directory: Path
    kind: str
    figures: int
    captioned: int
    pages: int


def open_book(path: Path, dpi: int):
    ext = path.suffix.lower()
    if ext in PDF_TYPES:
        from .pdf import PdfBook

        return PdfBook(path, dpi)
    if ext in EPUB_TYPES:
        from .epub import EpubBook

        return EpubBook(path)
    if ext in KINDLE_TYPES:
        from .epub import open_kindle

        return open_kindle(path)
    if ext in REJECTED:
        reason = REJECTED[".djvu"] if ext == ".djv" else REJECTED[ext]
        raise UnsupportedFormatError(f"{ext} files are not supported: {reason}")
    raise UnsupportedFormatError(f"{ext or 'extensionless'} files are not supported")


def extract_book(path: Path, opts: Options, progress=None) -> Result:
    """Extract one book. Raises UnsupportedBookError if it cannot be done."""
    path = Path(path)
    if not path.is_file():
        raise UnsupportedFormatError("not a file")
    book = open_book(path, opts.dpi)
    try:
        want_captions = opts.captions in ("with", "both")
        if want_captions and not book.supports_captions:
            raise CaptionsUnavailableError(book.captions_reason)
        title = clean_title(book.title) or title_from_filename(path)
        directory = _prepare_directory(opts.out / safe_name(title), path, opts.overwrite)
        try:
            if book.layout == "flow":
                figures, pages = _extract_flow(book, directory, opts, progress)
            else:
                figures, pages = _extract_paged(book, directory, opts, progress)
            if not figures:
                raise NoImagesError(f"no figures found on {pages} page(s)")
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        manifest = {
            "title": title,
            "source": path.name,
            "format": book.kind,
            "captions": opts.captions,
            "coordinates": "pixels on the page image; subtract a box's first two numbers to get crop-relative positions",
            "figures": figures,
        }
        (directory / MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
        return Result(title, directory, book.kind, len(figures), sum(1 for f in figures if f["caption"]), pages)
    finally:
        book.close()


# -- fixed-layout books --------------------------------------------------------


def _extract_paged(book, directory: Path, opts: Options, progress):
    want_plain = opts.captions in ("without", "both")
    want_captions = opts.captions in ("with", "both")
    records, pages = [], 0
    for page in book.pages(opts.pages, need_full=want_captions or opts.debug):
        pages += 1
        if progress:
            progress(page.index, book.page_count)
        figures = detect_figures(page.clean, page.blocks, opts.params)
        assign_captions(figures, page.blocks, page.clean.size, opts.params)
        for k, fig in enumerate(figures, start=1):
            stem = f"p{page.index:04d}_{k:02d}"
            record = {
                "id": stem,
                "page": page.index,
                "page_label": page.label,
                "whole_page": fig.whole_page,
                "box": list(fig.box),
                "caption": "\n".join(b.text for b in fig.blocks),
                "texts": [{"text": b.text, "box": list(b.box)} for b in fig.blocks],
            }
            if want_plain:
                record["image"] = _save(page.clean.crop(fig.box), directory / stem, opts)
            if want_captions:
                box = captioned_box(fig, page.full.size)
                record["captioned_image"] = _save(page.full.crop(box), directory / f"{stem}_cap", opts)
                record["captioned_box"] = list(box)
            records.append(record)
        if opts.debug:
            _debug_page(page, figures, directory)
    return records, pages


def _debug_page(page, figures, directory: Path) -> None:
    out = directory / "_debug"
    out.mkdir(exist_ok=True)
    image = (page.full or page.clean).copy()
    draw = ImageDraw.Draw(image)
    for b in page.blocks:
        draw.rectangle(b.box, outline=(60, 120, 255), width=1)
    for k, fig in enumerate(figures, start=1):
        draw.rectangle(fig.box, outline=(255, 0, 0), width=4)
        draw.text((fig.box[0] + 6, fig.box[1] + 4), str(k), fill=(255, 0, 0), font=_font(28))
        for b in fig.blocks:
            draw.rectangle(b.box, outline=(0, 170, 0), width=3)
    image.thumbnail((1400, 1400))
    image.save(out / f"p{page.index:04d}.jpg", quality=80)


# -- reflowable books ----------------------------------------------------------


def _extract_flow(book, directory: Path, opts: Options, progress):
    want_plain = opts.captions in ("without", "both")
    want_captions = opts.captions in ("with", "both")
    records, pages, counts = [], set(), {}
    for item in book.images(opts.pages):
        pages.add(item.index)
        if progress:
            progress(item.index, book.page_count)
        try:
            image = Image.open(io.BytesIO(item.data))
            image.load()
        except Exception:
            continue
        if min(image.size) < opts.min_pixels:
            continue  # ornaments, icons, rules
        k = counts[item.index] = counts.get(item.index, 0) + 1
        stem = f"p{item.index:04d}_{k:02d}"
        record = {
            "id": stem,
            "page": item.index,
            "page_label": item.label,
            "source_name": item.name,
            "caption": item.caption,
        }
        if want_plain:
            # The book's own file, byte for byte: no recompression.
            ext = Path(item.name).suffix.lower() or ".jpg"
            (directory / f"{stem}{ext}").write_bytes(item.data)
            record["image"] = f"{stem}{ext}"
        if want_captions:
            record["captioned_image"] = _save(_with_caption(image, item.caption), directory / f"{stem}_cap", opts)
        records.append(record)
    return records, len(pages)


def _with_caption(image: Image.Image, caption: str) -> Image.Image:
    """Typeset the caption on a white strip under the picture."""
    image = image.convert("RGB")
    if not caption:
        return image
    size = max(14, round(image.width / 42))
    margin = size
    head, body = _font(size, bold=True), _font(size)
    probe = ImageDraw.Draw(image)
    lines: list[tuple[str, ImageFont.FreeTypeFont]] = []
    paragraphs = caption.split("\n")
    for n, paragraph in enumerate(paragraphs):
        font = head if n == 0 and len(paragraphs) > 1 else body
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if current and probe.textlength(trial, font=font) > image.width - 2 * margin:
                lines.append((current, font))
                current = word
            else:
                current = trial
        if current:
            lines.append((current, font))
    leading = round(size * 1.35)
    out = Image.new("RGB", (image.width, image.height + 2 * margin + leading * len(lines)), "white")
    out.paste(image, (0, 0))
    draw = ImageDraw.Draw(out)
    for n, (text, font) in enumerate(lines):
        draw.text((margin, image.height + margin + n * leading), text, fill=(20, 20, 20), font=font)
    return out


def _font(size: int, bold: bool = False):
    path = os.environ.get("GRANGER_FONT_BOLD" if bold else "GRANGER_FONT")
    if path and os.path.exists(path):
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


# -- output --------------------------------------------------------------------


def _save(image: Image.Image, stem: Path, opts: Options) -> str:
    if opts.format == "png":
        path = stem.with_name(stem.name + ".png")
        image.save(path, optimize=True)
    else:
        path = stem.with_name(stem.name + ".jpg")
        image.convert("RGB").save(path, quality=opts.quality, optimize=True)
    return path.name


def _prepare_directory(directory: Path, source: Path, overwrite: bool) -> Path:
    if directory.exists():
        ours = (directory / MANIFEST).is_file()
        if not overwrite:
            raise FileExistsError(
                f"output directory already exists: {directory} (pass --overwrite to replace it)"
            )
        if not ours and any(directory.iterdir()):
            raise FileExistsError(
                f"refusing to overwrite {directory}: it has no {MANIFEST}, so it was not created by granger"
            )
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory


_JUNK_TITLE = re.compile(r"^(untitled|unknown|microsoft word|.*\.(indd|pdf|qxd|doc|docx))$", re.I)


def clean_title(title: str | None) -> str:
    title = " ".join((title or "").split())
    return "" if _JUNK_TITLE.match(title) or len(title) < 2 else title


def title_from_filename(path: Path) -> str:
    name = path.stem
    name = re.sub(r"\{[^}]*\}|\[[^\]]*\]", " ", name)  # {author} [series]
    name = re.sub(r"\([^)]*(z-lib|1lib|libgen|z-library|annas?-archive)[^)]*\)", " ", name, flags=re.I)
    name = re.sub(r"[-–\s]*\b(libgen\.\w+|z-lib\.\w+)\b", " ", name, flags=re.I)
    name = re.sub(r"\(\s*\d{4}[^)]*\)", " ", name)  # (2021, Publisher)
    name = name.replace("_ ", ": ").replace("_", " ")
    return " ".join(name.split()).strip(" -–,.") or path.stem


def safe_name(title: str) -> str:
    name = re.sub(r"\s*:\s*", " - ", title)
    name = re.sub(r'[/\\*?"<>|\x00-\x1f]', " ", name)
    return " ".join(name.split()).strip(" .")[:150] or "untitled"


def stderr_progress(name: str):
    def show(index: int, total: int) -> None:
        if sys.stderr.isatty():
            print(f"\r  {name[:50]}: page {index}/{total}", end="", file=sys.stderr, flush=True)

    return show
