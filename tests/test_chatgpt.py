"""ChatGPT pelo plano: testes isolados do login e do ciclo real de conversa e memória."""

from contextlib import contextmanager
import json
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from robin import api, chatgpt, chatgpt_auth
from robin.conversations import ConversationStore, ConversationRecorder, conversation_identity, merge_remote


def _tokens(access="access-for-test"):
    return dict(access_token=access, refresh_token="refresh-for-test", token_type="Bearer",
                expires_in=3600, id_token="id-for-test", scope=chatgpt_auth.SCOPES)


def test_login_verifica_state_e_salva_sessao_protegida(files, monkeypatch):
    path = files.dir / chatgpt_auth.SESSION_FILE
    account = chatgpt_auth.ChatGPTAccount(path)
    attempts = []
    callbacks_completed = threading.Event()

    def browser(url):
        params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        attempts.append(params)

        def callbacks():
            query = {"state": "incorreto", "code": "nao-usar", "client_id": "oaiapp_robin_test"}
            resp = httpx.get(params["redirect_uri"] + "?" + urlencode(query), timeout=5)
            assert resp.status_code == 400
            query.update(state=params["state"], code="code-for-test")
            resp = httpx.get(params["redirect_uri"] + "?" + urlencode(query), timeout=5)
            assert resp.status_code == 200
            callbacks_completed.set()

        threading.Thread(target=callbacks, daemon=True).start()
        return True

    exchanges = []
    monkeypatch.setattr(chatgpt_auth.webbrowser, "open", browser)
    monkeypatch.setattr(account, "_exchange", lambda **fields: exchanges.append(fields) or _tokens())
    monkeypatch.setattr(account, "_identity", lambda token, client, nonce: {"sub": "subject-for-test"})
    account.begin()
    assert callbacks_completed.wait(10)
    deadline = time.monotonic() + 5
    while account.status()["pending"] and time.monotonic() < deadline:
        time.sleep(0.01)

    assert account.status()["connected"]
    assert account.token() == "access-for-test"
    assert chatgpt_auth.ChatGPTAccount(path).token() == "access-for-test"
    assert b"access-for-test" not in path.read_bytes()
    assert len(exchanges) == 1 and exchanges[0]["code"] == "code-for-test"
    assert exchanges[0]["redirect_uri"] == attempts[0]["redirect_uri"]
    assert attempts[0]["code_challenge_method"] == "S256"


def test_id_token_confere_assinatura_conta_e_nonce(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {"kid": "test", **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))}
    monkeypatch.setattr(chatgpt_auth.httpx, "get", lambda url, **kwargs: httpx.Response(
        200, json={"keys": [jwk]}, request=httpx.Request("GET", url)))
    now = int(time.time())
    claims = {"iss": chatgpt_auth.ISSUER, "sub": "test", "aud": "oaiapp_test",
              "nonce": "test-nonce", "iat": now, "exp": now + 120}
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
    verify = chatgpt_auth.ChatGPTAccount._identity
    assert verify(token, "oaiapp_test", "test-nonce")["sub"] == "test"
    with pytest.raises(jwt.InvalidAudienceError):
        verify(token, "oaiapp_other", "test-nonce")
    with pytest.raises(chatgpt_auth.ChatGPTAuthError):
        verify(token, "oaiapp_test", "other-nonce")


def test_renovacao_substitui_token_e_desconectar_preserva_registro(files, monkeypatch):
    path = files.dir / chatgpt_auth.SESSION_FILE
    account = chatgpt_auth.ChatGPTAccount(path)
    account._save({"client_id": "oaiapp_robin_test", "host_id": "test-host", "subject": "test-subject",
                   "access_token": "old", "refresh_token": "old-refresh", "expires_at": 1,
                   "scope": chatgpt_auth.SCOPES})
    refreshed = []
    monkeypatch.setattr(account, "_exchange", lambda **fields: refreshed.append(fields) or _tokens())
    with pytest.raises(chatgpt_auth.ChatGPTAuthError):
        account.token(expected_profile="another-account")
    assert not refreshed
    assert account.token(expected_profile=account.profile_tag()) == "access-for-test"
    assert refreshed == [{"grant_type": "refresh_token", "client_id": "oaiapp_robin_test",
                          "refresh_token": "old-refresh"}]
    assert chatgpt_auth.ChatGPTAccount(path).token() == "access-for-test"

    requests = []
    monkeypatch.setattr(chatgpt_auth.httpx, "post",
                        lambda url, **kwargs: requests.append((url, kwargs)) or httpx.Response(200))
    assert account.disconnect()
    assert requests[0][1]["data"]["token"] == "refresh-for-test"
    state = chatgpt_auth.ChatGPTAccount(path).status()
    assert state["registered"] and not state["connected"]


