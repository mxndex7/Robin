"""Resposta junto com a ação: com ações de memória limpas, o argumento resposta das chamadas (ou o
texto escrito na mesma mensagem) já é a resposta (1 pedido). Ação com ressalva, leitura ou chamada
sem resposta voltam ao modelo (2º pedido), e o histórico guarda a resposta uma vez só."""

from types import SimpleNamespace

from conftest import send_and_wait
from google.genai import types

from robin import gemini
from robin.brain import PENDING
from robin.tools import ToolResult, clean_round


def _texts(events, gen_id) -> str:
    return "".join(chunk["text"] for chunk in events.of("onChunk", gen_id))


def _item(content, kind="a fazer"):
    return {"id": "x", "conteudo": content, "tipo": kind, "etiquetas": []}


def _reply_with_call(text: str, name: str, **args):
    """Gerador roteirizado: o modelo escreve `text` (ou nada) e pede a ferramenta na mesma mensagem."""
    def behavior():
        if text:
            yield types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)]),
            )])
        yield types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(
                function_call=types.FunctionCall(id="roteiro-1", name=name, args=args),
                thought_signature=b"assinatura-do-modelo",
            )]),
            finish_reason=types.FinishReason.STOP,
        )])
    return behavior


def test_rodada_limpa_so_com_acoes_sem_ressalva():
    saved = ("salvar_memoria", ToolResult({"ok": True, "item": _item("comprar uva")}))
    assert clean_round([saved])
    assert clean_round([saved, ("concluir_tarefa", ToolResult({"ok": True, "item": _item("pão")}))])

    assert not clean_round([])
    assert not clean_round([("buscar_memoria", ToolResult({"ok": True, "itens": []}))])
    assert not clean_round([("salvar_memoria", ToolResult({"ok": False, "ja_existe": _item("uva")}, ok=False))])
    assert not clean_round([("concluir_tarefa", ToolResult({"ok": True, "item": _item("uva"), "aviso": "Isso já estava feito."}))])
    assert not clean_round([("salvar_memoria", ToolResult({"ok": True, "item": _item("uva"), "nuvem": "NÃO foi para a nuvem"}))])
    # Uma ação limpa junto com uma leitura: o modelo responde (a leitura pode ser a resposta).
    assert not clean_round([saved, ("buscar_memoria", ToolResult({"ok": True, "itens": []}))])


def test_resposta_da_acao_termina_em_um_pedido(service, events, requests, brain, fast):
    send_and_wait(service, events, "lembre de comprar uva", 1)
    send_and_wait(service, events, "oi", 2)

    assert len(requests.calls) == 2  # um pedido por mensagem
    assert _texts(events, 1) == "Anotado: vou te lembrar de **comprar uva**."
    assert [item["content"] for item in brain.list_tasks(PENDING)] == ["comprar uva"]
    assert not events.of("onError")
    # A próxima mensagem vê o turno inteiro: chamada → resultado → a resposta, uma vez só.
    assert [text for role, text in requests.turns() if role == "model"] == ["", "Anotado: vou te lembrar de **comprar uva**."]


def test_chamada_sem_resposta_vai_ao_segundo_pedido(service, events, requests, brain, fast):
    requests.script(_reply_with_call("", "salvar_memoria", conteudo="comprar uva", etiquetas=[], tarefa=True))
    send_and_wait(service, events, "lembre de comprar uva", 1)

    assert len(requests.calls) == 2
    assert _texts(events, 1) == "Feito: **comprar uva**."
    assert len(brain.list_tasks(PENDING)) == 1


def test_ja_existe_volta_ao_modelo_sem_mostrar_a_resposta(service, events, requests, brain, fast):
    send_and_wait(service, events, "lembre de comprar uva", 1)
    send_and_wait(service, events, "lembre de comprar uva", 2)

    assert len(requests.calls) == 3  # 1 da primeira + 2 da segunda
    # O "Anotado" da chamada não aparece: com ressalva, quem fala é o 2º pedido.
    assert _texts(events, 2) == "Na verdade isso já estava aqui: **comprar uva**."
    assert len(brain.list_tasks(PENDING)) == 1


def test_texto_escrito_junto_tambem_vale_e_fica_uma_vez(service, events, requests, fast):
    requests.script(_reply_with_call("Anotado!", "salvar_memoria", conteudo="comprar uva", etiquetas=[], tarefa=True,
                                     resposta="Isto não aparece."))
    send_and_wait(service, events, "lembre de comprar uva", 1)
    send_and_wait(service, events, "oi", 2)

    assert len(requests.calls) == 2
    assert _texts(events, 1) == "Anotado!"  # o texto escrito vale mais que o argumento
    contents = requests.calls[-1]["contents"]
    assert [content.role for content in contents] == ["user", "model", "user", "model", "user"]
    assert [text for role, text in requests.turns() if role == "model"] == ["", "Anotado!"]
    calls = [part for part in contents[1].parts if part.function_call]
    assert [part.thought_signature for part in calls] == [b"assinatura-do-modelo"]


def test_desligado_a_resposta_vem_depois_do_resultado(service, events, requests, brain, fast, model_confirms):
    send_and_wait(service, events, "lembre de comprar uva", 1)

    assert len(requests.calls) == 2
    assert _texts(events, 1) == "Feito: **comprar uva**."


def test_respostas_de_varias_acoes_sem_repetir():
    calls = [SimpleNamespace(args={"resposta": "Anotei o pão."}), SimpleNamespace(args={"resposta": "Anotei o pão."}),
             SimpleNamespace(args={"resposta": " E a uva. "}), SimpleNamespace(args={}), SimpleNamespace(args=None)]
    assert gemini._call_replies(calls) == "Anotei o pão. E a uva."


def test_assinatura_do_texto_passa_para_a_chamada():
    content = types.Content(role="model", parts=[
        types.Part(text="pensando", thought=True),
        types.Part(text="Anotado!", thought_signature=b"assinatura"),
        types.Part(function_call=types.FunctionCall(name="salvar_memoria", args={})),
    ])
    parts = gemini._without_text(content).parts
    assert [bool(part.thought) for part in parts] == [True, False]
    assert parts[1].function_call.name == "salvar_memoria" and parts[1].thought_signature == b"assinatura"
