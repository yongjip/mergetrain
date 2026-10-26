#!/usr/bin/env python3
"""Export the approved express logo. Requires rsvg-convert for PNG output."""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLUE = "#005de8"
INK = "#172232"


def render(source: Path, destination: Path, size: int | None = None) -> None:
    args = ["rsvg-convert", str(source), "-o", str(destination)]
    if size:
        args += ["-w", str(size), "-h", str(size)]
    subprocess.run(args, check=True)


def main() -> None:
    if not shutil.which("rsvg-convert"):
        raise SystemExit("Install librsvg to provide rsvg-convert, then rerun.")
    source = ROOT / "docs/brand"
    site = ROOT / "site/assets"
    plugin = ROOT / "plugins/mergetrain/assets"
    package = ROOT / "src/mergetrain/assets"
    for directory in (site, plugin, package):
        directory.mkdir(parents=True, exist_ok=True)

    mark = (source / "express.svg").read_text(encoding="utf-8")
    lockup = (source / "wordmark.svg").read_text(encoding="utf-8")
    for suffix, color in (("", BLUE), ("-dark", "#ffffff")):
        icon = site / f"mergetrain-icon{suffix}.svg"
        icon.write_text(mark.replace(BLUE, color), encoding="utf-8")
        (site / f"mergetrain-wordmark{suffix}.svg").write_text(
            lockup.replace(BLUE, color).replace(INK, "#ffffff" if suffix else INK),
            encoding="utf-8",
        )
        render(icon, site / f"mergetrain-icon{suffix}.png", 128)
        render(icon, plugin / f"icon{suffix}.png", 512)
        shutil.copyfile(site / f"mergetrain-icon{suffix}.png", package / f"icon{suffix}.png")

    # The transparent favicon switches its fill with the browser's color scheme.
    favicon = mark.replace(
        "</title>",
        "</title><style>@media(prefers-color-scheme:dark){path{fill:#fff}}</style>",
    )
    (ROOT / "site/favicon.svg").write_text(favicon, encoding="utf-8")

    ns = {"svg": "http://www.w3.org/2000/svg"}
    symbol_path = ET.fromstring(mark).find("svg:path", ns)
    wordmark_path = ET.fromstring(lockup).find("svg:path", ns)
    assert symbol_path is not None and wordmark_path is not None
    for height, name in ((630, "mergetrain-share"), (675, "mergetrain-brand-card")):
        top = (height - 390) / 2
        body = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" '
            f'viewBox="0 0 1200 {height}">'
            '<rect width="1200" height="100%" fill="#f7f7f5"/>'
            f'<path transform="translate(344 {top - 100}) scale(2)" fill="{BLUE}" '
            f'fill-rule="evenodd" d="{symbol_path.attrib["d"]}"/>'
            f'<path transform="translate(312 {top + 300}) scale(1.18)" fill="{INK}" '
            f'd="{wordmark_path.attrib["d"]}"/></svg>'
        )
        card = site / f"{name}.svg"
        card.write_text(body, encoding="utf-8")
        render(card, site / f"{name}.png")


if __name__ == "__main__":
    main()
