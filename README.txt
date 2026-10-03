GOOGLE FORM TEST-COPY FILLER
==============================

This folder is configured only for the supplied:
"Neighbourhood Heat & Shade | DTL Survey" Google Form.

WHAT IT DOES
------------
- Uses the attached 109-response dataset as a structured response pool.
- Keeps only Q1-Q13 from that pool; timestamps and old free-text answers are excluded.
- Biases profiles toward college students.
- Opens a brand-new Chrome Incognito session for each response.
- Fills the form, submits it, closes Chrome, then repeats.
- Writes bot_log.csv so you can see which runs succeeded.
- Stops immediately if the form layout changes or Selenium hits an error.

SETUP - FIRST TIME ONLY
-----------------------
1. Make sure Python and Google Chrome are installed.
2. Double-click install.bat.
3. Selenium 4 will use Selenium Manager to obtain/use a compatible ChromeDriver automatically.

RUN
---
1. Double-click run_bot.bat.
2. Choose:
      1 = dry-run preview, no browser submission
      2 = actually submit to the TEST COPY
3. Enter the number of responses.

SETTINGS
--------
At the top of form_bot.py:
- STUDENT_BIAS = 0.85
  Increase toward 1.0 for more student profiles.
- MIN_DELAY / MAX_DELAY
  Controls the small delay between clicks.
- MAX_RUNS = 200
  Guard against accidentally launching a massive run.

NOTES
-----
- It is deliberately hard-coded to this one form URL.
- It does NOT use stealth/anti-bot bypasses.
- If Google changes the Form HTML or you edit question/answer wording, the bot may stop.
- If the very first run fails, read the ERROR line in the console and bot_log.csv.
