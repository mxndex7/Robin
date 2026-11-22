"""Robin sempre no PC: atalho global, bandeja, X que esconde, Sair, pedidos de outra execução e
iniciar com o Windows. Liga a janela (WindowFrame) à janela de mensagens (instance.py) e ao ícone
da bandeja (tray.py)."""

import logging
import threading
from typing import Any, Callable, Optional

from . import config, startup
from .config import ENV_AUTOSTART, ENV_HOTKEY, ENV_TRAY_HINT, read_env, write_env
from .hotkey import DEFAULT_HOTKEY, Hotkey, parse_hotkey
from .instance import MessageWindow
from .tray import Tray

logger = logging.getLogger("robin.desktop")

# Clicar na bandeja tira o foco da janela antes de o clique chegar: se ela estava na frente até
# este tempo atrás, o clique esconde em vez de trazer para a frente.
TRAY_GRACE_SECONDS = 0.6


def _error(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "error": message}


class Desktop:
    def __init__(
        self,
        window: Any,
        frame: Any,
        emit: Callable[[str, dict], None],
        key: str,
        messages: Optional[MessageWindow] = None,
        tray: Optional[Tray] = None,
        on_summon: Optional[Callable[[], None]] = None,
    ):
        self._window = window
        self._on_summon = on_summon  # a janela apareceu: a API puxa a memória da nuvem
        self._frame = frame
        self._emit = emit
        self._quitting = False
        self._hotkey: Hotkey = parse_hotkey(DEFAULT_HOTKEY)
        self._hotkey_error = ""
        self._messages = messages or MessageWindow(key, on_hotkey=self.toggle, on_show=self.summon, on_quit=self.quit)
        self._tray = tray or Tray(
            config.ICON_FILE,
            on_click=lambda: self.toggle(TRAY_GRACE_SECONDS),
            on_open=self.summon,
            on_quit=self.quit,
            autostart_state=self.autostart_enabled,
            on_autostart=self.set_autostart,
        )

    def start(self) -> None:
        """Na thread da interface, antes do primeiro Show() (evento before_show)."""
        self._messages.start()
        self._load_hotkey()
        self._tray.install(self._tooltip())
        form = self._window.native
        form.FormClosing += self._on_form_closing
        form.FormClosed += lambda *_: self._tray.dispose()
        self._apply_autostart_default()

    # ---------- janela ----------

    def summon(self) -> None:
        """Mostra, restaura, traz para a frente e foca o composer."""
        self._frame.bring_to_front()
        self._emit("onSummon", {})
        if self._on_summon:
            try:
                self._on_summon()
            except Exception:  # noqa: BLE001 - o pull nunca impede a janela de aparecer
                logger.warning("on_summon falhou", exc_info=True)

    def toggle(self, grace: float = 0.0) -> None:
        """Atalho e clique na bandeja: na frente, esconde; escondida, minimizada ou atrás, aparece."""
        if self._frame.is_in_front(grace):
            self._frame.hide()
        else:
            self.summon()

    def hide_to_tray(self, hint: bool = False) -> None:
        """X da barra de título e Alt+F4. Na primeira vez, avisa que o Robin continua na bandeja."""
        self._frame.hide()
        if not hint or read_env().get(ENV_TRAY_HINT) == "1":
            return
        opener = "Clique no ícone para abrir" if self._hotkey_error else f"{self._hotkey.text} abre"
        self._frame.run_on_ui(lambda: self._tray.show_hint(f"O Robin continua na bandeja. {opener}."), wait=False)
        try:
            write_env(ENV_TRAY_HINT, "1")
        except OSError:
            logger.warning("Não foi possível gravar %s no .env", ENV_TRAY_HINT, exc_info=True)

    def quit(self) -> None:
        """Sair (bandeja ou --quit): fecha de verdade."""
        if self._quitting:
            return
        self._quitting = True
        self._frame.run_on_ui(self._tray.dispose, wait=False)
        # Fora da thread que pediu (menu da bandeja, janela de mensagens): o destroy espera a interface.
        threading.Thread(target=self._window.destroy, daemon=True).start()

    def close_requested(self, reason: str) -> bool:
        """Decide um fechamento da janela. True cancela (e esconde). Só o usuário fechando (X, Alt+F4,
        "Fechar janela" da barra de tarefas) esconde; Sair, desligar o Windows e o Gerenciador de
        Tarefas fecham de verdade."""
        if self._quitting or reason != "UserClosing":
            return False
        self._frame.run_on_ui(lambda: self.hide_to_tray(hint=True), wait=False)
        return True

    def _on_form_closing(self, _sender, args) -> None:
        if self.close_requested(str(args.CloseReason)):
            args.Cancel = True

    # ---------- atalho global ----------

    def _load_hotkey(self) -> None:
        text = read_env().get(ENV_HOTKEY, "").strip() or DEFAULT_HOTKEY
        invalid = ""
        try:
            hotkey = parse_hotkey(text)
        except ValueError as error:
            invalid = f"{ENV_HOTKEY}={text} é inválido: {error} Usando {DEFAULT_HOTKEY}."
            logger.warning("Atalho global: %s", invalid)
            hotkey = parse_hotkey(DEFAULT_HOTKEY)
        error = self._messages.register(hotkey)
        if error:
            logger.warning("Atalho global: %s", error)
        self._hotkey = hotkey
        self._hotkey_error = error or invalid

    def set_hotkey(self, text: str) -> dict:
        try:
            hotkey = parse_hotkey(text)
        except ValueError as error:
            return _error("invalid_hotkey", str(error))
        error = self._messages.register(hotkey)
        if error:
            logger.warning("Atalho global: %s", error)
            return _error("hotkey_busy", error)
        self._hotkey, self._hotkey_error = hotkey, ""
        try:
            write_env(ENV_HOTKEY, hotkey.text)
        except OSError as error:
            logger.warning("Não foi possível gravar %s no .env: %s", ENV_HOTKEY, error)
        self._frame.run_on_ui(lambda: self._tray.set_tooltip(self._tooltip()), wait=False)
        return {"ok": True, "hotkey": hotkey.text}

    def _tooltip(self) -> str:
        return "Robin" if self._hotkey_error else f"Robin ({self._hotkey.text})"

    # ---------- iniciar com o Windows ----------

    @property
    def autostart_available(self) -> bool:
        return config.EXE_FILE is not None  # só o exe se registra, nunca o código-fonte

    def autostart_enabled(self) -> Optional[bool]:
        """None quando indisponível (código-fonte)."""
        if not self.autostart_available:
            return None
        try:
            return startup.is_enabled(config.EXE_FILE)
        except OSError:
            logger.warning("Não foi possível ler a chave Run", exc_info=True)
            return False

    def set_autostart(self, enabled: bool) -> dict:
        if not self.autostart_available:
            return _error("no_autostart", "Iniciar com o Windows só vale para o Robin.exe.")
        try:
            startup.set_enabled(config.EXE_FILE, enabled)
            write_env(ENV_AUTOSTART, "1" if enabled else "0")
        except OSError as error:
            logger.warning("Iniciar com o Windows: %s", error)
            return _error("io", f"Não foi possível alterar a inicialização: {error}")
        return {"ok": True}

    def _apply_autostart_default(self) -> None:
        """Primeira execução do exe: liga. Depois, só corrige o caminho se o exe mudou de pasta.
        Uma cópia de teste (ROBIN_DATA_DIR) ou a simulação nunca mexem no registro sozinhas."""
        if not self.autostart_available or config.MOCK or config.DATA_DIR_OVERRIDDEN:
            return
        choice = read_env().get(ENV_AUTOSTART, "").strip()
        try:
            applied = startup.apply_default(config.EXE_FILE, choice)
            if applied != choice:
                write_env(ENV_AUTOSTART, applied)
                logger.info("Iniciar com o Windows ligado na primeira execução do exe.")
        except OSError:
            logger.warning("Não foi possível aplicar o padrão de iniciar com o Windows", exc_info=True)

    # ---------- configurações ----------

    def settings(self) -> dict:
        return {
            "hotkey": self._hotkey.text,
            "hotkey_error": self._hotkey_error,
            "autostart": bool(self.autostart_enabled()),
            "autostart_available": self.autostart_available,
        }
