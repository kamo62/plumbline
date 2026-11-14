"""In-memory provider diagnostic. The result contains no raw responses."""
from dataclasses import dataclass
from .contract import EvidenceState, SCMProvider, Window


@dataclass(frozen=True)
class DoctorResult:
    capabilities: tuple[str, ...]
    checks: tuple[tuple[str, str, str | None, int], ...]


async def doctor(provider: SCMProvider, projects: list[str], window: Window) -> DoctorResult:
    """Collect once and return capability and field-state counts, without raw data."""
    try:
        snapshot = await provider.collect(projects, window)
    except Exception as exc:
        status = getattr(exc, "status", None)
        code = "permission_denied" if status in (401, 403) else "collection_failed"
        return DoctorResult(tuple(sorted(provider.capabilities)), (("collection", EvidenceState.FAILED.value, code, 1),))
    counts = {}
    for record in snapshot.projects + snapshot.repositories + snapshot.builds:
        for name, evidence in record.fields.items():
            key = (name, evidence.state.value, evidence.error_code)
            counts[key] = counts.get(key, 0) + 1
    return DoctorResult(tuple(sorted(snapshot.capabilities)), tuple((*key, count) for key, count in sorted(counts.items(), key=lambda item: str(item[0]))))
