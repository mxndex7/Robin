"""Modo simulação (ROBIN_MOCK=1): respostas locais, sem API Key e sem rede.

MockGeminiService é o próprio GeminiService com o cliente trocado. Sessão, histórico
(record_history ao parar, regenerar, rebuild mantendo o histórico), flusher, cancelamento,
lock, watchdog, nova tentativa, fallback e o laço das ferramentas são exatamente o código do modo
real; só a "rede" é simulada (generate_content_stream, embed_content e models.list).

Toda resposta começa com um pedaço de raciocínio (thought), como nos modelos 3.x, e qualquer 2.5
devolve 404 como para as chaves novas (um GEMINI_MODEL antigo no .env migra pelo fallback).
Gatilhos pela mensagem (palavra inteira, sem acento): tabela, codigo, longo, erro, fallback,
lento, cota, "o que eu disse antes?" / "qual foi minha mensagem anterior?". Qualquer outra mensagem
recebe um texto com títulos, listas, código inline, link e citação.

Cérebro (function calls de verdade, executados pelas ferramentas reais), como a diretiva pede: o
modelo simulado "sabe" a memória que o serviço pôs no system_instruction e manda a resposta no
argumento resposta da ação, como o flash-lite (o 2º pedido só vem quando a ação tem ressalva). "lembre de X" (a fazer) /
"lembre que X" (salvar_memoria; ela confere os parecidos), "o que pedi pra você anotar" (tudo, da
memória do prompt), "o que tenho pra fazer", "quais tarefas cumpri", "comprei X" (risca o que está a
fazer), "fruta roxa" e "busque X" (da memória do prompt; buscar_memoria só quando ela é oferecida,
com a memória cortada), "esqueça X" (pede confirmação), "confirmo" (apaga) e "conversamos sobre X" /
"falamos sobre X" (dos trechos das conversas anteriores; buscar_conversas quando ela é oferecida).
"sem embedding" na mensagem faz os embeddings falharem (429) por EMBED_FAIL_SECONDS.
Nuvem simulada (cloud_mock.py): "sem internet" (30s offline), "pausar banco" e "reativar banco".
O embedding falso é determinístico: palavras e conceitos (CONCEPTS) espalhados por hash nas 768
dimensões, então sinônimos óbvios ficam próximos.
"""

import hashlib

import random
import re
import time
import unicodedata
from types import SimpleNamespace
from typing import Iterator

from google import genai
from google.genai import errors, types

from . import cloud_mock
from .brain import normalize
from .config import EMBEDDING_DIMENSIONS, FALLBACK_MODEL, FALLBACK_MODELS
from .gemini import GeminiService, _content_parts, _is_user_message

THINKING_SECONDS = 0.8
# "lento": fica só no raciocínio por mais tempo que o watchdog (90s) em qualquer modelo que não
# seja o de reserva; depois da nova tentativa, o fallback temporário responde.
SLOW_SECONDS = 100.0
CHUNK_CHARS = (5, 20)
CHUNK_DELAY = (0.03, 0.06)

FENCE = "```"

MEMORY_QUESTIONS = ("o que eu disse antes", "qual foi minha mensagem anterior")

EMBED_FAIL_SECONDS = 30.0
_embed_fail_until = 0.0

# Conceitos do embedding falso: cada palavra também "acende" estes traços.
CONCEPTS = {
    "uva": ("fruta", "roxa", "pequena"),
    "jabuticaba": ("fruta", "roxa", "pequena"),
    "ameixa": ("fruta", "roxa"),
    "acai": ("fruta", "roxa"),
    "banana": ("fruta", "amarela"),
    "limao": ("fruta", "verde", "pequena"),
    "morango": ("fruta", "vermelha", "pequena"),
    "maca": ("fruta", "vermelha"),
    "feira": ("compra", "fruta"),
    "mercado": ("compra",),
    "comprar": ("compra",),
    "pagar": ("conta", "pagamento"),
    "boleto": ("conta", "pagamento"),
    "luz": ("conta", "energia"),
    "aniversario": ("aniversario", "data"),
    "anos": ("aniversario",),
    "nasci": ("aniversario",),
    "reuniao": ("trabalho", "compromisso"),
    "medico": ("saude", "compromisso"),
    "dentista": ("saude", "compromisso"),
    "remedio": ("saude",),
}
TAGS = {"compra": "compras", "conta": "contas", "saude": "saúde", "trabalho": "trabalho", "aniversario": "datas"}

