import csv
import random
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSeWny-vNgIV_A52XH-HA9g6MeW_-7cfic9iODddNT8qVzgAiw/viewform?usp=sharing&ouid=111763923918447360737"

# 0.0 = use the old response pool exactly
# 1.0 = almost always make the profile a student
STUDENT_BIAS = 0.85

# Human-ish pacing. This is only to make the test easier to watch/debug.
MIN_DELAY = 0.25
MAX_DELAY = 0.75
PAGE_DELAY = (0.8, 1.4)

# Safety guard against accidentally launching thousands of test submissions.
MAX_RUNS = 200

BASE = Path(__file__).resolve().parent
POOL_FILE = BASE / "response_pool.csv"
LOG_FILE = BASE / "bot_log.csv"

Q = [
    "1. What best describes your main daily activity?",
    "2. Which best describes the neighbourhood you are thinking about?",
    "3. On how many of the past 7 days did you spend at least 10 minutes outdoors in this neighbourhood during daylight?",
    "4. On a typical day you were outdoors there, about how much total time did you spend outside during daylight?",
    "5. When did outdoor heat feel strongest there?",
    "6. During the warmest part of the day you experienced, how uncomfortable did the heat feel in an UNSHADED place?",
    "7. Around a similar time of day, how uncomfortable did the heat feel in a nearby SHADED place?",
    "8. Which kinds of places felt hottest? Select all that apply. If choosing the last option, choose it alone.",
    "9. When you needed shade along your usual outdoor routes, how often could you find it nearby?",
    "10. What provided usable shade in the places you visited? Select all that apply. If choosing either of the last two options, choose it alone.",
    "11. How often did outdoor heat make you change your route, timing or activity in the past 7 days?",
    "12. Which improvements would help most in this neighbourhood? Choose up to 3. If choosing the last option, choose it alone.",
    "13. Would you be willing to contribute simple observations for a neighbourhood heat map? No contact details or commitment are needed here.",
    "14. Optional: Name one public place that needs more shade. A broad area/city and public landmark are enough; do not include a home address or live location.",
    "15. Optional: Any other comments or ideas for making outdoor spaces cooler and more comfortable? Please leave out names, contact details and other personal information.",
]

MULTI_OPTIONS = {
    8: [
        "Roads, footpaths or crossings",
        "Bus stops or transport waiting areas",
        "Open parking areas",
        "Markets or shopping streets",
        "Playgrounds or sports grounds",
        "Open areas around schools or colleges",
        "Parks or public seating with little shade",
        "Other public outdoor spaces",
        "No particular hot spot / not sure",
    ],
    10: [
        "Trees",
        "Bus shelters or covered waiting areas",
        "Building shade, verandas or covered walkways",
        "Canopies, awnings or shade sails",
        "Other shade structures",
        "No usable shade",
        "Not sure / I was not outdoors",
    ],
    12: [
        "More shade trees along walking routes",
        "Better care for existing trees",
        "Covered bus stops or waiting areas",
        "Shade structures over seating or gathering spaces",
        "Covered walkways",
        "More shaded parks or play areas",
        "A map showing shaded routes and cooler public spaces",
        "No changes needed / not sure",
    ],
}

Q14_TEXT = [
    "",
    "",
    "",
    "",
    "college bus stop",
    "campus main gate area",
    "open ground near the college",
    "bus stop near the campus",
    "walking path outside the campus",
    "college sports ground",
]

Q15_TEXT = [
    "",
    "",
    "",
    "",
    "",
    "",
    "more trees near walking paths",
    "covered bus stops would help",
    "more shaded seating areas",
    "plant more trees around open spaces",
    "covered walkways near busy areas",
]


def xpath_literal(text):
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ', "\'", '.join(f"'{p}'" for p in parts) + ")"


def short_sleep():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def read_pool():
    with POOL_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError("response_pool.csv is empty")
    return rows


def weighted_choice(items):
    values, weights = zip(*items)
    return random.choices(values, weights=weights, k=1)[0]


def sanitize_multi(qnum, combined):
    if not combined:
        return []

    options = MULTI_OPTIONS[qnum]
    selected = [opt for opt in options if opt in combined]

    if qnum == 8 and "No particular hot spot / not sure" in selected:
        return ["No particular hot spot / not sure"]

    if qnum == 10:
        if "Not sure / I was not outdoors" in selected:
            return ["Not sure / I was not outdoors"]
        if "No usable shade" in selected:
            return ["No usable shade"]

    if qnum == 12:
        if "No changes needed / not sure" in selected:
            return ["No changes needed / not sure"]
        selected = selected[:3]

    return selected


def make_response(pool):
    raw = random.choice(pool)
    ans = {i + 1: raw.get(Q[i], "") for i in range(13)}

    if random.random() < STUDENT_BIAS:
        ans[1] = weighted_choice([
            ("Studying", 88),
            ("A mix of indoor and outdoor work", 7),
            ("Mostly indoor work", 5),
        ])

        ans[2] = weighted_choice([
            ("Mainly a school or college campus", 55),
            ("Mixed residential and commercial area", 20),
            ("Mainly residential", 18),
            ("Mainly shops, offices or markets", 5),
            ("Other / not sure", 2),
        ])

    for qnum in (8, 10, 12):
        ans[qnum] = sanitize_multi(qnum, ans[qnum])

    ans[14] = random.choice(Q14_TEXT)
    ans[15] = random.choice(Q15_TEXT)
    return ans


