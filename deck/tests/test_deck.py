from io import BytesIO

from deck.hardware import image_reports, key_states
from deck.model import cards, load_settings, valid_chat_url
from deck.render import jpeg_key, preview


def test_fifteen_keys_and_actual_reported_week_only():
    status = {"codex": {"quota": {"buckets": [{"id": "codex", "windows": [
        {"duration_mins": 240, "remaining_percent": 99},
        {"duration_mins": 10080, "remaining_percent": 68}]}]}},
        "device": {"connected_recently": True}, "voice": {"state": "idle"}}
    output = cards(status, load_settings(__import__("pathlib").Path("nonexistent")))
    assert len(output) == 15
    assert output[6].detail == "68% left"
    assert output[12].action == "talk"
    assert preview(output).size == (480, 296)
    status["codex"]["quota"]["buckets"][0]["windows"].pop()
    assert cards(status, {"projects": [], "chats": []})[6].detail == "Not reported"
    assert cards({}, {"projects": [], "chats": []})[10].detail == "Unavailable"


def test_hid_image_reports_and_key_events():
    jpeg = jpeg_key(cards({}, {"projects": [], "chats": []})[0])
    assert BytesIO(jpeg).getvalue()[:2] == b"\xff\xd8"
    reports = image_reports(0, jpeg)
    assert all(len(report) == 1024 and report[:3] == b"\x02\x07\x00" for report in reports)
    assert reports[-1][3] == 1
    assert key_states(bytes([1, 0, 15, 0] + [0]*12 + [1] + [0]*2))[12]
    assert key_states(b"garbage") is None


def test_saved_chat_links_are_restricted():
    assert valid_chat_url("https://chatgpt.com/c/abc")
    assert not valid_chat_url("https://evil.example/c/abc")
    assert not valid_chat_url("https://chatgpt.com.evil.example/c/abc")
