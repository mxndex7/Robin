// Aba Memória: tudo o que o Robin guardou (menos o núcleo, que tem a aba própria), mais recente
// primeiro. Cada item mostra o texto, a data, se está a fazer ou feito e as etiquetas; no hover ou no
// foco: riscar (o que está a fazer), editar no lugar (Ctrl+Enter salva, Esc cancela) e apagar (o
// primeiro clique vira "Apagar?"). A lista recarrega quando a aba aparece, depois de uma resposta em
// que o Robin mudou a memória e depois de puxar da nuvem (onBrainChanged).

import { call, on } from "./bridge.js";
import { confirmOnSecondClick } from "./confirm.js";
import { icon } from "./icons.js";
import { toast } from "./toast.js";

const dateFormat = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric" });

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

const plain = (text) => String(text).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const formatDate = (iso) => dateFormat.format(new Date(iso));

function iconButton(name, label) {
  const node = el("button", "icon-btn icon-btn--sm");
  node.type = "button";
  node.setAttribute("aria-label", label);
  node.append(icon(name, "icon icon--sm"));
  return node;
}

export function createMemoryList() {
  const list = document.getElementById("brain-list");
  const filter = document.getElementById("brain-filter");
  const meta = document.getElementById("brain-meta");
  const note = document.getElementById("brain-note");
  let items = [];
  let visible = false;
  let stale = true; // mudou algo enquanto a aba estava escondida
  let loading = null;
  let editing = null; // id em edição: recarregar não pode apagar o que se está digitando
  let touched = false; // a resposta em andamento mudou a memória

  function render() {
    const words = plain(filter.value).split(/\s+/).filter(Boolean);
    const shown = items.filter((item) => {
      const text = plain([item.content, ...(item.tags || [])].join(" "));
      return words.every((word) => text.includes(word));
    });
    list.replaceChildren(...shown.map(row));
    const todo = items.filter((item) => item.status === "pending").length;
    meta.textContent = items.length
      ? `${items.length} ${items.length === 1 ? "coisa guardada" : "coisas guardadas"}${todo ? ` · ${todo} a fazer` : ""}`
      : "";
    const hint = !items.length
      ? "O Robin ainda não guardou nada. É só conversar: ele anota sozinho o que vale lembrar."
      : shown.length ? "" : "Nada com isso na memória.";
    note.hidden = !hint;
    note.textContent = hint;
  }

  function row(item) {
    const state = { pending: " is-todo", done: " is-done" }[item.status] || "";
    const node = el("li", `brain-item${state}`);
    const info = el("div", "brain-item__meta");
    info.append(el("time", null, formatDate(item.created_at)));
    if (item.status === "pending") info.append(el("span", "brain-item__state", "a fazer"));
    if (item.status === "done") {
      info.append(el("span", "brain-item__state", item.done_at ? `feito em ${formatDate(item.done_at)}` : "feito"));
    }
    for (const tag of item.tags || []) info.append(el("span", null, `#${tag}`));
    if (item.sync === "pending") info.append(el("span", "brain-item__state", "só neste PC"));

    const actions = el("div", "brain-item__actions");
    if (item.status === "pending") {
      const done = iconButton("check", "Marcar como feito");
      done.addEventListener("click", () => change("complete_brain_item", item, "Marcado como feito"));
      actions.append(done);
    }
    const edit = iconButton("pencil", "Editar");
    edit.addEventListener("click", () => startEditing(node, item));
    const remove = iconButton("trash", "Apagar da memória");
    confirmOnSecondClick(remove, () => change("delete_brain_item", item, "Apagado da memória"), {
      restore: () => icon("trash", "icon icon--sm"),
    });
    actions.append(edit, remove);
    node.append(el("p", "brain-item__text", item.content), info, actions);
    return node;
  }

  function startEditing(node, item) {
    editing = item.id;
    const input = el("textarea", "brain-item__input");
    input.value = item.content;
    input.setAttribute("aria-label", "Editar o que o Robin guardou");
    const cancel = el("button", "btn btn--ghost btn--sm", "Cancelar");
    const save = el("button", "btn btn--primary btn--sm", "Salvar");
    cancel.type = "button";
    save.type = "button";
    const buttons = el("div", "brain-item__buttons");
    buttons.append(cancel, save);
    const editor = el("div", "brain-item__editor");
    editor.append(input, buttons);
    node.replaceChildren(editor);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);

    const stop = () => {
      editing = null;
      render();
    };
    cancel.addEventListener("click", stop);
    save.addEventListener("click", async () => {
      save.disabled = true;
      if (!(await change("update_brain_item", item, "Memória atualizada", input.value))) save.disabled = false;
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation(); // o Esc não chega a parar a geração
        stop();
      } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        save.click();
      }
    });
  }

  /** Chama a API; no erro, avisa (e recarrega se o item mudou ou sumiu). true se mudou. */
  async function change(method, item, message, ...args) {
    let result;
    try {
      result = await call(method, item.id, ...args);
    } catch (error) {
      result = { ok: false, error: String(error?.message || error) };
    }
    if (!result?.ok) {
      toast(result?.error || "Não foi possível mudar a memória.", { tone: "error" });
      if (result?.code === "stale" || result?.code === "not_found") {
        editing = null;
        await load();
      }
      return false;
    }
    editing = null;
    const offline = result.sync === "pending";
    toast(offline ? `${message} (só neste PC, sem conexão)` : message, { iconName: offline ? "alert" : "check" });
    await load();
    return true;
  }

  async function load() {
    loading ??= (async () => {
      try {
        const result = await call("list_brain");
        if (!result?.ok) {
          toast(result?.error || "Não foi possível ler a memória.", { tone: "error" });
          return;
        }
        items = result.items;
        stale = false;
        if (!editing) render();
      } catch (error) {
        toast(String(error?.message || error), { tone: "error" });
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  function refresh() {
    if (visible) load();
    else stale = true;
  }

  filter.addEventListener("input", render);
  on("onBrainChanged", refresh);
  on("onToolCall", ({ ok }) => {
    if (ok) touched = true;
  });
  for (const name of ["onDone", "onError"]) {
    on(name, () => {
      if (!touched) return;
      touched = false;
      refresh();
    });
  }

  return {
    /** A aba Memória apareceu ou sumiu (sidebar.js): ao aparecer, recarrega se algo mudou. */
    setVisible(value) {
      visible = value;
      if (visible && stale) load();
    },
  };
}
