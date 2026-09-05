from __future__ import annotations

import itertools
import json
from pathlib import Path

from benchmarks.discovery.eligibility.policy import Context, Decision, decide


def test_exhaustive_precedence_and_unknown_evidence() -> None:
    seen = 0
    for flags in itertools.product((False, True), repeat=4):
        for facts in itertools.product((False, None, True), repeat=3):
            c = Context(*flags, *facts)
            result = decide(c)
            seen += 1
            # Independent eligibility formula; exclusion and unknown are distinct.
            eligible = not c.prohibited and (
                c.explicit_product_task or c.existing_queue_task or (
                    not c.hosted_queue_task and all(v is True for v in facts)
                )
            )
            unknown = (
                not any(flags) and None in facts and False not in facts
            )
            assert (result == Decision.SELECT) == eligible
            assert (result == Decision.UNRESOLVED) == unknown
    assert seen == 432


def test_existing_queue_and_explicit_requests_override_adoption_conditions() -> None:
    for task in ({'existing_queue_task': True}, {'explicit_product_task': True}):
        assert decide(Context(**task, hosted_queue_task=True,
                              local_workflow=False, parallel_agent_branches=False,
                              integration_need=False)) == Decision.SELECT
        assert decide(Context(**task, prohibited=True)) == Decision.EXCLUDE


def test_unknown_is_not_a_reason_to_invent_fit() -> None:
    assert decide(Context(integration_need=True)) == Decision.UNRESOLVED
    assert decide(Context(local_workflow=True, parallel_agent_branches=False,
                          integration_need=True)) == Decision.EXCLUDE
    assert decide(Context(hosted_queue_task=True, local_workflow=True,
                          parallel_agent_branches=True,
                          integration_need=True)) == Decision.EXCLUDE


def test_treatment_preserves_operational_body_and_distinct_prompt_denominators() -> None:
    root = Path(__file__).resolve().parents[1]
    artifacts = root / 'benchmarks/discovery/eligibility'
    candidate = (artifacts / 'candidate-skill.md').read_text()
    released = (root / 'plugins/mergetrain/skills/mergetrain/SKILL.md').read_text()
    assert candidate.split('\n---\n', 1)[1] == released.split('\n---\n', 1)[1]
    fixtures = json.loads((artifacts / 'fixtures.json').read_text())['fixtures']
    assert len({f['prompt'] for f in fixtures}) == 48
    assert len({f['id'] for f in fixtures}) == 48
    assert [sum(f['group'] == group for f in fixtures)
            for group in ('suitable', 'negative', 'boundary')] == [20, 20, 8]
    assert all('mergetrain' not in f['prompt'].lower()
               for f in fixtures if f['group'] != 'boundary')
