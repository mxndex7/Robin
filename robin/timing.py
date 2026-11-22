"""Logs de tempo (INFO, prefixo "tempo:") para achar onde um turno gasta ou para.

Cada thread carrega uma etiqueta (g<gen_id> na geração, "sync" no pull, "-" fora deles): os pedidos ao
Supabase e os embeddings feitos dentro de um turno saem com a etiqueta do turno.
"""

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("robin.tempo")
_local = threading.local()


def set_tag(tag: str) -> None:
    _local.tag = tag


def tag() -> str:
    return getattr(_local, "tag", "-")


def ms(start: float) -> int:
    """Milissegundos desde start (time.monotonic())."""
    return round((time.monotonic() - start) * 1000)


def log(message: str, *args) -> None:
    logger.info("tempo: [%s] " + message, tag(), *args)


@contextmanager
def measure(label: str) -> Iterator[None]:
    """Registra início e fim (com a duração) de um trecho; se ele levantar, registra a exceção."""
    start = time.monotonic()
    try:
        yield
    except BaseException as error:
        log("%s falhou em %dms: %s", label, ms(start), type(error).__name__)
        raise
    log("%s: %dms", label, ms(start))


@contextmanager
def wait_lock(lock, label: str, threshold_ms: int = 200) -> Iterator[None]:
    """Adquire o lock e registra a espera quando passa de threshold_ms."""
    start = time.monotonic()
    with lock:
        waited = ms(start)
        if waited >= threshold_ms:
            log("espera pelo lock %s: %dms", label, waited)
        yield
