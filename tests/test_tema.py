"""Tema e paleta: escolhas no .env, tema do Windows, moldura da janela e cores iguais às do tokens.css."""

import re
from pathlib import Path

from robin import config

TOKENS = re.sub(r"/\*.*?\*/", "", (Path(__file__).resolve().parent.parent / "ui" / "css" / "tokens.css").read_text(encoding="utf-8"), flags=re.S)


def _colors(palette: str, theme: str) -> dict:
    """--bg e --text do bloco de cores de uma paleta e tema no tokens.css."""
    light = f'"{palette}"][data-theme="light"]'
    for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", TOKENS):
        if f'[data-palette="{palette}"]' in selector and (light in selector) == (theme == "light"):
            token = lambda name: re.search(rf"{name}:\s*(#[0-9a-fA-F]{{6}})", block).group(1).lower()  # noqa: E731
            return {"bg": token("--bg"), "text": token("--text")}
    raise AssertionError(f"tokens.css sem as cores de {palette}/{theme}")


class FakeFrame:
    mode = "custom"

    def __init__(self):
        self.themes = []
        self.palettes = []

    def set_theme(self, theme: str) -> None:
        self.themes.append(theme)

    def set_palette(self, palette: str) -> None:
        self.palettes.append(palette)

    def is_maximized(self) -> bool:
        return False


def test_cores_da_janela_iguais_aos_tokens():
    assert set(config.WINDOW_COLORS) == set(config.PALETTES)
    for palette in config.PALETTES:
        for theme in ("dark", "light"):
            assert config.WINDOW_COLORS[palette][theme] == _colors(palette, theme), f"{palette}/{theme}"


def test_paleta_padrao_e_invalida(files):
    assert config.palette_preference() == "brasa"
    files.env.write_text("ROBIN_PALETTE=roxa\n", encoding="utf-8")
    assert config.palette_preference() == "brasa"
    files.env.write_text("ROBIN_PALETTE=Ninho\n", encoding="utf-8")
    assert config.palette_preference() == "ninho"


def test_set_palette_grava_no_env_e_avisa_a_moldura(robin_api, files):
    robin = robin_api()
    frame = FakeFrame()
    robin._attach(robin._window, frame)
    assert robin.set_palette("caderno") == {"ok": True, "palette": "caderno"}
    assert "ROBIN_PALETTE=caderno" in files.env.read_text(encoding="utf-8")
    assert robin.get_settings()["palette"] == "caderno"
    assert frame.palettes == ["caderno"]
    result = robin.set_palette("roxa")
    assert result["ok"] is False and result["code"] == "invalid_palette"
    assert frame.palettes == ["caderno"]


def test_escolha_padrao_segue_o_windows(files):
    assert config.theme_preference() == "system"
    files.env.write_text("ROBIN_THEME=roxo\n", encoding="utf-8")
    assert config.theme_preference() == "system"
    files.env.write_text("ROBIN_THEME=Light\n", encoding="utf-8")
    assert config.theme_preference() == "light"


def test_resolve_o_tema_do_sistema(monkeypatch):
    monkeypatch.setattr(config, "system_theme", lambda: "light")
    assert config.resolve_theme("system") == "light"
    assert config.resolve_theme("dark") == "dark"
    assert config.resolve_theme("light") == "light"


def test_tema_do_windows_ilegivel_vira_escuro(monkeypatch):
    def unreadable(*_):
        raise OSError("sem a chave Personalize")

    monkeypatch.setattr(config.winreg, "OpenKey", unreadable)
    assert config.system_theme() == "dark"


def test_set_theme_grava_no_env_e_avisa_a_moldura(robin_api, files):
    robin = robin_api()
    frame = FakeFrame()
    robin._attach(robin._window, frame)

    assert robin.set_theme("light", False) == {"ok": True, "theme": "light"}
    assert "ROBIN_THEME=light" in files.env.read_text(encoding="utf-8")
    assert robin.get_settings()["theme"] == "light"
    assert frame.themes == ["light"]

    # "system": grava a escolha e a moldura segue o tema que a página está mostrando.
    assert robin.set_theme("system", True)["ok"]
    assert config.theme_preference() == "system"
    assert frame.themes == ["light", "dark"]


def test_set_theme_recusa_valor_invalido(robin_api, files):
    robin = robin_api()
    frame = FakeFrame()
    robin._attach(robin._window, frame)
    result = robin.set_theme("roxo", True)
    assert result["ok"] is False and result["code"] == "invalid_theme"
    assert "ROBIN_THEME" not in (files.env.read_text(encoding="utf-8") if files.env.exists() else "")
    assert frame.themes == []
