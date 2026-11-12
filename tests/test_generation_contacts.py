"""Exercise contact recognition, validation and draft-backed duplicate repair."""

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from gformbot import chatgpt_http, cli, previews
from gformbot.ai_draft import Draft
from gformbot.config import QuestionConfig, RunConfig
from gformbot.generation_contacts import ContactAnswerError, contact_identity, contact_kind, contact_problem
from gformbot.generation_quality import Quality


@pytest.mark.parametrize(
    ("title", "description", "kind"),
    [
        ("S03-Q04 — What is your e-mail address?", "", "email"),
        ("Please provide your mobile number (local format)", "", "phone"),
        ("Contact Number", "", "phone"),
        ("Phone extension", "", None),
        ("Number of phones", "", None),
        ("Email for a fictional account", "Use a made-up example.com address.", None),
    ],
)
def test_identifies_single_contact_fields(title, description, kind):
    question = QuestionConfig(1, title, "text", description=description)
    assert contact_kind(question) == kind


@pytest.mark.parametrize(
    ("kind", "value", "identity", "valid"),
    [
        ("email", "Fixture.User452+alias@gmail.com", "fixtureuser452@gmail.com", True),
        ("email", "FixtureUser452@googlemail.com", "fixtureuser452@gmail.com", True),
        ("email", "fixture.user452@yahoo.com", "fixture.user452@yahoo.com", True),
        ("email", "fixture.user452@outlook.com", "fixture.user452@outlook.com", True),
        ("email", "fixture_user@gmail.com", "fixture_user@gmail.com", False),
        ("email", "name@bakery-test.example", "name@bakery-test.example", False),
        ("phone", "+1 (303) 555-0101", "+13035550101", True),
        ("phone", "00 1 (303) 555-0101", "+13035550101", True),
        ("phone", "303 555 0101", "3035550101", True),
        ("phone", "+44 7700 900123", "+447700900123", True),
        ("phone", "0044 7700 900123", "+447700900123", True),
        ("phone", "+91 98450 12348", "+919845012348", True),
        ("phone", "+91 1234567890", "+911234567890", False),
        ("phone", "+298 221 783", "+298221783", True),
        ("phone", "＋２９８ ２２１－７８３", "+298221783", True),
        ("phone", "221 783", "221783", True),
        ("phone", "+39 02 7621 9846", "+390276219846", True),
        ("phone", "1212121212", "1212121212", False),
        ("phone", "+0 103 555 0101", "+01035550101", False),
        ("phone", "1234567890", "1234567890", False),
        ("phone", "7777777777", "7777777777", False),
        ("phone", "", None, True),
        ("email", None, None, True),
    ],
)
def test_validates_and_canonicalizes_contacts(kind, value, identity, valid):
    assert contact_identity(kind, value) == identity
    assert (contact_problem(kind, value) is None) == valid


def _form(count):
    return RunConfig(
        "https://docs.google.com/forms/d/e/contact-fixture/viewform",
        "Contact fixture",
        count=count,
        questions=[
            QuestionConfig(1, "Full name", "text", True),
            QuestionConfig(2, "Email address", "text", True),
            QuestionConfig(3, "Phone number", "text"),
            QuestionConfig(4, "City", "text", True),
        ],
    )


def test_rejected_profile_does_not_claim_its_other_contact():
    quality = Quality(_form(3))
    quality.accept_contacts({2: "fixture.a243@gmail.com", 3: "303 555 0101"}, 1)
    with pytest.raises(ContactAnswerError) as duplicate:
        quality.accept_contacts({2: "fixturea243@gmail.com", 3: "303 555 0101"}, 2)
    assert duplicate.value.keys == ("2", "3")
    with pytest.raises(ContactAnswerError, match=r"Q3:.*another response"):
        quality.accept_contacts({2: "fixture.b532@outlook.com", 3: "(303) 555-0101"}, 2)
    quality.accept_contacts({2: "fixture.b532@outlook.com", 3: ""}, 3)


def test_phone_duplicate_detection_keeps_calling_codes():
    quality = Quality(_form(3))
    quality.accept_contacts({3: "+298 221 783"}, 1)
    quality.accept_contacts({3: "+299 221 783"}, 2)
    with pytest.raises(ContactAnswerError, match="another response"):
        quality.accept_contacts({3: "00 298 221783"}, 3)


