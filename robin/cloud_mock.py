"""Supabase simulado: o pedaço do PostgREST que o SupabaseStore e o ConversationCloud usam, em memória,
atrás de um httpx.MockTransport. Serve ao modo simulação (ROBIN_MOCK=1, que nunca acessa o Supabase
real) e aos testes (o mesmo HTTP do cliente real, só sem rede).

Suporta: GET/POST/PATCH/DELETE em /rest/v1/memories, /conversations e /conversation_messages com os
filtros eq, neq, lt, gt, is, in, cs, or e wfts(portuguese); select de colunas; order/limit/offset;
insert com on_conflict (request_id nas memórias, id nas conversas) e resolution=ignore-duplicates;
RPC match_memories. O embedding volta como texto, como o pgvector. Nas tabelas das conversas o
updated_at é do servidor (como o trigger de scripts/supabase_conversas.sql) e a mensagem precisa da
conversa (chave estrangeira). conversation_tables=False simula o SQL das conversas ainda não rodado.
Falhas: offline (ConnectError), paused (HTTP 540) e chave recusada (401).
"""

import json
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from .brain import cosine, normalize, now_iso, parse_time, text_score

_RESERVED = {"select", "order", "limit", "offset", "on_conflict"}
VALID_KEY_PREFIX = "sb_secret_"
MEMORIES = "memories"
CONVERSATION_TABLES = ("conversations", "conversation_messages")


class FakeSupabase:
    def __init__(self, path: Optional[Path] = None):
        self.tables: dict[str, dict[str, dict]] = {name: {} for name in (MEMORIES, *CONVERSATION_TABLES)}
        self.conversation_tables = True
        self.path = Path(path) if path else None
        self.offline_until = 0.0
        self.paused = False
        self.log: list[tuple[str, str]] = []  # (método, caminho) de cada pedido
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                # Formato antigo: só a lista das memórias.
                for name, rows in ({MEMORIES: data} if isinstance(data, list) else data).items():
                    self.tables[name] = {row["id"]: row for row in rows}
            except (OSError, ValueError, AttributeError, KeyError, TypeError):
                self.tables = {name: {} for name in self.tables}

    @property
    def rows(self) -> dict[str, dict]:
        """As linhas da tabela memories."""
        return self.tables[MEMORIES]

    # ---------- controle (testes e gatilhos da simulação) ----------

    def go_offline(self, seconds: float = 30.0) -> None:
        self.offline_until = time.monotonic() + seconds

    def go_online(self) -> None:
        self.offline_until = 0.0

    @property
    def offline(self) -> bool:
        return time.monotonic() < self.offline_until

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, method: Optional[str] = None) -> int:
        return sum(1 for logged, _ in self.log if method is None or logged == method)

    # ---------- HTTP ----------

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.log.append((request.method, request.url.path))
            if self.offline:
                raise httpx.ConnectError("simulação: sem internet", request=request)
            if self.paused:
                return httpx.Response(540, json={"message": "Project paused (simulação)"})
            if not request.headers.get("apikey", "").startswith(VALID_KEY_PREFIX) or request.headers.get("authorization"):
                return httpx.Response(401, json={"message": "Invalid API key (simulação)"})
            path = request.url.path.removeprefix("/rest/v1")
            params = list(request.url.params.multi_items())
            body = json.loads(request.content) if request.content else None
            if path == "/rpc/match_memories" and request.method == "POST":
                return httpx.Response(200, json=self._match(body))
            table = path.removeprefix("/")
            if table not in self.tables or (table in CONVERSATION_TABLES and not self.conversation_tables):
                return httpx.Response(404, json={"code": "PGRST205", "message": f"{path} não existe (simulação)"})
            if request.method == "GET":
                return httpx.Response(200, json=self._select(table, params))
            if request.method == "POST":
                return self._insert(table, body, request.headers.get("prefer", ""))
            if request.method == "PATCH":
                return self._update(table, params, body)
            if request.method == "DELETE":
                return self._delete(table, params)
            return httpx.Response(405, json={"message": "método não suportado"})

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {name: list(rows.values()) for name, rows in self.tables.items()}
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _out(row: dict, columns: Optional[list[str]] = None) -> dict:
        result = dict(row)
        if result.get("embedding") is not None:
            result["embedding"] = json.dumps(result["embedding"])
        return {key: result.get(key) for key in columns} if columns else result

    def _matching(self, table: str, params: list[tuple[str, str]]) -> list[dict]:
        rows = list(self.tables[table].values())
        for key, value in params:
            if key in _RESERVED:
                continue
            if key == "or":
                options = [part.split(".", 2) for part in value.strip("()").split(",")]
                rows = [row for row in rows if any(_test(row, col, op, val) for col, op, val in options)]
            elif value.startswith("wfts("):
                query = value.split(").", 1)[1]
                rows = [row for row in rows if text_score(query, {"content": row["content"], "tags": row.get("tags") or []}) == 1.0]
            else:
                op, _, val = value.partition(".")
                rows = [row for row in rows if _test(row, key, op, val)]
        return rows

    def _select(self, table: str, params: list[tuple[str, str]]) -> list[dict]:
        args = dict(params)
        rows = self._matching(table, params)
        if args.get("order"):
            rows.sort(key=lambda row: str(row.get(args["order"])))
        offset = int(args.get("offset") or 0)
        rows = rows[offset:]
        if args.get("limit"):
            rows = rows[: int(args["limit"])]
        columns = args.get("select", "*")
        return [self._out(row, None if columns == "*" else columns.split(",")) for row in rows]

    def _insert(self, table: str, rows: list[dict], prefer: str) -> httpx.Response:
        existing = self.tables[table]
        created = []
        for row in rows:
            duplicate = (row["id"] in existing if table != MEMORIES
                         else any(other.get("request_id") == row.get("request_id") for other in existing.values()))
            if duplicate:
                if "ignore-duplicates" in prefer:
                    continue
                return httpx.Response(409, json={"code": "23505", "message": "chave duplicada"})
            if row["id"] in existing:
                return httpx.Response(409, json={"code": "23505", "message": "id duplicado"})
            if table == "conversation_messages" and row.get("conversation_id") not in self.tables["conversations"]:
                return httpx.Response(409, json={"code": "23503", "message": "conversa inexistente (chave estrangeira)"})
            stamp = now_iso()
            saved = {**row, "created_at": row.get("created_at") or stamp, "updated_at": row.get("updated_at") or stamp}
            if table != MEMORIES:
                saved.update(updated_at=stamp, deleted=bool(row.get("deleted")))  # o trigger: hora do servidor
            created.append(saved)
        for saved in created:
            existing[saved["id"]] = saved
        self._save()
        return httpx.Response(201, json=[self._out(row) for row in created])

    def _update(self, table: str, params, patch: dict) -> httpx.Response:
        rows = self._matching(table, params)
        for row in rows:
            row.update(patch)
            if table != MEMORIES:
                row["updated_at"] = now_iso()
        self._save()
        return httpx.Response(200, json=[self._out(row) for row in rows])

    def _delete(self, table: str, params) -> httpx.Response:
        rows = self._matching(table, params)
        for row in rows:
            del self.tables[table][row["id"]]
        self._save()
        return httpx.Response(200, json=[self._out(row) for row in rows])

    def _match(self, body: dict) -> list[dict]:
        query = body["query_embedding"]
        rows = [
            row for row in self.rows.values()
            if row.get("embedding") and (body.get("include_done") or row.get("status") != "done")
        ]
        scored = sorted(((row, cosine(query, row["embedding"])) for row in rows), key=lambda pair: pair[1], reverse=True)
        keys = ("id", "content", "tags", "status", "pinned", "created_at", "done_at")
        return [{**{key: row.get(key) for key in keys}, "similarity": score} for row, score in scored[: body["match_count"]]]


