// Lista da aba Conversas: as conversas guardadas no PC, agrupadas por data (Hoje, Ontem, Últimos 7
// dias, Mais antigas), com a atual marcada pelo traço do R, a busca (título e texto, no Python) e
// apagar com confirmação no próprio botão (confirm.js).

import { confirmOnSecondClick } from "./confirm.js";
import { icon } from "./icons.js";

const SEARCH_DELAY_MS = 200;
const DAY_MS = 24 * 60 * 60 * 1000;

const hourFormat = new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" });
const dateFormat = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit" });

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** Grupo e hora curta de uma data ISO: hoje mostra HH:MM; o resto, DD/MM. */
function when(iso, today) {
  const date = new Date(iso);
  const days = Math.round((today - startOfDay(date)) / DAY_MS);
  const group = days <= 0 ? "Hoje" : days === 1 ? "Ontem" : days < 7 ? "Últimos 7 dias" : "Mais antigas";
  return { group, time: days <= 0 ? hourFormat.format(date) : dateFormat.format(date) };
}

export function createConversations({ onSelect, onDelete, onSearch } = {}) {
  const list = document.getElementById("conversations-list");
  const search = document.getElementById("conversations-search");
  const filter = document.getElementById("conversations-filter");
  const note = document.getElementById("conversations-note");
  let searchTimer = 0;
  let everHadItems = false;

  function row({ id, title, time, active }) {
    const wrap = el("div", "conversation-row");
    const open = el("button", `conversation-item${active ? " is-active" : ""}`);
    open.type = "button";
    open.dataset.id = id;
    if (active) open.setAttribute("aria-current", "true");
    open.append(el("span", "conversation-item__title", title));
    if (time) open.append(el("time", "conversation-item__time", time));
    open.addEventListener("click", () => onSelect?.(id));

    const remove = el("button", "icon-btn icon-btn--sm conversation-row__delete");
    remove.type = "button";
    remove.setAttribute("aria-label", `Apagar a conversa "${title}"`);
    remove.append(icon("trash", "icon icon--sm"));
    confirmOnSecondClick(remove, () => onDelete?.(id), { restore: () => icon("trash", "icon icon--sm") });
    wrap.append(open, remove);
    return wrap;
  }

  /** Desenha a lista: `items` do list_conversations ({ id, title, updated_at }) e a atual. */
  function render(items, { current = null, query = "" } = {}) {
    const today = startOfDay(new Date());
    const groups = new Map();
    for (const item of items) {
      const { group, time } = when(item.updated_at, today);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(row({ id: item.id, title: item.title, time, active: item.id === current }));
    }
    list.replaceChildren(
      ...[...groups].flatMap(([label, rows]) => [el("p", "conversations__group", label), ...rows]),
    );
    everHadItems ||= items.length > 0;
    search.hidden = !everHadItems;
    const hint = items.length ? "" : query ? "Nenhuma conversa com isso." : "Suas conversas ficam aqui, guardadas neste PC.";
    note.hidden = !hint;
    note.textContent = hint;
  }

  filter.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => onSearch?.(filter.value.trim()), SEARCH_DELAY_MS);
  });

  return {
    render,
    get query() {
      return filter.value.trim();
    },
  };
}
