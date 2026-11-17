"""Extract figures, with or without their captions, from books."""

from .errors import (
    CaptionsUnavailableError,
    DrmError,
    NoImagesError,
    ScannedBookError,
    UnsupportedBookError,
    UnsupportedFormatError,
)
from .extract import extract_book

__all__ = [
    "extract_book",
    "UnsupportedBookError",
    "UnsupportedFormatError",
    "ScannedBookError",
    "CaptionsUnavailableError",
    "DrmError",
    "NoImagesError",
]
