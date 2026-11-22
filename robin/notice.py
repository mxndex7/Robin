"""Notificação da bandeja numa execução que não abre a interface (uma segunda execução que vai
terminar): um ícone temporário só com Win32 (Shell_NotifyIconW), sem o .NET do pywebview, que
nesse caso nem chega a carregar."""

import ctypes
import logging
import time
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger("robin.notice")

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

NIM_ADD, NIM_DELETE = 0x0, 0x2
NIF_ICON, NIF_TIP, NIF_INFO = 0x2, 0x4, 0x10
NIIF_NONE = 0x0
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
HWND_MESSAGE = wintypes.HWND(-3)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON),
    ]


_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
_user32.LoadImageW.restype = wintypes.HANDLE
_user32.DestroyIcon.argtypes = [wintypes.HICON]


def tray_notice(text: str, icon: Path, seconds: float = 8.0, title: str = "Robin") -> bool:
    """Mostra a notificação e espera seconds antes de tirar o ícone (tirar antes some com ela).
    False se o Windows recusou; nunca levanta exceção."""
    hwnd = _user32.CreateWindowExW(0, "STATIC", title, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    hicon = _user32.LoadImageW(None, str(icon), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE) if icon.exists() else None
    data = NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    data.hWnd = hwnd
    data.uID = 1
    data.uFlags = NIF_TIP | NIF_INFO | (NIF_ICON if hicon else 0)
    data.hIcon = hicon
    data.szTip = title
    data.szInfo = text[:255]
    data.szInfoTitle = title[:63]
    data.dwInfoFlags = NIIF_NONE
    try:
        shown = bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))
        if shown:
            time.sleep(seconds)
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        else:
            logger.warning("A notificação da bandeja foi recusada pelo Windows")
        return shown
    except OSError:
        logger.warning("Notificação da bandeja falhou", exc_info=True)
        return False
    finally:
        if hicon:
            _user32.DestroyIcon(hicon)
        if hwnd:
            _user32.DestroyWindow(hwnd)
