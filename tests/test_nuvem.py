"""Cérebro no Supabase com HTTP falso (cloud_mock.FakeSupabase atrás do httpx.MockTransport): CRUD,
RPC de busca, fila offline e reenvio sem duplicar, proteção de versão velha, núcleo, migração do
brain.json, banco pausado, limpeza de 90 dias, configuração pela API e contagem de pedidos."""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import send_and_wait

from robin import supabase as supabase_module
from robin.brain import DONE, PENDING, BrainError, LocalStore, StaleError
from robin.cloud_mock import FakeSupabase
from robin.config import CORE_REQUEST_ID, DEFAULT_MODEL
from robin.mock import MockGeminiService, fake_embedding
from robin.supabase import SupabaseClient, SupabaseStore
from robin.tools import MemoryTools, Turn

URL = "https://teste.supabase.co"
KEY = "sb_secret_chave-de-teste-1234"


def _embed(texts, task):
    return [fake_embedding(text) for text in texts]


@pytest.fixture
def cloud():
    return FakeSupabase()


@pytest.fixture
def machine(tmp_path, cloud):
    """Fábrica de "máquinas": cada uma com a própria pasta (brain.json, sync.json) e a mesma nuvem."""
    def make(name: str = "A", transport=None) -> SupabaseStore:
        folder = tmp_path / name
        store = SupabaseStore(LocalStore(folder / "brain.json"),
                              SupabaseClient(URL, KEY, transport=transport or cloud.transport()), folder / "sync.json")
        store.embed = _embed
        return store
    return make


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# ---------- CRUD ----------

def test_crud_grava_na_nuvem_e_na_copia_local(machine, cloud):
    store = machine()
    item = store.add("comprar uva", ["compras"], PENDING, request_id="r1", embedding=fake_embedding("comprar uva"))

    assert item["sync"] == "cloud"
    row = cloud.rows[item["id"]]
    assert row["content"] == "comprar uva" and row["request_id"] == "r1" and row["updated_by"]
    assert row["embedding"] == pytest.approx(fake_embedding("comprar uva"))
    assert store.local.get(item["id"])["embedding"] == pytest.approx(row["embedding"])  # o texto do pgvector virou lista

    changed = store.update(item["id"], content="comprar uva verde")
    assert cloud.rows[item["id"]]["content"] == "comprar uva verde" and changed["embedding"] is None
    done = store.complete(item["id"])
    assert cloud.rows[item["id"]]["status"] == DONE and done["done_at"]
    store.delete(item["id"])
    assert cloud.rows == {} and store.local.snapshot() == []
    assert store.status()["state"] == "ok"


def test_chave_vai_so_no_apikey(cloud):
    seen = []
    inner = cloud.transport()

    def spy(request):
        seen.append(dict(request.headers))
        return inner.handle_request(request)

    SupabaseClient(URL, KEY, transport=httpx.MockTransport(spy)).select({"select": "id"})
    assert seen[0]["apikey"] == KEY and "authorization" not in seen[0]


def test_request_id_repetido_nao_duplica(machine, cloud):
    store = machine()
    first = store.add("comprar uva", [], PENDING, request_id="turno-1")
    again = store.add("comprar uvas", [], PENDING, request_id="turno-1")
    assert again["id"] == first["id"] and len(cloud.rows) == 1


# ---------- busca ----------

def test_busca_por_significado_usa_a_rpc(machine, cloud):
    store = machine()
    for text in ("comprar uva", "pagar conta de luz", "reunião com o João"):
        store.add(text, [], PENDING, embedding=fake_embedding(text))
    before = cloud.count()

    found, mode = store.search("fruta roxa pequena")
    assert mode == "semantica" and found[0][0]["content"] == "comprar uva"
    assert cloud.log[before:] == [("POST", "/rest/v1/rpc/match_memories")]


