"""Conversas na nuvem (fase 3): dois "PCs" com pastas próprias e a mesma nuvem simulada. Mensagens
sobem pela fila (sem duplicar), chegam no outro PC pelo pull incremental, continuar a conversa no
outro PC junta as mensagens, apagar e regenerar viram marcas sem conteúdo, sem conexão a fila espera,
sem as tabelas tudo fica no PC (com um aviso) e a migração sobe o que já existia."""

import time

import pytest

from robin.cloud_mock import FakeSupabase
from robin.conversations import ConversationRecorder, ConversationStore
from robin.conversations_cloud import MISSING_TABLES, ConversationCloud
from robin.supabase import SupabaseClient

URL = "https://teste.supabase.co"
KEY = "sb_secret_chave-de-teste-1234"


class Machine:
    """Um PC: conversas na própria pasta, a mesma nuvem."""

    def __init__(self, folder, cloud: FakeSupabase, cloud_on: bool = True):
        self.notices: list[str] = []
        self.store = ConversationStore(folder / "conversas")
        self.recorder = ConversationRecorder(self.store)
        self.client = SupabaseClient(URL, KEY, transport=cloud.transport())
        self.cloud = ConversationCloud(self.client, folder / "sync-conversas.json", self.store, self.recorder,
                                       on_notice=self.notices.append)
        if cloud_on:
            self.recorder.cloud = self.cloud
        self._gen = 0

    def say(self, user: str, robin: str, actions: tuple = ()) -> int:
        self._gen += 1
        self.recorder.begin(self._gen, user)
        for action in actions:
            self.recorder.event("onToolCall", {"gen_id": self._gen, **action})
        self.recorder.event("onChunk", {"gen_id": self._gen, "text": robin})
        self.recorder.event("onDone", {"gen_id": self._gen, "stopped": False})
        self.cloud.flush()  # espera a fila (a thread do envio usa o mesmo lock)
        return self._gen

    def texts(self, conversation_id: str) -> list[str]:
        return [message["text"] for message in self.store.get(conversation_id)["messages"]]


@pytest.fixture
def cloud():
    return FakeSupabase()


@pytest.fixture
def pcs(tmp_path, cloud):
    def make(name: str, **kwargs) -> Machine:
        return Machine(tmp_path / name, cloud, **kwargs)
    return make


def _messages(cloud, conversation_id=None) -> list[dict]:
    rows = sorted(cloud.tables["conversation_messages"].values(), key=lambda row: row["created_at"])
    return [row for row in rows if conversation_id is None or row["conversation_id"] == conversation_id]


def test_conversa_sobe_e_chega_no_outro_pc(pcs, cloud):
    a, b = pcs("a"), pcs("b")
    a.say("lembre de comprar pão", "Anotado.", [{"tool": "salvar_memoria", "label": "Salvou tarefa: comprar pão", "ok": True}])
    conversation_id = a.recorder.current_id

    assert list(cloud.tables["conversations"]) == [conversation_id]
    assert [row["text"] for row in _messages(cloud)] == ["lembre de comprar pão", "Anotado."]
    result = b.cloud.pull()
    assert result == {"ok": True, "changed": [conversation_id], "deleted": [], "current_deleted": False}
    copy = b.store.get(conversation_id)
    assert copy["title"] == "lembre de comprar pão"
    assert [m["text"] for m in copy["messages"]] == ["lembre de comprar pão", "Anotado."]
    assert copy["messages"][1]["actions"] == [{"tool": "salvar_memoria", "label": "Salvou tarefa: comprar pão", "ok": True}]
    # O que já está igual não é gravado de novo (nem no PC que mandou, quando o eco volta).
    assert b.cloud.pull()["changed"] == []
    assert a.cloud.pull()["changed"] == []


def test_continuar_no_outro_pc_junta_as_mensagens(pcs, cloud):
    a, b = pcs("a"), pcs("b")
    a.say("primeira", "resposta 1")
    conversation_id = a.recorder.current_id
    b.cloud.pull()
    b.recorder.open(b.store.get(conversation_id))
    time.sleep(0.002)  # created_at de B depois do de A
    b.say("continuando em B", "resposta 2")

    # A ainda está com a conversa aberta: o pull junta as mensagens de B na conversa atual dele.
    result = a.cloud.pull()
    assert result["changed"] == [conversation_id]
    time.sleep(0.002)
    a.say("de volta em A", "resposta 3")
    expected = ["primeira", "resposta 1", "continuando em B", "resposta 2", "de volta em A", "resposta 3"]
    assert a.texts(conversation_id) == expected
    assert [row["text"] for row in _messages(cloud, conversation_id)] == expected
    b.cloud.pull()
    assert b.texts(conversation_id) == expected