class _SSE:
    is_error = False

    def __init__(self, *events):
        self.events = events

    def iter_lines(self):
        for event in self.events:
            yield "data: " + json.dumps(event)
            yield ""


def test_robin_chatgpt_sem_gemini_usa_memoria_e_historico_proprios(files, events, monkeypatch):
    # Nenhum token nem tráfego real. O cliente OAuth e o stream são exercitados por interfaces simuladas.
    files.memory.write_text("# Núcleo de teste", encoding="utf-8")
    account = chatgpt_auth.ChatGPTAccount(files.dir / chatgpt_auth.SESSION_FILE)
    account._save(account._with_tokens({"client_id": "oaiapp_robin_test", "subject": "subject-test"}, _tokens()))
    saved = []
    initial_items = [{"type": "reasoning", "id": "rs_teste", "summary": [], "encrypted_content": "opaco"},
                     {"type": "function_call", "call_id": "call_teste", "name": "salvar_memoria", "namespace": chatgpt.MEMORY_NAMESPACE,
                      "arguments": json.dumps({"conteudo": "Guardar a caneta de teste"})}]
    answer = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Anotado."}]}

    @contextmanager
    def stream(method, url, *, json, headers, timeout):
        assert method == "POST" and url == "https://api.openai.com/v1/responses"
        assert headers["Authorization"] == "Bearer access-for-test"
        saved.append(json)
        if len(saved) == 1:
            yield _SSE({"type": "response.completed", "response": {"output": initial_items}})
        else:
            yield _SSE({"type": "response.output_text.delta", "delta": "Anotado."},
                       {"type": "response.completed", "response": {"output": [answer]}})

    monkeypatch.setattr(chatgpt.httpx, "stream", stream)
    legacy = ConversationRecorder(ConversationStore(files.conversations))
    legacy.begin(5, "Histórico só do Gemini")
    robin = api.RobinAPI()
    from conftest import FakeWindow
    robin._attach(FakeWindow(events), None)
    cloud_events = []
    robin._recorder.cloud = SimpleNamespace(
        saved=lambda *_: cloud_events.append("saved"),
        removed=lambda *_: cloud_events.append("removed"),
        deleted=lambda *_: cloud_events.append("deleted"),
    )
    assert robin.get_state()["provider"] == "chatgpt" and robin.get_state()["has_key"]
    assert not robin._api_key
    assert robin._local.embed is None
    result = robin.send_message("Lembre da caneta.")
    assert result["ok"]
    events.wait("onDone", result["gen_id"])
    assert robin.list_brain()["items"][0]["content"] == "Guardar a caneta de teste"
    first, second = saved
    assert first["store"] is False and first["stream"] is True
    assert first["tools"][0]["type"] == "namespace"
    assert first["tools"][0]["name"] == chatgpt.MEMORY_NAMESPACE
    assert "# Núcleo de teste" in first["instructions"]
    assert "Histórico só do Gemini" not in first["instructions"]
    assert robin._chatgpt._conversations.search("Gemini", 10) == []
    assert first["input"] == [{"role": "user", "content": "Lembre da caneta."}]
    assert all(key not in first for key in ("previous_response_id", "conversation"))
    assert {"reasoning", "function_call", "function_call_output"} <= {item.get("type") for item in second["input"]}
    saved_id = robin.list_conversations()["current"]
    conversation = robin._conversations.get(saved_id)
    assert conversation_identity(conversation) == ("chatgpt", account.profile_tag())
    synced = merge_remote(None, {key: value for key, value in conversation.items() if key != "messages"},
                          conversation["messages"])
    assert conversation_identity(synced) == ("chatgpt", account.profile_tag())
    robin.new_conversation()
    assert robin.open_conversation(saved_id)["state"]["provider"] == "chatgpt"
    other = robin._chatgpt_account
    other._save(other._with_tokens({"client_id": "oaiapp_other", "subject": "other"}, _tokens()))
    assert robin.send_message("Não deve sair do Robin.")["code"] == "other_account"
    robin.new_conversation()
    assert robin._chatgpt._conversations.search("caneta", 10) == []
    assert len(saved) == 2
    assert (files.conversations / f"{saved_id}.chatgpt").is_file()
    assert not (files.conversations / f"{saved_id}.json").exists()
    assert robin.delete_conversation(saved_id)["ok"]
    assert not cloud_events
