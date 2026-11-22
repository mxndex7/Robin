"""Conversas guardadas no PC: o arquivo de cada conversa (mensagens, ações, parada, erro, regenerar),
nova conversa, reabrir com o histórico na sessão, apagar, filtro da lista, trechos das conversas
anteriores no system_instruction, buscar_conversas e a aba Memória (listar, editar, riscar, apagar)."""

import json

from conftest import Requests

from robin import config, mock
from robin.brain import DONE, PENDING, LocalStore
from robin.conversations import ConversationStore, history_for_session


def _send(api, events, text: str) -> int:
    result = api.send_message(text)
    assert result["ok"], result
    events.wait("onDone", result["gen_id"])
    api._flush_events()
    return result["gen_id"]


def _files(files) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(files.conversations.glob("*.json"))]


def _only(files) -> dict:
    conversations = _files(files)
    assert len(conversations) == 1
    return conversations[0]


def _conversation(conversation_id: str, title: str, messages: list[tuple[str, str, str]]) -> dict:
    """messages: (papel, texto, created_at)."""
    return {
        "id": conversation_id, "title": title, "created_at": messages[0][2], "updated_at": messages[-1][2],
        "updated_by": "teste", "messages": [
            {"id": f"{conversation_id}-{n}", "role": role, "text": text, "created_at": stamp}
            for n, (role, text, stamp) in enumerate(messages)
        ],
    }


A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


# ---------- arquivo de cada conversa ----------

def test_mensagem_e_resposta_ficam_guardadas_com_as_acoes(robin_api, events, files):
    api = robin_api()
    _send(api, events, "lembre de comprar uva")

    conversation = _only(files)
    assert conversation["title"] == "lembre de comprar uva"
    user, robin = conversation["messages"]
    assert (user["role"], user["text"]) == ("user", "lembre de comprar uva")
    assert robin["role"] == "robin" and robin["text"] == "Anotado: vou te lembrar de **comprar uva**."
    assert robin["actions"] == [{"tool": "salvar_memoria", "label": "Salvou tarefa: comprar uva", "ok": True}]
    listed = api.list_conversations()
    assert listed["current"] == conversation["id"]
    assert [(item["id"], item["count"]) for item in listed["items"]] == [(conversation["id"], 2)]


def test_parar_guarda_o_trecho_como_interrompido(robin_api, events, files, monkeypatch):
    monkeypatch.setattr(mock, "CHUNK_CHARS", (8, 8))
    monkeypatch.setattr(mock, "CHUNK_DELAY", (0.004, 0.004))
    api = robin_api()
    gen_id = api.send_message("me dê um texto longo")["gen_id"]
    events.wait("onChunk", gen_id)
    api.stop_generation()
    events.wait("onDone", gen_id)

    robin = _only(files)["messages"][-1]
    assert robin["status"] == "stopped" and robin["text"]


def test_erro_fica_guardado_e_regenerar_troca_a_resposta(robin_api, events, files):
    api = robin_api()
    result = api.send_message("simule um erro")
    events.wait("onError", result["gen_id"])
    robin = _only(files)["messages"][-1]
    assert robin["status"] == "error" and robin["error"]["code"] == "network" and robin["text"] == ""

    # "Tentar novamente" (regenerar): o resultado novo toma o lugar do erro, não fica um atrás do outro.
    regenerated = api.regenerate()
    events.wait("onError", regenerated["gen_id"])  # a mensagem ainda tem "erro": falha de novo
    messages = _only(files)["messages"]
    assert [message["role"] for message in messages] == ["user", "robin"]


def test_regenerar_troca_a_ultima_resposta(robin_api, events, files):
    api = robin_api()
    _send(api, events, "me mostre uma tabela")
    regenerated = api.regenerate()
    events.wait("onDone", regenerated["gen_id"])
    api._flush_events()

    messages = _only(files)["messages"]
    assert [message["role"] for message in messages] == ["user", "robin"]
    assert "Comparativo simulado" in messages[1]["text"]


def test_envio_recusado_nao_fica_guardado(robin_api, events, files, monkeypatch):
    monkeypatch.setattr(mock, "CHUNK_CHARS", (8, 8))
    monkeypatch.setattr(mock, "CHUNK_DELAY", (0.004, 0.004))
    api = robin_api()
    first = api.send_message("me dê um texto longo")["gen_id"]
    busy = api.send_message("outra coisa")
    assert busy["code"] == "busy"
    events.wait("onDone", first, timeout=10)

    assert [message["text"] for message in _only(files)["messages"] if message["role"] == "user"] == ["me dê um texto longo"]


