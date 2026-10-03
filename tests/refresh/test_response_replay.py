import json
from pathlib import Path
from shutil import copytree

import pytest

from scripts.evaluate_refresh import main
from tests.refresh.test_evaluation_runner import arguments

RESPONSES = Path(__file__).resolve().parents[2] / "evaluation/responses/assistant-v1"


def test_conversation_responses_use_real_parser_rules_and_never_openai(
    tmp_path, monkeypatch
):
    import scripts.evaluate_refresh as cli

    def forbidden(**kwargs):
        raise AssertionError("Replay must not instantiate OpenAI")

    monkeypatch.setattr(cli, "OpenAI", forbidden)
    target = tmp_path / "replay"
    assert (
        main(arguments(target) + ["--responses-dir", str(RESPONSES), "--repeat-saved"])
        == 0
    )
    for name in ("document", "relation", "no-change", "both", "existing", "long-span"):
        report = json.loads((target / f"{name}.json").read_text())
        assert report["response_source"] == "conversation-assistant"
        assert not report["live"]
        assert report["assessment"]["rules_accepted"]
        assert not report["assessment"]["missing_targets"]
        assert report["assessment"]["expected_relations"]
    record = json.loads((target / "long-span-attempt-1.json").read_text())
    assert record["usage"] is None
    assert not record["remote_requested"]
    assert record["raw_response"] is None
    assert record["raw_candidate_text"]
    assert record["candidate"]["document_proposals"][0]["evidence"][0]["start"] == 4800


@pytest.mark.parametrize(
    "field,value",
    [("input_fingerprint", "wrong"), ("schema_version", "old"), ("target_ids", ["i1"])],
)
def test_mismatched_saved_response_fails(tmp_path, field, value):
    source = tmp_path / "responses"
    copytree(RESPONSES, source)
    file = source / "document--c1.json"
    data = json.loads(file.read_text())
    data[field] = value
    file.write_text(json.dumps(data))
    target = tmp_path / "run"
    assert (
        main(arguments(target) + ["--responses-dir", str(source), "--case", "document"])
        == 1
    )
    report = json.loads((target / "document.json").read_text())
    assert report["result"]["error"]["code"] == "REPLAY_INPUT_MISMATCH"


def test_invalid_span_is_rejected_without_repair(tmp_path):
    source = tmp_path / "responses"
    copytree(RESPONSES, source)
    file = source / "long-span--c1.json"
    data = json.loads(file.read_text())
    candidate = json.loads(data["response_text"])
    candidate["document_proposals"][0]["evidence"][0]["start"] = 4799
    data["response_text"] = json.dumps(candidate)
    file.write_text(json.dumps(data))
    target = tmp_path / "run"
    assert (
        main(
            arguments(target) + ["--responses-dir", str(source), "--case", "long-span"]
        )
        == 1
    )
    record = json.loads((target / "long-span-attempt-1.json").read_text())
    assert record["candidate"]["document_proposals"][0]["evidence"][0]["start"] == 4799
    report = json.loads((target / "long-span.json").read_text())
    assert report["result"]["outcome"] == "FAILED"
    assert report["result"]["document_proposals"] == []
