"""EPUB adapter (also used for AZW3 after unpacking).

Three kinds of EPUB turn up in illustrated books:

flow     Reflowable, semantic markup: every picture is its own file and
         captions are <figcaption>. Nothing to detect.
overlay  Fixed layout: each page is one background image holding all the
         artwork, with the text as HTML positioned on top. The background
         is already text-free, but placing the text needs a browser engine.
baked    Fixed layout from a Kindle PDF conversion: the page image includes
         the text, and an invisible word-by-word overlay says where it is.
"""

from __future__ import annotations

import io
import posixpath
import re
import shutil
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

from PIL import Image

from .errors import DrmError, ScannedBookError, UnsupportedFormatError
from .model import FlowImage, Page, TextBlock
from .segment import erase_text, group_lines

_IMG = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_VIEWPORT = re.compile(r"width\s*=\s*([\d.]+)\s*,\s*height\s*=\s*([\d.]+)")
_FONT_SIZE = re.compile(r"\.([\w-]+)\s*\{[^}]*?font-size:\s*([\d.]+)px", re.S)
_TRANSPARENT = re.compile(r"\{[^}]*color:\s*transparent", re.S)
_TAGS = re.compile(r"<[^>]+>")
# Font obfuscation is listed in encryption.xml but is not DRM.
_FONT_MANGLING = ("idpf.org/2008/embedding", "ns.adobe.com/pdf/enc#RC")


