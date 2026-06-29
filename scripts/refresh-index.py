#!/usr/bin/env python3
"""Regenerate the release index from whatever files are in this repo.

The dist repo owns all bookkeeping. Private app repos push their built
installers to a GitHub Release on this repo (assets are too big for git) and
commit only a small ``<app>/<version>/release.json`` describing them. This
script is the source of truth for everything derived from those manifests:

  * <app>/manifest.json   — every version of the app + per-asset metadata
  * <app>/readme.md        — that manifest rendered as a releases page
  * manifest.json          — one entry per app, pointing at its latest release
  * readme.md              — the root manifest rendered as a list of all apps

It is a pure function of the release.json files on disk: scanning is
idempotent, deleting a version self-heals on the next run, and markdown is
never hand-edited.

Per-app display names are read from apps/<app>.json ({"product_name": "..."}),
falling back to the app directory name.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = ROOT / "apps"
# Top-level entries that are not apps.
SKIP_DIRS = {".git", ".github", "apps", "scripts"}
VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def encode_href(path: str) -> str:
    return urllib.parse.quote(path, safe="/-._")


def md_link(text: str, href: str) -> str:
    return f"[{text}]({href})"


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def load_app_config(app_name: str) -> dict:
    config_path = APPS_DIR / f"{app_name}.json"
    if not config_path.is_file():
        return {}
    with config_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def list_apps() -> list[str]:
    configured = sorted(path.stem for path in APPS_DIR.glob("*.json")) if APPS_DIR.is_dir() else []
    discovered: set[str] = set()
    for entry in ROOT.iterdir():
        if not entry.is_dir() or entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        if any(VERSION_RE.fullmatch(child.name) for child in entry.iterdir() if child.is_dir()):
            discovered.add(entry.name)
    return sorted(set(configured) | discovered)


def infer_asset_meta(filename: str) -> tuple[str, str, str]:
    lower = filename.lower()
    if lower.endswith(".sig"):
        # Updater signature — inherit the signed artifact's platform/arch.
        platform, arch, _ = infer_asset_meta(filename[: -len(".sig")])
        return platform, arch, "signature"
    if lower.endswith(".appimage"):
        kind, platform = "AppImage", "linux"
    elif lower.endswith(".deb"):
        kind, platform = "deb", "linux"
    elif lower.endswith(".rpm"):
        kind, platform = "rpm", "linux"
    elif lower.endswith(".dmg"):
        kind, platform = "dmg", "macos"
    elif lower.endswith(".app.tar.gz"):
        kind, platform = "app", "macos"
    elif lower.endswith(".msi"):
        kind, platform = "msi", "windows"
    elif lower.endswith(".exe"):
        kind, platform = "setup", "windows"
    else:
        kind, platform = "file", "unknown"

    arch = "unknown"
    for token, label in (
        ("aarch64", "aarch64"),
        ("arm64", "aarch64"),
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
        ("x64", "x86_64"),
        ("i686", "i686"),
        ("universal", "universal"),
    ):
        if token in lower:
            arch = label
            break
    return platform, arch, kind


def version_published_at(app_name: str, version: str) -> str:
    rel_path = f"{app_name}/{version}"
    result = subprocess.run(
        ["git", "log", "-1", "--format=%aI", "--", rel_path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().replace("+00:00", "Z")
    version_dir = ROOT / app_name / version
    if version_dir.is_dir():
        latest = max((item.stat().st_mtime for item in version_dir.iterdir() if item.is_file()), default=0)
        if latest:
            return datetime.fromtimestamp(latest, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return utc_now()


def scan_app(app_name: str) -> dict:
    app_dir = ROOT / app_name
    config = load_app_config(app_name)
    product_name = config.get("product_name", app_name)
    releases: list[dict] = []

    if app_dir.is_dir():
        for version_dir in sorted(app_dir.iterdir(), key=lambda path: path.name, reverse=True):
            if not version_dir.is_dir() or not VERSION_RE.fullmatch(version_dir.name):
                continue
            release_file = version_dir / "release.json"
            if not release_file.is_file():
                continue
            with release_file.open(encoding="utf-8") as fh:
                release = json.load(fh)
            version = version_dir.name
            assets = []
            for asset in sorted(release.get("assets", []), key=lambda item: item["filename"].lower()):
                platform, arch, kind = infer_asset_meta(asset["filename"])
                assets.append(
                    {
                        "filename": asset["filename"],
                        "url": asset["url"],
                        "size": asset.get("size", 0),
                        "platform": platform,
                        "arch": arch,
                        "kind": kind,
                    }
                )
            if not assets:
                continue
            releases.append(
                {
                    "version": version,
                    "tag": release.get("tag", version),
                    "release_url": release.get("release_url"),
                    "published_at": version_published_at(app_name, version),
                    "assets": assets,
                }
            )

    releases.sort(key=lambda item: item["version"], reverse=True)
    latest = releases[0]["version"] if releases else None
    updated_at = releases[0]["published_at"] if releases else utc_now()
    return {
        "name": app_name,
        "product_name": product_name,
        "latest": latest,
        "updated_at": updated_at,
        "releases": releases,
    }


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def platform_label(platform: str, arch: str) -> str:
    labels = {"linux": "Linux", "macos": "macOS", "windows": "Windows", "unknown": "Unknown"}
    base = labels.get(platform, platform.title())
    return f"{base} ({arch})" if arch != "unknown" else base


def release_href(release: dict) -> str:
    """The GitHub release page if known, else the version directory."""
    return release.get("release_url") or encode_href(release["version"])


def render_app_readme(app_manifest: dict) -> str:
    product_name = app_manifest.get("product_name", app_manifest.get("name", "App"))
    releases = app_manifest.get("releases", [])
    latest = app_manifest.get("latest")
    latest_href = release_href(releases[0]) if releases else ""
    lines = [
        f"# {product_name}",
        "",
        f"**Latest release:** {md_link(latest, latest_href)}" if latest else "**Latest release:** none",
        "",
        "## Releases",
        "",
    ]
    for release in releases:
        version = release["version"]
        published_at = release.get("published_at", "")[:10]
        lines.append(f"### {md_link(version, release_href(release))} — {published_at}")
        lines.append("")
        lines.append("| Asset | Platform | Size |")
        lines.append("| --- | --- | --- |")
        for asset in release.get("assets", []):
            lines.append(
                f"| {md_link(asset['filename'], asset['url'])} | "
                f"{platform_label(asset['platform'], asset['arch'])} | {human_size(asset['size'])} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_root_readme(root_manifest: dict) -> str:
    lines = [
        "# Releases",
        "",
        "Public download mirror for private application releases.",
        "",
        "| App | Product | Latest |",
        "| --- | --- | --- |",
    ]
    for app_name, app_info in sorted(root_manifest.get("apps", {}).items(), key=lambda item: item[0].lower()):
        latest = app_info.get("latest")
        product_name = app_info.get("product_name", app_name)
        latest_cell = md_link(latest, encode_href(f"{app_name}/{latest}")) if latest else "—"
        lines.append(f"| {md_link(app_name, encode_href(f'{app_name}/'))} | {product_name} | {latest_cell} |")
    lines.extend(["", f"_Updated {root_manifest.get('updated_at', utc_now())}_", ""])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    updated_at = utc_now()
    root_apps: dict[str, dict] = {}

    for app_name in list_apps():
        app_manifest = scan_app(app_name)
        app_dir = ROOT / app_name
        app_dir.mkdir(parents=True, exist_ok=True)
        write_json(app_dir / "manifest.json", app_manifest)
        (app_dir / "readme.md").write_text(render_app_readme(app_manifest), encoding="utf-8")
        if app_manifest.get("latest"):
            root_apps[app_name] = {
                "product_name": app_manifest["product_name"],
                "latest": app_manifest["latest"],
                "path": f"{app_name}/",
            }

    root_manifest = {"updated_at": updated_at, "apps": root_apps}
    write_json(ROOT / "manifest.json", root_manifest)
    (ROOT / "readme.md").write_text(render_root_readme(root_manifest), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
