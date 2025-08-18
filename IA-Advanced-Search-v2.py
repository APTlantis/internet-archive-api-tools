import argparse
import json
import logging
import sys
import time
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_BASE_URL = "https://archive.org/metadata/"
DOWNLOAD_BASE_URL = "https://archive.org/download"

DEFAULT_FIELDS = ["identifier", "title", "date", "creator"]


def setup_logging(verbosity: int, log_file: Optional[str] = None):
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    # Tame noisy urllib3 retry warnings unless user asked for very verbose logs
    u3_level = logging.DEBUG if verbosity >= 2 else logging.ERROR
    for name in ("urllib3", "urllib3.connectionpool", "requests.packages.urllib3"):
        logging.getLogger(name).setLevel(u3_level)


def build_session(timeout: int, retries: int, backoff: float, user_agent: Optional[str]) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent or "Internet-Archive-API/2.0 (+https://example.local) Python-requests"
    })
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("HEAD", "GET", "OPTIONS"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # attach default timeout wrapper
    session.request = _timeout_wrapper(session.request, timeout)
    return session


def _timeout_wrapper(request_func, default_timeout: int):
    def wrapped(method, url, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = default_timeout
        return request_func(method, url, **kwargs)
    return wrapped


def search_page(session: requests.Session, query: str, fields: List[str], rows: int, page: int) -> dict:
    params = {
        "q": query,
        "fl[]": fields,
        "rows": rows,
        "page": page,
        "output": "json",
    }
    resp = session.get(SEARCH_URL, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"Advanced search failed with status {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse JSON from advanced search: {e}\nBody: {resp.text[:300]}") from e


def fetch_metadata(session: requests.Session, identifier: str) -> Optional[dict]:
    url = f"{METADATA_BASE_URL}{identifier}"
    try:
        resp = session.get(url)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Internet Archive Advanced Search (v2)")
    parser.add_argument("--query", "-q", default='(format:ISO OR format:IMG) AND mediatype:software AND description:"linux, distribution"', help="Advanced search query string")
    parser.add_argument("--rows", type=int, default=500, help="Rows per page (<=1000)")
    parser.add_argument("--max-pages", type=int, help="Limit number of pages to fetch")
    parser.add_argument("--sleep", type=float, default=1.0, help="Sleep seconds between requests")
    parser.add_argument("--fields", nargs="*", default=DEFAULT_FIELDS, help="Fields to fetch in search results")
    parser.add_argument("--out", "-o", default="iso_metadata.json", help="Output JSON file for results")
    parser.add_argument("--timeout", type=int, default=30, help="Request timeout seconds")
    parser.add_argument("--retries", type=int, default=5, help="HTTP retries for transient errors")
    parser.add_argument("--backoff", type=float, default=1.0, help="Retry backoff factor")
    parser.add_argument("--user-agent", help="Custom User-Agent header")
    parser.add_argument("--log-file", help="Optional log file path")
    parser.add_argument("-v", action="count", default=0, help="Increase verbosity (-v info, -vv debug)")
    parser.add_argument("--dry-run", action="store_true", help="Do not fetch per-item metadata, only list identifiers")
    args = parser.parse_args()

    setup_logging(args.v, args.log_file)
    session = build_session(args.timeout, args.retries, args.backoff, args.user_agent)

    logging.info(f"Query: {args.query}")

    iso_entries = []

    # Fetch first page to get numFound
    first = search_page(session, args.query, args.fields, args.rows, 1)
    response_obj = first.get("response")
    if not isinstance(response_obj, dict) or "docs" not in response_obj:
        err = first.get("error") or first
        raise RuntimeError(f"Unexpected search response structure, missing 'response.docs'. Details: {json.dumps(err)[:500]}")

    num_found = int(response_obj.get("numFound", 0))
    total_pages = max(1, (num_found + args.rows - 1) // args.rows)
    if args.max_pages is not None:
        total_pages = min(total_pages, args.max_pages)

    logging.info(f"numFound={num_found}, pages={total_pages}")

    for page in range(1, total_pages + 1):
        if page > 1:
            time.sleep(args.sleep)
            data = search_page(session, args.query, args.fields, args.rows, page)
            response_obj = data.get("response", {})
        docs = response_obj.get("docs", [])
        if not isinstance(docs, list):
            continue

        logging.debug(f"Processing page {page} with {len(docs)} docs")

        for item in docs:
            identifier = item.get("identifier")
            if not identifier:
                continue
            title = item.get("title", "")

            if args.dry_run:
                print(identifier, "-", title)
                continue

            time.sleep(args.sleep)
            meta_json = fetch_metadata(session, identifier)
            if not meta_json:
                logging.debug(f"No metadata for {identifier}")
                continue

            files = meta_json.get("files", []) or []
            for f in files:
                name = (f.get("name", "") or "")
                lname = name.lower()
                if lname.endswith((".iso", ".img", ".zip")):
                    iso_entries.append({
                        "identifier": identifier,
                        "title": title,
                        "file_name": name,
                        "download_url": f"{DOWNLOAD_BASE_URL}/{identifier}/{name}",
                        "size": f.get("size", "unknown"),
                    })

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(iso_entries, f, indent=2, ensure_ascii=False)

    print(f"Found {len(iso_entries)} ISO-like files. Saved to {args.out}.")


if __name__ == "__main__":
    main()
