# OneLake MCP Server

Read-only [Model Context Protocol](https://modelcontextprotocol.io/) server that lets Cursor explore Microsoft Fabric OneLake lakehouses: schema discovery, cross-source comparison, ingestion monitoring, and data quality analysis.

## Prerequisites

- Python **3.10+** (the `mcp` package does not support Python 3.9)
- Access to a Fabric workspace and lakehouse(s)
- Permission to sign in with Microsoft Entra ID (interactive browser login)

## Quick start

1. Clone this repository and create a virtual environment (see [SETUP.md](SETUP.md) or run `./setup.sh`).
2. Copy the environment template and fill in your Fabric IDs:

   ```bash
   cp .env.example .env
   # Edit .env with your tenant, workspace, and lakehouse GUIDs
   ```

3. Install dependencies and register with Cursor:

   ```bash
   python3.10 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Add the server to `~/.cursor/mcp.json` (paths only — secrets stay in `.env`):

   ```json
   {
     "mcpServers": {
       "onelake": {
         "command": "/path/to/onelake-mcp/.venv/bin/python",
         "args": ["/path/to/onelake-mcp/onelake_mcp.py"]
       }
     }
   }
   ```

5. Reload MCP in Cursor and call `get_active_config` to verify.

**Never commit `.env`** — it is listed in `.gitignore`. Only `.env.example` (placeholders) belongs in git.

## Configuration

All project-specific values live in `.env`. See [.env.example](.env.example) for the full list.

| Variable | Description |
|----------|-------------|
| `AZURE_TENANT_ID` | Microsoft Entra tenant GUID |
| `ONELAKE_WORKSPACE_DEV` | Fabric workspace GUID (dev) |
| `ONELAKE_LAKEHOUSE_LANDING` | Landing (Bronze) lakehouse GUID |
| `ONELAKE_LAKEHOUSE_BASE` | Base (Silver) lakehouse GUID |
| `ONELAKE_LAKEHOUSE_CURATED` | Curated (Gold) lakehouse GUID |
| `ONELAKE_WORKSPACE` | Active profile: `dev`, `tst`, or `prd` |
| `DATA_PRIVACY_MODE` | `strict` (default) or `permissive` |

Find GUIDs in the Fabric portal URL (`app.fabric.microsoft.com`).

## MCP tools

| Tool | Description |
|------|-------------|
| `get_active_config` | Active workspace, lakehouses, privacy settings |
| `list_schemas` | List schemas in a lakehouse |
| `list_tables` | List Delta tables in a schema |
| `get_table_schema` | Column names and types (cached 1h) |
| `get_table_stats` | Row count, file count, size, last modified |
| `get_data_model` | Full workspace map + FK candidates |
| `compare_schemas` | Align two tables for Silver/Gold merges |
| `search_columns` | Find columns by pattern + JOIN inference |
| `find_duplicate_candidates` | Dedup key analysis (no row data) |
| `suggest_silver_transforms` | Bronze → Silver transform suggestions |
| `get_watermark_status` | Ingestion status from watermark tables |
| `validate_notebook` | Validate column refs in `.py` / `.ipynb` / `.sql` |
| `sample_table` | Sample rows (restricted in strict mode) |

Default lakehouse name: **`Landing`**.

## First-run authentication

1. Reload the MCP server in Cursor; any tool call triggers auth if needed.
2. `InteractiveBrowserCredential` opens your browser for Microsoft sign-in.
3. Sign in with an account that has access to the configured Fabric workspace.
4. Tokens are cached at `~/.azure/onelake_mcp_token_cache` (expires after 24h by default).

The server uses the **Azure Storage** scope (`https://storage.azure.com/.default`).

## OneLake layout

Tables are read using lakehouse **GUIDs** (required when friendly names are disabled on the tenant):

```
abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}/Tables/{schema_name}/{table_name}
```

Workspace and lakehouse IDs are set in `.env`, not in source code.

## Error handling

If a table cannot be read, tools return JSON with an `error` field instead of crashing the server.

## Security notes

- **Read-only** — does not write or mutate OneLake data.
- **`.env` is git-ignored** — never commit real GUIDs or credentials.
- Protect `~/.azure/onelake_mcp_token_cache` like any credential store.
- Strict privacy mode blocks `sample_table` on `gold.*` and `silver.*` schemas.

## Further reading

- [SETUP.md](SETUP.md) — step-by-step setup (including Cursor-assisted install)
- [setup.sh](setup.sh) — interactive setup script
