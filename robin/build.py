"""Origem e build deste Robin, e o instancia.json que deixa uma segunda execução saber qual Robin já
está aberto (o exe que inicia com o Windows ocupa a instância única e esconde uma versão nova).

- exe: o Robin.exe e a data dele ("Robin.exe de 27/10 13:20");
- código-fonte: o commit do .git e a data do arquivo mais novo do código ("código-fonte, commit
  a721f65, arquivos de 31/10 15:20"): código editado e não reaberto também conta como outro build.
"""

import ctypes
import json
import logging
import os
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("robin.build")

INSTANCE_FILE = "instancia.json"
_SOURCE_PATTERNS = ("app.py", "robin/*.py", "ui/*.html", "ui/js/*.js", "ui/css/*.css")

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _stamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%d/%m %H:%M")


def exe_build(path: Path) -> dict:
    """Build de um Robin.exe (este ou o de outro processo): o caminho e a data do arquivo."""
    stamp = _stamp(path.stat().st_mtime)
    return {"origin": "exe", "path": str(path), "build": stamp, "label": f"{path.name} de {stamp}"}


def _git_commit(root: Path) -> Optional[str]:
    """Commit do HEAD lendo o .git direto (sem subprocesso)."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:7]
        ref = head[5:]
        if (git / ref).exists():
            return (git / ref).read_text(encoding="utf-8").strip()[:7]
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(f" {ref}"):
                return line[:7]
    except OSError:
        pass
    return None


def source_build(root: Path) -> dict:
    newest = max((path.stat().st_mtime for pattern in _SOURCE_PATTERNS for path in root.glob(pattern)), default=0.0)
    commit = _git_commit(root) or "sem commit"
    files = _stamp(newest) if newest else "?"
    return {
        "origin": "source", "path": str(root), "build": f"{commit} {files}",
        "label": f"código-fonte, commit {commit}, arquivos de {files}",
    }


def current_build() -> dict:
    """Origem e build desta execução, com a versão do app."""
    info = exe_build(config.EXE_FILE) if config.EXE_FILE else source_build(config.APP_DIR)
    return {**info, "version": config.APP_VERSION}


def same_build(a: Optional[dict], b: Optional[dict]) -> bool:
    if not a or not b:
        return False
    return (a.get("origin"), str(a.get("path", "")).lower(), a.get("build")) == \
        (b.get("origin"), str(b.get("path", "")).lower(), b.get("build"))


# ---------- instancia.json ----------

def write_instance_file(data_dir: Path, build: dict) -> None:
    """Gravado por quem ficou com a instância única: pid, origem, build e quando abriu."""
    info = {**build, "pid": os.getpid(), "aberto_em": datetime.now().isoformat(timespec="seconds")}
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / INSTANCE_FILE).write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        logger.warning("Não foi possível gravar o %s", INSTANCE_FILE, exc_info=True)


def read_instance_file(data_dir: Path) -> Optional[dict]:
    try:
        info = json.loads((data_dir / INSTANCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _process_image(pid: int) -> Optional[Path]:
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value)
    finally:
        _kernel32.CloseHandle(handle)


def running_build(data_dir: Path, pid: Optional[int]) -> Optional[dict]:
    """O Robin aberto (dono da janela de mensagens, pid): o instancia.json dele, se for do mesmo
    processo; senão (um exe antigo não grava o arquivo), o executável do processo."""
    if not pid:
        return None
    info = read_instance_file(data_dir)
    if info and info.get("pid") == pid:
        return info
    image = _process_image(pid)
    if image is None:
        return {"origin": "?", "path": "", "build": "?", "label": f"processo {pid}"}
    if image.name.lower() != "python.exe" and image.name.lower() != "pythonw.exe":
        try:
            return {**exe_build(image), "pid": pid}
        except OSError:
            pass
    return {"origin": "?", "path": str(image), "build": "?", "label": f"{image.name} (pid {pid}), versão desconhecida"}
