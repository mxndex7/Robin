// Ícones do sprite em index.html (<symbol id="i-...">).

const SVG_NS = "http://www.w3.org/2000/svg";

/** Cria <svg class="icon"><use href="#i-nome" /></svg>. */
export function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
