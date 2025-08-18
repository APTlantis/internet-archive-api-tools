import argparse
import logging
import sys
import os
from typing import Optional

import internetarchive

DEFAULT_DEST = "S:/Linux-FUCKIN-ISOs"


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


def main():
    p = argparse.ArgumentParser(description="Download an entire Internet Archive item/collection (v2)")
    p.add_argument("identifier", help="Archive.org item identifier")
    p.add_argument("--destdir", "-o", default=DEFAULT_DEST, help="Destination directory")
    p.add_argument("--ignore-existing", action="store_true", default=True, help="Skip files that already exist (default: true)")
    p.add_argument("--no-ignore-existing", action="store_false", dest="ignore_existing", help="Do not skip existing files")
    p.add_argument("--checksum", action="store_true", help="Verify checksums after download")
    p.add_argument("--retries", type=int, default=5, help="Number of retries")
    p.add_argument("--glob", help="Only download files matching this glob pattern (e.g. *.iso)")
    p.add_argument("--log-file", help="Optional path to a log file")
    p.add_argument("-v", action="count", default=0, help="Increase verbosity (-v info, -vv debug)")
    p.add_argument("--dry-run", action="store_true", help="List files without downloading")
    args = p.parse_args()

    setup_logging(args.v, args.log_file)

    os.makedirs(args.destdir, exist_ok=True)

    logging.info(f"Starting download for '{args.identifier}' -> {args.destdir}")

    if args.dry_run:
        # internetarchive library supports listing files via get_item
        item = internetarchive.get_item(args.identifier)
        for f in item.files:
            name = f.get("name")
            if args.glob and not item.session.matches(name, args.glob):
                continue
            print(name)
        return

    # Perform download
    internetarchive.download(
        args.identifier,
        destdir=args.destdir,
        verbose=args.v >= 1,
        ignore_existing=args.ignore_existing,
        checksum=args.checksum,
        retries=args.retries,
        glob_pattern=args.glob,
    )

    logging.info("Download finished")


if __name__ == "__main__":
    main()
