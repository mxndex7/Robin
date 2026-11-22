"""Atalho global: texto ("Ctrl+Alt+R") ↔ modificadores e tecla virtual do RegisterHotKey."""

from typing import NamedTuple

DEFAULT_HOTKEY = "Ctrl+Alt+R"

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
MOD_NOREPEAT = 0x4000  # segurar as teclas não repete o WM_HOTKEY

_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_ORDER = ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))

# Teclas aceitas e o código virtual de cada uma. Letras e dígitos são o próprio caractere.
KEYS = {
    **{char: ord(char) for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
    **{f"F{number}": 0x6F + number for number in range(1, 25)},
    "Space": 0x20, "Tab": 0x09, "PageUp": 0x21, "PageDown": 0x22, "End": 0x23, "Home": 0x24,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28, "Insert": 0x2D, "Delete": 0x2E,
}
_KEYS_BY_NAME = {name.lower(): name for name in KEYS}


class Hotkey(NamedTuple):
    modifiers: int
    vk: int
    text: str  # forma canônica, ex.: "Ctrl+Alt+R"


def parse_hotkey(text: str) -> Hotkey:
    """Lê "Ctrl+Alt+R" (sem diferenciar maiúsculas). ValueError com a mensagem para a interface."""
    parts = [part.strip() for part in (text or "").split("+")]
    if not any(parts) or not all(parts):
        raise ValueError("Atalho vazio ou mal formado (ex.: Ctrl+Alt+R).")
    *names, key = parts
    if key.lower() in _MODIFIERS:
        raise ValueError("Falta a tecla principal (ex.: Ctrl+Alt+R).")

    modifiers = 0
    for name in names:
        flag = _MODIFIERS.get(name.lower())
        if flag is None:
            raise ValueError(f"Modificador desconhecido: {name}.")
        if modifiers & flag:
            raise ValueError(f"{name} repetido no atalho.")
        modifiers |= flag

    canonical = _KEYS_BY_NAME.get(key.lower())
    if canonical is None:
        raise ValueError(f"Tecla não suportada: {key}.")
    if not modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        raise ValueError("Use Ctrl, Alt ou Win na combinação.")
    labels = [label for flag, label in _ORDER if modifiers & flag]
    return Hotkey(modifiers, KEYS[canonical], "+".join([*labels, canonical]))
