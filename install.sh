#!/usr/bin/env bash
# Automated installer for fisch-mcp and skills/fischipedia/SKILL.md
# Detects installed AI coding harnesses (OpenCode V2, Claude Code, Codex,
# Antigravity Desktop & agy CLI, Cursor IDE, and Cline) on Linux and macOS.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PY="$SCRIPT_DIR/server.py"
SKILL_SRC="$SCRIPT_DIR/skills/fischipedia/SKILL.md"

SERVER_NAME="fisch"
DRY_RUN=0
SKIP_SKILL=0

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Options:
  -n, --name <name>   MCP server name to register (default: fisch)
      --dry-run       Show detected harnesses and planned actions without modifying files
      --skip-skill    Skip installing skills/fischipedia/SKILL.md
  -h, --help          Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--name)
      SERVER_NAME="${2:-}"
      if [[ -z "$SERVER_NAME" ]]; then
        echo "Error: --name requires a non-empty value." >&2
        exit 1
      fi
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --skip-skill)
      SKIP_SKILL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

export PATH="$HOME/.local/bin:$PATH"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 (>=3.10) is required but was not found on PATH." >&2
  exit 1
fi

echo "==> Preparing fisch-mcp (server name: '$SERVER_NAME')..."

MCP_BIN=""
if command -v uv >/dev/null 2>&1; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] Would run: uv tool install --force \"$SCRIPT_DIR\""
    MCP_BIN="fisch-mcp"
  else
    echo "  -> Installing package via uv tool install..."
    uv tool install --force "$SCRIPT_DIR" >/dev/null
    MCP_BIN="$(command -v fisch-mcp || echo "$HOME/.local/bin/fisch-mcp")"
  fi
elif command -v pipx >/dev/null 2>&1; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] Would run: pipx install --force \"$SCRIPT_DIR\""
    MCP_BIN="fisch-mcp"
  else
    echo "  -> Installing package via pipx..."
    pipx install --force "$SCRIPT_DIR" >/dev/null
    MCP_BIN="$(command -v fisch-mcp || echo "$HOME/.local/bin/fisch-mcp")"
  fi
fi

if [[ -n "$MCP_BIN" ]]; then
  STDIO_CMD="$MCP_BIN"
  STDIO_ARGS_JSON="[]"
  CLI_CMD_STR="$MCP_BIN"
else
  STDIO_CMD="python3"
  STDIO_ARGS_JSON="[\"-u\", \"$SERVER_PY\"]"
  CLI_CMD_STR="python3 -u $SERVER_PY"
fi

echo "  -> MCP entrypoint: $CLI_CMD_STR"
echo ""

# Helper: safely upsert mcpServers.<SERVER_NAME> into a JSON config file via python3
upsert_mcp_json() {
  local file_path="$1"
  local include_disabled="$2"
  local include_auto_approve="$3"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "    [dry-run] Would update JSON config: $file_path"
    return 0
  fi

  python3 - "$file_path" "$SERVER_NAME" "$STDIO_CMD" "$STDIO_ARGS_JSON" "$include_disabled" "$include_auto_approve" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
server_name = sys.argv[2]
cmd = sys.argv[3]
args = json.loads(sys.argv[4])
inc_disabled = sys.argv[5] == "1"
inc_auto = sys.argv[6] == "1"

data = {}
if path.exists():
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            data = json.loads(raw)
    except Exception:
        data = {}

if not isinstance(data, dict):
    data = {}

mcp_servers = data.setdefault("mcpServers", {})
if not isinstance(mcp_servers, dict):
    mcp_servers = {}
    data["mcpServers"] = mcp_servers

entry = mcp_servers.get(server_name, {})
if not isinstance(entry, dict):
    entry = {}

entry["command"] = cmd
entry["args"] = args
if inc_disabled and "disabled" not in entry:
    entry["disabled"] = False
if inc_auto:
    entry["autoApprove"] = [
        "get_page",
        "search_wiki",
        "get_page_outline",
        "list_category",
    ]

mcp_servers[server_name] = entry
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
  echo "    ✓ Updated config: $file_path"
}

