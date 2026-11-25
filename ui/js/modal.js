// Modal de configurações: API Key (campo senha com mostrar/ocultar; da chave salva, só os 4 últimos
// caracteres), memória na nuvem (Supabase: URL e chave secreta testadas antes de salvar, estado,
// sincronizar e desconectar), atalho global (campo de captura), iniciar com o Windows, tema (sistema,
// claro ou escuro), a pasta de dados (caminho e botão para abrir) e o sync Git do memory.md
// (desligado por padrão; só disponível quando a pasta de dados é um repositório e sem o Supabase).

import { call } from "./bridge.js";
import { describeCloud } from "./cloud.js";
import { animateIn, animateOut, fadeBackdrop, openLayer, trapFocus } from "./overlay.js";
import { PALETTES, onThemeChange, setPalette, setThemeChoice } from "./theme.js";
import { toast } from "./toast.js";

const DISCONNECT_CONFIRM_MS = 3000;

export function createSettings({ onProviderChanged, onSyncChanged, onCloudChanged }) {
  const overlay = document.getElementById("settings-overlay");
  const modal = document.getElementById("settings-modal");
  const backdrop = overlay.querySelector(".overlay__backdrop");
  const chatgptSection = document.getElementById("chatgpt-settings");
  const chatgptHint = document.getElementById("chatgpt-status");
  const chatgptConnect = document.getElementById("chatgpt-connect");
  const chatgptNewAccount = document.getElementById("chatgpt-new-account");
  const chatgptDisconnect = document.getElementById("chatgpt-disconnect");
  const chatgptProfile = document.getElementById("chatgpt-profile");
  const chatgptProfileLabel = document.getElementById("chatgpt-profile-label");
  const providerSelect = document.getElementById("chat-provider");
  const form = document.getElementById("key-form");
  const keyInput = document.getElementById("key-input");
  const keySave = document.getElementById("key-save");
  const keyHint = document.getElementById("key-hint");
  const keyError = document.getElementById("key-error");
  const visibility = document.getElementById("key-visibility");
  const gitSwitch = document.getElementById("git-switch");
  const gitHint = document.getElementById("git-hint");
  const gitHelp = document.getElementById("git-help");
  const dataDir = document.getElementById("data-dir");
  const openDataDir = document.getElementById("open-data-dir");
  const footer = document.getElementById("settings-footer");
  const hotkeyInput = document.getElementById("hotkey-input");
  const hotkeySave = document.getElementById("hotkey-save");
  const hotkeyError = document.getElementById("hotkey-error");
  const autostartSwitch = document.getElementById("autostart-switch");
  const autostartHint = document.getElementById("autostart-hint");
  const cloudForm = document.getElementById("cloud-form");
  const cloudUrl = document.getElementById("cloud-url");
  const cloudKey = document.getElementById("cloud-key");
  const cloudKeyVisibility = document.getElementById("cloud-key-visibility");
  const cloudKeyHint = document.getElementById("cloud-key-hint");
  const cloudStatus = document.getElementById("cloud-status");
  const cloudSave = document.getElementById("cloud-save");
  const cloudSync = document.getElementById("cloud-sync");
  const cloudDisconnect = document.getElementById("cloud-disconnect");
  const cloudError = document.getElementById("cloud-error");
  const gitSection = document.getElementById("git-section");

  let close = null;
  let busy = false;
  let savedHotkey = "";
  let capturedHotkey = "";
  let savedUrl = "";
  let hasCloudKey = false;
  let disconnectTimer = 0;
  let chatProviders = {};
  let chatgptTimer = 0;

  function renderProviders(settings, state) {
    chatProviders = settings;
    onProviderChanged?.(state);
    chatgptSection.hidden = !settings.chatgpt_enabled;
    if (!settings.chatgpt_enabled) return;
    const connection = settings.chatgpt || {};
    chatgptHint.textContent = connection.pending ? "Conclua a conexão no navegador."
      : connection.message || (connection.connected ? connection.account || "Conectado." : "Nenhuma conta conectada.");
    chatgptConnect.hidden = Boolean(connection.connected);
    chatgptConnect.disabled = Boolean(connection.pending);
    chatgptNewAccount.hidden = !connection.registered || connection.connected || connection.pending;
    chatgptDisconnect.hidden = !connection.connected;
    const profile = chatgptProfile.value;
    const saved = connection.profiles ?? [];
    chatgptProfile.replaceChildren(...saved.map(({ id, label }) => new Option(label, id)));
    if (saved.some((item) => item.id === profile)) chatgptProfile.value = profile;
    chatgptProfileLabel.hidden = chatgptProfile.hidden = saved.length < 2 || connection.connected || connection.pending;
    providerSelect.options[0].disabled = !settings.key_hint;
    providerSelect.options[1].disabled = !connection.connected;
    providerSelect.disabled = providerSelect.options[0].disabled && providerSelect.options[1].disabled;
    const preferred = settings.preferred_provider === "chatgpt" ? providerSelect.options[1] : providerSelect.options[0];
    providerSelect.value = providerSelect.disabled ? "" :
      (preferred.disabled ? (connection.connected ? "chatgpt" : "gemini") : preferred.value);
  }

  function acceptConnection(result) {
    renderProviders({ ...chatProviders, chatgpt: result }, result.state);
    clearTimeout(chatgptTimer);
    if (result.pending) chatgptTimer = setTimeout(pollChatGPT, 750);
  }

  async function pollChatGPT() {
    const result = await call("chatgpt_status").catch(() => null);
    if (result?.ok) acceptConnection(result);
    else chatgptHint.textContent = "Não foi possível consultar a conexão. Abra as Configurações novamente.";
  }

  async function connectChatGPT(newAccount) {
    chatgptConnect.disabled = true;
    chatgptNewAccount.disabled = true;
    const result = await call("begin_chatgpt", newAccount, newAccount ? "" : chatgptProfile.value).catch(() => null);
    chatgptNewAccount.disabled = false;
    if (!result?.ok) {
      chatgptConnect.disabled = false;
      chatgptHint.textContent = result?.error || "Não foi possível iniciar o login.";
      return;
    }
    acceptConnection(result);
  }

  chatgptConnect.addEventListener("click", () => connectChatGPT(false));
  chatgptNewAccount.addEventListener("click", () => connectChatGPT(true));
  chatgptDisconnect.addEventListener("click", async () => {
    chatgptDisconnect.disabled = true;
    const result = await call("disconnect_chatgpt").catch(() => null);
    chatgptDisconnect.disabled = false;
    if (result?.ok) acceptConnection(result);
    else chatgptHint.textContent = result?.error || "Não foi possível desconectar.";
  });

  providerSelect.addEventListener("change", async () => {
    const provider = providerSelect.value;
    providerSelect.disabled = true;
    const result = await call("set_chat_provider", provider).catch(() => null);
    if (result?.ok) {
      renderProviders({ ...chatProviders, preferred_provider: provider }, result.state);
      if (result.state.provider !== provider) toast("O provedor escolhido vale ao iniciar uma nova conversa.");
    } else {
      renderProviders(chatProviders);
      toast(result?.error || "Não foi possível trocar o provedor.", { tone: "error" });
    }
  });

  function setKeyHint(hint) {
    keyHint.textContent = hint ? `Chave atual: ${hint}` : "Nenhuma chave configurada.";
  }

  function setError(message) {
    keyError.hidden = !message;
    keyError.textContent = message || "";
  }

  function setVisible(visible) {
    keyInput.type = visible ? "text" : "password";
    visibility.setAttribute("aria-pressed", String(visible));
    visibility.setAttribute("aria-label", visible ? "Ocultar chave" : "Mostrar chave");
  }

  function setGit({ git_sync: enabled, git_available: available }) {
    gitSwitch.setAttribute("aria-checked", String(Boolean(enabled && available)));
    gitSwitch.disabled = !available;
    gitHint.textContent = available
      ? "Ao salvar, faz add, commit e push do memory.md."
      : "Indisponível: a pasta de dados não é um repositório Git.";
    gitHelp.hidden = available;
  }

  function setDataDir(path) {
    dataDir.textContent = path || "";
    dataDir.title = path || "";
  }

  function setHotkeyError(message) {
    hotkeyError.hidden = !message;
    hotkeyError.textContent = message || "";
  }

  function setHotkey({ hotkey, hotkey_error: error }) {
    savedHotkey = hotkey || "";
    capturedHotkey = "";
    hotkeyInput.value = savedHotkey;
    hotkeySave.disabled = true;
    setHotkeyError(error);
  }

  function setAutostart({ autostart, autostart_available: available }) {
    autostartSwitch.setAttribute("aria-checked", String(Boolean(autostart && available)));
    autostartSwitch.disabled = !available;
    autostartHint.textContent = available
      ? "Abre escondido ao entrar no Windows, pronto para o atalho."
      : "Só no Robin.exe: rodando pelo código-fonte, o Robin não se registra.";
  }

  // ---------- nuvem (Supabase) ----------

  function setCloudError(message) {
    cloudError.hidden = !message;
    cloudError.textContent = message || "";
  }

  function setCloudStatus(status) {
    const described = describeCloud(status);
    cloudStatus.dataset.state = described.state;
    cloudStatus.textContent = described.long;
  }

  function updateCloudSave() {
    const url = cloudUrl.value.trim();
    const key = cloudKey.value.trim();
    // Sem chave nova, só vale trocar a URL quando já existe uma chave salva.
    cloudSave.disabled = busy || !url || (!key && (!hasCloudKey || url === savedUrl));
  }

  function resetDisconnect() {
    clearTimeout(disconnectTimer);
    cloudDisconnect.dataset.confirm = "false";
    cloudDisconnect.textContent = "Desconectar";
  }

  function setCloud({ supabase_url: url, supabase_key_hint: hint, cloud }) {
    savedUrl = url || "";
    hasCloudKey = Boolean(hint);
    cloudUrl.value = savedUrl;
    cloudKey.value = "";
    cloudKeyHint.textContent = hint ? `Chave atual: ${hint}` : "Nenhuma chave configurada.";
    const connected = Boolean(cloud);
    cloudSave.textContent = connected ? "Salvar" : "Conectar";
    cloudSync.hidden = !connected;
    cloudDisconnect.hidden = !connected;
    gitSection.hidden = connected; // com a nuvem, o memory.md não sincroniza pelo Git
    resetDisconnect();
    setCloudStatus(cloud);
    updateCloudSave();
  }

  async function refresh() {
    const [settings, state] = await Promise.all([call("get_settings"), call("get_state")]);
    setKeyHint(settings.key_hint);
    renderProviders(settings, state);
    if (settings.chatgpt?.pending) {
      clearTimeout(chatgptTimer);
      chatgptTimer = setTimeout(pollChatGPT, 750);
    }
    setCloud(settings);
    setGit(settings);
    setDataDir(settings.data_dir);
    setHotkey(settings);
    setAutostart(settings);
    // Origem e build: deixa claro se esta janela é o exe (e de quando) ou o código-fonte (qual commit).
    const build = settings.build?.label ? ` • ${settings.build.label}` : "";
    footer.textContent = `${state.app} ${state.version}${build}${state.mock ? " • modo simulação (ROBIN_MOCK=1)" : ""}`;
  }

  async function open() {
    if (close) return;
    setError("");
    setCloudError("");
    setVisible(false);
    keyInput.value = "";
    keySave.disabled = true;
    overlay.hidden = false;
    fadeBackdrop(backdrop, true);
    animateIn(modal);
    close = openLayer({
      element: modal,
      onClose: () => {
        close = null;
        fadeBackdrop(backdrop, false);
        animateOut(modal).then(() => {
          if (!close) overlay.hidden = true;
        });
      },
    });
    keyInput.focus();
    await refresh();
    if (close && !chatProviders.key_hint && !chatgptSection.hidden && !chatgptConnect.hidden) chatgptConnect.focus();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const key = keyInput.value.trim();
    if (!key || busy) return;
    busy = true;
    keySave.disabled = true;
    keySave.textContent = "Validando…";
    setError("");
    const result = await call("set_api_key", key);
    busy = false;
    keySave.textContent = "Salvar chave";
    if (!result?.ok) {
      keySave.disabled = false;
      setError(result?.error || "Não foi possível salvar a chave.");
      keyInput.focus();
      return;
    }
    keyInput.value = "";
    setKeyHint(result.key_hint);
    renderProviders({ ...chatProviders, key_hint: result.key_hint }, result.state || { has_key: true });
    toast("API Key salva", { iconName: "key" });
  });

  keyInput.addEventListener("input", () => {
    keySave.disabled = busy || keyInput.value.trim() === "";
    if (!keyError.hidden) setError("");
  });

  visibility.addEventListener("click", () => setVisible(keyInput.type === "password"));

  cloudForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (cloudSave.disabled || busy) return;
    busy = true;
    cloudSave.disabled = true;
    const label = cloudSave.textContent;
    cloudSave.textContent = "Testando…";
    setCloudError("");
    const result = await call("set_supabase", cloudUrl.value.trim(), cloudKey.value.trim());
    busy = false;
    cloudSave.textContent = label;
    if (!result?.ok) {
      updateCloudSave();
      setCloudError(result?.error || "Não foi possível conectar ao Supabase.");
      return;
    }
    setCloud(result);
    onCloudChanged?.(result.cloud);
    toast("Supabase conectado: sincronizando a memória", { iconName: "network" });
  });

  for (const field of [cloudUrl, cloudKey]) {
    field.addEventListener("input", () => {
      updateCloudSave();
      if (!cloudError.hidden) setCloudError("");
    });
  }

  cloudKeyVisibility.addEventListener("click", () => {
    const visible = cloudKey.type === "password";
    cloudKey.type = visible ? "text" : "password";
    cloudKeyVisibility.setAttribute("aria-pressed", String(visible));
    cloudKeyVisibility.setAttribute("aria-label", visible ? "Ocultar chave" : "Mostrar chave");
  });

  cloudSync.addEventListener("click", async () => {
    cloudSync.disabled = true;
    const result = await call("sync_now");
    cloudSync.disabled = false;
    if (result?.cloud) setCloudStatus(result.cloud);
    if (result?.ok) toast("Memória sincronizada", { iconName: "refresh" });
    else toast(result?.error || "Não foi possível sincronizar.", { tone: "error" });
  });

  // Desconectar pede um segundo clique (o botão vira "Desconectar?" por alguns segundos).
  cloudDisconnect.addEventListener("click", async () => {
    if (cloudDisconnect.dataset.confirm !== "true") {
      cloudDisconnect.dataset.confirm = "true";
      cloudDisconnect.textContent = "Desconectar?";
      clearTimeout(disconnectTimer);
      disconnectTimer = setTimeout(resetDisconnect, DISCONNECT_CONFIRM_MS);
      return;
    }
    resetDisconnect();
    const result = await call("disconnect_supabase");
    if (!result?.ok) {
      toast(result?.error || "Não foi possível desconectar.", { tone: "error" });
      return;
    }
    setCloud(result);
    setGit(result);
    onCloudChanged?.(null);
    toast("Supabase desconectado: a memória volta a ficar só neste PC");
  });
  cloudDisconnect.addEventListener("blur", resetDisconnect);

  // ---------- atalho global: captura da combinação ----------

  const NAMED_KEYS = {
    Space: "Space", Tab: "Tab", PageUp: "PageUp", PageDown: "PageDown", Home: "Home", End: "End",
    Insert: "Insert", Delete: "Delete", ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right",
  };

  // Pela posição física (event.code), como o código virtual do RegisterHotKey.
  function keyName(code) {
    const match = /^Key([A-Z])$/.exec(code) || /^Digit(\d)$/.exec(code);
    if (match) return match[1];
    if (/^F\d{1,2}$/.test(code)) return code;
    return NAMED_KEYS[code] || "";
  }

  hotkeyInput.addEventListener("keydown", (event) => {
    const plainTab = event.key === "Tab" && !event.ctrlKey && !event.altKey && !event.metaKey;
    if (event.key === "Escape" || plainTab) return; // Esc fecha o modal; Tab navega
    event.preventDefault();
    event.stopPropagation(); // os atalhos do app (Ctrl+K, Ctrl+S...) não disparam durante a captura
    const modifiers = [event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift", event.metaKey && "Win"].filter(Boolean);
    const key = keyName(event.code);
    if (!key) {
      hotkeyInput.value = modifiers.length ? `${modifiers.join("+")}+…` : capturedHotkey || savedHotkey;
      return;
    }
    capturedHotkey = [...modifiers, key].join("+");
    hotkeyInput.value = capturedHotkey;
    hotkeySave.disabled = capturedHotkey === savedHotkey;
    setHotkeyError("");
  });

  // Soltou só os modificadores: volta a mostrar a última combinação completa.
  hotkeyInput.addEventListener("keyup", () => {
    if (hotkeyInput.value.endsWith("…")) hotkeyInput.value = capturedHotkey || savedHotkey;
  });

  hotkeySave.addEventListener("click", async () => {
    if (!capturedHotkey) return;
    hotkeySave.disabled = true;
    const result = await call("set_hotkey", capturedHotkey);
    if (!result?.ok) {
      hotkeySave.disabled = false;
      setHotkeyError(result?.error || "Não foi possível trocar o atalho.");
      return;
    }
    setHotkey({ hotkey: result.hotkey, hotkey_error: "" });
    toast(`Atalho global: ${result.hotkey}`, { iconName: "key" });
  });

  autostartSwitch.addEventListener("click", async () => {
    if (autostartSwitch.disabled) return;
    const enable = autostartSwitch.getAttribute("aria-checked") !== "true";
    autostartSwitch.setAttribute("aria-checked", String(enable)); // resposta imediata; corrigida abaixo se falhar
    const result = await call("set_settings", { autostart: enable });
    if (!result?.ok) {
      autostartSwitch.setAttribute("aria-checked", String(!enable));
      toast(result?.error || "Não foi possível alterar a inicialização.", { tone: "error" });
      return;
    }
    setAutostart(result);
    toast(enable ? "Inicia com o Windows" : "Não inicia mais com o Windows");
  });

  openDataDir.addEventListener("click", async () => {
    const result = await call("open_data_dir");
    if (!result?.ok) toast(result?.error || "Não foi possível abrir a pasta de dados.", { tone: "error" });
  });

  gitSwitch.addEventListener("click", async () => {
    if (gitSwitch.disabled) return;
    const enable = gitSwitch.getAttribute("aria-checked") !== "true";
    gitSwitch.setAttribute("aria-checked", String(enable)); // resposta imediata; corrigida abaixo se falhar
    const result = await call("set_settings", { git_sync: enable });
    if (!result?.ok) {
      gitSwitch.setAttribute("aria-checked", String(!enable));
      toast(result?.error || "Não foi possível alterar o sync Git.", { tone: "error" });
      return;
    }
    setGit(result);
    onSyncChanged?.(Boolean(result.git_sync && result.git_available));
    toast(enable ? "Sync Git ligado" : "Sync Git desligado", { iconName: "git-branch" });
  });

  // ---------- tema e paleta ----------

  const themeChoices = [...modal.querySelectorAll("[data-theme-choice]")];
  const themeHint = document.getElementById("theme-hint");
  // Amostra de cada paleta: fundo, superfície, texto e destaque, nas cores dela e no tema atual.
  const swatches = PALETTES.map(({ id, name }) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "swatch";
    button.dataset.paletteChoice = id;
    const chips = document.createElement("span");
    chips.className = "swatch__chips";
    chips.dataset.palette = id;
    chips.setAttribute("aria-hidden", "true");
    chips.append(...["surface", "text", "accent"].map((part) => Object.assign(document.createElement("i"), { className: `swatch__${part}` })));
    const label = Object.assign(document.createElement("span"), { className: "swatch__name", textContent: name });
    button.append(chips, label);
    button.addEventListener("click", () => setPalette(id));
    return button;
  });
  document.getElementById("swatches").append(...swatches);

  onThemeChange(({ choice, theme, palette }) => {
    for (const button of themeChoices) button.setAttribute("aria-pressed", String(button.dataset.themeChoice === choice));
    themeHint.textContent =
      choice === "system" ? `Segue o Windows: ${theme === "dark" ? "escuro" : "claro"} agora.` : "Fixo: não muda com o Windows.";
    for (const button of swatches) {
      button.setAttribute("aria-pressed", String(button.dataset.paletteChoice === palette));
      button.querySelector(".swatch__chips").dataset.theme = theme;
    }
  });
  for (const button of themeChoices) button.addEventListener("click", () => setThemeChoice(button.dataset.themeChoice));

  for (const node of overlay.querySelectorAll("[data-close]")) node.addEventListener("click", () => close?.());
  modal.addEventListener("keydown", (event) => trapFocus(modal, event));

  return {
    open,
    /** onCloudStatus com o modal aberto: o estado acompanha. */
    setCloudStatus(status) {
      if (close && !cloudSync.hidden) setCloudStatus(status);
    },
  };
}
