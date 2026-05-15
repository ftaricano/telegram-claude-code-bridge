from src.bot.orchestrator import MessageOrchestrator
from src.bot.utils.formatting import FormattedMessage


def test_zero_delivery_telemetry_includes_original_format_and_failed_hop():
    telemetry = MessageOrchestrator._build_zero_delivery_telemetry(
        original_content="**hello**",
        formatted_messages=[FormattedMessage("", parse_mode="HTML")],
        messages_attempted=0,
        send_failures=0,
    )

    assert telemetry == {
        "original_content_length": len("**hello**"),
        "parse_mode_used": "HTML",
        "formatted_messages_count": 1,
        "failed_hop": "format",
    }


def test_zero_delivery_telemetry_identifies_send_hop_after_send_failures():
    telemetry = MessageOrchestrator._build_zero_delivery_telemetry(
        original_content="deliver me",
        formatted_messages=[FormattedMessage("deliver me", parse_mode=None)],
        messages_attempted=1,
        send_failures=1,
    )

    assert telemetry["original_content_length"] == len("deliver me")
    assert telemetry["parse_mode_used"] is None
    assert telemetry["formatted_messages_count"] == 1
    assert telemetry["failed_hop"] == "send"
