"""Temporary sessions on ChatGPT's undocumented Android guest HTTPS transport."""

from __future__ import annotations

import base64
import json
import math
import re
import time
from copy import copy
from datetime import datetime
from email.utils import parsedate_to_datetime
from random import uniform
from threading import Condition, Event
from uuid import uuid4

import certifi
import urllib3

# Protocol reference: etrnkz/chatgpt-unofficial-api, commit f8d679e. See LICENSE-chatgpt-protocol.
_HOST = "https://android.chat.openai.com"
_CONVERSATION = "/backend-anon/f/conversation"
_USER_AGENT = "ChatGPT/1.2026.265 (Android 14; Mobile; 2626541)"
_JSON_LIMIT = 128_000
_STREAM_LIMIT = 2_000_000
_DONE_EVENT = re.compile(rb"(?:^|(?<=[\r\n]))data:[ \t]*\[DONE\][ \t]*\r?\n\r?\n")


class ChatHTTPError(ValueError):
    """A failed HTTPS turn, including any output available for validation."""

    def __init__(
        self,
        message: str,
        *,
        partial_content: str = "",
        retryable: bool = False,
        status=None,
        stage=None,
        code=None,
        error_type=None,
        request_id=None,
        retry_after=None,
        rate_reset=None,
        reason=None,
    ):
        super().__init__(message)
        self.partial_content = partial_content
        self.retryable = retryable
        self.status, self.stage = status, stage
        self.code, self.error_type = code, error_type
        self.request_id, self.retry_after = request_id, retry_after
        self.rate_reset = rate_reset or {}
        self.reason = reason


def _identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else None


def _retry_delay(value, server_date=None):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        delay = float(value)
    except OverflowError:
        return None
    except ValueError:
        try:
            reset = parsedate_to_datetime(value).timestamp()
            now = parsedate_to_datetime(server_date).timestamp() if server_date else time.time()
            delay = max(0, reset - now)
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def _provider_error(payload, *, status=None, stage="generation", headers=None):
    """Retain bounded machine diagnostics without echoing arbitrary provider text."""
    headers = {key.lower(): value for key, value in (headers or {}).items()}
    detail = payload if isinstance(payload, dict) else {}
    for _ in range(4):
        nested = detail.get("error", detail.get("detail"))
        if not isinstance(nested, dict):
            break
        detail = nested
    code, kind = _identifier(detail.get("code")), _identifier(detail.get("type"))
    if status is None:
        candidate = detail.get("status", payload.get("status") if isinstance(payload, dict) else None)
        if type(candidate) is int and 400 <= candidate <= 599:
            status = candidate
    retry_after = _retry_delay(headers.get("retry-after"), headers.get("date"))
    if retry_after is None:
        retry_after = _retry_delay(detail.get("retry_after"))
    quota_codes = {
        "insufficient_quota",
        "quota_exceeded",
        "usage_limit_exceeded",
        "model_cap_exceeded",
        "daily_limit_exceeded",
        "weekly_limit_exceeded",
        "credit_balance_exhausted",
        "organization_usage_limit_exceeded",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
    }
    quota = code in quota_codes or kind in quota_codes
    throttle = code in {"rate_limit_exceeded", "too_many_requests", "slow_down"} or kind == "rate_limit_error"
    reason = "unknown"
    if status in (None, 429):
        if quota:
            reason = "quota"
        elif throttle or (status == 429 and retry_after is not None):
            reason = "throttle"
    if status == 429 or reason in {"quota", "throttle"}:
        message = f"ChatGPT HTTPS request limited during {stage}"
        if reason == "quota":
            message += "; the server reports an exhausted allowance"
        elif reason == "unknown":
            message += "; cause unclassified"
    elif status in (401, 403):
        message = f"ChatGPT HTTPS denied {stage}"
    else:
        message = f"ChatGPT HTTPS {stage} failed"
    if status is not None:
        message += f" (HTTP {status})"
    if code:
        message += f"; code={code}"
    if retry_after is not None:
        message += f"; retry after {retry_after:g}s"
    return ChatHTTPError(
        message + ".",
        status=status,
        stage=stage,
        code=code,
        error_type=kind,
        request_id=_identifier(headers.get("x-request-id") or headers.get("request-id")),
        retry_after=retry_after,
        rate_reset={
            unit: value
            for unit in ("requests", "tokens")
            if (value := _identifier(headers.get(f"x-ratelimit-reset-{unit}"))) is not None
        },
        reason=reason,
        retryable=status is not None and 500 <= status < 600,
    )


