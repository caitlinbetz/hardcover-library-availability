import os
import json
import requests

# --- Config ---
HARDCOVER_TOKEN = os.environ.get("HARDCOVER_TOKEN")
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

        # Filter tags to genre whitelist
        all_tags = [t["tag"]["tag"] for t in book.get("taggings", []) if t.get("tag")]
        genres = list(dict.fromkeys([
            t for t in all_tags if t.lower() in GENRE_WHITELIST
        ]))[:6]  # cap at 6

        # Clean description
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

# --- Step 2: Check availability on OverDrive ---
def check_overdrive(title, author, isbn, library_id):
    headers = {"User-Agent": "Mozilla/5.0"}

    queries = []
    if isbn:
        queries.append(isbn)
    queries.append(f"{title} {author}")

    for query in queries:
        url = f"https://thunder.api.overdrive.com/v2/libraries/{library_id}/media"
        params = {"query": query, "limit": 3}
        try:
            response = requests.get(url, params=params, headers=headers, timeout=10)
            data = response.json()
            items = data.get("items", [])
            if not items:
                continue
            for item in items:
                title_words = title.lower().replace('-', ' ').split()
                item_title = item.get("title", "").lower()
                if sum(1 for w in title_words if w in item_title) >= len(title_words) // 2:
                    copies_available = item.get("availableCopies", 0)
                    copies_owned = item.get("ownedCopies", 0)
                    holds = item.get("holdsCount", 0)
                    return {
                        "available": copies_available > 0,
                        "copies_available": copies_available,
                        "copies_owned": copies_owned,
                        "holds": holds,
                        "reason": "ok"
                    }
        except Exception as e:
            return {"available": None, "reason": f"error: {str(e)}"}

    return {"available": False, "reason": "not_in_catalog"}

# --- Step 3: Build results and write to data/results.json ---
def main():
    print("Fetching want-to-read list from Hardcover...")
    books = fetch_want_to_read()
    print(f"Found {len(books)} books")

    results = []
    for book in books:
        print(f"Checking: {book['title']}")
        availability = {}
        for lib in LIBRARIES:
            availability[lib["name"]] = check_overdrive(
                book["title"], book["author"], book["isbn"], lib["overdrive_id"]
            )
        results.append({**book, "availability": availability})

    os.makedirs("data", exist_ok=True)
    with open("data/results.json", "w") as f:
        json.dump({
            "updated": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "books": results
        }, f, indent=2)
    print("Done! Written to data/results.json")

if __name__ == "__main__":
    main()
