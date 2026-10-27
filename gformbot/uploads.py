"""Explicit attachments and conservative transfer recovery, separate from submission."""

import hashlib
import json
import os
import re
from pathlib import Path

_EXTENSIONS = {
    1: {".doc", ".docx", ".odt", ".rtf", ".txt"},
    2: {".ppt", ".pptx", ".odp"},
    3: {".xls", ".xlsx", ".ods", ".csv"},
    4: {".svg"},
    5: {".pdf"},
    6: {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"},
    7: {".mp4", ".mov", ".avi", ".webm", ".mpeg", ".mpg"},
    8: {".mp3", ".wav", ".ogg", ".m4a", ".flac"},
}


def file_info(path, *, max_size=None):
    from .config import clean_path

    path = clean_path(path).resolve()
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if max_size is not None and before.st_size > max_size:
                raise ValueError(f"File exceeds the size limit or changed since preparation: {path.name}.")
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
            raise ValueError(f"File changed while hashing: {path.name}.")
        current = path.stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"File changed while hashing: {path.name}.")
        return {"path": str(path), "size": before.st_size, "sha256": digest.hexdigest()}
    except OSError as exc:
        raise ValueError(f"File is unavailable: {path.name}.") from exc


def upload_settings(question, flags):
    if (
        not isinstance(flags, list)
        or len(flags) != 4
        or not isinstance(flags[0], str)
        or not isinstance(flags[1], list)
        or any(type(t) is not int or t not in range(9) for t in flags[1])
        or type(flags[2]) is not int
        or not 1 <= flags[2] <= 10
        or type(flags[3]) not in (str, int)
        or not re.fullmatch(r"[0-9]+", str(flags[3]))
        or int(flags[3]) < 1
    ):
        raise ValueError("Unrecognized file upload settings.")
    question.upload_types = flags[1] or [0]
    question.upload_max_files = flags[2]
    question.upload_max_size = int(flags[3])


def file_answer(question, value, *, base=None):
    if value in (None, "") or value == []:
        if question.required:
            raise ValueError(f"Q{question.index}: choose the required attachment files.")
        return ""
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(f"Q{question.index}: invalid attachment JSON.") from exc
    if isinstance(value, dict):
        if set(value) != {"files"} or not isinstance(value["files"], list):
            raise ValueError("Attachments need an object containing a files list.")
        files = value["files"]
    else:
        files = [value] if isinstance(value, str) else value
    if not isinstance(files, list) or not 1 <= len(files) <= question.upload_max_files:
        raise ValueError(f"Q{question.index}: choose at most {question.upload_max_files} files.")
    result, names, paths = [], set(), set()
    for file in files:
        if isinstance(file, str):
            from .config import clean_path

            path = clean_path(file)
            file = file_info(
                Path(base) / path if base and not path.is_absolute() else path,
                max_size=question.upload_max_size,
            )
        if (
            not isinstance(file, dict)
            or set(file) != {"path", "size", "sha256"}
            or not isinstance(file["path"], str)
            or not Path(file["path"]).is_absolute()
            or type(file["size"]) is not int
            or not 0 <= file["size"] <= question.upload_max_size
            or not isinstance(file["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", file["sha256"])
        ):
            raise ValueError(f"Q{question.index}: invalid file identity or file exceeds its size limit.")
        path = Path(file["path"])
        if any(c in str(path) for c in "\r\n\0"):
            raise ValueError("Attachment paths cannot contain newlines or null characters.")
        if path.name.casefold() in names or str(path).casefold() in paths:
            raise ValueError(f"Q{question.index}: duplicate files or filenames cannot be verified reliably.")
        if 0 not in question.upload_types and not any(
            path.suffix.lower() in _EXTENSIONS.get(t, ()) for t in question.upload_types
        ):
            raise ValueError(f"Q{question.index}: file extension is not allowed: {path.name}.")
        names.add(path.name.casefold())
        paths.add(str(path).casefold())
        result.append(dict(file))
    return {"files": result}


def verify_files(value):
    for expected in value["files"]:
        if file_info(expected["path"], max_size=expected["size"]) != expected:
            raise ValueError(f"File changed since preparation: {Path(expected['path']).name}.")


def manifest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class UploadGuard:
    def __init__(self, journal, response, *, allowed=False):
        self.journal, self.response, self.allowed = journal, response, allowed

    def begin(self, question, value):
        if not self.allowed:
            raise ValueError("File transfer needs explicit permission (--allow-uploads).")
        verify_files(value)
        self.journal.begin_upload(self.response, question.index, manifest(value))

    def finish(self, question, value, *, ids=None, uncertain=False):
        self.journal.finish_upload(
            self.response, question.index, manifest(value), ids=ids, uncertain=uncertain
        )


def fill_upload(driver, question, box, value, timeout, guard):
    from .filler import _wait
    from .selenium_runtime import By

    if not value:
        if box.find_elements(
            By.CSS_SELECTOR, f'input[name="entry.{question.entry_id}"][value]:not([value=""])'
        ):
            raise ValueError("An attachment is already present; inspect it before filling a blank answer.")
        return
    if guard is None:
        raise ValueError("Save the response plan and explicitly allow uploads before transferring files.")

    def accepted():
        tokens = [
            e.get_attribute("value")
            for e in box.find_elements(
                By.CSS_SELECTOR, f'input[type="hidden"][name="entry.{question.entry_id}"]'
            )
        ]
        tokens = [token for token in tokens if token]
        names = [Path(file["path"]).name for file in value["files"]]
        return (
            tokens
            if len(tokens) == len(names)
            and len(set(tokens)) == len(tokens)
            and all(name in box.text.splitlines() for name in names)
            else None
        )

    previous = guard.journal.upload_record(guard.response, question.index)
    if previous:
        if (
            previous["state"] == "verified"
            and previous["manifest"] == manifest(value)
            and accepted() == previous["ids"]
        ):
            return
        if previous["state"] != "prepared":
            raise ValueError("A previous upload is verified or uncertain; review it before another transfer.")
    fields = box.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
    if len(fields) != 1:
        raise ValueError(
            "This upload picker is not supported yet. No files were transferred; review the attachments manually in the browser."
        )
    if accepted():
        raise ValueError("Existing attachments were not prepared by this run; review them before continuing.")
    with guard.journal.upload_lock(guard.response, question.index):
        guard.begin(question, value)
        try:
            fields[0].send_keys("\n".join(file["path"] for file in value["files"]))
            ids = _wait(
                driver,
                timeout,
                lambda _: accepted(),
                "Upload acceptance could not be verified; do not retry without checking the form.",
            )
            guard.finish(question, value, ids=ids)
        except BaseException:
            guard.finish(question, value, uncertain=True)
            raise
