"""Cérebro no Supabase: cliente REST (PostgREST + RPC, só httpx) e SupabaseStore.

Modelo de sincronização (o usuário nunca usa duas máquinas ao mesmo tempo):
- leitura vem da cópia local (LocalStore em brain.json), que também é o backup;
- toda alteração grava na nuvem na hora e na cópia local. Sem conexão (ou 5xx), a alteração vai
  para a cópia local e para a fila de pendências (sync.json) e o item volta com "sync": "pending";
  o reenvio usa o mesmo request_id/id, então não duplica;
- versão velha: update e delete levam o filtro updated_at=eq.<versão local>. Se a nuvem tiver
  outra versão, nada muda, a cópia local é recarregada e sai StaleError;
- pull (ao abrir e quando a janela aparece): reenvia a fila, migra o brain.json uma vez, puxa só
  o que mudou (índice id + updated_at) e remove da cópia o que sumiu da nuvem (com cópia em
  brain.lixeira.json);
- banco pausado (HTTP 540) ou chave recusada: só leitura, com a mensagem na interface.
"""

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from . import timing
from .brain import (
    DONE,
    PENDING,
    STATUSES,
    TASK_QUERY,
    BrainError,
    LocalStore,
    MemoryStore,
    StaleError,
    _public,
    clean_tags,
    done_cutoff,
    machine_name,
    now_iso,
    text_score,
)
from .config import CORE_REQUEST_ID, HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT

logger = logging.getLogger("robin.supabase")

TABLE = "memories"
COLUMNS = "id,content,tags,status,pinned,embedding,request_id,created_at,updated_at,done_at,updated_by"
PAGE_SIZE = 1000  # limite padrão de linhas por resposta do PostgREST no Supabase
FETCH_CHUNK = 100  # ids por pedido no filtro in.(...)
_REPLACE_ATTEMPTS = 5

# Estados para a interface (onCloudStatus).
OK, OFFLINE, PAUSED, ERROR = "ok", "offline", "paused", "error"

MESSAGES = {
    "offline": "Sem conexão com o Supabase.",
    "server": "O Supabase falhou (erro do servidor).",
    "paused": "Banco pausado: reative no painel do Supabase. Até lá, a memória fica só para leitura.",
    "restricted": "Projeto do Supabase com restrição (veja o painel). A memória fica só para leitura.",
    "auth": "Chave do Supabase recusada: use a chave secreta (sb_secret_...) do projeto.",
    "schema": ("Tabela memories ou função match_memories não encontrada no Supabase: rode no SQL Editor "
               "o scripts/supabase_memorias.sql e depois o scripts/supabase_conversas.sql (veja o README)."),
    "bad_request": "O Supabase recusou o pedido.",
}


class CloudError(Exception):
    """Falha da nuvem já classificada. kind: offline, server, paused, restricted, auth, schema,
    bad_request, conflict."""

    def __init__(self, kind: str, detail: str = "", status: Optional[int] = None):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.status = status

    @property
    def retryable(self) -> bool:
        """Vale guardar na fila e reenviar depois."""
        return self.kind in ("offline", "server")

    @property
    def message(self) -> str:
        return MESSAGES.get(self.kind, MESSAGES["bad_request"])


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    for suffix in ("/rest/v1",):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def _classify(response: httpx.Response) -> CloudError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    code = str(body.get("code") or "")
    detail = str(body.get("message") or body.get("error") or response.text or "")[:300]
    status = response.status_code
    if status == 540 or "paused" in detail.lower():
        return CloudError("paused", detail, status)
    if status == 402:
        return CloudError("restricted", detail, status)
    if status in (401, 403):
        return CloudError("auth", detail, status)
    if status == 404 or code in ("PGRST202", "PGRST205", "42P01", "42883"):
        return CloudError("schema", detail, status)
    if status == 409 or code == "23505":
        return CloudError("conflict", detail, status)
    if status >= 500:
        return CloudError("server", detail, status)
    return CloudError("bad_request", detail, status)


