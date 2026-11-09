"""A temporary loopback-only editor; no form submission or external requests."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
import time
import webbrowser
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .generator import validate_answer
from .routing import Routing
from .validation import date_format, time_format


class EditorServer:
    def __init__(self, config, answers, save, statuses=None):
        self.config, self.answers, self.save = config, deepcopy(answers), save
        self.statuses = statuses or (lambda: ["ready"] * len(answers))
        self.closed = False
        self.last_request = time.monotonic()
        self.prefix = "/" + secrets.token_urlsafe(32) + "/"
        # HTML normalizes CRLF before checking inline-script hashes.
        self.html = Path(__file__).with_name("editor.html").read_text(encoding="utf-8").encode("utf-8")
        hashes = []
        for tag in (b"script", b"style"):
            blocks = re.findall(b"<" + tag + b">(.*?)</" + tag + b">", self.html, re.S)
            hashes.append(
                " ".join(
                    "'sha256-" + base64.b64encode(hashlib.sha256(b).digest()).decode() + "'" for b in blocks
                )
            )
        self.policy = (
            "default-src 'none'; connect-src 'self'; script-src "
            + hashes[0]
            + "; style-src "
            + hashes[1]
            + "; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass

            def send(self, status, data, html=False):
                body = data if html else json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header(
                    "Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8"
                )
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", owner.policy)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def route(self):
                parsed = urlsplit(self.path)
                if self.headers.get("Host") != owner.host or not parsed.path.startswith(owner.prefix):
                    self.send(404, {"error": "Editor session not found."})
                    return None
                owner.last_request = time.monotonic()
                return parsed.path[len(owner.prefix) :], parse_qs(parsed.query)

            def do_GET(self):
                route = self.route()
                if route is None:
                    return
                name, query = route
                try:
                    if name == "":
                        self.send(200, owner.html, html=True)
                    elif name == "state":
                        questions = []
                        for question in config.questions:
                            value = {
                                name: getattr(question, name)
                                for name in (
                                    "index",
                                    "text",
                                    "description",
                                    "qtype",
                                    "required",
                                    "options",
                                    "allow_other",
                                    "date_include_year",
                                    "date_include_time",
                                    "grid_title",
                                    "grid_row",
                                )
                            }
                            value["format"] = (
                                date_format(question)
                                if question.qtype == "date"
                                else time_format(question)
                                if question.qtype in {"time", "duration"}
                                else ""
                            )
                            questions.append(value)
                        states = owner.statuses()
                        self.send(
                            200,
                            {
                                "title": config.form_title,
                                "count": len(answers),
                                "questions": questions,
                                "statuses": states,
                                "initial_index": next(
                                    (i for i, state in enumerate(states, 1) if state == "ready"), 1
                                ),
                            },
                        )
                    elif name == "response":
                        self.send(200, owner.response(owner.index(query.get("index", [""])[0])))
                    else:
                        self.send(404, {"error": "Unknown editor action."})
                except (ValueError, OSError) as exc:
                    self.send(400, {"error": str(exc)})

            def do_POST(self):
                route = self.route()
                if route is None:
                    return
                name, _ = route
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4 * 1024 * 1024:
                        self.send(413, {"error": "The editor request is too large or empty."})
                        return
                    raw = self.rfile.read(length)
                    if (
                        self.headers.get("Origin") != owner.origin
                        or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                    ):
                        self.send(403, {"error": "Only this editor can save changes."})
                        return
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError("Expected an answer object.")
                    if name == "close":
                        owner.closed = True
                        self.send(200, {"closed": True})
                        return
                    if name != "save":
                        self.send(404, {"error": "Unknown editor action."})
                        return
                    index = owner.index(payload.get("index"))
                    if owner.statuses()[index - 1] != "ready":
                        self.send(
                            409,
                            {
                                "error": "Submitted or uncertain responses are read-only. Reopen the batch to check its status."
                            },
                        )
                        return
                    candidate = payload.get("answers")
                    try:
                        answer = validate_answer(config, candidate)
                    except ValueError as exc:
                        error = {"error": str(exc)}
                        match = re.search(r"\bQ(\d+)", str(exc))
                        if match:
                            error["question"] = int(match[1])
                        try:
                            error["active"] = sorted(
                                Routing(config).active({int(k): v for k, v in candidate.items()})
                            )
                        except (ValueError, TypeError, AttributeError):
                            pass
                        self.send(400, error)
                        return
                    owner.answers[index - 1] = deepcopy(owner.save(index, answer))
                    self.send(200, owner.response(index))
                except sqlite3.Error:
                    self.send(
                        409,
                        {
                            "error": "The response journal is busy or unavailable. Close this editor and reopen the batch before saving."
                        },
                    )
                except (ValueError, TypeError, OSError) as exc:
                    self.send(400, {"error": str(exc)})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 0.5
        self.host = "127.0.0.1:" + str(self.server.server_port)
        self.origin = "http://" + self.host
        self.url = self.origin + self.prefix

    def index(self, value):
        if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= len(self.answers):
            raise ValueError("Choose an existing response number.")
        return int(value)

    def response(self, index):
        answer = self.answers[index - 1]
        return {
            "index": index,
            "answers": answer,
            "active": sorted(Routing(self.config).active(answer)),
            "status": self.statuses()[index - 1],
        }

    def run(self):
        while not self.closed and time.monotonic() - self.last_request < 1800:
            self.server.handle_request()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.server.server_close()


def review(config, answers, save, statuses=None):
    with EditorServer(config, answers, save, statuses) as editor:
        if not webbrowser.open(editor.url, new=1):
            raise ValueError("Could not open the local editor. Set a default browser and try again.")
        try:
            editor.run()
        except KeyboardInterrupt:
            pass  # Saved edits remain; only unsaved browser changes are discarded.
