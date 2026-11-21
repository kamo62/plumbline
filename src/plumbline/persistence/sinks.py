from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Protocol

from sqlmodel import Session

from .models import Run, RawMetric, Metric


AssessmentResults = Dict[str, Any]


class StorageSink(Protocol):
    def persist(self, results: AssessmentResults, run_context: Dict[str, Any]) -> Dict[str, str] | None:  # pragma: no cover - protocol
        ...


@dataclass
class CompositeSink(StorageSink):
    sinks: List[StorageSink]
    # Propagate DB sink run_id to callers for validation/reporting
    last_run_id: Any = None
    last_snapshot_id: str | None = None

    def persist(self, results: AssessmentResults, run_context: Dict[str, Any]) -> Dict[str, str]:
        outputs: Dict[str, str] = {}
        self.last_run_id = None
        self.last_snapshot_id = None
        for sink in self.sinks:
            outputs.update(sink.persist(results, run_context) or {})
            # If a sink exposes last_run_id, propagate it
            lrid = getattr(sink, "last_run_id", None)
            if lrid is not None:
                self.last_run_id = lrid
            snapshot_id = getattr(sink, "last_snapshot_id", None)
            if snapshot_id is not None:
                self.last_snapshot_id = snapshot_id
        return outputs


class FileSink(StorageSink):
    """Write file artifacts (JSON/Excel) based on collected results.

    Expects the following keys in `results`:
      - raw_ado_data: Dict
      - file_output_base: str (base path without extension)
      - maturity_data: Optional[Dict]
    """

    def persist(self, results: AssessmentResults, run_context: Dict[str, Any]) -> Dict[str, str]:
        from pathlib import Path
        import json

        outputs: Dict[str, str] = {}
        base = results.get("file_output_base")
        raw = results.get("raw_ado_data") or {}
        maturity = results.get("maturity_data")

        if not base:
            # Default to timestamped path if not provided
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = f"exports/{ts}/metrics"

        base_path = Path(base)
        base_path.parent.mkdir(parents=True, exist_ok=True)

        # 1) Raw JSON + Excel
        if raw:
            try:
                from plumbline.backend.services.raw_data_exporter import RawDataExporter
                exporter = RawDataExporter(None)
                written = exporter.export_data(raw, str(base_path.with_suffix(".xlsx")))
                outputs.update({f"raw_{kind}": path for kind, path in written.items()})
            except Exception:
                # Fallback minimal JSON write if exporter failed
                with open(base_path.with_suffix(".json"), "w") as f:
                    json.dump(raw, f, indent=2)
                outputs["raw_json"] = str(base_path.with_suffix(".json"))

        # 2) Maturity assessment (Excel + JSON)
        if maturity:
            try:
                from plumbline.backend.services.maturity_assessment_exporter import (
                    MaturityAssessmentExporter,
                )
                maturity_output = base_path.with_name(f"{base_path.stem}_maturity_assessment").with_suffix(
                    ".xlsx"
                )
                exporter = MaturityAssessmentExporter(None)
                written = exporter.export_data(maturity, str(maturity_output))
                if not written:
                    raise RuntimeError("Maturity export produced no artifacts")
                outputs.update({("maturity_assessment" if kind == "json" else "maturity_excel"): path
                                for kind, path in written.items()})
            except Exception:
                # Fallback JSON only
                with open(
                    base_path.with_name(f"{base_path.stem}_maturity_assessment").with_suffix(".json"),
                    "w",
                ) as f:
                    json.dump(maturity, f, indent=2)
                outputs["maturity_assessment"] = str(base_path.with_name(f"{base_path.stem}_maturity_assessment").with_suffix(".json"))

        return outputs


