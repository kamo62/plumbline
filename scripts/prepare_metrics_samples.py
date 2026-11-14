"""Prepare a small, allowlisted historical-metrics fixture without copying raw exports."""
import argparse
from collections import Counter
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

METRICS = frozenset('maturity_score policy_compliance review_policy_enforcement branch_protection actual_review_participation cycle_time review_depth code_coverage test_pass_rate build_success_rate prod_deployment_frequency build_time deployment_success_rate security_scanning_coverage'.split())
FIELDS = frozenset('active_avg active_repo_count activity_tier blocking_count blocking_pct covers_default_branch current_passed_tests current_reviewed_prs current_sample_size current_scanned_builds current_success current_total current_total_builds current_total_prs current_total_tests decay_factor decayed_from_score default_branch_blocking default_branch_comment default_branch_count default_branch_pct default_branch_review dimensions enabled_count enabled_pct exempted_metrics has_blocking_review has_comment has_review historical_passed_tests historical_reviewed_prs historical_sample_size historical_scanned_builds historical_success historical_total historical_total_builds historical_total_prs historical_total_tests inactive_avg inactive_repo_count is_insufficient_data is_outdated last_activity_at last_scored_at latest_build_activity_at latest_commit_activity_at latest_pr_activity_at level2_cap_applied maturity_level missing_fields missing_penalty outdated_repo_count policy_count repo_count rollup_source scoring_inclusion_reason stale stale_only stale_repo_count tier_weights weighted_score quality_assurance deployment_pipeline security_compliance collaboration_metrics coverage_pct expected_metric_count missing_metrics observed_metric_count raw_score active inactive'.split())
ENUMS = frozenset('pass fail warning info missing_fields active inactive stale observed unavailable insufficient_data exempt not_applicable collection_failed project repository measured'.split())
REDACTED = '[removed]'


def shifted(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (parsed - timedelta(days=1000)).isoformat()
    except (ValueError, TypeError):
        return REDACTED


def metadata(value, removed):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        removed['unknown_keys'] += sum(k not in FIELDS for k in value)
        return {k: metadata(v, removed) for k, v in value.items() if k in FIELDS}
    if isinstance(value, list):
        return [metadata(v, removed) for v in value]
    if isinstance(value, str):
        if value in ENUMS | METRICS | FIELDS:
            return value
        if re.fullmatch(r'\d{4}-\d\d-\d\dT[0-9:.+Z-]+', value):
            return shifted(value)
    removed['free_text'] += 1
    return REDACTED


def prepare(source, destination):
    folders = sorted(p for p in source.iterdir() if re.fullmatch(r'2026-(07|08|09|10)_[a-f0-9]+', p.name) and (p / 'metrics.json').is_file())
    if len(folders) != 4:
        raise ValueError('Expected the four July–October 2026 monthly aggregate extracts')
    datasets = [json.loads((p / 'metrics.json').read_text()) for p in folders]
    common = set.intersection(*({(r['project'], r['repo']) for r in rows if r.get('project') and r.get('repo')} for rows in datasets))
    candidates = sorted({project for project, _ in common if sum(p == project for p, _ in common) >= 3})
    if len(candidates) < 3:
        raise ValueError('Not enough shared projects and repositories')
    chosen = candidates[:3]
    selected = {(project, repo) for project in chosen for repo in sorted(r for p, r in common if p == project)[:3]}
    project_alias = {name: f'Project-{i:03}' for i, name in enumerate(chosen, 1)}
    repo_alias = {key: f'Repository-{i:03}' for i, key in enumerate(sorted(selected), 1)}
    destination.mkdir(parents=True, exist_ok=True)
    removed = Counter()
    history, provenance = [], []
    for i, (folder, rows) in enumerate(zip(folders, datasets), 1):
        run_id = str(uuid5(NAMESPACE_URL, f'plumbline-example/run-{i}'))
        sample = []
        selected_rows = sorted((r for r in rows if (r.get('project'), r.get('repo')) in selected and r.get('metric_key') in METRICS), key=lambda r: (r['project'], r['repo'], r['metric_key']))
        for j, row in enumerate(selected_rows):
            key = (row['project'], row['repo'])
            value = row.get('metric_value')
            if isinstance(value, str):
                value = float(value) if re.fullmatch(r'-?\d+(?:\.\d+)?', value) else None
            sample.append({'id': str(uuid5(NAMESPACE_URL, f'plumbline-example/run-{i}/record-{j}')),
                'run_id': run_id, 'carried_from_run_id': None, 'org': 'Example-Organisation',
                'project': project_alias[key[0]], 'repo': repo_alias[key], 'scope': repo_alias[key],
                'metric_key': row['metric_key'], 'metric_value': value,
                'metric_json': metadata(row.get('metric_json'), removed), 'score': row.get('score'),
                'status': row.get('status') if row.get('status') in ENUMS else 'unavailable',
                'calculation_version': 'legacy-example', 'observed_at': shifted(row.get('observed_at', ''))})
        summary = json.loads((folder / 'run_summary.json').read_text())
        history.append({'id': run_id, 'status': 'succeeded', 'run_label': f'Historical sample {i}',
            'started_at': shifted(summary['started_at']), 'finished_at': shifted(summary['finished_at']),
            'completeness': None, 'assessment_status': 'legacy_import_example',
            'metrics_count': len(sample), 'fixture': f'run-{i:02}.json'})
        target = destination / f'run-{i:02}.json'
        target.write_text(json.dumps(sample, indent=2, allow_nan=False) + '\n')
        provenance.append({'source_period': folder.name[:7], 'source_sha256': sha256((folder / 'metrics.json').read_bytes()).hexdigest(),
            'source_records': len(rows), 'sample_records': len(sample), 'sample_sha256': sha256(target.read_bytes()).hexdigest()})
    (destination / 'run-history.json').write_text(json.dumps(history, indent=2) + '\n')
    manifest = {'format': 'sanitised-legacy-metrics-example-v1', 'source': 'metrics-new local aggregate exports',
        'projects': len(chosen), 'repositories': len(selected), 'runs': provenance,
        'date_shift_days': -1000, 'removed': dict(removed),
        'limitations': ['Aggregate metrics only; no raw builds, commits, PRs or reviewer identities.',
                       'Original numeric measurements and missingness retained; not validated Plumbline scoring.',
                       'No segments, executive summaries, people, URLs, tokens or source identity map included.',
                       'Repository-only subset; source organisation totals do not apply to this subset.',
                       'Not imported into the application database; no collector replay implied.']}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    result = prepare(args.source, args.destination)
    print(json.dumps({'runs': len(result['runs']), 'projects': result['projects'], 'repositories': result['repositories'],
                      'records': sum(r['sample_records'] for r in result['runs'])}))
