"""Robin sempre no PC, sem janela de verdade: parse do atalho, mutex por pasta de dados, pedido de
outra execução, registro do atalho, chave Run (numa subchave temporária do HKCU, apagada no fim) e
as decisões do Desktop (atalho, bandeja, X, Sair) com peças falsas."""

import threading
import uuid
import winreg
from pathlib import Path
from types import SimpleNamespace

import pytest

from robin import startup
from robin.hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, parse_hotkey
from robin.instance import WM_ROBIN_QUIT, MessageWindow, SingleInstance, instance_key, notify_running


@pytest.mark.parametrize("text, canonical", [
    ("Ctrl+Alt+R", "Ctrl+Alt+R"),
    ("alt + ctrl + r", "Ctrl+Alt+R"),
    ("Control+Shift+space", "Ctrl+Shift+Space"),
    ("Win+F12", "Win+F12"),
])
def test_atalho_valido(text, canonical):
    assert parse_hotkey(text).text == canonical


def test_atalho_padrao():
    hotkey = parse_hotkey("Ctrl+Alt+R")
    assert (hotkey.modifiers, hotkey.vk) == (MOD_CONTROL | MOD_ALT, ord("R"))


@pytest.mark.parametrize("text", ["", "R", "Shift+R", "Ctrl+Alt", "Ctrl+Ctrl+R", "Hyper+R", "Ctrl+Alt+Ç", "Ctrl++R"])
def test_atalho_invalido(text):
    with pytest.raises(ValueError):
        parse_hotkey(text)


def test_mutex_um_por_pasta_de_dados(tmp_path):
    key = instance_key(tmp_path / "a")
    assert key == instance_key(Path(str(tmp_path / "A")))  # sem diferenciar maiúsculas
    assert key != instance_key(tmp_path / "b")
    first, second = SingleInstance(key), SingleInstance(key)
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_pedido_de_outra_execucao_e_atalho(tmp_path):
    key = instance_key(tmp_path)
    quit_requested = threading.Event()
    window = MessageWindow(key, on_hotkey=lambda: None, on_show=lambda: None, on_quit=quit_requested.set)
    window.start()
    try:
        assert notify_running(key, WM_ROBIN_QUIT) is True
        assert quit_requested.wait(5)
        assert notify_running(instance_key(tmp_path / "outra"), timeout=0.2) is False

        rare = parse_hotkey("Ctrl+Alt+Shift+F24")
        assert window.register(rare) is None
        assert window.register(rare) is None  # o mesmo de novo não conta como "em uso"
        other = MessageWindow(instance_key(tmp_path / "x"), lambda: None, lambda: None, lambda: None)
        other.start()
        try:
            assert "em uso" in other.register(rare)
        finally:
            other.stop()
    finally:
        window.stop()
    assert rare.modifiers == MOD_CONTROL | MOD_ALT | MOD_SHIFT