PENDING_QUESTIONS = ("o que tenho pra fazer", "o que tenho para fazer", "o que falta fazer", "minhas tarefas",
                     "tarefas pendentes")
DONE_QUESTIONS = ("quais tarefas cumpri", "o que eu ja fiz", "tarefas concluidas")
ALL_QUESTIONS = ("o que pedi pra voce anotar", "o que pedi para voce anotar", "o que pedi pra anotar",
                 "o que pedi pra voce lembrar", "o que voce sabe de mim")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{word}\b", text) is not None


def _quote(text: str, limit: int = 280) -> str:
    text = text.strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _reply_default(prompt: str, model: str) -> str:
    return f"""## Resposta simulada

Este é o **modo simulação** do Robin: nada sai para a API do Gemini (modelo exibido: `{model}`). Você escreveu:

{_quote(prompt)}

### O que dá para testar aqui
- Streaming com fade por trecho e o cursor no fim do texto.
- Parar no meio com `Esc` ou com o botão quadrado.
- Regenerar, copiar e o botão de ir para o fim.

### Gatilhos
1. `tabela`, `codigo` ou `longo` para respostas específicas.
2. `erro` para simular falha de rede.
3. `fallback` para simular a troca de modelo.
4. `lento` para testar o watchdog (o modelo fica 100s só raciocinando).
5. `cota` para simular uma chave sem cota no modelo (429 com "limit: 0"): o seletor marca o modelo.
6. "o que eu disse antes?" para ler o histórico real da sessão.

> O histórico desta conversa é real: parar, regenerar e salvar a memória seguem as mesmas regras do modo normal.

Referência do SDK: [google-genai](https://googleapis.github.io/python-genai/)."""


def _reply_table() -> str:
    return """Comparativo simulado de estratégias de cache:

| Estratégia | Latência | Consistência | Complexidade |
|---|---|---|---|
| Sem cache | Alta | Forte | Baixa |
| Cache local (LRU) | Baixa | Eventual | Baixa |
| Redis compartilhado | Baixa | Eventual | Média |
| Write-through | Média | Forte | Média |
| CDN na borda | Muito baixa | Eventual | Alta |

Escolha primeiro pela consistência exigida; a latência vem depois."""


def _reply_code() -> str:
    return f"""Dois blocos simulados, um em Python e um em bash:

{FENCE}python
from pathlib import Path


def changed_files(root: Path, suffix: str = ".py") -> list[Path]:
    \"\"\"Lista arquivos com o sufixo pedido, ignorando ambientes virtuais.\"\"\"
    return sorted(
        path for path in root.rglob(f"*{{suffix}}")
        if ".venv" not in path.parts
    )


if __name__ == "__main__":
    for path in changed_files(Path.cwd()):
        print(path)
{FENCE}

{FENCE}bash
# Revisa antes de qualquer commit (nada é executado sem sua confirmação)
git status --short
git diff --stat
git add -p
{FENCE}

Confirme antes de eu sugerir o commit."""


def _reply_long() -> str:
    sections = [
        ("Levantamento", "Mapear módulos, dependências e pontos de entrada"),
        ("Contratos", "Definir interfaces estáveis entre as camadas"),
        ("Dados", "Planejar a migração de esquema sem janela de indisponibilidade"),
        ("Testes", "Cobrir os fluxos críticos antes de mexer na estrutura"),
        ("Entrega", "Publicar em etapas, com caminho de volta em cada uma"),
        ("Observabilidade", "Medir antes e depois para provar o ganho"),
    ]
    lines = ["# Plano de migração (simulado)", "", "Resposta longa para testar a rolagem e o botão parar.", ""]
    for number, (title, summary) in enumerate(sections, start=1):
        lines += [f"## {number}. {title}", "", summary + ".", ""]
        lines += [f"- Passo {number}.{step}: detalhe operacional {step} da etapa de {title.lower()}." for step in range(1, 7)]
        lines.append("")
    lines += ["> Fim do plano simulado. Nada foi alterado no seu código."]
    return "\n".join(lines)


