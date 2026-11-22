"""Ferramentas do cérebro: cada ferramenta, confirmação para apagar, cache de ações do turno, itens
parecidos e o laço explícito no GeminiService (idempotência com 503, watchdog, fallback, parar e
regenerar) e a contagem de pedidos por pedido do usuário.

A rede simulada (mock.py) pede as ferramentas como a diretiva manda: "lembre de X" escreve a
resposta e chama salvar_memoria na mesma mensagem; perguntas saem da memória do system_instruction;
requests.script substitui pedidos pontuais por geradores roteirizados.
"""

import threading
import time

import pytest
from conftest import TIMEOUT, history, send_and_wait
from google.genai import errors, types

from robin import gemini, mock
from robin.brain import DONE, PENDING
from robin.config import DEFAULT_MODEL, FALLBACK_MODEL
from robin.mock import fake_embedding
from robin.tools import MemoryTools, Turn, request_id


@pytest.fixture
def tools(brain):
    brain.embed = lambda texts, task: [fake_embedding(text) for text in texts]
    return MemoryTools(brain)


def _turn(n: int = 1) -> Turn:
    return Turn(f"teste:{n}")


def _items(brain) -> list[dict]:
    return brain.list_tasks("all") + [item for item in brain._load() if item["status"] is None]


def _labels(events, gen_id=None) -> list[str]:
    return [event["label"] for event in events.of("onToolCall", gen_id)]


def _function_call(name: str, **args):
    """Gerador roteirizado: o modelo pede uma ferramenta."""
    def behavior():
        yield types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(
                function_call=types.FunctionCall(id="roteiro-1", name=name, args=args),
                thought_signature=b"assinatura-do-modelo",
            )]),
            finish_reason=types.FinishReason.STOP,
        )])
    return behavior


def _server_error():
    raise errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}})
    yield


def _not_found():
    raise errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "model not found"}})
    yield


def _function_parts(contents, kind: str) -> list:
    return [getattr(part, kind) for content in contents for part in content.parts or [] if getattr(part, kind)]


# ---------- cada ferramenta ----------

def test_salvar_devolve_o_id_e_grava_tarefa_ou_nota(tools, brain):
    turn = _turn()
    task = tools.call("salvar_memoria", {"conteudo": "comprar uva", "etiquetas": ["Compras"], "tarefa": True}, turn, set())
    note = tools.call("salvar_memoria", {"conteudo": "meu carro é azul", "etiquetas": [], "tarefa": False}, turn, set())

    assert task.ok and task.response["id"] and task.label == "Salvou tarefa: comprar uva"
    assert note.label == "Salvou: meu carro é azul"
    assert brain.get(task.response["id"])["status"] == PENDING
    assert brain.get(note.response["id"])["status"] is None
    assert task.response["item"]["tipo"] == "a fazer" and note.response["item"]["tipo"] == "guardado"
    assert task.response["item"]["etiquetas"] == ["compras"]
    assert turn.seen_ids == {task.response["id"], note.response["id"]}


def test_buscar_devolve_os_ids(tools, brain):
    uva = brain.add("comprar uva", [], PENDING)
    brain.add("pagar conta de luz", [], PENDING)
    brain.backfill_embeddings()

    found = tools.call("buscar_memoria", {"consulta": "fruta roxa pequena"}, _turn(), set())
    assert found.response["modo"] == "semantica"
    assert found.response["itens"][0]["id"] == uva["id"]
    assert found.label == "Buscou: fruta roxa pequena"
    assert not tools.call("listar_tarefas", {}, _turn(), set()).ok  # saiu: a memória vai inteira no prompt


def test_atualizar_informa_o_que_mudou(tools, brain):
    item = brain.add("reunião sexta", ["trabalho"])
    result = tools.call("atualizar_memoria", {"id": item["id"], "conteudo": "reunião sexta às 15h"}, _turn(), set())

    assert result.ok and result.label == "Atualizou: reunião sexta às 15h"
    assert result.response["mudancas"] == {"conteudo": {"antes": "reunião sexta", "depois": "reunião sexta às 15h"}}
    assert len(_items(brain)) == 1
    unchanged = tools.call("atualizar_memoria", {"id": item["id"], "conteudo": "reunião sexta às 15h"}, _turn(2), set())
    assert unchanged.response["mudancas"] == {} and unchanged.label == ""
    assert not tools.call("atualizar_memoria", {"id": "nao-existe", "conteudo": "x"}, _turn(), set()).ok


