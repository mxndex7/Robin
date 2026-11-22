"""Sessão com o Gemini: streaming, cancelamento, watchdog, nova tentativa, fallback de modelo,
regenerar, lista de modelos, embeddings e o laço das ferramentas do cérebro.

Laço das ferramentas (explícito; o automatic function calling do SDK fica desligado): cada rodada é
um send_message_stream numa sessão criada a partir de base_history + as rodadas já concluídas do
turno. Uma nova tentativa (5xx, 429, watchdog, fallback) repete só a rodada que falhou: as
ferramentas já executadas não rodam de novo. Regenerar mantém o mesmo Turn (tools.py), cujo cache
de ações e request_id impedem que uma ação rode duas vezes no mesmo turno do usuário.
"""

import logging
import math
import queue
import re
import threading
import time
import uuid
from typing import Any, Callable, Optional

import httpx
from google import genai
from google.genai import errors, types

from .config import (
    DEFAULT_MODEL,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EMBEDDING_TIMEOUT_MS,
    FALLBACK_EXPIRY_SECONDS,
    FALLBACK_MODEL,
    FALLBACK_MODELS,
    HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT,
    MAX_PROMPT_MEMORY_CHARS,
    MAX_TOOL_ROUNDS,
    RATE_LIMIT_PER_MINUTE,
    RATE_WAIT_MAX,
    REPLY_WITH_ACTION,
    RETRY_DELAY,
    ROBIN_SYSTEM_DIRECTIVE,
    STREAM_FLUSH_INTERVAL,
    THINKING_LEVEL,
    WATCHDOG_SECONDS,
)
from . import timing
from .ratelimit import RateLimiter
from .brain import MemoryStore
from .clock import now_text
from .memory import read_memory
from .tools import (
    MUTATING,
    MemoryTools,
    ToolResult,
    Turn,
    clean_round,
    declarations,
    describe_action,
    memory_for_prompt,
)

logger = logging.getLogger("robin.gemini")

Emit = Callable[[str, dict], None]

# Seletor: só modelos estáveis de chat, gemini-X.Y-flash, -flash-lite e -pro (X ≥ 3: os 2.5 dão 404
# para chaves novas) e gemini-4-*. Ficam de fora previews, versões com data, aliases (-latest) e
# variantes de imagem, áudio, tts, live e embedding.
_STABLE_CHAT_MODEL = re.compile(r"gemini-(?:(\d+)(?:\.\d+)?-(?:flash|flash-lite|pro)|4(?:\.\d+)?-[a-z][a-z0-9-]*)")
_EXCLUDED_MODEL_TOKENS = (
    "preview", "exp", "latest", "image", "tts", "live", "audio", "embedding", "transcribe",
    "customtools", "robotics", "computer-use",
)
_DATED_MODEL = re.compile(r"\d{2}-\d{4}|\d{8}|-\d{3}$")

# Falhas passageiras: uma nova tentativa automática e, se persistirem, o modelo de reserva.
_TRANSIENT_CODES = ("server", "rate_limit", "timeout")

_POLL_INTERVAL = 0.1  # a thread da geração confere parar e o watchdog neste intervalo
_DETAIL_LIMIT = 300
# 429 com "limit: 0": a chave não tem cota nenhuma para o modelo (não é limite passageiro).
_NO_QUOTA = re.compile(r"\blimit:\s*0(?![\d.])")
# Chave de API do Google (AIza + 35) e parâmetro key= de URL: nunca vão para log nem interface.
_SECRET_PATTERNS = (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), re.compile(r"(?<=key=)[^&\s'\"]+"))
# Assinatura que a API aceita no lugar da original quando o histórico vai para outro modelo
# (as assinaturas de raciocínio só valem para o modelo que as gerou).
_SKIP_SIGNATURE = b"skip_thought_signature_validator"


