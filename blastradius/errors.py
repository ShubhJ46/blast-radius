class BlastRadiusError(Exception):
    """Base class for errors this tool raises deliberately."""


class ParseError(BlastRadiusError):
    """A source file could not be parsed.

    Raised rather than swallowed: the caller indexing a whole tree decides
    whether one unparseable file is fatal or merely skipped, and a silently
    skipped file would understate impact without saying so.
    """