class EpubBook:
    supports_captions = True

    def __init__(self, path: Path, kind: str = "EPUB", cleanup: Path | None = None):
        self.path = path
        self._cleanup = cleanup
        try:
            self.zip = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise UnsupportedFormatError(f"not a valid EPUB (zip) file: {exc}") from exc
        self.names = set(self.zip.namelist())
        self._check_drm()
        self._read_package()
        self.page_count = len(self.spine)
        self._classify()
        self.kind = f"{kind} ({self.variant})"

    # -- package -----------------------------------------------------------

    def _check_drm(self) -> None:
        if "META-INF/rights.xml" in self.names:
            raise DrmError("EPUB is DRM protected (META-INF/rights.xml present)")
        if "META-INF/encryption.xml" in self.names:
            xml = self.zip.read("META-INF/encryption.xml").decode("utf8", "replace")
            algorithms = re.findall(r"Algorithm\s*=\s*\"([^\"]+)\"", xml)
            if any(not any(m in a for m in _FONT_MANGLING) for a in algorithms):
                raise DrmError("EPUB content is encrypted (DRM)")

    def _read_package(self) -> None:
        try:
            container = ElementTree.fromstring(self.zip.read("META-INF/container.xml"))
            opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
            self.opf_text = self.zip.read(opf_path).decode("utf8", "replace")
            opf = ElementTree.fromstring(self.opf_text)
        except (KeyError, StopIteration, ElementTree.ParseError) as exc:
            raise UnsupportedFormatError(f"EPUB package is unreadable: {exc}") from exc
        base = posixpath.dirname(opf_path)
        self.title = next((e.text or "" for e in opf.iter() if e.tag.endswith("}title")), "").strip()
        manifest = {
            e.get("id"): posixpath.normpath(posixpath.join(base, unquote(e.get("href", ""))))
            for e in opf.iter()
            if e.tag.endswith("}item")
        }
        self.spine = [
            manifest[e.get("idref")]
            for e in opf.iter()
            if e.tag.endswith("}itemref") and manifest.get(e.get("idref")) in self.names
        ]
        self.css = "\n".join(
            self.zip.read(n).decode("utf8", "replace") for n in self.names if n.lower().endswith(".css")
        )

    def _classify(self) -> None:
        sample = [self._doc(n) for n in self.spine[:: max(1, len(self.spine) // 20)][:20]]
        if not sample:
            raise UnsupportedFormatError("EPUB has no readable content documents")
        paged = sum(1 for d in sample if _VIEWPORT.search(d) and len(_IMG.findall(d)) == 1)
        fixed = "pre-paginated" in self.opf_text or paged > len(sample) / 2
        if not fixed:
            self.variant, self.layout = "reflowable", "flow"
            return
        self.layout = "paged"
        words = sum(1 for d in sample if re.search(r'class="word\b', d))
        if words > len(sample) / 3 and _TRANSPARENT.search(self.css + "".join(sample)):
            self.variant = "fixed layout, text baked into page images"
            self.baked = True
            self.font_px = {m[0]: float(m[1]) for m in _FONT_SIZE.findall(self.css)}
            return
        text = sum(len(_TAGS.sub("", re.sub(r"(?s)<(head|style)\b.*?</\1>", "", d)).strip()) for d in sample)
        if text / len(sample) < 20:
            raise ScannedBookError(
                "fixed-layout EPUB whose pages are bare page images with no text layer: "
                "text is baked into the pictures, so figures cannot be separated from it."
            )
        self.variant = "fixed layout, HTML text over artwork"
        self.baked = False
        self.supports_captions = False
        self.captions_reason = (
            "this fixed-layout EPUB draws its text as positioned HTML over a text-free "
            "background image; reproducing that needs a browser engine. Images can still "
            "be extracted without captions."
        )

    def _doc(self, name: str) -> str:
        return self.zip.read(name).decode("utf8", "replace")

    def _resolve(self, doc: str, href: str) -> str:
        return posixpath.normpath(posixpath.join(posixpath.dirname(doc), unquote(href.split("#")[0])))

    # -- fixed layout ------------------------------------------------------

    def pages(self, wanted: Iterable[int] | None, need_full: bool) -> Iterator[Page]:
        for index in wanted or range(1, len(self.spine) + 1):
            if not 1 <= index <= len(self.spine):
                continue
            name = self.spine[index - 1]
            doc = self._doc(name)
            image = self._page_image(name, doc)
            if image is None:
                continue
            if not self.baked:
                yield Page(index, str(index), image, None, [])
                continue
            lines = self._kindle_lines(doc, image.size)
            words = [w for line in lines for w in line.words]
            clean = erase_text(image, words)
            blocks = group_lines([TextBlock(l.box, l.text, l.size) for l in lines])
            yield Page(index, str(index), clean, image, blocks)

    def _page_image(self, name: str, doc: str) -> Image.Image | None:
        best = None
        for src in _IMG.findall(doc):
            target = self._resolve(name, src)
            if target not in self.names:
                continue
            try:
                im = Image.open(io.BytesIO(self.zip.read(target)))
            except Exception:
                continue
            if best is None or im.width * im.height > best.width * best.height:
                best = im
        return best.convert("RGB") if best else None

    def _kindle_lines(self, doc: str, size: tuple[int, int]) -> list["_Line"]:
        view = _VIEWPORT.search(doc)
        sx = size[0] / float(view[1]) if view else 1.0
        sy = size[1] / float(view[2]) if view else 1.0
        parser = _KindleWords(self.font_px, sx, sy)
        parser.feed(doc)
        return parser.lines

    # -- reflowable --------------------------------------------------------

    def images(self, wanted: Iterable[int] | None) -> Iterator[FlowImage]:
        docs = {name: self._doc(name) for name in self.spine}
        uses = Counter(self._resolve(n, src) for n, d in docs.items() for src in _IMG.findall(d))
        seen: set[str] = set()
        for index in wanted or range(1, len(self.spine) + 1):
            if not 1 <= index <= len(self.spine):
                continue
            name = self.spine[index - 1]
            parser = _FlowFigures()
            parser.feed(docs[name])
            for src, caption, label in parser.found:
                target = self._resolve(name, src)
                # An image used again and again is an ornament, not a figure.
                if target in seen or target not in self.names or uses[target] > 2:
                    continue
                seen.add(target)
                yield FlowImage(index, label or str(index), posixpath.basename(target), self.zip.read(target), caption)

    def close(self) -> None:
        self.zip.close()
        if self._cleanup:
            shutil.rmtree(self._cleanup, ignore_errors=True)


def open_kindle(path: Path) -> EpubBook:
    """Unpack AZW3/KF8 to EPUB with KindleUnpack, then read that."""
    import contextlib

    import mobi

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            tempdir, unpacked = mobi.extract(str(path))
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        if "drm" in reason.lower() or "encrypt" in reason.lower():
            raise DrmError(f"Kindle book is DRM protected: {reason}") from exc
        raise UnsupportedFormatError(f"cannot unpack Kindle book: {reason}") from exc
    if not unpacked.lower().endswith(".epub"):
        shutil.rmtree(tempdir, ignore_errors=True)
        raise UnsupportedFormatError(
            "old-style MOBI without a KF8 (AZW3) section: only a flattened HTML dump "
            "is available, which keeps neither layout nor captions"
        )
    try:
        return EpubBook(Path(unpacked), kind="AZW3", cleanup=Path(tempdir))
    except Exception:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise


class _Line:
    def __init__(self) -> None:
        self.words: list[tuple[int, int, int, int]] = []
        self.parts: list[str] = []
        self.size = 0.0

    @property
    def box(self):
        return (
            min(w[0] for w in self.words),
            min(w[1] for w in self.words),
            max(w[2] for w in self.words),
            max(w[3] for w in self.words),
        )

    @property
    def text(self) -> str:
        return " ".join(self.parts)


class _KindleWords(HTMLParser):
    """Reads <span class="line"><span class="word" style="left;top;width">."""

    def __init__(self, font_px: dict[str, float], sx: float, sy: float):
        super().__init__()
        self.font_px, self.sx, self.sy = font_px, sx, sy
        self.lines: list[_Line] = []
        self.stack: list[str] = []
        self.line: _Line | None = None
        self.word: tuple | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "span":
            return
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        role = "line" if "line" in classes else "word" if "word" in classes else ""
        self.stack.append(role)
        if role == "line":
            self.line = _Line()
        elif role == "word" and self.line is not None:
            style = a.get("style") or ""
            get = lambda k: float(m[1]) if (m := re.search(rf"\b{k}:\s*(-?[\d.]+)px", style)) else None
            left, top, width = get("left"), get("top"), get("width")
            size = next((self.font_px[c] for c in classes if c in self.font_px), 16.0)
            if left is not None and top is not None and width:
                self.word = (left, top, width, size)

    def handle_data(self, data):
        if self.word and self.line is not None and data.strip():
            left, top, width, size = self.word
            self.line.words.append(
                (
                    int(left * self.sx),
                    int(top * self.sy),
                    round((left + width) * self.sx + 0.5),
                    round((top + size * 1.25) * self.sy + 0.5),
                )
            )
            self.line.parts.append(data.strip())
            self.line.size = max(self.line.size, size * self.sy)
            self.word = None

    def handle_endtag(self, tag):
        if tag != "span" or not self.stack:
            return
        role = self.stack.pop()
        if role == "word":
            self.word = None
        elif role == "line":
            if self.line and self.line.words:
                self.lines.append(self.line)
            self.line = None


class _FlowFigures(HTMLParser):
    """Collects (src, caption, page label) for every <img> in a document."""

    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self.found: list[list] = []
        self.label = ""
        self.figure_start: int | None = None  # index into found where the open <figure> began
        self.capturing: str | None = None  # tag whose text is being collected as a caption
        self.depth = 0
        self.text: list[str] = []
        self.since_image = 0  # characters of ordinary text since the last image

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("epub:type") == "pagebreak" or a.get("role") == "doc-pagebreak":
            m = re.search(r"\d+|[ivxlcdm]+$", a.get("aria-label") or a.get("title") or a.get("id") or "", re.I)
            if m:
                self.label = m[0]
        if tag == "img" and a.get("src"):
            self.found.append([a["src"], "", self.label])
            self.since_image = 0
            return
        if tag == "figure" and self.figure_start is None:
            self.figure_start = len(self.found)
        if self.capturing:
            if tag == self.capturing:
                self.depth += 1
            if tag in self.BLOCK:
                self.text.append("\n")
        elif tag == "figcaption" or (
            self.figure_start is None
            and "caption" in (a.get("class") or "").lower()
            and self.found
            and not self.found[-1][1]
            and self.since_image < 20
        ):
            self.capturing, self.depth, self.text = tag, 1, []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag != "img":
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.capturing:
            self.text.append(data)
        else:
            self.since_image += len(data.strip())

    def handle_endtag(self, tag):
        if self.capturing and tag in self.BLOCK:
            self.text.append("\n")
        if self.capturing and tag == self.capturing:
            self.depth -= 1
            if self.depth == 0:
                caption = "\n".join(
                    s for s in (" ".join(line.split()) for line in "".join(self.text).split("\n")) if s
                )
                targets = self.found[self.figure_start :] if self.figure_start is not None else self.found[-1:]
                for item in targets:
                    item[1] = item[1] or caption
                self.capturing = None
        if tag == "figure":
            self.figure_start = None
