"""Pasta de dados única: migração dos dados antigos, .gitignore, simulação isolada (mock\\),
ROBIN_DATA_DIR, semente da memória e o sync Git só com a pasta de dados como repositório."""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values

from robin import datadir
from robin import memory as memory_store

ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, text: str, age: float = 0) -> Path:
    """Cria o arquivo com mtime `age` segundos no passado."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def legacy(tmp_path):
    """Os dois lugares antigos: dist\\Robin2 (ao lado do Robin2.exe) e a raiz do projeto."""
    dist, root = tmp_path / "dist" / "Robin2", tmp_path / "projeto"
    dist.mkdir(parents=True)
    root.mkdir()
    return dist, root


# ---------- migração ----------

def test_migra_o_env_mais_recente_com_chave_e_a_memoria_mais_recente(tmp_path, legacy, caplog):
    caplog.set_level(logging.INFO, logger="robin.datadir")
    dist, root = legacy
    _write(dist / ".env", "GEMINI_API_KEY=chave-do-exe\nGEMINI_MODEL=gemini-3.6-flash\n", age=100)
    _write(root / ".env", "GEMINI_API_KEY=chave-da-raiz\n", age=10)
    _write(dist / "memory.md", "# memória do exe\n", age=10)
    _write(root / "memory.md", "# memória da raiz\n", age=100)
    target = tmp_path / "Robin"
    target.mkdir()

    copied = datadir.migrate(target, [dist, root])

    assert copied == [root / ".env", dist / "memory.md"]
    assert dotenv_values(target / ".env") == {"GEMINI_API_KEY": "chave-da-raiz"}
    assert (target / "memory.md").read_text(encoding="utf-8") == "# memória do exe\n"
    # Os antigos ficam onde estavam.
    assert (dist / ".env").exists() and (root / ".env").exists()
    assert (dist / "memory.md").exists() and (root / "memory.md").exists()
    # A migração vai para o log, só com os caminhos: o conteúdo do .env nunca aparece.
    messages = [record.getMessage() for record in caplog.records if record.name == "robin.datadir"]
    assert len(messages) == 2 and all("Migração:" in message for message in messages)
    assert not any("chave-" in message for message in messages)


def test_env_sem_chave_nao_migra(tmp_path, legacy):
    dist, root = legacy
    _write(dist / ".env", "GEMINI_API_KEY=\nGEMINI_MODEL=gemini-3.6-flash\n", age=1)
    _write(root / ".env", "ROBIN_GIT_SYNC=0\n", age=1)
    target = tmp_path / "Robin"
    target.mkdir()

    assert datadir.migrate(target, [dist, root]) == []
    assert not (target / ".env").exists()


def test_env_sem_chave_perde_para_o_mais_antigo_com_chave(tmp_path, legacy):
    dist, root = legacy
    _write(dist / ".env", "GEMINI_API_KEY=chave-antiga\n", age=100)
    _write(root / ".env", "GEMINI_MODEL=gemini-3.8-flash\n", age=1)
    target = tmp_path / "Robin"
    target.mkdir()

    datadir.migrate(target, [dist, root])

    assert dotenv_values(target / ".env") == {"GEMINI_API_KEY": "chave-antiga"}


def test_nao_sobrescreve_o_que_ja_esta_na_pasta_de_dados(tmp_path, legacy):
    dist, root = legacy
    _write(dist / ".env", "GEMINI_API_KEY=chave-antiga\n")
    _write(root / "memory.md", "# antiga\n")
    target = tmp_path / "Robin"
    _write(target / ".env", "GEMINI_API_KEY=chave-atual\n")
    _write(target / "memory.md", "# atual\n")

    assert datadir.migrate(target, [dist, root]) == []
    assert dotenv_values(target / ".env") == {"GEMINI_API_KEY": "chave-atual"}
    assert (target / "memory.md").read_text(encoding="utf-8") == "# atual\n"


def test_sem_dados_antigos_nao_faz_nada(tmp_path):
    target = tmp_path / "Robin"
    target.mkdir()
    assert datadir.migrate(target, [tmp_path / "nao-existe", tmp_path]) == []
    assert list(target.iterdir()) == []


def test_ensure_cria_a_pasta_com_gitignore_sem_sobrescrever(tmp_path):
    target = tmp_path / "Robin"
    datadir.ensure(target)
    assert (target / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        ".env", "mock/", "brain.json", "brain.*.json", ".brain-*.tmp", "sync.json", ".sync-*.tmp", "instancia.json",
        "conversas/", "sync-conversas.json", "chatgpt-session.bin", ".chatgpt-*.tmp",
    ]

    # Um .gitignore antigo mantém as linhas do usuário e ganha só as que faltam (o cérebro, as conversas).
    (target / ".gitignore").write_text(".env\nmeu-arquivo", encoding="utf-8")
    datadir.ensure(target)
    assert (target / ".gitignore").read_text(encoding="utf-8") == (
        ".env\nmeu-arquivo\nmock/\nbrain.json\nbrain.*.json\n.brain-*.tmp\nsync.json\n.sync-*.tmp\ninstancia.json\n"
        "conversas/\nsync-conversas.json\nchatgpt-session.bin\n.chatgpt-*.tmp\n"
    )
    datadir.ensure(target)  # nada muda na segunda vez
    assert (target / ".gitignore").read_text(encoding="utf-8").count("brain.json") == 1


def test_brain_json_e_conversas_ficam_fora_do_git_da_pasta_de_dados(tmp_path):
    target = tmp_path / "Robin"
    datadir.ensure(target)
    (target / "conversas").mkdir()
    names = ("brain.json", "memory.md", "brain.corrompido-20261030-120000.json", ".brain-abc.tmp", "conversas/a.json")
    for name in names:
        (target / name).write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    ignored = subprocess.run(["git", "check-ignore", *names], cwd=target, capture_output=True, text=True).stdout.split()
    assert ignored == ["brain.json", "brain.corrompido-20261030-120000.json", ".brain-abc.tmp", "conversas/a.json"]


# ---------- pasta de dados no processo real (config lido do zero) ----------

def _run_robin(code: str, data_dir: Path, **env: str) -> dict:
    """Roda `code` num Python novo, com o robin importado do zero, e devolve o JSON que ele imprime."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("ROBIN_")}
    environment.update(ROBIN_DATA_DIR=str(data_dir), **env)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_robin_data_dir_e_a_pasta_usada(tmp_path):
    data_dir = tmp_path / "segunda-copia"
    paths = _run_robin(
        "import json; from robin import config, datadir; datadir.prepare();"
        "print(json.dumps({'dir': str(config.DATA_DIR), 'env': str(config.ENV_FILE), 'mem': str(config.MEMORY_FILE)}))",
        data_dir,
    )
    assert Path(paths["dir"]) == data_dir.resolve()
    assert Path(paths["env"]) == data_dir.resolve() / ".env"
    assert Path(paths["mem"]) == data_dir.resolve() / "memory.md"
    assert (data_dir / ".gitignore").exists()


