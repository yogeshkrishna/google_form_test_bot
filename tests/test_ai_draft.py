import json
import shutil
from dataclasses import replace

import pytest

from gformbot import ai_draft
from gformbot.ai_draft import Draft
from gformbot.config import QuestionConfig, RunConfig


@pytest.fixture
def config():
    return RunConfig(
        url="https://docs.google.com/forms/d/e/test/viewform?usp=header",
        count=2,
        questions=[QuestionConfig(1, "Name", "text", required=True, entry_id="123")],
    )


def progress(value="Alice"):
    return {
        "profiles": [
            {"answers": {"1": value}, "position": 1, "route": [1]},
            {"answers": {}, "position": 0, "route": None},
        ]
    }


def flat_progress(value="Alice", *, reviewed=False):
    return {
        "strategy": "flat",
        "profiles": [
            {"answers": {"1": value}, "position": 1, "route": None},
            {"answers": {}, "position": 0, "route": None},
        ],
        "review": {
            "checked": [1] if reviewed else [],
            "issues": [{"response": 1, "question": 1, "reason": "Check name", "blocking": False}]
            if reviewed
            else [],
        },
        "setup": {"facts": {"1": {"name": value}}},
    }


@pytest.mark.parametrize(
    ("second_route", "suffix"),
    [([1], "1/5 question groups saved"), (None, "1 question group saved")],
)
def test_partition_draft_progress_includes_saved_groups(config, second_route, suffix):
    config.sections = ["Start", "Follow-up"]
    config.questions += [
        QuestionConfig(2, "Choice", "radio", options=["Finish", "Continue"], page=1),
        QuestionConfig(3, "Notes", "textarea", page=2),
    ]
    state = progress()
    state["profiles"][0]["route"] = [1, 2]
    state["profiles"][1]["route"] = second_route
    draft = Draft.create(config, None, "", None, state)
    assert Draft.read(draft.path).progress_label() == f"0/2 generated · {suffix}"


def test_roundtrip_freezes_inputs_and_preserves_identity(config, tmp_path):
    reference = [{1: "参考"}]
    state = progress()
    draft = Draft.create(config, reference, "Use fictional names", None, state)
    reference[0][1] = "changed"
    state["profiles"][0]["answers"]["1"] = "changed"
    loaded = Draft.load(draft.path, replace(config, url=config.url.split("?")[0]))
    assert loaded.reference == [{1: "参考"}]
    assert loaded.state == progress()
    assert loaded.config.count == 2
    assert loaded.instructions == "Use fictional names"
    assert "responses" not in json.loads(draft.path.read_text(encoding="utf-8"))
    copy = tmp_path / "copied.json"
    shutil.copyfile(draft.path, copy)
    copied = Draft.load(copy, config)
    assert copied.identity == draft.identity
    copied.finish()
    assert copy.exists()
    assert draft.path.exists()
    draft.finish()
    assert not draft.path.exists()
    assert copy.exists()


def test_save_and_stale_writer(config):
    draft = Draft.create(config, None, "", None, progress())
    stale = Draft.load(draft.path, config)
    draft.save(progress("Bob"))
    assert Draft.load(draft.path, config).state == progress("Bob")
    with pytest.raises(ValueError, match="another process"):
        stale.save(progress("Carol"))
    with pytest.raises(ValueError, match="another process"):
        stale.finish()
    assert Draft.load(draft.path, config).state == progress("Bob")


