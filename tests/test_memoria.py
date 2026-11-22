"""save_memory e get_memory pela RobinAPI: proteção contra salvar vazio e erros de leitura no log."""

import logging


def test_save_memory_recusa_vazio_sobre_conteudo(files, robin_api):
    files.memory.write_text("# Memória\n- item\n", encoding="utf-8")
    robin = robin_api()

    for empty in ("", "  \n\t"):
        result = robin.save_memory(empty)
        assert result["ok"] is False
        assert result["code"] == "confirm_empty"
    assert files.memory.read_text(encoding="utf-8") == "# Memória\n- item\n"

    assert robin.save_memory("", True)["ok"] is True  # a interface só manda depois da confirmação inline
    assert files.memory.read_text(encoding="utf-8") == ""
    assert robin.save_memory("  ")["ok"] is True  # vazio sobre vazio não pede confirmação


def test_save_memory_com_conteudo_salva(files, robin_api):
    robin = robin_api()
    assert robin.save_memory("# Nova memória\n") == {"ok": True, "sync": "local"}
    assert files.memory.read_text(encoding="utf-8") == "# Nova memória\n"
    assert robin.get_memory() == {"ok": True, "content": "# Nova memória\n", "source": "local"}


def test_get_memory_ilegivel_avisa_registra_no_log_e_nao_derruba_o_app(files, robin_api, caplog):
    caplog.set_level(logging.WARNING)
    original = "# Memória em latin-1: ação\n".encode("latin-1")
    files.memory.write_bytes(original)

    robin = robin_api()  # a sessão nasce mesmo com o memory.md ilegível
    result = robin.get_memory()

    assert result["ok"] is False
    assert result["code"] == "io"
    assert result["error"].startswith("Não foi possível ler o memory.md")
    assert any(
        record.name == "robin.api" and "get_memory falhou: UnicodeDecodeError" in record.getMessage()
        for record in caplog.records
    )
    # Sem saber se o arquivo está vazio, salvar vazio por cima é recusado.
    assert robin.save_memory("")["code"] == "io"
    assert files.memory.read_bytes() == original
