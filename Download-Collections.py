import internetarchive
import os

# Archive.org identifier
item_id = "linuxtracker.org_p1"
download_dir = "S:/Linux-FUCKIN-ISOs"

# Make sure directory exists
os.makedirs(download_dir, exist_ok=True)

# Download all files (skip existing, verify checksums)
internetarchive.download(
    item_id,
    verbose=True,
    destdir=download_dir,
    ignore_existing=True,
    checksum=True,
    retries=5
)
