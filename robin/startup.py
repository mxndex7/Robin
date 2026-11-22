"""Iniciar com o Windows: valor "Robin" em HKCU\\...\\Run com o Robin.exe e --hidden.
Só o exe se registra, nunca o código-fonte (quem decide é desktop.py)."""

import winreg
from pathlib import Path
from typing import Optional

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Robin"
HIDDEN_ARG = "--hidden"


def command_for(exe: Path) -> str:
    return f'"{exe}" {HIDDEN_ARG}'


def registered(key_path: str = RUN_KEY) -> Optional[str]:
    """Comando registrado hoje, ou None."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return None
    return str(value)


def is_enabled(exe: Path, key_path: str = RUN_KEY) -> bool:
    """Ligado só se o valor aponta para este exe (outro caminho, ex.: pasta renomeada, conta como desligado)."""
    command = registered(key_path)
    return command is not None and command.strip().lower() == command_for(exe).lower()


def set_enabled(exe: Path, enabled: bool, key_path: str = RUN_KEY) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command_for(exe))
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass


def apply_default(exe: Path, choice: str, key_path: str = RUN_KEY) -> str:
    """Na abertura do exe. choice é o ROBIN_AUTOSTART do .env ("" na primeira execução, "1" ou "0").
    - primeira execução: liga e devolve "1" (para gravar no .env);
    - ligado, mas o valor aponta para outro caminho (o exe mudou de pasta): corrige o caminho;
    - desligado: não mexe."""
    if not choice:
        set_enabled(exe, True, key_path)
        return "1"
    if choice == "1" and registered(key_path) is not None and not is_enabled(exe, key_path):
        set_enabled(exe, True, key_path)
    return choice
