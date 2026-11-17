# Audio loudness / excitement signal (experiment)

Question: does audio level catch the moments chat misses, and does it reject chat
peaks that weren't moments? **Short answer: no, not on this data. Don't adopt it as a
detector or as a fusion term.** The cost is negligible, so it could stay as a cheap
debug trace or a tie-breaker. Details below.

## Method (`audio_signal.py`)

- Decode the buffered HLS segments' audio with ffmpeg: mono, 16 kHz, approximate
  K-weighting (high-pass 60 Hz + high shelf +4 dB at 1.5 kHz), f32 PCM. Live, this is
  one persistent ffmpeg per stream that gets each segment's bytes as it lands in the
  buffer (`SegmentDecoder`). MPEG-TS segments concatenate cleanly. Restart the
  decoder at a buffer gap.
- For each 1 s bin: momentary loudness (400 ms, max in the second), short-term (3 s),
  1–4 kHz "voice band" level, and spectral flux (32 ms frames). Short-term matches
  ffmpeg `ebur128` S with r = 0.99 and a bias of -0.2 dB.
- Baseline as in `argus/detect/peak.py`: a rolling 180 s median, with bins inside a
  peak left out. The spread is MAD x 1.4826 with a 1.5 dB floor, a higher 60 s median
  lifts the baseline, and there is a 60 s warm-up. The rise term is loudness now
  minus the median of the 10 s before (with a 2 s gap), in spread units.
- score = z_level + 0.5 z_voice + z_rise + 0.5 min(z_flux, 6). Thresholded events
  start at score > 5.5 and end below 1.5, with a 30 s cooldown. Top-N/h candidates
  are picked by ranked non-maximum suppression, 30 s apart.

## Evaluation (`evaluate.py`, `chat_replay.py`)

The dataset labels 30 s windows as moment, maybe or no: 18 / 46 / 74 windows over
2.8 h and 5 streams (READY set). A window's score is the maximum of the signal
inside it. The chat z comes from `argus.detect.replay.replay` and is read 4–8 s
later. Hit = a candidate within the window ±5 s. "No loud source" drops the windows
that a loudness ranker had proposed. That ranker is nearly this signal, so those
windows bias the test in its favour.

| AUC, moment vs no | all | no loud source | random windows only |
|---|---|---|---|
| audio score | 0.53 | 0.56 | 0.38 |
| audio level / rise only | 0.53 / 0.53 | 0.59 / 0.59 | 0.41 / 0.43 |
| chat z | **0.81** | **0.86** | **1.0** |
| fusion chat/2 + audio | 0.65 | 0.66 | 0.76 |
| fusion chat x (1 + audio/4) | 0.76 | 0.80 | 0.95 |

At about 10 candidates/h (moments hit / no-windows hit): audio 2/18 and 8/74, chat
10/18 and 9/74, union 5/18 and 9/74, boost 6/18 and 12/74.

Thresholded chat peaks (relaxed thresholds, 21/h) hit 11/18 moments and 32/74
no-windows. Dropping the peaks without an audio score of 3 or more nearby leaves 6/18
and 22/74, and a cut at 5.5 leaves 4/18 and 14/74. Precision stays at about
0.21–0.26, so the gate removes good peaks as often as bad ones. Adding audio events
to the chat peaks: +1 moment, +12 no-windows.

Chat-quiet moments: none of the 18 moments had a quiet chat. On the 15 chat-quiet
maybe windows against the chat-quiet no-windows, audio AUC = 0.53, which is chance.
Per stream, audio does about as well as chat on the two most shouty/gaming streams
(0.70 vs 0.70) and worse elsewhere (0.45–0.62 vs 0.63–0.76).

Why: these streams are compressed and limited by the broadcaster, so a yell is only a
few dB above talk. Loud stretches are mostly music, sponsor reads and game beds. The
labels favour funny lines and punchlines, which are not loud. The labels lean on
speech, so purely audio/visual game moments are under-represented, and there were no
IRL streams. The counts are small and there is one AI labeller.

## Cost (`bench.py`, 8 cores, no GPU)

12 streams fed 720p60 segments in real time:
- persistent ffmpeg pipe: 0.9 % of one core per stream (ffmpeg 0.64 %, Python 0.27 %),
  about 11 % of one core for all 12. Memory is about 64 MB RSS per ffmpeg plus 55 MB
  of Python.
- latency from a segment landing to its last second being scored: median 12 ms, p95
  21 ms. Start-up takes about 4 s while ffmpeg probes the stream. The signal is
  available about one segment (2 s) after the frame, well before chat reacts.
- one ffmpeg per segment instead: 3.7 % per stream, p95 97 ms.

## Run

```
uv venv venv && uv pip install --python venv/bin/python numpy
<app-venv>/bin/python -I chat_replay.py <repo> <stream>/chat.jsonl out/<stream>.chat.json --config config.toml
venv/bin/python -I evaluate.py <dataset> <cache> --chat-json-dir out --out results.json
venv/bin/python -I bench.py <dir of .ts segments> --streams 12 --segments 45
```