def test_interrupted_write_preserves_previous_snapshot(config, monkeypatch):
    draft = Draft.create(config, None, "", None, progress())
    before = draft.path.read_bytes()

    def failed_write(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(ai_draft, "atomic_json", failed_write)
    with pytest.raises(OSError, match="disk failure"):
        draft.save(progress("Bob"))
    assert draft.path.read_bytes() == before
    assert Draft.load(draft.path, config).state == progress()


def test_flat_checkpoint_round_trip_keeps_review_progress_and_options(config):
    options = {"browser": "chrome", "quality": {"review": True}}
    draft = Draft.create(config, None, "", None, flat_progress(), options=options)
    options["quality"]["review"] = False
    draft.options["quality"]["review"] = False
    draft.save(flat_progress(reviewed=True))
    resumed = Draft.load(draft.path, config)
    assert resumed.identity == draft.identity
    assert resumed.strategy == "flat"
    assert resumed.options == {"browser": "chrome", "quality": {"review": True}}
    assert resumed.state == flat_progress(reviewed=True)
    assert json.loads(draft.path.read_bytes())["version"] == 2
    with pytest.raises(ValueError, match="strategy changed"):
        resumed.save(progress())


def test_partition_migration_preserves_completed_answers_review_and_presets(config, monkeypatch):
    from gformbot.ai_partition import _restore, _units
    from gformbot.routing import Routing

    form = replace(
        config,
        sections=["Choice", "Optional section", "Finish"],
        questions=[
            QuestionConfig(
                1, "Route", "radio", required=True, options=["Skip", "Continue"], branches={"Skip": 3}
            ),
            QuestionConfig(2, "Note", "text", page=2),
            QuestionConfig(3, "Result", "text", required=True, page=3, fixed_value="Ready"),
        ],
    )
    accepted = {"1": "Skip", "2": "Saved even though the route skips this section", "3": "Ready"}
    state = flat_progress(reviewed=True)
    state["profiles"][0]["answers"] = accepted
    state["setup"]["repair_attempts"] = {"1": 1}
    draft = Draft.create(form, None, "", [{}, {2: "Keep this preset"}], state, options={"browser": "chrome"})
    stale = Draft.load(draft.path, form)
    previous = draft.path.read_bytes()
    profiles = ai_draft.partition_flat_profiles(form, draft.state["profiles"], draft.partials)
    assert profiles == [
        {"answers": accepted, "position": 2, "route": [1, 3]},
        {"answers": {"2": "Keep this preset", "3": "Ready"}, "position": 0, "route": None},
    ]
    invalid = [{**profiles[0], "answers": {**accepted, "2": "Changed"}}, profiles[1]]
    with pytest.raises(ValueError, match="strategy changed"):
        draft.save({**state, "strategy": "partition", "profiles": invalid})
    assert draft.path.read_bytes() == previous
    migrated = {**state, "strategy": "partition", "profiles": profiles}

    def fail_write(*args, **kwargs):
        raise OSError("simulated write interruption")

    with monkeypatch.context() as patch:
        patch.setattr(ai_draft, "atomic_json", fail_write)
        with pytest.raises(OSError, match="write interruption"):
            draft.save(migrated)
    assert draft.path.read_bytes() == previous
    assert draft.strategy == "flat"

    draft.save(migrated)
    assert draft.strategy == "partition"
    current = Draft.load(draft.path, form)
    assert current.identity == draft.identity
    assert current.state == migrated
    assert current.options == {"browser": "chrome"}
    rows, positions, paths = _restore(
        form,
        {"profiles": current.state["profiles"]},
        [{3: "Ready"}, {2: "Keep this preset", 3: "Ready"}],
        Routing(form),
        _units(form),
    )
    assert rows == [{int(key): value for key, value in accepted.items()}, {2: "Keep this preset", 3: "Ready"}]
    assert positions == [2, 0]
    assert paths == [(1, 3), None]
    with pytest.raises(ValueError, match="another process"):
        stale.save(migrated)


def test_legacy_partition_draft_migrates_on_save_with_same_identity(config):
    draft = Draft.create(config, None, "", None, progress())
    old = draft.path.read_bytes()
    assert json.loads(old)["version"] == 1
    legacy = Draft.load(draft.path, config)
    assert legacy.strategy == "partition"
    assert legacy.options == {}
    assert legacy.state == progress()
    assert legacy.path.read_bytes() == old
    state = {**legacy.state, "strategy": "partition", "review": {"checked": [], "issues": []}, "setup": {}}
    legacy.save(state)
    resumed = Draft.load(legacy.path, config)
    assert json.loads(resumed.path.read_bytes())["version"] == 2
    assert resumed.identity == draft.identity
    assert resumed.state == state


@pytest.mark.parametrize(
    "review",
    [
        {"checked": [1, 1], "issues": []},
        {"checked": [2], "issues": []},
        {"checked": [True], "issues": []},
        {"checked": [], "issues": [{"response": 3, "question": 1, "reason": "invalid response"}]},
        {"checked": [], "issues": [{"response": 1, "question": 2, "reason": "unknown question"}]},
        {"checked": [], "issues": [{"response": 1, "question": None, "reason": "  "}]},
        {"checked": [], "issues": [{"response": 1, "question": 1, "reason": "flag", "blocking": 1}]},
    ],
)
def test_invalid_review_state_never_replaces_previous_snapshot(config, review):
    draft = Draft.create(config, None, "", None, flat_progress())
    previous = draft.path.read_bytes()
    with pytest.raises(ValueError, match="AI draft review"):
        draft.save({**flat_progress(), "review": review})
    assert draft.path.read_bytes() == previous


def test_snapshot_fits_cap_on_disk_and_oversize_update_keeps_previous(config, monkeypatch):
    state = flat_progress("Alice" * 1_000, reviewed=True)
    draft = Draft.create(config, None, "", None, state)
    payload = json.loads(draft.path.read_bytes())
    canonical_size = len(ai_draft._encode(payload))
    monkeypatch.setattr(ai_draft, "_MAX_BYTES", canonical_size)
    draft.save(state)
    previous = draft.path.read_bytes()
    assert len(previous) <= canonical_size
    with pytest.raises(ValueError, match="size limit"):
        draft.save(flat_progress("Alice" * 2_000, reviewed=True))
    assert Draft.load(draft.path, config).state == state
    assert draft.path.read_bytes() == previous


def test_rejects_changed_form_and_instruction_context(config):
    draft = Draft.create(config, None, "", None, progress())
    with pytest.raises(ValueError, match="form or its instructions changed"):
        Draft.load(draft.path, replace(config, questions=[replace(config.questions[0], text="Changed")]))
    with pytest.raises(ValueError, match="form or its instructions changed"):
        Draft.load(
            draft.path,
            replace(
                config, instructions=[{"page": 1, "before_question": 1, "title": "Read", "description": "X"}]
            ),
        )
    with pytest.raises(ValueError, match="form or its instructions changed"):
        Draft.load(draft.path, replace(config, url=config.url.replace("test/", "another/")))


def test_checksum_rejects_changed_saved_answers(config):
    draft = Draft.create(config, None, "", None, progress())
    payload = json.loads(draft.path.read_text(encoding="utf-8"))
    payload["state"]["profiles"][0]["answers"]["1"] = "Tampered"
    draft.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="changed or is damaged"):
        Draft.load(draft.path, config)


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"profiles": []},
        {"profiles": [{"answers": {"99": "Unknown"}, "position": 1, "route": [1]}] * 2},
        {"profiles": [{"answers": {"1": 3}, "position": 1, "route": [1]}] * 2},
        {"profiles": [{"answers": {}, "position": True, "route": [1]}] * 2},
        {"profiles": [{"answers": {}, "position": 1, "route": None}] * 2},
        {"profiles": [{"answers": {}, "position": 0, "route": [2]}] * 2},
    ],
)
def test_rejects_invalid_progress(config, state):
    with pytest.raises(ValueError, match="AI draft"):
        Draft.create(config, None, "", None, state)


