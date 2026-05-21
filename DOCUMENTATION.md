# OneLake MCP — Complete Documentation

**Repository:** https://github.com/Inoj-Hettiarachchi/onelake-mcp

A read-only [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that connects **Cursor AI** to **Microsoft Fabric OneLake**. It lets you explore lakehouse schemas, compare Bronze sources for Silver/Gold modelling, monitor ingestion, validate notebooks, and analyse data quality — without writing ad-hoc scripts or opening the Fabric portal for every question.

---

## Table of contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Repository structure](#3-repository-structure)
4. [Prerequisites](#4-prerequisites)
5. [Installation](#5-installation)
6. [Configuration](#6-configuration)
7. [Cursor integration](#7-cursor-integration)
8. [Authentication](#8-authentication)
9. [OneLake paths and lakehouses](#9-onelake-paths-and-lakehouses)
10. [MCP tools reference](#10-mcp-tools-reference)
11. [Recommended workflows](#11-recommended-workflows)
12. [Privacy, GDPR, and audit](#12-privacy-gdpr-and-audit)
13. [Caching and performance](#13-caching-and-performance)
14. [Reusing in other Fabric projects](#14-reusing-in-other-fabric-projects)
15. [Troubleshooting](#15-troubleshooting)
16. [Security checklist for git](#16-security-checklist-for-git)

---

## 1. Overview

### What it does

| Capability | How |
|------------|-----|
| Discover schemas and tables | ADLS Gen2 API against OneLake |
| Read Delta table metadata | `deltalake` (schema, stats, file actions) |
| Compare schemas across sources | Side-by-side column alignment |
| Suggest Silver transforms | Heuristics on types and null stats |
| Monitor ingestion | Watermark / file-registry tables |
| Validate notebooks | Static analysis of column references |
| Sample rows (restricted) | SQL via DataFusion, with PII redaction |

### What it does **not** do

- Write, update, or delete data in OneLake
- Run Fabric pipelines or Spark jobs
- Replace Fabric notebooks for production transforms

### Medallion layout (typical)

| Lakehouse (friendly name) | Layer | Role |
|---------------------------|-------|------|
| `Landing` | Bronze | Raw ingest (Zuora, Banqsoft, BusinessNXT, etc.) |
| `Base` | Silver | Cleaned, conformed (`silver.*`) |
| `Curated` | Gold | Reporting (`gold.*`) |

Configure each lakehouse with a GUID in `.env` (see [Configuration](#6-configuration)).

---

## 2. Architecture

```
┌─────────────────┐     stdio (JSON-RPC)     ┌──────────────────┐
│  Cursor IDE     │ ◄──────────────────────► │  onelake_mcp.py  │
│  (MCP client)   │                          │  (FastMCP)       │
└─────────────────┘                          └────────┬─────────┘
                                                      │
                    ┌─────────────────────────────────┼─────────────────────────────────┐
                    │                                 │                                 │
                    ▼                                 ▼                                 ▼
           ┌────────────────┐              ┌─────────────────┐              ┌─────────────────┐
           │ azure-identity │              │ azure-storage-  │              │ deltalake       │
           │ (Entra ID      │              │ file-datalake   │              │ (Delta log +    │
           │  browser auth) │              │ (list paths)    │              │  queries)       │
           └────────┬───────┘              └────────┬────────┘              └────────┬────────┘
                    │                                 │                                 │
                    └─────────────────────────────────┼─────────────────────────────────┘
                                                      ▼
                                    ┌─────────────────────────────────────┐
                                    │  Microsoft Fabric OneLake           │
                                    │  onelake.dfs.fabric.microsoft.com   │
                                    └─────────────────────────────────────┘
```

**Startup sequence** (every time Cursor launches the server):

1. Load `.env` (secrets and GUIDs)
2. Expire token cache if older than 24 hours (configurable)
3. Health check — list `Tables/` on first configured lakehouse
4. Start MCP stdio transport and register 13 tools

---

## 3. Repository structure

| Path | Purpose |
|------|---------|
| `onelake_mcp.py` | MCP server (all tools and logic) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for secrets (safe to commit) |
| `.env` | **Your** secrets (git-ignored) |
| `.gitignore` | Excludes `.env`, `.venv`, caches |
| `.cursor/rules/onelake-privacy.mdc` | Always-on Cursor AI privacy rules |
| `README.md` | Quick start |
| `SETUP.md` | Step-by-step setup (Cursor-assisted) |
| `setup.sh` | Interactive installer |
| `DOCUMENTATION.md` | This file |

**Local files (not in repo):**

| Path | Purpose |
|------|---------|
| `~/.cursor/mcp.json` | Registers server with Cursor |
| `~/.azure/onelake_mcp_token_cache` | Entra ID token cache |
| `~/.azure/onelake_mcp_access.log` | Audit log (tool calls, no data values) |
| `~/.azure/onelake_mcp_schema_cache/` | Cached `get_table_schema` results |

---

## 4. Prerequisites

- **Python 3.10+** (`mcp` does not support 3.9)
- **Microsoft Entra ID** account with read access to the Fabric workspace
- **Fabric workspace** and **lakehouse** GUIDs (from portal URLs)
- **Cursor** with MCP support

---

## 5. Installation

### Option A — Automated (`setup.sh`)

```bash
cd /path/to/onelake-mcp
chmod +x setup.sh
./setup.sh
```

Prompts for GUIDs and writes `~/.cursor/mcp.json`.

### Option B — Manual

```bash
cd /path/to/onelake-mcp
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your GUIDs
python -c "import onelake_mcp; print('ok')"
```

### Option C — Cursor-assisted

Open `SETUP.md` in Cursor and ask it to follow the steps.

---

## 6. Configuration

All secrets and environment-specific IDs live in **`.env`**. Copy from `.env.example`:

```bash
cp .env.example .env
```

### Required variables

| Variable | Description | Where to find |
|----------|-------------|---------------|
| `AZURE_TENANT_ID` | Entra tenant GUID | Azure Portal → Entra ID → Overview |
| `ONELAKE_WORKSPACE_DEV` | Fabric workspace GUID | URL: `.../groups/<GUID>/...` |
| `ONELAKE_LAKEHOUSE_LANDING` | Bronze lakehouse GUID | URL: `.../lakehouses/<GUID>?...` |

### Optional variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ONELAKE_WORKSPACE` | `dev` | Active profile: `dev`, `tst`, or `prd` |
| `ONELAKE_WORKSPACE_TST` | — | Test workspace GUID |
| `ONELAKE_WORKSPACE_PRD` | — | Production workspace GUID |
| `ONELAKE_LAKEHOUSE_BASE` | — | Silver lakehouse GUID |
| `ONELAKE_LAKEHOUSE_CURATED` | — | Gold lakehouse GUID |
| `ONELAKE_LAKEHOUSES_EXTRA` | — | `Name:guid,Name2:guid2` |
| `ONELAKE_SCHEMA_CONTEXT_JSON` | — | Extra schema descriptions (JSON object) |
| `ONELAKE_WATERMARK_TABLES_JSON` | — | Extra watermark tables (JSON array) |
| `DATA_PRIVACY_MODE` | `strict` | `strict` or `permissive` |
| `TOKEN_CACHE_MAX_AGE_HOURS` | `24` | Force re-auth after N hours |
| `SCHEMA_CACHE_TTL_SECONDS` | `3600` | Schema cache TTL (1 hour) |
| `ONELAKE_ACCOUNT_URL` | OneLake DFS URL | Rarely changed |

### Switching workspace (dev / tst / prd)

1. Set `ONELAKE_WORKSPACE=tst` (and `ONELAKE_WORKSPACE_TST=<guid>`) in `.env`
2. Reload the MCP server in Cursor

Or pass env vars in `mcp.json` (paths only recommended; keep secrets in `.env`).

### Built-in schema context

Shown in `list_schemas` to help Cursor understand your data:

| Schema | Annotation |
|--------|------------|
| `zuora` | Bronze, Zuora billing, Amili AS |
| `banqsoft` | Bronze, Banqsoft collection, Amili Collection AS |
| `businessnxt` | Bronze, BusinessNXT ERP |
| `GoogleSheets` | Bronze, manual imports |
| `dbo` | Bronze, legacy |
| `silver` | Silver, `_company` for multi-source joins |
| `gold` | Gold, surrogate keys on dimensions |

Extend via `ONELAKE_SCHEMA_CONTEXT_JSON`.

---

## 7. Cursor integration

Edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "onelake": {
      "command": "/absolute/path/to/onelake-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/onelake-mcp/onelake_mcp.py"]
    }
  }
}
```

**Do not** put secrets in `mcp.json` — use `.env` only.

Reload: **Cursor Settings → MCP → Reload**, or restart Cursor.

Verify in chat:

```
Call get_active_config
```

---

## 8. Authentication

### Method

- **InteractiveBrowserCredential** (Microsoft Entra ID)
- Opens the default browser on first use (or after cache expiry)
- Scope: `https://storage.azure.com/.default` (OneLake data plane)

### Token cache

| File | Purpose |
|------|---------|
| `~/.azure/onelake_mcp_token_cache` | MSAL token cache |
| `~/.azure/onelake_mcp_token_cache.cae` | Continuous access evaluation cache |

Cache files older than `TOKEN_CACHE_MAX_AGE_HOURS` are deleted on server startup to limit exposure if the machine is compromised.

### Force re-login

```bash
rm ~/.azure/onelake_mcp_token_cache ~/.azure/onelake_mcp_token_cache.cae
```

Then call any MCP tool — browser sign-in will run again.

---

## 9. OneLake paths and lakehouses

### URI pattern (Delta / deltalake)

Many Fabric tenants require **lakehouse GUIDs**, not friendly names:

```
abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}/Tables/{schema_name}/{table_name}
```

### ADLS listing (schemas / tables)

```
{lakehouse_id}/Tables/{schema_name}/{table_name}
```

The server maps friendly names (`Landing`, `Base`, `Curated`) to GUIDs from `.env`.

### Error: `FriendlyNameSupportDisabled`

Use GUIDs in `.env`, not `Landing.Lakehouse` style paths. The server already uses GUIDs when configured correctly.

---

## 10. MCP tools reference

All tools return **JSON strings**. Errors use `{"error": "..."}` instead of crashing the server.

### Discovery

#### `get_active_config`

No parameters. Returns active workspace, lakehouse IDs, privacy settings, and configured workspaces.

**When to use:** First call in every session.

---

#### `list_schemas`

| Parameter | Type | Description |
|-----------|------|-------------|
| `lakehouse_name` | string | e.g. `Landing`, `Base`, `Curated` |

Returns schema names with context annotations.

---

#### `list_tables`

| Parameter | Type | Description |
|-----------|------|-------------|
| `lakehouse_name` | string | Lakehouse friendly name |
| `schema_name` | string | e.g. `zuora`, `silver` |

Returns Delta tables only (directories with `_delta_log`).

---

#### `get_table_schema`

| Parameter | Type | Description |
|-----------|------|-------------|
| `lakehouse_name` | string | |
| `schema_name` | string | |
| `table_name` | string | |

Returns columns (name, type, nullable, metadata), partition columns, table URI. Cached for 1 hour under `~/.azure/onelake_mcp_schema_cache/`.

---

#### `get_table_stats`

| Parameter | Type | Description |
|-----------|------|-------------|
| `lakehouse_name` | string | |
| `schema_name` | string | |
| `table_name` | string | |

Returns approximate row count, file count, size (bytes + human-readable), last modified, latest Delta commit.

---

#### `get_data_model`

No parameters. Scans **all** configured lakehouses: every schema, table list, table counts, and **cross-lakehouse FK candidates** (column names appearing in 3+ tables across 2+ schemas).

**When to use:** Start of a modelling session for full workspace context.

---

### Comparison and search

#### `compare_schemas`

| Parameter | Type | Description |
|-----------|------|-------------|
| `lakehouse_name_1` | string | First table's lakehouse |
| `schema_name_1` | string | |
| `table_name_1` | string | |
| `lakehouse_name_2` | string | Second table's lakehouse |
| `schema_name_2` | string | |
| `table_name_2` | string | |

Returns: matching columns, type mismatches (with CAST suggestions), columns only in table 1 or 2, **alignment score (0–100%)**.

**Example use:** Align `zuora.account` vs `banqsoft.vParty` for `Dim_Client`.

---

#### `search_columns`

| Parameter | Type | Description |
|-----------|------|-------------|
| `column_name_pattern` | string | Regex or glob (`*Key`, `account.*`) |

Returns all matches across lakehouses plus **join_candidates** when the same column name appears in multiple schemas.

---

### Data quality (no row values)

#### `find_duplicate_candidates`

| Parameter | Type | Description |
|-----------|------|-------------|
| `lakehouse_name` | string | |
| `schema_name` | string | |
| `table_name` | string | |

Ranks columns as deduplication keys (name patterns, partitions, types, nullability). Excludes PII-named columns. Safe on `gold` and `silver`.

---

#### `suggest_silver_transforms`

| Parameter | Type | Description |
|-----------|------|-------------|
| `lakehouse_name` | string | Typically `Landing` (Bronze) |
| `schema_name` | string | |
| `table_name` | string | |

Suggests: high-null columns to drop, STRING→date/number casts, nullable merge keys, missing merge key. Uses Delta file statistics only.

---

### Ingestion and validation

#### `get_watermark_status`

No parameters. Reads configured watermark tables (default: `zuora.ingest_watermarks`, `banqsoft._file_registry`). Returns last-ingested timestamps and row counts.

---

#### `validate_notebook`

| Parameter | Type | Description |
|-----------|------|-------------|
| `notebook_path` | string | Absolute path to `.py`, `.ipynb`, or `.sql` |
| `lakehouse_name` | string | |
| `schema_name` | string | Target table for validation |
| `table_name` | string | |

Parses PySpark/pandas/SQL column references and compares to actual schema. Returns valid vs invalid columns with suggestions.

---

### Sampling (restricted)

#### `sample_table`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lakehouse_name` | string | — | |
| `schema_name` | string | — | Blocked: `gold`, `silver` in strict mode |
| `table_name` | string | — | |
| `n_rows` | int | `10` | Max rows returned |

- PII-named columns redacted to `[REDACTED]`
- Response includes `WARNING` field
- Logged to access log

**Safe examples:** `zuora.ingest_watermarks`, `banqsoft._file_registry`

---

## 11. Recommended workflows

### New Cursor session

```
1. get_active_config
2. get_data_model
3. get_watermark_status
```

### Explore a Bronze source

```
1. list_schemas("Landing")
2. list_tables("Landing", "zuora")
3. get_table_schema("Landing", "zuora", "account")
4. get_table_stats("Landing", "zuora", "account")
```

### Build a Silver dimension from two Bronze tables

```
1. compare_schemas("Landing", "zuora", "account", "Landing", "banqsoft", "vParty")
2. suggest_silver_transforms("Landing", "zuora", "account")
3. find_duplicate_candidates("Landing", "zuora", "account")
```

### Before uploading a Fabric notebook

```
validate_notebook("/path/to/notebook.py", "Landing", "silver", "dim_client")
```

### Find join keys

```
search_columns("AccountKey")
```

---

## 12. Privacy, GDPR, and audit

### Legal context

Norwegian B2B customer data in OneLake — **GDPR** and Norwegian privacy law apply.

### Three enforcement layers

| Layer | Mechanism |
|-------|-----------|
| **Server** | Blocks `sample_table` on `gold`/`silver`; redacts PII column values |
| **Cursor rule** | `.cursor/rules/onelake-privacy.mdc` — instructs AI what never to repeat |
| **Audit log** | `~/.azure/onelake_mcp_access.log` — tool, schema, table, user, workspace |

### PII column detection (redaction)

Column names matching (case-insensitive): `name`, `email`, `phone`, `address`, `number`, `ssn`, `organisation`, `organization`, `id`.

### Safe to show in chat

Column names and types, row counts, schema/table names, watermark timestamps, file sizes, dedup key **names**.

### Never show

Customer names, org numbers, account IDs, invoice IDs, financial amounts, or sampled row values from sensitive columns.

### Cursor rule

The project includes an always-on rule so Cursor prefers schema-safe tools and uses placeholders (`AccountId = "XXXX"`) in generated code.

---

## 13. Caching and performance

| Cache | Location | TTL | Invalidation |
|-------|----------|-----|----------------|
| Entra token | `~/.azure/onelake_mcp_token_cache` | 24h default | File mtime on startup |
| Table schema | `~/.azure/onelake_mcp_schema_cache/{workspace}/{lakehouse}/{schema}/{table}.json` | 1h default | TTL expiry |

`get_data_model` can be slow on first run (scans all schemas). Use targeted tools afterward.

---

## 14. Reusing in other Fabric projects

1. Clone the repo
2. Copy `.env.example` → `.env` and fill in **your** GUIDs
3. Optionally customize `SCHEMA_CONTEXT` in code or via `ONELAKE_SCHEMA_CONTEXT_JSON`
4. Add lakehouses with `ONELAKE_LAKEHOUSE_<NAME>` or `ONELAKE_LAKEHOUSES_EXTRA`
5. Point Cursor `mcp.json` at your venv Python and `onelake_mcp.py`

No code changes required if all IDs are in `.env`.

---

## 15. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `No matching distribution found for mcp` | Python &lt; 3.10 | Use `python3.10 -m venv .venv` |
| `FriendlyNameSupportDisabled` | Friendly names in paths | Use lakehouse GUIDs in `.env` |
| `WorkspaceNotFound` | Wrong workspace GUID | Check Fabric URL |
| `401` / auth errors | Expired or wrong account | Delete token cache; sign in again |
| MCP shows **errored** in Cursor | Stale server process | Reload MCP in Cursor |
| `Unsupported Delta table type: 'void'` | Empty/uninitialized Delta table | Ingestion not run for that table |
| Health check fails on startup | Network or config | Check `.env`, VPN, workspace access |
| `ModuleNotFoundError: dotenv` | Missing dependency | `pip install -r requirements.txt` |

### Debug auth manually

```bash
source .venv/bin/activate
python -c "
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path('.env'))
from azure.identity import InteractiveBrowserCredential
import os
cred = InteractiveBrowserCredential(tenant_id=os.environ['AZURE_TENANT_ID'])
t = cred.get_token('https://storage.azure.com/.default')
print('Auth OK')
"
```

### View MCP logs

Cursor: **Settings → MCP → onelake** (click server name for stderr output).

---

## 16. Security checklist for git

Before pushing to GitHub:

- [ ] `.env` is **not** tracked (`git check-ignore -v .env`)
- [ ] No real GUIDs in `README.md`, `SETUP.md`, or code
- [ ] Only `.env.example` has placeholders
- [ ] Token cache and access log are outside the repo
- [ ] Consider **private** repo if internal architecture docs are sensitive

**Dependencies:** `azure-identity`, `azure-storage-file-datalake`, `deltalake`, `mcp`, `python-dotenv`

---

## Quick reference — all tools

| # | Tool | Parameters |
|---|------|------------|
| 1 | `get_active_config` | — |
| 2 | `list_schemas` | `lakehouse_name` |
| 3 | `list_tables` | `lakehouse_name`, `schema_name` |
| 4 | `get_table_schema` | `lakehouse_name`, `schema_name`, `table_name` |
| 5 | `get_table_stats` | `lakehouse_name`, `schema_name`, `table_name` |
| 6 | `get_data_model` | — |
| 7 | `sample_table` | `lakehouse_name`, `schema_name`, `table_name`, `n_rows?` |
| 8 | `search_columns` | `column_name_pattern` |
| 9 | `compare_schemas` | two × (`lakehouse`, `schema`, `table`) |
| 10 | `find_duplicate_candidates` | `lakehouse_name`, `schema_name`, `table_name` |
| 11 | `suggest_silver_transforms` | `lakehouse_name`, `schema_name`, `table_name` |
| 12 | `get_watermark_status` | — |
| 13 | `validate_notebook` | `notebook_path`, `lakehouse_name`, `schema_name`, `table_name` |

---

*Last updated: May 2026 — matches `onelake_mcp.py` with 13 tools and `.env`-based configuration.*
