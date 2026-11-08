#!/usr/bin/env python3
"""Follow the Meldkamer version in cyberjunky/home-assistant-apps.

This repository only forwards existing installations to the image built there. When a newer
version has been published, this raises `version` in config.yaml and copies its changelog
entries, so those installations are offered the update. It waits until the image is on GHCR,
because an update offered earlier could not be installed.
"""

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

RAW = "https://raw.githubusercontent.com/cyberjunky/home-assistant-apps/main/app-meldkamer"
IMAGE = "cyberjunky/app-meldkamer"
VERSION_LINE = re.compile(r'^version:\s*"?([0-9.]+)"?\s*$', re.M)
ROOT = pathlib.Path(os.environ.get("SYNC_ROOT", "."))


def fetch(url: str, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def image_published(version: str) -> bool:
    token = json.loads(fetch(f"https://ghcr.io/token?scope=repository:{IMAGE}:pull"))["token"]
    accept = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"
    try:
        fetch(
            f"https://ghcr.io/v2/{IMAGE}/manifests/{version}",
            {"Authorization": f"Bearer {token}", "Accept": accept},
        )
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return False
        raise
    return True


def new_entries(changelog: str, current: str) -> str:
    """The changelog sections above the one for the current version."""
    start = changelog.index("\n## ") + 1
    end = changelog.find(f"\n## {current}\n")
    if end == -1:
        sys.exit(f"Version {current} is not in the upstream changelog; not guessing what changed")
    return changelog[start:end + 1]


def main() -> None:
    config_path = ROOT / "config.yaml"
    config = config_path.read_text(encoding="utf-8")
    current = VERSION_LINE.search(config).group(1)
    upstream = VERSION_LINE.search(fetch(f"{RAW}/config.yaml").decode()).group(1)

    if as_tuple(upstream) <= as_tuple(current):
        print(f"Up to date at {current} (upstream {upstream})")
        return
    if not image_published(upstream):
        print(f"{upstream} is not on GHCR yet; staying at {current}")
        return

    entries = new_entries(fetch(f"{RAW}/CHANGELOG.md").decode(), current)
    changelog_path = ROOT / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    first = changelog.index("\n## ") + 1
    changelog_path.write_text(changelog[:first] + entries + changelog[first:], encoding="utf-8")
    config_path.write_text(VERSION_LINE.sub(f'version: "{upstream}"', config, count=1), encoding="utf-8")
    print(f"Updated {current} to {upstream}")


if __name__ == "__main__":
    main()
