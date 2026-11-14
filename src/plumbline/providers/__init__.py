"""Provider contracts and bounded offline and Azure DevOps adapters."""
from .ado import ADOProvider
from .contract import Evidence, EvidenceState, Identity, Record, SCMProvider, Snapshot, Window
from .doctor import DoctorResult, doctor
from .synthetic import SyntheticProvider

__all__ = ["ADOProvider", "DoctorResult", "Evidence", "EvidenceState", "Identity", "Record", "SCMProvider", "Snapshot", "SyntheticProvider", "Window", "doctor"]
