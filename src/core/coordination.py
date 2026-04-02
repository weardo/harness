"""
Coordination -- File-based scope claims and instance registry for parallel agents.

Ported from Citadel's JavaScript coordination layer.
Each agent instance registers itself, claims a scope (set of file paths),
and a sweep process reaps stale/dead instances to free their scopes.

All JSON writes use atomic_write from state.py for crash safety.
"""

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .state import atomic_write, atomic_read

STALE_INSTANCE_HOURS = 2.0


class ScopeOverlapError(Exception):
    """Raised when a scope claim conflicts with an existing claim."""

    def __init__(self, overlap: dict):
        self.overlap = overlap
        super().__init__(
            f"Scope overlap with {overlap.get('instanceId')}: "
            f"{overlap.get('scope')}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_instance_id() -> str:
    """Generate a random agent instance ID like 'agent-a1b2c3d4'."""
    return f"agent-{secrets.token_hex(4)}"


def _coordination_paths(state_dir: Path) -> dict:
    """Return {instances_dir, claims_dir} under .harness/coordination/.

    coordination dir is at .harness/coordination/ (sibling of .harness/state/).
    state_dir is typically  <root>/.harness/state/
    so we go two levels up to reach .harness/ then into coordination/.
    """
    state_dir = Path(state_dir)
    coordination_dir = state_dir.parent / "coordination"
    return {
        "instances_dir": coordination_dir / "instances",
        "claims_dir": coordination_dir / "claims",
    }


def _normalize_scope_entry(entry: str) -> str:
    """Strip '(read-only)' suffix and whitespace."""
    return str(entry or "").replace("(read-only)", "").strip()


def _is_read_only(entry: str) -> bool:
    """Check if scope entry ends with '(read-only)'."""
    return str(entry or "").strip().endswith("(read-only)")


def _is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _remove_file_if_exists(path: Path) -> None:
    """Remove a file if it exists, ignoring errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Scope overlap logic
# ---------------------------------------------------------------------------

def scopes_overlap(scope_a: list[str], scope_b: list[str]) -> bool:
    """True if any non-read-only entry in A is a prefix of any in B, or vice versa.

    Parent/child dirs overlap (src/api/ and src/api/auth/).
    Siblings don't (src/api/ and src/ui/).
    Read-only entries never conflict.
    """
    for left in scope_a:
        if _is_read_only(left):
            continue
        clean_left = _normalize_scope_entry(left)

        for right in scope_b:
            if _is_read_only(right):
                continue
            clean_right = _normalize_scope_entry(right)
            if clean_left.startswith(clean_right) or clean_right.startswith(clean_left):
                return True

    return False


# ---------------------------------------------------------------------------
# Instance registry
# ---------------------------------------------------------------------------

def register_instance(
    instance_id: str,
    feature_id: str,
    pid: int,
    wave: int,
    state_dir: Path,
) -> dict:
    """Write instance record to .harness/coordination/instances/{id}.json."""
    paths = _coordination_paths(state_dir)
    instances_dir = paths["instances_dir"]
    instances_dir.mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    data = {
        "instanceId": instance_id,
        "featureId": feature_id,
        "pid": pid,
        "wave": wave,
        "startedAt": now,
        "lastSeen": now,
        "status": "active",
    }
    atomic_write(instances_dir / f"{instance_id}.json", data)
    return data


def unregister_instance(instance_id: str, state_dir: Path) -> None:
    """Remove BOTH instance file AND claim file atomically (Citadel pattern).

    Avoids orphaned claims when an agent crashes or shuts down.
    """
    paths = _coordination_paths(state_dir)
    _remove_file_if_exists(paths["instances_dir"] / f"{instance_id}.json")
    _remove_file_if_exists(paths["claims_dir"] / f"{instance_id}.json")


def heartbeat_instance(instance_id: str, state_dir: Path) -> dict:
    """Update lastSeen timestamp on instance record."""
    paths = _coordination_paths(state_dir)
    instance_file = paths["instances_dir"] / f"{instance_id}.json"
    data = atomic_read(instance_file)

    if data is None:
        raise FileNotFoundError(f"Instance not found: {instance_id}")

    data["lastSeen"] = _now_iso()
    atomic_write(instance_file, data)
    return data


def list_instances(state_dir: Path) -> list[dict]:
    """List all active instances."""
    paths = _coordination_paths(state_dir)
    instances_dir = paths["instances_dir"]
    instances_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for entry in sorted(instances_dir.iterdir()):
        if entry.suffix == ".json" and not entry.name.startswith("."):
            data = atomic_read(entry)
            if data is not None:
                results.append(data)
    return results


# ---------------------------------------------------------------------------
# Scope claims
# ---------------------------------------------------------------------------

def claim_scope(
    instance_id: str,
    scope: list[str],
    feature_id: str,
    state_dir: Path,
) -> dict:
    """Claim scope. Raises ScopeOverlapError if conflict detected."""
    paths = _coordination_paths(state_dir)
    claims_dir = paths["claims_dir"]
    claims_dir.mkdir(parents=True, exist_ok=True)

    # Check for overlapping claims from other instances
    for existing in list_claims(state_dir):
        if existing["instanceId"] == instance_id:
            continue
        if scopes_overlap(scope, existing.get("scope", [])):
            raise ScopeOverlapError(existing)

    now = _now_iso()
    data = {
        "instanceId": instance_id,
        "featureId": feature_id,
        "scope": scope,
        "claimedAt": now,
    }
    atomic_write(claims_dir / f"{instance_id}.json", data)
    return data


def release_claim(instance_id: str, state_dir: Path) -> Optional[dict]:
    """Release a scope claim. Returns the released claim data or None."""
    paths = _coordination_paths(state_dir)
    claim_file = paths["claims_dir"] / f"{instance_id}.json"
    data = atomic_read(claim_file)
    _remove_file_if_exists(claim_file)
    return data


def list_claims(state_dir: Path) -> list[dict]:
    """List all active claims."""
    paths = _coordination_paths(state_dir)
    claims_dir = paths["claims_dir"]
    claims_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for entry in sorted(claims_dir.iterdir()):
        if entry.suffix == ".json" and not entry.name.startswith("."):
            data = atomic_read(entry)
            if data is not None:
                results.append(data)
    return results


# ---------------------------------------------------------------------------
# Sweep stale instances
# ---------------------------------------------------------------------------

def sweep_stale_instances(
    state_dir: Path,
    stale_hours: float = STALE_INSTANCE_HOURS,
    *,
    _now: Optional[datetime] = None,
) -> list[dict]:
    """Sweep dead (PID check) or stale (age > stale_hours) instances.

    Returns list of swept instance dicts with 'reason' field.
    Also removes their claim files.
    """
    now = _now or datetime.now(timezone.utc)
    stale_seconds = stale_hours * 3600
    paths = _coordination_paths(state_dir)
    swept: list[dict] = []

    for instance in list_instances(state_dir):
        pid = instance.get("pid")
        last_seen = instance.get("lastSeen", "")
        instance_id = instance.get("instanceId", "")

        # Check PID liveness
        dead = pid is not None and not _is_process_alive(pid)

        # Check age staleness
        stale = False
        if last_seen:
            try:
                seen_dt = datetime.fromisoformat(last_seen)
                age_seconds = (now - seen_dt).total_seconds()
                stale = age_seconds > stale_seconds
            except (ValueError, TypeError):
                stale = True  # unparseable timestamp treated as stale

        if not dead and not stale:
            continue

        # Remove instance file and its claim
        _remove_file_if_exists(paths["instances_dir"] / f"{instance_id}.json")
        _remove_file_if_exists(paths["claims_dir"] / f"{instance_id}.json")

        swept.append({
            **instance,
            "reason": "dead process" if dead else "stale",
        })

    return swept


def get_swept_feature_ids(swept: list[dict]) -> list[str]:
    """Extract feature IDs from swept instances for requeueing."""
    seen = set()
    result = []
    for entry in swept:
        fid = entry.get("featureId")
        if fid and fid not in seen:
            seen.add(fid)
            result.append(fid)
    return result
