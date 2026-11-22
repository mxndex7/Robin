# Fontes dos previews

Embutidas (base64) em cada `direcao-N.html`, para os previews abrirem offline com duplo clique (o Chrome bloqueia fontes de `file://` por CORS). Baixadas do registro npm (`npm pack`, pacotes do Fontsource, versão 5.3.0), só o subconjunto `latin`, que cobre o português. Licença SIL OFL 1.1 (textos ao lado).

| Direção | Fonte | Pacote | Arquivo | Tamanho |
|---|---|---|---|---|
| 1 · Brasa | Geologica (todos os eixos: peso, inclinação, nitidez, cursivo) | `@fontsource-variable/geologica` | `geologica-latin-full-normal.woff2` | 48 KB |
| 1 · Brasa | Martian Mono (peso e largura) | `@fontsource-variable/martian-mono` | `martian-mono-latin-standard-normal.woff2` | 38 KB |
| 2 · Caderno | Newsreader (peso e tamanho óptico) | `@fontsource-variable/newsreader` | `newsreader-latin-opsz-normal.woff2` | 132 KB |
| 2 · Caderno | Newsreader itálico (peso) | `@fontsource-variable/newsreader` | `newsreader-latin-wght-italic.woff2` | 64 KB |
| 2 · Caderno | IBM Plex Mono 400, 500, 600 e 400 itálico | `@fontsource/ibm-plex-mono` | `ibm-plex-mono-latin-{400,500,600}-normal.woff2`, `-400-italic.woff2` | 61 KB |
| 3 · Ninho | Atkinson Hyperlegible Next (peso) e itálico | `@fontsource-variable/atkinson-hyperlegible-next` | `atkinson-hyperlegible-next-latin-wght-{normal,italic}.woff2` | 34 + 38 KB |
| 3 · Ninho | Atkinson Hyperlegible Mono (peso) | `@fontsource-variable/atkinson-hyperlegible-mono` | `atkinson-hyperlegible-mono-latin-wght-normal.woff2` | 18 KB |

A direção escolhida foi a 1 (Brasa). As fontes dela estão em `ui/assets/fonts/` (origem no `ui/vendor/VERSIONS.md`); no app, a Martian Mono roda a 75% de largura (aqui, 87,5%) para alinhar com a Geist Mono, a reserva de símbolos.
