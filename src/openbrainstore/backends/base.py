"""Backend contract. Every projection lane lives behind this interface;
recall.py owns the cascade + fusion, backends own the storage-native SQL.
All projections are disposable — rebuildable from canonical blobs."""

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..canonical import Memory


class Backend(ABC):
    def set_acting_user(self, user: str) -> None:
        """Declare the tenant this connection is acting for. Postgres enforces
        it with row-level security (fail-closed: no acting user means no rows);
        single-user local backends may no-op. Callers set it at the top of
        every operation — see docs/specs/tenant-isolation.md."""
        return None

    # ---- write path ----
    @abstractmethod
    def project_memory(self, mem: Memory) -> None: ...

    @abstractmethod
    def remove_memory(self, mem_id: str) -> None: ...

    @abstractmethod
    def emit_event(self, topic: str, payload: dict) -> None: ...

    @abstractmethod
    def clear_user(self, user: str) -> None:
        """Drop every projection for a user (reindex support)."""

    # ---- retrieval lanes ----
    @abstractmethod
    def lane_structured(self, user: str, filters: dict, entities: list[str]) -> list[str]: ...

    @abstractmethod
    def lane_fts(self, user: str, query: str, k: int = 50) -> list[str]: ...

    @abstractmethod
    def graph_expand(
        self, user: str, entities: list[str], seed_ids: list[str], depth: int
    ) -> list[str]: ...

    # ---- vector lane storage (embedding computation lives in embeddings.py) ----
    @abstractmethod
    def pending_embeddings(self, user: str) -> list[tuple[str, str]]:
        """(id, body) rows that still need an embedding."""

    @abstractmethod
    def store_embedding(self, mem_id: str, vec: list[float]) -> None: ...

    @abstractmethod
    def vector_search(self, user: str, qvec: list[float], k: int = 20) -> list[str]: ...

    # ---- reads ----
    @abstractmethod
    def get_memories(self, ids: list[str]) -> dict[str, dict]:
        """id -> {id, type, body, meta, created} for existing ids."""

    @abstractmethod
    def iter_meta(self, user: str) -> Iterator[tuple[str, dict]]:
        """(type, meta) for every memory of the user."""

    @abstractmethod
    def count_memories(self, user: str) -> int:
        """Cheap count for the per-tenant quota check."""

    # ---- instrumentation ----
    @abstractmethod
    def log_recall(
        self, lanes: dict[str, int], result_count: int, vector_fired: bool, duration_ms: float
    ) -> None: ...

    @abstractmethod
    def stats_summary(self, user: str) -> dict: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "Backend":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
