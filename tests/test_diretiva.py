"""A diretiva é neutra (repositório público): nenhum nome fixo de pessoa; o nome, se houver, vem só do
núcleo escrito pela própria pessoa. E o bloco SEGURANÇA deixa claro que o Robin não executa nada."""

import re
from pathlib import Path

from robin import tools
from robin.config import APP_USER_MODEL_ID, MEMORY_SEED
from robin.gemini import system_instruction

ROOT = Path(__file__).resolve().parent.parent
AUTHOR = APP_USER_MODEL_ID.split(".")[0]  # o único lugar do código com o usuário do GitHub do autor


def _render(core: str) -> str:
    return system_instruction(core=core, memory="(nada ainda)", conversations="(nenhuma ainda)")


def _seed() -> str:
    return MEMORY_SEED.read_text(encoding="utf-8")


def test_diretiva_sem_nome_fixo_com_nucleo_vazio_e_com_a_semente():
    for core in ("", _seed()):
        text = _render(core)
        assert AUTHOR.lower() not in text.lower()
        assert "SOBRE A PESSOA QUE VOCÊ AJUDA (escrito por ela; vale acima do resto):" in text
        assert "não invente nome nem apelido" in text
        assert "{" not in text.replace(core, "")  # todos os placeholders preenchidos


def test_semente_tem_nome_em_branco_para_preencher():
    seed = _seed()
    assert re.search(r"^- \*\*Nome\*\*:\s*$", seed, re.M)
    assert AUTHOR.lower() not in seed.lower()


def test_nome_so_vem_do_nucleo():
    text = _render(_seed().replace("- **Nome**:", "- **Nome**: Alex"))
    assert text.count("Alex") == 1  # só a linha do núcleo


def test_seguranca_diz_que_nao_executa_nem_edita_arquivos():
    text = _render("")
    security = text.split("SEGURANÇA (inegociável)", 1)[1].split("AGORA:", 1)[0]
    assert "não é um agente de código" in security
    assert "não executa nada" in security and "não edita arquivos" in security
    assert "mostre o comando e espere a pessoa conferir e rodar" in security


def test_textos_das_ferramentas_sem_nome_fixo():
    described = [tool.description for tool in tools.declarations()]
    described += [str(tool.parameters_json_schema) for tool in tools.declarations()]
    described += [tools._PINNED_REFUSAL, tools._PENDING_NOTE]
    assert not [text for text in described if AUTHOR.lower() in text.lower()]


def test_nome_do_autor_so_no_app_user_model_id():
    """Código, interface, mock, prévias e testes: o nome do autor só aparece no AppUserModelID."""
    patterns = ("robin/*.py", "ui/*.html", "ui/js/*.js", "ui/css/*.css", "scripts/*.py", "scripts/*.sql",
                "design-previews/*.html", "tests/*.py", "memory.example.md", "app.py")
    found = []
    for pattern in patterns:
        for path in ROOT.glob(pattern):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if AUTHOR.lower() in line.lower() and APP_USER_MODEL_ID not in line:
                    found.append(f"{path.relative_to(ROOT)}:{number}")
    assert not found, found
