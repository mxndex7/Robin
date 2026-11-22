# Bibliotecas locais

Baixadas uma vez do registro npm (`npm pack`) e copiadas sem alteração. Nada é carregado de CDN.

| Pacote | Versão | Arquivos | Licença |
|---|---|---|---|
| marked | 18.1.1 | `marked/marked.esm.js` | MIT (`marked/LICENSE`) |
| dompurify | 3.4.17 | `dompurify/purify.es.js` (de `dist/purify.es.mjs`) | Apache-2.0 ou MPL-2.0 (`dompurify/LICENSE*`) |
| @highlightjs/cdn-assets | 11.12.0 | `highlight/core.min.js`, `highlight/languages/*.min.js` | BSD-3-Clause (`highlight/LICENSE`) |
| @fontsource-variable/geologica | 5.3.0 | `../assets/fonts/Geologica-Variable.woff2` (de `geologica-latin-full-normal.woff2`: todos os eixos, subconjunto latino) | SIL OFL 1.1 (`../assets/fonts/LICENSE-Geologica-OFL.txt`) |
| @fontsource-variable/martian-mono | 5.3.0 | `../assets/fonts/MartianMono-Variable.woff2` (de `martian-mono-latin-standard-normal.woff2`: peso e largura, subconjunto latino) | SIL OFL 1.1 (`../assets/fonts/LICENSE-MartianMono-OFL.txt`) |
| geist | 1.8.2 | `../assets/fonts/GeistMono-Variable.woff2` (só reserva dos símbolos que a Martian não tem: ─ ├ → ≥) | SIL OFL 1.1 (`../assets/fonts/LICENSE-Geist-OFL.txt`) |

O `purify.es.mjs` foi renomeado para `.js` porque o servidor local do pywebview decide o MIME pela extensão.
O subconjunto latino das fontes cobre o português; o que faltar cai na reserva (Segoe UI na interface, Geist Mono no código).
