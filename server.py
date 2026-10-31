#!/usr/bin/env python3
"""
Fisch Official Wiki (`fischipedia.org`) Local MCP Server.

Exposes 4 tools over MCP stdio (JSON-RPC 2.0):
- `get_page`: Scrape and clean a Fischipedia page or `Page#Fragment` into compact Markdown
  (with automatic redirect resolution, `_(Disambiguation)` hatnote expansion, `#Fragment` extraction,
  in-memory caching, and optional section filtering or disk output).
- `search_wiki`: Search `fischipedia.org` via MediaWiki's `w/api.php` (`opensearch` + `list=search`).
- `get_page_outline`: Get a page's canonical title, categories, and section headings/anchors via `w/api.php`.
- `list_category`: List pages in a Fischipedia category (e.g. `Fishing Rods`, `Mutations`, `Fish`) via `w/api.php`.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any

# Ensure local directory is importable regardless of working directory
_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

import fischipedia_filter  # noqa: E402

_FILTER_PATH = _THIS_DIR / "fischipedia_filter.py"
_FILTER_MTIME: float = _FILTER_PATH.stat().st_mtime if _FILTER_PATH.exists() else 0.0


def _ensure_latest_filter() -> None:
    """Hot-reload fischipedia_filter.py if modified on disk."""
    global _FILTER_MTIME, fischipedia_filter
    try:
        mtime = _FILTER_PATH.stat().st_mtime
        if mtime != _FILTER_MTIME:
            fischipedia_filter = importlib.reload(fischipedia_filter)
            _FILTER_MTIME = mtime
    except Exception:
        pass

SERVER_NAME = "fisch-mcp"
SERVER_VERSION = "2.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "get_page",
        "description": (
            "Fetch, parse, and clean an Official Fisch Wiki (fischipedia.org) page or Page#Fragment "
            "into ultra-compact Markdown via MediaWiki's w/api.php (action=parse) at zero Firecrawl credit "
            "cost. Strips MediaWiki UI chrome, hidden tooltips, navboxes, image/media URLs, wiki link URLs, "
            "and non-gameplay sections ('Skins', 'Gallery', 'Catch Quips', 'Description') by default while "
            "keeping 'Trivia' and compacted 'Change History'; compacts Infobox stats, Mastery quests, and "
            "Fish values into dense bullet lists; expands _(Disambiguation) hatnotes; and extracts matching "
            "sections/table rows when a #Fragment is given (e.g. 'No-Life Rod', 'Fishing Rods#Stage_8', "
            "'Enchanting#Empyrean', 'No-Life Rod#Skins')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Fischipedia page name (e.g. 'No-Life Rod', 'Megalodon'), "
                        "Page#Fragment (e.g. 'Enchanting#Empyrean', 'No-Life Rod#Skins'), or full fischipedia.org URL."
                    ),
                },
                "keep_wiki_links": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Preserve internal [Label](https://fischipedia.org/wiki/...) links. Default: false (unwraps links to plain text to save context)."
                    ),
                },
                "include_images": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Preserve https://static.wikitide.net/... image and media links. Default: false."
                    ),
                },
                "include_all_sections": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include cosmetic/flavor sections ('Skins', 'Gallery', 'Catch Quips', 'Description') that are excluded by default. Default: false."
                    ),
                },
                "compact_infobox": {
                    "type": "boolean",
                    "default": True,
                    "description": "Compact top Infobox key/value lines into '- **Key**: Value' bullets. Default: true.",
                },
                "expand_disambiguation": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Automatically fetch and inline _(Disambiguation) hatnote links at the top "
                        "of ambiguous pages. Default: true."
                    ),
                },
                "exclude_sections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional explicit list of section headings to strip (overrides default exclusions ['Skins', 'Gallery', 'Catch Quips', 'Description'])."
                    ),
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Optional file path to save the full cleaned Markdown to disk. When set, "
                        "writes the file and returns a short summary + section outline instead of "
                        "the full text."
                    ),
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "search_wiki",
        "description": (
            "Search the Official Fisch Wiki (fischipedia.org) using MediaWiki's w/api.php "
            "(combines prefix opensearch and full-text search). Instant and uses zero Firecrawl "
            "credits. Use this when you don't know the exact page title or want to find pages "
            "mentioning a fish, rod, mutation, enchant, bait, location, or mechanic."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (e.g. 'Empyrean', 'Seraphic Rod', 'Stage 8', 'Scylla').",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum number of search results to return (1-50, default 10).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_page_outline",
        "description": (
            "Fetch a Fischipedia page's canonical title (resolving redirects), wiki categories, "
            "and table-of-contents section headings/anchors via MediaWiki's w/api.php (action=parse). "
            "Instant and uses zero Firecrawl credits. Useful for checking section names on large "
            "pages before calling get_page with a Page#Fragment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Fischipedia page title or URL (e.g. 'Enchanting', 'Fishing Rods', 'No-Life Rod').",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "list_category",
        "description": (
            "List pages belonging to an Official Fisch Wiki category via MediaWiki's w/api.php "
            "(action=query&list=categorymembers). Instant and uses zero Firecrawl credits. "
            "Examples of categories: 'Fishing Rods', 'Mutations', 'Unappraisable Mutations', "
            "'Fish', 'Baits', 'Locations', 'Events', 'Totems'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Category name (with or without 'Category:' prefix, e.g. 'Mutations' or 'Fishing Rods').",
                },
                "filter_text": {
                    "type": "string",
                    "description": "Optional case-insensitive substring to filter category member titles.",
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum number of category members to return (1-500, default 100).",
                },
            },
            "required": ["category"],
        },
    },
]


def _handle_get_page(args: dict[str, Any]) -> str:
    target = str(args.get("target", "")).strip()
    if not target:
        raise ValueError("Parameter 'target' is required.")

    keep_wiki_links = bool(args.get("keep_wiki_links", False))
    include_images = bool(args.get("include_images", False))
    include_all_sections = bool(args.get("include_all_sections", False))
    compact_infobox = bool(args.get("compact_infobox", True))
    expand_disambiguation = bool(args.get("expand_disambiguation", True))

    raw_exclude = args.get("exclude_sections")
    if isinstance(raw_exclude, list):
        exclude_sections: list[str] | None = [str(s) for s in raw_exclude if str(s).strip()]
    elif include_all_sections or "#" in target:
        exclude_sections = None
    else:
        exclude_sections = list(fischipedia_filter.DEFAULT_EXCLUDE_SECTIONS)

    output_path_raw = args.get("output_path")

    cleaned = fischipedia_filter.scrape_and_clean_fischipedia(
        target,
        strip_wiki_links=not keep_wiki_links,
        compact_infobox=compact_infobox,
        include_images=include_images,
        expand_disambiguation_hatnote=expand_disambiguation,
        exclude_sections=exclude_sections,
        use_cache=True,
    )

    if output_path_raw:
        out_path = Path(str(output_path_raw)).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(cleaned, encoding="utf-8")
        headings = [
            ln.strip()
            for ln in cleaned.splitlines()
            if re.match(r"^#{1,4}\s+", ln.strip())
        ]
        line_count = cleaned.count("\n")
        heading_preview = "\n".join(f"  {h}" for h in headings[:25])
        return (
            f"Saved cleaned Fischipedia scrape for '{target}' to {out_path} "
            f"({len(cleaned):,} chars, {line_count} lines).\n\n"
            f"Headings in file:\n{heading_preview}"
        )

    # Context guard: prevent huge unfiltered index pages (>25 KB) from flooding the context window
    if len(cleaned) > 25_000 and "#" not in target:
        headings = [
            ln.strip()
            for ln in cleaned.splitlines()
            if re.match(r"^#{1,4}\s+", ln.strip())
        ]
        heading_preview = "\n".join(f"- `{h}`" for h in headings[:30])
        lead_preview = "\n".join(cleaned.splitlines()[:35])
        return (
            f"⚠️ Context Guard: `{target}` is a large index page ({len(cleaned):,} chars). "
            f"Full page omitted to protect your context window (the page is cached in memory, so follow-up `#Fragment` queries are instant and free).\n\n"
            f"**How to query this page safely:**\n"
            f"- Pass `{target}#<Section_or_Row_or_Column=Value>` to `get_page` (e.g. `Fishing Rods#Stage_8`, `Enchanting#Empyrean`), OR\n"
            f"- Use `list_category` (e.g. `category: 'Stage 8 Fishing Rods'`).\n\n"
            f"### Headings on `{target}`\n{heading_preview}\n\n"
            f"### Lead Preview\n{lead_preview}\n"
        )

    return cleaned


def _handle_search_wiki(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    limit = int(args.get("limit", 10))
    data = fischipedia_filter.search_fischipedia(query, limit=limit)
    results = data.get("results", [])
    if not results:
        return f"No Fischipedia results found for query: '{query}'."

    lines = [
        f"# Fischipedia Search: `{data['query']}` ({data['total_hits']} hits)",
        "",
    ]
    for item in results:
        title = item["title"]
        snippet = item.get("snippet", "")
        wc = item.get("wordcount")
        wc_str = f" _({wc}w)_" if wc is not None else ""
        if snippet:
            lines.append(f"- **{title}**{wc_str}: {snippet}")
        else:
            lines.append(f"- **{title}**{wc_str}")
    return "\n".join(lines) + "\n"


def _handle_get_page_outline(args: dict[str, Any]) -> str:
    target = str(args.get("target", "")).strip()
    if not target:
        raise ValueError("Parameter 'target' is required.")
    outline = fischipedia_filter.get_page_outline(target)
    lines = [
        f"# Outline: {outline['title']} (ID: {outline.get('pageid')})",
    ]
    if outline.get("categories"):
        lines.append(f"- **Categories**: {', '.join(outline['categories'])}")
    lines.append("")
    lines.append("## Sections (Pass `fragment_target` to `get_page`)")
    sections = outline.get("sections", [])
    if not sections:
        lines.append("_(No subsections)_")
    else:
        for sec in sections:
            indent = "  " * max(0, int(sec.get("level", 2)) - 2)
            lines.append(
                f"{indent}- `{sec['fragment_target']}` — {sec.get('number', '')} {sec['heading']}"
            )
    return "\n".join(lines) + "\n"


def _handle_list_category(args: dict[str, Any]) -> str:
    category = str(args.get("category", "")).strip()
    if not category:
        raise ValueError("Parameter 'category' is required.")
    filter_text = args.get("filter_text")
    limit = int(args.get("limit", 100))
    res = fischipedia_filter.list_category_members(
        category,
        filter_text=str(filter_text) if filter_text else None,
        limit=limit,
    )
    members = res.get("members", [])
    header = f"# {res['category']} ({res['count']} items"
    if res.get("filter_text"):
        header += f", filter: `{res['filter_text']}`"
    if res.get("has_more"):
        header += ", more available"
    header += ")"

    lines = [header, ""]
    if not members:
        lines.append("_(No matching pages found in this category.)_")
    else:
        for m in members:
            tag = " _(subcategory)_" if m.get("type") == "subcategory" else ""
            lines.append(f"- {m['title']}{tag}")
    return "\n".join(lines) + "\n"


def dispatch_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one MCP tool and return an MCP CallToolResult payload."""
    try:
        _ensure_latest_filter()
        if name == "get_page":
            text = _handle_get_page(arguments)
        elif name == "search_wiki":
            text = _handle_search_wiki(arguments)
        elif name == "get_page_outline":
            text = _handle_get_page_outline(arguments)
        elif name == "list_category":
            text = _handle_list_category(arguments)
        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            }
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }
    except Exception as exc:
        err_msg = f"Error in {name}: {exc}\n{traceback.format_exc()}"
        return {
            "content": [{"type": "text", "text": err_msg}],
            "isError": True,
        }