def chrome_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--incognito")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    return webdriver.Chrome(options=options)


def question_box(driver, question, timeout=2):
    q = xpath_literal(question)
    candidates = [
        f"//*[normalize-space(text())={q}]/ancestor::*[@role='listitem'][1]",
        f"//*[normalize-space(.)={q}]/ancestor::*[@role='listitem'][1]",
    ]

    for xp in candidates:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.XPATH, xp))
            )
        except TimeoutException:
            pass
    return None


def click_option(driver, box, value):
    lit = xpath_literal(value)
    xpaths = [
        f".//*[@role='radio' and @aria-label={lit}]",
        f".//*[@role='checkbox' and @aria-label={lit}]",
        f".//*[normalize-space(text())={lit}]/ancestor::*[@role='radio'][1]",
        f".//*[normalize-space(text())={lit}]/ancestor::*[@role='checkbox'][1]",
        f".//*[normalize-space(.)={lit}]/ancestor::*[@role='radio'][1]",
        f".//*[normalize-space(.)={lit}]/ancestor::*[@role='checkbox'][1]",
    ]

    for xp in xpaths:
        try:
            el = box.find_element(By.XPATH, xp)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            short_sleep()
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            return
        except NoSuchElementException:
            pass

    raise RuntimeError(f"Could not find option: {value}")


def fill_text(driver, box, value):
    if not value:
        return

    fields = box.find_elements(By.XPATH, ".//input[not(@type='hidden')] | .//textarea")
    if not fields:
        raise RuntimeError("Could not find text field")

    field = fields[0]
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
    short_sleep()
    field.send_keys(value)


def click_button(driver, label):
    lit = xpath_literal(label)
    xpaths = [
        f"//*[@role='button'][.//*[normalize-space(text())={lit}]]",
        f"//*[@role='button' and normalize-space(.)={lit}]",
        f"//span[normalize-space(text())={lit}]/ancestor::*[@role='button'][1]",
    ]

    for xp in xpaths:
        try:
            button = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, xp))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
            short_sleep()
            button.click()
            return
        except TimeoutException:
            pass

    raise RuntimeError(f"Could not find {label} button")


def fill_one(driver, qnum, value):
    box = question_box(driver, Q[qnum - 1], timeout=2)
    if box is None:
        return False

    if qnum in MULTI_OPTIONS:
        for item in value:
            click_option(driver, box, item)
    elif qnum in (14, 15):
        fill_text(driver, box, value)
    else:
        click_option(driver, box, value)

    short_sleep()
    return True


def submit_response(answer):
    driver = chrome_driver()
    try:
        driver.get(FORM_URL)
        WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(random.uniform(*PAGE_DELAY))

        qnum = 1
        while qnum <= 15:
            filled_this_page = 0

            while qnum <= 15 and fill_one(driver, qnum, answer[qnum]):
                qnum += 1
                filled_this_page += 1

            if qnum <= 15:
                if filled_this_page == 0:
                    raise RuntimeError(
                        f"Question {qnum} was not found. The form layout may have changed."
                    )

                click_button(driver, "Next")
                time.sleep(random.uniform(*PAGE_DELAY))

                if question_box(driver, Q[qnum - 1], timeout=8) is None:
                    raise RuntimeError(
                        f"Could not reach question {qnum}. "
                        "Check Chrome for a validation message or changed form layout."
                    )

        click_button(driver, "Submit")

        WebDriverWait(driver, 10).until(
            lambda d: "response has been recorded" in d.page_source.lower()
            or "submit another response" in d.page_source.lower()
            or "viewform" not in d.current_url.lower()
        )

        return True, "submitted"

    finally:
        time.sleep(0.5)
        driver.quit()


def log_result(index, answer, ok, message):
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "run", "status", "message", "q1", "q2"])
        w.writerow([
            datetime.now().isoformat(timespec="seconds"),
            index,
            "OK" if ok else "ERROR",
            message,
            answer.get(1, ""),
            answer.get(2, ""),
        ])


def show_preview(answer):
    print()
    for i in range(1, 16):
        print(f"Q{i}: {answer[i]}")
    print()


def main():
    pool = read_pool()

    print("Neighbourhood Heat & Shade - TEST COPY filler")
    print("Hard-coded form:", FORM_URL)
    print()
    print("1 = dry run / preview only")
    print("2 = actually submit test responses")
    mode = input("Choose 1 or 2: ").strip()

    try:
        count = int(input(f"How many responses? (1-{MAX_RUNS}): ").strip())
    except ValueError:
        print("Invalid number.")
        return

    if count < 1 or count > MAX_RUNS:
        print(f"Use a number from 1 to {MAX_RUNS}.")
        return

    if mode == "1":
        for _ in range(count):
            show_preview(make_response(pool))
        return

    if mode != "2":
        print("Invalid mode.")
        return

    print()
    print("Chrome will open in Incognito, submit once, close, then repeat.")
    print("Press Ctrl+C any time to stop.")
    print()

    for i in range(1, count + 1):
        answer = make_response(pool)
        print(f"[{i}/{count}] launching...")
        try:
            ok, message = submit_response(answer)
            log_result(i, answer, ok, message)
            print(f"[{i}/{count}] submitted")
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            message = str(e)
            log_result(i, answer, False, message)
            print(f"[{i}/{count}] ERROR: {message}")
            print("Stopped so it does not keep failing blindly.")
            break


if __name__ == "__main__":
    main()