# ---------- nova, reabrir e apagar ----------

def test_nova_conversa_guarda_a_anterior(robin_api, events, files):
    api = robin_api()
    _send(api, events, "primeira conversa")
    assert api.new_conversation()["ok"]
    assert api.list_conversations()["current"] is None
    _send(api, events, "segunda conversa")

    titles = [item["title"] for item in api.list_conversations()["items"]]
    assert titles == ["segunda conversa", "primeira conversa"]


def test_reabrir_poe_o_historico_na_sessao_e_continua_o_arquivo(robin_api, events, files):
    api = robin_api()
    _send(api, events, "primeira conversa")
    first = api.list_conversations()["current"]
    api.new_conversation()
    _send(api, events, "outra coisa")

    opened = api.open_conversation(first)
    assert opened["ok"] and [m["text"] for m in opened["conversation"]["messages"]][0] == "primeira conversa"
    requests = Requests(api._gemini)
    _send(api, events, "o que eu disse antes?")

    # A sessão recebeu a conversa reaberta, só texto: a resposta cita a mensagem anterior dela.
    assert [role for role, _ in requests.turns()][:3] == ["user", "model", "user"]
    assert requests.turns()[0] == ("user", "primeira conversa")
    reopened = next(c for c in _files(files) if c["id"] == first)
    assert len(reopened["messages"]) == 4
    assert "primeira conversa" in reopened["messages"][-1]["text"]
    assert api.list_conversations()["current"] == first


def test_reabrir_a_atual_nao_interrompe_nada(robin_api, events, files):
    api = robin_api()
    _send(api, events, "oi")
    current = api.list_conversations()["current"]
    assert api.open_conversation(current) == {"ok": True, "conversation": _only(files), "current": True}


def test_apagar_a_atual_recomeca_e_ids_invalidos_sao_recusados(robin_api, events, files):
    api = robin_api()
    _send(api, events, "oi")
    current = api.list_conversations()["current"]

    assert api.delete_conversation(current) == {"ok": True, "was_current": True}
    assert _files(files) == [] and api.list_conversations() == {"ok": True, "current": None, "items": []}
    for bad in (current, "..\\brain", "../../x", ""):
        assert api.delete_conversation(bad)["code"] == "not_found"
        assert api.open_conversation(bad)["code"] == "not_found"
    _send(api, events, "de novo")
    assert len(_files(files)) == 1


def test_lista_filtra_por_titulo_e_texto_sem_acento(tmp_path):
    store = ConversationStore(tmp_path)
    store.save(_conversation(A, "Ideia do LinkedIn", [("user", "Mudar a foto", "2026-10-30T10:00:00+00:00")]))
    store.save(_conversation(B, "Mercado", [("user", "comprar pão e uva", "2026-10-31T10:00:00+00:00")]))

    assert [item["id"] for item in store.summaries()] == [B, A]
    assert [item["id"] for item in store.summaries("PAO uva")] == [B]
    assert [item["id"] for item in store.summaries("linkedin foto")] == [A]
    assert store.summaries("nada disso") == []


def test_arquivo_ilegivel_fica_fora_e_nao_e_apagado(tmp_path):
    store = ConversationStore(tmp_path)
    broken = tmp_path / f"{A}.json"
    broken.write_text("{ não é json", encoding="utf-8")
    assert store.summaries() == [] and store.get(A) is None
    assert broken.exists()


def test_historico_da_sessao_cabe_no_orcamento_e_comeca_pelo_usuario():
    conversation = _conversation(A, "t", [
        ("user", "a" * 50, "2026-10-30T10:00:00+00:00"),
        ("robin", "b" * 50, "2026-10-30T10:01:00+00:00"),
        ("user", "c" * 50, "2026-10-30T10:02:00+00:00"),
        ("robin", "", "2026-10-30T10:03:00+00:00"),  # erro sem texto
    ])
    assert history_for_session(conversation, 1_000) == [("user", "a" * 50), ("model", "b" * 50), ("user", "c" * 50)]
    assert history_for_session(conversation, 100) == [("user", "c" * 50)]  # cortou o "b": sobra só o usuário


# ---------- conversas anteriores no prompt ----------

