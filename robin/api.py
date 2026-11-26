"""RobinAPI: a ponte exposta ao JavaScript (window.pywebview.api.*).

O pywebview expõe os métodos públicos e percorre os atributos públicos do objeto;
por isso todo o estado interno fica em atributos com "_".
"""

import hashlib
import itertools
import json
import logging
import os
import queue
import secrets
import threading
import time
import webbrowser
from typing import Any, Optional

from .config import (
    APP_NAME,
    APP_VERSION,
    BRAIN_FILE,
    CONVERSATION_SYNC_FILE,
    CONVERSATIONS_DIR,
    DATA_DIR,
    DATA_ROOT,
    DEBUG,
    DEFAULT_MODEL,
    DONE_RETENTION_DAYS,
    ENV_API_KEY,
    ENV_GIT_SYNC,
    ENV_MODEL,
    ENV_RPM,
    ENV_SUPABASE_KEY,
    ENV_SUPABASE_URL,
    ENV_PALETTE,
    ENV_THEME,
    MAX_REOPEN_HISTORY_CHARS,
    MOCK,
    PALETTES,
    PULL_MIN_INTERVAL,
    SYNC_FILE,
    THEMES,
    WEBVIEW_DATA_DIR,
    palette_preference,
    read_env,
    theme_preference,
    write_env,
)
from . import cloud_mock, timing
from .attachments import attachment_limits, message_with_attachments, text_attachments
from .project_folder import ProjectFolder
from .brain import BrainError, LocalStore, StaleError
from .build import current_build
from .chatgpt import ChatGPTService, DEFAULT_CHATGPT_MODEL
from .chatgpt_auth import ChatGPTAccount, ChatGPTAuthError, SESSION_FILE
from .conversations import (ConversationContext, ConversationRecorder, ConversationStore,
                            conversation_identity, history_for_session)
from .conversations_cloud import ConversationCloud
from .gemini import GeminiError, GeminiService, validate_api_key
from .hotkey import DEFAULT_HOTKEY
from .memory import git_available, memory_is_blank, read_memory, sync_git, write_memory
from .mock import MockGeminiService
from .supabase import CloudError, SupabaseClient, SupabaseStore, normalize_url
from .window import WindowFrame, set_clipboard_text

logger = logging.getLogger("robin.api")


def _key_hint(key: str) -> str:
    return f"••••{key[-4:]}" if len(key) >= 4 else ""


def _error(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "error": message}


# Nova tentativa automática da fila enquanto a nuvem estiver fora (além de abrir e aparecer a janela).
CLOUD_RETRY_SECONDS = 60.0
ENV_PROVIDER = "ROBIN_PROVIDER"
ENV_CHATGPT_MODEL = "ROBIN_CHATGPT_MODEL"
# Entrega de um evento à interface acima disto vai para o log como aviso.
EVENT_SLOW_MS = 2000


def cloud_transport():
    """Transporte HTTP do Supabase: None (rede de verdade) ou a nuvem simulada (ROBIN_MOCK=1, que
    nunca acessa o Supabase real). Os testes trocam esta função."""
    if MOCK:
        return cloud_mock.simulated_cloud(DATA_DIR / "nuvem-simulada.json").transport()
    return None


