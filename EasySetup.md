Option A — Cursor-assisted setup (paste into chat)
Open SETUP.md in Cursor, then paste this into the chat:

"Please follow the instructions in SETUP.md step by step to set up the OneLake MCP server on my machine. Run each command, check the output, and tell me what to fill in when you need information from me."

Cursor will walk through every step, run the shell commands, and prompt you only when it needs your Fabric GUIDs.

Option B — Automated shell script
For a new machine, just run:

cd ~/Desktop/onelake-mcp
chmod +x setup.sh
./setup.sh
The script will:

Auto-detect Python 3.10+ (including Homebrew)
Create .venv (or reuse if already valid)
Install all dependencies
Interactively prompt for your 3–5 Fabric GUIDs (with format validation)
Write ~/.cursor/mcp.json — merging safely with any existing servers
Verify the server loads correctly
Print a clear "what to do next" summary
Both options end with the same result: reload Cursor, call get_active_config, sign in via browser, and you're live.