class PostgresSink(StorageSink):
    def __init__(self, engine):
        self._engine = engine
        self.last_run_id = None
        self.last_snapshot_id = None

    def _serialize_value(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            if obj.tzinfo is None:
                obj = obj.replace(tzinfo=timezone.utc)
            return obj.astimezone(timezone.utc).isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if hasattr(obj, "__dict__"):
            return str(obj)
        return obj

    def _sanitize_raw_data(self, raw_data: Any) -> Dict[str, Any]:
        import json
        pii_keys = {
            "email",
            "username",
            "displayname",
            "principalname",
            "token",
            "password",
            "secret",
            "authorization",
            "key",
        }

        def sanitize(obj: Any) -> Any:
            if isinstance(obj, dict):
                cleaned: Dict[str, Any] = {}
                for k, v in obj.items():
                    if isinstance(k, str) and k.lower() in pii_keys:
                        cleaned[k] = "[REDACTED]"
                    else:
                        cleaned[k] = sanitize(v)
                return cleaned
            if isinstance(obj, list):
                return [sanitize(i) for i in obj]
            return self._serialize_value(obj)

        try:
            cleaned = sanitize(raw_data)
            json.dumps(cleaned)  # ensure serializable
            return cleaned  # type: ignore[return-value]
        except (TypeError, ValueError) as e:
            return {"error": f"Failed to sanitize data: {e}", "raw_string": str(raw_data)}

    def persist(self, results: AssessmentResults, run_context: Dict[str, Any]) -> None:
        """
        Persist a run with:
        - Run row with audit context and executed_at timestamp
        - RawMetric rows for raw_ado_data (sanitized)
        - Metric rows for calculated metrics if provided via results['calculated_metrics']
        """
        from decimal import Decimal, InvalidOperation
        from uuid import uuid4

        self.last_run_id = None
        self.last_snapshot_id = None
        now = datetime.now(timezone.utc)
        # Ensure executed_at is timezone-aware
        executed_at_raw = run_context.get("executed_at")
        if isinstance(executed_at_raw, datetime):
            if executed_at_raw.tzinfo is None:
                # Naive datetime - assume UTC
                executed_at = executed_at_raw.replace(tzinfo=timezone.utc)
            else:
                # Aware datetime - convert to UTC
                executed_at = executed_at_raw.astimezone(timezone.utc)
        else:
            executed_at = now
        with Session(self._engine) as session:
            from uuid import UUID
            from sqlalchemy import delete
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            from sqlalchemy.dialects.postgresql import insert as postgres_insert

            supplied_id = run_context.get("run_id")
            run_id = UUID(str(supplied_id)) if supplied_id else uuid4()
            values = {key: run_context.get(key) for key in (
                "org", "project", "repo", "branch", "commit_sha", "run_label",
                "tool_version", "thresholds_version", "config_hash", "calculation_version")}
            values.update(id=run_id, executed_at=executed_at,
                          environment=run_context.get("environment", "local"))
            insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgres_insert
            session.execute(insert(Run).values(**values).on_conflict_do_nothing(index_elements=["id"]))
            run = session.get(Run, run_id)
            for key, value in values.items():
                if key != "id":
                    setattr(run, key, value)
            snapshot_id = None
            if results.get("provider_snapshot") is not None:
                from plumbline.collection.store import link_run_snapshot, persist_snapshot
                conn = session.connection()
                snapshot_id = persist_snapshot(conn, results["provider_snapshot"])
                link_run_snapshot(conn, run_id, snapshot_id)
            # One complete snapshot per UUID; retries replace its dependent records atomically.
            session.execute(delete(RawMetric).where(RawMetric.run_id == run_id))
            session.execute(delete(Metric).where(Metric.run_id == run_id))

            # Store raw data if present
            raw_data_dict = results.get("raw_ado_data") or {}
            if raw_data_dict:
                sanitized = self._sanitize_raw_data(raw_data_dict)
                raw_metric = RawMetric(
                    run_id=run.id,
                    source_system="ado",
                    source_key="raw_export",
                    raw_data=sanitized,
                    collected_at=now,
                    org=run.org,
                    project=run.project,
                    repo=run.repo,
                )
                session.add(raw_metric)

            # Store calculated metrics if provided
            calc_metrics = results.get("calculated_metrics") or []
            if isinstance(calc_metrics, list) and calc_metrics:
                metric_rows: List[Metric] = []
                for m in calc_metrics:
                    scope = m.get("scope") or ""
                    metric_key = m.get("metric_key") or m.get("key")
                    if not metric_key:
                        continue  # skip malformed entries
                    # Preserve valid 0.0 values; only fall back if None
                    value_float = m.get("value_float") if m.get("value_float") is not None else m.get("metric_value")
                    # Convert floats to Decimal to preserve precision in NUMERIC columns
                    metric_value = None
                    if value_float is not None:
                        try:
                            metric_value = Decimal(str(value_float))
                        except (InvalidOperation, ValueError, TypeError):
                            metric_value = None
                    metric_json = m.get("value_json") or m.get("metric_json")
                    score_val = m.get("score")
                    score = None
                    if score_val is not None:
                        try:
                            score = Decimal(str(score_val))
                        except (InvalidOperation, ValueError, TypeError):
                            score = None
                    status = m.get("status")
                    # Default observed_at to run.executed_at for consistency
                    observed_at = m.get("observed_at") if isinstance(m.get("observed_at"), datetime) else executed_at

                    metric_rows.append(
                        Metric(
                            run_id=run.id,
                            scope=scope,
                            metric_key=metric_key,
                            metric_value=metric_value,
                            metric_json=metric_json,
                            score=score,
                            status=status,
                            calculation_version=run_context.get("calculation_version"),
                            observed_at=observed_at,
                            # denormalized context for query perf
                            org=run.org,
                            project=run.project,
                            repo=run.repo,
                        )
                    )
                if metric_rows:
                    session.add_all(metric_rows)

            session.commit()
            # Track last run id for downstream validation/reporting
            self.last_run_id = str(run.id)
            self.last_snapshot_id = snapshot_id
