// Seletor de modelo: popover próprio (sem <select>), lista vinda da API, check no item atual,
// navegação por setas/Home/End/Enter, Esc ou clique fora fecha. Modelos que deram 429 com "limit: 0"
// nesta execução aparecem com "sem cota nesta chave" (discreto; continuam selecionáveis).

import { call } from "./bridge.js";
import { icon } from "./icons.js";
import { animateIn, animateOut, openLayer } from "./overlay.js";
import { toast } from "./toast.js";

export function createModelPicker() {
  const trigger = document.getElementById("model-trigger");
  const label = document.getElementById("model-label");
  const popover = document.getElementById("model-popover");
  const list = document.getElementById("model-list");
  const main = document.getElementById("main");

  let current = label.textContent;
  let models = [];
  let noQuota = new Set();
  let close = null;

  const options = () => [...list.querySelectorAll(".popover__item")];

  function render() {
    list.replaceChildren(
      ...models.map((name) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "popover__item";
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", String(name === current));
        item.dataset.model = name;
        const text = document.createElement("span");
        text.className = "popover__label";
        text.textContent = name;
        if (noQuota.has(name)) {
          const tag = document.createElement("span");
          tag.className = "popover__tag";
          tag.textContent = "sem cota nesta chave";
          text.append(tag);
        }
        item.append(text, icon("check", "icon icon--sm popover__check"));
        item.addEventListener("click", () => select(name));
        return item;
      }),
    );
  }

  function place() {
    const anchor = trigger.getBoundingClientRect();
    const host = main.getBoundingClientRect();
    popover.style.left = `${anchor.left - host.left}px`;
    popover.style.top = `${anchor.bottom - host.top + 6}px`;
  }

  async function open() {
    if (close) return;
    if (!models.length) {
      list.replaceChildren(Object.assign(document.createElement("p"), { className: "popover__loading", textContent: "Carregando modelos…" }));
    } else {
      render();
    }
    place();
    popover.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    animateIn(popover, "top left");
    close = openLayer({
      element: popover,
      anchor: trigger,
      returnFocus: trigger,
      onClose: () => {
        close = null;
        trigger.setAttribute("aria-expanded", "false");
        animateOut(popover).then(() => {
          if (!close) popover.hidden = true;
        });
      },
    });

    const result = await call("get_models");
    models = result?.models?.length ? result.models : [current];
    noQuota = new Set(result?.no_quota ?? []);
    current = result?.current || current;
    if (!close) return;
    render();
    (options().find((item) => item.dataset.model === current) || options()[0])?.focus();
  }

  async function select(name) {
    close?.();
    if (name === current) return;
    const result = await call("set_model", name);
    if (!result?.ok) {
      toast(result?.error || "Não foi possível trocar o modelo.", { tone: "error" });
      return;
    }
    setCurrent(result.model);
    toast(`Modelo alterado: ${result.model}`, { iconName: "cpu" });
  }

  function setCurrent(name) {
    current = name;
    label.textContent = name;
  }

  trigger.addEventListener("click", () => (close ? close() : open()));

  popover.addEventListener("keydown", (event) => {
    const items = options();
    const index = items.indexOf(document.activeElement);
    const target = {
      ArrowDown: items[(index + 1) % items.length],
      ArrowUp: items[(index - 1 + items.length) % items.length],
      Home: items[0],
      End: items.at(-1),
    }[event.key];
    if (target) {
      event.preventDefault();
      target.focus();
    }
  });

  return {
    open,
    /** Atualiza o rótulo sem toast (ex.: fallback automático do Python). */
    setCurrent,
  };
}
