"""Robin: ponto de entrada. Cria a janela pywebview e liga a API à interface."""

import logging
import logging.handlers
import os
import socket
import sys
import time

import bottle
import webview

from robin import config, datadir
from robin.api import RobinAPI
from robin.build import current_build, running_build, same_build, write_instance_file
from robin.desktop import Desktop
from robin.instance import (
    WM_ROBIN_QUIT,
    WM_ROBIN_SHOW,
    SingleInstance,
    instance_key,
    notify_running,
    running_pid,
)
from robin.notice import tray_notice
from robin.window import WindowFrame, set_app_user_model_id

logger = logging.getLogger("robin.app")

# --replace: quanto esperar a execução aberta sair (o WebView2 leva alguns segundos para fechar).
REPLACE_TIMEOUT = 20.0


def _serve_ui_without_stale_cache() -> None:
    """O pywebview define Cache-Control e depois devolve bottle.static_file(), cujo HTTPResponse
    substitui todos os cabeçalhos: o no-cache se perde e a WebView2 (perfil persistente) passa a
    servir JS/CSS antigos do cache entre execuções. O 304 também não serve: a porta e o perfil são
    os mesmos no exe e no código-fonte, e o bottle compara só a data (If-Modified-Since >= data do
    arquivo), então o exe antigo confirmava o index.html/JS mais novos do cache, que importam
    módulos que ele não tem, e os botões morriam. Resposta sempre completa e nunca guardada."""
    original = bottle.static_file

    def static_file(*args, **kwargs):
        bottle.request.environ.pop("HTTP_IF_MODIFIED_SINCE", None)
        bottle.request.environ.pop("HTTP_IF_NONE_MATCH", None)
        response = original(*args, **kwargs)
        response.set_header("Cache-Control", "no-store")
        return response

    bottle.static_file = static_file


def _http_port() -> int:
    """Porta fixa (origem estável para o localStorage); se estiver ocupada, uma porta livre."""
    for port in (config.HTTP_PORT, 0):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return probe.getsockname()[1]
    return 0


def _setup_logging() -> None:
    """Console (quando existe) no nível do modo debug. Arquivo em LOG_FILE com avisos e erros de tudo
    e os registros informativos do próprio Robin (ex.: a migração da pasta de dados)."""
    config.WEBVIEW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = logging.handlers.RotatingFileHandler(config.LOG_FILE, maxBytes=512_000, backupCount=1, encoding="utf-8")
    log_file.setLevel(logging.INFO)
    handlers: list[logging.Handler] = [log_file]
    if sys.stderr:  # o exe --windowed não tem stderr
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    if not config.DEBUG:
        logging.getLogger("robin").setLevel(logging.INFO)  # as bibliotecas seguem só com avisos


def _second_run(key: str, mine: dict) -> None:
    """Já há um Robin aberto para esta pasta de dados. Mesmo build: pede para ele aparecer. Outro
    build (ex.: o exe antigo que inicia com o Windows na frente do código novo): não o traz para a
    frente (pareceria a versão nova) e avisa pela bandeja e no log."""
    running = running_build(config.DATA_DIR, running_pid(key))
    if running is None or same_build(running, mine):
        if not config.START_HIDDEN:
            notify_running(key, WM_ROBIN_SHOW)
        logger.info("O Robin já está aberto para %s; esta execução termina.", config.DATA_DIR)
        return
    logger.warning("Já existe um Robin aberto (%s, pid %s); esta execução (%s) termina. Use --replace para trocar.",
                   running["label"], running.get("pid"), mine["label"])
    if not config.START_HIDDEN:
        tray_notice(f"Já existe um Robin aberto ({running['label']}). Use --replace para trocar.", config.ICON_FILE)


def _replace_running(key: str, instance: SingleInstance) -> bool:
    """--replace: pede para a execução aberta sair e fica com a instância única quando ela sair."""
    running = running_build(config.DATA_DIR, running_pid(key, timeout=2.0))
    label = running["label"] if running else "?"
    logger.info("--replace: fechando o Robin aberto (%s)", label)
    notify_running(key, WM_ROBIN_QUIT, timeout=2.0)
    deadline = time.monotonic() + REPLACE_TIMEOUT
    while time.monotonic() < deadline:
        if instance.acquire():
            logger.info("--replace: o Robin aberto (%s) saiu; esta execução assume", label)
            return True
        time.sleep(0.25)
    logger.error("--replace: o Robin aberto (%s) não saiu em %.0fs", label, REPLACE_TIMEOUT)
    tray_notice(f"O Robin aberto ({label}) não fechou. Use Sair na bandeja dele e abra de novo.", config.ICON_FILE)
    return False


def main() -> None:
    _setup_logging()
    # Um Robin por pasta de dados: uma segunda execução pede para a aberta aparecer (ou sair, com
    # --quit; ou dar o lugar, com --replace) e termina. Aberta pelo "iniciar com o Windows"
    # (--hidden), só termina.
    key = instance_key(config.DATA_DIR)
    instance = SingleInstance(key)
    mine = current_build()
    if not instance.acquire():
        if config.QUIT_REQUESTED:
            notify_running(key, WM_ROBIN_QUIT)
            logger.info("--quit: pedido de saída enviado ao Robin aberto para %s.", config.DATA_DIR)
            return
        if not config.REPLACE_REQUESTED:
            _second_run(key, mine)
            return
        if not _replace_running(key, instance):
            return
    if config.QUIT_REQUESTED:
        return  # nada aberto para encerrar
    write_instance_file(config.DATA_DIR, mine)
    logger.info("Robin %s aberto (%s), pid %d", config.APP_VERSION, mine["label"], os.getpid())
    datadir.prepare()
    if config.MOCK:
        print(f"[Robin] MODO SIMULAÇÃO ativo (ROBIN_MOCK=1): respostas locais, sem API Key. Dados em {config.DATA_DIR}")

    set_app_user_model_id(config.APP_USER_MODEL_ID)
    api = RobinAPI()
    # Fundo e moldura já no tema e na paleta da interface: a janela não pisca com outras cores.
    theme = config.resolve_theme(config.theme_preference())
    palette = config.palette_preference()
    window = webview.create_window(
        config.APP_NAME,
        url=str(config.UI_DIR / "index.html"),
        js_api=api,
        width=1200,
        height=800,
        min_size=(900, 600),
        background_color=config.WINDOW_COLORS[palette][theme]["bg"],
        text_select=True,
        easy_drag=False,
        hidden=config.START_HIDDEN,  # escondida, mas com o WebView já carregado para o atalho
    )

    def before_show(window):
        # Roda na thread da interface, antes do primeiro Show(): a barra nativa nunca aparece.
        frame = WindowFrame(window, config.FRAME_MODE, theme, palette)
        frame.install()
        desktop = Desktop(window, frame, api._emit, key, on_summon=api._on_summon)
        desktop.start()
        api._attach(window, frame, desktop)

    window.events.before_show += before_show
    window.events.maximized += lambda: api._emit("onWindowState", {"maximized": True})
    window.events.restored += lambda: api._emit("onWindowState", {"maximized": False})

    _serve_ui_without_stale_cache()
    webview.start(
        gui="edgechromium",
        http_server=True,
        http_port=_http_port(),
        private_mode=False,
        storage_path=str(config.WEBVIEW_DATA_DIR),
        debug=config.DEBUG,
        icon=str(config.ICON_FILE),  # Form.Icon: barra de tarefas e Alt+Tab, no exe e no código-fonte
    )


if __name__ == "__main__":
    main()
