#!/usr/bin/env bash
# =============================================================================
# OneLake MCP — Automated Setup Script
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh
#
# What this script does:
#   1. Finds Python 3.10+ on your machine
#   2. Creates a .venv virtual environment
#   3. Installs all dependencies
#   4. Prompts you for Fabric IDs (tenant, workspace, lakehouses)
#   5. Writes ~/.cursor/mcp.json with your values
#   6. Verifies the server loads correctly
#
# Prerequisites:
#   - Python 3.10+ installed
#   - Access to Microsoft Fabric (you will need GUIDs from the portal)
#   - Cursor IDE installed
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
info() { echo -e "${CYAN}  → $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $*${NC}"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }
header() { echo -e "\n${CYAN}━━━ $* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# ── Locate project root ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

header "OneLake MCP Setup"
echo "  Project directory: $SCRIPT_DIR"

# ── Step 1: Find Python 3.10+ ─────────────────────────────────────────────────
header "Step 1 — Finding Python 3.10+"

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c "import sys; print(sys.version_info[:2])")
        if [[ "$version" > "(3, 9)" ]]; then
            PYTHON_BIN=$(command -v "$candidate")
            ok "Found: $PYTHON_BIN  ($($PYTHON_BIN --version))"
            break
        fi
    fi
done

# Homebrew fallback (macOS)
if [[ -z "$PYTHON_BIN" ]]; then
    for brew_path in /opt/homebrew/bin /usr/local/bin; do
        for v in 3.13 3.12 3.11 3.10; do
            if [[ -x "$brew_path/python$v" ]]; then
                PYTHON_BIN="$brew_path/python$v"
                ok "Found via Homebrew: $PYTHON_BIN  ($($PYTHON_BIN --version))"
                break 2
            fi
        done
    done
fi

if [[ -z "$PYTHON_BIN" ]]; then
    fail "Python 3.10+ not found. Install with: brew install python@3.10\nThen re-run this script."
fi

# ── Step 2: Create virtual environment ────────────────────────────────────────
header "Step 2 — Creating virtual environment"

if [[ -d ".venv" ]]; then
    VENV_PYTHON=".venv/bin/python"
    VENV_VER=$("$VENV_PYTHON" -c "import sys; print(sys.version_info[:2])" 2>/dev/null || echo "(0, 0)")
    if [[ "$VENV_VER" > "(3, 9)" ]]; then
        ok "Existing .venv is Python 3.10+ ($VENV_VER) — reusing"
    else
        warn "Existing .venv is old ($VENV_VER) — recreating with $PYTHON_BIN"
        rm -rf .venv
        "$PYTHON_BIN" -m venv .venv
        ok ".venv recreated"
    fi
else
    info "Creating .venv with $PYTHON_BIN"
    "$PYTHON_BIN" -m venv .venv
    ok ".venv created"
fi

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
info "Virtual environment Python: $($VENV_PYTHON --version)"

# ── Step 3: Install dependencies ──────────────────────────────────────────────
header "Step 3 — Installing dependencies"

info "Upgrading pip..."
"$VENV_PYTHON" -m pip install --upgrade pip --quiet

info "Installing from requirements.txt..."
"$VENV_PYTHON" -m pip install -r requirements.txt --quiet

# Verify key packages
for pkg in azure.identity azure.storage.filedatalake deltalake mcp; do
    if "$VENV_PYTHON" -c "import $pkg" 2>/dev/null; then
        ok "$pkg"
    else
        fail "$pkg failed to install. Check pip output above."
    fi
done

# ── Step 4: Collect Fabric IDs ────────────────────────────────────────────────
header "Step 4 — Fabric configuration"

echo ""
echo "  You need GUIDs from the Microsoft Fabric portal (https://app.fabric.microsoft.com)"
echo "  Tip: GUIDs look like  xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
echo ""