def test_concluir_mantem_a_tarefa_com_data(tools, brain):
    task = brain.add("comprar uva", [], PENDING)
    result = tools.call("concluir_tarefa", {"id": task["id"]}, _turn(), set())

    assert result.ok and result.label == "Concluiu: comprar uva"
    saved = brain.get(task["id"])
    assert saved["status"] == DONE and saved["done_at"]
    assert result.response["item"]["feito_em"] and result.response["item"]["tipo"] == "feito"
    assert [t["id"] for t in brain.list_tasks(DONE)] == [task["id"]]

    note = brain.add("uma nota", [])
    assert not tools.call("concluir_tarefa", {"id": note["id"]}, _turn(), set()).ok


def test_apagar_nota_sem_confirmacao_e_recusado(tools, brain):
    note = brain.add("meu carro é azul", [])

    refused = tools.call("apagar_memoria", {"id": note["id"]}, _turn(), set())
    assert not refused.ok and refused.response["confirmacao_necessaria"] is True and refused.label == ""
    # confirmado=true no mesmo turno em que o item apareceu também não passa.
    same_turn = tools.call("apagar_memoria", {"id": note["id"], "confirmado": True}, _turn(), set())
    assert not same_turn.ok
    assert brain.get(note["id"]) is not None

    # Num turno seguinte (o usuário viu o item e confirmou), apaga.
    deleted = tools.call("apagar_memoria", {"id": note["id"], "confirmado": True}, _turn(2), {note["id"]})
    assert deleted.ok and deleted.label == "Apagou: meu carro é azul"
    assert brain.get(note["id"]) is None


def test_apagar_tarefa_nao_pede_confirmacao(tools, brain):
    task = brain.add("tarefa errada", [], PENDING)
    assert tools.call("apagar_memoria", {"id": task["id"]}, _turn(), set()).ok
    assert brain.get(task["id"]) is None


def test_ferramenta_desconhecida_e_argumentos_ruins_nao_derrubam(tools):
    assert not tools.call("rm_rf", {}, _turn(), set()).ok
    assert not tools.call("buscar_memoria", {"consulta": "x", "limite": "muitos"}, _turn(), set()).ok
    assert not tools.call("salvar_memoria", {"conteudo": "  "}, _turn(), set()).ok


# ---------- cache de ações do turno ----------

def test_mesma_acao_no_mesmo_turno_roda_uma_vez(tools, brain, monkeypatch):
    calls = []
    original = brain.add
    monkeypatch.setattr(brain, "add", lambda *a, **k: calls.append(1) or original(*a, **k))
    turn = _turn()

    first = tools.call("salvar_memoria", {"conteudo": "Comprar  UVA", "etiquetas": ["a"], "tarefa": True}, turn, set())
    again = tools.call("salvar_memoria", {"conteudo": "comprar uva", "etiquetas": ["b"], "tarefa": True}, turn, set())

    assert again is first and len(calls) == 1
    assert len(_items(brain)) == 1
    # Outro turno do usuário é outra ação (com forcar, porque o mesmo texto já existe).
    tools.call("salvar_memoria", {"conteudo": "comprar uva", "etiquetas": [], "tarefa": True, "forcar": True}, _turn(2), set())
    assert len(_items(brain)) == 2


# ---------- itens parecidos e itens fixos ----------

def test_salvar_parecido_devolve_ja_existe_sem_gravar(tools, brain):
    first = tools.call("salvar_memoria", {"conteudo": "comprar uva", "etiquetas": [], "tarefa": True}, _turn(1), set())
    similar = tools.call("salvar_memoria", {"conteudo": "comprar uvas", "etiquetas": [], "tarefa": True}, _turn(2), set())

    assert not similar.ok and similar.response["ja_existe"]["id"] == first.response["id"]
    assert similar.response["similaridade"] >= 0.9
    assert similar.label == "Já existe parecido: comprar uva"
    assert len(_items(brain)) == 1
    # Outro assunto passa; forcar grava mesmo parecido.
    assert tools.call("salvar_memoria", {"conteudo": "pagar conta de luz", "etiquetas": [], "tarefa": True}, _turn(3), set()).ok
    assert tools.call("salvar_memoria", {"conteudo": "comprar uvas", "etiquetas": [], "tarefa": True, "forcar": True},
                      _turn(4), set()).ok
    assert len(_items(brain)) == 3
    # O vetor calculado para conferir parecidos já vai gravado com o item (sem backfill).
    assert all(item["embedding"] for item in brain._load())


