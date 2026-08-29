"""Capcode import from p2000.bommel.net.

Fetches P2000 capcode data from the bommel.net CSV export.
"""

import csv
import io
import logging
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# URL for full capcode CSV export
BOMMEL_CSV_URL = "https://p2000.bommel.net/cap2csv.php"

# Global progress tracking
_import_progress: dict[str, Any] = {
    "running": False,
    "imported": 0,
    "total_estimated": 10000,
    "percent": 0,
    "status": "idle",
}


def get_import_progress() -> dict[str, Any]:
    """Get current import progress."""
    return _import_progress.copy()


def reset_progress() -> None:
    """Reset progress state - call before starting import thread."""
    global _import_progress
    _import_progress = {
        "running": True,
        "imported": 0,
        "total_estimated": 10000,
        "percent": 0,
        "status": "Downloading capcodes from bommel.net...",
    }


def import_all_capcodes(database) -> dict[str, Any]:
    """
    Import all capcodes from bommel.net CSV export.
    This downloads a single CSV file with all ~9600 capcodes.
    """
    global _import_progress

    _import_progress = {
        "running": True,
        "imported": 0,
        "total_estimated": 10000,
        "percent": 0,
        "status": "Downloading capcodes from bommel.net...",
    }

    total_imported = 0
    total_skipped = 0

    try:
        # Download CSV
        logger.info(f"Fetching capcodes from {BOMMEL_CSV_URL}")
        req = urllib.request.Request(BOMMEL_CSV_URL, headers={"User-Agent": "P2000-Studio/2.1"})

        with urllib.request.urlopen(req, timeout=60) as response:
            csv_data = response.read().decode("utf-8", errors="replace")

        # Parse CSV (semicolon separated, quoted)
        # Format: "capcode";"discipline";"region";"location";"description";"remark"
        lines = csv_data.strip().split("\n")
        total_lines = len(lines)

        _import_progress.update({"total_estimated": total_lines, "status": f"Processing {total_lines} capcodes..."})

        for i, line in enumerate(lines):
            if not line.strip():
                continue

            try:
                # Parse semicolon-separated quoted values
                reader = csv.reader(io.StringIO(line), delimiter=";", quotechar='"')
                row = next(reader)

                if len(row) >= 5:
                    capcode = row[0].strip()
                    discipline = row[1].strip()
                    region = row[2].strip()
                    location = row[3].strip()
                    description = row[4].strip()
                    remark = row[5].strip() if len(row) > 5 else ""

                    # Skip invalid capcodes
                    if not capcode or not capcode[0].isdigit():
                        continue

                    try:
                        database.add_capcode(
                            capcode=capcode.zfill(9),
                            discipline=discipline,
                            region=region,
                            location=location,
                            description=description,
                            remark=remark,
                        )
                        total_imported += 1
                    except Exception:
                        total_skipped += 1  # Duplicate or error

            except Exception as e:
                logger.debug(f"Failed to parse line: {e}")
                total_skipped += 1

            # Update progress every 100 records
            if i % 100 == 0:
                percent = min(99, int((i / total_lines) * 100))
                _import_progress.update(
                    {
                        "imported": total_imported,
                        "percent": percent,
                        "status": f"Importing... {total_imported} capcodes",
                    }
                )

        _import_progress.update(
            {
                "running": False,
                "imported": total_imported,
                "percent": 100,
                "status": f"Complete! {total_imported} capcodes imported",
            }
        )

        logger.info(f"Capcode import complete: {total_imported} imported, {total_skipped} skipped")

    except Exception as e:
        logger.error(f"Capcode import failed: {e}")
        _import_progress.update({"running": False, "status": f"Error: {e}"})

    return {
        "imported": total_imported,
        "skipped": total_skipped,
        "regions": 26,  # All Dutch regions
    }
