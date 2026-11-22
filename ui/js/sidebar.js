// Painel lateral: abas Conversas | Memória | Núcleo (setas, Home e End trocam; a última aba fica
// lembrada), largura arrastável e recolher (Ctrl+B). A aba Núcleo tem o editor com numeração de
// linhas, a aba Visualizar, salvar (Ctrl+S) com estados e recarregar (confirma se houver alterações).
// As listas são do conversations.js (Conversas) e do memory-list.js (Memória: o que o Robin guardou).
//
// Proteção contra apagar o memory.md: salvar só existe depois de uma carga bem-sucedida (se a carga
// falhar, o editor fica vazio e somente leitura) e salvar vazio pede confirmação inline.
//
// Com o Supabase, o editor mostra e salva o núcleo (item fixo na nuvem). O núcleo mudado em outra
// máquina (onMemoryChanged) recarrega sozinho se o editor não tiver alterações; com alterações, avisa.

import { call, on } from "./bridge.js";
import { describeCloud } from "./cloud.js";
import { renderMarkdown } from "./markdown.js";
import { motion, prefersReducedMotion, settled, wait } from "./overlay.js";
import { toast } from "./toast.js";

// O que se move na área do chat quando a sidebar abre ou fecha.
const flipTargets = () => [
  ...document.querySelectorAll(".chat-header > *"),
  document.querySelector(".conversation__inner"),
  document.getElementById("composer"),
  document.querySelector(".composer-caption"),
  document.getElementById("scroll-bottom"),
  document.getElementById("toasts"),
];

const COLLAPSED_KEY = "robin.sidebar.collapsed";
const WIDTH_KEY = "robin.sidebar.width";
const PANEL_KEY = "robin.sidebar.panel";
const PANELS = ["conversations", "memory", "core"];
const DEFAULT_WIDTH = 340;
const MIN_WIDTH = 280;
const MAX_WIDTH = 480;
const KEYBOARD_STEP = 16;
const MIN_SAVING_MS = 400; // o spinner fica visível por um instante, mesmo com disco rápido
const SAVED_MS = 1400;
const SYNC_RESULT_MS = 1800;
const CONFIRM_MS = 3000;
const LOAD_RETRY_MS = 1000; // nova tentativa automática quando a carga inicial falha

function readPref(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writePref(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Armazenamento indisponível: a preferência só não persiste.
  }
}

const clampWidth = (width) => Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, width)));

// Largura arrastável pela borda direita (280–480px), lembrada entre execuções.
function wireResizer(app, resizer) {
  const setWidth = (width) => {
    const value = clampWidth(width);
    app.style.setProperty("--sidebar-w", `${value}px`);
    resizer.setAttribute("aria-valuenow", String(value));
    return value;
  };
  const currentWidth = () => document.getElementById("sidebar").getBoundingClientRect().width;

  resizer.setAttribute("aria-valuemin", String(MIN_WIDTH));
  resizer.setAttribute("aria-valuemax", String(MAX_WIDTH));
  setWidth(Number(readPref(WIDTH_KEY)) || DEFAULT_WIDTH);

  resizer.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    resizer.setPointerCapture(event.pointerId);
    document.body.classList.add("is-resizing-sidebar");
    const startX = event.clientX;
    const startWidth = currentWidth();
    let pending = 0;
    let latestX = startX;

    const onMove = (move) => {
      latestX = move.clientX;
      pending ||= requestAnimationFrame(() => {
        pending = 0;
        setWidth(startWidth + latestX - startX);
      });
    };
    const onUp = () => {
      resizer.removeEventListener("pointermove", onMove);
      resizer.removeEventListener("pointerup", onUp);
      resizer.removeEventListener("pointercancel", onUp);
      cancelAnimationFrame(pending);
      document.body.classList.remove("is-resizing-sidebar");
      writePref(WIDTH_KEY, String(setWidth(startWidth + latestX - startX)));
    };
    resizer.addEventListener("pointermove", onMove);
    resizer.addEventListener("pointerup", onUp);
    resizer.addEventListener("pointercancel", onUp);
  });

  resizer.addEventListener("dblclick", () => writePref(WIDTH_KEY, String(setWidth(DEFAULT_WIDTH))));

  resizer.addEventListener("keydown", (event) => {
    const delta = { ArrowLeft: -KEYBOARD_STEP, ArrowRight: KEYBOARD_STEP }[event.key];
    if (!delta) return;
    event.preventDefault();
    writePref(WIDTH_KEY, String(setWidth(currentWidth() + delta)));
  });
}

