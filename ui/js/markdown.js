// Markdown: marked (GFM) → DOMPurify → acabamento (blocos de código com cabeçalho, tabelas roláveis).
// O destaque de sintaxe usa highlight.js com um tema monocromático próprio (components.css).

import DOMPurify from "../vendor/dompurify/purify.es.js";
import hljs from "../vendor/highlight/core.min.js";
import bash from "../vendor/highlight/languages/bash.min.js";
import css from "../vendor/highlight/languages/css.min.js";
import diff from "../vendor/highlight/languages/diff.min.js";
import dockerfile from "../vendor/highlight/languages/dockerfile.min.js";
import go from "../vendor/highlight/languages/go.min.js";
import ini from "../vendor/highlight/languages/ini.min.js";
import javascript from "../vendor/highlight/languages/javascript.min.js";
import json from "../vendor/highlight/languages/json.min.js";
import markdown from "../vendor/highlight/languages/markdown.min.js";
import plaintext from "../vendor/highlight/languages/plaintext.min.js";
import powershell from "../vendor/highlight/languages/powershell.min.js";
import python from "../vendor/highlight/languages/python.min.js";
import rust from "../vendor/highlight/languages/rust.min.js";
import shell from "../vendor/highlight/languages/shell.min.js";
import sql from "../vendor/highlight/languages/sql.min.js";
import typescript from "../vendor/highlight/languages/typescript.min.js";
import xml from "../vendor/highlight/languages/xml.min.js";
import yaml from "../vendor/highlight/languages/yaml.min.js";
import { Marked } from "../vendor/marked/marked.esm.js";
import { icon } from "./icons.js";

const LANGUAGES = {
  bash, css, diff, dockerfile, go, ini, javascript, json, markdown, plaintext,
  powershell, python, rust, shell, sql, typescript, xml, yaml,
};
for (const [name, definition] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, definition);

const escapeHtml = (text) =>
  text.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

const md = new Marked({
  gfm: true,
  breaks: false,
  renderer: {
    // Bloco de código cru e sanitizável; o cabeçalho (linguagem + copiar) entra depois do DOMPurify.
    code({ text, lang }) {
      const language = (lang || "").trim().split(/\s+/)[0].toLowerCase();
      const known = Boolean(language && hljs.getLanguage(language));
      const body = known ? hljs.highlight(text, { language, ignoreIllegals: true }).value : escapeHtml(text);
      return `<pre data-lang="${escapeHtml(language)}"><code class="hljs">${body}</code></pre>\n`;
    },
  },
});

function codeBlock(pre) {
  const block = document.createElement("div");
  block.className = "code-block";

  const header = document.createElement("div");
  header.className = "code-block__header";
  const label = document.createElement("span");
  label.className = "code-block__lang";
  label.textContent = pre.dataset.lang || "texto";

  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "copy-btn code-block__copy";
  copy.setAttribute("aria-label", "Copiar código");
  copy.append(icon("copy", "icon icon--sm copy-btn__copy"), icon("check", "icon icon--sm copy-btn__check"));

  header.append(label, copy);
  pre.replaceWith(block);
  block.append(header, pre);
}

function finish(fragment) {
  for (const pre of fragment.querySelectorAll("pre[data-lang]")) codeBlock(pre);
  for (const table of fragment.querySelectorAll("table")) {
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    table.replaceWith(wrap);
    wrap.append(table);
  }
  for (const link of fragment.querySelectorAll("a[href]")) {
    link.setAttribute("rel", "noopener noreferrer");
    link.title = link.getAttribute("href");
  }
  return fragment;
}

/** Tokens de bloco do markdown (o renderizador de streaming trabalha bloco a bloco). */
export function lex(source) {
  return md.lexer(source);
}

/** Renderiza tokens em um DocumentFragment já sanitizado e com o acabamento aplicado. */
export function renderTokens(tokens) {
  const html = md.parser(tokens);
  return finish(DOMPurify.sanitize(html, { RETURN_DOM_FRAGMENT: true }));
}

/** Renderiza um markdown completo (aba Visualizar da memória). */
export function renderMarkdown(source) {
  return renderTokens(lex(source));
}
