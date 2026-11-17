from __future__ import annotations

import pytest

from livewatcher.capture.buffer import BufferIndex, Gap
from livewatcher.core.interfaces import WindowUnavailable

T0 = 1_800_000_000.0


def feed(index: BufferIndex, n: int, *, start: int = 0, dur: float = 2.0, lag: float = 0.3,
         date0: float = T0, burst_until: int = -1) -> None:
    """Add segments ``start..start+n-1``. Segment i starts at date0 + i*dur on Twitch's
    clock and arrives ``lag`` after it ends, except burst ones (all at once, late)."""
    for i in range(start, start + n):
        date = date0 + i * dur
        recv = date + dur + lag
        if i <= burst_until:
            recv = date0 + (burst_until + 1) * dur + 5.0  # delivered in one burst, late
        index.apply({"t": "seg", "num": 100 + i, "file": f"seg_{i:06d}.ts", "date": date,
                     "dur": dur, "recv_end": recv, "bytes": 1000})


def test_time_mapping_uses_twitch_dates_and_min_lag() -> None:
    idx = BufferIndex()
    feed(idx, 10, lag=0.3)
    assert idx.clock_offset == pytest.approx(0.3)
    assert idx.start == pytest.approx(T0 + 0.3)
    assert idx.end == pytest.approx(T0 + 20 + 0.3)


def test_startup_burst_does_not_shift_the_mapping() -> None:
    """The first segments arrive in a burst seconds late; their dates still place them."""
    idx = BufferIndex()
    feed(idx, 4, burst_until=3)          # 4 old segments delivered together, late
    feed(idx, 6, start=4, lag=0.3)       # then live
    assert idx.clock_offset == pytest.approx(0.3)
    segs, off = idx.select(T0 + 0.3 + 3.0, T0 + 0.3 + 9.0)
    assert [s.num for s in segs] == [101, 102, 103, 104]
    assert off == pytest.approx(1.0)


def test_local_clock_error_is_absorbed() -> None:
    idx = BufferIndex()
    feed(idx, 5, lag=-4.0)               # local clock 4.3 s behind Twitch's
    assert idx.clock_offset == pytest.approx(-4.0)
    assert idx.to_chat(T0) == pytest.approx(T0 - 4.0)


def test_select_bounds_and_offsets() -> None:
    idx = BufferIndex()
    feed(idx, 6, lag=0.0)
    segs, off = idx.select(T0 + 3.0, T0 + 8.0)
    assert [s.file for s in segs] == ["seg_000001.ts", "seg_000002.ts", "seg_000003.ts"]
    assert off == pytest.approx(1.0)
    segs, off = idx.select(T0, T0 + 12.0)
    assert len(segs) == 6 and off == 0.0
    with pytest.raises(WindowUnavailable, match="before the buffer"):
        idx.select(T0 - 1.0, T0 + 4.0)
    with pytest.raises(WindowUnavailable, match="after the buffer"):
        idx.select(T0 + 8.0, T0 + 13.0)
    with pytest.raises(WindowUnavailable):
        idx.select(T0 + 5.0, T0 + 5.0)


def test_ad_gap_is_recorded_and_blocks_overlapping_windows() -> None:
    idx = BufferIndex()
    feed(idx, 3, lag=0.0)                                   # 0..6
    for i in range(3, 6):                                   # 6..12 filtered as ads
        idx.apply({"t": "skip", "num": 100 + i, "date": T0 + 2 * i, "dur": 2.0, "reason": "ad"})
    result = None
    for i in range(6, 9):                                   # 12..18
        r = idx.apply({"t": "seg", "num": 100 + i, "file": f"seg_{i:06d}.ts", "date": T0 + 2 * i,
                       "dur": 2.0, "recv_end": T0 + 2 * i + 2, "bytes": 1})
        result = result or (r if isinstance(r, Gap) else None)
    assert isinstance(result, Gap)
    assert (result.start, result.end, result.reason) == (T0 + 6, T0 + 12, "ad")
    assert idx.gaps_chat() == [(T0 + 6, T0 + 12, "ad")]
    with pytest.raises(WindowUnavailable, match="gap"):
        idx.select(T0 + 4.0, T0 + 14.0)
    idx.select(T0 + 1.0, T0 + 5.0)                           # before the gap: fine
    idx.select(T0 + 12.5, T0 + 17.0)                         # after the gap: fine
    text = idx.playlist_text()
    assert text.count("#EXT-X-DISCONTINUITY\n") == 1


