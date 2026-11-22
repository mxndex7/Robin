"""Seletor de modelos (com a marca de "sem cota"), nível de raciocínio e o modelo salvo no .env
(fallback 404 x temporário)."""

from types import SimpleNamespace

import pytest
from conftest import Requests
from dotenv import dotenv_values
from google.genai import errors

from robin.config import DEFAULT_MODEL, FALLBACK_MODEL
from robin.gemini import _chat_models, _classify, _model_sort_key, _thinking_level
from robin.mock import MockGeminiService

# Resposta real do models.list() (só os com generateContent) para a chave do usuário, out/2026.
REAL_MODELS = [
    "antigravity-preview-05-2026", "antigravity-preview-09-2026", "antigravity-preview-latest",
    "deep-research-max-preview-04-2026", "deep-research-preview-04-2026", "deep-research-pro-preview-12-2025",
    "gemini-2.5-computer-use-preview-10-2025", "gemini-2.5-flash", "gemini-2.5-flash-image",
    "gemini-2.5-flash-lite", "gemini-2.5-flash-preview-tts", "gemini-2.5-pro", "gemini-2.5-pro-preview-tts",
    "gemini-3-flash-preview", "gemini-3-pro-image", "gemini-3-pro-image-preview", "gemini-3.1-flash-image",
    "gemini-3.1-flash-image-preview", "gemini-3.1-flash-lite", "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-lite-preview", "gemini-3.1-flash-tts-preview", "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.5-transcribe",
    "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts",
    "gemini-4-argon", "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-nano-banana-2.1",
    "gemini-omni-1.1-flash", "gemini-omni-flash-preview", "gemini-pro-latest", "gemini-robotics-er-2-preview",
    "gemma-4-26b-a4b-it", "gemma-4-31b-it", "lyria-3-clip-preview", "lyria-3-pro-preview", "lyria-3.5",
    "nano-banana-pro-preview",
]


def _fake_client(names: list[str]) -> SimpleNamespace:
    listing = [SimpleNamespace(name=f"models/{name}", supported_actions=["generateContent"]) for name in names]
    return SimpleNamespace(models=SimpleNamespace(list=lambda config=None: iter(listing)))


# ---------- seletor ----------

def test_seletor_com_a_lista_real_so_tem_modelos_estaveis_de_chat():
    names = sorted(_chat_models(_fake_client(REAL_MODELS)), key=_model_sort_key)
    assert names == [
        "gemini-4-argon",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]


@pytest.mark.parametrize("name, listed", [
    ("gemini-3.9-pro", True),
    ("gemini-4.1-flash", True),
    ("gemini-4-argon", True),
    ("gemini-3.6-flash-001", False),         # versão numerada
    ("gemini-3.6-flash-09-2026", False),     # versão com data
    ("gemini-4-argon-001", False),
    ("gemini-4-argon-preview-11-2026", False),
    ("gemini-4-argon-tts", False),
    ("gemini-4-argon-live", False),
    ("gemini-4-argon-image", False),
    ("gemini-embedding-2", False),
    ("gemini-2.5-flash", False),
    ("gemini-2.0-flash", False),
    ("gemini-flash-latest", False),
])
def test_filtro_do_seletor(name, listed):
    assert (_chat_models(_fake_client([name])) == [name]) is listed


def test_modelo_atual_sempre_aparece(memory, events):
    service = MockGeminiService(emit=events, model="gemini-2.5-pro")
    assert service.list_models() == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-2.5-pro"]


@pytest.mark.parametrize("name, level", [
    ("gemini-3.8-flash", "LOW"),
    ("gemini-3.6-flash", "LOW"),
    ("gemini-3-flash-preview", "LOW"),
    ("gemini-3.5-flash-lite", "LOW"),  # padrão desde 31/10/2026: LOW em vez do minimal do modelo
    ("gemini-4-argon", None),
    ("gemini-2.5-pro", None),
])
def test_nivel_de_raciocinio(name, level):
    assert _thinking_level(name) == level


# ---------- modelo salvo no .env ----------

def test_sem_modelo_no_env_abre_no_padrao(robin_api):
    assert robin_api().get_state()["model"] == DEFAULT_MODEL == "gemini-3.5-flash-lite"


def test_25_no_env_migra_sozinho_para_a_reserva_e_grava_no_env(files, robin_api, events):
    files.env.write_text("GEMINI_API_KEY=chave-de-teste\nGEMINI_MODEL=gemini-2.5-pro\nROBIN_GIT_SYNC=0\n", encoding="utf-8")
    robin = robin_api()
    assert robin.get_state()["model"] == "gemini-2.5-pro"
    assert "gemini-2.5-pro" in robin.get_models()["models"]

    result = robin.send_message("oi")
    events.wait("onDone", result["gen_id"])

    assert events.of("onModelChanged") == [{"model": FALLBACK_MODEL, "previous": "gemini-2.5-pro", "reason": "fallback"}]
    assert not events.of("onError")
    # Só o GEMINI_MODEL mudou; a chave e as outras variáveis ficaram.
    assert dotenv_values(files.env) == {
        "GEMINI_API_KEY": "chave-de-teste", "GEMINI_MODEL": FALLBACK_MODEL, "ROBIN_GIT_SYNC": "0",
    }
    assert robin_api().get_state()["model"] == FALLBACK_MODEL  # a próxima abertura já começa nele