/** onPanelChange(nome): a aba visível mudou (a lista da Memória recarrega quando aparece). */
export function createSidebar({ onPanelChange } = {}) {
  const app = document.getElementById("app");
  const toggleButton = document.getElementById("sidebar-toggle");
  const input = document.getElementById("memory-input");
  const gutter = document.getElementById("editor-gutter");
  const mirror = document.getElementById("editor-mirror");
  const scroller = document.getElementById("editor-scroll");
  const preview = document.getElementById("memory-preview");
  const tabs = [...document.querySelectorAll("#panel-core .segmented__item")];
  const panelTabs = [...document.querySelectorAll(".sidebar__tab")];
  const dirtyDot = document.getElementById("dirty-dot");
  const meta = document.getElementById("memory-meta");
  const syncLabel = document.getElementById("memory-sync");
  const sourceLabel = document.getElementById("memory-source");
  const saveButton = document.getElementById("memory-save");
  const reloadButton = document.getElementById("memory-reload");
  const reloadLabel = reloadButton.querySelector(".reload-btn__label");
  const loadError = document.getElementById("memory-load-error");
  const retryButton = document.getElementById("memory-retry");

  let loaded = false;
  let savedContent = "";
  let frame = 0;
  let lastLayoutKey = "";
  let viewing = false;
  let saving = false;
  let resetTimer = 0;
  let confirmTimer = 0;
  let cloudOn = false;
  let gitOn = false;

  // ---------- editor ----------

  // Numeração alinhada com a quebra visual: cada linha lógica é medida num espelho invisível
  // com a mesma largura útil do textarea; o número ocupa a altura que a linha ocupa.
  function layout() {
    frame = 0;
    const styles = getComputedStyle(input);
    const contentWidth = input.clientWidth - parseFloat(styles.paddingLeft) - parseFloat(styles.paddingRight);
    const lines = input.value.split("\n");

    meta.textContent = `${lines.length} ${lines.length === 1 ? "linha" : "linhas"}`;
    if (viewing || contentWidth <= 0) return;
    // Mesma largura e mesmo texto (ex.: a sidebar só reabriu): nada a remedir. Evita um quadro
    // pesado no meio da animação de abertura.
    const layoutKey = `${contentWidth}|${input.value}`;
    if (layoutKey === lastLayoutKey) return;
    lastLayoutKey = layoutKey;

    mirror.style.width = `${contentWidth}px`;
    mirror.replaceChildren(
      ...lines.map((line) => {
        const row = document.createElement("div");
        row.textContent = line || "​";
        return row;
      }),
    );

    const numbers = document.createDocumentFragment();
    lines.forEach((_, index) => {
      const number = document.createElement("span");
      number.textContent = String(index + 1);
      number.style.height = `${mirror.children[index].offsetHeight}px`;
      numbers.append(number);
    });
    gutter.replaceChildren(numbers);

    // O textarea cresce com o conteúdo; quem rola é o contêiner (numeração e texto juntos).
    const scrollTop = scroller.scrollTop;
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
    scroller.scrollTop = scrollTop;
  }

  function scheduleLayout() {
    if (!frame) frame = requestAnimationFrame(layout);
  }

  function updateDirty() {
    dirtyDot.classList.toggle("is-visible", input.value !== savedContent);
  }

  // ---------- abas Editar / Visualizar ----------

  function setTab(tab) {
    viewing = tab === "view";
    for (const item of tabs) {
      const active = item.dataset.tab === tab;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-selected", String(active));
    }
    if (viewing) preview.replaceChildren(renderMarkdown(input.value));
    scroller.hidden = viewing;
    preview.hidden = !viewing;
    const { base, easeOut } = motion();
    (viewing ? preview : scroller).animate([{ opacity: 0 }, { opacity: 1 }], { duration: base, easing: easeOut });
    if (!viewing) scheduleLayout();
  }

  // ---------- abas do painel: Conversas | Memória | Núcleo ----------

  function setPanel(name, { focus = false } = {}) {
    for (const tab of panelTabs) {
      const active = tab.dataset.panel === name;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !active;
      if (active && focus) tab.focus();
    }
    writePref(PANEL_KEY, name);
    if (name === "core") scheduleLayout(); // escondido, o editor não tinha largura para medir
    onPanelChange?.(name);
  }

  // ---------- salvar ----------

  function setSaveState(state) {
    saveButton.dataset.state = state;
  }

  function resetSaveLater(ms) {
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => setSaveState("idle"), ms);
  }

  // Salvar vazio por cima de uma memória com conteúdo: o botão vira "Salvar vazio?" e só o clique
  // nele (ou Enter com ele focado) confirma. Editar, sair do botão ou esperar cancela.
  function askConfirmEmpty() {
    setSaveState("confirm");
    saveButton.focus();
    resetSaveLater(CONFIRM_MS);
  }

  function cancelConfirmEmpty() {
    if (saveButton.dataset.state !== "confirm") return;
    clearTimeout(resetTimer);
    setSaveState("idle");
  }

  async function save({ confirmEmpty = false } = {}) {
    if (saving) return;
    if (!loaded) {
      toast("A memória ainda não foi carregada.", { iconName: "alert" });
      return;
    }
    const content = input.value;
    if (!confirmEmpty && !content.trim() && savedContent.trim()) {
      askConfirmEmpty();
      return;
    }
    saving = true;
    clearTimeout(resetTimer);
    setSaveState("saving");
    const started = performance.now();

    let result;
    try {
      result = await call("save_memory", content, confirmEmpty);
    } catch (error) {
      result = { ok: false, error: String(error?.message || error) };
    }
    await wait(MIN_SAVING_MS - (performance.now() - started));

    if (!result?.ok) {
      saving = false;
      if (result?.code === "confirm_empty") {
        askConfirmEmpty(); // o arquivo ganhou conteúdo por fora desde a carga
        return;
      }
      setSaveState("idle");
      toast(result?.error || "Não foi possível salvar a memória.", { tone: "error" });
      return;
    }

    savedContent = content;
    updateDirty();
    setSaveState(result.sync === "pending" ? "warn" : "saved");
    if (result.sync === "git") return; // o resultado do Git chega por onMemoryStatus (um único toast)
    saving = false;
    if (result.sync === "pending") {
      toast("Núcleo salvo só neste PC (sem conexão): vai para a nuvem sozinho", { iconName: "alert" });
    } else {
      toast(result.sync === "cloud" ? "Núcleo salvo na nuvem" : "Memória salva");
    }
    resetSaveLater(result.sync === "pending" ? SYNC_RESULT_MS : SAVED_MS);
  }

  on("onMemoryStatus", ({ status }) => {
    if (status === "git_ok") {
      setSaveState("git");
      toast("Git sincronizado", { iconName: "git-branch" });
    } else if (status === "git_warn") {
      setSaveState("warn");
      toast("Salvo local (aviso Git)", { iconName: "alert" });
    } else {
      return; // git_syncing: o botão continua em "Salvo"
    }
    saving = false;
    resetSaveLater(SYNC_RESULT_MS);
  });

  // ---------- recarregar ----------

  function resetReload() {
    clearTimeout(confirmTimer);
    reloadButton.dataset.confirm = "false";
    reloadLabel.textContent = "Recarregar";
  }

  async function reload() {
    if (input.value !== savedContent && reloadButton.dataset.confirm !== "true") {
      reloadButton.dataset.confirm = "true";
      reloadLabel.textContent = "Descartar alterações?";
      clearTimeout(confirmTimer);
      confirmTimer = setTimeout(resetReload, CONFIRM_MS);
      return;
    }
    resetReload();
    if (await load()) toast("Memória recarregada", { iconName: "rotate-ccw" });
  }

  // ---------- carregar ----------

  // Sem uma carga bem-sucedida, o editor fica vazio e somente leitura, e salvar não existe.
  function setLoaded() {
    loaded = true;
    input.readOnly = false;
    saveButton.disabled = false;
    loadError.hidden = true;
  }

  async function load({ notify = true } = {}) {
    let result;
    try {
      result = await call("get_memory");
    } catch (error) {
      result = { ok: false, error: String(error?.message || error) };
    }
    if (!result?.ok) {
      if (!loaded) loadError.hidden = false; // já carregada: o conteúdo atual continua no editor
      if (notify) toast(result?.error || "Não foi possível ler o memory.md.", { tone: "error" });
      return false;
    }
    savedContent = result.content;
    input.value = result.content;
    setLoaded();
    cancelConfirmEmpty();
    updateDirty();
    if (viewing) preview.replaceChildren(renderMarkdown(input.value));
    scheduleLayout();
    return true;
  }

  async function retryLoad() {
    retryButton.disabled = true;
    await load();
    retryButton.disabled = false;
  }

  // ---------- recolher ----------

  function applyCollapsed(collapsed) {
    app.classList.toggle("is-sidebar-collapsed", collapsed);
    toggleButton.setAttribute("aria-expanded", String(!collapsed));
    writePref(COLLAPSED_KEY, collapsed ? "1" : "0");
    if (!collapsed) scheduleLayout();
  }

  // FLIP: aplica o layout final de uma vez, inverte cada elemento da área do chat com translateX
  // até a posição antiga e anima até zero (só transform). A sidebar entra/sai com translateX + fade
  // do conteúdo; ao fechar, ela sai por cima (posição absoluta) e só então some do layout.
  function setCollapsed(collapsed, { animate = true } = {}) {
    if (app.classList.contains("is-sidebar-collapsed") === collapsed) return;
    const sidebar = document.getElementById("sidebar");
    const inner = sidebar.querySelector(".sidebar__inner");
    for (const animation of [...sidebar.getAnimations(), ...inner.getAnimations()]) animation.cancel();
    sidebar.classList.remove("is-leaving");

    if (!animate) {
      applyCollapsed(collapsed);
      return;
    }
    const { base, panel, easeOut, easeInOut } = motion();
    if (prefersReducedMotion()) {
      applyCollapsed(collapsed);
      if (!collapsed) inner.animate([{ opacity: 0 }, { opacity: 1 }], { duration: base, easing: easeOut });
      return;
    }

    const targets = flipTargets().filter(Boolean);
    const before = new Map(targets.map((node) => [node, node.getBoundingClientRect().left]));
    applyCollapsed(collapsed);
    if (collapsed) sidebar.classList.add("is-leaving");

    for (const [node, left] of before) {
      const dx = left - node.getBoundingClientRect().left;
      if (Math.abs(dx) < 0.5) continue;
      node.animate([{ transform: `translateX(${dx}px)` }, { transform: "none" }], { duration: panel, easing: easeInOut });
    }

    const slide = collapsed
      ? [{ transform: "none" }, { transform: "translateX(-100%)" }]
      : [{ transform: "translateX(-100%)" }, { transform: "none" }];
    const sliding = sidebar.animate(slide, { duration: panel, easing: easeInOut });
    inner.animate(collapsed ? [{ opacity: 1 }, { opacity: 0 }] : [{ opacity: 0 }, { opacity: 1 }], {
      duration: collapsed ? panel * 0.6 : panel,
      delay: collapsed ? 0 : panel * 0.25,
      easing: easeOut,
      fill: "backwards",
    });
    if (collapsed) {
      settled(sliding).then(() => {
        if (app.classList.contains("is-sidebar-collapsed")) sidebar.classList.remove("is-leaving");
      });
    }
  }

  input.addEventListener("input", () => {
    scheduleLayout();
    updateDirty();
    cancelConfirmEmpty();
  });
  toggleButton.addEventListener("click", () => setCollapsed(!app.classList.contains("is-sidebar-collapsed")));
  for (const tab of tabs) tab.addEventListener("click", () => setTab(tab.dataset.tab));
  for (const tab of panelTabs) {
    tab.addEventListener("click", () => setPanel(tab.dataset.panel));
    tab.addEventListener("keydown", (event) => {
      const index = panelTabs.indexOf(tab);
      const next = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: panelTabs.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      setPanel(panelTabs[(next + panelTabs.length) % panelTabs.length].dataset.panel, { focus: true });
    });
  }
  saveButton.addEventListener("click", () => save({ confirmEmpty: saveButton.dataset.state === "confirm" }));
  saveButton.addEventListener("blur", cancelConfirmEmpty);
  reloadButton.addEventListener("click", reload);
  reloadButton.addEventListener("blur", resetReload);
  retryButton.addEventListener("click", retryLoad);
  preview.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link) return;
    event.preventDefault();
    call("open_external", link.getAttribute("href"));
  });
  new ResizeObserver(scheduleLayout).observe(scroller);

  // O núcleo mudou (pull da nuvem, outra máquina, conectar ou desconectar o Supabase).
  on("onMemoryChanged", ({ keep_editor: keepEditor } = {}) => {
    if (saving) return;
    if (input.value === savedContent) {
      load({ notify: false });
    } else if (!keepEditor) {
      toast("O núcleo mudou em outra máquina: recarregue para ver (suas alterações continuam no editor).", { iconName: "alert" });
    }
  });

  function renderSyncLabel() {
    if (cloudOn) return;
    syncLabel.dataset.state = "local";
    syncLabel.textContent = gitOn ? "sync Git" : "local";
  }

  wireResizer(app, document.getElementById("sidebar-resizer"));
  applyCollapsed(readPref(COLLAPSED_KEY) === "1");
  setPanel(PANELS.includes(readPref(PANEL_KEY)) ? readPref(PANEL_KEY) : PANELS[0]);

  return {
    /** Carga inicial: se falhar, mostra o erro e tenta de novo sozinha uma vez, sem toast repetido. */
    async load() {
      if (await load()) return;
      setTimeout(() => !loaded && load({ notify: false }), LOAD_RETRY_MS);
    },
    save: () => save(),
    get canSave() {
      return loaded;
    },
    toggle() {
      setCollapsed(!app.classList.contains("is-sidebar-collapsed"));
    },
    /** Abre o painel (se estiver recolhido) na aba pedida: "conversations", "memory" ou "core". */
    showPanel(name) {
      setCollapsed(false);
      setPanel(name, { focus: true });
    },
    setSyncEnabled(enabled) {
      gitOn = enabled;
      renderSyncLabel();
    },
    /** Estado do Supabase (null: desligado). Com a nuvem, o editor é o núcleo. */
    setCloud(status) {
      cloudOn = Boolean(status);
      sourceLabel.textContent = cloudOn ? "núcleo" : "memory.md";
      if (!cloudOn) {
        renderSyncLabel();
        return;
      }
      const described = describeCloud(status);
      syncLabel.dataset.state = described.state;
      syncLabel.textContent = described.short;
    },
  };
}
