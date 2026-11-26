"""Conversas guardadas no PC: um JSON por conversa em DATA_DIR/conversas, gravado de forma atômica.

ConversationStore lista (com filtro), abre, grava e apaga, busca nas conversas antigas
(buscar_conversas) e monta o trecho das mais recentes para o system_instruction.
ConversationRecorder acompanha a conversa atual pelos eventos da geração: o que a interface mostra é
o que fica guardado (a mensagem do usuário na hora; a resposta no fim, com as ações, parada ou erro;
regenerar troca a última resposta). Com o Supabase, cada mudança também vai para a fila da nuvem
(conversations_cloud.py) e o que veio de outros PCs entra por apply_remote.

Formato: {id, title, created_at, updated_at, updated_by, messages: [{id, role: "user" | "robin", text,
created_at, actions?, status?: "stopped" | "error", error?}]}. Mensagem não muda depois de gravada.
"""

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import config
from .brain import atomic_write, machine_name, normalize, now_iso, parse_time, text_score
from .clock import local_time

logger = logging.getLogger("robin.conversations")

USER = "user"
ROBIN = "robin"
STOPPED = "stopped"
ERROR = "error"
TITLE_LIMIT = 60
SEARCH_TEXT_LIMIT = 500
_SPEAKERS = {USER: "Pessoa", ROBIN: "Robin"}
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _collapse(text: str) -> str:
    return " ".join(str(text or "").split())


