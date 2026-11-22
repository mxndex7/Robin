// API falsa para testar a interface no navegador, sem o Python.
//
// Toda resposta passa por "Pensando" → "Raciocinando" (onThinking) antes do texto; a partir de 1s o
// indicador mostra os segundos ("Pensando · 3s").
//
// Parâmetros de URL:
//   ?key             simula API Key configurada (sem ela, enviar devolve no_key)
//   ?frame=native    simula a moldura nativa
//   ?error=start     falha antes do primeiro pedaço (card com "Detalhes")
//   ?error=mid       falha no meio do stream
//   ?fallback        primeiro envio: modelo indisponível → gemini-3.6-flash (gravado)
//   ?fallback=temp   primeiro envio: nova tentativa e troca temporária (5 min) para o gemini-3.6-flash;
//                    o segundo envio volta ao modelo escolhido (reason "restored")
//   ?fallback=daily  primeiro envio: cota diária acabou → reserva até ela voltar (sem nova tentativa)
//   ?retry           primeiro envio: 503 depois de só raciocínio → "Tentando de novo" → responde
//   ?wait            primeiro envio: o limitador local espera a vaga do minuto (onWaiting, 4s)
//   ?slow            raciocínio longo (8s) antes de cada resposta, para ver o contador
//   ?memerror        as duas primeiras leituras da memória falham (carga inicial e nova tentativa)
//   ?fast            stream rápido (sem pausas)
//   ?git             a pasta "é" um repositório (permite ligar o sync Git)
//   ?hotkeybusy      o atalho global Ctrl+Alt+R "está em uso" (aviso nas Configurações; salvar Ctrl+Alt+B também falha)
//   ?exe             simula o Robin.exe (iniciar com o Windows disponível)
//   ?noquota         o seletor mostra o gemini-3.8-flash como "sem cota nesta chave" (429 com limit: 0)
//   ?sim             mostra o selo de modo simulação
//   ?cloud           Supabase já configurado (a lateral mostra "núcleo • nuvem")
//   ?cloud=offline   configurado e sem conexão, com 2 alterações na fila
//   ?cloud=paused    configurado e com o banco pausado (só leitura)
// Em Configurações, uma chave que não começa com sb_secret_ é recusada; "pausado" na URL simula o
// banco pausado. Com a nuvem, as mensagens "sem internet", "pausar banco" e "reativar banco" mudam o
// estado (onCloudStatus), como a nuvem simulada do Python.
//
// Cérebro (linhas de ação via onToolCall, guardado só nesta aba): "lembre de X" (a fazer) ou
// "lembre que X", "o que pedi pra você anotar" (tudo), "o que tenho pra fazer", "comprei X",
// "fruta roxa" (acha uva, ameixa...) e "quais tarefas cumpri". Como no app, a memória inteira já
// está no prompt: perguntas não geram linha de ação, e a resposta vem junto com a ação.
//
// Conversas guardadas só nesta aba, como o conversations.py (abrir, nova, apagar, buscar).
//   ?history         começa com 3 conversas e algumas coisas na memória (aba Memória)

const SAMPLE_MEMORY = `# Memória de exemplo (mock)

## 1. Quem sou
- **Nome**: Alex
- **Foco**: arquitetura, depuração e automação Git/GitHub.

## 2. Regras críticas
- Validação explícita antes de qualquer commit, push, reset ou delete.
- Apontar a causa raiz antes de propor correções.

## 3. Notas
- Esta é uma memória de teste servida pelo mock.js; o app real lê o memory.md.
`;

const FENCE = "```";

