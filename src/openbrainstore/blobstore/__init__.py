from .base import BlobStore

_cache: dict[tuple, BlobStore] = {}


def get_blobstore() -> BlobStore:
    from .. import config

    kind = config.blob_backend()
    if kind == "fs":
        key = ("fs", str(config.blob_dir()))
        if key not in _cache:
            from .fs import FilesystemBlobStore
            _cache[key] = FilesystemBlobStore(config.blob_dir())
    elif kind == "s3":
        key = ("s3", config.s3_bucket(), config.s3_endpoint() or "")
        if key not in _cache:
            from .s3 import S3BlobStore
            _cache[key] = S3BlobStore(config.s3_bucket(), config.s3_endpoint())
    else:
        raise ValueError(f"unknown OBS_BLOB {kind!r} (expected fs or s3)")
    return _cache[key]


def reset_cache() -> None:
    """Drop cached store instances (tests swap env/mocks between cases)."""
    _cache.clear()
