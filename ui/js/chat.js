// Conversa: mensagens, ciclo da geração (pensando → raciocinando → streaming → fim), parar, regenerar,
// erros, rolagem, nova conversa e abrir uma conversa guardada (as mensagens entram prontas, sem
// streaming).
//
// Ordem dos eventos: o Python dispara a geração antes de devolver o gen_id, então onThinking/onChunk
// podem chegar antes da resposta da chamada. Enquanto o gen_id não chega, os eventos ficam num
// buffer e são reaplicados depois; eventos de outra geração (já parada ou trocada) são ignorados.

import { call, on } from "./bridge.js";
import { icon } from "./icons.js";
import { playEmptyIntro } from "./intro.js";
import { renderMarkdown } from "./markdown.js";
import { motion, prefersReducedMotion, settled } from "./overlay.js";
import { createStreamRenderer } from "./stream-render.js";
import { setStatus } from "./titlebar.js";
import { toast } from "./toast.js";

const FOLLOW_THRESHOLD = 32;
const BUTTON_THRESHOLD = 160;
const COPIED_MS = 1500;

const ERROR_TITLES = {
  no_key: "API Key não configurada",
  invalid_key: "API Key inválida",
  rate_limit: "Limite de uso atingido",
  network: "Sem conexão",
  model_unavailable: "Modelo indisponível",
  empty_response: "Resposta vazia",
  server: "Falha no serviço do Gemini",
  timeout: "Tempo esgotado",
  forbidden: "Sem permissão",
};

// Ícone do card de erro: sem conexão tem o próprio; o resto usa o alerta.
const ERROR_ICONS = {
  network: "wifi-off",
};

// Ícone de cada ferramenta do cérebro na linha discreta da mensagem.
const TOOL_ICONS = {
  buscar_memoria: "search",
  salvar_memoria: "save",
  atualizar_memoria: "pencil",
  concluir_tarefa: "check",
  apagar_memoria: "trash",
};

// Rótulos do indicador antes do primeiro texto (o conteúdo do raciocínio nunca é exibido). Sem
// reticências: a partir de 1s o contador vem logo depois ("Pensando · 12s").
const THINKING_LABEL = "Pensando";
const REASONING_LABEL = "Raciocinando";
const RETRY_LABEL = "Tentando de novo";
const WAITING_LABEL = "Aguardando o limite por minuto";
const ELAPSED_FROM_MS = 1000;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function button(className, label, ...children) {
  const node = el("button", className);
  node.type = "button";
  node.setAttribute("aria-label", label);
  node.append(...children);
  return node;
}

const copiedTimers = new WeakMap();

