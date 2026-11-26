// Composer: textarea que cresce até ~8 linhas, Enter envia, Shift+Enter quebra linha,
// contador discreto em textos longos e botão enviar que vira "parar" durante a geração.

import { icon } from "./icons.js";
import { toast } from "./toast.js";

const COUNT_FROM = 500;

export function createComposer({ onSubmit, onStop, limits, changeFolder }) {
  const form = document.getElementById("composer");
  const input = document.getElementById("composer-input");
  const button = document.getElementById("send-button");
  const count = document.getElementById("composer-count");

  const fileInput = document.getElementById("composer-file-input");
  const attachButton = document.getElementById("attach-button");
  const folderButton = document.getElementById("folder-button");
  const attachmentList = document.getElementById("composer-attachments");
  // A opção "Todos os arquivos" do seletor permite escolher Dockerfile, Makefile e outros nomes sem extensão.
  if (limits) fileInput.accept = limits.extensions.join(",");
  attachButton.hidden = !limits;

  let generating = false;
  let reading = false;
  let selection = 0;
  let attachments = [];
  let context = limits?.context;
  let pickerContext = null;
  let folder = null;
  let folderBusy = false;
  let folderRemove = null;

  function chip(text, removeLabel, removeEntry) {
    const item = document.createElement("li");
    item.className = "file-chip";
    const name = document.createElement("span");
    name.className = "file-chip__name";
    name.textContent = name.title = text;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn icon-btn--sm";
    remove.setAttribute("aria-label", removeLabel);
    remove.append(icon("x", "icon icon--sm"));
    remove.addEventListener("click", removeEntry);
    item.append(name, remove);
    return { item, remove };
  }

  function renderAttachments() {
    const items = attachments.map((file, index) => chip(file.name, `Remover ${file.name}`, () => {
      attachments = attachments.filter((_, i) => i !== index);
      renderAttachments();
      update();
      input.focus();
    }).item);
    folderRemove = null;
    if (folder) {
      const { item, remove } = chip(`${folder.name} · Somente leitura`, "Remover pasta", () => selectFolder(folder.id));
      item.title = "O provedor ativo recebe os trechos consultados dentro desta pasta.";
      folderRemove = remove;
      items.unshift(item);
    }
    attachmentList.hidden = !items.length;
    attachmentList.replaceChildren(...items);
  }

  async function selectFolder(removeId = "") {
    if (folderBusy || generating) return;
    folderBusy = true;
    const previousContext = context;
    update();
    try {
      const result = await changeFolder(previousContext, removeId, () => previousContext === context);
      if (!result?.ok && previousContext === context) {
        toast(result?.error || "Não foi possível atualizar a pasta desta conversa.", { tone: "error" });
      }
    } catch {
      if (previousContext === context) toast("Não foi possível atualizar a pasta desta conversa.", { tone: "error" });
    } finally {
      folderBusy = false;
      update();
      input.focus();
    }
  }

  function clearFiles() {
    selection += 1;
    reading = false;
    attachments = [];
    fileInput.value = "";
    renderAttachments();
    update();
  }

  attachButton.addEventListener("click", () => {
    pickerContext = context;
    fileInput.click();
  });
  folderButton.addEventListener("click", () => selectFolder());
  fileInput.addEventListener("cancel", () => {
    pickerContext = null;
  });
  fileInput.addEventListener("change", async () => {
    const chosenFor = pickerContext;
    pickerContext = null;
    const files = [...fileInput.files];
    fileInput.value = "";
    if (!limits || reading || generating || !files.length) return;
    if (chosenFor && chosenFor !== context) {
      toast("A conversa ou o provedor mudou. Selecione os arquivos novamente.");
      return;
    }
    const ticket = ++selection;
    reading = true;
    update();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    try {
      for (const file of files) {
        const lower = file.name.toLowerCase();
        if (attachments.length >= limits.max_files) {
          toast(`Selecione até ${limits.max_files} anexos por mensagem.`, { tone: "error" });
          break;
        }
        if (!limits.basenames.includes(lower) && !limits.extensions.some((ext) => lower.endsWith(ext))) {
          toast(`${file.name}: selecione um arquivo de texto, código ou log.`, { tone: "error" });
          continue;
        }
        if (file.size > limits.max_file_bytes) {
          toast(`${file.name}: cada arquivo pode ter até ${limits.max_file_bytes / 1024} KB.`, { tone: "error" });
          continue;
        }
        if (attachments.reduce((total, item) => total + item.size, file.size) > limits.max_total_bytes) {
          toast(`Os arquivos podem somar até ${limits.max_total_bytes / 1024} KB.`, { tone: "error" });
          continue;
        }
        try {
          const content = decoder.decode(await file.arrayBuffer());
          if (ticket !== selection) return;
          if (/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(content)) throw new Error("binary");
          attachments.push({ name: file.name, size: file.size, content });
        } catch {
          if (ticket !== selection) return;
          toast(`${file.name}: use um arquivo de texto em UTF-8.`, { tone: "error" });
        }
      }
    } finally {
      if (ticket === selection) {
        reading = false;
        renderAttachments();
        update();
      }
    }
  });

  // Cresce e encolhe com o texto, sem animar a altura (só transform e opacity animam). O teto de
  // ~8 linhas é o max-height do CSS; acima dele, o campo rola.
  function autosize() {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }

  function update() {
    const length = input.value.length;
    button.disabled = !generating && (reading || folderBusy || (!attachments.length && input.value.trim() === ""));
    attachButton.disabled = reading || folderBusy || generating;
    folderButton.disabled = reading || folderBusy || generating;
    if (folderRemove) folderRemove.disabled = folderBusy || generating;
    count.hidden = length < COUNT_FROM;
    count.textContent = `${length.toLocaleString("pt-BR")} caracteres`;
  }

  function submit() {
    if (generating) {
      onStop?.();
      return;
    }
    const text = input.value.trim();
    if (reading || folderBusy || (!text && !attachments.length)) return;
    onSubmit?.(text, [...attachments], context, folder?.id || "");
  }

  input.addEventListener("input", () => {
    autosize();
    update();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });

  update();

  return {
    get hasFolder() {
      return Boolean(folder);
    },
    focus() {
      input.focus();
    },
    setText(text) {
      input.value = text;
      autosize();
      update();
      input.focus();
      input.setSelectionRange(text.length, text.length);
    },
    clear() {
      input.value = "";
      autosize();
      clearFiles();
    },
    clearFiles,
    setAttachmentContext(next) {
      if (!next || next === context) return;
      context = next;
      if (attachments.length || reading) {
        clearFiles();
        toast("A conversa ou o provedor mudou. Selecione os arquivos novamente.");
      }
    },
    setFolder(value, available) {
      folderButton.hidden = !available;
      if (folder?.id === value?.id) return;
      folder = value;
      renderAttachments();
      update();
    },
    restoreDraft(text, files, previousContext) {
      input.value = input.value ? [text, input.value].filter(Boolean).join("\n\n") : text;
      if (previousContext === context) attachments = [...files];
      renderAttachments();
      autosize();
      update();
      input.focus();
    },
    setGenerating(value) {
      generating = value;
      button.classList.toggle("is-stop", value);
      button.setAttribute("aria-label", value ? "Parar" : "Enviar");
      update();
    },
  };
}
