class SakError(Exception):
    """Base error with a user-facing message."""


class KeyError(SakError):
    pass


class FormatError(SakError):
    pass


class UnsupportedFormat(FormatError):
    pass


class BundleError(SakError):
    pass
