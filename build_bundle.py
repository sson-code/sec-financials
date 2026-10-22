#!/usr/bin/env python3
"""Build sec-financials.mcpb — run from the repo root."""
import zipfile
import os

ROOT = os.path.dirname(__file__)
OUT = os.path.join(ROOT, "sec-financials.mcpb")

FILES = {
    "manifest.json":       os.path.join(ROOT, "manifest.json"),
    "requirements.txt":    os.path.join(ROOT, "requirements.txt"),
    "server/sec_mcp.py":   os.path.join(ROOT, "sec_mcp.py"),
    "server/sec_statements.py": os.path.join(ROOT, "sec_statements.py"),
    "server/config.json":  os.path.join(ROOT, "config.json"),
}

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for arc_path, src_path in FILES.items():
        z.write(src_path, arc_path)
        print(f"  added {arc_path}")

print(f"\nBuilt {OUT}")
