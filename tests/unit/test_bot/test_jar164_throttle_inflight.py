"""F3-B: throttle 300ms + mutex inFlight (JAR-164)."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from src.bot.utils.draft_streamer import DraftStreamer


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    bot.send_message_draft = AsyncMock(return_value=type("R", (), {"message_id": 1})())
    return bot


@pytest.fixture
def streamer(mock_bot):
    return DraftStreamer(
        bot=mock_bot,
        chat_id=42,
        draft_id=1,
        throttle_interval=0.3,
        retry_attempts=1,
    )


@pytest.mark.asyncio
async def test_300ms_throttle_coalesces_rapid_appends(streamer, mock_bot):
    """5 appends rápidos (50ms apart) devem coalescer em poucas chamadas."""
    for i in range(5):
        await streamer.append_text(f"chunk{i} ")
        await asyncio.sleep(0.05)
    await streamer.flush()
    # Throttle 300ms — esperado coalesce em ≤3 chamadas (não 5)
    assert mock_bot.send_message_draft.call_count <= 3


@pytest.mark.asyncio
async def test_in_flight_lock_serializes_concurrent_flushes(streamer, mock_bot):
    """Mutex inFlight: chamada B espera A terminar antes de enviar."""
    timings = []

    async def slow_send(**kwargs):
        timings.append(("start", time.monotonic()))
        await asyncio.sleep(0.1)
        timings.append(("end", time.monotonic()))
        return type("R", (), {"message_id": 1})()

    mock_bot.send_message_draft.side_effect = slow_send

    await streamer.append_text("a")
    t1 = asyncio.create_task(streamer.flush())
    await streamer.append_text("b")
    t2 = asyncio.create_task(streamer.flush())
    await asyncio.gather(t1, t2)

    starts = [t for tag, t in timings if tag == "start"]
    ends = [t for tag, t in timings if tag == "end"]
    if len(starts) >= 2:
        # Se houve 2 sends, segundo só começou após primeiro terminar
        assert starts[1] >= ends[0] - 0.001


@pytest.mark.asyncio
async def test_default_throttle_interval_is_300ms():
    """Default deve ser 0.3s, não 1.5s."""
    # Settings carrega defaults; stream_draft_interval default = 0.3
    # (não força __init__ porque Settings requer env vars; testa constante)
    # Use o atributo de classe / inspecione Field default
    import inspect

    from src.config.settings import Settings

    src = inspect.getsource(Settings)
    assert "stream_draft_interval" in src
    # Default value check via descriptor
    field = Settings.model_fields["stream_draft_interval"]
    assert field.default == 0.3, f"Expected default 0.3, got {field.default}"
