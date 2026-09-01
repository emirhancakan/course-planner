"""
Scrapes course schedule data from registration.boun.edu.tr for a given semester
and writes it to ../data/courses-<semester>.json.

Usage:
    python scrape_boun.py "2026/2027-1"   # explicit semester
    python scrape_boun.py                 # newest semester, auto-detected
"""

import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://registration.boun.edu.tr"
SCHEDULE_URL = f"{BASE}/buis/General/schedule.aspx"
SCH_ASP_URL = f"{BASE}/scripts/sch.asp"
REQUEST_DELAY_SECONDS = 0.6
# Per-request retries: 5s then 20s of backoff before a department is parked.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 5
# Parked departments are retried in later rounds. The registrar has returned
# 500s for a single department for a minute or more, which used to fail the
# whole run; spacing the retries by minutes rides that out.
RECOVERY_ROUNDS = 2
RECOVERY_PAUSE_SECONDS = 120
# If this many departments fail at once the site itself is unwell, not flaky -
# retry rounds would just burn the job's time budget, so fail straight away.
MAX_RECOVERABLE_FAILURES = 8
HEADERS = {"User-Agent": "BounCoursePlannerPersonalScript/1.0 (personal semester-planning tool)"}

# The registrar writes days as concatenated tokens, e.g. "MMT" or "StSt".
# Two-letter tokens must be matched before single letters, otherwise "St"
# (Saturday) is read as "S" + "t" and every day/hour pair after it shifts.
TWO_CHAR_DAYS = {"Th": "Th", "St": "S"}
VALID_DAYS = {"M", "T", "W", "Th", "F", "S"}

SLOT_TIMES = {
    1: ("09:00", "09:50"), 2: ("10:00", "10:50"), 3: ("11:00", "11:50"),
    4: ("12:00", "12:50"), 5: ("13:00", "13:50"), 6: ("14:00", "14:50"),
    7: ("15:00", "15:50"), 8: ("16:00", "16:50"), 9: ("17:00", "17:50"),
    10: ("18:00", "18:50"), 11: ("19:00", "19:50"), 12: ("20:00", "20:50"),
    13: ("21:00", "21:50"), 14: ("22:00", "22:50"),
}


SEMESTER_RE = re.compile(r"^\d{4}/\d{4}-\d$")