const RESPONSES = [
  `## Causa raiz

O erro vem de \`items\` estar **indefinido** quando o componente renderiza antes do \`fetch\` terminar: o \`map\` roda sobre \`undefined\`.

### Correção

1. Inicialize o estado com um array vazio.
2. Trate o carregamento de forma explícita.
3. Valide a resposta da API antes de gravar no estado.

${FENCE}ts
const [items, setItems] = useState<Item[]>([]);

useEffect(() => {
  // Garante um array mesmo se a API devolver null
  fetchItems()
    .then((data) => setItems(Array.isArray(data) ? data : []))
    .catch(() => setItems([]));
}, []);
${FENCE}

| Opção | Risco | Esforço |
|---|---|---|
| Estado inicial \`[]\` | Baixo | Mínimo |
| Optional chaining \`items?.map\` | Esconde o problema | Mínimo |
| Validação com schema (zod) | Baixo | Médio |

> Antes de aplicar, confirme se \`fetchItems\` pode retornar \`null\` em algum cenário. Nenhum arquivo foi alterado.

Referência: [Synchronizing with Effects](https://react.dev/learn/synchronizing-with-effects).`,

  `Duas regras resolvem a maior parte:

- **Um commit por responsabilidade**: separe refatoração de mudança de comportamento.
- **Mensagem no imperativo**: \`fix: trata lista vazia no carregamento\`.

${FENCE}python
def plan_commits(changes: list[str]) -> list[list[str]]:
    """Agrupa as mudanças pelo diretório raiz."""
    groups: dict[str, list[str]] = {}
    for path in changes:
        groups.setdefault(path.split("/")[0], []).append(path)
    return list(groups.values())
${FENCE}

${FENCE}diff
- const total = items.map(i => i.price).reduce((a, b) => a + b);
+ const total = items.reduce((sum, item) => sum + item.price, 0);
${FENCE}

Confirme antes de eu sugerir a sequência de \`git add -p\`.`,
];

const PURPLE_FRUITS = ["uva", "ameixa", "jabuticaba", "acai", "amora"];

const plain = (text) => text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
const STATE = { pending: " (a fazer)", done: " (feito)" };
const bullets = (items) => items.map((item) => `- ${item.content}${STATE[item.status] || ""}`).join("\n");

const daysAgo = (days, hours = 0) => new Date(Date.now() - (days * 24 + hours) * 3600 * 1000).toISOString();

// ?history: o que o Robin já teria guardado e conversado.
function sampleBrain() {
  return [
    { id: "b1", content: "comprar uva", tags: ["compras"], status: "done", created_at: daysAgo(3), done_at: daysAgo(1) },
    { id: "b2", content: "Ideia: mudar a foto do LinkedIn", tags: ["ideia", "linkedin"], status: null, created_at: daysAgo(1, 2) },
    { id: "b3", content: "Comprar pão em 02/11/2026", tags: ["compras"], status: "pending", created_at: daysAgo(0, 3) },
  ];
}

function sampleConversations() {
  const conversation = (id, title, days, turns) => ({
    id,
    title,
    created_at: daysAgo(days, 1),
    updated_at: daysAgo(days),
    messages: turns.flatMap(([user, robin, actions], index) => [
      { id: `${id}-u${index}`, role: "user", text: user, created_at: daysAgo(days, 1) },
      { id: `${id}-r${index}`, role: "robin", text: robin, created_at: daysAgo(days, 1), ...(actions ? { actions } : {}) },
    ]),
  });
  return [
    conversation("c-hoje", "lembre de comprar pão amanhã", 0, [
      ["lembre de comprar pão amanhã", "Anotado: amanhã eu te lembro do pão.", [{ tool: "salvar_memoria", label: "Salvou tarefa: Comprar pão em 02/11/2026", ok: true }]],
    ]),
    conversation("c-ontem", "tive uma ideia: mudar a foto do LinkedIn", 1, [
      ["tive uma ideia: mudar a foto do LinkedIn", "Ideia anotada. Quer que eu te lembre no fim de semana?", [{ tool: "salvar_memoria", label: "Salvou: Ideia: mudar a foto do LinkedIn", ok: true }]],
      ["pode ser", "Fechado."],
    ]),
    conversation("c-antiga", "revise este diff do login", 10, [
      ["revise este diff do login", "O `token` é lido antes de checar se a sessão existe: em sessão expirada, isso estoura. Confira antes de usar."],
    ]),
  ];
}

/** Cérebro simulado: reply(prompt) devolve { tools: [{ tool, label }], text } para os gatilhos, ou
 *  null; items é o que está guardado (a aba Memória lê daqui). */
