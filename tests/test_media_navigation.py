from threading import Event
from urllib.parse import parse_qs

import pytest
from test_inspector import URL, document, item
from test_transport import Pool, Response

from gformbot import media_navigation as navigation
from gformbot.config import QuestionConfig, config_from_schema
from gformbot.inspector import parse_form_html
from gformbot.routing import Routing


def image(identifier):
    return [identifier, f"Image {identifier}", None, 11, None, None, [f"opaque-{identifier}", 0]]


def rendered(items, history, images=(), *, next_control=True):
    markup = document(items)
    markup += f'<form action="{URL.rsplit("/", 1)[0]}/formResponse">'
    markup += '<input type="hidden" name="fbzx" value="fixture">'
    markup += f'<input type="hidden" name="pageHistory" value="{history}">'
    markup += f'<input type="hidden" name="partialResponse" value="server-state-{history}">'
    for identifier in images:
        markup += f'<div data-item-id="{identifier}"><img src="https://docs.google.com/forms-images-rt/image-{identifier}"></div>'
    if next_control:
        markup += '<div role="button" jsname="OCpkoe"><span>Next</span></div>'
    else:
        markup += '<div role="button" jsname="M2UYVd"><span>Submit</span></div>'
    return markup + "</form>"


def setup(monkeypatch, items, *responses):
    first = rendered(items, "0")
    config = config_from_schema(parse_form_html(first, URL))
    pool = Pool([Response(200, response) for response in responses])
    monkeypatch.setattr(navigation.urllib3, "PoolManager", lambda **_: pool)
    return config, first, pool


def test_real_next_fields_are_preserved_and_final_page_is_never_posted(monkeypatch):
    items = [item(1, "Name", 0), [1010, "Second", None, 8], image(201), [1011, "Last", None, 8], image(202)]
    second = rendered(items, "0,1", [201])
    last = rendered(items, "0,1,2", [202], next_control=False)
    config, first, pool = setup(monkeypatch, items, second, last)
    scanned, media_links = [], navigation.media_links

    def scan(media, markup, **options):
        scanned.append(markup)
        return media_links(media, markup, **options)

    monkeypatch.setattr(navigation, "media_links", scan)
    links = {}
    navigation.discover(config, first, Event(), links.update)
    assert len(links) == 2
    assert scanned == [first, second, last]
    assert len(pool.calls) == 2
    fields = [parse_qs(call[2]["body"].decode(), keep_blank_values=True) for call in pool.calls]
    assert [f["pageHistory"] for f in fields] == [["0"], ["0,1"]]
    assert [f["partialResponse"] for f in fields] == [["server-state-0"], ["server-state-0,1"]]
    assert all(f["continue"] == ["1"] for f in fields)
    assert pool.cleared


def test_shortest_real_routes_load_other_branches_too(monkeypatch):
    route = item(1, "Route", 2, ["A", "B", "Finish"])
    route[4][0][1] = [["A", None, 1010], ["B", None, 1011], ["Finish", None, -3]]
    items = [
        route,
        [1010, "A", None, 8, None, -3],
        image(201),
        [1011, "B", None, 8],
        image(202),
    ]
    config, first, pool = setup(
        monkeypatch,
        items,
        rendered(items, "0,1", [201], next_control=False),
        rendered(items, "0"),
        rendered(items, "0,2", [202], next_control=False),
    )
    links = {}
    navigation.discover(config, first, Event(), links.update)
    assert len(links) == 2
    assert [call[0] for call in pool.calls] == ["POST", "GET", "POST"]
    assert [parse_qs(call[2]["body"].decode())["entry.1"] for call in pool.calls if call[0] == "POST"] == [
        ["A"],
        ["B"],
    ]


def test_cancellation_after_one_image_keeps_it_and_stops_other_requests(monkeypatch):
    items = [item(1, "Name", 0), [1010, "Second", None, 8], image(201), [1011, "Last", None, 8], image(202)]
    config, first, pool = setup(monkeypatch, items, rendered(items, "0,1", [201]))
    stop, links = Event(), {}

    def publish(found):
        links.update(found)
        stop.set()

    navigation.discover(config, first, stop, publish)
    assert len(links) == 1
    assert len(pool.calls) == 1
    assert pool.cleared


def test_missing_next_control_never_sends_a_request(monkeypatch):
    items = [item(1, "Name", 0), [1010, "Second", None, 8], image(201)]
    config, _, pool = setup(monkeypatch, items)
    first = rendered(items, "0", next_control=False)
    with pytest.raises(ValueError, match="verified Next control"):
        navigation.discover(config, first, Event(), lambda _: None)
    assert not pool.calls
    assert pool.cleared


def test_rejected_next_is_not_treated_as_progress_or_retried(monkeypatch):
    items = [item(1, "Name", 0), [1010, "Second", None, 8], image(201)]
    config, first, pool = setup(monkeypatch, items, rendered(items, "0"))
    with pytest.raises(ValueError, match="did not accept"):
        navigation.discover(config, first, Event(), lambda _: None)
    assert len(pool.calls) == 1


def test_simple_validated_temporary_values_and_unsupported_regex_failure():
    assert (
        navigation._text_value(QuestionConfig(1, "Code", "text", validation=[[4, 301, ["^STU-[0-9]{3}$"]]]))
        == "STU-000"
    )
    assert (
        navigation._text_value(
            QuestionConfig(1, "Number", "text", validation=[[1, 1, ["5"]], [1, 3, ["5.1"]]])
        )
        == "5.05"
    )
    with pytest.raises(ValueError, match="satisfying its validation"):
        navigation._text_value(
            QuestionConfig(1, "Complex code", "text", validation=[[4, 301, ["^AB(C|D)[5-7]{3}$"]]])
        )


def test_bfs_routes_around_a_page_with_unsupported_temporary_answers():
    route = item(1, "Route", 2, ["Hard", "Easy"])
    route[4][0][1] = [["Hard", None, 1010], ["Easy", None, 1011]]
    hard = item(2, "Code", 0, rules=[[4, 301, ["^AB(C|D)[5-7]{3}$"]]])
    items = [
        route,
        [1010, "Hard", None, 8, None, 1012],
        hard,
        [1011, "Easy", None, 8],
        [1012, "Media", None, 8],
        image(201),
    ]
    config = config_from_schema(parse_form_html(document(items), URL))
    path, _ = navigation._path(config, Routing(config), 1, {4}, set())
    assert [(source, target) for source, target, _ in path] == [(1, 3), (3, 4)]


def test_hidden_next_cannot_authorize_navigation():
    page = navigation._NavigationPage('<div style="display:none" role="button" jsname="OCpkoe">Next</div>')
    assert not page.next_control


def test_empty_validation_containers_do_not_block_normal_next(monkeypatch):
    items = [item(1, "Name", 0), [1010, "Second", None, 8], image(201)]
    config, first, pool = setup(monkeypatch, items, rendered(items, "0,1", [201], next_control=False))
    first += '<div role="alert"></div>'
    links = {}
    navigation.discover(config, first, Event(), links.update)
    assert len(pool.calls) == 1 and len(links) == 1
