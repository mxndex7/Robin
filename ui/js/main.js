// Ponto de entrada da interface: liga os módulos e os atalhos globais.

import { call, on, ready } from "./bridge.js";
import { createChat } from "./chat.js";
import { cloudNotice } from "./cloud.js";
import { createComposer } from "./composer.js";
import { createConversations } from "./conversations.js";
import { playIntro } from "./intro.js";
import { createMemoryList } from "./memory-list.js";
import { createSettings } from "./modal.js";
import { createModelPicker } from "./models.js";
import { hasOpenLayer } from "./overlay.js";
import { createPalette } from "./palette.js";
import { measureSmoothness } from "./perf.js";
import { createSidebar } from "./sidebar.js";
import { PALETTES, initTheme, onThemeChange, setPalette, toggleTheme } from "./theme.js";
import { initTitlebar, setStatus } from "./titlebar.js";
import { toast } from "./toast.js";

const $ = (selector) => document.querySelector(selector);

function isTyping(target) {
  return target.closest("textarea, input, [contenteditable='true']");
}

// Soltar um arquivo na janela faria o WebView abri-lo fora do app. Só arrastes com arquivo são
// barrados: arrastar texto para o composer continua funcionando. (A outra camada é o
// AllowExternalDrop = False em window.py.)
function blockFileDrop() {
  const hasFiles = (event) => event.dataTransfer?.types.includes("Files");
  window.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "none";
  });
  window.addEventListener("drop", (event) => {
    if (hasFiles(event)) event.preventDefault();
  });
}

// Aba Conversas: a lista vem do Python (list_conversations) e é redesenhada a cada mudança
// (onConversationsChanged) ou busca. Só a resposta da chamada mais recente vale (uma busca digitada
// depressa não deixa a lista voltar para um resultado velho).
function wireConversations(chat, composer) {
  let current = null;
  let sequence = 0;

  async function refresh() {
    const ticket = ++sequence;
    const result = await call("list_conversations", conversations.query).catch(() => null);
    if (!result?.ok || ticket !== sequence) return;
    current = result.current;
    conversations.render(result.items, { current, query: conversations.query });
  }

  const conversations = createConversations({
    onSelect: async (id) => {
      if (id === current) {
        composer.focus();
        return;
      }
      if (await chat.openConversation(id)) composer.focus();
    },
    onDelete: async (id) => {
      const result = await call("delete_conversation", id).catch((error) => ({ ok: false, error: String(error?.message || error) }));
      if (!result?.ok) {
        toast(result?.error || "Não foi possível apagar a conversa.", { tone: "error" });
        return;
      }
      if (result.was_current) await chat.newConversation();
      toast("Conversa apagada", { iconName: "trash" });
    },
    onSearch: refresh,
  });
  on("onConversationsChanged", refresh);
  // A conversa aberta foi apagada em outro PC (pull da nuvem): começa outra, como o Python já fez.
  on("onConversationRemoved", async ({ message }) => {
    await chat.newConversation();
    toast(message, { iconName: "alert" });
  });
  refresh();

  /** Nova conversa (Ctrl+L, cabeçalho, lateral): a atual continua guardada, sem confirmação. */
  return async function startNew() {
    if (chat.hasMessages || current || composer.hasFolder) await chat.newConversation();
    composer.focus();
  };
}