def _reply_memory(user_texts: list[str]) -> str:
    previous = user_texts[:-1]
    if not previous:
        return "Esta é a sua primeira mensagem nesta conversa: não há mensagem anterior no histórico."
    return (
        f"Sua mensagem anterior foi:\n\n{_quote(previous[-1])}\n\n"
        f"Tirei do histórico real desta sessão ({len(user_texts)} mensagens suas até agora, contando esta)."
    )


def _reply_fallback(model: str) -> str:
    if model == FALLBACK_MODEL:
        return (
            f"Resposta gerada pelo `{model}`. Se esta mensagem disparou o fallback, o modelo anterior "
            "devolveu 404 no primeiro envio e a sessão foi recriada com o histórico intacto."
        )
    return f"Fallback não simulado: o modelo atual já é `{model}`."


def _reply_quota(model: str) -> str:
    return (
        f"Resposta do `{model}`: o modelo anterior devolveu 429 com `limit: 0` (sem cota nesta chave), "
        "a nova tentativa falhou igual e o modelo de reserva assumiu nesta sessão. "
        "O seletor de modelos marca o anterior como sem cota até o app reabrir."
    )


def _reply_slow(model: str) -> str:
    return (
        f"Resposta do `{model}` depois do teste de lentidão: o watchdog abandonou as tentativas "
        "que ficaram só raciocinando e o modelo de reserva assumiu nesta sessão."
    )


def _features(text: str) -> list[tuple[str, float]]:
    features = []
    for word in re.findall(r"[a-z0-9]+", normalize(text)):
        if len(word) < 3:
            continue
        stem = word[:-1] if len(word) > 3 and word.endswith("s") else word
        features.append((stem, 1.0))
        features += [(concept, 0.8) for concept in CONCEPTS.get(stem, ())]
    return features