# Helper: install SKILL.md into a target skills directory
install_skill_to() {
  local target_dir="$1/fischipedia"
  if [[ "$SKIP_SKILL" -eq 1 ]] || [[ ! -f "$SKILL_SRC" ]]; then
    return 0
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "    [dry-run] Would install SKILL.md -> $target_dir/SKILL.md"
    return 0
  fi
  mkdir -p "$target_dir"
  cp "$SKILL_SRC" "$target_dir/SKILL.md"
  echo "    ✓ Installed skill: $target_dir/SKILL.md"
}

# Helper: check if a native (non-/mnt/ Windows WSL-interop) CLI exists on PATH
has_native_cli() {
  local bin_path
  bin_path="$(command -v "$1" 2>/dev/null || true)"
  [[ -n "$bin_path" && "$bin_path" != /mnt/* ]]
}

# Helper: update Codex ~/.codex/config.toml directly via python3
upsert_codex_toml() {
  local toml_path="$HOME/.codex/config.toml"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "    [dry-run] Would update TOML config: $toml_path"
    return 0
  fi
  python3 - "$toml_path" "$SERVER_NAME" "$STDIO_CMD" "$STDIO_ARGS_JSON" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
name = sys.argv[2]
cmd = sys.argv[3]
args = json.loads(sys.argv[4])

path.parent.mkdir(parents=True, exist_ok=True)
existing = path.read_text(encoding="utf-8") if path.exists() else ""
block = f'[mcp_servers.{name}]\ncommand = "{cmd}"\nargs = {json.dumps(args)}\n'
pattern = re.compile(rf"^\[mcp_servers\.{re.escape(name)}\]\n(?:[^\[]*)", re.MULTILINE)
if pattern.search(existing):
    updated = pattern.sub(block + "\n", existing)
else:
    sep = "\n" if existing and not existing.endswith("\n") else ""
    updated = f"{existing}{sep}\n{block}".lstrip("\n")
path.write_text(updated, encoding="utf-8")
PY
  echo "    ✓ Updated config: $toml_path"
}

DETECTED_COUNT=0

# 1. OpenCode V2
OPENCODE_CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
if has_native_cli opencode || [[ -d "$OPENCODE_CFG_DIR" ]]; then
  DETECTED_COUNT=$((DETECTED_COUNT + 1))
  echo "==> [1/6] Detected OpenCode V2"
  if has_native_cli opencode; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "    [dry-run] Would run: opencode mcp add $SERVER_NAME --global -- $CLI_CMD_STR"
    else
      if [[ -n "$MCP_BIN" ]]; then
        opencode mcp add "$SERVER_NAME" --global -- "$MCP_BIN"
      else
        opencode mcp add "$SERVER_NAME" --global -- python3 -u "$SERVER_PY"
      fi
    fi
  fi
  install_skill_to "$OPENCODE_CFG_DIR/skills"
fi

# 2. Claude Code
if has_native_cli claude || [[ -d "$HOME/.claude" ]] || [[ -f "$HOME/.claude.json" ]]; then
  DETECTED_COUNT=$((DETECTED_COUNT + 1))
  echo "==> [2/6] Detected Claude Code"
  if has_native_cli claude; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "    [dry-run] Would run: claude mcp add --transport stdio --scope user $SERVER_NAME -- $CLI_CMD_STR"
    else
      claude mcp remove -s user "$SERVER_NAME" >/dev/null 2>&1 || true
      if [[ -n "$MCP_BIN" ]]; then
        claude mcp add --transport stdio --scope user "$SERVER_NAME" -- "$MCP_BIN" || upsert_mcp_json "$HOME/.claude.json" 0 0
      else
        claude mcp add --transport stdio --scope user "$SERVER_NAME" -- python3 -u "$SERVER_PY" || upsert_mcp_json "$HOME/.claude.json" 0 0
      fi
    fi
  else
    upsert_mcp_json "$HOME/.claude.json" 0 0
  fi
  install_skill_to "$HOME/.claude/skills"
fi

# 3. OpenAI Codex
if has_native_cli codex || [[ -d "$HOME/.codex" ]]; then
  DETECTED_COUNT=$((DETECTED_COUNT + 1))
  echo "==> [3/6] Detected OpenAI Codex"
  if has_native_cli codex; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "    [dry-run] Would run: codex mcp add $SERVER_NAME -- $CLI_CMD_STR"
    else
      codex mcp remove "$SERVER_NAME" >/dev/null 2>&1 || true
      if [[ -n "$MCP_BIN" ]]; then
        codex mcp add "$SERVER_NAME" -- "$MCP_BIN" || upsert_codex_toml
      else
        codex mcp add "$SERVER_NAME" -- python3 -u "$SERVER_PY" || upsert_codex_toml
      fi
    fi
  else
    upsert_codex_toml
  fi
  install_skill_to "$HOME/.agents/skills"
fi

# 4. Google Antigravity (Desktop & agy CLI)
if has_native_cli agy || [[ -d "$HOME/.gemini" ]]; then
  DETECTED_COUNT=$((DETECTED_COUNT + 1))
  echo "==> [4/6] Detected Google Antigravity (Desktop / agy CLI)"
  upsert_mcp_json "$HOME/.gemini/config/mcp_config.json" 1 0
  if [[ -d "$HOME/.gemini/antigravity" ]]; then
    upsert_mcp_json "$HOME/.gemini/antigravity/mcp_config.json" 1 0
    install_skill_to "$HOME/.gemini/antigravity/skills"
  fi
  install_skill_to "$HOME/.gemini/antigravity-cli/skills"
fi

# 5. Cursor IDE
if has_native_cli cursor || [[ -d "$HOME/.cursor" ]]; then
  DETECTED_COUNT=$((DETECTED_COUNT + 1))
  echo "==> [5/6] Detected Cursor IDE"
  upsert_mcp_json "$HOME/.cursor/mcp.json" 0 0
  install_skill_to "$HOME/.cursor/skills"
fi

# 6. Cline (CLI & VS Code Extension)
if [[ "$(uname -s)" == "Darwin" ]]; then
  VSCODE_CLINE_DIR="$HOME/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev"
else
  VSCODE_CLINE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/Code/User/globalStorage/saoudrizwan.claude-dev"
fi

if has_native_cli cline || [[ -d "$HOME/.cline" ]] || [[ -d "$VSCODE_CLINE_DIR" ]]; then
  DETECTED_COUNT=$((DETECTED_COUNT + 1))
  echo "==> [6/6] Detected Cline"
  if has_native_cli cline; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "    [dry-run] Would run: cline mcp add $SERVER_NAME --yes -- $CLI_CMD_STR"
    else
      if [[ -n "$MCP_BIN" ]]; then
        cline mcp add "$SERVER_NAME" --yes -- "$MCP_BIN" || true
      else
        cline mcp add "$SERVER_NAME" --yes -- python3 -u "$SERVER_PY" || true
      fi
    fi
  elif [[ -d "$HOME/.cline" ]]; then
    upsert_mcp_json "$HOME/.cline/data/settings/cline_mcp_settings.json" 1 1
  fi
  if [[ -d "$VSCODE_CLINE_DIR" ]]; then
    upsert_mcp_json "$VSCODE_CLINE_DIR/settings/cline_mcp_settings.json" 1 1
  fi
  install_skill_to "$HOME/.cline/skills"
fi

echo ""
if [[ "$DETECTED_COUNT" -eq 0 ]]; then
  echo "No supported harnesses were auto-detected. Run 'python3 server.py --print-config all' for manual setup."
else
  echo "Done! Configured '$SERVER_NAME' across $DETECTED_COUNT detected harness(es)."
fi
