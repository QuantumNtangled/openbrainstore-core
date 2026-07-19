"""Filesystem blob store — S3-style keys mapped onto a local directory tree.
The default for local use; byte-for-byte the same canonical files an S3
bucket would hold."""

import shutil
from pathlib import Path

from .base import BlobStore


class FilesystemBlobStore(BlobStore):
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key

    def put_text(self, key: str, text: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def get_text(self, key: str) -> str:
        return self._path(key).read_text(encoding="utf-8")

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.exists():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob("*")
            if p.is_file()
        )

    def delete(self, key: str) -> None:
        p = self._path(key)
        p.unlink(missing_ok=True)
        # prune now-empty parent dirs so tombstoned memories don't leave husks
        parent = p.parent
        while parent != self.root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    def copy(self, src_key: str, dst_key: str) -> None:
        dst = self._path(dst_key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(src_key), dst)
