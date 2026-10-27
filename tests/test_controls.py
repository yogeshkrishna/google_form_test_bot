import json
import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.environ.get("GFORMBOT_BROWSER_TESTS") != "1", reason="Enable browser fixtures"),
]


def test_localized_navigation_and_conflicts(html_page):
    from gformbot.browser import get_driver
    from gformbot.filler import _button

    ids = json.loads((Path(__file__).parent / "fixtures/google-controls.json").read_text())
    driver = get_driver("chrome", headless=True)
    try:
        driver.get(
            html_page(
                f'<button jsname="{ids["next"]}">अगला</button><button jsname="{ids["submit"]}">Enviar</button><button jsname="{ids["clear_form"]}">Borrar formulario</button>'
            )
        )
        assert _button(driver, "Next").text == "अगला"
        assert _button(driver, "Submit").text == "Enviar"
        driver.execute_script('document.querySelectorAll("button")[0].textContent="Submit"')
        with pytest.raises(RuntimeError, match="conflict|ambiguous"):
            _button(driver, "Submit")
        driver.get(
            html_page(
                f'<button jsname="{ids["submit"]}">Enviar</button><button jsname="{ids["submit"]}">Enviar</button>'
            )
        )
        with pytest.raises(RuntimeError, match="multiple|ambiguous"):
            _button(driver, "Submit")
    finally:
        driver.quit()


def test_localized_clear_uses_scoped_container(html_page):
    from gformbot.browser import get_driver
    from gformbot.config import QuestionConfig
    from gformbot.filler import _fill_question
    from gformbot.selenium_runtime import By

    q = QuestionConfig(1, "Choice", "radio", False, ["A"])
    driver = get_driver("chrome", headless=True)
    try:
        driver.get(
            html_page(
                '<div id="question"><input type="radio" value="A" checked><div jsname="YlCLKb"><button onclick="document.querySelector(\'input\').checked=false">Effacer la sélection</button></div></div>'
            )
        )
        _fill_question(driver, q, driver.find_element(By.ID, "question"), "", 3)
        assert not driver.find_element(By.CSS_SELECTOR, "input").is_selected()
    finally:
        driver.quit()
