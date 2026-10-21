"""Flask-based local Kite login callback, adapted from Zerodha's example."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import RLock, Thread
from urllib.parse import urlsplit

from dotenv import set_key
from flask import Flask, request
from kiteconnect import KiteConnect
from werkzeug.serving import BaseWSGIServer, make_server

logger = logging.getLogger(__name__)


class KiteLoginServer:
    """Run the Flask callback used by Kite's browser login redirect."""

    def __init__(self, kite: KiteConnect | None, api_secret: str, redirect_url: str,
                 env_path: Path):
        parsed = urlsplit(redirect_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.path != "/login"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "KITE_REDIRECT_URL must be a loopback URL such as "
                "http://127.0.0.1:5000/login"
            )

        self.kite = kite
        self.api_secret = api_secret
        self.redirect_url = redirect_url
        self.env_path = env_path
        self.host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
        self.port = parsed.port or 80
        self.expired = False
        self.start_error: str | None = None
        self._server: BaseWSGIServer | None = None
        self._thread: Thread | None = None
        self._token_lock = RLock()
        self._app = Flask("zerodha_mcp_login")
        self._app.add_url_rule("/", "login_index", self._index)
        self._app.add_url_rule("/login", "login_callback", self._login_callback)

    def _index(self):
        if not self.kite:
            return "KITE_API_KEY is not configured for this MCP server.", 503
        return (
            "<p>Sign in to Zerodha to refresh the MCP session.</p>"
            f'<p><a href="{self.kite.login_url()}">Login to Zerodha</a></p>'
            f"<p>Registered callback URL: {self.redirect_url}</p>"
        )

    def _login_callback(self):
        request_token = request.args.get("request_token")
        if not request_token:
            return "Login was not completed. Return to Codex and request a new login link.", 400
        if not self.kite or not self.api_secret:
            return "Kite API credentials are not configured on the MCP server.", 503
        try:
            session_data = self.kite.generate_session(
                request_token, api_secret=self.api_secret
            )
            access_token = session_data.get("access_token")
            if not access_token:
                raise RuntimeError("Kite returned an empty access token")
            self.activate_access_token(access_token)
            logger.info("Kite login completed for user %s", session_data.get("user_id", "unknown"))
        except Exception as exc:
            logger.warning("Kite login callback failed (%s)", type(exc).__name__)
            return (
                "Kite login could not be completed. Return to Codex and request a new login link.",
                400,
            )
        return "<h2 style='color:green'>Kite authentication complete</h2><p>Return to Codex.</p>"

    def activate_access_token(self, access_token: str) -> None:
        if not access_token:
            raise ValueError("Kite returned an empty access token")
        if not self.kite:
            raise RuntimeError("KITE_API_KEY is not configured")
        with self._token_lock:
            # Persist first so a disk error cannot leave a session that disappears on restart.
            set_key(str(self.env_path), "KITE_ACCESS_TOKEN", access_token)
            os.environ["KITE_ACCESS_TOKEN"] = access_token
            self.kite.set_access_token(access_token)
            self.expired = False

    def mark_expired(self) -> None:
        self.expired = True

    def start(self) -> None:
        if not self.kite:
            self.start_error = "KITE_API_KEY is not configured"
            return
        if self._server:
            return
        try:
            # Flask's development server is not used; this WSGI server runs only on loopback.
            logging.getLogger("werkzeug").setLevel(logging.ERROR)
            self._server = make_server(self.host, self.port, self._app, threaded=True)
            self._thread = Thread(
                target=self._server.serve_forever,
                name="kite-flask-login",
                daemon=True,
            )
            self._thread.start()
            self.start_error = None
            logger.info("Kite login callback listening on %s", self.redirect_url)
        except OSError as exc:
            self.start_error = f"Could not bind the Kite callback listener ({type(exc).__name__})"
            logger.error("%s", self.start_error)

    def stop(self) -> None:
        server, self._server = self._server, None
        if not server:
            return
        server.shutdown()
        server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def login_instructions(self) -> dict[str, str | bool]:
        if not self.kite:
            return {
                "status": "not_configured",
                "message": "Set KITE_API_KEY and KITE_API_SECRET before logging in.",
                "callback_ready": False,
            }
        if not self.api_secret:
            return {
                "status": "not_configured",
                "message": "Set KITE_API_SECRET before logging in.",
                "callback_ready": False,
            }
        result: dict[str, str | bool] = {
            "status": "login_required",
            "message": "Open login_url, sign in to Zerodha, then return to Codex after the redirect succeeds.",
            "login_url": self.kite.login_url(),
            "redirect_url": self.redirect_url,
            "callback_ready": self._server is not None,
        }
        if self.start_error:
            result["status"] = "callback_unavailable"
            result["message"] = self.start_error
        return result
