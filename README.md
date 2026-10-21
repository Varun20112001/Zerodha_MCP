# Zerodha Kite Connect MCP

An MCP server that exposes Kite Connect's Python SDK operations as tools. REST
methods run in worker threads so synchronous SDK requests do not block the MCP
event loop. KiteTicker is controlled through dedicated WebSocket tools.

## Setup

Requires Python 3.10 or newer.

```powershell
cd D:\PRO\zerodha_mcp
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `KITE_API_KEY` and, when available, `KITE_ACCESS_TOKEN` in `.env`. Set
`KITE_API_SECRET` to let `generate_session` use it when exchanging a
`request_token`. Alternatively, pass `api_secret` directly to that tool. Keep
`.env` private; it is ignored by Git.

Start using stdio:

```powershell
python server.py
```

Example MCP client configuration:

```json
{
  "mcpServers": {
    "zerodha-kite-connect": {
      "command": "D:\\PRO\\zerodha_mcp\\.venv\\Scripts\\python.exe",
      "args": ["D:\\PRO\\zerodha_mcp\\server.py"],
      "env": {
        "KITE_API_KEY": "your-api-key",
        "KITE_API_SECRET": "your-api-secret",
        "KITE_ACCESS_TOKEN": "your-access-token"
      }
    }
  }
}
```

## Tools

Every REST SDK method has its own tool name. Its SDK keyword parameters are
passed inside an `arguments` JSON object. Examples include `place_order`,
`orders`, `order_history`, `order_trades`, `positions`, `holdings`, `quote`,
`ohlc`, `ltp`, `instruments`, `historical_data`, margin calculations, GTT,
mutual fund orders and SIPs, session login/token lifecycle, and auction tools.
The exact accepted parameters and values are those of the installed
`kiteconnect` SDK; Kite Connect's own validation and errors are returned.

The `ticker_*` tools manage the KiteTicker WebSocket: connect, subscribe,
unsubscribe, set mode, check connection, read recent ticks, resubscribe, stop
retries, stop, and close. Call `ticker_connect` before subscribing. Recent ticks
are kept in a bounded in-memory buffer (up to 5,000) and retrieved with
`ticker_read_ticks`.

## SQLite audit batching

Every tool invocation is queued in memory, then written with SQLite
`executemany` from a background worker. The worker flushes at the configured
batch size, after the flush interval for partial batches, and on clean shutdown.
Defaults are 25 calls or 2 seconds, whichever comes first.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MCP_SQLITE_PATH` | `./kite_tool_calls.sqlite3` | SQLite audit database path |
| `MCP_AUDIT_BATCH_SIZE` | `25` | Calls per batch flush |
| `MCP_AUDIT_FLUSH_INTERVAL_SECONDS` | `2` | Max delay for partial batches |

The `tool_calls` table stores UTC time, tool name, JSON arguments and result,
error text, and duration in milliseconds. Credential fields such as access,
refresh, request, and API secret tokens are redacted. Large JSON values are
truncated to keep the audit database bounded per call. Since batching is
in-memory, an abrupt process termination can lose the pending batch.

## Notes

- This server sends real requests to the authenticated Kite account. Order
  placement, modification, cancellation, GTT, and mutual fund tools can cause
  financial transactions.
- Authentication follows Kite's login redirect: obtain a request token through
  Kite login, call `generate_session`, then set the returned access token using
  `set_access_token` if needed.
- Ticker and REST methods require an active Kite session. The daily session
  lifecycle and API permissions are controlled by Zerodha.

