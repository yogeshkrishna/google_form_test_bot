import json
import threading
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from test_inspector import URL, document

from gformbot import ai, chatgpt_http, cli, inspector
from gformbot.config import config_from_dict, config_from_schema, config_to_dict
from gformbot.inspector import parse_form_html, schema_fingerprint
from gformbot.media import media_links


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


def test_form_inspection_links_browser_media_without_saving_rendered_urls(monkeypatch):
    rendered_url = "https://docs.google.com/forms-images-rt/example-image"
    explicit_url = "https://lh3.googleusercontent.com/example-image"
    video_url = "https://www.youtube.com/watch?v=O9CY0VauxUs"
    markup = f'<form><div data-item-id="1"><img src="{rendered_url}"></div></form>'
    metadata = document(
        [
            [1, "Visible image", None, 11, None, None, ["opaque-id", 0]],
            [2, "Clip", None, 12, None, None, [None, 1, None, "O9CY0VauxUs"]],
            [3, "Next section", None, 8],
            [4, "Image with URL", None, 11, None, None, [None, 2, None, None, explicit_url]],
            [5, "Unresolved image", None, 11, None, None, ["opaque-later-id", 0]],
        ]
    )
    response = BytesIO((markup + metadata).encode())
    response.url, response.status, response.headers = URL, 200, Message()
    requests = []

    def fetch(request, **kwargs):
        requests.append(request.full_url)
        return response

    monkeypatch.setattr(inspector, "urlopen", fetch)
    monkeypatch.setattr(cli, "_INQUIRER", False)
    output = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=output, width=120, force_terminal=True, color_system="standard", legacy_windows=False),
    )
    config = cli._run_inspect(URL)

    display = output.getvalue()
    for url in (rendered_url, explicit_url, video_url):
        assert f";{url}\x1b\\" in display
    assert "Extra instructions" in display
    assert "Unresolved image" in display and "opaque-later-id" not in display
    assert "Unavailable" in display
    assert requests == [URL]
    assert rendered_url not in json.dumps(config_to_dict(config))
    assert schema_fingerprint(config) == schema_fingerprint(parse_form_html(metadata, URL))


def test_rendered_option_image_links_only_when_item_match_is_unique():
    from test_inspector import item

    raw = item(1, "Image choices", 2, ["A", "B"])
    raw[4][0][1][1].append(["asset-id", 0])
    rendered = "https://docs.google.com/forms-images-rt/choice-image"
    markup = f"""<form><div data-params='%.@.[101,"Image choices"]'>
        <img src="{rendered}"></div></form>"""
    form = parse_form_html(document([raw]), URL)
    assert media_links(form.media, markup) == {form.media[0]["key"]: rendered}

    raw[4][0][1][0].append(["another-asset-id", 0])
    form = parse_form_html(document([raw]), URL)
    assert media_links(form.media, markup) == {}


def test_option_image_does_not_use_a_link_scoped_to_a_different_choice():
    from test_inspector import item

    raw = item(1, "Image choices", 2, ["A", "B"])
    raw[4][0][1][1].append(["asset-id", 0])
    form = parse_form_html(document([raw]), URL)
    markup = """<form><div data-item-id="101">
        <div role="radio" data-value="A"><img src="https://docs.google.com/forms-images-rt/a"></div>
        </div></form>"""

    assert media_links(form.media, markup) == {}


def test_later_section_links_only_match_the_page_actually_rendered():
    markup = (
        '<form><div data-item-id="2"><img src="https://docs.google.com/forms-images-rt/later"></div></form>'
    )
    form = parse_form_html(
        document(
            [
                [1, "Later", None, 8],
                [2, "Later image", None, 11, None, None, ["opaque", 0]],
            ]
        ),
        URL,
    )
    assert media_links(form.media, markup) == {}
    assert media_links(form.media, markup, page=2) == {
        form.media[0]["key"]: "https://docs.google.com/forms-images-rt/later"
    }


def test_public_mp4_is_kept_as_a_direct_video_link():
    mp4 = "https://media.example/clip.MP4?v=2"
    assets = [
        {"key": "direct", "kind": "video", "asset_id": mp4},
        {"key": "page", "kind": "video", "asset_id": "https://media.example/watch"},
        {"key": "unsafe", "kind": "video", "asset_id": "https://user@media.example/clip.mp4"},
    ]
    assert media_links(assets, "") == {"direct": mp4}