def test_partial_answers_restore_integer_keys(config):
    partials = [{1: "Alice"}, {}]
    draft = Draft.create(config, None, "", partials, progress())
    assert Draft.load(draft.path, config).partials == partials


@pytest.mark.parametrize("text", ["{}", '{"a":1,"a":2}', "[]", "", "not json"])
def test_rejects_unrelated_or_malformed_files(config, tmp_path, text):
    path = tmp_path / "unrelated.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        Draft.load(path, config)
    assert path.read_text(encoding="utf-8") == text


def test_resource_limit(config, monkeypatch):
    draft = Draft.create(config, None, "", None, progress())
    monkeypatch.setattr(ai_draft, "_MAX_BYTES", 30)
    with pytest.raises(ValueError, match="size limit"):
        Draft.load(draft.path, config)


def test_identity_is_usable_by_submission_journal(config):
    from gformbot.state import Journal

    draft = Draft.create(config, None, "", None, progress())
    journal = Journal(draft.identity)
    # Save a final batch under the draft identity, as the CLI does.
    plan = {"batch_id": draft.identity, "form_fingerprint": "schema", "responses": [{"1": "Alice"}]}
    journal.bind(plan)
    journal.set(0, "confirmed")
    journal.set(1, "uncertain")
    resumed = Draft.load(draft.path, config)
    assert Journal(resumed.identity).states() == {0: "confirmed", 1: "uncertain"}
