#!/usr/bin/env python3
r"""Compare last month's and this month's metrics Excel exports.

Generates CSVs of row-by-row changes and a concise Markdown summary.

Inputs default to exports/previous and exports/current maturity workbooks.
Use --prev-maturity and --curr-maturity to select other workbooks.
Optionally supply --prev-raw and --curr-raw to compare raw repository records by ID.
Outputs are change CSVs and a Markdown summary.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import pandas as pd
from plumbline.config.access import checked_path


def _ensure_out_dir(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)


def _safe_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _status_row(prev_present: bool, curr_present: bool, changed: bool) -> str:
    if prev_present and not curr_present:
        return "removed"
    if not prev_present and curr_present:
        return "new"
    if changed:
        return "changed"
    return "unchanged"


def _outer_join(
    prev: pd.DataFrame, curr: pd.DataFrame, keys: List[str]
) -> Tuple[pd.DataFrame, List[str]]:
    # Prepare to avoid duplicate columns during merge by suffixing
    merged = prev.merge(
        curr,
        on=keys,
        how="outer",
        suffixes=("__prev", "__curr"),
        indicator=True,
    )
    return merged, keys


def _compute_numeric_deltas(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    for c in cols:
        p, q = f"{c}__prev", f"{c}__curr"
        if p in df.columns and q in df.columns:
            df[p] = _safe_numeric(df[p])
            df[q] = _safe_numeric(df[q])
            df[f"{c}__delta"] = df[q] - df[p]
    return df


def _compute_string_changes(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    for c in cols:
        p, q = f"{c}__prev", f"{c}__curr"
        if p in df.columns and q in df.columns:
            df[f"{c}__changed"] = df[p].fillna("").astype(str) != df[q].fillna("").astype(str)
    return df


def _finalize_status(df: pd.DataFrame, compare_cols: List[str]) -> pd.DataFrame:
    # Determine if any compare col changed
    changed_cols = []
    for c in compare_cols:
        if c not in df.columns:
            continue
        if c.endswith("__delta"):
            changed_cols.append(df[c].fillna(0) != 0)
        elif c.endswith("__changed"):
            changed_cols.append(df[c].fillna(False))
    any_changed = pd.Series(False, index=df.index)
    if changed_cols:
        any_changed = changed_cols[0]
        for s in changed_cols[1:]:
            any_changed = any_changed | s

    # Mark presence
    prev_present = df["_merge"].isin(["left_only", "both"])  # left = prev
    curr_present = df["_merge"].isin(["right_only", "both"])  # right = curr

    df["row_status"] = [
        _status_row(bool(pp), bool(cp), bool(ch))
        for pp, cp, ch in zip(prev_present, curr_present, any_changed)
    ]
    return df


def compare_maturity_assessment(prev_path: Path, curr_path: Path) -> pd.DataFrame:
    sheet = "Maturity Assessment"
    keys = ["Project", "Repository"]
    numeric = [
        "Overall Score",
        "Quality Assurance Score",
        "Deployment Pipeline Score",
        "Security Compliance Score",
        "Collaboration Metrics Score",
    ]
    strings = [
        "Overall Level",
        "Overall Label",
        "Quality Assurance Level",
        "Deployment Pipeline Level",
        "Security Compliance Level",
        "Collaboration Metrics Level",
    ]

    prev = pd.read_excel(checked_path(prev_path), sheet_name=sheet)
    curr = pd.read_excel(checked_path(curr_path), sheet_name=sheet)

    # Ensure keys exist even if column order differs
    for k in keys:
        if k not in prev.columns:
            raise KeyError(f"Missing key column in prev: {k}")
        if k not in curr.columns:
            raise KeyError(f"Missing key column in curr: {k}")

    merged, _ = _outer_join(prev, curr, keys)
    merged = _compute_numeric_deltas(merged, numeric)
    merged = _compute_string_changes(merged, strings)

    compare_cols = [f"{c}__delta" for c in numeric] + [f"{c}__changed" for c in strings]
    merged = _finalize_status(merged, compare_cols)

    front = keys + ["row_status"]
    numeric_cols = sum(([[f"{c}__prev", f"{c}__curr", f"{c}__delta"] for c in numeric]), [])
    string_cols = sum(([[f"{c}__prev", f"{c}__curr", f"{c}__changed"] for c in strings]), [])
    other = [c for c in merged.columns if c not in front + numeric_cols + string_cols]
    ordered = [c for c in front + numeric_cols + string_cols + other if c in merged.columns]
    return merged[ordered].sort_values(keys)


def compare_raw_repositories(prev_path: Path, curr_path: Path) -> pd.DataFrame:
    """Compare repository records by their source ID in raw Excel exports."""
    prev = pd.read_excel(checked_path(prev_path), sheet_name="Repositories")
    curr = pd.read_excel(checked_path(curr_path), sheet_name="Repositories")
    for frame in (prev, curr):
        if "id" not in frame or frame["id"].isna().any() or frame["id"].duplicated().any():
            raise ValueError("Raw repository exports require unique, non-empty id values")
    columns = sorted((set(prev.columns) | set(curr.columns)) - {"id"})
    prev = prev.reindex(columns=["id", *columns])
    curr = curr.reindex(columns=["id", *columns])
    merged, _ = _outer_join(prev, curr, ["id"])
    merged = _compute_string_changes(merged, columns)
    return _finalize_status(merged, [f"{c}__changed" for c in columns]).sort_values("id")


@dataclass
class CompareInputs:
    prev_maturity: Path
    curr_maturity: Path
    out_dir: Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prev-raw", type=Path, help="Previous raw Excel workbook")
    p.add_argument("--curr-raw", type=Path, help="Current raw Excel workbook")
    p.add_argument(
        "--prev-maturity",
        type=Path,
        default=Path("exports/previous/metrics_maturity_assessment.xlsx"),
        help="Previous month maturity assessment Excel",
    )
    p.add_argument(
        "--curr-maturity",
        type=Path,
        default=Path("exports/current/metrics_maturity_assessment.xlsx"),
        help="Current month maturity assessment Excel",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory for CSVs and summary (default: exports/diffs_YYYYMMDD_HHMMSS)",
    )
    return p


def write_summary(
    out_dir: Path,
    mat_df: pd.DataFrame,
) -> None:
    def status_counts(df: pd.DataFrame) -> str:
        counts = df["row_status"].value_counts().to_dict()
        # Ensure stable order
        parts = [
            f"new: {counts.get('new', 0)}",
            f"removed: {counts.get('removed', 0)}",
            f"changed: {counts.get('changed', 0)}",
            f"unchanged: {counts.get('unchanged', 0)}",
        ]
        return ", ".join(parts)

    def top_changes(df: pd.DataFrame, key_cols: List[str], delta_col: str, n: int = 5) -> Tuple[pd.DataFrame, pd.DataFrame]:
        if delta_col not in df.columns:
            return df.head(0), df.head(0)
        changed = df[df[delta_col].fillna(0) != 0]
        inc = changed.sort_values(delta_col, ascending=False).head(n)
        dec = changed.sort_values(delta_col, ascending=True).head(n)
        return (
            inc[key_cols + [delta_col]].reset_index(drop=True),
            dec[key_cols + [delta_col]].reset_index(drop=True),
        )

    mat_inc, mat_dec = top_changes(
        mat_df, ["Project", "Repository"], "Overall Score__delta"
    )

    md = []
    md.append("# Monthly Metrics Diff Summary")
    md.append("")
    md.append("## Maturity Assessment (per repository)")
    md.append(f"Counts: {status_counts(mat_df)}")
    if not mat_inc.empty:
        md.append("")
        md.append("Top increases (Overall Score):")
        md.extend([
            f"- {r['Project']} / {r['Repository']}: +{r['Overall Score__delta']:.2f}"
            for _, r in mat_inc.iterrows()
        ])
    if not mat_dec.empty:
        md.append("")
        md.append("Top decreases (Overall Score):")
        md.extend([
            f"- {r['Project']} / {r['Repository']}: {r['Overall Score__delta']:.2f}"
            for _, r in mat_dec.iterrows()
        ])

    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if bool(args.prev_raw) != bool(args.curr_raw):
        parser.error("--prev-raw and --curr-raw must be supplied together")
    # Resolve out_dir default dynamically if not provided
    if args.out_dir is None:
        from datetime import datetime
        args.out_dir = Path("exports") / f"diffs_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    inputs = CompareInputs(
        prev_maturity=args.prev_maturity,
        curr_maturity=args.curr_maturity,
        out_dir=args.out_dir,
    )

    _ensure_out_dir(checked_path(inputs.out_dir))

    mat_changes = compare_maturity_assessment(inputs.prev_maturity, inputs.curr_maturity)

    mat_changes.to_csv(inputs.out_dir / "maturity_assessment_changes.csv", index=False)
    if args.prev_raw:
        raw_changes = compare_raw_repositories(args.prev_raw, args.curr_raw)
        raw_changes.to_csv(inputs.out_dir / "raw_repository_changes.csv", index=False)

    write_summary(inputs.out_dir, mat_changes)

    print(f"Wrote diffs to: {inputs.out_dir}")
    if args.prev_raw:
        print(" - raw_repository_changes.csv")
    print(" - maturity_assessment_changes.csv")
    print(" - summary.md")


if __name__ == "__main__":
    main()
