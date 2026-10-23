#!/usr/bin/env bash
set -e

REPO="https://github.com/sson-code/sec-financials.git"
INSTALL_DIR="$HOME/sec-financials"

echo ""
echo "SEC Financials MCP — installer"
echo "================================"
echo ""

# 1. Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing installation at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "Cloning into $INSTALL_DIR..."
    git clone "$REPO" "$INSTALL_DIR"
fi

# 2. Install Python dependencies
echo ""
echo "Installing Python dependencies..."
pip3 install --quiet "mcp<2" pandas requests
echo "Done."

# 3. SEC user agent
echo ""
echo "The SEC requires a name and email to identify API clients."
echo "This is stored locally in $INSTALL_DIR/config.json and never shared."
echo ""
read -p "Your name: " UA_NAME
read -p "Your email: " UA_EMAIL

cat > "$INSTALL_DIR/config.json" <<EOF
{
  "sec_user_agent": "$UA_NAME $UA_EMAIL"
}
EOF
echo "Saved."

# 4. Register with Claude Code (if available)
echo ""
if command -v claude &>/dev/null; then
    claude mcp remove sec-financials 2>/dev/null || true
    claude mcp add sec-financials -- python3 "$INSTALL_DIR/sec_mcp.py"
    echo "Added to Claude Code."
else
    echo "Claude Code not found — skipping. To add manually:"
    echo "  claude mcp add sec-financials -- python3 $INSTALL_DIR/sec_mcp.py"
fi

# 5. Claude Desktop config (optional)
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
if [ -f "$DESKTOP_CONFIG" ]; then
    echo ""
    read -p "Add to Claude Desktop as well? (y/n): " ADD_DESKTOP
    if [[ "$ADD_DESKTOP" =~ ^[Yy]$ ]]; then
        # Use Python to safely merge into existing JSON
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
