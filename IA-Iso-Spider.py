import argparse
import json
import logging
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Endpoints
ADV_SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/"
DOWNLOAD_BASE = "https://archive.org/download"

# Default headers
DEFAULT_UA = "IA-Iso-Spider/1.0 (+https://archive.org) Python-requests"


@dataclass(order=True)
class FrontierItem:
    # Priority tuple: negative score so higher score pops first in min-heap replacement structure if used.
    # Here we will use a simple list sorted on push to avoid external deps; keep fields for future extension.
    priority: float
    kind: str  # 'collection' or 'identifier'
    value: str
    depth: int = 0
    stats_key: str = field(default="", compare=False)


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

    # Tame urllib3 unless very verbose
    u3_level = logging.DEBUG if verbosity >= 2 else logging.ERROR
    for name in ("urllib3", "urllib3.connectionpool", "requests.packages.urllib3"):
        logging.getLogger(name).setLevel(u3_level)


def build_session(timeout: int, retries: int, backoff: float, user_agent: Optional[str]) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent or DEFAULT_UA})
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("HEAD", "GET"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Attach default timeout wrapper
    orig_request = session.request

    def wrapped(method, url, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = timeout
        return orig_request(method, url, **kwargs)

    session.request = wrapped  # type: ignore
    return session


def adv_search_collection(session: requests.Session, collection: str, rows: int, page: int) -> dict:
    params = {
        "q": f"collection:{collection}",
        "fl[]": ["identifier", "title", "collection", "creator"],
        "rows": rows,
        "page": page,
        "output": "json",
    }
    resp = session.get(ADV_SEARCH_URL, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"Advanced search failed {resp.status_code} for collection={collection}: {resp.text[:200]}")
    return resp.json()


def fetch_metadata(session: requests.Session, identifier: str) -> Optional[dict]:
    try:
        r = session.get(METADATA_URL + identifier)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logging.debug(f"metadata fetch failed for {identifier}: {e}")
        return None


def extract_iso_entries(identifier: str, title: str, meta: dict) -> List[dict]:
    files = meta.get("files") or []
    results: List[dict] = []
    for f in files:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        lname = name.lower()
        if lname.endswith((".iso", ".img")):
            results.append({
                "identifier": identifier,
                "title": title,
                "file_name": name,
                "download_url": f"{DOWNLOAD_BASE}/{identifier}/{name}",
                "size": f.get("size", "unknown"),
            })
    return results


def related_collections_from_meta(meta: dict) -> Set[str]:
    rel: Set[str] = set()
    md = meta.get("metadata") or {}
    # The 'collection' field on items can be array or string; it refers to parent collections.
    col = md.get("collection")
    if isinstance(col, list):
        rel.update([str(x) for x in col if x])
    elif isinstance(col, str) and col:
        rel.add(col)
    return rel


def classify_seed(session: requests.Session, seed: str) -> str:
    """Return 'collection' or 'identifier' based on metadata; default to 'collection' on failure."""
    meta = fetch_metadata(session, seed)
    if not meta:
        return "collection"
    md = meta.get("metadata") or {}
    mediatype = str(md.get("mediatype", "")).lower()
    if mediatype == "collection":
        return "collection"
    return "identifier"


def push_frontier(frontier: List[FrontierItem], item: FrontierItem):
    frontier.append(item)
    # Keep higher priority first (descending)
    frontier.sort(key=lambda x: x.priority, reverse=True)


def pop_frontier(frontier: List[FrontierItem]) -> Optional[FrontierItem]:
    if not frontier:
        return None
    return frontier.pop(0)


def main():
    parser = argparse.ArgumentParser(description="IA ISO Spider: Crawl collections and discover ISO files")
    parser.add_argument("--seeds", nargs="*", default=[
        "ubuntu_releases", "vintagesoftware", "linuxtracker", "archlinux_archive", "Fedora_Project", "debian-cd"
    ], help="Seed collection identifiers or item identifiers (3-5 recommended)")
    parser.add_argument("--max-visits", type=int, default=200, help="Max number of frontier pops (collections/items) to process")
    parser.add_argument("--max-depth", type=int, default=4, help="Max crawl depth")
    parser.add_argument("--rows", type=int, default=500, help="Rows per page for advanced search within a collection")
    parser.add_argument("--sleep", type=float, default=0.75, help="Sleep seconds between HTTP requests")
    parser.add_argument("--user-agent", default=None, help="Custom User-Agent")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument("--retries", type=int, default=5, help="HTTP retries for transient errors")
    parser.add_argument("--backoff", type=float, default=1.0, help="Retry backoff factor")
    parser.add_argument("--out-jsonl", default="iso_spider_results.jsonl", help="Output JSONL for discovered ISO files")
    parser.add_argument("--stats-json", default="iso_spider_stats.json", help="Output JSON for crawl stats (collections yield)")
    parser.add_argument("--log-file", default="iso_spider.log", help="Path to log file")
    parser.add_argument("-v", action="count", default=0, help="Increase verbosity (-v info, -vv debug)")
    parser.add_argument("--stop-on-dry-spell", type=int, default=25, help="Stop after this many consecutive visits yield no new ISOs")
    args = parser.parse_args()

    setup_logging(args.v, args.log_file)
    session = build_session(args.timeout, args.retries, args.backoff, args.user_agent)

    # State
    seen_collections: Set[str] = set()
    seen_identifiers: Set[str] = set()
    seen_files: Set[Tuple[str, str]] = set()  # (identifier, file_name)

    # Stats per collection: items seen, isos found
    coll_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"items": 0, "isos": 0})

    # Frontier of collections/items (primary crawl unit)
    frontier: List[FrontierItem] = []
    for s in args.seeds:
        kind = classify_seed(session, s)
        if kind == "collection":
            push_frontier(frontier, FrontierItem(priority=1.0, kind="collection", value=s, depth=0, stats_key=s))
        else:
            push_frontier(frontier, FrontierItem(priority=1.0, kind="identifier", value=s, depth=0, stats_key=""))

    total_iso = 0
    visits = 0
    dry_streak = 0

    # Output JSONL stream
    out_fp = open(args.out_jsonl, "w", encoding="utf-8")

    try:
        while frontier and visits < args.max_visits:
            node = pop_frontier(frontier)
            if node is None:
                break

            if node.kind == "collection":
                coll = node.value
                if coll in seen_collections:
                    logging.debug(f"Skip seen collection {coll}")
                    continue
                if node.depth > args.max_depth:
                    logging.debug(f"Skip {coll} due to depth {node.depth} > {args.max_depth}")
                    continue

                visits += 1
                seen_collections.add(coll)
                logging.info(f"[C] Visiting collection '{coll}' (depth={node.depth}) | visits={visits}/{args.max_visits}")

                # Page through items in this collection
                page = 1
                new_isos_from_coll = 0
                try:
                    first = adv_search_collection(session, coll, args.rows, page)
                except Exception as e:
                    logging.warning(f"Search failed for collection {coll}: {e}")
                    continue

                response_obj = first.get("response") or {}
                num_found = int(response_obj.get("numFound", 0))
                total_pages = max(1, (num_found + args.rows - 1) // args.rows)
                logging.debug(f"Collection {coll}: numFound={num_found}, pages={total_pages}")

                for p in range(1, total_pages + 1):
                    if p > 1:
                        time.sleep(args.sleep)
                        try:
                            data = adv_search_collection(session, coll, args.rows, p)
                        except Exception as e:
                            logging.warning(f"Search page {p} failed for {coll}: {e}")
                            break
                        response_obj = data.get("response") or {}
                    docs = response_obj.get("docs", []) or []
                    logging.debug(f"{coll} page {p} docs={len(docs)}")
                    for doc in docs:
                        identifier = (doc.get("identifier") or "").strip()
                        title = (doc.get("title") or "").strip()
                        if not identifier:
                            continue
                        if identifier in seen_identifiers:
                            continue
                        seen_identifiers.add(identifier)
                        coll_stats[coll]["items"] += 1

                        time.sleep(args.sleep)
                        meta = fetch_metadata(session, identifier)
                        if not meta:
                            continue

                        # Extract ISO entries
                        entries = extract_iso_entries(identifier, title, meta)
                        if entries:
                            for e in entries:
                                key = (e["identifier"], e["file_name"]) 
                                if key in seen_files:
                                    continue
                                seen_files.add(key)
                                out_fp.write(json.dumps(e, ensure_ascii=False) + "\n")
                                total_iso += 1
                                new_isos_from_coll += 1
                        # Discover related collections from this item and push to frontier
                        rel_cols = related_collections_from_meta(meta)
                        for rc in rel_cols:
                            if rc not in seen_collections:
                                # Score seed: use historical iso/item ratio if present otherwise 0.5 baseline
                                stats = coll_stats.get(rc)
                                if stats and stats["items"] > 0:
                                    ratio = (stats["isos"] / max(1, stats["items"]))
                                else:
                                    ratio = 0.5
                                push_frontier(frontier, FrontierItem(priority=ratio, kind="collection", value=rc, depth=node.depth + 1, stats_key=rc))

                    # Optional early-break: if this collection yields nothing after first page and huge, deprioritize
                    # Keep simple: continue paging always to be thorough

                # Update stats and frontier priorities for this collection's neighbors already in frontier
                coll_stats[coll]["isos"] += new_isos_from_coll
                logging.info(f"[C] Done {coll}: items={coll_stats[coll]['items']}, isos_found_now={new_isos_from_coll}, total_isos={total_iso}")

                if new_isos_from_coll == 0:
                    dry_streak += 1
                else:
                    dry_streak = 0

                # Re-weigh existing frontier entries using updated stats
                for i in range(len(frontier)):
                    fi = frontier[i]
                    if fi.kind == "collection":
                        stats = coll_stats.get(fi.stats_key)
                        if stats and stats["items"] > 0:
                            ratio = stats["isos"] / max(1, stats["items"])
                            fi.priority = 1.0 + ratio
                        else:
                            fi.priority = max(fi.priority * 0.95, 0.1)
                frontier.sort(key=lambda x: x.priority, reverse=True)

                if dry_streak >= args.stop_on_dry_spell:
                    logging.warning(f"Stopping due to dry streak of {dry_streak} visits without new ISOs")
                    break

            else:
                # Process a single item identifier: emit ISO entries and push its parent collections
                ident = node.value
                if ident in seen_identifiers:
                    logging.debug(f"Skip seen identifier {ident}")
                    continue
                visits += 1
                seen_identifiers.add(ident)
                logging.info(f"[I] Visiting identifier '{ident}' (depth={node.depth}) | visits={visits}/{args.max_visits}")

                time.sleep(args.sleep)
                meta = fetch_metadata(session, ident)
                if not meta:
                    logging.debug(f"No metadata for {ident}")
                    continue

                # Title best-effort from metadata block
                md = meta.get("metadata") or {}
                title = str(md.get("title", ""))

                entries = extract_iso_entries(ident, title, meta)
                if entries:
                    for e in entries:
                        key = (e["identifier"], e["file_name"]) 
                        if key in seen_files:
                            continue
                        seen_files.add(key)
                        out_fp.write(json.dumps(e, ensure_ascii=False) + "\n")
                        total_iso += 1

                # Parent collections of this item become future crawl targets
                rel_cols = related_collections_from_meta(meta)
                for rc in rel_cols:
                    if rc not in seen_collections:
                        stats = coll_stats.get(rc)
                        if stats and stats["items"] > 0:
                            ratio = (stats["isos"] / max(1, stats["items"]))
                        else:
                            ratio = 0.5
                        push_frontier(frontier, FrontierItem(priority=ratio, kind="collection", value=rc, depth=node.depth + 1, stats_key=rc))

                # Note: Do not update coll_stats counters here since we don't know which collection to attribute the item to conclusively.
                if dry_streak >= args.stop_on_dry_spell:
                    logging.warning(f"Stopping due to dry streak of {dry_streak} visits without new ISOs")
                    break

        logging.info(f"Finished crawl. Visits={visits}, total_isos={total_iso}, unique_items={len(seen_identifiers)}, unique_collections={len(seen_collections)}")
    finally:
        out_fp.close()

    # Write stats JSON
    try:
        with open(args.stats_json, "w", encoding="utf-8") as sf:
            json.dump({k: {"items": v["items"], "isos": v["isos"]} for k, v in coll_stats.items()}, sf, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Failed to write stats JSON: {e}")

    print(f"Discovered {total_iso} ISO files. Results saved to {args.out_jsonl}. Stats saved to {args.stats_json}.")


if __name__ == "__main__":
    main()