def test_salvar_parecido_sem_embedding_compara_o_texto(tools, brain):
    brain.embed = None
    tools.call("salvar_memoria", {"conteudo": "Comprar Uva", "etiquetas": [], "tarefa": True}, _turn(1), set())
    assert not tools.call("salvar_memoria", {"conteudo": "comprar  uva", "etiquetas": [], "tarefa": True}, _turn(2), set()).ok
    assert tools.call("salvar_memoria", {"conteudo": "comprar uvas", "etiquetas": [], "tarefa": True}, _turn(3), set()).ok


def test_ferramentas_recusam_itens_fixos(tools, brain):
    core = brain.add("Sou Alex", ["núcleo"], None, request_id="robin:nucleo", pinned=True)
    for name, args in (
        ("atualizar_memoria", {"id": core["id"], "conteudo": "outro"}),
        ("concluir_tarefa", {"id": core["id"]}),
        ("apagar_memoria", {"id": core["id"], "confirmado": True}),
    ):
        result = tools.call(name, args, _turn(2), {core["id"]})
        assert not result.ok and "editor da lateral" in result.response["erro"], name
    assert brain.get(core["id"])["content"] == "Sou Alex"
    # Um texto igual ao núcleo ainda pode virar nota (fixos não contam como parecidos).
    assert tools.call("salvar_memoria", {"conteudo": "Sou Alex", "etiquetas": [], "tarefa": False}, _turn(3), set()).ok


def test_request_id_e_deterministico_e_ignora_acento_e_caixa():
    args = {"conteudo": "Comprar Uva", "etiquetas": ["x"], "tarefa": True}
    assert request_id("t:1", "salvar_memoria", args) == request_id("t:1", "salvar_memoria", {**args, "conteudo": "comprar  uva"})
    assert request_id("t:1", "salvar_memoria", args) != request_id("t:2", "salvar_memoria", args)
    assert request_id("t:1", "salvar_memoria", args) != request_id("t:1", "salvar_memoria", {**args, "tarefa": False})


# ---------- laço no GeminiService ----------

def test_lembre_salva_direto_e_responde(service, events, requests, brain, fast):
    send_and_wait(service, events, "lembre de comprar uva", 1)

    assert _labels(events, 1) == ["Salvou tarefa: comprar uva"]
    assert [item["content"] for item in brain.list_tasks(PENDING)] == ["comprar uva"]
    assert len(requests.calls) == 1  # a resposta veio junto com a ação, sem o 2º pedido
    # Mensagem, chamada, resposta, texto: o histórico guarda o turno inteiro.
    roles = [content.role for content in service._history()]
    assert roles == ["user", "model", "user", "model"]
    text = "".join(chunk["text"] for chunk in events.of("onChunk", 1))
    assert text == "Anotado: vou te lembrar de **comprar uva**." and not events.of("onError")
    sequence = events.sequence(1)
    assert sequence[:2] == ["onStart", "onThinking"] and "onToolCall" in sequence and sequence[-1] == "onDone"


def test_memoria_no_prompt_responde_e_concluir_pelo_chat(service, events, requests, brain, fast):
    send_and_wait(service, events, "lembre de comprar uva", 1)

    send_and_wait(service, events, "tem alguma fruta roxa na lista?", 2)
    assert "comprar uva" in "".join(chunk["text"] for chunk in events.of("onChunk", 2))
    assert not events.of("onToolCall", 2)  # a memória já estava no prompt: nenhuma busca

    send_and_wait(service, events, "comprei uva", 3)
    assert _labels(events, 3) == ["Concluiu: comprar uva"]  # o #código veio da memória do system_instruction
    assert brain.list_tasks(PENDING) == []

    send_and_wait(service, events, "quais tarefas cumpri?", 4)
    assert "comprar uva" in "".join(chunk["text"] for chunk in events.of("onChunk", 4))
    assert len(_items(brain)) == 1  # concluir não apaga
    assert len(requests.calls) == 4  # um pedido por mensagem


