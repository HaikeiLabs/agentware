"""Replay cross-port progress fixtures against the Python implementation."""

import json
from pathlib import Path

import pytest

from haikeilabs.agentware.middleware.guardrails.progress import ProgressTracker


def test_shared_fixture():
    path = Path(__file__).resolve().parents[2] / "fixtures/workflow/progress.v1.json"
    data = json.loads(path.read_text())
    tracker = ProgressTracker(data["required"], data["max_no_progress"])
    for event in data["events"]:
        result = (
            tracker.failure()
            if event["kind"] == "failure"
            else tracker.observe(event["key"], event["present"])
        )
        assert list(result.pending) == event["pending"]
        assert result.ready == event["ready"]
        assert result.no_progress == event["no_progress"]
        assert result.exhausted == event["exhausted"]


def test_invalid_config_and_unknown_requirement():
    for items in ([], [""], ["a", "a"]):
        with pytest.raises(ValueError):
            ProgressTracker(items)
    with pytest.raises(ValueError):
        ProgressTracker(["a"], -1)
    with pytest.raises(ValueError):
        ProgressTracker(["a"]).observe("not-a")


def test_observation_batch_does_not_partially_apply_invalid_data():
    tracker = ProgressTracker(["first", "second"])
    with pytest.raises(ValueError):
        tracker.observe_many([("first", True), ("unknown", True)])
    assert tracker.snapshot().pending == ("first", "second")
    with pytest.raises(TypeError):
        tracker.observe_many([("first", True), ("second", "yes")])
    assert tracker.snapshot().pending == ("first", "second")
    assert tracker.observe_many([("first", True), ("second", True)]).ready


def test_duplicate_keys_in_observation_batch_are_rejected_atomically():
    tracker = ProgressTracker(["a"])
    with pytest.raises(ValueError):
        tracker.observe_many([("a", True), ("a", False)])
    assert tracker.snapshot().pending == ("a",)
