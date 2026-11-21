"""Project provider evidence into the transitional raw export shape.

This boundary never contacts a provider. Unsupported or ambiguous evidence blocks
legacy assessment while the original, project-scoped snapshot remains intact.
"""
from dataclasses import asdict
from datetime import datetime
from enum import Enum

from plumbline.providers.contract import EvidenceState


def _json(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json(v) for v in value]
    return value


def _policies(configs, types, repo_id, observed_at, default_branch=None, *, empty_repo=False):
    """Project policies once per branch while retaining each applicable scope."""
    if any(not isinstance(item, dict) for item in configs + types):
        raise ValueError("malformed_policy")
    type_map = {item.get("id"): item for item in types if item.get("id")}
    repository, branches, applied = [], [], []
    project_ids, repository_ids = set(), set()
    for index, original in enumerate(configs):
        if original.get("isDeleted"):
            continue
        settings, kind = original.get("settings"), original.get("type")
        if not isinstance(settings, dict) or not isinstance(kind, dict):
            raise ValueError("malformed_policy")
        scopes = settings.get("scope")
        if scopes is None:
            label = str(type_map.get(kind.get("id"), kind).get("displayName", "")).lower()
            if any(term in label for term in ("work item query", "work item type", "test plan")):
                continue
            raise ValueError("malformed_policy_scope")
        if isinstance(scopes, dict):
            scopes = [scopes]
        if not isinstance(scopes, list) or not scopes or any(not isinstance(scope, dict) or "repositoryId" not in scope for scope in scopes):
            raise ValueError("malformed_policy_scope")
        matches = []
        for scope in scopes:
            if scope.get("repositoryId") not in (None, repo_id):
                continue
            ref, match = scope.get("refName"), scope.get("matchKind")
            if match == "DefaultBranch":
                if not default_branch and empty_repo:
                    continue
                if not isinstance(default_branch, str) or not default_branch.startswith("refs/heads/") or default_branch == "refs/heads/":
                    raise ValueError("unknown_default_branch")
                ref = default_branch
            elif match in ("Prefix", "Exact"):
                if match == "Prefix" and ref == "refs/heads":
                    ref = "refs/heads/"
                if not isinstance(ref, str) or not ref.startswith("refs/heads/") or (match == "Exact" and ref == "refs/heads/"):
                    raise ValueError("malformed_policy_scope")
                if match == "Prefix":
                    if not default_branch and empty_repo:
                        continue
                    if not isinstance(default_branch, str) or not default_branch.startswith("refs/heads/") or default_branch == "refs/heads/":
                        raise ValueError("unknown_default_branch")
                    # The documented prefix is a branch-folder prefix.
                    folder = ref.rstrip("/") + "/"
                    if default_branch.startswith(folder):
                        ref = default_branch
                    elif not ref.endswith("/") and default_branch.startswith(ref):
                        raise ValueError("ambiguous_policy_scope")
                    else:
                        # Retain non-default folder scopes for policy statistics
                        # and exports without claiming a default-branch match.
                        ref = folder
            elif match is None and ref is None:
                ref = None
            else:
                raise ValueError("unknown_policy_scope")
            matches.append((ref, scope))
        if not matches:
            continue
        if type(original.get("isEnabled")) is not bool or type(original.get("isBlocking")) is not bool:
            raise ValueError("malformed_policy_flags")
        policy = {**original, "type": type_map.get(kind.get("id"), kind)}
        identity = ("id", str(policy["id"])) if policy.get("id") is not None else ("index", index)
        if not any(identity == item[0] for item in applied):
            applied.append((identity, policy))
        if any(scope.get("repositoryId") is None for _, scope in matches):
            project_ids.add(identity)
        if any(scope.get("repositoryId") == repo_id for _, scope in matches):
            repository_ids.add(identity)
        for ref in dict.fromkeys(ref for ref, _ in matches):
            relevant = [scope for target, scope in matches if target == ref]
            scoped = {**policy, "applicable_scopes": relevant}
            target = repository if ref is None else None
            if ref is not None:
                name = ref.removeprefix("refs/heads/")
                entry = next((item for item in branches if item["branch_name"] == name), None)
                if entry is None:
                    entry = {"branch_name": name, "is_default_branch": ref == default_branch,
                             "policies": [], "is_project_level": any(x.get("repositoryId") is None for x in relevant)}
                    branches.append(entry)
                entry["is_project_level"] |= any(x.get("repositoryId") is None for x in relevant)
                target = entry["policies"]
            existing = next((x for x in target if policy.get("id") is not None and x.get("id") == policy["id"]), None)
            if existing:
                existing["applicable_scopes"].extend(relevant)
            else:
                target.append(scoped)
    return {"repository_policies": repository, "branch_policies": branches,
            "statistics": {"total_policies": len(applied),
                           "blocking_policies": sum(p["isBlocking"] for _, p in applied),
                           "enabled_policies": sum(p["isEnabled"] for _, p in applied),
                           "policy_types": sorted({p["type"].get("displayName", "Unknown") for _, p in applied}),
                           "project_level_policies": len(project_ids), "repository_level_policies": len(repository_ids)},
            "scan_time": observed_at.isoformat() if observed_at else None}