def test_hora_e_fuso_vao_no_system_instruction_e_nao_no_historico(service, events, requests, fast):
    send_and_wait(service, events, "oi", 1)
    instruction = requests.system_instruction()
    assert "AGORA:" in instruction and "America/Sao_Paulo" in instruction and "UTC-03:00" in instruction
    assert "America/Sao_Paulo" not in "".join(text for _, text in history(service))
    assert "PEDIU PRA LEMBRAR" in instruction and "(nada ainda)" in instruction
    tools_config = requests.calls[-1]["config"]
    # buscar_memoria só vem com a memória cortada.
    assert [d.name for d in tools_config.tools[0].function_declarations] == [
        "salvar_memoria", "atualizar_memoria", "concluir_tarefa", "apagar_memoria",
    ]
    assert tools_config.automatic_function_calling.disable is True


def test_503_depois_de_salvar_nao_salva_de_novo(service, events, requests, brain, resilience, model_confirms):
    requests.script(None, _server_error)  # salvar e a resposta final falha uma vez
    send_and_wait(service, events, "lembre de comprar uva", 1)

    assert events.of("onRetry", 1) == [{"gen_id": 1, "code": "server"}]
    assert len(brain.list_tasks(PENDING)) == 1
    assert _labels(events, 1) == ["Salvou tarefa: comprar uva"]
    # A nova tentativa repetiu só a rodada que falhou, com a rodada anterior no histórico.
    assert len(requests.calls) == 3
    assert requests.texts(1) == requests.texts(2)
    assert len(_function_parts(requests.calls[2]["contents"], "function_response")) == 1


def test_watchdog_depois_de_uma_acao_nao_repete_a_acao(service, events, requests, brain, resilience, model_confirms):
    def stuck():
        yield mock._thought_chunk()
        time.sleep(1.0)  # mais que o watchdog (0,3s)

    requests.script(None, stuck)
    send_and_wait(service, events, "lembre de comprar uva", 1)

    assert events.of("onRetry", 1) == [{"gen_id": 1, "code": "timeout"}]
    assert len(brain.list_tasks(PENDING)) == 1
    assert len(_labels(events, 1)) == 1
    time.sleep(1.0)  # a tentativa abandonada termina e não grava nada
    assert [content.role for content in service._history()] == ["user", "model", "user", "model"]


def test_fallback_de_modelo_depois_de_uma_acao(service, events, requests, brain, resilience, model_confirms):
    requests.script(None, _not_found)
    send_and_wait(service, events, "lembre de comprar uva", 1)

    assert events.of("onModelChanged") == [{"model": FALLBACK_MODEL, "previous": DEFAULT_MODEL, "reason": "fallback"}]
    assert len(brain.list_tasks(PENDING)) == 1
    assert [call["model"] for call in requests.calls] == [DEFAULT_MODEL, DEFAULT_MODEL, FALLBACK_MODEL]
    # As chamadas de função do outro modelo seguem com a assinatura neutra, sem o raciocínio dele.
    retry = requests.calls[2]["contents"]
    signatures = [part.thought_signature for c in retry if c.role == "model" for part in c.parts if part.function_call]
    assert signatures == [gemini._SKIP_SIGNATURE]
    assert not any(part.thought for c in retry if c.role == "model" for part in c.parts)


