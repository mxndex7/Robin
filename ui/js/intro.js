// Coreografia de abertura e volta do estado vazio depois de limpar a conversa.
//   1. Fundo e estrutura aparecem em fade.
//   2. O R entra em duas partes: o traço da esquerda (em brasa) sobe pela própria inclinação e, logo
//      depois, o corpo chega da esquerda (fade + translate).
//   3. Cabeçalho, título, sugestões e composer sobem em sequência (fade + --rise).
// Durações e curvas vêm dos tokens de movimento (overlay.js: motion). Com movimento reduzido: só fades.

import { motion, prefersReducedMotion } from "./overlay.js";

const fadeIn = (element, delay = 0) => {
  const { base, easeOut } = motion();
  return element.animate([{ opacity: 0 }, { opacity: 1 }], { duration: base, delay, easing: easeOut, fill: "backwards" });
};

// Os deslocamentos estão em unidades do viewBox do R (176 de lado, ~3 por pixel no estado vazio). O
// traço sobe na própria inclinação: 1 para o lado a cada 2 para cima, como o --slant.
function revealMark(delay) {
  const mark = document.getElementById("empty-mark");
  if (prefersReducedMotion()) {
    fadeIn(mark, delay);
    return;
  }
  const { intro, stagger, easeOut } = motion();
  const enter = (part, from, partDelay) =>
    mark.querySelector(part).animate([{ opacity: 0, transform: from }, { opacity: 1, transform: "none" }], {
      duration: intro,
      delay: partDelay,
      easing: easeOut,
      fill: "backwards",
    });
  enter(".mark__stem", "translate(-13px, 26px)", delay);
  enter(".mark__body", "translateX(-24px)", delay + 2 * stagger);
}

function rise(elements, startDelay) {
  const reduced = prefersReducedMotion();
  const { base, panel, stagger, easeOut, rise: distance } = motion();
  const frames = reduced
    ? [{ opacity: 0 }, { opacity: 1 }]
    : [{ opacity: 0, transform: `translateY(${distance})` }, { opacity: 1, transform: "none" }];
  elements.forEach((element, index) => {
    element.animate(frames, {
      duration: reduced ? base : panel,
      delay: startDelay + index * stagger,
      easing: easeOut,
      fill: "backwards",
    });
  });
}

function emptyStateParts() {
  const empty = document.getElementById("empty-state");
  return [
    empty.querySelector(".empty__title"),
    empty.querySelector(".empty__subtitle"),
    ...[...empty.querySelectorAll(".key-notice:not([hidden]), .suggestion")],
  ];
}

/** Abertura do app. Chamada uma vez, quando o estado inicial já está aplicado. */
export function playIntro() {
  const { stagger } = motion();
  const structure = [document.getElementById("titlebar"), document.getElementById("app")];
  document.documentElement.classList.remove("is-booting");
  for (const element of structure) fadeIn(element);

  const emptyVisible = !document.getElementById("empty-state").hidden;
  if (emptyVisible) revealMark(3 * stagger);
  rise(
    [
      document.querySelector(".chat-header"),
      ...(emptyVisible ? emptyStateParts() : []),
      document.getElementById("composer"),
      document.querySelector(".composer-caption"),
    ],
    4 * stagger,
  );
}

/** O estado vazio volta com a mesma coreografia (depois de limpar a conversa). */
export function playEmptyIntro() {
  revealMark(0);
  rise(emptyStateParts(), 2 * motion().stagger);
}
