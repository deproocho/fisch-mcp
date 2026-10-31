<#
.SYNOPSIS
    Automated installer for fisch-mcp and skills/fischipedia/SKILL.md on Windows.
.DESCRIPTION
    Detects installed AI coding harnesses (OpenCode V2, Claude Code, OpenAI Codex,
    Google Antigravity Desktop & agy CLI, Cursor IDE, and Cline) and registers the
    fisch-mcp stdio server and SKILL.md automatically.
.PARAMETER Name
    MCP server name to register (default: "fisch").
.PARAMETER DryRun
    Preview detected harnesses and actions without modifying files.
.PARAMETER SkipSkill
    Skip installing skills/fischipedia/SKILL.md.
#>
[CmdletBinding()]
param(
    [string]$Name = "fisch",
    [switch]$DryRun,
    [switch]$SkipSkill
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ServerPy  = Join-Path $ScriptDir "server.py"
$SkillSrc  = Join-Path $ScriptDir "skills\fischipedia\SKILL.md"

$UserBin = Join-Path $env:USERPROFILE ".local\bin"
if ((Test-Path $UserBin) -and ($env:PATH -notlike "*$UserBin*")) {
    $env:PATH = "$UserBin;$env:PATH"
}

$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    $PythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $PythonCmd) {
    Write-Error "Python 3.10+ ('python' or 'python3') is required on PATH."
    exit 1
}
$PyExe = $PythonCmd.Source

Write-Host "==> Preparing fisch-mcp (server name: '$Name')..." -ForegroundColor Cyan

$McpBin = $null
if (Get-Command uv -ErrorAction SilentlyContinue) {
    if ($DryRun) {
        Write-Host "  [dry-run] Would run: uv tool install --force `"$ScriptDir`""
        $McpBin = "fisch-mcp"
    } else {
        Write-Host "  -> Installing package via uv tool install..."
        uv tool install --force "$ScriptDir" | Out-Null
        $Resolved = Get-Command fisch-mcp -ErrorAction SilentlyContinue
        $McpBin = if ($Resolved) { $Resolved.Source } else { Join-Path $UserBin "fisch-mcp.exe" }
    }
} elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
    if ($DryRun) {
        Write-Host "  [dry-run] Would run: pipx install --force `"$ScriptDir`""
        $McpBin = "fisch-mcp"
    } else {
        Write-Host "  -> Installing package via pipx..."
        pipx install --force "$ScriptDir" | Out-Null
        $Resolved = Get-Command fisch-mcp -ErrorAction SilentlyContinue
        $McpBin = if ($Resolved) { $Resolved.Source } else { Join-Path $UserBin "fisch-mcp.exe" }
    }
}