def test_trechos_das_conversas_anteriores_vao_no_prompt(robin_api, events, files):
    api = robin_api()
    _send(api, events, "minha ideia é mudar a foto do LinkedIn")
    api.new_conversation()
    requests = Requests(api._gemini)
    _send(api, events, "oi")

    instruction = requests.system_instruction()
    assert "CONVERSAS ANTERIORES" in instruction
    assert "Pessoa: minha ideia é mudar a foto do LinkedIn" in instruction
    declared = [d.name for d in requests.calls[-1]["config"].tools[0].function_declarations]
    assert "buscar_conversas" not in declared  # tudo coube nos trechos

    # A conversa de agora não entra nos trechos (ela já está no histórico da sessão).
    _send(api, events, "de novo")
    assert "Pessoa: oi" not in requests.system_instruction()


def test_conversa_antiga_fora_dos_trechos_e_buscada(robin_api, events, files, monkeypatch):
    store = ConversationStore(files.conversations)
    store.save(_conversation(A, "LinkedIn", [
        ("user", "quero trocar a foto do linkedin", "2026-09-01T10:00:00+00:00"),
        ("robin", "Boa, uma foto nova ajuda.", "2026-09-01T10:01:00+00:00"),
    ]))
    store.save(_conversation(B, "Mercado", [("user", "comprar pão", "2026-10-31T10:00:00+00:00")]))
    monkeypatch.setattr(config, "MAX_PROMPT_CONVERSATION_CHARS", 60)  # só a mais recente cabe
    api = robin_api()
    requests = Requests(api._gemini)
    gen_id = _send(api, events, "lembra quando falamos sobre a foto do linkedin?")

    instruction = requests.system_instruction(0)
    assert "comprar pão" in instruction and "trocar a foto" not in instruction
    assert "use buscar_conversas" in instruction
    declared = [d.name for d in requests.calls[0]["config"].tools[0].function_declarations]
    assert "buscar_conversas" in declared
    assert [event["label"] for event in events.of("onToolCall", gen_id)] == ["Buscou nas conversas: a foto do linkedin"]
    text = "".join(chunk["text"] for chunk in events.of("onChunk", gen_id))
    assert "quero trocar a foto do linkedin" in text


def test_trechos_cortados_pelo_orcamento(tmp_path):
    store = ConversationStore(tmp_path)
    store.save(_conversation(A, "Velha", [("user", "conversa velha", "2026-09-01T10:00:00+00:00")]))
    store.save(_conversation(B, "Nova", [
        ("user", "primeira mensagem", "2026-10-31T10:00:00+00:00"),
        ("robin", "resposta", "2026-10-31T10:01:00+00:00"),
    ]))

    full, cut = store.excerpt(None, 10_000, 300)
    assert not cut and full.index("Velha") < full.index("Nova")  # cronológico: a mais antiga primeiro
    assert full.index("primeira mensagem") < full.index("resposta")
    text, cut = store.excerpt(None, 80, 300)
    assert cut and "resposta" in text and "conversa velha" not in text
    assert store.excerpt(B, 10_000, 300)[0].count("conversa velha") == 1  # a excluída fica fora
    assert ConversationStore(tmp_path / "vazia").excerpt(None, 10_000, 300) == ("(nenhuma ainda)", False)


# ---------- aba Memória ----------

def test_aba_memoria_lista_edita_risca_e_apaga(robin_api, files):
    store = LocalStore(files.brain)
    store.add("núcleo", [], None, request_id=config.CORE_REQUEST_ID, pinned=True)
    task = store.add("comprar uva", ["compras"], PENDING)
    note = store.add("meu carro é azul", [])
    api = robin_api()

    listed = api.list_brain()
    assert [item["content"] for item in listed["items"]] == ["meu carro é azul", "comprar uva"]
    assert "embedding" not in listed["items"][0]

    edited = api.update_brain_item(note["id"], "  meu carro é vermelho ")
    assert edited["ok"] and edited["item"]["content"] == "meu carro é vermelho"
    assert api.update_brain_item(note["id"], "   ")["code"] == "empty"
    done = api.complete_brain_item(task["id"])
    assert done["ok"] and done["item"]["status"] == DONE
    assert api.delete_brain_item(note["id"])["ok"]
    assert [item["content"] for item in api.list_brain()["items"]] == ["comprar uva"]

    core = next(item for item in LocalStore(files.brain).snapshot() if item.get("pinned"))
    assert api.delete_brain_item(core["id"])["code"] == "pinned"
    assert api.update_brain_item("nao-existe", "x")["code"] == "not_found"
