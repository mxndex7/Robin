"""Histórico da sessão com o MockGeminiService: parar, limpar, regenerar, rebuild e fallback.

O MockGeminiService é o próprio GeminiService com só a "rede" simulada, então estes testes
exercitam o código real de sessão, cancelamento e fallback.
"""

from conftest import TIMEOUT, history, send_and_wait

from robin.config import DEFAULT_MODEL, FALLBACK_MODEL
from robin.mock import _reply_long


# ---------- parar ----------

def test_parar_grava_o_parcial_e_a_proxima_mensagem_funciona(service, events, requests, slow_stream):
    service.send("me dê um texto longo", 1)
    events.wait("onChunk", 1)
    generation = service._active

    assert service.stop() is True
    assert events.wait("onDone", 1)["stopped"] is True
    assert generation.finished.wait(TIMEOUT)

    turns = history(service)
    assert [role for role, _ in turns] == ["user", "model"]
    assert turns[0][1] == "me dê um texto longo"
    partial = turns[1][1]
    assert partial.strip()
    assert partial == generation.text[:len(partial)]
    assert len(partial) < len(_reply_long())  # parou antes do fim

    send_and_wait(service, events, "continue", 2)

    turns = history(service)
    assert [role for role, _ in turns] == ["user", "model", "user", "model"]
    assert turns[1][1] == partial
    assert turns[2][1] == "continue"
    # O pedido seguinte levou o parcial como contexto.
    assert requests.texts()[:2] == [("user", "me dê um texto longo"), ("model", partial)]
    assert not events.of("onError")


# ---------- limpar ----------

def test_limpar_durante_a_geracao_nao_vaza_o_turno(service, events, slow_stream):
    send_and_wait(service, events, "primeira conversa", 1)
    service.send("me dê um texto longo", 2)
    events.wait("onChunk", 2)
    generation = service._active

    service.clear()
    assert generation.finished.wait(TIMEOUT)  # a thread do stream antigo terminou

    assert history(service) == []
    assert not service.is_generating

    send_and_wait(service, events, "nova conversa", 3)

    turns = history(service)
    assert [role for role, _ in turns] == ["user", "model"]
    assert turns[0][1] == "nova conversa"
    assert "texto longo" not in "".join(text for _, text in turns)
    # O turno limpo não termina nem falha na interface.
    assert not events.of("onDone", 2)
    assert not events.of("onError", 2)


# ---------- regenerar ----------

def test_regenerar_substitui_o_ultimo_turno_sem_duplicar(service, events, requests, fast):
    send_and_wait(service, events, "primeira", 1)
    send_and_wait(service, events, "segunda", 2)
    before = history(service)

    service.regenerate(3)
    events.wait("onDone", 3)

    after = history(service)
    assert len(after) == 4
    assert after[:2] == before[:2]
    assert [(role, text) for role, text in after[2:3]] == [("user", "segunda")]
    assert after[3][0] == "model"
    # O reenvio levou só o primeiro turno de contexto, sem a resposta antiga.
    assert requests.texts() == [*before[:2], ("user", "segunda")]


def test_regenerar_depois_de_parar_descarta_o_parcial(service, events, slow_stream):
    service.send("me dê um texto longo", 1)
    events.wait("onChunk", 1)
    generation = service._active
    service.stop()
    generation.finished.wait(TIMEOUT)
    partial = history(service)[1][1]

    service.regenerate(2)
    events.wait("onDone", 2, timeout=10)

    turns = history(service)
    assert [role for role, _ in turns] == ["user", "model"]
    assert turns[0][1] == "me dê um texto longo"
    assert turns[1][1] != partial
    assert turns[1][1] == _reply_long()


# ---------- salvar memória e trocar modelo ----------

def test_salvar_memoria_mantem_o_historico(service, events, requests, memory, fast):
    send_and_wait(service, events, "oi", 1)
    assert "memória inicial" in requests.system_instruction()
    before = history(service)

    memory["text"] = "memória nova"
    service.rebuild()  # o que RobinAPI.save_memory faz depois de gravar o arquivo

    assert history(service) == before
    send_and_wait(service, events, "de novo", 2)
    assert "memória nova" in requests.system_instruction()
    assert requests.texts()[:2] == before
    assert len(history(service)) == 4


def test_salvar_memoria_durante_a_geracao_aplica_no_fim(service, events, requests, memory, slow_stream):
    service.send("me dê um texto longo", 1)
    events.wait("onChunk", 1)

    memory["text"] = "memória nova"
    service.rebuild()  # adiado: a geração em andamento não é interrompida
    events.wait("onDone", 1, timeout=10)

    turns = history(service)
    assert [role for role, _ in turns] == ["user", "model"]
    send_and_wait(service, events, "oi", 2)
    assert "memória nova" in requests.system_instruction()
    assert requests.texts()[:2] == turns


def test_trocar_modelo_mantem_o_historico(service, events, requests, fast):
    send_and_wait(service, events, "oi", 1)
    before = history(service)

    service.set_model("gemini-3.6-flash")

    assert service.model == "gemini-3.6-flash"
    assert history(service) == before
    send_and_wait(service, events, "de novo", 2)
    assert requests.calls[-1]["model"] == "gemini-3.6-flash"
    assert requests.texts()[:2] == before
    assert len(history(service)) == 4


# ---------- fallback ----------

def test_fallback_troca_para_a_reserva_mantendo_o_historico(service, events, requests, fast):
    send_and_wait(service, events, "oi", 1)
    before = history(service)

    # A simulação devolve 404 para "fallback" em qualquer modelo que não seja o de reserva.
    send_and_wait(service, events, "teste de fallback", 2)

    changed = events.wait("onModelChanged")
    assert changed == {"model": FALLBACK_MODEL, "previous": DEFAULT_MODEL, "reason": "fallback"}
    assert (DEFAULT_MODEL, FALLBACK_MODEL) == ("gemini-3.5-flash-lite", "gemini-3.6-flash")
    assert service.model == FALLBACK_MODEL
    assert [call["model"] for call in requests.calls] == [DEFAULT_MODEL, DEFAULT_MODEL, FALLBACK_MODEL]
    # O reenvio pela reserva levou o histórico anterior.
    assert requests.texts()[:2] == before

    turns = history(service)
    assert turns[:2] == before
    assert turns[2] == ("user", "teste de fallback")
    assert turns[3][0] == "model" and FALLBACK_MODEL in turns[3][1]
    assert len(turns) == 4
    assert not events.of("onError")
