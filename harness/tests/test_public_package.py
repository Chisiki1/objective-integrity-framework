"""Public policy identity and source-only distribution regressions."""
import json
from pathlib import Path
import pytest
from policy_harness.models import PolicyError
from policy_harness.practical_policy import PracticalPolicy

ROOT=Path(__file__).resolve().parents[1]


def test_self_contained_public_policy_has_complete_current_inventory():
    policy=PracticalPolicy(ROOT/'policy/complete-policy-v3.json')
    assert len(policy.baseline.conditions)==236
    assert set(policy.baseline.rules)=={f'R{n:02d}' for n in range(1,19)}
    assert all(row['body'] in policy.parent_reference for row in policy.baseline.conditions.values())
    assert policy.baseline.supplement is None and policy.baseline.role_supplement is None
    assert policy.overlay['mapping']
    policy.verify_current()


def test_changed_public_policy_is_not_silently_accepted(tmp_path):
    for name in ('complete-policy-v3.json','practical-runtime-v1.json'):
        (tmp_path/name).write_bytes((ROOT/'policy'/name).read_bytes())
    policy=PracticalPolicy(tmp_path/'complete-policy-v3.json')
    source=json.loads(policy.path.read_bytes())
    source['conditions'][0]['body']='Changed condition'
    policy.path.write_text(json.dumps(source),encoding='utf-8')
    with pytest.raises(PolicyError,match='changed'):
        policy.verify_current()
