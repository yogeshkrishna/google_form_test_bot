"""Exercise contact recognition, validation and draft-backed duplicate repair."""

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from gformbot import ai, chatgpt_http, cli, previews
from gformbot.ai_draft import Draft
from gformbot.config import QuestionConfig, RunConfig
from gformbot.generation import Generation
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


def test_restoring_same_field_never_shares_a_contact_reserved_by_a_preset():
    config = _form(3)
    quality = Quality(config)
    quality.remember_contacts({3: "+1 303 555 0101"}, 1)
    quality.remember_contacts({"3": "00 1 (303) 555-0101"}, 2, synthetic=True)

    assert "used_synthetic_contacts" not in quality.brief(config.questions, include_contacts=True)
    with pytest.raises(ContactAnswerError, match="another response") as caught:
        quality.accept_contacts({3: "+1 303 555 0101"}, 3)
    assert not caught.value.duplicates


def test_stubborn_duplicate_phone_does_not_block_later_profiles(monkeypatch):
    config = _form(6)
    checkpoints, requests = [], []
    fixed = False

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        rows = []
        for i in task["profile_numbers"]:
            requests.append(i)
            phone = 1 if i == 2 and not fixed else i
            row = {
                "1": f"Fixture Person {i}",
                "2": f"fixture.person84{i}@outlook.com",
                "3": f"+1 303 555 010{phone}",
                "4": "Denver",
            }
            rows.append(json.dumps({"profile_id": i, "answers": row}))
        return "\n".join(rows)

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    with pytest.raises(ai.GenerationError, match=r"5/6.*Response 2: Q3:.*another response") as caught:
        ai.generate_responses(config, 6, batch_size=2, checkpoint=checkpoints.append, quality=Quality(config))
    assert caught.value.completed == 5
    assert set(caught.value.partial_records) == {0, 2, 3, 4, 5}
    assert requests.count(2) == 3
    assert requests.count(6) == 1
    saved = checkpoints[-1]
    assert [profile["position"] for profile in saved["profiles"]] == [1, 0, 1, 1, 1, 1]

    fixed = True
    requests.clear()
    quality = Quality(config)
    for identity, profile in enumerate(saved["profiles"], 1):
        quality.remember_contacts(profile["answers"], identity)
    resumed = ai.generate_responses(config, 6, resume_state=saved, quality=quality)
    assert requests == [2]
    assert resumed[1][3] == "+1 303 555 0102"
    assert all(resumed[i] == value for i, value in caught.value.partial_records.items())


def test_duplicate_contact_repairs_avoid_each_generated_collision(monkeypatch):
    config = _form(3)
    original, another, replacement = "+1 303 555 0101", "+1 303 555 0102", "+1 303 555 0103"
    expected = [
        {"2": {"3": ["+13035550101"]}},
        {"2": {"3": ["+13035550101", "+13035550102"]}},
    ]
    repairs = []

    def ask(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        ids = task["profile_numbers"]
        if "repair_fields" in task:
            assert ids == [2]
            exclusions = task.get("avoid_contact_values")
            value = replacement if exclusions == expected[-1] else another
            repairs.append(exclusions)
            return json.dumps({"profile_id": 2, "answers": {"3": value}})
        return "\n".join(
            json.dumps(
                {
                    "profile_id": number,
                    "answers": {
                        "1": f"Fixture Person {number}",
                        "2": f"fixture.person84{number}@outlook.com",
                        "3": another if number == 3 else original,
                        "4": "Denver",
                    },
                }
            )
            for number in ids
        )

    monkeypatch.setattr(chatgpt_http.Client, "ask", ask)
    rows = ai.generate_responses(config, 3, quality=Quality(config))
    assert repairs == expected
    assert [row[3] for row in rows] == [original, replacement, another]


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
            assert "used_synthetic_contacts" not in task["generation_brief"]
            results = {2: first, 3: second}
        else:
            assert all("another response used this one" in message for message in task["corrections"])
            assert "avoid_contact_values" not in task
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


@pytest.mark.parametrize("guided", [False, True])
def test_resume_restores_only_known_synthetic_contact_guidance(monkeypatch, guided):
    config = _form(2)
    saved = {1: "Fixture One", 2: "supplied.contact@example.org", 3: "+1 303 555 0101", 4: "Denver"}
    candidate = {1: "Fixture Two", 2: "fixture.two937@outlook.com", 3: "00 1 (303) 555-0101", 4: "Boulder"}
    reference = [saved] if guided else None

    def interrupt(self, messages, *, timeout):
        exc = KeyboardInterrupt()
        exc.partial_content = json.dumps({"profile_id": 1, "answers": saved})
        raise exc

    monkeypatch.setattr(chatgpt_http.Client, "ask", interrupt)
    job = Generation(config, reference, "", partials=[{2: saved[2]}, {}])
    with pytest.raises(KeyboardInterrupt):
        job.run()

    draft = Draft.read(job.draft.path)
    turns = []

    def finish(self, messages, *, timeout):
        task = json.loads(messages[-1]["content"])
        assert task["profile_numbers"] == [2]
        assert saved[2] not in str(task["generation_brief"])
        known = None if guided else {"3": ["+13035550101"]}
        assert task["generation_brief"].get("used_synthetic_contacts") == known
        if "repair_fields" in task:
            assert task["repair_fields"] == {"2": ["3"]}
            assert task.get("avoid_contact_values") == (None if guided else {"2": known})
            turns.append("repair")
            return '{"profile_id":2,"answers":{"3":"720 555 0102"}}'
        turns.append("generate")
        return json.dumps({"profile_id": 2, "answers": candidate})

    monkeypatch.setattr(chatgpt_http.Client, "ask", finish)
    resumed = Generation(
        draft.config, draft.reference, draft.instructions, partials=draft.partials, draft=draft
    )
    assert resumed.run() == [saved, {**candidate, 3: "720 555 0102"}]
    assert turns == ["generate", "repair"]


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
