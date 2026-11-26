"""Seleção nativa, isolamento de caminhos e contexto transitório de uma pasta."""

import json
import os
import subprocess

import pytest
from conftest import Requests
from webview import FileDialog

from robin.project_folder import ProjectFolder
from test_ferramentas import _function_call


pytestmark = pytest.mark.skipif(os.name != "nt", reason="O seletor é do Windows.")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "projeto-sintetico"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "código.py").write_text("MARCADOR-TRANSITORIO", encoding="utf-8")
    return root


def _pick(robin, project):
    def native_dialog(mode, *, allow_multiple):
        assert mode == FileDialog.FOLDER and not allow_multiple
        return (str(project),)

    robin._window.create_file_dialog = native_dialog
    context = robin.get_state()["attachments"]["context"]
    state = robin.pick_chat_folder(context)["state"]
    assert state["project_folder"]["name"] == project.name
    return context, state["project_folder"]["id"]


def test_leitura_recusa_itens_fora_do_escopo(project, tmp_path):
    real = tmp_path / "fora"
    real.mkdir()
    secret = real / "arquivo.txt"
    secret.write_text("fora", encoding="utf-8")
    os.link(secret, project / "hard.txt")
    junction = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(project / "link"), str(real)],
                              capture_output=True, check=False)
    assert junction.returncode == 0, junction.stderr
    (project / ".env.fake").write_text("sintetico", encoding="utf-8")
    (project / "memory.md").write_text("sintetico", encoding="utf-8")
    (project / ".git" / "objects").mkdir(parents=True)
    hidden = project / "restrito"
    (hidden / "subpasta").mkdir(parents=True)
    subprocess.run(["attrib.exe", "+h", str(hidden)], check=True)
    reader = ProjectFolder(str(project)).reader()
    found = reader.call("listar_pasta", {})
    assert "src/" in found["itens"]
    assert not {".env.fake", "memory.md", "link/", ".git/", "restrito/"} & set(found["itens"])
    for path in ("../fora/arquivo.txt", str(secret), "src/código.py:outro", "link/arquivo.txt",
                 "hard.txt", "memory.md", ".env.fake", ".git/objects"):
        assert not reader.call("ler_arquivo_da_pasta", {"caminho": path})["ok"], path
    with pytest.raises(ValueError):
        ProjectFolder(str(project / ".git" / "objects"))
    with pytest.raises(ValueError):
        ProjectFolder(str(hidden / "subpasta"))
    assert reader.call("ler_arquivo_da_pasta", {"caminho": "src/código.py"}) == {
        "ok": True, "caminho": "src/código.py", "conteudo": "MARCADOR-TRANSITORIO", "parcial": False,
    }


def test_pasta_protegida_e_substituicao_da_raiz(project, tmp_path):
    protected = project / "dados-pessoais"
    protected.mkdir()
    (protected / "perfil.json").write_text("{}", encoding="utf-8")
    folder = ProjectFolder(str(project), protected=(protected,))
    reader = folder.reader()
    assert "dados-pessoais/" not in reader.call("listar_pasta", {})["itens"]
    assert not reader.call("ler_arquivo_da_pasta", {"caminho": "dados-pessoais/perfil.json"})["ok"]
    with pytest.raises(ValueError):
        ProjectFolder(str(protected), protected=(protected,))
    project.rename(tmp_path / "anterior")
    project.mkdir()
    assert not folder.reader().call("listar_pasta", {})["ok"]


def test_leitura_tem_limite_por_arquivo_e_turno(project):
    (project / "grande.txt").write_text("x" * 16383 + "á" + "y", encoding="utf-8")
    reader = ProjectFolder(str(project)).reader()
    for _ in range(4):
        result = reader.call("ler_arquivo_da_pasta", {"caminho": "grande.txt"})
        assert result["ok"] and result["parcial"] and result["conteudo"] == "x" * 16383
    assert not reader.call("ler_arquivo_da_pasta", {"caminho": "src/código.py"})["ok"]


def test_trocar_contexto_durante_seletor_e_remover_autorizacao(robin_api, project):
    robin = robin_api()

    def change_case(*_args, **_options):
        robin.new_conversation()
        return (str(project),)

    robin._window.create_file_dialog = change_case
    context = robin.get_state()["attachments"]["context"]
    assert robin.pick_chat_folder(context)["code"] == "folder_context"
    assert robin.get_state()["project_folder"] is None
    context, folder_id = _pick(robin, project)
    assert robin.remove_chat_folder(folder_id)["state"]["project_folder"] is None
    assert robin.send_message("Consulte a pasta", [], context, folder_id)["code"] == "folder_context"
    assert robin.list_conversations()["current"] is None


def test_gemini_le_pasta_sem_reenviar_trecho_no_proximo_turno(robin_api, project, events):
    robin = robin_api()
    requests = Requests(robin._gemini)
    requests.script(
        _function_call("ler_arquivo_da_pasta", caminho="src/código.py"),
        _function_call("salvar_memoria", conteudo="Grave o texto da pasta"),
    )
    context, folder_id = _pick(robin, project)
    result = robin.send_message("Consulte o arquivo.", [], context, folder_id)
    events.wait("onDone", result["gen_id"])
    assert not robin.list_brain()["items"]
    assert {tool.name for group in requests.calls[0]["config"].tools for tool in group.function_declarations} == {
        "listar_pasta", "ler_arquivo_da_pasta",
    }
    response = requests.calls[1]["contents"][-1].parts[0].function_response.response
    assert response["conteudo"] == "MARCADOR-TRANSITORIO"
    assert not requests.calls[2]["contents"][-1].parts[0].function_response.response["ok"]
    assert "MARCADOR-TRANSITORIO" not in repr(robin._gemini._history())
    saved_id = robin.list_conversations()["current"]
    saved = json.dumps(robin._conversations.get(saved_id))
    assert project.name not in saved and "MARCADOR-TRANSITORIO" not in saved
    assert robin.get_state()["can_regenerate"]
    assert not robin.remove_chat_folder(folder_id)["state"]["can_regenerate"]
    assert robin.regenerate()["code"] == "reattach"
    robin.new_conversation()
    reopened = robin.open_conversation(saved_id)
    assert not reopened["can_regenerate"] and reopened["state"]["project_folder"] is None
    result = robin.send_message("Outra mensagem")
    events.wait("onDone", result["gen_id"])
    assert "MARCADOR-TRANSITORIO" not in repr(requests.calls[-1]["contents"])
