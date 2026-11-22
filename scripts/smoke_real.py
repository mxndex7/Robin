"""Diagnóstico do modo real: modelos disponíveis e envios curtos com a chave do .env.

Uso: .venv/Scripts/python scripts/smoke_real.py [--models a,b] [--variants a,b] [--repeat N] [--prompt TEXTO] [--no-list]
A chave vem do .env da pasta de dados (robin.config.read_env()) e nunca é impressa (as mensagens passam por _safe(),
que a substitui).

Modelos (padrão: o padrão do app, o 3.8-flash e a reserva). Opcional: --models gemini-4-argon.
A listagem mostra todos os modelos com generateContent e a lista que o seletor do app exibiria.

Variantes (padrão: app,service):
  app      um stream com o cliente e a configuração do app (gemini._new_client e _generate_config:
           timeouts do httpx, sem temperature, include_thoughts, nível do raciocínio). Imprime cada
           pedaço; prazo de 60s controlado por fora do SDK.
  legacy   a configuração antiga (temperature=0.2, sem thinking_config, sem timeout), para comparar.
  service  o GeminiService de ponta a ponta em dois turnos: watchdog, nova tentativa, fallback
           temporário e histórico com o raciocínio. Imprime os eventos que iriam para a interface.
           Aqui o fallback nunca grava no .env.
  brain    o cérebro de ponta a ponta com um brain.json temporário (o real não é tocado) e um núcleo
           neutro (o memory.md real não vai): pede para lembrar de algo a fazer, conta uma ideia sem
           pedir para anotar (memória automática), pergunta o que pediu para anotar (tem de vir tudo)
           e risca o que fez. Imprime os eventos (inclusive onToolCall), a resposta, quantos pedidos
           ao modelo cada mensagem gastou (1 = a resposta veio junto com a ação) e o que ficou guardado.
  supabase o Supabase real (ROBIN_SUPABASE_URL/KEY do .env; a chave nunca é impressa): valida a
           tabela e a RPC, cria itens com a etiqueta "smoke-test" (cópias locais temporárias), busca
           pela RPC e pelo fts, testa a proteção de versão velha com duas "máquinas", conclui e, no
           fim, APAGA tudo o que tem a etiqueta "smoke-test". Usa a chave do Gemini para os
           embeddings, se houver. Depois confere as tabelas das conversas (scripts/supabase_conversas.sql):
           grava uma conversa "smoke-test", confere o updated_at do servidor, o reenvio sem duplicar e a
           marca de apagada, e apaga essa conversa. Roda uma vez (ignora --models).
"""

import argparse
import queue
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from robin import datadir  # noqa: E402
from robin.brain import LocalStore, StaleError  # noqa: E402
from robin.supabase import CloudError, SupabaseClient, SupabaseStore  # noqa: E402
from robin.config import (  # noqa: E402
    DEFAULT_MODEL,
    ENV_API_KEY,
    ENV_FILE,
    ENV_SUPABASE_KEY,
    ENV_SUPABASE_URL,
    FALLBACK_MODEL,
    read_env,
)
from robin.gemini import GeminiService, _content_parts, _generate_config, _new_client, system_instruction  # noqa: E402

MODELS = [DEFAULT_MODEL, "gemini-3.8-flash", FALLBACK_MODEL]
PROMPT = "responda só: ok"
FOLLOW_UP = "Qual foi exatamente a sua resposta anterior? Responda só ela."
BRAIN_PROMPTS = (
    "Lembre de comprar pão amanhã.",
    "Tive uma ideia: mudar a foto do LinkedIn.",
    "O que eu pedi pra você anotar até agora?",
    "Comprei o pão.",
)
# Núcleo da variante brain: neutro, para medir a diretiva sem o memory.md de quem roda.
BRAIN_CORE = "- Nome: Alex (teste do smoke_real)."
DEADLINE = 60.0
# O serviço tem os próprios prazos: até 2 tentativas de 60s, a espera e o fallback.
SERVICE_DEADLINE = 240.0
LEGACY_TEMPERATURE = 0.2

_KEY = ""
_SUPABASE_KEY = ""
SMOKE_TAG = "smoke-test"
# Pausa entre as mensagens da variante brain (um 429 viraria pedido extra na contagem).
BRAIN_PAUSE = 13.0


def _safe(text: str) -> str:
    for secret, label in ((_KEY, "<API_KEY>"), (_SUPABASE_KEY, "<SUPABASE_KEY>")):
        if secret:
            text = text.replace(secret, label)
    return text


def _out(text: str = "") -> None:
    print(_safe(text), flush=True)


def _describe_exc(exc: BaseException) -> str:
    lines = [
        f"tipo: {type(exc).__module__}.{type(exc).__name__}",
        f"código HTTP: {getattr(exc, 'code', None)}",
        f"status: {getattr(exc, 'status', None)}",
        f"mensagem: {exc}",
    ]
    return "\n".join("    " + line for line in lines)


