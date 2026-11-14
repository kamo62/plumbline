import importlib.util
import json
from pathlib import Path

from plumbline.api.models import CollectionConfig


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_samples', ROOT / 'scripts/prepare_metrics_samples.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_allowlist_removes_nested_identities_and_secrets():
    from collections import Counter

    original = {'current_total': 12, 'missing_fields': ['user@example.com', 'test_pass_rate'],
                'credential': 'private-token', 'dimensions': {'quality_assurance': {
                    'weighted_score': 45, 'owner': 'Private Person'}},
                'scoring_inclusion_reason': 'https://internal.example/private',
                'last_activity_at': '2026-07-01T00:00:00Z'}
    removed = Counter()
    cleaned = module.metadata(original, removed)
    text = json.dumps(cleaned)
    for sensitive in ('user@example.com', 'private-token', 'Private Person', 'internal.example'):
        assert sensitive not in text
    assert cleaned['current_total'] == 12
    assert cleaned['missing_fields'] == ['[removed]', 'test_pass_rate']
    assert cleaned['last_activity_at'] != original['last_activity_at']
    assert original['credential'] == 'private-token'


def test_example_run_requests_match_current_contract():
    requests = json.loads((ROOT / 'samples/run-requests.json').read_text())
    assert len(requests) == 3
    for body in requests.values():
        config = CollectionConfig.model_validate(body)
        assert config.connection_id
