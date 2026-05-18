# OneLake MCP — Setup Guide for Cursor

> **How to use this file with Cursor**
> Open this file in Cursor, then paste the following into the chat:
> ```
> Please follow the instructions in SETUP.md step by step to set up the OneLake MCP server on my machine. Run each command, check the output, and tell me what to fill in when you need information from me.
> ```
> Cursor will read this file, execute the shell commands, detect issues, and guide you through every step.

---

## What you need before starting

| Requirement | How to check |
|---|---|
| Python 3.10 or newer | `python3 --version` |
| Internet access | Required for pip and Azure login |
| Microsoft Entra account | Must have read access to the Fabric workspace |
| Fabric workspace & lakehouse IDs | Found in Fabric portal URLs (see Step 4) |

---

## Step 1 — Find Python 3.10+

Run this to discover all Python versions on your machine:

```bash
which python3.10 python3.11 python3.12 python3.13 2>/dev/null
python3 --version
```

**If Python 3.10+ is missing on macOS**, install it with Homebrew:

```bash
brew install python@3.10
```

**If Python 3.10+ is missing on Windows**, download from https://python.org/downloads and install.

**Expected output:** A path like `/opt/homebrew/bin/python3.10` or `/usr/local/bin/python3.11`.
Save this path — you will need it in Step 2.

---

## Step 2 — Clone or copy the project

If you are setting up on a new machine, clone or copy the `onelake-mcp` folder to your desired location.

Then navigate into it:

```bash
cd ~/Desktop/onelake-mcp   # adjust path as needed
pwd                         # confirm you are in the right folder
ls                          # should show onelake_mcp.py, requirements.txt, etc.
```

---

## Step 3 — Create the virtual environment

Replace `/opt/homebrew/bin/python3.10` with the Python 3.10+ path found in Step 1.

```bash
# macOS / Linux
/opt/homebrew/bin/python3.10 -m venv .venv
source .venv/bin/activate
python --version            # must show 3.10 or newer

# Windows (PowerShell)
# C:\Python310\python.exe -m venv .venv
# .venv\Scripts\Activate.ps1
# python --version
```

---

## Step 4 — Get your Fabric IDs

You need four GUIDs from the Microsoft Fabric portal. Open https://app.fabric.microsoft.com in your browser.

### Tenant ID
1. Click your profile icon (top-right corner)
2. Click **My account** → find "Tenant ID" on the account page
   — OR —
   Go to https://portal.azure.com → Azure Active Directory → Overview → "Tenant ID"

### Workspace ID
1. In Fabric portal, open your workspace (e.g. "dev" or "onelake-dev")
2. Look at the URL: `https://app.fabric.microsoft.com/groups/<WORKSPACE_ID>/...`
3. Copy the GUID after `/groups/`

### Lakehouse IDs
For **each** lakehouse (Landing / Base / Curated):
1. In your workspace, click on the lakehouse name
2. Look at the URL: `.../lakehouses/<LAKEHOUSE_ID>?...`
3. Copy the GUID after `/lakehouses/`

Fill in the values below — Cursor will use these in the next step:

```
AZURE_TENANT_ID       = ____________________________________
ONELAKE_WORKSPACE_DEV = ____________________________________
ONELAKE_LAKEHOUSE_LANDING  = ____________________________________
ONELAKE_LAKEHOUSE_BASE     = ____________________________________   (leave blank if not yet created)
ONELAKE_LAKEHOUSE_CURATED  = ____________________________________   (leave blank if not yet created)
```

---

## Step 5 — Install dependencies

