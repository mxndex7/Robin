"""Cérebro do Robin: a memória única que ele grava e consulta pelas ferramentas (tools.py).

MemoryStore é a interface trocável: LocalStore (brain.json) e SupabaseStore (supabase.py), que usa
um LocalStore como cópia local e backup. Cada item segue o esquema da tabela `memories`: id, content, tags, status (None | "pending" | "done"), pinned,
embedding (768), request_id, created_at, updated_at, done_at, updated_by.

Embeddings: o store recebe a função `embed` (GeminiService.embed). No plano gratuito 429 é
esperado: salvar nunca depende do embedding (o item fica sem e o backfill gera depois) e a busca
cai para texto (sem acento e caixa) quando o embedding da consulta falha.
"""

import json
import logging
import math
import os
import platform
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import CORE_REQUEST_ID

logger = logging.getLogger("robin.brain")

PENDING = "pending"
DONE = "done"
STATUSES = (None, PENDING, DONE)

TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"

# Depois de uma falha de embedding (429, rede), nenhuma chamada nova por este tempo.
EMBED_COOLDOWN = 60.0
BACKFILL_BATCH = 50
_REPLACE_ATTEMPTS = 5  # os.replace no Windows falha se o antivírus estiver lendo o arquivo

# (textos, task_type) -> vetores. Levanta exceção quando não há embedding disponível.
Embed = Callable[[list[str], str], list[list[float]]]

_STOPWORDS = {
    "a", "o", "as", "os", "um", "uma", "de", "da", "do", "das", "dos", "e", "em", "no", "na", "nos",
    "nas", "por", "pra", "para", "com", "que", "meu", "minha", "meus", "minhas", "se", "eu", "me",
}


class BrainError(Exception):
    """Falha do armazenamento (arquivo ilegível, item inexistente, nuvem recusou)."""


class StaleError(BrainError):
    """O item mudou em outra máquina desde a cópia local: nada foi alterado. current: a versão nova."""

    def __init__(self, current: dict):
        super().__init__("Este item mudou em outra máquina; a versão atual foi recarregada.")
        self.current = current


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def machine_name() -> str:
    return os.getenv("COMPUTERNAME") or platform.node() or "desconhecido"


