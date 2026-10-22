#!/bin/bash
# SEC Financials — one-click setup for Claude Desktop

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

echo ""
echo "SEC Financials Setup"
echo "===================="
echo ""

# Install Python dependencies
echo "Installing dependencies..."
pip3 install "mcp<2" pandas requests --quiet
echo "Done."
echo ""

# Create Claude config dir if needed
mkdir -p "$HOME/Library/Application Support/Claude"

# Add or update the sec-financials entry in claude_desktop_config.json
python3 - <<PYEOF
import json, os

config_path = os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
script_dir = "$SCRIPT_DIR"

# Load existing config or start fresh
if os.path.exists(config_path):
    with open(config_path) as f:
        config = json.load(f)
else:
    config = {}

config.setdefault("mcpServers", {})
config["mcpServers"]["sec-financials"] = {
    "command": "python3",
    "args": [os.path.join(script_dir, "sec_mcp.py")]
}

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)

print(f"Configured: {os.path.join(script_dir, 'sec_mcp.py')}")
PYEOF

echo ""
echo "All done! Restart Claude Desktop and you're ready to go."
echo ""
