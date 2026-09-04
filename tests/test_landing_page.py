from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


class _LandingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}
        self.links: list[str] = []
        self.structured_data: list[str] = []
        self._json_ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = values.get("name") or values.get("property")
            if key and values.get("content"):
                self.meta[key] = values["content"] or ""
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        if tag == "script" and values.get("type") == "application/ld+json":
            self._json_ld = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._json_ld = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._json_ld:
            self.structured_data.append(data)


def test_landing_page_has_problem_first_search_metadata() -> None:
    parser = _LandingParser()
    parser.feed((SITE / "index.html").read_text(encoding="utf-8"))

    assert "Merge queue for parallel coding agents" in parser.title
    assert "parallel coding-agent worktrees" in parser.meta["description"]
    assert parser.meta["robots"] == "index,follow"
    assert parser.meta["og:url"] == "https://yongjip.github.io/mergetrain/"
    data = json.loads("".join(parser.structured_data))
    assert data["@type"] == "SoftwareApplication"
    assert data["codeRepository"] == "https://github.com/yongjip/mergetrain"


def test_landing_page_has_no_tracking_or_release_version_drift() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")

    assert not re.search(
        r"google-analytics|googletagmanager|segment\.com|plausible\.io|posthog",
        html,
        re.IGNORECASE,
    )
    assert not re.search(r"\b(?:v)?\d+\.\d+\.\d+\b", html)
    assert "No product telemetry" in html


def test_landing_page_links_to_demo_install_and_evidence() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    parser = _LandingParser()
    parser.feed(html)

    assert "uvx mergetrain demo" in html
    assert "https://pypi.org/project/mergetrain/" in parser.links
    assert any(link.endswith("/benchmarks") for link in parser.links)
    assert (SITE / "styles.css").is_file()
    assert (SITE / "favicon.svg").is_file()
    assert (SITE / "robots.txt").is_file()
    assert (SITE / "sitemap.xml").is_file()
