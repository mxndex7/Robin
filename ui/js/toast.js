// Toasts no canto inferior direito, empilháveis.
// Entram de baixo (--rise-lg + fade), saem com fade + scale --pop, e a pilha se reacomoda
// com FLIP (só transform). Cada chamada cria exatamente um toast.

import { icon } from "./icons.js";
import { motion, prefersReducedMotion, settled } from "./overlay.js";

const DURATION_MS = 2600;
const MAX_TOASTS = 4;

const region = document.getElementById("toasts");

// Anima os toasts que mudaram de lugar depois de `mutate` (inserção ou remoção).
function withFlip(mutate) {
  const before = new Map([...region.children].map((child) => [child, child.getBoundingClientRect().top]));
  mutate();
  if (prefersReducedMotion()) return;
  const { slow, easeOut } = motion();
  for (const [child, top] of before) {
    if (!child.isConnected) continue;
    const delta = top - child.getBoundingClientRect().top;
    if (delta) {
      child.animate([{ transform: `translateY(${delta}px)` }, { transform: "none" }], { duration: slow, easing: easeOut });
    }
  }
}

function dismiss(item) {
  if (item.dataset.leaving) return;
  item.dataset.leaving = "1";
  const { base, easeIn, pop } = motion();
  const frames = prefersReducedMotion()
    ? [{ opacity: 1 }, { opacity: 0 }]
    : [{ opacity: 1, transform: "none" }, { opacity: 0, transform: `scale(${pop})` }];
  settled(item.animate(frames, { duration: base, easing: easeIn, fill: "forwards" })).then(() =>
    withFlip(() => item.remove()),
  );
}

/** Mostra um toast. `tone: "error"` usa o ícone de alerta. */
export function toast(message, { iconName = "check", tone = "default" } = {}) {
  const item = document.createElement("div");
  item.className = `toast${tone === "error" ? " toast--error" : ""}`;
  const text = document.createElement("span");
  text.textContent = message;
  item.append(icon(tone === "error" ? "alert" : iconName, "icon icon--sm"), text);

  withFlip(() => region.append(item));
  const { slow, easeOut, riseLg } = motion();
  item.animate(
    prefersReducedMotion()
      ? [{ opacity: 0 }, { opacity: 1 }]
      : [{ opacity: 0, transform: `translateY(${riseLg})` }, { opacity: 1, transform: "none" }],
    { duration: slow, easing: easeOut },
  );

  const visible = [...region.children].filter((child) => !child.dataset.leaving);
  if (visible.length > MAX_TOASTS) dismiss(visible[0]);
  setTimeout(() => dismiss(item), DURATION_MS);
}
