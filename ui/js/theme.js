// Tema claro/escuro e paleta de cores. Tema padrão: segue o Windows; paleta padrão: Brasa. A troca vem
// do botão do cabeçalho (tema), da palette e das Configurações (tema e paleta). As escolhas ficam no
// localStorage (o script do index.html aplica data-theme e data-palette antes da primeira pintura) e
// no .env pelo Python (set_theme, set_palette), que abre a janela e a moldura nas mesmas cores.

import { call } from "./bridge.js";

const THEME_KEY = "robin.theme";
const PALETTE_KEY = "robin.palette";
const CHOICES = ["system", "light", "dark"];
/** Paletas do tokens.css (data-palette), na ordem das Configurações. */
export const PALETTES = [
  { id: "brasa", name: "Brasa" },
  { id: "caderno", name: "Caderno" },
  { id: "ninho", name: "Ninho" },
  { id: "grafite", name: "Grafite" },
];
const PALETTE_IDS = PALETTES.map(({ id }) => id);
const systemLight = matchMedia("(prefers-color-scheme: light)");
const listeners = new Set();
let choice = readStored(THEME_KEY, CHOICES, "system");
let palette = readStored(PALETTE_KEY, PALETTE_IDS, PALETTE_IDS[0]);

function readStored(key, allowed, fallback) {
  try {
    const value = localStorage.getItem(key);
    return allowed.includes(value) ? value : fallback;
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Armazenamento indisponível: o .env (Python) ainda guarda a escolha.
  }
}

/** O tema na tela agora: "light" ou "dark". */
export const currentTheme = () => (choice === "system" ? (systemLight.matches ? "light" : "dark") : choice);

function notify() {
  for (const listener of listeners) listener({ choice, theme: currentTheme(), palette });
}

function applyTheme() {
  const theme = currentTheme();
  document.documentElement.dataset.theme = theme;
  // A moldura e o fundo da janela acompanham; com "system", o Python também segue o Windows.
  call("set_theme", choice, theme === "dark").catch(() => {});
  notify();
}

function applyPalette() {
  document.documentElement.dataset.palette = palette;
  call("set_palette", palette).catch(() => {});
  notify();
}

/** "system", "light" ou "dark" (Configurações). */
export function setThemeChoice(value) {
  if (!CHOICES.includes(value)) return;
  choice = value;
  store(THEME_KEY, value);
  applyTheme();
}

/** Botão do cabeçalho e palette: o tema oposto ao da tela, como escolha manual. */
export const toggleTheme = () => setThemeChoice(currentTheme() === "dark" ? "light" : "dark");

/** Uma das PALETTES (Configurações e palette). */
export function setPalette(value) {
  if (!PALETTE_IDS.includes(value)) return;
  palette = value;
  store(PALETTE_KEY, value);
  applyPalette();
}

/** Avisa a cada aplicação: { choice, theme, palette }. */
export function onThemeChange(listener) {
  listeners.add(listener);
}

/**
 * Liga tema e paleta. `saved`: as escolhas do .env ({ theme, palette }), que valem se o localStorage
 * tiver sido limpo ou mudado por fora.
 */
export function initTheme({ theme: savedTheme, palette: savedPalette } = {}) {
  if (CHOICES.includes(savedTheme) && savedTheme !== choice) {
    choice = savedTheme;
    store(THEME_KEY, savedTheme);
  }
  if (PALETTE_IDS.includes(savedPalette) && savedPalette !== palette) {
    palette = savedPalette;
    store(PALETTE_KEY, savedPalette);
  }
  systemLight.addEventListener("change", () => {
    if (choice === "system") applyTheme();
  });
  document.documentElement.dataset.palette = palette;
  call("set_palette", palette).catch(() => {});
  applyTheme();
}
