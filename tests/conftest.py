"""Fixtures comuns: MockGeminiService sem atrasos, memória controlada, registro de eventos e pedidos,
falhas roteirizadas na rede simulada e a RobinAPI com uma janela falsa."""

import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Antes de importar o robin: nenhum teste enxerga a pasta de dados real (%APPDATA%\Robin) nem as
# variáveis de controle do ambiente de quem roda os testes.
os.environ["ROBIN_DATA_DIR"] = tempfile.mkdtemp(prefix="robin-testes-")
for _flag in ("ROBIN_MOCK", "ROBIN_DEBUG", "ROBIN_FRAME"):
    os.environ.pop(_flag, None)

from robin import api as api_module  # noqa: E402
from robin import config, gemini, mock  # noqa: E402
from robin import memory as memory_store  # noqa: E402
from robin.brain import LocalStore  # noqa: E402
from robin.cloud_mock import FakeSupabase  # noqa: E402
from robin.config import DEFAULT_MODEL  # noqa: E402
from robin.gemini import _content_parts  # noqa: E402
from robin.mock import MockGeminiService  # noqa: E402

TIMEOUT = 5


def _content_text(content) -> str:
    return _content_parts(content.parts or [])[0]


class Events:
    """Coleta os eventos que o serviço emitiria para a interface (chamado de várias threads)."""

    def __init__(self):
        self._items: list[tuple[str, dict]] = []
        self._cond = threading.Condition()

    def __call__(self, name: str, payload: dict) -> None:
        with self._cond:
            self._items.append((name, payload))
            self._cond.notify_all()

    def of(self, name: str, gen_id=None) -> list[dict]:
        with self._cond:
            return [p for n, p in self._items if n == name and (gen_id is None or p.get("gen_id") == gen_id)]

    def sequence(self, gen_id) -> list[str]:
        """Nomes dos eventos da geração, em ordem, com os onChunk seguidos contados uma vez."""
        with self._cond:
            names = [n for n, p in self._items if p.get("gen_id") == gen_id]
        return [n for i, n in enumerate(names) if not (n == "onChunk" and i and names[i - 1] == "onChunk")]

    def wait(self, name: str, gen_id=None, timeout: float = TIMEOUT) -> dict:
        with self._cond:
            ok = self._cond.wait_for(lambda: self.of(name, gen_id), timeout)
        assert ok, f"evento {name} (gen_id={gen_id}) não chegou em {timeout}s"
        return self.of(name, gen_id)[-1]