def test_simulacao_grava_so_na_subpasta_mock(tmp_path):
    data_dir = tmp_path / "Robin"
    _write(data_dir / ".env", "GEMINI_API_KEY=chave-real\nGEMINI_MODEL=gemini-3.6-flash\n")
    _write(data_dir / "memory.md", "# memória real\n")
    code = """
import json
from robin import config, datadir
datadir.prepare()
from robin.api import RobinAPI
api = RobinAPI()
model = api.set_model("gemini-3.8-flash")
key = api.set_api_key("chave-da-simulacao")
saved = api.save_memory("# memória da simulação\\n")
api._gemini.clear()
print(json.dumps({"dir": str(config.DATA_DIR), "ok": [model["ok"], key["ok"], saved["ok"]]}))
"""
    result = _run_robin(code, data_dir, ROBIN_MOCK="1")

    mock_dir = data_dir.resolve() / "mock"
    assert Path(result["dir"]) == mock_dir
    assert result["ok"] == [True, True, True]
    assert dotenv_values(mock_dir / ".env") == {"GEMINI_MODEL": "gemini-3.8-flash", "GEMINI_API_KEY": "chave-da-simulacao"}
    assert (mock_dir / "memory.md").read_text(encoding="utf-8") == "# memória da simulação\n"
    # Os dados reais ficaram intactos.
    assert dotenv_values(data_dir / ".env") == {"GEMINI_API_KEY": "chave-real", "GEMINI_MODEL": "gemini-3.6-flash"}
    assert (data_dir / "memory.md").read_text(encoding="utf-8") == "# memória real\n"


# ---------- memória ----------

def test_memoria_nasce_da_semente(files, tmp_path, monkeypatch):
    seed = _write(tmp_path / "memory.example.md", "# Semente\n")
    monkeypatch.setattr(memory_store, "MEMORY_SEED", seed)
    files.memory.unlink()

    assert memory_store.read_memory() == "# Semente\n"
    assert files.memory.read_text(encoding="utf-8") == "# Semente\n"


def test_semente_do_projeto_existe_e_empacota():
    assert (ROOT / "memory.example.md").read_text(encoding="utf-8").strip()
    assert '"memory.example.md"' in (ROOT / "scripts" / "robin.spec").read_text(encoding="utf-8")


# ---------- sync Git ----------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git não instalado")


def _git_init(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(folder)], check=True, capture_output=True)


@needs_git
def test_git_disponivel_so_com_a_pasta_de_dados_como_raiz_do_repositorio(files, tmp_path, monkeypatch):
    assert memory_store.git_available() is False

    _git_init(files.dir)
    assert memory_store.git_available() is True

    # Uma pasta de dados dentro de outro repositório não conta.
    inner = tmp_path / "outro-repo" / "dados"
    _git_init(tmp_path / "outro-repo")
    inner.mkdir()
    monkeypatch.setattr(memory_store, "DATA_DIR", inner)
    assert memory_store.git_available() is False


def test_configuracoes_mostram_a_pasta_de_dados(files, robin_api):
    settings = robin_api().get_settings()
    assert Path(settings["data_dir"]) == files.dir
    assert settings["git_available"] is False
    result = robin_api().set_settings({"git_sync": True})
    assert result == {"ok": False, "code": "no_git", "error": "A pasta de dados não é um repositório Git."}
