# SEC Financials MCP — Windows installer
$ErrorActionPreference = "Stop"

$INSTALL_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "SEC Financials MCP — installer"
Write-Host "================================"
Write-Host ""

# 1. Install Python dependencies
Write-Host "Installing Python dependencies..."
pip install --quiet "mcp<2" pandas requests
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: pip not found. Please install Python from https://www.python.org/downloads/"
    Write-Host "Make sure to check 'Add Python to PATH' during installation."
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "Done."

# 2. SEC user agent
Write-Host ""
Write-Host "The SEC requires a name and email to identify API clients."
Write-Host "This is stored locally and never shared."
Write-Host ""
$UA_NAME = Read-Host "Your name"
$UA_EMAIL = Read-Host "Your email"

$config = @{ sec_user_agent = "$UA_NAME $UA_EMAIL" } | ConvertTo-Json
Set-Content -Path "$INSTALL_DIR\config.json" -Value $config
Write-Host "Saved."

# 3. Register with Claude Code (if available)
Write-Host ""
$claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if ($claudeCmd) {
    claude mcp remove sec-financials 2>$null
    claude mcp add sec-financials -- python "$INSTALL_DIR\sec_mcp.py"
    Write-Host "Added to Claude Code."
} else {
    Write-Host "Claude Code not found — skipping. To add manually:"
    Write-Host "  claude mcp add sec-financials -- python `"$INSTALL_DIR\sec_mcp.py`""
}

# 4. Claude Desktop config (optional)
$DESKTOP_CONFIG = "$env:APPDATA\Claude\claude_desktop_config.json"
if (Test-Path $DESKTOP_CONFIG) {
    Write-Host ""
    $addDesktop = Read-Host "Add to Claude Desktop as well? (y/n)"
    if ($addDesktop -match "^[Yy]$") {
        $cfg = Get-Content $DESKTOP_CONFIG | ConvertFrom-Json
        if (-not $cfg.mcpServers) {
            $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue @{}
        }
        $cfg.mcpServers | Add-Member -NotePropertyName "sec-financials" -NotePropertyValue @{
            command = "python"
            args    = @("$INSTALL_DIR\sec_mcp.py")
        } -Force
        $cfg | ConvertTo-Json -Depth 10 | Set-Content $DESKTOP_CONFIG
        Write-Host "Added to Claude Desktop — restart Claude Desktop to apply."
    }
}

Write-Host ""
Write-Host "All done. Try: ""Show me AAPL's latest income statement"""
Write-Host ""
Read-Host "Press Enter to close"
