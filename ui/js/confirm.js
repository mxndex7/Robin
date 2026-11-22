// Botão de duas etapas (apagar uma conversa ou algo da memória): o primeiro clique vira "Apagar?" e
// só o segundo confirma. Sair do botão, Esc ou 3s cancelam; o Esc não chega aos atalhos globais
// (como parar a geração).

const CONFIRM_MS = 3000;

/** `restore()` devolve o conteúdo normal do botão (o ícone). */
export function confirmOnSecondClick(button, onConfirm, { restore, label = "Apagar?" }) {
  const normalLabel = button.getAttribute("aria-label") || "";
  let timer = 0;
  const armed = () => button.classList.contains("is-confirming");

  function reset() {
    clearTimeout(timer);
    button.classList.remove("is-confirming");
    button.setAttribute("aria-label", normalLabel);
    button.replaceChildren(restore());
  }

  button.addEventListener("click", () => {
    if (!armed()) {
      button.classList.add("is-confirming");
      button.setAttribute("aria-label", `${label} Clique de novo para confirmar.`);
      button.replaceChildren(label);
      clearTimeout(timer);
      timer = setTimeout(reset, CONFIRM_MS);
      return;
    }
    reset();
    onConfirm();
  });
  button.addEventListener("blur", () => armed() && reset());
  button.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !armed()) return;
    event.preventDefault();
    event.stopPropagation();
    reset();
  });
}
