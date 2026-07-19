"""Vector escape hatch — model side (optional, requires the [vector] extra).
Storage lives in the backends; this module owns embedding computation.
Degrades gracefully: if fastembed isn't installed, the vector lane never fires.

Local deviation from the cloud spec: the cloud plan embeds only consolidated
memories (a cost lever). Locally the corpus is tiny and CPU is free, so all
memories are eligible, embedded lazily when the lane first fires."""

from . import config
from .backends.base import Backend

_model = None


def _installed() -> bool:
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def _model_cached() -> bool:
    """True if the ONNX model files are already on disk. We never download
    inside a recall/remember call — a synchronous multi-minute model download
    inside an interactive tool call is a hang, not a feature."""
    d = config.model_dir()
    return d.exists() and any(d.rglob("*.onnx"))


def available() -> bool:
    """The vector lane may fire only if the library is installed AND the model
    is already cached locally. Download happens explicitly via download_model()
    (`obs embed`)."""
    return _installed() and (_model is not None or _model_cached())


def download_model() -> str:
    """Explicitly fetch the embedding model (used by `obs embed`). This is the
    only code path allowed to download."""
    if not _installed():
        raise RuntimeError("vector extra not installed: pip install -e .[vector]")
    _get_model(allow_download=True)
    return str(config.model_dir())


def _get_model(allow_download: bool = False):
    global _model
    if _model is None:
        if not allow_download and not _model_cached():
            raise RuntimeError(
                "embedding model not cached; run `obs embed` once to download it"
            )
        from fastembed import TextEmbedding
        config.model_dir().mkdir(parents=True, exist_ok=True)
        _model = TextEmbedding(
            model_name=config.EMBED_MODEL, cache_dir=str(config.model_dir())
        )
    return _model


def _embed(texts: list[str]) -> list[list[float]]:
    import numpy as np
    vecs = np.array(list(_get_model().embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).tolist()


def ensure_embedded(backend: Backend, user: str) -> int:
    if not available():
        return 0
    backend.set_acting_user(user)
    pending = backend.pending_embeddings(user)
    if not pending:
        return 0
    vecs = _embed([body for _, body in pending])
    for (mem_id, _), vec in zip(pending, vecs):
        backend.store_embedding(mem_id, vec)
    return len(pending)


def search(backend: Backend, user: str, query: str, k: int = 20) -> list[str]:
    if not available():
        return []
    ensure_embedded(backend, user)
    return backend.vector_search(user, _embed([query])[0], k)
