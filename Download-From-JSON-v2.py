import os
import json
import argparse
import logging
import sys
import time
import re
from typing import Iterable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Defaults
DEFAULT_OUTPUT_DIR = "S:/Linux-FUCKIN-ISOs/"
DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 5
DEFAULT_BACKOFF = 1.0
DEFAULT_CHUNK_SIZE = 1024 * 256  # 256 KiB
BAR_WIDTH = 40


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
    # store default timeout in session for convenience
    session.request = _timeout_wrapper(session.request, timeout)
    return session


def _timeout_wrapper(request_func, default_timeout: int):
    def wrapped(method, url, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = default_timeout
        return request_func(method, url, **kwargs)
    return wrapped


def _format_size(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{num_bytes}B"


def _print_bar(prefix: str, downloaded: int, total: Optional[int], out_stream=sys.stdout):
    if total and total > 0:
        frac = min(1.0, downloaded / total)
        filled = int(BAR_WIDTH * frac)
        bar = "#" * filled + "-" * (BAR_WIDTH - filled)
        percent = int(frac * 100)
        total_s = _format_size(total)
        cur_s = _format_size(downloaded)
        line = f"{prefix} [{bar}] {percent:3d}% ({cur_s}/{total_s})"
    else:
        # Unknown total size
        cur_s = _format_size(downloaded)
        bar = "#" * (downloaded // (10 * 1024 * 1024))  # one # per ~10MB
        bar = bar[-BAR_WIDTH:]
        line = f"{prefix} [{bar:<{BAR_WIDTH}}] {cur_s}"
    print("\r" + line, end="", flush=True, file=out_stream)


def _iter_items(data: Iterable[dict], include: Optional[str], exclude: Optional[str], max_items: Optional[int]) -> Iterable[dict]:
    inc_re = re.compile(include, re.IGNORECASE) if include else None
    exc_re = re.compile(exclude, re.IGNORECASE) if exclude else None
    count = 0
    for item in data:
        name = (item.get("file_name") or "") + " " + (item.get("title") or "")
        if inc_re and not inc_re.search(name):
            continue
        if exc_re and exc_re.search(name):
            continue
        yield item
        count += 1
        if max_items and count >= max_items:
            break


def download_file(session: requests.Session, url: str, dest_path: str, chunk_size: int, show_progress: bool, resume: bool):
    """Download a URL to dest_path with optional resume and progress bar."""
    # Resume support
    headers = {}
    mode = "wb"
    downloaded = 0
    if resume and os.path.exists(dest_path):
        downloaded = os.path.getsize(dest_path)
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"
            mode = "ab"
            logging.info(f"Resuming {os.path.basename(dest_path)} from {downloaded} bytes")

    with session.get(url, stream=True, headers=headers) as r:
        if r.status_code in (206, 200):
            total_size = None
            content_length = r.headers.get("Content-Length") or r.headers.get("content-length")
            if content_length and content_length.isdigit():
                total_size = int(content_length)
                if r.status_code == 206:
                    # For partial content, total length is remaining; add already downloaded
                    total_size += downloaded
            prefix = f"[↓] {os.path.basename(dest_path)}"
            last_update = 0.0
            if show_progress:
                _print_bar(prefix, downloaded, total_size)
            with open(dest_path, mode) as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if show_progress:
                        now = time.time()
                        if now - last_update >= 0.05:
                            _print_bar(prefix, downloaded, total_size)
                            last_update = now
            if show_progress:
                _print_bar(prefix, downloaded, total_size)
                print()
        else:
            r.raise_for_status()


def main():
    p = argparse.ArgumentParser(description="Download files from an Internet Archive JSON list (v2)")
    p.add_argument("--input", "-i", default="iso_metadataz.json", help="Path to JSON metadata file (list of items)")
    p.add_argument("--output-dir", "-o", default=DEFAULT_OUTPUT_DIR, help="Destination directory for downloads")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Total HTTP retries for transient errors")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Request timeout seconds")
    p.add_argument("--backoff", type=float, default=DEFAULT_BACKOFF, help="Retry backoff factor")
    p.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Download chunk size in bytes")
    p.add_argument("--resume", action="store_true", help="Resume partially downloaded files using HTTP Range")
    p.add_argument("--no-progress", action="store_true", help="Disable per-file progress bar output")
    p.add_argument("--dry-run", action="store_true", help="Do not download, just list actions")
    p.add_argument("--max", type=int, default=None, help="Limit the number of items to process")
    p.add_argument("--include", help="Regex that file_name/title must match to be downloaded")
    p.add_argument("--exclude", help="Regex that if matched will skip the item")
    p.add_argument("--user-agent", help="Custom User-Agent header")
    p.add_argument("--log-file", help="Optional path to a log file")
    p.add_argument("-v", action="count", default=0, help="Increase verbosity (-v info, -vv debug)")
    args = p.parse_args()

    setup_logging(args.v, args.log_file)

    # Load JSON
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Input JSON must be a list of items")
    except Exception as e:
        logging.error(f"Failed to read input JSON '{args.input}': {e}")
        sys.exit(1)

    # Prepare destination
    os.makedirs(args.output_dir, exist_ok=True)

    session = build_session(args.timeout, args.retries, args.backoff, args.user_agent)

    # Filter and iterate
    items = list(_iter_items(data, args.include, args.exclude, args.max))
    total_items = len(items)
    logging.info(f"Total items to process: {total_items}")

    # Determine progress behavior: disable if not a TTY or explicitly no-progress
    show_progress = (not args.no_progress) and sys.stdout.isatty()

    success = 0
    skipped = 0
    failed = 0

    for idx, iso in enumerate(items, start=1):
        file_name = iso.get("file_name")
        url = iso.get("download_url")
        if not file_name or not url:
            logging.warning(f"[{idx}/{total_items}] Missing file_name or download_url, skipping")
            failed += 1
            continue

        dest_path = os.path.join(args.output_dir, file_name)
        prefix = f"[{idx}/{total_items} {(idx/total_items*100):.1f}%]"

        if os.path.exists(dest_path):
            logging.info(f"{prefix} Already exists: {file_name}")
            skipped += 1
            continue

        if args.dry_run:
            print(f"{prefix} [DRY-RUN] Would download: {file_name} <- {url}")
            skipped += 1
            continue

        try:
            download_file(session, url, dest_path, args.chunk_size, show_progress, args.resume)
            print(f"{prefix} [✔] Done: {file_name}")
            success += 1
        except Exception as e:
            print()  # ensure clean line after any partial progress bar
            logging.error(f"{prefix} [✗] Failed: {file_name} - {e}")
            failed += 1

    logging.info(f"Completed. Success: {success}, Skipped: {skipped}, Failed: {failed}")


if __name__ == "__main__":
    main()