def test_parar_durante_a_ferramenta_espera_ela_terminar(service, events, requests, brain, fast, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = brain.add

    def slow_add(*args, **kwargs):
        entered.set()
        release.wait(TIMEOUT)
        return original(*args, **kwargs)

    monkeypatch.setattr(brain, "add", slow_add)
    service.send("lembre de comprar uva", 1)
    assert entered.wait(TIMEOUT)

    stopper = threading.Thread(target=service.stop)
    stopper.start()
    stopper.join(0.2)
    assert stopper.is_alive()  # parar espera a ação em andamento
    release.set()
    stopper.join(TIMEOUT)

    assert events.wait("onDone", 1)["stopped"] is True
    assert len(brain.list_tasks(PENDING)) == 1
    assert events.sequence(1)[-2:] == ["onToolCall", "onDone"]  # a linha da ação aparece antes do fim
    time.sleep(0.2)
    assert len(requests.calls) == 1  # nenhuma rodada depois de parar
    # O turno fica resumido em texto, com a ação feita: a próxima mensagem tem contexto válido.
    turns = history(service)
    assert [role for role, _ in turns] == ["user", "model"]
    assert "Ações já executadas" in turns[1][1] and "comprar uva" in turns[1][1]
    send_and_wait(service, events, "oi", 2)
    assert not events.of("onError")


def test_regenerar_um_turno_que_salvou_nao_duplica(service, events, requests, brain, fast, model_confirms):
    send_and_wait(service, events, "lembre de comprar uva", 1)
    item_id = brain.list_tasks(PENDING)[0]["id"]

    # A resposta regenerada tenta salvar de novo (outras etiquetas): o cache do turno devolve o mesmo item.
    requests.script(_function_call("salvar_memoria", conteudo="Comprar uva", etiquetas=["feira"], tarefa=True))
    service.regenerate(2)
    events.wait("onDone", 2)

    assert [item["id"] for item in _items(brain)] == [item_id]
    assert _labels(events, 2) == ["Salvou tarefa: comprar uva"]
    assert "AÇÕES JÁ EXECUTADAS" in requests.system_instruction(-2) and item_id in requests.system_instruction(-2)
    # O histórico tem um turno só (o antigo foi trocado pelo regenerado).
    users = [c for c in service._history() if gemini._is_user_message(c)]
    assert len(users) == 1

    # Uma mensagem nova é outro turno: o aviso some.
    send_and_wait(service, events, "oi", 3)
    assert "AÇÕES JÁ EXECUTADAS" not in requests.system_instruction()


def test_erro_depois_de_uma_acao_guarda_o_turno_resumido(service, events, requests, brain, resilience, model_confirms):
    requests.script(None, _server_error, _server_error, _server_error)
    service.send("lembre de comprar uva", 1)
    error = events.wait("onError", 1)

    assert error["code"] == "server"
    assert len(brain.list_tasks(PENDING)) == 1
    turns = history(service)
    assert [role for role, _ in turns] == ["user", "model"] and "comprar uva" in turns[1][1]
    # "Tentar novamente" regenera o mesmo turno sem salvar de novo.
    service.regenerate(2)
    events.wait("onDone", 2)
    assert len(_items(brain)) == 1


def test_ultima_rodada_vai_sem_ferramentas(service, events, requests, brain, fast, monkeypatch):
    monkeypatch.setattr(gemini, "MAX_TOOL_ROUNDS", 1)
    requests.script(_function_call("buscar_memoria", consulta="uva"))  # gasta a única rodada
    send_and_wait(service, events, "lembre de comprar uva", 1)

    assert len(requests.calls) == 2
    mode = requests.calls[1]["config"].tool_config.function_calling_config.mode
    assert mode == types.FunctionCallingConfigMode.NONE
    assert brain.list_tasks(PENDING) == []  # o salvar não chegou a rodar
    assert not events.of("onError")


def test_embedding_falhando_salva_sem_vetor(service, events, requests, brain, fast):
    send_and_wait(service, events, "sem embedding: lembre de comprar pão integral", 1)
    time.sleep(0.2)  # o backfill em segundo plano também falha
    item = brain.list_tasks(PENDING)[0]
    assert item["content"] == "comprar pao integral" and item["embedding"] is None

    # Sem vetor, a coisa está na memória do prompt do mesmo jeito.
    send_and_wait(service, events, "busque pão", 2)
    assert "comprar pao integral" in "".join(chunk["text"] for chunk in events.of("onChunk", 2))

    # Com o embedding de volta (fim da janela da simulação e chave nova), o backfill preenche.
    mock._embed_fail_until = 0.0
    service.configure("")
    _wait_embeddings(brain)


def test_apagar_nota_pede_confirmacao_na_conversa(service, events, requests, brain, fast):
    send_and_wait(service, events, "lembre que meu carro é azul", 1)
    assert [item["content"] for item in _items(brain)] == ["meu carro e azul"]
    _wait_embeddings(brain)

    send_and_wait(service, events, "esqueça meu carro", 2)
    assert len(_items(brain)) == 1
    assert "confirmo" in "".join(chunk["text"] for chunk in events.of("onChunk", 2))

    send_and_wait(service, events, "confirmo", 3)
    assert _labels(events, 3) == ["Apagou: meu carro e azul"]
    assert _items(brain) == []


def test_limpar_esquece_os_ids_vistos(service, events, requests, brain, fast):
    send_and_wait(service, events, "lembre que meu carro é azul", 1)
    _wait_embeddings(brain)
    send_and_wait(service, events, "esqueça meu carro", 2)
    service.clear()
    send_and_wait(service, events, "oi", 3)  # nova conversa: o "confirmo" não acha nada pendente
    assert service._known_ids == set()


def _wait_embeddings(brain, timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if brain.backfill_embeddings() >= 0 and all(item["embedding"] for item in brain._load()):
            return
        time.sleep(0.02)
    raise AssertionError("embeddings não preenchidos")