def handle_rpc_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Process a single JSON-RPC 2.0 request/notification and return a response dict (or None)."""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications have no `id` and expect no response
    if msg_id is None:
        return None

    if method == "initialize":
        client_Version = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": client_Version,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "Official Fisch Wiki (fischipedia.org) local MCP server. "
                    "Prefer `search_wiki` or `list_category` when exact page titles are unknown, "
                    "`get_page_outline` to inspect sections on large pages, and `get_page` "
                    "(supports `Page#Fragment` such as `Fishing Rods#Stage_8` or `Enchanting#Empyrean`) to fetch "
                    "cleaned Markdown. When invoking these tools inside Code Mode (`execute`), start the script "
                    "with a short `// Fischipedia: <tool> (<target/query>)` comment on line 1 for TUI visibility."
                ),
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS_SCHEMA},
        }

    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        tool_args = params.get("arguments") or {}
        if not isinstance(tool_args, dict):
            tool_args = {}
        result = dispatch_tool_call(tool_name, tool_args)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}

    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resourceTemplates": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    }


def serve_stdio() -> int:
    """Run the MCP JSON-RPC 2.0 stdio loop with UTF-8 binary streams."""
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        line = stdin.readline()
        if not line:
            break

        stripped = line.strip()
        if not stripped:
            continue

        # Support optional Content-Length header framing as well as standard newline-delimited JSON
        if stripped.lower().startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1].strip())
                # Consume blank header separator line(s)
                while True:
                    hdr_line = stdin.readline()
                    if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                        break
                payload_bytes = stdin.read(length)
            except Exception:
                continue
        else:
            payload_bytes = stripped

        try:
            msg = json.loads(payload_bytes.decode("utf-8"))
        except Exception as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            stdout.write((json.dumps(err_resp) + "\n").encode("utf-8"))
            stdout.flush()
            continue

        if isinstance(msg, dict):
            resp = handle_rpc_message(msg)
            if resp is not None:
                stdout.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(serve_stdio())
