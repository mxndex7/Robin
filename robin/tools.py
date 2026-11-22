"""Ferramentas do cérebro (function calling): declarações para o Gemini, execução sobre o MemoryStore
e a memória como o modelo a vê no system_instruction (memory_for_prompt, com ids curtos).

Idempotência: toda ação que altera a memória recebe um request_id derivado do turno do usuário,
da ferramenta e dos argumentos normalizados. Dentro do turno (novas tentativas, watchdog,
fallback, regenerar) a mesma ação devolve o resultado guardado em Turn.actions sem rodar de novo;
o store também recusa um request_id repetido (add devolve o item existente).

Os textos que o modelo lê (descrições, erros, "tipo") evitam o vocabulário interno (nota, tarefa,
item, núcleo): ele repete o que lê, e a conversa não deve falar da engrenagem.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from google.genai import types

from .brain import DONE, PENDING, BrainError, MemoryStore, StaleError, clean_tags, normalize
from .clock import local_time
from .config import SIMILAR_THRESHOLD

logger = logging.getLogger("robin.tools")

_REQUEST_NAMESPACE = uuid.UUID("6f1c2b0e-7d0a-4f43-9a51-52b1a0f0c0de")
_LABEL_LIMIT = 60
# Id curto da memória no prompt (#3f2a9c1e): mais caracteres só quando dois ids começam igual.
SHORT_ID = 8
MUTATING = frozenset({"salvar_memoria", "atualizar_memoria", "concluir_tarefa", "apagar_memoria"})
_PINNED_REFUSAL = "Isso faz parte do que a pessoa escreveu sobre si: só ela muda, pelo editor da lateral."
_PENDING_NOTE = (
    "NÃO foi para a nuvem ainda (sem conexão com o Supabase): ficou só neste PC, na fila, e vai sozinho "
    "quando a conexão voltar. Diga isso à pessoa; não diga que salvou na nuvem."
)


@dataclass
class ToolResult:
    response: dict  # vai ao modelo (function_response)
    label: str = ""  # linha discreta na mensagem ("Salvou: comprar uva"); vazio = nada a mostrar
    ok: bool = True
    ids: list[str] = field(default_factory=list)


@dataclass
class Turn:
    """Um turno do usuário (sobrevive a novas tentativas e a regenerar)."""

    key: str
    actions: dict[str, ToolResult] = field(default_factory=dict)  # request_id -> resultado
    seen_ids: set[str] = field(default_factory=set)  # ids que as ferramentas mostraram neste turno


def declarations(search: bool = True, conversations: bool = True) -> list[types.FunctionDeclaration]:
    """As ferramentas do modelo. Com a memória inteira e os trechos das conversas no prompt, as buscas
    só são oferecidas quando algo ficou de fora (ou já foram usadas na conversa): search=False tira
    buscar_memoria e conversations=False, buscar_conversas."""
    def fn(name: str, description: str, properties: dict, required: list[str]) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=name,
            description=description,
            parameters_json_schema={"type": "object", "properties": properties, "required": required},
        )

    item_id = {"type": "string", "description": "O #código da memória (ex.: 3f2a9c1e) ou o id devolvido por uma ação."}
    tags = {
        "type": "array", "items": {"type": "string"},
        "description": "Etiquetas curtas em minúsculas escolhidas por você (ex.: compras, trabalho, saúde).",
    }
    # O flash-lite quase nunca escreve texto junto com a chamada (smoke real, nov/2026): a resposta vem
    # como argumento e, com a ação limpa, é ela que aparece, sem o 2º pedido (gemini._answered_with_action).
    reply = {
        "type": "string",
        "description": "O que você diz à pessoa sobre isto, no seu jeito e curto, como se já estivesse feito. "
                       "Com várias ações na mesma mensagem, escreva a resposta inteira só na primeira.",
    }
    tools = [
        fn(
            "buscar_memoria",
            "Busca por significado nas coisas antigas que não couberam na memória do prompt (não precisa das "
            "mesmas palavras). Use quando a memória avisar que há coisas antigas fora da lista.",
            {
                "consulta": {"type": "string", "description": "O que procurar, em linguagem natural."},
                "incluir_concluidas": {"type": "boolean", "description": "Incluir o que já foi feito. Padrão: false."},
                "limite": {"type": "integer", "description": "Máximo de resultados (1 a 20). Padrão: 8."},
            },
            ["consulta"],
        ),
        fn(
            "buscar_conversas",
            "Busca nas conversas antigas com a pessoa que não estão nos trechos do prompt, pelas palavras do "
            "assunto. Use quando a pessoa falar de algo que vocês conversaram e não está nos trechos nem na memória.",
            {
                "consulta": {"type": "string", "description": "As palavras do assunto (ex.: foto linkedin)."},
                "limite": {"type": "integer", "description": "Máximo de trechos (1 a 20). Padrão: 8."},
            },
            ["consulta"],
        ),
        fn(
            "salvar_memoria",
            "Guarda algo novo: o que a pessoa contou e vale lembrar (pessoa, data, plano, preferência, decisão, "
            "ideia) ou algo a fazer. Confere sozinha se já existe parecido e, se existir, devolve ja_existe sem "
            "gravar (aí use atualizar_memoria, ou repita com forcar=true se for outra coisa).",
            {
                "conteudo": {"type": "string", "description": "Uma frase que se explica sozinha, com datas absolutas."},
                "etiquetas": tags,
                "tarefa": {"type": "boolean", "description": "true se for algo a fazer (fica marcado até a pessoa contar que fez)."},
                "forcar": {"type": "boolean", "description": "Grava mesmo havendo algo parecido. Padrão: false."},
                "resposta": reply,
            },
            ["conteudo", "etiquetas", "tarefa", "resposta"],
        ),
        fn(
            "atualizar_memoria",
            "Muda algo que já está na memória, no lugar (sem duplicar). Informe só o que muda.",
            {"id": item_id, "conteudo": {"type": "string", "description": "O novo texto completo."}, "etiquetas": tags,
             "resposta": reply},
            ["id", "resposta"],
        ),
        fn(
            "concluir_tarefa",
            "Risca algo a fazer que a pessoa contou que fez (continua guardado, com a data em que foi feito).",
            {"id": item_id, "resposta": reply},
            ["id", "resposta"],
        ),
        fn(
            "apagar_memoria",
            "Apaga da memória. O que não é coisa a fazer só sai com confirmado=true, depois de a pessoa confirmar "
            "na conversa; sem isso, recusa e devolve o que seria apagado. Coisa feita não se apaga: risque.",
            {"id": item_id, "confirmado": {"type": "boolean", "description": "A pessoa confirmou na conversa. Padrão: false."},
             "resposta": reply},
            ["id", "resposta"],
        ),
    ]
    hidden = {name for name, offered in (("buscar_memoria", search), ("buscar_conversas", conversations)) if not offered}
    return [tool for tool in tools if tool.name not in hidden]


def request_id(turn_key: str, name: str, args: dict) -> str:
    return str(uuid.uuid5(_REQUEST_NAMESPACE, f"{turn_key}|{name}|{_normalized_args(name, args)}"))


def _normalized_args(name: str, args: dict) -> str:
    if name == "salvar_memoria":
        # Etiquetas ficam de fora: o mesmo conteúdo com outras etiquetas ainda é a mesma ação.
        key = {"conteudo": normalize(str(args.get("conteudo", ""))), "tarefa": bool(args.get("tarefa")),
               "forcar": bool(args.get("forcar"))}
    elif name == "atualizar_memoria":
        key = {
            "id": str(args.get("id", "")).strip(),
            "conteudo": normalize(str(args.get("conteudo") or "")),
            "etiquetas": sorted(normalize(tag) for tag in clean_tags(args.get("etiquetas"))) if args.get("etiquetas") is not None else None,
        }
    elif name == "apagar_memoria":
        key = {"id": str(args.get("id", "")).strip(), "confirmado": bool(args.get("confirmado"))}
    else:
        key = {"id": str(args.get("id", "")).strip()}
    return json.dumps(key, sort_keys=True, ensure_ascii=False)


class MemoryTools:
    """conversations: as conversas guardadas (ConversationRecorder) para buscar_conversas; None sem elas."""

    def __init__(self, store: MemoryStore, conversations: Any = None):
        self.store = store
        self.conversations = conversations

    def call(self, name: str, args: Optional[dict], turn: Turn, known_ids: set[str]) -> ToolResult:
        """Executa uma chamada do modelo. known_ids: ids mostrados em turnos anteriores (apagar)."""
        args = dict(args or {})
        if "id" in args:
            # Antes do request_id: o #código do prompt e o id inteiro são a mesma ação.
            args["id"] = self._full_id(args["id"])
        handler = getattr(self, f"_{name}", None)
        if name not in _NAMES or handler is None:
            return ToolResult({"ok": False, "erro": f"Ferramenta desconhecida: {name}"}, ok=False)
        if name not in MUTATING:
            result = self._safe(handler, args, None, turn, known_ids)
        else:
            rid = request_id(turn.key, name, args)
            cached = turn.actions.get(rid)
            if cached is not None:
                logger.info("Ação repetida no mesmo turno ignorada: %s (%s)", name, rid)
                return cached
            result = self._safe(handler, args, rid, turn, known_ids)
            if result.ok:
                turn.actions[rid] = result
        turn.seen_ids.update(result.ids)
        return result

    def _safe(self, handler, args, rid, turn, known_ids) -> ToolResult:
        try:
            return handler(args, rid, turn, known_ids)
        except StaleError as error:
            return ToolResult({
                "ok": False,
                "versao_mudou": True,
                "item": format_item(error.current),
                "erro": "Isso mudou em outro PC e nada foi alterado. Esta é a versão atual: mostre à pessoa e "
                        "confirme antes de mudar de novo.",
            }, ok=False, ids=[error.current["id"]])
        except BrainError as error:
            return ToolResult({"ok": False, "erro": str(error)}, ok=False)
        except Exception as error:  # noqa: BLE001 - a ferramenta falha, a conversa segue
            logger.exception("Ferramenta falhou")
            return ToolResult({"ok": False, "erro": f"Falha interna: {type(error).__name__}"}, ok=False)

    def _full_id(self, value: Any) -> str:
        """O id inteiro a partir do que o modelo mandou: o próprio id ou o #código da memória do prompt
        (um começo de id com pelo menos SHORT_ID caracteres que só um id tem). Sem par, volta como veio
        e a ação responde que não achou."""
        text = str(value or "").strip().lstrip("#").strip()
        if len(text) < SHORT_ID or self.store.get(text) is not None:
            return text
        matches = [item["id"] for item in self.store.snapshot() if item["id"].startswith(text)]
        return matches[0] if len(matches) == 1 else text

    # ---------- leitura ----------

    def _buscar_memoria(self, args, rid, turn, known_ids):
        query = str(args.get("consulta", "")).strip()
        if not query:
            return ToolResult({"ok": False, "erro": "Informe a consulta."}, ok=False)
        limit = min(max(int(args.get("limite") or 8), 1), 20)
        found, mode = self.store.search(query, include_done=bool(args.get("incluir_concluidas")), limit=limit)
        items = [{**format_item(item), "relevancia": score} for item, score in found]
        response = {"ok": True, "modo": mode, "itens": items}
        if mode == "texto":
            response["aviso"] = "Busca por texto (sem embedding agora): tente também sinônimos."
        return ToolResult(response, f"Buscou: {_short(query)}", ids=[item["id"] for item in items])

    def _buscar_conversas(self, args, rid, turn, known_ids):
        query = str(args.get("consulta", "")).strip()
        if not query:
            return ToolResult({"ok": False, "erro": "Informe a consulta."}, ok=False)
        if self.conversations is None:
            return ToolResult({"ok": False, "erro": "Não há conversas guardadas."}, ok=False)
        limit = min(max(int(args.get("limite") or 8), 1), 20)
        found = self.conversations.search(query, limit)
        return ToolResult({"ok": True, "trechos": found}, f"Buscou nas conversas: {_short(query)}")

    # ---------- ações ----------

    def _salvar_memoria(self, args, rid, turn, known_ids):
        content = str(args.get("conteudo", "")).strip()
        if not content:
            return ToolResult({"ok": False, "erro": "Conteúdo vazio."}, ok=False)
        is_task = bool(args.get("tarefa"))
        vector = self.store.document_embedding(content)
        if not args.get("forcar"):
            similar = self.store.most_similar(content, vector)
            if similar and similar[1] >= SIMILAR_THRESHOLD:
                item, score = similar
                return ToolResult({
                    "ok": False,
                    "ja_existe": format_item(item),
                    "similaridade": score,
                    "erro": f"Já existe parecido: \"{item['content']}\". Nada foi gravado. Se for a mesma coisa, "
                            "use atualizar_memoria com este id; se for outra, chame salvar_memoria com forcar=true.",
                }, f"Já existe parecido: {_short(item['content'])}", ok=False, ids=[item["id"]])
        item = self.store.add(content, clean_tags(args.get("etiquetas")), PENDING if is_task else None,
                              request_id=rid, embedding=vector)
        verb = "Salvou tarefa" if is_task else "Salvou"
        return _with_sync(ToolResult({"ok": True, "id": item["id"], "item": format_item(item)},
                                     f"{verb}: {_short(item['content'])}", ids=[item["id"]]), item)

    def _atualizar_memoria(self, args, rid, turn, known_ids):
        item_id = str(args.get("id", "")).strip()
        before = self.store.get(item_id)
        if before is None:
            return _not_found(item_id)
        if before.get("pinned"):
            return ToolResult({"ok": False, "erro": _PINNED_REFUSAL}, ok=False, ids=[item_id])
        content = args.get("conteudo")
        tags = args.get("etiquetas")
        after = self.store.update(item_id, content=str(content) if content is not None else None,
                                  tags=list(tags) if tags is not None else None)
        changes = {
            field_name: {"antes": before[key], "depois": after[key]}
            for field_name, key in (("conteudo", "content"), ("etiquetas", "tags"))
            if before[key] != after[key]
        }
        if not changes:
            return ToolResult({"ok": True, "id": item_id, "mudancas": {}, "aviso": "Nada mudou."}, ids=[item_id])
        return _with_sync(ToolResult({"ok": True, "id": item_id, "mudancas": changes, "item": format_item(after)},
                                     f"Atualizou: {_short(after['content'])}", ids=[item_id]), after)

    def _concluir_tarefa(self, args, rid, turn, known_ids):
        item_id = str(args.get("id", "")).strip()
        before = self.store.get(item_id)
        if before is None:
            return _not_found(item_id)
        if before.get("pinned"):
            return ToolResult({"ok": False, "erro": _PINNED_REFUSAL}, ok=False, ids=[item_id])
        if before["status"] is None:
            return ToolResult({"ok": False, "erro": "Isso não é coisa a fazer: não há o que riscar."}, ok=False)
        item = self.store.complete(item_id)
        response = {"ok": True, "id": item_id, "item": format_item(item)}
        if before["status"] == DONE:
            response["aviso"] = "Isso já estava feito."
        return _with_sync(ToolResult(response, f"Concluiu: {_short(item['content'])}", ids=[item_id]), item)

    def _apagar_memoria(self, args, rid, turn, known_ids):
        item_id = str(args.get("id", "")).strip()
        item = self.store.get(item_id)
        if item is None:
            return _not_found(item_id)
        if item.get("pinned"):
            return ToolResult({"ok": False, "erro": _PINNED_REFUSAL}, ok=False, ids=[item_id])
        if item["status"] is None:
            # A confirmação tem de vir de outra mensagem do usuário: o item precisa ter aparecido
            # numa resposta de ferramenta de um turno anterior (a chamada sem confirmado).
            if not args.get("confirmado") or item_id not in known_ids:
                return ToolResult({
                    "ok": False,
                    "confirmacao_necessaria": True,
                    "item": format_item(item),
                    "erro": "Não apagou. Mostre à pessoa o que seria apagado, pergunte se pode e, só na resposta, "
                            "chame apagar_memoria de novo com confirmado=true.",
                }, ok=False, ids=[item_id])
        removed = self.store.delete(item_id)
        return _with_sync(ToolResult({"ok": True, "id": item_id, "apagado": format_item(removed)},
                                     f"Apagou: {_short(removed['content'])}", ids=[item_id]), removed)


_NAMES = frozenset(declaration.name for declaration in declarations())


def format_item(item: dict) -> dict:
    """Item como o modelo vê nas respostas das ações: campos em português e datas no fuso local."""
    kind = {None: "guardado", PENDING: "a fazer", DONE: "feito"}[item["status"]]
    result: dict[str, Any] = {
        "id": item["id"],
        "conteudo": item["content"],
        "etiquetas": item["tags"],
        "tipo": kind,
        "criado_em": _local(item["created_at"]),
    }
    if item.get("updated_at") and item["updated_at"] != item["created_at"]:
        result["atualizado_em"] = _local(item["updated_at"])
    if item.get("done_at"):
        result["feito_em"] = _local(item["done_at"])
    if item.get("pinned"):
        result["fixado"] = True
    return result


def short_ids(ids: Iterable[str]) -> dict[str, str]:
    """#código de cada id: os primeiros SHORT_ID caracteres, ou mais quando outro id começa igual (o
    maior começo em comum com qualquer outro está num vizinho da ordem alfabética)."""
    ordered = sorted(set(ids))
    codes = {}
    for index, item_id in enumerate(ordered):
        neighbors = ordered[max(index - 1, 0):index] + ordered[index + 1:index + 2]
        shared = max((_common_prefix(item_id, other) for other in neighbors), default=0)
        codes[item_id] = item_id[:max(SHORT_ID, shared + 1)]
    return codes


def memory_for_prompt(items: list[dict], budget: int) -> tuple[str, bool]:
    """A memória inteira para o system_instruction: uma linha por coisa guardada, das mais novas para
    as mais antigas, com o #código. Itens fixos ficam de fora (vão no bloco "Sobre a pessoa"). O que é a fazer
    entra sempre; o resto, por recência, até `budget` caracteres. Devolve (texto, cortou)."""
    codes = short_ids(item["id"] for item in items)
    shown = sorted((item for item in items if not item.get("pinned")), key=lambda item: item["created_at"], reverse=True)
    if not shown:
        return "(nada ainda)", False
    lines = {item["id"]: _prompt_line(item, codes[item["id"]]) for item in shown}
    kept = {item["id"] for item in shown if item["status"] == PENDING}
    used = sum(len(lines[item_id]) + 1 for item_id in kept)
    for item in shown:
        if item["id"] in kept:
            continue
        size = len(lines[item["id"]]) + 1
        if used + size > budget:
            break  # corte por data: o que é mais antigo que isto também fica de fora
        kept.add(item["id"])
        used += size
    result = [lines[item["id"]] for item in shown if item["id"] in kept]
    left = len(shown) - len(kept)
    if left:
        result.append(f"- … e mais {left} coisa(s) antiga(s) que não couberam aqui: para elas, use "
                      "buscar_memoria antes de dizer que não sabe.")
    return "\n".join(result), bool(left)


def _prompt_line(item: dict, code: str) -> str:
    content = " ".join(str(item["content"]).split())
    tags = f" [{', '.join(item['tags'])}]" if item.get("tags") else ""
    state = {PENDING: " · a fazer", DONE: f" · feito em {_local_date(item.get('done_at'))}"}.get(item["status"], "")
    return f"- {_local_date(item['created_at'])} · {content}{tags}{state} · #{code}"


def _common_prefix(a: str, b: str) -> int:
    size = 0
    for x, y in zip(a, b):
        if x != y:
            break
        size += 1
    return size


def _not_found(item_id: str) -> ToolResult:
    return ToolResult({"ok": False, "erro": f"Não achei nada com o código {item_id} na memória."}, ok=False)


def _with_sync(result: ToolResult, item: dict) -> ToolResult:
    """Alteração que ficou só na fila (sem conexão): o modelo e a linha da interface dizem isso."""
    if item.get("sync") == "pending":
        result.response["nuvem"] = _PENDING_NOTE
        result.label = f"{result.label} (na fila: sem conexão)"
    return result


def clean_round(calls: list[tuple[str, ToolResult]]) -> bool:
    """Rodada só com ações de memória que deram certo sem ressalva (sem aviso e sem fila): o texto que o
    modelo escreveu junto com elas já é a resposta. Leitura, recusa, "já existe parecido", confirmação
    de apagar, versão mudou, "nada mudou", "já estava feito" e "ficou só neste PC" voltam ao modelo."""
    return bool(calls) and all(
        name in MUTATING and result.ok and "aviso" not in result.response and "nuvem" not in result.response
        for name, result in calls
    )


def describe_action(result: ToolResult) -> str:
    """Resumo de uma ação para o histórico achatado (parar, erro) e para regenerar."""
    return result.label or json.dumps(result.response, ensure_ascii=False)[:200]


def _local(stamp: Optional[str], pattern: str = "%d/%m/%Y %H:%M") -> Optional[str]:
    if not stamp:
        return None
    try:
        return local_time(datetime.fromisoformat(stamp)).strftime(pattern)
    except ValueError:
        return stamp


def _local_date(stamp: Optional[str]) -> str:
    return _local(stamp, "%d/%m/%Y") or "?"


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _LABEL_LIMIT else text[:_LABEL_LIMIT - 1].rstrip() + "…"