```bash
# Make sure the venv is active (prompt should show (.venv))
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**Expected output:** All four packages install successfully:
`azure-identity`, `azure-storage-file-datalake`, `deltalake`, `mcp`

**If `mcp` fails with "No matching distribution"**, your Python is older than 3.10.
Go back to Step 3 and use a Python 3.10+ binary.

---

## Step 6 — Verify the server loads

```bash
python -c "
import onelake_mcp as m
tools = [t.name for t in m.mcp._tool_manager.list_tools()]
print('OK — tools:', tools)
print('Lakehouses:', list(m.LAKEHOUSES.keys()))
" 2>/dev/null
```

**Expected output:**
```
OK — tools: ['get_active_config', 'list_schemas', 'list_tables', ...]
Lakehouses: ['Landing', ...]
```

---

## Step 7 — Write the Cursor MCP config

This step writes `~/.cursor/mcp.json` with the IDs from Step 4.
Replace every `<PLACEHOLDER>` with the actual GUID before running.

```bash
# Get the absolute Python path for this venv
PYTHON_PATH="$(pwd)/.venv/bin/python"
SERVER_PATH="$(pwd)/onelake_mcp.py"
echo "Python : $PYTHON_PATH"
echo "Server : $SERVER_PATH"
```

Then create (or update) `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "onelake": {
      "command": "<FULL_PATH_TO_VENV_PYTHON>",
      "args": ["<FULL_PATH_TO_onelake_mcp.py>"],
      "env": {
        "AZURE_TENANT_ID":             "<YOUR_TENANT_ID>",
        "DATA_PRIVACY_MODE":           "strict",

        "ONELAKE_WORKSPACE":           "dev",
        "ONELAKE_WORKSPACE_DEV":       "<YOUR_WORKSPACE_ID>",
        "ONELAKE_WORKSPACE_TST":       "",
        "ONELAKE_WORKSPACE_PRD":       "",

        "ONELAKE_LAKEHOUSE_LANDING":   "<YOUR_LANDING_LAKEHOUSE_ID>",
        "ONELAKE_LAKEHOUSE_BASE":      "<YOUR_BASE_LAKEHOUSE_ID_OR_BLANK>",
        "ONELAKE_LAKEHOUSE_CURATED":   "<YOUR_CURATED_LAKEHOUSE_ID_OR_BLANK>"
      }
    }
  }
}
```

**Tip:** Cursor can write this file for you. Just say:
> "Write my mcp.json using these IDs: tenant=XXX, workspace=XXX, landing=XXX"

---

## Step 8 — Reload MCP in Cursor

1. Open Cursor Settings (`Cmd+,` on macOS, `Ctrl+,` on Windows)
2. Search for **MCP** → click **Reload** next to the `onelake` server
   — OR —
   Fully restart Cursor

3. Confirm the server is listed as **running** in the MCP panel.

If it shows an error, check:
- Python path in `mcp.json` is absolute and points to the `.venv` Python
- Server path in `mcp.json` is absolute and correct
- All GUIDs are filled in (no `<PLACEHOLDER>` remaining)

---

## Step 9 — First authentication

1. In Cursor chat, type:
   ```
   Call get_active_config to confirm the server is connected
   ```
2. The server will open your **default browser** for Microsoft Entra sign-in
3. Sign in with the account that has access to the Fabric workspace
4. The browser tab will close automatically after sign-in
5. The tool call completes and returns workspace/lakehouse info

**Token cache location:** `~/.azure/onelake_mcp_token_cache`
Tokens expire and are evicted after 24 hours (configurable via `TOKEN_CACHE_MAX_AGE_HOURS`).

---

## Step 10 — Smoke test

Paste these into Cursor chat one at a time to confirm everything works:

```
Call list_schemas with lakehouse_name="Landing"
```

```
Call get_watermark_status to check ingestion health
```

```
Call get_data_model to see the full workspace overview
```

**Expected:** JSON responses with schema names, table counts, and watermark timestamps.

---

## Troubleshooting

### `FriendlyNameSupportDisabled`
Your tenant requires lakehouse GUIDs, not names like `Landing.Lakehouse`.
The server handles this automatically — confirm `ONELAKE_LAKEHOUSE_LANDING` is set to the GUID, not the friendly name.

### `ModuleNotFoundError: No module named 'mcp'`
Python version is below 3.10. Re-run Step 3 with a Python 3.10+ binary.

### `WorkspaceNotFound` or `401 Unauthorized`
Either the workspace GUID is wrong, or your account does not have access.
Re-check the GUID from the Fabric URL and confirm your account has at least Viewer role on the workspace.

### Browser does not open during auth
Run this in terminal to pre-authenticate:
```bash
cd ~/Desktop/onelake-mcp
source .venv/bin/activate
python -c "
from azure.identity import InteractiveBrowserCredential
cred = InteractiveBrowserCredential(tenant_id='<YOUR_TENANT_ID>')
token = cred.get_token('https://storage.azure.com/.default')
print('Auth OK — token expires:', token.expires_on)
"
```

### Health check fails on startup
Check `~/.azure/onelake_mcp_access.log` for recent error entries.
Also check Cursor's MCP output panel (Settings → MCP → click the server name).

### Token cache too old / forced re-auth
```bash
rm ~/.azure/onelake_mcp_token_cache
rm ~/.azure/onelake_mcp_token_cache.cae
```
The next tool call will open the browser for fresh login.

---

## Adding more lakehouses later

Once Base (Silver) and Curated (Gold) lakehouses are created in Fabric:

1. Find each lakehouse GUID from the Fabric URL (same method as Step 4)
2. Edit `~/.cursor/mcp.json` and fill in:
   ```json
   "ONELAKE_LAKEHOUSE_BASE":    "<BASE_GUID>",
   "ONELAKE_LAKEHOUSE_CURATED": "<CURATED_GUID>"
   ```
3. Reload the MCP server in Cursor

No code changes needed — the server picks up new lakehouses automatically from env vars.

---

## Adding a second environment (tst / prd)

1. Add the workspace GUID to `mcp.json`:
   ```json
   "ONELAKE_WORKSPACE_TST": "<TST_WORKSPACE_GUID>"
   ```
2. To switch to tst, change:
   ```json
   "ONELAKE_WORKSPACE": "tst"
   ```
3. Reload the server. Run `get_active_config` to confirm.

---

## Files created by this setup

| Path | Purpose |
|---|---|
| `<project>/onelake_mcp.py` | The MCP server |
| `<project>/.venv/` | Python virtual environment |
| `<project>/requirements.txt` | Pinned dependencies |
| `<project>/.cursor/rules/onelake-privacy.mdc` | Always-on privacy rule for Cursor |
| `~/.cursor/mcp.json` | Registers server with Cursor |
| `~/.azure/onelake_mcp_token_cache` | Entra auth token cache |
| `~/.azure/onelake_mcp_access.log` | Audit log (tool calls, no data values) |
| `~/.azure/onelake_mcp_schema_cache/` | Local schema cache (1h TTL) |

---

## Quick reference — available tools

| Tool | Arguments | Use for |
|---|---|---|
| `get_active_config` | — | Check workspace + lakehouses |
| `list_schemas` | `lakehouse_name` | Discover schemas |
| `list_tables` | `lakehouse_name`, `schema_name` | Discover tables |
| `get_table_schema` | `lakehouse_name`, `schema_name`, `table_name` | Column names + types |
| `get_table_stats` | `lakehouse_name`, `schema_name`, `table_name` | Row count, size, last modified |
| `sample_table` | `lakehouse_name`, `schema_name`, `table_name`, `n_rows` | Sample rows (restricted) |
| `search_columns` | `column_name_pattern` | Find columns + FK inference |
| `compare_schemas` | two sets of `lakehouse/schema/table` | Align Bronze sources for Silver merge |
| `find_duplicate_candidates` | `lakehouse_name`, `schema_name`, `table_name` | Dedup key analysis |
| `suggest_silver_transforms` | `lakehouse_name`, `schema_name`, `table_name` | Bronze → Silver transform suggestions |
| `get_data_model` | — | Full workspace map |
| `get_watermark_status` | — | Ingestion health check |
| `validate_notebook` | `notebook_path`, `lakehouse_name`, `schema_name`, `table_name` | Validate column refs in a notebook |
