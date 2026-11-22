"""memory.md: leitura, gravação e sincronização Git opcional (quando a pasta de dados é um repositório)."""

import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR, MEMORY_FILE, MEMORY_SEED

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def read_memory() -> str:
    """Conteúdo do memory.md. Se ele não existir, nasce da semente memory.example.md (empacotada
    no exe). Quebras de linha em \\n."""
    if not MEMORY_FILE.exists():
        content = MEMORY_SEED.read_text(encoding="utf-8") if MEMORY_SEED.exists() else ""
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_FILE.write_text(content, encoding="utf-8", newline="\n")
    return MEMORY_FILE.read_text(encoding="utf-8")


def memory_is_blank() -> bool:
    """True se o memory.md não existe ou só tem espaços."""
    return not MEMORY_FILE.exists() or not MEMORY_FILE.read_text(encoding="utf-8").strip()


def write_memory(content: str) -> None:
    """Grava o memory.md mantendo o estilo de quebra de linha que o arquivo já usa."""
    newline = "\n"
    if MEMORY_FILE.exists() and b"\r\n" in MEMORY_FILE.read_bytes():
        newline = "\r\n"
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(content.replace("\r\n", "\n"), encoding="utf-8", newline=newline)


def _git(*args: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(DATA_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
        timeout=timeout,
    )


def git_available() -> bool:
    """True se a própria pasta de dados é a raiz de um repositório Git (e o git está instalado).
    Uma pasta dentro de outro repositório não conta: o sync faria commit da memória nele."""
    if not DATA_DIR.is_dir():
        return False
    try:
        result = _git("rev-parse", "--show-toplevel", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == DATA_DIR.resolve()


def sync_git() -> tuple[bool, str]:
    """git add/commit/push do memory.md. Retorna (sucesso, detalhe do erro)."""
    try:
        added = _git("add", "memory.md", timeout=15)
        if added.returncode != 0:
            raise RuntimeError(f"git add falhou: {added.stderr.strip()}")

        committed = _git("commit", "-m", "Auto-sync: atualização da memória do Robin", timeout=15)
        if committed.returncode != 0:
            output = (committed.stdout + " " + committed.stderr).lower()
            if "nothing to commit" not in output and "nada a submeter" not in output:
                raise RuntimeError(f"git commit falhou: {committed.stderr.strip()}")

        pushed = _git("push", timeout=30)
        if pushed.returncode != 0:
            raise RuntimeError(f"git push falhou: {pushed.stderr.strip()}")
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        return False, str(error)
    return True, ""
