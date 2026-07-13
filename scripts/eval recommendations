"""
eval_recommendations.py

Lightweight eval harness for the Hardcover recommendation pipeline.
Run this after generate_recommendations() to check output quality
before it ships to the site.

Checks:
  1. Schema validity      -- exactly 8 recs, all required fields present/non-empty
  2. Already-read overlap -- flags recs that fuzzy-match something already read
  3. Want-to-read overlap -- flags recs that fuzzy-match something already on the list
  4. Repetition            -- flags recs that showed up in recent past runs

Usage:
    python scripts/eval_recommendations.py

Expects these files to exist in data/:
    results.json                 (want-to-read snapshot, written by fetch_availability.py)
    read_titles.json             (already-read snapshot -- see note below)
    recommendations.json         (latest recommendation run)
    recommendation_history.json  (rolling history -- auto-created if missing)

NOTE: read_titles.json and recommendation_history.json aren't written by the
current version of fetch_availability.py. See the accompanying diff to persist them.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher

DATA_DIR = "data"
FUZZY_THRESHOLD = 0.85  # 0-1, higher = stricter match required
REPETITION_LOOKBACK = 5  # how many past runs to check for repeats


def normalize(s):
    return re.sub(r'[^a-z0-9\s]', '', s.lower()).strip()


def book_key(title, author):
    return f"{normalize(title)} by {normalize(author)}"


def fuzzy_match(a, b, threshold=FUZZY_THRESHOLD):
    return SequenceMatcher(None, a, b).ratio() >= threshold


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


# --- Check 1: Schema validity ---
def check_schema(recs):
    issues = []
    if len(recs) != 8:
        issues.append(f"Expected 8 recommendations, got {len(recs)}")
    for i, rec in enumerate(recs):
        for field in ("title", "author", "reason"):
            val = rec.get(field, "")
            if not isinstance(val, str) or not val.strip():
                issues.append(f"Rec #{i+1}: missing or empty '{field}'")
    return issues


# --- Check 2: Already-read overlap ---
def check_already_read(recs, read_titles):
    issues = []
    read_keys = [normalize(t) for t in read_titles]
    for rec in recs:
        key = book_key(rec.get("title", ""), rec.get("author", ""))
        for read_key in read_keys:
            if fuzzy_match(key, read_key):
                issues.append(
                    f"'{rec.get('title')}' by {rec.get('author')} closely matches "
                    f"an already-read title: '{read_key}'"
                )
                break
    return issues


# --- Check 3: Want-to-read overlap ---
def check_want_to_read(recs, want_to_read_books):
    issues = []
    wtr_keys = [book_key(b["title"], b["author"]) for b in want_to_read_books]
    for rec in recs:
        key = book_key(rec.get("title", ""), rec.get("author", ""))
        for wtr_key in wtr_keys:
            if fuzzy_match(key, wtr_key):
                issues.append(
                    f"'{rec.get('title')}' by {rec.get('author')} is already on the "
                    f"want-to-read list"
                )
                break
    return issues


# --- Check 4: Repetition across recent runs ---
def check_repetition(recs, history, lookback=REPETITION_LOOKBACK):
    issues = []
    recent_runs = history[-lookback:] if history else []
    recent_keys = set()
    for run in recent_runs:
        for rec in run.get("recommendations", []):
            recent_keys.add(book_key(rec.get("title", ""), rec.get("author", "")))

    for rec in recs:
        key = book_key(rec.get("title", ""), rec.get("author", ""))
        if key in recent_keys:
            issues.append(
                f"'{rec.get('title')}' by {rec.get('author')} was also recommended "
                f"in one of the last {lookback} runs"
            )
    return issues


def append_to_history(recs, history_path, max_runs=20):
    history = load_json(history_path, default=[])
    history.append({
        "run_at": datetime.now(timezone.utc).isoformat(),
        "recommendations": recs
    })
    history = history[-max_runs:]  # keep it bounded
    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)


def main():
    recs_data = load_json(os.path.join(DATA_DIR, "recommendations.json"))
    if not recs_data:
        print("No data/recommendations.json found. Run the pipeline first.")
        sys.exit(1)
    recs = recs_data.get("recommendations", [])

    want_to_read_data = load_json(os.path.join(DATA_DIR, "results.json"), default={"books": []})
    want_to_read_books = want_to_read_data.get("books", [])

    read_titles_data = load_json(os.path.join(DATA_DIR, "read_titles.json"), default=None)
    read_titles = read_titles_data.get("read_titles") if read_titles_data else None
    history_path = os.path.join(DATA_DIR, "recommendation_history.json")
    history = load_json(history_path, default=[])

    print(f"Evaluating {len(recs)} recommendations from data/recommendations.json\n")

    results = {
        "schema": check_schema(recs),
        "already_read_overlap": [],
        "want_to_read_overlap": check_want_to_read(recs, want_to_read_books),
        "repetition": check_repetition(recs, history),
    }

    if read_titles is None:
        print("⚠️  data/read_titles.json not found -- skipping already-read overlap check.")
        print("   (fetch_availability.py needs to persist this file -- see accompanying diff)\n")
    else:
        results["already_read_overlap"] = check_already_read(recs, read_titles)

    total_issues = sum(len(v) for v in results.values())

    for check_name, issues in results.items():
        label = check_name.replace("_", " ").title()
        if not issues:
            print(f"✅ {label}: passed")
        else:
            print(f"❌ {label}: {len(issues)} issue(s)")
            for issue in issues:
                print(f"   - {issue}")

    print(f"\n{'PASS' if total_issues == 0 else 'FAIL'} -- {total_issues} total issue(s) found")

    # Log this run to history regardless of pass/fail, so future runs can check repetition
    append_to_history(recs, history_path)

    # Fail the CI step on critical issues (schema or already-read violations).
    # Repetition and want-to-read overlap are treated as warnings for now, not hard failures --
    # tune this once you decide how strict you want the pipeline to be.
    critical = results["schema"] + results["already_read_overlap"]
    sys.exit(1 if critical else 0)


if __name__ == "__main__":
    main()
