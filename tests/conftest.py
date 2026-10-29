import pytest

from gformbot import previews


@pytest.fixture
def html_page():
    """Serve inline fixtures over HTTP, including in fresh Firefox private windows."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit

    pages = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = pages.get(urlsplit(self.path).path)
            self.send_response(200 if content is not None else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((content or "").encode("utf-8"))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def serve(content):
        path = f"/page/{len(pages)}/viewform"
        pages[path] = content
        return f"http://127.0.0.1:{server.server_port}{path}"

    yield serve
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture(autouse=True)
def selected_fixture_browser(request, monkeypatch):
    """Allow an explicitly selected browser for optional visible interaction checks."""
    import os

    selected = os.environ.get("GFORMBOT_TEST_BROWSER")
    if request.node.get_closest_marker("browser") and selected:
        from gformbot import browser

        factory = browser.get_driver
        monkeypatch.setattr(
            browser,
            "get_driver",
            lambda name="auto", **kw: factory(selected),
        )


@pytest.fixture(autouse=True)
def isolated_preview_index(monkeypatch, tmp_path):
    monkeypatch.setattr(previews, "index_path", lambda: tmp_path / "state" / "previews.json")
