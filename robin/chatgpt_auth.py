"""Login do ChatGPT para o Robin, com callback local e credenciais protegidas pelo Windows."""

import base64
import ctypes
from ctypes import wintypes
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid
import webbrowser

import httpx
import jwt

from .brain import atomic_write

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
CALLBACK = "/auth/callback"
DYNAMIC_CLIENT = "dynamic_agent_client"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
SESSION_FILE = "chatgpt-session.bin"
RECONNECT = "Entre novamente com o ChatGPT nas Configurações."


class ChatGPTAuthError(Exception):
    def __init__(self, message: str, code: str = "auth"):
        super().__init__(message)
        self.code = code


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_secret(data: bytes, *, protect: bool) -> bytes:
    """DPAPI no escopo do usuário Windows, com interface desabilitada."""
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = _Blob(len(data), buffer)
    outgoing = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    method = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    method.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    method.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not method(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


class _CallbackServer(HTTPServer):
    def handle_error(self, _request, _address):
        pass  # Não registra exceções com o URL de autorização.

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address


class _Callback(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # URL do callback pode conter o código.

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != CALLBACK:
            self.send_error(404)
            return
        try:
            values = parse_qs(url.query, max_num_fields=16)
            state = values.get("state", [])
            valid = len(state) == 1 and secrets.compare_digest(state[0].encode(), self.server.oauth_state.encode())
        except ValueError:
            valid = False
        if not valid:
            self.send_error(400, "Invalid authorization response")
            return
        self.server.oauth_result = values
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write("Volte ao Robin para ver o resultado da conexão.".encode())


class ChatGPTAccount:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.RLock()
        self._record = {}
        self._profiles = {}
        self._attempt = None
        self._message = ""
        try:
            if path.exists():
                saved = json.loads(_windows_secret(base64.b64decode(path.read_bytes()), protect=False))
                if not isinstance(saved, dict):
                    raise ValueError
                self._record = saved.get("active", saved)
                self._profiles = saved.get("profiles", {})
                if not isinstance(self._record, dict) or not isinstance(self._profiles, dict):
                    raise ValueError
        except (ValueError, OSError, TypeError):
            self._record = {}
            self._profiles = {}
            self._message = "O Windows não conseguiu abrir a conexão salva. Entre novamente."

    def _save(self, record: dict) -> None:
        profiles = dict(self._profiles)
        if record.get("client_id"):
            profiles[record["client_id"]] = self._registration(record)
        saved = {"active": record, "profiles": profiles}
        protected = _windows_secret(json.dumps(saved).encode(), protect=True)
        atomic_write(self._path, base64.b64encode(protected).decode(), ".chatgpt-")
        self._record, self._profiles = record, profiles

    def status(self) -> dict:
        with self._lock:
            return {"connected": bool(self._record.get("access_token")), "pending": self._attempt is not None,
                    "account": self._record.get("email", ""), "message": self._message,
                    "profiles": [{"id": key, "label": f"{profile.get('email') or 'Conta ChatGPT'} · {key[-6:]}"}
                                 for key, profile in self._profiles.items()],
                    "registered": bool(self._profiles)}

    def profile_tag(self) -> str:
        with self._lock:
            client = self._record.get("client_id")
            subject = self._record.get("subject")
            return hashlib.sha256(f"{client}:{subject}".encode()).hexdigest() if client and subject else ""

    def begin(self, *, new_account: bool = False, client_id: str = "") -> None:
        with self._lock:
            if self._attempt is not None:
                raise ChatGPTAuthError("Já existe uma conexão em andamento.")
            host = self._record.get("host_id") or f"urn:uuid:{uuid.uuid4()}"
            if client_id and client_id not in self._profiles:
                raise ChatGPTAuthError("Essa conexão salva não existe.")
            registration = {} if new_account else self._profiles.get(client_id, self._record)
            if not self._record.get("host_id"):
                self._save({**self._record, "host_id": host})
            attempt = threading.Event()
            self._attempt = attempt
            self._message = ""
        threading.Thread(target=self._authorize, args=(attempt, host, registration), daemon=True).start()

    def cancel(self) -> None:
        with self._lock:
            if self._attempt:
                self._attempt.set()
                self._attempt = None

    def _authorize(self, attempt, host: str, registration: dict) -> None:
        state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        client_id = registration.get("client_id") or DYNAMIC_CLIENT
        try:
            with _CallbackServer(("127.0.0.1", 0), _Callback) as server:
                server.timeout = 0.25
                server.oauth_result = None
                server.oauth_state = state
                redirect = f"http://127.0.0.1:{server.server_port}{CALLBACK}"
                params = dict(client_id=client_id, ext_agent_host_id=host, response_type="code",
                              redirect_uri=redirect, scope=SCOPES, resource=RESOURCE, state=state,
                              nonce=nonce, code_challenge_method="S256", code_challenge=challenge)
                if client_id == DYNAMIC_CLIENT:
                    params["agent_name_hint"] = "Robin"
                else:
                    if registration.get("email"):
                        params["login_hint"] = registration["email"]
                    if registration.get("needs_consent"):
                        params["prompt"] = "consent"
                if not webbrowser.open(ISSUER + "/api/accounts/authorize?" + urlencode(params)):
                    raise ChatGPTAuthError("Não foi possível abrir o navegador.")
                deadline = time.monotonic() + 180
                while server.oauth_result is None and not attempt.is_set() and time.monotonic() < deadline:
                    server.handle_request()
                result = server.oauth_result
            if attempt.is_set():
                return
            if result is None:
                raise ChatGPTAuthError("O tempo para conectar terminou. Tente novamente.")
            if result.get("error"):
                raise ChatGPTAuthError("O acesso do Robin ao plano do ChatGPT não foi autorizado.")
            issued = result.get("client_id", [client_id])
            code = result.get("code", [])
            if (len(issued) != 1 or not issued[0].startswith("oaiapp_") or len(code) != 1
                    or (client_id != DYNAMIC_CLIENT and client_id != issued[0])):
                raise ChatGPTAuthError("O registro do Robin no ChatGPT ficou incompleto.")
            tokens = self._exchange(grant_type="authorization_code", client_id=issued[0], code=code[0],
                                    code_verifier=verifier, redirect_uri=redirect)
            identity = self._identity(tokens.get("id_token"), issued[0], nonce)
            if registration.get("subject") and registration["subject"] != identity["sub"]:
                raise ChatGPTAuthError("A conta escolhida é diferente da conexão salva. Use Trocar conta.")
            registration = {"client_id": issued[0], "host_id": host, "subject": identity["sub"],
                            "email": identity.get("email", "")}
            scopes = tokens.get("scope") or ""
            with self._lock:
                if self._attempt is attempt:
                    if PLAN_SCOPE not in scopes.split():
                        self._save({**registration, "needs_consent": True})
                        self._message = "Conecte novamente e autorize o uso da cota do ChatGPT para conversar."
                    else:
                        self._save(self._with_tokens(registration, tokens))
        except ChatGPTAuthError as error:
            with self._lock:
                if self._attempt is attempt:
                    self._message = str(error)
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, jwt.PyJWTError):
            with self._lock:
                if self._attempt is attempt:
                    self._message = "Não foi possível validar ou salvar a conexão com o ChatGPT."
        finally:
            with self._lock:
                if self._attempt is attempt:
                    self._attempt = None

    @staticmethod
    def _identity(token: str, client_id: str, nonce: str) -> dict:
        if not isinstance(token, str):
            raise ChatGPTAuthError("O ChatGPT não confirmou a identidade da conta.")
        response = httpx.get(ISSUER + "/.well-known/jwks.json", timeout=10)
        response.raise_for_status()
        kid = jwt.get_unverified_header(token).get("kid")
        key = next((key for key in response.json().get("keys", []) if kid and key.get("kid") == kid), None)
        if key is None:
            raise ChatGPTAuthError("A assinatura da conta do ChatGPT não pôde ser verificada.")
        claims = jwt.decode(token, jwt.PyJWK.from_dict(key, algorithm="RS256").key, algorithms=["RS256"],
                            audience=client_id, issuer=ISSUER, leeway=5,
                            options={"require": ["sub", "exp", "iat", "nonce"]})
        if claims["nonce"] != nonce or not claims["sub"]:
            raise ChatGPTAuthError("A identidade recebida não corresponde a esta solicitação.")
        return claims

    @staticmethod
    def _exchange(**data) -> dict:
        response = httpx.post(TOKEN_URL, data={**data, "resource": RESOURCE}, timeout=15)
        result = response.json()
        if response.is_error:
            terminal = result.get("error") in {"invalid_grant", "invalid_refresh_token", "token_expired",
                                                "refresh_token_expired", "refresh_token_invalidated",
                                                "refresh_token_reused"}
            raise ChatGPTAuthError(RECONNECT if terminal else "Falha ao conectar ao ChatGPT. Tente novamente.",
                                   "expired" if terminal else "auth")
        return result

    @staticmethod
    def _with_tokens(record: dict, tokens: dict) -> dict:
        scopes = tokens.get("scope", record.get("scope", ""))
        if not isinstance(scopes, str) or PLAN_SCOPE not in scopes.split():
            raise ChatGPTAuthError("O plano do ChatGPT não foi autorizado para uso no Robin.", "expired")
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise ChatGPTAuthError("O ChatGPT retornou credenciais incompletas.")
        try:
            lifetime = int(tokens["expires_in"])
            earliest = float(tokens.get("earliest_refresh_at") or 0)
        except (ValueError, TypeError, KeyError):
            raise ChatGPTAuthError("O ChatGPT retornou uma validade de sessão inválida.") from None
        if str(tokens.get("token_type", "")).lower() != "bearer" or lifetime <= 0:
            raise ChatGPTAuthError("O ChatGPT retornou credenciais incompletas.")
        return {**record, "access_token": access, "refresh_token": refresh, "scope": scopes,
                "expires_at": time.time() + lifetime, "earliest_refresh_at": earliest}

    def _registration(self, record: dict) -> dict:
        return {key: record[key] for key in ("host_id", "client_id", "subject", "email", "needs_consent") if key in record}

    def token(self, *, expected_profile: str | None = None) -> str:
        with self._lock:
            if expected_profile is not None and expected_profile != self.profile_tag():
                raise ChatGPTAuthError("A conta do ChatGPT mudou. Inicie uma nova conversa.")
            access = self._record.get("access_token")
            if not access:
                raise ChatGPTAuthError(RECONNECT, "expired")
            now = time.time()
            expires = self._record["expires_at"]
            if expires > now + 60 or (expires > now and now < self._record.get("earliest_refresh_at", 0)):
                return access
            try:
                tokens = self._exchange(grant_type="refresh_token", client_id=self._record["client_id"],
                                        refresh_token=self._record["refresh_token"])
                self._save(self._with_tokens(self._record, tokens))
            except ChatGPTAuthError as error:
                if error.code == "expired":
                    self._save(self._registration(self._record))
                    self._message = RECONNECT
                raise
            return self._record["access_token"]

    def invalidate(self, access: str) -> None:
        """Desativa só a sessão que o provedor recusou, mesmo se outra já entrou."""
        with self._lock:
            if self._record.get("access_token") == access:
                self._save(self._registration(self._record))
                self._message = RECONNECT

    def disconnect(self) -> bool:
        """Encerra a sessão renovável e mantém só o registro para a próxima autorização."""
        with self._lock:
            self.cancel()
            refresh = self._record.get("refresh_token")
            revoked = not refresh
            if refresh:
                try:
                    response = httpx.post(ISSUER + "/api/accounts/oauth/revoke",
                                          data={"token": refresh, "token_type_hint": "refresh_token",
                                                "client_id": self._record["client_id"]}, timeout=8)
                    revoked = response.status_code == 200
                except httpx.HTTPError:
                    pass
                finally:
                    self._save(self._registration(self._record))
            self._message = "" if revoked else "Sessão removida do Robin. Confira a revogação nas configurações do ChatGPT."
            return revoked