# ---------- app / legacy: um stream, pedaço a pedaço ----------

def _client_and_config(model: str, variant: str) -> tuple[genai.Client, types.GenerateContentConfig]:
    instruction = system_instruction()
    if variant == "legacy":
        config = types.GenerateContentConfig(system_instruction=instruction, temperature=LEGACY_TEMPERATURE)
        return genai.Client(api_key=_KEY), config
    return _new_client(_KEY), _generate_config(model, instruction)


def _worker(model: str, variant: str, prompt: str, events: queue.Queue) -> None:
    try:
        client, config = _client_and_config(model, variant)
        chat = client.chats.create(model=model, config=config)
        start = time.monotonic()
        for chunk in chat.send_message_stream(prompt):
            events.put(("chunk", (time.monotonic() - start, chunk)))
        events.put(("end", time.monotonic() - start))
    except BaseException as exc:  # noqa: BLE001 - diagnóstico: tudo é relatado
        events.put(("error", exc))


def _chunk_line(elapsed: float, chunk) -> str:
    candidate = (chunk.candidates or [None])[0]
    parts = (candidate.content.parts if candidate and candidate.content else None) or []
    visible, _ = _content_parts(parts)
    thoughts = sum(1 for p in parts if p.thought)
    if len(visible) > 60:
        visible = visible[:57] + "..."
    reason = candidate.finish_reason if candidate else None
    reason = getattr(reason, "name", reason)
    return f"  +{elapsed:6.2f}s  partes={len(parts)} thought={thoughts}  texto={visible!r}  finish={reason}"


def run_stream(model: str, variant: str, prompt: str) -> None:
    _out(f"\n=== {model} | {variant} ===")
    events: queue.Queue = queue.Queue()
    threading.Thread(target=_worker, args=(model, variant, prompt, events), daemon=True).start()
    deadline = time.monotonic() + DEADLINE
    last = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _out(f"  TIMEOUT: nenhum fim de stream em {DEADLINE:.0f}s (thread abandonada)")
            return
        try:
            kind, payload = events.get(timeout=remaining)
        except queue.Empty:
            continue
        if kind == "chunk":
            elapsed, chunk = payload
            if last is None and chunk.model_version:
                _out(f"  model_version={chunk.model_version}")
            _out(_chunk_line(elapsed, chunk))
            last = chunk
        elif kind == "end":
            usage = last.usage_metadata if last else None
            if usage:
                _out(f"  tokens: prompt={usage.prompt_token_count} thoughts={usage.thoughts_token_count} "
                     f"resposta={usage.candidates_token_count}")
            _out(f"  OK em {payload:.2f}s")
            return
        else:
            _out("  FALHOU:")
            _out(_describe_exc(payload))
            tb = traceback.format_exception(type(payload), payload, payload.__traceback__)
            for line in "".join(tb).rstrip().splitlines()[-3:]:
                _out("      " + line)
            return


# ---------- service: o GeminiService de ponta a ponta ----------

def run_service(model: str, prompt: str) -> None:
    _out(f"\n=== {model} | service ===")
    finished = threading.Event()
    state = {"start": time.monotonic(), "chunks": 0, "error": False}

    def emit(name: str, payload: dict) -> None:
        elapsed = time.monotonic() - state["start"]
        if name == "onChunk":
            state["chunks"] += 1
            if state["chunks"] == 1:
                _out(f"  +{elapsed:6.2f}s onChunk (primeiro) {payload['text'][:40]!r}")
            return
        extra = f" ({state['chunks']} onChunk)" if name in ("onDone", "onError") else ""
        _out(f"  +{elapsed:6.2f}s {name} {payload}{extra}")
        if name in ("onDone", "onError"):
            state["error"] = name == "onError"
            finished.set()

    service = GeminiService(emit=emit, api_key=_KEY, model=model)  # sem save_model: nada vai para o .env
    for gen_id, text in enumerate((prompt, FOLLOW_UP), start=1):
        _out(f"  turno {gen_id}: {text!r}")
        finished.clear()
        state.update(start=time.monotonic(), chunks=0)
        service.send(text, gen_id)
        if not finished.wait(SERVICE_DEADLINE):
            _out(f"  SEM onDone/onError em {SERVICE_DEADLINE:.0f}s")
            return
        if state["error"]:
            break
    turns = [(content.role, _content_parts(content.parts or [])[0][:40]) for content in service._history()]
    _out(f"  modelo no fim: {service.model}")
    _out(f"  histórico: {turns}")


