// Estado da memória na nuvem (Supabase), em texto para a lateral e as Configurações.
// status vem do Python (onCloudStatus, get_state, get_settings): { state, message, pending, url } ou null.

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

/** { state, short, long }: short para a lateral, long para as Configurações. */
export function describeCloud(status) {
  if (!status) return { state: "off", short: "local", long: "Desligada: o cérebro fica só neste PC." };
  const pending = status.pending ? plural(status.pending, "alteração na fila", "alterações na fila") : "";
  if (status.state === "ok") {
    return {
      state: "ok",
      short: status.pending ? `nuvem • ${status.pending} na fila` : "nuvem",
      long: pending ? `Conectada • ${pending}.` : "Conectada e sincronizada.",
    };
  }
  if (status.state === "offline") {
    return {
      state: "offline",
      short: status.pending ? `nuvem offline • ${status.pending} na fila` : "nuvem offline",
      long: `${status.message || "Sem conexão com o Supabase."}${pending ? ` ${pending}: o Robin reenvia sozinho.` : ""}`,
    };
  }
  if (status.state === "paused") {
    return { state: "paused", short: "nuvem pausada", long: status.message || "Banco pausado: reative no painel do Supabase." };
  }
  return { state: "error", short: "nuvem com erro", long: status.message || "O Supabase recusou o pedido." };
}

// Avisos quando o estado muda (o mesmo estado repetido não gera outro toast).
const NOTICES = {
  offline: "Sem conexão com o Supabase: as alterações ficam na fila e vão sozinhas depois.",
  paused: "Banco pausado: reative no painel do Supabase. Memória só para leitura.",
};

/** Texto do toast para a mudança de estado, ou null. */
export function cloudNotice(previous, status) {
  const state = status?.state ?? "off";
  if (state === previous) return null;
  if (state === "ok") return previous && previous !== "off" ? { message: "Nuvem conectada de novo." } : null;
  if (state === "off") return null;
  return { message: state === "error" ? status.message : NOTICES[state], tone: "error" };
}
