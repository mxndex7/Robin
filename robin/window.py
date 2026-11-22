"""Moldura da janela no Windows (Win32 + DWM via ctypes).

Modo "custom": a janela nasce como uma janela redimensionável normal e perde só a barra
de título, tratando WM_NCCALCSIZE. As bordas laterais e de baixo (invisíveis no Win11)
continuam nativas, então redimensionar, snap, Win+setas e as animações de minimizar e
restaurar seguem do sistema. O arraste usa `app-region: drag` do WebView2: o que ele
deixa passar no WM_NCHITTEST vira HTCAPTION (ou HTTOP*, na faixa de cima da janela).
Se o WebView2 não deixar passar, a interface chama start_drag/start_resize, que pedem
ao Windows o mesmo arraste nativo via WM_NCLBUTTONDOWN.

Modo "native" (fallback): moldura do sistema com a barra pintada na cor do tema via DWM.
Nos dois modos, a moldura e o fundo da janela seguem o tema e a paleta da interface (set_theme,
set_palette).
"""

import ctypes
import logging
import time
from ctypes import wintypes
from typing import Any

from .config import PALETTES, WINDOW_COLORS

logger = logging.getLogger("robin.window")

# Instâncias próprias: argtypes definidos aqui não afetam o windll usado pelo pywebview.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi")

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
UINT_PTR = ctypes.c_size_t
DWORD_PTR = ctypes.c_size_t
HWND = wintypes.HWND
UINT = wintypes.UINT

SUBCLASSPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM, UINT_PTR, DWORD_PTR)

_comctl32.SetWindowSubclass.argtypes = [HWND, SUBCLASSPROC, UINT_PTR, DWORD_PTR]
_comctl32.SetWindowSubclass.restype = wintypes.BOOL
_comctl32.RemoveWindowSubclass.argtypes = [HWND, SUBCLASSPROC, UINT_PTR]
_comctl32.RemoveWindowSubclass.restype = wintypes.BOOL
_comctl32.DefSubclassProc.argtypes = [HWND, UINT, WPARAM, LPARAM]
_comctl32.DefSubclassProc.restype = LRESULT

_user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
_user32.IsZoomed.argtypes = [HWND]
_user32.IsIconic.argtypes = [HWND]
_user32.GetDpiForWindow.argtypes = [HWND]
_user32.GetDpiForWindow.restype = UINT
_user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, UINT]
_user32.ReleaseCapture.argtypes = []
_user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
_user32.ShowWindowAsync.argtypes = [HWND, ctypes.c_int]
_user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, UINT]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.IsWindowVisible.argtypes = [HWND]
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = HWND
_user32.GetAncestor.argtypes = [HWND, UINT]
_user32.GetAncestor.restype = HWND
_user32.SetForegroundWindow.argtypes = [HWND]
_user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
_dwmapi.DwmSetWindowAttribute.argtypes = [HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

WM_WINDOWPOSCHANGING = 0x0046
WM_WINDOWPOSCHANGED = 0x0047
WM_NCDESTROY = 0x0082
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1

HTCLIENT, HTCAPTION = 1, 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = range(10, 18)
EDGE_HITS = {
    "left": HTLEFT, "right": HTRIGHT, "top": HTTOP, "bottom": HTBOTTOM,
    "top-left": HTTOPLEFT, "top-right": HTTOPRIGHT,
    "bottom-left": HTBOTTOMLEFT, "bottom-right": HTBOTTOMRIGHT,
}

SM_CXFRAME, SM_CYFRAME, SM_CXPADDEDBORDER = 32, 33, 92
GA_ROOTOWNER = 3
VK_LBUTTON = 0x01
SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x10, 0x20

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_ROUND = 2
DWMSBT_NONE = 1

# Faixa de redimensionamento no topo e largura dos cantos superiores (px lógicos).
RESIZE_BAND = 6
CORNER_SIZE = 16


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class WINDOWPOS(ctypes.Structure):
    _fields_ = [
        ("hwnd", HWND), ("hwndInsertAfter", HWND),
        ("x", ctypes.c_int), ("y", ctypes.c_int), ("cx", ctypes.c_int), ("cy", ctypes.c_int),
        ("flags", UINT),
    ]


def _colorref(hex_color: str) -> int:
    value = hex_color.lstrip("#")
    r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return r | (g << 8) | (b << 16)


def _set_dwm(hwnd: int, attribute: int, value: int) -> None:
    data = ctypes.c_int(value)
    _dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))


