import os
import json
import re
import requests
import urllib.parse

# --- Config ---
HARDCOVER_TOKEN = os.environ.get("HARDCOVER_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
LIBRARIES = [
    {"name": "Fairfax County Public Library", "overdrive_id": "fairfax"},
    {"name": "Montgomery County Public Library", "overdrive_id": "mcplmd"},
    {"name": "Alexandria Public Library", "overdrive_id": "alexandria"},
]

GENRE_WHITELIST = {
    "fiction", "nonfiction", "non-fiction", "literary fiction", "literature & fiction",
    "historical fiction", "science fiction", "fantasy", "horror", "thriller",
    "thriller & suspense", "mystery", "romance", "biography", "memoir",
    "autobiography", "essay", "essays", "poetry", "short stories",
    "graphic novel", "young adult", "children", "classics", "adventure",
    "crime", "dystopian", "humor", "comedy", "political", "philosophy",
    "psychology", "sociology", "history", "true crime", "self-help",
    "business", "economics", "science", "nature", "travel", "sports",
    "art", "music", "food", "cooking", "religion", "spirituality",
    "african american fiction", "lgbtqia+ fiction", "lgbtq+",
    "literary collections", "contemporary fiction", "magical realism",
    "african american", "asian literature", "war", "family", "coming of age"
}

EBOOK_FORMATS = {"ebook-overdrive", "ebook-epub-adobe", "ebook-kindle", "ebook-kobo"}
AUDIOBOOK_FORMATS = {"audiobook-overdrive", "audiobook-mp3"}

def normalize(s):
    return re.sub(r'[^a-z0-9\s]', '', s.lower())

# --- Step 1: Fetch want-to-read list from Hardcover ---
def fetch_want_to_read():
    query = """
    query WantToRead {
      me {
        user_books(where: { status_id: { _eq: 1 } }, order_by: { created_at: desc }) {
          created_at
          book {
            title
            description
            image { url }
            contributions { author { name } }
            editions(limit: 1) { isbn_13 isbn_10 }
            taggings { tag { tag } }
          }
        }
      }
    }
    """
    response = requests.post(
        "https://api.hardcover.app/v1/graphql",
        json={"query": query},
        headers={
            "Content-Type": "application/json",
            "authorization": f"Bearer {HARDCOVER_TOKEN}"
        }
    )
    data = response.json()
    books = []
    for ub in data["data"]["me"][0]["user_books"]:
        book = ub["book"]
        isbn = None
        if book["editions"]:
            isbn = book["editions"][0].get("isbn_13") or book["editions"][0].get("isbn_10")
        authors = [c["author"]["name"] for c in book.get("contributions", [])]

        all_tags = [t["tag"]["tag"] for t in book.get("taggings", []) if t.get("tag")]
        genres = list(dict.fromkeys([
            t for t in all_tags if t.lower() in GENRE_WHITELIST
        ]))[:6]

        description = book.get("description") or ""
        description = description.replace("\r\n", " ").replace("\n", " ").strip()

        books.append({
            "title": book["title"],
            "author": authors[0] if authors else "Unknown",
            "isbn": isbn,
            "added_at": ub.get("created_at"),
            "cover": book.get("image", {}).get("url") if book.get("image") else None,
            "description": description,
            "genres": genres
        })
    return books

# --- Step 1b: Fetch already-read titles from Hardcover (to exclude from recommendations) ---
def fetch_read_titles():
    query = """
    query AlreadyRead {
      me {
        user_books(where: { status_id: { _eq: 3 } }) {
          book {
            title
            contributions { author { name } }
          }
        }
      }
    }
    """
    response = requests.post(
        "https://api.hardcover.app/v1/graphql",
        json={"query": query},
        headers={
            "Content-Type": "application/json",
            "authorization": f"Bearer {HARDCOVER_TOKEN}"
        }
    )
    data = response.json()
    titles = []
    for ub in data["data"]["me"][0]["user_books"]:
        book = ub["book"]
        authors = [c["author"]["name"] for c in book.get("contributions", [])]
        author = authors[0] if authors else "Unknown"
        titles.append(f"{book['title']} by {author}")
    return titles

# --- Step 2: Check availability on OverDrive, split by format ---
def check_overdrive(title, author, isbn, library_id):
    headers = {"User-Agent": "Mozilla/5.0"}

    queries = []
    if isbn:
        queries.append(isbn)
    queries.append(f"{title} {author}")

    ebook_result = {"available": False, "reason": "not_in_catalog", "libby_url": None}
    audio_result = {"available": False, "reason": "not_in_catalog", "libby_url": None}
    found = False

    query_slug = urllib.parse.quote(f"{title} {author}")
    libby_url = f"https://libbyapp.com/library/{library_id}/search/query-{query_slug}/page-1"

    for query in queries:
        if found:
            break
        url = f"https://thunder.api.overdrive.com/v2/libraries/{library_id}/media"
        params = {"query": query, "limit": 5}
        try:
            response = requests.get(url, params=params, headers=headers, timeout=10)
            data = response.json()
            items = data.get("items", [])
            if not items:
                continue

            for item in items:
                title_words = normalize(title).split()
                item_title = normalize(item.get("title", ""))
                if sum(1 for w in title_words if w in item_title) < len(title_words) // 2:
                    continue

                found = True
                formats = {f["id"] for f in item.get("formats", [])}
                is_ebook = bool(formats & EBOOK_FORMATS)
                is_audio = bool(formats & AUDIOBOOK_FORMATS)

                copies_available = item.get("availableCopies", 0)
                copies_owned = item.get("ownedCopies", 0)
                holds = item.get("holdsCount", 0)

                if is_ebook:
                    ebook_result = {
                        "available": copies_available > 0,
                        "copies_available": copies_available,
                        "copies_owned": copies_owned,
                        "holds": holds,
                        "reason": "ok",
                        "libby_url": libby_url
                    }
                if is_audio:
                    audio_result = {
                        "available": copies_available > 0,
                        "copies_available": copies_available,
                        "copies_owned": copies_owned,
                        "holds": holds,
                        "reason": "ok",
                        "libby_url": libby_url
                    }

        except Exception as e:
            return (
                {"available": None, "reason": f"error: {str(e)}", "libby_url": None},
                {"available": None, "reason": f"error: {str(e)}", "libby_url": None}
            )

    if not found:
        reason = "no_isbn" if isbn is None else "not_in_catalog"
        return (
            {"available": False, "reason": reason, "libby_url": libby_url},
            {"available": False, "reason": reason, "libby_url": libby_url}
        )

    return ebook_result, audio_result

# --- Step 3: Generate recommendations via Claude ---
def generate_recommendations(books, read_titles):
    print("Generating recommendations via Claude...")

    # Build a compact reading list for the prompt
    book_list = "\n".join([
        f"- {b['title']} by {b['author']}" +
        (f" [{', '.join(b['genres'][:3])}]" if b['genres'] else "")
        for b in books[:80]  # cap at 80 to stay within token limits
    ])

    read_list = "\n".join([f"- {t}" for t in read_titles[:150]])

    prompt = f"""Here is someone's want-to-read list:

{book_list}

Here are titles they have ALREADY READ (do not recommend any of these, or a different edition/printing of the same book):

{read_list}

Based on their taste — the authors, genres, themes, and styles represented in their want-to-read list — recommend exactly 8 books they would likely love that are NOT already on their want-to-read list AND NOT in their already-read list above.

For each recommendation, provide:
- title
- author
- a single sentence explaining why it fits their taste based on specific books or patterns you noticed in their list

Respond ONLY with a JSON array, no markdown, no preamble. Format:
[
  {{"title": "Book Title", "author": "Author Name", "reason": "One sentence why."}}
]"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}]
        }
    )

    data = response.json()
    raw = data["content"][0]["text"].strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)

    return json.loads(raw)

# --- Step 4: Check availability for recommendations ---
def check_recommendations_availability(recs):
    print("Checking availability for recommendations...")
    results = []
    for rec in recs:
        print(f"  Checking: {rec['title']}")
        ebook_availability = {}
        audio_availability = {}
        for lib in LIBRARIES:
            ebook, audio = check_overdrive(
                rec["title"], rec["author"], None, lib["overdrive_id"]
            )
            ebook_availability[lib["name"]] = ebook
            audio_availability[lib["name"]] = audio
        results.append({
            **rec,
            "cover": None,
            "genres": [],
            "ebook_availability": ebook_availability,
            "audio_availability": audio_availability
        })
    return results

# --- Step 5: Build results and write to JSON files ---
def main():
    print("Fetching want-to-read list from Hardcover...")
    books = fetch_want_to_read()
    print(f"Found {len(books)} books")

    results = []
    for book in books:
        print(f"Checking: {book['title']}")
        ebook_availability = {}
        audio_availability = {}
        for lib in LIBRARIES:
            ebook, audio = check_overdrive(
                book["title"], book["author"], book["isbn"], lib["overdrive_id"]
            )
            ebook_availability[lib["name"]] = ebook
            audio_availability[lib["name"]] = audio
        results.append({
            **book,
            "ebook_availability": ebook_availability,
            "audio_availability": audio_availability
        })

    os.makedirs("data", exist_ok=True)
    with open("data/results.json", "w") as f:
        json.dump({
            "updated": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "books": results
        }, f, indent=2)
    print("Done! Written to data/results.json")

    # Generate and save recommendations
    try:
        read_titles = fetch_read_titles()
        print(f"Found {len(read_titles)} already-read books to exclude")

        # Persist read_titles so downstream tooling (evals) has ground truth
        # to check recommendations against, without re-hitting the Hardcover API.
        with open("data/read_titles.json", "w") as f:
            json.dump({
                "updated": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                "read_titles": read_titles
            }, f, indent=2)

        recs = generate_recommendations(books, read_titles)
        recs_with_availability = check_recommendations_availability(recs)
        with open("data/recommendations.json", "w") as f:
            json.dump({
                "updated": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                "recommendations": recs_with_availability
            }, f, indent=2)
        print("Done! Written to data/recommendations.json")
    except Exception as e:
        print(f"Recommendations failed: {e}")

if __name__ == "__main__":
    main()
