#!/usr/bin/env python3
"""
Fischipedia MediaWiki Parser, Action API Client & Markdown Post-Processor.

Provides:
1. Direct MediaWiki Action API (`w/api.php`) helpers for instant, zero-credit wiki search
   (`opensearch` + `list=search`), redirect resolution (including `#tofragment`), section
   outlines (`action=parse`), category listing (`list=categorymembers`), raw wikitext
   retrieval, and server-side expanded HTML parsing (`action=parse&prop=text`).
2. Zero-credit, zero-dependency MediaWiki HTML-to-Markdown DOM parser (`_DOMBuilder` +
   `convert_mw_html_to_markdown`) that converts expanded MediaWiki parser output directly
   into clean Markdown while preserving `https://static.wikitide.net/...` Infobox, Skin,
   and Gallery images and stripping hidden tooltips, navboxes, filter widgets, and tiny
   inline icons (`<= 32px`).
3. Universal MediaWiki support via `FISCHIPEDIA_BASE_URL` / `MEDIAWIKI_BASE_URL` and
   `FISCHIPEDIA_API_URL` / `MEDIAWIKI_API_URL` environment variables (defaults to
   `https://fischipedia.org`).
4. Optional Firecrawl `/scrape` fallback (`--use-firecrawl`) and post-processing pipeline
   (`clean_fischipedia_markdown`) for cleaning raw Markdown/JSON scrapes, handling
   Disambiguation pages, and extracting `#Fragment` sections or table rows (with caching).
"""

from __future__ import annotations

import argparse
import gzip
import html
import html.parser
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FISCHIPEDIA_BASE_URL = os.environ.get(
    "FISCHIPEDIA_BASE_URL",
    os.environ.get("MEDIAWIKI_BASE_URL", "https://fischipedia.org"),
).rstrip("/")

FISCHIPEDIA_API_URL = os.environ.get(
    "FISCHIPEDIA_API_URL",
    os.environ.get("MEDIAWIKI_API_URL", f"{FISCHIPEDIA_BASE_URL}/w/api.php"),
)

FISCHIPEDIA_USER_AGENT = os.environ.get(
    "FISCHIPEDIA_USER_AGENT",
    f"FischMCP/2.0 ({FISCHIPEDIA_BASE_URL}; zero-credit MediaWiki MCP)",
)

# In-memory cache of cleaned pages keyed by (canonical_url, strip_wiki_links, compact_infobox, include_images, use_firecrawl)
_CLEANED_PAGE_CACHE: dict[tuple[str, bool, bool, bool, bool], tuple[str, str, str | None]] = {}

DEFAULT_EXCLUDE_SECTIONS: list[str] = [
    "Skins",
    "Gallery",
    "Catch Quips",
    "Description",
]

# ---------------------------------------------------------------------------
# 1. MediaWiki Action API (`w/api.php`) Helpers
# ---------------------------------------------------------------------------