def _cut(text: str, limit: int) -> str:
    text = _collapse(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def title_for(text: str) -> str:
    """Título da conversa: a primeira mensagem, numa linha (sem pedido ao modelo)."""
    return _cut(text, TITLE_LIMIT) or "Conversa"


def _local(stamp: str, pattern: str) -> str:
    try:
        return local_time(datetime.fromisoformat(stamp)).strftime(pattern)
    except (TypeError, ValueError):
        return "?"


def _valid_id(value: str) -> bool:
    """Ids vêm da interface: só uuid vira nome de arquivo (nada de caminho)."""
    try:
        return str(uuid.UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def _message(role: str, text: str) -> dict:
    return {"id": str(uuid.uuid4()), "role": role, "text": text, "created_at": now_iso()}


def _summary(conversation: dict) -> dict:
    messages = conversation["messages"]
    return {
        "id": conversation["id"],
        "title": conversation.get("title") or "Conversa",
        "created_at": conversation.get("created_at", ""),
        "updated_at": conversation.get("updated_at", ""),
        "count": len(messages),
        "haystack": normalize(" ".join([conversation.get("title", ""), *(m.get("text") or "" for m in messages)])),
    }


def conversation_identity(conversation: dict) -> tuple[str, str]:
    """O JSON de ações da primeira mensagem leva provedor e conta no sync existente."""
    first = next((message for message in conversation["messages"] if message["role"] == USER), {})
    meta = first.get("actions")
    if isinstance(meta, dict) and meta.get("provider") == "chatgpt":
        return "chatgpt", meta.get("profile") or ""
    return "gemini", ""


def history_for_session(conversation: dict, budget: int) -> list[tuple[str, str]]:
    """As mensagens mais recentes (até `budget` caracteres) como (papel, texto) para a sessão do
    Gemini: "user" e "model", só texto (resposta sem texto fica de fora). Começa pelo usuário."""
    picked: list[tuple[str, str]] = []
    used = 0
    for message in reversed(conversation["messages"]):
        text = message.get("text") or ""
        if not text.strip():
            continue
        if picked and used + len(text) > budget:
            break
        picked.append(("user" if message["role"] == USER else "model", text))
        used += len(text)
    picked.reverse()
    while picked and picked[0][0] == "model":
        picked.pop(0)
    return picked


class ConversationStore:
    """Um JSON por conversa em `folder`. O índice (título, datas, número de mensagens e o texto
    normalizado para filtrar a lista) fica em memória, montado na primeira consulta."""

    def __init__(self, folder: Path):
        self._folder = Path(folder)
        self._lock = threading.RLock()
        self._index: Optional[dict[str, dict]] = None

    def _path(self, conversation_id: str, *, chatgpt: bool = False) -> Path:
        # Versões antigas enumeram apenas .json quando montam o contexto do Gemini.
        suffix = "chatgpt" if chatgpt else "json"
        return self._folder / f"{conversation_id}.{suffix}"

    def _existing_path(self, conversation_id: str) -> Path:
        local = self._path(conversation_id, chatgpt=True)
        return local if local.exists() else self._path(conversation_id)

    def _read(self, path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("id") != path.stem or not isinstance(data.get("messages"), list):
                raise ValueError("formato inesperado")
            return data
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            # Nunca apaga nem sobrescreve o que não entendeu: só fica fora da lista.
            logger.error("Conversa ilegível (%s): %s", path.name, error)
            return None

    def _load_index(self) -> dict[str, dict]:
        if self._index is None:
            index = {}
            for path in sorted([*self._folder.glob("*.json"), *self._folder.glob("*.chatgpt")]):
                data = self._read(path)
                if data is not None:
                    index[data["id"]] = _summary(data)
            self._index = index
        return self._index

    def summaries(self, query: str = "") -> list[dict]:
        """Mais recentes primeiro. `query` filtra pelo título e pelo texto (todas as palavras, sem
        acento e caixa)."""
        words = normalize(query).split()
        with self._lock:
            known = list(self._load_index().values())
        found = [summary for summary in known if all(word in summary["haystack"] for word in words)]
        found.sort(key=lambda summary: summary["updated_at"], reverse=True)
        return [{key: value for key, value in summary.items() if key != "haystack"} for summary in found]

    def get(self, conversation_id: str) -> Optional[dict]:
        if not _valid_id(conversation_id):
            return None
        return self._read(self._existing_path(conversation_id))

    def save(self, conversation: dict) -> None:
        """Grava a conversa inteira. Levanta OSError."""
        text = json.dumps(conversation, ensure_ascii=False, indent=1)
        with self._lock:
            path = self._path(conversation["id"], chatgpt=conversation_identity(conversation)[0] == "chatgpt")
            atomic_write(path, text, ".conversa-")
            self._load_index()[conversation["id"]] = _summary(conversation)

    def delete(self, conversation_id: str) -> bool:
        if not _valid_id(conversation_id):
            return False
        with self._lock:
            self._load_index().pop(conversation_id, None)
            try:
                self._existing_path(conversation_id).unlink()
            except FileNotFoundError:
                return False
            return True

    def excerpt(self, exclude: Optional[str], budget: int, message_chars: int,
                *, identity: Optional[tuple[str, str]] = None) -> tuple[str, bool]:
        """Trechos das conversas mais recentes (menos a `exclude`) para o system_instruction: as
        mensagens mais novas até `budget` caracteres, em ordem cronológica e agrupadas por conversa.
        Devolve (texto, cortou); cortou = ficou mensagem de fora, e aí buscar_conversas é oferecida."""
        blocks: list[tuple[str, list[str]]] = []
        used, cut = 0, False
        for summary in self.summaries():
            if summary["id"] == exclude:
                continue
            conversation = self.get(summary["id"])
            if conversation is None or (identity is not None and conversation_identity(conversation) != identity):
                continue
            if used >= budget:
                cut = True
                break
            header = f"— {_local(conversation.get('created_at', ''), '%d/%m/%Y')} · \"{summary['title']}\""
            used += len(header) + 1
            lines: list[str] = []
            for message in reversed(conversation["messages"]):
                text = _cut(message.get("text") or "", message_chars)
                if not text:
                    continue
                line = f"{_local(message.get('created_at', ''), '%d/%m %H:%M')} {_SPEAKERS.get(message['role'], '?')}: {text}"
                if used + len(line) + 1 > budget:
                    cut = True
                    break
                lines.append(line)
                used += len(line) + 1
            if lines:
                blocks.append((header, lines[::-1]))
            if cut:
                break
        parts = ["\n".join([header, *lines]) for header, lines in reversed(blocks)]
        if cut:
            parts.append("— … há conversas mais antigas fora daqui: para elas, use buscar_conversas.")
        return ("\n\n".join(parts) or "(nenhuma ainda)"), cut

    def search(self, query: str, exclude: Optional[str], limit: int,
               *, identity: Optional[tuple[str, str]] = None) -> list[dict]:
        """Mensagens das outras conversas com as palavras da consulta (sem acento e caixa), das mais
        parecidas e recentes para as menos."""
        found = []
        for summary in self.summaries():
            if summary["id"] == exclude:
                continue
            conversation = self.get(summary["id"])
            if conversation is None or (identity is not None and conversation_identity(conversation) != identity):
                continue
            for message in conversation["messages"]:
                text = message.get("text") or ""
                score = text_score(query, {"content": text, "tags": []}) if text.strip() else 0.0
                if score > 0:
                    found.append((score, message.get("created_at", ""), summary["title"], message))
        found.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [{
            "conversa": title,
            "quando": _local(message.get("created_at", ""), "%d/%m/%Y %H:%M"),
            "quem": _SPEAKERS.get(message["role"], "?"),
            "texto": _cut(message["text"], SEARCH_TEXT_LIMIT),
        } for _, _, title, message in found[:limit]]


def merge_remote(local: Optional[dict], head: Optional[dict], rows: list[dict]) -> Optional[dict]:
    """A conversa local com as mensagens que vieram da nuvem (linhas de conversation_messages): as novas
    entram em ordem de criação, as marcadas como apagadas saem e as que já existem ficam como estão
    (mensagem não muda). Sem conversa local, nasce da linha de conversations (`head`). None: nada a
    guardar (conversa desconhecida ou sem mensagens)."""
    if local is None:
        if head is None:
            return None
        local = {"id": head["id"], "title": head.get("title") or "Conversa", "created_at": head.get("created_at"),
                 "updated_at": head.get("created_at"), "updated_by": head.get("updated_by"), "messages": []}
    messages = {message["id"]: message for message in local["messages"]}
    for row in rows:
        if row.get("deleted"):
            messages.pop(row["id"], None)
        elif row["id"] not in messages:
            message = {key: row.get(key) for key in ("id", "role", "text", "created_at")}
            message["text"] = message["text"] or ""
            message.update({key: row[key] for key in ("actions", "status", "error") if row.get(key)})
            messages[row["id"]] = message
    if not messages:
        return None
    ordered = sorted(messages.values(), key=lambda message: parse_time(message.get("created_at")) or _EPOCH)
    merged = {**local, "messages": ordered}
    newest = max((local.get("updated_at"), ordered[-1].get("created_at")),
                 key=lambda stamp: parse_time(stamp) or _EPOCH)
    merged["updated_at"] = newest
    return merged


class ConversationContext:
    """Contexto que um provedor pode consultar para além da conversa atual."""

    def __init__(self, recorder, provider: str, account: Callable[[], str] = lambda: ""):
        self._recorder = recorder
        self._provider = provider
        self._account = account

    def prompt_excerpt(self) -> tuple[str, bool]:
        return self._recorder.prompt_excerpt(identity=(self._provider, self._account()))

    def search(self, query: str, limit: int) -> list[dict]:
        return self._recorder.search(query, limit, identity=(self._provider, self._account()))


class ConversationRecorder:
    """A conversa atual e as respostas em andamento (por gen_id), montadas pelos eventos da geração.
    Os eventos chegam das threads da geração; abrir, nova e apagar, da interface; o que veio da nuvem,
    do pull: tudo sob o lock (só o recorder grava as conversas). Falha de disco ou da fila da nuvem vai
    para o log e nunca derruba a conversa."""

    def __init__(self, store: ConversationStore, on_change: Callable[[], None] = lambda: None):
        self._store = store
        self._on_change = on_change
        self._lock = threading.RLock()
        self._current: Optional[dict] = None  # None: conversa nova, que só vira arquivo na 1ª mensagem
        self._pending: dict[int, dict] = {}
        # Com o Supabase (conversations_cloud.ConversationCloud): recebe cada mudança, só para a fila.
        self.cloud: Any = None

    @property
    def current_id(self) -> Optional[str]:
        with self._lock:
            return self._current["id"] if self._current else None

    # ---------- o que o GeminiService usa ----------

    @property
    def identity(self) -> Optional[tuple[str, str]]:
        with self._lock:
            return conversation_identity(self._current) if self._current else None

    def prompt_excerpt(self, *, identity: Optional[tuple[str, str]] = None) -> tuple[str, bool]:
        return self._store.excerpt(self.current_id, config.MAX_PROMPT_CONVERSATION_CHARS,
                                   config.PROMPT_MESSAGE_CHARS, identity=identity)

    def search(self, query: str, limit: int, *, identity: Optional[tuple[str, str]] = None) -> list[dict]:
        return self._store.search(query, self.current_id, limit, identity=identity)

    # ---------- geração ----------

    def begin(self, gen_id: int, text: str, identity: tuple[str, str] = ("gemini", ""),
              attachments: Optional[list[str]] = None, folder: bool = False) -> str:
        """Grava a mensagem do usuário antes da resposta e devolve o ID do turno."""
        with self._lock:
            if self._current is None:
                stamp = now_iso()
                self._current = {"id": str(uuid.uuid4()), "title": title_for(text), "created_at": stamp,
                                 "updated_at": stamp, "updated_by": machine_name(), "messages": []}
            message = _message(USER, text)
            provider, profile = identity
            if provider == "chatgpt" and not self._current["messages"]:
                message["actions"] = {"provider": provider, "profile": profile}
            if attachments:
                message.setdefault("actions", {})["attachments"] = attachments
            if folder:
                message.setdefault("actions", {})["folder"] = True
            self._current["messages"].append(message)
            self._pending[gen_id] = {"text": "", "actions": [], "user": message["id"], "regenerate": False}
            self._save()
            self._to_cloud("saved", self._current, [message])
            return message["id"]

    @property
    def last_external_context_turn(self) -> Optional[str]:
        """ID do último turno quando sua mensagem usou anexos ou uma pasta."""
        with self._lock:
            if not self._current:
                return None
            message = next((msg for msg in reversed(self._current["messages"]) if msg["role"] == USER), {})
            meta = message.get("actions")
            return message["id"] if isinstance(meta, dict) and (meta.get("attachments") or meta.get("folder")) else None

    def begin_regenerate(self, gen_id: int) -> None:
        with self._lock:
            self._pending[gen_id] = {"text": "", "actions": [], "user": None, "regenerate": True}

    def abort(self, gen_id: int) -> None:
        """A geração nem começou (sem chave, ocupado, falha ao montar): desfaz o begin."""
        with self._lock:
            pending = self._pending.pop(gen_id, None)
            conversation = self._current
            if pending is None or pending["user"] is None or conversation is None:
                return
            local_only = conversation_identity(conversation)[0] == "chatgpt"
            messages = conversation["messages"]
            if messages and messages[-1]["id"] == pending["user"]:
                messages.pop()
            if messages:
                self._save()
                self._to_cloud("removed", [pending["user"]])
                return
            self._current = None
            try:
                self._store.delete(conversation["id"])
            except OSError as error:
                logger.error("Conversa vazia não apagada (%s): %s", conversation["id"], error)
            if not local_only:
                self._to_cloud("deleted", conversation["id"])
        self._on_change()

    def event(self, name: str, payload: dict) -> None:
        """Eventos da geração (os mesmos que vão para a interface)."""
        gen_id = payload.get("gen_id") if isinstance(payload, dict) else None
        if gen_id is None:
            return
        with self._lock:
            pending = self._pending.get(gen_id)
            if pending is None:
                return
            if name == "onChunk":
                pending["text"] += payload.get("text") or ""
            elif name == "onToolCall":
                pending["actions"].append({"tool": payload.get("tool"), "label": payload.get("label"),
                                           "ok": bool(payload.get("ok"))})
            elif name == "onDone":
                self._finish(gen_id, STOPPED if payload.get("stopped") else None)
            elif name == "onError":
                self._finish(gen_id, ERROR, {"code": payload.get("code"), "message": payload.get("message")})

    def _finish(self, gen_id: int, status: Optional[str], error: Optional[dict] = None) -> None:
        pending = self._pending.pop(gen_id)
        if self._current is None:
            return
        messages = self._current["messages"]
        removed: list[str] = []
        if pending["regenerate"]:
            # Regenerar troca o que veio depois da última mensagem do usuário (resposta antiga, erro).
            last_user = max((index for index, message in enumerate(messages) if message["role"] == USER), default=-1)
            removed = [message["id"] for message in messages[last_user + 1:]]
            del messages[last_user + 1:]
        message = _message(ROBIN, pending["text"])
        if pending["actions"]:
            message["actions"] = pending["actions"]
        if status:
            message["status"] = status
        if error:
            message["error"] = error
        messages.append(message)
        self._save()
        if removed:
            self._to_cloud("removed", removed)
        self._to_cloud("saved", self._current, [message])

    # ---------- conversa atual ----------

    def new(self) -> None:
        """Começa outra conversa; a anterior continua guardada."""
        with self._lock:
            self._settle()
            self._current = None
        self._on_change()

    def open(self, conversation: dict) -> None:
        """Continua uma conversa guardada (a interface e a sessão do Gemini já a carregaram)."""
        with self._lock:
            self._settle()
            self._current = conversation
        self._on_change()

    def delete(self, conversation_id: str) -> bool:
        """Apaga a conversa (do PC e, com a nuvem, dos outros PCs). Se era a atual, a próxima mensagem
        começa outra. Devolve se havia arquivo; levanta OSError se o disco recusar."""
        with self._lock:
            was_current = bool(self._current and self._current["id"] == conversation_id)
            conversation = self._current if was_current else self._store.get(conversation_id)
            local_only = conversation and conversation_identity(conversation)[0] == "chatgpt"
            if was_current:
                self._pending.clear()
                self._current = None
            deleted = self._store.delete(conversation_id)
            if (deleted or was_current) and not local_only:
                self._to_cloud("deleted", conversation_id)
        self._on_change()
        return deleted

    def apply_remote(self, heads: dict[str, dict], rows: dict[str, list[dict]]) -> tuple[list[str], list[str], bool]:
        """O que o pull trouxe da nuvem: `heads` são as linhas de conversations por id e `rows`, as de
        conversation_messages por conversa. Grava as conversas que mudaram (a atual também em memória)
        e apaga as marcadas como apagadas. Devolve (mudadas, apagadas, a atual foi apagada)."""
        changed: list[str] = []
        deleted: list[str] = []
        current_deleted = False
        with self._lock:
            for conversation_id in [*heads, *(key for key in rows if key not in heads)]:
                head = heads.get(conversation_id)
                is_current = bool(self._current and self._current["id"] == conversation_id)
                if head is not None and head.get("deleted"):
                    try:
                        gone = self._store.delete(conversation_id)
                    except OSError as error:
                        logger.error("Conversa apagada em outro PC não apagada aqui (%s): %s", conversation_id, error)
                        continue
                    if is_current:
                        self._pending.clear()
                        self._current = None
                        current_deleted = True
                    if gone or is_current:
                        deleted.append(conversation_id)
                    continue
                local = self._current if is_current else self._store.get(conversation_id)
                merged = merge_remote(local, head, rows.get(conversation_id, []))
                if merged is None or merged == local:
                    continue
                try:
                    self._store.save(merged)
                except OSError as error:
                    logger.error("Conversa da nuvem não gravada (%s): %s", conversation_id, error)
                    continue
                if is_current:
                    self._current = merged
                changed.append(conversation_id)
        if changed or deleted:
            self._on_change()
        return changed, deleted, current_deleted

    def _to_cloud(self, method: str, *args) -> None:
        cloud = self.cloud
        if cloud is None:
            return
        if method != "deleted" and self._current and conversation_identity(self._current)[0] == "chatgpt":
            return
        try:
            getattr(cloud, method)(*args)
        except Exception:  # noqa: BLE001 - a fila da nuvem nunca derruba a conversa
            logger.exception("Conversa: a mudança não entrou na fila da nuvem (%s)", method)

    def _settle(self) -> None:
        """Trocar de conversa no meio de uma resposta: o que já tinha chegado fica como interrompido."""
        for gen_id in list(self._pending):
            pending = self._pending[gen_id]
            if pending["text"].strip() or pending["actions"]:
                self._finish(gen_id, STOPPED)
            else:
                self._pending.pop(gen_id)

    def _save(self) -> None:
        conversation = self._current
        conversation["updated_at"] = now_iso()
        conversation["updated_by"] = machine_name()
        try:
            self._store.save(conversation)
        except OSError as error:
            logger.error("Conversa não gravada (%s): %s", conversation["id"], error)
            return
        self._on_change()