def get_latest_semester():
    """Returns the newest semester code offered by the registrar, e.g. "2026/2027-1".

    The semester dropdown renders on a plain GET, newest option first. The page
    does carry a reCAPTCHA, but it only gates the POST that lists departments -
    reading the option values needs no submit at all.
    """
    r = requests.get(SCHEDULE_URL, params={"p": "semester"}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    select = soup.find("select", attrs={"name": re.compile("ddlSemester")})
    options = [o.get("value", "").strip() for o in select.find_all("option")] if select else []
    options = [o for o in options if SEMESTER_RE.match(o)]
    if not options:
        raise RuntimeError(
            "could not read the semester list from the schedule page - the page "
            "layout may have changed; pass a semester explicitly to work around it"
        )
    return options[0]


def get_departments():
    """Returns the list of {kisaadi, bolum} dicts for all departments/programs.

    The schedule.aspx picker page that normally lists these is protected by an
    invisible reCAPTCHA on its POST, which a plain requests.Session can't solve.
    The department list is stable (institutional structure, not semester data),
    so it's captured once via a real browser and cached in data/departments.json
    rather than re-derived on every run.
    """
    path = Path(__file__).resolve().parent.parent / "data" / "departments.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _cell_text(td):
    return td.get_text(strip=True).replace("\xa0", "")


def _parse_days(days_str):
    days = []
    i = 0
    while i < len(days_str):
        two = days_str[i:i + 2]
        if two in TWO_CHAR_DAYS:
            days.append(TWO_CHAR_DAYS[two])
            i += 2
        else:
            days.append(days_str[i])
            i += 1
    return days


def _parse_hours(hours_str, num_days):
    """Hour slots are 1-14; slots >= 10 are two digits, so a raw string like
    '8910' is ambiguous without knowing how many days it must split into.
    Try every valid tokenization and keep the one matching num_days slots."""
    candidates = [[]]
    for ch in hours_str:
        next_candidates = []
        for tokens in candidates:
            # option 1: ch starts a new single-digit slot (or continues one)
            next_candidates.append(tokens + [ch])
            # option 2: ch is the second digit of a "1x" two-digit slot
            if tokens and tokens[-1] == "1":
                next_candidates.append(tokens[:-1] + [tokens[-1] + ch])
        candidates = next_candidates

    def usable(tokens):
        return all(t.isdigit() and 1 <= int(t) <= 14 for t in tokens)

    valid = [c for c in candidates if len(c) == num_days and usable(c)]
    if valid:
        return [int(x) for x in valid[0]]
    # Nothing tokenised cleanly. Fall back to one character per day, using -1 for
    # anything non-numeric so the caller's range check rejects and reports it -
    # a surprise value here must not take down the whole run.
    return [int(x) if x.isdigit() else -1 for x in hours_str[:num_days]]


def _parse_rooms(td, expected_count):
    spans = [s.get_text(strip=True) for s in td.find_all("span") if s.get("onclick")]
    if not spans:
        text = _cell_text(td)
        spans = [text] if text else []
    if len(spans) < expected_count:
        spans = spans + [spans[-1] if spans else ""] * (expected_count - len(spans))
    return spans[:expected_count]


# Header label -> the field name this scraper uses for it.
COLUMN_LABELS = {
    "code.sec": "code",
    "name": "name",
    "cr.": "credit",
    "ects": "ects",
    "instr.": "instructor",
    "days": "days",
    "hours": "hours",
    "rooms": "rooms",
}
REQUIRED_COLUMNS = set(COLUMN_LABELS.values())


class TableLayoutError(RuntimeError):
    """Raised when the schedule table's columns can't be identified."""


def find_column_map(soup):
    """Maps field name -> column index by reading the table's header row.

    Column positions are not fixed: the registrar inserted a "Quota" column at
    index 5 partway through 2026, which shifted Instr./Days/Hours/Rooms one to
    the right and silently broke every hard-coded index. Reading the header
    keeps the parser working across that kind of change instead of quietly
    mis-assigning fields.
    """
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        labels = [_cell_text(c).lower() for c in cells]
        if "code.sec" not in labels or "days" not in labels:
            continue
        mapping = {}
        for i, label in enumerate(labels):
            field = COLUMN_LABELS.get(label)
            if field and field not in mapping:
                mapping[field] = i
        missing = REQUIRED_COLUMNS - set(mapping)
        if missing:
            raise TableLayoutError(
                f"schedule table is missing expected column(s): {sorted(missing)}; "
                f"header was {labels}"
            )
        return mapping
    raise TableLayoutError("could not find the schedule table's header row")


def parse_department_courses(html, department_code, department_name):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr", class_=re.compile(r"schtd"))
    if not rows:
        return []

    col = find_column_map(soup)
    min_cells = max(col.values()) + 1

    sections = []
    current = None

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < min_cells:
            continue

        code_sec = _cell_text(cells[col["code"]])
        is_continuation = "labps" in (row.get("class") or [])

        if code_sec and not is_continuation:
            code, _, sec = code_sec.partition(".")
            current = {
                "code": code,
                "section": sec,
                "department": department_name,
                "departmentCode": department_code,
                "name": _cell_text(cells[col["name"]]),
                "credit": _to_number(_cell_text(cells[col["credit"]])),
                "ects": _to_number(_cell_text(cells[col["ects"]])),
                "meetings": [],
            }
            sections.append(current)
            meeting_type = "LECT"
        else:
            if current is None:
                continue
            meeting_type = _cell_text(cells[col["name"]]) or "LECT"

        instructor = _cell_text(cells[col["instructor"]])
        days_str = _cell_text(cells[col["days"]])
        hours_str = _cell_text(cells[col["hours"]])
        if not days_str or not hours_str:
            continue

        days = _parse_days(days_str)
        hours = _parse_hours(hours_str, len(days))
        rooms = _parse_rooms(cells[col["rooms"]], len(days))

        for day, slot, room in zip(days, hours, rooms):
            # A day or slot outside the known range means the day/hour strings
            # were mis-tokenized, which silently drops the meeting from the
            # timetable. Surface it loudly instead of writing bad data.
            if day not in VALID_DAYS or not (1 <= slot <= 14):
                print(f"  ! unparsable meeting on {current['code']}.{current['section']}: "
                      f"days={days_str!r} hours={hours_str!r} -> day={day!r} slot={slot}")
                continue

            meeting = {
                "type": meeting_type,
                "instructor": instructor,
                "day": day,
                "slot": slot,
                "room": room,
            }
            # the registrar's own table occasionally lists one lab/PS group as
            # two identical rows; treat an exact repeat as the same meeting,
            # not a self-conflict
            if meeting not in current["meetings"]:
                current["meetings"].append(meeting)

    return sections


def _to_number(text):
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


class ScrapeIncompleteError(RuntimeError):
    """Raised when one or more departments could not be fetched, so the run
    must not overwrite good existing data with a partial snapshot."""


def _is_retryable(error):
    """Connection problems, timeouts, 429 and 5xx are worth another go; a plain
    4xx means the URL itself is wrong and retrying just wastes requests."""
    response = getattr(error, "response", None)
    if response is None:
        return True  # connection reset, DNS, timeout
    return response.status_code == 429 or response.status_code >= 500


def fetch_department(session, semester, dept, attempts=MAX_ATTEMPTS):
    """Fetches one department's schedule page, retrying transient failures.

    The registrar's server intermittently resets connections and returns 500s
    for a single department for a stretch of time. Backoff is exponential so a
    short server-side wobble is ridden out rather than failing the whole run.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(
                SCH_ASP_URL,
                params={"donem": semester, "kisaadi": dept["kisaadi"], "bolum": dept["bolum"]},
                headers=HEADERS,
                timeout=30,
            )
            r.raise_for_status()
            r.encoding = r.apparent_encoding
            return r.text
        except requests.RequestException as e:
            last_error = e
            if not _is_retryable(e):
                print(f"  ! {dept['kisaadi']}: {e} (not retryable)")
                raise
            if attempt < attempts:
                backoff = RETRY_BACKOFF_SECONDS * (4 ** (attempt - 1))
                print(f"  ! attempt {attempt}/{attempts} failed ({e}); retrying in {backoff:.0f}s")
                time.sleep(backoff)
    raise last_error


def scrape_semester(semester, department_filter=None):
    session = requests.Session()
    departments = get_departments()
    if department_filter:
        departments = [d for d in departments if d["kisaadi"] in department_filter]

    total = len(departments)
    pages = {}       # department index -> html
    pending = list(enumerate(departments))
    errors = {}

    for attempt_round in range(1, RECOVERY_ROUNDS + 2):
        if attempt_round > 1:
            print(f"\n{len(pending)} department(s) still missing; waiting "
                  f"{RECOVERY_PAUSE_SECONDS}s before retry round {attempt_round - 1}"
                  f"/{RECOVERY_ROUNDS}...")
            time.sleep(RECOVERY_PAUSE_SECONDS)

        still_pending = []
        for i, dept in pending:
            label = f"[{i + 1}/{total}] {dept['kisaadi']} - {dept['bolum']}"
            print(label if attempt_round == 1 else f"(retry) {label}")
            time.sleep(REQUEST_DELAY_SECONDS)
            try:
                pages[i] = fetch_department(session, semester, dept)
                errors.pop(i, None)
            except requests.RequestException as e:
                print(f"  ! giving up on {dept['kisaadi']} for now: {e}")
                errors[i] = f"{dept['kisaadi']} ({dept['bolum']}): {e}"
                still_pending.append((i, dept))

        pending = still_pending
        if not pending:
            break
        if len(pending) > MAX_RECOVERABLE_FAILURES:
            raise ScrapeIncompleteError(
                f"{len(pending)} departments failed in one pass, which looks like a "
                "site-wide outage rather than a transient error; not retrying:\n  - "
                + "\n  - ".join(errors[i] for i, _ in pending[:10])
            )

    if pending:
        raise ScrapeIncompleteError(
            "these departments could not be fetched, so the dataset would be "
            "incomplete:\n  - " + "\n  - ".join(errors[i] for i, _ in pending)
        )

    # Assemble strictly in department order, independent of which round fetched
    # each page, so a retry can't reshuffle which department a shared course is
    # credited to and produce a phantom diff.
    all_sections = []
    seen_codes = set()
    for i, dept in enumerate(departments):
        sections = parse_department_courses(pages[i], dept["kisaadi"], dept["bolum"])
        # program-variant duplicates (e.g. "with thesis" listings) can repeat
        # the same course table verbatim; keep only the first occurrence
        for s in sections:
            key = (s["code"], s["section"])
            if key not in seen_codes:
                seen_codes.add(key)
                all_sections.append(s)

    return {
        "semester": semester,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "slotTimes": {str(k): {"start": v[0], "end": v[1]} for k, v in SLOT_TIMES.items()},
        "sections": all_sections,
    }


def write_js_wrapper(data, out_dir, semester):
    """Opening index.html directly (file://) can't fetch() local JSON in
    Chrome, but <script src> loads local files fine - so also emit a JS
    file that assigns the dataset onto a global the page can read."""
    js_path = out_dir / ("courses-" + semester.replace("/", "-") + ".js")
    payload = json.dumps(data, ensure_ascii=False)
    js_path.write_text(
        "window.COURSE_DATASETS = window.COURSE_DATASETS || {};\n"
        f"window.COURSE_DATASETS[{json.dumps(semester)}] = {payload};\n",
        encoding="utf-8",
    )


def update_manifest(out_dir, semester):
    manifest_path = out_dir / "manifest.json"
    semesters = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    if semester not in semesters:
        semesters.append(semester)
    semesters.sort(reverse=True)
    manifest_path.write_text(json.dumps(semesters, ensure_ascii=False, indent=2), encoding="utf-8")

    js_path = out_dir / "manifest.js"
    js_path.write_text(
        f"window.AVAILABLE_SEMESTERS = {json.dumps(semesters, ensure_ascii=False)};\n",
        encoding="utf-8",
    )


def main():
    if len(sys.argv) > 1:
        semester = sys.argv[1]
        if not SEMESTER_RE.match(semester):
            print(f'Invalid semester {semester!r}. Expected a code like "2026/2027-1".')
            sys.exit(1)
        print(f"Semester: {semester} (from argument)")
    else:
        try:
            semester = get_latest_semester()
        except (requests.RequestException, RuntimeError) as e:
            print(f"Could not detect the current semester: {e}")
            sys.exit(1)
        print(f"Semester: {semester} (auto-detected as newest)")

    try:
        data = scrape_semester(semester)
    except ScrapeIncompleteError as e:
        print(f"\nAborted - {e}")
        print("Existing data files were left untouched. Re-run to try again.")
        sys.exit(1)
    except TableLayoutError as e:
        print(f"\nAborted - {e}")
        print("The registrar's table layout changed; the column mapping in "
              "COLUMN_LABELS needs updating. Existing data files were left untouched.")
        sys.exit(1)

    out_dir = Path(__file__).resolve().parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    filename = "courses-" + semester.replace("/", "-") + ".json"
    out_path = out_dir / filename
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_js_wrapper(data, out_dir, semester)
    update_manifest(out_dir, semester)
    print(f"\nWrote {len(data['sections'])} sections to {out_path}")


if __name__ == "__main__":
    main()