class GeminiError(Exception):
    """Erro já traduzido para exibir na interface. detail: texto original curto, sem a chave.
    no_quota: 429 com "limit: 0" (a chave não tem cota para o modelo). daily: 429 da cota diária
    (nova tentativa não adianta). retry_after: segundos que o 429 manda esperar (retryDelay)."""

    def __init__(self, code: str, message: str, detail: str = "", no_quota: bool = False,
                 daily: bool = False, retry_after: Optional[float] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.no_quota = no_quota
        self.daily = daily
        self.retry_after = retry_after


class WatchdogTimeout(Exception):
    """Nenhum pedaço do stream (nem de raciocínio) dentro do prazo do watchdog."""


class _TimeoutClient(httpx.Client):
    """httpx com timeout de conexão e de leitura. Sem HttpOptions.timeout, o SDK passa timeout=None
    em cada requisição, o que anularia o padrão do cliente: aqui o None vira o padrão."""

    def build_request(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = httpx.USE_CLIENT_DEFAULT
        return super().build_request(*args, **kwargs)

    def __del__(self):
        # O SDK não fecha um cliente httpx fornecido por fora.
        try:
            self.close()
        except Exception:  # noqa: BLE001 - fim do processo
            pass


def _new_client(api_key: str) -> genai.Client:
    http = _TimeoutClient(
        timeout=httpx.Timeout(HTTP_READ_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
        follow_redirects=True,
    )
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(httpx_client=http))


class _Generation:
    """Estado de uma resposta em andamento."""

    def __init__(self, gen_id: int, user_text: str, base_history: list, turn: Turn, prior: list[ToolResult]):
        self.id = gen_id
        self.user_text = user_text
        self.chat: Any = None
        # Histórico antes deste turno: base para parar (record_history), nova tentativa e fallback.
        self.base_history = base_history
        self.turn = turn
        # Ações de uma geração anterior do mesmo turno (regenerar): vão no system_instruction.
        self.prior = prior
        # Rodadas de ferramentas já concluídas neste turno (mensagem, resposta do modelo, ...) e a
        # próxima mensagem: o texto do usuário ou as respostas das ferramentas.
        self.contents: list = []
        self.message: Any = user_text
        self.rounds = 0
        self.round_start = 0  # tamanho de text quando a rodada atual começou
        self.round_model: list = []  # Contents do modelo na tentativa atual
        self.calls: list = []  # function_call da tentativa atual
        self.round_results: list = []  # (ferramenta, ToolResult) da última rodada de ferramentas
        # Uma ação que altera a memória roda inteira: parar espera por ela (é local e rápida).
        self.tool_lock = threading.Lock()
        self.cancel = threading.Event()
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.text = ""  # tudo o que chegou
        self.pending = ""  # ainda não enviado à interface
        self.finish_reason: Optional[str] = None
        self.thinking = False  # onThinking já enviado nesta tentativa
        self.stopping = False  # stop() em andamento: ele encerra o turno
        # Memória e trechos das conversas anteriores do início do turno (texto, cortou), os mesmos
        # em todas as rodadas.
        self.memory: Optional[tuple[str, bool]] = None
        self.recent: tuple[str, bool] = ("", False)
        self.requests = 0  # pedidos ao modelo neste turno (logs de tempo)

    def push(self, text: str) -> None:
        with self.lock:
            self.text += text
            self.pending += text

    def take(self) -> str:
        with self.lock:
            chunk, self.pending = self.pending, ""
            return chunk


class GeminiService:
    def __init__(
        self,
        emit: Emit,
        api_key: str = "",
        model: str = "",
        save_model: Optional[Callable[[str], None]] = None,
        store: Optional[MemoryStore] = None,
        core: Optional[Callable[[], str]] = None,
        rate_limit: Optional[int] = None,
        conversations: Any = None,
    ):
        self._emit = emit
        # Conversas guardadas (conversations.ConversationRecorder): trechos das anteriores no
        # system_instruction e buscar_conversas. None: só a conversa atual.
        self._conversations = conversations
        # Pedidos por minuto por modelo (plano gratuito: 5); 0 desliga.
        self._limiter = RateLimiter(RATE_LIMIT_PER_MINUTE if rate_limit is None else rate_limit)
        # Troca temporária para o modelo de reserva: o modelo escolhido e até quando a troca vale.
        self._fallback_from: Optional[str] = None
        self._fallback_until = 0.0
        # Núcleo fixo do system_instruction: os itens fixos da nuvem ou o memory.md (padrão).
        self._core = core or _memory_for_prompt
        # Grava o modelo quando o fallback troca de modelo de vez (modelo indisponível para a chave).
        self._save_model = save_model
        self._lock = threading.RLock()
        self._client: Optional[genai.Client] = None
        self._chat: Any = None
        self._model = model or DEFAULT_MODEL
        self._active: Optional[_Generation] = None
        self._last_user_text: Optional[str] = None
        self._last_turn_recorded = False
        self._pending_rebuild = False
        # Modelos que deram 429 com "limit: 0" nesta execução (o seletor os marca até reabrir o app).
        self._no_quota: set[str] = set()
        # Cérebro: sem store, a sessão não tem ferramentas. O store usa este serviço para os embeddings.
        self._store: Optional[MemoryStore] = None
        self._tools: Optional[MemoryTools] = None
        self._attach_store(store)
        self._session = uuid.uuid4().hex[:12]
        self._turn_number = 0
        self._turn: Optional[Turn] = None
        # ids que as ferramentas mostraram em turnos anteriores: apagar uma nota exige que o
        # usuário a tenha visto antes de confirmar.
        self._known_ids: set[str] = set()
        self.configure(api_key)

    # ---------- estado ----------

    @property
    def model(self) -> str:
        return self._model

    @property
    def is_generating(self) -> bool:
        return self._active is not None

    @property
    def no_quota_models(self) -> list[str]:
        return sorted(self._no_quota)

    # ---------- sessão ----------

    def configure(self, api_key: str) -> None:
        """(Re)cria o cliente. O histórico da conversa atual é mantido."""
        with self._lock:
            history = self._history()
            self._client = _new_client(api_key) if api_key else None
            self._chat = self._new_chat(history) if self._client else None
        self._embeddings_changed()

    def _attach_store(self, store: Optional[MemoryStore]) -> None:
        self._store = store
        self._tools = MemoryTools(store, self._conversations) if store is not None else None
        if store is not None:
            store.embed = self.embed

    def set_store(self, store: Optional[MemoryStore]) -> None:
        """Troca o armazenamento (ligar ou desligar o Supabase). Vale a partir do próximo turno;
        a sessão é recriada com o núcleo e a memória do store novo."""
        with self._lock:
            self._attach_store(store)
        self.rebuild()
        self._embeddings_changed()

    def _embeddings_changed(self) -> None:
        """Chave nova: o embedding volta a ser tentado e os itens sem vetor são preenchidos."""
        if self._store is not None and self._client:
            self._store.reset_embed_cooldown()
            self.schedule_backfill()

    def _new_chat(self, history: list, generation: Optional[_Generation] = None) -> Any:
        """Nova sessão com a memória e a hora atuais no system_instruction (chats.create não acessa a
        rede). Na última rodada de ferramentas do turno, elas vão desligadas."""
        start = time.monotonic()
        core = self._core()
        core_ms = timing.ms(start)
        # A memória fica congelada no início da mensagem: refeita a cada rodada, a coisa recém-salva
        # aparecia como já existente e o modelo respondia "já existe" em vez de "anotado".
        if generation is not None and generation.memory is not None:
            memory, cut = generation.memory
            recent, older = generation.recent
        else:
            memory, cut = _brain_for_prompt(self._store)
            recent, older = _conversations_for_prompt(self._conversations)
            if generation is not None:
                generation.memory = (memory, cut)
                generation.recent = (recent, older)
        instruction = system_instruction(core, memory, recent)
        timing.log("sessão montada: núcleo %dms, memória e conversas %dms (%d caracteres no system_instruction)",
                   core_ms, timing.ms(start) - core_ms, len(instruction))
        if generation is not None and generation.prior:
            instruction += _prior_actions_note(generation.prior)
        tools_off = generation is not None and generation.rounds >= MAX_TOOL_ROUNDS
        # As buscas só quando algo ficou de fora do prompt (ou se a conversa já as usou: o histórico
        # tem chamadas delas).
        search = cut or _calls_tool(history, "buscar_memoria")
        search_conversations = older or _calls_tool(history, "buscar_conversas")
        return self._client.chats.create(
            model=self._model,
            config=_generate_config(self._model, instruction, self._tools is not None, tools_off, search,
                                    search_conversations),
            history=history,
        )

    def _history(self) -> list:
        """Histórico curado da sessão. O SDK grava um Content por pedaço do stream;
        aqui os pedaços consecutivos do mesmo papel viram um Content só."""
        if not self._chat:
            return []
        merged: list = []
        for content in self._chat.get_history(curated=True):
            if merged and merged[-1].role == content.role:
                merged[-1] = types.Content(role=content.role, parts=[*(merged[-1].parts or []), *(content.parts or [])])
            else:
                merged.append(content)
        return merged

    def rebuild(self) -> None:
        """Recria a sessão com a memória nova mantendo o histórico (salvar memória, trocar modelo).
        Durante uma geração, a troca fica para quando ela terminar."""
        with self._lock:
            if not self._client:
                return
            if self._active:
                self._pending_rebuild = True
                return
            self._chat = self._new_chat(self._history())

    def set_model(self, name: str) -> None:
        with self._lock:
            self._model = name
            self._fallback_from = None  # escolha do usuário vale mais que a troca temporária
            self.rebuild()

    def _restore_model(self) -> None:
        """A troca temporária para o modelo de reserva expirou: volta ao modelo escolhido (no
        próximo envio; com o _lock na mão)."""
        if self._fallback_from is None or time.monotonic() < self._fallback_until:
            return
        previous, self._model, self._fallback_from = self._model, self._fallback_from, None
        logger.info("Troca temporária expirou: de volta a %s (estava em %s)", self._model, previous)
        self._emit("onModelChanged", {"model": self._model, "previous": previous, "reason": "restored"})

    def clear(self) -> None:
        """Conversa nova: interrompe a geração (sem gravar nada) e recria a sessão vazia."""
        self.load_history([])

    def load_history(self, messages: list[tuple[str, str]]) -> None:
        """Troca a conversa da sessão por `messages` ((papel, texto); "user" ou "model"), como numa
        conversa reaberta: interrompe a geração (sem gravar nada) e o histórico vira só texto (as
        chamadas de ferramenta não voltam; a memória inteira já vai no system_instruction)."""
        with self._lock:
            if self._active:
                self._active.cancel.set()
                self._active = None
            self._pending_rebuild = False
            users = [text for role, text in messages if role == "user"]
            self._last_user_text = users[-1] if users else None
            self._last_turn_recorded = bool(messages) and messages[-1][0] == "model"
            self._turn = None
            self._known_ids = set()
            self._chat = self._new_chat(_text_history(messages)) if self._client else None

    # ---------- geração ----------

    def send(self, text: str, gen_id: int) -> None:
        self._start(text, gen_id, new_turn=True)

    def _start(self, text: str, gen_id: int, new_turn: bool) -> None:
        timing.set_tag(f"g{gen_id}")
        start = time.monotonic()
        timing.log("envio recebido (%d caracteres)", len(text))
        with timing.wait_lock(self._lock, "do GeminiService"):
            if not self._client:
                raise GeminiError("no_key", "Configure a API Key do Gemini para começar.")
            if self._active:
                raise GeminiError("busy", "Aguarde a resposta atual ou pare a geração.")
            self._restore_model()
            if new_turn or self._turn is None:
                if self._turn is not None:
                    self._known_ids |= self._turn.seen_ids
                self._turn_number += 1
                self._turn = Turn(f"{self._session}:{self._turn_number}")
                prior = []
            else:
                prior = list(self._turn.actions.values())
            generation = _Generation(gen_id, text, self._history(), self._turn, prior)
            # Sessão nova a cada turno: a hora no system_instruction é a deste envio.
            generation.chat = self._new_chat(generation.base_history, generation)
            self._chat = generation.chat
            self._active = generation
            self._last_user_text = text
        timing.log("envio preparado em %dms", timing.ms(start))
        threading.Thread(target=self._run, args=(generation,), daemon=True).start()

    def regenerate(self, gen_id: int) -> None:
        with self._lock:
            if self._active:
                raise GeminiError("busy", "Aguarde a resposta atual ou pare a geração.")
            if not self._last_user_text:
                raise GeminiError("empty", "Nada para regenerar.")
            if self._last_turn_recorded and self._client:
                # Remove o último turno (mensagem do usuário, rodadas de ferramentas e resposta).
                history = self._history()
                last_user = max((i for i, c in enumerate(history) if _is_user_message(c)), default=None)
                if last_user is not None:
                    self._chat = self._new_chat(history[:last_user])
            self._last_turn_recorded = False
            text = self._last_user_text
        # Mesmo turno: as ações já feitas não se repetem (cache do Turn e aviso no system_instruction).
        self._start(text, gen_id, new_turn=False)

    def stop(self) -> bool:
        """Para a geração e grava o trecho já gerado, para o contexto seguir coerente."""
        with self._lock:
            generation = self._active
            if not generation:
                return False
            generation.stopping = True  # daqui em diante, quem encerra o turno é o stop
        # Uma ação em andamento termina antes de parar. A espera fica fora do lock do serviço: a
        # ação pode estar na rede (Supabase, embedding) e nada mais pode ficar preso atrás dela.
        with generation.tool_lock:
            generation.cancel.set()
        with self._lock:
            if self._active is not generation:
                return False  # limpeza (clear) no meio da espera
            self._active = None
            self._flush(generation)

            # A sessão antiga é abandonada: a thread do stream pode ainda estar bloqueada
            # na rede e, se o stream terminasse, o SDK gravaria o turno nela de novo.
            chat = self._new_chat(generation.base_history)
            self._last_turn_recorded = _record_flat(chat, generation)
            self._chat = chat
            self._pending_rebuild = False
        self._emit("onDone", {"gen_id": generation.id, "stopped": True})
        return True

    def _run(self, generation: _Generation) -> None:
        timing.set_tag(f"g{generation.id}")
        started = time.monotonic()
        timing.log("geração iniciada (modelo %s)", self._model)
        flusher = threading.Thread(target=self._flush_loop, args=(generation,), daemon=True)
        self._emit("onStart", {"gen_id": generation.id, "model": self._model})
        flusher.start()

        # Qualquer exceção vira onError e vai para o log: a interface nunca fica em "pensando".
        try:
            error = self._generate(generation)
        except Exception as exc:  # noqa: BLE001 - falha fora do stream (ferramentas, sessão nova)
            logger.exception("Falha inesperada na geração %d", generation.id)
            error = _internal_error(exc)
        if error is None and not generation.text.strip() and not generation.cancel.is_set():
            reason = generation.finish_reason or "desconhecido"
            error = GeminiError("empty_response", f"O modelo não retornou texto (motivo: {reason}).")

        generation.finished.set()
        flusher.join()

        try:
            if not self._finish(generation, error):
                return  # parada ou limpeza já cuidou deste turno
        except Exception as exc:  # noqa: BLE001 - a sessão nova falhou: o turno termina mesmo assim
            logger.exception("Falha ao encerrar a geração %d", generation.id)
            with self._lock:
                if self._active is generation:
                    self._active = None
            error = error or _internal_error(exc)

        if error is None:
            self._emit("onDone", {"gen_id": generation.id, "stopped": False})
            timing.log("onDone emitido: turno em %dms, %d pedido(s) ao modelo", timing.ms(started), generation.requests)
        else:
            timing.log("onError (%s) a emitir: turno em %dms", error.code, timing.ms(started))
            self._emit("onError", {
                "gen_id": generation.id,
                "code": error.code,
                "message": error.message,
                "detail": error.detail,
                "partial": bool(generation.text.strip()),
            })

    def _finish(self, generation: _Generation, error: Optional[GeminiError]) -> bool:
        """Encerra o turno na sessão. False: parar ou limpar já cuidou dele."""
        with self._lock:
            if self._active is not generation or generation.stopping:
                return False
            self._flush(generation)
            self._active = None
            if error is None:
                self._last_turn_recorded = True
            else:
                # Com erro, nada do turno fica na sessão ("Tentar novamente" só reenvia a mensagem),
                # a não ser as ações já feitas: o turno entra resumido em texto.
                self._chat = self._new_chat(generation.base_history)
                self._last_turn_recorded = bool(generation.turn.actions) and _record_flat(self._chat, generation)
            if self._pending_rebuild:
                self._pending_rebuild = False
                self._chat = self._new_chat(self._history())
        return True

    def _generate(self, generation: _Generation) -> Optional[GeminiError]:
        """Envia o turno com as reservas e roda as ferramentas pedidas, rodada a rodada. Cada
        tentativa parte do histórico anterior ao turno mais as rodadas já concluídas, então nem o
        turno nem uma ação ficam duplicados. Só antes de qualquer texto visível na rodada:
        - modelo inexistente (404): troca para o FALLBACK_MODEL e grava no .env;
        - falha passageira (5xx, 429, timeout): nova tentativa após RETRY_DELAY e, se persistir,
          o FALLBACK_MODEL só nesta execução."""
        retried = False
        while True:
            if not self._wait_for_slot(generation):
                return None  # parada ou limpeza durante a espera
            model = self._model
            try:
                self._stream_into(generation)
            except Exception as exc:  # noqa: BLE001 - erros do SDK/rede são traduzidos aqui
                error = _translate_error(exc, model)
            else:
                if generation.cancel.is_set() or not generation.calls:
                    return None
                written = generation.text[generation.round_start:]  # o que o modelo escreveu junto
                if not self._run_tools(generation):
                    return None  # parada ou limpeza durante as ferramentas
                if self._answered_with_action(generation, written):
                    return None  # ações limpas e a resposta já veio junto: sem outro pedido
                retried = False  # cada rodada tem a própria nova tentativa
                continue
            if error.no_quota:
                self._no_quota.add(model)
            if generation.cancel.is_set():
                return None
            if generation.text[generation.round_start:]:
                return error  # o texto já está na tela: reenviar duplicaria a resposta

            if error.code == "model_unavailable" and self._model != FALLBACK_MODEL:
                resumed = self._use_fallback(generation, persist=True)
            elif error.daily and self._model != FALLBACK_MODEL:
                # Cota diária do modelo acabou: nova tentativa não adianta; a reserva vale até ela voltar.
                resumed = self._use_fallback(generation, persist=False, cause="daily_quota", until=error.retry_after)
            elif error.daily:
                return error  # a reserva também sem cota hoje
            elif error.code in _TRANSIENT_CODES and not retried:
                retried = True
                delay = RETRY_DELAY
                if error.code == "rate_limit" and error.retry_after and error.retry_after <= RATE_WAIT_MAX:
                    delay = error.retry_after  # o 429 por minuto diz quando a vaga volta
                self._emit("onRetry", {"gen_id": generation.id, "code": error.code})
                resumed = not generation.cancel.wait(delay) and self._restart(generation)
            elif error.code in _TRANSIENT_CODES and self._model != FALLBACK_MODEL:
                resumed = self._use_fallback(generation, persist=False)
            else:
                return error
            if not resumed:
                return None  # parada ou limpeza durante a espera ou a troca

    def _restart(self, generation: _Generation, model: Optional[str] = None) -> bool:
        """Sessão nova (opcionalmente com outro modelo) a partir do histórico anterior ao turno:
        a tentativa que falhou não deixa nada gravado."""
        with self._lock:
            if self._active is not generation:
                return False
            if model and model != self._model:
                self._model = model
                generation.contents = _for_other_model(generation.contents)
            generation.chat = self._new_chat(generation.base_history + generation.contents, generation)
            generation.thinking = False
            generation.finish_reason = None
            self._chat = generation.chat
        return True

    # ---------- ferramentas ----------

    def _run_tools(self, generation: _Generation) -> bool:
        """Executa as chamadas da rodada e prepara a próxima. False: parada ou limpeza."""
        parts = []
        changed = False
        generation.round_results = []
        for call in generation.calls:
            name = call.name or ""
            if name in MUTATING:
                with generation.tool_lock:
                    if generation.cancel.is_set():
                        return False
                    result = self._call_tool(generation, name, call.args)
                    changed = changed or result.ok
            else:
                if generation.cancel.is_set():
                    return False
                result = self._call_tool(generation, name, call.args)
            generation.round_results.append((name, result))
            parts.append(types.Part(function_response=types.FunctionResponse(
                id=call.id or None, name=name, response=result.response,
            )))
        if changed:
            self.schedule_backfill()
        with self._lock:
            if self._active is not generation:
                return False
            model_parts = [part for content in generation.round_model for part in (content.parts or [])]
            generation.contents = [
                *generation.contents,
                _user_content(generation.message),
                types.Content(role="model", parts=model_parts),
            ]
            generation.message = parts
            generation.rounds += 1
            generation.round_start = len(generation.text)
            generation.chat = self._new_chat(generation.base_history + generation.contents, generation)
            self._chat = generation.chat
        return True

    def _answered_with_action(self, generation: _Generation, written: str) -> bool:
        """O modelo mandou a resposta junto com as ações e todas deram certo sem ressalva
        (tools.clean_round): o turno termina sem o 2º pedido. A resposta é o texto escrito na mesma
        mensagem ou, sem ele, o argumento resposta das chamadas (o flash-lite quase nunca escreve
        texto junto), que só aparece agora, com o resultado limpo. Na sessão, a rodada fica como
        chamada → resultado → a resposta, que sai da mensagem da chamada para não aparecer duas vezes.
        Ação com problema ("já existe", recusa, fila) ou sem resposta volta ao modelo, que explica."""
        if not REPLY_WITH_ACTION or not clean_round(generation.round_results):
            return False
        reply = written if written.strip() else _call_replies(generation.calls)
        if not reply:
            return False
        with self._lock:
            if self._active is not generation or generation.cancel.is_set():
                return False
            if not written.strip():
                if generation.text.strip():
                    generation.push("\n\n")  # texto de uma rodada anterior do turno
                generation.push(reply)
            contents = [*generation.contents[:-1], _without_text(generation.contents[-1])]
            chat = self._new_chat(generation.base_history + contents, generation)
            chat.record_history(
                user_input=_user_content(generation.message),
                model_output=[types.ModelContent(parts=[types.Part.from_text(text=reply.strip())])],
                is_valid=True,
            )
            generation.contents = contents
            generation.chat = chat
            self._chat = chat
        timing.log("resposta junto com a ação (%s), sem 2º pedido (%d ação(ões))",
                   "texto" if written.strip() else "argumento", len(generation.round_results))
        return True

    def _call_tool(self, generation: _Generation, name: str, args: Any) -> ToolResult:
        start = time.monotonic()
        if self._tools is None:
            result = ToolResult({"ok": False, "erro": "Ferramentas indisponíveis."}, ok=False)
        else:
            result = self._tools.call(name, dict(args or {}), generation.turn, self._known_ids)
        timing.log("ferramenta %s: %dms (%s)", name, timing.ms(start), "ok" if result.ok else "recusada")
        logger.info("Ferramenta %s: %s", name, "ok" if result.ok else result.response.get("erro", "recusada"))
        if result.label:
            self._emit("onToolCall", {"gen_id": generation.id, "tool": name, "label": result.label, "ok": result.ok})
        return result

    def schedule_backfill(self) -> None:
        """Gera em segundo plano os embeddings que faltam (o store ignora se já houver um rodando)."""
        if self._store is not None and self._client:
            threading.Thread(target=self._backfill, daemon=True).start()

    def _backfill(self) -> None:
        try:
            count = self._store.backfill_embeddings()
        except Exception:  # noqa: BLE001 - nunca derruba o app
            logger.warning("Backfill de embeddings falhou", exc_info=True)
            return
        if count:
            logger.info("Backfill: %d embedding(s) gerado(s)", count)

    def embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        """Embeddings do cérebro (MemoryStore.embed). Levanta exceção sem chave ou com falha da API."""
        client = self._client
        if client is None:
            raise GeminiError("no_key", "Sem API Key para os embeddings.")
        with timing.measure(f"embedding ({len(texts)} texto(s), {task_type})"):
            response = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=list(texts),
                config=types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=EMBEDDING_DIMENSIONS,
                    http_options=types.HttpOptions(timeout=EMBEDDING_TIMEOUT_MS),
                ),
            )
        return [list(embedding.values or []) for embedding in response.embeddings or []]

    def _wait_for_slot(self, generation: _Generation) -> bool:
        """Limitador local: espera a vaga do minuto em vez de tomar 429. Se a espera passar de
        RATE_WAIT_MAX e o modelo de reserva tiver vaga, usa a reserva. False: parada ou limpeza."""
        delay = self._limiter.delay(self._model)
        if not delay:
            return True
        if delay > RATE_WAIT_MAX and self._model != FALLBACK_MODEL and not self._limiter.delay(FALLBACK_MODEL):
            return self._use_fallback(generation, persist=False, cause="rate_limit")
        timing.log("limitador: %.1fs até a vaga de %s", delay, self._model)
        self._emit("onWaiting", {"gen_id": generation.id, "seconds": math.ceil(delay)})
        return not generation.cancel.wait(delay) and self._active is generation

    def _use_fallback(self, generation: _Generation, persist: bool, cause: str = "transient",
                      until: Optional[float] = None) -> bool:
        """persist: modelo inexistente para a chave (grava no .env). Senão, troca temporária: vale
        FALLBACK_EXPIRY_SECONDS (ou até a cota diária voltar, se for mais) e depois volta sozinha."""
        previous = self._model
        if not self._restart(generation, FALLBACK_MODEL):
            return False
        payload = {"model": FALLBACK_MODEL, "previous": previous, "reason": "fallback" if persist else "temporary"}
        if persist:
            if self._save_model:
                try:
                    self._save_model(FALLBACK_MODEL)
                except OSError:
                    logger.warning("Não foi possível gravar o modelo de reserva no .env", exc_info=True)
        else:
            expires = max(FALLBACK_EXPIRY_SECONDS, until or 0.0)
            with self._lock:
                self._fallback_from = self._fallback_from or previous
                self._fallback_until = time.monotonic() + expires
            payload.update(cause=cause, expires_in=round(expires))
            logger.info("Troca temporária para %s (%s) por %ds", FALLBACK_MODEL, cause, round(expires))
        self._emit("onModelChanged", payload)
        return True

    def _stream_into(self, generation: _Generation) -> None:
        """Uma tentativa. O stream roda numa thread própria e os pedaços chegam por uma fila:
        parar e o watchdog não dependem da rede (a thread presa na rede é abandonada)."""
        chunks: queue.Queue = queue.Queue()
        abandon = threading.Event()
        generation.round_model = []
        generation.calls = []
        generation.requests += 1
        number, model = generation.requests, self._model
        self._limiter.record(model)
        timing.log("pedido %d ao Gemini (%s, rodada %d) enviado", number, model, generation.rounds)
        started = time.monotonic()
        first = None
        threading.Thread(target=_pump, args=(generation.chat, generation.message, chunks, abandon), daemon=True).start()
        deadline = time.monotonic() + WATCHDOG_SECONDS
        try:
            while not generation.cancel.is_set():
                try:
                    kind, payload = chunks.get(timeout=_POLL_INTERVAL)
                except queue.Empty:
                    if time.monotonic() < deadline:
                        continue
                    # A sessão da tentativa abandonada ainda gravaria o turno se o stream terminasse.
                    self._restart(generation)
                    timing.log("pedido %d: watchdog sem pedaços em %dms", number, timing.ms(started))
                    raise WatchdogTimeout(f"nenhum pedaço do stream em {WATCHDOG_SECONDS:g}s") from None
                if kind == "end":
                    timing.log("pedido %d: fim em %dms (1º pedaço em %sms, %d chamada(s) de ferramenta)",
                               number, timing.ms(started), first, len(generation.calls))
                    return
                if kind == "error":
                    timing.log("pedido %d: erro em %dms: %s", number, timing.ms(started), type(payload).__name__)
                    raise payload
                if first is None:
                    first = timing.ms(started)
                    timing.log("pedido %d: 1º pedaço em %dms", number, first)
                deadline = time.monotonic() + WATCHDOG_SECONDS
                self._consume(generation, payload)
            timing.log("pedido %d: parado em %dms", number, timing.ms(started))
        finally:
            abandon.set()

    def _consume(self, generation: _Generation, chunk: Any) -> None:
        candidates = getattr(chunk, "candidates", None) or []
        content = candidates[0].content if candidates else None
        if content is not None:
            generation.round_model.append(content)
            generation.calls.extend(part.function_call for part in content.parts or [] if part.function_call)
        text, thought = _chunk_parts(chunk)
        if text:
            if generation.rounds and len(generation.text) == generation.round_start and generation.text.strip():
                generation.push("\n\n")  # texto de uma rodada depois de outra que já escreveu
            generation.push(text)
        elif thought and not generation.text and not generation.thinking:
            generation.thinking = True
            self._emit("onThinking", {"gen_id": generation.id})
        reason = _finish_reason(chunk)
        if reason:
            generation.finish_reason = reason

    def _flush_loop(self, generation: _Generation) -> None:
        while not generation.finished.wait(STREAM_FLUSH_INTERVAL):
            if generation.cancel.is_set():
                return
            self._flush(generation)

    def _flush(self, generation: _Generation) -> None:
        chunk = generation.take()
        if chunk:
            self._emit("onChunk", {"gen_id": generation.id, "text": chunk})

    # ---------- modelos e chave ----------

    def list_models(self) -> list[str]:
        """Modelos de chat disponíveis para a chave atual; lista fixa como reserva."""
        names: list[str] = []
        if self._client:
            try:
                names = _chat_models(self._client)
            except Exception:  # noqa: BLE001 - sem rede/chave: usa a reserva
                names = []
        names = names or list(FALLBACK_MODELS)
        if self._model not in names:
            names.append(self._model)
        return sorted(set(names), key=_model_sort_key)


