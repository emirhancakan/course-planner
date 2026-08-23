"""
Decides whether a fresh scrape is worth committing, and writes the commit message.

The dataset carries a "generatedAt" timestamp that changes on every run, so a
plain `git diff` would report a change every single time and produce a pointless
daily commit. This compares the "sections" payload only.

Also re-runs the dataset integrity checks, so a malformed scrape fails the job
instead of being published.

Usage:
    python detect_changes.py [semester]

Outputs:
    stdout                 human-readable summary
    $GITHUB_OUTPUT         changed=true|false, semester=<code>
    $COMMIT_MESSAGE_FILE   commit message (only when changed)

Exit codes:
    0  ran fine (whether or not anything changed)
    1  integrity check failed - do not publish
"""

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
VALID_DAYS = {"M", "T", "W", "Th", "F", "S"}


def data_filename(semester):
    return "courses-" + semester.replace("/", "-") + ".json"


def resolve_semester(argv):
    if len(argv) > 1:
        return argv[1]
    manifest = DATA_DIR / "manifest.json"
    semesters = json.loads(manifest.read_text(encoding="utf-8"))
    if not semesters:
        raise SystemExit("manifest.json is empty - nothing to compare")
    return semesters[0]


def read_committed(rel_path):
    """Returns the version of the file at HEAD, or None if it isn't tracked yet."""
    try:
        blob = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"],
            capture_output=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    return json.loads(blob.decode("utf-8"))


def check_integrity(data, semester):
    """The assertions that previously caught the Saturday ("St") parsing bug."""
    problems = []
    sections = data["sections"]

    seen = set()
    for s in sections:
        key = (s["code"], s["section"])
        if key in seen:
            problems.append(f"duplicate section {s['code']}.{s['section']}")
        seen.add(key)

    for s in sections:
        for m in s["meetings"]:
            if m["day"] not in VALID_DAYS:
                problems.append(f"{s['code']}.{s['section']}: invalid day {m['day']!r}")
            if not (1 <= m["slot"] <= 14):
                problems.append(f"{s['code']}.{s['section']}: invalid slot {m['slot']}")

    if not sections:
        problems.append("dataset contains no sections at all")
    else:
        # A column shift in the source table (the registrar inserted a "Quota"
        # column in 2026) makes the parser read instructor names as days, so
        # every meeting gets rejected and sections come back timetable-less
        # while still looking structurally valid. Roughly 54% of sections
        # legitimately have no meetings - thesis, seminars, internships - so a
        # 25% floor flags a systemic break without tripping on normal data.
        with_meetings = sum(1 for s in sections if s["meetings"])
        share = with_meetings / len(sections)
        if share < 0.25:
            problems.append(
                f"only {with_meetings} of {len(sections)} sections ({share:.1%}) have "
                "any meetings - the schedule columns were probably misread"
            )

    # the .js wrapper is what the site actually loads, so it must agree
    js_path = DATA_DIR / ("courses-" + semester.replace("/", "-") + ".js")
    if not js_path.exists():
        problems.append(f"missing JS wrapper {js_path.name}")
    else:
        js_text = js_path.read_text(encoding="utf-8")
        # The file is two statements; the payload is the object assigned in the
        # second one. Anchoring on "] = " avoids grabbing the "|| {}" on line 1.
        marker = "] = "
        idx = js_text.find(marker)
        try:
            if idx == -1:
                raise ValueError("assignment marker not found")
            payload = js_text[idx + len(marker):].strip().rstrip(";").strip()
            js_data = json.loads(payload)
            if len(js_data["sections"]) != len(sections):
                problems.append(
                    f"JS wrapper has {len(js_data['sections'])} sections, "
                    f"JSON has {len(sections)}"
                )
        except (ValueError, KeyError) as e:
            problems.append(f"could not parse JS wrapper: {e}")

    return problems


def diff_sections(old, new):
    o = {(s["code"], s["section"]): s for s in old["sections"]}
    n = {(s["code"], s["section"]): s for s in new["sections"]}
    ok, nk = set(o), set(n)

    added = sorted(nk - ok)
    removed = sorted(ok - nk)
    schedule, staffing = [], []
    for k in sorted(ok & nk):
        if o[k]["meetings"] == n[k]["meetings"]:
            continue
        slots_o = [(m["day"], m["slot"], m["room"]) for m in o[k]["meetings"]]
        slots_n = [(m["day"], m["slot"], m["room"]) for m in n[k]["meetings"]]
        (staffing if slots_o == slots_n else schedule).append(k)

    return {"o": o, "n": n, "added": added, "removed": removed,
            "schedule": schedule, "staffing": staffing}


