"""Replaying a recorded chat log through the detector."""

from __future__ import annotations

import argparse
import json

from livewatcher.core.config import ConfigStore
from livewatcher.detect import replay

T0 = 1_800_000_000.0


def test_replay_finds_exactly_the_planted_spike(sample_chat_log, capsys):
    args = argparse.Namespace(file=str(sample_chat_log), streamer=None, store=ConfigStore())
    assert replay.main(args) == 0
    out = capsys.readouterr().out
    assert "1 peak(s)" in out

    msgs = list(replay.read_chat_log(sample_chat_log))
    peaks, samples = replay.replay(msgs, ConfigStore())
    assert len(peaks) == 1
    p = peaks[0]
    assert abs(p.ts_start - (T0 + 400)) <= 3
    assert T0 + 400 <= p.ts_peak <= T0 + 411
    assert T0 + 405 <= p.ts_end <= T0 + 420
    assert p.score > 3
    assert 590 <= len(samples["alpha"]) <= 620


def test_replay_filters_by_streamer(sample_chat_log, tmp_path, capsys):
    other = tmp_path / "mixed.jsonl"
    lines = sample_chat_log.read_text().splitlines()
    beta = [json.dumps({**json.loads(line), "streamer": "beta"}) for line in lines]
    other.write_text("\n".join(lines + beta) + "\nnot json\n")

    peaks, _ = replay.replay(sorted(replay.read_chat_log(other), key=lambda m: m.ts), ConfigStore())
    assert sorted(p.streamer for p in peaks) == ["alpha", "beta"]

    args = argparse.Namespace(file=str(other), streamer="BETA", store=ConfigStore())
    assert replay.main(args) == 0
    out = capsys.readouterr().out
    assert "1 peak(s)" in out and "beta" in out and "alpha" not in out


def test_replay_of_empty_log(tmp_path, capsys):
    f = tmp_path / "empty.jsonl"
    f.write_text("")
    assert replay.main(argparse.Namespace(file=str(f), streamer=None, store=ConfigStore())) == 0
    assert "no chat messages" in capsys.readouterr().out
