"""Leitura de texto limitada à pasta escolhida no diálogo nativo do Windows."""

import ctypes
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
import os
from pathlib import Path, PureWindowsPath
import stat
from struct import Struct
import unicodedata

from .attachments import BASENAMES, EXTENSIONS, MAX_FILE_BYTES


FOLDER_TOOLS = {
    "listar_pasta": ("Liste arquivos de texto e subpastas. Use '.' para a raiz.", False),
    "ler_arquivo_da_pasta": ("Leia um arquivo UTF-8 pelo caminho relativo retornado por listar_pasta.", True),
}
_PRIVATE_NAMES = {
    "appdata", "brain.json", "conversas", "conversations", "credentials", "credentials.json", "memory.md",
    "node_modules", "robin.log", "secrets", "secrets.json", "secrets.toml", "__pycache__",
}
_FILE_READ_LIMIT = 12
_MAX_CALLS = 18
_FILE_BYTES = 16 * 1024
_MAX_READ_BYTES = 64 * 1024
_SCAN_LIMIT = 256
_LIST_LIMIT = 80
_LIST_BYTES = 8 * 1024
_MAX_LIST_BYTES = 24 * 1024
_LOCAL_DRIVE_TYPES = {2, 3, 5, 6}  # removível, fixo, óptico ou RAM
_DIRECTORY = stat.FILE_ATTRIBUTE_DIRECTORY
_PRIVATE_ATTRIBUTES = (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM | stat.FILE_ATTRIBUTE_REPARSE_POINT)
_DIRECTORY_ENTRY = Struct("<II6qIII")  # FILE_FULL_DIR_INFO até FileName (UTF-16)


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", wintypes.FILETIME),
        ("accessed", wintypes.FILETIME),
        ("written", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]

    @property
    def identity(self):
        return self.volume, self.index_high, self.index_low


if os.name == "nt":
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        wintypes.DWORD, wintypes.HANDLE,
    )
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    _kernel.GetDriveTypeW.restype = wintypes.UINT
    _kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    )
    _kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(_FileInformation))
    _kernel.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    _kernel.ReadFile.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    )
    _INVALID_HANDLE = ctypes.c_void_p(-1).value


def _visible(name: str) -> bool:
    return (0 < len(name) <= 255 and name not in {".", ".."}
            and (not name.startswith(".") or name.lower() in BASENAMES)
            and name.lower() not in _PRIVATE_NAMES
            and not any(unicodedata.category(char).startswith("C") for char in name)
            and not os.path.isreserved(name))


def _parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError
    if value == ".":
        return ()
    parts = tuple(value.replace("\\", "/").split("/"))
    if len(parts) > 24 or any(not _visible(part) for part in parts):
        raise ValueError
    return parts


def _text_file(name: str) -> bool:
    return name.lower() in BASENAMES or PureWindowsPath(name).suffix.lower() in EXTENSIONS


@contextmanager
def _open_path(path: Path, directory: bool):
    # OPEN_REPARSE_POINT e BACKUP_SEMANTICS abrem o próprio item, inclusive diretórios.
    # Sem FILE_SHARE_DELETE, os ancestrais abertos não podem ser trocados durante a operação.
    access = 0x81 if directory else 0x80000000  # FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY ou GENERIC_READ
    handle = _kernel.CreateFileW(str(path), access, 0x3, None, 3, 0x02200000, None)
    if handle == _INVALID_HANDLE:
        raise OSError
    try:
        final = ctypes.create_unicode_buffer(32768)
        size = _kernel.GetFinalPathNameByHandleW(handle, final, len(final), 0)
        if not 0 < size < len(final) or os.path.normcase(final.value[4:]) != os.path.normcase(str(path)):
            raise ValueError
        info = _FileInformation()
        if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError
        if info.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or bool(info.attributes & _DIRECTORY) != directory:
            raise ValueError
        if not directory and (info.links != 1 or info.size_high or info.size_low > MAX_FILE_BYTES):
            raise ValueError
        yield handle, info
    finally:
        _kernel.CloseHandle(handle)