def test_falha_passageira_cai_na_reserva_so_nesta_execucao(files, robin_api, events, resilience):
    files.env.write_text("GEMINI_MODEL=gemini-3.8-flash\n", encoding="utf-8")
    robin = robin_api()

    result = robin.send_message("teste lento")
    events.wait("onDone", result["gen_id"])

    assert events.of("onModelChanged") == [{"model": FALLBACK_MODEL, "previous": "gemini-3.8-flash", "reason": "temporary",
                                            "cause": "transient", "expires_in": 300}]
    assert robin.get_state()["model"] == FALLBACK_MODEL
    assert dotenv_values(files.env) == {"GEMINI_MODEL": "gemini-3.8-flash"}  # nada gravado


# ---------- modelo sem cota nesta chave (429 com "limit: 0") ----------

def _quota_error(limit: str) -> errors.ClientError:
    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota. Quota exceeded for metric: "
                   f"generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: {limit}, model: x",
    }})


@pytest.mark.parametrize("limit, no_quota", [("0", True), ("10", False), ("0.5", False), ("20", False)])
def test_429_so_com_limit_zero_e_sem_cota(limit, no_quota):
    error = _classify(_quota_error(limit), "gemini-3.8-flash")
    assert error.code == "rate_limit"
    assert error.no_quota is no_quota


def test_429_sem_cota_marca_o_modelo_ate_reabrir_o_app(files, robin_api, events, resilience):
    files.env.write_text("GEMINI_MODEL=gemini-3.8-flash\n", encoding="utf-8")
    robin = robin_api()
    assert robin.get_models()["no_quota"] == []

    result = robin.send_message("teste de cota")
    events.wait("onDone", result["gen_id"])

    # Mesmo fluxo do 429 comum (nova tentativa e reserva só nesta execução), com o modelo marcado.
    assert events.of("onModelChanged") == [{"model": FALLBACK_MODEL, "previous": "gemini-3.8-flash", "reason": "temporary",
                                            "cause": "transient", "expires_in": 300}]
    models = robin.get_models()
    assert models["no_quota"] == ["gemini-3.8-flash"]
    assert "gemini-3.8-flash" in models["models"]  # continua selecionável
    assert robin.set_model("gemini-3.8-flash")["ok"] is True
    assert robin_api().get_models()["no_quota"] == []  # só em memória: some ao reabrir


def test_429_comum_nao_marca_o_modelo(memory, events, resilience):
    service = MockGeminiService(emit=events, model="gemini-3.8-flash")
    requests = Requests(service)
    requests.script(_raise(_quota_error("15")), _raise(_quota_error("15")))
    service.send("oi", 1)
    events.wait("onDone", 1)
    assert service.no_quota_models == []
    service.clear()


def _raise(error):
    def behavior():
        raise error
        yield  # gerador: o erro sai no primeiro pedaço, como no SDK
    return behavior


# ---------- _classify: só o 404 troca de modelo ----------

def _client_error(code: int, status: str, message: str) -> errors.ClientError:
    return errors.ClientError(code, {"error": {"code": code, "status": status, "message": message}})


@pytest.mark.parametrize("error, expected", [
    (_client_error(404, "NOT_FOUND", "models/x is not found for API version v1beta"), "model_unavailable"),
    (_client_error(403, "PERMISSION_DENIED", "The caller does not have permission"), "forbidden"),
    (_client_error(403, "PERMISSION_DENIED", "Generative Language API has not been used in project"), "forbidden"),
    (_client_error(400, "INVALID_ARGUMENT", "Function response not found for call"), "unknown"),
    (_client_error(400, "INVALID_ARGUMENT", "missing permission field in schema"), "unknown"),
])
def test_classify_so_404_e_modelo_indisponivel(error, expected):
    assert _classify(error, "gemini-3.6-flash").code == expected


def test_403_vira_erro_sem_trocar_nem_gravar_o_modelo(files, robin_api, events):
    files.env.write_text("GEMINI_MODEL=gemini-3.8-flash\n", encoding="utf-8")
    robin = robin_api()
    requests = Requests(robin._gemini)
    requests.script(_raise(_client_error(403, "PERMISSION_DENIED", "The caller does not have permission")))

    result = robin.send_message("oi")
    error = events.wait("onError", result["gen_id"])

    assert error["code"] == "forbidden"
    assert not events.of("onModelChanged") and not events.of("onRetry")
    assert len(requests.calls) == 1
    assert robin.get_state()["model"] == "gemini-3.8-flash"
    assert dotenv_values(files.env) == {"GEMINI_MODEL": "gemini-3.8-flash"}


def test_robin_api_usa_o_brain_json_da_pasta_de_dados(files, robin_api, events):
    robin = robin_api()
    result = robin.send_message("lembre de comprar uva")
    events.wait("onDone", result["gen_id"])
    assert files.brain.exists()
    assert "comprar uva" in files.brain.read_text(encoding="utf-8")