function createBrain(items) {
  let nextId = 1;
  const pending = () => items.filter((item) => item.status === "pending");

  function reply(prompt) {
    const key = plain(prompt);
    // Perguntas antes de "lembre": "o que pedi pra você lembrar" também tem a palavra.
    if (/o que (eu )?(te )?pedi (pra|para) (voce )?(anotar|lembrar)|o que voce sabe de mim/.test(key)) {
      return {
        tools: [], // a memória inteira já está no prompt: nenhuma ação
        text: items.length ? `Até agora:\n\n${bullets(items)}` : "Por enquanto você não me pediu pra lembrar de nada.",
      };
    }
    const remember = key.match(/\blembr(?:e|a|ar)\b\s*(de|que)?\s*(.+)/);
    if (remember) {
      const content = remember[2].replace(/[.!?]+$/, "");
      const isTask = remember[1] !== "que";
      items.push({ id: `n${nextId++}`, content, tags: [], status: isTask ? "pending" : null, created_at: new Date().toISOString() });
      return {
        tools: [{ tool: "salvar_memoria", label: `${isTask ? "Salvou tarefa" : "Salvou"}: ${content}` }],
        text: isTask ? `Anotado: vou te lembrar de **${content}**.` : `Guardado: **${content}**.`,
      };
    }
    const done = key.match(/\bcomprei\s+(?:a |o |as |os )?(.+)/);
    if (done) {
      const word = done[1].replace(/[.!?]+$/, "").replace(/s$/, "");
      const task = pending().find((item) => plain(item.content).includes(word));
      if (!task) return { tools: [], text: `Não achei nada a fazer sobre "${word}".` };
      task.status = "done";
      task.done_at = new Date().toISOString();
      return { tools: [{ tool: "concluir_tarefa", label: `Concluiu: ${task.content}` }], text: `Boa, risquei **${task.content}**.` };
    }
    if (key.includes("o que tenho pra fazer") || key.includes("o que tenho para fazer")) {
      const tasks = pending();
      return { tools: [], text: tasks.length ? `Falta fazer:\n\n${bullets(tasks)}` : "Nada pendente. Aproveita." };
    }
    if (key.includes("quais tarefas cumpri")) {
      const tasks = items.filter((item) => item.status === "done");
      return { tools: [], text: tasks.length ? `Você já fez:\n\n${bullets(tasks)}` : "Nada riscado ainda." };
    }
    if (key.includes("fruta roxa")) {
      const found = items.filter((item) => PURPLE_FRUITS.some((fruit) => plain(item.content).includes(fruit)));
      return { tools: [], text: found.length ? `Tá na memória:\n\n${bullets(found)}` : "Fruta roxa? Não tenho nada sobre isso." };
    }
    return null;
  }

  return { reply, items };
}