async function init() {
  await ready();
  const [state, settingsState] = await Promise.all([call("get_state"), call("get_settings")]);

  initTitlebar(state);
  $("#model-label").textContent = state.model;
  $("#key-notice").hidden = state.has_key;
  $("#mock-badge").hidden = !state.mock;

  const memoryList = createMemoryList();
  const sidebar = createSidebar({ onPanelChange: (name) => memoryList.setVisible(name === "memory") });
  sidebar.setSyncEnabled(Boolean(settingsState.git_sync && settingsState.git_available));
  sidebar.setCloud(state.cloud);

  const composer = createComposer({
    onSubmit: (text, files, context, folderId) => chat.send(text, files, context, folderId),
    onStop: () => chat.stop(),
    limits: state.attachments,
    changeFolder: async (context, removeId, isCurrent) => {
      const result = removeId ? await call("remove_chat_folder", removeId) : await call("pick_chat_folder", context);
      if (result?.ok && isCurrent()) applyProvider(result.state);
      return result;
    },
  });
  const modelPicker = createModelPicker();
  let chat;
  function applyProvider(next) {
    if (!next) return;
    if (next.model) modelPicker.setProvider(next.provider, next.model);
    composer.setAttachmentContext(next.attachments?.context);
    composer.setFolder(next.project_folder, next.folder_picker);
    if (next.can_regenerate !== undefined) chat?.setCanRegenerate(next.can_regenerate);
    $("#key-notice").hidden = next.has_key;
  }
  applyProvider(state);
  chat = createChat({ composer, onModelChanged: (model) => modelPicker.setCurrent(model), onProviderChanged: applyProvider });
  const settings = createSettings({
    onProviderChanged: applyProvider,
    onSyncChanged: (enabled) => sidebar.setSyncEnabled(enabled),
    onCloudChanged: (status) => applyCloud(status),
  });

  // Memória na nuvem: estado na lateral e nas Configurações, toast só quando o estado muda.
  let cloudState = state.cloud?.state ?? "off";
  function applyCloud(status) {
    sidebar.setCloud(status);
    settings.setCloudStatus(status);
    const notice = cloudNotice(cloudState, status);
    cloudState = status?.state ?? "off";
    if (notice) toast(notice.message, notice.tone ? { tone: notice.tone } : { iconName: "network" });
  }
  on("onCloudStatus", applyCloud);
  on("onCloudNotice", ({ message, tone }) => toast(message, tone === "error" ? { tone: "error" } : { iconName: "alert" }));

  const startNewConversation = wireConversations(chat, composer);
  $("#new-chat").addEventListener("click", startNewConversation);
  $("#header-new-chat").addEventListener("click", startNewConversation);

  // Tema e paleta: o botão do cabeçalho troca o tema; as escolhas do .env valem na abertura.
  const themeButton = $("#theme-toggle");
  onThemeChange(({ theme }) => themeButton.setAttribute("aria-label", theme === "dark" ? "Usar o tema claro" : "Usar o tema escuro"));
  themeButton.addEventListener("click", toggleTheme);
  initTheme({ theme: settingsState.theme, palette: settingsState.palette });

  const palette = createPalette([
    { label: "Nova conversa", iconName: "square-pen", shortcut: "Ctrl L", keywords: "limpar resetar começar", run: () => startNewConversation() },
    { label: "Trocar modelo", iconName: "cpu", keywords: "gemini modelo", run: () => modelPicker.open() },
    { label: "Alternar painel lateral", iconName: "panel-left", shortcut: "Ctrl B", keywords: "sidebar painel memória conversas", run: () => sidebar.toggle() },
    { label: "Ver conversas", iconName: "message", keywords: "lateral histórico antigas", run: () => sidebar.showPanel("conversations") },
    { label: "Ver memória", iconName: "save", keywords: "lateral guardado anotado lembrar tarefas", run: () => sidebar.showPanel("memory") },
    { label: "Editar núcleo", iconName: "pencil", keywords: "lateral memory.md núcleo editar", run: () => sidebar.showPanel("core") },
    // Só depois de uma carga bem-sucedida: salvar um editor vazio apagaria o memory.md.
    { label: "Salvar memória", iconName: "save", shortcut: "Ctrl S", keywords: "memory.md", when: () => sidebar.canSave, run: () => sidebar.save() },
    { label: "Configurações", iconName: "settings", keywords: "api key chave git sync tema", run: () => settings.open() },
    { label: "Alternar tema", iconName: "sun", keywords: "claro escuro aparência cores", run: () => toggleTheme() },
    ...PALETTES.map(({ id, name }) => ({
      label: `Paleta ${name}`,
      iconName: "palette",
      keywords: "cores aparência tema",
      run: () => setPalette(id),
    })),
    { label: "Focar no composer", iconName: "message", shortcut: "/", keywords: "mensagem escrever", run: () => composer.focus() },
    { label: "Parar geração", iconName: "stop", shortcut: "Esc", when: () => chat.generating, run: () => chat.stop() },
    { label: "Regenerar última resposta", iconName: "refresh", when: () => chat.canRegenerate, run: () => chat.regenerate() },
    {
      label: "Medir fluidez (debug)",
      iconName: "cpu",
      keywords: "fps desempenho performance",
      when: () => state.debug,
      run: () => measureSmoothness({ sidebar, palette, models: modelPicker, chat, mock: state.mock }),
    },
  ]);

  for (const suggestion of document.querySelectorAll(".suggestion")) {
    suggestion.addEventListener("click", () => composer.setText(suggestion.dataset.prompt));
  }
  $("#key-notice-button").addEventListener("click", () => settings.open());
  $("#open-settings").addEventListener("click", () => settings.open());
  $("#palette-hint").addEventListener("click", () => palette.open());
  window.addEventListener("robin:configure", () => settings.open());
  // Atalho global, bandeja ou segunda execução trouxeram a janela: pronto para digitar.
  on("onSummon", () => {
    if (!hasOpenLayer()) composer.focus();
  });

  // Atalhos. O Esc de camadas abertas (popover, modal, palette, confirmação) é tratado antes,
  // em overlay.js; aqui ele só para a geração.
  window.addEventListener("keydown", (event) => {
    const ctrl = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();

    if (ctrl && key === "k") {
      event.preventDefault();
      if (!hasOpenLayer() || palette.isOpen) palette.toggle();
      return;
    }
    if (hasOpenLayer()) return;

    if (ctrl && key === "b") {
      event.preventDefault();
      sidebar.toggle();
    } else if (ctrl && key === "s") {
      event.preventDefault();
      sidebar.save();
    } else if (ctrl && key === "l") {
      event.preventDefault();
      startNewConversation();
    } else if (event.key === "Escape" && chat.generating) {
      event.preventDefault();
      chat.stop();
    } else if (event.key === "/" && !ctrl && !isTyping(event.target)) {
      event.preventDefault();
      composer.focus();
    }
  });

  await sidebar.load();
  playIntro();
  setStatus("ready", "Pronto");
  composer.focus();
}

blockFileDrop();
init();
