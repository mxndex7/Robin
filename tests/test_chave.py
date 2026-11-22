"""validate_api_key passa pelo cliente real do google-genai; só o transporte HTTP é simulado.

O transporte simulado fica abaixo do httpx.Client.send, que recusa enviar com o cliente
fechado: um Client temporário encadeado volta a falhar com "client has been closed".
"""

import httpx

from robin.gemini import validate_api_key


def _fake_transport(monkeypatch, status: int, body: dict) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def handle_request(self, request):
        sent.append(request)
        return httpx.Response(status, json=body, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    return sent


def test_chave_valida_passa(monkeypatch):
    sent = _fake_transport(monkeypatch, 200, {
        "models": [{"name": "models/gemini-3.8-flash", "supportedActions": ["generateContent"]}],
    })

    assert validate_api_key("chave-de-teste") is None
    assert len(sent) == 1
    assert sent[0].url.path.endswith("/models")
    assert sent[0].headers["x-goog-api-key"] == "chave-de-teste"


def test_cliente_tem_timeout_de_conexao_e_leitura_sem_prazo_no_servidor(monkeypatch):
    sent = _fake_transport(monkeypatch, 200, {"models": []})

    assert validate_api_key("chave-de-teste") is None

    # Timeout do httpx por operação (o SDK passaria timeout=None) e nenhum X-Server-Timeout,
    # que viraria prazo total no servidor e cortaria respostas longas.
    assert sent[0].extensions["timeout"] == {"connect": 10.0, "read": 60.0, "write": 60.0, "pool": 60.0}
    assert "x-server-timeout" not in sent[0].headers


def test_chave_invalida_vira_erro_traduzido(monkeypatch):
    _fake_transport(monkeypatch, 400, {"error": {
        "code": 400,
        "message": "API key not valid. Please pass a valid API key.",
        "status": "INVALID_ARGUMENT",
        "details": [{"reason": "API_KEY_INVALID"}],
    }})

    error = validate_api_key("chave-errada")

    assert error is not None
    assert error.code == "invalid_key"