export function createMockApi(emit) {
  const params = new URLSearchParams(location.search);
  const fast = params.has("fast");
  let maximized = false;
  let model = "gemini-3.5-flash-lite";
  let memory = SAMPLE_MEMORY;
  let memoryReads = 0;
  let genCounter = 0;
  let turn = 0;
  let active = null;
  let fallbackUsed = false;
  let retryUsed = false;
  let waitUsed = false;
  let restoreFrom = null; // modelo escolhido durante a troca temporária (?fallback=temp)
  let hasKey = params.has("key");
  let gitSync = false;
  let hotkey = "Ctrl+Alt+R";
  let hotkeyError = params.has("hotkeybusy") ? "Ctrl+Alt+R já está em uso por outro programa." : "";
  let autostart = params.has("exe");
  // Tema e paleta guardados como o .env do Python guardaria (aqui, o próprio localStorage do theme.js).
  const stored = (key, fallback) => {
    try {
      return localStorage.getItem(key) || fallback;
    } catch {
      return fallback;
    }
  };
  let theme = stored("robin.theme", "system");
  let palette = stored("robin.palette", "brasa");
  const brain = createBrain(params.has("history") ? sampleBrain() : []);
  const brainReply = brain.reply;

  // Conversas guardadas, como o conversations.py: a mensagem do usuário ao enviar e a resposta no
  // fim (com as ações, parada ou erro); regenerar troca a última resposta.
  const conversations = params.has("history") ? sampleConversations() : [];
  let currentConversation = null;
  const recording = new Map(); // gen_id -> { text, actions, regenerate }
  const nowIso = () => new Date().toISOString();
  const conversationsChanged = () => emit("onConversationsChanged", {});

  function beginRecord(genId, text) {
    if (!currentConversation) {
      const title = text.replace(/\s+/g, " ").trim();
      currentConversation = { id: crypto.randomUUID(), title: title.length > 60 ? `${title.slice(0, 59)}…` : title, created_at: nowIso(), messages: [] };
      conversations.push(currentConversation);
    }
    currentConversation.messages.push({ id: crypto.randomUUID(), role: "user", text, created_at: nowIso() });
    currentConversation.updated_at = nowIso();
    recording.set(genId, { text: "", actions: [], regenerate: false });
    conversationsChanged();
  }

  function record(event, payload) {
    const pending = recording.get(payload.gen_id);
    if (!pending) return;
    if (event === "onChunk") pending.text += payload.text;
    if (event === "onToolCall") pending.actions.push({ tool: payload.tool, label: payload.label, ok: payload.ok });
    if (event !== "onDone" && event !== "onError") return;
    recording.delete(payload.gen_id);
    if (!currentConversation) return;
    const messages = currentConversation.messages;
    if (pending.regenerate) messages.splice(messages.findLastIndex((message) => message.role === "user") + 1);
    const message = { id: crypto.randomUUID(), role: "robin", text: pending.text, created_at: nowIso() };
    if (pending.actions.length) message.actions = pending.actions;
    if (event === "onDone" && payload.stopped) message.status = "stopped";
    if (event === "onError") Object.assign(message, { status: "error", error: { code: payload.code, message: payload.message } });
    messages.push(message);
    currentConversation.updated_at = nowIso();
    conversationsChanged();
  }

  // Eventos da geração passam pelo registro da conversa antes de ir para a interface.
  const emitGen = (event, payload) => {
    record(event, payload);
    emit(event, payload);
  };

  function dropActive() {
    if (active) clearTimeout(active.timer);
    active = null;
    recording.clear();
  }
  const cloudParam = params.get("cloud");
  let cloudUrl = params.has("cloud") ? "https://exemplo.supabase.co" : "";
  let cloud = params.has("cloud")
    ? {
        state: { offline: "offline", paused: "paused" }[cloudParam] || "ok",
        message: { offline: "Sem conexão com o Supabase.", paused: "Banco pausado: reative no painel do Supabase. Até lá, a memória fica só para leitura." }[cloudParam] || "",
        pending: cloudParam === "offline" ? 2 : 0,
        url: "https://exemplo.supabase.co",
      }
    : null;
  let core = SAMPLE_MEMORY;

  const settings = () => ({
    git_sync: gitSync && !cloud,
    git_available: params.has("git"),
    key_hint: hasKey ? "••••abcd" : "",
    data_dir: String.raw`C:\Users\voce\AppData\Roaming\Robin`,
    hotkey,
    hotkey_error: hotkeyError,
    autostart,
    autostart_available: params.has("exe"),
    build: { label: params.has("exe") ? "Robin.exe de 27/10 13:20" : "código-fonte, commit a721f65, arquivos de 31/10 15:04" },
    supabase_url: cloudUrl,
    supabase_key_hint: cloud ? "••••7f3a" : "",
    cloud,
    theme,
    palette,
  });

  function setCloud(changes) {
    cloud = { ...cloud, ...changes };
    emit("onCloudStatus", cloud);
  }

  // Gatilhos da nuvem pela mensagem (como o cloud_mock.py).
  function cloudTriggers(prompt) {
    if (!cloud) return;
    const key = plain(prompt);
    if (key.includes("sem internet")) setCloud({ state: "offline", message: "Sem conexão com o Supabase." });
    if (key.includes("pausar banco")) setCloud({ state: "paused", message: "Banco pausado: reative no painel do Supabase. Até lá, a memória fica só para leitura." });
    if (key.includes("reativar banco")) setCloud({ state: "ok", message: "", pending: 0 });
  }

  function stream(genId, brain) {
    const text = brain?.text ?? RESPONSES[turn++ % RESPONSES.length];
    const errorMode = params.get("error");
    let index = 0;

    emit("onStart", { gen_id: genId, model });

    const tick = () => {
      if (errorMode === "start" || (errorMode === "mid" && index > text.length / 3)) {
        active = null;
        emitGen("onError", {
          gen_id: genId,
          code: "server",
          message: "O serviço do Gemini falhou. Tente de novo.",
          detail: "ServerError 503 UNAVAILABLE: This model is currently experiencing high demand. (mock)",
          partial: index > 0,
        });
        return;
      }
      const size = 2 + Math.floor(Math.random() * 22);
      emitGen("onChunk", { gen_id: genId, text: text.slice(index, index + size) });
      index += size;
      if (index >= text.length) {
        active = null;
        emitGen("onDone", { gen_id: genId, stopped: false });
        return;
      }
      active.timer = setTimeout(tick, fast ? 5 : 25 + Math.random() * 45);
    };

    // Antes do texto, em sequência: trocas de modelo (?fallback), raciocínio e o primeiro pedaço.
    const steps = [];
    if (params.has("retry") && !retryUsed) {
      // Como o Python: 503 depois de só raciocínio vira onRetry, e a nova tentativa raciocina de novo.
      retryUsed = true;
      steps.push([fast ? 0 : 450, () => emit("onThinking", { gen_id: genId })]);
      steps.push([fast ? 0 : 1000, () => emit("onRetry", { gen_id: genId, code: "server" })]);
    }
    if (params.has("wait") && !waitUsed) {
      waitUsed = true;
      steps.push([0, () => emit("onWaiting", { gen_id: genId, seconds: 4 })]);
      steps.push([fast ? 0 : 4000, () => {}]);
    }
    if (params.has("fallback") && fallbackUsed && restoreFrom) {
      // A troca temporária "expirou": o envio seguinte volta ao modelo escolhido.
      const previous = model;
      model = restoreFrom;
      restoreFrom = null;
      emit("onModelChanged", { model, previous, reason: "restored" });
    }
    if (params.has("fallback") && !fallbackUsed) {
      fallbackUsed = true;
      const kind = params.get("fallback");
      const temporary = kind === "temp" || kind === "daily";
      if (kind === "temp") steps.push([fast ? 0 : 900, () => emit("onRetry", { gen_id: genId, code: "server" })]);
      steps.push([fast ? 0 : 1200, () => {
        const change = { model: "gemini-3.6-flash", previous: model, reason: temporary ? "temporary" : "fallback" };
        if (temporary) Object.assign(change, kind === "daily" ? { cause: "daily_quota", expires_in: 47699 } : { cause: "transient", expires_in: 300 });
        emit("onModelChanged", change);
        if (kind === "temp") restoreFrom = model;
        model = "gemini-3.6-flash";
      }]);
    }
    if (params.has("slow")) steps.push([fast ? 0 : 8000, () => {}]);
    steps.push([fast ? 0 : 450, () => emit("onThinking", { gen_id: genId })]);
    for (const call of brain?.tools ?? []) {
      steps.push([fast ? 0 : 500, () => emitGen("onToolCall", { gen_id: genId, ok: true, ...call })]);
    }
    steps.push([fast ? 50 : 900, tick]);

    const next = () => {
      const [delay, run] = steps.shift();
      active.timer = setTimeout(() => {
        run();
        if (active?.genId === genId && steps.length) next();
      }, delay);
    };
    active = { genId, timer: 0 };
    next();
  }

  // regenerate: o cérebro já gravou na primeira vez; a resposta repete só o texto e as linhas.
  let lastBrain = null;

  function startGeneration(prompt) {
    if (!hasKey) return { ok: false, code: "no_key", error: "Configure a API Key do Gemini para começar." };
    if (active) return { ok: false, code: "busy", error: "Aguarde a resposta atual ou pare a geração." };
    const genId = ++genCounter;
    if (prompt === undefined) recording.set(genId, { text: "", actions: [], regenerate: true });
    else beginRecord(genId, prompt);
    if (prompt !== undefined) {
      cloudTriggers(prompt);
      lastBrain = brainReply(prompt);
      if (lastBrain && cloud?.state === "offline" && lastBrain.tools.some(({ tool }) => tool !== "buscar_memoria")) {
        // Alteração sem conexão: a linha diz que ficou na fila e o 2º pedido explica.
        lastBrain.tools = lastBrain.tools.map((call) =>
          call.tool === "buscar_memoria" ? call : { ...call, label: `${call.label} (na fila: sem conexão)` },
        );
        lastBrain.text += "\n\nSó que ficou só neste PC por enquanto (sem conexão com a nuvem): sobe sozinho quando a conexão voltar.";
        setCloud({ pending: cloud.pending + 1 });
      }
    }
    // Emite onStart antes de responder, como pode acontecer no pywebview (exercita o buffer do chat.js).
    stream(genId, lastBrain);
    return { ok: true, gen_id: genId };
  }

  return {
    async get_state() {
      return {
        app: "Robin",
        version: "mock",
        model,
        has_key: hasKey,
        mock: params.has("sim"),
        frame: params.get("frame") === "native" ? "native" : "custom",
        maximized,
        debug: true,
        cloud,
      };
    },
    async send_message(text) {
      if (!text.trim()) return { ok: false, code: "empty", error: "Mensagem vazia." };
      if (!hasKey) return startGeneration();
      return startGeneration(text);
    },
    async regenerate() {
      return startGeneration();
    },
    async stop_generation() {
      if (!active) return { ok: false };
      clearTimeout(active.timer);
      const genId = active.genId;
      active = null;
      emitGen("onDone", { gen_id: genId, stopped: true });
      return { ok: true };
    },
    async list_conversations(query = "") {
      const words = plain(query).split(/\s+/).filter(Boolean);
      const items = conversations
        .filter((conversation) => {
          const text = plain([conversation.title, ...conversation.messages.map((message) => message.text)].join(" "));
          return words.every((word) => text.includes(word));
        })
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .map(({ id, title, created_at, updated_at, messages }) => ({ id, title, created_at, updated_at, count: messages.length }));
      return { ok: true, current: currentConversation?.id ?? null, items };
    },
    async new_conversation() {
      dropActive();
      currentConversation = null;
      conversationsChanged();
      return { ok: true };
    },
    async open_conversation(id) {
      const found = conversations.find((conversation) => conversation.id === id);
      if (!found) return { ok: false, code: "not_found", error: "Essa conversa não existe mais." };
      dropActive();
      currentConversation = found;
      conversationsChanged();
      return { ok: true, conversation: structuredClone(found) };
    },
    async delete_conversation(id) {
      const index = conversations.findIndex((conversation) => conversation.id === id);
      if (index < 0) return { ok: false, code: "not_found", error: "Essa conversa não existe mais." };
      conversations.splice(index, 1);
      const wasCurrent = currentConversation?.id === id;
      if (wasCurrent) {
        dropActive();
        currentConversation = null;
      }
      conversationsChanged();
      return { ok: true, was_current: wasCurrent };
    },
    async list_brain() {
      const items = [...brain.items].sort((a, b) => b.created_at.localeCompare(a.created_at));
      return { ok: true, items: items.map((item) => ({ ...item, sync: null })) };
    },
    async update_brain_item(id, content) {
      const item = brain.items.find((entry) => entry.id === id);
      if (!item) return { ok: false, code: "not_found", error: "Isso não está mais na memória." };
      if (!content.trim()) return { ok: false, code: "empty", error: "O texto não pode ficar vazio." };
      item.content = content.trim();
      return { ok: true, item, sync: null };
    },
    async complete_brain_item(id) {
      const item = brain.items.find((entry) => entry.id === id);
      if (!item) return { ok: false, code: "not_found", error: "Isso não está mais na memória." };
      Object.assign(item, { status: "done", done_at: nowIso() });
      return { ok: true, item, sync: null };
    },
    async delete_brain_item(id) {
      const index = brain.items.findIndex((entry) => entry.id === id);
      if (index < 0) return { ok: false, code: "not_found", error: "Isso não está mais na memória." };
      const [item] = brain.items.splice(index, 1);
      return { ok: true, item, sync: null };
    },
    async get_models() {
      return {
        models: ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"],
        current: model,
        no_quota: params.has("noquota") ? ["gemini-3.8-flash"] : [],
      };
    },
    async set_model(name) {
      model = name;
      return { ok: true, model };
    },
    async get_memory() {
      if (params.has("memerror") && memoryReads++ < 2) {
        return { ok: false, code: "io", error: "Não foi possível ler o memory.md: [Errno 13] Permission denied (mock)" };
      }
      return cloud ? { ok: true, content: core, source: "cloud" } : { ok: true, content: memory, source: "local" };
    },
    async save_memory(content, confirmEmpty = false) {
      if (cloud) {
        if (cloud.state === "paused") return { ok: false, code: "cloud", error: cloud.message };
        if (!content.trim() && core.trim() && !confirmEmpty) {
          return { ok: false, code: "confirm_empty", error: "O núcleo ficaria vazio. Confirme para apagar o conteúdo." };
        }
        core = content;
        if (cloud.state === "offline") {
          setCloud({ pending: cloud.pending + 1 });
          return { ok: true, sync: "pending" };
        }
        return { ok: true, sync: "cloud" };
      }
      if (!content.trim() && memory.trim() && !confirmEmpty) {
        return { ok: false, code: "confirm_empty", error: "A memória ficaria vazia. Confirme para apagar o conteúdo do memory.md." };
      }
      memory = content;
      if (!gitSync) return { ok: true, sync: "local" };
      setTimeout(() => emit("onMemoryStatus", { status: "git_syncing" }), 50);
      setTimeout(() => emit("onMemoryStatus", { status: "git_ok" }), 900);
      return { ok: true, sync: "git" };
    },
    async get_settings() {
      return settings();
    },
    async set_settings(values) {
      if (values.git_sync && !params.has("git")) {
        return { ok: false, code: "no_git", error: "A pasta de dados não é um repositório Git." };
      }
      if ("git_sync" in values) gitSync = Boolean(values.git_sync);
      if ("autostart" in values) {
        if (!params.has("exe")) return { ok: false, code: "no_autostart", error: "Iniciar com o Windows só vale para o Robin.exe." };
        autostart = Boolean(values.autostart);
      }
      return { ok: true, ...settings() };
    },
    async set_supabase(url, key) {
      await new Promise((resolve) => setTimeout(resolve, 600));
      if (!/^https:\/\//.test(url)) return { ok: false, code: "invalid_url", error: "Use a URL do projeto: https://<projeto>.supabase.co" };
      if (!key && !cloud) return { ok: false, code: "empty", error: "Informe a chave secreta do Supabase." };
      if (key && !key.startsWith("sb_secret_")) {
        return { ok: false, code: "auth", error: "Chave do Supabase recusada: use a chave secreta (sb_secret_...) do projeto." };
      }
      if (url.includes("pausado")) {
        return { ok: false, code: "paused", error: "Banco pausado: reative no painel do Supabase. Até lá, a memória fica só para leitura." };
      }
      cloudUrl = url.replace(/\/+$/, "");
      cloud = { state: "ok", message: "", pending: 0, url: cloudUrl };
      setTimeout(() => emit("onCloudStatus", cloud), 300);
      return { ok: true, ...settings() };
    },
    async disconnect_supabase() {
      cloud = null;
      cloudUrl = "";
      emit("onMemoryChanged", {});
      return { ok: true, ...settings() };
    },
    async sync_now() {
      await new Promise((resolve) => setTimeout(resolve, 400));
      if (!cloud) return { ok: false, error: "O Supabase não está configurado." };
      if (cloud.state === "offline") return { ok: false, cloud, error: "Sem conexão com o Supabase." };
      if (cloud.state === "paused") return { ok: false, cloud, error: cloud.message };
      cloud = { ...cloud, pending: 0 };
      return { ok: true, cloud };
    },
    async set_theme(value) {
      if (!["system", "light", "dark"].includes(value)) return { ok: false, code: "invalid_theme", error: "Tema inválido." };
      theme = value;
      return { ok: true, theme };
    },
    async set_palette(value) {
      if (!["brasa", "caderno", "ninho", "grafite"].includes(value)) return { ok: false, code: "invalid_palette", error: "Paleta inválida." };
      palette = value;
      return { ok: true, palette };
    },
    async set_hotkey(text) {
      if (!/(^|\+)(Ctrl|Alt|Win)\+/.test(text)) {
        return { ok: false, code: "invalid_hotkey", error: "Use Ctrl, Alt ou Win na combinação." };
      }
      if (text === "Ctrl+Alt+B") return { ok: false, code: "hotkey_busy", error: "Ctrl+Alt+B já está em uso por outro programa." };
      hotkey = text;
      hotkeyError = "";
      return { ok: true, hotkey };
    },
    async set_api_key(key) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      if (key.length < 8) return { ok: false, code: "invalid_key", error: "API Key inválida. Confira a chave nas configurações." };
      hasKey = true;
      return { ok: true, key_hint: `••••${key.slice(-4)}` };
    },
    async window_minimize() {},
    async window_maximize() {
      maximized = !maximized;
      emit("onWindowState", { maximized });
      return { maximized };
    },
    async window_close() {},
    async window_start_drag() {},
    async window_start_resize() {},
    async open_data_dir() {
      return { ok: true };
    },
    async copy_text() {
      return { ok: true };
    },
    async log_debug(message) {
      console.log(`[ui] ${message}`);
    },
    async open_external(url) {
      window.open(url, "_blank", "noopener");
      return { ok: true };
    },
  };
}