def validate_api_key(api_key: str) -> Optional[GeminiError]:
    """Testa a chave listando os modelos. Retorna None se estiver válida."""
    try:
        # O cliente precisa viver até o fim da requisição: um Client temporário encadeado
        # é apagado antes do envio e o __del__ do SDK fecha a conexão ("client has been closed").
        with _new_client(api_key) as client:
            # Só a primeira página: basta uma resposta autenticada.
            next(iter(client.models.list(config={"page_size": 1})), None)
    except Exception as exc:  # noqa: BLE001
        return _translate_error(exc, "")
    return None


def _pump(chat: Any, message: Any, chunks: queue.Queue, abandon: threading.Event) -> None:
    """Lê o stream (na thread própria) e repassa cada pedaço pela fila."""
    try:
        stream = chat.send_message_stream(message)
        try:
            for chunk in stream:
                if abandon.is_set():
                    return  # fecha o stream sem o SDK gravar o turno
                chunks.put(("chunk", chunk))
        finally:
            stream.close()
        chunks.put(("end", None))
    except Exception as exc:  # noqa: BLE001 - tratado na thread da geração
        chunks.put(("error", exc))


def _memory_for_prompt() -> str:
    """memory.md para o system_instruction. Ilegível (sem permissão, fora de UTF-8): a sessão segue
    sem ela em vez de derrubar o app; o erro vai para o log e a interface avisa ao carregar a memória."""
    try:
        return read_memory()
    except (OSError, ValueError) as error:
        logger.error("memory.md ilegível para o system_instruction: %s: %s", type(error).__name__, error)
        return "(memory.md indisponível: não foi possível ler o arquivo)"


