"""A memória inteira no system_instruction: tudo o que foi guardado (não só o que é a fazer), mais
novo primeiro, com #código curto que as ferramentas resolvem; orçamento com corte por data e
buscar_memoria só quando há corte; o que foi feito não é apagado ao abrir."""

from datetime import datetime, timedelta, timezone

from conftest import send_and_wait

from robin import gemini
from robin.brain import DONE, PENDING, LocalStore
from robin.tools import SHORT_ID, MemoryTools, Turn, memory_for_prompt, short_ids


def _item(item_id, content, status=None, days=0, tags=(), pinned=False, done_days=None) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": item_id, "content": content, "tags": list(tags), "status": status, "pinned": pinned,
        "created_at": (now - timedelta(days=days)).isoformat(),
        "done_at": (now - timedelta(days=done_days)).isoformat() if done_days is not None else None,
    }


def _text(events, gen_id) -> str:
    return "".join(chunk["text"] for chunk in events.of("onChunk", gen_id))


def test_memoria_inteira_menos_o_fixo_e_mais_nova_primeiro():
    text, cut = memory_for_prompt([
        _item("aaaaaaaa-1", "comprar pão", PENDING, days=3, tags=["compras"]),
        _item("bbbbbbbb-2", "Ideia: mudar a foto do LinkedIn", days=1, tags=["ideia"]),
        _item("cccccccc-3", "comprar\nuva", DONE, days=5, done_days=2),
        _item("dddddddd-4", "Sou Alex", days=9, pinned=True),
    ], 10_000)
    lines = text.splitlines()

    assert not cut and len(lines) == 3
    assert lines[0].endswith(" · Ideia: mudar a foto do LinkedIn [ideia] · #bbbbbbbb")
    assert lines[1].endswith(" · comprar pão [compras] · a fazer · #aaaaaaaa")
    assert " · comprar uva · feito em " in lines[2] and lines[2].endswith(" · #cccccccc")  # uma linha por coisa
    assert "Sou Alex" not in text  # o fixo vai no "Sobre a pessoa"
    assert memory_for_prompt([], 10_000) == ("(nada ainda)", False)


def test_codigo_curto_cresce_so_quando_dois_comecam_igual():
    assert short_ids(["aaaaaaaa11", "aaaaaaaa22", "aaaaaaab33", "bbbbbbbbcc"]) == {
        "aaaaaaaa11": "aaaaaaaa1", "aaaaaaaa22": "aaaaaaaa2", "aaaaaaab33": "aaaaaaab", "bbbbbbbbcc": "bbbbbbbb",
    }


def test_orcamento_corta_o_mais_antigo_e_mantem_o_que_e_a_fazer():
    items = [_item(f"{n:08d}-x", f"nota número {n}", days=n) for n in range(1, 6)]
    items.append(_item("ffffffff-p", "pagar a luz", PENDING, days=30))  # a fazer e a mais antiga
    full, _ = memory_for_prompt(items, 10_000)
    size = {line.split(" · ")[1]: len(line) + 1 for line in full.splitlines()}
    budget = size["pagar a luz"] + size["nota número 1"] + size["nota número 2"]

    text, cut = memory_for_prompt(items, budget)
    assert cut
    assert all(content in text for content in ("pagar a luz", "nota número 1", "nota número 2"))
    assert "nota número 3" not in text
    assert text.splitlines()[-1].startswith("- … e mais 3 coisa(s) antiga(s)") and "buscar_memoria" in text


def test_ferramentas_aceitam_o_codigo_curto(brain):
    tools = MemoryTools(brain)
    task = brain.add("comprar uva", [], PENDING)
    note = brain.add("meu carro é azul", [])

    code = short_ids(item["id"] for item in brain.snapshot())[task["id"]]
    assert tools.call("concluir_tarefa", {"id": f"#{code}"}, Turn("t:1"), set()).ok
    assert brain.get(task["id"])["status"] == DONE
    updated = tools.call("atualizar_memoria", {"id": note["id"][:SHORT_ID], "conteudo": "meu carro é vermelho"},
                         Turn("t:2"), set())
    assert updated.ok and updated.ids == [note["id"]]

    # Curto demais, sem par ou com dois pares: não acha (nunca adivinha).
    assert not tools.call("concluir_tarefa", {"id": task["id"][:4]}, Turn("t:3"), set()).ok
    assert not tools.call("concluir_tarefa", {"id": "ffffffffff"}, Turn("t:4"), set()).ok
    for suffix in ("1", "2"):
        brain.put({**brain.get(task["id"]), "id": f"abcdefgh-{suffix}", "request_id": None, "status": PENDING})
    refused = tools.call("concluir_tarefa", {"id": "abcdefgh"}, Turn("t:5"), set())
    assert not refused.ok and "Não achei" in refused.response["erro"]


def test_o_que_pedi_pra_anotar_traz_tudo(service, events, requests, brain, fast):
    brain.add("comprar pão", ["compras"], PENDING)
    brain.add("comprar uva", ["compras"], PENDING)
    brain.add("Ideia: mudar foto do LinkedIn", ["ideia"])

    send_and_wait(service, events, "o que pedi pra você anotar até agora?", 1)

    instruction = requests.system_instruction()
    assert all(content in instruction for content in ("comprar pão", "comprar uva", "Ideia: mudar foto do LinkedIn"))
    text = _text(events, 1)
    assert "Ideia: mudar foto do LinkedIn" in text and "comprar pão (a fazer)" in text
    assert len(requests.calls) == 1 and not events.of("onToolCall", 1)


def test_memoria_cortada_oferece_a_busca(service, events, requests, brain, fast, monkeypatch):
    monkeypatch.setattr(gemini, "MAX_PROMPT_MEMORY_CHARS", 1)  # além do que é a fazer, nada cabe
    brain.add("gosto de uva", [])
    brain.add("pagar a luz", [], PENDING)
    brain.backfill_embeddings()

    send_and_wait(service, events, "tem alguma fruta roxa?", 1)

    instruction = requests.system_instruction(0)
    assert "pagar a luz" in instruction and "gosto de uva" not in instruction
    assert "e mais 1 coisa(s) antiga(s)" in instruction
    declared = [d.name for d in requests.calls[0]["config"].tools[0].function_declarations]
    assert declared[0] == "buscar_memoria"
    assert [event["label"] for event in events.of("onToolCall", 1)] == ["Buscou: fruta roxa pequena"]
    text = _text(events, 1)
    assert "gosto de uva" in text and "semantica" in text


def test_abrir_o_robin_nao_apaga_o_que_foi_feito(files, robin_api):
    store = LocalStore(files.brain)
    old = store.add("comprar uva", [], PENDING)
    store.complete(old["id"])
    store.put({**store.get(old["id"]), "done_at": (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()})

    robin_api()
    assert LocalStore(files.brain).get(old["id"])["status"] == DONE
