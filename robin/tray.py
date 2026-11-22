"""Ícone na bandeja (NotifyIcon do WinForms). Criado e usado só na thread da interface.

Clique esquerdo abre/esconde; menu: Abrir Robin, Iniciar com o Windows (marcado conforme o registro,
lido a cada abertura do menu) e Sair.
"""

import time
from pathlib import Path
from typing import Any, Callable, Optional

TOOLTIP_LIMIT = 63  # NotifyIcon.Text recusa textos maiores


class Tray:
    def __init__(
        self,
        icon_path: Path,
        on_click: Callable[[], None],
        on_open: Callable[[], None],
        on_quit: Callable[[], None],
        autostart_state: Callable[[], Optional[bool]],
        on_autostart: Callable[[bool], Any],
    ):
        self._icon_path = icon_path
        self._on_click = on_click
        self._on_open = on_open
        self._on_quit = on_quit
        self._autostart_state = autostart_state  # None: indisponível (código-fonte)
        self._on_autostart = on_autostart
        self._icon: Any = None
        self._autostart_item: Any = None
        self._last_click = 0.0

    def install(self, tooltip: str) -> None:
        from System.Drawing import Font, FontStyle, Icon
        from System.Windows.Forms import ContextMenuStrip, NotifyIcon, ToolStripMenuItem, ToolStripSeparator

        open_item = ToolStripMenuItem("Abrir Robin")
        open_item.Font = Font(open_item.Font, FontStyle.Bold)  # ação padrão, como o clique
        open_item.Click += lambda *_: self._on_open()
        self._autostart_item = ToolStripMenuItem("Iniciar com o Windows")
        self._autostart_item.Click += lambda *_: self._toggle_autostart()
        quit_item = ToolStripMenuItem("Sair")
        quit_item.Click += lambda *_: self._on_quit()

        menu = ContextMenuStrip()
        for item in (open_item, self._autostart_item, ToolStripSeparator(), quit_item):
            menu.Items.Add(item)
        menu.Opening += lambda *_: self._refresh_autostart()

        self._icon = NotifyIcon()
        self._icon.Icon = Icon(str(self._icon_path))
        self._icon.ContextMenuStrip = menu
        self._icon.MouseClick += self._mouse_click
        self.set_tooltip(tooltip)
        self._icon.Visible = True

    def set_tooltip(self, text: str) -> None:
        if self._icon is not None:
            self._icon.Text = text[:TOOLTIP_LIMIT]

    def show_hint(self, text: str) -> None:
        """Notificação do Windows saindo do ícone da bandeja."""
        if self._icon is None:
            return
        from System.Windows.Forms import ToolTipIcon

        self._icon.ShowBalloonTip(5000, "Robin", text, getattr(ToolTipIcon, "None"))

    def dispose(self) -> None:
        """Some com o ícone na hora (sem isso ele fica na bandeja até o mouse passar por cima)."""
        if self._icon is None:
            return
        self._icon.Visible = False
        self._icon.Dispose()
        self._icon = None

    # ---------- eventos ----------

    def _mouse_click(self, _sender, event) -> None:
        from System.Windows.Forms import MouseButtons, SystemInformation

        if event.Button != MouseButtons.Left:
            return
        now = time.monotonic()
        double = now - self._last_click < SystemInformation.DoubleClickTime / 1000
        self._last_click = now
        # Duplo clique por hábito não pode abrir e esconder em seguida: o segundo clique só garante aberto.
        (self._on_open if double else self._on_click)()

    def _refresh_autostart(self) -> None:
        state = self._autostart_state()
        self._autostart_item.Enabled = state is not None
        self._autostart_item.Checked = bool(state)
        self._autostart_item.Text = "Iniciar com o Windows" if state is not None else "Iniciar com o Windows (só no Robin.exe)"

    def _toggle_autostart(self) -> None:
        state = self._autostart_state()
        if state is not None:
            self._on_autostart(not state)
