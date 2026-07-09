"""OneNote integration (Part 2).

Nothing here is imported by database.py except `store` -- which deliberately
depends on nothing heavier than sqlite3. `auth`, `graph`, and `client` pull in
msal/httpx and must only ever be imported lazily, from inside a tool body.
Importing them at module scope would drag a network stack into the MCP
server's startup path, where a failure is invisible to Claude Desktop.
"""
