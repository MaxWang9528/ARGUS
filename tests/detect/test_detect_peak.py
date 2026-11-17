"""Peak detector maths on made-up chat series (no clock, no sleeping)."""

from __future__ import annotations

import asyncio
import math
import random

from argus.core.config import Detection
from argus.core.events import Topic
from argus.core.models import ChatMessage
from argus.detect.peak import ChatPeakDetector, PeakTracker, is_counted

T0 = 1_800_000_000.0
DET = Detection()


def chat(rates: list[float], *, seed: int = 1, users: int = 300, text: str = "LUL",
         noisy: bool = True, user_for=None) -> list[ChatMessage]:
    """Messages for a series of per-second rates, starting at T0."""
    rng = random.Random(seed)
    out = []
    for sec, rate in enumerate(rates):
        n = max(0, round(rng.gauss(rate, math.sqrt(rate)))) if noisy else round(rate)
        for ts in sorted(T0 + sec + rng.random() for _ in range(n)):
            uid = user_for(sec, rng) if user_for else str(rng.randrange(users))
            out.append(ChatMessage(streamer="alpha", ts=ts, user_id=uid, user=f"user{uid}", text=text))
    return out


def run(messages: list[ChatMessage], duration_s: float, det: Detection = DET):
    """Drive one tracker bin by bin. Returns (samples, peaks)."""
    tr = PeakTracker("alpha")
    samples, peaks = [], []
    msgs = sorted(messages, key=lambda m: m.ts)
    i = 0
    for sec in range(1, int(duration_s) + 1):
        now = T0 + sec
        while i < len(msgs) and msgs[i].ts < now:
            tr.feed(msgs[i], det)
            i += 1
        s, p = tr.tick(now, det)
        samples += s
        peaks += p
    return samples, peaks


def spike(base: float, duration: int, spikes: list[tuple[int, int, float]]) -> list[float]:
    rates = [base] * duration
    for at, length, rate in spikes:
        for s in range(at, at + length):
            rates[s] = rate
    return rates


def test_flat_chat_gives_no_peaks():
    samples, peaks = run(chat([4.0] * 900), 900)
    assert peaks == []
    assert len(samples) == 900
    assert all(s.rate < s.threshold for s in samples[120:])


def test_slow_ramp_gives_no_peaks():
    rates = [4.0 + 36.0 * s / 1800 for s in range(1800)]  # 4 -> 40 msg/s over 30 minutes
    _, peaks = run(chat(rates), 1800)
    assert peaks == []


def test_sharp_spike_gives_one_peak():
    samples, peaks = run(chat(spike(4.0, 600, [(400, 10, 30.0)])), 600)
    assert len(peaks) == 1
    p = peaks[0]
    assert p.streamer == "alpha" and p.source == "auto"
    assert T0 + 394 <= p.ts_start <= T0 + 402  # noise just before the spike can join the rise
    assert T0 + 400 <= p.ts_peak <= T0 + 412
    assert p.ts_peak <= p.ts_end <= T0 + 420
    assert 4 < p.score < 12
    # The baseline stays put during the peak and the threshold tells what a peak needs.
    during = [s for s in samples if T0 + 402 <= s.ts <= T0 + 410]
    assert all(3 < s.baseline < 5 for s in during)
    assert all(abs(s.threshold - max(s.baseline * 3, 2.0)) < 3 for s in during)


def test_two_spikes_inside_cooldown_give_one_peak():
    # cooldown_s = 45: the first peak ends around +415, the second starts at +440
    _, peaks = run(chat(spike(4.0, 700, [(400, 10, 30.0), (440, 10, 30.0)])), 700)
    assert len(peaks) == 1
    # ... but once the cooldown has passed the second one counts.
    _, peaks = run(chat(spike(4.0, 800, [(400, 10, 30.0), (600, 10, 30.0)])), 800)
    assert len(peaks) == 2


def test_nothing_fires_during_warmup():
    _, peaks = run(chat(spike(4.0, 300, [(20, 10, 40.0)])), 300)
    assert peaks == []


def test_silent_chat_with_one_message_gives_no_peaks():
    msg = ChatMessage(streamer="alpha", ts=T0 + 200.5, user_id="1", user="someone", text="hi")
    samples, peaks = run([msg], 400)
    assert peaks == []
    assert all(math.isfinite(s.z) for s in samples)
    assert max(s.z for s in samples) < 10
    assert all(s.threshold >= DET.min_msgs_per_sec for s in samples)


