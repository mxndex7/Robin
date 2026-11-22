// Ponte JS ↔ Python.
// No pywebview, as chamadas vão para window.pywebview.api; num navegador comum
// (ou com ?mock na URL), usa o mock.js para testar a interface sem o Python.

const handlers = new Map();

/** Registra um ouvinte de evento vindo do Python (onChunk, onDone, ...). Devolve a função para remover. */
export function on(event, handler) {
  if (!handlers.has(event)) handlers.set(event, new Set());
  handlers.get(event).add(handler);
  return () => handlers.get(event)?.delete(handler);
}

/** Despacha um evento para os ouvintes. O mock usa esta mesma função. */
export function emit(event, payload) {
  for (const handler of handlers.get(event) ?? []) {
    try {
      handler(payload);
    } catch (error) {
      console.error(`[robin] erro no ouvinte de ${event}:`, error);
    }
  }
}

// O Python chama window.robin.<evento>(payload): qualquer nome vira um evento.
window.robin = new Proxy({}, { get: (_, name) => (payload) => emit(String(name), payload) });

const MOCK_WAIT_MS = 1500;
let apiPromise = null;

function loadMock(resolve) {
  import("./mock.js").then(({ createMockApi }) => resolve({ api: createMockApi(emit), mock: true }));
}

function resolveApi() {
  return new Promise((resolve) => {
    if (new URLSearchParams(location.search).has("mock")) {
      loadMock(resolve);
      return;
    }
    const useReal = () => resolve({ api: window.pywebview.api, mock: false });
    if (typeof window.pywebview?.api?.get_state === "function") {
      useReal();
      return;
    }
    window.addEventListener("pywebviewready", useReal, { once: true });
    // Sem o objeto pywebview depois do prazo: estamos num navegador comum.
    setTimeout(() => {
      if (!window.pywebview) {
        window.removeEventListener("pywebviewready", useReal);
        loadMock(resolve);
      }
    }, MOCK_WAIT_MS);
  });
}

/** Resolve quando a API (real ou mock) estiver pronta: { api, mock }. */
export function ready() {
  apiPromise ??= resolveApi();
  return apiPromise;
}

/** Chama um método da RobinAPI. */
export async function call(method, ...args) {
  const { api } = await ready();
  return api[method](...args);
}