def test_sem_embedding_a_busca_usa_o_fts(machine, cloud):
    store = machine()
    store.add("Reunião com o João na sexta", [], None)
    store.embed = None
    found, mode = store.search("reuniao joao")
    assert mode == "texto" and [item["content"] for item, _ in found] == ["Reunião com o João na sexta"]
    assert cloud.log[-1] == ("GET", "/rest/v1/memories")


def test_offline_a_busca_usa_a_copia_local(machine, cloud):
    store = machine()
    store.add("comprar uva", [], PENDING, embedding=fake_embedding("comprar uva"))
    cloud.go_offline()
    found, _ = store.search("fruta roxa pequena")
    assert found[0][0]["content"] == "comprar uva"
    assert store.status()["state"] == "offline"


# ---------- fila offline ----------

def test_offline_vai_para_a_fila_e_reenvia_sem_duplicar(machine, cloud):
    store = machine()
    cloud.go_offline()
    item = store.add("comprar uva", [], PENDING, request_id="r1")

    assert item["sync"] == "pending"
    assert cloud.rows == {} and store.local.get(item["id"])
    assert store.status() | {} == {**store.status(), "state": "offline", "pending": 1}
    assert json.loads(store.state._path.read_text(encoding="utf-8"))["queue"][0]["op"] == "insert"

    cloud.go_online()
    assert store.flush() == 1
    assert [row["content"] for row in cloud.rows.values()] == ["comprar uva"]
    assert store.flush() == 0 and len(cloud.rows) == 1


def test_reenvio_depois_de_resposta_perdida_nao_duplica(machine, cloud):
    """O insert chegou ao banco mas a resposta se perdeu: a fila reenvia e o request_id segura."""
    inner = cloud.transport()
    state = {"lose": True}

    def lossy(request):
        response = inner.handle_request(request)
        if state["lose"] and request.method == "POST":
            state["lose"] = False
            raise httpx.ReadTimeout("resposta perdida (teste)", request=request)
        return response

    store = machine(transport=httpx.MockTransport(lossy))
    item = store.add("comprar uva", [], PENDING)
    assert item["sync"] == "pending" and len(cloud.rows) == 1

    assert store.flush() == 1
    assert len(cloud.rows) == 1 and store.state.queue == []


def test_alteracoes_offline_em_sequencia_chegam_em_ordem(machine, cloud):
    store = machine()
    item = store.add("comprar uva", [], PENDING)
    cloud.go_offline()
    store.update(item["id"], content="comprar uva verde")
    store.complete(item["id"])
    assert len(store.state.queue) == 2

    cloud.go_online()
    assert store.flush() == 2
    row = cloud.rows[item["id"]]
    assert row["content"] == "comprar uva verde" and row["status"] == DONE


def test_nova_alteracao_esvazia_a_fila_primeiro(machine, cloud):
    store = machine()
    cloud.go_offline()
    store.add("primeiro", [], None)
    cloud.go_online()
    second = store.add("segundo", [], None)
    assert second["sync"] == "cloud" and store.state.queue == []
    assert sorted(row["content"] for row in cloud.rows.values()) == ["primeiro", "segundo"]


def test_ferramenta_avisa_que_nao_salvou_na_nuvem(machine, cloud):
    store = machine()
    cloud.go_offline()
    result = MemoryTools(store).call("salvar_memoria", {"conteudo": "comprar uva", "etiquetas": [], "tarefa": True},
                                     Turn("t:1"), set())
    assert result.ok and "NÃO foi para a nuvem" in result.response["nuvem"]
    assert result.label == "Salvou tarefa: comprar uva (na fila: sem conexão)"


# ---------- versão velha ----------

def test_versao_velha_recarrega_e_nao_sobrescreve(machine, cloud):
    a, b = machine("A"), machine("B")
    item = a.add("reunião sexta", ["trabalho"], None)
    b.pull()
    a.update(item["id"], content="reunião sexta às 15h")

    with pytest.raises(StaleError) as stale:
        b.update(item["id"], content="reunião sábado")
    assert stale.value.current["content"] == "reunião sexta às 15h"
    assert cloud.rows[item["id"]]["content"] == "reunião sexta às 15h"
    assert b.local.get(item["id"])["content"] == "reunião sexta às 15h"  # recarregou

    result = MemoryTools(b).call("atualizar_memoria", {"id": item["id"], "conteudo": "x"}, Turn("t:2"), set())
    assert result.ok  # com a versão nova na cópia, a próxima alteração passa


