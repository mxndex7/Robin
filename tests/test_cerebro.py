"""LocalStore: esquema, gravação atômica, arquivo corrompido, idempotência por request_id, busca por
significado e por texto, backfill e a espera depois de uma falha de embedding."""

import json
import threading

import pytest

from robin import brain as brain_module
from robin.brain import DONE, PENDING, BrainError, LocalStore, TASK_DOCUMENT, TASK_QUERY, text_score
from robin.mock import fake_embedding

SCHEMA = {
    "id", "content", "tags", "status", "pinned", "embedding", "request_id",
    "created_at", "updated_at", "done_at", "updated_by",
}


class FakeEmbed:
    """embed do store: o embedding falso da simulação, com falha ligável e registro das chamadas."""

    def __init__(self):
        self.calls: list[tuple[list[str], str]] = []
        self.fail = False

    def __call__(self, texts, task_type):
        self.calls.append((list(texts), task_type))
        if self.fail:
            raise RuntimeError("429 RESOURCE_EXHAUSTED (teste)")
        return [fake_embedding(text) for text in texts]


@pytest.fixture
def embed():
    return FakeEmbed()


@pytest.fixture
def store(files, embed):
    store = LocalStore(files.brain)
    store.embed = embed
    return store


def _on_disk(store) -> list[dict]:
    return json.loads(store.path.read_text(encoding="utf-8"))["items"]


# ---------- esquema e operações ----------

def test_item_tem_o_esquema_da_tabela_memories(store):
    item = store.add("Comprar uva", ["Compras", "compras", " feira "], PENDING, request_id="r1")
    assert set(item) == SCHEMA
    assert item["tags"] == ["compras", "feira"]
    assert item["status"] == PENDING and item["pinned"] is False and item["done_at"] is None
    assert item["request_id"] == "r1" and item["updated_by"]
    assert item["created_at"] == item["updated_at"] and item["created_at"].endswith("+00:00")
    assert [set(saved) for saved in _on_disk(store)] == [SCHEMA]


def test_concluir_marca_done_e_mantem_o_item(store):
    task = store.add("pagar a luz", ["contas"], PENDING)
    done = store.complete(task["id"])
    assert done["status"] == DONE and done["done_at"]
    assert store.complete(task["id"]) == done  # concluir de novo não muda nada
    assert [item["id"] for item in store.list_tasks(DONE)] == [task["id"]]
    assert store.list_tasks(PENDING) == []
    assert len(_on_disk(store)) == 1  # nada foi apagado

    note = store.add("meu carro é azul", [])
    with pytest.raises(BrainError):
        store.complete(note["id"])


def test_atualizar_edita_no_lugar_e_descarta_o_embedding(store):
    item = store.add("reunião sexta", ["trabalho"])
    store.backfill_embeddings()
    assert store.get(item["id"])["embedding"]

    changed = store.update(item["id"], content="reunião sexta às 15h")
    assert changed["id"] == item["id"] and changed["embedding"] is None
    assert changed["updated_at"] >= item["updated_at"]
    assert len(_on_disk(store)) == 1
    assert store.update(item["id"], tags=["trabalho", "Reunião"])["tags"] == ["trabalho", "reunião"]
    with pytest.raises(BrainError):
        store.update("nao-existe", content="x")


def test_listar_tarefas_devolve_todas_as_pendentes(store):
    ids = [store.add(f"tarefa {n}", [], PENDING)["id"] for n in range(30)]
    store.add("uma nota", [])
    assert sorted(item["id"] for item in store.list_tasks(PENDING)) == sorted(ids)
    assert len(store.list_tasks("all")) == 30


def test_request_id_repetido_devolve_o_item_existente(store):
    first = store.add("comprar uva", [], PENDING, request_id="turno-1-salvar")
    again = store.add("comprar uvas", ["outra"], PENDING, request_id="turno-1-salvar")
    assert again == first
    assert len(_on_disk(store)) == 1


# ---------- arquivo ----------

def test_gravacao_atomica_falha_nao_corrompe_nem_deixa_temporario(store, monkeypatch):
    store.add("primeiro", [])
    before = store.path.read_bytes()

    def broken_replace(source, target):
        raise OSError("disco cheio (teste)")

    monkeypatch.setattr(brain_module.os, "replace", broken_replace)
    with pytest.raises(BrainError):
        store.add("segundo", [])

    assert store.path.read_bytes() == before
    assert [item["content"] for item in store._load()] == ["primeiro"]  # a memória em uso voltou atrás
    assert list(store.path.parent.glob(".brain-*.tmp")) == []


