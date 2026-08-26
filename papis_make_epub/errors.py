"""The base of the failures a build reports by their message."""


class BuildError(Exception):
    """A failure the build reports by its message: the page's or the
    renderer's. Any other exception is a bug."""
