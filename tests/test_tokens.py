"""Tokens da interface: nenhuma cor nem tamanho fixo fora do tokens.css, e todo var(--x) existe."""

import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"
TOKENS = (UI / "css" / "tokens.css").read_text(encoding="utf-8")
STYLES = {name: (UI / "css" / name).read_text(encoding="utf-8") for name in ("base.css", "components.css", "animations.css")}

COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(", re.I)
PX = re.compile(r"(?<![\w.-])\d*\.?\d+px\b")


def _sem_comentarios(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_nenhuma_cor_fora_dos_tokens():
    for name, css in STYLES.items():
        found = COLOR.findall(_sem_comentarios(css))
        assert not found, f"{name}: cor fixa {found[:3]}"
    index = (UI / "index.html").read_text(encoding="utf-8")
    styles = " ".join(re.findall(r'style="([^"]*)"', index))
    assert not COLOR.findall(styles), "index.html: cor fixa num style"
    for script in (UI / "js").glob("*.js"):
        found = re.findall(r"[\"'`]#[0-9a-fA-F]{3,8}\b", script.read_text(encoding="utf-8"))
        assert not found, f"{script.name}: cor fixa {found[:3]}"


def test_nenhum_tamanho_em_px_fora_dos_tokens():
    for name, css in STYLES.items():
        # Única exceção: a condição de @media/@container, que o CSS não deixa usar var().
        rules = re.sub(r"@(?:container|media)[^{]*\{", "", _sem_comentarios(css))
        found = PX.findall(rules)
        assert not found, f"{name}: tamanho fixo {found[:5]}"


def test_todo_token_usado_existe():
    defined = set(re.findall(r"(--[\w-]+)\s*:", TOKENS))
    for css in STYLES.values():
        defined |= set(re.findall(r"(--[\w-]+)\s*:", css))
    index = (UI / "index.html").read_text(encoding="utf-8")
    for name, text in {**STYLES, "index.html": index}.items():
        missing = set(re.findall(r"var\((--[\w-]+)", text)) - defined
        assert not missing, f"{name}: tokens inexistentes {sorted(missing)}"