/** Copia texto: Clipboard API → área de transferência nativa (Python) → execCommand. */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      if ((await call("copy_text", text))?.ok) return true;
    } catch {
      // Sem a ponte: segue para o execCommand.
    }
    const area = el("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/** Ícone copiar → check por 1,5s, mais um único toast "Copiado". */
function flashCopied(target) {
  target.classList.add("is-copied");
  clearTimeout(copiedTimers.get(target));
  copiedTimers.set(target, setTimeout(() => target.classList.remove("is-copied"), COPIED_MS));
  toast("Copiado", { iconName: "copy" });
}

function copyButton(label, className = "") {
  return button(
    `icon-btn icon-btn--sm copy-btn ${className}`.trim(),
    label,
    icon("copy", "icon icon--sm copy-btn__copy"),
    icon("check", "icon icon--sm copy-btn__check"),
  );
}

const clock = new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" });
const dayAndClock = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

/** Horário da resposta no cabeçalho da mensagem: HH:MM se for de hoje; senão, com o dia. */
function timeNode(date = new Date()) {
  const today = date.toDateString() === new Date().toDateString();
  const node = el("time", "message__time", (today ? clock : dayAndClock).format(date).replace(",", "")); // 01/11 14:20
  node.dateTime = date.toISOString();
  return node;
}

function thinkingIndicator() {
  const wrap = el("div", "thinking");
  // Três traços de brasa (o traço do R) acendendo em sequência (animations.css).
  const embers = el("span", "embers");
  embers.setAttribute("aria-hidden", "true");
  embers.append(el("i"), el("i"), el("i"));
  // Segundos desde o envio: 20s de "Pensando" parado parecem travamento. Os leitores de tela
  // ficam com o status da barra de título (o contador mudaria a cada segundo).
  const elapsed = el("span", "thinking__elapsed");
  elapsed.setAttribute("aria-hidden", "true");
  wrap.append(embers, el("span", "thinking__label", THINKING_LABEL), elapsed);
  return wrap;
}

/** Liga o contador "· Ns" do indicador; para sozinho no primeiro texto ou no fim (stopElapsed). */
function startElapsed(view) {
  const node = view.message.querySelector(".thinking__elapsed");
  const start = performance.now();
  const tick = () => {
    const ms = performance.now() - start;
    if (ms >= ELAPSED_FROM_MS) node.textContent = `· ${Math.floor(ms / 1000)}s`;
  };
  view.timer = setInterval(tick, 250); // 250ms: o segundo vira na hora certa, sem atraso visível
}

function stopElapsed(view) {
  clearInterval(view.timer);
  view.timer = null;
}

function duration(seconds) {
  if (!seconds) return "alguns minutos";
  if (seconds >= 3600) return `cerca de ${Math.round(seconds / 3600)}h`;
  return `${Math.max(1, Math.round(seconds / 60))} min`;
}

/** Troca o rótulo do indicador com um fade curto (e o status para leitores de tela). */
function setThinkingLabel(view, label) {
  if (view.started || view.label === label) return;
  view.label = label;
  const node = view.message.querySelector(".thinking__label");
  node.textContent = label;
  const { base, easeOut } = motion();
  node.animate([{ opacity: 0 }, { opacity: 1 }], { duration: base, easing: easeOut });
  setStatus("thinking", label);
}

export function createChat({ composer, onModelChanged, onProviderChanged }) {
  const main = document.getElementById("main");
  const conversation = document.getElementById("conversation");
  const list = document.getElementById("messages");
  const empty = document.getElementById("empty-state");
  const dock = document.querySelector(".composer-dock");
  const toBottom = document.getElementById("scroll-bottom");

  let generating = false;
  let regenerationAllowed = true;
  let current = null; // { genId, view } da geração exibida
  let pendingView = null; // view criada enquanto o gen_id ainda não chegou
  let starting = null; // chamada send/regenerate em andamento
  let clearing = null; // chamada new_conversation/open_conversation em andamento
  let buffered = [];
  let stopRequested = false;
  let follow = true;
  let smoothScrolling = false;

  // ---------- rolagem ----------

  // O espaço no fim da conversa acompanha a altura do composer (que cresce até ~8 linhas).
  new ResizeObserver(() => main.style.setProperty("--dock-h", `${dock.offsetHeight}px`)).observe(dock);

  const distanceFromEnd = () => conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight;

  function updateScrollButton() {
    toBottom.classList.toggle("is-visible", distanceFromEnd() > BUTTON_THRESHOLD);
  }

  function stickToEnd() {
    if (follow) conversation.scrollTop = conversation.scrollHeight;
    updateScrollButton();
  }

  const header = document.querySelector(".chat-header");
  conversation.addEventListener(
    "scroll",
    () => {
      if (!smoothScrolling) follow = distanceFromEnd() < FOLLOW_THRESHOLD;
      updateScrollButton();
      header.classList.toggle("is-scrolled", conversation.scrollTop > 4); // linha sob o cabeçalho
    },
    { passive: true },
  );
  conversation.addEventListener("scrollend", () => {
    smoothScrolling = false;
    follow = distanceFromEnd() < FOLLOW_THRESHOLD;
  });
  toBottom.addEventListener("click", () => {
    follow = true;
    smoothScrolling = !prefersReducedMotion();
    conversation.scrollTo({ top: conversation.scrollHeight, behavior: smoothScrolling ? "smooth" : "auto" });
  });

  // ---------- mensagens ----------

  function appendUser(text, files = []) {
    const message = el("div", "message message--user");
    const bubble = el("div", "message__bubble");
    if (files.length) {
      const fileList = el("ul", "message__attachments file-chips");
      fileList.setAttribute("aria-label", "Anexos enviados");
      for (const name of files) {
        const chip = el("li", "file-chip");
        const label = el("span", "file-chip__name", name);
        label.title = name;
        chip.append(icon("paperclip", "icon icon--sm"), label);
        fileList.append(chip);
      }
      bubble.append(fileList);
    }
    bubble.append(document.createTextNode(text));
    message.append(bubble);
    list.append(message);
    return message;
  }

  function createAssistantView() {
    const message = el("article", "message message--assistant is-thinking is-latest");
    const meta = el("div", "message__meta");
    meta.append(icon("robin", "mark message__mark"), el("span", "message__who", "Robin"), timeNode());
    const body = el("div", "message__body markdown");
    // "Pensando" e a resposta ocupam a mesma célula: o crossfade não desloca o layout.
    const stage = el("div", "message__stage");
    stage.append(thinkingIndicator(), body);
    const tools = el("div", "message__tools");
    const actions = el("div", "message__actions");
    const copy = copyButton("Copiar resposta");
    const regen = button("icon-btn icon-btn--sm regenerate-btn", "Regenerar resposta", icon("refresh", "icon icon--sm"));
    actions.append(copy, regen);
    message.append(meta, tools, stage, actions);

    for (const old of list.querySelectorAll(".message--assistant.is-latest")) old.classList.remove("is-latest");
    list.append(message);

    const renderer = createStreamRenderer(body, { onUpdate: stickToEnd });
    copy.addEventListener("click", async () => {
      if (await copyText(renderer.text)) flashCopied(copy);
      else toast("Não foi possível copiar.", { tone: "error" });
    });
    regen.addEventListener("click", () => regenerate());
    const view = { message, body, tools, renderer, started: false, label: THINKING_LABEL, timer: null };
    startElapsed(view);
    return view;
  }

  function appendToolLine(view, { tool, label }) {
    const line = el("div", "tool-line");
    line.append(icon(TOOL_ICONS[tool] || "cpu", "icon icon--sm"), el("span", "tool-line__label", label));
    view.tools.append(line);
    stickToEnd();
  }

  function appendNote(text, before = null) {
    const note = el("div", "message message--note");
    note.append(icon("alert", "icon icon--sm"), el("span", null, text));
    list.insertBefore(note, before);
  }

  function appendError({ code, message, error, detail }, allowRetry = true) {
    const card = el("div", "message message--error");
    card.setAttribute("role", "alert");
    const text = el("div", "error-card__text");
    text.append(
      el("p", "error-card__title", ERROR_TITLES[code] || "Não foi possível responder"),
      el("p", "error-card__detail", message || error || "Erro desconhecido."),
    );
    if (detail) {
      // Erro original do SDK (sem a chave), recolhido por padrão.
      const more = el("details", "error-card__more");
      const summary = el("summary", "error-card__summary");
      summary.append(el("span", null, "Detalhes"), icon("chevron-down", "icon icon--sm"));
      more.append(summary, el("pre", "error-card__raw", detail));
      text.append(more);
    }
    const action = el("button", "btn btn--secondary btn--sm", code === "no_key" ? "Configurar" : "Tentar novamente");
    action.type = "button";
    action.addEventListener("click", () => {
      if (code === "no_key") window.dispatchEvent(new CustomEvent("robin:configure"));
      else regenerate();
    });
    card.append(icon(ERROR_ICONS[code] || "alert", "icon error-card__icon"), text);
    if (allowRetry || code === "no_key") card.append(action);
    list.append(card);
    stickToEnd();
  }

  // Resposta de uma conversa guardada: o mesmo cartão do streaming, já pronto (texto, ações, parada).
  function appendStoredAssistant(stored) {
    const message = el("article", "message message--assistant is-done");
    const meta = el("div", "message__meta");
    meta.append(icon("robin", "mark message__mark"), el("span", "message__who", "Robin"), timeNode(new Date(stored.created_at)));
    const body = el("div", "message__body markdown");
    if (stored.text) body.append(renderMarkdown(stored.text));
    const stage = el("div", "message__stage");
    stage.append(body);
    const tools = el("div", "message__tools");
    for (const action of stored.actions || []) appendToolLine({ tools }, action);
    const actions = el("div", "message__actions");
    const copy = copyButton("Copiar resposta");
    const regen = button("icon-btn icon-btn--sm regenerate-btn", "Regenerar resposta", icon("refresh", "icon icon--sm"));
    actions.append(copy, regen);
    message.append(meta, tools, stage, actions);
    if (stored.status === "stopped") {
      message.append(el("p", "message__status", stored.text ? "Interrompido" : "Interrompido antes da resposta"));
    }
    copy.addEventListener("click", async () => {
      if (await copyText(stored.text)) flashCopied(copy);
      else toast("Não foi possível copiar.", { tone: "error" });
    });
    regen.addEventListener("click", () => regenerate());
    list.append(message);
  }

  function renderStored(messages) {
    for (const stored of messages) {
      if (stored.role === "user") {
        const files = stored.actions?.attachments;
        appendUser(stored.text, Array.isArray(files) ? files : []);
        continue;
      }
      if (stored.text || stored.status !== "error") appendStoredAssistant(stored);
      if (stored.status === "error") appendError({ code: stored.error?.code, message: stored.error?.message }, regenerationAllowed);
    }
    // "Tentar novamente" e regenerar só valem para o último turno.
    for (const card of list.querySelectorAll(".message--error")) card.classList.toggle("is-stale", card !== list.lastElementChild);
    const latest = [...list.querySelectorAll(".message--assistant")].at(-1);
    latest?.classList.add("is-latest");
    if (!regenerationAllowed) {
      latest?.querySelector(".regenerate-btn")?.remove();
      list.append(el("p", "message message__status", "Para usar os arquivos novamente, anexe-os em uma nova mensagem."));
    }
  }

  // Remove tudo depois da última mensagem do usuário (resposta anterior, erros, avisos).
  function clearAfterLastUser() {
    const lastUser = [...list.querySelectorAll(".message--user")].at(-1);
    if (!lastUser) return;
    while (lastUser.nextElementSibling) lastUser.nextElementSibling.remove();
  }

  // ---------- geração ----------

  function setGenerating(value) {
    generating = value;
    composer.setGenerating(value);
    setStatus(value ? "thinking" : "ready", value ? "Gerando resposta" : "Pronto");
  }

  function finalize(view) {
    stopElapsed(view);
    view.message.classList.remove("is-thinking");
    view.message.classList.add("is-done");
    current = null;
    setGenerating(false);
  }

  function dispatch(name, payload) {
    if (!current || payload.gen_id !== current.genId) return;
    const { view } = current;

    if (name === "onChunk") {
      if (!view.started) {
        view.started = true;
        stopElapsed(view);
        view.message.classList.remove("is-thinking");
        if (view.label !== THINKING_LABEL) setStatus("thinking", "Gerando resposta");
      }
      view.renderer.append(payload.text);
    } else if (name === "onThinking") {
      setThinkingLabel(view, REASONING_LABEL);
    } else if (name === "onRetry") {
      setThinkingLabel(view, RETRY_LABEL);
    } else if (name === "onWaiting") {
      setThinkingLabel(view, WAITING_LABEL);
    } else if (name === "onToolCall") {
      appendToolLine(view, payload);
    } else if (name === "onDone") {
      view.renderer.finish();
      if (payload.stopped) view.message.append(el("p", "message__status", view.started ? "Interrompido" : "Interrompido antes da resposta"));
      finalize(view);
    } else if (name === "onError") {
      if (view.started) view.renderer.finish();
      else view.message.remove();
      finalize(view);
      appendError(payload);
    }
  }

  function handle(name, payload) {
    if (pendingView) buffered.push([name, payload]);
    else dispatch(name, payload);
  }

  for (const name of ["onThinking", "onRetry", "onWaiting", "onToolCall", "onChunk", "onDone", "onError"]) {
    on(name, (payload) => handle(name, payload));
  }

  // fallback: modelo indisponível para a chave (gravado no .env). temporary: troca que expira
  // (expires_in segundos; cause: transient = falhas seguidas, rate_limit = limite por minuto,
  // daily_quota = cota diária, até ela voltar). restored: a troca expirou e o modelo escolhido voltou.
  const MODEL_NOTES = {
    fallback: ({ previous, model }) => `${previous} indisponível para esta chave. Usando ${model}.`,
    temporary: ({ previous, model, cause, expires_in: seconds }) =>
      cause === "daily_quota"
        ? `A cota diária de ${previous} acabou. Usando ${model} até ela voltar (${duration(seconds)}).`
        : cause === "rate_limit"
          ? `${previous} está no limite por minuto. Usando ${model} por ${duration(seconds)}.`
          : `${previous} não está respondendo. Usando ${model} por ${duration(seconds)}.`,
    restored: ({ previous, model }) => `De volta a ${model} (a troca para ${previous} expirou).`,
  };

  on("onModelChanged", (payload) => {
    onModelChanged?.(payload.model);
    if (MODEL_NOTES[payload.reason]) {
      const anchor = (pendingView || current?.view)?.message ?? null;
      appendNote(MODEL_NOTES[payload.reason](payload), anchor);
    }
  });

  async function startGeneration(request) {
    setGenerating(true);
    follow = true;
    stopRequested = false;
    const view = createAssistantView();
    pendingView = view;
    buffered = [];
    stickToEnd();

    let result;
    starting = Promise.resolve(request());
    try {
      result = await starting;
    } catch (error) {
      result = { ok: false, code: "bridge", error: String(error?.message || error) };
    }
    starting = null;
    pendingView = null;

    if (!result?.ok) {
      stopElapsed(view);
      view.message.remove();
      buffered = [];
      setGenerating(false);
      return result;
    }

    current = { genId: result.gen_id, view };
    const events = buffered;
    buffered = [];
    for (const [name, payload] of events) dispatch(name, payload);
    if (stopRequested && current) call("stop_generation");
    return result;
  }

  async function send(text, files = [], context = "") {
    if (generating) return;
    if (clearing) {
      await clearing;
      if (files.length) return; // anexos de outra conversa não acompanham a troca de contexto
    }
    if (generating) return;
    empty.hidden = true;
    for (const card of list.querySelectorAll(".message--error")) card.classList.add("is-stale");
    const userMessage = appendUser(text || "Analise os arquivos anexados.", files.map((file) => file.name));
    composer.clear();

    const payload = files.map(({ name, content }) => ({ name, content }));
    const result = await startGeneration(() => call("send_message", text, payload, context));
    if (result?.ok) {
      regenerationAllowed = true;
      return;
    }

    userMessage.remove();
    composer.restoreDraft(text, result?.code === "attachment_context" ? [] : files, context);
    appendError(result || {}, false);
  }

  async function regenerate() {
    if (generating || !regenerationAllowed) return;
    clearAfterLastUser();
    const result = await startGeneration(() => call("regenerate"));
    if (!result?.ok) appendError(result || {});
  }

  function stop() {
    if (!generating) return;
    if (pendingView) stopRequested = true;
    else call("stop_generation");
  }

  // Mensagens saem em fade escalonado de baixo para cima (o escalonamento cabe numa duração base).
  function fadeOutMessages(messages) {
    const { base, stagger, easeIn, rise } = motion();
    const step = Math.min(stagger, base / Math.max(1, messages.length));
    const frames = prefersReducedMotion()
      ? [{ opacity: 1 }, { opacity: 0 }]
      : [{ opacity: 1, transform: "none" }, { opacity: 0, transform: `translateY(${rise})` }];
    return Promise.all(
      messages.map((message, index) =>
        settled(
          message.animate(frames, {
            duration: base,
            delay: (messages.length - 1 - index) * step,
            easing: easeIn,
            fill: "forwards",
          }),
        ),
      ),
    );
  }

  // Antes de trocar de conversa: estado local primeiro, e os eventos da geração antiga passam a ser
  // ignorados (o Python interrompe a geração ao trocar).
  async function detach() {
    if (starting) await starting.catch(() => {}); // sem geração "fantasma" no Python
    current = null;
    pendingView = null;
    buffered = [];
    stopRequested = false;
    if (generating) setGenerating(false);
  }

  // As mensagens da conversa anterior saem em fade; as da próxima (render) entram de uma vez, com um
  // fade curto. Sem mensagens, volta o estado vazio.
  async function swapMessages(render) {
    const messages = [...list.querySelectorAll(".message")];
    await fadeOutMessages(messages);
    for (const message of messages) message.remove();
    render();
    const filled = Boolean(list.querySelector(".message"));
    empty.hidden = filled;
    if (filled) {
      const { base, easeOut } = motion();
      for (const message of list.querySelectorAll(".message")) message.animate([{ opacity: 0 }, { opacity: 1 }], { duration: base, easing: easeOut });
    } else {
      playEmptyIntro();
    }
    follow = true;
    conversation.scrollTop = conversation.scrollHeight;
    updateScrollButton();
  }

  /** Conversa nova: a atual continua guardada (aba Conversas). */
  async function newConversation() {
    await detach();
    composer.clearFiles();
    regenerationAllowed = true;
    clearing = call("new_conversation")
      .then((result) => onProviderChanged?.(result?.state))
      .catch(() => null)
      .finally(() => {
        clearing = null;
      });
    await swapMessages(() => {});
  }

  /** Abre uma conversa guardada para continuar. Devolve false se ela não abriu (aí nada muda: uma
   *  resposta em andamento continua). */
  async function openConversation(id) {
    if (starting) await starting.catch(() => {});
    clearing = call("open_conversation", id).catch((error) => ({ ok: false, error: String(error?.message || error) }));
    let result;
    try {
      result = await clearing;
    } finally {
      clearing = null;
    }
    if (!result?.ok) {
      toast(result?.error || "Não foi possível abrir a conversa.", { tone: "error" });
      return false;
    }
    await detach(); // o Python já interrompeu a geração da conversa anterior
    onProviderChanged?.(result.state);
    composer.clearFiles();
    regenerationAllowed = result.can_regenerate !== false;
    await swapMessages(() => renderStored(result.conversation?.messages ?? []));
    return true;
  }

  // Links abrem no navegador do sistema; botões de copiar dos blocos de código.
  list.addEventListener("click", async (event) => {
    const link = event.target.closest("a[href]");
    if (link) {
      event.preventDefault();
      call("open_external", link.getAttribute("href"));
      return;
    }
    const copy = event.target.closest(".code-block__copy");
    if (copy) {
      const code = copy.closest(".code-block").querySelector("code");
      if (await copyText(code.textContent)) flashCopied(copy);
      else toast("Não foi possível copiar.", { tone: "error" });
    }
  });

  return {
    send,
    stop,
    regenerate,
    newConversation,
    openConversation,
    setCanRegenerate(allowed) {
      regenerationAllowed = allowed;
      const button = list.querySelector(".message--assistant.is-latest .regenerate-btn");
      if (button) button.disabled = !allowed;
    },
    get generating() {
      return generating;
    },
    get hasMessages() {
      return Boolean(list.querySelector(".message"));
    },
    get canRegenerate() {
      return regenerationAllowed && !generating && Boolean(list.querySelector(".message--user"));
    },
  };
}