def _parse_stream(body: bytearray, *, failure: ChatHTTPError | None = None, headers=None) -> str:
    pieces: list[str] = []
    completed = False
    for line in body.splitlines():
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            completed = True
            text = "".join(pieces).strip()
            if text:
                return text
            break
        try:
            event = json.loads(data)
        except (ValueError, RecursionError):
            if failure is not None:
                failure.partial_content = "".join(pieces)
                raise failure from None
            raise ChatHTTPError(
                "ChatGPT HTTPS sent an invalid stream event.",
                partial_content="".join(pieces),
            ) from None
        if not isinstance(event, dict):
            continue
        if event.get("error") or event.get("type") == "error":
            error = _provider_error(event, headers=headers)
            error.partial_content = "".join(pieces)
            raise error

        value = event.get("v")
        message = event.get("message")
        if not isinstance(message, dict) and isinstance(value, dict):
            message = value.get("message")
        if isinstance(message, dict):
            author = message.get("author")
            content = message.get("content")
            if isinstance(author, dict) and author.get("role") == "assistant" and isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list) and parts and all(isinstance(part, str) for part in parts):
                    pieces = parts.copy()

        operations = value if event.get("o") == "patch" and isinstance(value, list) else [event]
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("p") != "/message/content/parts/0":
                continue
            fragment = operation.get("v")
            if isinstance(fragment, str):
                if operation.get("o") == "append":
                    pieces.append(fragment)
                elif operation.get("o") == "replace":
                    pieces = [fragment]
        if isinstance(value, str) and not any(event.get(key) for key in ("o", "p", "type")):
            pieces.append(value)
    if failure is None:
        failure = ChatHTTPError("ChatGPT HTTPS ended without a complete answer.", retryable=not completed)
    failure.partial_content = "".join(pieces)
    raise failure


