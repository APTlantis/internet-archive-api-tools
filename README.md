# Internet Archive API Tools (v2)

[![Made with Python](https://img.shields.io/badge/Made%20with-Python-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Python Versions](https://img.shields.io/badge/python-3.9%2B-blue.svg)](#requirements)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)](#)
[![Requests](https://img.shields.io/badge/uses-requests-informational)](https://docs.python-requests.org/)
[![Internet Archive](https://img.shields.io/badge/API-archive.org-blue?logo=archive.org&logoColor=white)](https://archive.org/developers/)
[![Last Updated](https://img.shields.io/badge/last%20updated-2025--08--18-success)](#)

A small set of Python CLI tools for searching and downloading software images from the Internet Archive. The v2 tools focus on resiliency, logging, progress feedback, and convenience while keeping the original scripts available under `Versions/`.

## Contents
- IA-Advanced-Search-v2.py — advanced search wrapper that produces a JSON list of ISO/IMG/ZIP files.
- Download-From-JSON-v2.py — downloader for a list produced by the search tool (resume, retries, filters, progress bars).
- Download-Collections-v2.py — download all or filtered files from a specific Internet Archive item/collection using the official `internetarchive` library.
- IA-Iso-Spider.py — seed with 3–5 collection IDs or item identifiers, crawls related collections/items prioritizing higher ISO yield; logs and outputs JSONL results.
- Versions/ — original legacy scripts preserved.

## Features
- Robust HTTP with retries/backoff and default timeouts (via `requests` + `urllib3.Retry`).
- Structured logging to stdout and optional log file (`-v` / `-vv` for verbosity).
- Per-file progress bars that auto-disable when stdout is not a TTY.
- Resume support via HTTP Range requests (opt-in `--resume`).
- Include/Exclude filtering (regex) and `--max` to limit items.
- Dry-run modes to preview actions without downloading.
- Customizable User-Agent header.

## Requirements
- Python 3.9+ (tested with 3.10–3.13)
- Dependencies:
  - `requests`
  - `urllib3` (via requests)
  - `internetarchive` (only for Download-Collections-v2.py)

Install globally or in a virtual environment:

```bash
pip install requests internetarchive
```

On Windows PowerShell:

```powershell
py -m pip install requests internetarchive
```

## Quick Start

1) Find ISO/IMG/ZIP files on archive.org and save metadata to JSON

```powershell
# Windows PowerShell examples (works similarly on bash)
python IA-Advanced-Search-v2.py -q "(format:ISO OR format:IMG) AND mediatype:software AND description:\"linux, distribution\"" -o iso_metadataz.json -v
```

2) Download from that JSON list to a directory

```powershell
python Download-From-JSON-v2.py -i iso_metadataz.json -o S:\Linux-FUCKIN-ISOs --resume -v
```

3) Download a full Internet Archive item/collection

4) Crawl via Spider (seed collections -> discover more -> prioritize by ISO yield)

```powershell
python IA-Iso-Spider.py --seeds ubuntu_releases linuxtracker debian-cd -v --max-visits 100 --out-jsonl iso_spider_results.jsonl --stats-json iso_spider_stats.json
```

```powershell
# Download all files listed under a specific identifier
python Download-Collections-v2.py linuxtracker.org_p1 -o S:\Linux-FUCKIN-ISOs -v --glob *.iso
```

Tip: Add `--dry-run` to preview actions without downloading.

## Script Details

### IA-Advanced-Search-v2.py
Searches the Internet Archive Advanced Search API and optionally fetches per-item metadata to enumerate downloadable files. Results are saved as a JSON list.

Key options:
- `--query/-q` Advanced search query (default tailored for Linux ISOs)
- `--rows` Results per page (<= 1000)
- `--max-pages` Limit total pages
- `--fields` Additional fields to retrieve
- `--out/-o` Output JSON (default: `iso_metadataz.json` in this repo snapshot)
- `--timeout`, `--retries`, `--backoff` Network resilience
- `--user-agent` Custom UA
- `--dry-run` Only print identifiers and titles
- `-v`/`-vv` Increase verbosity; `-vv` enables urllib3 debug logs

Output format (per entry):
```json
{
  "identifier": "<archive.org item>",
  "title": "<item title>",
  "file_name": "<file name>",
  "download_url": "https://archive.org/download/<identifier>/<file>",
  "size": "<bytes or unknown>"
}
```

### IA-Iso-Spider.py
Crawls from a small set of Internet Archive collection IDs, discovers item identifiers and related collections via metadata, and prioritizes crawling of collections that historically yield more ISO files. Outputs JSONL of found ISO entries and writes a stats JSON summarizing yield per collection. A rolling log file records progress.

Key options:
- `--seeds` 3–5 starting collection IDs or item identifiers (default includes popular Linux release collections, plus vintagesoftware)
- `--max-visits` cap on number of frontier pops (collections/items processed)
- `--max-depth` limit expansion depth from seeds
- `--out-jsonl` path for results (one JSON per line)
- `--stats-json` path for stats summary (per collection: items, isos)
- `--sleep`, `--timeout`, `--retries`, `--backoff`, `--user-agent`, `-v`, `--log-file`
- `--stop-on-dry-spell` stop after N consecutive visits yield no new ISOs

Example:
```powershell
python IA-Iso-Spider.py --seeds ubuntu_releases linuxtracker debian-cd -v --max-visits 150
```

### Download-From-JSON-v2.py
Consumes a JSON file (like the one produced above) and downloads each file.

Highlights:
- Resume support (`--resume`) via HTTP Range
- Per-file progress bar (auto-disables on non-TTY or `--no-progress`)
- Include/Exclude filtering using regex against file_name/title
- `--max` to limit processed items
- Retries/backoff and default timeouts

Common options:
- `--input/-i` Path to JSON (default: `iso_metadataz.json`)
- `--output-dir/-o` Destination (default: `S:/Linux-FUCKIN-ISOs/`)
- `--retries`, `--timeout`, `--backoff`, `--chunk-size`
- `--resume`, `--no-progress`, `--dry-run`, `--max`, `--include`, `--exclude`
- `--user-agent`, `--log-file`, `-v`

Example:
```powershell
python Download-From-JSON-v2.py -i iso_metadataz.json -o D:\ISOs --resume --include "ubuntu|mint" --exclude beta -v
```

### Download-Collections-v2.py
Downloads an entire Internet Archive item/collection using the `internetarchive` package.

Options:
- `identifier` Required archive.org item id
- `--destdir/-o` Destination directory
- `--ignore-existing/--no-ignore-existing` Skip or re-download existing files
- `--checksum` Verify checksums
- `--retries` Number of retries
- `--glob` Filter files with a glob (e.g., `*.iso`)
- `--dry-run` List files only
- `-v` Verbosity

Example:
```powershell
python Download-Collections-v2.py tsurugi_linux_2023.2 -o D:\Archive --glob *.iso -v
```

## Notes & Defaults
- Default output directory in examples is a Windows path (`S:/Linux-FUCKIN-ISOs/`). Adjust paths for your OS and preferences.
- The tools set a default User-Agent. You can override via `--user-agent`.
- By default, urllib3 retry noise is suppressed unless you use `-vv` on the search tool.
- Legacy scripts remain in `Versions/` if you prefer the original simpler behavior.

## Troubleshooting
- Connection resets / transient errors: The tools automatically retry with backoff. Increase `--retries`/`--backoff` if needed.
- UnicodeDecodeError on JSON: v2 tools read JSON with UTF-8 explicitly.
- Progress bar not showing: Progress auto-disables when stdout isn’t a TTY. Use a real terminal or omit `--no-progress`.

## Contributing
PRs and issues are welcome. Please include:
- Your environment (OS, Python version)
- Command used and relevant output
- Minimal repro if applicable

## Disclaimer
These tools access third-party content hosted on the Internet Archive. Ensure you comply with their Terms of Use and applicable laws. Use at your own risk.

---

# Rust Counterparts

[![Made with Rust](https://img.shields.io/badge/Made%20with-Rust-000000?logo=rust&logoColor=white)](https://www.rust-lang.org/)

This repo now also includes Rust counterparts for the Python tools. They live under `rust/` and provide similar functionality with a single static binary per tool (no Python runtime required).

## Build

- Install Rust (Windows/macOS/Linux): https://rustup.rs
  - Windows: ensure you have the MSVC build tools (Visual Studio Build Tools with "C++ build tools"), otherwise cargo may fail with `link.exe not found`.
- Build all tools from the project root:

```powershell
cd rust
cargo build --release
```

Binaries will be produced under `rust/target/release/`:
- `ia-advanced-search`
- `download-from-json`
- `download-collections`

You can add `rust/target/release` to your PATH or call them with full path.

## Rust Tooling Parity

1) ia-advanced-search (Rust)
- Mirrors IA-Advanced-Search-v2.py
- Key flags:
  - `--query/-q`, `--rows`, `--max-pages`, `--sleep`, `--fields ...`
  - `--out/-o`, `--timeout`, `--retries`, `--backoff`, `--user-agent`, `--dry-run`, `-v`
- Example:
```powershell
rust/target/release/ia-advanced-search -q "(format:ISO OR format:IMG) AND mediatype:software" -o iso_metadata.json -v
```

2) download-from-json (Rust)
- Mirrors Download-From-JSON-v2.py
- Key flags:
  - `--input/-i`, `--output-dir/-o`, `--retries`, `--timeout`, `--backoff`, `--chunk-size`
  - `--resume`, `--no-progress`, `--dry-run`, `--max`, `--include`, `--exclude`, `--user-agent`, `-v`
- Example:
```powershell
rust/target/release/download-from-json -i iso_metadata.json -o S:\Linux-FUCKIN-ISOs --resume -v
```

3) download-collections (Rust)
- Counterpart to Download-Collections-v2.py (without the `internetarchive` Python dependency); uses IA metadata + direct downloads.
- Key flags:
  - `identifier` (positional), `--destdir/-o`, `--ignore-existing/--no-ignore-existing`, `--glob`, `--retries`, `--dry-run`, `-v`
- Example:
```powershell
rust/target/release/download-collections tsurugi_linux_2023.2 -o D:\Archive --glob *.iso -v
```

Notes:
- The Rust download-collections currently does not implement checksum verification; it focuses on listing and downloading filtered files.
- All Rust tools implement simple retry/backoff logic and default timeouts.

---

# Go Counterparts

[![Made with Go](https://img.shields.io/badge/Made%20with-Go-00ADD8?logo=go&logoColor=white)](https://go.dev/)

Go implementations of the same tools live under `go/` as separate commands, built without external dependencies.

## Build

- Install Go 1.22+ from https://go.dev/dl/
- From project root:

```powershell
# Windows PowerShell
cd go
# build individual commands
go build ./cmd/ia_advanced_search
go build ./cmd/download_from_json
go build ./cmd/download_collections

# or install into GOPATH/bin (adds to PATH)
go install ./cmd/ia_advanced_search
go install ./cmd/download_from_json
go install ./cmd/download_collections
```

Binaries will be placed in the current folder when using `go build`, named after the package directory (e.g., `ia_advanced_search.exe` on Windows).

## Go Tooling Parity

1) ia_advanced_search (Go)
- Mirrors IA-Advanced-Search-v2.py.
- Flags:
  - `--query/-q`, `--rows`, `--max-pages`, `--sleep`, `--fields` (space-separated)
  - `--out/-o`, `--timeout`, `--retries`, `--backoff`, `--user-agent`, `--dry-run`, `-v`
- Example:
```powershell
./ia_advanced_search -q "(format:ISO OR format:IMG) AND mediatype:software" -o iso_metadata.json -v
```

2) download_from_json (Go)
- Mirrors Download-From-JSON-v2.py.
- Flags:
  - `--input/-i`, `--output-dir/-o`, `--retries`, `--timeout`, `--backoff`, `--chunk-size`
  - `--resume`, `--no-progress`, `--dry-run`, `--max`, `--include`, `--exclude`, `--user-agent`, `-v`
- Example:
```powershell
./download_from_json -i iso_metadata.json -o S:\Linux-FUCKIN-ISOs --resume -v
```

3) download_collections (Go)
- Counterpart to Download-Collections-v2.py; uses IA metadata + direct downloads (no Python dependency).
- Flags:
  - `identifier` (positional via `--identifier` or first arg), `--destdir/-o`, `--ignore-existing`, `--glob`, `--retries`, `--dry-run`, `-v`, `--checksum`
- Example:
```powershell
./download_collections --identifier tsurugi_linux_2023.2 --destdir D:\Archive --glob *.iso -v --checksum
```

Notes:
- The Go tools implement basic retry/backoff and default timeouts.
- `download_collections` verifies MD5 checksums when `--checksum` is provided and metadata includes MD5.
- Progress output is lightweight text-based (no external packages) and auto-updates on the same line when possible.