def _coverage(value):
    if not isinstance(value, dict):
        raise ValueError("malformed_coverage")
    if not value:
        return value
    items = value.get("value", [value] if "modules" in value or "coverageData" in value or "lastError" in value else None)
    if not isinstance(items, list):
        raise ValueError("malformed_coverage")
    projected = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("malformed_coverage")
        if "modules" not in item and "coverageData" not in item and "lastError" not in item:
            raise ValueError("malformed_coverage")
        modules = item.get("modules", [])
        if not isinstance(modules, list) or any(not isinstance(m, dict) for m in modules):
            raise ValueError("malformed_coverage")
        modules = list(modules)
        for module in modules:
            if "statistics" in module:
                stats = module["statistics"]
                if not isinstance(stats, dict) or any(type(stats[k]) not in (int, float) or stats[k] < 0 for k in ("linesCovered", "linesNotCovered") if k in stats):
                    raise ValueError("malformed_coverage")
            elif type(module.get("coverage")) not in (int, float) or not 0 <= module["coverage"] <= 100:
                raise ValueError("malformed_coverage")
        native = item.get("coverageData", [])
        if not isinstance(native, list):
            raise ValueError("malformed_coverage")
        for section in native:
            if not isinstance(section, dict) or not isinstance(section.get("coverageStats"), list):
                raise ValueError("malformed_coverage")
            for stat in section["coverageStats"]:
                if not isinstance(stat, dict):
                    raise ValueError("malformed_coverage")
                if str(stat.get("label", "")).lower() == "lines":
                    covered, total = stat.get("covered"), stat.get("total")
                    if type(covered) not in (int, float) or type(total) not in (int, float) or covered < 0 or total < covered:
                        raise ValueError("malformed_coverage")
                    modules.append({"statistics": {"linesCovered": covered, "linesNotCovered": total - covered}})
        projected.append({**item, "modules": modules})
    return {"value": projected}


