"""Pointers and on-demand access to the canonical, independently maintained registry."""
from __future__ import annotations

import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "https://github.com/smlfg/HAI-skills-registry"
LIBRARY = "https://smlfg.github.io/HAI-skills-registry/"
INDEX_URI = "hai://skills/index"
SKILL_URI = "hai://skills/{category}/{name}"
RAW_BASE = "https://raw.githubusercontent.com/smlfg/HAI-skills-registry/main/"


def registry_info() -> dict:
    return {
        "repository": REPOSITORY,
        "library": LIBRARY,
        "index_resource": INDEX_URI,
        "skill_resource_template": SKILL_URI,
        "index_url": f"{REPOSITORY}/blob/main/README.md",
        "accessibility": "not_checked",
        "local_installation": "not_checked",
        "activation": "not_checked",
        "note": "Discovery does not imply accessibility, installation, or activation. Resource reads require network access.",
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _read(path: str) -> str:
    try:
        with build_opener(_NoRedirect).open(
            Request(RAW_BASE + path, headers={"User-Agent": "hai-mcp"}), timeout=5
        ) as response:
            data = response.read(1_000_001)
        if len(data) > 1_000_000:
            raise ValueError("Registry response exceeds size limit")
        return data.decode("utf-8")
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError) as exc:
        raise ValueError(f"Registry unavailable or file not found: {exc}") from exc


def read_index() -> str:
    return (
        "# Official HAI Skills Registry\n\n"
        "The index below is the canonical registry README. For a link such as "
        "skills/engineering/prior-art-search/SKILL.md, read the MCP resource "
        "hai://skills/engineering/prior-art-search. The original instructions are fetched on demand. "
        "Access does not imply local installation or activation.\n\n"
        + _read("README.md")
    )


def read_skill(category: str, name: str) -> str:
    if not all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", x) for x in (category, name)):
        raise ValueError("Invalid registry category or skill name")
    return _read(f"skills/{category}/{name}/SKILL.md")
