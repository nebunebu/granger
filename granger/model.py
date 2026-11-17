from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in page pixels


@dataclass
class TextBlock:
    """A run of text on a page: a caption, a label, a paragraph."""

    box: Box
    text: str
    size: float  # font size in page pixels
    lines: int = 1


@dataclass
class Page:
    """One page of a book whose layout is fixed (PDF, fixed-layout EPUB/AZW3)."""

    index: int  # 1-based position in the file
    label: str  # printed page number where the file knows it
    clean: Image.Image  # artwork with the text removed
    full: Image.Image | None  # the page as printed; None if it cannot be rendered
    blocks: list[TextBlock] = field(default_factory=list)


@dataclass
class FlowImage:
    """An image in a reflowable book, where figure and caption are explicit."""

    index: int  # 1-based spine position
    label: str
    name: str  # file name inside the book
    data: bytes
    caption: str


@dataclass
class Figure:
    box: Box
    whole_page: bool = False
    blocks: list[TextBlock] = field(default_factory=list)