def test_ferramenta_avisa_versao_velha(machine, cloud):
    a, b = machine("A"), machine("B")
    item = a.add("comprar uva", [], PENDING)
    b.pull()
    a.update(item["id"], content="comprar uva verde")
    result = MemoryTools(b).call("concluir_tarefa", {"id": item["id"]}, Turn("t:1"), set())
    assert not result.ok and result.response["versao_mudou"] is True
    assert result.response["item"]["conteudo"] == "comprar uva verde"
    assert cloud.rows[item["id"]]["status"] == PENDING


def test_alteracao_da_fila_perde_para_a_nuvem_mais_nova(machine, cloud):
    a, b = machine("A"), machine("B")
    item = a.add("comprar uva", [], PENDING)
    b.pull()
    cloud.go_offline()
    b.update(item["id"], content="versão de B (offline)")
    cloud.go_online()
    a.update(item["id"], content="versão de A")

    b.flush()
    assert cloud.rows[item["id"]]["content"] == "versão de A"
    assert b.local.get(item["id"])["content"] == "versão de A"
    assert b.take_conflicts() and b.state.queue == []


def test_item_apagado_em_outra_maquina(machine, cloud):
    a, b = machine("A"), machine("B")
    item = a.add("nota", [], None)
    b.pull()
    a.delete(item["id"])
    with pytest.raises(BrainError, match="apagado em outra máquina"):
        b.update(item["id"], content="x")
    assert b.local.get(item["id"]) is None


# ---------- pull ----------

def test_pull_traz_o_que_mudou_e_tira_o_que_sumiu(machine, cloud):
    a, b = machine("A"), machine("B")
    kept = a.add("fica", [], None)
    gone = a.add("sai", [], None)
    b.pull()
    a.delete(gone["id"])
    a.add("novo", [], PENDING)

    summary = b.pull()
    assert summary["ok"] and summary["removed"] == 1
    assert sorted(item["content"] for item in b.local.snapshot()) == ["fica", "novo"]
    trash = json.loads((b.local.path.with_name("brain.lixeira.json")).read_text(encoding="utf-8"))
    assert [item["content"] for item in trash] == ["sai"]  # o backup guarda o que saiu
    assert b.local.get(kept["id"])["updated_at"] == cloud.rows[kept["id"]]["updated_at"]

    requests = cloud.count()
    b.pull()  # nada mudou: só o índice
    assert cloud.count() - requests == 1


def test_pull_pagina_o_indice(machine, cloud, monkeypatch):
    monkeypatch.setattr(supabase_module, "PAGE_SIZE", 2)
    a, b = machine("A"), machine("B")
    for n in range(5):
        a.add(f"item {n}", [], None)
    assert b.pull()["ok"]
    assert len(b.local.snapshot()) == 5


def test_pull_offline_nao_apaga_nada(machine, cloud):
    a = machine("A")
    a.add("item", [], None)
    cloud.go_offline()
    assert a.pull() == {"ok": False, "state": "offline"}
    assert len(a.local.snapshot()) == 1


# ---------- migração ----------

def test_migracao_sobe_o_brain_json_uma_vez(tmp_path, machine, cloud, caplog):
    caplog.set_level(logging.INFO, logger="robin.supabase")
    local = LocalStore(tmp_path / "A" / "brain.json")
    local.add("antiga sem request_id", [], None)
    local.add("antiga com request_id", [], PENDING, request_id="turno-velho")
    cloud.rows["x"] = {"id": "x", "content": "já estava", "tags": [], "status": None, "pinned": False, "embedding": None,
                       "request_id": "turno-velho", "created_at": _ago(1), "updated_at": _ago(1), "done_at": None,
                       "updated_by": "B"}

    store = machine("A")
    assert store.pull()["ok"]
    contents = sorted(row["content"] for row in cloud.rows.values())
    assert contents == ["antiga sem request_id", "já estava"]  # o request_id repetido não duplicou
    assert store.state.migrated(URL)
    assert any("migração do brain.json: 2 item(ns) local(is), 1 enviado(s)" in r.getMessage() for r in caplog.records)

    store.pull()
    assert len(cloud.rows) == 2  # a migração não roda de novo