def test_question_and_option_images_are_matched_by_control_identity():
    from test_inspector import item

    raw = item(1, "Image choices", 2, ["A", "B"])
    raw.extend([None] * (10 - len(raw)))
    raw[9] = [["question-image", 0]]
    for index, option in enumerate(raw[4][0][1]):
        option.append([f"option-{index}", 0])
    form = parse_form_html(document([raw]), URL)
    markup = """<form><div data-item-id="101">
        <img src="https://docs.google.com/forms-images-rt/question">
        <div role="radio" data-value="B"><img src="https://docs.google.com/forms-images-rt/b"></div>
        <div role="radio" data-value="A"><img src="https://docs.google.com/forms-images-rt/a"></div>
        </div></form>"""
    assert media_links(form.media, markup) == {
        "101:question": "https://docs.google.com/forms-images-rt/question",
        "101:1:option:0": "https://docs.google.com/forms-images-rt/a",
        "101:1:option:1": "https://docs.google.com/forms-images-rt/b",
    }


@pytest.mark.parametrize("rich_prompts", [True, False])
def test_wizard_inspection_starts_discovery_before_showing_form(monkeypatch, rich_prompts):
    from gformbot.media import MediaDiscovery

    form = media_form()
    form._media_document = "public responder HTML"
    events = []

    def start(discovery):
        assert discovery.document == form._media_document
        events.append("start")
        return discovery

    monkeypatch.setattr(inspector, "inspect_form", lambda url: form)
    monkeypatch.setattr(cli, "_INQUIRER", rich_prompts)
    monkeypatch.setattr(MediaDiscovery, "start", start)
    monkeypatch.setattr(cli, "_show_form", lambda config: events.append("show"))
    token = cli.wizard_navigation.set(True)
    try:
        config = cli._run_inspect(URL)
    finally:
        cli.wizard_navigation.reset(token)
    assert events == ["start", "show"]
    discovery = config._media_discovery
    cli._stop_media_discovery(config)
    assert discovery.stop.is_set() and not hasattr(config, "_media_discovery")
    cli._stop_media_discovery(config)


@pytest.mark.parametrize("outcome", [None, cli.PromptBack, KeyboardInterrupt])
def test_leaving_form_cancels_discovery_without_waiting(monkeypatch, outcome):
    events = []
    discovery = SimpleNamespace(snapshot=lambda: ({}, False), close=lambda: events.append("closed"))
    config = config_from_schema(media_form())
    config._media_discovery = discovery
    monkeypatch.setattr(cli, "_url_prompt", lambda: URL)
    monkeypatch.setattr(cli, "_run_inspect", lambda url, **kwargs: config)

    def choose(config):
        if outcome:
            raise outcome()

    monkeypatch.setattr(cli, "_form_choices", choose)
    if outcome is cli.PromptBack:
        # Returning to URL selection must already have cancelled the old worker.
        def url():
            if events:
                raise KeyboardInterrupt()
            return URL

        monkeypatch.setattr(cli, "_url_prompt", url)
    if outcome:
        with pytest.raises(KeyboardInterrupt):
            cli._new_batch_wizard()
    else:
        cli._new_batch_wizard()
    assert events == ["closed"]


def test_inspection_discovery_lives_until_the_next_menu_selection(monkeypatch):
    events = []
    config = config_from_schema(media_form())
    config._media_discovery = SimpleNamespace(
        snapshot=lambda: ({}, False), close=lambda: events.append("closed")
    )
    selections = iter(["inspect", "back"])

    def choose(*args, **kwargs):
        assert not events
        choice = next(selections)
        if choice == "back":
            assert kwargs["media"] is config
        return choice

    monkeypatch.setattr(cli, "_ask_select", choose)
    monkeypatch.setattr(cli, "_url_prompt", lambda: URL)
    monkeypatch.setattr(cli, "_run_inspect", lambda url: config)
    cli._more_options()
    assert events == ["closed"]


def test_answer_source_shows_media_in_the_prompt_without_opening_a_browser(monkeypatch):
    config = config_from_schema(media_form())

    def choose(message, choices, **kwargs):
        assert message == "Where should the answers come from?"
        assert kwargs["media"] is config
        raise cli.PromptBack

    monkeypatch.setattr(cli, "_ask_select", choose)
    monkeypatch.setattr(cli.click, "launch", lambda *a: pytest.fail("No browser expected"))
    with pytest.raises(cli.PromptBack):
        cli._form_choices(config)


