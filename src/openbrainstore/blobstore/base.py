"""Blob store contract — the spec's 'abstraction is production-shaped from
day one' principle. Keys are S3-style (forward-slash, no leading slash);
the filesystem implementation maps them onto a local directory tree."""

from abc import ABC, abstractmethod


class BlobStore(ABC):
    @abstractmethod
    def put_text(self, key: str, text: str) -> None: ...

    @abstractmethod
    def get_text(self, key: str) -> str: ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """All keys under prefix, sorted lexicographically."""

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def copy(self, src_key: str, dst_key: str) -> None: ...
