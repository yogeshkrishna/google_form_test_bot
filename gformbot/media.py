"""Keep media metadata in form order for inspection and change detection."""

import re
from collections import Counter, defaultdict
from copy import deepcopy
from html.parser import HTMLParser
from threading import Event, Lock, Thread
from urllib.parse import urlsplit

_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_ITEM_PARAMS = re.compile(r"%\.@\.\[(\d+),")
_FORM_IMAGE = re.compile(r"https://docs\.google\.com/forms-images-rt/[\w=-]+", re.ASCII)


def _public_link(value):
    if not isinstance(value, str) or re.search(r"[\s\x00-\x1f\x7f-\x9f\\]", value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None
    except ValueError:
        return False


class MediaDiscovery:
    """Resolve browser links in the background for one inspected form."""

    def __init__(self, config, document, links):
        self.config = deepcopy(config)
        self.document = document
        self.stop = Event()
        self._lock = Lock()
        self._keys = {item["key"] for item in config.media}
        self._links = {}
        self.publish(links)
        self._done = self._keys <= self._links.keys()

    def snapshot(self):
        with self._lock:
            return self._links.copy(), self._done or self.stop.is_set()

    def publish(self, links):
        with self._lock:
            if not self.stop.is_set():
                self._links.update(
                    (key, url) for key, url in links.items() if key in self._keys and _public_link(url)
                )

    def start(self):
        if not self._done and not self.stop.is_set():
            Thread(target=self._discover, name="gformbot-media-discovery", daemon=True).start()
        return self

    def _discover(self):
        from .media_navigation import discover

        try:
            discover(self.config, self.document, self.stop, self.publish)
        except (ValueError, OSError):
            pass  # Found links remain usable even when a later section cannot be reached.
        finally:
            with self._lock:
                self._done = True

    def close(self):
        with self._lock:
            self.stop.set()


class _RenderedImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.options = defaultdict(set)
        self.scoped_items = set()
        self.unscoped = defaultdict(set)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        _, in_form, item, option = self.stack[-1] if self.stack else (None, False, "", None)
        in_form = in_form or tag == "form"
        if in_form:
            params = _ITEM_PARAMS.match(attrs.get("data-params") or "")
            identity = attrs.get("data-item-id") or (params[1] if params else item)
            if identity != item:
                option = None
            item = identity
            if attrs.get("role") in {"radio", "checkbox", "option"} and "data-value" in attrs:
                option = attrs["data-value"]
            if tag == "img" and item and _FORM_IMAGE.fullmatch(src := attrs.get("src") or ""):
                if option is None:
                    self.unscoped[item].add(src)
                else:
                    self.scoped_items.add(item)
                    self.options[item, option].add(src)
        if tag not in _VOID:
            self.stack.append((tag, in_form, item, option))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def media_links(media, document, *, page=1):
    """Return browser targets available in the public responder page."""
    from .followups import safe_url

    links = {}
    unresolved = []
    for asset in media:
        identity = asset["asset_id"]
        if asset["kind"] == "video":
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", identity):
                links[asset["key"]] = f"https://www.youtube.com/watch?v={identity}"
            elif _public_link(identity) and urlsplit(identity).path.lower().endswith(".mp4"):
                links[asset["key"]] = identity
        elif identity.startswith("https://"):
            if (url := safe_url(identity, "https://docs.google.com/")) and _public_link(url):
                links[asset["key"]] = url
        elif page is None or asset["page"] == page:
            unresolved.append(asset)

    if unresolved:
        # Match by item ID since Google can shuffle questions. An item with multiple
        # images needs a more specific match than the enclosing question provides.
        counts = Counter(asset["item_id"] for asset in media)
        unscoped = Counter(asset["item_id"] for asset in media if asset["placement"] != "option")
        options = Counter(
            (asset["item_id"], asset["caption"]) for asset in media if asset["placement"] == "option"
        )
        rendered = _RenderedImages()
        rendered.feed(document)
        for asset in unresolved:
            identity = asset["item_id"]
            if asset["placement"] == "option":
                key = identity, asset["caption"]
                urls = rendered.options.get(key, ()) if options[key] == 1 else ()
                if not urls and counts[identity] == 1 and identity not in rendered.scoped_items:
                    # Older markup may only put the image inside the question.
                    urls = rendered.unscoped.get(identity, ())
            else:
                urls = rendered.unscoped.get(identity, ()) if unscoped[identity] == 1 else ()
            if len(urls) == 1:
                links[asset["key"]] = next(iter(urls))
    return links


def inspect_media(item, before_question, page):
    from .inspector import _get

    found = []

    def add(embedded, placement, suffix, caption="", option_index=None):
        if embedded is None:
            return
        if not isinstance(embedded, list) or not embedded:
            raise ValueError(f"Unrecognized media metadata for item {item[0]}.")
        kind = _get(embedded, 1)
        if kind is not None and (type(kind) is not int or kind not in (0, 1, 2)):
            raise ValueError(f"Unrecognized media type for item {item[0]}.")
        asset = _get(embedded, 3) if kind == 1 else _get(embedded, 4) if kind == 2 else embedded[0]
        if not isinstance(asset, str) or not asset:
            raise ValueError(f"Missing media identity for item {item[0]}.")
        if kind == 2:
            from .followups import safe_url

            asset = safe_url(asset, "https://docs.google.com/")
            if urlsplit(asset).scheme != "https":
                raise ValueError("Unrecognized trusted image URL.")
        found.append(
            {
                "key": f"{item[0]}:{suffix}",
                "item_id": str(item[0]),
                "page": page,
                "before_question": before_question,
                "placement": placement,
                "option_index": option_index,
                "kind": "video" if kind == 1 else "image",
                "asset_id": asset,
                "caption": str(caption or "").strip(),
            }
        )

    if item[3] in (11, 12):
        add(_get(item, 6), "item", "item", item[1])
        if not found:
            raise ValueError(f"Missing media item data for {item[0]}.")
    inline = _get(item, 9)
    if inline:
        add(_get(inline, 0), "question", "question", _get(inline, 1) or item[1])
    entries = _get(item, 4)
    for entry in entries if isinstance(entries, list) else []:
        options = _get(entry, 1)
        for index, option in enumerate(options if isinstance(options, list) else []):
            embedded = _get(option, 5)
            if embedded is not None:
                add(embedded, "option", f"{entry[0]}:option:{index}", _get(option, 0), index)
    return found


def validate_media(config):
    if not isinstance(config.media, list):
        raise ValueError("Media must be an ordered list.")
    keys = set()
    fields = {
        "key",
        "item_id",
        "page",
        "before_question",
        "placement",
        "option_index",
        "kind",
        "asset_id",
        "caption",
    }
    for item in config.media:
        if (
            not isinstance(item, dict)
            or set(item) != fields
            or any(not isinstance(item[name], str) for name in ("key", "item_id", "asset_id", "caption"))
            or not item["key"]
            or item["key"] in keys
            or not item["asset_id"]
            or type(item["page"]) is not int
            or item["page"] < 1
            or type(item["before_question"]) is not int
            or item["before_question"] < 1
            or item["placement"] not in ("item", "question", "option")
            or item["kind"] not in ("image", "video")
            or item["option_index"] is not None
            and (type(item["option_index"]) is not int or item["option_index"] < 0)
        ):
            raise ValueError("Invalid or duplicate media item.")
        keys.add(item["key"])
