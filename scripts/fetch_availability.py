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

# --- Step 1: Fetch want-to-read list from Hardcover ---
def fetch_want_to_read():
    query = """
    query WantToRead {
      me {
        user_books(where: { status_id: { _eq: 1 } }) {
          book {
            title
            contributions { author { name } }
            editions(limit: 1) { isbn_13 isbn_10 }
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
        books.append({
            "title": book["title"],
            "author": authors[0] if authors else "Unknown",
            "isbn": isbn
        })
    return books

# --- Step 2: Check availability on OverDrive ---
def check_overdrive(title, author, isbn, library_id):
    headers = {"User-Agent": "Mozilla/5.0"}

    # Try ISBN first, then title+author
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
            # Find best match by title
            for item in items:
                if title.lower() in item.get("title", "").lower():
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
            "updated": __import__("datetime").datetime.utcnow().isoformat(),
            "books": results
        }, f, indent=2)
    print("Done! Written to data/results.json")

if __name__ == "__main__":
    main()
