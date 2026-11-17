"""Find figures on a page of text-free artwork and pair them with captions.

Illustrated pages are a background (usually white) with photographs and cut-out
objects scattered over it. Once the text is gone, a figure is simply a
connected patch of pixels that is not background and has photographic detail.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

from .model import Box, Figure, TextBlock


@dataclass
class Params:
    analysis_px: int = 600  # pages are shrunk to this long side for analysis
    tolerance: int = 22  # colour distance from the background that counts as ink
    merge_gap: float = 0.010  # fragments closer than this (x page width) are one figure
    min_size: float = 0.06  # figures narrower/shorter than this (x page width) are dropped
    min_detail: float = 0.03  # share of pixels on an edge; below this it is a flat panel
    max_text_cover: float = 0.40  # a patch mostly covered by text is a text box
    whole_page: float = 0.60  # a figure covering this much of the page is the page
    caption_gap: float = 0.03  # captions sit within this (x page height) of a figure
    caption_chars: int = 600  # longer blocks are body text, not captions
    label_chars: int = 160  # limit for blocks set in the body font


def detect_figures(clean: Image.Image, blocks: list[TextBlock], p: Params) -> list[Figure]:
    width, height = clean.size
    scale = min(1.0, p.analysis_px / max(width, height))
    sw, sh = max(1, round(width * scale)), max(1, round(height * scale))
    a = np.asarray(clean.convert("RGB").resize((sw, sh), Image.BILINEAR), dtype=np.int16)

    # Background = the single most common colour on the page.
    q = a >> 3
    key = (q[..., 0] << 10) | (q[..., 1] << 5) | q[..., 2]
    values, counts = np.unique(key, return_counts=True)
    background = a[key == values[counts.argmax()]].mean(axis=0)
    ink = np.abs(a - background).max(axis=2) > p.tolerance

    # Opening removes leader lines, rules and page frames. Components are
    # seeded from a strong opening (so a frame cannot chain figures together),
    # joined across small gaps (a cut-out in two parts, a photo and its drop
    # shadow), then measured on a gentler opening that keeps thin details.
    solid = ndi.binary_opening(ink, structure=np.ones((3, 3), bool))
    core = ndi.binary_opening(ink, structure=np.ones((5, 5), bool))
    reach = max(1, round(p.merge_gap * sw / 2))
    labels, count = ndi.label(ndi.binary_dilation(core, iterations=reach))
    if not count:
        return []
    labels = labels * solid

    edges = np.zeros((sh, sw), bool)
    edges[:, 1:] |= np.abs(a[:, 1:] - a[:, :-1]).sum(axis=2) > 40
    edges[1:, :] |= np.abs(a[1:, :] - a[:-1, :]).sum(axis=2) > 40

    text = np.zeros((sh, sw), bool)
    for b in blocks:
        x0, y0, x1, y1 = (round(v * scale) for v in b.box)
        text[max(0, y0) : y1, max(0, x0) : x1] = True

    min_side = p.min_size * sw
    boxes: list[Box] = []
    for i, sl in enumerate(ndi.find_objects(labels), start=1):
        if sl is None:
            continue
        ys, xs = sl
        w, h = xs.stop - xs.start, ys.stop - ys.start
        if w < min_side or h < min_side:
            continue
        member = labels[sl] == i
        if edges[sl][member].mean() < p.min_detail:
            continue  # flat tint: sidebar, text panel, rule
        covers_page = w * h >= p.whole_page * sw * sh
        if not covers_page and text[sl].mean() > p.max_text_cover:
            continue  # a decorated box whose content is text
        boxes.append((xs.start, ys.start, xs.stop, ys.stop))

    boxes = _merge_overlapping(boxes)
    if any(_area(b) >= p.whole_page * sw * sh for b in boxes):
        return [Figure((0, 0, width, height), whole_page=True)]

    pad = 2
    figures = []
    for x0, y0, x1, y1 in sorted(boxes, key=lambda b: (b[1] // max(1, sh // 12), b[0])):
        figures.append(
            Figure(
                (
                    max(0, int(x0 / scale) - pad),
                    max(0, int(y0 / scale) - pad),
                    min(width, int(np.ceil(x1 / scale)) + pad),
                    min(height, int(np.ceil(y1 / scale)) + pad),
                )
            )
        )
    return figures


def assign_captions(figures: list[Figure], blocks: list[TextBlock], size: tuple[int, int], p: Params) -> None:
    """Attach each caption-like text block to the figure it sits in or beside."""
    if not figures or not blocks:
        return
    width, height = size
    body = _body_size(blocks)
    max_gap = p.caption_gap * height
    for b in blocks:
        cx, cy = (b.box[0] + b.box[2]) / 2, (b.box[1] + b.box[3]) / 2
        if cy < 0.035 * height or cy > 0.965 * height:
            continue  # running head, folio
        best, best_gap = None, None
        for f in figures:
            inside = f.box[0] <= cx <= f.box[2] and f.box[1] <= cy <= f.box[3]
            gap = -1.0 if inside else _gap(f.box, b.box)
            if best_gap is None or gap < best_gap:
                best, best_gap = f, gap
        n = len(b.text)
        if best.whole_page:
            ok = True
        elif best_gap <= max_gap:
            # Captions are set smaller than body text; labels are short. A
            # long block in the body font is a paragraph, even when it wraps
            # around a cut-out and so falls inside the figure's box.
            smaller = b.size < body * 0.95 and n <= p.caption_chars
            short = (best_gap < 0 or b.size <= body * 1.1) and n <= p.label_chars
            ok = smaller or short
        else:
            ok = False
        if ok:
            best.blocks.append(b)
    for f in figures:
        f.blocks.sort(key=lambda b: (b.box[1], b.box[0]))


def captioned_box(figure: Figure, size: tuple[int, int], pad: int = 6) -> Box:
    """The figure's box grown to take in its captions."""
    x0, y0, x1, y1 = figure.box
    for b in figure.blocks:
        x0, y0 = min(x0, b.box[0] - pad), min(y0, b.box[1] - pad)
        x1, y1 = max(x1, b.box[2] + pad), max(y1, b.box[3] + pad)
    return max(0, x0), max(0, y0), min(size[0], x1), min(size[1], y1)