def _test(row: dict, column: str, op: str, value: str) -> bool:
    current = row.get(column)
    if op == "is":
        return {"null": current is None, "true": current is True, "false": current is False}[value]
    if op == "in":
        return str(current) in value.strip("()").split(",")
    if op == "cs":  # array contém: tags=cs.{a,b}
        return set(value.strip("{}").split(",")) <= set(current or [])
    if current is None:
        return False
    if column.endswith("_at"):
        left, right = parse_time(current), parse_time(value)
        return {"eq": left == right, "neq": left != right, "lt": left < right, "gt": left > right}[op]
    if isinstance(current, bool):
        current = "true" if current else "false"
    return {"eq": str(current) == value, "neq": str(current) != value,
            "lt": str(current) < value, "gt": str(current) > value}[op]


# Nuvem do modo simulação (uma por processo, gravada na pasta mock\ para sobreviver a reabrir).
_SIMULATED: Optional[FakeSupabase] = None


def simulated_cloud(path: Optional[Path] = None) -> FakeSupabase:
    global _SIMULATED
    if _SIMULATED is None:
        _SIMULATED = FakeSupabase(path)
    return _SIMULATED


def apply_trigger(text: str) -> None:
    """Gatilhos da simulação pela mensagem: "sem internet" (30s offline), "pausar banco" e "reativar banco"."""
    if _SIMULATED is None:
        return
    key = normalize(text)
    if "sem internet" in key:
        _SIMULATED.go_offline(30.0)
    if "pausar banco" in key:
        _SIMULATED.paused = True
    if "reativar banco" in key:
        _SIMULATED.paused = False