class ProjectFolder:
    def __init__(self, selected: str, *, protected: tuple[Path, ...] = ()):
        self._root = Path(selected)
        self._protected = tuple(path.resolve() for path in protected)
        root = self._root
        if (os.name != "nt" or len(root.anchor) != 3 or root.anchor[1:] != ":\\"
                or not _visible(root.name)
                or any(not _visible(part) and part.lower() != "appdata" for part in root.parts[1:-1])):
            raise ValueError
        if _kernel.GetDriveTypeW(root.anchor) not in _LOCAL_DRIVE_TYPES:
            raise ValueError
        self.name = root.name
        with self._open((), directory=True, identity=None) as (_, info):
            self._identity = info.identity

    @contextmanager
    def _open(self, parts: tuple[str, ...], *, directory: bool, identity):
        if self._is_protected(self._root.joinpath(*parts)):
            raise ValueError
        with ExitStack() as stack:
            path = Path(self._root.anchor)
            handle, info = stack.enter_context(_open_path(path, directory=True))
            for component in self._root.parts[1:]:
                path /= component
                handle, info = stack.enter_context(_open_path(path, directory=True))
                if info.attributes & _PRIVATE_ATTRIBUTES and path != Path.home() / "AppData":
                    raise ValueError
            if info.attributes & _PRIVATE_ATTRIBUTES or (identity is not None and identity != info.identity):
                raise ValueError
            for index, component in enumerate(parts):
                path /= component
                handle, info = stack.enter_context(_open_path(path, directory or index < len(parts) - 1))
                if info.attributes & _PRIVATE_ATTRIBUTES:
                    raise ValueError
            yield handle, info

    def reader(self):
        return FolderReader(self)

    def _is_protected(self, candidate: Path) -> bool:
        return any(candidate.is_relative_to(path) for path in self._protected)


def _directory_entries(handle):
    buffer = ctypes.create_string_buffer(32768)
    while _kernel.GetFileInformationByHandleEx(handle, 14, buffer, len(buffer)):  # FileFullDirectoryInfo
        offset = 0
        while True:
            next_offset, _, _, _, _, _, size, _, attributes, name_size, _ = _DIRECTORY_ENTRY.unpack_from(buffer, offset)
            start = offset + _DIRECTORY_ENTRY.size
            name = buffer[start:start + name_size].decode("utf-16-le")
            yield name, attributes, size
            if not next_offset:
                break
            offset += next_offset
    if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
        raise OSError


class FolderReader:
    def __init__(self, folder: ProjectFolder):
        self._folder = folder
        self._calls = 0
        self._files = 0
        self._bytes = 0
        self._list_bytes = 0

    def call(self, name: str, args: dict) -> dict:
        self._calls += 1
        if self._calls > _MAX_CALLS:
            return {"ok": False, "erro": "Limite de consultas à pasta por mensagem atingido."}
        try:
            if name == "listar_pasta":
                return self._list(_parts(args.get("caminho", ".")))
            return self._read(_parts(args.get("caminho")))
        except (OSError, ValueError):
            return {"ok": False, "erro": "Item indisponível para leitura. Confira o caminho relativo, o tipo e o tamanho do arquivo."}

    def _list(self, parts: tuple[str, ...]) -> dict:
        items = []
        remaining = min(_LIST_BYTES, _MAX_LIST_BYTES - self._list_bytes)
        if not remaining:
            return {"ok": False, "erro": "Limite de listagem da pasta por mensagem atingido."}
        with self._folder._open(parts, directory=True, identity=self._folder._identity) as (handle, _):
            for index, (name, attributes, size) in enumerate(_directory_entries(handle)):
                if index == _SCAN_LIMIT or len(items) == _LIST_LIMIT:
                    return {"ok": True, "itens": sorted(items), "parcial": True}
                if not _visible(name) or attributes & _PRIVATE_ATTRIBUTES:
                    continue
                if self._folder._is_protected(self._folder._root.joinpath(*parts, name)):
                    continue
                if attributes & _DIRECTORY:
                    name += "/"
                elif not _text_file(name) or size > MAX_FILE_BYTES:
                    continue
                cost = len(name.encode("utf-8")) + 4
                if cost > remaining:
                    return {"ok": True, "itens": sorted(items), "parcial": True}
                items.append(name)
                remaining -= cost
                self._list_bytes += cost
        return {"ok": True, "itens": sorted(items), "parcial": False}

    def _read(self, parts: tuple[str, ...]) -> dict:
        if not parts or not _text_file(parts[-1]):
            raise ValueError
        if self._files >= _FILE_READ_LIMIT or self._bytes >= _MAX_READ_BYTES:
            return {"ok": False, "erro": "Limite de leitura de arquivos por mensagem atingido."}
        limit = min(_FILE_BYTES, _MAX_READ_BYTES - self._bytes)
        with self._folder._open(parts, directory=False, identity=self._folder._identity) as (handle, _):
            buffer = ctypes.create_string_buffer(limit + 1)
            count = wintypes.DWORD()
            if not _kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
                raise OSError
        data = buffer.raw[:count.value]
        self._files += 1
        self._bytes += min(limit, len(data))
        cut = len(data) > limit
        try:
            content = data[:limit].decode("utf-8-sig")
        except UnicodeDecodeError as error:
            if not cut or error.reason != "unexpected end of data":
                raise
            content = data[:error.start].decode("utf-8-sig")
        if any(ord(char) < 32 and char not in "\r\n\t" for char in content):
            raise ValueError
        return {"ok": True, "caminho": "/".join(parts), "conteudo": content, "parcial": cut}
