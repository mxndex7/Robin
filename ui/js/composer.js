// Composer: textarea que cresce até ~8 linhas, Enter envia, Shift+Enter quebra linha,
// contador discreto em textos longos e botão enviar que vira "parar" durante a geração.

const COUNT_FROM = 500;

export function createComposer({ onSubmit, onStop }) {
  const form = document.getElementById("composer");
  const input = document.getElementById("composer-input");
  const button = document.getElementById("send-button");
  const count = document.getElementById("composer-count");

  let generating = false;

  // Cresce e encolhe com o texto, sem animar a altura (só transform e opacity animam). O teto de
  // ~8 linhas é o max-height do CSS; acima dele, o campo rola.
  function autosize() {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }

  function update() {
    const length = input.value.length;
    button.disabled = !generating && input.value.trim() === "";
    count.hidden = length < COUNT_FROM;
    count.textContent = `${length.toLocaleString("pt-BR")} caracteres`;
  }

  function submit() {
    if (generating) {
      onStop?.();
      return;
    }
    const text = input.value.trim();
    if (!text) return;
    onSubmit?.(text);
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
      update();
    },
    setGenerating(value) {
      generating = value;
      button.classList.toggle("is-stop", value);
      button.setAttribute("aria-label", value ? "Parar" : "Enviar");
      update();
    },
  };
}