def mw_api_request(params: dict[str, Any], *, timeout: int = 20) -> dict[str, Any] | list[Any]:
    """Perform a gzipped GET request against the MediaWiki `w/api.php` endpoint."""
    query_params = {**params, "format": "json"}
    qs = urllib.parse.urlencode(query_params)
    req = urllib.request.Request(
        f"{FISCHIPEDIA_API_URL}?{qs}",
        headers={
            "User-Agent": FISCHIPEDIA_USER_AGENT,
            "Accept-Encoding": "gzip",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw_bytes = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw_bytes = gzip.decompress(raw_bytes)
        return json.loads(raw_bytes.decode("utf-8"))


def page_title_to_url(title: str, *, base_url: str = FISCHIPEDIA_BASE_URL) -> str:
    """Convert a canonical MediaWiki page title into its `/wiki/...` URL."""
    slug = urllib.parse.quote(title.strip().replace(" ", "_"), safe="_-():'/")
    return f"{base_url.rstrip('/')}/wiki/{slug}"


def extract_title_from_url_or_page(url_or_page: str) -> str:
    """Extract the MediaWiki page title (without `#Fragment`) from a page name or URL."""
    url, _ = split_target_and_fragment(url_or_page)
    m = re.match(r"^https?://[^/]+/wiki/(.+)$", url, flags=re.IGNORECASE)
    if m:
        return urllib.parse.unquote(m.group(1)).replace("_", " ")
    return url_or_page.split("#", 1)[0].strip()


def resolve_canonical_title(url_or_page: str) -> tuple[str, str | None]:
    """
    Use `w/api.php?action=query&redirects=1` to resolve redirects and canonical title casing.
    Returns `(canonical_title, redirected_from_or_None)`.
    Falls back to case-insensitive `opensearch` if the exact title casing is missing.
    """
    raw_title = extract_title_from_url_or_page(url_or_page)
    try:
        data = mw_api_request(
            {
                "action": "query",
                "titles": raw_title,
                "redirects": 1,
            }
        )
        if not isinstance(data, dict):
            return raw_title, None
        query = data.get("query", {})
        redirects = query.get("redirects", [])
        redirected_from = redirects[0].get("from") if redirects else None
        pages = query.get("pages", {})
        for page_id, page_info in pages.items():
            if str(page_id) != "-1" and "missing" not in page_info:
                return str(page_info.get("title", raw_title)), redirected_from
        # If exact title was missing, check opensearch for case-insensitive title match
        os_data = mw_api_request(
            {
                "action": "opensearch",
                "search": raw_title,
                "limit": 5,
                "redirects": "resolve",
            }
        )
        if isinstance(os_data, list) and len(os_data) >= 2 and isinstance(os_data[1], list):
            for candidate in os_data[1]:
                if isinstance(candidate, str) and candidate.lower() == raw_title.lower():
                    return candidate, raw_title
    except Exception:
        pass
    return raw_title, None


def search_fischipedia(query: str, *, limit: int = 10) -> dict[str, Any]:
    """
    Search the wiki using MediaWiki's `opensearch` and `list=search` APIs.
    Returns structured title matches, canonical URLs, and cleaned text snippets.
    """
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Search query cannot be empty.")
    clamped_limit = max(1, min(int(limit), 50))

    results_by_title: dict[str, dict[str, Any]] = {}

    # 1. OpenSearch for exact/prefix title matches
    try:
        os_data = mw_api_request(
            {
                "action": "opensearch",
                "search": clean_query,
                "limit": clamped_limit,
                "redirects": "resolve",
            }
        )
        if isinstance(os_data, list) and len(os_data) >= 4:
            titles = os_data[1] if isinstance(os_data[1], list) else []
            urls = os_data[3] if isinstance(os_data[3], list) else []
            for idx, title in enumerate(titles):
                if not isinstance(title, str):
                    continue
                url = urls[idx] if idx < len(urls) and isinstance(urls[idx], str) else page_title_to_url(title)
                results_by_title[title] = {
                    "title": title,
                    "url": url,
                    "match_type": "title_prefix",
                    "snippet": "",
                }
    except Exception:
        pass

    # 2. Full-text search (`list=search`) for rich snippets and broader matches
    total_hits = 0
    try:
        sr_data = mw_api_request(
            {
                "action": "query",
                "list": "search",
                "srsearch": clean_query,
                "srlimit": clamped_limit,
            }
        )
        if isinstance(sr_data, dict):
            q_obj = sr_data.get("query", {})
            total_hits = int(q_obj.get("searchinfo", {}).get("totalhits", 0))
            for item in q_obj.get("search", []):
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).strip()
                if not title:
                    continue
                raw_snippet = str(item.get("snippet", ""))
                clean_snippet = html.unescape(re.sub(r"<[^>]+>", "", raw_snippet)).strip()
                clean_snippet = re.sub(r"\s+", " ", clean_snippet)
                if title in results_by_title:
                    results_by_title[title]["snippet"] = clean_snippet
                    results_by_title[title]["wordcount"] = item.get("wordcount")
                else:
                    results_by_title[title] = {
                        "title": title,
                        "url": page_title_to_url(title),
                        "match_type": "fulltext",
                        "snippet": clean_snippet,
                        "wordcount": item.get("wordcount"),
                    }
    except Exception:
        pass

    items = list(results_by_title.values())[:clamped_limit]
    return {
        "query": clean_query,
        "total_hits": total_hits or len(items),
        "results": items,
    }


def get_page_outline(url_or_page: str) -> dict[str, Any]:
    """
    Fetch page metadata, categories, and section headings/anchors via MediaWiki's
    `action=parse&prop=sections|categories|displaytitle&redirects=1`.
    """
    raw_title = extract_title_from_url_or_page(url_or_page)
    data = mw_api_request(
        {
            "action": "parse",
            "page": raw_title,
            "prop": "sections|categories|displaytitle",
            "redirects": 1,
        }
    )
    if not isinstance(data, dict) or "error" in data:
        resolved_title, _ = resolve_canonical_title(raw_title)
        if resolved_title != raw_title:
            data = mw_api_request(
                {
                    "action": "parse",
                    "page": resolved_title,
                    "prop": "sections|categories|displaytitle",
                    "redirects": 1,
                }
            )
    if not isinstance(data, dict) or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError(
            f"MediaWiki parse failed for '{raw_title}': {err.get('info', 'Unknown error')}"
        )

    parse = data.get("parse", {})
    canonical_title = str(parse.get("title", raw_title))
    canonical_url = page_title_to_url(canonical_title)

    sections = []
    for sec in parse.get("sections", []):
        if not isinstance(sec, dict):
            continue
        anchor = str(sec.get("anchor") or sec.get("linkAnchor") or "")
        sections.append(
            {
                "number": sec.get("number"),
                "level": int(sec.get("level", 2)),
                "heading": html.unescape(re.sub(r"<[^>]+>", "", str(sec.get("line", "")))),
                "anchor": anchor,
                "fragment_target": f"{canonical_title}#{anchor}" if anchor else canonical_title,
            }
        )

    categories = [
        str(cat.get("*", "")).replace("_", " ")
        for cat in parse.get("categories", [])
        if isinstance(cat, dict)
        and not cat.get("hidden")
        and not str(cat.get("*", "")).lower().startswith("pages_using_")
    ]

    return {
        "title": canonical_title,
        "pageid": parse.get("pageid"),
        "url": canonical_url,
        "categories": categories,
        "sections": sections,
    }


def get_page_wikitext(url_or_page: str) -> dict[str, Any]:
    """
    Fetch raw MediaWiki source wikitext (`action=parse&prop=wikitext|displaytitle&redirects=1`).
    """
    raw_title = extract_title_from_url_or_page(url_or_page)
    data = mw_api_request(
        {
            "action": "parse",
            "page": raw_title,
            "prop": "wikitext|displaytitle",
            "redirects": 1,
        }
    )
    if not isinstance(data, dict) or "error" in data:
        resolved_title, _ = resolve_canonical_title(raw_title)
        if resolved_title != raw_title:
            data = mw_api_request(
                {
                    "action": "parse",
                    "page": resolved_title,
                    "prop": "wikitext|displaytitle",
                    "redirects": 1,
                }
            )
    if not isinstance(data, dict) or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError(
            f"MediaWiki wikitext fetch failed for '{raw_title}': {err.get('info', 'Unknown error')}"
        )
    parse = data.get("parse", {})
    canonical_title = str(parse.get("title", raw_title))
    wikitext = str(parse.get("wikitext", {}).get("*", ""))
    return {
        "title": canonical_title,
        "pageid": parse.get("pageid"),
        "url": page_title_to_url(canonical_title),
        "wikitext": wikitext,
    }


def list_category_members(
    category: str,
    *,
    filter_text: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """
    List pages inside a MediaWiki category via `w/api.php?action=query&list=categorymembers`.
    """
    clean_cat = category.strip()
    if not clean_cat.lower().startswith("category:"):
        clean_cat = f"Category:{clean_cat}"

    clamped_limit = max(1, min(int(limit), 500))
    members: list[dict[str, str]] = []
    cmcontinue: str | None = None
    needle = filter_text.strip().lower() if filter_text else None

    while len(members) < clamped_limit:
        batch_limit = 500 if needle else min(500, clamped_limit - len(members))
        params: dict[str, Any] = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": clean_cat,
            "cmlimit": batch_limit,
            "cmnamespace": "0|14",  # Main articles and subcategories
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue

        data = mw_api_request(params)
        if not isinstance(data, dict):
            break
        batch = data.get("query", {}).get("categorymembers", [])
        for item in batch:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", ""))
            if not title:
                continue
            if needle and needle not in title.lower():
                continue
            members.append(
                {
                    "title": title,
                    "url": page_title_to_url(title),
                    "type": "subcategory" if item.get("ns") == 14 else "page",
                }
            )
            if len(members) >= clamped_limit:
                break

        cont = data.get("continue", {})
        cmcontinue = cont.get("cmcontinue") if isinstance(cont, dict) else None
        if not cmcontinue:
            break

    return {
        "category": clean_cat,
        "filter_text": filter_text,
        "count": len(members),
        "has_more": bool(cmcontinue),
        "members": members,
    }


# ---------------------------------------------------------------------------
# 2. Zero-Credit MediaWiki HTML-to-Markdown Parser (`action=parse&prop=text`)
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        cls = self.attrs.get("class", "")
        return set(cls.split()) if cls else set()

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")


_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

_EXCLUDED_HTML_TAGS = {"style", "script", "link", "meta", "noscript", "header", "button"}

_EXCLUDED_HTML_CLASSES = {
    "ext-floatingui-content",
    "tabber__header",
    "filter-wrapper",
    "control-preview-minigame",
    "navbox",
    "navbox-border",
    "fish-navbox",
    "mw-editsection",
    "item-icon",
    "ambox",
    "toc",
    "catlinks",
    "mw-empty-elt",
    "magnify",
    "reference",
    "plainlinks",
}

_EXCLUDED_MBOX_CLASSES = {"stub", "subjective", "outdated", "trivia", "delete"}


class _DOMBuilder(html.parser.HTMLParser):
    """Lightweight stdlib HTML-to-DOM builder that strips MediaWiki chrome & hidden elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack: list[_Node] = [self.root]
        self.skip_depth = 0

    def _should_skip(self, tag: str, attrs_dict: dict[str, str]) -> bool:
        if tag in _EXCLUDED_HTML_TAGS:
            return True
        classes = set(attrs_dict.get("class", "").split())
        if classes & _EXCLUDED_HTML_CLASSES:
            return True
        if "mbox" in classes and (classes & _EXCLUDED_MBOX_CLASSES):
            return True
        if attrs_dict.get("id") in {"toc"}:
            return True
        style = attrs_dict.get("style", "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.skip_depth > 0:
            if tag not in _VOID_TAGS:
                self.skip_depth += 1
            return

        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if self._should_skip(tag, attrs_dict):
            if tag not in _VOID_TAGS:
                self.skip_depth = 1
            return

        node = _Node(tag=tag, attrs=attrs_dict)
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        if self.skip_depth > 0:
            self.skip_depth -= 1
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0 and data:
            self.stack[-1].children.append(data)


def _normalize_url(url: str, base_url: str = FISCHIPEDIA_BASE_URL) -> str:
    url = url.strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{base_url.rstrip('/')}{url}"
    return url


def _find_all(node: _Node, predicate: Any) -> list[_Node]:
    res: list[_Node] = []
    for child in node.children:
        if isinstance(child, _Node):
            if predicate(child):
                res.append(child)
            res.extend(_find_all(child, predicate))
    return res


def _find_first(node: _Node, predicate: Any) -> _Node | None:
    for child in node.children:
        if isinstance(child, _Node):
            if predicate(child):
                return child
            found = _find_first(child, predicate)
            if found is not None:
                return found
    return None


def _render_img(node: _Node, base_url: str = FISCHIPEDIA_BASE_URL) -> str:
    """
    Render `<img>` tags to Markdown `![alt](https://static.wikitide.net/...)` while stripping
    tiny decorative inline icons (`<= 32px`, minigame bar caps, bestiary/enchanting icons)
    and converting the inline Robux icon into `"Robux "`.
    """
    src = _normalize_url(node.attrs.get("src", ""), base_url)
    if not src:
        return ""
    alt = node.attrs.get("alt", "").strip()
    alt = re.sub(r"Robux(\d)", r"Robux \1", alt)
    if "Robux.png" in src or "Robux.svg" in src or "icon for Robux" in alt:
        return "Robux "
    width_str = node.attrs.get("width", "")
    height_str = node.attrs.get("height", "")
    try:
        if width_str and int(width_str) <= 32 and (not height_str or int(height_str) <= 32):
            return ""
    except ValueError:
        pass
    if any(tok in src for tok in ("Control_Minigame_", "Bestiary_Description_Icon", "Enchanting.png")):
        return ""
    return f"![{alt}]({src})"


def _wrap_inline_marker(raw_inner: str, marker: str) -> str:
    stripped = raw_inner.strip()
    if not stripped:
        return raw_inner
    lead = " " if raw_inner[:1].isspace() else ""
    trail = " " if raw_inner[-1:].isspace() else ""
    return f"{lead}{marker}{stripped}{marker}{trail}"


def _render_inline(node: _Node | str, *, base_url: str = FISCHIPEDIA_BASE_URL, br_sep: str = "\n") -> str:
    if isinstance(node, str):
        return node
    tag = node.tag
    if tag == "br":
        return br_sep
    if tag == "img":
        return _render_img(node, base_url)
    if tag in {"b", "strong"}:
        raw_inner = "".join(_render_inline(c, base_url=base_url, br_sep=br_sep) for c in node.children)
        return _wrap_inline_marker(raw_inner, "**")
    if tag in {"i", "em"}:
        raw_inner = "".join(_render_inline(c, base_url=base_url, br_sep=br_sep) for c in node.children)
        return _wrap_inline_marker(raw_inner, "*")
    if tag == "code":
        raw_inner = "".join(_render_inline(c, base_url=base_url, br_sep=br_sep) for c in node.children)
        return _wrap_inline_marker(raw_inner, "`")
    if tag == "a":
        href = _normalize_url(node.attrs.get("href", ""), base_url)
        raw_inner = "".join(_render_inline(c, base_url=base_url, br_sep=br_sep) for c in node.children)
        inner = raw_inner.strip()
        if not inner:
            return ""
        lead = " " if raw_inner[:1].isspace() else ""
        trail = " " if raw_inner[-1:].isspace() else ""
        if inner.startswith("![") and "/wiki/File:" in href:
            return f"{lead}{inner}{trail}"
        if not href or href.startswith("#"):
            return f"{lead}{inner}{trail}"
        return f"{lead}[{inner}]({href}){trail}"

    return "".join(_render_inline(c, base_url=base_url, br_sep=br_sep) for c in node.children)


def _clean_inline_ws(text: str) -> str:
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _clean_comma_joined(text: str) -> str:
    text = _clean_inline_ws(text)
    text = re.sub(r"^(?:,\s*)+|(?:\s*,)+$", "", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)
    return text.strip()


def _render_infobox(node: _Node, base_url: str = FISCHIPEDIA_BASE_URL) -> str:
    out: list[str] = []
    img_containers = _find_all(node, lambda n: "infobox-img" in n.classes)
    seen_srcs: set[str] = set()
    for ic in img_containers:
        for img in _find_all(ic, lambda n: n.tag == "img"):
            md_img = _render_img(img, base_url)
            if md_img and md_img not in seen_srcs:
                seen_srcs.add(md_img)
                out.append(md_img)

    groups = [c for c in node.children if isinstance(c, _Node) and "infobox-group" in c.classes]
    if not groups:
        groups = [node]

    for grp in groups:
        heading_node = _find_first(grp, lambda n: "heading" in n.classes)
        h_text = ""
        if heading_node and grp is not node:
            h_text = _clean_inline_ws(_render_inline(heading_node, base_url=base_url, br_sep=" "))
            h_text = re.sub(r"^\*+|\*+$", "", h_text).strip()
            if h_text.lower() == "colors":
                continue

        rows = _find_all(grp, lambda n: "infobox-datarow" in n.classes)
        parsed_rows: list[tuple[str, str]] = []
        for row in rows:
            dh = _find_first(row, lambda n: "data-heading" in n.classes)
            dc = _find_first(row, lambda n: "data-content" in n.classes)
            ds = _find_first(row, lambda n: "data-subcontent" in n.classes)
            key = _clean_inline_ws(_render_inline(dh, base_url=base_url, br_sep=" ")) if dh else ""
            key = re.sub(r"^\*+|\*+$", "", key).strip()
            val = _clean_comma_joined(_render_inline(dc, base_url=base_url, br_sep=", ")) if dc else ""
            if ds:
                sub = _clean_comma_joined(_render_inline(ds, base_url=base_url, br_sep=", "))
                if sub:
                    val = f"{val} ({sub})" if val else sub
            if key or val:
                parsed_rows.append((key, val))

        if not parsed_rows:
            continue

        # Compact Fish "Value" and "Sizes" groups into dense single-line summaries
        h_lower = h_text.lower()
        row_dict = {k: v for k, v in parsed_rows if k}
        if h_lower == "value" and any(k in row_dict for k in ("Min. Kg", "Avg. Kg", "Base Kg", "Max. Kg")):
            if h_text:
                out.append(f"### {h_text}")
            val_lines: list[str] = []
            top_parts: list[str] = []
            if "XP" in row_dict:
                top_parts.append(f"**XP**: {row_dict['XP']}")
            if "C$/kg" in row_dict:
                top_parts.append(f"**C$/kg**: {row_dict['C$/kg']}")
            if top_parts:
                val_lines.append("- " + " | ".join(top_parts))

            tier_parts: list[str] = []
            for label in ("Min.", "Avg.", "Base", "Max."):
                kg_val = row_dict.get(f"{label} Kg")
                cs_val = row_dict.get(f"{label} C$")
                short_lbl = label.rstrip(".")
                if kg_val and cs_val:
                    tier_parts.append(f"{short_lbl}: {kg_val} ({cs_val})")
                elif kg_val:
                    tier_parts.append(f"{short_lbl}: {kg_val}")
            if tier_parts:
                val_lines.append("- **Weight & Value**: " + " | ".join(tier_parts))

            handled_keys = {
                "XP", "C$/kg",
                "Min. Kg", "Min. C$",
                "Avg. Kg", "Avg. C$",
                "Base Kg", "Base C$",
                "Max. Kg", "Max. C$",
            }
            for k, v in parsed_rows:
                if k not in handled_keys:
                    val_lines.append(f"- **{k}**: {v}" if k and v else f"- {v or k}")
            if val_lines:
                out.append("\n".join(val_lines))
            continue

        if h_lower == "sizes" and len(parsed_rows) >= 2:
            size_parts = [f"{k} {v}" if k and v else (v or k) for k, v in parsed_rows]
            out.append("- **Sizes**: " + " | ".join(size_parts))
            continue

        if h_text:
            out.append(f"### {h_text}")
        row_lines = [
            f"- **{k}**: {v}" if k and v else f"- {v or k}"
            for k, v in parsed_rows
        ]
        out.append("\n".join(row_lines))

    return "\n\n".join(out)


def _render_gallery(node: _Node, base_url: str = FISCHIPEDIA_BASE_URL) -> str:
    items: list[str] = []
    for box in _find_all(node, lambda n: "gallerybox" in n.classes):
        img = _find_first(box, lambda n: n.tag == "img")
        gtext = _find_first(box, lambda n: "gallerytext" in n.classes)
        md_img = _render_img(img, base_url) if img else ""
        caption = _clean_inline_ws(_render_inline(gtext, base_url=base_url, br_sep=" ")) if gtext else ""
        if md_img and caption:
            items.append(f"- {md_img} — {caption}")
        elif md_img:
            items.append(f"- {md_img}")
        elif caption:
            items.append(f"- {caption}")
    return "\n".join(items)


def _collect_table_rows(table: _Node) -> tuple[_Node | None, list[_Node]]:
    caption: _Node | None = None
    rows: list[_Node] = []
    for child in table.children:
        if not isinstance(child, _Node):
            continue
        if child.tag == "caption" and caption is None:
            caption = child
        elif child.tag == "tr":
            rows.append(child)
        elif child.tag in {"thead", "tbody", "tfoot"}:
            for sub in child.children:
                if isinstance(sub, _Node) and sub.tag == "tr":
                    rows.append(sub)
    return caption, rows


def _render_table(node: _Node, base_url: str = FISCHIPEDIA_BASE_URL) -> str:
    caption_node, rows = _collect_table_rows(node)
    caption_text = ""
    if caption_node:
        caption_text = _clean_inline_ws(_render_inline(caption_node, base_url=base_url, br_sep=" "))
        caption_text = re.sub(r"^\*+|\*+$", "", caption_text).strip()

    if not rows:
        return f"**{caption_text}**" if caption_text else ""

    all_cells = [
        [c for c in r.children if isinstance(c, _Node) and c.tag in {"th", "td"}]
        for r in rows
    ]
    all_cells = [r for r in all_cells if r]
    if not all_cells:
        return ""

    if len(all_cells) == 1 and len(all_cells[0]) == 1:
        inner_md = _render_blocks(all_cells[0][0], base_url=base_url)
        if caption_text:
            return f"**{caption_text}**\n\n{inner_md}"
        return inner_md

    header_rows: list[list[_Node]] = []
    data_rows: list[list[_Node]] = []
    in_headers = True
    for r in all_cells:
        if in_headers and all(c.tag == "th" for c in r):
            header_rows.append(r)
        else:
            in_headers = False
            data_rows.append(r)

    if not header_rows:
        header_row = data_rows[0]
        data_rows = data_rows[1:]
    else:
        header_row = max(header_rows, key=len)

    def _format_cell(cell: _Node) -> str:
        raw = _render_inline(cell, base_url=base_url, br_sep=" <BR> ")
        cleaned = re.sub(r"[ \t\xa0\r\n]+", " ", raw).strip()
        parts = [p.strip() for p in cleaned.split("<BR>") if p.strip()]
        return " / ".join(parts).replace("|", r"\|")

    headers = [_format_cell(c) for c in header_row]
    col_count = len(headers)
    if col_count == 0:
        return ""

    out_lines: list[str] = []
    if caption_text:
        out_lines.append(f"**{caption_text}**")
        out_lines.append("")

    out_lines.append("| " + " | ".join(headers) + " |")
    out_lines.append("| " + " | ".join("---" for _ in headers) + " |")

    for r in data_rows:
        cells = [_format_cell(c) for c in r]
        if len(cells) < col_count:
            cells.extend([""] * (col_count - len(cells)))
        elif len(cells) > col_count:
            cells = cells[:col_count]
        out_lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(out_lines)


def _iter_list_items(container: _Node) -> list[_Node]:
    """
    Yield `<li>` elements from a list container (`<ul>`, `<ol>`, or `.rod-mastery-reward-note`),
    unwrapping intermediate inline formatting tags (`<i>`, `<b>`, `<span>`) that MediaWiki
    sometimes wraps around `<li>` elements.
    """
    items: list[_Node] = []
    for child in container.children:
        if not isinstance(child, _Node):
            continue
        if child.tag == "li":
            items.append(child)
        elif child.tag in {"i", "em", "b", "strong", "span"}:
            items.extend(_iter_list_items(child))
    return items


def _split_li_children(li_node: _Node) -> tuple[list[_Node | str], list[_Node]]:
    """
    Split an `<li>` node's children into `(inline_parts, sublists)`, even when a nested
    `<ul>`/`<ol>` is wrapped inside an `<i>` or `<b>` tag.
    """
    inline_parts: list[_Node | str] = []
    sublists: list[_Node] = []
    for sub in li_node.children:
        if isinstance(sub, _Node):
            if sub.tag in {"ul", "ol"}:
                sublists.append(sub)
            elif sub.tag in {"i", "em", "b", "strong", "span"} and any(
                isinstance(gc, _Node) and gc.tag in {"ul", "ol"} for gc in sub.children
            ):
                rem_inline: list[_Node | str] = []
                for gc in sub.children:
                    if isinstance(gc, _Node) and gc.tag in {"ul", "ol"}:
                        sublists.append(gc)
                    else:
                        rem_inline.append(gc)
                if rem_inline:
                    inline_parts.append(_Node(sub.tag, attrs=sub.attrs, children=rem_inline))
            else:
                inline_parts.append(sub)
        else:
            inline_parts.append(sub)
    return inline_parts, sublists


def _render_list(node: _Node, *, base_url: str = FISCHIPEDIA_BASE_URL, indent: int = 0) -> str:
    if "gallery" in node.classes:
        return _render_gallery(node, base_url)

    lines: list[str] = []
    ordered = node.tag == "ol"
    idx = 1
    prefix_pad = "  " * indent

    for child in _iter_list_items(node):
        bullet = f"{idx}." if ordered else "-"
        idx += 1

        inline_parts, sublists = _split_li_children(child)

        temp_node = _Node("span", children=inline_parts)
        item_text = _clean_inline_ws(_render_inline(temp_node, base_url=base_url, br_sep=" — "))
        if item_text:
            lines.append(f"{prefix_pad}{bullet} {item_text}")
        for sl in sublists:
            sub_md = _render_list(sl, base_url=base_url, indent=indent + 1)
            if sub_md:
                lines.append(sub_md)

    return "\n".join(lines)


def _render_dl(node: _Node, *, base_url: str = FISCHIPEDIA_BASE_URL, indent: int = 0) -> str:
    """Render MediaWiki `<dl>/<dt>/<dd>` definition/dialogue trees with indentation."""
    lines: list[str] = []
    pad = "  " * indent
    for child in node.children:
        if not isinstance(child, _Node) or child.tag not in {"dt", "dd"}:
            continue
        inline_parts: list[_Node | str] = []
        sub_dls: list[_Node] = []
        for sub in child.children:
            if isinstance(sub, _Node) and sub.tag == "dl":
                sub_dls.append(sub)
            else:
                inline_parts.append(sub)
        temp = _Node("span", children=inline_parts)
        txt = _clean_inline_ws(_render_inline(temp, base_url=base_url, br_sep=" "))
        if txt:
            if child.tag == "dt" and not txt.startswith("**"):
                lines.append(f"{pad}**{txt}**")
            else:
                lines.append(f"{pad}{txt}")
        for sdl in sub_dls:
            sub_md = _render_dl(sdl, base_url=base_url, indent=indent + 1)
            if sub_md:
                lines.append(sub_md)
    return "\n".join(lines)


def _render_rod_mastery_container(node: _Node, *, base_url: str = FISCHIPEDIA_BASE_URL) -> str:
    """Render `<div class="rod-mastery-container">` directly from DOM without losing sub-notes."""
    lines: list[str] = []
    challenges = [
        c for c in node.children if isinstance(c, _Node) and "rod-mastery-challenge" in c.classes
    ]
    for ch in challenges:
        hdr_node = _find_first(ch, lambda n: "rod-mastery-header" in n.classes)
        desc_node = _find_first(ch, lambda n: "rod-mastery-description" in n.classes)
        rew_node = _find_first(ch, lambda n: "rod-mastery-reward" in n.classes)
        note_node = _find_first(ch, lambda n: "rod-mastery-reward-note" in n.classes)

        hdr = _clean_inline_ws(_render_inline(hdr_node, base_url=base_url, br_sep=" ")) if hdr_node else ""
        hdr = re.sub(r"^\*+|\*+$", "", hdr).strip()
        desc = _clean_inline_ws(_render_inline(desc_node, base_url=base_url, br_sep=" ")) if desc_node else ""
        rew = _clean_inline_ws(_render_inline(rew_node, base_url=base_url, br_sep=" ")) if rew_node else ""
        rew = re.sub(r"^Reward:\s*", "", rew, flags=re.IGNORECASE).strip()

        if "rod-mastery-grand" in ch.classes or hdr.upper() == "GRAND REWARD":
            if rew:
                lines.append(f"- **Grand Reward**: {rew}")
            continue

        if hdr and desc and rew:
            lines.append(f"- **{hdr}**: {desc} → **Reward**: {rew}")
        elif hdr and rew:
            lines.append(f"- **{hdr}** → **Reward**: {rew}")
        elif hdr and desc:
            lines.append(f"- **{hdr}**: {desc}")

        if note_node:
            # Note node may contain `<ul>` children or bare `<li>` inside `<i>`
            note_uls = [c for c in note_node.children if isinstance(c, _Node) and c.tag in {"ul", "ol"}]
            if note_uls and not _iter_list_items(note_node):
                for ul in note_uls:
                    ul_md = _render_list(ul, base_url=base_url, indent=1)
                    if ul_md:
                        lines.append(ul_md)
            elif _iter_list_items(note_node):
                note_md = _render_list(note_node, base_url=base_url, indent=1)
                if note_md:
                    lines.append(note_md)
            else:
                note_txt = _clean_inline_ws(_render_inline(note_node, base_url=base_url, br_sep=" "))
                if note_txt:
                    lines.append(f"  - {note_txt}")

    return "\n".join(lines)


def _render_blocks(node: _Node, *, base_url: str = FISCHIPEDIA_BASE_URL) -> str:
    blocks: list[str] = []
    inline_buf: list[_Node | str] = []

    def _flush_inline() -> None:
        nonlocal inline_buf
        if not inline_buf:
            return
        temp = _Node("span", children=inline_buf)
        inline_buf = []
        txt = _clean_inline_ws(_render_inline(temp, base_url=base_url))
        if txt:
            blocks.append(txt)

    for child in node.children:
        if isinstance(child, str):
            inline_buf.append(child)
            continue

        tag = child.tag
        classes = child.classes

        if "mbox" in classes:
            _flush_inline()
            hdr_node = _find_first(child, lambda n: "mbox__content__header" in n.classes)
            if hdr_node:
                hdr_txt = _clean_inline_ws(_render_inline(hdr_node, base_url=base_url, br_sep=" "))
                if hdr_txt:
                    blocks.append(f"> **Note**: {hdr_txt}")
            continue

        if "infobox" in classes:
            _flush_inline()
            ib = _render_infobox(child, base_url)
            if ib:
                blocks.append(ib)
            continue

        if "rod-mastery-container" in classes:
            _flush_inline()
            rm = _render_rod_mastery_container(child, base_url=base_url)
            if rm:
                blocks.append(rm)
            continue

        if "mw-heading" in classes or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            _flush_inline()
            h_node = child if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} else _find_first(
                child, lambda n: n.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
            )
            if h_node:
                level = int(h_node.tag[1])
                h_text = _clean_inline_ws(_render_inline(h_node, base_url=base_url, br_sep=" "))
                if h_text.lower() == "navigation":
                    break
                blocks.append(f"{'#' * level} {h_text}")
            continue

        if "bestiary-description" in classes and "quest" in classes:
            _flush_inline()
            t_node = _find_first(child, lambda n: "bestiary-description-title" in n.classes)
            c_node = _find_first(child, lambda n: "bestiary-description-content" in n.classes)
            q_title = _clean_inline_ws(_render_inline(t_node, base_url=base_url, br_sep=" ")) if t_node else ""
            q_title = re.sub(r"^\*+|\*+$", "", q_title).strip()
            q_body = _render_blocks(c_node, base_url=base_url) if c_node else ""
            if q_title and q_body:
                blocks.append(f"### {q_title}\n\n{q_body}")
            elif q_body:
                blocks.append(q_body)
            continue

        if "bestiary-description" in classes and not (classes & {"ability", "enchanting", "quest"}):
            _flush_inline()
            c_node = _find_first(child, lambda n: "bestiary-description-content" in n.classes)
            s_node = _find_first(child, lambda n: "bestiary-description-source" in n.classes)
            quote_txt = _clean_inline_ws(_render_inline(c_node, base_url=base_url, br_sep=" ")) if c_node else ""
            src_txt = _clean_inline_ws(_render_inline(s_node, base_url=base_url, br_sep=" ")) if s_node else ""
            if quote_txt and src_txt:
                blocks.append(f'> *"{quote_txt}"* — {src_txt}')
            elif quote_txt:
                blocks.append(f'> *"{quote_txt}"*')
            continue

        if tag == "article" and "tabber__panel" in classes:
            _flush_inline()
            panel_id = child.id
            tab_title = child.attrs.get("data-mw-tabber-title", "")
            if not tab_title and panel_id.startswith("tabber-"):
                tab_title = urllib.parse.unquote(panel_id[len("tabber-"):]).replace("_", " ")
            panel_body = _render_blocks(child, base_url=base_url)
            if tab_title and panel_body:
                blocks.append(f"### {tab_title}\n\n{panel_body}")
            elif panel_body:
                blocks.append(panel_body)
            continue

        if tag == "table":
            _flush_inline()
            tbl = _render_table(child, base_url)
            if tbl:
                blocks.append(tbl)
            continue

        if tag in {"ul", "ol"}:
            _flush_inline()
            lst = _render_list(child, base_url=base_url)
            if lst:
                blocks.append(lst)
            continue

        if tag == "dl":
            _flush_inline()
            dl_md = _render_dl(child, base_url=base_url)
            if dl_md:
                blocks.append(dl_md)
            continue

        if tag == "p":
            _flush_inline()
            p_txt = _clean_inline_ws(_render_inline(child, base_url=base_url))
            if p_txt:
                blocks.append(p_txt)
            continue

        if tag == "hr":
            _flush_inline()
            continue

        if tag in {"div", "section", "article", "blockquote", "figure", "center"}:
            _flush_inline()
            sub = _render_blocks(child, base_url=base_url)
            if sub:
                blocks.append(sub)
            continue

        inline_buf.append(child)

    _flush_inline()
    return "\n\n".join(b for b in blocks if b.strip())


def convert_mw_html_to_markdown(
    html_text: str,
    *,
    title: str | None = None,
    base_url: str = FISCHIPEDIA_BASE_URL,
) -> str:
    """Convert MediaWiki `action=parse` HTML output into structured Markdown."""
    builder = _DOMBuilder()
    builder.feed(html_text)
    builder.close()
    body = _render_blocks(builder.root, base_url=base_url)
    if title:
        return f"# {title}\n\n{body}\n"
    return f"{body}\n"


def parse_fischipedia_page(
    url_or_page: str,
    *,
    base_url: str = FISCHIPEDIA_BASE_URL,
) -> tuple[str, str, str | None]:
    """
    Fetch and parse a MediaWiki page at zero Firecrawl credit cost via
    `w/api.php?action=parse&prop=text|displaytitle&disablelimitreport=1&disableeditsection=1&disabletoc=1&redirects=1`.

    Returns `(canonical_title, raw_markdown, redirect_tofragment_or_None)`.
    """
    raw_title = extract_title_from_url_or_page(url_or_page)
    parse_params: dict[str, Any] = {
        "action": "parse",
        "page": raw_title,
        "prop": "text|displaytitle",
        "disablelimitreport": 1,
        "disableeditsection": 1,
        "disabletoc": 1,
        "redirects": 1,
    }
    data = mw_api_request(parse_params)
    if not isinstance(data, dict) or "error" in data:
        resolved_title, _ = resolve_canonical_title(raw_title)
        if resolved_title != raw_title:
            parse_params["page"] = resolved_title
            data = mw_api_request(parse_params)

    if not isinstance(data, dict) or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError(
            f"MediaWiki parse failed for '{raw_title}': {err.get('info', 'Unknown error')}"
        )

    parse = data.get("parse", {})
    canonical_title = str(parse.get("title", raw_title))
    html_text = str(parse.get("text", {}).get("*", ""))

    redirect_tofragment: str | None = None
    redirects = parse.get("redirects", [])
    if isinstance(redirects, list) and redirects:
        last_redir = redirects[-1]
        if isinstance(last_redir, dict) and last_redir.get("tofragment"):
            frag = str(last_redir["tofragment"]).strip()
            if frag.startswith("tabber-"):
                frag = frag[len("tabber-") :]
            redirect_tofragment = frag.replace("_", " ") or None

    raw_md = convert_mw_html_to_markdown(
        html_text,
        title=canonical_title,
        base_url=base_url,
    )
    return canonical_title, raw_md, redirect_tofragment


# ---------------------------------------------------------------------------
# 3. Optional Firecrawl Scrape Fallback Configuration
# ---------------------------------------------------------------------------

FISCHIPEDIA_INCLUDE_TAGS: list[str] = [
    "#firstHeading",      # Main article <h1> title
    ".mw-parser-output",  # Main MediaWiki article content container
]

FISCHIPEDIA_EXCLUDE_TAGS: list[str] = [
    ".ext-floatingui-content",            # Hidden hover tooltips that fuse into stat text
    "#toc",                               # Table of Contents container
    ".toc",                               # Table of Contents class
    ".tabber__header",                    # Redundant Tabber tab-bar anchor links
    ".navbox",                            # Bottom-of-page navigation boxes
    ".navbox-border",                     # Outer navbox table wrapper
    ".fish-navbox",                       # Fish bestiary navboxes
    ".mw-editsection",                    # Section [edit] links
    ".change-history-header .plainlinks", # [Edit History] link in Change History
    ".item-icon",                         # Tiny 20px duplicate inline item icons
    ".bestiary-description-source img",   # Tiny bestiary icon next to "Bestiary Entry"
    ".ambox",                             # Article message/stub/maintenance banners
    ".mbox",                              # General MediaWiki message boxes
    ".catlinks",                          # Category links block
    ".mw-cookiewarning-container",        # Miraheze cookie banner
]


def split_target_and_fragment(
    url_or_page: str,
    *,
    base_url: str = FISCHIPEDIA_BASE_URL,
) -> tuple[str, str | None]:
    """
    Split a page name or URL into `(canonical_url_without_fragment, decoded_fragment)`.
    Example:
      - `"Enchanting#Empyrean"` -> `("https://fischipedia.org/wiki/Enchanting", "Empyrean")`
      - `"No-Life Rod"` -> `("https://fischipedia.org/wiki/No-Life_Rod", None)`
    """
    target = url_or_page.strip()
    fragment: str | None = None
    if "#" in target:
        target, raw_frag = target.split("#", 1)
        fragment = urllib.parse.unquote(raw_frag.strip()).replace("_", " ") or None

    if target.startswith(("http://", "https://")):
        url = target
    else:
        slug = urllib.parse.quote(target.replace(" ", "_"), safe="_-():'/")
        url = f"{base_url.rstrip('/')}/wiki/{slug}"
    return url, fragment


def resolve_fischipedia_url(url_or_page: str) -> str:
    """Return the canonical wiki URL (without fragment)."""
    url, _ = split_target_and_fragment(url_or_page)
    return url


def get_firecrawl_scrape_params(*, max_age: int = 86400000) -> dict[str, Any]:
    """
    Return the recommended parameters for `client.scrape(url, **params)`
    in `firecrawl-py` (v2 SDK).
    """
    return {
        "formats": ["markdown"],
        "only_main_content": False,
        "include_tags": FISCHIPEDIA_INCLUDE_TAGS,
        "exclude_tags": FISCHIPEDIA_EXCLUDE_TAGS,
        "remove_base64_images": True,
        "max_age": max_age,
    }


def scrape_fischipedia_url(
    url_or_page: str,
    *,
    api_key: str | None = None,
    api_url: str | None = None,
    max_age: int = 86400000,
) -> str:
    """
    Optional Firecrawl fallback scraper for a single Fischipedia URL (or page title).
    Note: By default, `scrape_and_clean_fischipedia` uses `parse_fischipedia_page`
    (zero-credit MediaWiki `w/api.php?action=parse`), so Firecrawl is not required.
    """
    url = resolve_fischipedia_url(url_or_page)
    resolved_key = api_key or os.environ.get("FIRECRAWL_API_KEY")
    firecrawl_bin = shutil.which("firecrawl")

    if firecrawl_bin:
        cmd = [
            firecrawl_bin,
            "scrape",
            url,
            "--include-tags",
            ",".join(FISCHIPEDIA_INCLUDE_TAGS),
            "--exclude-tags",
            ",".join(FISCHIPEDIA_EXCLUDE_TAGS),
            "--max-age",
            str(max_age),
        ]
        if resolved_key:
            cmd.extend(["--api-key", resolved_key])
        if api_url:
            cmd.extend(["--api-url", api_url])

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        if not resolved_key:
            err_msg = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            raise RuntimeError(f"firecrawl CLI scrape failed for {url}: {err_msg}")

    if not resolved_key:
        raise RuntimeError(
            "Neither authenticated `firecrawl` CLI nor FIRECRAWL_API_KEY was found. "
            "Run `firecrawl login` or set FIRECRAWL_API_KEY."
        )

    resolved_base_url = (
        api_url or os.environ.get("FIRECRAWL_API_URL") or "https://api.firecrawl.dev"
    ).rstrip("/")
    params = get_firecrawl_scrape_params(max_age=max_age)

    try:
        from firecrawl import Firecrawl  # type: ignore

        client = Firecrawl(api_key=resolved_key, api_url=resolved_base_url)
        doc = client.scrape(url, **params)
        markdown = getattr(doc, "markdown", None)
        if markdown is None and isinstance(doc, dict):
            markdown = doc.get("markdown") or doc.get("data", {}).get("markdown")
        if not markdown:
            raise RuntimeError(f"Firecrawl returned empty markdown for {url}")
        return str(markdown)
    except ImportError:
        pass

    payload = {
        "url": url,
        "formats": params["formats"],
        "onlyMainContent": params["only_main_content"],
        "includeTags": params["include_tags"],
        "excludeTags": params["exclude_tags"],
        "removeBase64Images": params["remove_base64_images"],
        "maxAge": params["max_age"],
    }
    req = urllib.request.Request(
        f"{resolved_base_url}/v2/scrape",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
            "User-Agent": "FischipediaCleaner/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Firecrawl API error ({exc.code}): {err_text}") from exc

    data = body.get("data", body)
    markdown = data.get("markdown")
    if not markdown:
        raise RuntimeError(f"Firecrawl REST response missing markdown for {url}: {body}")
    return str(markdown)


# ---------------------------------------------------------------------------
# 4. Markdown Post-Processing Pipeline
# ---------------------------------------------------------------------------

_TOOLTIP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(Favoring)This fish's chance increases with luck\."),
        r"\1",
    ),
    (
        re.compile(r"(Unfavoring)This fish's chance does not increase with luck\."),
        r"\1",
    ),
    (
        re.compile(
            r"(\(Locked\)|_\(Locked\)_)This Progress Speed modifier cannot be changed by most modifiers\."
        ),
        r" \1",
    ),
    (
        re.compile(
            r"The lowest possible weight for this fish without external buffs\."
        ),
        "",
    ),
    (
        re.compile(r"The sell value for this fish at its minimum weight\."),
        "",
    ),
    (
        re.compile(r"The true average weight for this fish\."),
        "",
    ),
    (
        re.compile(r"The sell value for this fish at its true average weight\."),
        "",
    ),
    (
        re.compile(
            r'The upper limit of what the game considers the "Average" weight for this fish\.'
        ),
        "",
    ),
    (
        re.compile(r"The sell value for this fish at its base weight\."),
        "",
    ),
    (
        re.compile(
            r"The maximum possible weight for this fish WITHOUT any external weight boosts\."
        ),
        "",
    ),
    (
        re.compile(r"The sell value for this fish at its maximum natural weight\."),
        "",
    ),
]

_INFOBOX_GROUPS = {
    "Information",
    "Preferences",
    "Value",
    "Sizes",
    "Stats",
    "Colors",
    "Obtainability",
    "Details",
    "General",
    "Properties",
    "Requirements",
    "Passive",
}

_HEADERLESS_INFOBOX_KEYS = {
    "Type",
    "Source",
    "Value",
    "Appraisable",
    "Rarity",
    "Category",
    "Event",
    "Price",
    "Cost",
    "Stage",
    "Location",
    "Sublocation",
    "NPC",
    "Quest",
    "Multiplier",
}


def is_disambiguation_markdown(text: str) -> bool:
    """Return True if the page is a Disambiguation page."""
    head = text[:800]
    if re.search(r"^# [^\n]*\(Disambiguation\)", head, flags=re.MULTILINE | re.IGNORECASE):
        return True
    if re.search(r"\b(?:may(?: also)? refer to|most commonly refers to):", head, flags=re.IGNORECASE):
        return True
    return False


def find_disambiguation_hatnote_url(raw_markdown: str) -> str | None:
    """
    If the top of the article has a hatnote linking to a `_(Disambiguation)` page,
    return that disambiguation URL.
    """
    head = raw_markdown[:1000]
    m = re.search(
        r"\((https?://[^/]+/wiki/[^)\s\"]+_\(Disambiguation\))(?:\s+\"[^\"]*\")?\)",
        head,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return None


def _strip_raw_wiki_chrome(text: str) -> str:
    """
    Remove top/bottom MediaWiki skin chrome and interactive filter widget text.
    """
    h1_match = re.search(r"^# [^\n]+", text, flags=re.MULTILINE)
    if h1_match and ("### Not logged in" in text[: h1_match.start()] or "### Search" in text[: h1_match.start()]):
        text = text[h1_match.start() :]

    text = re.sub(
        r"^From Official Fisch Wiki\s*\n+"
        r"(?:### (?:Namespaces|More|Page actions)\s*\n+(?:- [^\n]+\n+)+)*",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = re.sub(
        r"^## Contents\s*\n+(?:- \[\d+[^\n]+\n+)+",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = re.sub(
        r"^## Navigation\b[\s\S]*$",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = re.sub(
        r'^Retrieved from "\s*\[?https?://[^\s"]+[\s\S]*$',
        "",
        text,
        flags=re.MULTILINE,
    )

    # Strip interactive table filter widgets (e.g. "Reset all\n\nStage:\n\nAll\n..." before "Showing X of Y")
    text = re.sub(
        r"^Reset all\s*\n[\s\S]*?(?=^Showing \d+ of \d+|^\|)",
        "",
        text,
        flags=re.MULTILINE,
    )

    # Unwrap outer collapsible table header wrapper rows (e.g. "| Rod | CollapseStats |\n| --- | --- |\n" before real header)
    text = re.sub(
        r"^\|[^\n]*Collapse[^\n]*\|\s*\n\|(?:\s*---+\s*\|)+\s*\n(?=\|[^\n]+\|\s*\n\|(?:\s*---+\s*\|)+)",
        "",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    return text


def _clean_tooltips_and_ui_tokens(text: str) -> str:
    """Remove fused FloatingUI tooltips, Tabber tab links, and [Edit History] links."""
    for pattern, replacement in _TOOLTIP_PATTERNS:
        text = pattern.sub(replacement, text)

    text = re.sub(
        r"^(?:\[[^\]]+\]\([^)]*#tabber-[^)]+\)\s*)+\n+",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = re.sub(
        r"^\\?\[\s*\[Edit History\]\([^)]+\)\s*\\?\]\s*\n*",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = re.sub(r"(\S)_(\([^)_]+\))_", r"\1 _\2_", text)

    return text


def _unwrap_single_cell_tables(text: str) -> str:
    """
    Unwrap 1-column MediaWiki layout tables (such as collapsible Skin galleries)
    where Firecrawl stuffs an entire bullet list into a single table cell with `<br>`.
    """
    single_cell_pattern = re.compile(
        r"^\|\s*\|\s*\n"
        r"\|\s*---\s*\|\s*\n"
        r"\|\s*(.*?)\s*\|\s*$"
        r"(?:\n+[^\n]+\n+Expand\b|\n+Expand\b|\n+Collapse\b)?",
        flags=re.MULTILINE,
    )

    def _replace_cell(match: re.Match[str]) -> str:
        cell = match.group(1)
        cell = re.sub(r"(?:<br\s*/?>\s*)+", "\n", cell, flags=re.IGNORECASE)
        lines = [line.strip() for line in cell.splitlines() if line.strip()]

        compacted: list[str] = []
        for line in lines:
            if line.startswith("- "):
                compacted.append(line)
            elif compacted:
                compacted[-1] = f"{compacted[-1]} — {line}"
            else:
                compacted.append(f"- {line}")
        return "\n".join(compacted)

    return single_cell_pattern.sub(_replace_cell, text)


def _strip_decorative_icons(text: str, *, include_images: bool = False) -> str:
    """
    Strip inline icons and, by default (`include_images=False`), all `static.wikitide.net`
    image/media embeds while keeping text labels and converting Robux icons to `"Robux"`.
    """
    text = re.sub(
        r"Robux\s*!\[[^\]]*Robux[^\]]*\]\([^)]+\)\s*(\d+(?:,\d+)*)",
        r"\1 Robux",
        text,
        flags=re.IGNORECASE,
    )

    if not include_images:
        # Remove linked images [![alt](img)](href)
        text = re.sub(
            r"\[!\[[^\]]*\]\((?:[^)\s]|\([^)\s]*\))+\)\]\((?:[^)\s]|\([^)\s]*\))+\)\s*",
            "",
            text,
        )
        # Remove standalone images ![alt](img)
        text = re.sub(
            r"!\[[^\]]*\]\((?:[^)\s]|\([^)\s]*\))+\)\s*(?:—\s*)?",
            "",
            text,
        )
        return text

    text = re.sub(
        r"\[!\[[^\]]*\]\(https?://static\.wikitide\.net/[^)]+/(?:[12]\d|[1-9]|3[0-2])px-[^)]+\)\]\([^)]+\)",
        "",
        text,
    )

    text = re.sub(
        r"!\[[^\]]*\]\(https?://static\.wikitide\.net/[^)]+/(?:[12]\d|[1-9]|3[0-2])px-[^)]+\)\s*",
        "",
        text,
    )

    text = re.sub(
        r"!\[[^\]]*\]\(https?://static\.wikitide\.net/[^)]+(?:Control_Minigame_|Bestiary_Description_Icon|Enchanting\.png)[^)]*\)\s*",
        "",
        text,
    )

    return text


def _clean_links_and_escapes(
    text: str,
    *,
    strip_wiki_links: bool = True,
    include_images: bool = False,
) -> str:
    """
    Simplify internal `/wiki/` links and unescape Markdown characters.
    """
    text = re.sub(r'(\(https?://(?:[^)\s"]|\([^)\s"]*\))+)\s+"[^"]*"\)', r"\1)", text)

    base_host_re = re.escape(FISCHIPEDIA_BASE_URL)
    wiki_url_pat = rf"(?:{base_host_re}|https?://fischipedia\.org)/wiki/(?:[^)\s]|\([^)\s]*\))+"

    if is_disambiguation_markdown(text):
        def _clean_disambig_line(line: str) -> str:
            if not line.lstrip().startswith("- ["):
                return line
            first_link_match = re.match(
                rf"^(\s*-\s*)\[([^\[\]]+)\]\(({wiki_url_pat})\)(.*)$",
                line,
            )
            if not first_link_match:
                return line
            indent_dash, label, url, rest = (
                first_link_match.group(1),
                first_link_match.group(2),
                first_link_match.group(3),
                first_link_match.group(4),
            )
            rest = re.sub(
                rf"(?<!!)\[([^\[\]]+)\]\({wiki_url_pat}\)",
                r"\1",
                rest,
            )
            if strip_wiki_links:
                page_title = extract_title_from_url_or_page(url)
                if page_title.lower() != label.strip().lower():
                    return f"{indent_dash}**{label}** (`{page_title}`){rest}"
                return f"{indent_dash}**{label}**{rest}"
            return f"{indent_dash}[{label}]({url}){rest}"

        lines = [_clean_disambig_line(ln) for ln in text.splitlines()]
        text = "\n".join(lines)
        text = text.replace(r"\>", ">").replace(r"\+", "+").replace(r"\[", "[").replace(r"\]", "]")
        return text

    if strip_wiki_links:
        text = re.sub(
            rf"(\[!\[[^\]]*\]\([^)]+\))\s*\]\((?:{base_host_re}|https?://fischipedia\.org)/wiki/File:(?:[^)\s]|\([^)\s]*\))+\)",
            lambda m: m.group(1)[1:],
            text,
        )

        # Ensure space before [C$](...) when preceded by a digit (e.g. 0.47[C$](...) -> 0.47 C$)
        text = re.sub(r"(\d)(\[C\$\]\()", r"\1 \2", text)

        def _unwrap_wiki_link(match: re.Match[str]) -> str:
            label, url = match.group(1), match.group(2)
            if re.search(r"\.(?:mp4|mp3|ogg|wav|webm)(?:\?|$)", url, re.IGNORECASE):
                return match.group(0) if include_images else ""
            if "_(Disambiguation)" in url:
                return match.group(0)
            return label

        text = re.sub(
            rf"(?<!!)\[([^\[\]]+)\]\(({wiki_url_pat})\)",
            _unwrap_wiki_link,
            text,
        )
        # Also unwrap external non-media Markdown links (e.g. fandom wiki links in Trivia)
        text = re.sub(
            r"(?<!!)\[([^\[\]]+)\]\(https?://(?:[^)\s]|\([^)\s]*\))+\)",
            r"\1",
            text,
        )

    if not include_images:
        # Strip standalone media/sound file lines or trailing sound links
        text = re.sub(
            r"^\s*\[?(?:Media:|https?://[^\s\]]+/File:)[^\n]*\.(?:mp4|mp3|ogg|wav|webm)[^\n]*$\n?",
            "",
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        text = re.sub(
            r"^\s*-\s*The [^\n]+ plays a sound when activated:\s*$\n?",
            "",
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )

    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"([A-Za-z0-9%\])])_(\([^_\n]+\))_", r"\1 _\2_", text)
    text = text.replace(r"\>", ">").replace(r"\+", "+").replace(r"\[", "[").replace(r"\]", "]")

    return text


def _compact_infobox_section(text: str) -> str:
    """
    Transform a Firecrawl vertical Infobox dump at the top of the page
    into compact `- **Key**: Value` Markdown bullet lists under each group header.
    """
    if is_disambiguation_markdown(text):
        return text

    h2_match = re.search(r"^##\s+", text, flags=re.MULTILINE)
    if h2_match:
        preamble = text[: h2_match.start()]
        body = text[h2_match.start() :]
    else:
        preamble = text
        body = ""

    blocks = [b.strip() for b in re.split(r"\n{2,}", preamble) if b.strip()]
    if not blocks:
        return text

    page_title = ""
    if blocks[0].startswith("# "):
        page_title = blocks[0][2:].strip()

    out_blocks: list[str] = []
    in_infobox = False
    pending_key: str | None = None
    current_group_items: list[str] = []

    def _flush_group() -> None:
        nonlocal pending_key, current_group_items
        if pending_key is not None:
            current_group_items.append(f"- {pending_key}")
            pending_key = None
        if current_group_items:
            out_blocks.append("\n".join(current_group_items))
            current_group_items = []

    for block in blocks:
        visible_block = re.sub(
            r"\[([^\[\]]+)\]\((?:[^)\s]|\([^)\s]*\))+\)", r"\1", block
        ).strip()

        if page_title and visible_block == f"**{page_title}**":
            continue

        m_group = re.match(r"^\*\*([^*]+)\*\*$", visible_block)
        if m_group and m_group.group(1).strip() in _INFOBOX_GROUPS:
            _flush_group()
            in_infobox = True
            out_blocks.append(f"### {m_group.group(1).strip()}")
            continue

        if not in_infobox and visible_block in _HEADERLESS_INFOBOX_KEYS and not out_blocks[1:]:
            in_infobox = True
            out_blocks.append("### Information")
            pending_key = block
            continue
        if not in_infobox and visible_block in _HEADERLESS_INFOBOX_KEYS and all(
            not b.startswith("### ") for b in out_blocks
        ):
            in_infobox = True
            out_blocks.append("### Information")
            pending_key = block
            continue

        if in_infobox:
            is_lead_sentence = (
                len(visible_block) > 45
                or (visible_block.endswith(".") and len(visible_block) > 25)
                or bool(re.search(r"\b(?:is a|is an|is the|are|was|refers to)\b", visible_block))
            )
            if is_lead_sentence and pending_key is None:
                _flush_group()
                in_infobox = False
                out_blocks.append(block)
                continue

            if pending_key is None and current_group_items and (
                visible_block.startswith("in ")
                or (
                    re.match(r"^#[0-9a-fA-F]{6}$", visible_block)
                    and re.search(r"#[0-9a-fA-F]{6}$", current_group_items[-1])
                )
            ):
                if visible_block.startswith("in "):
                    current_group_items[-1] = f"{current_group_items[-1]} ({block})"
                else:
                    current_group_items[-1] = f"{current_group_items[-1]}, {block}"
                continue

            if pending_key is None:
                pending_key = block
            else:
                clean_val = block[2:].strip() if block.startswith("- ") and "\n" not in block else block
                current_group_items.append(f"- **{pending_key}**: {clean_val}")
                pending_key = None
        else:
            out_blocks.append(block)

    _flush_group()
    cleaned_preamble = "\n\n".join(out_blocks)
    return f"{cleaned_preamble}\n\n{body}" if body else cleaned_preamble


def strip_sections_by_heading(markdown: str, exclude_sections: list[str]) -> str:
    """
    Remove specific top-level or sub-sections (e.g. `["Change History", "Skins", "Gallery"]`)
    from cleaned Markdown to save context tokens.
    """
    normalized_excludes = {s.strip().lower().replace("_", " ") for s in exclude_sections if s.strip()}
    if not normalized_excludes:
        return markdown

    lines = markdown.splitlines()
    out_lines: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        m_head = re.match(r"^(#{2,6})\s+(.*)$", line)
        if m_head:
            level = len(m_head.group(1))
            heading_text = re.sub(r"\[([^\[\]]+)\]\([^)]+\)", r"\1", m_head.group(2)).strip().lower().replace("_", " ")
            if heading_text in normalized_excludes:
                i += 1
                while i < n:
                    m_next = re.match(r"^(#{2,6})\s+", lines[i])
                    if m_next and len(m_next.group(1)) <= level:
                        break
                    i += 1
                continue
        out_lines.append(line)
        i += 1

    return re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip() + "\n"


def extract_fragment_content(cleaned_markdown: str, fragment: str) -> str:
    """
    Filter a large cleaned page (such as `Enchanting` or `Fishing Rods`)
    down to only the sections, table rows (with table headers preserved), and bullet items
    matching `#fragment` (e.g. `Enchanting#Empyrean`, `Fishing Rods#Stage_8`).
    """
    term = urllib.parse.unquote(fragment.strip())
    if not term:
        return cleaned_markdown

    # Match spaces and underscores interchangeably (e.g. #Stage_8 matches "Stage 8")
    flex_regex = re.sub(r"(?:\\_|\\\s)+", r"[\\s_]+", re.escape(term))
    pattern = re.compile(flex_regex, re.IGNORECASE)

    # Also parse optional Column/Value pairs like "Stage_8", "Stage 8", "Stage=8", "Stage:8"
    col_val_match = re.match(r"^([A-Za-z][A-Za-z0-9\s]*?)[\s_:=]+([^\s_:=].*)$", term)
    target_col_name = col_val_match.group(1).strip().lower() if col_val_match else None
    target_col_val = col_val_match.group(2).strip().lower() if col_val_match else None

    lines = cleaned_markdown.splitlines()
    out_lines: list[str] = []

    for line in lines[:5]:
        if line.startswith("# "):
            out_lines.append(f"{line} (Filtered for `#{term}`)")
            out_lines.append("")
            break

    i = 0
    n = len(lines)
    matched_sections: list[str] = []
    matched_line_indices: set[int] = set()
    while i < n:
        line = lines[i]
        m_head = re.match(r"^(#{2,6})\s+(.*)$", line)
        if m_head and pattern.search(m_head.group(2)):
            level = len(m_head.group(1))
            sec_lines = [line]
            matched_line_indices.add(i)
            i += 1
            while i < n:
                m_next = re.match(r"^(#{2,6})\s+", lines[i])
                if m_next and len(m_next.group(1)) <= level:
                    break
                sec_lines.append(lines[i])
                matched_line_indices.add(i)
                i += 1
            matched_sections.append("\n".join(sec_lines).strip())
        else:
            i += 1

    if matched_sections:
        out_lines.extend(matched_sections)
        out_lines.append("")

    current_heading = ""
    used_headings: set[str] = set()
    i = 0
    while i < n:
        if i in matched_line_indices:
            i += 1
            continue
        line = lines[i]
        if re.match(r"^#{2,6}\s+", line):
            current_heading = line
            i += 1
            continue

        if line.startswith("|"):
            table_lines: list[str] = []
            while i < n and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1
            # Unwrap outer collapsible wrapper header if present
            if (
                len(table_lines) >= 4
                and "---" in table_lines[1]
                and "---" in table_lines[3]
            ):
                table_lines = table_lines[2:]

            if len(table_lines) >= 2 and "---" in table_lines[1]:
                header_rows = table_lines[:2]
                header_cells = [
                    c.strip().lower()
                    for c in header_rows[0].strip().strip("|").split("|")
                ]
                matched_col_idx: int | None = None
                if target_col_name and target_col_name in header_cells:
                    matched_col_idx = header_cells.index(target_col_name)

                data_rows: list[str] = []
                for row in table_lines[2:]:
                    if pattern.search(row):
                        data_rows.append(row)
                    elif matched_col_idx is not None and target_col_val is not None:
                        cells = [c.strip() for c in row.strip().strip("|").split("|")]
                        if matched_col_idx < len(cells) and cells[matched_col_idx].lower() == target_col_val:
                            data_rows.append(row)

                if data_rows:
                    if current_heading and current_heading not in used_headings:
                        out_lines.append(current_heading)
                        out_lines.append("")
                        used_headings.add(current_heading)
                    out_lines.extend(header_rows)
                    out_lines.extend(data_rows)
                    out_lines.append("")
            continue

        if line.startswith("- "):
            bullet_block = [line]
            i += 1
            while i < n and (lines[i].startswith("  ") or lines[i].strip() == ""):
                bullet_block.append(lines[i])
                i += 1
            block_text = "\n".join(bullet_block).strip()
            if pattern.search(block_text):
                if current_heading and current_heading not in used_headings:
                    out_lines.append(current_heading)
                    out_lines.append("")
                    used_headings.add(current_heading)
                out_lines.append(block_text)
                out_lines.append("")
            continue

        i += 1

    result = "\n".join(out_lines).strip()
    if not matched_sections and len(out_lines) <= 2:
        if len(cleaned_markdown) <= 4000:
            return cleaned_markdown
        headings = [
            ln.strip()
            for ln in lines
            if re.match(r"^#{2,4}\s+", ln.strip())
        ]
        heading_list = "\n".join(f"- `{h}`" for h in headings[:30]) or "- *(none)*"
        return (
            "\n".join(out_lines)
            + f"\nNo section, table row, or bullet matched `#{term}` "
            f"(page is {len(cleaned_markdown):,} chars — full page omitted to protect context).\n\n"
            f"Available sections on this page:\n{heading_list}\n"
        )
    return re.sub(r"\n{3,}", "\n\n", result) + "\n"


def _compact_mastery_section(body_lines: list[str]) -> list[str]:
    """
    Compact a `## Mastery` section's level requirement and grand reward header
    while preserving all quest bullets and nested enhancement sub-bullets.
    """
    raw_body = "\n".join(body_lines).strip()
    if not raw_body:
        return body_lines

    # Fast path for DOM-rendered `.rod-mastery-container` bullet lists
    if "- **Grand Reward**:" in raw_body:
        level_req: str | None = None
        filtered_lines: list[str] = []
        for ln in raw_body.splitlines():
            m_lvl = re.match(r"^Starting Level Requirement:\s*\**([^\n*]+)\**$", ln.strip(), re.IGNORECASE)
            if m_lvl:
                level_req = m_lvl.group(1).strip()
                continue
            if level_req and ln.startswith("- **Grand Reward**:"):
                grand_val = ln.split("- **Grand Reward**:", 1)[1].strip()
                filtered_lines.append(f"- **Level Req**: {level_req} | **Grand Reward**: {grand_val}")
                level_req = None
                continue
            if ln.strip():
                filtered_lines.append(ln)
        if level_req:
            filtered_lines.insert(0, f"- **Level Req**: {level_req}")
        return filtered_lines

    paragraphs = [
        p.strip()
        for p in re.split(r"\n{2,}", raw_body)
        if p.strip()
    ]
    if not paragraphs:
        return body_lines

    out: list[str] = []
    level_req: str | None = None
    grand_reward: str | None = None
    current_quest: str | None = None
    current_req_parts: list[str] = []

    i = 0
    n = len(paragraphs)
    while i < n:
        p = paragraphs[i]
        m_lvl = re.match(r"^Starting Level Requirement:\s*\**([^\n*]+)\**$", p, re.IGNORECASE)
        if m_lvl:
            level_req = m_lvl.group(1).strip()
            i += 1
            continue

        if re.match(r"^\*{2,3}GRAND REWARD\*{2,3}$", p, re.IGNORECASE):
            i += 1
            while i < n:
                sub = paragraphs[i]
                if sub.lower().startswith("complete all quests"):
                    i += 1
                    continue
                m_rew = re.match(r"^Reward:\s*(.+)$", sub, re.IGNORECASE)
                if m_rew:
                    grand_reward = m_rew.group(1).strip()
                    i += 1
                break
            continue

        # Quest title in italics: *Quest Name*
        m_quest = re.match(r"^\*([^*]+)\*$", p)
        if m_quest and not p.lower().startswith("*also includes") and not p.lower().startswith("*main article"):
            current_quest = m_quest.group(1).strip()
            current_req_parts = []
            i += 1
            continue

        m_rew = re.match(r"^Reward:\s*(.+)$", p, re.IGNORECASE)
        if m_rew and current_quest:
            rew_text = m_rew.group(1).strip()
            # Check if next paragraph is an italic or bulleted Mastery Enhancement detail (e.g. "*Also includes...*" or "- *Puritas chance...*")
            while i + 1 < n and (
                re.match(r"^\*Also includes\b", paragraphs[i + 1], re.IGNORECASE)
                or re.match(r"^-\s*\*[^*\n]+", paragraphs[i + 1])
            ):
                extra = re.sub(r"^-\s*", "", paragraphs[i + 1]).strip()
                extra = re.sub(r"^\*|\*$", "", extra).strip().rstrip(".")
                rew_text = f"{rew_text} ({extra})"
                i += 1
            req_str = " ".join(current_req_parts).strip()
            if req_str:
                out.append(f"- **{current_quest}**: {req_str} → **Reward**: {rew_text}")
            else:
                out.append(f"- **{current_quest}** → **Reward**: {rew_text}")
            current_quest = None
            current_req_parts = []
            i += 1
            continue

        if current_quest:
            current_req_parts.append(p.replace("\n", " "))
        else:
            out.append(p)
        i += 1

    header_parts: list[str] = []
    if level_req:
        header_parts.append(f"**Level Req**: {level_req}")
    if grand_reward:
        header_parts.append(f"**Grand Reward**: {grand_reward}")
    if header_parts:
        out.insert(0, "- " + " | ".join(header_parts))

    return out


def _compact_change_history_section(
    heading_line: str,
    body_lines: list[str],
    *,
    strip_skin_patches: bool = True,
) -> list[str]:
    """
    Compact `## Change History` by folding `Released in Version X` into the heading,
    stripping cosmetic-only `"Added N new skin(s) for ..."` bullets when `strip_skin_patches=True`,
    and collapsing each version's bullets into a single `- **Version X**: ...` bullet line.
    """
    released_in: str | None = None
    versions: list[tuple[str, list[str]]] = []
    current_ver: str | None = None
    current_bullets: list[str] = []
    skip_subbullets = False
    other_lines: list[str] = []

    for raw_ln in body_lines:
        ln = raw_ln.rstrip()
        stripped = ln.strip()
        if not stripped:
            continue

        m_rel = re.match(r"^Released in\s+\**([^\n*]+)\**$", stripped, re.IGNORECASE)
        if m_rel:
            released_in = m_rel.group(1).strip()
            continue

        # Version header line like "Version 2.0" or "[Version 1.69.0](...)"
        m_ver = re.match(
            r"^(?:\[(Version\s+[^\]]+)\]\([^)]+\)|(Version\s+[0-9][0-9A-Za-z._\s-]*))$",
            stripped,
            re.IGNORECASE,
        )
        if m_ver:
            if current_ver is not None:
                versions.append((current_ver, current_bullets))
            current_ver = (m_ver.group(1) or m_ver.group(2) or "").strip()
            current_bullets = []
            skip_subbullets = False
            continue

        if current_ver is not None:
            is_subbullet = ln.startswith("  ") and stripped.startswith("- ")
            if is_subbullet:
                if skip_subbullets:
                    continue
                sub_txt = stripped[2:].strip().rstrip(".")
                if current_bullets:
                    current_bullets[-1] = f"{current_bullets[-1]} ({sub_txt})"
                else:
                    current_bullets.append(sub_txt)
                continue

            if stripped.startswith("- "):
                b_txt = stripped[2:].strip()
                if strip_skin_patches and re.match(
                    r"^(?:Added|Swapped|Renamed|Removed)\s+\d+\s+(?:new\s+)?(?:\[skin\]\([^)]+\)|skins?)\s+for\b",
                    b_txt,
                    re.IGNORECASE,
                ):
                    skip_subbullets = True
                    continue
                skip_subbullets = False
                # Skip redundant "<Page> was released/added." if Released in Version is already captured
                if released_in and re.search(r"\bwas (?:released|added)\.?$", b_txt, re.IGNORECASE):
                    continue
                current_bullets.append(b_txt.rstrip("."))
                continue

        other_lines.append(stripped)

    if current_ver is not None:
        versions.append((current_ver, current_bullets))

    new_heading = f"{heading_line} (Released in {released_in})" if released_in else heading_line
    out = [new_heading, ""]
    for ver, b_list in versions:
        if not b_list:
            continue
        # Merge multiple parenthetical sub-items cleanly
        joined = "; ".join(b_list)
        joined = re.sub(r"\)\s+\(", ", ", joined)
        out.append(f"- **{ver}**: {joined}")

    if len(out) == 2 and not other_lines:
        if released_in:
            out.append(f"- Released in **{released_in}**")
    else:
        out.extend(other_lines)

    return out


def _compact_article_body(text: str, *, strip_skin_patches: bool = True) -> str:
    """
    Prune boilerplate prose, fold `### Control Preview` into `- **Control**`,
    compact `**Subgroup**` single-bullet blocks, and compact `## Mastery` and `## Change History`.
    """
    if is_disambiguation_markdown(text):
        return text

    # 1. Extract Control Preview bar percentage and fold into Infobox "- **Control**: ..."
    m_ctrl_bar = re.search(
        r"### Control Preview\s*\n+[^\n]*?resulting in a bar\s+\**(\d+(?:\.\d+)?%)\**\s+of the reeling minigame:?\s*\n*",
        text,
        flags=re.IGNORECASE,
    )
    if m_ctrl_bar:
        bar_pct = m_ctrl_bar.group(1)
        text = text[: m_ctrl_bar.start()] + text[m_ctrl_bar.end() :]
        text = re.sub(
            r"^(- \*\*Control\*\*: [^\n(]+)$",
            rf"\1 ({bar_pct} bar)",
            text,
            count=1,
            flags=re.MULTILINE,
        )

    # 2. Strip boilerplate lines:
    #    - "*Main article: ...*"
    #    - Flavor blockquotes (`> *"..."*` and `> *"..."* — *Journal Hint*` / `*Bestiary Entry*`)
    #    - Redundant `***... Ability***` / `***Recommended Enchantments***` banners
    #    - Desmos XP calculator boilerplate on level rods
    #    - Redundant "Obtaining the X rewards Y XP in the Rod Journal." when "- **Journal**:" is present
    text = re.sub(r"^\*Main article:[^\n]+\*\s*\n+", "", text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r'^>\s*\*"[^"\n]+"\*\s*—\s*\*?(?:Journal Hint|Bestiary Entry)\*?\s*\n+', "", text, flags=re.MULTILINE)
    text = re.sub(
        r"^\*{3}(?:[^\n*]+'s Ability|Recommended Enchantments)\*{3}\s*\n+",
        "",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    text = re.sub(
        r"The following charts can be used to determine the amount of XP required[^\n]*\n+(?:- [^\n]+\n*)+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    if "- **Journal**:" in text:
        text = re.sub(
            r"^Obtaining the [^\n]+ rewards [\d,]+ (?:\[XP\]\([^)]+\)|XP) in the (?:\[Rod Journal\]\([^)]+\)|Rod Journal)\.\s*\n*",
            "",
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )

    # 3. Section-level processing (Overview pruning, Mastery compaction, Change History compaction)
    lines = text.splitlines()
    out_lines: list[str] = []
    i = 0
    n = len(lines)
    has_stats_infobox = "### Stats" in text

    while i < n:
        line = lines[i]
        m_h2 = re.match(r"^(##\s+)(.+)$", line)
        if m_h2:
            h_title = m_h2.group(2).strip().lower()
            sec_start = i
            i += 1
            body: list[str] = []
            while i < n and not re.match(r"^##\s+", lines[i]):
                body.append(lines[i])
                i += 1

            if h_title == "overview" and has_stats_infobox:
                # Filter out generic Rod stat-restatement filler paragraphs
                paras = [p.strip() for p in re.split(r"\n{2,}", "\n".join(body).strip()) if p.strip()]
                kept_paras: list[str] = []
                for p in paras:
                    is_stat_filler = (
                        bool(re.search(r"\b(?:Lure Speed|Resilience)\b", p))
                        and bool(re.search(r"\b(?:Luck|Control|Fishing Rod|stat profile)\b", p))
                    ) or bool(
                        re.match(
                            r"^Overall,\s+the\s+[^\n]+is\s+(?:a|an)\s+(?:reliable|outstanding|solid|great|good|strong|versatile|decent|powerful)\b",
                            p,
                            re.IGNORECASE,
                        )
                    )
                    if not is_stat_filler:
                        kept_paras.append(p)
                if kept_paras:
                    out_lines.append(lines[sec_start])
                    out_lines.append("")
                    out_lines.append("\n\n".join(kept_paras))
                    out_lines.append("")
                continue

            if h_title == "mastery":
                compacted_mastery = _compact_mastery_section(body)
                out_lines.append(lines[sec_start])
                out_lines.append("")
                out_lines.extend(compacted_mastery)
                out_lines.append("")
                continue

            if h_title == "change history":
                compacted_ch = _compact_change_history_section(
                    lines[sec_start],
                    body,
                    strip_skin_patches=strip_skin_patches,
                )
                out_lines.extend(compacted_ch)
                out_lines.append("")
                continue

            out_lines.append(lines[sec_start])
            out_lines.extend(body)
            continue

        out_lines.append(line)
        i += 1

    text = "\n".join(out_lines)

    # 5. Compact `**Subheading**\n\n- single bullet` into `- **Subheading**: single bullet`
    def _merge_bold_subhead(match: re.Match[str]) -> str:
        subhead = match.group(1).strip()
        bullets_block = match.group(2).rstrip()
        b_lines = [ln for ln in bullets_block.splitlines() if ln.strip()]
        if len(b_lines) == 1 and b_lines[0].startswith("- "):
            item = b_lines[0][2:].strip()
            return f"- **{subhead}**: {item}\n"
        indented = "\n".join(f"  {ln}" for ln in b_lines)
        return f"- **{subhead}**:\n{indented}\n"

    text = re.sub(
        r"^\*\*([^*\n]+)\*\*\s*\n+((?:- [^\n]+\n?(?:  - [^\n]+\n?)*)+)",
        _merge_bold_subhead,
        text,
        flags=re.MULTILINE,
    )

    # Collapse blank lines between consecutive top-level bullet items
    text = re.sub(r"(^- [^\n]+(?:\n  - [^\n]+)*)\n\n+(?=- )", r"\1\n", text, flags=re.MULTILINE)
    # Ensure a blank line before headings
    text = re.sub(r"([^\n])\n(#{2,6}\s+)", r"\1\n\n\2", text)

    return text


def clean_fischipedia_markdown(
    markdown: str,
    *,
    strip_wiki_links: bool = True,
    compact_infobox: bool = True,
    include_images: bool = False,
    strip_skin_patches: bool = True,
) -> str:
    """
    Full post-processing pipeline for Fischipedia Markdown.
    """
    text = markdown.replace("\r\n", "\n")

    text = _strip_raw_wiki_chrome(text)
    text = _clean_tooltips_and_ui_tokens(text)
    text = _unwrap_single_cell_tables(text)
    text = _strip_decorative_icons(text, include_images=include_images)
    text = _clean_links_and_escapes(
        text,
        strip_wiki_links=strip_wiki_links,
        include_images=include_images,
    )

    if compact_infobox:
        text = _compact_infobox_section(text)

    text = _compact_article_body(text, strip_skin_patches=strip_skin_patches)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def scrape_and_clean_fischipedia(
    url_or_page: str,
    *,
    api_key: str | None = None,
    api_url: str | None = None,
    strip_wiki_links: bool = True,
    compact_infobox: bool = True,
    include_images: bool = False,
    expand_disambiguation_hatnote: bool = True,
    exclude_sections: list[str] | None = None,
    use_cache: bool = True,
    use_firecrawl: bool = False,
) -> str:
    """
    High-level helper that fetches and cleans a Fischipedia (or MediaWiki) page.
    By default (`use_firecrawl=False`), uses MediaWiki's native `w/api.php?action=parse`
    at zero Firecrawl credit cost.
    Automatically resolves redirects (including `#tofragment`), expands any
    `_(Disambiguation)` hatnote at the top of the page, and filters by `#Fragment`
    or `exclude_sections` if provided.
    """
    raw_url, explicit_fragment = split_target_and_fragment(url_or_page)

    if not use_firecrawl:
        cache_lookup_key = (raw_url.lower(), strip_wiki_links, False, include_images, False)
        if use_cache and cache_lookup_key in _CLEANED_PAGE_CACHE:
            raw_md, cleaned, redirect_tofragment = _CLEANED_PAGE_CACHE[cache_lookup_key]
        else:
            canonical_title, raw_md, redirect_tofragment = parse_fischipedia_page(url_or_page)
            canonical_url = page_title_to_url(canonical_title)
            canon_key = (canonical_url.lower(), strip_wiki_links, False, include_images, False)
            if use_cache and canon_key in _CLEANED_PAGE_CACHE:
                raw_md, cleaned, _ = _CLEANED_PAGE_CACHE[canon_key]
            else:
                cleaned = clean_fischipedia_markdown(
                    raw_md,
                    strip_wiki_links=strip_wiki_links,
                    compact_infobox=False,
                    include_images=include_images,
                )
                if use_cache:
                    _CLEANED_PAGE_CACHE[canon_key] = (raw_md, cleaned, redirect_tofragment)
            if use_cache:
                _CLEANED_PAGE_CACHE[cache_lookup_key] = (raw_md, cleaned, redirect_tofragment)
        fragment = explicit_fragment or redirect_tofragment
    else:
        canonical_title, _ = resolve_canonical_title(url_or_page)
        url = page_title_to_url(canonical_title)
        cache_key = (url.lower(), strip_wiki_links, compact_infobox, include_images, True)
        if use_cache and cache_key in _CLEANED_PAGE_CACHE:
            raw_md, cleaned, _ = _CLEANED_PAGE_CACHE[cache_key]
        else:
            raw_md = scrape_fischipedia_url(url, api_key=api_key, api_url=api_url)
            cleaned = clean_fischipedia_markdown(
                raw_md,
                strip_wiki_links=strip_wiki_links,
                compact_infobox=compact_infobox,
                include_images=include_images,
            )
            if use_cache:
                _CLEANED_PAGE_CACHE[cache_key] = (raw_md, cleaned, None)
        fragment = explicit_fragment

    result = cleaned

    if fragment:
        result = extract_fragment_content(result, fragment)
    elif expand_disambiguation_hatnote and not is_disambiguation_markdown(raw_md):
        disambig_url = find_disambiguation_hatnote_url(raw_md)
        if disambig_url:
            try:
                disambig_key = (disambig_url.lower(), strip_wiki_links, False, include_images, use_firecrawl)
                if use_cache and disambig_key in _CLEANED_PAGE_CACHE:
                    _, disambig_clean, _ = _CLEANED_PAGE_CACHE[disambig_key]
                else:
                    if not use_firecrawl:
                        _, disambig_raw, _ = parse_fischipedia_page(disambig_url)
                    else:
                        disambig_raw = scrape_fischipedia_url(disambig_url, api_key=api_key, api_url=api_url)
                    disambig_clean = clean_fischipedia_markdown(
                        disambig_raw,
                        strip_wiki_links=strip_wiki_links,
                        compact_infobox=False,
                        include_images=include_images,
                    )
                    if use_cache:
                        _CLEANED_PAGE_CACHE[disambig_key] = (disambig_raw, disambig_clean, None)

                disambig_bullets = [
                    f"> {ln}"
                    for ln in disambig_clean.splitlines()
                    if ln.lstrip().startswith("- ")
                ]
                if disambig_bullets:
                    notice = (
                        "> **Disambiguation — other pages matching this name:**\n"
                        + "\n".join(disambig_bullets)
                    )
                    lines = result.splitlines()
                    insert_idx = 1
                    for idx, ln in enumerate(lines[:6]):
                        if "_(Disambiguation)" in ln or "(Disambiguation)" in ln:
                            insert_idx = idx + 1
                            break
                    lines.insert(insert_idx, "\n" + notice + "\n")
                    result = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
            except Exception:
                pass

    if exclude_sections:
        result = strip_sections_by_heading(result, exclude_sections)

    return result


# ---------------------------------------------------------------------------
# 5. File / Directory Batch Processing & CLI
# ---------------------------------------------------------------------------


def extract_markdown_from_file(path: Path) -> str:
    """Read Markdown from either a `.md`/`.txt` file or a Firecrawl `.json` file."""
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if "markdown" in data and isinstance(data["markdown"], str):
                    return data["markdown"]
                if "data" in data and isinstance(data["data"], dict):
                    md = data["data"].get("markdown")
                    if isinstance(md, str):
                        return md
        except json.JSONDecodeError:
            if path.suffix.lower() == ".json":
                raise
    return raw


def process_path(
    input_path: Path,
    output_path: Path | None,
    *,
    strip_wiki_links: bool = True,
    compact_infobox: bool = True,
) -> list[Path]:
    """Process a single `.md`/`.json` file or a directory of files."""
    written: list[Path] = []

    if input_path.is_file():
        md = extract_markdown_from_file(input_path)
        cleaned = clean_fischipedia_markdown(
            md,
            strip_wiki_links=strip_wiki_links,
            compact_infobox=compact_infobox,
        )
        if output_path is None:
            dest = input_path.with_name(f"{input_path.stem}.cleaned.md")
        elif output_path.is_dir():
            dest = output_path / f"{input_path.stem}.md"
        else:
            dest = output_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(cleaned, encoding="utf-8")
        written.append(dest)
        return written

    if input_path.is_dir():
        out_dir = output_path or (input_path / "cleaned")
        out_dir.mkdir(parents=True, exist_ok=True)
        for file_path in sorted(input_path.iterdir()):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in {".md", ".json", ".txt"}:
                continue
            if file_path.name.endswith(".cleaned.md"):
                continue
            md = extract_markdown_from_file(file_path)
            cleaned = clean_fischipedia_markdown(
                md,
                strip_wiki_links=strip_wiki_links,
                compact_infobox=compact_infobox,
            )
            dest = out_dir / f"{file_path.stem}.md"
            dest.write_text(cleaned, encoding="utf-8")
            written.append(dest)
        return written

    raise FileNotFoundError(f"Input path does not exist: {input_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch and parse Fischipedia (or any MediaWiki) pages into clean Markdown at zero Firecrawl credit cost."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Optional positional Fischipedia URL, page name (e.g. 'No-Life Rod' or 'Fishing Rods#Stage_8'), or local file path.",
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "-i",
        "--input",
        type=Path,
        help="Path to an existing scraped file (.md or Firecrawl .json) or directory of files.",
    )
    group.add_argument(
        "-u",
        "--url",
        type=str,
        help="Fischipedia URL or page title (supports #Fragment filtering, e.g. 'Enchanting#Empyrean').",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output file or directory path (defaults to stdout for URL lookups or <stem>.cleaned.md for file inputs).",
    )
    parser.add_argument(
        "--keep-wiki-links",
        action="store_true",
        help="Keep internal /wiki/ Markdown links instead of unwrapping them to plain text.",
    )
    parser.add_argument(
        "--no-compact-infobox",
        action="store_true",
        help="Do not compact the top Infobox key/value lines into bullet lists.",
    )
    parser.add_argument(
        "--no-disambig-expand",
        action="store_true",
        help="Do not automatically fetch and inline _(Disambiguation) hatnote links.",
    )
    parser.add_argument(
        "--exclude-section",
        action="append",
        default=[],
        help="Section heading to exclude (repeatable, e.g. --exclude-section 'Change History').",
    )
    parser.add_argument(
        "--wikitext",
        action="store_true",
        help="Output raw MediaWiki source wikitext (via action=parse&prop=wikitext) instead of rendered Markdown.",
    )
    parser.add_argument(
        "--use-firecrawl",
        action="store_true",
        help="Use Firecrawl /scrape instead of the default zero-credit MediaWiki API parser.",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Firecrawl API key (only used with --use-firecrawl).",
    )
    args = parser.parse_args(argv)

    strip_wiki_links = not args.keep_wiki_links
    compact_infobox = not args.no_compact_infobox

    url_target = args.url
    input_path = args.input

    if args.target and not url_target and not input_path:
        candidate_path = Path(args.target)
        if candidate_path.exists():
            input_path = candidate_path
        else:
            url_target = args.target

    if not url_target and not input_path:
        parser.error("Provide a URL/page name (-u/--url or positional) or an input file (-i/--input).")

    if url_target:
        if args.wikitext:
            wt_info = get_page_wikitext(url_target)
            cleaned = f"# {wt_info['title']} (Wikitext)\n\n{wt_info['wikitext'].strip()}\n"
        else:
            cleaned = scrape_and_clean_fischipedia(
                url_target,
                api_key=args.api_key,
                strip_wiki_links=strip_wiki_links,
                compact_infobox=compact_infobox,
                expand_disambiguation_hatnote=not args.no_disambig_expand,
                exclude_sections=args.exclude_section,
                use_firecrawl=args.use_firecrawl,
            )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(cleaned, encoding="utf-8")
            line_count = cleaned.count("\n")
            print(
                f"Saved cleaned output ({len(cleaned):,} chars, {line_count} lines) to {args.output}"
            )
        else:
            sys.stdout.buffer.write(cleaned.encode("utf-8"))
        return 0

    written = process_path(
        input_path,
        args.output,
        strip_wiki_links=strip_wiki_links,
        compact_infobox=compact_infobox,
    )
    for path in written:
        print(f"Cleaned: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
