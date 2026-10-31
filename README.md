# Fisch Official Wiki (`fischipedia.org`) Local MCP Server

Zero-dependency, zero-credit local `stdio` Model Context Protocol (MCP) server for looking up **Fisch** game data from the authoritative [Official Fisch Wiki (`fischipedia.org`)](https://fischipedia.org) (or any MediaWiki site via `MEDIAWIKI_BASE_URL`).

Compatible out of the box with **OpenCode V2**, **Claude Code**, **OpenAI Codex**, **Google Antigravity (Desktop & `agy` CLI)**, **Cursor IDE**, and **Cline (Extension & CLI)** across Linux, macOS, and Windows.

## Architecture

1. **MediaWiki Action API (`https://fischipedia.org/w/api.php`)**: Instant, zero-credit wiki search (`opensearch` + full-text `list=search`), redirect resolution (including `#tofragment`), section outlines (`action=parse`), and category member browsing (`list=categorymembers`).
2. **Stdlib HTML-to-Markdown DOM Parser & Compactor (`fischipedia_filter.py`)**: Converts `action=parse&prop=text` directly into ultra-compact Markdown (~75–80% smaller than raw wiki scrapes) with in-memory caching, automatic `_(Disambiguation)` hatnote expansion, `#Fragment` section/table-row extraction, and section filtering.

## Exposed MCP Tools

| Tool | Backend | Description |
| :--- | :--- | :--- |
| `get_page` | MediaWiki `action=parse` + DOM/Markdown Compactor + Cache | Fetches, parses, and compacts a page (`"No-Life Rod"`), `Page#Fragment` (`"Enchanting#Empyrean"`, `"No-Life Rod#Skins"`), or URL into dense Markdown. Keeps `Trivia` and compacted `Change History` while excluding `Skins`, `Gallery`, `Catch Quips`, and `Description` by default (override via `#Fragment`, `include_all_sections`, or `exclude_sections`). |
| `search_wiki` | `w/api.php` (`opensearch` + `list=search`) | Instant prefix + full-text search returning canonical titles, word counts, and cleaned text snippets. |
| `get_page_outline` | `w/api.php` (`action=parse`) | Returns canonical title, categories, and section headings with ready-to-use `Page#Anchor` fragment targets. |
| `list_category` | `w/api.php` (`list=categorymembers`) | Lists pages in categories like `Fishing Rods`, `Mutations`, `Unappraisable Mutations`, `Fish`, `Baits`, `Locations`, `Events`, `Totems`. |

*(Note: For backward compatibility, the server also accepts legacy `fisch_`-prefixed tool calls such as `fisch_get_page`, or can advertise prefixed names when started with `--tool-prefix fisch_` / `FISCH_MCP_TOOL_PREFIX=fisch_`.)*

---

## Installation (Linux / macOS / Windows)

Requires **Python 3.10+** (uses 100% Python standard library — no `pip` dependencies required).

### Quick Auto-Install (`install.sh` / `install.ps1`)

The included installer scripts automatically install `fisch-mcp` (via `uv` or `pipx` if available, or direct Python fallback), detect all installed harnesses on your machine (**OpenCode V2**, **Claude Code**, **Codex**, **Antigravity**, **Cursor**, **Cline**), register the MCP server, and install `skills/fischipedia/SKILL.md`:

```bash
# Linux / macOS
./install.sh

# Preview actions without modifying files, or use a custom server name:
./install.sh --dry-run
./install.sh --name fisch-universal
```

```powershell
# Windows PowerShell
powershell -ExecutionPolicy Bypass -File .\install.ps1

# Preview actions or customize server name:
.\install.ps1 -DryRun
.\install.ps1 -Name fisch-universal
```

### Option A: Install `fisch-mcp` onto `PATH` Manually

Installing via `uv` or `pipx` places `fisch-mcp` and `fischipedia-filter` directly on your `PATH`, so harness configs don't need hardcoded file paths:

```bash
# Using uv
uv tool install .
# Or directly from GitHub without cloning:
uv tool install git+https://github.com/deproocho/fisch-mcp

# Using pipx
pipx install .
```

### Option B: Run Directly from Source

```bash
# Linux / macOS
python3 -u /path/to/fisch-mcp/server.py

# Windows
python -u C:\path\to\fisch-mcp\server.py
```

To auto-generate the exact CLI command and config snippet for your current OS and path, run:

```bash
python3 server.py --print-config all
```

---

## Harness Configuration Guide

Replace `fisch-mcp` below with `python3 -u /path/to/fisch-mcp/server.py` (or `python -u C:\path\to\fisch-mcp\server.py` on Windows) if you did not install the binary onto `PATH`.

### 1. OpenCode V2

```bash
opencode mcp add fisch --global -- fisch-mcp
opencode mcp list
```

Or in `~/.config/opencode/opencode.jsonc` (global) / `opencode.jsonc` (project):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "fisch": {
        "type": "local",
        "command": ["fisch-mcp"]
      }
    }
  }
}
```

### 2. Claude Code

```bash
claude mcp add --transport stdio --scope user fisch -- fisch-mcp
claude mcp list
```

Or in `~/.claude.json` (user) / `.mcp.json` (project):

```json
{
  "mcpServers": {
    "fisch": {
      "command": "fisch-mcp",
      "args": []
    }
  }
}
```

### 3. OpenAI Codex (CLI, IDE Extension, & Desktop App)

```bash
codex mcp add fisch -- fisch-mcp
codex mcp list
```

Or in `~/.codex/config.toml` (global) / `.codex/config.toml` (project):

```toml
[mcp_servers.fisch]
command = "fisch-mcp"
args = []
```

### 4. Google Antigravity (Desktop App, IDE, & `agy` CLI)

```bash
agy mcp add fisch fisch-mcp
```

Or in `~/.gemini/config/mcp_config.json` (global, or `%USERPROFILE%\.gemini\config\mcp_config.json` on Windows) / `<workspace>/.agents/mcp_config.json` (workspace):

```json
{
  "mcpServers": {
    "fisch": {
      "command": "fisch-mcp",
      "args": [],
      "disabled": false
    }
  }
}
```

*(For headless `agy run`, add `"mcp(fisch/*)"` under `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`.)*

### 5. Cursor IDE

Add to `~/.cursor/mcp.json` (global) or `<workspace>/.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "fisch": {
      "command": "fisch-mcp",
      "args": []
    }
  }
}
```

### 6. Cline (VS Code / JetBrains Extension & `cline` CLI)

```bash
cline mcp add fisch --yes -- fisch-mcp
```

Or in `~/.cline/data/settings/cline_mcp_settings.json` (CLI) / `cline_mcp_settings.json` (**MCP Servers → Configure → Configure MCP Servers** in the IDE extension):

```json
{
  "mcpServers": {
    "fisch": {
      "command": "fisch-mcp",
      "args": [],
      "disabled": false,
      "autoApprove": [
        "get_page",
        "search_wiki",
        "get_page_outline",
        "list_category"
      ]
    }
  }
}
```

---

## Optional Agent Skill (`skills/fischipedia/SKILL.md`)

A portable [`skills/fischipedia/SKILL.md`](skills/fischipedia/SKILL.md) file is included for harnesses that support Agent Skills (`OpenCode`, `Claude Code`, `Codex`, `Antigravity / agy`, `Cursor`, `Cline`):

```bash
# OpenCode V2
mkdir -p ~/.config/opencode/skills && cp -r skills/fischipedia ~/.config/opencode/skills/

# Claude Code
mkdir -p ~/.claude/skills && cp -r skills/fischipedia ~/.claude/skills/

# OpenAI Codex
mkdir -p ~/.agents/skills && cp -r skills/fischipedia ~/.agents/skills/

# Antigravity CLI (agy)
mkdir -p ~/.gemini/antigravity-cli/skills && cp -r skills/fischipedia ~/.gemini/antigravity-cli/skills/
```

---

## Standalone CLI Usage

```bash
# Fetch and compact a page or #Fragment directly to stdout
python3 fischipedia_filter.py "No-Life Rod"
python3 fischipedia_filter.py "Fishing Rods#Stage_8"

# Point at any other MediaWiki installation
MEDIAWIKI_BASE_URL="https://minecraft.wiki" python3 fischipedia_filter.py "Diamond Pickaxe"
```
