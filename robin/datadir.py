"""Pasta de dados (%APPDATA%\\Robin): criação e migração única dos dados que ficavam ao lado do
Robin2.exe ou na raiz do projeto. Os arquivos antigos nunca são apagados."""

import logging
import shutil
from pathlib import Path
from typing import Iterable, Optional

from dotenv import dotenv_values

from . import config

logger = logging.getLogger("robin.datadir")

# A pasta de dados pode virar um repositório Git (sync da memória): a chave, a simulação, o cérebro
# (brain.json, suas cópias, a fila da nuvem em sync.json e os temporários da gravação atômica) e as
# conversas ficam fora, como o instancia.json (qual Robin está aberto nesta máquina).
GITIGNORE_LINES = (".env", "mock/", "brain.json", "brain.*.json", ".brain-*.tmp", "sync.json", ".sync-*.tmp",
                   "instancia.json", "conversas/", "sync-conversas.json")
GITIGNORE = "".join(f"{line}\n" for line in GITIGNORE_LINES)


def prepare() -> None:
    """Cria a pasta de dados (com o .gitignore) e, na pasta real, migra os dados antigos.
    Com ROBIN_DATA_DIR ou na simulação não há migração: só a pasta padrão herda os dados."""
    ensure(config.DATA_ROOT)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not config.MOCK and not config.DATA_DIR_OVERRIDDEN:
        migrate(config.DATA_DIR, config.LEGACY_DIRS)


def ensure(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    gitignore = data_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE, encoding="utf-8", newline="\n")
        return
    # Um .gitignore antigo (ou editado pelo usuário) ganha só as linhas que faltam.
    try:
        current = gitignore.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        logger.warning("Não foi possível ler %s: %s", gitignore, error)
        return
    missing = [line for line in GITIGNORE_LINES if line not in current.splitlines()]
    if missing:
        prefix = "" if not current or current.endswith("\n") else "\n"
        with gitignore.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix + "".join(f"{line}\n" for line in missing))


def migrate(data_dir: Path, legacy_dirs: Iterable[Path]) -> list[Path]:
    """Copia para data_dir o que ainda não existe lá. Devolve os arquivos de origem copiados.
    - .env: o mais recente dos que têm GEMINI_API_KEY;
    - memory.md: o mais recente."""
    legacy_dirs = [Path(folder) for folder in legacy_dirs if Path(folder).resolve() != data_dir.resolve()]
    copied = []
    for name, accept in ((".env", _has_api_key), ("memory.md", Path.is_file)):
        target = data_dir / name
        if target.exists():
            continue
        source = _newest(folder / name for folder in legacy_dirs if accept(folder / name))
        if source is None:
            continue
        try:
            shutil.copy2(source, target)
        except OSError as error:
            logger.error("Migração: não foi possível copiar %s para %s: %s", source, target, error)
            continue
        logger.info("Migração: %s copiado para %s (o original foi mantido).", source, target)
        copied.append(source)
    return copied


def _has_api_key(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return bool((dotenv_values(path).get(config.ENV_API_KEY) or "").strip())
    except (OSError, UnicodeDecodeError):
        return False


def _newest(paths: Iterable[Path]) -> Optional[Path]:
    return max(paths, key=lambda path: path.stat().st_mtime, default=None)
