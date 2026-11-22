// Camadas sobrepostas (popover, modal, palette, confirmação inline) e os utilitários de movimento
// usados pelos outros módulos.
// Uma pilha: Esc e clique fora fecham só a camada do topo, e o foco volta para quem a abriu.
// Entrada: scale --pop → 1 + fade a partir do ponto de disparo; saída: fade curto.

const stack = [];
let motionTokens = null;

export const prefersReducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Tokens de movimento do tokens.css (durações em ms, curvas e distâncias), lidos na primeira vez que
 * alguém anima: as coreografias em JS seguem os mesmos valores do CSS.
 */
export function motion() {
  if (!motionTokens) {
    const css = getComputedStyle(document.documentElement);
    const value = (name) => css.getPropertyValue(name).trim();
    const ms = (name) => parseFloat(value(name)) || 0;
    motionTokens = {
      fast: ms("--dur-1"),
      base: ms("--dur-2"),
      slow: ms("--dur-3"),
      panel: ms("--dur-4"),
      intro: ms("--dur-intro"),
      stagger: ms("--stagger"),
      easeOut: value("--ease-out"),
      easeIn: value("--ease-in"),
      easeInOut: value("--ease-in-out"),
      rise: value("--rise"),
      riseLg: value("--rise-lg"),
      pop: parseFloat(value("--pop")) || 1,
    };
  }
  return motionTokens;
}

export const wait = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));

/**
 * Promessa do fim de uma animação com prazo de reserva: sem renderização (janela minimizada,
 * aba em segundo plano), `finished` só resolveria no próximo frame e a UI ficaria presa.
 */
export function settled(animation) {
  const timing = animation.effect?.getComputedTiming?.();
  const limit = (timing?.endTime ?? 300) + 50;
  return Promise.race([animation.finished.catch(() => {}), new Promise((resolve) => setTimeout(resolve, limit))]);
}

// Cancela animações anteriores do elemento (a saída usa fill: forwards e ficaria acumulada).
function reset(element) {
  for (const animation of element.getAnimations()) animation.cancel();
}

export function animateIn(element, origin = "center") {
  reset(element);
  element.style.transformOrigin = origin;
  const { base, fast, easeOut, pop } = motion();
  const reduced = prefersReducedMotion();
  const frames = reduced
    ? [{ opacity: 0 }, { opacity: 1 }]
    : [{ opacity: 0, transform: `scale(${pop})` }, { opacity: 1, transform: "none" }];
  return settled(element.animate(frames, { duration: reduced ? fast : base, easing: easeOut }));
}

export function animateOut(element) {
  reset(element);
  const { fast, easeIn, pop } = motion();
  const frames = prefersReducedMotion()
    ? [{ opacity: 1 }, { opacity: 0 }]
    : [{ opacity: 1, transform: "none" }, { opacity: 0, transform: `scale(${pop})` }];
  return settled(element.animate(frames, { duration: fast, easing: easeIn, fill: "forwards" }));
}

/**
 * Registra uma camada aberta. `element`: área que não fecha ao clicar; `anchor`: gatilho (também não fecha);
 * `onClose`: fecha de fato. Devolve a função que fecha a camada.
 */
export function openLayer({ element, anchor = null, onClose, returnFocus = document.activeElement }) {
  const layer = { element, anchor, onClose, returnFocus };
  stack.push(layer);
  return () => closeLayer(layer);
}

function closeLayer(layer, { restoreFocus = true } = {}) {
  const index = stack.indexOf(layer);
  if (index === -1) return;
  stack.splice(index, 1);
  layer.onClose();
  if (restoreFocus && layer.returnFocus?.isConnected) layer.returnFocus.focus({ preventScroll: true });
}

export const hasOpenLayer = () => stack.length > 0;

/** Fundo escurecido de modal e palette: entra com fade e sai com fade (fica transparente até ser escondido). */
export function fadeBackdrop(backdrop, show) {
  for (const animation of backdrop.getAnimations()) animation.cancel();
  const { base, fast, easeOut, easeIn } = motion();
  backdrop.animate(show ? [{ opacity: 0 }, { opacity: 1 }] : [{ opacity: 1 }, { opacity: 0 }], {
    duration: show ? base : fast,
    easing: show ? easeOut : easeIn,
    fill: show ? "none" : "forwards",
  });
}

// Fase de captura: o Esc fecha a camada antes de chegar ao atalho "parar geração".
window.addEventListener(
  "keydown",
  (event) => {
    if (event.key !== "Escape" || !stack.length) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    closeLayer(stack.at(-1));
  },
  true,
);

document.addEventListener(
  "pointerdown",
  (event) => {
    const top = stack.at(-1);
    if (!top || top.element.contains(event.target) || top.anchor?.contains(event.target)) return;
    closeLayer(top, { restoreFocus: false });
  },
  true,
);

/** Mantém o Tab dentro de `container` (modais e palette). */
export function trapFocus(container, event) {
  if (event.key !== "Tab") return;
  const focusable = [...container.querySelectorAll("button, input, textarea, [tabindex='0']")].filter(
    (node) => !node.disabled && !node.hidden && node.offsetParent !== null,
  );
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