def test_spike_from_a_single_user_gives_no_peaks():
    def spammer(sec, rng):
        return "666" if 400 <= sec < 415 else str(rng.randrange(300))

    # Busy chat: one account floods 30 msg/s.
    _, peaks = run(chat(spike(4.0, 600, [(400, 15, 34.0)]), user_for=spammer), 600)
    assert peaks == []
    # Quiet chat: one account is nearly the only one talking.
    _, peaks = run(chat(spike(0.3, 600, [(400, 15, 6.0)]), user_for=spammer), 600)
    assert peaks == []


def test_too_few_unique_chatters_gives_no_peaks():
    _, peaks = run(chat(spike(0.2, 600, [(400, 10, 8.0)]), users=2), 600)  # min_unique_chatters = 3
    assert peaks == []


def test_bang_commands_are_ignored():
    base = chat([4.0] * 600)
    commands = [m for m in chat(spike(0.0, 600, [(400, 10, 30.0)]), seed=2, text="!drop")]
    _, peaks = run(base + commands, 600)
    assert peaks == []
    # The same messages without the "!" are a peak.
    plain = [m.model_copy(update={"text": "drop"}) for m in commands]
    _, peaks = run(base + plain, 600)
    assert len(peaks) == 1


def test_is_counted_filters_commands_and_ignored_users():
    det = Detection(ignore_users=["NightBot", "streamelements"])
    assert is_counted(ChatMessage(streamer="a", ts=0, user="viewer", text="hello"), det)
    assert not is_counted(ChatMessage(streamer="a", ts=0, user="viewer", text="!uptime"), det)
    assert not is_counted(ChatMessage(streamer="a", ts=0, user="Nightbot", text="hi"), det)
    assert not is_counted(ChatMessage(streamer="a", ts=0, user="StreamElements", text="hi"), det)


def test_peak_is_cut_at_max_clip_s():
    det = Detection(max_clip_s=20)
    _, peaks = run(chat(spike(4.0, 600, [(400, 60, 30.0)])), 600, det)
    assert len(peaks) == 1
    assert peaks[0].ts_end - peaks[0].ts_start <= 20 + 1e-6


def test_threshold_follows_config_changes():
    msgs = chat([4.0] * 200)
    tr = PeakTracker("alpha")
    i = 0
    for sec in range(1, 201):
        while i < len(msgs) and msgs[i].ts < T0 + sec:
            tr.feed(msgs[i], DET)
            i += 1
        s, _ = tr.tick(T0 + sec, DET)
    before = s[-1].threshold
    s, _ = tr.tick(T0 + 201, Detection(multiplier=6.0))
    assert s[-1].threshold > before * 1.7


def test_one_tick_closes_every_ended_bin():
    tr = PeakTracker("alpha")
    tr.tick(T0, DET)
    samples, _ = tr.tick(T0 + 10, DET)
    assert [s.ts - T0 for s in samples] == [float(x) for x in range(1, 11)]
    assert all(s.rate == 0 for s in samples)


def test_detector_publishes_a_rate_sample_for_every_enabled_streamer(store, bus):
    det = ChatPeakDetector(store=store, bus=bus)
    sub = bus.subscribe(Topic.RATE)
    det.handle(ChatMessage(streamer="Alpha", ts=T0 + 0.5, user_id="1", user="x", text="hi"))
    det.handle(ChatMessage(streamer="nobody", ts=T0 + 0.5, user_id="1", user="x", text="hi"))
    det.tick(T0)
    det.tick(T0 + 1)
    got = []
    while not sub.queue.empty():
        got.append(sub.queue.get_nowait()[1])
    assert {s.streamer for s in got} == {"alpha", "beta"}
    alpha = [s for s in got if s.streamer == "alpha" and s.ts == T0 + 1]
    assert alpha and alpha[0].rate == 1 / DET.smooth_s
    assert "nobody" not in det.trackers


