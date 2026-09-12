"""Thin HTTP client for the AutoTyper backend.

Knows nothing about DB or payment secrets — it just calls the backend and
carries the user's bearer token.
"""
from __future__ import annotations

import httpx


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


class ApiClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._timeout = timeout

    # ── helpers ────────────────────────────────────────────────
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _post(self, path: str, json_body: dict, auth: bool = True) -> dict:
        return self._request("POST", path, json_body, auth)

    def _get(self, path: str, auth: bool = True) -> dict:
        return self._request("GET", path, None, auth)

    def _request(self, method: str, path: str, json_body, auth: bool) -> dict:
        headers = self._headers() if auth else {"Content-Type": "application/json"}
        try:
            resp = httpx.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.RequestError as e:
            raise ApiError(0, f"Cannot reach server: {e}") from e
        if resp.status_code >= 400:
            detail = _extract_detail(resp)
            raise ApiError(resp.status_code, detail)
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return {}

    # ── auth ───────────────────────────────────────────────────
    def signup(self, email: str, password: str) -> str:
        data = self._post("/auth/signup", {"email": email, "password": password}, auth=False)
        self.token = data["access_token"]
        return self.token

    def login(self, email: str, password: str) -> str:
        data = self._post("/auth/login", {"email": email, "password": password}, auth=False)
        self.token = data["access_token"]
        return self.token

    # ── balance ────────────────────────────────────────────────
    def balance(self) -> dict:
        return self._get("/me/balance")

    # ── sessions ───────────────────────────────────────────────
    def start_session(self) -> dict:
        return self._post("/session/start", {})

    def heartbeat(self, session_id: str, elapsed_secs: float) -> dict:
        return self._post(
            "/session/heartbeat",
            {"session_id": session_id, "elapsed_secs": elapsed_secs},
        )

    def stop_session(self, session_id: str, elapsed_secs: float) -> dict:
        return self._post(
            "/session/stop",
            {"session_id": session_id, "elapsed_secs": elapsed_secs},
        )

    # ── payments ───────────────────────────────────────────────
    def buy_hours(self, hours: float) -> dict:
        return self._post("/payments/buy", {"hours": hours})

    def order_status(self, order_id: str) -> dict:
        return self._get(f"/payments/status/{order_id}")


def _extract_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            d = body["detail"]
            return d if isinstance(d, str) else str(d)
    except Exception:
        pass
    return resp.text or "Request failed"