def system_instruction(core: Optional[str] = None, memory: str = "(nada ainda)",
                       conversations: str = "(nenhuma ainda)") -> str:
    """Diretiva com o núcleo fixo ("Sobre a pessoa"), a memória inteira, os trechos das conversas
    anteriores e a data e hora atuais."""
    return ROBIN_SYSTEM_DIRECTIVE.format(
        core=_memory_for_prompt() if core is None else core, now=now_text(), memory=memory,
        conversations=conversations,
    )


def _conversations_for_prompt(conversations: Any) -> tuple[str, bool]:
    """Trechos das conversas anteriores (ConversationRecorder.prompt_excerpt) e se ficou algo de fora:
    aí buscar_conversas é oferecida."""
    if conversations is None:
        return "(nenhuma ainda)", False
    try:
        return conversations.prompt_excerpt()
    except Exception as error:  # noqa: BLE001 - sem os trechos, a conversa segue
        logger.warning("Conversas anteriores indisponíveis para o system_instruction: %s", error)
        return "(indisponíveis agora)", False


def _text_history(messages: list[tuple[str, str]]) -> list:
    """(papel, texto) em Contents; mensagens seguidas do mesmo papel viram um Content só."""
    history: list = []
    for role, text in messages:
        part = types.Part.from_text(text=text)
        if history and history[-1].role == role:
            history[-1] = types.Content(role=role, parts=[*history[-1].parts, part])
        else:
            history.append(types.Content(role=role, parts=[part]))
    return history


