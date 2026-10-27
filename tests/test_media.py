import json
from pathlib import Path

import pytest
from test_inspector import URL, document

from gformbot import ai
from gformbot.config import config_from_dict, config_from_schema, config_to_dict
from gformbot.generator import validate_answer
from gformbot.inspector import parse_form_html, schema_fingerprint


def media_form():
    items = json.loads((Path(__file__).parent / "fixtures/google-media.json").read_text(encoding="utf-8"))[
        "items"
    ]
    return parse_form_html(document(items), URL)


def test_real_media_order_attachments_and_roundtrip():
    form = media_form()
    assert form.media and form.media[0]["placement"] == "item"
    assert any(m["placement"] == "question" for m in form.media)
    assert any(m["kind"] == "video" for m in form.media)
    assert len({m["key"] for m in form.media}) == len(form.media)
    config = config_from_schema(form)
    assert config_from_dict(config_to_dict(config)).media == form.media
    fingerprint = schema_fingerprint(config)
    config.media_context = "The user reviewed these assets and declares them decorative."
    assert schema_fingerprint(config) == fingerprint
    config.media[0]["asset_id"] += "changed"
    assert schema_fingerprint(config) != fingerprint


def test_generation_needs_explicit_interpretation_not_for_supplied_answers(monkeypatch):
    config = config_from_schema(media_form())
    config.questions = [q for q in config.questions if q.options]
    monkeypatch.setattr(ai, "_chat", lambda *a, **k: pytest.fail("Unresolved media reached AI"))
    with pytest.raises(ai.GenerationError, match="media"):
        ai.generate_responses(config, 1)
    assert validate_answer(config, {q.index: q.options[0] for q in config.questions})


def test_option_media_retains_placement():
    from test_inspector import item

    raw = item(1, "Image choices", 2, ["A", "B"])
    raw[4][0][1][1].append(["asset-id", 0, [100, 100, 0]])
    form = parse_form_html(document([raw]), URL)
    assert form.media[0]["placement"] == "option"
    assert form.media[0]["option_index"] == 1
    assert form.media[0]["asset_id"] == "asset-id"


def test_explicit_decorative_context_reaches_generation(monkeypatch):
    from types import SimpleNamespace

    config = config_from_schema(media_form())
    config.media_context = "I reviewed the media; it is decorative for these synthetic test answers."
    requests = []
    monkeypatch.setattr(ai, "_open_chat", lambda *a, **k: SimpleNamespace(quit=lambda: None))

    def chat(driver, messages, *args, **kwargs):
        requests.append(json.loads(messages[-1]["content"]))
        return json.dumps({"responses": [{str(q.index): 1 for q in config.questions}]})

    monkeypatch.setattr(ai, "_chat", chat)
    assert ai.generate_responses(config, 1)
    assert requests[0]["user_media_context"] == config.media_context
    assert requests[0]["media"] == config.media