def fake_embedding(text: str, dims: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Determinístico: cada traço cai numa dimensão (e sinal) pelo hash."""
    vector = [0.0] * dims
    for feature, weight in _features(text):
        digest = hashlib.md5(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "little") % dims
        vector[index] += weight if digest[4] & 1 else -weight
    if not any(vector):
        vector[0] = 1.0
    return vector


def _tags_for(text: str) -> list[str]:
    tags = []
    for feature, _ in _features(text):
        tag = TAGS.get(feature)
        if tag and tag not in tags:
            tags.append(tag)
    return tags or ["geral"]


def _after(key: str, words: tuple[str, ...]) -> str:
    """O resto da mensagem depois do gatilho (sem pontuação final)."""
    for word in words:
        match = re.search(rf"\b{word}\b\s*(.*)", key)
        if match:
            return match.group(1).strip(" .!?:")
    return ""


def _turn_responses(contents: list) -> list[tuple[str, dict]]:
    """Respostas das ferramentas depois da última mensagem do usuário, em ordem."""
    last = max((i for i, content in enumerate(contents) if _is_user_message(content)), default=-1)
    return [
        (part.function_response.name, part.function_response.response or {})
        for content in contents[last + 1:] if content.role == "user"
        for part in content.parts or [] if part.function_response
    ]


def _tools_enabled(config) -> bool:
    if not config or not config.tools:
        return False
    calling = config.tool_config.function_calling_config if config.tool_config else None
    return not (calling and calling.mode == types.FunctionCallingConfigMode.NONE)


def _declared(config, name: str) -> bool:
    """A ferramenta foi oferecida neste pedido (buscar_memoria só vem com a memória cortada)."""
    return _tools_enabled(config) and any(
        declaration.name == name for tool in config.tools for declaration in tool.function_declarations or []
    )


def _call(name: str, **args) -> SimpleNamespace:
    """Uma chamada de ferramenta. As ações levam a resposta no argumento resposta, como o modelo real."""
    return SimpleNamespace(name=name, args=args)


def _bullets(items: list[dict]) -> str:
    return "\n".join(f"- {item['conteudo']}" + (f" ({item['estado']})" if item.get("estado") else "") for item in items)


# Uma linha da memória do system_instruction (tools.memory_for_prompt).
_PROMPT_LINE = re.compile(r"^- \d{2}/\d{2}/\d{4} · (.+?)(?: \[[^\]]*\])?(?: · (a fazer|feito em \S+))? · #(\w+)$", re.M)


def _prompt_memory(config) -> list[dict]:
    """A memória que o serviço pôs no system_instruction: o que o modelo simulado "sabe"."""
    instruction = str(getattr(config, "system_instruction", "") or "") if config else ""
    return [{"id": match.group(3), "conteudo": match.group(1), "estado": match.group(2) or ""}
            for match in _PROMPT_LINE.finditer(instruction)]


# Uma mensagem dos trechos das conversas anteriores (conversations.ConversationStore.excerpt).
_CONVERSATION_LINE = re.compile(r"^\d{2}/\d{2} \d{2}:\d{2} (?:Pessoa|Robin): .+$", re.M)


def _prompt_conversation_lines(config, text: str) -> str:
    """As linhas dos trechos das conversas anteriores com algum traço em comum com `text`."""
    instruction = str(getattr(config, "system_instruction", "") or "") if config else ""
    terms = {feature for feature, _ in _features(text)}
    lines = [line for line in _CONVERSATION_LINE.findall(instruction)
             if terms & {feature for feature, _ in _features(line)}]
    return "\n".join(f"- {line}" for line in lines)


def _matching(items: list[dict], text: str) -> list[dict]:
    """O que o modelo acharia pelo assunto: palavras ou conceitos (CONCEPTS) em comum."""
    terms = {feature for feature, _ in _features(text)}
    return [item for item in items if terms & {feature for feature, _ in _features(item["conteudo"])}]


def _after_action(last: dict) -> str:
    """O 2º pedido depois de uma ação: só acontece com ressalva (ou sem REPLY_WITH_ACTION)."""
    if last.get("ja_existe"):
        return f"Na verdade isso já estava aqui: **{last['ja_existe']['conteudo']}**."
    if not last.get("ok"):
        return f"Não deu: {last.get('erro')}"
    if last.get("nuvem"):
        return "Só que ficou só neste PC por enquanto (sem conexão com a nuvem): sobe sozinho quando a conexão voltar."
    if last.get("aviso"):
        return f"Detalhe: {last['aviso']}"
    item = last.get("item") or last.get("apagado") or {}
    return f"Feito: **{item.get('conteudo', '')}**."


def _brain_step(key: str, responses: list[tuple[str, dict]], history: list, config=None) -> object:
    """Próximo passo do cérebro simulado: uma chamada (_call), um texto, ou None (não é gatilho)."""
    rounds = len(responses)
    last = responses[-1][1] if responses else {}
    memory = _prompt_memory(config)

    if _has_word(key, "confirmo"):
        pending = [
            response["item"]["id"] for name, response in _turn_responses_all(history)
            if name == "apagar_memoria" and response.get("confirmacao_necessaria")
        ]
        if not pending:
            return "Não tem nada esperando confirmação pra apagar."
        if rounds == 0:
            return _call("apagar_memoria", resposta="Pronto, esqueci.", id=pending[-1], confirmado=True)
        return _after_action(last)

    # Perguntas antes dos gatilhos de ação: "o que pedi pra você lembrar" tem "lembrar".
    if any(question in key for question in ALL_QUESTIONS):
        return f"Até agora:\n\n{_bullets(memory)}" if memory else "Por enquanto você não me pediu pra lembrar de nada."

    if any(question in key for question in DONE_QUESTIONS):
        done = [item for item in memory if item["estado"].startswith("feito")]
        return f"Você já fez:\n\n{_bullets(done)}" if done else "Nada riscado ainda."

    if any(question in key for question in PENDING_QUESTIONS):
        todo = [item for item in memory if item["estado"] == "a fazer"]
        return f"Falta fazer:\n\n{_bullets(todo)}" if todo else "Nada pendente. Aproveita."

    # Antes de "lembre": "lembra quando falamos sobre X" é sobre uma conversa antiga.
    talked = _after(key, ("conversamos sobre", "falamos sobre", "falamos de"))
    if talked:
        if _declared(config, "buscar_conversas") or responses:
            if rounds == 0:
                return _call("buscar_conversas", consulta=talked)
            found = last.get("trechos", [])
            if not found:
                return f"Não achei nada sobre \"{talked}\" nas conversas antigas."
            lines = "\n".join(f"- {item['quando']} {item['quem']}: {item['texto']}" for item in found)
            return f"Achei nas conversas antigas:\n\n{lines}"
        found = _prompt_conversation_lines(config, talked)
        return f"Pelo que a gente conversou:\n\n{found}" if found else f"Não lembro de a gente ter falado de \"{talked}\"."

    remember = _after(key, ("lembre", "lembra", "lembrar", "anote", "anota"))
    if remember:
        is_task = not remember.startswith("que ")
        content = re.sub(r"^(de|que)\s+", "", remember)
        if rounds == 0:
            reply = f"Anotado: vou te lembrar de **{content}**." if is_task else f"Guardado: **{content}**."
            return _call("salvar_memoria", conteudo=content, etiquetas=_tags_for(content), tarefa=is_task, resposta=reply)
        return _after_action(last)

    done = _after(key, ("comprei", "terminei", "fiz", "paguei"))
    if done:
        if rounds == 0:
            matches = [item for item in _matching(memory, done) if item["estado"] == "a fazer"]
            if not matches:
                return f"Não achei nada a fazer sobre \"{done}\"."
            if len(matches) > 1:
                return f"Qual delas?\n\n{_bullets(matches)}"
            return _call("concluir_tarefa", id=matches[0]["id"], resposta=f"Boa, risquei **{matches[0]['conteudo']}**.")
        return _after_action(last)

    query = "fruta roxa pequena" if "fruta roxa" in key else _after(key, ("busque", "procure"))
    if query:
        if not _declared(config, "buscar_memoria") and not responses:
            found = _matching(memory, query)
            return f"Tá na memória:\n\n{_bullets(found)}" if found else f"Não tenho nada sobre \"{query}\"."
        if rounds == 0:
            return _call("buscar_memoria", consulta=query)
        items = last.get("itens", [])
        if not items:
            return f"Nada sobre \"{query}\" nas coisas antigas (busca {last.get('modo')})."
        lines = "\n".join(f"- {item['conteudo']} ({item['tipo']})" for item in items)
        return f"Achei nas coisas antigas (busca {last.get('modo')}):\n\n{lines}"

    forget = _after(key, ("esqueca", "apague"))
    if forget:
        if rounds == 0:
            found = _matching(memory, forget)
            if not found:
                return f"Não achei nada sobre \"{forget}\"."
            # Sem confirmado: a ferramenta recusa e mostra o que seria apagado; o 2º pedido pergunta.
            return _call("apagar_memoria", id=found[0]["id"], resposta="Esqueci.")
        if last.get("confirmacao_necessaria"):
            return f"Posso esquecer **{last['item']['conteudo']}**? Responda \"confirmo\"."
        return _after_action(last)
    return None


def _turn_responses_all(contents: list) -> list[tuple[str, dict]]:
    return [
        (part.function_response.name, part.function_response.response or {})
        for content in contents if content.role == "user"
        for part in content.parts or [] if part.function_response
    ]


def _call_chunk(call: SimpleNamespace, number: int) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(id=f"mock-{number}", name=call.name, args=call.args),
            thought_signature=b"assinatura-simulada",
        )]),
        finish_reason=types.FinishReason.STOP,
    )])


def _thought_chunk() -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(text="Analisando o pedido (simulação).", thought=True)],
    ))])


class _SimulatedNetwork:
    """Substitui as chamadas de rede do SDK."""

    def generate_content_stream(self, model: str, contents, config=None) -> Iterator[types.GenerateContentResponse]:
        global _embed_fail_until
        contents = list(contents)
        user_texts = [_content_parts(content.parts or [])[0] for content in contents if _is_user_message(content)]
        prompt = user_texts[-1] if user_texts else ""
        key = _normalize(prompt)
        name = model.removeprefix("models/")
        responses = _turn_responses(contents)
        if "sem embedding" in key and not responses:
            _embed_fail_until = time.monotonic() + EMBED_FAIL_SECONDS
        if not responses:
            cloud_mock.apply_trigger(prompt)

        time.sleep(THINKING_SECONDS / 2)  # "pensando"

        if name.startswith("gemini-2.5") or (_has_word(key, "fallback") and name != FALLBACK_MODEL):
            raise errors.ClientError(404, {"error": {
                "code": 404, "status": "NOT_FOUND",
                "message": f"This model models/{name} is no longer available to new users (simulação).",
            }})
        if _has_word(key, "cota") and name != FALLBACK_MODEL:
            raise errors.ClientError(429, {"error": {
                "code": 429, "status": "RESOURCE_EXHAUSTED",
                "message": "You exceeded your current quota. Quota exceeded for metric: "
                           f"generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 0, model: {name} (simulação)",
            }})
        if _has_word(key, "erro"):
            raise ConnectionError("Simulação: falha de rede ao conectar à API (network).")

        yield _thought_chunk()  # "Raciocinando…"
        if _has_word(key, "lento") and name != FALLBACK_MODEL:
            time.sleep(SLOW_SECONDS)
        time.sleep(THINKING_SECONDS / 2)

        step = _brain_step(key, responses, contents, config) if _tools_enabled(config) or responses else None
        if isinstance(step, SimpleNamespace):
            if _tools_enabled(config):
                yield _call_chunk(step, len(responses) + 1)
                return
            step = "Limite de ferramentas neste turno atingido (simulação)."
        if isinstance(step, str):
            text = step
        elif any(question in key for question in MEMORY_QUESTIONS):
            text = _reply_memory(user_texts)
        elif _has_word(key, "fallback"):
            text = _reply_fallback(name)
        elif _has_word(key, "cota"):
            text = _reply_quota(name)
        elif _has_word(key, "lento"):
            text = _reply_slow(name)
        elif _has_word(key, "tabela"):
            text = _reply_table()
        elif _has_word(key, "codigo"):
            text = _reply_code()
        elif _has_word(key, "longo"):
            text = _reply_long()
        else:
            text = _reply_default(prompt, name)
        yield from self._chunks(text)

    @staticmethod
    def _chunks(text: str) -> Iterator[types.GenerateContentResponse]:
        index = 0
        while index < len(text):
            size = random.randint(*CHUNK_CHARS)
            piece = text[index:index + size]
            index += size
            last = index >= len(text)
            yield types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=piece)]),
                finish_reason=types.FinishReason.STOP if last else None,
            )])
            if not last:
                time.sleep(random.uniform(*CHUNK_DELAY))

    @staticmethod
    def embed_content(model: str, contents, config=None) -> types.EmbedContentResponse:
        if time.monotonic() < _embed_fail_until:
            raise errors.ClientError(429, {"error": {
                "code": 429, "status": "RESOURCE_EXHAUSTED",
                "message": "Quota exceeded for metric: embed_content_free_tier_requests (simulação)",
            }})
        dims = (config.output_dimensionality if config else None) or EMBEDDING_DIMENSIONS
        return types.EmbedContentResponse(embeddings=[
            types.ContentEmbedding(values=fake_embedding(str(text), dims)) for text in contents
        ])

    @staticmethod
    def list_models(config=None):
        return iter(SimpleNamespace(name=f"models/{name}", supported_actions=["generateContent"]) for name in FALLBACK_MODELS)


def _simulated_client() -> genai.Client:
    client = genai.Client(api_key="simulacao")  # não acessa a rede: só a sessão do SDK é usada
    network = _SimulatedNetwork()
    client.models.generate_content_stream = network.generate_content_stream
    client.models.list = network.list_models
    client.models.embed_content = network.embed_content
    return client


class MockGeminiService(GeminiService):
    """GeminiService com a rede simulada. Funciona sem API Key."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("rate_limit", 0)  # sem rede, sem limite por minuto
        super().__init__(*args, **kwargs)

    def configure(self, api_key: str) -> None:
        with self._lock:
            history = self._history()
            self._client = _simulated_client()
            self._chat = self._new_chat(history)
        self._embeddings_changed()