class SupabaseClient:
    """O mínimo do PostgREST para a tabela memories e a RPC match_memories.

    A chave secreta vai só no cabeçalho apikey: no Authorization: Bearer o Supabase tenta lê-la como
    JWT e recusa. Timeouts iguais aos do Gemini (conexão 10s, leitura 60s)."""

    def __init__(self, url: str, key: str, transport: Optional[httpx.BaseTransport] = None):
        self.url = normalize_url(url)
        self._http = httpx.Client(
            base_url=f"{self.url}/rest/v1",
            headers={"apikey": key, "Accept": "application/json", "Content-Type": "application/json"},
            timeout=httpx.Timeout(HTTP_READ_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
            transport=transport,
        )
        self.requests = 0  # pedidos feitos (diagnóstico e testes)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, params: Optional[dict] = None, body: Any = None,
                 prefer: Optional[str] = None) -> Any:
        self.requests += 1
        headers = {"Prefer": prefer} if prefer else None
        start = time.monotonic()
        try:
            response = self._http.request(method, path, params=params, json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            timing.log("supabase %s %s: falhou em %dms (%s)", method, path, timing.ms(start), type(error).__name__)
            raise CloudError("offline", type(error).__name__) from error
        timing.log("supabase %s %s: HTTP %d em %dms", method, path, response.status_code, timing.ms(start))
        if response.status_code >= 400:
            raise _classify(response)
        if not response.content:
            return None
        return response.json()

    def select(self, params: dict, table: str = TABLE) -> list[dict]:
        return self._request("GET", f"/{table}", params=params) or []

    def select_all(self, params: dict, table: str = TABLE, order: str = "id") -> list[dict]:
        """Todas as linhas, paginando (o PostgREST do Supabase devolve no máximo 1000 por vez)."""
        rows: list[dict] = []
        while True:
            page = self.select({**params, "order": order, "limit": PAGE_SIZE, "offset": len(rows)}, table)
            rows += page
            if len(page) < PAGE_SIZE:
                return rows

    def insert(self, rows: list[dict]) -> list[dict]:
        """Insere ignorando request_id já gravado: devolve só as linhas novas."""
        return self._request(
            "POST", f"/{TABLE}", params={"on_conflict": "request_id"}, body=rows,
            prefer="return=representation,resolution=ignore-duplicates",
        ) or []

    def insert_new(self, table: str, rows: list[dict]) -> None:
        """Insere ignorando id já gravado: reenviar não duplica nem sobrescreve (conversas)."""
        self._request("POST", f"/{table}", params={"on_conflict": "id"}, body=rows,
                      prefer="return=minimal,resolution=ignore-duplicates")

    def update(self, filters: dict, patch: dict, table: str = TABLE) -> list[dict]:
        return self._request("PATCH", f"/{table}", params=filters, body=patch, prefer="return=representation") or []

    def delete(self, filters: dict, table: str = TABLE) -> list[dict]:
        return self._request("DELETE", f"/{table}", params=filters, prefer="return=representation") or []

    def rpc(self, name: str, args: dict) -> list[dict]:
        return self._request("POST", f"/rpc/{name}", body=args) or []

    def check(self) -> None:
        """Valida URL, chave, tabela e RPC (dois pedidos). Levanta CloudError."""
        self.select({"select": "id", "limit": 1})
        self.rpc("match_memories", {"query_embedding": [0.0] * 768, "match_count": 1, "include_done": False})


# ---------- linhas <-> itens ----------

def from_row(row: dict) -> dict:
    embedding = row.get("embedding")
    if isinstance(embedding, str):  # o pgvector volta como texto "[0.1,0.2,...]"
        embedding = json.loads(embedding) if embedding.strip() else None
    return {
        "id": row["id"],
        "content": row.get("content") or "",
        "tags": list(row.get("tags") or []),
        "status": row.get("status"),
        "pinned": bool(row.get("pinned")),
        "embedding": embedding,
        "request_id": row.get("request_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "done_at": row.get("done_at"),
        "updated_by": row.get("updated_by"),
    }


def to_row(item: dict) -> dict:
    return {key: item.get(key) for key in COLUMNS.split(",")}


def _eq(value: str) -> str:
    return f"eq.{value}"


# ---------- fila de pendências ----------

class SyncState:
    """sync.json: fila de alterações ainda não enviadas e a marca da migração do brain.json."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data: Optional[dict] = None

    def _load(self) -> dict:
        if self._data is None:
            data = {"queue": [], "migrated": []}
            if self._path.exists():
                try:
                    loaded = json.loads(self._path.read_text(encoding="utf-8"))
                    data.update({key: list(loaded.get(key) or []) for key in data})
                except (OSError, ValueError, AttributeError) as error:
                    logger.error("sync.json ilegível (%s); a fila começa vazia", error)
            self._data = data
        return self._data

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(f".sync-{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                temp.replace(self._path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    temp.unlink(missing_ok=True)
                    raise
                time.sleep(0.05 * (attempt + 1))

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


# ---------- o store ----------

class SupabaseStore(MemoryStore):
    def __init__(self, local: LocalStore, client: SupabaseClient, sync_file: Path,
                 on_status: Optional[Callable[[dict], None]] = None):
        super().__init__()
        self.local = local
        self.client = client
        self.state = SyncState(sync_file)
        self.on_status = on_status
        self._lock = threading.RLock()  # escrita, reenvio e pull, um de cada vez
        self._status = {"state": OK, "message": "", "pending": len(self.state.queue)}
        self._conflicts: list[str] = []

    # O embed vale também para a cópia local (busca offline por significado).
    @property
    def embed(self):
        return self.__dict__.get("_embed")

    @embed.setter
    def embed(self, value):
        self.__dict__["_embed"] = value
        if "local" in self.__dict__:
            self.local.embed = value

    # ---------- estado para a interface ----------

    def status(self) -> dict:
        return {**self._status, "pending": len(self.state.queue), "url": self.client.url}

    def _set_status(self, state: str, message: str = "") -> None:
        changed = (state, message) != (self._status["state"], self._status["message"])
        self._status = {"state": state, "message": message, "pending": len(self.state.queue)}
        if changed:
            log = logger.info if state == OK else logger.warning
            log("Supabase: %s %s", state, message)
        if self.on_status:
            try:
                self.on_status(self.status())
            except Exception:  # noqa: BLE001 - a interface pode estar fechando
                logger.debug("on_status falhou", exc_info=True)

    def _cloud_failed(self, error: CloudError) -> None:
        state = {"paused": PAUSED, "restricted": PAUSED, "offline": OFFLINE, "server": OFFLINE}.get(error.kind, ERROR)
        self._set_status(state, error.message)

    def take_conflicts(self) -> list[str]:
        """Avisos de alterações da fila descartadas porque o item mudou em outra máquina."""
        conflicts, self._conflicts = self._conflicts, []
        return conflicts

    def _writable(self) -> None:
        if self._status["state"] == PAUSED:
            raise BrainError(self._status["message"] or MESSAGES["paused"])

    # ---------- leitura (cópia local) ----------

    def get(self, item_id):
        return self.local.get(item_id)

    def list_tasks(self, status=PENDING):
        return self.local.list_tasks(status)

    def snapshot(self):
        return self.local.snapshot()

    def search(self, query, include_done=False, limit=8):
        if not (query or "").strip():
            return [], "texto"
        limit = max(1, int(limit))
        vectors = self._try_embed([query], TASK_QUERY)
        try:
            if vectors is not None:
                rows = self.client.rpc("match_memories", {
                    "query_embedding": vectors[0], "match_count": limit, "include_done": bool(include_done),
                })
                found = [(self._merged(row), float(row.get("similarity") or 0)) for row in rows]
                # Itens ainda sem embedding na nuvem entram pela busca por texto na cópia local.
                seen = {item["id"] for item, _ in found}
                found += [
                    (_public(item), text_score(query, item)) for item in self.local.snapshot()
                    if not item.get("embedding") and item["id"] not in seen
                    and (include_done or item["status"] != DONE) and text_score(query, item) > 0
                ]
                found.sort(key=lambda pair: pair[1], reverse=True)
                self._mark_ok()
                return [(item, round(score, 3)) for item, score in found[:limit]], "semantica"
            params = {"select": COLUMNS, "fts": f"wfts(portuguese).{query}", "limit": limit}
            if not include_done:
                params["or"] = "(status.is.null,status.neq.done)"
            rows = self.client.select(params)
            self._mark_ok()
            return [(_public(from_row(row)), 1.0) for row in rows], "texto"
        except CloudError as error:
            self._cloud_failed(error)
            return self.local.search(query, include_done, limit)  # cópia local (só leitura)

    def _merged(self, row: dict) -> dict:
        local = self.local.get(row["id"])
        return _public(local) if local else _public(from_row({**row, "embedding": None}))

    def _mark_ok(self) -> None:
        if self._status["state"] != OK:
            self._set_status(OK)

    # ---------- escrita ----------

    def add(self, content, tags, status=None, request_id=None, pinned=False, embedding=None):
        content = (content or "").strip()
        if not content:
            raise BrainError("Conteúdo vazio.")
        if status not in STATUSES:
            raise BrainError(f"Status inválido: {status}")
        with timing.wait_lock(self._lock, "da nuvem"):
            self._writable()
            if request_id:
                existing = self.local.find_request(request_id)
                if existing:
                    return {**existing, "sync": "pending" if self._has_pending(existing["id"]) else "cloud"}
            stamp = now_iso()
            item_id = str(uuid.uuid4())
            item = {
                "id": item_id, "content": content, "tags": clean_tags(tags), "status": status,
                "pinned": bool(pinned), "embedding": embedding,
                "request_id": request_id or f"robin:{item_id}",
                "created_at": stamp, "updated_at": stamp, "done_at": None, "updated_by": machine_name(),
            }
            if not self._queue_clear():  # há pendências antes desta: mantém a ordem
                return self._queue_insert(item)
            try:
                saved = self._insert_now(item)
            except CloudError as error:
                if not error.retryable:
                    self._cloud_failed(error)
                    raise BrainError(error.message) from error
                return self._queue_insert(item, error)
            self.local.put(saved)
            self._mark_ok()
            return {**saved, "sync": "cloud"}

    def _insert_now(self, item: dict) -> dict:
        rows = self.client.insert([to_row(item)])
        if rows:
            return from_row(rows[0])
        # request_id já estava na nuvem (reenvio): vale a linha que está lá.
        existing = self.client.select({"select": COLUMNS, "request_id": _eq(item["request_id"])})
        if not existing:
            raise CloudError("bad_request", "insert ignorado sem linha correspondente")
        return from_row(existing[0])

    def _queue_insert(self, item: dict, error: Optional[CloudError] = None) -> dict:
        self.local.put(item)
        self.state.push({"op": "insert", "id": item["id"], "row": to_row(item)})
        self._set_status(OFFLINE, error.message if error else MESSAGES["offline"])
        return {**item, "sync": "pending"}

    def update(self, item_id, content=None, tags=None):
        with timing.wait_lock(self._lock, "da nuvem"):
            item = self._existing(item_id)
            patch: dict[str, Any] = {}
            if content is not None and content.strip() and content.strip() != item["content"]:
                patch["content"] = content.strip()
                patch["embedding"] = None
            if tags is not None and clean_tags(tags) != item["tags"]:
                patch["tags"] = clean_tags(tags)
            if not patch:
                return {**item, "sync": "cloud"}
            return self._patch(item, patch)

    def complete(self, item_id):
        with timing.wait_lock(self._lock, "da nuvem"):
            item = self._existing(item_id)
            if item["status"] == DONE:
                return {**item, "sync": "cloud"}
            if item["status"] != PENDING:
                raise BrainError("Este item não é uma tarefa.")
            return self._patch(item, {"status": DONE, "done_at": now_iso()})

    def delete(self, item_id):
        with timing.wait_lock(self._lock, "da nuvem"):
            self._writable()
            item = self._existing(item_id)
            if not self._queue_clear():
                return self._queue_delete(item)
            try:
                rows = self.client.delete({"id": _eq(item_id), "updated_at": _eq(item["updated_at"])})
            except CloudError as error:
                if not error.retryable:
                    self._cloud_failed(error)
                    raise BrainError(error.message) from error
                return self._queue_delete(item, error)
            if not rows:
                self._reload_after_miss(item)  # levanta StaleError ou BrainError
            self.local.remove_many([item_id])
            self._mark_ok()
            return {**item, "sync": "cloud"}

    def _queue_delete(self, item: dict, error: Optional[CloudError] = None) -> dict:
        self.local.remove_many([item["id"]])
        self.state.push({"op": "delete", "id": item["id"], "base": item["updated_at"]})
        self._set_status(OFFLINE, error.message if error else MESSAGES["offline"])
        return {**item, "sync": "pending"}

    def _existing(self, item_id: str) -> dict:
        item = self.local.get(item_id)
        if item is None:
            raise BrainError(f"Item {item_id} não encontrado.")
        return item

    def _queue_clear(self) -> bool:
        """Antes de uma alteração nova: tenta esvaziar a fila (a ordem das alterações é mantida)."""
        if self.state.queue:
            self.flush()
        return not self.state.queue

    def _has_pending(self, item_id: str) -> bool:
        return any(op["id"] == item_id for op in self.state.queue)

    def _patch(self, item: dict, patch: dict) -> dict:
        """Altera com a proteção de versão: a nuvem só aceita se ainda estiver na versão local."""
        self._writable()
        patch = {**patch, "updated_at": now_iso(), "updated_by": machine_name()}
        if not self._queue_clear():
            return self._queue_patch(item, patch)
        item = self.local.get(item["id"]) or item  # o reenvio pode ter trazido outra versão
        try:
            rows = self.client.update({"id": _eq(item["id"]), "updated_at": _eq(item["updated_at"])}, patch)
        except CloudError as error:
            if not error.retryable:
                self._cloud_failed(error)
                raise BrainError(error.message) from error
            return self._queue_patch(item, patch, error)
        if not rows:
            self._reload_after_miss(item)
        saved = from_row(rows[0])
        self.local.put(saved)
        self._mark_ok()
        return {**saved, "sync": "cloud"}

    def _queue_patch(self, item: dict, patch: dict, error: Optional[CloudError] = None) -> dict:
        updated = {**item, **patch}
        self.local.put(updated)
        self.state.push({"op": "update", "id": item["id"], "base": item["updated_at"], "patch": patch})
        self._set_status(OFFLINE, error.message if error else MESSAGES["offline"])
        return {**updated, "sync": "pending"}

    def _reload_after_miss(self, item: dict) -> None:
        """O filtro de versão não casou: o item mudou ou foi apagado em outra máquina."""
        rows = self.client.select({"select": COLUMNS, "id": _eq(item["id"])})
        if not rows:
            self.local.remove_many([item["id"]])
            raise BrainError("Este item foi apagado em outra máquina.")
        current = from_row(rows[0])
        self.local.put(current)
        raise StaleError(_public(current))

    # ---------- fila ----------

    def flush(self) -> int:
        """Reenvia a fila em ordem. Para no primeiro erro de rede (o resto fica). Devolve quantas
        foram enviadas. Alteração cujo item mudou na nuvem é descartada (vale a nuvem) e avisada."""
        sent = 0
        with self._lock:
            queue = self.state.queue
            while queue:
                op = queue[0]
                try:
                    new_base = self._replay(op)
                except CloudError as error:
                    self.state.set_queue(queue)
                    self._cloud_failed(error)
                    return sent
                queue.pop(0)
                if new_base:  # as próximas alterações do mesmo item partem da versão gravada
                    for later in queue:
                        if later["id"] == op["id"] and "base" in later:
                            later["base"] = new_base
                            break
                self.state.set_queue(queue)
                sent += 1
        if sent:
            logger.info("Supabase: %d alteração(ões) da fila enviada(s)", sent)
        return sent

    def _replay(self, op: dict) -> Optional[str]:
        if op["op"] == "insert":
            saved = self._insert_now(from_row(op["row"]))
            self.local.put(saved)
            return saved["updated_at"]
        filters = {"id": _eq(op["id"]), "updated_at": _eq(op["base"])}
        if op["op"] == "update":
            rows = self.client.update(filters, op["patch"])
        else:
            rows = self.client.delete(filters)
        if rows:
            if op["op"] == "update":
                saved = from_row(rows[0])
                self.local.put(saved)
                return saved["updated_at"]
            return None
        current = self.client.select({"select": COLUMNS, "id": _eq(op["id"])})
        if current:
            fresh = from_row(current[0])
            self.local.put(fresh)
            message = f"Alteração descartada: \"{fresh['content'][:60]}\" mudou em outra máquina."
            logger.warning("Supabase: %s", message)
            self._conflicts.append(message)
        elif op["op"] == "update":
            self.local.remove_many([op["id"]])
        return None

    # ---------- pull, migração, núcleo e limpeza ----------

    def pull(self, core_seed: Optional[Callable[[], str]] = None, cleanup_days: Optional[int] = None) -> dict:
        """Reenvia a fila, migra o brain.json (uma vez por URL), limpa as concluídas antigas, puxa o
        que mudou e garante o núcleo. Devolve um resumo; com a nuvem fora, {"ok": False}."""
        with timing.wait_lock(self._lock, "da nuvem"):
            self.flush()
            if self.state.queue:
                return {"ok": False, "state": self._status["state"]}
            try:
                if not self.state.migrated(self.client.url):
                    self._migrate()
                if cleanup_days:
                    self._cleanup_cloud(cleanup_days)
                summary = self._pull_rows()
                if core_seed is not None and not any(item.get("request_id") == CORE_REQUEST_ID for item in self.local.snapshot()):
                    self._seed_core(core_seed())
                    summary["core_seeded"] = True
            except CloudError as error:
                self._cloud_failed(error)
                return {"ok": False, "state": self._status["state"]}
            self._set_status(OK)
            return {"ok": True, **summary}

    def _pull_rows(self) -> dict:
        index = {row["id"]: row["updated_at"] for row in self.client.select_all({"select": "id,updated_at"})}
        local = {item["id"]: item for item in self.local.snapshot()}
        gone = [item_id for item_id in local if item_id not in index]
        if gone:
            removed = self.local.remove_many(gone)
            self._to_trash(removed)
        wanted = [
            item_id for item_id, stamp in index.items()
            if item_id not in local or local[item_id]["updated_at"] != stamp
        ]
        fetched = []
        for start in range(0, len(wanted), FETCH_CHUNK):
            chunk = wanted[start:start + FETCH_CHUNK]
            fetched += [from_row(row) for row in self.client.select({"select": COLUMNS, "id": f"in.({','.join(chunk)})"})]
        changed = [item for item in fetched if local.get(item["id"]) != item]
        self.local.put_many(changed)
        return {"changed": len(changed), "removed": len(gone)}

    def _to_trash(self, items: list[dict]) -> None:
        """Itens apagados na nuvem saem da cópia local, mas ficam em brain.lixeira.json (backup)."""
        trash = self.local.path.with_name("brain.lixeira.json")
        try:
            old = json.loads(trash.read_text(encoding="utf-8")) if trash.exists() else []
        except (OSError, ValueError):
            old = []
        stamp = now_iso()
        trash.write_text(
            json.dumps([*old, *({**item, "removido_em": stamp} for item in items)], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        logger.info("Supabase: %d item(ns) apagado(s) na nuvem removido(s) da cópia local (cópia em %s)", len(items), trash.name)

    def _migrate(self) -> None:
        """Sobe uma vez os itens do brain.json que a nuvem ainda não tem (request_id evita duplicar)."""
        items = self.local.snapshot()
        rows = []
        for item in items:
            if not item.get("request_id"):
                item["request_id"] = f"robin:{item['id']}"
            rows.append(to_row(item))
        inserted = self.client.insert(rows) if rows else []
        self.state.mark_migrated(self.client.url)
        logger.info("Supabase: migração do brain.json: %d item(ns) local(is), %d enviado(s) (o resto já estava lá)",
                    len(items), len(inserted))

    def _seed_core(self, content: str) -> None:
        """Primeira conexão sem núcleo na nuvem: o memory.md sobe como item fixo."""
        content = (content or "").strip() or "(núcleo vazio)"
        stamp = now_iso()
        item = {
            "id": str(uuid.uuid4()), "content": content, "tags": ["núcleo"], "status": None, "pinned": True,
            "embedding": None, "request_id": CORE_REQUEST_ID, "created_at": stamp, "updated_at": stamp,
            "done_at": None, "updated_by": machine_name(),
        }
        self.local.put(self._insert_now(item))
        logger.info("Supabase: núcleo criado na nuvem a partir do memory.md")

    def _cleanup_cloud(self, days: int) -> int:
        rows = self.client.delete({"status": "eq.done", "done_at": f"lt.{done_cutoff(days)}", "pinned": "is.false"})
        if rows:
            self.local.remove_many([row["id"] for row in rows])
            logger.info("Supabase: %d tarefa(s) concluída(s) há mais de %d dias apagada(s)", len(rows), days)
        return len(rows)

    def cleanup_done(self, days):
        with self._lock:
            try:
                return self._cleanup_cloud(days)
            except CloudError as error:
                self._cloud_failed(error)
                return 0

    # ---------- núcleo pelo editor ----------

    def core_item(self) -> Optional[dict]:
        return self.local.find_request(CORE_REQUEST_ID)

    def save_core(self, content: str) -> dict:
        """O editor da lateral: só o usuário altera o núcleo."""
        with self._lock:
            item = self.core_item()
            if item is None:
                return self.add(content, ["núcleo"], None, request_id=CORE_REQUEST_ID, pinned=True)
            if content == item["content"]:
                return {**item, "sync": "cloud"}
            # O núcleo vai inteiro no system_instruction: não precisa de embedding.
            return self._patch(item, {"content": content})

    # ---------- embeddings ----------

    def backfill_embeddings(self, limit=None):
        if not self._backfill_lock.acquire(blocking=False):
            return 0
        try:
            missing = [
                item for item in self.local.snapshot()
                if not item.get("embedding") and not item.get("pinned") and not self._has_pending(item["id"])
            ][: limit or None]
            if not missing:
                return 0
            vectors = self._try_embed([item["content"] for item in missing], "RETRIEVAL_DOCUMENT")
            if vectors is None:
                return 0
            done = 0
            with self._lock:
                for item, vector in zip(missing, vectors):
                    try:
                        # Nova versão (updated_at): as outras máquinas recebem o vetor no próximo pull.
                        rows = self.client.update({"id": _eq(item["id"]), "updated_at": _eq(item["updated_at"])},
                                                  {"embedding": vector, "updated_at": now_iso()})
                    except CloudError as error:
                        self._cloud_failed(error)
                        break
                    if rows:
                        self.local.put(from_row(rows[0]))
                        done += 1
            return done
        finally:
            self._backfill_lock.release()