def project_snapshot(snapshot):
    evidence = _json(asdict(snapshot))
    failures = []
    coverage_denominator_unavailable = []
    for kind in ('projects', 'repositories', 'builds'):
        for record in getattr(snapshot, kind):
            for name, field in record.fields.items():
                heuristic = name == 'security_scans' and field.state == EvidenceState.PARTIAL and field.error_code == 'artifact_name_heuristic'
                if (field.state not in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY) and not heuristic) or field.fresh_as_of is False:
                    failures.append({'scope': kind, 'project_id': record.identity.project,
                                     'field': name, 'state': field.state.value,
                                     'error': field.error_code or 'historical_evidence_unavailable'})
    project_records = {r.identity.project: r for r in snapshot.projects}
    projects = {r.identity.project: r.fields['project'].value for r in snapshot.projects
                if 'project' in r.fields and isinstance(r.fields['project'].value, dict)}
    from plumbline.types.data_types import create_project_info, merge_project_info
    projects = {key: create_project_info(value) for key, value in projects.items()}
    raw = {'provider': snapshot.connection, 'evidence': evidence, 'projects': list(projects.values()),
           'project': merge_project_info(list(projects.values())) if projects else {},
           'repositories': [], 'repository_data': {}, 'builds': [], 'build_data': {}}
    for record in snapshot.repositories:
        ident = record.identity
        repo = record.fields['repository'].value
        if not isinstance(repo, dict) or repo.get('isDisabled'):
            continue
        project = {**projects.get(ident.project, {}), **(repo.get('project') or {})}
        raw['repositories'].append({**repo, 'project': project})
        key = ident.repository
        if key in raw['repository_data']:
            failures.append({'scope': 'repository', 'error': 'identity_collision', 'repository_id': key})
            continue
        data = {'project': project}
        for field in ('commits', 'branches', 'pull_requests'):
            ev = record.fields.get(field)
            if ev and ev.value is not None:
                data[field] = ev.value
        policy_ev = project_records[ident.project].fields.get('policy_configurations')
        type_ev = project_records[ident.project].fields.get('policy_types')
        if policy_ev and type_ev and policy_ev.state in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY) and type_ev.state in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY) and isinstance(policy_ev.value, list) and isinstance(type_ev.value, list):
            try:
                branches_ev = record.fields.get("branches")
                if not repo.get("defaultBranch") and branches_ev and branches_ev.state == EvidenceState.OBSERVED and branches_ev.value:
                    raise ValueError("unknown_default_branch")
                data['policies'] = _policies(policy_ev.value, type_ev.value, key, policy_ev.observed_at, repo.get('defaultBranch'), empty_repo=(record.fields.get('branches') is not None and record.fields['branches'].state == EvidenceState.OBSERVED_EMPTY))
            except (ValueError, TypeError, AttributeError) as exc:
                data.pop('policies', None)
                failures.append({'scope': 'policies', 'project_id': ident.project,
                                 'repository_id': key, 'error': str(exc)})
        raw['repository_data'][key] = data
    for record in snapshot.builds:
        ident = record.identity
        build = record.fields['build'].value
        if isinstance(build, dict):
            raw['builds'].append({**build, 'project': projects.get(ident.project, {})})
        key = ident.build
        if key in raw['build_data']:
            failures.append({'scope': 'build', 'error': 'identity_collision', 'build_id': key})
            continue
        data = {name: ev.value for name, ev in record.fields.items()
                if name != 'build' and ev.value is not None}
        coverage = data.get('coverage')
        if coverage is not None:
            try:
                data['coverage'] = _coverage(coverage)
                if any("statistics" in module and not all(k in module["statistics"] for k in ("linesCovered", "linesNotCovered"))
                       for item in data['coverage'].get('value', []) for module in item.get('modules', [])):
                    data['coverage_status'] = 'coverage_denominator_unavailable'
                    coverage_denominator_unavailable.append({'project_id': ident.project, 'build_id': key})
            except ValueError as exc:
                data.pop('coverage', None)
                failures.append({'scope': 'build', 'build_id': key, 'field': 'coverage', 'error': str(exc)})
        data['failures'] = {name: ev.error_code for name, ev in record.fields.items()
                            if ev.state not in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY)}
        raw['build_data'][key] = data
    if not snapshot.projects:
        failures.append({'scope': 'collection', 'error': 'no_projects'})
    raw['completeness'] = {'complete': not failures, 'failures': failures,
                           'caveats': ['Security scan presence is inferred from artifact names; execution and results are unverified.'] if snapshot.builds else []}
    if coverage_denominator_unavailable:
        raw['completeness']['coverage_denominator_unavailable'] = coverage_denominator_unavailable
        raw['completeness']['caveats'].append(
            f"Coverage denominator unavailable for {len(coverage_denominator_unavailable)} build(s); "
            "no coverage percentage was inferred. Legacy scoring treats this metric as missing.")
    return raw
