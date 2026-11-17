# granger

Pulls the figures out of illustrated books (visual encyclopedias, atlases,
histories…). Each figure can be extracted **without** its caption (text-free
artwork), **with** its caption and labels, or both.

> [!NOTE]
> This project is vibecoded: it was written with an AI coding assistant and
> has not been reviewed line by line. Expect rough edges and check the output.

No OCR and no LLM: it only works on books where the file itself keeps text and
artwork apart. Books where it can't do that are rejected with an error rather
than producing junk.

## Name

*Grangerizing* (or [extra-illustration](https://en.wikipedia.org/wiki/Extra-illustration))
is the old hobby of enlarging a book with plates and prints cut out of other
books, named after James Granger, whose 1769 biographical history of England
set off the fashion. The first half of the job is harvesting: taking the
pictures out of their source volumes. This tool does that half digitally and
without damaging the book, hence *granger*.

## Usage

```sh
nix run . -- books/BOOK...                 # or: nix build && ./result/bin/granger
nix develop                          # shell with python + deps; then: python -m granger
```

```sh
granger book.pdf                              # figures only, no text
granger --captions book.pdf                   # figures with their captions/labels
granger --both book.pdf                       # both versions side by side
granger --both -j 6 .                         # every book in a directory
granger --pages 100-140 --debug book.pdf      # a range, plus detection overlays
```

| Flag | Meaning |
|---|---|
| *(none)* | Figures without captions |
| `-c`, `--captions` | Figures with captions |
| `-b`, `--both` | Both |
| `-o DIR` | Output root (default `./out`) |
| `--pages 10-20,35` | Only these pages (position in the file, from 1) |
| `--dpi N` | PDF render resolution (default 200) |
| `--format jpg\|png`, `--quality N` | Output encoding (default JPEG 90) |
| `--overwrite` | Replace an existing output directory |
| `-j N` | Process N books in parallel |
| `--debug` | Also write `_debug/pNNNN.jpg`: the page with figures boxed in red and their captions in green |
| `--min-size`, `--merge-gap`, `--caption-gap` | Detection tuning, see `--help` |

## Output

One directory per book, named after the **title in the book's metadata**. If
the metadata has no title, the filename is used with the download-site clutter
(`(z-library…)`, `libgen.li`, `{author}`, `[series]`) stripped.

```
out/Book Title/
  manifest.json
  p0121_01.jpg        figure 1 on page 121, text removed
  p0121_01_cap.jpg    same figure, cropped wider to take in its caption and labels
  p0121_02.jpg
  ...
```

`manifest.json` lists every figure with its page, its caption text, and the
pixel box of each individual label/caption block, so caption text can be used
on its own and label boxes can be used as masks without drawing them by hand.

```json
{"id": "p0121_01", "page": 121, "page_label": "121", "whole_page": false,
 "image": "p0121_01.jpg", "captioned_image": "p0121_01_cap.jpg",
 "box": [0, 100, 753, 647], "captioned_box": [0, 100, 997, 647],
 "caption": "Head of a long-eared fox\nEnd of the wand shows signs of wear",
 "texts": [{"text": "Head of a long-eared fox", "box": [678, 361, 813, 413]},
           {"text": "End of the wand shows signs of wear", "box": [805, 528, 991, 581]}]}
```

Boxes are in page pixels. Subtract the first two numbers of `captioned_box`
from a text box to get its position inside `*_cap.jpg`.

An existing output directory is never silently overwritten; two files with the
same metadata title (e.g. two copies of one book) collide and the second is
reported as an error unless `--overwrite` is given.

## What works and what doesn't

The file extension matters less than how the book was produced.

| Format | Result |
|---|---|
| PDF, born-digital | works; the best case |
| PDF, two pages per sheet | works |
| EPUB, reflowable | works (cleanest) |
| AZW3 / KF8, fixed layout | works |
| EPUB, fixed layout with HTML text | images only; `--captions` is an error |
| PDF, scan + OCR layer | **error** |
| PDF, scan, no text | **error** |
| DjVu | **error** |
| `.txt`, anything else | **error** |

Many books are laid out with full-bleed pages, which come out as whole-page
images (see below). Processing is CPU-heavy and output can be large: use
`--pages` or a lower `--dpi` to cut it down.

### By file type

**PDF, born-digital — works.** Exported from InDesign: real text drawn over
artwork. The tool renders each page twice, once as printed and once with every
text object deleted, finds the figures on the text-free render, and crops the
captioned version from the normal one. Leader lines and rules are vector
graphics, not text, so they remain in both versions.

**PDF, scanned — error (`ScannedBookError`).** Every page is one photograph of
paper; captions are pixels inside the picture. Detected when most sampled pages
are a page-sized image with no *visible* text. An invisible OCR layer (as in many Internet
Archive scans) does not help: the text is still
baked into the image.

**EPUB, reflowable — works.** Each picture is its own file with a
`<figcaption>`. The plain version is the book's original image file copied
byte for byte; the captioned version has the caption typeset on a white strip
underneath. Ornaments (tiny or repeated images) are skipped.

**EPUB, fixed layout with HTML text — images only.** Each page is one
text-free background image with the text positioned over it in HTML. Figures
are detected on the background, so the no-caption output works well. Placing
that text needs a browser engine, which this tool does not carry, so
`--captions` and `--both` fail with `CaptionsUnavailableError`. Low-resolution page images give correspondingly small figures.

**AZW3 / KF8 — works.** Unpacked to EPUB first. A typical one is a fixed
layout made from a PDF: text is baked into the page images, but an invisible
overlay gives the position of every word. Words on a flat background are
painted out for the no-caption version; words printed over a photograph stay.
Old `.mobi` without a KF8 section is an error. DRM-protected Kindle and EPUB
files are an error (`DrmError`).

**DjVu — error (`UnsupportedFormatError`).** DjVu splits a page into a low-resolution background and a foreground mask, and
the mask holds the text and the line artwork together. Neither layer is a
text-free picture.

**EPUB of bare page images, CBZ/CBR, TXT, everything else — error.**

When several books are given, the others still run; failures are listed and
the exit status is 1. From Python, `granger.extract_book()` raises
`UnsupportedBookError` (or one of the subclasses named above).

## How figures are found, and where it goes wrong

On a text-free page, the most common colour is taken as the background.
Anything else that forms a connected patch with photographic detail is a
figure; flat tinted panels, rules and frames are discarded. A text block
belongs to a figure if it is printed over it, or sits close by and looks like
a caption (smaller than the body text) or a label (short).

This is a heuristic. Use `--debug` on a few pages to see what it decided.

- **Full-bleed pages come out as one whole-page image.** If artwork or a
  textured background covers most of the page there is no background to
  separate figures from. In some books that is most pages; in others, a minority. These are marked `"whole_page": true` in the manifest and their
  caption is all the text on the page.
- **Neighbours can merge or split.** Photos nearly touching become one figure;
  a cut-out object in two separated pieces may become two. `--merge-gap`
  adjusts this.
- **Captions are a best guess.** A short body paragraph next to a picture can
  be taken as a caption, and a label joined to its object by a long leader
  line can be missed (`--caption-gap` widens the search).
- **Empty boxes.** A decorated text panel with enough texture can pass as a
  figure once its text is removed.
- Scan detection samples 24 pages. A born-digital photo book with almost no
  text would be misread as a scan.

## Dependencies

All in `flake.nix`: Python 3 with PyMuPDF (PDF), Pillow, NumPy and SciPy
(figure detection), `mobi` (Kindle unpacking), and DejaVu Sans for typeset
captions. Book files and `out/` are git-ignored, which also keeps Nix from
copying them into the store.