def test_resume_repairs_only_contacts_that_repeat_saved_values(tmp_path, monkeypatch):
    config = _form(3)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(previews, "index_path", lambda: tmp_path / "state" / "previews.json")
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **k: deepcopy(config))
    saved = {"1": "Dora Lane", "2": "fixture.dora584@gmail.com", "3": "+1 303 555 0101", "4": "Denver"}
    draft = Draft.create(
        config,
        None,
        "",
        None,
        {
            "strategy": "flat",
            "profiles": [{"answers": saved, "position": 1, "route": None}]
            + [{"answers": {}, "position": 0, "route": None} for _ in range(2)],
            "review": {"checked": [], "issues": []},
            "setup": {},
        },
        options={"browser": None},
    )
    first = {"1": "Cora Vale", "2": "FixtureDora584+orders@gmail.com", "3": "", "4": "Boulder"}
    second = {
        "1": "Perry Dale",
        "2": "fixture.perry829@outlook.com",
        "3": "00 1 (303) 555-0101",
        "4": "Aurora",
    }
    repairs = {2: {"2": "fixture.cora751@yahoo.com"}, 3: {"3": "720 555 0102"}}
    attempted_repairs = set()

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        ids = task["profile_numbers"]
        if "corrections" not in task:
            assert ids == [2, 3]
            assert "Gmail, Outlook or Yahoo" in str(task["generation_brief"])
            results = {2: first, 3: second}
        else:
            assert all("another response used this one" in message for message in task["corrections"])
            assert task["repair_fields"] == {str(i): list(repairs[i]) for i in ids}
            context = task["repair_context"]
            assert context["questions"]["1"] == "Full name"
            assert context["questions"]["4"] == "City"
            assert all(
                context["profiles"][str(i)][key] == {2: first, 3: second}[i][key]
                for i in ids
                for key in ("1", "4")
            )
            attempted_repairs.update(ids)
            results = repairs
        return "\n".join(json.dumps({"profile_id": i, "answers": results[i]}) for i in ids)

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = CliRunner().invoke(cli.cli, ["prepare", config.url, "--resume", str(draft.path)])
    assert result.exit_code == 0, result.exception
    assert attempted_repairs == {2, 3}
    (plan_path,) = tmp_path.glob("gformbot-responses*.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["batch_id"] == draft.identity
    assert plan["responses"] == [saved, {**first, **repairs[2]}, {**second, **repairs[3]}]


def test_partition_resume_deduplicates_across_original_profile_ids(tmp_path, monkeypatch):
    config = _form(3)
    config.questions = config.questions[:2]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(previews, "index_path", lambda: tmp_path / "state" / "previews.json")
    monkeypatch.setattr(cli, "_run_inspect", lambda *a, **k: deepcopy(config))
    saved = {"1": "Dora Lane", "2": "fixture.dora584@gmail.com"}
    draft = Draft.create(
        config,
        None,
        "",
        None,
        {
            "strategy": "partition",
            "profiles": [
                {"answers": saved, "position": 2, "route": [1]},
                {"answers": {"1": "Cora Vale"}, "position": 1, "route": [1]},
                {"answers": {"1": "Perry Dale"}, "position": 1, "route": [1]},
            ],
            "review": {"checked": [], "issues": []},
            "setup": {},
        },
        options={"browser": None},
    )
    repaired = "fixture.cora751@yahoo.com"
    distinct = "fixture.perry829@outlook.com"
    attempts = []

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        assert [q["key"] for q in task["questions"]] == ["2"]
        fixes = task.get("repair_fields", {})
        replies = []
        for local in task["profile_numbers"]:
            original = task["profile_context"][str(local)]["response_number"]
            correction = str(local) in fixes
            attempts.append((original, correction))
            if correction:
                assert original == 2
                assert fixes[str(local)] == ["2"]
                assert "another response used this one" in str(task["corrections"])
            value = distinct if original == 3 else repaired if correction else "FixtureDora584@gmail.com"
            replies.append(json.dumps({"profile_id": local, "answers": {"2": value}}))
        return "\n".join(replies)

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    result = CliRunner().invoke(cli.cli, ["prepare", config.url, "--resume", str(draft.path)])
    assert result.exit_code == 0, result.exception
    assert sorted(attempts) == [(2, False), (2, True), (3, False)]
    (plan_path,) = tmp_path.glob("gformbot-responses*.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["batch_id"] == draft.identity
    assert plan["responses"] == [saved, {"1": "Cora Vale", "2": repaired}, {"1": "Perry Dale", "2": distinct}]
