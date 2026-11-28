import csv
import json

from gformbot.config import QuestionConfig, RunConfig
from gformbot.csv_prompt import csv_prompt
from gformbot.generator import load_reference


def test_prompt_headers_and_other_branch_match_csv_import(tmp_path):
    config = RunConfig(
        "https://docs.google.com/forms/d/e/csv-fixture/viewform",
        "Workshop",
        sections=["Track", "Workshop date"],
        count=2,
        form_description="Choose a workshop date or suggest another plan.",
        questions=[
            QuestionConfig(
                1, "Track", "radio", True, ["Workshop"], entry_id="17", allow_other=True, other_branch=0
            ),
            QuestionConfig(2, "Workshop date", "date", True, page=2),
        ],
    )
    prompt = csv_prompt(config)
    _, output = prompt.split("Exact CSV header:\n")
    header, form = output.split("\n\nForm data:\n")
    fields = json.loads(form)
    assert fields["description"] == config.form_description
    assert fields["columns"]["entry.17"]["other_next_section"] == 0
    assert fields["columns"]["Q2"]["format"] == "YYYY-MM-DD"

    path = tmp_path / "responses.csv"
    other = {"choices": [], "other": "Later, please"}
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(next(csv.reader([header])))
        writer.writerow([json.dumps(other), ""])
        writer.writerow(["Workshop", "2028-06-05"])
    assert load_reference(config, path) == [{1: other, 2: ""}, {1: "Workshop", 2: "2028-06-05"}]