def normalize(text: str) -> str:
    """Minúsculas, sem acento e com espaços simples (busca por texto e chaves de idempotência)."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.split())


def _words(text: str) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9]+", normalize(text)) if word not in _STOPWORDS]


def _stem(word: str) -> str:
    """Plural simples: "uvas" acha "uva"."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def text_score(query: str, item: dict) -> float:
    """Fração das palavras da consulta presentes no conteúdo ou nas etiquetas."""
    terms = {_stem(word) for word in _words(query)}
    if not terms:
        return 0.0
    haystack = {_stem(word) for word in _words(" ".join([item["content"], *item["tags"]]))}
    found = sum(1 for term in terms if any(word.startswith(term) for word in haystack))
    return found / len(terms)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def parse_time(stamp: Optional[str]) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def done_cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def atomic_write(path: Path, text: str, prefix: str) -> None:
    """Grava num temporário da mesma pasta e troca de uma vez (os.replace, com novas tentativas: no
    Windows ele falha se o antivírus estiver lendo o arquivo). Levanta OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def clean_tags(tags) -> list[str]:
    result: list[str] = []
    for tag in tags or []:
        tag = " ".join(str(tag).split()).lower()
        if tag and tag not in result:
            result.append(tag)
    return result


class MemoryStore(ABC):
    """Interface do cérebro. Datas em ISO 8601 (UTC). Toda escrita devolve o item gravado."""

    def __init__(self):
        self.embed: Optional[Embed] = None
        self._embed_blocked_until = 0.0
        self._backfill_lock = threading.Lock()

    @abstractmethod
    def add(self, content: str, tags: list[str], status: Optional[str] = None,
            request_id: Optional[str] = None, pinned: bool = False,
            embedding: Optional[list[float]] = None) -> dict:
        """Novo item. Um request_id já gravado devolve o item existente (idempotência).
        No SupabaseStore, o item devolvido traz "sync": "cloud" ou "pending" (na fila, sem conexão)."""

    @abstractmethod
    def update(self, item_id: str, content: Optional[str] = None, tags: Optional[list[str]] = None) -> dict:
        """Edita no lugar. Mudar o conteúdo descarta o embedding (o backfill gera de novo)."""

    @abstractmethod
    def complete(self, item_id: str) -> dict:
        """Marca a tarefa como concluída (status "done" e done_at). Nada é apagado."""

    @abstractmethod
    def delete(self, item_id: str) -> dict:
        """Remove o item e devolve o que foi removido."""

    @abstractmethod
    def get(self, item_id: str) -> Optional[dict]: ...

    @abstractmethod
    def list_tasks(self, status: str = PENDING) -> list[dict]:
        """status: "pending", "done" ou "all". Todas as tarefas desse status, mais recentes primeiro."""

    @abstractmethod
    def search(self, query: str, include_done: bool = False, limit: int = 8) -> tuple[list[tuple[dict, float]], str]:
        """Itens por significado (cosseno) ou, sem embedding, por texto. Devolve ([(item, nota)], modo)."""

    @abstractmethod
    def backfill_embeddings(self, limit: Optional[int] = None) -> int:
        """Gera os embeddings que faltam. Devolve quantos foram gravados."""

    @abstractmethod
    def cleanup_done(self, days: int) -> int:
        """Apaga as tarefas concluídas há mais de `days` dias (nunca itens fixos). Devolve quantas."""

    @abstractmethod
    def snapshot(self) -> list[dict]:
        """Todos os itens (com o vetor), da cópia mais próxima: o arquivo ou a cópia local da nuvem."""

    # ---------- consultas comuns ----------

    def pinned_items(self) -> list[dict]:
        """Itens fixos (núcleo), o do editor primeiro."""
        pinned = [item for item in self.snapshot() if item.get("pinned")]
        return sorted(pinned, key=lambda item: (item.get("request_id") != CORE_REQUEST_ID, item["created_at"]))

    def document_embedding(self, content: str) -> Optional[list[float]]:
        """Vetor de documento para gravar junto com o item (e conferir parecidos). None se indisponível."""
        vectors = self._try_embed([content], TASK_DOCUMENT)
        return vectors[0] if vectors else None

    def most_similar(self, content: str, vector: Optional[list[float]]) -> Optional[tuple[dict, float]]:
        """Item não concluído mais parecido com `content` (cosseno entre documentos; sem vetor, só
        o mesmo texto normalizado conta). Itens fixos ficam de fora."""
        best: Optional[tuple[dict, float]] = None
        wanted = normalize(content)
        for item in self.snapshot():
            if item["status"] == DONE or item.get("pinned"):
                continue
            if normalize(item["content"]) == wanted:
                score = 1.0
            elif vector and item.get("embedding"):
                score = cosine(vector, item["embedding"])
            else:
                continue
            if best is None or score > best[1]:
                best = (_public(item), round(score, 3))
        return best

    # ---------- embedding (comum às implementações) ----------

    def _try_embed(self, texts: list[str], task_type: str) -> Optional[list[list[float]]]:
        """Vetores ou None (sem embed, em espera depois de uma falha, ou falhou agora)."""
        if not self.embed or not texts or time.monotonic() < self._embed_blocked_until:
            return None
        try:
            vectors = self.embed(texts, task_type)
        except Exception as error:  # noqa: BLE001 - 429, rede, chave: o cérebro segue sem embedding
            self._embed_blocked_until = time.monotonic() + EMBED_COOLDOWN
            logger.warning("Embedding indisponível (%s): %s", type(error).__name__, str(error)[:200])
            return None
        if len(vectors) != len(texts):
            logger.warning("Embedding devolveu %d vetores para %d textos", len(vectors), len(texts))
            return None
        return [[round(float(value), 6) for value in vector] for vector in vectors]

    def reset_embed_cooldown(self) -> None:
        """Chave nova: tenta o embedding de novo na hora."""
        self._embed_blocked_until = 0.0


class LocalStore(MemoryStore):
    """JSON em DATA_DIR/brain.json com gravação atômica (temporário + os.replace)."""

    def __init__(self, path: Path):
        super().__init__()
        self._path = Path(path)
        self._lock = threading.RLock()
        self._items: Optional[list[dict]] = None

    @property
    def path(self) -> Path:
        return self._path

    # ---------- arquivo ----------

    def _load(self) -> list[dict]:
        if self._items is not None:
            return self._items
        items: list[dict] = []
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                items = list(data["items"])
            except (ValueError, KeyError, TypeError) as error:
                # Nunca sobrescreve um arquivo que não entendeu: guarda uma cópia antes.
                backup = self._path.with_name(f"brain.corrompido-{time.strftime('%Y%m%d-%H%M%S')}.json")
                self._path.replace(backup)
                logger.error("brain.json ilegível (%s); cópia em %s, começando vazio", error, backup)
            except OSError as error:
                raise BrainError(f"Não foi possível ler {self._path.name}: {error}") from error
        self._items = items
        return items

    def _save(self) -> None:
        data = json.dumps({"version": 1, "items": self._items}, ensure_ascii=False, indent=1)
        try:
            atomic_write(self._path, data, ".brain-")
        except OSError as error:
            raise BrainError(f"Não foi possível gravar {self._path.name}: {error}") from error

    def _find(self, item_id: str) -> dict:
        for item in self._load():
            if item["id"] == item_id:
                return item
        raise BrainError(f"Item {item_id} não encontrado.")

    def _commit(self, before: list[dict]) -> None:
        """Grava; se falhar, a memória em uso volta ao estado anterior."""
        try:
            self._save()
        except BrainError:
            self._items = before
            raise

    # ---------- operações ----------

    def add(self, content, tags, status=None, request_id=None, pinned=False, embedding=None):
        content = (content or "").strip()
        if not content:
            raise BrainError("Conteúdo vazio.")
        if status not in STATUSES:
            raise BrainError(f"Status inválido: {status}")
        with self._lock:
            items = self._load()
            if request_id:
                for item in items:
                    if item.get("request_id") == request_id:
                        return dict(item)
            stamp = now_iso()
            item = {
                "id": str(uuid.uuid4()),
                "content": content,
                "tags": clean_tags(tags),
                "status": status,
                "pinned": bool(pinned),
                "embedding": embedding,
                "request_id": request_id,
                "created_at": stamp,
                "updated_at": stamp,
                "done_at": None,
                "updated_by": machine_name(),
            }
            before = list(items)
            self._items = [*items, item]
            self._commit(before)
            return dict(item)

    def update(self, item_id, content=None, tags=None):
        with self._lock:
            item = self._find(item_id)
            changed = dict(item)
            if content is not None and content.strip() and content.strip() != item["content"]:
                changed["content"] = content.strip()
                changed["embedding"] = None
            if tags is not None:
                changed["tags"] = clean_tags(tags)
            if changed == item:
                return dict(item)
            changed["updated_at"] = now_iso()
            changed["updated_by"] = machine_name()
            self._replace(item, changed)
            return dict(changed)

    def complete(self, item_id):
        with self._lock:
            item = self._find(item_id)
            if item["status"] == DONE:
                return dict(item)
            if item["status"] != PENDING:
                raise BrainError("Este item não é uma tarefa.")
            stamp = now_iso()
            changed = {**item, "status": DONE, "done_at": stamp, "updated_at": stamp, "updated_by": machine_name()}
            self._replace(item, changed)
            return dict(changed)

    def delete(self, item_id):
        with self._lock:
            item = self._find(item_id)
            before = list(self._items)
            self._items = [other for other in before if other is not item]
            self._commit(before)
            return dict(item)

    def get(self, item_id):
        with self._lock:
            try:
                return dict(self._find(item_id))
            except BrainError:
                return None

    def list_tasks(self, status=PENDING):
        wanted = (PENDING, DONE) if status == "all" else (status,)
        with self._lock:
            tasks = [dict(item) for item in self._load() if item["status"] in wanted]
        key = "done_at" if status == DONE else "created_at"
        return sorted(tasks, key=lambda item: item.get(key) or item["created_at"], reverse=True)

    def search(self, query, include_done=False, limit=8):
        with self._lock:
            items = [dict(item) for item in self._load() if include_done or item["status"] != DONE]
        if not items or not (query or "").strip():
            return [], "texto"
        limit = max(1, int(limit))
        vectors = self._try_embed([query], TASK_QUERY) if any(item["embedding"] for item in items) else None
        if vectors is None:
            scored = [(item, text_score(query, item)) for item in items]
            scored = [pair for pair in scored if pair[1] > 0]
            mode = "texto"
        else:
            # Itens ainda sem embedding entram pela busca por texto.
            scored = [
                (item, cosine(vectors[0], item["embedding"]) if item["embedding"] else text_score(query, item))
                for item in items
            ]
            scored = [pair for pair in scored if pair[1] > 0]
            mode = "semantica"
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [(_public(item), round(score, 3)) for item, score in scored[:limit]], mode

    def backfill_embeddings(self, limit=None):
        if not self._backfill_lock.acquire(blocking=False):
            return 0  # já há um backfill em andamento
        try:
            done = 0
            while limit is None or done < limit:
                with self._lock:
                    missing = [(item["id"], item["content"]) for item in self._load() if not item["embedding"]]
                batch = missing[:min(BACKFILL_BATCH, (limit - done) if limit else BACKFILL_BATCH)]
                if not batch:
                    break
                vectors = self._try_embed([content for _, content in batch], TASK_DOCUMENT)
                if vectors is None:
                    break
                with self._lock:
                    stored = 0
                    before = [dict(item) for item in self._items]
                    for (item_id, content), vector in zip(batch, vectors):
                        for item in self._items:
                            # O conteúdo pode ter mudado durante a chamada: aí o vetor não serve.
                            if item["id"] == item_id and item["content"] == content and not item["embedding"]:
                                item["embedding"] = vector
                                stored += 1
                    if stored:
                        self._commit(before)
                done += stored
                if stored < len(batch):
                    break
            return done
        finally:
            self._backfill_lock.release()

    def cleanup_done(self, days):
        cutoff = parse_time(done_cutoff(days))
        with self._lock:
            old = [
                item for item in self._load()
                if item["status"] == DONE and not item.get("pinned") and (parse_time(item.get("done_at")) or cutoff) < cutoff
            ]
            if old:
                self.remove_many([item["id"] for item in old])
        return len(old)

    def snapshot(self):
        with self._lock:
            return [dict(item) for item in self._load()]

    # ---------- cópia local da nuvem ----------

    def put_many(self, items: list[dict]) -> None:
        """Grava os itens como vieram (novo ou substitui pelo id), numa gravação só."""
        if not items:
            return
        with self._lock:
            before = list(self._load())
            incoming = {item["id"]: dict(item) for item in items}
            merged = [incoming.pop(item["id"], item) for item in before]
            self._items = merged + list(incoming.values())
            self._commit(before)

    def put(self, item: dict) -> None:
        self.put_many([item])

    def remove_many(self, ids) -> list[dict]:
        ids = set(ids)
        with self._lock:
            before = list(self._load())
            removed = [item for item in before if item["id"] in ids]
            if removed:
                self._items = [item for item in before if item["id"] not in ids]
                self._commit(before)
            return removed

    def find_request(self, request_id: str) -> Optional[dict]:
        with self._lock:
            for item in self._load():
                if item.get("request_id") == request_id:
                    return dict(item)
        return None

    def _replace(self, old: dict, new: dict) -> None:
        before = list(self._items)
        self._items = [new if item is old else item for item in before]
        self._commit(before)


def _public(item: dict) -> dict:
    """Item sem o vetor (o que sai do store para as ferramentas)."""
    return {key: value for key, value in item.items() if key != "embedding"}
