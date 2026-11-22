// Barra de título: botões da janela, estado maximizado, status e fallback de arraste.

import { call, on } from "./bridge.js";

const DRAG_THRESHOLD_PX = 4;

/** Atualiza o estado no R da marca (barra de título e cabeçalho no modo nativo) e o texto para leitores de tela. */
export function setStatus(state, label) {
  for (const mark of document.querySelectorAll(".brand__mark")) mark.dataset.state = state;
  document.getElementById("status-text").textContent = label;
}

function setMaximized(maximized) {
  document.body.classList.toggle("is-maximized", maximized);
  document
    .querySelector('.window-button[data-action="maximize"]')
    .setAttribute("aria-label", maximized ? "Restaurar" : "Maximizar");
}

function wireWindowButtons() {
  const actions = {
    minimize: () => call("window_minimize"),
    maximize: () => call("window_maximize"),
    close: () => call("window_close"),
  };
  for (const button of document.querySelectorAll(".window-button")) {
    button.addEventListener("click", () => actions[button.dataset.action]());
  }
}

// O WebView2 trata o arraste de app-region: drag nativamente. Este fallback só entra se a página
// receber o mousedown: o arraste começa depois de o mouse andar alguns pixels com o botão
// pressionado (um clique simples não abre o laço de arraste do Windows).
function wireFallbackDrag() {
  const titlebar = document.getElementById("titlebar");
  const isControl = (event) => event.target.closest(".window-controls");

  titlebar.addEventListener("mousedown", (event) => {
    if (event.button !== 0 || isControl(event)) return;
    const startX = event.screenX;
    const startY = event.screenY;
    const stop = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", stop);
    };
    const onMove = (move) => {
      if (Math.hypot(move.screenX - startX, move.screenY - startY) < DRAG_THRESHOLD_PX) return;
      stop();
      call("window_start_drag");
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", stop);
  });

  titlebar.addEventListener("dblclick", (event) => {
    if (!isControl(event)) call("window_maximize");
  });

  for (const edge of document.querySelectorAll(".resize-edge")) {
    edge.addEventListener("mousedown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      call("window_start_resize", edge.dataset.edge);
    });
  }
}

export function initTitlebar(state) {
  document.body.classList.toggle("frame-native", state.frame === "native");
  setMaximized(state.maximized);
  on("onWindowState", ({ maximized }) => setMaximized(maximized));
  wireWindowButtons();
  wireFallbackDrag();
}