def _brain_for_prompt(store: Optional[MemoryStore]) -> tuple[str, bool]:
    """A memória inteira (tools.memory_for_prompt) e se ela foi cortada: aí buscar_memoria é oferecida."""
    if store is None:
        return "(nada guardado nesta sessão)", False
    try:
        items = store.snapshot()
    except Exception as error:  # noqa: BLE001 - sem a lista, o modelo ainda pode buscar
        logger.warning("Memória indisponível para o system_instruction: %s", error)
        return "(indisponível agora: use buscar_memoria)", True
    return memory_for_prompt(items, MAX_PROMPT_MEMORY_CHARS)


def _call_replies(calls: list) -> str:
    """O argumento resposta das chamadas da rodada: as respostas diferentes, na ordem."""
    replies: list[str] = []
    for call in calls:
        text = str((call.args or {}).get("resposta") or "").strip()
        if text and text not in replies:
            replies.append(text)
    return " ".join(replies)


def _calls_tool(history: list, name: str) -> bool:
    return any(
        part.function_call and part.function_call.name == name
        for content in history for part in content.parts or []
    )


def _prior_actions_note(actions: list[ToolResult]) -> str:
    lines = "\n".join(f"- {describe_action(action)} (id {', '.join(action.ids)})" for action in actions)
    return (
        "\n\nAÇÕES JÁ EXECUTADAS PARA A ÚLTIMA MENSAGEM DO USUÁRIO (esta é uma resposta regenerada): "
        f"elas já valem; não as repita.\n{lines}\n"
    )


