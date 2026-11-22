"""Conversas na nuvem (fase 3 da reestruturação): as conversas guardadas no PC (conversations.py)
também no Supabase, para valerem em todos os PCs. Tabelas conversations (uma linha por conversa) e
conversation_messages (uma por mensagem), criadas pelo usuário com scripts/supabase_conversas.sql.

- Cada mudança do ConversationRecorder vira uma operação na fila (sync-conversas.json) e uma thread a
  envia em ordem: a conversa (e a geração) nunca espera a rede. Sem conexão, a fila espera e sai no
  próximo envio ou pull (ao abrir, ao aparecer a janela, a cada minuto offline).
- Mensagem só é inserida (on_conflict=id, ignore-duplicates): reenviar não duplica. Regenerar e
  desfazer um envio marcam a mensagem como apagada, sem o conteúdo; apagar a conversa apaga as
  mensagens dela na nuvem e deixa só a marca (sem o título), para os outros PCs apagarem também.
- Pull incremental pelo updated_at do servidor (trigger do SQL), com uma folga de OVERLAP: não depende
  do relógio de cada PC. Na primeira vez em cada projeto (URL), as conversas deste PC sobem.
- Sem as tabelas (SQL não rodado), as conversas ficam só no PC: nada vai para a fila, o aviso aparece
  uma vez e, quando as tabelas existirem, a migração sobe tudo.
"""

import json
import logging
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from .brain import atomic_write, machine_name, parse_time
from .supabase import CloudError, SupabaseClient

logger = logging.getLogger("robin.conversations_cloud")

CONVERSATIONS = "conversations"
MESSAGES = "conversation_messages"
CONVERSATION_COLUMNS = "id,title,created_at,updated_at,updated_by,deleted"
MESSAGE_COLUMNS = "id,conversation_id,role,text,actions,status,error,created_at,updated_at,deleted"
# Folga do pull: linhas gravadas perto do último pull são lidas de novo (aplicar de novo não muda nada).
OVERLAP = timedelta(seconds=5)
ID_CHUNK = 100  # ids por pedido no filtro in.(...)
_REMOVED = {"deleted": True, "text": "", "actions": None, "error": None}
MISSING_TABLES = ("As conversas ainda não vão para a nuvem: crie as tabelas no Supabase com o "
                  "scripts/supabase_conversas.sql (depois do scripts/supabase_memorias.sql; veja o README).")


def _conversation_row(conversation: dict) -> dict:
    return {"id": conversation["id"], "title": conversation.get("title") or "Conversa",
            "created_at": conversation.get("created_at"), "updated_by": machine_name()}


def _message_row(conversation_id: str, message: dict) -> dict:
    return {"id": message["id"], "conversation_id": conversation_id, "role": message["role"],
            "text": message.get("text") or "", "actions": message.get("actions"), "status": message.get("status"),
            "error": message.get("error"), "created_at": message.get("created_at")}


def _save_op(conversation: dict, messages: list[dict]) -> dict:
    return {"op": "save", "conversation": _conversation_row(conversation),
            "messages": [_message_row(conversation["id"], message) for message in messages]}


