class UnsupportedBookError(Exception):
    """This book cannot be processed. Subclasses say why."""


class UnsupportedFormatError(UnsupportedBookError):
    """The file type is not one the extractor can read."""


class ScannedBookError(UnsupportedBookError):
    """Pages are photographs of paper: text and artwork share the same pixels."""


class CaptionsUnavailableError(UnsupportedBookError):
    """Images can be extracted from this book, but captions cannot."""


class DrmError(UnsupportedBookError):
    """The book is encrypted."""


class NoImagesError(UnsupportedBookError):
    """The book was readable but nothing in it looked like a figure."""
