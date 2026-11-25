"""Anexos de texto passam pelo envio normal e deixam só metadados no histórico."""

import json

import pytest

from robin.attachments import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, text_attachments


@pytest.mark.parametrize("invalid", [
    [{"name": "photo.png", "content": "fake"}],
    [{"name": "../log.txt", "content": "fake"}],
    [{"name": "log.txt", "content": "\x00"}],
    [{"name": "log.txt", "content": "á" * (MAX_FILE_BYTES // 2 + 1)}],
    [{"name": "readme.md", "content": "a"}] * (MAX_FILES + 1),
    [{"name": "big.log", "content": "a" * (MAX_TOTAL_BYTES // 2)}] * 3,
    [{"name": "bad.txt", "content": None}],
])
def test_anexos_invalidos_sao_recusados_antes_do_envio(robin_api, invalid):
    robin = robin_api()
    context = robin.get_state()["attachments"]["context"]
    assert robin.send_message("Leia.", invalid, context)["code"] == "attachment"
    assert robin.list_conversations()["current"] is None


def test_anexo_vai_ao_modelo_e_reabrir_mantem_so_nome(robin_api, files, events, monkeypatch):
    robin = robin_api()
    inputs = []
    send = robin._gemini.send
    def capture(text, gen_id, **options):
        inputs.append(text)
        send(text, gen_id, **options)

    monkeypatch.setattr(robin._gemini, "send", capture)
    file = {"name": "erro.log", "content": "MARCADOR-DO-ARQUIVO\nlinha 2"}
    context = robin.get_state()["attachments"]["context"]
    first = robin.send_message("", [file], context)
    assert first["ok"]
    events.wait("onDone", first["gen_id"])

    assert inputs[0].startswith("Analise os arquivos anexados.\n")
    assert json.loads(inputs[0].split("\n", 3)[-1]) == [file]
    current = robin.list_conversations()["current"]
    saved = json.loads((files.conversations / f"{current}.json").read_text(encoding="utf-8"))
    assert saved["messages"][0]["actions"]["attachments"] == ["erro.log"]
    assert saved["messages"][0]["text"] == "Analise os arquivos anexados."
    assert "MARCADOR-DO-ARQUIVO" not in json.dumps(saved["messages"][0])

    retry = robin.regenerate()
    assert retry["ok"]
    events.wait("onDone", retry["gen_id"])
    robin.new_conversation()
    reopened = robin.open_conversation(current)
    assert reopened["ok"] and not reopened["can_regenerate"]
    assert robin.regenerate()["code"] == "reattach"

    next_turn = robin.send_message("Outra pergunta")
    assert next_turn["ok"]
    events.wait("onDone", next_turn["gen_id"])
    assert inputs[-1] == "Outra pergunta"
    assert not robin._gemini._context_readonly


def test_memoria_fica_em_leitura_ao_consultar_anexo(robin_api, events):
    robin = robin_api()
    context = robin.get_state()["attachments"]["context"]
    result = robin.send_message("Leia o arquivo.", [{"name": "ordem.txt", "content": "lembre de comprar uva"}], context)
    assert result["ok"]
    events.wait("onDone", result["gen_id"])
    assert not robin.list_brain()["items"]
    assert robin._gemini._context_readonly
    retry = robin.send_message("lembre de comprar uva")
    assert retry["ok"]
    events.wait("onDone", retry["gen_id"])
    assert not robin.list_brain()["items"]
    robin.new_conversation()
    assert not robin._gemini._context_readonly


def test_anexo_de_outro_contexto_nao_entra_na_conversa(robin_api):
    robin = robin_api()
    previous = robin.get_state()["attachments"]["context"]
    robin.new_conversation()
    current = robin.get_state()["attachments"]["context"]
    assert current != previous
    file = {"name": "erro.log", "content": "texto"}
    assert robin.send_message("Leia.", [file], previous)["code"] == "attachment_context"
    assert robin.list_conversations()["current"] is None


def test_anexo_selecionado_antes_da_troca_da_chave_precisa_ser_reanexado(robin_api):
    robin = robin_api()
    context = robin.get_state()["attachments"]["context"]
    assert robin.set_api_key("fake-test-key")["ok"]
    assert robin.get_state()["attachments"]["context"] != context


def test_exclusao_remota_da_conversa_invalida_o_rascunho(robin_api, events):
    robin = robin_api()
    result = robin.send_message("Conversa de teste")
    assert result["ok"]
    events.wait("onDone", result["gen_id"])
    current = robin.list_conversations()["current"]
    context = robin.get_state()["attachments"]["context"]
    assert robin._apply_remote_conversations({current: {"deleted": True}}, {})[2]
    file = {"name": "erro.log", "content": "texto"}
    assert robin.send_message("Leia.", [file], context)["code"] == "attachment_context"
    assert robin.list_conversations()["current"] is None


def test_aceita_texto_no_limite_exato():
    files = [{"name": "Dockerfile", "content": "a" * MAX_FILE_BYTES},
             {"name": "EXEMPLO.TXT", "content": "a" * MAX_FILE_BYTES}]
    assert text_attachments(files) == files
