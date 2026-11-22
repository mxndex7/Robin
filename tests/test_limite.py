"""Limitador local (pedidos por minuto por modelo), cota diária do 429 e a troca temporária para o
modelo de reserva que expira sozinha."""

import time

from conftest import send_and_wait
from google.genai import errors

from robin import gemini, ratelimit
from robin.config import DEFAULT_MODEL, FALLBACK_MODEL
from robin.mock import MockGeminiService
from robin.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _daily_quota_error(model=DEFAULT_MODEL, retry="3600s"):
    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": f"You exceeded your current quota. model: {model}",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20",
            }]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
        ],
    }})


def _raising(error):
    def behavior():
        raise error
        yield  # gerador: o erro sai no primeiro pedaço, como no SDK
    return behavior


# ---------- RateLimiter ----------

def test_limitador_conta_por_modelo_numa_janela_de_60s():
    clock = Clock()
    limiter = RateLimiter(2, clock=clock)
    limiter.record("a")
    clock.now += 10
    limiter.record("a")
    assert limiter.delay("a") == 50  # a vaga volta quando o 1º pedido sai da janela
    assert limiter.delay("b") == 0  # outro modelo, outra cota
    clock.now += 50
    assert limiter.delay("a") == 0
    assert RateLimiter(0).delay("a") == 0  # 0 desliga


def test_espera_a_vaga_em_vez_de_tomar_429(memory, events, brain, fast, monkeypatch):
    monkeypatch.setattr(ratelimit, "WINDOW_SECONDS", 0.6)
    service = MockGeminiService(emit=events, model=DEFAULT_MODEL, store=brain, rate_limit=1)
    try:
        send_and_wait(service, events, "oi", 1)
        start = time.monotonic()
        send_and_wait(service, events, "oi de novo", 2)
        assert time.monotonic() - start >= 0.4  # esperou a vaga no mesmo modelo
        assert events.of("onWaiting", 2) and events.of("onWaiting", 2)[0]["seconds"] == 1
        assert not events.of("onModelChanged") and not events.of("onError")
    finally:
        service.clear()


def test_espera_longa_usa_a_reserva_com_vaga(memory, events, brain, fast, monkeypatch):
    monkeypatch.setattr(ratelimit, "WINDOW_SECONDS", 30.0)
    monkeypatch.setattr(gemini, "RATE_WAIT_MAX", 5.0)
    service = MockGeminiService(emit=events, model=DEFAULT_MODEL, store=brain, rate_limit=1)
    try:
        send_and_wait(service, events, "oi", 1)
        send_and_wait(service, events, "oi de novo", 2)
        change = events.of("onModelChanged")[0]
        assert (change["model"], change["reason"], change["cause"]) == (FALLBACK_MODEL, "temporary", "rate_limit")
        assert not events.of("onWaiting")
    finally:
        service.clear()


# ---------- cota diária ----------

def test_cota_diaria_vai_direto_para_a_reserva_ate_ela_voltar(service, events, requests, fast):
    requests.script(_raising(_daily_quota_error()))
    send_and_wait(service, events, "oi", 1)

    assert not events.of("onRetry")  # nova tentativa não adiantaria
    change = events.of("onModelChanged")[0]
    assert change["cause"] == "daily_quota" and change["expires_in"] == 3600  # o retryDelay, maior que 5 min
    assert [call["model"] for call in requests.calls] == [DEFAULT_MODEL, FALLBACK_MODEL]


def test_cota_diaria_da_reserva_vira_erro_claro(service, events, requests, fast):
    service.set_model(FALLBACK_MODEL)
    requests.script(_raising(_daily_quota_error(FALLBACK_MODEL, "45000s")))
    service.send("oi", 1)
    error = events.wait("onError", 1)

    assert "cota diária" in error["message"] and "20 pedidos por dia" in error["message"]
    assert "Volta em cerca de 12h" in error["message"]
    assert len(requests.calls) == 1 and not events.of("onRetry")


# ---------- a troca temporária expira ----------

def _fall_back(service, events, requests):
    requests.script(_raising(_daily_quota_error(retry="1s")))  # expira em 5 min (o mínimo)
    send_and_wait(service, events, "oi", 1)
    assert service.model == FALLBACK_MODEL


def test_troca_temporaria_volta_ao_modelo_escolhido(service, events, requests, fast):
    _fall_back(service, events, requests)
    send_and_wait(service, events, "oi", 2)
    assert requests.calls[-1]["model"] == FALLBACK_MODEL  # ainda dentro dos 5 minutos

    service._fallback_until = time.monotonic() - 1  # os 5 minutos passaram
    send_and_wait(service, events, "oi", 3)
    assert requests.calls[-1]["model"] == DEFAULT_MODEL and service.model == DEFAULT_MODEL
    assert events.of("onModelChanged")[-1] == {"model": DEFAULT_MODEL, "previous": FALLBACK_MODEL, "reason": "restored"}


def test_escolher_modelo_cancela_a_volta(service, events, requests, fast):
    _fall_back(service, events, requests)
    service.set_model("gemini-3.8-flash")
    service._fallback_until = time.monotonic() - 1
    send_and_wait(service, events, "oi", 2)
    assert requests.calls[-1]["model"] == "gemini-3.8-flash"
    assert events.of("onModelChanged")[-1]["reason"] == "temporary"  # nenhum "restored"