def _generate_config(model: str, system_instruction: str, tools: bool = False, tools_off: bool = False,
                     search: bool = False, search_conversations: bool = False) -> types.GenerateContentConfig:
    """Sem temperature (ver config.py). Os resumos do raciocínio vêm no stream: a interface mostra
    "Raciocinando…" e o watchdog conta esses pedaços como atividade. Ferramentas sem o automatic
    function calling do SDK: o laço é o deste módulo. search / search_conversations: incluem
    buscar_memoria / buscar_conversas."""
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        thinking_config=types.ThinkingConfig(include_thoughts=True, thinking_level=_thinking_level(model)),
    )
    if tools:
        config.tools = [types.Tool(function_declarations=declarations(search, search_conversations))]
        config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
        if tools_off:
            config.tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.NONE),
            )
    return config


def _user_content(message: Any) -> types.Content:
    if isinstance(message, str):
        return types.UserContent(parts=[types.Part.from_text(text=message)])
    return types.UserContent(parts=list(message))


def _is_user_message(content: types.Content) -> bool:
    """Mensagem do usuário (não as respostas das ferramentas, que também têm o papel "user")."""
    return content.role == "user" and not any(part.function_response for part in content.parts or [])


def _without_text(content: types.Content) -> types.Content:
    """A mensagem do modelo sem o texto visível (ficam o raciocínio e as chamadas). Uma assinatura que
    estivesse no texto passa para a primeira chamada."""
    parts, signature = [], None
    for part in content.parts or []:
        if part.text and not part.thought:
            signature = signature or part.thought_signature
            continue
        parts.append(part)
    if signature:
        first = next((index for index, part in enumerate(parts) if part.function_call), None)
        if first is not None and not parts[first].thought_signature:
            parts[first] = parts[first].model_copy(update={"thought_signature": signature})
    return types.Content(role=content.role, parts=parts)


