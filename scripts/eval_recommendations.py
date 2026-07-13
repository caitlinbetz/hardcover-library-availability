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
  5. Existence             -- flags recs that don't turn up in Open Library at all
                               (catches hallucinated titles). Best-effort: a network
                               error or a genuinely obscure book both look like "not
                               found," so treat a miss here as a strong signal, not
                               absolute proof.

Usage:
    python scripts/eval_recommendations.py

Expects these files to exist in data/:
    results.json                 (want-to-read snapshot, written by fetch_availability.py)
    read_titles.json             (already-read snapshot, as {"read_titles": [{"title", "author"}, ...]})
    recommendations.json         (latest recommendation run)
    recommendation_history.json  (rolling history -- auto-created if missing)

NOTE: read_titles.json and recommendation_history.json aren't written by the
current version of fetch_availability.py. See the accompanying diff to persist them.
"""

import json
import os
import re
import sys
import requests
from datetime import datetime, timezone
from difflib import SequenceMatcher

DATA_DIR = "data"
FUZZY_THRESHOLD = 0.85  # 0-1, higher = stricter match required
REPETITION_LOOKBACK = 5  # how many past runs to check for repeats


def normalize(s):
    return re.sub(r'[^a-z0-9\s]', '', s.lower()).strip()


def primary_title(title):
    """
    Strips a trailing subtitle so 'Blood Meridian: Or the Evening Redness in
    the West' and 'Blood Meridian' compare as the same book. Colon and em/en
    dash are the common subtitle separators in book titles.
    """
    for sep in (':', ' — ', ' – ', ' - '):
        if sep in title:
            return title.split(sep, 1)[0].strip()
    return title.strip()


def book_key(title, author):
    return f"{normalize(primary_title(title))} by {normalize(author)}"


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
    read_keys = [book_key(t["title"], t["author"]) for t in read_titles]
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


# --- Check 5: Existence (catches hallucinated titles) ---
# open_library_lookup() also returns a cover image URL when it finds a match,
# so fetch_availability.py reuses this same function for cover art on the
# recommendations page -- one lookup function, two call sites.
EXISTENCE_MATCH_THRESHOLD = 0.75  # looser than FUZZY_THRESHOLD -- Open Library's
                                   # title formatting doesn't always match cleanly


def open_library_lookup(title, author, timeout=8):
    """
    Looks the book up on Open Library's public search API.
    Returns {"exists": True/False/None, "cover_url": str or None}.
    exists=None means a network error / inconclusive result -- don't treat
    that as evidence the book doesn't exist.
    """
    try:
        response = requests.get(
            "https://openlibrary.org/search.json",
            params={"title": title, "author": author, "limit": 5},
            headers={"User-Agent": "hardcover-library-availability-eval/1.0"},
            timeout=timeout
        )
        response.raise_for_status()
        docs = response.json().get("docs", [])
    except (requests.RequestException, ValueError):
        return {"exists": None, "cover_url": None}

    if not docs:
        return {"exists": False, "cover_url": None}

    target_title = normalize(primary_title(title))
    target_author = normalize(author)

    for doc in docs:
        doc_title = normalize(primary_title(doc.get("title", "")))
        doc_authors = [normalize(a) for a in doc.get("author_name", [])]
        title_matches = fuzzy_match(doc_title, target_title, threshold=EXISTENCE_MATCH_THRESHOLD)
        author_matches = any(target_author in a or a in target_author for a in doc_authors)
        if title_matches and author_matches:
            cover_id = doc.get("cover_i")
            cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else None
            return {"exists": True, "cover_url": cover_url}

    return {"exists": False, "cover_url": None}


def check_book_exists(title, author, timeout=8):
    """Thin wrapper over open_library_lookup for callers that only need the boolean."""
    return open_library_lookup(title, author, timeout=timeout)["exists"]


def check_hallucination(recs):
    """
    Returns (issues, inconclusive). Issues are recs Open Library couldn't
    find at all. Inconclusive entries had a network error -- surfaced
    separately so a flaky request doesn't silently count as "hallucinated."
    """
    issues = []
    inconclusive = []
    for rec in recs:
        title = rec.get("title", "")
        author = rec.get("author", "")
        if not title or not author:
            continue  # schema check already flags this
        exists = check_book_exists(title, author)
        if exists is False:
            issues.append(
                f"'{title}' by {author} was not found on Open Library -- possibly hallucinated"
            )
        elif exists is None:
            inconclusive.append(f"'{title}' by {author} -- Open Library lookup failed, skipped")
    return issues, inconclusive


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
        "existence": [],
    }

    if read_titles is None:
        print("⚠️  data/read_titles.json not found -- skipping already-read overlap check.")
        print("   (fetch_availability.py needs to persist this file -- see accompanying diff)\n")
    else:
        results["already_read_overlap"] = check_already_read(recs, read_titles)

    existence_issues, inconclusive = check_hallucination(recs)
    results["existence"] = existence_issues

    total_issues = sum(len(v) for v in results.values())

    for check_name, issues in results.items():
        label = check_name.replace("_", " ").title()
        if not issues:
            print(f"✅ {label}: passed")
        else:
            print(f"❌ {label}: {len(issues)} issue(s)")
            for issue in issues:
                print(f"   - {issue}")

    if inconclusive:
        print(f"\n⚠️  {len(inconclusive)} existence lookup(s) inconclusive (not counted as issues):")
        for note in inconclusive:
            print(f"   - {note}")

    print(f"\n{'PASS' if total_issues == 0 else 'FAIL'} -- {total_issues} total issue(s) found")

    # Log this run to history regardless of pass/fail, so future runs can check repetition
    append_to_history(recs, history_path)

    # Fail the CI step on critical issues: schema, already-read, and existence.
    # A hallucinated book is at least as bad as an already-read one -- both mean
    # something shouldn't have been published. Repetition and want-to-read overlap
    # stay warnings for now; tune this once you've seen how the existence check
    # performs (Open Library coverage isn't perfect, so watch the inconclusive/
    # false-positive rate before trusting it fully).
    critical = results["schema"] + results["already_read_overlap"] + results["existence"]
    sys.exit(1 if critical else 0)


if __name__ == "__main__":
    main()