if ($McpBin) {
    $StdioCmd = $McpBin
    $StdioArgsJson = "[]"
    $CliCmdStr = $McpBin
} else {
    $StdioCmd = $PyExe
    $EscapedPy = $ServerPy.Replace("\", "\\")
    $StdioArgsJson = "[`"-u`", `"$EscapedPy`"]"
    $CliCmdStr = "$PyExe -u `"$ServerPy`""
}

Write-Host "  -> MCP entrypoint: $CliCmdStr`n"

function Update-McpJson {
    param(
        [string]$FilePath,
        [bool]$IncludeDisabled = $false,
        [bool]$IncludeAutoApprove = $false
    )
    if ($DryRun) {
        Write-Host "    [dry-run] Would update JSON config: $FilePath"
        return
    }
    $IncDis = if ($IncludeDisabled) { "1" } else { "0" }
    $IncAuto = if ($IncludeAutoApprove) { "1" } else { "0" }

    $PyCode = @'
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
'@
    & $PyExe -c $PyCode $FilePath $Name $StdioCmd $StdioArgsJson $IncDis $IncAuto
    Write-Host "    [OK] Updated config: $FilePath" -ForegroundColor Green
}

function Install-SkillTo {
    param([string]$SkillsBaseDir)
    if ($SkipSkill -or -not (Test-Path $SkillSrc)) { return }
    $TargetDir = Join-Path $SkillsBaseDir "fischipedia"
    $TargetFile = Join-Path $TargetDir "SKILL.md"
    if ($DryRun) {
        Write-Host "    [dry-run] Would install SKILL.md -> $TargetFile"
        return
    }
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
    Copy-Item -Force -Path $SkillSrc -Destination $TargetFile
    Write-Host "    [OK] Installed skill: $TargetFile" -ForegroundColor Green
}

$DetectedCount = 0

# 1. OpenCode V2
$OpenCodeDir = Join-Path $env:USERPROFILE ".config\opencode"
if ((Get-Command opencode -ErrorAction SilentlyContinue) -or (Test-Path $OpenCodeDir)) {
    $DetectedCount++
    Write-Host "==> [1/6] Detected OpenCode V2" -ForegroundColor Cyan
    if (Get-Command opencode -ErrorAction SilentlyContinue) {
        if ($DryRun) {
            Write-Host "    [dry-run] Would run: opencode mcp add $Name --global -- $CliCmdStr"
        } else {
            if ($McpBin) {
                opencode mcp add $Name --global -- $McpBin
            } else {
                opencode mcp add $Name --global -- $PyExe -u $ServerPy
            }
        }
    }
    Install-SkillTo (Join-Path $OpenCodeDir "skills")
}

# 2. Claude Code
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$ClaudeJson = Join-Path $env:USERPROFILE ".claude.json"
if ((Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path $ClaudeDir) -or (Test-Path $ClaudeJson)) {
    $DetectedCount++
    Write-Host "==> [2/6] Detected Claude Code" -ForegroundColor Cyan
    if (Get-Command claude -ErrorAction SilentlyContinue) {
        if ($DryRun) {
            Write-Host "    [dry-run] Would run: claude mcp add --transport stdio --scope user $Name -- $CliCmdStr"
        } else {
            try { claude mcp remove -s user $Name 2>$null | Out-Null } catch {}
            if ($McpBin) {
                claude mcp add --transport stdio --scope user $Name -- $McpBin
            } else {
                claude mcp add --transport stdio --scope user $Name -- $PyExe -u $ServerPy
            }
        }
    } else {
        Update-McpJson -FilePath $ClaudeJson
    }
    Install-SkillTo (Join-Path $ClaudeDir "skills")
}

# 3. OpenAI Codex
$CodexDir = Join-Path $env:USERPROFILE ".codex"
if ((Get-Command codex -ErrorAction SilentlyContinue) -or (Test-Path $CodexDir)) {
    $DetectedCount++
    Write-Host "==> [3/6] Detected OpenAI Codex" -ForegroundColor Cyan
    if (Get-Command codex -ErrorAction SilentlyContinue) {
        if ($DryRun) {
            Write-Host "    [dry-run] Would run: codex mcp add $Name -- $CliCmdStr"
        } else {
            try { codex mcp remove $Name 2>$null | Out-Null } catch {}
            if ($McpBin) {
                codex mcp add $Name -- $McpBin
            } else {
                codex mcp add $Name -- $PyExe -u $ServerPy
            }
        }
    } else {
        $CodexToml = Join-Path $CodexDir "config.toml"
        if ($DryRun) {
            Write-Host "    [dry-run] Would update TOML config: $CodexToml"
        } else {
            $PyToml = @'
import json, re, sys
from pathlib import Path
path = Path(sys.argv[1]).expanduser()
name, cmd, args = sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
path.parent.mkdir(parents=True, exist_ok=True)
existing = path.read_text(encoding="utf-8") if path.exists() else ""
block = f'[mcp_servers.{name}]\ncommand = {json.dumps(cmd)}\nargs = {json.dumps(args)}\n'
pattern = re.compile(rf"^\[mcp_servers\.{re.escape(name)}\]\n(?:[^\[]*)", re.MULTILINE)
updated = pattern.sub(block + "\n", existing) if pattern.search(existing) else f"{existing}\n\n{block}".lstrip("\n")
path.write_text(updated, encoding="utf-8")
'@
            & $PyExe -c $PyToml $CodexToml $Name $StdioCmd $StdioArgsJson
            Write-Host "    [OK] Updated config: $CodexToml" -ForegroundColor Green
        }
    }
    Install-SkillTo (Join-Path $env:USERPROFILE ".agents\skills")
}

# 4. Google Antigravity (Desktop & agy CLI)
$GeminiDir = Join-Path $env:USERPROFILE ".gemini"
if ((Get-Command agy -ErrorAction SilentlyContinue) -or (Test-Path $GeminiDir)) {
    $DetectedCount++
    Write-Host "==> [4/6] Detected Google Antigravity (Desktop / agy CLI)" -ForegroundColor Cyan
    Update-McpJson -FilePath (Join-Path $GeminiDir "config\mcp_config.json") -IncludeDisabled $true
    $AgyDesktopDir = Join-Path $GeminiDir "antigravity"
    if (Test-Path $AgyDesktopDir) {
        Update-McpJson -FilePath (Join-Path $AgyDesktopDir "mcp_config.json") -IncludeDisabled $true
        Install-SkillTo (Join-Path $AgyDesktopDir "skills")
    }
    Install-SkillTo (Join-Path $GeminiDir "antigravity-cli\skills")
}

# 5. Cursor IDE
$CursorDir = Join-Path $env:USERPROFILE ".cursor"
if ((Get-Command cursor -ErrorAction SilentlyContinue) -or (Test-Path $CursorDir)) {
    $DetectedCount++
    Write-Host "==> [5/6] Detected Cursor IDE" -ForegroundColor Cyan
    Update-McpJson -FilePath (Join-Path $CursorDir "mcp.json")
    Install-SkillTo (Join-Path $CursorDir "skills")
}

# 6. Cline (CLI & VS Code Extension)
$ClineDir = Join-Path $env:USERPROFILE ".cline"
$VsCodeClineDir = Join-Path $env:APPDATA "Code\User\globalStorage\saoudrizwan.claude-dev"
if ((Get-Command cline -ErrorAction SilentlyContinue) -or (Test-Path $ClineDir) -or (Test-Path $VsCodeClineDir)) {
    $DetectedCount++
    Write-Host "==> [6/6] Detected Cline" -ForegroundColor Cyan
    if (Get-Command cline -ErrorAction SilentlyContinue) {
        if ($DryRun) {
            Write-Host "    [dry-run] Would run: cline mcp add $Name --yes -- $CliCmdStr"
        } else {
            if ($McpBin) {
                cline mcp add $Name --yes -- $McpBin
            } else {
                cline mcp add $Name --yes -- $PyExe -u $ServerPy
            }
        }
    } elseif (Test-Path $ClineDir) {
        Update-McpJson -FilePath (Join-Path $ClineDir "data\settings\cline_mcp_settings.json") -IncludeDisabled $true -IncludeAutoApprove $true
    }
    if (Test-Path $VsCodeClineDir) {
        Update-McpJson -FilePath (Join-Path $VsCodeClineDir "settings\cline_mcp_settings.json") -IncludeDisabled $true -IncludeAutoApprove $true
    }
    Install-SkillTo (Join-Path $ClineDir "skills")
}

Write-Host ""
if ($DetectedCount -eq 0) {
    Write-Host "No supported harnesses were auto-detected. Run 'python server.py --print-config all' for manual setup." -ForegroundColor Yellow
} else {
    Write-Host "Done! Configured '$Name' across $DetectedCount detected harness(es)." -ForegroundColor Green
}
