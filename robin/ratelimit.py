"""Limitador local de pedidos ao Gemini, por modelo (janela deslizante de 60s).

O plano gratuito recusa com 429 o 6º pedido no mesmo minuto ao mesmo modelo: esperar alguns segundos
aqui sai mais barato do que tomar o 429, esperar a nova tentativa e cair no modelo de reserva.
Só conhece os pedidos deste processo.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Callable

WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self.per_minute = max(0, int(per_minute))  # 0 desliga
        self._clock = clock
        self._sent: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def delay(self, model: str) -> float:
        """Segundos até haver vaga para mais um pedido a este modelo (0: pode enviar já)."""
        if not self.per_minute:
            return 0.0
        with self._lock:
            sent = self._prune(model)
            if len(sent) < self.per_minute:
                return 0.0
            return max(0.0, sent[len(sent) - self.per_minute] + WINDOW_SECONDS - self._clock())

    def record(self, model: str) -> None:
        if not self.per_minute:
            return
        with self._lock:
            self._prune(model).append(self._clock())

    def _prune(self, model: str) -> deque:
        sent, now = self._sent[model], self._clock()
        while sent and now - sent[0] >= WINDOW_SECONDS:
            sent.popleft()
        return sent
