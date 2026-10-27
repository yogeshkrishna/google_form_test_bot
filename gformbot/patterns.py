"""A bounded regex worker and a verified subset of flagless JavaScript syntax."""

import atexit
import functools
import json
import os
import queue
import re
import subprocess
import sys
import threading

_SPACE = r"\t\n\v\f\r \xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def utf16(value):
    raw = value.encode("utf-16-le", errors="surrogatepass")
    return "".join(chr(raw[i] | raw[i + 1] << 8) for i in range(0, len(raw), 2))


@functools.lru_cache(maxsize=128)
def translate(pattern):
    if len(pattern) > 4096:
        raise ValueError("Regular expressions longer than 4096 characters are unsupported.")
    # Named groups, flags, lookbehind and backreferences need separate conformance
    # coverage. Do not quietly interpret Python-only syntax as JavaScript.
    result, inside, index, class_start = [], False, 0, -1
    pattern = utf16(pattern)
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 1
            if index == len(pattern):
                raise ValueError("Incomplete regular expression escape.")
            code = pattern[index]
            if code in "xu":
                width = 2 if code == "x" else 4
                digits = pattern[index + 1 : index + width + 1]
                if len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
                    raise ValueError("Unsupported regular expression escape.")
                result.append("\\" + code + digits)
                index += width
            elif code in "sS":
                if inside and code == "S":
                    raise ValueError("\\S inside character classes is not supported.")
                result.append(_SPACE if inside else "[" + ("^" if code == "S" else "") + _SPACE + "]")
            elif code in "dDwWbBfnrtv\\.^$*+?{}[]()|/-":
                result.append("\\" + code)
            else:
                raise ValueError("Unsupported regular expression escape or backreference.")
        else:
            if inside and (char == "[" or char == "]" and index == class_start + 1):
                raise ValueError("Empty or nested regular expression character classes are unsupported.")
            if not inside and char == "{" and not re.match(r"\{[0-9]+(?:,[0-9]*)?\}", pattern[index:]):
                raise ValueError("Unsupported regular expression quantifier; escape literal braces.")
            if not inside and char == "(" and pattern[index + 1 : index + 2] == "?":
                if pattern[index + 2 : index + 3] not in (":", "=", "!"):
                    raise ValueError("Unsupported regular expression group or flags.")
            if not inside and char == "+" and index and pattern[index - 1] in "*+?}":
                raise ValueError("Possessive regular expression quantifiers are unsupported.")
            if char == "[" and not inside:
                inside = True
                class_start = index
            elif char == "]" and inside:
                inside = False
            if not inside and char == ".":
                char = r"[^\n\r\u2028\u2029]"
            elif not inside and char == "$":
                char = r"\Z"
            result.append(char)
        index += 1
    return "".join(result)


class _Worker:
    def __init__(self):
        self.process = None
        self.lock = threading.Lock()

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass  # A broken pipe must not hide the original validation failure.

    def check(self, pattern, value, timeout=2):
        with self.lock:
            try:
                if self.process is None:
                    self.results = queue.Queue()
                    self.process = subprocess.Popen(
                        [sys.executable, "-I", "-u", os.path.abspath(__file__), "--worker"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        encoding="utf-8",
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    threading.Thread(
                        target=self._read, args=(self.process.stdout, self.results), daemon=True
                    ).start()
                self.process.stdin.write(json.dumps([pattern, value]) + "\n")
                self.process.stdin.flush()
                response = self.results.get(timeout=timeout)
                if response is None:
                    raise ValueError("Regular expression worker stopped unexpectedly.")
                result = json.loads(response)
                if "error" in result:
                    raise ValueError("Invalid or unsupported regular expression.")
                return result["matched"]
            except queue.Empty as exc:
                self.close()
                raise ValueError("Regular expression check timed out; answer validation stopped.") from exc
            except (OSError, ValueError) as exc:
                self.close()
                raise ValueError(str(exc)) from exc
            except BaseException:
                self.close()
                raise

    @staticmethod
    def _read(stream, results):
        try:
            for line in stream:
                results.put(line)
        except (OSError, ValueError):
            pass
        finally:
            results.put(None)


_WORKER = _Worker()
atexit.register(_WORKER.close)


@functools.lru_cache(maxsize=128)
def compile_pattern(pattern):
    translated = translate(pattern)
    _WORKER.check(translated, None)
    return translated


def matches(pattern, value):
    if len(value) > 1_000_000:
        raise ValueError("Answer exceeds the regular expression validation size limit.")
    if len(value.encode("utf-16-le", errors="surrogatepass")) // 2 > 1_000_000:
        raise ValueError("Answer exceeds the regular expression validation size limit.")
    return _match_cached(pattern, value) if len(value) <= 4096 else _WORKER.check(pattern, value)


@functools.lru_cache(maxsize=256)
def _match_cached(pattern, value):
    return _WORKER.check(pattern, value)


def _serve():
    compile_cached = functools.lru_cache(maxsize=128)(lambda pattern: re.compile(utf16(pattern), re.ASCII))
    for line in sys.stdin:
        try:
            pattern, value = json.loads(line)
            compiled = compile_cached(pattern)
            # JSON decoding combines surrogate pairs; JS's flagless regex engine
            # operates on individual UTF-16 units, including inside classes/dot.
            answer = {"matched": value is None or compiled.search(utf16(value)) is not None}
        except (re.error, ValueError, OverflowError, RecursionError, MemoryError):
            answer = {"error": True}
        print(json.dumps(answer), flush=True)


if __name__ == "__main__":
    _serve()
