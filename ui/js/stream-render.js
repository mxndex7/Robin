// Renderização do markdown durante o streaming.
//
// - marked.lexer separa o texto em blocos; cada bloco vira um <div class="md-block">.
//   Blocos cujo texto bruto não mudou não são tocados (só o bloco final costuma mudar).
// - A tela é atualizada no máximo uma vez por frame (requestAnimationFrame).
// - Um patcher de DOM (no estilo do morphdom) mantém os nós existentes: quando um nó de texto
//   cresce, só o sufixo novo entra num <span class="chunk"> com fade; elementos novos entram com fade.
//   Nada que já estava na tela é recriado, então o layout não treme.
// - O bloco de código ainda aberto é re-renderizado (e re-destacado) no máximo a cada 150ms.
// - Um cursor fino acompanha o fim do texto enquanto a resposta chega.

import { lex, renderTokens } from "./markdown.js";
import { motion, settled } from "./overlay.js";

const CODE_THROTTLE_MS = 150;
const ENTER = [{ opacity: 0 }, { opacity: 1 }];

// Elemento novo do markdown entra em fade (mesma duração e curva dos trechos .chunk no CSS).
function fadeIn(node) {
  const { base, easeOut } = motion();
  node.animate(ENTER, { duration: base, easing: easeOut });
}

const isChunk = (node) => node.nodeType === Node.ELEMENT_NODE && node.classList.contains("chunk");
const isTextLike = (node) => node.nodeType === Node.TEXT_NODE || isChunk(node);

function chunkSpan(text) {
  const span = document.createElement("span");
  span.className = "chunk";
  span.textContent = text;
  return span;
}

function syncAttributes(target, desired) {
  for (const { name } of [...target.attributes]) {
    if (!desired.hasAttribute(name)) target.removeAttribute(name);
  }
  for (const { name, value } of desired.attributes) {
    if (target.getAttribute(name) !== value) target.setAttribute(name, value);
  }
}

// Um nó de texto desejado corresponde, no DOM vivo, a uma sequência de nós de texto e spans .chunk.
function patchText(parent, cursor, text) {
  const nodes = [];
  let current = "";
  let node = cursor;
  while (node && isTextLike(node)) {
    nodes.push(node);
    current += node.textContent;
    node = node.nextSibling;
  }
  if (nodes.length && text.startsWith(current)) {
    const suffix = text.slice(current.length);
    if (suffix) parent.insertBefore(chunkSpan(suffix), node);
    return node;
  }
  // O texto mudou (o markdown reinterpretou o trecho): troca sem fade, para não piscar.
  for (const old of nodes) old.remove();
  parent.insertBefore(nodes.length ? document.createTextNode(text) : chunkSpan(text), node);
  return node;
}

function patchChildren(target, source) {
  let cursor = target.firstChild;
  for (const desired of [...source.childNodes]) {
    if (desired.nodeType === Node.TEXT_NODE) {
      cursor = patchText(target, cursor, desired.data);
    } else if (desired.nodeType === Node.ELEMENT_NODE) {
      if (cursor && cursor.nodeType === Node.ELEMENT_NODE && !isChunk(cursor) && cursor.tagName === desired.tagName) {
        syncAttributes(cursor, desired);
        patchChildren(cursor, desired);
        cursor = cursor.nextSibling;
      } else {
        target.insertBefore(desired, cursor);
        fadeIn(desired);
      }
    }
  }
  while (cursor) {
    const next = cursor.nextSibling;
    cursor.remove();
    cursor = next;
  }
}

// Desfaz os spans .chunk (depois do fade) para o DOM não acumular nós.
function unwrapChunk(span) {
  const parent = span.parentNode;
  if (!parent) return;
  span.replaceWith(span.textContent);
  parent.normalize();
}

function lastTextNode(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => (node.data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
  });
  let last = null;
  while (walker.nextNode()) last = walker.currentNode;
  return last;
}

export function createStreamRenderer(root, { onUpdate } = {}) {
  const blocks = []; // { raw, el }
  const caret = document.createElement("span");
  caret.className = "caret";
  caret.setAttribute("aria-hidden", "true");

  let source = "";
  let done = false;
  let frame = 0;
  let lastCodeRender = 0;

  root.addEventListener("animationend", (event) => {
    if (isChunk(event.target)) unwrapChunk(event.target);
  });

  function placeCaret() {
    const last = blocks.at(-1)?.el;
    if (!last) return;
    const code = last.querySelector(".code-block code");
    if (code) {
      code.append(caret);
      return;
    }
    const text = lastTextNode(last);
    if (text) text.parentNode.insertBefore(caret, text.nextSibling);
    else last.append(caret);
  }

  function render() {
    frame = 0;
    caret.remove();
    const tokens = lex(source).filter((token) => token.type !== "space");
    let deferred = false;

    tokens.forEach((token, index) => {
      const block = blocks[index];
      if (block && block.raw === token.raw) return;

      const live = !done && index === tokens.length - 1;
      if (block && live && token.type === "code" && performance.now() - lastCodeRender < CODE_THROTTLE_MS) {
        deferred = true;
        return;
      }
      if (token.type === "code") lastCodeRender = performance.now();

      const fragment = renderTokens([token]);
      if (block) {
        patchChildren(block.el, fragment);
        block.raw = token.raw;
      } else {
        const el = document.createElement("div");
        el.className = "md-block";
        el.append(fragment);
        root.append(el);
        fadeIn(el);
        blocks.push({ raw: token.raw, el });
      }
    });

    // O lexer pode reagrupar blocos (ex.: uma linha "===" transforma o parágrafo em título).
    while (blocks.length > tokens.length) blocks.pop().el.remove();

    if (!done) placeCaret();
    if (deferred) schedule();
    onUpdate?.();
  }

  function schedule() {
    if (!frame) frame = requestAnimationFrame(render);
  }

  return {
    get text() {
      return source;
    },
    append(text) {
      source += text;
      schedule();
    },
    /** Última renderização: fecha blocos de código, destaca tudo e some com o cursor. */
    finish() {
      if (done) return;
      done = true;
      cancelAnimationFrame(frame);
      render();
      for (const span of root.querySelectorAll(".chunk")) unwrapChunk(span);
      placeCaret();
      if (caret.isConnected) {
        const { base, easeOut } = motion();
        const fade = caret.animate([{ opacity: 1 }, { opacity: 0 }], { duration: base, easing: easeOut, fill: "forwards" });
        settled(fade).then(() => caret.remove());
      }
    },
  };
}