def _for_other_model(contents: list) -> list:
    """Rodadas do turno para outro modelo: sem as partes de raciocínio e com a assinatura neutra
    nas chamadas de função."""
    result = []
    for content in contents:
        if content.role != "model":
            result.append(content)
            continue
        parts = []
        for part in content.parts or []:
            if part.thought:
                continue
            signature = _SKIP_SIGNATURE if part.function_call else None
            parts.append(part.model_copy(update={"thought_signature": signature}))
        result.append(types.Content(role="model", parts=parts))
    return result


def _record_flat(chat: Any, generation: _Generation) -> bool:
    """Grava o turno interrompido (parar, erro) como texto: o parcial e as ações já feitas.
    Sem as partes de função, o histórico segue válido para a próxima mensagem."""
    partial = generation.text
    actions = list(generation.turn.actions.values())
    if not partial.strip() and not actions:
        return False
    text = partial
    if actions:
        summary = "; ".join(describe_action(action) for action in actions)
        text = f"{partial.rstrip()}\n\n[Ações já executadas neste turno: {summary}]".strip()
    chat.record_history(
        user_input=types.UserContent(parts=[types.Part.from_text(text=generation.user_text)]),
        model_output=[types.ModelContent(parts=[types.Part.from_text(text=text)])],
        is_valid=True,
    )
    return True


