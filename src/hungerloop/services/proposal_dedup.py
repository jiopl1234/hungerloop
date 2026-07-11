"""Persistent deduplication helpers for rejected check proposals."""
from __future__ import annotations

from hungerloop.models.events import EventType
from hungerloop.repository.protocol import RepositoryProtocol

_DEDUP_KEY_PREFIXES = ("shell_exit_zero:", "file_exists:")


def collect_rejected_proposal_dedup_keys(
    repo: RepositoryProtocol,
    task_id: str,
) -> set[str]:
    """Return behavior keys rejected in any prior synthesis batch."""
    keys: set[str] = set()
    events = repo.list_events(
        task_id,
        event_types=[EventType.SYNTH_CHECK_REJECTED.value],
    )
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        _add_key(keys, payload.get("dedup_key"))
        dedup_keys = payload.get("dedup_keys")
        if isinstance(dedup_keys, list):
            for dedup_key in dedup_keys:
                _add_key(keys, dedup_key)
        details = payload.get("rejected_proposals")
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict):
                    _add_key(keys, detail.get("dedup_key"))
    return keys


def _add_key(keys: set[str], value: object) -> None:
    if isinstance(value, str) and value.startswith(_DEDUP_KEY_PREFIXES):
        keys.add(value)
