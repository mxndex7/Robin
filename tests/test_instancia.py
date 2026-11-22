"""Origem e build, instancia.json e a segunda execução: mesmo build traz o aberto para a frente;
outro build (ex.: o exe antigo do iniciar com o Windows) avisa pela bandeja e não o traz."""

import os
import time

import app
from robin import build


def _fake_repo(root, commit="a721f65d00000000000000000000000000000000", packed=False):
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    if packed:
        (git / "packed-refs").write_text(f"# pack-refs\n{commit} refs/heads/main\n", encoding="utf-8")
    else:
        (git / "refs" / "heads" / "main").write_text(commit + "\n", encoding="utf-8")
    (root / "robin").mkdir()
    (root / "robin" / "x.py").write_text("x = 1\n", encoding="utf-8")


def test_codigo_fonte_tem_commit_e_data_dos_arquivos(tmp_path):
    _fake_repo(tmp_path)
    info = build.source_build(tmp_path)
    assert info["origin"] == "source" and info["build"].startswith("a721f65 ")
    assert info["label"].startswith("código-fonte, commit a721f65, arquivos de ")

    # Editar o código (sem commit) já é outro build: a janela aberta está com o código velho.
    later = time.time() + 120
    os.utime(tmp_path / "robin" / "x.py", (later, later))
    assert not build.same_build(info, build.source_build(tmp_path))


def test_commit_em_packed_refs(tmp_path):
    _fake_repo(tmp_path, packed=True)
    assert build.source_build(tmp_path)["build"].startswith("a721f65 ")


def test_exe_usa_a_data_do_arquivo(tmp_path):
    exe = tmp_path / "Robin.exe"
    exe.write_bytes(b"MZ")
    stamp = time.mktime((2026, 10, 27, 13, 20, 0, 0, 0, -1))
    os.utime(exe, (stamp, stamp))
    assert build.exe_build(exe)["label"] == "Robin.exe de 27/10 13:20"


def test_instancia_json_vale_so_para_o_mesmo_processo(tmp_path):
    mine = {"origin": "source", "path": "C:\\Robin", "build": "a721f65 31/10 15:00", "label": "código-fonte"}
    build.write_instance_file(tmp_path, mine)
    assert build.running_build(tmp_path, os.getpid())["label"] == "código-fonte"
    # Outro pid (o arquivo é de uma execução antiga): vale o executável do processo.
    other = build.running_build(tmp_path, os.getppid() or 4)
    assert other is not None and other.get("label") != "código-fonte"


def _second_run(monkeypatch, running, mine):
    calls = {"show": 0, "notice": []}
    monkeypatch.setattr(app, "running_pid", lambda key, timeout=5.0: 1234)
    monkeypatch.setattr(app, "running_build", lambda data_dir, pid: running)
    monkeypatch.setattr(app, "notify_running", lambda key, message, timeout=5.0: calls.__setitem__("show", calls["show"] + 1))
    monkeypatch.setattr(app, "tray_notice", lambda text, icon: calls["notice"].append(text))
    app._second_run("Robin-x", mine)
    return calls


def test_segunda_execucao_do_mesmo_build_traz_o_aberto(monkeypatch):
    same = {"origin": "exe", "path": "C:\\dist\\Robin.exe", "build": "27/10 13:20", "label": "Robin.exe de 27/10 13:20"}
    calls = _second_run(monkeypatch, {**same, "pid": 1234}, same)
    assert calls == {"show": 1, "notice": []}


def test_segunda_execucao_de_outro_build_avisa_pela_bandeja(monkeypatch, caplog):
    old = {"origin": "exe", "path": "C:\\dist\\Robin.exe", "build": "27/10 13:20", "label": "Robin.exe de 27/10 13:20", "pid": 1234}
    mine = {"origin": "source", "path": "C:\\Robin2", "build": "a721f65 31/10 15:00", "label": "código-fonte, commit a721f65"}
    calls = _second_run(monkeypatch, old, mine)
    assert calls["show"] == 0  # não traz o antigo para a frente (pareceria a versão nova)
    assert calls["notice"] == ["Já existe um Robin aberto (Robin.exe de 27/10 13:20). Use --replace para trocar."]
    assert "Use --replace" in caplog.text and "código-fonte, commit a721f65" in caplog.text
