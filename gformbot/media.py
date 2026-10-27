"""Keep ordered visual context without downloading or pretending to interpret it."""

from urllib.parse import urlsplit


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
    if (
        not isinstance(config.media, list)
        or not isinstance(config.media_context, str)
        or len(config.media_context) > 10_000
    ):
        raise ValueError("Media must be an ordered list and its context text at most 10,000 characters.")
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
            raise ValueError("Invalid or duplicate media context item.")
        keys.add(item["key"])