def group_lines(lines: list[TextBlock]) -> list[TextBlock]:
    """Join single lines into blocks (for formats that only give us lines)."""
    blocks: list[TextBlock] = []
    for line in sorted(lines, key=lambda b: (b.box[1], b.box[0])):
        lh = line.box[3] - line.box[1]
        for b in reversed(blocks[-40:]):
            overlap = min(b.box[2], line.box[2]) - max(b.box[0], line.box[0])
            narrower = min(b.box[2] - b.box[0], line.box[2] - line.box[0])
            vgap = line.box[1] - b.box[3]
            same_size = abs(b.size - line.size) <= 0.12 * max(b.size, line.size)
            if same_size and -0.5 * lh <= vgap <= 0.7 * lh and overlap >= 0.5 * narrower:
                b.box = (
                    min(b.box[0], line.box[0]),
                    b.box[1],
                    max(b.box[2], line.box[2]),
                    max(b.box[3], line.box[3]),
                )
                b.text = join_lines([b.text, line.text])
                b.lines += 1
                break
        else:
            blocks.append(TextBlock(line.box, line.text, line.size))
    return blocks


def join_lines(lines: list[str]) -> str:
    out = ""
    for line in (s.strip() for s in lines):
        if not line:
            continue
        if out.endswith("-") and len(out) > 1 and out[-2].isalpha() and line[0].islower():
            out = out[:-1] + line  # undo end-of-line hyphenation
        else:
            out = f"{out} {line}" if out else line
    return out


def erase_text(image: Image.Image, boxes: list[Box]) -> Image.Image:
    """Paint over words that sit on a flat background (for pages with baked-in text).

    Words printed over a photograph are left alone: there is nothing to
    restore them to.
    """
    a = np.array(image.convert("RGB"))
    h, w = a.shape[:2]
    boxes = [(max(0, x0 - 2), max(0, y0 - 2), min(w, x1 + 2), min(h, y1 + 2)) for x0, y0, x1, y1 in boxes]
    is_text = np.zeros((h, w), bool)
    for x0, y0, x1, y1 in boxes:
        is_text[y0:y1, x0:x1] = True
    for x0, y0, x1, y1 in boxes:
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        # The background is judged from a ring around the word, skipping
        # whatever part of the ring is itself covered by neighbouring words.
        ox0, oy0, ox1, oy1 = max(0, x0 - 4), max(0, y0 - 4), min(w, x1 + 4), min(h, y1 + 4)
        ring = a[oy0:oy1, ox0:ox1][~is_text[oy0:oy1, ox0:ox1]]
        if len(ring) >= 8 and ring.std(axis=0).max() < 10:
            a[y0:y1, x0:x1] = np.median(ring, axis=0)
    return Image.fromarray(a)


def _body_size(blocks: list[TextBlock]) -> float:
    weight: dict[float, int] = {}
    for b in blocks:
        key = round(b.size * 2) / 2
        weight[key] = weight.get(key, 0) + len(b.text)
    return max(weight, key=weight.get)


def _area(b: Box) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _gap(a: Box, b: Box) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0)
    dy = max(a[1] - b[3], b[1] - a[3], 0)
    return float(np.hypot(dx, dy))


def _merge_overlapping(boxes: list[Box]) -> list[Box]:
    boxes = list(boxes)
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                inter = _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))
                if inter > 0.3 * min(_area(a), _area(b)):
                    boxes[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return boxes