def _thinking_level(model: str) -> Optional[str]:
    """THINKING_LEVEL nos flash e flash-lite 3.x; os demais (pro, 2.5) ficam no padrão do modelo."""
    match = re.match(r"gemini-(\d+)(?:\.\d+)?-flash", model)  # inclui o -lite
    return THINKING_LEVEL if match and int(match.group(1)) >= 3 else None


def _chat_models(client: genai.Client) -> list[str]:
    names = []
    for model in client.models.list(config={"page_size": 100}):
        name = (model.name or "").removeprefix("models/")
        if "generateContent" in (model.supported_actions or []) and _is_stable_chat_model(name):
            names.append(name)
    return names


def _is_stable_chat_model(name: str) -> bool:
    match = _STABLE_CHAT_MODEL.fullmatch(name)
    if not match or (match.group(1) and int(match.group(1)) < 3):
        return False
    return not any(token in name for token in _EXCLUDED_MODEL_TOKENS) and not _DATED_MODEL.search(name)


def _model_sort_key(name: str) -> tuple:
    """Versão mais nova primeiro; dentro dela, pro > flash > flash-lite; estáveis antes de previews."""
    match = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    version = float(match.group(1)) if match else 0.0
    tier = 0 if "-pro" in name else 2 if "lite" in name else 1
    preview = 1 if ("preview" in name or "exp" in name) else 0
    return (-version, tier, preview, name)


def _chunk_parts(chunk: Any) -> tuple[str, bool]:
    candidates = getattr(chunk, "candidates", None) or []
    if not candidates or not candidates[0].content or not candidates[0].content.parts:
        return "", False
    return _content_parts(candidates[0].content.parts)


def _content_parts(parts: list) -> tuple[str, bool]:
    """Texto visível (sem o raciocínio) e se há partes de raciocínio (thought)."""
    text = "".join(part.text for part in parts if part.text and not part.thought)
    return text, any(part.thought for part in parts)


def _finish_reason(chunk: Any) -> Optional[str]:
    candidates = getattr(chunk, "candidates", None) or []
    if candidates and candidates[0].finish_reason:
        reason = candidates[0].finish_reason
        return getattr(reason, "name", str(reason))
    return None


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<oculto>", text)
    return text


def _describe(exc: Exception) -> str:
    """Tipo, código HTTP e mensagem originais, sem a chave."""
    if isinstance(exc, errors.APIError):
        head = " ".join(str(part) for part in (type(exc).__name__, exc.code, exc.status) if part)
        return _redact(f"{head}: {exc.message or exc}")
    return _redact(f"{type(exc).__name__}: {exc}")


def _internal_error(exc: Exception) -> GeminiError:
    description = _describe(exc)
    return GeminiError("internal", "Falha interna ao responder. Os detalhes estão no robin.log.",
                       detail=description[:_DETAIL_LIMIT])


def _translate_error(exc: Exception, model: str) -> GeminiError:
    """Traduz para a interface e registra no log a exceção original."""
    description = _describe(exc)
    logger.warning("Gemini%s: %s", f" ({model})" if model else "", description)
    error = _classify(exc, model)
    error.detail = description if len(description) <= _DETAIL_LIMIT else description[:_DETAIL_LIMIT - 1] + "…"
    return error


def _quota_details(exc: Exception) -> tuple[bool, Optional[str], Optional[float]]:
    """Do 429 do Gemini: se é a cota diária (quotaId ...PerDay...), o limite e o retryDelay em segundos."""
    details = getattr(exc, "details", None)
    error = details.get("error", details) if isinstance(details, dict) else {}
    daily, limit, retry_after = False, None, None
    for item in (error.get("details") or []) if isinstance(error, dict) else []:
        if not isinstance(item, dict):
            continue
        for violation in item.get("violations") or []:
            if "PerDay" in str(violation.get("quotaId", "")):
                daily, limit = True, str(violation.get("quotaValue") or "") or None
        if item.get("retryDelay"):
            try:
                retry_after = float(str(item["retryDelay"]).rstrip("s"))
            except ValueError:
                pass
    return daily, limit, retry_after


def _classify(exc: Exception, model: str) -> GeminiError:
    code = getattr(exc, "code", None)
    message = str(exc)
    lowered = message.lower()
    if "api key not valid" in lowered or "api_key_invalid" in lowered:
        return GeminiError("invalid_key", "API Key inválida. Confira a chave nas configurações.")
    if isinstance(exc, (WatchdogTimeout, httpx.TimeoutException)):
        return GeminiError("timeout", "O Gemini não respondeu a tempo.")
    if code == 429 or "resource_exhausted" in lowered:
        if _NO_QUOTA.search(message):
            return GeminiError("rate_limit", f"Esta chave não tem cota para o modelo {model}.", no_quota=True)
        daily, limit, retry_after = _quota_details(exc)
        if daily:
            hours = f" Volta em cerca de {max(1, round(retry_after / 3600))}h." if retry_after else ""
            per_day = f" ({limit} pedidos por dia no plano gratuito)" if limit else ""
            return GeminiError("rate_limit", f"A cota diária do modelo {model} acabou{per_day}.{hours}",
                               daily=True, retry_after=retry_after)
        return GeminiError("rate_limit", "Limite de uso da API atingido. Tente de novo em instantes.",
                           retry_after=retry_after)
    if code == 404:
        # Só o 404 troca de modelo (e grava): modelo inexistente ou fora do alcance desta chave.
        return GeminiError("model_unavailable", f"Modelo {model} indisponível para esta chave.")
    if code == 403 or "permission_denied" in lowered:
        return GeminiError("forbidden", "Sem permissão para usar a API do Gemini com esta chave.")
    if code is not None and code >= 500:
        return GeminiError("server", "O serviço do Gemini falhou. Tente de novo.")
    if any(token in lowered for token in ("connect", "timeout", "timed out", "network", "getaddrinfo")):
        return GeminiError("network", "Sem conexão com a API do Gemini.")
    return GeminiError("unknown", _redact(message) or exc.__class__.__name__)