class WindowFrame:
    """Moldura de uma janela pywebview. install() deve rodar na thread da interface."""

    def __init__(self, window: Any, mode: str, theme: str = "dark", palette: str = PALETTES[0]):
        self._window = window
        self._mode = mode
        self._theme = theme if theme in ("light", "dark") else "dark"
        self._palette = palette if palette in WINDOW_COLORS else PALETTES[0]
        self._form = window.native
        self._hwnd = int(self._form.Handle.ToInt64())
        self._proc = SUBCLASSPROC(self._subclass_proc)  # referência mantida viva de propósito
        self._placement = "normal"
        self._restoring = False
        self._deactivated_at = 0.0  # quando a janela perdeu o foco pela última vez

    @property
    def mode(self) -> str:
        return self._mode

    # ---------- instalação ----------

    def install(self) -> None:
        # Nos dois modos: o bloqueio de drop vale também na moldura nativa.
        self._form.browser.webview.CoreWebView2InitializationCompleted += self._on_webview_ready
        self._form.Deactivate += self._on_deactivate
        self._install_frame()

    def _install_frame(self) -> None:
        self._apply_theme()
        _set_dwm(self._hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)
        _set_dwm(self._hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)

        if self._mode == "native":
            return

        if not _comctl32.SetWindowSubclass(self._hwnd, self._proc, 1, 0):
            logger.error("SetWindowSubclass falhou (%s); usando moldura nativa.", ctypes.get_last_error())
            self._mode = "native"
            self._install_frame()
            return
        # Força o Windows a recalcular a área não-cliente com o novo WM_NCCALCSIZE.
        _user32.SetWindowPos(
            self._hwnd, None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )

    def _apply_theme(self) -> None:
        """Borda e barra nativas no tema atual (modo escuro do DWM; cores da barra no modo native)."""
        colors = WINDOW_COLORS[self._palette][self._theme]
        _set_dwm(self._hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if self._theme == "dark" else 0)
        if self._mode == "native":
            _set_dwm(self._hwnd, DWMWA_CAPTION_COLOR, _colorref(colors["bg"]))
            _set_dwm(self._hwnd, DWMWA_TEXT_COLOR, _colorref(colors["text"]))

    def set_theme(self, theme: str) -> None:
        """A interface trocou de tema: moldura e fundo da janela (o que aparece ao redimensionar antes
        de a página pintar) passam a ser os do tema novo."""
        if theme in ("light", "dark") and theme != self._theme:
            self._theme = theme
            self._refresh()

    def set_palette(self, palette: str) -> None:
        """A interface trocou de paleta: idem, com as cores da paleta nova."""
        if palette in WINDOW_COLORS and palette != self._palette:
            self._palette = palette
            self._refresh()

    def _refresh(self) -> None:
        def apply():
            from System.Drawing import Color, ColorTranslator  # pythonnet, carregado pelo pywebview

            self._apply_theme()
            background = WINDOW_COLORS[self._palette][self._theme]["bg"]
            self._form.BackColor = ColorTranslator.FromHtml(background)
            red, green, blue = (int(background[i:i + 2], 16) for i in (1, 3, 5))
            self._form.browser.webview.DefaultBackgroundColor = Color.FromArgb(255, red, green, blue)

        self.run_on_ui(apply, wait=False)

    def _on_webview_ready(self, sender: Any, args: Any) -> None:
        if not args.IsSuccess:
            return
        try:
            # Arquivo solto na janela não é aberto pelo WebView (o JS também barra, em main.js).
            sender.AllowExternalDrop = False
        except Exception as error:  # noqa: BLE001 - SDK antigo sem a propriedade
            logger.warning("AllowExternalDrop indisponível no WebView2: %s", error)
        if self._mode != "custom":
            return
        try:
            sender.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True
        except Exception as error:  # noqa: BLE001 - runtime antigo sem a propriedade
            logger.warning("app-region: drag indisponível no WebView2: %s", error)

    def _on_deactivate(self, *_) -> None:
        self._deactivated_at = time.monotonic()

    # ---------- procedimento da janela ----------

    def _subclass_proc(self, hwnd, msg, wparam, lparam, _uid, _ref):
        try:
            if msg == WM_NCCALCSIZE and wparam:
                return self._on_nccalcsize(hwnd, msg, wparam, lparam)
            if msg == WM_NCHITTEST:
                return self._on_nchittest(hwnd, msg, wparam, lparam)
            if msg == WM_WINDOWPOSCHANGED:
                return self._on_windowposchanged(hwnd, msg, wparam, lparam)
            if msg == WM_WINDOWPOSCHANGING and self._restoring:
                position = WINDOWPOS.from_address(lparam)
                position.flags |= SWP_NOSIZE | SWP_NOMOVE
            if msg == WM_NCDESTROY:
                _comctl32.RemoveWindowSubclass(hwnd, self._proc, 1)
        except Exception:  # noqa: BLE001 - nunca deixar exceção escapar do WndProc
            logger.exception("Erro no procedimento da janela")
        return _comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)

    def _on_nccalcsize(self, hwnd, msg, wparam, lparam) -> int:
        params = NCCALCSIZE_PARAMS.from_address(lparam)
        original_top = params.rgrc[0].top
        result = _comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
        if result != 0:
            return result
        # Remove a barra de título e a borda de cima; laterais e base ficam como o sistema calculou.
        # Maximizada, a janela passa da tela pela espessura da moldura: compensa no topo.
        params.rgrc[0].top = original_top + (self._frame_thickness() if _user32.IsZoomed(hwnd) else 0)
        return 0

    def _on_nchittest(self, hwnd, msg, wparam, lparam) -> int:
        result = _comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
        if result != HTCLIENT:
            return result
        # Só chega aqui o que o WebView2 deixou passar: regiões com app-region: drag.
        if not _user32.IsZoomed(hwnd):
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            rect = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            scale = _user32.GetDpiForWindow(hwnd) / 96
            if y < rect.top + RESIZE_BAND * scale:
                frame = self._frame_thickness()
                if x < rect.left + frame + CORNER_SIZE * scale:
                    return HTTOPLEFT
                if x > rect.right - frame - CORNER_SIZE * scale:
                    return HTTOPRIGHT
                return HTTOP
        return HTCAPTION

    def _on_windowposchanged(self, hwnd, msg, wparam, lparam) -> int:
        # Ao sair de maximizada/minimizada, o WinForms reaplica o ClientSize guardado somando
        # a barra de título que removemos (+31px por ciclo). O Windows já devolveu a janela ao
        # retângulo certo, então o reajuste aninhado do WinForms é bloqueado.
        previous = self._placement
        self._placement = "max" if _user32.IsZoomed(hwnd) else "min" if _user32.IsIconic(hwnd) else "normal"
        self._restoring = previous != "normal" and self._placement == "normal"
        try:
            return _comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
        finally:
            self._restoring = False

    def _frame_thickness(self) -> int:
        dpi = _user32.GetDpiForWindow(self._hwnd)
        return _user32.GetSystemMetricsForDpi(SM_CYFRAME, dpi) + _user32.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi)

    # ---------- comandos vindos da interface ----------

    def run_on_ui(self, func, wait: bool = True) -> None:
        """Roda func na thread da interface (wait=False: enfileira e volta na hora)."""
        from System import Action  # pythonnet, carregado pelo pywebview

        def guarded():
            # Uma exceção Python que escapasse para o WinForms viraria o diálogo de erro do .NET.
            try:
                func()
            except Exception:  # noqa: BLE001
                logger.exception("Erro na thread da interface")

        if wait:
            self._form.Invoke(Action(guarded))
        else:
            self._form.BeginInvoke(Action(guarded))

    def _start_nc_drag(self, hit: int) -> None:
        def start():
            # Botão já solto (clique rápido): iniciar o laço de arraste agora deixaria a janela
            # presa ao cursor até o próximo clique.
            if not _user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
                return
            point = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(point))
            _user32.ReleaseCapture()
            lparam = ((point.y & 0xFFFF) << 16) | (point.x & 0xFFFF)  # MAKELPARAM
            _user32.PostMessageW(self._hwnd, WM_NCLBUTTONDOWN, hit, lparam)

        self.run_on_ui(start)

    def start_drag(self) -> None:
        self._start_nc_drag(HTCAPTION)

    def start_resize(self, edge: str) -> None:
        hit = EDGE_HITS.get(edge)
        if hit and not self.is_maximized():
            self._start_nc_drag(hit)

    def is_in_front(self, grace: float = 0.0) -> bool:
        """Visível, não minimizada e em primeiro plano (ou estava até `grace` segundos atrás)."""
        if not _user32.IsWindowVisible(self._hwnd) or _user32.IsIconic(self._hwnd):
            return False
        foreground = _user32.GetForegroundWindow()
        if foreground and _user32.GetAncestor(foreground, GA_ROOTOWNER) == self._hwnd:
            return True
        return time.monotonic() - self._deactivated_at < grace

    def bring_to_front(self) -> None:
        def show():
            if not _user32.IsWindowVisible(self._hwnd):
                self._form.Show()
            if _user32.IsIconic(self._hwnd):
                _user32.ShowWindow(self._hwnd, SW_RESTORE)
            _user32.SetForegroundWindow(self._hwnd)
            self._form.Activate()
            self._form.browser.webview.Focus()  # teclado no WebView: o composer recebe o foco em seguida

        self.run_on_ui(show)

    def hide(self) -> None:
        self.run_on_ui(lambda: self._form.Hide())

    def is_maximized(self) -> bool:
        return bool(_user32.IsZoomed(self._hwnd))

    def minimize(self) -> None:
        _user32.ShowWindowAsync(self._hwnd, SW_MINIMIZE)

    def toggle_maximize(self) -> bool:
        maximize = not self.is_maximized()
        _user32.ShowWindowAsync(self._hwnd, SW_MAXIMIZE if maximize else SW_RESTORE)
        return maximize


def set_app_user_model_id(app_id: str) -> None:
    """AppUserModelID do processo, antes de criar a janela: a barra de tarefas agrupa e mostra o
    ícone do Robin, e não o do python.exe, mesmo rodando pelo código-fonte."""
    shell32 = ctypes.WinDLL("shell32")
    shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
    shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.HRESULT
    try:
        shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except OSError as error:  # HRESULT de erro vira OSError
        logger.warning("SetCurrentProcessExplicitAppUserModelID falhou: %s", error)


# ---------- área de transferência (reserva quando navigator.clipboard é negado) ----------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GlobalAlloc.argtypes = [UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_user32.OpenClipboard.argtypes = [HWND]
_user32.EmptyClipboard.argtypes = []
_user32.CloseClipboard.argtypes = []
_user32.SetClipboardData.argtypes = [UINT, ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def set_clipboard_text(text: str) -> bool:
    """Copia texto (UTF-16) para a área de transferência do Windows."""
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _user32.OpenClipboard(None):
        return False
    try:
        _user32.EmptyClipboard()
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = _kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)  # o sistema só assume o bloco se SetClipboardData der certo
            return False
        return True
    finally:
        _user32.CloseClipboard()