def build_summary(d, old, new):
    """Commit message body: what changed, in the buckets that proved useful."""
    lines = []
    lines.append(f"Sections: {len(old['sections'])} -> {len(new['sections'])}")
    lines.append("")

    def name(k, src):
        return f"{k[0]}.{k[1]} {src[k]['name'][:52]}"

    if d["added"]:
        lines.append(f"Added ({len(d['added'])}):")
        lines += [f"  + {name(k, d['n'])}" for k in d["added"][:20]]
        if len(d["added"]) > 20:
            lines.append(f"  ... and {len(d['added']) - 20} more")
        lines.append("")

    if d["removed"]:
        lines.append(f"Removed ({len(d['removed'])}):")
        lines += [f"  - {name(k, d['o'])}" for k in d["removed"][:20]]
        if len(d["removed"]) > 20:
            lines.append(f"  ... and {len(d['removed']) - 20} more")
        lines.append("")

    if d["schedule"]:
        lines.append(f"Day/time/room changed ({len(d['schedule'])}):")
        for k in d["schedule"][:15]:
            def brief(ms):
                return ", ".join(f"{m['day']}{m['slot']}@{m['room'] or 'TBA'}" for m in ms) or "(none)"
            lines.append(f"  * {k[0]}.{k[1]}")
            lines.append(f"      was: {brief(d['o'][k]['meetings'])[:96]}")
            lines.append(f"      now: {brief(d['n'][k]['meetings'])[:96]}")
        if len(d["schedule"]) > 15:
            lines.append(f"  ... and {len(d['schedule']) - 15} more")
        lines.append("")

    if d["staffing"]:
        lines.append(f"Instructor/type changed ({len(d['staffing'])}):")
        for k in d["staffing"][:15]:
            oi = sorted({m["instructor"] for m in d["o"][k]["meetings"]})
            ni = sorted({m["instructor"] for m in d["n"][k]["meetings"]})
            lines.append(f"  ~ {k[0]}.{k[1]}: {', '.join(oi)[:40]} -> {', '.join(ni)[:40]}")
        if len(d["staffing"]) > 15:
            lines.append(f"  ... and {len(d['staffing']) - 15} more")
        lines.append("")

    od = Counter(s["departmentCode"] for s in old["sections"])
    nd = Counter(s["departmentCode"] for s in new["sections"])
    dept = [f"  {k}: {od.get(k, 0)} -> {nd.get(k, 0)}"
            for k in sorted(set(od) | set(nd)) if od.get(k, 0) != nd.get(k, 0)]
    if dept:
        lines.append("By department:")
        lines += dept

    return "\n".join(lines).rstrip()


def write_output(**pairs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in pairs.items():
            f.write(f"{k}={v}\n")


def main():
    semester = resolve_semester(sys.argv)
    rel_path = f"data/{data_filename(semester)}"
    new = json.loads((DATA_DIR / data_filename(semester)).read_text(encoding="utf-8"))

    problems = check_integrity(new, semester)
    if problems:
        print(f"Integrity check FAILED for {semester}:")
        for p in problems[:25]:
            print(f"  ! {p}")
        if len(problems) > 25:
            print(f"  ... and {len(problems) - 25} more")
        print("\nRefusing to publish this dataset.")
        sys.exit(1)
    print(f"Integrity check passed: {len(new['sections'])} sections, {semester}")

    old = read_committed(rel_path)
    if old is None:
        subject = f"Add course data for {semester}"
        body = f"First scrape of {semester}: {len(new['sections'])} sections."
        print(f"\n{subject} (no previous version tracked)")
        emit(semester, subject, body, changed=True)
        return

    d = diff_sections(old, new)
    total = len(d["added"]) + len(d["removed"]) + len(d["schedule"]) + len(d["staffing"])

    if total == 0:
        print("\nNo course changes since the last commit (only the timestamp moved).")
        write_output(changed="false", semester=semester)
        return

    body = build_summary(d, old, new)
    bits = []
    if d["added"]:
        bits.append(f"+{len(d['added'])}")
    if d["removed"]:
        bits.append(f"-{len(d['removed'])}")
    if d["schedule"]:
        bits.append(f"{len(d['schedule'])} rescheduled")
    if d["staffing"]:
        bits.append(f"{len(d['staffing'])} staffing")
    subject = f"Update {semester} course data ({', '.join(bits)})"

    print(f"\n{subject}\n")
    print(body)
    emit(semester, subject, body, changed=True)


def emit(semester, subject, body, changed):
    write_output(changed="true" if changed else "false", semester=semester)
    msg_file = os.environ.get("COMMIT_MESSAGE_FILE")
    if msg_file:
        Path(msg_file).write_text(f"{subject}\n\n{body}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