prompt_guid() {
    local label="$1"
    local hint="$2"
    local value=""
    while [[ -z "$value" || ${#value} -ne 36 ]]; do
        echo -e "  ${CYAN}${label}${NC}"
        echo "  $hint"
        read -rp "  Enter GUID (or press Enter to skip): " value
        if [[ -z "$value" ]]; then
            echo "  (skipped)"
            echo ""
            echo ""
            return 0
        fi
        # Basic GUID format check
        if [[ ! "$value" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
            warn "That doesn't look like a valid GUID. Please try again."
            value=""
        fi
    done
    echo "$value"
}

echo -e "  ${YELLOW}Where to find each ID in Fabric:${NC}"
echo "  • Tenant ID    → Azure Portal → Azure Active Directory → Overview"
echo "  • Workspace ID → Fabric URL: app.fabric.microsoft.com/groups/<WORKSPACE_ID>/..."
echo "  • Lakehouse ID → Fabric URL: .../lakehouses/<LAKEHOUSE_ID>?..."
echo ""

read -rp "  Azure Tenant ID (36-char GUID): " TENANT_ID
while [[ ! "$TENANT_ID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; do
    warn "Invalid format. Tenant ID must be a GUID like xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    read -rp "  Azure Tenant ID: " TENANT_ID
done

read -rp "  Workspace ID (dev environment): " WORKSPACE_DEV
while [[ ! "$WORKSPACE_DEV" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; do
    warn "Invalid format."
    read -rp "  Workspace ID (dev): " WORKSPACE_DEV
done

read -rp "  Landing lakehouse ID: " LAKEHOUSE_LANDING
while [[ ! "$LAKEHOUSE_LANDING" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; do
    warn "Invalid format."
    read -rp "  Landing lakehouse ID: " LAKEHOUSE_LANDING
done

echo ""
echo "  The following are optional — press Enter to skip if not created yet:"
read -rp "  Base lakehouse ID (Silver, optional): " LAKEHOUSE_BASE
read -rp "  Curated lakehouse ID (Gold, optional): " LAKEHOUSE_CURATED
echo ""

ok "Fabric IDs collected"

# ── Step 5: Write mcp.json ────────────────────────────────────────────────────
header "Step 5 — Writing Cursor MCP config"

MCP_CONFIG_DIR="$HOME/.cursor"
MCP_CONFIG_FILE="$MCP_CONFIG_DIR/mcp.json"
mkdir -p "$MCP_CONFIG_DIR"

SERVER_PATH="$SCRIPT_DIR/onelake_mcp.py"

# Merge with existing mcp.json if it already has other servers.
if [[ -f "$MCP_CONFIG_FILE" ]]; then
    info "Existing $MCP_CONFIG_FILE found — backing up to ${MCP_CONFIG_FILE}.bak"
    cp "$MCP_CONFIG_FILE" "${MCP_CONFIG_FILE}.bak"
    # Check if it already has mcpServers with other entries.
    if "$VENV_PYTHON" -c "
import json, sys
with open('$MCP_CONFIG_FILE') as f:
    data = json.load(f)
servers = data.get('mcpServers', {})
others = {k: v for k, v in servers.items() if k != 'onelake'}
print(json.dumps(others))
" 2>/dev/null > /tmp/other_servers.json; then
        OTHER_SERVERS=$(cat /tmp/other_servers.json)
    else
        OTHER_SERVERS="{}"
    fi
else
    OTHER_SERVERS="{}"
fi

# Build the onelake server entry.
ONELAKE_ENTRY=$(cat <<EOF
{
  "command": "$VENV_PYTHON",
  "args": ["$SERVER_PATH"],
  "env": {
    "AZURE_TENANT_ID":            "$TENANT_ID",
    "DATA_PRIVACY_MODE":          "strict",
    "ONELAKE_WORKSPACE":          "dev",
    "ONELAKE_WORKSPACE_DEV":      "$WORKSPACE_DEV",
    "ONELAKE_WORKSPACE_TST":      "",
    "ONELAKE_WORKSPACE_PRD":      "",
    "ONELAKE_LAKEHOUSE_LANDING":  "$LAKEHOUSE_LANDING",
    "ONELAKE_LAKEHOUSE_BASE":     "$LAKEHOUSE_BASE",
    "ONELAKE_LAKEHOUSE_CURATED":  "$LAKEHOUSE_CURATED"
  }
}
EOF
)

# Write final mcp.json (merging other servers if any).
"$VENV_PYTHON" - <<PYEOF
import json

with open('/tmp/other_servers.json') as f:
    others = json.load(f)

onelake = json.loads('''$ONELAKE_ENTRY''')

config = {
    "mcpServers": {
        **others,
        "onelake": onelake
    }
}

with open('$MCP_CONFIG_FILE', 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

print("Written: $MCP_CONFIG_FILE")
PYEOF

ok "mcp.json written: $MCP_CONFIG_FILE"

# ── Step 6: Verify server loads ───────────────────────────────────────────────
header "Step 6 — Verifying server"

ONELAKE_TENANT_ID="$TENANT_ID" \
ONELAKE_WORKSPACE_DEV="$WORKSPACE_DEV" \
ONELAKE_LAKEHOUSE_LANDING="$LAKEHOUSE_LANDING" \
ONELAKE_LAKEHOUSE_BASE="$LAKEHOUSE_BASE" \
ONELAKE_LAKEHOUSE_CURATED="$LAKEHOUSE_CURATED" \
"$VENV_PYTHON" -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
import onelake_mcp as m
tools = [t.name for t in m.mcp._tool_manager.list_tools()]
lakehouses = list(m.LAKEHOUSES.keys())
print('Tools (' + str(len(tools)) + '):', ', '.join(tools))
print('Lakehouses:', lakehouses)
print('Privacy mode:', m.DATA_PRIVACY_MODE)
" 2>/dev/null

ok "Server loads correctly"

# ── Done ──────────────────────────────────────────────────────────────────────
header "Setup complete"

cat <<MSG

  ${GREEN}Everything is configured.${NC}

  Next steps:
  1. Restart Cursor (or Settings → MCP → Reload)
  2. In Cursor chat, type:
       Call get_active_config to confirm the server is connected
  3. Your browser will open for Microsoft sign-in on first use.
     Sign in with an account that has access to the Fabric workspace.
  4. After sign-in, try:
       Call list_schemas with lakehouse_name="Landing"

  ${CYAN}Files created:${NC}
    $MCP_CONFIG_FILE
    $SCRIPT_DIR/.venv/
    ~/.azure/onelake_mcp_token_cache   (created on first sign-in)
    ~/.azure/onelake_mcp_access.log    (created on first tool call)
    ~/.azure/onelake_mcp_schema_cache/ (created on first schema read)

  ${YELLOW}To add Base/Curated lakehouses later:${NC}
    Edit $MCP_CONFIG_FILE
    Set ONELAKE_LAKEHOUSE_BASE and ONELAKE_LAKEHOUSE_CURATED
    Reload the MCP server in Cursor

  ${YELLOW}To switch environment (dev → tst):${NC}
    Edit $MCP_CONFIG_FILE
    Change ONELAKE_WORKSPACE to "tst" and set ONELAKE_WORKSPACE_TST
    Reload the MCP server in Cursor

MSG
