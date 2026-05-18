#!/usr/bin/env python3
"""
OneLake MCP Server — Read-only access to Microsoft Fabric OneLake.

Gives Cursor AI deep visibility into OneLake lakehouses for schema discovery,
data quality analysis, ingestion monitoring, and cross-source comparison.

REUSABILITY
-----------
Every project-specific value is driven by environment variables so this
server can be dropped into any Fabric project without code changes.

SECRETS
-------
All GUIDs and credentials are read from a .env file in the same directory
as this script. Copy .env.example → .env and fill in your values.
The .env file is git-ignored. Never hardcode secrets in this file.

Required vars (in .env or environment):
  AZURE_TENANT_ID              Entra tenant
  ONELAKE_WORKSPACE_DEV        Workspace GUID for dev environment
  ONELAKE_LAKEHOUSE_LANDING    Lakehouse GUID for Landing (Bronze)
  ONELAKE_LAKEHOUSE_BASE       Lakehouse GUID for Base (Silver)
  ONELAKE_LAKEHOUSE_CURATED    Lakehouse GUID for Curated (Gold)

Optional env vars:
  ONELAKE_WORKSPACE            Active profile: dev | tst | prd  (default: dev)
  ONELAKE_WORKSPACE_TST        Workspace GUID for tst
  ONELAKE_WORKSPACE_PRD        Workspace GUID for prd
  ONELAKE_LAKEHOUSES_EXTRA     Extra lakehouses: "Name:guid,Name2:guid2"
  ONELAKE_SCHEMA_CONTEXT_JSON  Extra schema annotations JSON object
  DATA_PRIVACY_MODE            strict | permissive  (default: strict)
  TOKEN_CACHE_MAX_AGE_HOURS    Hours before token cache is expired (default: 24)
  SCHEMA_CACHE_TTL_SECONDS     Seconds before schema cache is refreshed (default: 3600)
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from azure.identity import InteractiveBrowserCredential
from azure.storage.filedatalake import DataLakeServiceClient
from deltalake import DeltaTable, QueryBuilder
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from msal_extensions import FilePersistence, PersistedTokenCache

# Load .env from the same directory as this script.
# Existing environment variables (e.g. from mcp.json env block) take priority.
load_dotenv(Path(__file__).parent / ".env", override=False)


# ─── CONFIGURATION ────────────────────────────────────────────────────────────
# All secrets come from .env (or environment variables set by the caller).
# No GUIDs or credentials are hardcoded here.
# See .env.example for the full list of supported variables.

ACCOUNT_URL = os.environ.get(
    "ONELAKE_ACCOUNT_URL", "https://onelake.dfs.fabric.microsoft.com"
)
TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
STORAGE_SCOPE = "https://storage.azure.com/.default"

# Workspace profiles — populated entirely from .env / environment variables.
_WORKSPACE_PROFILES: dict[str, str] = {
    "dev": os.environ.get("ONELAKE_WORKSPACE_DEV", ""),
    "tst": os.environ.get("ONELAKE_WORKSPACE_TST", ""),
    "prd": os.environ.get("ONELAKE_WORKSPACE_PRD", ""),
}

ACTIVE_WORKSPACE = os.environ.get("ONELAKE_WORKSPACE", "dev")
WORKSPACE_ID = _WORKSPACE_PROFILES.get(ACTIVE_WORKSPACE) or _WORKSPACE_PROFILES["dev"]


def _build_lakehouses() -> dict[str, str]:
    """Build the lakehouse registry from env vars + hardcoded defaults.

    Keys are friendly names used in tool arguments.
    Values are the lakehouse GUIDs required by OneLake's DFS API.
    Add ONELAKE_LAKEHOUSE_<NAME> env vars to override or add new lakehouses
    without changing the code.
    """
    # All lakehouse GUIDs come from .env / environment variables — no hardcoded values.
    defaults: dict[str, str] = {
        "Landing": "",   # set ONELAKE_LAKEHOUSE_LANDING in .env
        "Base": "",      # set ONELAKE_LAKEHOUSE_BASE in .env
        "Curated": "",   # set ONELAKE_LAKEHOUSE_CURATED in .env
    }
    result: dict[str, str] = {}
    for name, default_id in defaults.items():
        guid = os.environ.get(f"ONELAKE_LAKEHOUSE_{name.upper()}", default_id).strip()
        if guid:
            result[name] = guid
    # Allow arbitrary extra lakehouses: ONELAKE_LAKEHOUSES_EXTRA="Staging:guid,Archive:guid"
    for part in os.environ.get("ONELAKE_LAKEHOUSES_EXTRA", "").split(","):
        if ":" in part:
            n, g = part.strip().split(":", 1)
            if n.strip() and g.strip():
                result[n.strip()] = g.strip()
    return result


LAKEHOUSES = _build_lakehouses()

# Schema context annotations — shown in list_schemas output so Cursor
# understands each schema's role and source system.
# Extend with ONELAKE_SCHEMA_CONTEXT_JSON='{"myschema": "description"}'
SCHEMA_CONTEXT: dict[str, str] = {
    "zuora":        "Bronze | Zuora billing data | source_system='zuora' | company='Amili AS'",
    "banqsoft":     "Bronze | Banqsoft collection data | source_system='banqsoft' | company='Amili Collection AS'",
    "businessnxt":  "Bronze | BusinessNXT ERP data | source_system='businessnxt'",
    "GoogleSheets": "Bronze | Google Sheets manual imports",
    "dbo":          "Bronze | Legacy schema",
    "silver":       "Silver | Cleaned & conformed layer | _company column for multi-source joins",
    "gold":         "Gold | Reporting layer | surrogate keys on all dimensions",
}
_extra_ctx = os.environ.get("ONELAKE_SCHEMA_CONTEXT_JSON", "")
if _extra_ctx:
    try:
        SCHEMA_CONTEXT.update(json.loads(_extra_ctx))
    except (json.JSONDecodeError, TypeError):
        pass

# Known watermark / ingestion status tables.
# Extend with ONELAKE_WATERMARK_TABLES_JSON='[["Landing","schema","table"]]'
_WATERMARK_TABLES: list[tuple[str, str, str]] = [
    ("Landing", "zuora", "ingest_watermarks"),
    ("Landing", "banqsoft", "_file_registry"),
]
_extra_wm = os.environ.get("ONELAKE_WATERMARK_TABLES_JSON", "")
if _extra_wm:
    try:
        _WATERMARK_TABLES.extend(
            [tuple(t) for t in json.loads(_extra_wm)]  # type: ignore[misc]
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        pass


# ─── PRIVACY & AUDIT ──────────────────────────────────────────────────────────

TOKEN_CACHE_PATH = Path.home() / ".azure" / "onelake_mcp_token_cache"
TOKEN_CACHE_CAE_PATH = Path(f"{TOKEN_CACHE_PATH}.cae")
ACCESS_LOG_PATH = Path.home() / ".azure" / "onelake_mcp_access.log"
SCHEMA_CACHE_DIR = Path.home() / ".azure" / "onelake_mcp_schema_cache"

TOKEN_CACHE_MAX_AGE_HOURS = float(os.environ.get("TOKEN_CACHE_MAX_AGE_HOURS", "24"))
SCHEMA_CACHE_TTL_SECONDS = int(os.environ.get("SCHEMA_CACHE_TTL_SECONDS", "3600"))
DATA_PRIVACY_MODE = os.environ.get("DATA_PRIVACY_MODE", "strict").lower()
_STRICT_MODE = DATA_PRIVACY_MODE == "strict"

# Schemas where sample_table is blocked in strict mode.
_BLOCKED_SAMPLE_SCHEMAS: set[str] = {"gold", "silver"}

# Column name patterns that trigger automatic value redaction.
_PII_COLUMN_PATTERNS = re.compile(
    r"(name|email|phone|address|number|ssn|organisation|organization|\bid\b)",
    re.IGNORECASE,
)

# Access logger — file-only, never stdout (stdout is the MCP stdio transport).
_access_logger = logging.getLogger("onelake_mcp.access")
_access_logger.setLevel(logging.INFO)
_access_logger.propagate = False
ACCESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
_fh = logging.FileHandler(ACCESS_LOG_PATH)
_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
_access_logger.addHandler(_fh)


def _log_access(tool: str, schema_name: str, table_name: str) -> None:
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    _access_logger.info(
        "tool=%s schema=%s table=%s user=%s workspace=%s",
        tool, schema_name, table_name, user, ACTIVE_WORKSPACE,
    )


# ─── MCP SERVER ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    "onelake",
    instructions=(
        "Read-only access to Microsoft Fabric OneLake lakehouses for schema discovery "
        "and data analysis. Default lakehouse: 'Landing'. "
        f"Active workspace: {ACTIVE_WORKSPACE}. "
        f"Available lakehouses: {', '.join(LAKEHOUSES) or 'none configured'}."
    ),
)


# ─── UTILITY HELPERS ──────────────────────────────────────────────────────────

def _json_result(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _error(message: str, **details: Any) -> str:
    return _json_result({"error": message, **details})


def _format_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def _is_pii_column(column_name: str) -> bool:
    return bool(_PII_COLUMN_PATTERNS.search(column_name))


def _redact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {col: "[REDACTED]" if _is_pii_column(col) else val for col, val in row.items()}
        for row in rows
    ]


def _column_matches(pattern: str, column_name: str) -> bool:
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatchcase(column_name.lower(), pattern.lower())
    return bool(re.search(pattern, column_name, re.IGNORECASE))


# ─── TOKEN CACHE & STARTUP ────────────────────────────────────────────────────

def _expire_stale_token_cache() -> None:
    """Remove MSAL token cache files older than TOKEN_CACHE_MAX_AGE_HOURS.

    Refresh tokens can live up to 90 days. Expiring the cache file regularly
    limits the window of exposure if the local cache file is compromised.
    """
    for cache_path in (TOKEN_CACHE_PATH, TOKEN_CACHE_CAE_PATH):
        if not cache_path.exists():
            continue
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours > TOKEN_CACHE_MAX_AGE_HOURS:
            cache_path.unlink(missing_ok=True)
            print(
                f"[onelake-mcp] Token cache expired ({age_hours:.1f}h > "
                f"{TOKEN_CACHE_MAX_AGE_HOURS}h limit) — removed {cache_path}. "
                "Re-authentication required on next tool call.",
                file=sys.stderr,
            )


def _health_check() -> bool:
    """Verify OneLake connectivity before serving tools.

    Lists the Tables directory of the first configured lakehouse.
    Prints a clear status message to stderr so it appears in Cursor's MCP logs.
    """
    try:
        filesystem = _get_service_client().get_file_system_client(WORKSPACE_ID)
        first_lh = next(iter(LAKEHOUSES))
        tables_path = _tables_root(first_lh)
        list(filesystem.get_paths(path=tables_path, recursive=False, max_results=1))
        print(
            f"[onelake-mcp] Health check OK — workspace={ACTIVE_WORKSPACE} "
            f"({WORKSPACE_ID}), lakehouses={list(LAKEHOUSES.keys())}",
            file=sys.stderr,
        )
        return True
    except Exception as exc:
        print(
            f"[onelake-mcp] Health check FAILED: {exc}\n"
            f"  workspace={ACTIVE_WORKSPACE} ({WORKSPACE_ID})\n"
            f"  account_url={ACCOUNT_URL}\n"
            "  Check network access, Entra sign-in, workspace ID, and lakehouse IDs.\n"
            "  Set ONELAKE_WORKSPACE=dev|tst|prd to switch environments.",
            file=sys.stderr,
        )
        return False


# ─── AZURE / ONELAKE CLIENTS ──────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_credential() -> InteractiveBrowserCredential:
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_cache = PersistedTokenCache(FilePersistence(str(TOKEN_CACHE_PATH)))
    cae_cache = PersistedTokenCache(FilePersistence(str(TOKEN_CACHE_CAE_PATH)))
    return InteractiveBrowserCredential(
        tenant_id=TENANT_ID,
        _cache=file_cache,
        _cae_cache=cae_cache,
    )


def _get_storage_token() -> str:
    return _get_credential().get_token(STORAGE_SCOPE).token


def _storage_options() -> dict[str, str]:
    return {"bearer_token": _get_storage_token(), "use_fabric_endpoint": "true"}


@lru_cache(maxsize=1)
def _get_service_client() -> DataLakeServiceClient:
    return DataLakeServiceClient(account_url=ACCOUNT_URL, credential=_get_credential())


def _validate_lakehouse(lakehouse_name: str) -> str | None:
    if lakehouse_name not in LAKEHOUSES:
        return _error(
            f"Unknown lakehouse '{lakehouse_name}'. Known lakehouses: {', '.join(sorted(LAKEHOUSES))}",
        )
    return None


def _lakehouse_id(lakehouse_name: str) -> str:
    return LAKEHOUSES[lakehouse_name]


def _tables_root(lakehouse_name: str) -> str:
    # OneLake requires artifact GUIDs; friendly names are disabled on this tenant.
    return f"{_lakehouse_id(lakehouse_name)}/Tables"


def _schema_path(lakehouse_name: str, schema_name: str) -> str:
    return f"{_tables_root(lakehouse_name)}/{schema_name}"


def _table_uri(lakehouse_name: str, schema_name: str, table_name: str) -> str:
    return (
        f"abfss://{WORKSPACE_ID}@onelake.dfs.fabric.microsoft.com/"
        f"{_lakehouse_id(lakehouse_name)}/Tables/{schema_name}/{table_name}"
    )


def _list_child_directories(path: str) -> list[str]:
    filesystem = _get_service_client().get_file_system_client(WORKSPACE_ID)
    prefix = path.rstrip("/") + "/"
    names: list[str] = []
    for item in filesystem.get_paths(path=path, recursive=False):
        if not item.is_directory:
            continue
        name = (item.name or "").removeprefix(prefix).strip("/")
        if name and "/" not in name:
            names.append(name)
    return sorted(set(names))


def _is_delta_table(lakehouse_name: str, schema_name: str, table_name: str) -> bool:
    log_path = f"{_schema_path(lakehouse_name, schema_name)}/{table_name}/_delta_log"
    filesystem = _get_service_client().get_file_system_client(WORKSPACE_ID)
    try:
        next(filesystem.get_paths(path=log_path, recursive=False, max_results=1))
        return True
    except StopIteration:
        return False


def _open_delta_table(
    lakehouse_name: str, schema_name: str, table_name: str
) -> DeltaTable | str:
    uri = _table_uri(lakehouse_name, schema_name, table_name)
    try:
        return DeltaTable(uri, storage_options=_storage_options())
    except Exception as exc:
        return _error(
            f"Unable to read Delta table '{schema_name}.{table_name}' in "
            f"'{lakehouse_name}': {exc}",
            table_uri=uri,
        )


def _table_to_rows(table: Any, max_rows: int | None = None) -> list[dict[str, Any]]:
    row_count = table.num_rows if max_rows is None else min(table.num_rows, max_rows)
    col_names = table.column_names
    columns = {n: table.column(n).to_pylist()[:row_count] for n in col_names}
    return [{n: columns[n][i] for n in col_names} for i in range(row_count)]


# ─── SCHEMA CACHE ─────────────────────────────────────────────────────────────
# Caches get_table_schema results locally for SCHEMA_CACHE_TTL_SECONDS (default 1h).
# Keyed by workspace/lakehouse/schema/table so multiple projects don't collide.

def _schema_cache_path(lakehouse_name: str, schema_name: str, table_name: str) -> Path:
    return (
        SCHEMA_CACHE_DIR
        / WORKSPACE_ID
        / _lakehouse_id(lakehouse_name)
        / schema_name
        / f"{table_name}.json"
    )


def _read_schema_cache(
    lakehouse_name: str, schema_name: str, table_name: str
) -> dict[str, Any] | None:
    p = _schema_cache_path(lakehouse_name, schema_name, table_name)
    if not p.exists():
        return None
    if (time.time() - p.stat().st_mtime) > SCHEMA_CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_schema_cache(
    lakehouse_name: str, schema_name: str, table_name: str, data: dict[str, Any]
) -> None:
    p = _schema_cache_path(lakehouse_name, schema_name, table_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(data, default=str))
    except Exception:
        pass  # Cache write failure is non-fatal


def _get_schema_columns(
    lakehouse_name: str, schema_name: str, table_name: str
) -> list[dict[str, Any]] | str:
    """Return column list, using cache when available. Returns error string on failure."""
    cached = _read_schema_cache(lakehouse_name, schema_name, table_name)
    if cached:
        return cached.get("columns", [])
    table = _open_delta_table(lakehouse_name, schema_name, table_name)
    if isinstance(table, str):
        return table
    try:
        columns = [
            {
                "name": f.name,
                "type": str(f.type),
                "nullable": f.nullable,
                "metadata": dict(f.metadata) if f.metadata else {},
            }
            for f in table.schema().fields
        ]
        data: dict[str, Any] = {
            "lakehouse_name": lakehouse_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "table_uri": _table_uri(lakehouse_name, schema_name, table_name),
            "columns": columns,
            "column_count": len(columns),
            "partition_columns": table.metadata().partition_columns,
        }
        _write_schema_cache(lakehouse_name, schema_name, table_name, data)
        return columns
    except Exception as exc:
        return _error(f"Schema read failed for '{schema_name}.{table_name}': {exc}")


def _get_column_null_stats(table: DeltaTable) -> dict[str, dict[str, Any]]:
    """Aggregate per-column null counts from Delta file statistics.

    Uses Delta add_actions (file-level stats) — never reads actual row data.
    Returns {col_name: {null_count, total_rows, null_pct}} for each column
    that has stats recorded.
    """
    try:
        actions = table.get_add_actions(flatten=True)
        total_rows = table.count()
        stats: dict[str, dict[str, Any]] = {}
        for col_name in actions.column_names:
            if not col_name.startswith("null_count."):
                continue
            actual_col = col_name[len("null_count."):]
            values = actions.column(col_name).to_pylist()
            total_nulls = sum(v for v in values if v is not None)
            null_pct = round((total_nulls / total_rows * 100), 1) if total_rows > 0 else 0.0
            stats[actual_col] = {
                "null_count": total_nulls,
                "total_rows": total_rows,
                "null_pct": null_pct,
            }
        return stats
    except Exception:
        return {}


# ─── TOOLS: DISCOVERY ─────────────────────────────────────────────────────────

@mcp.tool()
def get_active_config() -> str:
    """Return the current workspace, lakehouses, and privacy settings.

    Call this at the start of a session to confirm which environment is active
    and which lakehouses are available.
    """
    return _json_result({
        "active_workspace": ACTIVE_WORKSPACE,
        "workspace_id": WORKSPACE_ID,
        "account_url": ACCOUNT_URL,
        "tenant_id": TENANT_ID,
        "lakehouses": {
            name: {"id": guid, "context": SCHEMA_CONTEXT.get(name.lower(), "")}
            for name, guid in LAKEHOUSES.items()
        },
        "available_workspaces": {
            k: v for k, v in _WORKSPACE_PROFILES.items() if v
        },
        "privacy_mode": DATA_PRIVACY_MODE,
        "token_cache_max_age_hours": TOKEN_CACHE_MAX_AGE_HOURS,
        "schema_cache_ttl_seconds": SCHEMA_CACHE_TTL_SECONDS,
        "note": (
            "To switch workspace, restart the MCP server with "
            "ONELAKE_WORKSPACE=dev|tst|prd in the mcp.json env block."
        ),
    })


@mcp.tool()
def list_schemas(lakehouse_name: str) -> str:
    """List all schemas in a Fabric lakehouse with context annotations."""
    if err := _validate_lakehouse(lakehouse_name):
        return err
    try:
        schemas = _list_child_directories(_tables_root(lakehouse_name))
        schema_details = [
            {
                "schema": s,
                "context": SCHEMA_CONTEXT.get(s.lower(), ""),
            }
            for s in schemas
        ]
        return _json_result({
            "lakehouse_name": lakehouse_name,
            "lakehouse_id": _lakehouse_id(lakehouse_name),
            "workspace_id": WORKSPACE_ID,
            "active_workspace": ACTIVE_WORKSPACE,
            "schemas": schema_details,
            "schema_count": len(schemas),
        })
    except Exception as exc:
        return _error(f"Failed to list schemas in '{lakehouse_name}': {exc}")


@mcp.tool()
def list_tables(lakehouse_name: str, schema_name: str) -> str:
    """List all Delta tables in a lakehouse schema."""
    if err := _validate_lakehouse(lakehouse_name):
        return err
    try:
        candidates = _list_child_directories(_schema_path(lakehouse_name, schema_name))
        tables = [n for n in candidates if _is_delta_table(lakehouse_name, schema_name, n)]
        return _json_result({
            "lakehouse_name": lakehouse_name,
            "schema_name": schema_name,
            "schema_context": SCHEMA_CONTEXT.get(schema_name.lower(), ""),
            "tables": tables,
            "table_count": len(tables),
        })
    except Exception as exc:
        return _error(f"Failed to list tables in '{lakehouse_name}.{schema_name}': {exc}")


@mcp.tool()
def get_table_schema(lakehouse_name: str, schema_name: str, table_name: str) -> str:
    """Return column names, data types, nullability, and partition info for a Delta table.

    Results are cached locally for SCHEMA_CACHE_TTL_SECONDS (default 1 hour) to reduce
    latency on repeated calls. The cache is keyed per workspace/lakehouse/schema/table.
    """
    if err := _validate_lakehouse(lakehouse_name):
        return err

    cached = _read_schema_cache(lakehouse_name, schema_name, table_name)
    if cached:
        cached["_from_cache"] = True
        return _json_result(cached)

    table = _open_delta_table(lakehouse_name, schema_name, table_name)
    if isinstance(table, str):
        return table
    try:
        columns = [
            {
                "name": f.name,
                "type": str(f.type),
                "nullable": f.nullable,
                "metadata": dict(f.metadata) if f.metadata else {},
            }
            for f in table.schema().fields
        ]
        result: dict[str, Any] = {
            "lakehouse_name": lakehouse_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "table_uri": _table_uri(lakehouse_name, schema_name, table_name),
            "columns": columns,
            "column_count": len(columns),
            "partition_columns": table.metadata().partition_columns,
            "_from_cache": False,
        }
        _write_schema_cache(lakehouse_name, schema_name, table_name, result)
        return _json_result(result)
    except Exception as exc:
        return _error(
            f"Failed to read schema for '{schema_name}.{table_name}': {exc}",
            table_uri=_table_uri(lakehouse_name, schema_name, table_name),
        )


@mcp.tool()
def get_table_stats(lakehouse_name: str, schema_name: str, table_name: str) -> str:
    """Return row count, file count, on-disk size, and last modified time for a Delta table."""
    if err := _validate_lakehouse(lakehouse_name):
        return err
    table = _open_delta_table(lakehouse_name, schema_name, table_name)
    if isinstance(table, str):
        return table
    try:
        add_actions = table.get_add_actions()
        sizes = add_actions.column("size_bytes").to_pylist()
        mod_times = add_actions.column("modification_time").to_pylist()
        total_size = sum(s for s in sizes if s is not None)
        valid_times = [t for t in mod_times if t is not None]
        last_modified_ms = max(valid_times) if valid_times else None
        last_modified = (
            datetime.fromtimestamp(last_modified_ms / 1000, tz=timezone.utc).isoformat()
            if last_modified_ms else None
        )
        history = table.history(limit=1)
        return _json_result({
            "lakehouse_name": lakehouse_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "table_uri": _table_uri(lakehouse_name, schema_name, table_name),
            "row_count": table.count(),
            "row_count_note": "Approximate from per-file statistics.",
            "file_count": len(table.file_uris()),
            "size_bytes": total_size,
            "size_human": _format_bytes(total_size),
            "last_modified": last_modified,
            "latest_commit": history[0] if history else None,
        })
    except Exception as exc:
        return _error(
            f"Failed to collect stats for '{schema_name}.{table_name}': {exc}",
            table_uri=_table_uri(lakehouse_name, schema_name, table_name),
        )


@mcp.tool()
def get_data_model() -> str:
    """Return a high-level map of all schemas and tables across every configured lakehouse.

    Call this once at the start of a session so Cursor has full context for what
    data exists before writing any transformation code.
    Also reports cross-lakehouse column name overlaps as likely foreign key candidates.
    """
    model: dict[str, Any] = {
        "workspace": ACTIVE_WORKSPACE,
        "workspace_id": WORKSPACE_ID,
        "lakehouses": {},
        "cross_lakehouse_key_candidates": [],
    }
    # col_name -> [(lakehouse, schema, table)]
    col_index: dict[str, list[tuple[str, str, str]]] = {}

    for lh_name in LAKEHOUSES:
        lh_entry: dict[str, Any] = {"schemas": {}, "total_tables": 0}
        try:
            schema_names = _list_child_directories(_tables_root(lh_name))
        except Exception as exc:
            lh_entry["error"] = str(exc)
            model["lakehouses"][lh_name] = lh_entry
            continue

        for schema_name in schema_names:
            try:
                table_names = _list_child_directories(_schema_path(lh_name, schema_name))
                delta_tables = [
                    t for t in table_names if _is_delta_table(lh_name, schema_name, t)
                ]
                lh_entry["schemas"][schema_name] = {
                    "table_count": len(delta_tables),
                    "tables": delta_tables,
                    "context": SCHEMA_CONTEXT.get(schema_name.lower(), ""),
                }
                lh_entry["total_tables"] += len(delta_tables)

                # Index columns for FK inference (schema only, no data read).
                for table_name in delta_tables:
                    cols = _get_schema_columns(lh_name, schema_name, table_name)
                    if isinstance(cols, list):
                        for col in cols:
                            col_index.setdefault(col["name"], []).append(
                                (lh_name, schema_name, table_name)
                            )
            except Exception as exc:
                lh_entry["schemas"][schema_name] = {"error": str(exc)}

        model["lakehouses"][lh_name] = lh_entry

    # Surface columns that appear in 3+ tables across different schemas as FK candidates.
    for col_name, locations in col_index.items():
        schemas_involved = {loc[1] for loc in locations}
        if len(locations) >= 3 and len(schemas_involved) >= 2:
            model["cross_lakehouse_key_candidates"].append({
                "column_name": col_name,
                "appears_in": len(locations),
                "locations": [
                    {"lakehouse": lh, "schema": sc, "table": tb}
                    for lh, sc, tb in locations[:10]  # cap output
                ],
            })

    model["cross_lakehouse_key_candidates"].sort(
        key=lambda x: x["appears_in"], reverse=True
    )
    return _json_result(model)


# ─── TOOLS: SAMPLING & PRIVACY ────────────────────────────────────────────────

@mcp.tool()
def sample_table(
    lakehouse_name: str,
    schema_name: str,
    table_name: str,
    n_rows: int = 10,
) -> str:
    """Return the first N rows from a Delta table as JSON.

    In strict privacy mode:
    - Calls on gold.* and silver.* schemas are blocked.
    - Values in PII-named columns (name, id, email, phone, address, etc.)
      are replaced with [REDACTED].
    - A WARNING field is always present in the response.
    - Every call is appended to the access log.
    """
    if err := _validate_lakehouse(lakehouse_name):
        return err
    if n_rows < 1:
        return _error("n_rows must be at least 1")

    if _STRICT_MODE and schema_name.lower() in _BLOCKED_SAMPLE_SCHEMAS:
        return _error(
            f"sample_table is blocked for schema '{schema_name}' in strict privacy mode. "
            "Use get_table_schema or get_table_stats instead.",
            schema_name=schema_name,
            table_name=table_name,
            privacy_mode=DATA_PRIVACY_MODE,
        )

    _log_access("sample_table", schema_name, table_name)
    table = _open_delta_table(lakehouse_name, schema_name, table_name)
    if isinstance(table, str):
        return table

    try:
        reader = (
            QueryBuilder()
            .register("t", table)
            .execute(f"SELECT * FROM t LIMIT {int(n_rows)}")
        )
        data = reader.read_all()
        rows = _table_to_rows(data, max_rows=n_rows)
        redacted = _redact_rows(rows)
        redacted_columns = [c for c in (rows[0].keys() if rows else []) if _is_pii_column(c)]
        return _json_result({
            "WARNING": "This data may contain PII. Do not share or reproduce values outside this session.",
            "privacy_mode": DATA_PRIVACY_MODE,
            "redacted_columns": redacted_columns,
            "lakehouse_name": lakehouse_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "n_rows_requested": n_rows,
            "n_rows_returned": len(redacted),
            "rows": redacted,
        })
    except Exception as exc:
        return _error(
            f"Failed to sample '{schema_name}.{table_name}': {exc}",
            table_uri=_table_uri(lakehouse_name, schema_name, table_name),
        )


# ─── TOOLS: SEARCH & COMPARISON ───────────────────────────────────────────────

@mcp.tool()
def search_columns(column_name_pattern: str) -> str:
    """Search all schemas and tables for columns matching a regex or glob pattern.

    Also infers likely foreign key relationships: columns with the same name
    that appear across multiple schemas are flagged as JOIN candidates.
    Supports regex (e.g. 'account.*key') and globs (e.g. '*Key').
    """
    if not column_name_pattern.strip():
        return _error("column_name_pattern must not be empty")

    matches: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for lh_name in LAKEHOUSES:
        try:
            schema_names = _list_child_directories(_tables_root(lh_name))
        except Exception as exc:
            errors.append({"lakehouse": lh_name, "stage": "list_schemas", "message": str(exc)})
            continue

        for schema_name in schema_names:
            try:
                table_names = _list_child_directories(_schema_path(lh_name, schema_name))
            except Exception as exc:
                errors.append({"lakehouse": lh_name, "schema": schema_name,
                                "stage": "list_tables", "message": str(exc)})
                continue

            for table_name in table_names:
                if not _is_delta_table(lh_name, schema_name, table_name):
                    continue
                cols = _get_schema_columns(lh_name, schema_name, table_name)
                if isinstance(cols, str):
                    errors.append({"lakehouse": lh_name, "schema": schema_name,
                                   "table": table_name, "stage": "read_schema",
                                   "message": json.loads(cols).get("error", cols)})
                    continue
                for col in cols:
                    if _column_matches(column_name_pattern, col["name"]):
                        matches.append({
                            "lakehouse_name": lh_name,
                            "schema_name": schema_name,
                            "table_name": table_name,
                            "column_name": col["name"],
                            "data_type": col["type"],
                        })

    # FK inference: flag columns that match the pattern in 2+ distinct schemas.
    schema_groups: dict[str, list[dict[str, Any]]] = {}
    for m in matches:
        schema_groups.setdefault(m["column_name"], []).append(m)

    join_candidates = [
        {
            "column_name": col,
            "tables": locs,
            "join_note": f"'{col}' appears in {len(locs)} tables across "
                         f"{len({l['schema_name'] for l in locs})} schemas — likely a JOIN key.",
        }
        for col, locs in schema_groups.items()
        if len({l["schema_name"] for l in locs}) >= 2
    ]

    return _json_result({
        "pattern": column_name_pattern,
        "match_count": len(matches),
        "matches": matches,
        "join_candidates": join_candidates,
        "errors": errors,
    })


@mcp.tool()
def compare_schemas(
    lakehouse_name_1: str,
    schema_name_1: str,
    table_name_1: str,
    lakehouse_name_2: str,
    schema_name_2: str,
    table_name_2: str,
) -> str:
    """Compare the schemas of two Delta tables and report alignment.

    Returns:
    - Columns present in both tables with matching types
    - Columns present in both with mismatched types
    - Columns only in table 1
    - Columns only in table 2
    - An alignment score (0–100%)

    Useful for checking if two Bronze source tables can be merged into
    a shared Silver/Gold dimension (e.g. zuora.Account vs banqsoft.vParty).
    """
    for lh in (lakehouse_name_1, lakehouse_name_2):
        if err := _validate_lakehouse(lh):
            return err

    cols1 = _get_schema_columns(lakehouse_name_1, schema_name_1, table_name_1)
    if isinstance(cols1, str):
        return cols1
    cols2 = _get_schema_columns(lakehouse_name_2, schema_name_2, table_name_2)
    if isinstance(cols2, str):
        return cols2

    map1 = {c["name"].lower(): c for c in cols1}
    map2 = {c["name"].lower(): c for c in cols2}
    all_keys = set(map1) | set(map2)

    matching: list[dict[str, Any]] = []
    type_mismatches: list[dict[str, Any]] = []
    only_in_1: list[dict[str, Any]] = []
    only_in_2: list[dict[str, Any]] = []

    for key in sorted(all_keys):
        if key in map1 and key in map2:
            c1, c2 = map1[key], map2[key]
            if c1["type"] == c2["type"]:
                matching.append({"column": c1["name"], "type": c1["type"]})
            else:
                type_mismatches.append({
                    "column": c1["name"],
                    f"type_in_{table_name_1}": c1["type"],
                    f"type_in_{table_name_2}": c2["type"],
                    "cast_suggestion": f"CAST({c1['name']} AS {c2['type']})",
                })
        elif key in map1:
            c = map1[key]
            only_in_1.append({"column": c["name"], "type": c["type"]})
        else:
            c = map2[key]
            only_in_2.append({"column": c["name"], "type": c["type"]})

    total = len(all_keys)
    alignment_score = round(len(matching) / total * 100, 1) if total else 0.0

    return _json_result({
        "table_1": f"{lakehouse_name_1}.{schema_name_1}.{table_name_1}",
        "table_2": f"{lakehouse_name_2}.{schema_name_2}.{table_name_2}",
        "alignment_score_pct": alignment_score,
        "total_unique_columns": total,
        "matching_columns": matching,
        "type_mismatches": type_mismatches,
        "only_in_table_1": only_in_1,
        "only_in_table_2": only_in_2,
        "summary": (
            f"{len(matching)} columns align, {len(type_mismatches)} type mismatches, "
            f"{len(only_in_1)} only in {table_name_1}, {len(only_in_2)} only in {table_name_2}."
        ),
    })


# ─── TOOLS: DATA QUALITY ──────────────────────────────────────────────────────

@mcp.tool()
def find_duplicate_candidates(
    lakehouse_name: str,
    schema_name: str,
    table_name: str,
) -> str:
    """Identify columns that are likely deduplication keys for a Delta table.

    Ranks candidate columns by name patterns, partition membership, data type,
    and nullability. Reads zero row data — safe on all schemas including gold/silver.
    """
    if err := _validate_lakehouse(lakehouse_name):
        return err

    _log_access("find_duplicate_candidates", schema_name, table_name)
    table = _open_delta_table(lakehouse_name, schema_name, table_name)
    if isinstance(table, str):
        return table

    try:
        schema = table.schema()
        metadata = table.metadata()
        partition_cols = set(metadata.partition_columns or [])
        row_count = table.count()
        file_count = len(table.file_uris())

        _KEY_RE = re.compile(
            r"(key|_id$|^id_|^id$|code|hash|checksum|fingerprint|etag|version|"
            r"sequence|seq_|_seq$|timestamp|updated_at|created_at|modified|watermark|"
            r"surrogate|natural|business)",
            re.IGNORECASE,
        )
        _DATE_TYPES = {"timestamp", "date", "timestampntz"}

        candidates: list[dict[str, Any]] = []
        pii_excluded: list[str] = []

        for field in schema.fields:
            if _is_pii_column(field.name):
                pii_excluded.append(field.name)
                continue
            type_str = str(field.type).lower()
            score, reasons = 0, []
            if _KEY_RE.search(field.name):
                score += 3; reasons.append("name matches key pattern")
            if field.name in partition_cols:
                score += 2; reasons.append("partition column")
            if any(t in type_str for t in _DATE_TYPES):
                score += 1; reasons.append("date/timestamp type")
            if not field.nullable:
                score += 1; reasons.append("non-nullable")
            if score > 0:
                candidates.append({
                    "column_name": field.name,
                    "data_type": str(field.type),
                    "nullable": field.nullable,
                    "is_partition_column": field.name in partition_cols,
                    "dedup_score": score,
                    "reasons": reasons,
                })

        candidates.sort(key=lambda x: x["dedup_score"], reverse=True)

        return _json_result({
            "lakehouse_name": lakehouse_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "row_count": row_count,
            "file_count": file_count,
            "total_columns": len(schema.fields),
            "pii_columns_excluded": pii_excluded,
            "dedup_key_candidates": candidates,
            "partition_columns": metadata.partition_columns,
            "note": (
                "Combine top-scored candidates as a composite key. "
                "Use get_table_stats to verify row counts before/after deduplication. "
                "No data values were read."
            ),
        })
    except Exception as exc:
        return _error(
            f"Failed to analyse duplicate candidates for '{schema_name}.{table_name}': {exc}",
            table_uri=_table_uri(lakehouse_name, schema_name, table_name),
        )


@mcp.tool()
def suggest_silver_transforms(
    lakehouse_name: str,
    schema_name: str,
    table_name: str,
) -> str:
    """Analyse a Bronze table and suggest Silver-layer transformations.

    Uses Delta file statistics and schema metadata — reads no actual row values.

    Flags:
    - Columns with high null rates (>80%) — candidates to drop or coalesce
    - String columns whose names suggest dates — cast to TimestampType
    - String columns whose names suggest numbers — cast to DoubleType / LongType
    - Nullable columns that are likely MERGE keys — mark as NOT NULL
    - Whether a plausible MERGE key exists at all
    """
    if err := _validate_lakehouse(lakehouse_name):
        return err

    table = _open_delta_table(lakehouse_name, schema_name, table_name)
    if isinstance(table, str):
        return table

    try:
        schema = table.schema()
        null_stats = _get_column_null_stats(table)
        total_rows = table.count()

        _DATE_NAME_RE = re.compile(
            r"(date|_dt$|^dt_|time|_ts$|^ts_|created|updated|modified|processed|period|month|year)",
            re.IGNORECASE,
        )
        _NUM_NAME_RE = re.compile(
            r"(amount|price|qty|quantity|count|total|sum|balance|rate|ratio|percent|score|size|weight|fee|cost)",
            re.IGNORECASE,
        )
        _KEY_RE = re.compile(r"(key|_id$|^id_|^id$|surrogate|natural|business)", re.IGNORECASE)

        suggestions: list[dict[str, Any]] = []
        has_merge_key = False

        for field in schema.fields:
            col = field.name
            type_str = str(field.type).lower()
            is_string = type_str in ("string", "varchar", "text")
            ns = null_stats.get(col, {})
            null_pct = ns.get("null_pct", None)

            if null_pct is not None and null_pct > 80:
                suggestions.append({
                    "column": col,
                    "current_type": str(field.type),
                    "issue": f"High null rate: {null_pct}% of {total_rows} rows",
                    "suggestion": "Consider dropping or replacing with a default/coalesce.",
                    "priority": "high" if null_pct > 95 else "medium",
                })

            if is_string and _DATE_NAME_RE.search(col) and not _is_pii_column(col):
                suggestions.append({
                    "column": col,
                    "current_type": "string",
                    "issue": "Date/time column stored as STRING",
                    "suggestion": "CAST TO TimestampType — e.g. to_timestamp(col, 'yyyy-MM-dd HH:mm:ss')",
                    "priority": "high",
                })

            if is_string and _NUM_NAME_RE.search(col) and not _is_pii_column(col):
                suggestions.append({
                    "column": col,
                    "current_type": "string",
                    "issue": "Numeric column stored as STRING",
                    "suggestion": "CAST TO DoubleType or LongType — validate for non-numeric values first.",
                    "priority": "medium",
                })

            if _KEY_RE.search(col) and not _is_pii_column(col):
                has_merge_key = True
                if field.nullable:
                    suggestions.append({
                        "column": col,
                        "current_type": str(field.type),
                        "issue": "Likely MERGE key is nullable",
                        "suggestion": "Add NOT NULL constraint or coalesce before using as MERGE key.",
                        "priority": "high",
                    })

        if not has_merge_key:
            suggestions.append({
                "column": "(none found)",
                "issue": "No obvious MERGE key column detected",
                "suggestion": (
                    "Add a surrogate key column (e.g. sha2(concat_ws('|', col1, col2), 256)) "
                    "or identify a natural business key."
                ),
                "priority": "high",
            })

        suggestions.sort(key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x.get("priority", "low"), 2))

        return _json_result({
            "lakehouse_name": lakehouse_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "total_rows": total_rows,
            "total_columns": len(schema.fields),
            "columns_with_null_stats": len(null_stats),
            "has_merge_key": has_merge_key,
            "suggestion_count": len(suggestions),
            "suggestions": suggestions,
            "note": "Analysis uses Delta file statistics only — no row data was read.",
        })
    except Exception as exc:
        return _error(
            f"Failed to analyse transforms for '{schema_name}.{table_name}': {exc}",
            table_uri=_table_uri(lakehouse_name, schema_name, table_name),
        )


# ─── TOOLS: INGESTION & MONITORING ────────────────────────────────────────────

@mcp.tool()
def get_watermark_status() -> str:
    """Read ingestion watermark tables to show ingestion status at a glance.

    Checks all configured watermark tables (zuora.ingest_watermarks,
    banqsoft._file_registry, and any added via ONELAKE_WATERMARK_TABLES_JSON).
    Returns timestamps and record counts — no PII or business data.
    """
    results: list[dict[str, Any]] = []

    for lh_name, schema_name, table_name in _WATERMARK_TABLES:
        if lh_name not in LAKEHOUSES:
            results.append({
                "source": f"{schema_name}.{table_name}",
                "lakehouse": lh_name,
                "status": "skipped",
                "reason": f"Lakehouse '{lh_name}' not configured.",
            })
            continue

        table = _open_delta_table(lh_name, schema_name, table_name)
        if isinstance(table, str):
            results.append({
                "source": f"{schema_name}.{table_name}",
                "lakehouse": lh_name,
                "status": "error",
                "reason": json.loads(table).get("error", "unknown"),
            })
            continue

        try:
            reader = (
                QueryBuilder()
                .register("wm", table)
                .execute("SELECT * FROM wm LIMIT 200")
            )
            data = reader.read_all()
            rows = _table_to_rows(data)

            # Find timestamp-like columns for the "last ingested" headline.
            ts_cols = [
                c for c in data.column_names
                if re.search(r"(time|date|stamp|updated|modified|watermark|last)", c, re.IGNORECASE)
                and not _is_pii_column(c)
            ]
            last_ingest: str | None = None
            if ts_cols and rows:
                ts_col = ts_cols[0]
                ts_values = [r[ts_col] for r in rows if r.get(ts_col) is not None]
                if ts_values:
                    try:
                        last_ingest = str(max(ts_values))
                    except Exception:
                        last_ingest = str(ts_values[-1])

            results.append({
                "source": f"{schema_name}.{table_name}",
                "lakehouse": lh_name,
                "status": "ok",
                "row_count": len(rows),
                "last_ingested": last_ingest,
                "timestamp_columns": ts_cols,
                "rows": rows,  # safe — watermark tables contain only metadata
            })
        except Exception as exc:
            results.append({
                "source": f"{schema_name}.{table_name}",
                "lakehouse": lh_name,
                "status": "error",
                "reason": str(exc),
            })

    return _json_result({"watermark_status": results, "checked_at": datetime.now(tz=timezone.utc).isoformat()})


# ─── TOOLS: NOTEBOOK VALIDATION ───────────────────────────────────────────────

@mcp.tool()
def validate_notebook(
    notebook_path: str,
    lakehouse_name: str,
    schema_name: str,
    table_name: str,
) -> str:
    """Validate column references in a local notebook/script against the actual table schema.

    Scans a .py, .ipynb, or .sql file for column name references and checks them
    against the Delta table schema in OneLake. Catches schema mismatches before
    you upload to Fabric — avoiding the upload → run → fail → fix cycle.

    Only accepts .py, .ipynb, and .sql files. Does not execute the notebook.
    Only column names and types are shown — no data values.
    """
    if err := _validate_lakehouse(lakehouse_name):
        return err

    path = Path(notebook_path).expanduser().resolve()
    if not path.exists():
        return _error(f"File not found: {notebook_path}")
    if path.suffix.lower() not in (".py", ".ipynb", ".sql"):
        return _error(
            f"Unsupported file type '{path.suffix}'. Only .py, .ipynb, and .sql are accepted."
        )
    if path.stat().st_size > 5 * 1024 * 1024:
        return _error("File exceeds 5 MB limit.")

    # Extract text content.
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".ipynb":
            nb = json.loads(raw)
            source_lines: list[str] = []
            for cell in nb.get("cells", []):
                source_lines.extend(cell.get("source", []))
            text = "\n".join(source_lines)
        else:
            text = raw
    except Exception as exc:
        return _error(f"Could not read file: {exc}")

    # Extract column name candidates using common PySpark / pandas patterns.
    # Patterns: .select("col"), df["col"], F.col("col"), col("col"), "col_name", withColumnRenamed("old","new")
    _COL_PATTERNS = [
        re.compile(r'\.select\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'\bF\.col\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'\bcol\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'\bdf\s*\[\s*["\']([^"\']+)["\']'),
        re.compile(r'\.withColumn(?:Renamed)?\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'\.filter\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'\.groupBy\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'\.orderBy\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'\.join\s*\([^,]+,\s*["\']([^"\']+)["\']'),
        re.compile(r'SELECT\s+([\w\s,.*]+?)\s+FROM', re.IGNORECASE),
    ]

    referenced: set[str] = set()
    for pattern in _COL_PATTERNS:
        for match in pattern.finditer(text):
            # Handle comma-separated column lists from SELECT
            for part in match.group(1).split(","):
                col = part.strip().strip("`").strip('"').strip("'")
                if col and not col.startswith("--") and len(col) <= 128:
                    referenced.add(col)

    # Get actual schema.
    cols = _get_schema_columns(lakehouse_name, schema_name, table_name)
    if isinstance(cols, str):
        return cols
    actual_columns = {c["name"].lower(): c for c in cols}
    actual_set = set(actual_columns.keys())

    valid: list[str] = []
    invalid: list[dict[str, str]] = []
    for ref in sorted(referenced):
        if ref.lower() in actual_set:
            valid.append(ref)
        else:
            # Suggest closest match by substring.
            suggestions = [
                a for a in actual_set if ref.lower() in a or a in ref.lower()
            ][:3]
            invalid.append({"column": ref, "suggestions": suggestions})

    return _json_result({
        "file": str(path),
        "lakehouse_name": lakehouse_name,
        "schema_name": schema_name,
        "table_name": table_name,
        "referenced_columns_found": len(referenced),
        "valid_columns": valid,
        "invalid_columns": invalid,
        "valid_count": len(valid),
        "invalid_count": len(invalid),
        "all_table_columns": [c["name"] for c in cols],
        "note": (
            "Column detection uses regex heuristics for common PySpark/pandas/SQL patterns. "
            "Dynamic column references (computed strings) cannot be detected statically."
        ),
    })


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    _expire_stale_token_cache()
    _health_check()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
