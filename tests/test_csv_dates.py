import pytest

from gformbot import cli, generator
from gformbot.config import QuestionConfig, RunConfig
from gformbot.validation import AmbiguousDateError, normalize_csv_dates


def date_question(year=True, time=True):
    return QuestionConfig(1, "Date needed", "date", date_include_year=year, date_include_time=time)


def test_entire_google_export_column_resolves_ambiguous_first_row():
    assert normalize_csv_dates(date_question(), ["10/5/2023 14:30:00", "", "11/20/2023 9:00:00"]) == [
        "2023-10-05 14:30",
        "",
        "2023-11-20 09:00",
    ]


def test_day_first_export_resolves_whole_column():
    assert normalize_csv_dates(date_question(), ["5/10/2023 14:30:00", "20/11/2023 9:00:00"]) == [
        "2023-10-05 14:30",
        "2023-11-20 09:00",
    ]


def test_ambiguous_dates_require_explicit_order():
    values = ["10/5/2023 14:30:00", "10/6/2023 09:00:00"]
    with pytest.raises(AmbiguousDateError):
        normalize_csv_dates(date_question(), values)
    assert normalize_csv_dates(date_question(), values, "mdy")[0] == "2023-10-05 14:30"
    assert normalize_csv_dates(date_question(), values, "dmy")[0] == "2023-05-10 14:30"


@pytest.mark.parametrize(
    "value,canonical",
    [
        ("2023-10-15 14:30:00", "2023-10-15 14:30"),
        ("2023-10-15T14:30:00", "2023-10-15 14:30"),
        ("10/15/2023 2:30:00 PM", "2023-10-15 14:30"),
        ("10/15/2023 12:05 AM", "2023-10-15 00:05"),
        ("10/15/2023 12:05 PM", "2023-10-15 12:05"),
        (" 10/15/2023 9:00:00 ", "2023-10-15 09:00"),
        ("01/01/0001 0:00:00", "0001-01-01 00:00"),
    ],
)
def test_export_formats_and_clock(value, canonical):
    assert normalize_csv_dates(date_question(), [value]) == [canonical]


@pytest.mark.parametrize(
    "value",
    [
        "2/30/2023 14:30:00",
        "10/15/2023 14:30:05",
        "10/15/2023",
        "10/15/23 14:30",
        "10/15/2023 25:00",
        "10/15/2023 14:30 PM",
    ],
)
def test_invalid_or_lossy_dates_rejected(value):
    with pytest.raises(ValueError, match="CSV row 2"):
        normalize_csv_dates(date_question(), [value])


def test_mixed_date_orders_cannot_be_guessed_per_row():
    with pytest.raises(ValueError):
        normalize_csv_dates(date_question(), ["10/15/2023 14:30:00", "20/11/2023 14:30:00"])


def test_date_only_and_yearless_exports():
    assert normalize_csv_dates(date_question(time=False), ["10/15/2023"]) == ["2023-10-15"]
    assert normalize_csv_dates(date_question(year=False), ["10/15 14:30:00"]) == ["10-15 14:30"]
    assert normalize_csv_dates(date_question(year=False, time=False), ["02-29"]) == ["02-29"]


def test_import_preserves_input_file_and_optional_blanks(tmp_path):
    c = RunConfig("https://docs.google.com/forms/d/e/test/viewform", questions=[date_question()])
    path = tmp_path / "pool.csv"
    path.write_text('Date needed\n10/15/2023 14:30:00\n""\n', encoding="utf-8")
    before = path.read_bytes()
    assert generator.load_reference(c, path) == [{1: "2023-10-15 14:30"}, {1: ""}]
    assert path.read_bytes() == before


def test_import_only_prompts_date_order_for_ambiguous_column(tmp_path, monkeypatch):
    c = RunConfig("https://docs.google.com/forms/d/e/test/viewform", questions=[date_question()])
    path = tmp_path / "pool.csv"
    path.write_text("Date needed\n10/5/2023 14:30:00\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_ask_select", lambda *args: "mdy")
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *args: pytest.fail("Date errors must not offer checkbox repair")
    )
    assert cli._import_profiles(c, path) == [{1: "2023-10-05 14:30"}]
    assert c.csv_date_order == "mdy"


@pytest.mark.parametrize(
    "question,value",
    [
        (date_question(), "invalid date"),
        (QuestionConfig(1, "Email", "text", validation=[[2, 102]]), "bad-email"),
    ],
)
def test_other_import_errors_never_offer_checkbox_repair(tmp_path, monkeypatch, question, value):
    c = RunConfig("https://docs.google.com/forms/d/e/test/viewform", questions=[question])
    path = tmp_path / "pool.csv"
    path.write_text(f"Q1\n{value}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *args: pytest.fail("Unrelated repair offered"))
    with pytest.raises(ValueError):
        cli._import_profiles(c, path)


def test_checkbox_repair_still_available_when_applicable(tmp_path, monkeypatch):
    q = QuestionConfig(1, "Choices", "checkbox", options=["A", "B"])
    c = RunConfig("https://docs.google.com/forms/d/e/test/viewform", questions=[q])
    path = tmp_path / "pool.csv"
    generator.write_csv(c, [{1: ["A"]}], path)
    path.write_text(path.read_text().replace('[""A""]', '[""A"", ""A""]'))
    prompts = []
    monkeypatch.setattr(cli, "_ask_confirm", lambda message: prompts.append(message) or True)
    assert cli._import_profiles(c, path) == [{1: ["A"]}]
    assert len(prompts) == 1 and c._repairs
