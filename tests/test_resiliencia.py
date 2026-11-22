"""Watchdog, nova tentativa automática, fallback temporário, raciocínio e detalhes do erro.

Os prazos são encurtados pela fixture resilience (watchdog 0,3s, espera 0,05s, "lento" 1s).
Falhas pontuais vêm de requests.script: cada gerador substitui um pedido à rede simulada.
"""

import logging
import time

from conftest import TIMEOUT, history, send_and_wait
from google.genai import errors, types

from robin import mock
from robin.config import DEFAULT_MODEL, FALLBACK_MODEL


def _server_error() -> errors.ServerError:
    return errors.ServerError(503, {"error": {
        "code": 503, "status": "UNAVAILABLE",
        "message": "This model is currently experiencing high demand. Please try again later.",
    }})


def _text_chunk(text: str, last: bool = False) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        finish_reason=types.FinishReason.STOP if last else None,
    )])


def server_error_at_start():
    raise _server_error()
    yield  # gerador: o erro sai no primeiro pedaço, como no SDK


def thought_then_server_error():
    yield mock._thought_chunk()
    raise _server_error()


def text_only():
    yield _text_chunk("resposta ")
    yield _text_chunk("sem raciocínio", last=True)


def text_then_thought():
    yield _text_chunk("começo ")
    yield mock._thought_chunk()
    yield _text_chunk("fim", last=True)


# ---------- watchdog ----------

def test_watchdog_esgota_tentativa_reserva_e_vira_timeout(service, events, requests, resilience, monkeypatch):
    monkeypatch.setattr(mock, "FALLBACK_MODEL", "nenhum")  # a reserva também fica só raciocinando

    service.send("teste lento", 1)
    error = events.wait("onError", 1, timeout=TIMEOUT)

    assert error["code"] == "timeout"
    assert error["message"] == "O Gemini não respondeu a tempo."
    assert error["detail"].startswith("WatchdogTimeout: nenhum pedaço do stream")
    assert error["partial"] is False
    assert events.of("onRetry", 1) == [{"gen_id": 1, "code": "timeout"}]
    assert events.of("onModelChanged") == [{"model": FALLBACK_MODEL, "previous": DEFAULT_MODEL, "reason": "temporary",
                                            "cause": "transient", "expires_in": 300}]
    assert [call["model"] for call in requests.calls] == [DEFAULT_MODEL, DEFAULT_MODEL, FALLBACK_MODEL]
    # As três tentativas partiram do mesmo histórico (vazio).
    assert [requests.texts(i) for i in range(3)] == [[("user", "teste lento")]] * 3

    # As tentativas abandonadas terminam depois e não gravam nada na sessão.
    time.sleep(mock.SLOW_SECONDS + 0.3)
    assert history(service) == []
    send_and_wait(service, events, "oi", 2)
    assert [role for role, _ in history(service)] == ["user", "model"]
    assert history(service)[0] == ("user", "oi")


def test_watchdog_e_a_reserva_responde_nesta_sessao(service, events, requests, resilience):
    send_and_wait(service, events, "teste lento", 1)

    assert events.sequence(1) == [
        "onStart", "onThinking", "onRetry", "onThinking", "onThinking", "onChunk", "onDone",
    ]
    assert events.of("onModelChanged") == [{"model": FALLBACK_MODEL, "previous": DEFAULT_MODEL, "reason": "temporary",
                                            "cause": "transient", "expires_in": 300}]
    assert service.model == FALLBACK_MODEL
    turns = history(service)
    assert len(turns) == 2
    assert turns[0] == ("user", "teste lento")
    assert FALLBACK_MODEL in turns[1][1]


def test_parar_durante_o_raciocinio_libera_na_hora(service, events, fast, monkeypatch):
    monkeypatch.setattr(mock, "SLOW_SECONDS", 5.0)  # a thread do stream fica presa; o watchdog é o real (90s)
    service.send("teste lento", 1)
    events.wait("onThinking", 1)

    started = time.monotonic()
    assert service.stop() is True
    assert events.wait("onDone", 1)["stopped"] is True
    assert time.monotonic() - started < 0.5
    assert not service.is_generating
    assert history(service) == []


# ---------- nova tentativa automática ----------

def test_503_antes_do_texto_tenta_de_novo_sem_duplicar_o_historico(service, events, requests, resilience):
    send_and_wait(service, events, "primeira", 1)
    before = history(service)

    requests.script(server_error_at_start)
    send_and_wait(service, events, "segunda", 2)

    assert events.of("onRetry", 2) == [{"gen_id": 2, "code": "server"}]
    assert not events.of("onModelChanged")
    assert not events.of("onError")
    assert service.model == DEFAULT_MODEL
    assert len(requests.calls) == 3
    assert requests.turns(1) == requests.turns(2) == [*before, ("user", "segunda")]
    turns = history(service)
    assert len(turns) == 4
    assert turns[:2] == before
    assert turns[2] == ("user", "segunda")


