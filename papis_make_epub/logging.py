"""Loggers under the papis namespace.

papis attaches its output handler to the ``papis`` logger only (see
``papis.logging.setup``), so lines logged outside that namespace stay
invisible under a papis run.
"""

from papis.logging import get_logger

__all__ = ["get_logger"]