def test_apagar_num_pc_apaga_no_outro_e_tira_o_conteudo_da_nuvem(pcs, cloud):
    a, b = pcs("a"), pcs("b")
    a.say("segredo", "guardado")
    conversation_id = a.recorder.current_id
    b.cloud.pull()
    b.recorder.open(b.store.get(conversation_id))

    assert a.recorder.delete(conversation_id)
    a.cloud.flush()
    head = cloud.tables["conversations"][conversation_id]
    assert head["deleted"] is True and head["title"] == ""
    assert _messages(cloud, conversation_id) == []  # o conteúdo saiu da nuvem

    result = b.cloud.pull()
    assert result["deleted"] == [conversation_id] and result["current_deleted"] is True
    assert b.store.get(conversation_id) is None and b.recorder.current_id is None


def test_regenerar_e_desfazer_viram_marcas_sem_conteudo(pcs, cloud):
    a, b = pcs("a"), pcs("b")
    gen = a.say("me conte algo", "versão 1")
    conversation_id = a.recorder.current_id
    a.recorder.begin_regenerate(gen + 1)
    a.recorder.event("onChunk", {"gen_id": gen + 1, "text": "versão 2"})
    a.recorder.event("onDone", {"gen_id": gen + 1, "stopped": False})
    a.recorder.begin(gen + 2, "esta não foi")
    a.recorder.abort(gen + 2)  # envio recusado (ocupado, sem chave)
    a.cloud.flush()

    tombstones = [row for row in _messages(cloud) if row["deleted"]]
    assert sorted(row["text"] for row in tombstones) == ["", ""] and len(tombstones) == 2
    b.cloud.pull()
    assert b.texts(conversation_id) == ["me conte algo", "versão 2"]


def test_sem_conexao_a_fila_espera_e_sai_no_pull(pcs, cloud):
    a, b = pcs("a"), pcs("b")
    cloud.go_offline()
    a.say("sem internet", "fica na fila")
    assert a.cloud.pending == 2 and _messages(cloud) == []
    assert a.store.get(a.recorder.current_id)  # no PC está tudo

    cloud.go_online()
    assert a.cloud.pull()["ok"] and a.cloud.pending == 0
    assert [row["text"] for row in _messages(cloud)] == ["sem internet", "fica na fila"]
    b.cloud.pull()
    assert b.texts(a.recorder.current_id) == ["sem internet", "fica na fila"]


def test_sem_as_tabelas_fica_no_pc_avisa_uma_vez_e_migra_depois(pcs, cloud):
    cloud.conversation_tables = False  # o SQL das conversas ainda não foi rodado
    a = pcs("a")
    a.say("primeira", "ok")
    a.recorder.new()
    a.say("segunda", "ok")

    assert a.cloud.pending == 0 and a.notices == [MISSING_TABLES]
    assert len(a.store.summaries()) == 2
    assert a.cloud.pull()["ok"] is False and a.notices == [MISSING_TABLES]

    cloud.conversation_tables = True
    assert a.cloud.pull()["ok"]
    assert sorted(row["text"] for row in _messages(cloud)) == ["ok", "ok", "primeira", "segunda"]


def test_migracao_sobe_o_que_ja_existia_uma_vez_por_projeto(pcs, cloud):
    a = pcs("a", cloud_on=False)  # conversas de antes da nuvem
    a.recorder.begin(1, "de antes")
    a.recorder.event("onChunk", {"gen_id": 1, "text": "ok"})
    a.recorder.event("onDone", {"gen_id": 1})
    assert cloud.tables["conversations"] == {}

    a.recorder.cloud = a.cloud
    assert a.cloud.pull()["ok"]
    assert [row["text"] for row in _messages(cloud)] == ["de antes", "ok"]
    cloud.tables["conversation_messages"].clear()
    a.cloud.pull()
    assert _messages(cloud) == []  # a migração não roda de novo neste projeto


def test_api_manda_a_conversa_para_a_nuvem(files, robin_api, events):
    files.env.write_text(f"ROBIN_SUPABASE_URL={URL}\nROBIN_SUPABASE_KEY={KEY}\n", encoding="utf-8")
    robin = robin_api()
    _wait(lambda: robin._last_pull > 0)
    result = robin.send_message("oi, nuvem")
    events.wait("onDone", result["gen_id"])
    robin._conversation_cloud.flush()

    conversation_id = robin.list_conversations()["current"]
    assert list(files.cloud.tables["conversations"]) == [conversation_id]
    assert [row["role"] for row in _messages(files.cloud)] == ["user", "robin"]

    assert robin.delete_conversation(conversation_id)["ok"]
    robin._conversation_cloud.flush()
    assert files.cloud.tables["conversations"][conversation_id]["deleted"] is True

    robin.disconnect_supabase()
    assert robin._recorder.cloud is None


def _wait(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condição não atingida")
