import asyncio

from sashmo.speech import split_spoken_text
from sashmo.voice_stream import VoiceStreamManager


def test_split_spoken_text_preserves_long_answer():
    answer = "A detailed opening. " * 60
    parts = split_spoken_text(answer)
    assert len(parts) > 4
    assert " ".join(parts) == answer.strip()
    assert all(len(part) <= 180 for part in parts)


def test_stream_releases_each_segment_and_expires_after_final():
    async def fake_cedar(text):
        return text.encode()

    async def scenario():
        manager = VoiceStreamManager(fake_cedar)
        first, reply_id, more = await manager.start(["one", "two", "three"])
        assert first == b"one" and reply_id and more
        assert await manager.next(reply_id) == (b"two", True, 2, 3)
        assert await manager.next(reply_id) == (b"three", False, 3, 3)
        try:
            await manager.next(reply_id)
        except LookupError:
            pass
        else:
            raise AssertionError("completed reply was still available")

    asyncio.run(scenario())