def test_gravacao_tenta_de_novo_se_o_arquivo_estiver_preso(store, monkeypatch):
    original = brain_module.os.replace
    attempts = []

    def flaky_replace(source, target):
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError("arquivo em uso (teste)")
        original(source, target)

    monkeypatch.setattr(brain_module.os, "replace", flaky_replace)
    store.add("item", [])
    assert len(attempts) == 2
    assert _on_disk(store)[0]["content"] == "item"


def test_arquivo_corrompido_vira_copia_e_o_cerebro_comeca_vazio(files, caplog):
    files.brain.write_text("{ isto não é json", encoding="utf-8")
    store = LocalStore(files.brain)
    assert store.list_tasks("all") == []
    backups = list(files.dir.glob("brain.corrompido-*.json"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{ isto não é json"
    store.add("novo", [])
    assert len(_on_disk(store)) == 1


def test_outra_instancia_le_o_que_foi_gravado(store, files):
    item = store.add("persistido", ["x"], PENDING)
    assert LocalStore(files.brain).get(item["id"])["content"] == "persistido"


# ---------- busca ----------

def test_busca_por_significado_acha_a_uva(store, embed):
    for content in ("comprar uva", "pagar conta de luz", "reunião com o João sexta", "comprar banana"):
        store.add(content, [], PENDING)
    assert store.backfill_embeddings() == 4

    found, mode = store.search("fruta roxa pequena")
    assert mode == "semantica"
    assert found[0][0]["content"] == "comprar uva"
    assert "embedding" not in found[0][0]
    assert embed.calls[-1] == (["fruta roxa pequena"], TASK_QUERY)
    assert embed.calls[0][1] == TASK_DOCUMENT


def test_busca_ignora_concluidas_salvo_pedido(store):
    task = store.add("comprar uva", [], PENDING)
    store.complete(task["id"])
    store.backfill_embeddings()
    assert store.search("uva")[0] == []
    assert [item["id"] for item, _ in store.search("uva", include_done=True)[0]] == [task["id"]]


def test_embedding_falhando_salva_e_busca_por_texto(store, embed, caplog):
    embed.fail = True
    item = store.add("Reunião com o João na sexta", ["Trabalho"])
    store.add("comprar uvas", ["feira"], PENDING)
    assert store.backfill_embeddings() == 0
    assert all(saved["embedding"] is None for saved in _on_disk(store))

    found, mode = store.search("REUNIAO joao")
    assert mode == "texto" and [i["id"] for i, _ in found] == [item["id"]]
    assert [i["content"] for i, _ in store.search("uva")[0]] == ["comprar uvas"]  # plural
    assert [i["content"] for i, _ in store.search("trabalho")[0]] == ["Reunião com o João na sexta"]  # etiqueta
    assert store.search("fruta roxa pequena")[0] == []  # sem embedding, só palavras


def test_falha_de_embedding_espera_antes_de_tentar_de_novo(store, embed):
    store.add("comprar uva", [])
    store.backfill_embeddings()
    embed.fail = True
    store.add("pagar luz", [])
    store.backfill_embeddings()
    calls = len(embed.calls)

    store.search("uva")
    store.backfill_embeddings()
    assert len(embed.calls) == calls  # em espera: nem a consulta nem o backfill chamam

    embed.fail = False
    store.reset_embed_cooldown()  # o que uma chave nova faz
    assert store.backfill_embeddings() == 1
    assert all(saved["embedding"] for saved in _on_disk(store))


def test_busca_mista_inclui_itens_ainda_sem_embedding(store, embed):
    store.add("comprar uva", [])
    store.backfill_embeddings()
    embed.fail = True
    store.add("lembrar da uva no sábado", [])  # fica sem embedding
    embed.fail = False
    store.reset_embed_cooldown()

    found, mode = store.search("uva")
    assert mode == "semantica"
    assert {item["content"] for item, _ in found} == {"comprar uva", "lembrar da uva no sábado"}


def test_backfill_descarta_o_vetor_se_o_conteudo_mudou_durante_a_chamada(store):
    item = store.add("versão 1", [])
    entered, release = threading.Event(), threading.Event()

    def slow_embed(texts, task_type):
        entered.set()
        release.wait(5)
        return [fake_embedding(text) for text in texts]

    store.embed = slow_embed
    worker = threading.Thread(target=store.backfill_embeddings)
    worker.start()
    assert entered.wait(5)
    assert store.backfill_embeddings() == 0  # um backfill de cada vez
    store.update(item["id"], content="versão 2")
    release.set()
    worker.join(5)
    assert store.get(item["id"])["embedding"] is None


def test_text_score_normaliza_acento_caixa_e_plural():
    item = {"content": "Ligar para a Mãe", "tags": ["família"]}
    assert text_score("ligar mae", item) == 1.0
    assert text_score("FAMILIA", item) == 1.0
    assert text_score("ligações", item) == 0.0