class Requests:
    """Envolve a "rede" simulada: guarda o modelo, o histórico e a configuração de cada pedido e
    permite roteirizar os próximos (script)."""

    def __init__(self, service: MockGeminiService):
        self.calls: list[dict] = []
        self._scripted: list = []
        models = service._client.models
        original = models.generate_content_stream

        def recording(model, contents, config=None):
            contents = list(contents)
            self.calls.append({"model": model.removeprefix("models/"), "contents": contents, "config": config})
            behavior = self._scripted.pop(0) if self._scripted else None
            if behavior is not None:
                return behavior()
            return original(model=model, contents=contents, config=config)

        models.generate_content_stream = recording

    def script(self, *behaviors) -> None:
        """Próximos pedidos, em ordem: um gerador de pedaços cada (None: a simulação normal)."""
        self._scripted.extend(behaviors)

    def texts(self, index: int = -1) -> list[tuple[str, str]]:
        return [(c.role, _content_text(c)) for c in self.calls[index]["contents"]]

    def turns(self, index: int = -1) -> list[tuple[str, str]]:
        """Como texts, com os Contents seguidos do mesmo papel juntos (o SDK grava um por pedaço;
        a sessão recriada a partir de _history() já vem junta)."""
        merged: list[tuple[str, str]] = []
        for role, text in self.texts(index):
            if merged and merged[-1][0] == role:
                merged[-1] = (role, merged[-1][1] + text)
            else:
                merged.append((role, text))
        return merged

    def system_instruction(self, index: int = -1) -> str:
        config = self.calls[index]["config"]
        value = config.get("system_instruction") if isinstance(config, dict) else config.system_instruction
        return str(value)


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    """Cada teste tem a própria pasta de dados (.env e memory.md), nunca a real."""
    data_dir = tmp_path / "dados"
    data_dir.mkdir()
    env_file = data_dir / ".env"
    memory_file = data_dir / "memory.md"
    memory_file.write_text("# Memória de teste\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setattr(memory_store, "MEMORY_FILE", memory_file)
    monkeypatch.setattr(memory_store, "DATA_DIR", data_dir)
    monkeypatch.setattr(api_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(api_module, "BRAIN_FILE", data_dir / "brain.json")
    monkeypatch.setattr(api_module, "SYNC_FILE", data_dir / "sync.json")
    monkeypatch.setattr(api_module, "CONVERSATIONS_DIR", data_dir / "conversas")
    # Nenhum teste acessa um Supabase de verdade: a nuvem é sempre a simulada (uma por teste).
    cloud = FakeSupabase()
    monkeypatch.setattr(api_module, "cloud_transport", cloud.transport)
    monkeypatch.setattr(mock, "_embed_fail_until", 0.0)
    return SimpleNamespace(dir=data_dir, env=env_file, memory=memory_file, brain=data_dir / "brain.json",
                           sync=data_dir / "sync.json", conversations=data_dir / "conversas", cloud=cloud)


@pytest.fixture
def memory(monkeypatch):
    """Conteúdo do memory.md visto pelo serviço (o arquivo não é lido nem gravado)."""
    state = {"text": "memória inicial"}
    monkeypatch.setattr(gemini, "read_memory", lambda: state["text"])
    return state


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(mock, "THINKING_SECONDS", 0)
    monkeypatch.setattr(mock, "CHUNK_DELAY", (0, 0))


@pytest.fixture
def model_confirms(monkeypatch):
    """Sem a resposta junto com a ação: depois de uma ação, sempre há o 2º pedido (testes da 2ª rodada)."""
    monkeypatch.setattr(gemini, "REPLY_WITH_ACTION", False)


@pytest.fixture
def slow_stream(monkeypatch):
    """Stream lento o bastante para parar ou limpar no meio (resposta "longo" leva ~1 s)."""
    monkeypatch.setattr(mock, "THINKING_SECONDS", 0)
    monkeypatch.setattr(mock, "CHUNK_CHARS", (8, 8))
    monkeypatch.setattr(mock, "CHUNK_DELAY", (0.004, 0.004))


@pytest.fixture
def resilience(monkeypatch, fast):
    """Prazos curtos para o watchdog, a nova tentativa e o gatilho "lento"."""
    monkeypatch.setattr(gemini, "WATCHDOG_SECONDS", 0.3)
    monkeypatch.setattr(gemini, "RETRY_DELAY", 0.05)
    monkeypatch.setattr(mock, "SLOW_SECONDS", 1.0)


@pytest.fixture
def events():
    return Events()


@pytest.fixture
def brain(files):
    return LocalStore(files.brain)


@pytest.fixture
def service(memory, events, brain):
    svc = MockGeminiService(emit=events, api_key="", model=DEFAULT_MODEL, store=brain)
    yield svc
    svc.clear()  # encerra qualquer stream que tenha sobrado


@pytest.fixture
def requests(service):
    return Requests(service)


class FakeWindow:
    """Recebe o window.run_js da RobinAPI e devolve o evento ao coletor."""

    def __init__(self, events: Events):
        self._events = events

    def run_js(self, script: str) -> None:
        match = re.fullmatch(r"window\.robin && window\.robin\.(\w+)\((.*)\)", script, re.S)
        self._events(match.group(1), json.loads(match.group(2)))


@pytest.fixture
def robin_api(monkeypatch, events, fast):
    """Fábrica de RobinAPI em modo simulação, lendo o .env e o memory.md temporários."""
    monkeypatch.setattr(api_module, "MOCK", True)
    created = []

    def make():
        robin = api_module.RobinAPI()
        robin._attach(FakeWindow(events), None)
        created.append(robin)
        return robin

    yield make
    for robin in created:
        robin._gemini.clear()


def history(service) -> list[tuple[str, str]]:
    return [(c.role, _content_text(c)) for c in service._history()]


def send_and_wait(service, events, text: str, gen_id: int) -> dict:
    service.send(text, gen_id)
    return events.wait("onDone", gen_id)
