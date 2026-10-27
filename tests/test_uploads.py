import json
import os

import pytest

from gformbot.config import QuestionConfig, RunConfig
from gformbot.state import Journal


def config():
    return RunConfig(
        "https://docs.google.com/forms/d/e/test/viewform",
        questions=[
            QuestionConfig(
                1,
                "Attachment",
                "upload",
                True,
                entry_id="123",
                item_id="456",
                upload_types=[5],
                upload_max_files=2,
                upload_max_size=1024,
            )
        ],
    )


def test_prepare_files_limits_hashes_and_roundtrip(tmp_path):
    from gformbot.generator import load_reference, validate_answer, write_csv
    from gformbot.uploads import file_answer, verify_files

    c = config()
    file = tmp_path / "sample.pdf"
    file.write_bytes(b"%PDF-test")
    answer = {1: file_answer(c.questions[0], [str(file)])}
    assert answer[1]["files"][0]["size"] == 9
    assert validate_answer(c, answer) == answer
    path = tmp_path / "files.csv"
    write_csv(c, [answer], path)
    assert load_reference(c, path) == [answer]
    verify_files(answer[1])
    file.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        verify_files(answer[1])
    # Saved plans remain inspectable even after an original file is removed.
    file.unlink()
    assert validate_answer(c, answer) == answer
    with pytest.raises(ValueError, match="file|File"):
        verify_files(answer[1])


def test_upload_states_prevent_crash_or_concurrent_repeat():
    journal = Journal("upload-test")
    journal.begin_upload(1, 1, "manifest")
    assert journal.upload_record(1, 1)["state"] == "uploading"
    with pytest.raises(ValueError, match="uncertain|already|review"):
        journal.begin_upload(1, 1, "manifest")
    journal.finish_upload(1, 1, "manifest", ids=["accepted-file-id"])
    assert journal.upload_record(1, 1)["state"] == "verified"
    with pytest.raises(ValueError):
        journal.begin_upload(1, 1, "manifest")
    with pytest.raises(ValueError):
        journal.begin_upload(1, 1, "changed")
    journal.reset_upload(1, 1)
    journal.begin_upload(1, 1, "manifest")
    journal.finish_upload(1, 1, "manifest", uncertain=True)
    assert journal.upload_record(1, 1)["state"] == "uncertain"


def test_permission_and_hash_check_happen_before_transfer(tmp_path):
    from gformbot.uploads import UploadGuard, file_answer

    c = config()
    file = tmp_path / "sample.pdf"
    file.write_bytes(b"%PDF-test")
    value = file_answer(c.questions[0], [str(file)])
    guard = UploadGuard(Journal("permissions"), 1, allowed=False)
    with pytest.raises(ValueError, match="permission|allow"):
        guard.begin(c.questions[0], value)
    assert guard.journal.upload_record(1, 1) is None
    guard.allowed = True
    file.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        guard.begin(c.questions[0], value)
    assert guard.journal.upload_record(1, 1) is None


def test_ai_never_invents_or_receives_file_paths(tmp_path):
    from gformbot import ai
    from gformbot.uploads import file_answer

    c = config()
    file = tmp_path / "sample.pdf"
    file.write_bytes(b"%PDF-test")
    c.questions[0].fixed_value = file_answer(c.questions[0], [str(file)])
    assert ai._encode_profile(c, {1: c.questions[0].fixed_value}) == {"1": 0}
    assert ai._decode_profile(c, {"1": 0}) == {"1": c.questions[0].fixed_value}
    schema = ai._response_schema(c, 1)
    assert str(file) not in json.dumps(schema)


