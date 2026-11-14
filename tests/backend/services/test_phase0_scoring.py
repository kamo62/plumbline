"""Synthetic regression coverage through the real raw assessment path."""
from datetime import datetime, timezone

import pytest

from plumbline.backend.services.raw_maturity_assessment import RawMaturityAssessmentService
from plumbline.backend.services.scoring.scoring_service import ScoringService
from plumbline.services.scoring import ScoringService as LegacyScoringService


def test_synthetic_raw_assessment_scopes_evidence():
    now = datetime.now(timezone.utc).isoformat()
    repo = {'id': 'r1', 'name': 'Repo', 'project': {'id': 'p1', 'name': 'Project'},
            'defaultBranch': 'refs/heads/trunk'}
    policies = {'statistics': {'total_policies': 2, 'enabled_policies': 2},
                'branch_policies': [
                    {'branch_name': 'trunk', 'is_default_branch': False,
                     'policies': [{'isEnabled': True, 'isBlocking': True,
                                   'type': {'displayName': 'Minimum number of reviewers'}}]},
                    {'branch_name': 'main', 'is_default_branch': True,
                     'policies': [{'isEnabled': True, 'type': {'displayName': 'Build'}}]},
                ]}
    prs = [
        {'status': 'completed', 'createdBy': {'id': 'a'}, 'reviewers': [{'id': 'b', 'vote': 10}]},
        {'status': 'completed', 'createdBy': {'id': 'a'}, 'reviewers': [{'id': 'a', 'vote': 10}]},
        {'status': 'active', 'createdBy': {'id': 'a'}, 'reviewers': [{'id': 'b', 'vote': 0}]},
        {'status': 'active', 'createdBy': {'id': 'a'}, 'reviewers': [{'id': 'b', 'vote': -10}]},
    ]
    builds = [
        {'id': str(i), 'repository': {'id': 'r1'}, 'result': 'succeeded',
         'sourceBranch': branch, 'reason': reason, 'finishTime': now}
        for i, branch, reason in [(1, 'refs/heads/trunk', 'individualCI'),
                                  (2, 'refs/heads/feature', 'individualCI'),
                                  (3, 'refs/heads/trunk', 'pullRequest')]
    ]
    data = {'repositories': [repo], 'repository_data': {'r1': {'policies': policies, 'pull_requests': prs}},
            'builds': builds, 'build_data': {
                '1': {'tests': {'results': [{'total': 10, 'passed': 9, 'failed': 1}]}},
                '2': {'tests': {'results': [{'total': 90, 'passed': 45, 'failed': 40}]}},
                '99': {'tests': {'results': [{'total': 1000, 'passed': 1000}]}},
            }}
    service = RawMaturityAssessmentService()
    result = service.assess_raw_data(data)['repositories']['r1']
    qa = result['quality_assurance']
    assert qa['metrics']['test_pass_rate'] == 54.0  # (9 + 45) / (10 + 90)
    assert qa['metrics']['build_success_rate'] == 100.0
    assert result['deployment_pipeline']['metrics']['deployment_frequency'] == 1.0
    assert any('not a DORA' in line for line in result['deployment_pipeline']['explanation'])
    assert result['collaboration_metrics']['metrics']['review_coverage'] == 25.0
    assert result['security_compliance']['metrics']['branch_protection'] == 45.0
    assert service.assessors['security_compliance']._calculate_branch_protection({'policies': policies}) is None
    assert service.assessors['deployment_pipeline']._calculate_deployment_frequency(builds, None) is None


def test_levels_and_defaults_match_across_scoring_entrypoints():
    service = RawMaturityAssessmentService()
    assert (service.scoring.current_month_weight, service.scoring.historical_weight) == (0.8, 0.2)
    for score, level in [(0.199, 1), (0.2, 2), (0.4, 3), (0.6, 4), (0.8, 5)]:
        assert ScoringService().determine_maturity_level(score) == level
        assert LegacyScoringService().determine_maturity_level(score) == level


def test_build_success_is_reported_but_does_not_change_qa_score():
    assessor = RawMaturityAssessmentService().assessors['quality_assurance']
    data = {'1': {'tests': {'results': [{'total': 10, 'passed': 5}]}}}
    passing = assessor.assess({}, [{'id': '1', 'result': 'succeeded'}], data, 'Project')
    failing = assessor.assess({}, [{'id': '1', 'result': 'failed'}], data, 'Project')
    assert passing['metrics']['build_success_rate'] == 100
    assert failing['metrics']['build_success_rate'] == 0
    assert passing['score'] == pytest.approx(failing['score'])
