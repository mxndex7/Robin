"""Instância única por pasta de dados e a janela só de mensagens do Robin.

- SingleInstance: mutex nomeado pela pasta de dados. Só a primeira execução fica com ele.
- notify_running: a segunda execução acha a janela de mensagens da primeira (pelo nome) e pede
  "apareça" ou "saia" (--quit).
- MessageWindow: thread com uma janela HWND_MESSAGE que recebe esses pedidos e o WM_HOTKEY do
  atalho global. O RegisterHotKey precisa rodar na thread dona da janela, então trocar o atalho
  é um pedido para essa thread.
"""

import ctypes
import hashlib
import itertools
import logging
import queue
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

from .hotkey import MOD_NOREPEAT, Hotkey

logger = logging.getLogger("robin.instance")

# Instâncias próprias: argtypes definidos aqui não afetam o windll usado pelo pywebview.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowExW.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]

ERROR_ALREADY_EXISTS = 183
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
HWND_MESSAGE = wintypes.HWND(-3)
WM_CLOSE, WM_DESTROY, WM_HOTKEY = 0x0010, 0x0002, 0x0312
WM_APP = 0x8000
WM_ROBIN_SHOW = WM_APP + 1  # de outra execução: apareça
WM_ROBIN_QUIT = WM_APP + 2  # de outra execução (--quit): saia
_WM_ROBIN_HOTKEY = WM_APP + 3  # interno: (re)registrar o atalho nesta thread

_class_ids = itertools.count(1)


def instance_key(data_dir: Path) -> str:
    """Nome do mutex e da janela de mensagens: um Robin por pasta de dados."""
    digest = hashlib.sha1(str(Path(data_dir).resolve()).lower().encode("utf-8")).hexdigest()[:12]
    return f"Robin-{digest}"


class SingleInstance:
    """Mutex nomeado na sessão do usuário; fica com o processo até ele sair (ou release())."""

    def __init__(self, key: str):
        self._name = f"Local\\{key}"
        self._handle = None

    def acquire(self) -> bool:
        """True se esta é a primeira execução para a pasta de dados."""
        ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, False, self._name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None


def _message_window(key: str, timeout: float) -> Optional[int]:
    """A janela de mensagens da execução aberta. Espera ela existir (a execução pode estar começando)."""
    deadline = time.monotonic() + timeout
    while True:
        hwnd = _user32.FindWindowExW(HWND_MESSAGE, None, None, key)
        if hwnd or time.monotonic() >= deadline:
            return hwnd
        time.sleep(0.1)


def running_pid(key: str, timeout: float = 5.0) -> Optional[int]:
    """pid da execução aberta (dona da janela de mensagens), ou None."""
    hwnd = _message_window(key, timeout)
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def notify_running(key: str, message: int = WM_ROBIN_SHOW, timeout: float = 5.0) -> bool:
    """Manda o pedido para a execução que já está aberta e libera o primeiro plano para ela
    aparecer na frente."""
    hwnd = _message_window(key, timeout)
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    _user32.AllowSetForegroundWindow(pid.value)
    return bool(_user32.PostMessageW(hwnd, message, 0, 0))


class MessageWindow:
    """Janela só de mensagens numa thread própria. Os callbacks rodam numa thread curta à parte, para
    o laço de mensagens nunca ficar preso esperando a interface."""

    def __init__(self, key: str, on_hotkey: Callable[[], None], on_show: Callable[[], None], on_quit: Callable[[], None]):
        self._key = key
        self._handlers = {WM_ROBIN_SHOW: on_show, WM_ROBIN_QUIT: on_quit}
        self._on_hotkey = on_hotkey
        self._class_name = f"RobinMessageWindow{next(_class_ids)}"
        self._proc = WNDPROC(self._wndproc)  # referência mantida viva de propósito
        self._hwnd = None
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._requests: queue.Queue = queue.Queue()
        self._hotkey_ids = itertools.count(1)
        self._hotkey_id = 0
        self._hotkey: Optional[Hotkey] = None

    def start(self, timeout: float = 5.0) -> None:
        self._thread = threading.Thread(target=self._run, name="robin-mensagens", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout) or not self._hwnd:
            raise OSError("A janela de mensagens do Robin não foi criada.")

    def stop(self, timeout: float = 5.0) -> None:
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout)

    def register(self, hotkey: Hotkey, timeout: float = 5.0) -> Optional[str]:
        """Troca o atalho global. Devolve None ou a mensagem de erro; se falhar, o anterior continua."""
        done = threading.Event()
        result: dict = {}
        self._requests.put((hotkey, done, result))
        _user32.PostMessageW(self._hwnd, _WM_ROBIN_HOTKEY, 0, 0)
        if not done.wait(timeout):
            return "O atalho global não respondeu."
        return result.get("error")

    # ---------- thread da janela ----------

    def _run(self) -> None:
        hinstance = _kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinstance, lpszClassName=self._class_name)
        if not _user32.RegisterClassW(ctypes.byref(window_class)):
            logger.error("RegisterClassW falhou: %s", ctypes.get_last_error())
            self._ready.set()
            return
        self._hwnd = _user32.CreateWindowExW(
            0, self._class_name, self._key, 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinstance, None,
        )
        self._ready.set()
        if not self._hwnd:
            logger.error("CreateWindowExW falhou: %s", ctypes.get_last_error())
            return
        message = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(message))
            _user32.DispatchMessageW(ctypes.byref(message))
        _user32.UnregisterClassW(self._class_name, hinstance)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY and wparam == self._hotkey_id:
                self._dispatch(self._on_hotkey)
                return 0
            if msg in self._handlers:
                self._dispatch(self._handlers[msg])
                return 0
            if msg == _WM_ROBIN_HOTKEY:
                self._apply_requests(hwnd)
                return 0
            if msg == WM_CLOSE:
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                if self._hotkey_id:
                    _user32.UnregisterHotKey(hwnd, self._hotkey_id)
                self._hwnd = None
                _user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 - nunca deixar exceção escapar do WndProc
            logger.exception("Erro na janela de mensagens")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    @staticmethod
    def _dispatch(callback: Callable[[], None]) -> None:
        threading.Thread(target=callback, daemon=True).start()

    def _apply_requests(self, hwnd) -> None:
        while True:
            try:
                hotkey, done, result = self._requests.get_nowait()
            except queue.Empty:
                return
            result["error"] = self._swap_hotkey(hwnd, hotkey)
            done.set()

    def _swap_hotkey(self, hwnd, hotkey: Hotkey) -> Optional[str]:
        """Registra o novo antes de soltar o atual: se o novo estiver em uso, nada muda."""
        if self._hotkey and self._hotkey.text == hotkey.text:
            return None
        new_id = next(self._hotkey_ids)
        if not _user32.RegisterHotKey(hwnd, new_id, hotkey.modifiers | MOD_NOREPEAT, hotkey.vk):
            error = ctypes.get_last_error()
            if error == ERROR_HOTKEY_ALREADY_REGISTERED:
                return f"{hotkey.text} já está em uso por outro programa."
            return f"Não foi possível registrar {hotkey.text} (erro {error})."
        if self._hotkey_id:
            _user32.UnregisterHotKey(hwnd, self._hotkey_id)
        self._hotkey_id, self._hotkey = new_id, hotkey
        return None