def _token(body: dict, key: str, stage: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value or not value.isascii() or not value.isprintable():
        raise ChatHTTPError(f"ChatGPT HTTPS omitted valid {stage} state.")
    return value


class Client:
    """Use one fresh guest identity and reusable connections for a batch of independent turns."""

    def __init__(self, *, on_wait=None, max_wait=60, max_retries=2):
        if not isinstance(max_wait, (int, float)) or not math.isfinite(max_wait) or max_wait < 0:
            raise ValueError("ChatGPT cooldown budget must be finite and nonnegative.")
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ValueError("ChatGPT throttle retries must be an integer from 0 to 3.")
        self._device = str(uuid4())
        self._cancelled = Event()
        self._condition = Condition()
        self._active = 0
        self._cooldown = 0.0
        self._cooldown_used = 0.0
        self._limit_error = None
        self._serial = False
        self._on_wait = on_wait
        self._max_wait, self._max_retries = max_wait, max_retries
        self._pool = urllib3.PoolManager(
            maxsize=2,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
                "oai-client-type": "android",
                "oai-package-name": "com.openai.chatgpt",
                "oai-device-id": self._device,
                "oai-android-play-integrity-token-failed": "true",
            },
        )

    def cancel(self) -> None:
        self._cancelled.set()
        with self._condition:
            self._condition.notify_all()

    def close(self) -> None:
        self.cancel()
        if self._pool is not None:
            self._pool.clear()
            self._pool = None

    def _ready(self, deadline, *, enter=False):
        with self._condition:
            while True:
                if self._limit_error is not None:
                    error = copy(self._limit_error)
                    error.partial_content = ""
                    raise error
                if self._cancelled.is_set():
                    raise ChatHTTPError("The ChatGPT HTTPS session was cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ChatHTTPError("ChatGPT HTTPS reached its time limit while waiting.")
                cooldown = self._cooldown - time.monotonic()
                if cooldown <= 0 and (not enter or self._active < (1 if self._serial else 2)):
                    if enter:
                        self._active += 1
                    return
                self._condition.wait(min(remaining, cooldown) if cooldown > 0 else remaining)

    def _leave(self):
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def _defer(self, error, delay, deadline, *, can_retry):
        with self._condition:
            now = time.monotonic()
            until = max(self._cooldown, now + delay)
            added = max(0, until - max(self._cooldown, now))
            if (
                self._limit_error is not None
                or not can_retry
                or self._cooldown_used + added > self._max_wait
                or until >= deadline
            ):
                if self._limit_error is None:
                    self._limit_error = error
                self._condition.notify_all()
                return False
            notify = self._cooldown <= now
            self._cooldown = until
            self._cooldown_used += added
            self._serial = True
            self._condition.notify_all()
        if notify and self._on_wait:
            self._on_wait(delay)
        return True

    def _post(self, path, data, deadline, stage, *, extra_headers=None):
        self._ready(deadline)
        pool = self._pool
        if pool is None:
            raise ChatHTTPError("The ChatGPT HTTPS session is closed.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChatHTTPError("ChatGPT HTTPS reached its time limit.", retryable=True)
        streaming = extra_headers is not None
        limit = _STREAM_LIMIT if streaming else _JSON_LIMIT
        body = bytearray()
        scan_from = 0
        failure = None
        response = None
        try:
            response = pool.request(
                "POST",
                _HOST + path,
                body=json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(),
                headers={**pool.headers, **(extra_headers or {})},
                timeout=urllib3.Timeout(total=remaining, connect=min(5, remaining), read=min(30, remaining)),
                preload_content=False,
                redirect=False,
                retries=False,
            )
            if response.status != 200:
                error_body = bytearray()
                try:
                    while len(error_body) < 16_384 and time.monotonic() < deadline:
                        chunk = response.read1(16_384 - len(error_body))
                        if not chunk:
                            break
                        error_body.extend(chunk[: 16_384 - len(error_body)])
                except (urllib3.exceptions.HTTPError, OSError):
                    pass  # The HTTP status and header diagnostics remain useful.
                try:
                    payload = json.loads(error_body)
                except (ValueError, RecursionError):
                    payload = {}
                error = _provider_error(
                    payload, status=response.status, stage=stage, headers=response.headers
                )
                if response.status == 413 and streaming:
                    error.args = (
                        "This prompt is too large for ChatGPT HTTPS (HTTP 413). "
                        "Reduce the instructions or prepare the form with supplied answers.",
                    )
                raise error
            expected = "text/event-stream" if streaming else "application/json"
            if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != expected:
                raise ChatHTTPError(f"ChatGPT HTTPS returned an unexpected {stage} response.")
            while True:
                if self._cancelled.is_set():
                    raise ChatHTTPError("The ChatGPT HTTPS session was cancelled.")
                if time.monotonic() >= deadline:
                    raise ChatHTTPError("ChatGPT HTTPS reached its time limit.", retryable=True)
                chunk = response.read1(min(16_384, limit + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > limit:
                    raise ChatHTTPError("ChatGPT HTTPS exceeded the response size limit.")
                if streaming and b"\n" in chunk:
                    if _DONE_EVENT.search(body, scan_from):
                        break
                    # Retain the last line in case the event's blank separator arrives next.
                    last_newline = body.rfind(b"\n")
                    scan_from = max(scan_from, body.rfind(b"\n", scan_from, last_newline) + 1)
        except KeyboardInterrupt as interrupt:
            if streaming and body:
                try:
                    interrupt.partial_content = _parse_stream(body)
                except ChatHTTPError as error:
                    interrupt.partial_content = error.partial_content
            raise
        except ChatHTTPError as error:
            failure = error
        except (urllib3.exceptions.TimeoutError, TimeoutError):
            failure = ChatHTTPError("ChatGPT HTTPS reached its time limit.", retryable=True)
        except (urllib3.exceptions.HTTPError, OSError):
            failure = ChatHTTPError(f"ChatGPT HTTPS connection failed during {stage}.", retryable=True)
        finally:
            if response is not None:
                response.close()
                response.release_conn()
        if streaming:
            return _parse_stream(body, failure=failure, headers=response.headers if response else None)
        if failure:
            raise failure
        return body

    def _json(self, path, data, deadline, stage):
        try:
            result = json.loads(self._post(path, data, deadline, stage))
        except ChatHTTPError:
            raise
        except (ValueError, RecursionError):
            raise ChatHTTPError(f"ChatGPT HTTPS returned invalid {stage} data.") from None
        if not isinstance(result, dict):
            raise ChatHTTPError(f"ChatGPT HTTPS returned invalid {stage} data.")
        return result

    def ask(self, messages, *, timeout: float = 180) -> str:
        """Retry identified temporary limits in the same guest session, within a fixed budget."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("ChatGPT HTTPS needs a finite positive timeout.")
        deadline = time.monotonic() + timeout
        for attempt in range(self._max_retries + 1):
            self._ready(deadline, enter=True)
            try:
                return self._ask_once(messages, deadline)
            except ChatHTTPError as error:
                if error.status != 429 and error.reason not in {"throttle", "quota"}:
                    raise
                delay = error.retry_after if error.retry_after is not None else 2**attempt + uniform(0, 0.25)
                can_retry = (
                    error.reason == "throttle" and not error.partial_content and attempt < self._max_retries
                )
                if not self._defer(error, delay, deadline, can_retry=can_retry):
                    raise
            finally:
                self._leave()

    def _ask_once(self, messages, deadline):
        prompt = "\n".join(message["content"] for message in messages)
        # Match the guest client's requirements envelope. No proof state is persisted.
        config = [
            4880,
            datetime.now().strftime("%a %b %d %Y"),
            4294705152,
            2000,
            _USER_AGENT,
            None,
            "",
            "en-US",
            "en-US,en",
            0.5,
            "webkitGetUserMedia",
            "location",
            "window",
            900,
            self._device,
            "",
            20,
            round(time.time() * 1000),
        ]
        encoded = base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode("ascii")
        requirements = self._json(
            "/backend-anon/sentinel/chat-requirements", {"p": "gAAAAAC" + encoded}, deadline, "setup"
        )
        for name in ("proofofwork", "turnstile", "arkose"):
            proof = requirements.get(name)
            if proof and (not isinstance(proof, dict) or proof.get("required") or proof.get("seed")):
                raise ChatHTTPError("ChatGPT HTTPS now requires guest verification.")
        requirements_token = _token(requirements, "token", "setup")
        body = {
            "action": "next",
            "parent_message_id": "client-created-root",
            "model": "auto",
            "timezone_offset_min": 0,
            "history_and_training_disabled": True,
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": [],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
        }
        conduit = self._json(
            _CONVERSATION + "/prepare", {**body, "fork_from_shared_post": False}, deadline, "preparation"
        )
        message = {
            "id": str(uuid4()),
            "author": {"role": "user"},
            "create_time": round(time.time()),
            "content": {"content_type": "text", "parts": [prompt]},
            "metadata": {},
        }
        return self._post(
            _CONVERSATION,
            {**body, "messages": [message]},
            deadline,
            "generation",
            extra_headers={
                "Accept": "text/event-stream",
                "OpenAI-Sentinel-Chat-Requirements-Token": requirements_token,
                "x-conduit-token": _token(conduit, "conduit_token", "preparation"),
                "oai-echo-logs": "0,7000,1,7500",
            },
        )