def test_terminal_media_table_updates_partial_links_and_keeps_the_direct_targets(monkeypatch):
    from gformbot import media_navigation
    from gformbot.media import MediaDiscovery

    config = config_from_schema(media_form())
    image_key = next(asset["key"] for asset in config.media if asset["kind"] == "image")
    video_key = next(asset["key"] for asset in config.media if asset["kind"] == "video")
    image_url = "https://docs.google.com/forms-images-rt/public-image=w480"
    video_url = "https://www.youtube.com/watch?v=O9CY0VauxUs"
    entered, image_requested, image_published = threading.Event(), threading.Event(), threading.Event()
    finished = threading.Event()

    def discover(config, document, stop, publish):
        entered.set()
        if image_requested.wait(3):
            publish({image_key: image_url})
            image_published.set()
        if stop.wait(3):
            publish({video_key: "https://www.youtube.com/watch?v=abcdefghijk"})
        finished.set()

    monkeypatch.setattr(media_navigation, "discover", discover)
    discovery = config._media_discovery = MediaDiscovery(config, "", {video_key: video_url}).start()

    def display(*, plain_links=False):
        output = StringIO()
        terminal = Console(
            file=output, width=140, force_terminal=True, color_system="standard", legacy_windows=False
        )
        terminal.print(cli._media_table(config, plain_links=plain_links))
        return output.getvalue()

    try:
        assert entered.wait(3)
        before = display()
        assert "Loading" in before and "Open video" in before
        assert image_url not in before and f";{video_url}\x1b\\" in before
        image_requested.set()
        assert image_published.wait(3)
        during = display()
        assert "Open image" in during and f";{image_url}\x1b\\" in during
        assert image_url in display(plain_links=True)
    finally:
        cli._stop_media_discovery(config)
        image_requested.set()
    assert discovery.stop.is_set()
    assert finished.wait(3)
    assert discovery.snapshot()[0][video_key] == video_url
    assert "Unavailable" in display()


def test_terminal_media_link_highlights_on_hover_and_opens_on_plain_click(monkeypatch):
    import asyncio

    from prompt_toolkit.application import Application, set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType, MouseModifier
    from prompt_toolkit.output import DummyOutput

    url = "https://docs.google.com/forms-images-rt/public-image=w480"
    config = SimpleNamespace(
        media=[{"key": "photo", "kind": "image", "caption": "Photo", "page": 1}],
        _media_links={"photo": url},
    )
    launched = []

    def launch(link):
        launched.append(link)
        return 0

    monkeypatch.setattr(cli.click, "launch", launch)
    app = Application(layout=Layout(Window()), input=DummyInput(), output=DummyOutput())
    cli._attach_media(app, config)
    (control,) = (
        window.content
        for window in app.layout.find_all_windows()
        if isinstance(window.content, FormattedTextControl)
    )
    assert app.renderer.mouse_support()

    def fragment_at(label):
        app.render_counter += 1
        content = control.create_content(80, 40)
        for y in range(content.line_count):
            x = 0
            for style, text, *_ in content.get_line(y):
                if label in text:
                    return Point(x + text.index(label), y), style
                x += len(text)
        pytest.fail(f"Missing media prompt text: {label}")

    def mouse(point, event, button=MouseButton.NONE, modifiers=frozenset()):
        control.mouse_handler(MouseEvent(point, event, button, modifiers))

    async def interact():
        with set_app(app):
            link, style = fragment_at("Open image")
            assert "reverse" not in style
            mouse(link, MouseEventType.MOUSE_MOVE)
            assert "reverse" in fragment_at("Open image")[1]
            caption, _ = fragment_at("Photo")
            mouse(caption, MouseEventType.MOUSE_MOVE)
            assert "reverse" not in fragment_at("Open image")[1]

            mouse(link, MouseEventType.MOUSE_DOWN, MouseButton.LEFT)
            mouse(caption, MouseEventType.MOUSE_UP, MouseButton.LEFT)
            ctrl = frozenset({MouseModifier.CONTROL})
            mouse(link, MouseEventType.MOUSE_DOWN, MouseButton.LEFT, ctrl)
            mouse(link, MouseEventType.MOUSE_UP, MouseButton.LEFT, ctrl)
            mouse(link, MouseEventType.MOUSE_DOWN, MouseButton.LEFT)
            mouse(link, MouseEventType.MOUSE_UP, MouseButton.LEFT)
            await asyncio.gather(*app._background_tasks)

    asyncio.run(interact())
    assert launched == [url]


def test_generation_only_sends_media_facts_the_user_included_in_instructions(monkeypatch):
    config = config_from_schema(media_form())
    config.media[0]["asset_id"] = "private-media-identity"
    config.media[0]["caption"] = "private-media-caption"
    instructions = "The image shows a red circle."
    requests = []

    def chat(self, messages, *, timeout):
        requests.extend(message["content"] for message in messages)
        return json.dumps({"profile_id": 1, "answers": {str(q.index): 1 for q in config.questions}})

    monkeypatch.setattr(chatgpt_http.Client, "ask", chat)
    assert ai.generate_responses(config, 1, instructions=instructions)
    task = json.loads(requests[-1])
    assert task["instructions"] == instructions
    assert not {"media", "user_media_context"} & task.keys()
    assert all("private-media-" not in message for message in requests)
