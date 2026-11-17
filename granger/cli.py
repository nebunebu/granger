from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .errors import UnsupportedBookError
from .extract import BOOK_TYPES, Options, extract_book, stderr_progress
from .segment import Params


def parse_pages(text: str) -> list[int]:
    pages: list[int] = []
    for part in text.split(","):
        first, _, last = part.strip().partition("-")
        try:
            a = int(first)
            b = int(last) if last else a
        except ValueError:
            raise argparse.ArgumentTypeError(f"bad page range: {part!r} (use e.g. 10-20,35)")
        if a < 1 or b < a:
            raise argparse.ArgumentTypeError(f"bad page range: {part!r}")
        pages.extend(range(a, b + 1))
    return pages


def build_parser() -> argparse.ArgumentParser:
    d = Params()
    p = argparse.ArgumentParser(
        prog="granger",
        description="Extract figures from books into <out>/<book title>/.",
    )
    p.add_argument("books", nargs="+", type=Path, help="book files, or directories containing them")
    p.add_argument("-o", "--out", type=Path, default=Path("out"), help="output root (default: ./out)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("-c", "--captions", action="store_const", const="with", dest="captions",
                      help="extract each figure together with its caption and labels")
    mode.add_argument("-b", "--both", action="store_const", const="both", dest="captions",
                      help="write both versions: NAME.jpg (no text) and NAME_cap.jpg (with caption)")
    p.set_defaults(captions="without")
    p.add_argument("--pages", type=parse_pages, metavar="RANGES",
                   help="only these pages, e.g. 10-20,35 (position in the file, starting at 1)")
    p.add_argument("--dpi", type=int, default=200, help="render resolution for PDFs (default: 200)")
    p.add_argument("--format", choices=["jpg", "png"], default="jpg", help="output image format (default: jpg)")
    p.add_argument("--quality", type=int, default=90, help="JPEG quality (default: 90)")
    p.add_argument("--overwrite", action="store_true", help="replace an existing output directory")
    p.add_argument("-j", "--jobs", type=int, default=1, help="books to process in parallel (default: 1)")
    p.add_argument("--debug", action="store_true",
                   help="also write _debug/pNNNN.jpg: each page with detected figures (red) and captions (green)")
    t = p.add_argument_group("detection tuning (fractions of the page width/height)")
    t.add_argument("--min-size", type=float, default=d.min_size, metavar="F",
                   help=f"ignore figures smaller than F x page width (default: {d.min_size})")
    t.add_argument("--merge-gap", type=float, default=d.merge_gap, metavar="F",
                   help=f"treat fragments closer than F x page width as one figure (default: {d.merge_gap})")
    t.add_argument("--caption-gap", type=float, default=d.caption_gap, metavar="F",
                   help=f"a caption lies within F x page height of its figure (default: {d.caption_gap})")
    return p


def _run(path: Path, opts: Options, quiet: bool):
    try:
        result = extract_book(path, opts, None if quiet else stderr_progress(path.name))
        return path, result, None
    except (UnsupportedBookError, FileExistsError) as exc:
        return path, None, exc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    files: list[Path] = []
    for entry in args.books:
        if entry.is_dir():
            files += sorted(f for f in entry.iterdir() if f.is_file() and f.suffix.lower() in BOOK_TYPES)
        else:
            files.append(entry)
    if not files:
        print("error: no book files found", file=sys.stderr)
        return 2
    opts = Options(
        out=args.out, captions=args.captions, dpi=args.dpi, format=args.format, quality=args.quality,
        pages=args.pages, overwrite=args.overwrite, debug=args.debug,
        params=Params(min_size=args.min_size, merge_gap=args.merge_gap, caption_gap=args.caption_gap),
    )
    if args.jobs > 1 and len(files) > 1:
        with ProcessPoolExecutor(args.jobs) as pool:
            outcomes = list(pool.map(_run, files, [opts] * len(files), [True] * len(files)))
    else:
        outcomes = []
        for f in files:
            outcomes.append(_run(f, opts, False))
            _report(outcomes[-1])
    if args.jobs > 1 and len(files) > 1:
        for outcome in outcomes:
            _report(outcome)
    failed = sum(1 for _, _, exc in outcomes if exc)
    if len(files) > 1:
        print(f"{len(files) - failed} of {len(files)} books extracted, {failed} failed", file=sys.stderr)
    return 1 if failed else 0


def _report(outcome) -> None:
    path, result, exc = outcome
    if sys.stderr.isatty():
        print("\r\033[K", end="", file=sys.stderr)
    if exc:
        print(f"error: {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    else:
        print(f"{result.directory}: {result.figures} figures ({result.captioned} with caption text) "
              f"from {result.pages} pages [{result.kind}]")
