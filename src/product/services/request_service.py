"""Local-first request store for the early product layer."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

try:
    from core.state import atomic_read, atomic_write
    from product.models.request import RequestRecord
except ImportError:  # pragma: no cover - package import path under pytest
    from src.core.state import atomic_read, atomic_write
    from src.product.models.request import RequestRecord

logger = logging.getLogger(__name__)

_ALL_STATUSES = ("new", "triaged", "planned", "running", "completed", "blocked")
_CACHE_TTL = 5.0  # seconds

# Module-level TTL cache: (cached_data, cache_timestamp)
# cached_data is the raw list of request records (or None on failure)
_cache: tuple[list | None, float] = (None, 0.0)


def _invalidate_cache() -> None:
    global _cache
    _cache = (None, 0.0)


class RequestService:
    """Persist minimal request objects under `.harness/product/requests.json`."""

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.product_dir = self.project_dir / ".harness" / "product"
        self.product_dir.mkdir(parents=True, exist_ok=True)
        self.requests_path = self.product_dir / "requests.json"

    def list_requests(self) -> list[RequestRecord]:
        data = atomic_read(self.requests_path)
        if data is None:
            return []
        return data.get("requests", [])

    def get_request(self, request_id: str) -> Optional[RequestRecord]:
        for record in self.list_requests():
            if record.get("id") == request_id:
                return record
        return None

    def create_request(
        self,
        *,
        project_id: str,
        title: str,
        description: str,
        source: str = "manual",
        priority: str = "normal",
        acceptance_criteria: Optional[list[str]] = None,
        linked_paths: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> RequestRecord:
        record = RequestRecord(
            id=f"req-{uuid4().hex[:10]}",
            project_id=project_id,
            title=title,
            description=description,
            source=source,
            status="new",
            priority=priority,
            acceptance_criteria=acceptance_criteria or [],
            linked_paths=linked_paths or [],
            metadata=metadata or {},
        )
        requests = self.list_requests()
        requests.append(record)
        atomic_write(self.requests_path, {"requests": requests})
        _invalidate_cache()
        return record

    def update_request(self, request_id: str, **updates) -> RequestRecord:
        requests = self.list_requests()
        for record in requests:
            if record.get("id") == request_id:
                record.update(updates)
                atomic_write(self.requests_path, {"requests": requests})
                _invalidate_cache()
                return record
        raise ValueError(f"Request not found: {request_id}")

    # ------------------------------------------------------------------
    # Aggregate counts
    # ------------------------------------------------------------------

    def _load_requests_cached(self) -> list:
        """Return request records from a 5-second TTL module-level cache.

        On any I/O or parse error, emits a WARNING and returns an empty list.
        """
        global _cache
        cached_data, cached_at = _cache
        if cached_at > 0 and (time.monotonic() - cached_at) < _CACHE_TTL:
            return cached_data if cached_data is not None else []

        try:
            data = atomic_read(self.requests_path)
        except Exception as exc:
            logger.warning(
                "Failed to read requests file %s (%s: %s)",
                self.requests_path,
                type(exc).__name__,
                exc,
            )
            _cache = (None, time.monotonic())
            return []

        if data is None:
            # File does not exist — log warning, cache empty result
            logger.warning(
                "Requests file not found: %s — returning empty counts",
                self.requests_path,
            )
            _cache = ([], time.monotonic())
            return []

        if not isinstance(data, dict):
            logger.warning(
                "Corrupt requests file %s (%s: unexpected top-level type %s)",
                self.requests_path,
                "TypeError",
                type(data).__name__,
            )
            _cache = ([], time.monotonic())
            return []

        records = data.get("requests", [])
        _cache = (records, time.monotonic())
        return records

    def count_requests(self) -> int:
        """Return total number of requests in the store."""
        return len(self._load_requests_cached())

    def count_by_status(self) -> dict[str, int]:
        """Return a count of requests grouped by status.

        Always returns all 6 RequestStatus keys, initialised to 0 for any
        status that has no records.
        """
        counts: dict[str, int] = {s: 0 for s in _ALL_STATUSES}
        for record in self._load_requests_cached():
            status = record.get("status")
            if status in counts:
                counts[status] += 1
        return counts