def test_missing_download_leaves_a_gap() -> None:
    idx = BufferIndex()
    feed(idx, 2, lag=0.0)
    feed(idx, 2, start=3, lag=0.0)                          # segment 2 never arrived
    assert len(idx.gaps) == 1 and idx.gaps[0].reason == "missing"


def test_trim_keeps_about_keep_s_and_drops_old_gaps() -> None:
    idx = BufferIndex()
    feed(idx, 3, lag=0.0)
    feed(idx, 100, start=4, lag=0.0)                         # gap at 6..8
    removed = idx.trim(30.0)
    assert removed and removed[0].num == 100
    span = idx.segments[-1].end - idx.segments[0].date
    assert 30.0 <= span < 32.0
    assert idx.gaps == []
    assert not idx.segments[0].gap_reason
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in idx.playlist_text()


def test_backwards_dates_reset_the_index() -> None:
    idx = BufferIndex()
    feed(idx, 5, lag=0.0)
    feed(idx, 2, start=0, lag=0.0, date0=T0 - 500)          # encoder restarted
    assert len(idx.segments) == 2 and idx.segments[0].date == T0 - 500


def test_meta_update_fixes_a_prefetch_estimate() -> None:
    idx = BufferIndex()
    feed(idx, 3, lag=0.0)
    idx.apply({"t": "meta", "num": 102, "date": T0 + 4.0, "dur": 1.5})
    assert idx.segments[-1].duration == 1.5
    assert idx.clock_offset == pytest.approx(0.0)


def test_missing_date_is_chained() -> None:
    idx = BufferIndex()
    feed(idx, 2, lag=0.0)
    idx.apply({"t": "seg", "num": 102, "file": "x.ts", "date": None, "dur": 2.0,
               "recv_end": T0 + 6.0})
    assert idx.segments[-1].date == T0 + 4.0


def test_playlist_parses_with_streamlink() -> None:
    from streamlink.stream.hls.m3u8 import parse_m3u8

    idx = BufferIndex()
    feed(idx, 5, lag=0.25)
    m3u8 = parse_m3u8(idx.playlist_text(), base_uri="http://localhost/live/live.m3u8")
    assert [s.uri.rsplit("/", 1)[-1] for s in m3u8.segments] == [f"seg_{i:06d}.ts" for i in range(5)]
    assert m3u8.media_sequence == 0
    assert m3u8.segments[0].date.timestamp() == pytest.approx(T0 + 0.25, abs=0.002)
    assert all(s.duration == 2.0 for s in m3u8.segments)
    assert not m3u8.is_endlist


def test_skip_marks_a_gap_even_when_dates_touch() -> None:
    """A skipped segment is a hole even if the next date looks contiguous (estimated)."""
    idx = BufferIndex()
    feed(idx, 2, lag=0.0, dur=0.2)
    idx.apply({"t": "skip", "num": 102, "date": T0 + 0.4, "dur": 0.2, "reason": "ad"})
    idx.apply({"t": "seg", "num": 103, "file": "x.ts", "date": T0 + 0.4, "dur": 0.2, "recv_end": T0 + 0.6})
    assert [(g.reason, g.start) for g in idx.gaps] == [("ad", pytest.approx(T0 + 0.4))]
    with pytest.raises(WindowUnavailable, match="gap"):
        idx.select(T0 + 0.1, T0 + 0.5)


def test_meta_update_rejudges_a_date_jump() -> None:
    """A prefetch estimate that looked like a jump is not a gap once the real date arrives."""
    idx = BufferIndex()
    feed(idx, 3, lag=0.0)
    idx.apply({"t": "seg", "num": 103, "file": "p.ts", "date": T0 + 7.0, "dur": 2.0,
               "recv_end": T0 + 9.0, "prefetch": True})
    assert [g.reason for g in idx.gaps] == ["date jump"]
    idx.apply({"t": "meta", "num": 103, "date": T0 + 6.0, "dur": 2.0})
    assert idx.gaps == []
    idx.select(T0 + 5.0, T0 + 7.0)
