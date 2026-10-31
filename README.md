# Fisch Official Wiki (`fischipedia.org`) Local MCP Server

Local `stdio` Model Context Protocol (MCP) server for looking up **Fisch** game data from the authoritative [Official Fisch Wiki (`fischipedia.org`)](https://fischipedia.org).

Combines:
1. **MediaWiki Action API (`https://fischipedia.org/w/api.php`)** for instant, zero-credit wiki search (`opensearch` + full-text `list=search`), redirect resolution, page outlines (`action=parse`), and category member browsing (`list=categorymembers`).
2. **Zero-Credit MediaWiki HTML-to-Markdown DOM Parser & Cleaner (`fischipedia_filter.py`)** via `action=parse&prop=text` (with optional Firecrawl fallback), in-memory page caching, automatic `_(Disambiguation)` hatnote expansion, `#Fragment` extraction, and section filtering.

## Exposed MCP Tools

| Tool | Backend | Description |
| :--- | :--- | :--- |
| `get_page` | MediaWiki `action=parse` + DOM/Markdown Cleaner + Memory Cache | Fetches, parses, and cleans a page (`"No-Life Rod"`), `Page#Fragment` (`"Enchanting#Empyrean"`), or URL into compact Markdown. Supports `exclude_sections` (e.g. `["Change History", "Skins", "Gallery"]`) and `output_path`. |
| `search_wiki` | `w/api.php` (`opensearch` + `list=search`) | Instant prefix + full-text search returning canonical titles, URLs, word counts, and text snippets. |
| `get_page_outline` | `w/api.php` (`action=parse`) | Returns canonical title, categories, and section headings with ready-to-use `Page#Anchor` fragment targets. |
| `list_category` | `w/api.php` (`list=categorymembers`) | Lists pages in categories like `Fishing Rods`, `Mutations`, `Unappraisable Mutations`, `Fish`, `Baits`, `Locations`, `Events`. |

## OpenCode V2 Setup (Ubuntu / Linux)

Register the server globally in OpenCode V2 (`~/.config/opencode/opencode.json`):

```bash
opencode mcp add fisch --global -- python3 /home/juan/projects/fisch-mcp-antigravity/server.py
opencode mcp list
```

Or in `opencode.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "fisch": {
        "type": "local",
        "command": ["python3", "/home/juan/projects/fisch-mcp-antigravity/server.py"]
      }
    }
  }
}
```

## Running Standalone

```bash
# Start MCP stdio server
python3 /home/juan/projects/fisch-mcp-antigravity/server.py

# Or use the CLI cleaner directly
python3 /home/juan/projects/fisch-mcp-antigravity/fischipedia_filter.py "No-Life Rod" --keep-wiki-links
```
