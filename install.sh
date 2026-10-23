#!/usr/bin/env bash
set -e

# Run from the folder containing this script
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "SEC Financials MCP — installer"
echo "================================"
echo ""

# 1. Install Python dependencies
echo "Installing Python dependencies..."
pip3 install --quiet "mcp<2" pandas requests
echo "Done."

# 2. SEC user agent
echo ""
echo "The SEC requires a name and email to identify API clients."
echo "This is stored locally and never shared."
echo ""
read -p "Your name: " UA_NAME
read -p "Your email: " UA_EMAIL

cat > "$INSTALL_DIR/config.json" <<EOF
{
  "sec_user_agent": "$UA_NAME $UA_EMAIL"
}
EOF
echo "Saved."

# 3. Register with Claude Code (if available)
echo ""
if command -v claude &>/dev/null; then
    claude mcp remove sec-financials 2>/dev/null || true
    claude mcp add sec-financials -- python3 "$INSTALL_DIR/sec_mcp.py"
    echo "Added to Claude Code."
else
    echo "Claude Code not found — skipping. To add manually:"
    echo "  claude mcp add sec-financials -- python3 $INSTALL_DIR/sec_mcp.py"
fi

# 4. Claude Desktop config (optional)
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
if [ -f "$DESKTOP_CONFIG" ]; then
    echo ""
    read -p "Add to Claude Desktop as well? (y/n): " ADD_DESKTOP
    if [[ "$ADD_DESKTOP" =~ ^[Yy]$ ]]; then
        python3 - "$DESKTOP_CONFIG" "$INSTALL_DIR/sec_mcp.py" <<'PYEOF'
import json, sys
config_path, script_path = sys.argv[1], sys.argv[2]
with open(config_path) as f:
    cfg = json.load(f)
cfg.setdefault("mcpServers", {})["sec-financials"] = {
    "command": "python3",
    "args": [script_path]
}
with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2)
print("Added to Claude Desktop — restart Claude Desktop to apply.")
PYEOF
    fi
fi

echo ""
echo "All done. Try: \"Show me AAPL's latest income statement\""
echo ""
