"""모델 원본의 손실과 서버 보존 후 후보의 차이를 구분해 기록한다."""

import json
from dataclasses import asdict, replace

from app.refresh.repair import preserve_unaffected_proposals
from evaluation.repair_observation import observe_repair, write_repair_report
from tests.refresh.test_repair_preservation import candidates


def test_period_loss_is_visible_in_raw_but_absent_after_server_preservation(tmp_path):
    scenario, previous, raw, _, diagnostics = candidates()
    effective = preserve_unaffected_proposals(previous, raw, diagnostics)
    model_input = replace(
        scenario.model_input,
        prompt=json.dumps(
            {"validation_repair": {"previous_candidate": asdict(previous)}}
        ),
    )
    observation = observe_repair(model_input, raw, effective)
    assert observation["preservation_applied"]
    dropped = observation["raw_changes"]["proposal_changes"]
    assert len(dropped) == 1
    assert dropped[0]["removed"]
    assert dropped[0]["previous_fragments_absent"] == [
        "백도윤의 파견 기한은 한 달이다."
    ]
    assert not observation["effective_changes"]["proposal_changes"]
    assert (
        observation["effective_changes"]["semantic_verdict"] == "human_review_pending"
    )
    write_repair_report(
        tmp_path,
        [
            {
                "case": "period",
                "observed_attempts": [
                    {"attempt": 2, "repair_observation": observation}
                ],
            }
        ],
    )
    saved = json.loads((tmp_path / "repair-observation.json").read_text())
    assert saved["attempts"][0]["preservation_applied"]
    assert (tmp_path / "repair-observation.md").stat().st_mode & 0o777 == 0o600


def test_unparsed_previous_candidate_has_no_successful_loss_observation():
    scenario, _, candidate, _, _ = candidates()
    model_input = replace(
        scenario.model_input,
        prompt=json.dumps({"validation_repair": {"previous_candidate": "{invalid"}}),
    )
    assert observe_repair(model_input, candidate, candidate)["status"] == "unavailable"