# ---------- núcleo ----------

def test_primeira_conexao_sem_nucleo_sobe_o_memory_md(machine, cloud):
    store = machine()
    store.pull(core_seed=lambda: "# Núcleo da máquina A\n")
    core = [row for row in cloud.rows.values() if row["pinned"]]
    assert len(core) == 1 and core[0]["request_id"] == CORE_REQUEST_ID
    assert core[0]["content"] == "# Núcleo da máquina A"
    assert store.pinned_items()[0]["content"] == "# Núcleo da máquina A"


def test_primeira_conexao_com_nucleo_na_nuvem_vence(machine, cloud):
    machine("A").pull(core_seed=lambda: "núcleo de A")
    b = machine("B")
    b.pull(core_seed=lambda: "núcleo de B (perde)")
    assert [row["content"] for row in cloud.rows.values() if row["pinned"]] == ["núcleo de A"]
    assert b.core_item()["content"] == "núcleo de A"


def test_editor_salva_o_nucleo_com_protecao_de_versao(machine, cloud):
    a, b = machine("A"), machine("B")
    a.pull(core_seed=lambda: "v1")
    b.pull()
    a.save_core("v2 de A")
    with pytest.raises(StaleError):
        b.save_core("v2 de B")
    assert b.core_item()["content"] == "v2 de A"


# ---------- banco pausado ----------

def test_banco_pausado_fica_so_leitura(machine, cloud):
    store = machine()
    item = store.add("comprar uva", [], PENDING)
    cloud.paused = True

    assert store.pull()["ok"] is False
    assert store.status()["state"] == "paused"
    assert "Banco pausado: reative no painel do Supabase" in store.status()["message"]
    with pytest.raises(BrainError, match="Banco pausado"):
        store.add("outra", [], None)
    with pytest.raises(BrainError, match="Banco pausado"):
        store.complete(item["id"])
    assert store.state.queue == []  # pausado não enfileira: nada a reenviar
    assert store.get(item["id"]) and store.list_tasks(PENDING)  # leitura continua
    assert store.search("uva")[0]

    cloud.paused = False
    assert store.pull()["ok"] and store.status()["state"] == "ok"
    store.add("outra", [], None)


def test_ferramenta_com_banco_pausado_nao_diz_que_salvou(machine, cloud):
    store = machine()
    store.pull()
    cloud.paused = True
    store.search("x")  # descobre que pausou
    result = MemoryTools(store).call("salvar_memoria", {"conteudo": "comprar uva", "etiquetas": [], "tarefa": True},
                                     Turn("t:1"), set())
    assert not result.ok and "Banco pausado" in result.response["erro"]


# ---------- limpeza ----------

def test_limpeza_apaga_concluidas_ha_mais_de_90_dias(machine, cloud):
    store = machine()
    old = store.add("velha", [], PENDING)
    recent = store.add("recente", [], PENDING)
    store.add("núcleo", [], None, request_id=CORE_REQUEST_ID, pinned=True)
    for item, days in ((old, 100), (recent, 10)):
        cloud.rows[item["id"]].update(status=DONE, done_at=_ago(days))
    store.pull()

    assert store.pull(cleanup_days=90)["ok"]
    assert sorted(row["content"] for row in cloud.rows.values()) == ["núcleo", "recente"]
    assert store.local.get(old["id"]) is None