class RobinAPI:
    def __init__(self):
        env = read_env()
        self._window: Any = None
        self._events: queue.Queue = queue.Queue()
        threading.Thread(target=self._deliver_events, name="robin-eventos", daemon=True).start()
        self._frame: Optional[WindowFrame] = None
        self._desktop: Any = None  # Desktop (desktop.py): atalho, bandeja, iniciar com o Windows
        self._api_key = env.get(ENV_API_KEY, "").strip()
        self._build = current_build()  # origem e build (rodapé das Configurações)
        self._gen_ids = itertools.count(1)
        self._mock = MOCK
        self._chat_lock = threading.RLock()
        self._attachment_turn: Optional[str] = None
        self._attachment_scope = secrets.token_hex(16)
        self._folder: Optional[tuple[str, str, ProjectFolder]] = None
        self._last_folder_id = ""
        self._chatgpt_account = ChatGPTAccount(DATA_DIR / SESSION_FILE)
        self._chatgpt_model = env.get(ENV_CHATGPT_MODEL) or DEFAULT_CHATGPT_MODEL
        self._chatgpt = None
        self._preferred_provider = env.get(ENV_PROVIDER, "gemini")
        self._provider = "gemini"
        service = MockGeminiService if self._mock else GeminiService
        # Cérebro: brain.json na pasta de dados (na simulação, na pasta mock\). Com o Supabase, ele é a
        # cópia local e o backup do SupabaseStore.
        self._local = LocalStore(BRAIN_FILE)
        self._cloud: Optional[SupabaseStore] = None
        self._supabase_key = env.get(ENV_SUPABASE_KEY, "").strip()
        self._sync_lock = threading.Lock()
        self._last_pull = 0.0
        self._retry_timer: Optional[threading.Timer] = None
        # Conversas no Supabase (fase 3): só com a nuvem configurada.
        self._conversation_cloud: Optional[ConversationCloud] = None
        # Conversas guardadas: o recorder acompanha a atual pelos eventos da geração (_emit).
        self._conversations = ConversationStore(CONVERSATIONS_DIR)
        self._recorder = ConversationRecorder(self._conversations, on_change=self._conversations_changed)
        self._gemini = service(
            emit=self._emit,
            api_key=self._api_key,
            model=env.get(ENV_MODEL, "").strip() or DEFAULT_MODEL,
            save_model=lambda name: write_env(ENV_MODEL, name),
            store=self._local,
            core=self._core_text,
            rate_limit=_rate_limit(env),
            conversations=ConversationContext(self._recorder, "gemini"),
            manage_embeddings=self._default_provider() == "gemini",
        )
        self._select_provider(self._default_provider())
        url = env.get(ENV_SUPABASE_URL, "").strip()
        if url and self._supabase_key:
            self._connect(url, self._supabase_key)
        else:
            self._cleanup_local()

    @property
    def _service(self):
        return self._chatgpt if self._provider == "chatgpt" else self._gemini

    def _provider_ready(self, provider: str) -> bool:
        if provider == "chatgpt":
            return not self._mock and self._chatgpt_account.status()["connected"]
        return bool(self._api_key) or self._mock

    def _identity(self) -> tuple[str, str]:
        return self._provider, self._chatgpt_account.profile_tag() if self._provider == "chatgpt" else ""

    def _attachment_context(self) -> str:
        provider, profile = self._identity()
        return hashlib.sha256(f"{self._attachment_scope}:{provider}:{profile}".encode()).hexdigest()

    def _rotate_attachment_scope(self) -> None:
        self._attachment_scope = secrets.token_hex(16)
        self._folder = None
        self._last_folder_id = ""
        self._attachment_turn = None

    def _selected_folder(self):
        folder = self._folder
        if folder is not None and folder[1] == self._attachment_context():
            return folder
        return None

    def _chat_connection_error(self) -> Optional[dict]:
        if not self._provider_ready(self._provider):
            return _error("no_key", "Conecte o provedor desta conversa nas Configurações.")
        identity = self._recorder.identity
        if identity is not None and identity != self._identity():
            return _error("other_account", "Esta conversa pertence a outra conta do ChatGPT. Abra uma nova conversa ou conecte a conta original.")
        return None

    def _default_provider(self) -> str:
        if self._mock:
            return "gemini"
        if self._preferred_provider in {"gemini", "chatgpt"} and self._provider_ready(self._preferred_provider):
            return self._preferred_provider
        return "chatgpt" if self._provider_ready("chatgpt") else "gemini"

    def _select_provider(self, provider: str) -> None:
        self._gemini.set_embeddings_enabled(provider == "gemini")
        if provider == "chatgpt" and self._chatgpt is None:
            self._chatgpt = ChatGPTService(
                self._chatgpt_account, emit=self._emit, model=self._chatgpt_model,
                store=self._brain(), core=self._core_text,
                conversations=ConversationContext(self._recorder, "chatgpt", self._chatgpt_account.profile_tag),
            )
        if self._provider != provider:
            self._rotate_attachment_scope()
        self._provider = provider

    def _set_chat_store(self, store) -> None:
        with self._chat_lock:
            self._gemini.set_store(store)
            if self._chatgpt is not None:
                self._chatgpt.set_store(store)

    def set_chat_provider(self, provider: str) -> dict:
        with self._chat_lock:
            if provider not in ("chatgpt", "gemini") or not self._provider_ready(provider):
                return _error("no_key", "Conecte esse provedor primeiro.")
            if self._service.is_generating:
                return _error("busy", "Pare a geração antes de trocar o provedor.")
            self._preferred_provider = provider
            write_env(ENV_PROVIDER, provider)
            if self._recorder.current_id is None:
                self._service.clear()
                self._select_provider(provider)
            return {"ok": True, "state": self.get_state()}

    def begin_chatgpt(self, new_account: bool = False, client_id: str = "") -> dict:
        if self._mock:
            return _error("mock", "O login fica disponível no Robin fora da simulação.")
        with self._chat_lock:
            if self._service.is_generating:
                return _error("busy", "Pare a geração antes de conectar outra conta.")
            if self._chatgpt_account.status()["connected"]:
                return _error("connected", "Desconecte a conta atual primeiro.")
            try:
                self._chatgpt_account.begin(new_account=new_account is True, client_id=str(client_id or ""))
            except (ChatGPTAuthError, OSError) as error:
                return _error("chatgpt_auth", str(error) if isinstance(error, ChatGPTAuthError) else
                              "O Windows não conseguiu salvar a conexão do ChatGPT.")
            return {"ok": True, **self._chatgpt_account.status()}

    def chatgpt_status(self) -> dict:
        with self._chat_lock:
            status = self._chatgpt_account.status()
            if self._recorder.current_id is None and not self._service.is_generating:
                provider = self._default_provider()
                if provider != self._provider:
                    self._service.clear()
                    self._select_provider(provider)
            return {"ok": True, **status, "state": self.get_state()}

    def disconnect_chatgpt(self) -> dict:
        with self._chat_lock:
            if self._service.is_generating:
                return _error("busy", "Pare a geração antes de desconectar.")
            try:
                self._chatgpt_account.disconnect()
            except OSError:
                return _error("io", "O Windows não conseguiu atualizar a conexão do ChatGPT.")
            return self.chatgpt_status()

    # ---------- uso interno (app.py) ----------

    def _attach(self, window: Any, frame: Optional[WindowFrame], desktop: Any = None) -> None:
        self._window = window
        self._frame = frame
        self._desktop = desktop

    def _emit(self, event: str, payload: dict) -> None:
        """Agenda window.robin.<event>(payload) na interface. Pode ser usado de qualquer thread, inclusive
        a da interface, e nunca espera por ela: o window.run_js do pywebview faz Invoke e espera o
        resultado do script, o que trava a thread da interface se for chamado nela (clique na bandeja)
        e prende quem emite (geração, gravação na nuvem com o lock na mão) enquanto ela estiver
        ocupada. Uma thread só entrega os eventos, na ordem em que chegaram. A conversa guardada
        acompanha os mesmos eventos (com ou sem janela)."""
        self._recorder.event(event, payload)
        if self._window is None:
            return
        self._events.put((event, payload, timing.tag(), time.monotonic()))

    def _deliver_events(self) -> None:
        while True:
            event, payload, tag, queued = self._events.get()
            try:
                self._deliver(event, payload, tag, queued)
            except Exception:  # noqa: BLE001 - um evento ruim não pode parar a entrega dos próximos
                logger.exception("Evento %s não entregue", event)
            finally:
                self._events.task_done()

    def _flush_events(self) -> None:
        """Espera a fila de eventos esvaziar (testes)."""
        self._events.join()

    def _deliver(self, event: str, payload: dict, tag: str, queued: float) -> None:
        window = self._window
        if window is None:
            return
        timing.set_tag(tag)
        script = f"window.robin && window.robin.{event}({json.dumps(payload)})"
        start = time.monotonic()
        try:
            window.run_js(script)
        except Exception:  # noqa: BLE001 - janela fechando
            logger.debug("Falha ao emitir %s", event, exc_info=True)
        took = timing.ms(start)
        if event != "onChunk" or took >= 500:
            timing.log("evento %s entregue à interface em %dms (%dms na fila)", event, took,
                       round((start - queued) * 1000))
        if took >= EVENT_SLOW_MS:
            logger.warning("A interface demorou %dms para receber %s", took, event)

    # ---------- nuvem (Supabase) ----------

    def _connect(self, url: str, key: str) -> None:
        client = SupabaseClient(url, key, transport=cloud_transport())
        self._cloud = SupabaseStore(self._local, client, SYNC_FILE, on_status=self._on_cloud_status)
        self._conversation_cloud = ConversationCloud(
            client, CONVERSATION_SYNC_FILE, self._conversations, self._recorder,
            on_notice=lambda message: self._emit("onCloudNotice", {"message": message, "tone": "warn"}),
            apply_remote=self._apply_remote_conversations,
        )
        self._recorder.cloud = self._conversation_cloud
        self._set_chat_store(self._cloud)
        self._start_sync(initial=True)

    def _apply_remote_conversations(self, heads, rows):
        with self._chat_lock:
            result = self._recorder.apply_remote(heads, rows)
            if result[2]:
                self._service.clear()
                self._rotate_attachment_scope()
            return result

    def _start_sync(self, initial: bool = False) -> None:
        threading.Thread(target=self._sync, args=(initial,), daemon=True).start()

    def _sync(self, initial: bool = False) -> dict:
        """Pull da nuvem (com reenvio da fila, migração, limpeza ao abrir e núcleo). Um de cada vez."""
        cloud = self._cloud
        if cloud is None or not self._sync_lock.acquire(blocking=False):
            return {"ok": False, "skipped": True}
        previous_tag = timing.tag()
        timing.set_tag("sync")
        start = time.monotonic()
        try:
            core_before = self._core_text()
            summary = cloud.pull(core_seed=_memory_md, cleanup_days=DONE_RETENTION_DAYS if initial else None)
            conversations = self._pull_conversations()
            self._last_pull = time.monotonic()
            timing.log("pull%s: %dms (%s; conversas %s)", " inicial" if initial else "", timing.ms(start), summary,
                       conversations)
        except BrainError as error:  # cópia local ilegível ou sem gravar: avisa e segue
            logger.error("Sync com o Supabase falhou: %s", error)
            self._emit("onCloudNotice", {"message": f"Sync com a nuvem falhou: {error}", "tone": "error"})
            return {"ok": False}
        finally:
            self._sync_lock.release()
            timing.set_tag(previous_tag)
        for message in cloud.take_conflicts():
            self._emit("onCloudNotice", {"message": message, "tone": "warn"})
        # Núcleo e memória nova valem já no próximo turno.
        self._service.rebuild()
        if self._core_text() != core_before:
            self._emit("onMemoryChanged", {})
        if summary.get("ok"):
            self._gemini.schedule_backfill()
            self._emit("onBrainChanged", {})  # a aba Memória recarrega a lista
        if conversations.get("current_deleted"):
            # A interface começa outra conversa (new_conversation) e avisa.
            self._emit("onConversationRemoved", {"message": "A conversa aberta foi apagada em outro PC."})
        return summary

    def _pull_conversations(self) -> dict:
        """Conversas da nuvem (fase 3): nunca derruba o pull das memórias."""
        cloud = self._conversation_cloud
        if cloud is None:
            return {}
        try:
            return cloud.pull()
        except Exception:  # noqa: BLE001 - disco, dado inesperado: fica para o próximo pull
            logger.exception("Pull das conversas falhou")
            return {"ok": False}

    def _on_summon(self) -> None:
        """A janela apareceu (atalho, bandeja, segunda execução): puxa o que mudou em outra máquina."""
        if self._cloud and time.monotonic() - self._last_pull >= PULL_MIN_INTERVAL:
            self._start_sync()

    def _cloud_status(self, status: Optional[dict] = None) -> Optional[dict]:
        """Estado da nuvem para a interface: a fila conta as memórias e as conversas."""
        if status is None:
            if self._cloud is None:
                return None
            status = self._cloud.status()
        queued = self._conversation_cloud.pending if self._conversation_cloud else 0
        return {**status, "pending": status["pending"] + queued}

    def _on_cloud_status(self, status: dict) -> None:
        status = self._cloud_status(status)
        self._emit("onCloudStatus", status)
        if status["state"] == "offline" and status["pending"] and self._retry_timer is None:
            # Sem conexão com alterações na fila: tenta de novo sozinho de tempos em tempos.
            def retry():
                self._retry_timer = None
                self._sync()

            self._retry_timer = threading.Timer(CLOUD_RETRY_SECONDS, retry)
            self._retry_timer.daemon = True
            self._retry_timer.start()

    def _cleanup_local(self) -> None:
        if DONE_RETENTION_DAYS is None:
            return  # o Robin lembra de tudo: nada concluído é apagado
        try:
            removed = self._local.cleanup_done(DONE_RETENTION_DAYS)
        except BrainError as error:
            logger.error("Limpeza das tarefas concluídas falhou: %s", error)
            return
        if removed:
            logger.info("%d tarefa(s) concluída(s) há mais de %d dias apagada(s)", removed, DONE_RETENTION_DAYS)

    def _core_text(self) -> str:
        """Núcleo do system_instruction: os itens fixos da nuvem ou, sem eles, o memory.md."""
        if self._cloud is not None:
            pinned = self._cloud.pinned_items()
            if pinned:
                return "\n\n".join(item["content"] for item in pinned)
        return _memory_md()

    def set_supabase(self, url: str, key: str = "") -> dict:
        """Valida (tabela e RPC) e grava ROBIN_SUPABASE_URL/KEY. Chave vazia reaproveita a salva."""
        url = normalize_url(url)
        key = (key or "").strip() or self._supabase_key
        if not url.startswith("https://") and not self._mock:
            return _error("invalid_url", "Use a URL do projeto: https://<projeto>.supabase.co")
        if not key:
            return _error("empty", "Informe a chave secreta do Supabase.")
        if self._service.is_generating:
            return _error("busy", "Pare a geração antes de trocar a nuvem.")
        client = SupabaseClient(url, key, transport=cloud_transport())
        try:
            client.check()
        except CloudError as error:
            client.close()
            logger.warning("Supabase recusado ao configurar: %s. %s", error, error.message)
            return _error(error.kind, error.message)
        client.close()
        write_env(ENV_SUPABASE_URL, url)
        write_env(ENV_SUPABASE_KEY, key)
        self._supabase_key = key
        self._close_cloud()
        self._connect(url, key)
        return {"ok": True, **self.get_settings()}

    def disconnect_supabase(self) -> dict:
        """Volta a usar só o brain.json e o memory.md. A cópia local e a fila (sync.json) ficam."""
        if self._service.is_generating:
            return _error("busy", "Pare a geração antes de desconectar a nuvem.")
        write_env(ENV_SUPABASE_URL, "")
        write_env(ENV_SUPABASE_KEY, "")
        self._supabase_key = ""
        self._close_cloud()
        self._set_chat_store(self._local)
        self._emit("onMemoryChanged", {})
        return {"ok": True, **self.get_settings()}

    def sync_now(self) -> dict:
        """Puxar da nuvem agora (botão em Configurações)."""
        if self._cloud is None:
            return _error("no_cloud", "O Supabase não está configurado.")
        summary = self._sync()
        if summary.get("skipped"):
            return {"ok": True, "cloud": self._cloud_status()}
        status = self._cloud_status()
        return {"ok": bool(summary.get("ok")), "cloud": status,
                **({} if summary.get("ok") else {"error": status["message"] or "Nuvem indisponível."})}

    def _close_cloud(self) -> None:
        if self._retry_timer:
            self._retry_timer.cancel()
            self._retry_timer = None
        self._recorder.cloud = None
        self._conversation_cloud = None
        if self._cloud is not None:
            self._cloud.client.close()
            self._cloud = None

    # ---------- estado ----------

    def get_state(self) -> dict:
        folder = self._selected_folder()
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "model": self._service.model,
            "provider": self._provider,
            "attachments": {**attachment_limits(), "context": self._attachment_context()},
            "project_folder": {"id": folder[0], "name": folder[2].name} if folder else None,
            "folder_picker": self._window is not None and os.name == "nt",
            "has_key": self._chat_connection_error() is None,
            "can_regenerate": self._can_regenerate(),
            "mock": self._mock,
            "frame": self._frame.mode if self._frame else "native",
            "maximized": bool(self._frame and self._frame.is_maximized()),
            "debug": DEBUG,
            "cloud": self._cloud_status(),
        }

    # ---------- chat ----------

    def pick_chat_folder(self, attachment_context: str) -> dict:
        from webview import FileDialog

        with self._chat_lock:
            if self._window is None or os.name != "nt":
                return _error("unavailable", "O seletor de pasta está disponível no app do Windows.")
            if self._service.is_generating:
                return _error("busy", "Aguarde a resposta atual ou pare a geração.")
            if attachment_context != self._attachment_context():
                return _error("folder_context", "A conversa ou o provedor mudou. Escolha a pasta novamente.")
        try:
            paths = self._window.create_file_dialog(FileDialog.FOLDER, allow_multiple=False)
            folder = ProjectFolder(paths[0], protected=(DATA_ROOT, DATA_DIR, WEBVIEW_DATA_DIR)) if paths else None
        except (OSError, ValueError):
            return _error("folder", "Não foi possível abrir essa pasta local para leitura.")
        with self._chat_lock:
            if attachment_context != self._attachment_context() or self._service.is_generating:
                return _error("folder_context", "O contexto mudou. Escolha a pasta novamente.")
            if folder is not None:
                self._folder = (secrets.token_hex(16), attachment_context, folder)
            return {"ok": True, "state": self.get_state()}

    def remove_chat_folder(self, folder_id: str) -> dict:
        with self._chat_lock:
            if self._service.is_generating:
                return _error("busy", "Aguarde a resposta atual ou pare a geração.")
            folder = self._selected_folder()
            if folder and folder[0] == folder_id:
                self._folder = None
            return {"ok": True, "state": self.get_state()}

    def send_message(self, text: str, attachments=None, attachment_context: str = "", folder_id: str = "") -> dict:
        with self._chat_lock:
            if not isinstance(text, str):
                return _error("empty", "Mensagem inválida.")
            text = text.strip()
            if attachments and attachment_context != self._attachment_context():
                return _error("attachment_context", "A conversa ou o provedor mudou. Selecione os arquivos novamente.")
            selection = self._selected_folder()
            if folder_id and (not selection or folder_id != selection[0] or attachment_context != selection[1]):
                return _error("folder_context", "A pasta não está disponível nesta conversa. Escolha a pasta novamente.")
            folder = selection[2] if folder_id else None
            try:
                files = text_attachments(attachments)
            except ValueError as error:
                return _error("attachment", str(error))
            if not text:
                if not files:
                    return _error("empty", "Mensagem vazia.")
                text = "Analise os arquivos anexados."
            if error := self._chat_connection_error():
                return error
            if self._service.is_generating:
                return _error("busy", "Aguarde a resposta atual ou pare a geração.")
            gen_id = next(self._gen_ids)
            # Antes do envio: os eventos da geração podem chegar antes de send() voltar.
            turn = self._recorder.begin(gen_id, text, identity=self._identity(),
                                        attachments=[file["name"] for file in files], folder=folder is not None)
            try:
                self._service.send(message_with_attachments(text, files), gen_id,
                                   memory_read_only=bool(files), folder=folder)
            except GeminiError as error:
                self._recorder.abort(gen_id)
                return _error(error.code, error.message)
            except Exception as error:  # noqa: BLE001 - montar a sessão falhou: a interface sai de "pensando"
                self._recorder.abort(gen_id)
                logger.exception("send_message falhou")
                return _error("internal", f"Falha interna ao enviar: {type(error).__name__}. Detalhes no robin.log.")
            self._attachment_turn = turn if files or folder else None
            self._last_folder_id = folder_id
            return {"ok": True, "gen_id": gen_id}

    def stop_generation(self) -> dict:
        with self._chat_lock:
            return {"ok": self._service.stop()}

    def _can_regenerate(self) -> bool:
        turn = self._recorder.last_external_context_turn
        folder = self._selected_folder()
        return not turn or (turn == self._attachment_turn
                            and (not self._last_folder_id or bool(folder and folder[0] == self._last_folder_id)))

    def regenerate(self) -> dict:
        with self._chat_lock:
            if not self._can_regenerate():
                return _error("reattach", "Selecione os anexos ou a pasta e envie uma nova mensagem.")
            if error := self._chat_connection_error():
                return error
            if self._service.is_generating:
                return _error("busy", "Aguarde a resposta atual ou pare a geração.")
            gen_id = next(self._gen_ids)
            self._recorder.begin_regenerate(gen_id)
            try:
                folder = self._selected_folder()
                self._service.regenerate(gen_id, folder=folder[2] if self._last_folder_id and folder else None)
            except GeminiError as error:
                self._recorder.abort(gen_id)
                return _error(error.code, error.message)
            except Exception as error:  # noqa: BLE001 - idem send_message
                self._recorder.abort(gen_id)
                logger.exception("regenerate falhou")
                return _error("internal", f"Falha interna ao regenerar: {type(error).__name__}. Detalhes no robin.log.")
            return {"ok": True, "gen_id": gen_id}

    # ---------- conversas guardadas ----------

    def _conversations_changed(self) -> None:
        self._emit("onConversationsChanged", {})

    def list_conversations(self, query: str = "") -> dict:
        """Conversas mais recentes primeiro (filtradas por `query`) e a atual."""
        return {"ok": True, "current": self._recorder.current_id, "items": self._conversations.summaries(query or "")}

    def new_conversation(self) -> dict:
        """Conversa nova: a sessão fica vazia e a anterior continua guardada."""
        with self._chat_lock:
            self._service.clear()
            self._recorder.new()
            self._rotate_attachment_scope()
            self._select_provider(self._default_provider())
            return {"ok": True, "state": self.get_state()}

    def open_conversation(self, conversation_id: str) -> dict:
        """Continua uma conversa guardada: a sessão recebe o histórico em texto (as mais recentes,
        até MAX_REOPEN_HISTORY_CHARS) e as próximas mensagens entram nela."""
        with self._chat_lock:
            conversation_id = str(conversation_id or "")
            if conversation_id and conversation_id == self._recorder.current_id:
                return {"ok": True, "conversation": self._conversations.get(conversation_id), "current": True}
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                return _error("not_found", "Essa conversa não existe mais.")
            self._service.clear()
            self._select_provider(conversation_identity(conversation)[0])
            self._service.load_history(history_for_session(conversation, MAX_REOPEN_HISTORY_CHARS))
            self._recorder.open(conversation)
            self._rotate_attachment_scope()
            return {"ok": True, "conversation": conversation, "state": self.get_state(),
                    "can_regenerate": self._can_regenerate()}

    def delete_conversation(self, conversation_id: str) -> dict:
        """Apaga do PC (e, com a nuvem, dos outros PCs). Se for a atual, a conversa recomeça vazia."""
        with self._chat_lock:
            conversation_id = str(conversation_id or "")
            was_current = conversation_id == self._recorder.current_id
            if was_current:
                self._service.clear()
                self._rotate_attachment_scope()
            try:
                deleted = self._recorder.delete(conversation_id)
            except OSError as error:
                logger.error("Conversa não apagada (%s): %s", conversation_id, error)
                return _error("io", f"Não foi possível apagar a conversa: {error}")
            if not deleted and not was_current:
                return _error("not_found", "Essa conversa não existe mais.")
            return {"ok": True, "was_current": was_current}

    # ---------- o que o Robin guardou (aba Memória) ----------

    def _brain(self):
        return self._cloud or self._local

    def list_brain(self) -> dict:
        """Tudo o que o Robin guardou, menos o núcleo, mais recente primeiro."""
        try:
            items = [item for item in self._brain().snapshot() if not item.get("pinned")]
        except BrainError as error:
            return _error("brain", str(error))
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return {"ok": True, "items": [_brain_item(item) for item in items]}

    def update_brain_item(self, item_id: str, content: str) -> dict:
        content = (content or "").strip()
        if not content:
            return _error("empty", "O texto não pode ficar vazio.")
        return self._change_brain(item_id, lambda store, key: store.update(key, content=content))

    def complete_brain_item(self, item_id: str) -> dict:
        return self._change_brain(item_id, lambda store, key: store.complete(key))

    def delete_brain_item(self, item_id: str) -> dict:
        return self._change_brain(item_id, lambda store, key: store.delete(key))

    def _change_brain(self, item_id: str, change) -> dict:
        store = self._brain()
        key = str(item_id or "")
        item = store.get(key)
        if item is None:
            return _error("not_found", "Isso não está mais na memória.")
        if item.get("pinned"):
            return _error("pinned", "Isso é do núcleo: edite na aba Núcleo.")
        try:
            changed = change(store, key)
        except StaleError as error:
            return {**_error("stale", "Isso mudou em outro PC: a lista foi atualizada."), "item": _brain_item(error.current)}
        except BrainError as error:
            return _error("brain", str(error))
        self._gemini.schedule_backfill()  # texto novo fica sem vetor até o backfill
        return {"ok": True, "item": _brain_item(changed), "sync": changed.get("sync")}

    # ---------- modelo e chave ----------

    def get_models(self) -> dict:
        service = self._service
        return {
            "models": service.list_models(),
            "current": service.model,
            "no_quota": service.no_quota_models,
        }

    def set_model(self, name: str) -> dict:
        with self._chat_lock:
            name = (name or "").strip()
            if not name:
                return _error("empty", "Modelo inválido.")
            if self._service.is_generating:
                return _error("busy", "Pare a geração antes de trocar o modelo.")
            if self._provider == "chatgpt" and name not in self._service.list_models():
                return _error("model_unavailable", "Esse modelo não está disponível no ChatGPT.")
            self._service.set_model(name)
            if self._provider == "chatgpt":
                self._chatgpt_model = name
            write_env(ENV_CHATGPT_MODEL if self._provider == "chatgpt" else ENV_MODEL, name)
            return {"ok": True, "model": name}

    def set_api_key(self, key: str) -> dict:
        with self._chat_lock:
            key = (key or "").strip()
            if not key:
                return _error("empty", "Informe a API Key.")
            if self._service.is_generating:
                return _error("busy", "Pare a geração antes de trocar a chave.")
            error = None if self._mock else validate_api_key(key)  # a simulação não acessa a rede
            if error:
                return _error(error.code, error.message)
            write_env(ENV_API_KEY, key)
            changed = self._api_key != key
            self._api_key = key
            self._gemini.configure(key)
            if changed and self._provider == "gemini":
                self._rotate_attachment_scope()
                if self._gemini.has_attachment_context:
                    current = self._conversations.get(self._recorder.current_id)
                    self._gemini.load_history(history_for_session(current, MAX_REOPEN_HISTORY_CHARS) if current else [])
            if self._recorder.current_id is None:
                self._select_provider(self._default_provider())
            return {"ok": True, "key_hint": _key_hint(key), "state": self.get_state()}

    # ---------- memória ----------

    def get_memory(self) -> dict:
        """Com o Supabase, o editor mostra o núcleo (item fixo); antes do primeiro pull, o memory.md."""
        core = self._cloud.core_item() if self._cloud else None
        if core is not None:
            return {"ok": True, "content": core["content"], "source": "cloud"}
        try:
            return {"ok": True, "content": read_memory(), "source": "local"}
        except (OSError, ValueError) as error:  # ValueError: memory.md fora de UTF-8
            logger.error("get_memory falhou: %s: %s", type(error).__name__, error)
            return _error("io", f"Não foi possível ler o memory.md: {error}")

    def save_memory(self, content: str, confirm_empty: bool = False) -> dict:
        """Salva, recria a sessão mantendo o histórico e, só se o sync estiver ligado e a pasta for
        repositório, sincroniza com o Git em segundo plano (status via onMemoryStatus).
        Conteúdo vazio sobre um memory.md com conteúdo só passa com confirm_empty=True
        (a interface envia depois de uma confirmação inline)."""
        content = content or ""
        if self._cloud is not None:
            return self._save_core(content, confirm_empty)
        try:
            if not content.strip() and not confirm_empty and not memory_is_blank():
                return _error("confirm_empty", "A memória ficaria vazia. Confirme para apagar o conteúdo do memory.md.")
            write_memory(content)
        except (OSError, ValueError) as error:
            logger.error("save_memory falhou: %s: %s", type(error).__name__, error)
            return _error("io", f"Não foi possível salvar o memory.md: {error}")
        self._service.rebuild()

        if read_env().get(ENV_GIT_SYNC) == "1" and git_available():  # só sem o Supabase
            threading.Thread(target=self._sync_git, daemon=True).start()
            return {"ok": True, "sync": "git"}
        return {"ok": True, "sync": "local"}

    def _save_core(self, content: str, confirm_empty: bool) -> dict:
        core = self._cloud.core_item()
        if core is None:
            return _error("not_synced", "Aguarde a primeira sincronização com a nuvem antes de editar o núcleo.")
        if not content.strip() and not confirm_empty and core["content"].strip():
            return _error("confirm_empty", "O núcleo ficaria vazio. Confirme para apagar o conteúdo.")
        try:
            item = self._cloud.save_core(content)
        except StaleError:
            self._emit("onMemoryChanged", {"keep_editor": True})
            return _error("stale", "O núcleo mudou em outra máquina e nada foi salvo. Recarregue para ver a "
                                   "versão nova (suas alterações continuam no editor até lá).")
        except BrainError as error:
            return _error("cloud", str(error))
        self._service.rebuild()
        return {"ok": True, "sync": "pending" if item.get("sync") == "pending" else "cloud"}

    def _sync_git(self) -> None:
        self._emit("onMemoryStatus", {"status": "git_syncing"})
        ok, detail = sync_git()
        if ok:
            self._emit("onMemoryStatus", {"status": "git_ok"})
        else:
            logger.warning("Sync Git: %s", detail)
            self._emit("onMemoryStatus", {"status": "git_warn", "detail": detail})

    # ---------- configurações ----------

    def get_settings(self) -> dict:
        env = read_env()
        return {
            "git_sync": env.get(ENV_GIT_SYNC) == "1" and self._cloud is None,
            "git_available": git_available(),
            "key_hint": _key_hint(self._api_key),
            "chatgpt_enabled": not self._mock,
            "chatgpt": self._chatgpt_account.status(),
            "preferred_provider": self._preferred_provider,
            "data_dir": str(DATA_DIR),
            "supabase_url": env.get(ENV_SUPABASE_URL, "").strip(),
            "supabase_key_hint": _key_hint(self._supabase_key),
            "cloud": self._cloud_status(),
            "build": self._build,
            "theme": theme_preference(),
            "palette": palette_preference(),
            **self._desktop_settings(),
        }

    def _desktop_settings(self) -> dict:
        if self._desktop:
            return self._desktop.settings()
        return {"hotkey": DEFAULT_HOTKEY, "hotkey_error": "", "autostart": False, "autostart_available": False}

    def set_settings(self, settings: dict) -> dict:
        settings = settings or {}
        if "git_sync" in settings:
            enable = bool(settings["git_sync"])
            if enable and self._cloud is not None:
                return _error("cloud", "Com o Supabase configurado, a memória já sincroniza pela nuvem.")
            if enable and not git_available():
                return _error("no_git", "A pasta de dados não é um repositório Git.")
            write_env(ENV_GIT_SYNC, "1" if enable else "0")
        if "autostart" in settings:
            if not self._desktop:
                return _error("no_autostart", "Iniciar com o Windows só vale para o Robin.exe.")
            result = self._desktop.set_autostart(bool(settings["autostart"]))
            if not result["ok"]:
                return result
        return {"ok": True, **self.get_settings()}

    def set_theme(self, preference: str, dark: Optional[bool] = None) -> dict:
        """Tema escolhido na interface (system, light ou dark), gravado como ROBIN_THEME no .env para a
        próxima abertura. `dark`: o tema que a página mostra agora (com "system", o do Windows); a
        moldura e o fundo da janela acompanham."""
        preference = str(preference or "").strip().lower()
        if preference not in THEMES:
            return _error("invalid_theme", "Tema inválido: use system, light ou dark.")
        if read_env().get(ENV_THEME, "") != preference:
            write_env(ENV_THEME, preference)
        if self._frame and dark is not None:
            self._frame.set_theme("dark" if dark else "light")
        return {"ok": True, "theme": preference}

    def set_palette(self, palette: str) -> dict:
        """Paleta de cores escolhida na interface, gravada como ROBIN_PALETTE no .env; a moldura e o
        fundo da janela acompanham."""
        palette = str(palette or "").strip().lower()
        if palette not in PALETTES:
            return _error("invalid_palette", f"Paleta inválida: use {', '.join(PALETTES)}.")
        if read_env().get(ENV_PALETTE, "") != palette:
            write_env(ENV_PALETTE, palette)
        if self._frame:
            self._frame.set_palette(palette)
        return {"ok": True, "palette": palette}

    def set_hotkey(self, text: str) -> dict:
        """Troca o atalho global (gravado como ROBIN_HOTKEY no .env)."""
        if not self._desktop:
            return _error("no_desktop", "O atalho global não está disponível.")
        return self._desktop.set_hotkey(text or "")

    # ---------- janela ----------

    def window_minimize(self) -> None:
        if self._frame:
            self._frame.minimize()

    def window_maximize(self) -> dict:
        return {"maximized": self._frame.toggle_maximize() if self._frame else False}

    def window_close(self) -> None:
        """O X esconde para a bandeja; quem encerra é o "Sair" da bandeja."""
        if self._desktop:
            self._desktop.hide_to_tray(hint=True)
        elif self._window:
            self._window.destroy()

    def window_start_drag(self) -> None:
        if self._frame:
            self._frame.start_drag()

    def window_start_resize(self, edge: str) -> None:
        if self._frame:
            self._frame.start_resize(edge)

    # ---------- utilidades ----------

    def log_debug(self, message: str) -> None:
        """Relatórios de depuração da interface (ex.: medição de fluidez). Só com ROBIN_DEBUG=1."""
        if DEBUG:
            print(f"[ui] {message}", flush=True)

    def copy_text(self, text: str) -> dict:
        """Reserva do botão copiar quando a WebView nega navigator.clipboard."""
        return {"ok": set_clipboard_text(text or "")}

    def open_data_dir(self) -> dict:
        """Abre a pasta de dados (memory.md, .env) no Explorador."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(DATA_DIR)  # noqa: S606 - pasta do próprio app
        except OSError as error:
            logger.warning("Não foi possível abrir a pasta de dados: %s", error)
            return _error("io", f"Não foi possível abrir a pasta de dados: {error}")
        return {"ok": True}

    def open_external(self, url: str) -> dict:
        """Abre links http/https no navegador do sistema (o WebView nunca sai do app)."""
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return _error("invalid_url", "Link não suportado.")
        webbrowser.open(url)
        return {"ok": True}


def _rate_limit(env: dict) -> Optional[int]:
    """ROBIN_RPM do .env (pedidos por minuto por modelo; 0 desliga). Vazio ou inválido: o padrão."""
    try:
        return max(0, int(env.get(ENV_RPM, "").strip()))
    except ValueError:
        return None


def _memory_md() -> str:
    """memory.md para o núcleo (e a semente do núcleo na nuvem). Ilegível: vazio, com o erro no log."""
    try:
        return read_memory()
    except (OSError, ValueError) as error:
        logger.error("memory.md ilegível: %s: %s", type(error).__name__, error)
        return ""


def _brain_item(item: dict) -> dict:
    """O que a aba Memória mostra de cada coisa guardada (sem o vetor)."""
    keys = ("id", "content", "tags", "status", "created_at", "updated_at", "done_at", "sync")
    return {key: item.get(key) for key in keys}