def test_503_depois_de_so_raciocinio_tenta_de_novo_e_o_rotulo_volta(service, events, requests, resilience):
    """O 503 no meio do stream, com só pedaços de raciocínio, ainda é seguro repetir: nada apareceu
    na tela. A interface troca "Raciocinando…" por "Tentando de novo…" no onRetry."""
    requests.script(thought_then_server_error)
    send_and_wait(service, events, "oi", 1)

    assert events.sequence(1) == ["onStart", "onThinking", "onRetry", "onThinking", "onChunk", "onDone"]
    assert events.of("onRetry", 1) == [{"gen_id": 1, "code": "server"}]
    assert len(requests.calls) == 2
    assert requests.texts(0) == requests.texts(1) == [("user", "oi")]
    turns = history(service)
    assert [role for role, _ in turns] == ["user", "model"]


def test_503_persistente_cai_na_reserva_sem_gravar(service, events, requests, resilience):
    requests.script(server_error_at_start, server_error_at_start)
    send_and_wait(service, events, "oi", 1)

    assert events.of("onModelChanged") == [{"model": FALLBACK_MODEL, "previous": DEFAULT_MODEL, "reason": "temporary",
                                            "cause": "transient", "expires_in": 300}]
    assert [call["model"] for call in requests.calls] == [DEFAULT_MODEL, DEFAULT_MODEL, FALLBACK_MODEL]
    assert len(history(service)) == 2


def test_503_depois_de_texto_nao_repete(service, events, requests, resilience):
    def text_then_server_error():
        yield _text_chunk("parte visível")
        raise _server_error()

    requests.script(text_then_server_error)
    service.send("oi", 1)
    error = events.wait("onError", 1)

    assert error["code"] == "server"
    assert error["partial"] is True
    assert not events.of("onRetry", 1)
    assert len(requests.calls) == 1


# ---------- raciocínio ----------

def test_raciocinando_so_enquanto_chega_so_raciocinio(service, events, requests, fast):
    requests.script(text_only)
    send_and_wait(service, events, "oi", 1)
    assert not events.of("onThinking", 1)

    requests.script(text_then_thought)
    send_and_wait(service, events, "de novo", 2)
    assert not events.of("onThinking", 2)  # raciocínio depois do texto não volta o indicador

    send_and_wait(service, events, "mais uma", 3)  # a simulação raciocina antes de responder
    assert events.sequence(3) == ["onStart", "onThinking", "onChunk", "onDone"]
    assert len(events.of("onThinking", 3)) == 1
    # O raciocínio nunca vai para a interface.
    assert "Analisando o pedido" not in "".join(chunk["text"] for chunk in events.of("onChunk"))


def test_configuracao_sem_temperature_e_com_raciocinio(service, events, requests, fast):
    send_and_wait(service, events, "oi", 1)
    config = requests.calls[-1]["config"]
    assert config.temperature is None
    assert config.thinking_config.include_thoughts is True
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW

    service.set_model("gemini-3.5-pro")
    send_and_wait(service, events, "de novo", 2)
    config = requests.calls[-1]["config"]
    assert config.thinking_config.include_thoughts is True
    assert config.thinking_config.thinking_level is None  # pro: padrão do modelo


# ---------- detalhes do erro ----------

def test_onerror_traz_o_detail_e_o_log_registra_a_excecao(service, events, fast, caplog):
    caplog.set_level(logging.WARNING, logger="robin.gemini")
    service.send("simule um erro", 1)
    error = events.wait("onError", 1)

    assert error["code"] == "network"
    assert error["detail"] == "ConnectionError: Simulação: falha de rede ao conectar à API (network)."
    assert not events.of("onRetry", 1)  # falha de rede não é passageira para a nova tentativa
    assert any(
        record.name == "robin.gemini" and "ConnectionError" in record.getMessage() for record in caplog.records
    )


def test_detail_e_log_nunca_levam_a_chave(service, events, requests, fast, caplog):
    caplog.set_level(logging.WARNING, logger="robin.gemini")
    key = "AIza" + "B1c2D3e4F5" * 3 + "g6H7i"

    def leaky_error():
        raise errors.ClientError(400, {"error": {
            "code": 400, "status": "INVALID_ARGUMENT", "message": f"bad request ?key={key}&alt=sse ({key})",
        }})
        yield

    requests.script(leaky_error)
    service.send("oi", 1)
    error = events.wait("onError", 1)

    assert len(key) == 39
    assert error["detail"].startswith("ClientError 400 INVALID_ARGUMENT: ")
    assert "<oculto>" in error["detail"]
    for text in (error["detail"], error["message"], caplog.text):
        assert key not in text


def test_detail_curto(service, events, requests, resilience):
    def long_error():
        raise errors.ServerError(500, {"error": {"code": 500, "status": "INTERNAL", "message": "x" * 1000}})
        yield

    requests.script(long_error, long_error, long_error)  # tentativa, nova tentativa e reserva
    service.send("oi", 1)
    error = events.wait("onError", 1)

    assert error["code"] == "server"
    assert len(error["detail"]) == 300
    assert error["detail"].endswith("…")
