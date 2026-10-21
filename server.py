"""MCP server exposing the Zerodha Kite Connect Python SDK."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from kiteconnect import KiteConnect, KiteTicker
from mcp.server.fastmcp import FastMCP

from audit import SQLiteBatchWriter

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("zerodha-mcp")

API_KEY = os.getenv("KITE_API_KEY", "")
API_SECRET = os.getenv("KITE_API_SECRET", "")
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")
AUDIT = SQLiteBatchWriter(
    os.getenv("MCP_SQLITE_PATH", "./kite_tool_calls.sqlite3"),
    int(os.getenv("MCP_AUDIT_BATCH_SIZE", "25")),
    float(os.getenv("MCP_AUDIT_FLUSH_INTERVAL_SECONDS", "2")),
)


@asynccontextmanager
async def lifespan(_server: Any):
    await AUDIT.start()
    try:
        yield {"audit": AUDIT}
    finally:
        await AUDIT.close()


mcp = FastMCP("zerodha-kite-connect", lifespan=lifespan)
kite = KiteConnect(api_key=API_KEY) if API_KEY else None
if kite and ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)

_ticker: KiteTicker | None = None
_ticker_thread: Any = None
_tick_buffer: list[dict[str, Any]] = []
_tick_lock = asyncio.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_SESSION_EXPIRED = False


def _client() -> KiteConnect:
    if not API_KEY:
        raise RuntimeError("KITE_API_KEY is not configured in the environment or .env file")
    assert kite is not None
    return kite


def _ticker_client() -> KiteTicker:
    global _ticker
    if not API_KEY:
        raise RuntimeError("KITE_API_KEY is not configured")
    token = _client().access_token
    if not token:
        raise RuntimeError("No access token is configured; complete Kite login first")
    if _ticker is None:
        _ticker = KiteTicker(API_KEY, token)

        def on_ticks(_ws: KiteTicker, ticks: list[dict[str, Any]]) -> None:
            # Keep a bounded recent tick buffer. Schedule mutation on the MCP loop.
            loop = _LOOP
            if loop and loop.is_running():
                def append_ticks() -> None:
                    _tick_buffer.extend(ticks)
                    del _tick_buffer[:-5000]
                loop.call_soon_threadsafe(append_ticks)

        _ticker.on_ticks = on_ticks
    return _ticker


# KiteConnect v5.x REST methods exposed as independent MCP tools. This list follows
# the public KiteConnect SDK methods; keyword arguments are validated by that SDK.
_REST_TOOLS: dict[str, str] = {
    "basket_order_margins": "Calculate basket margins, including applicable margin benefits.",
    "cancel_mf_order": "Cancel a mutual fund order.",
    "cancel_mf_sip": "Cancel a mutual fund SIP.",
    "cancel_order": "Cancel an order.",
    "convert_position": "Convert an open position between product types.",
    "delete_gtt": "Delete a GTT trigger.",
    "exit_order": "Exit a cover order.",
    "get_auction_instruments": "Get instruments available for an auction session.",
    "get_gtt": "Get one GTT trigger by trigger ID.",
    "get_gtts": "List GTT triggers.",
    "get_virtual_contract_note": "Calculate order-wise charges for a virtual contract note.",
    "historical_data": "Get historical candle data for an instrument token.",
    "holdings": "Get equity holdings.",
    "instruments": "Get instruments, optionally filtered by exchange.",
    "invalidate_access_token": "Invalidate the current access token.",
    "invalidate_refresh_token": "Invalidate a refresh token.",
    "login_url": "Get the Kite Connect login URL.",
    "ltp": "Get last traded prices for exchange:symbol instruments.",
    "margins": "Get available account margins.",
    "mf_holdings": "Get mutual fund holdings.",
    "mf_instruments": "Get mutual fund instruments.",
    "mf_orders": "Get mutual fund orders.",
    "mf_sips": "Get mutual fund SIPs.",
    "modify_gtt": "Modify an existing GTT trigger.",
    "modify_mf_sip": "Modify a mutual fund SIP.",
    "modify_order": "Modify an order.",
    "ohlc": "Get OHLC data for exchange:symbol instruments.",
    "order_history": "Get history for a specific order.",
    "order_margins": "Calculate margins for a list of orders.",
    "order_trades": "Get trades for a specific order.",
    "orders": "Get the order book.",
    "place_autoslice_order": "Place an order and automatically split it by exchange freeze limits.",
    "place_gtt": "Create a GTT trigger.",
    "place_mf_order": "Place a mutual fund order.",
    "place_mf_sip": "Create a mutual fund SIP.",
    "place_order": "Place an equity, derivative, currency, or commodity order.",
    "positions": "Get day and net positions.",
    "profile": "Get the authenticated user's profile.",
    "quote": "Get full quotes for exchange:symbol instruments.",
    "renew_access_token": "Renew an access token using a refresh token.",
    "set_session_expiry_hook": "Install the SDK session-expiry callback; query session_expiry_status to inspect it.",
    "trades": "Get trades for the current day.",
    "trigger_range": "Get cover-order trigger price ranges.",
}


def _register_rest_tool(name: str, description: str) -> None:
    async def invoke(arguments: dict[str, Any]) -> Any:
        global kite
        started = time.perf_counter()
        result = None
        error = None
        try:
            client = _client()
            if name == "generate_session":
                session_arguments = dict(arguments)
                session_arguments.setdefault("api_secret", API_SECRET)
                result = await asyncio.to_thread(client.generate_session, **session_arguments)
            elif name == "set_session_expiry_hook":
                if arguments:
                    raise ValueError("This MCP tool takes no arguments")
                def on_expiry() -> None:
                    global _SESSION_EXPIRED
                    _SESSION_EXPIRED = True
                result = await asyncio.to_thread(client.set_session_expiry_hook, on_expiry)
            else:
                method = getattr(client, name)
                result = await asyncio.to_thread(method, **arguments)
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await AUDIT.record(name, arguments, result, error,
                               (time.perf_counter() - started) * 1000)

    invoke.__name__ = name
    invoke.__doc__ = description + " Pass SDK parameters as a JSON object in `arguments`."
    mcp.tool(name=name, description=invoke.__doc__)(invoke)


for _name, _description in _REST_TOOLS.items():
    _register_rest_tool(_name, _description)


_register_rest_tool(
    "generate_session",
    "Exchange the request token from the Kite login redirect for an authenticated session. "
    "Pass request_token and optionally api_secret; KITE_API_SECRET is used when omitted.",
)


@mcp.tool(description="Set or replace the Kite access token for this server process.")
async def set_access_token(access_token: str) -> dict[str, str]:
    """Set current session token; the token is redacted from SQLite audit records."""
    started = time.perf_counter()
    error = None
    result = None
    try:
        _client().set_access_token(access_token)
        result = {"status": "access token set"}
        return result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await AUDIT.record("set_access_token", {"access_token": access_token}, result, error,
                           (time.perf_counter() - started) * 1000)


@mcp.tool(description="Return whether the SDK session-expiry hook has fired since this server started.")
async def session_expiry_status() -> dict[str, bool]:
    result = {"expired": _SESSION_EXPIRED}
    await AUDIT.record("session_expiry_status", {}, result, None, 0.0)
    return result


@mcp.tool(description="Connect the KiteTicker WebSocket in a background thread.")
async def ticker_connect(threaded: bool = True, disable_ssl: bool = False) -> dict[str, str]:
    global _LOOP, _ticker_thread
    started = time.perf_counter()
    error = None
    result = None
    try:
        _LOOP = asyncio.get_running_loop()
        ticker = _ticker_client()
        if threaded:
            _ticker_thread = await asyncio.to_thread(ticker.connect, threaded=True, disable_ssl=disable_ssl)
        else:
            return {"status": "For a non-threaded WebSocket, use threaded=true with MCP."}
        result = {"status": "connecting"}
        return result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await AUDIT.record("ticker_connect", {"threaded": threaded, "disable_ssl": disable_ssl},
                           result, error, (time.perf_counter() - started) * 1000)


async def _ticker_call(name: str, arguments: dict[str, Any], fn: Any) -> Any:
    started = time.perf_counter()
    error = None
    result = None
    try:
        result = await asyncio.to_thread(fn)
        return result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await AUDIT.record(name, arguments, result, error, (time.perf_counter() - started) * 1000)


@mcp.tool(description="Subscribe to one or more numeric instrument tokens on KiteTicker.")
async def ticker_subscribe(instrument_tokens: list[int]) -> dict[str, str]:
    ticker = _ticker_client()
    return await _ticker_call("ticker_subscribe", {"instrument_tokens": instrument_tokens},
                              lambda: ticker.subscribe(instrument_tokens)) or {"status": "subscribed"}


@mcp.tool(description="Unsubscribe from one or more numeric instrument tokens.")
async def ticker_unsubscribe(instrument_tokens: list[int]) -> dict[str, str]:
    ticker = _ticker_client()
    return await _ticker_call("ticker_unsubscribe", {"instrument_tokens": instrument_tokens},
                              lambda: ticker.unsubscribe(instrument_tokens)) or {"status": "unsubscribed"}


@mcp.tool(description="Set ticker mode to 'ltp', 'quote', or 'full' for instrument tokens.")
async def ticker_set_mode(mode: str, instrument_tokens: list[int]) -> dict[str, str]:
    ticker = _ticker_client()
    return await _ticker_call("ticker_set_mode", {"mode": mode, "instrument_tokens": instrument_tokens},
                              lambda: ticker.set_mode(mode, instrument_tokens)) or {"status": "mode set"}


@mcp.tool(description="Read and clear the most recent buffered KiteTicker ticks (up to 5000).")
async def ticker_read_ticks(clear: bool = True) -> list[dict[str, Any]]:
    started = time.perf_counter()
    error = None
    ticks: list[dict[str, Any]] = []
    try:
        async with _tick_lock:
            ticks = list(_tick_buffer)
            if clear:
                _tick_buffer.clear()
        return ticks
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await AUDIT.record("ticker_read_ticks", {"clear": clear}, ticks, error,
                           (time.perf_counter() - started) * 1000)


@mcp.tool(description="Get KiteTicker WebSocket connection status.")
async def ticker_is_connected() -> bool:
    ticker = _ticker_client()
    return bool(await _ticker_call("ticker_is_connected", {}, ticker.is_connected))


@mcp.tool(description="Close the KiteTicker WebSocket connection.")
async def ticker_close() -> dict[str, str]:
    ticker = _ticker_client()
    await _ticker_call("ticker_close", {}, ticker.close)
    return {"status": "closed"}


@mcp.tool(description="Stop KiteTicker reconnection attempts.")
async def ticker_stop_retry() -> dict[str, str]:
    ticker = _ticker_client()
    await _ticker_call("ticker_stop_retry", {}, ticker.stop_retry)
    return {"status": "retry stopped"}


@mcp.tool(description="Stop the KiteTicker event loop and disable automatic reconnection.")
async def ticker_stop() -> dict[str, str]:
    ticker = _ticker_client()
    await _ticker_call("ticker_stop", {}, ticker.stop)
    return {"status": "stopped"}


@mcp.tool(description="Resubscribe to all instruments restored by KiteTicker after reconnect.")
async def ticker_resubscribe() -> dict[str, str]:
    ticker = _ticker_client()
    await _ticker_call("ticker_resubscribe", {}, ticker.resubscribe)
    return {"status": "resubscribed"}


if __name__ == "__main__":
    mcp.run()

