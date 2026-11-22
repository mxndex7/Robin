"""Travamentos do turno: a emissão de eventos nunca bloqueia quem emite, qualquer exceção no laço vira
onError, parar não segura o lock do serviço enquanto a ação está na rede, e a lista de tarefas do
system_instruction fica a mesma em todas as rodadas do turno."""

import threading
import time

from conftest import TIMEOUT, send_and_wait

from robin.brain import PENDING


class BlockedWindow:
    """Janela cujo run_js fica preso até ser liberado (a thread da interface ocupada ou travada)."""

    def __init__(self, events):
        self.events = events
        self.release = threading.Event()
        self.entered = threading.Event()

    def run_js(self, script: str) -> None:
        self.entered.set()
        self.release.wait(TIMEOUT)
        self.events("js", {"script": script})


def test_emitir_nunca_espera_a_interface(robin_api, events):
    robin = robin_api()
    window = BlockedWindow(events)
    robin._attach(window, None)

    start = time.monotonic()
    for index in range(5):
        robin._emit("onCloudStatus", {"index": index})
    assert time.monotonic() - start < 0.2  # quem emite (geração, nuvem com o lock) segue na hora
    assert window.entered.wait(TIMEOUT)

    window.release.set()
    robin._flush_events()
    scripts = [event["script"] for event in events.of("js")]
    assert [f'"index": {index}' in script for index, script in enumerate(scripts)] == [True] * 5  # em ordem


def test_evento_que_falha_nao_para_os_proximos(robin_api, events):
    robin = robin_api()
    robin._emit("onCloudStatus", {"ruim": object()})  # json.dumps falha
    robin._emit("onCloudStatus", {"ok": True})
    robin._flush_events()
    assert events.of("onCloudStatus")[-1] == {"ok": True}


def test_excecao_no_laco_das_ferramentas_vira_onerror(service, events, fast, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("quebrou no meio do laço")

    monkeypatch.setattr(service._tools, "call", broken)
    service.send("lembre de comprar uva", 1)

    error = events.wait("onError", 1)
    assert error["code"] == "internal" and "RuntimeError" in error["detail"]
    assert not service.is_generating  # a próxima mensagem não fica presa em "busy"
    assert "Falha inesperada na geração 1" in caplog.text
    monkeypatch.undo()
    send_and_wait(service, events, "oi", 2)


def test_excecao_ao_montar_a_sessao_nao_trava_o_envio(robin_api, events, monkeypatch):
    robin = robin_api()

    def broken():
        raise RuntimeError("núcleo ilegível")

    monkeypatch.setattr(robin._gemini, "_core", broken)
    result = robin.send_message("oi")
    assert result["ok"] is False and result["code"] == "internal"
    assert not robin._gemini.is_generating
    monkeypatch.undo()


def test_parar_espera_a_acao_sem_segurar_o_lock_do_servico(service, events, brain, fast, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = brain.add

    def slow_add(*args, **kwargs):  # a gravação na nuvem demorando
        entered.set()
        release.wait(TIMEOUT)
        return original(*args, **kwargs)

    monkeypatch.setattr(brain, "add", slow_add)
    service.send("lembre de comprar uva", 1)
    assert entered.wait(TIMEOUT)
    stopper = threading.Thread(target=service.stop)
    stopper.start()
    time.sleep(0.1)

    # Enquanto parar espera a ação, o serviço segue respondendo (pull da nuvem recriando a sessão,
    # a interface perguntando o estado).
    rebuilt = threading.Thread(target=service.rebuild)
    rebuilt.start()
    rebuilt.join(1.0)
    assert not rebuilt.is_alive()
    assert stopper.is_alive()

    release.set()
    stopper.join(TIMEOUT)
    assert events.wait("onDone", 1)["stopped"] is True
    assert len(brain.list_tasks(PENDING)) == 1
    assert len(events.of("onDone", 1)) == 1  # só o onDone do parar


def test_memoria_do_system_instruction_fica_a_do_inicio_do_turno(service, events, requests, fast, model_confirms):
    send_and_wait(service, events, "lembre de comprar uva", 1)
    first, second = requests.system_instruction(0), requests.system_instruction(1)
    # Na rodada da resposta, a coisa recém-salva não aparece como "já existente".
    assert "(nada ainda)" in first and "(nada ainda)" in second
    assert "comprar uva" not in second

    send_and_wait(service, events, "o que tenho pra fazer?", 2)
    assert "comprar uva" in requests.system_instruction()  # a mensagem seguinte já vê