def test_preparation_never_opens_browser_or_requests_transfer(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gformbot.execution import Runner
    from gformbot.uploads import file_answer

    c = config()
    file = tmp_path / "sample.pdf"
    file.write_bytes(b"%PDF-test")
    ui = SimpleNamespace(
        _info=lambda *a: None, _ask_confirm=lambda *a, **k: pytest.fail("Dry run asked to transfer")
    )
    assert Runner(c, [{1: file_answer(c.questions[0], [str(file)])}], ui).run(dry_run=True) is False


def test_running_upload_cannot_be_reconciled():
    journal = Journal("locked-upload")
    journal.begin_upload(1, 1, "manifest")
    with journal.upload_lock(1, 1), pytest.raises(ValueError, match="still running"):
        journal.reset_upload(1, 1)
    # A crash releases the OS lock but leaves the durable upload intent.
    assert journal.upload_record(1, 1)["state"] == "uploading"
    journal.reset_upload(1, 1)
    assert journal.upload_record(1, 1)["state"] == "prepared"


def test_transfer_exception_is_durable_and_not_retried(tmp_path):
    from gformbot.uploads import UploadGuard, file_answer, fill_upload

    c = config()
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    value = file_answer(c.questions[0], [str(path)])
    calls = []

    class Field:
        def send_keys(self, value):
            calls.append(value)
            raise KeyboardInterrupt()

    class Box:
        text = ""

        def find_elements(self, by, selector):
            return [Field()] if selector == 'input[type="file"]' else []

    guard = UploadGuard(Journal("crashed-transfer"), 1, allowed=True)
    with pytest.raises(KeyboardInterrupt):
        fill_upload(None, c.questions[0], Box(), value, 1, guard)
    assert guard.journal.upload_record(1, 1)["state"] == "uncertain"
    with pytest.raises(ValueError, match="previous"):
        fill_upload(None, c.questions[0], Box(), value, 1, guard)
    assert len(calls) == 1


def test_upload_inspection_limits_from_client_descriptor():
    from test_inspector import URL, document, item

    from gformbot.config import config_from_schema
    from gformbot.inspector import parse_form_html

    raw = item(1, "Attachment", 13)
    raw[4][0].extend([None] * 5 + [["test-folder", [5], 2, "1024"]])
    form = parse_form_html(document([raw]), URL)
    assert not form.unsupported and form.requires_signin
    q = config_from_schema(form).questions[0]
    assert (q.upload_types, q.upload_max_files, q.upload_max_size) == ([5], 2, 1024)


def test_saved_upload_plan_preserves_confirmations_and_transfer_uncertainty(tmp_path):
    from gformbot import cli
    from gformbot.artifacts import digest
    from gformbot.state import Session, UncertainSubmission
    from gformbot.uploads import file_answer, manifest

    c = config()
    c.count = 2
    file = tmp_path / "sample.pdf"
    file.write_bytes(b"%PDF-test")
    value = file_answer(c.questions[0], [str(file)])
    path = cli._save_plan(c, [{1: value}, {1: value}], tmp_path / "plan.json", cleanup_on_success=False)
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 2
    session = Session(path, payload, digest(path))
    session.before_submit(1)
    session.confirm(1)
    session.journal.begin_upload(2, 1, manifest(value))
    file.unlink()
    before = path.read_bytes()
    restored, rows = cli._load_plan(path)
    assert restored._submitted_indices == [1]
    assert rows == [{1: value}, {1: value}]
    with pytest.raises(UncertainSubmission, match="previous transfer"):
        cli._track_plan(restored, rows, path, digest(path), interactive=False)
    assert session.journal.upload_record(2, 1)["state"] == "uploading"
    assert session.journal.states()[1] == "confirmed"
    assert path.read_bytes() == before


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable browser fixtures")
def test_local_upload_readback_and_no_repeat(html_page, tmp_path):
    from gformbot.browser import get_driver
    from gformbot.filler import execute_response
    from gformbot.uploads import UploadGuard, file_answer

    c = config()
    c.questions[0].required = False
    file = tmp_path / "sample.pdf"
    file.write_bytes(b"%PDF-test")
    answer = {1: file_answer(c.questions[0], [str(file)])}
    c.url = html_page(
        """<div role="listitem"><div role="heading">Attachment</div><input type="file" onchange="const q=this.parentElement;const token=document.createElement('input');token.type='hidden';token.name='entry.123';token.value='accepted-token';q.append(token);const label=document.createElement('span');label.textContent=this.files[0].name;q.append(label)"></div><button>Submit</button>"""
    )
    d = get_driver("chrome", headless=True)
    guard = UploadGuard(Journal("local-upload"), 1, allowed=True)
    try:
        execute_response(d, c, answer, submit=False, timeout=3, uploads=guard)
        assert guard.journal.upload_record(1, 1)["state"] == "verified"
        with pytest.raises(ValueError, match="review|previous|verified"):
            execute_response(d, c, answer, submit=False, timeout=3, uploads=guard)
    finally:
        d.quit()