def run_brain(model: str) -> None:
    _out(f"\n=== {model} | brain ===")
    finished = threading.Event()
    texts: list[str] = []

    def emit(name: str, payload: dict) -> None:
        if name == "onChunk":
            texts.append(payload["text"])
            return
        if name in ("onToolCall", "onRetry", "onModelChanged", "onError"):
            _out(f"  {name} {payload}")
        if name in ("onDone", "onError"):
            finished.set()

    with tempfile.TemporaryDirectory(prefix="robin-smoke-") as folder:
        store = LocalStore(Path(folder) / "brain.json")
        service = GeminiService(emit=emit, api_key=_KEY, model=model, store=store, core=lambda: BRAIN_CORE)
        calls = []
        original = service._client.models.generate_content_stream
        service._client.models.generate_content_stream = lambda **kw: calls.append(1) or original(**kw)
        for gen_id, text in enumerate(BRAIN_PROMPTS, start=1):
            _out(f"  turno {gen_id}: {text!r}")
            finished.clear()
            texts.clear()
            calls.clear()
            if gen_id > 1:
                time.sleep(BRAIN_PAUSE)  # limite do plano gratuito: 5 pedidos por minuto
            service.send(text, gen_id)
            if not finished.wait(SERVICE_DEADLINE):
                _out(f"  SEM onDone/onError em {SERVICE_DEADLINE:.0f}s")
                return
            _out(f"  resposta: {''.join(texts).strip()[:300]!r}")
            _out(f"  pedidos ao modelo: {len(calls)}")
        for item in store.snapshot():
            _out(f"  item: {item['content']!r} status={item['status']} tags={item['tags']} "
                 f"embedding={'sim' if item['embedding'] else 'não'}")


def run_supabase() -> None:
    """Supabase real: cria só itens com a etiqueta smoke-test e apaga todos eles no fim."""
    global _SUPABASE_KEY
    env = read_env()
    url = env.get(ENV_SUPABASE_URL, "").strip()
    _SUPABASE_KEY = env.get(ENV_SUPABASE_KEY, "").strip()
    _out("\n=== supabase ===")
    if not url or not _SUPABASE_KEY:
        _out(f"  {ENV_SUPABASE_URL}/{ENV_SUPABASE_KEY} ausentes no .env: configure em Configurações.")
        return
    gemini = GeminiService(emit=lambda *_: None, api_key=_KEY) if _KEY else None
    client = SupabaseClient(url, _SUPABASE_KEY)
    try:
        client.check()
        _out(f"  conexão ok: tabela memories e RPC match_memories ({url})")
    except CloudError as error:
        _out(f"  FALHOU ao validar: {error.kind}: {_safe(error.message)} | {_safe(error.detail)}")
        client.close()
        return

    def step(label, action):
        try:
            result = action()
            _out(f"  ok   {label}{'' if result is None else f': {_safe(str(result))[:200]}'}")
        except Exception as error:  # noqa: BLE001 - diagnóstico
            _out(f"  FALHOU {label}: {_safe(_describe_exc(error))}")

    with tempfile.TemporaryDirectory(prefix="robin-smoke-supabase-") as folder:
        machines = []
        for name in ("A", "B"):
            store = SupabaseStore(LocalStore(Path(folder) / name / "brain.json"),
                                  SupabaseClient(url, _SUPABASE_KEY), Path(folder) / name / "sync.json")
            store.embed = gemini.embed if gemini else None
            machines.append(store)
        a, b = machines
        created = {}
        try:
            step("add com embedding", lambda: created.setdefault("uva", a.add(
                "comprar uva (smoke)", [SMOKE_TAG], "pending", embedding=a.document_embedding("comprar uva"),
            ))["sync"])
            step("add nota", lambda: created.setdefault("nota", a.add(
                "reunião com o João na sexta (smoke)", [SMOKE_TAG], None))["sync"])
            step("busca RPC 'fruta roxa pequena'", lambda: [
                (item["content"], score) for item, score in a.search("fruta roxa pequena", limit=5)[0]
            ] if a.embed else "sem chave do Gemini: pulada")

            def fts():
                a.embed = None
                try:
                    return [item["content"] for item, _ in a.search("reunião joão")[0]]
                finally:
                    a.embed = gemini.embed if gemini else None
            step("busca fts 'reunião joão'", fts)
            step("pull na máquina B (só índice e diferenças)", lambda: b.pull())
            step("A atualiza a nota", lambda: a.update(created["nota"]["id"], content="reunião com o João na sexta às 15h (smoke)")["sync"])

            def stale():
                try:
                    b.update(created["nota"]["id"], content="versão velha de B (smoke)")
                except StaleError as error:
                    return f"recusado como esperado; versão atual: {error.current['content']!r}"
                raise AssertionError("B sobrescreveu a versão de A")
            step("B com versão velha", stale)
            step("A conclui a tarefa", lambda: a.complete(created["uva"]["id"])["status"])
            step("pedidos ao Supabase (A, B)", lambda: (a.client.requests, b.client.requests))
        finally:
            removed = client.delete({"tags": "cs.{" + SMOKE_TAG + "}"})
            _out(f"  limpeza: {len(removed)} item(ns) com a etiqueta {SMOKE_TAG} apagado(s) do banco")
            for store in machines:
                store.client.close()
        _check_conversation_tables(client, step)
        client.close()


