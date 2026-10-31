---
name: fischipedia
description: Look up Roblox Fisch game data (fishing rods, fish, bestiary stats, mutations, enchantments, baits, totems, locations, events, and rod mastery quests) from the Official Fisch Wiki (fischipedia.org) using the local fisch MCP server.
---

# Fischipedia (`fischipedia.org`) MCP Skill

Use the `fisch` (or `fisch-universal`) MCP server tools to retrieve accurate, zero-credit, pre-compacted Markdown from the Official Fisch Wiki (`https://fischipedia.org`).

## Available MCP Tools

1. **`get_page`** (`target`, `keep_wiki_links=false`, `include_images=false`, `include_all_sections=false`, `exclude_sections`)
   - Fetches, parses, and compacts a wiki page or `Page#Fragment` into dense Markdown.
   - **Default behavior**:
     - Includes Infobox stats, `Ability`, `Obtainment`, `Enchanting`, `Mastery`, `Gameplay Notes`, `Trivia`, and compacted `Change History` (with skin-only patches stripped).
     - Excludes `Skins`, `Gallery`, `Catch Quips`, and Bestiary flavor `Description` by default to save context tokens.
     - Strips `https://fischipedia.org/wiki/...` link URLs and `static.wikitide.net` image URLs by default.
   - **Querying large index pages or specific sections (`#Fragment`)**:
     - Pass `Page#Fragment` to extract only matching sections, table rows (preserving table headers), or bullets:
       - `Fishing Rods#Stage_8`
       - `Enchanting#Empyrean`
       - `Titles#The_Ascended`
     - Passing a `#Fragment` for an excluded section (e.g. `No-Life Rod#Skins` or `Onirifalx#Skins`) automatically bypasses the default exclusion and returns that section.
     - Set `include_all_sections: true` if you need all cosmetic/flavor sections on a full page, or `include_images: true` if the user explicitly asks for image/media URLs.

2. **`search_wiki`** (`query`, `limit=10`)
   - Combines MediaWiki prefix `opensearch` and full-text `list=search`.
   - Use this first whenever the exact page title is unknown or when searching for a passive, quest, or mechanic (e.g. `"Absolute Mastery"`).

3. **`get_page_outline`** (`target`)
   - Returns the canonical page title (resolving redirects), categories, and table-of-contents section headings with ready-to-use `Page#Anchor` fragment targets.
   - Use this before querying unfamiliar large pages so you can fetch the exact `Page#Anchor` needed.

4. **`list_category`** (`category`, `filter_text`, `limit=100`)
   - Lists pages and subcategories in a MediaWiki category.
   - Common categories: `Fishing Rods`, `Stage 8 Fishing Rods`, `Mutations`, `Unappraisable Mutations`, `Fish`, `Baits`, `Locations`, `Events`, `Totems`.

## Recommended Workflow

1. **Known item name** (e.g. *"No-Life Rod"*, *"Megalodon"*, *"Onirifalx"*):
   - Call `get_page(target="<Name>")` directly.
2. **Unknown title or sub-mechanic** (e.g. *"What is Absolute Mastery?"*):
   - Call `search_wiki(query="Absolute Mastery")`, inspect the top matching page titles/snippets, and call `get_page` on the relevant page(s).
3. **Comparing a group of items** (e.g. *"Compare all Stage 8 rods"*):
   - Call `get_page(target="Fishing Rods#Stage_8")` or `list_category(category="Stage 8 Fishing Rods")`.