async def test_detector_run_ticks_on_the_wall_clock(store, bus):
    store.update({"detection": {"bin_s": 0.1, "smooth_s": 0.5}})
    det = ChatPeakDetector(store=store, bus=bus)
    sub = bus.subscribe(Topic.RATE)
    task = asyncio.create_task(det.run())
    await asyncio.sleep(0.05)
    import time
    for _ in range(5):
        bus.publish(Topic.CHAT, ChatMessage(streamer="alpha", ts=time.time(), user_id="1", user="x"))
    await asyncio.sleep(0.6)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    got = []
    while not sub.queue.empty():
        got.append(sub.queue.get_nowait()[1])
    assert len([s for s in got if s.streamer == "beta"]) >= 4
    assert max(s.rate for s in got if s.streamer == "alpha") > 0
    assert len(bus._subs) == 1  # the detector closed its subscription


def test_silence_then_steady_chat_gives_no_peaks():
    # Offline (or disconnected) for 10+ minutes, then the stream goes live at 4 msg/s.
    _, peaks = run(chat([0.0] * 700 + [4.0] * 900), 1600)
    assert peaks == []


def test_a_step_to_a_new_chat_level_gives_at_most_one_peak():
    for rates in ([0.05] * 700 + [4.0] * 900, [1.0] * 700 + [4.0] * 900):
        _, peaks = run(chat(rates), len(rates))
        assert len(peaks) <= 1
    # ... and a spike on the new level is still found.
    rates = [1.0] * 700 + spike(4.0, 900, [(400, 10, 30.0)])
    _, peaks = run(chat(rates), len(rates))
    assert any(abs(p.ts_start - (T0 + 1100)) <= 6 for p in peaks)


def test_bad_bin_s_does_not_crash():
    # Config validation now rejects bin_s <= 0; the detector's own guard stays as a backstop.
    samples, _ = run(chat([4.0] * 30), 30, Detection().model_copy(update={"bin_s": 0}))
    assert len(samples) == 30


# ---- sustained chatter (min_sustain_s) ----------------------------------------------
# Steady background chat, so the measured length is the burst's own, not stretched by
# random noise just before it. These test the rule itself at min_sustain_s = 10 (the
# default is 6, which lets a one-second burst of many people through).
SUSTAIN10 = Detection(min_sustain_s=10)

def chat_steady(rates):
    return chat(rates, noisy=False)


def test_one_second_burst_does_not_clip():
    # 80 messages from many people inside one second, then back to normal: smoothing
    # stretches it to a ~6 s bump, shorter than min_sustain_s (10 s).
    _, peaks = run(chat_steady(spike(4.0, 600, [(400, 1, 80.0)])), 600, SUSTAIN10)
    assert peaks == []


def test_one_second_burst_clips_without_the_sustain_rule():
    _, peaks = run(chat_steady(spike(4.0, 600, [(400, 1, 80.0)])), 600, Detection(min_sustain_s=0))
    assert len(peaks) == 1


def test_two_quick_bursts_are_not_sustained_chatter():
    _, peaks = run(chat_steady(spike(4.0, 600, [(400, 1, 80.0), (402, 1, 80.0)])), 600, SUSTAIN10)
    assert peaks == []


def test_batched_delivery_still_counts_as_sustained():
    # Twitch delivers busy chats in bursts with empty seconds between them.
    rates = spike(4.0, 600, [(400 + 2 * k, 1, 60.0) for k in range(8)])
    _, peaks = run(chat_steady(rates), 600)
    assert len(peaks) == 1
    assert T0 + 398 <= peaks[0].ts_start <= T0 + 402


def test_a_spike_does_not_start_the_cooldown():
    # a lone spike, then a real peak 20 s later (inside cooldown_s): still clipped
    rates = spike(4.0, 600, [(400, 1, 80.0), (420, 12, 30.0)])
    _, peaks = run(chat_steady(rates), 600, SUSTAIN10)
    assert len(peaks) == 1 and peaks[0].ts_start >= T0 + 410


def test_longer_sustain_needs_longer_chatter():
    det = Detection(min_sustain_s=20)
    assert run(chat_steady(spike(4.0, 600, [(400, 8, 30.0)])), 600, det)[1] == []
    assert len(run(chat_steady(spike(4.0, 600, [(400, 25, 30.0)])), 600, det)[1]) == 1