def _check_conversation_tables(client: SupabaseClient, step) -> None:
    """Tabelas das conversas (scripts/supabase_conversas.sql): grava uma conversa "smoke-test", confere o
    updated_at do servidor (trigger) e a marca de apagada, e apaga tudo no fim."""
    conversation_id, message_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        client.select({"select": "id", "limit": 1}, "conversations")
        client.select({"select": "id", "limit": 1}, "conversation_messages")
    except CloudError as error:
        _out(f"  conversas: tabelas não encontradas ({error.kind}): rode scripts/supabase_conversas.sql no SQL Editor")
        return
    _out("  conversas: tabelas conversations e conversation_messages encontradas")
    try:
        step("conversa e mensagem inseridas (com created_at do PC)", lambda: (
            client.insert_new("conversations", [{"id": conversation_id, "title": SMOKE_TAG,
                                                "created_at": "2000-01-01T00:00:00+00:00", "updated_by": "smoke"}]),
            client.insert_new("conversation_messages", [{"id": message_id, "conversation_id": conversation_id,
                                                         "role": "user", "text": SMOKE_TAG, "actions": None, "status": None,
                                                         "error": None, "created_at": "2000-01-01T00:00:00+00:00"}]),
        ) and None)

        def server_time():
            row = client.select({"select": "created_at,updated_at", "id": f"eq.{message_id}"}, "conversation_messages")[0]
            if row["updated_at"].startswith("2000"):
                raise AssertionError("updated_at veio do PC: o trigger robin_touch_updated_at não está ativo")
            return f"created_at {row['created_at']} (do PC), updated_at {row['updated_at']} (do servidor)"
        step("updated_at do servidor", server_time)
        step("reenviar não duplica", lambda: (client.insert_new("conversation_messages", [{
            "id": message_id, "conversation_id": conversation_id, "role": "user", "text": "outro", "actions": None,
            "status": None, "error": None, "created_at": "2000-01-01T00:00:00+00:00"}]),
            client.select({"select": "text", "id": f"eq.{message_id}"}, "conversation_messages")[0]["text"])[1])
        step("marca de apagada", lambda: client.update({"id": f"eq.{conversation_id}"}, {"deleted": True, "title": ""},
                                                        "conversations")[0]["deleted"])
    finally:
        client.delete({"conversation_id": f"eq.{conversation_id}"}, "conversation_messages")
        client.delete({"id": f"eq.{conversation_id}"}, "conversations")
        _out("  conversas: limpeza da conversa smoke-test feita")


def list_models() -> list[str]:
    with _new_client(_KEY) as client:
        names = [
            (m.name or "").removeprefix("models/")
            for m in client.models.list(config={"page_size": 100})
            if "generateContent" in (m.supported_actions or [])
        ]
    return sorted(names)


def main() -> int:
    global _KEY
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default=",".join(MODELS), help="lista separada por vírgula")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--variants", default="app,service")
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--no-list", action="store_true", help="pula a listagem de modelos")
    args = parser.parse_args()

    datadir.prepare()  # como o app: a primeira execução migra a chave e a memória antigas
    _KEY = read_env().get(ENV_API_KEY, "").strip()
    if not _KEY:
        _out(f"{ENV_API_KEY} ausente no .env ({ENV_FILE}).")
        return 1
    _out(f"SDK google-genai {genai.__version__}; chave carregada (não exibida).")

    if not args.no_list:
        try:
            names = list_models()
            _out(f"\nModelos com generateContent ({len(names)}):")
            _out("  " + ", ".join(names))
            picker = GeminiService(emit=lambda *_: None, api_key=_KEY).list_models()
            _out(f"\nSeletor do app, com o modelo padrão como atual ({len(picker)}):")
            _out("  " + ", ".join(picker))
        except Exception as exc:  # noqa: BLE001
            _out("\nFalha ao listar modelos:")
            _out(_describe_exc(exc))

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    if "supabase" in variants:
        run_supabase()
        variants.remove("supabase")
    for model in (m.strip() for m in args.models.split(",") if m.strip()):
        for variant in variants:
            for _ in range(args.repeat):
                if variant == "service":
                    run_service(model, args.prompt)
                elif variant == "brain":
                    run_brain(model)
                else:
                    run_stream(model, variant, args.prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
