import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
import json

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_BASE_URL = "https://archive.org/metadata/"
DOWNLOAD_BASE_URL = "https://archive.org/download"

# Build a valid query:
# - Only software media type
# - ISO or IMG files are likely
# - Mention "linux" in title/subject/description (subject:"linux" is common)
QUERY = '(format:ISO OR format:IMG) AND mediatype:software AND description:"linux, distribution"'

FIELDS = ["identifier", "title", "date", "creator"]
ROWS_PER_PAGE = 500  # IA allows up to 1000; use 500 to be gentle
REQUEST_TIMEOUT = 30
SLEEP_SECONDS = 1.0  # rate limiting between requests

# Configure a resilient HTTP session with retries and backoff
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Internet-Archive-API/1.0 (+https://example.local) Python-requests"
})
_retry = Retry(
    total=5,
    connect=5,
    read=5,
    backoff_factor=1.0,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("HEAD", "GET", "OPTIONS"),
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)

def search_page(page: int) -> dict:
    params = {
        "q": QUERY,
        "fl[]": FIELDS,
        "rows": ROWS_PER_PAGE,
        "page": page,
        "output": "json"
    }
    try:
        resp = _SESSION.get(SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"Advanced search request error on page {page}: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"Advanced search failed with status {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse JSON from advanced search: {e}\nBody: {resp.text[:300]}")
    return data

def fetch_metadata(identifier: str) -> dict | None:
    url = f"{METADATA_BASE_URL}{identifier}"
    try:
        resp = _SESSION.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        return None

def main():
    iso_entries = []

    # Fetch first page to get numFound and validate structure
    first = search_page(1)
    response_obj = first.get("response")
    if not isinstance(response_obj, dict) or "docs" not in response_obj:
        # Provide more context for debugging
        err = first.get("error") or first
        raise RuntimeError(f"Unexpected search response structure, missing 'response.docs'. Details: {json.dumps(err)[:500]}")

    num_found = int(response_obj.get("numFound", 0))
    pages = max(1, (num_found + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)

    for page in range(1, pages + 1):
        if page > 1:
            time.sleep(SLEEP_SECONDS)
            data = search_page(page)
            response_obj = data.get("response", {})
        docs = response_obj.get("docs", [])
        if not isinstance(docs, list):
            continue

        for item in docs:
            identifier = item.get("identifier")
            if not identifier:
                continue

            time.sleep(SLEEP_SECONDS)
            meta_json = fetch_metadata(identifier)
            if not meta_json:
                continue

            files = meta_json.get("files", []) or []
            for f in files:
                name = f.get("name", "") or ""
                lname = name.lower()
                if lname.endswith((".iso", ".img", ".zip")):
                    iso_entries.append({
                        "identifier": identifier,
                        "title": item.get("title", ""),
                        "file_name": name,
                        "download_url": f"{DOWNLOAD_BASE_URL}/{identifier}/{name}",
                        "size": f.get("size", "unknown")
                    })

    out_file = "../iso_metadata.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(iso_entries, f, indent=2, ensure_ascii=False)

    print(f"Found {len(iso_entries)} ISO-like files. Saved to {out_file}.")

if __name__ == "__main__":
    main()
