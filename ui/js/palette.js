// Command palette (Ctrl+K): busca com filtro, navegação por setas, Enter executa, atalhos à direita.

import { icon } from "./icons.js";
import { animateIn, animateOut, fadeBackdrop, openLayer, trapFocus } from "./overlay.js";

const normalize = (text) =>
  text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();

/**
 * `commands`: [{ label, keywords?, iconName, shortcut?, when?(), run() }].
 * `when` esconde o comando quando não se aplica (ex.: parar geração sem geração em andamento).
 */
export function createPalette(commands) {
  const overlay = document.getElementById("palette-overlay");
  const panel = overlay.querySelector(".palette");
  const backdrop = overlay.querySelector(".overlay__backdrop");
  const input = document.getElementById("palette-input");
  const list = document.getElementById("palette-list");
  const empty = document.getElementById("palette-empty");

  let close = null;
  let visible = [];
  let active = 0;

  function setActive(index) {
    active = index;
    for (const [position, item] of [...list.children].entries()) {
      item.setAttribute("aria-selected", String(position === active));
    }
    const current = list.children[active];
    input.setAttribute("aria-activedescendant", current?.id || "");
    current?.scrollIntoView({ block: "nearest" });
  }

  function render() {
    const words = normalize(input.value).split(/\s+/).filter(Boolean);
    visible = commands.filter(
      (command) =>
        (!command.when || command.when()) &&
        words.every((word) => normalize(`${command.label} ${command.keywords || ""}`).includes(word)),
    );
    list.replaceChildren(
      ...visible.map((command, index) => {
        const item = document.createElement("div");
        item.className = "palette__item";
        item.id = `palette-option-${index}`;
        item.setAttribute("role", "option");
        const label = document.createElement("span");
        label.className = "palette__label";
        label.textContent = command.label;
        item.append(icon(command.iconName, "icon palette__icon"), label);
        if (command.shortcut) {
          const kbd = document.createElement("kbd");
          kbd.textContent = command.shortcut;
          item.append(kbd);
        }
        item.addEventListener("pointermove", () => {
          if (active !== index) setActive(index);
        });
        item.addEventListener("click", () => run(command));
        return item;
      }),
    );
    empty.hidden = visible.length > 0;
    setActive(Math.min(active, Math.max(0, visible.length - 1)));
  }

  function run(command) {
    close?.(); // fecha antes (o foco volta), depois o comando decide para onde o foco vai
    command.run();
  }

  function open() {
    if (close) return;
    input.value = "";
    active = 0;
    render();
    overlay.hidden = false;
    fadeBackdrop(backdrop, true);
    animateIn(panel, "top center");
    close = openLayer({
      element: panel,
      onClose: () => {
        close = null;
        fadeBackdrop(backdrop, false);
        animateOut(panel).then(() => {
          if (!close) overlay.hidden = true;
        });
      },
    });
    input.focus();
  }

  input.addEventListener("input", () => {
    active = 0;
    render();
  });

  input.addEventListener("keydown", (event) => {
    if (!visible.length && event.key !== "Tab") return;
    const moves = {
      ArrowDown: (active + 1) % visible.length,
      ArrowUp: (active - 1 + visible.length) % visible.length,
      Home: 0,
      End: visible.length - 1,
    };
    if (event.key in moves) {
      event.preventDefault();
      setActive(moves[event.key]);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (visible[active]) run(visible[active]);
    }
  });

  panel.addEventListener("keydown", (event) => trapFocus(panel, event));
  backdrop.addEventListener("click", () => close?.());

  return {
    open,
    toggle() {
      if (close) close();
      else open();
    },
    get isOpen() {
      return Boolean(close);
    },
  };
}