def test_limpeza_local_sem_supabase(tmp_path):
    local = LocalStore(tmp_path / "brain.json")
    old = local.add("velha", [], PENDING)
    local.complete(old["id"])
    local.put({**local.get(old["id"]), "done_at": _ago(91)})
    local.add("pendente antiga", [], PENDING)
    assert local.cleanup_done(90) == 1
    assert [item["content"] for item in local.snapshot()] == ["pendente antiga"]


# ---------- RobinAPI ----------

def _with_cloud_env(files):
    files.env.write_text(f"ROBIN_SUPABASE_URL={URL}\nROBIN_SUPABASE_KEY={KEY}\n", encoding="utf-8")


def _wait(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condição não atingida")


def test_api_configura_valida_e_desconecta(files, robin_api, events):
    robin = robin_api()
    bad = robin.set_supabase(URL, "chave-errada")
    assert bad["ok"] is False and bad["code"] == "auth"
    assert "ROBIN_SUPABASE_URL" not in files.env.read_text(encoding="utf-8") if files.env.exists() else True

    files.cloud.paused = True
    assert robin.set_supabase(URL, KEY)["code"] == "paused"
    files.cloud.paused = False

    result = robin.set_supabase(URL + "/rest/v1/", KEY)
    assert result["ok"] and result["supabase_url"] == URL and result["supabase_key_hint"] == "••••1234"
    assert result["git_sync"] is False
    _wait(lambda: robin._cloud.core_item() is not None)  # primeira conexão subiu o memory.md
    assert robin.get_memory() == {"ok": True, "content": "# Memória de teste", "source": "cloud"}
    assert robin.set_settings({"git_sync": True})["code"] == "cloud"

    off = robin.disconnect_supabase()
    assert off["ok"] and off["cloud"] is None and off["supabase_url"] == ""
    assert robin.get_memory()["source"] == "local"


def test_esquema_ausente_aponta_para_os_dois_sqls(files, robin_api, events, caplog):
    """Projeto sem a tabela memories: Configurações e log dizem quais SQLs rodar, e na ordem."""
    robin = robin_api()
    memories = files.cloud.tables.pop("memories")
    try:
        with caplog.at_level(logging.WARNING, logger="robin.api"):
            result = robin.set_supabase(URL, KEY)
    finally:
        files.cloud.tables["memories"] = memories
    assert result["ok"] is False and result["code"] == "schema"
    for text in (result["error"], caplog.text):
        assert "scripts/supabase_memorias.sql" in text and "scripts/supabase_conversas.sql" in text
        assert text.index("supabase_memorias.sql") < text.index("supabase_conversas.sql")
    assert "ROBIN_SUPABASE_URL" not in (files.env.read_text(encoding="utf-8") if files.env.exists() else "")


def test_scripts_sql_existem_e_nao_apagam_nada():
    """Os SQLs citados têm a estrutura, os índices e as permissões esperadas pelo cliente."""
    import re
    from pathlib import Path

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    memories = (scripts / "supabase_memorias.sql").read_text(encoding="utf-8")
    conversations = (scripts / "supabase_conversas.sql").read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", memories).lower()
    assert not re.search(r"^\s*(drop|truncate|delete)\b", code, re.M)
    assert "create table if not exists public.memories" in code
    assert "create unique index if not exists memories_request_id_key on public.memories (request_id)" in code
    assert "create or replace function public.match_memories" in code
    assert "enable row level security" in code
    # As colunas que o cliente lê e grava estão todas na tabela.
    for column in supabase_module.COLUMNS.split(","):
        assert re.search(rf"^\s+{column}\s", code, re.M), column
    # A memória não usa o trigger das conversas (o updated_at é a versão gravada pelo app).
    assert "robin_touch_updated_at" not in code
    assert "robin_touch_updated_at" in conversations
    assert "grant select, insert, update, delete on table public.memories to service_role" in code
    assert "grant execute on function public.match_memories(vector, integer, boolean) to service_role" in code
    assert "grant select, insert, update on table public.conversations to service_role" in conversations
    assert "grant select, insert, update, delete on table public.conversation_messages to service_role" in conversations


def test_api_nucleo_vai_no_system_instruction_e_editor_salva(files, robin_api, events):
    _with_cloud_env(files)
    robin = robin_api()
    _wait(lambda: robin._cloud.core_item() is not None)

    assert robin.save_memory("# Núcleo novo\n")["ok"]
    assert [row["content"] for row in files.cloud.rows.values() if row["pinned"]] == ["# Núcleo novo\n"]
    assert files.memory.read_text(encoding="utf-8") == "# Memória de teste\n"  # o memory.md fica como backup
    assert "# Núcleo novo" in robin._gemini._new_chat([])._config.system_instruction

    files.cloud.go_offline()
    assert robin.save_memory("# Núcleo offline\n") == {"ok": True, "sync": "pending"}


def test_api_salvar_nucleo_velho_e_recusado(files, robin_api, events):
    _with_cloud_env(files)
    robin = robin_api()
    _wait(lambda: robin._cloud.core_item() is not None)
    core = robin._cloud.core_item()
    files.cloud.rows[core["id"]].update(content="mudado em B", updated_at=_ago(-1))

    result = robin.save_memory("minha versão")
    assert result["code"] == "stale"
    assert files.cloud.rows[core["id"]]["content"] == "mudado em B"
    robin._flush_events()
    assert events.of("onMemoryChanged")


def test_api_puxa_ao_aparecer_a_janela_com_intervalo(files, robin_api, events, monkeypatch):
    _with_cloud_env(files)
    robin = robin_api()
    _wait(lambda: robin._last_pull > 0)
    before = files.cloud.count()

    robin._on_summon()  # logo depois do pull inicial: ignorado
    time.sleep(0.1)
    assert files.cloud.count() == before

    robin._last_pull = 0.0
    robin._on_summon()
    _wait(lambda: files.cloud.count() > before)


def test_api_pull_avisa_a_aba_memoria(files, robin_api, events):
    _with_cloud_env(files)
    robin = robin_api()
    _wait(lambda: robin._last_pull > 0)
    robin._flush_events()
    assert events.of("onBrainChanged")  # a lista da aba Memória recarrega depois de puxar


def test_api_avisa_status_da_nuvem(files, robin_api, events):
    _with_cloud_env(files)
    robin = robin_api()
    _wait(lambda: robin._last_pull > 0)
    files.cloud.paused = True
    robin._sync()
    robin._flush_events()
    status = events.of("onCloudStatus")[-1]
    assert status["state"] == "paused" and "Banco pausado" in status["message"]
    assert robin.get_state()["cloud"]["state"] == "paused"


# ---------- pedidos por pedido do usuário ----------

def test_pedidos_por_pedido_do_usuario(tmp_path, memory, events, cloud, fast):
    """Meta da fase 2: no máximo 2 pedidos ao modelo. Antes: "lembre de X" = 3 (buscar, salvar,
    resposta) e "o que tenho pra fazer?" = 2 (listar, resposta). Com a confirmação local, as ações
    claras gastam 1 (a confirmação sai do app)."""
    store = SupabaseStore(LocalStore(tmp_path / "brain.json"),
                          SupabaseClient(URL, KEY, transport=cloud.transport()), tmp_path / "sync.json")
    service = MockGeminiService(emit=events, model=DEFAULT_MODEL, store=store)
    calls = []
    original = service._client.models.generate_content_stream
    service._client.models.generate_content_stream = lambda **kw: calls.append(1) or original(**kw)
    try:
        cloud_before = cloud.count()
        send_and_wait(service, events, "lembre de comprar uva", 1)
        assert len(calls) == 1
        assert cloud.count() - cloud_before == 1  # um insert, com o embedding junto

        calls.clear()
        send_and_wait(service, events, "o que tenho pra fazer?", 2)
        assert len(calls) == 1
        assert "comprar uva" in "".join(chunk["text"] for chunk in events.of("onChunk", 2))

        calls.clear()
        send_and_wait(service, events, "comprei uva", 3)
        assert len(calls) == 1 and store.list_tasks(PENDING) == []
    finally:
        service.clear()
