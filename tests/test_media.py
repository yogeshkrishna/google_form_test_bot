import json
from pathlib import Path

from test_inspector import URL, document

from gformbot import ai
from gformbot.config import config_from_dict, config_from_schema, config_to_dict
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
    fingerprint = schema_fingerprint(config)
    legacy = {**config_to_dict(config), "media_context": "Old media instructions"}
    restored = config_from_dict(legacy)
    assert restored.media == form.media
    assert "media_context" not in config_to_dict(restored)
    assert schema_fingerprint(restored) == fingerprint
    config.media[0]["asset_id"] += "changed"
    assert schema_fingerprint(config) != fingerprint


def test_option_media_retains_placement():
    from test_inspector import item

    raw = item(1, "Image choices", 2, ["A", "B"])
    raw[4][0][1][1].append(["asset-id", 0, [100, 100, 0]])
    form = parse_form_html(document([raw]), URL)
    assert form.media[0]["placement"] == "option"
    assert form.media[0]["option_index"] == 1
    assert form.media[0]["asset_id"] == "asset-id"


def test_generation_only_sends_media_facts_the_user_included_in_instructions(monkeypatch):
    from types import SimpleNamespace

    config = config_from_schema(media_form())
    config.media[0]["asset_id"] = "private-media-identity"
    config.media[0]["caption"] = "private-media-caption"
    instructions = "The image shows a red circle."
    requests = []
    monkeypatch.setattr(ai, "_open_chat", lambda *a, **k: SimpleNamespace(quit=lambda: None))

    def chat(driver, messages, *args, **kwargs):
        requests.extend(message["content"] for message in messages)
        return json.dumps({"profile_id": 1, "answers": {str(q.index): 1 for q in config.questions}})

    monkeypatch.setattr(ai, "_chat", chat)
    assert ai.generate_responses(config, 1, instructions=instructions)
    task = json.loads(requests[-1])
    assert task["instructions"] == instructions
    assert not {"media", "user_media_context"} & task.keys()
    assert all("private-media-" not in message for message in requests)
