"""Chat log recorder and reader."""

from __future__ import annotations

import asyncio

import pytest

from livewatcher.chat.chatlog import ChatLogRecorder, chatlog_path, read_chat_log
from livewatcher.core.events import Topic
from livewatcher.core.models import ChatMessage

DAY1 = 1_800_000_000.0          # 2027-01-15 08:00 UTC
DAY2 = DAY1 + 16 * 3600 + 5     # the next UTC day


def test_path_uses_the_utc_date(tmp_path):
    assert chatlog_path(tmp_path, "Alpha", DAY1) == tmp_path / "chatlogs" / "alpha" / "2027-01-15.jsonl"
    assert chatlog_path(tmp_path, "alpha", DAY2).name == "2027-01-16.jsonl"


def test_write_and_read_back_with_day_rollover(store, bus):
    rec = ChatLogRecorder(store=store, bus=bus)
    msgs = [ChatMessage(streamer="alpha", ts=DAY1 + i, user_id=str(i), user=f"u{i}", text=f"m{i}")
            for i in range(3)]
    msgs.append(ChatMessage(streamer="alpha", ts=DAY2, user_id="9", user="late", text="next day"))
    msgs.append(ChatMessage(streamer="beta", ts=DAY1, user_id="5", user="b", text="other channel"))
    for m in msgs:
        rec.write(m)
    rec.close()
    data = store.get().data_dir
    assert list(read_chat_log(chatlog_path(data, "alpha", DAY1))) == msgs[:3]
    assert list(read_chat_log(chatlog_path(data, "alpha", DAY2))) == [msgs[3]]
    assert list(read_chat_log(chatlog_path(data, "beta", DAY1))) == [msgs[4]]


def test_reader_skips_blank_and_broken_lines(tmp_path):
    f = tmp_path / "log.jsonl"
    good = ChatMessage(streamer="alpha", ts=DAY1, user="a", text="ok")
    f.write_text("\n" + good.model_dump_json() + "\n{broken\n" + '{"ts": 1}\n' + good.model_dump_json())
    assert list(read_chat_log(f)) == [good, good]


async def test_recorder_run_appends_published_chat(store, bus):
    rec = ChatLogRecorder(store=store, bus=bus)
    task = asyncio.create_task(rec.run())
    await asyncio.sleep(0)
    for i in range(5):
        bus.publish(Topic.CHAT, ChatMessage(streamer="alpha", ts=DAY1 + i, user=f"u{i}", text="x"))
    path = chatlog_path(store.get().data_dir, "alpha", DAY1)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if path.exists() and len(path.read_text().splitlines()) == 5:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(list(read_chat_log(path))) == 5
    assert rec.written == 5
    assert not bus._subs
