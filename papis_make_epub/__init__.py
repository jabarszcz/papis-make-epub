from importlib.metadata import PackageNotFoundError, version

# derived from the installed metadata so that pyproject's [project]
# version stays the only declared version
try:
    __version__: str = version("papis-make-epub")
except PackageNotFoundError:  # running from the source tree, uninstalled
    __version__ = "0.0.0.dev0"

from papis_make_epub.api import (
    arxiv_builder,
    author_names,
    build_epub,
    doc_meta,
    epub_name,
    existing_epub,
    matching_sources,
    page_builder,
    renderer_resolver,
    saved_pages,
)

__all__ = [
    "arxiv_builder",
    "author_names",
    "build_epub",
    "doc_meta",
    "epub_name",
    "existing_epub",
    "matching_sources",
    "page_builder",
    "renderer_resolver",
    "saved_pages",
]
