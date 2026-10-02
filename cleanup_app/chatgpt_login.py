"""Sign in with ChatGPT and run an analysis on the user's plan.

This follows OpenAI's local Sign in with ChatGPT flow. The browser login
returns tokens for the public Responses API. It does not read ChatGPT chats.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from cleanup_app.ai_analyst import SYSTEM_PROMPT
from cleanup_app.util import app_dir

ISSUER = "https://auth.openai.com"
DISCOVERY = "https://auth.openai.com/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
RESPONSES_URL = "https://api.openai.com/v1/responses"
MODELS_URL = "https://api.openai.com/v1/models"
USAGE_URL = "https://chatgpt.com/settings/usage"
APP_NAME = "PC Cleanup"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT = "dynamic_agent_client"
CALLBACK_PATH = "/auth/callback"

_refresh_lock = threading.Lock()
_discovery_cache: dict | None = None
_jwks_cache: dict | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("ChatGPT refused the request with an unexpected redirect.")


_opener = urllib.request.build_opener(_NoRedirect)


def account_summary() -> dict:
    data = _load_account()
    models = data.get("models") if isinstance(data.get("models"), list) else []
    slug = data.get("model") if isinstance(data.get("model"), str) else ""
    label = ""
    for item in models:
        if isinstance(item, dict) and item.get("slug") == slug:
            label = str(item.get("label") or slug)
            break
    signed_in = bool(data.get("access_token") and data.get("refresh_token") and data.get("client_id"))
    scopes = data.get("scopes") if isinstance(data.get("scopes"), list) else []
    return {
        "signed_in": signed_in,
        "plan_enabled": signed_in and PLAN_SCOPE in scopes,
        "email": data.get("email") if isinstance(data.get("email"), str) else "",
        "has_registration": bool(data.get("client_id")),
        "models": [item for item in models if isinstance(item, dict) and item.get("slug") and item.get("label")],
        "model_label": label,
    }


def remember_model_label(label: str) -> None:
    data = _load_account()
    for item in data.get("models") or []:
        if isinstance(item, dict) and item.get("label") == label and item.get("slug"):
            data["model"] = item["slug"]
            _save_account(data)
            return


def open_usage_settings() -> None:
    webbrowser.open(USAGE_URL)


def sign_in(cancel, progress, *, new_account: bool = False, consent: bool = False) -> dict:
    progress("Opening ChatGPT in your browser…")
    endpoints = _discovery()
    host = _host_id()
    saved = _load_account()
    client_id = DYNAMIC_CLIENT
    if not new_account and isinstance(saved.get("client_id"), str) and saved.get("client_id"):
        client_id = saved["client_id"]
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    port, result = _wait_for_callback(
        cancel,
        endpoints["authorization"],
        verifier,
        state,
        nonce,
        host,
        client_id,
        None if new_account else saved.get("email"),
        None if new_account else saved.get("id_token"),
        consent and client_id != DYNAMIC_CLIENT,
    )
    del port
    if cancel.is_set():
        raise RuntimeError("Sign-in was stopped.")
    _check_callback(result, state)
    issued = result.get("client_id") or ("" if client_id == DYNAMIC_CLIENT else client_id)
    if client_id == DYNAMIC_CLIENT:
        if not _safe_client_id(issued):
            raise RuntimeError("ChatGPT did not finish registering this app. Try again.")
    elif issued and issued != client_id:
        raise RuntimeError("ChatGPT returned a different app registration. Sign-in was discarded.")
    else:
        issued = client_id
    saved["client_id"] = issued
    saved["ext_agent_host_id"] = host
    _save_account(saved)
    progress("Checking the ChatGPT sign-in…")
    token = _form_post(
        endpoints["token"],
        {
            "grant_type": "authorization_code",
            "client_id": issued,
            "code": result["code"],
            "code_verifier": verifier,
            "redirect_uri": result["redirect_uri"],
            "resource": RESOURCE,
        },
    )
    expected_subject = None if new_account else saved.get("subject")
    first_plan = _store_token_result(saved, token, issued, nonce, expected_subject)
    try:
        models = _fetch_models(saved["access_token"])
    except (OSError, RuntimeError, ValueError):
        models = saved.get("models") if isinstance(saved.get("models"), list) else []
    if models:
        saved["models"] = models
        if saved.get("model") not in {item["slug"] for item in models}:
            saved["model"] = models[0]["slug"]
        _save_account(saved)
    summary = account_summary()
    summary["first_plan"] = first_plan
    return summary


def sign_out() -> bool:
    data = _load_account()
    refresh = data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else ""
    client_id = data.get("client_id") if isinstance(data.get("client_id"), str) else ""
    revoked = True
    if refresh and client_id:
        try:
            endpoints = _discovery()
            _form_post(
                endpoints["revocation"],
                {"token": refresh, "token_type_hint": "refresh_token", "client_id": client_id},
            )
        except (OSError, RuntimeError, ValueError):
            revoked = False
    data["access_token"] = ""
    data["refresh_token"] = ""
    data["id_token"] = ""
    data["scopes"] = []
    data["expires_at"] = 0
    _save_account(data)
    return revoked


def ask_with_plan(model_label: str, report: str, progress, cancel) -> str:
    if cancel.is_set():
        raise RuntimeError("Stopped.")
    if not report.strip():
        raise ValueError("Build the local report first.")
    summary = account_summary()
    if not summary["plan_enabled"]:
        raise RuntimeError("ChatGPT plan usage is off. Allow it on this page, then try again.")
    progress("Checking your ChatGPT sign-in…")
    token = _fresh_access_token(False)
    slug = _slug_for_label(model_label)
    if not slug:
        models = _fetch_models(token)
        data = _load_account()
        data["models"] = models
        slug = models[0]["slug"]
        data["model"] = slug
        _save_account(data)
    progress("Sending the report with your ChatGPT plan…")
    try:
        return _stream_answer(token, slug, report, progress, cancel)
    except _Unauthorized:
        progress("Refreshing ChatGPT sign-in…")
        token = _fresh_access_token(True)
        return _stream_answer(token, slug, report, progress, cancel)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def endpoint_is_openai(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "auth.openai.com":
        return False
    if parsed.port not in (None, 443) or parsed.username or parsed.password:
        return False
    return parsed.path.startswith("/api/accounts/") or parsed.path.startswith("/.well-known/")


def build_authorize_url(
    authorize_endpoint: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    verifier: str,
    host_id: str,
    client_id: str,
    login_hint: str | None = None,
    id_token_hint: str | None = None,
    consent: bool = False,
) -> str:
    if not endpoint_is_openai(authorize_endpoint):
        raise ValueError("Refusing a sign-in address that is not OpenAI's.")
    if not redirect_uri.startswith("http://127.0.0.1:") or not redirect_uri.endswith(CALLBACK_PATH):
        raise ValueError("The sign-in return address must stay on this PC.")
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge(verifier),
        "ext_agent_host_id": host_id,
    }
    if client_id == DYNAMIC_CLIENT:
        params["agent_name_hint"] = APP_NAME
    if login_hint:
        params["login_hint"] = login_hint
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    if consent:
        params["prompt"] = "consent"
    return authorize_endpoint + "?" + urllib.parse.urlencode(params)


def completed_text(raw: bytes) -> str:
    completed = False
    parts: list[str] = []
    for event in _sse_events(raw):
        kind = event.get("type")
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                parts.append(delta)
        elif kind == "response.output_text.done" and not parts:
            text = event.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "response.failed":
            raise RuntimeError(_failure_message(event))
        elif kind == "response.incomplete":
            raise RuntimeError("ChatGPT stopped before the analysis finished.")
        elif kind == "error":
            raise RuntimeError(_failure_message(event))
        elif kind == "response.completed":
            completed = True
            if not parts:
                parts.append(_text_from_completed(event))
    if not completed:
        raise RuntimeError("ChatGPT closed the reply before it finished.")
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("ChatGPT returned an empty analysis.")
    return text


def verify_id_token(token: str, client_id: str, nonce: str | None) -> dict:
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError("ChatGPT sign-in could not be verified.")
    try:
        header = json.loads(_b64url(parts[0]))
        payload = json.loads(_b64url(parts[1]))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("ChatGPT sign-in could not be verified.") from exc
    if not isinstance(header, dict) or header.get("alg") != "RS256":
        raise ValueError("ChatGPT sign-in could not be verified.")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise ValueError("ChatGPT sign-in could not be verified.")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    signature = _b64url(parts[2])
    _verify_rs256(kid, signing_input, signature)
    if not isinstance(payload, dict):
        raise ValueError("ChatGPT sign-in could not be verified.")
    if payload.get("iss") != ISSUER:
        raise ValueError("ChatGPT sign-in could not be verified.")
    audience = payload.get("aud")
    if audience != client_id and not (isinstance(audience, list) and client_id in audience):
        raise ValueError("ChatGPT sign-in could not be verified.")
    now = time.time()
    exp = payload.get("exp")
    iat = payload.get("iat")
    if not isinstance(exp, (int, float)) or exp < now - 5:
        raise ValueError("ChatGPT sign-in expired. Try again.")
    if not isinstance(iat, (int, float)) or iat > now + 5:
        raise ValueError("ChatGPT sign-in could not be verified.")
    nbf = payload.get("nbf")
    if nbf is not None and (not isinstance(nbf, (int, float)) or nbf > now + 5):
        raise ValueError("ChatGPT sign-in could not be verified.")
    if nonce is not None and payload.get("nonce") != nonce:
        raise ValueError("ChatGPT sign-in could not be verified.")
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError("ChatGPT sign-in could not be verified.")
    email = payload.get("email") if isinstance(payload.get("email"), str) else ""
    return {"sub": subject, "email": email}


class _Unauthorized(RuntimeError):
    def __init__(self) -> None:
        super().__init__("ChatGPT rejected the sign-in. Sign in again on the AI page.")


def _verify_rs256(kid: str, signing_input: bytes, signature: bytes) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:
        raise RuntimeError("Sign-in support is missing. Start the app with run.bat so it can install.") from exc
    key_data = _jwk_for_kid(kid)
    try:
        numbers = rsa.RSAPublicNumbers(
            int.from_bytes(_b64url(key_data["e"]), "big"),
            int.from_bytes(_b64url(key_data["n"]), "big"),
        )
        numbers.public_key().verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, KeyError, ValueError) as exc:
        raise ValueError("ChatGPT sign-in could not be verified.") from exc


def _jwk_for_kid(kid: str) -> dict:
    keys = _jwks(False)
    for key in keys:
        if key.get("kid") == kid and key.get("kty") == "RSA":
            return key
    keys = _jwks(True)
    for key in keys:
        if key.get("kid") == kid and key.get("kty") == "RSA":
            return key
    raise ValueError("ChatGPT sign-in could not be verified.")


def _jwks(force: bool) -> list:
    global _jwks_cache
    now = time.time()
    cached = _usable_jwks_cache(_jwks_cache, now)
    if not force and cached is not None:
        return cached
    endpoints = _discovery()
    request = urllib.request.Request(endpoints["jwks_uri"], headers={"Accept": "application/json"})
    try:
        with _opener.open(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not check ChatGPT's sign-in keys.") from exc
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, list):
        raise RuntimeError("Could not check ChatGPT's sign-in keys.")
    _jwks_cache = {"expires": now + 600, "keys": keys}
    return keys


def _usable_jwks_cache(cached, now: float) -> list | None:
    if not isinstance(cached, dict):
        return None
    keys = cached.get("keys")
    try:
        fresh = float(cached.get("expires") or 0) > now
    except (TypeError, ValueError):
        return None
    if fresh and isinstance(keys, list):
        return keys
    return None


def _discovery() -> dict:
    global _discovery_cache
    now = time.time()
    if _discovery_cache and _discovery_cache.get("expires", 0) > now:
        return _discovery_cache
    request = urllib.request.Request(DISCOVERY, headers={"Accept": "application/json"})
    try:
        with _opener.open(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not reach ChatGPT sign-in. Check the internet connection and try again.") from exc
    if not isinstance(payload, dict) or payload.get("issuer") != ISSUER:
        raise RuntimeError("ChatGPT sign-in configuration did not match OpenAI.")
    found = {
        "authorization": payload.get("authorization_endpoint"),
        "token": payload.get("token_endpoint"),
        "jwks_uri": payload.get("jwks_uri"),
        "revocation": payload.get("revocation_endpoint"),
    }
    if not all(isinstance(value, str) and endpoint_is_openai(value) for value in found.values()):
        raise RuntimeError("ChatGPT sign-in configuration did not match OpenAI.")
    found["expires"] = now + 3600
    _discovery_cache = found
    return found


def _wait_for_callback(cancel, authorize, verifier, state, nonce, host, client_id, email, id_token, consent):
    box: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH or "result" in box:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
            box["result"] = {key: values[0] for key, values in query.items() if values}
            body = b"<html><body><p>You can close this tab and return to PC Cleanup.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 0.5
    host_name, port = server.server_address
    if host_name != "127.0.0.1":
        server.server_close()
        raise RuntimeError("Sign-in could not listen on this PC.")
    redirect = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    url = build_authorize_url(
        authorize,
        redirect,
        state,
        nonce,
        verifier,
        host,
        client_id,
        email if isinstance(email, str) and email else None,
        id_token if isinstance(id_token, str) and id_token else None,
        consent,
    )
    deadline = time.time() + 300
    try:
        _open_browser(url)
        while time.time() < deadline:
            if cancel.is_set():
                raise RuntimeError("Sign-in was stopped.")
            server.handle_request()
            if "result" in box:
                box["result"]["redirect_uri"] = redirect
                return port, box["result"]
        raise RuntimeError("ChatGPT sign-in timed out. Try again from the AI page.")
    finally:
        server.server_close()


def _open_browser(url: str) -> None:
    try:
        if webbrowser.open(url, new=1):
            return
    except Exception:
        pass
    try:
        os.startfile(url)
    except OSError:
        raise RuntimeError("Could not open the browser for ChatGPT sign-in.") from None


def _check_callback(result: dict, state: str) -> None:
    returned = result.get("state") or ""
    if not returned or len(returned) != len(state) or not hmac.compare_digest(returned, state):
        raise RuntimeError("ChatGPT sign-in did not match this attempt. Try again.")
    error = result.get("error")
    if error:
        if error == "access_denied":
            raise RuntimeError("ChatGPT sign-in was cancelled, or plan usage was not allowed.")
        code = error if isinstance(error, str) and error.replace("_", "").isalnum() else "unknown"
        raise RuntimeError(f"ChatGPT sign-in did not finish ({code}).")
    if not result.get("code"):
        raise RuntimeError("ChatGPT sign-in did not finish. Try again.")


def _store_token_result(saved: dict, token: dict, client_id: str, nonce: str | None, expected_subject: str | None) -> bool:
    if not isinstance(token, dict):
        raise RuntimeError("ChatGPT sign-in did not finish. Try again.")
    id_token = token.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise RuntimeError("ChatGPT did not confirm the account.")
    identity = verify_id_token(id_token, client_id, nonce)
    if expected_subject and identity["sub"] != expected_subject:
        raise RuntimeError("That ChatGPT account does not match the one saved on this PC. Choose Use another account to switch.")
    access = token.get("access_token")
    refresh = token.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise RuntimeError("ChatGPT did not allow this app to use the account. Try again and approve the requested access.")
    scopes = str(token.get("scope") or "").split()
    expires_in = token.get("expires_in")
    try:
        lifetime = int(expires_in)
    except (TypeError, ValueError):
        lifetime = 3600
    had_plan = PLAN_SCOPE in (saved.get("scopes") or []) and bool(saved.get("welcomed"))
    saved.update(
        {
            "issuer": ISSUER,
            "subject": identity["sub"],
            "email": identity["email"] or saved.get("email") or "",
            "client_id": client_id,
            "id_token": id_token,
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_at": time.time() + max(0, lifetime),
            "scopes": scopes,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    first_plan = PLAN_SCOPE in scopes and not had_plan
    if first_plan:
        saved["welcomed"] = True
    _save_account(saved)
    return first_plan


def _fresh_access_token(force: bool) -> str:
    with _refresh_lock:
        data = _load_account()
        if not force and _token_is_fresh(data):
            return data["access_token"]
        refresh = data.get("refresh_token")
        client_id = data.get("client_id")
        if not isinstance(refresh, str) or not refresh or not isinstance(client_id, str) or not client_id:
            raise RuntimeError("Sign in with ChatGPT first.")
        endpoints = _discovery()
        try:
            token = _form_post(
                endpoints["token"],
                {
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh,
                    "resource": RESOURCE,
                },
            )
        except RuntimeError as exc:
            if getattr(exc, "oauth_error", "") in {
                "invalid_grant",
                "invalid_refresh_token",
                "token_expired",
                "refresh_token_expired",
                "refresh_token_invalidated",
                "refresh_token_reused",
            }:
                data["access_token"] = ""
                data["refresh_token"] = ""
                data["id_token"] = ""
                data["scopes"] = []
                data["expires_at"] = 0
                _save_account(data)
                raise RuntimeError("ChatGPT sign-in expired. Sign in again on the AI page.") from None
            raise
        if not isinstance(token, dict):
            raise RuntimeError("ChatGPT did not renew the sign-in. Try again.")
        access = token.get("access_token")
        rotated = token.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(rotated, str) or not rotated:
            raise RuntimeError("ChatGPT did not renew the sign-in. Sign in again.")
        id_token = token.get("id_token")
        if isinstance(id_token, str) and id_token:
            identity = verify_id_token(id_token, client_id, None)
            if data.get("subject") and identity["sub"] != data.get("subject"):
                raise RuntimeError("ChatGPT renewed a different account. Sign in again.")
            data["id_token"] = id_token
            if identity["email"]:
                data["email"] = identity["email"]
        if token.get("scope"):
            data["scopes"] = str(token.get("scope")).split()
        lifetime = token.get("expires_in")
        try:
            seconds = int(lifetime)
        except (TypeError, ValueError):
            seconds = 3600
        data["access_token"] = access
        data["refresh_token"] = rotated
        data["expires_at"] = time.time() + max(0, seconds)
        _save_account(data)
        return access


def _token_is_fresh(data: dict) -> bool:
    try:
        expires = float(data.get("expires_at") or 0)
    except (TypeError, ValueError):
        return False
    return bool(data.get("access_token")) and expires > time.time() + 60


def _fetch_models(token: str) -> list[dict]:
    request = urllib.request.Request(
        MODELS_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with _opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise _Unauthorized()
        raise RuntimeError(_http_plan_message(exc.code, exc.read().decode("utf-8", errors="replace"))) from None
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not load the ChatGPT model list.") from exc
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("models") if isinstance(payload.get("models"), list) else payload.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("ChatGPT did not list any models for this account.")
    models = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("visibility") not in (None, "list"):
            continue
        slug = row.get("slug") or row.get("id")
        if not isinstance(slug, str) or not slug or slug in seen:
            continue
        label = row.get("display_name") if isinstance(row.get("display_name"), str) else slug
        if any(item["label"] == label for item in models):
            label = f"{label} ({slug})"
        seen.add(slug)
        models.append({"slug": slug, "label": label})
    if not models:
        for row in rows:
            if not isinstance(row, dict):
                continue
            slug = row.get("slug") or row.get("id")
            if isinstance(slug, str) and slug and slug not in seen:
                seen.add(slug)
                models.append({"slug": slug, "label": slug})
    if not models:
        raise RuntimeError("ChatGPT did not list any models for this account.")
    return models


def _slug_for_label(label: str) -> str:
    data = _load_account()
    wanted = (label or "").strip()
    for item in data.get("models") or []:
        if isinstance(item, dict) and (item.get("label") == wanted or item.get("slug") == wanted):
            return str(item.get("slug") or "")
    if isinstance(data.get("model"), str) and data.get("model") and not wanted:
        return data["model"]
    return wanted


def _stream_answer(token: str, slug: str, report: str, progress, cancel) -> str:
    payload = {
        "model": slug,
        "instructions": SYSTEM_PROMPT,
        "input": [{"role": "user", "content": report}],
        "store": False,
        "stream": True,
    }
    request = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        response = _opener.open(request, timeout=180)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise _Unauthorized()
        raise RuntimeError(_http_plan_message(exc.code, body)) from None
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("Could not reach ChatGPT.") from exc
    chunks = []
    try:
        while True:
            if cancel.is_set():
                raise RuntimeError("Stopped.")
            piece = response.read(4096)
            if not piece:
                break
            chunks.append(piece)
            if sum(len(item) for item in chunks) > 2_000_000:
                raise RuntimeError("ChatGPT sent an unexpectedly large reply.")
    finally:
        response.close()
    progress("Reading the ChatGPT reply…")
    return completed_text(b"".join(chunks))


def _sse_events(raw: bytes):
    buffer = raw
    while b"\n\n" in buffer:
        block, buffer = buffer.split(b"\n\n", 1)
        event = _event_from_block(block)
        if event is not None:
            yield event
    event = _event_from_block(buffer)
    if event is not None:
        yield event


def _event_from_block(block: bytes) -> dict | None:
    data_lines = []
    for line in block.splitlines():
        if line.startswith(b"data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return None
    raw = b"\n".join(data_lines)
    if raw == b"[DONE]":
        return {"type": "done"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _text_from_completed(event: dict) -> str:
    response = event.get("response")
    output = response.get("output") if isinstance(response, dict) else None
    if not isinstance(output, list):
        return ""
    parts = []
    for item in output:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "".join(parts)


def _failure_message(event: dict) -> str:
    response = event.get("response") if isinstance(event.get("response"), dict) else {}
    error = response.get("error") if isinstance(response.get("error"), dict) else event.get("error")
    code = ""
    if isinstance(error, dict):
        code = str(error.get("code") or "")
    return _plan_code_message(0, code, "")


def _http_plan_message(status: int, body: str) -> str:
    code = ""
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "")
        elif isinstance(error, str):
            code = error
    except json.JSONDecodeError:
        code = ""
    return _plan_code_message(status, code, "")


def _plan_code_message(status: int, code: str, fallback: str) -> str:
    if code in {"subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"} or status == 429:
        return "The ChatGPT plan limit for this app was reached. Open Manage usage to review it."
    if code == "subscription_sharing_user_not_eligible":
        return "This ChatGPT account cannot use its plan in this app. Check the account, or use an API key instead."
    if code == "subscription_sharing_route_not_supported":
        return "ChatGPT refused this kind of request."
    if status in {401, 403}:
        return "ChatGPT rejected the sign-in. Sign in again on the AI page."
    return fallback or "ChatGPT could not finish the analysis."


def _form_post(url: str, fields: dict) -> dict:
    if not endpoint_is_openai(url):
        raise RuntimeError("Refusing to send sign-in details anywhere except OpenAI.")
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise _oauth_failure(exc.code, body) from None
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("Could not reach ChatGPT sign-in.") from exc
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ChatGPT sign-in returned an unreadable reply.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ChatGPT sign-in returned an unreadable reply.")
    return payload


def _oauth_failure(status: int, body: str) -> RuntimeError:
    code = ""
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            code = payload["error"]
    except json.JSONDecodeError:
        code = ""
    if code == "invalid_grant":
        error = RuntimeError("That ChatGPT sign-in expired before it finished. Try again.")
    else:
        error = RuntimeError("ChatGPT sign-in was rejected. Try again.")
    error.oauth_error = code or str(status)
    return error


def _safe_client_id(value: str) -> bool:
    if not isinstance(value, str) or value == DYNAMIC_CLIENT:
        return False
    if not 8 <= len(value) <= 200:
        return False
    return all(character.isalnum() or character in "._-" for character in value)


def _host_id() -> str:
    path = os.path.join(app_dir(), "chatgpt_host.json")
    try:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        current = saved.get("ext_agent_host_id") if isinstance(saved, dict) else ""
        if isinstance(current, str) and current.startswith("urn:uuid:") and len(current) == 45:
            return current
    except (OSError, json.JSONDecodeError):
        pass
    current = "urn:uuid:" + str(uuid.uuid4())
    _write_json(path, {"ext_agent_host_id": current})
    return current


def _account_path() -> str:
    return os.path.join(app_dir(), "chatgpt_account.json")


def _load_account() -> dict:
    try:
        with open(_account_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_account(data: dict) -> None:
    _write_json(_account_path(), data)


def _write_json(path: str, data: dict) -> None:
    temporary = path + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temporary, path)


def _b64url(value: str | bytes) -> bytes:
    if isinstance(value, str):
        raw = value.encode("ascii")
    else:
        raw = value
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