class _State:
    """sync-conversas.json: a fila, os projetos (URL) já migrados e até onde o pull leu cada tabela."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data: Optional[dict] = None

    def _load(self) -> dict:
        if self._data is None:
            data: dict[str, Any] = {"queue": [], "migrated": [], "cursors": {}}
            if self._path.exists():
                try:
                    loaded = json.loads(self._path.read_text(encoding="utf-8"))
                    data.update({key: type(data[key])(loaded.get(key) or data[key]) for key in data})
                except (OSError, ValueError, AttributeError, TypeError) as error:
                    logger.error("sync-conversas.json ilegível (%s); começa do zero", error)
            self._data = data
        return self._data

    def _save(self) -> None:
        atomic_write(self._path, json.dumps(self._data, ensure_ascii=False, indent=1), ".sync-")

    @property
    def queue(self) -> list[dict]:
        with self._lock:
            return [dict(op) for op in self._load()["queue"]]

    def push(self, op: dict) -> None:
        with self._lock:
            self._load()["queue"].append(op)
            self._save()

    def set_queue(self, ops: list[dict]) -> None:
        with self._lock:
            self._load()["queue"] = ops
            self._save()

    def migrated(self, url: str) -> bool:
        with self._lock:
            return url in self._load()["migrated"]

    def mark_migrated(self, url: str) -> None:
        with self._lock:
            self._load()["migrated"].append(url)
            self._save()

    def cursor(self, table: str) -> Optional[str]:
        with self._lock:
            return self._load()["cursors"].get(table)

    def set_cursor(self, table: str, value: str) -> None:
        with self._lock:
            self._load()["cursors"][table] = value
            self._save()


class ConversationCloud:
    """A ponte das conversas com o Supabase. `store` e `recorder` são os de conversations.py: a
    migração lê o store; o que vem da nuvem é gravado só pelo recorder (apply_remote)."""

    def __init__(self, client: SupabaseClient, state_file: Path, store: Any, recorder: Any,
                 on_notice: Callable[[str], None] = lambda message: None):
        self._client = client
        self._state = _State(state_file)
        self._store = store
        self._recorder = recorder
        self._on_notice = on_notice
        self._lock = threading.RLock()  # envio da fila e pull, um de cada vez
        self._available = True  # False: as tabelas não existem (até um pull dar certo)
        self._notified = False

    @property
    def pending(self) -> int:
        return len(self._state.queue)

    # ---------- o que o ConversationRecorder chama (só entra na fila; o envio é numa thread) ----------

    def saved(self, conversation: dict, messages: list[dict]) -> None:
        """Mensagens novas (com a conversa, que a nuvem cria se ainda não existir)."""
        self._enqueue(_save_op(conversation, messages))

    def removed(self, message_ids: list[str]) -> None:
        self._enqueue({"op": "remove", "ids": list(message_ids)})

    def deleted(self, conversation_id: str) -> None:
        self._enqueue({"op": "delete", "id": conversation_id})

    def _enqueue(self, op: dict) -> None:
        if not self._available:
            return  # sem as tabelas: a migração sobe tudo quando elas existirem
        self._state.push(op)
        threading.Thread(target=self._flush_quietly, name="robin-conversas-nuvem", daemon=True).start()

    def _flush_quietly(self) -> None:
        try:
            self.flush()
        except Exception:  # noqa: BLE001 - a fila fica para o próximo envio ou pull
            logger.exception("Envio das conversas para a nuvem falhou")

    # ---------- fila ----------

    def flush(self) -> int:
        """Envia a fila em ordem; para no primeiro erro de rede (o resto espera). Devolve quantas foram."""
        sent = 0
        with self._lock:
            queue = self._state.queue
            while queue:
                op = queue[0]
                try:
                    self._send(op)
                except CloudError as error:
                    if error.kind == "schema":
                        self._missing_tables(error)
                        self._state.set_queue([])  # a migração sobe tudo quando as tabelas existirem
                        return sent
                    if error.kind not in ("bad_request", "conflict"):
                        self._state.set_queue(queue)
                        logger.warning("Conversas: nuvem indisponível (%s); %d na fila", error.kind, len(queue))
                        return sent
                    # Recusada pela nuvem (dado inválido): reenviar não adianta e travaria a fila.
                    logger.error("Conversas: operação %s descartada pela nuvem: %s", op["op"], error)
                queue.pop(0)
                self._state.set_queue(queue)
                sent += 1
        return sent

    def _send(self, op: dict) -> None:
        if op["op"] == "save":
            # A conversa antes das mensagens (chave estrangeira); se já existe, nada muda nela.
            self._client.insert_new(CONVERSATIONS, [op["conversation"]])
            if op["messages"]:
                self._client.insert_new(MESSAGES, op["messages"])
        elif op["op"] == "remove":
            for start in range(0, len(op["ids"]), ID_CHUNK):
                chunk = op["ids"][start:start + ID_CHUNK]
                self._client.update({"id": f"in.({','.join(chunk)})"}, _REMOVED, MESSAGES)
        elif op["op"] == "delete":
            # A marca fica (sem o título) para os outros PCs apagarem; as mensagens saem da nuvem.
            self._client.update({"id": f"eq.{op['id']}"}, {"deleted": True, "title": ""}, CONVERSATIONS)
            self._client.delete({"conversation_id": f"eq.{op['id']}"}, MESSAGES)

    # ---------- pull ----------

    def pull(self) -> dict:
        """Envia a fila, sobe as conversas deste PC (uma vez por projeto) e aplica o que mudou na nuvem
        (recorder.apply_remote). Devolve {"ok", "changed", "deleted", "current_deleted"}."""
        with self._lock:
            self.flush()
            try:
                if not self._state.migrated(self._client.url):
                    self._migrate()
                heads, heads_cursor = self._since(CONVERSATIONS, CONVERSATION_COLUMNS)
                rows, rows_cursor = self._since(MESSAGES, MESSAGE_COLUMNS)
                by_conversation: dict[str, list[dict]] = {}
                for row in rows:
                    by_conversation.setdefault(row["conversation_id"], []).append(row)
                head_by_id = {head["id"]: head for head in heads}
                # Mensagens de conversas cuja linha não mudou: a linha diz se ela existe e se foi apagada.
                missing = [key for key in by_conversation if key not in head_by_id]
                for start in range(0, len(missing), ID_CHUNK):
                    chunk = missing[start:start + ID_CHUNK]
                    for head in self._client.select({"select": CONVERSATION_COLUMNS, "id": f"in.({','.join(chunk)})"},
                                                     CONVERSATIONS):
                        head_by_id[head["id"]] = head
            except CloudError as error:
                if error.kind == "schema":
                    self._missing_tables(error)
                else:
                    logger.warning("Conversas: pull falhou (%s)", error.kind)
                return {"ok": False, "kind": error.kind}
            self._available = True
            changed, deleted, current_deleted = self._recorder.apply_remote(head_by_id, by_conversation)
            for table, cursor in ((CONVERSATIONS, heads_cursor), (MESSAGES, rows_cursor)):
                if cursor:
                    self._state.set_cursor(table, cursor)
        if changed or deleted:
            logger.info("Conversas: %d mudada(s) e %d apagada(s) por outro PC", len(changed), len(deleted))
        return {"ok": True, "changed": changed, "deleted": deleted, "current_deleted": current_deleted}

    def _since(self, table: str, columns: str) -> tuple[list[dict], Optional[str]]:
        """As linhas com updated_at depois do último pull (menos a folga), e o novo cursor (que nunca volta)."""
        params = {"select": columns}
        cursor = parse_time(self._state.cursor(table))
        if cursor:
            params["updated_at"] = f"gt.{(cursor - OVERLAP).isoformat()}"
        rows = self._client.select_all(params, table, order="updated_at")
        stamps = [stamp for stamp in (parse_time(row.get("updated_at")) for row in rows) if stamp]
        newest = max([*stamps, *([cursor] if cursor else [])], default=None)
        return rows, newest.isoformat() if newest and newest != cursor else None

    def _migrate(self) -> None:
        """Primeira vez neste projeto: as conversas deste PC sobem (reenviar não duplica)."""
        sent = 0
        for summary in self._store.summaries():
            conversation = self._store.get(summary["id"])
            if conversation and conversation["messages"]:
                self._send(_save_op(conversation, conversation["messages"]))
                sent += 1
        self._state.mark_migrated(self._client.url)
        if sent:
            logger.info("Conversas: %d conversa(s) deste PC enviada(s) à nuvem", sent)

    def _missing_tables(self, error: CloudError) -> None:
        self._available = False
        logger.warning("Conversas: tabelas não encontradas no Supabase (%s)", error.detail)
        if not self._notified:
            self._notified = True
            self._on_notice(MISSING_TABLES)
