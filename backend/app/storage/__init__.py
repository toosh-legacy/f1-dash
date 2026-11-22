"""Durable storage that does not depend on the local disk surviving.

See :mod:`app.storage.files` for the file store: local files are a cache, the
database is the record.
"""
from app.storage import files

__all__ = ["files"]