# ---- stalls, late messages, rank ---------------------------------------------------------
def test_stall_then_flush_by_receive_time_gives_no_peak():
    # Busy chat (8 msg/s); the connection stalls for 30 s, then the backlog lands in one
    # second (receive times). That burst is not a moment.
    msgs = [m for m in chat([8.0] * 700) if not T0 + 400 <= m.ts < T0 + 430]
    backlog = [m.model_copy(update={"ts": T0 + 430 + (m.ts - T0 - 400) / 300})
               for m in chat([8.0] * 700, seed=2) if T0 + 400 <= m.ts < T0 + 430]
    _, peaks = run(msgs + backlog, 700)
    assert peaks == []
    # ... while the same chat without the stall still finds a real spike afterwards.
    _, peaks = run(msgs + backlog + chat(spike(0.0, 700, [(600, 12, 50.0)]), seed=3), 700)
    assert len(peaks) == 1 and peaks[0].ts_start > T0 + 590


def test_gap_bins_stay_out_of_the_baseline():
    msgs = [m for m in chat([8.0] * 700) if not T0 + 400 <= m.ts < T0 + 460]
    samples, _ = run(msgs, 700)
    after = [s for s in samples if T0 + 480 <= s.ts <= T0 + 500]
    assert all(s.baseline > 6 for s in after)  # 60 s of zeros did not drag it down


def test_messages_from_long_closed_bins_are_dropped():
    tr = PeakTracker("alpha")
    tr.tick(T0 + 100, DET)
    old = ChatMessage(streamer="alpha", ts=T0 + 60, user_id="1", user="x", text="hi")
    recent = ChatMessage(streamer="alpha", ts=T0 + 98.5, user_id="2", user="y", text="hi")
    assert tr.feed(old, DET) is False and tr.dropped_late == 1
    assert tr.feed(recent, DET) is True  # a little late: still counted


def test_laughter_outranks_a_vote_of_the_same_size():
    def peak_with(text):
        rates = spike(4.0, 600, [(400, 12, 30.0)])
        base = [m for m in chat(rates) if not T0 + 400 <= m.ts < T0 + 412]
        burst = [m for m in chat(rates, text=text) if T0 + 400 <= m.ts < T0 + 412]
        _, peaks = run(base + burst, 600)
        assert len(peaks) == 1
        return peaks[0]

    laugh, vote = peak_with("LMAOOO no way"), peak_with("1")
    assert abs(laugh.score - vote.score) < 0.5
    assert laugh.reaction > 0.6 and vote.reaction < 0.2  # the background chat says LUL
    assert laugh.rank > 2.5 * vote.rank
    assert laugh.chatters >= 50


def test_text_classifiers():
    from argus.detect.peak import ANSWER, REACTION
    for t in ["LMAOOO", "lol", "KEKW", "no wayyy", "WTF", "???", "HAHAHA", "OMEGALUL", "clip it", "💀"]:
        assert REACTION.search(t), t
    for t in ["what addon is this", "hello there friend", "1 more game", "lollipop"]:
        assert not REACTION.search(t), t
    for t in ["1", "yes", "NO", "o7", "gg", "bye!", "wsg"]:
        assert ANSWER.match(t), t
    for t in ["no shot", "gg that was insane", "123"]:
        assert not ANSWER.match(t), t


def test_slow_climb_past_the_threshold_then_back_gives_no_peak():
    # Slow chat (1 msg/s) creeps up to 5 msg/s over 90 s, holds, then drops back. Over
    # the slow 5-minute baseline that clears every threshold, but there was never a jump.
    rates = [1.0] * 400 + [1.0 + 4.0 * s / 90 for s in range(90)] + [5.0] * 20 + [1.0] * 290
    _, peaks = run(chat(rates, noisy=False), 800)
    assert peaks == []
    # The same height reached in one jump is a peak.
    _, peaks = run(chat([1.0] * 400 + [5.0] * 15 + [1.0] * 385, noisy=False), 800)
    assert len(peaks) == 1


def test_chat_still_climbing_long_after_the_jump_is_not_a_peak():
    # A jump, but chat keeps building for another 40 s: a climb to a new level.
    rates = [1.0] * 400 + [4.0] * 5 + [4.0 + 0.2 * s for s in range(40)] + [1.0] * 355
    _, peaks = run(chat(rates, noisy=False), 800)
    assert peaks == []
    # A reaction peaks within seconds of the jump, then fades.
    rates = [1.0] * 400 + [8.0] * 8 + [4.0] * 10 + [1.0] * 382
    _, peaks = run(chat(rates, noisy=False), 800)
    assert len(peaks) == 1
