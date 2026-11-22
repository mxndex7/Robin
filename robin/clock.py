"""Data e hora no fuso do usuário (America/Sao_Paulo) para o modelo e para as datas do cérebro."""

from datetime import datetime, timedelta, timezone, tzinfo

from .config import TIMEZONE

_WEEKDAYS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")


def _zone() -> tzinfo:
    """O Windows não traz a base de fusos do Python (pacote tzdata). Sem ela, UTC-3 fixo:
    o Brasil não tem horário de verão desde 2019."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(TIMEZONE)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, módulo ausente
        return timezone(timedelta(hours=-3), TIMEZONE)


ZONE = _zone()


def local_time(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZONE)


def now_text(moment: datetime | None = None) -> str:
    """Ex.: "sexta-feira, 30/10/2026 14:05 (America/Sao_Paulo, UTC-03:00)"."""
    local = local_time(moment or datetime.now(timezone.utc))
    offset = local.strftime("%z")
    return f"{_WEEKDAYS[local.weekday()]}, {local:%d/%m/%Y %H:%M} ({TIMEZONE}, UTC{offset[:3]}:{offset[3:]})"
