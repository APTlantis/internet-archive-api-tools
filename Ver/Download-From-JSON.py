import os
import json
import requests
import sys
import time

# Load the ISO metadata (explicit UTF-8 to avoid Windows cp1252 decode issues)
with open("../crawler-results.json", "r", encoding="utf-8") as f:
    iso_list = json.load(f)

# Make sure the output directory exists
output_dir = "S:/Linux-FUCKIN-ISOs/"
os.makedirs(output_dir, exist_ok=True)

BAR_WIDTH = 40
CHUNK_SIZE = 1024 * 256  # 256 KiB chunks for smoother progress
REQUEST_TIMEOUT = 60


def _format_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{num_bytes}B"


def _print_bar(prefix: str, downloaded: int, total: int | None):
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
        bar = "#" * (downloaded // (10 * 1024 * 1024))  # one # per ~10MB as a rough indicator
        bar = bar[-BAR_WIDTH:]
        line = f"{prefix} [{bar:<{BAR_WIDTH}}] {cur_s}"
    print("\r" + line, end="", flush=True)


def download_file(url: str, dest_path: str, display_name: str | None = None):
    """Download a URL to dest_path with a simple progress bar."""
    display_name = display_name or os.path.basename(dest_path)
    try:
        with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
            r.raise_for_status()
            total_str = r.headers.get("Content-Length") or r.headers.get("content-length")
            total = int(total_str) if total_str and total_str.isdigit() else None

            downloaded = 0
            last_update = 0.0
            prefix = f"[↓] {display_name}"
            _print_bar(prefix, downloaded, total)
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    # Throttle refresh rate to reduce flicker/CPU
                    now = time.time()
                    if now - last_update >= 0.05:
                        _print_bar(prefix, downloaded, total)
                        last_update = now
            # Finalize bar at 100%
            _print_bar(prefix, downloaded, total)
            print()  # newline after bar
    except Exception:
        # Ensure the progress line doesn't stick on errors
        print()
        raise


def main():
    total_items = len(iso_list)
    for idx, iso in enumerate(iso_list, start=1):
        file_name = iso.get("file_name")
        url = iso.get("download_url")
        if not file_name or not url:
            continue

        dest_path = os.path.join(output_dir, file_name)
        prefix = f"[{idx}/{total_items} {(idx/total_items*100):.1f}%]"

        if os.path.exists(dest_path):
            print(f"{prefix} [✓] Already exists: {file_name}")
            continue

        try:
            download_file(url, dest_path, display_name=file_name)
            print(f"{prefix} [✔] Done: {file_name}")
        except Exception as e:
            print(f"{prefix} [✗] Failed: {file_name} - {e}")


if __name__ == "__main__":
    main()
