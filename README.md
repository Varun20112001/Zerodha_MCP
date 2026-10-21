# Zerodha Kite Connect MCP

An MCP server that exposes Kite Connect's Python SDK operations as tools. REST
methods run in worker threads so synchronous SDK requests do not block the MCP
event loop. KiteTicker is controlled through dedicated WebSocket tools.

## Setup

Requires Python 3.10 or newer.

```powershell
cd D:\PRO\Zerodha_MCP
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `KITE_API_KEY` and `KITE_API_SECRET` in `.env`. The default callback is
`http://127.0.0.1:5000/login`; set `KITE_REDIRECT_URL` if the redirect URL
registered in the Kite developer console is different. The URL must match
exactly, including host, port, and path. Keep `.env` private; it is ignored by
Git.

The MCP starts a Flask loopback callback listener on that URL using the flow in
`login.py`. Call `kite_login` to get a Zerodha login link. After sign-in, Kite redirects to `/login` with a
short-lived `request_token`; the MCP exchanges it for an access token, updates
the running client, and persists the token to `.env`. The browser page reports
success without displaying the token. If any authenticated SDK tool gets a Kite
`TokenException`, it returns an `authentication_required` result with a fresh
login link. Complete login, then retry the original tool. The direct
`generate_session` and `set_access_token` tools also persist refreshed tokens.

Start using stdio:

```powershell
python server.py
```

Example MCP client configuration:

```json
{
  "mcpServers": {
    "zerodha-kite-connect": {
      "command": "D:\\PRO\\Zerodha_MCP\\.venv\\Scripts\\python.exe",
      "args": ["D:\\PRO\\Zerodha_MCP\\server.py"],
      "env": {
        "KITE_API_KEY": "your-api-key",
        "KITE_API_SECRET": "your-api-secret",
        "KITE_REDIRECT_URL": "http://127.0.0.1:5000/login"
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
- The registered redirect URL must be reachable on the same machine running
  this MCP process. Port 5000 must be free when using the default URL.
- Kite redirects with a `request_token`, not an access code. The callback
  exchanges it server-side; the API secret and access token are not shown in the
  browser response.
- Ticker and REST methods require an active Kite session. The daily session
  lifecycle and API permissions are controlled by Zerodha.