@pytest.fixture
def run_key():
    path = rf"Software\RobinTestes\{uuid.uuid4().hex}"
    yield path
    for sub in (path, r"Software\RobinTestes"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass


def test_chave_run(run_key):
    exe = Path(r"C:\Apps\Robin\Robin.exe")
    assert startup.registered(run_key) is None
    assert startup.apply_default(exe, "", run_key) == "1"  # primeira execução do exe liga
    assert startup.registered(run_key) == r'"C:\Apps\Robin\Robin.exe" --hidden'
    assert startup.is_enabled(exe, run_key)

    moved = Path(r"C:\Outro\Robin\Robin.exe")
    assert not startup.is_enabled(moved, run_key)
    assert startup.apply_default(moved, "1", run_key) == "1"  # pasta mudou: corrige o caminho
    assert startup.is_enabled(moved, run_key)

    startup.set_enabled(moved, False, run_key)
    assert startup.registered(run_key) is None
    assert startup.apply_default(moved, "0", run_key) == "0"  # desligado pelo usuário: não religa
    assert startup.registered(run_key) is None


# ---------- Desktop: decisões de janela, bandeja e atalho (sem janela de verdade) ----------

class FakeFrame:
    def __init__(self, in_front=False):
        self.in_front = in_front
        self.calls = []

    def is_in_front(self, grace=0.0):
        self.calls.append(("is_in_front", grace))
        return self.in_front

    def bring_to_front(self):
        self.calls.append("bring_to_front")

    def hide(self):
        self.calls.append("hide")

    def run_on_ui(self, func, wait=True):
        func()


class FakeTray:
    def __init__(self):
        self.hints, self.tooltips, self.disposed = [], [], 0

    def show_hint(self, text):
        self.hints.append(text)

    def set_tooltip(self, text):
        self.tooltips.append(text)

    def dispose(self):
        self.disposed += 1


class FakeMessages:
    def __init__(self, busy=()):
        self.busy = set(busy)
        self.registered = []

    def register(self, hotkey):
        if hotkey.text in self.busy:
            return f"{hotkey.text} já está em uso por outro programa."
        self.registered.append(hotkey.text)
        return None


class FakeWindow:
    def __init__(self):
        self.destroyed = threading.Event()

    def destroy(self):
        self.destroyed.set()


@pytest.fixture
def desktop(events):
    from robin.desktop import Desktop

    def make(in_front=False, busy=()):
        frame, tray, messages, window = FakeFrame(in_front), FakeTray(), FakeMessages(busy), FakeWindow()
        item = Desktop(window, frame, events, "Robin-teste", messages=messages, tray=tray)
        return SimpleNamespace(desktop=item, frame=frame, tray=tray, messages=messages, window=window)

    return make


def test_atalho_esconde_se_na_frente_e_traz_se_nao(desktop, events):
    front = desktop(in_front=True)
    front.desktop.toggle()
    assert front.frame.calls == [("is_in_front", 0.0), "hide"]

    behind = desktop(in_front=False)
    behind.desktop.toggle()
    assert behind.frame.calls == [("is_in_front", 0.0), "bring_to_front"]
    assert events.of("onSummon") == [{}]  # a interface foca o composer


def test_clique_na_bandeja_usa_a_folga_do_foco(desktop):
    from robin.desktop import TRAY_GRACE_SECONDS

    item = desktop(in_front=True)
    item.desktop.toggle(TRAY_GRACE_SECONDS)
    assert item.frame.calls[0] == ("is_in_front", TRAY_GRACE_SECONDS)


def test_x_esconde_e_avisa_so_na_primeira_vez(files, desktop):
    item = desktop()
    item.desktop._load_hotkey()
    item.desktop.hide_to_tray(hint=True)
    item.desktop.hide_to_tray(hint=True)
    assert item.frame.calls == ["hide", "hide"]
    assert item.tray.hints == ["O Robin continua na bandeja. Ctrl+Alt+R abre."]
    assert "ROBIN_TRAY_HINT=1" in files.env.read_text(encoding="utf-8")


def test_fechamento_do_usuario_esconde_e_o_resto_fecha(desktop):
    item = desktop()
    assert item.desktop.close_requested("UserClosing") is True  # X, Alt+F4, "Fechar janela"
    assert item.frame.calls == ["hide"]
    for reason in ("WindowsShutDown", "TaskManagerClosing", "ApplicationExitCall"):
        assert item.desktop.close_requested(reason) is False  # nunca segura o desligamento do Windows


def test_sair_fecha_de_verdade(desktop):
    item = desktop()
    item.desktop.quit()
    assert item.window.destroyed.wait(5)
    assert item.tray.disposed == 1
    assert item.desktop.close_requested("UserClosing") is False  # o fechamento que o Sair dispara passa
    item.desktop.quit()  # duas vezes não faz nada
    assert item.tray.disposed == 1


def test_atalho_em_uso_vira_aviso_e_pode_ser_trocado(files, desktop):
    item = desktop(busy={"Ctrl+Alt+R"})
    item.desktop._load_hotkey()
    settings = item.desktop.settings()
    assert settings["hotkey"] == "Ctrl+Alt+R"
    assert "em uso" in settings["hotkey_error"]

    assert item.desktop.set_hotkey("ctrl+shift+space") == {"ok": True, "hotkey": "Ctrl+Shift+Space"}
    assert item.desktop.settings()["hotkey_error"] == ""
    assert "ROBIN_HOTKEY=Ctrl+Shift+Space" in files.env.read_text(encoding="utf-8")
    assert item.tray.tooltips == ["Robin (Ctrl+Shift+Space)"]

    assert item.desktop.set_hotkey("Shift+R")["code"] == "invalid_hotkey"
    assert item.desktop.set_hotkey("Ctrl+Alt+R")["code"] == "hotkey_busy"
    assert item.desktop.settings()["hotkey"] == "Ctrl+Shift+Space"  # o anterior continua


def test_robin_hotkey_do_env(files, desktop):
    files.env.write_text("ROBIN_HOTKEY=Alt+Win+J\n", encoding="utf-8")
    item = desktop()
    item.desktop._load_hotkey()
    assert item.messages.registered == ["Alt+Win+J"]

    files.env.write_text("ROBIN_HOTKEY=Shift+J\n", encoding="utf-8")
    invalid = desktop()
    invalid.desktop._load_hotkey()
    assert invalid.messages.registered == ["Ctrl+Alt+R"]  # inválido: volta ao padrão e avisa
    assert "ROBIN_HOTKEY=Shift+J" in invalid.desktop.settings()["hotkey_error"]


def test_codigo_fonte_nunca_se_registra_no_windows(desktop):
    item = desktop()
    assert item.desktop.autostart_available is False  # os testes rodam pelo código-fonte
    assert item.desktop.autostart_enabled() is None
    assert item.desktop.set_autostart(True)["code"] == "no_autostart"


def test_api_x_esconde_e_configuracoes_trazem_o_desktop(robin_api, desktop):
    robin = robin_api()
    item = desktop()
    item.desktop._load_hotkey()
    robin._attach(None, None, item.desktop)

    robin.window_close()
    assert item.frame.calls == ["hide"]
    settings = robin.get_settings()
    assert settings["hotkey"] == "Ctrl+Alt+R" and settings["autostart_available"] is False
    assert robin.set_hotkey("Ctrl+Alt+J") == {"ok": True, "hotkey": "Ctrl+Alt+J"}
    assert robin.set_settings({"autostart": True})["code"] == "no_autostart"


def test_exe_liga_iniciar_com_o_windows_so_na_primeira_vez(files, desktop, run_key, monkeypatch):
    from robin import config

    exe = Path(r"C:\Apps\Robin\Robin.exe")
    monkeypatch.setattr(config, "EXE_FILE", exe)
    monkeypatch.setattr(config, "MOCK", False)
    monkeypatch.setattr(config, "DATA_DIR_OVERRIDDEN", False)
    for function in (startup.registered, startup.is_enabled, startup.set_enabled, startup.apply_default):
        monkeypatch.setattr(function, "__defaults__", (run_key,))  # a subchave temporária no lugar da Run

    first = desktop()
    first.desktop._apply_autostart_default()
    assert startup.registered(run_key) == r'"C:\Apps\Robin\Robin.exe" --hidden'
    assert "ROBIN_AUTOSTART=1" in files.env.read_text(encoding="utf-8")
    assert first.desktop.settings()["autostart"] is True

    assert first.desktop.set_autostart(False) == {"ok": True}
    assert startup.registered(run_key) is None
    desktop().desktop._apply_autostart_default()  # próxima abertura: a escolha do usuário fica
    assert startup.registered(run_key) is None

    monkeypatch.setattr(config, "DATA_DIR_OVERRIDDEN", True)  # cópia de teste: nunca registra sozinha
    files.env.write_text("", encoding="utf-8")
    desktop().desktop._apply_autostart_default()
    assert startup.registered(run_key) is None
