# Audio event signal (experiment)

Can a pretrained sound-event model find moments that chat rate misses, or reject chat
peaks where nothing happened? Prototype only: nothing in `argus/` uses it.

## Model: YAMNet (TFLite, CPU)

YAMNet (MobileNet-v1 on AudioSet, 521 classes, Google, TF Hub `lite-model/yamnet/
classification/tflite/1`, 4 MB, float32) runs through `ai-edge-litert`. It was picked
because it is the cheapest of the candidates (about 1 ms of CPU per 0.975 s frame on one
thread, against tens of ms for PANNs CNN14 or BEATs and more for CLAP's audio tower) and
it has exactly the classes needed: Laughter, Giggle, Chuckle, Shout, Yell, Screaming,
Whoop, Gasp, Groan, Cheering, Applause, Crowd, Explosion, Gunshot, Boom. A bigger model was not tried. The class set itself, not tagging accuracy, seems to be
the limit (see results).

Weights and data are not in the repo. Get the model and class map from the official
sources:

```
curl -L -o yamnet.tflite "https://tfhub.dev/google/lite-model/yamnet/classification/tflite/1?lite-format=tflite"
curl -L -o yamnet_class_map.csv https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv
uv venv .venv && uv pip install --python .venv/bin/python numpy ai-edge-litert av
```

## Signal

- `scorer.py`: 16 kHz mono, one 0.975 s frame every 0.5 s, max-pooled into one row per
  second. `StreamScorer.feed_segment()` takes one HLS segment from the capture buffer
  (`argus/capture/buffer.py`, ~2 s MPEG-TS) with its chat-time start
  (`date + clock_offset`), keeps the leftover samples for the next segment and re-anchors
  on a hole. It lags real time by about one segment plus 1.5 s. Feeding segments gives the
  same rows as decoding the whole file at once (per-class correlation 0.96 to 1.0 on
  two streams; the only differences are AAC decoder edges).
- `excitement.py`: the classes are grouped into laugh, scream, gasp, cheer and bang, plus
  RMS loudness. Each group's log-score gets a per-stream causal baseline (trailing
  10 min median, MAD spread with a floor) and z is clipped to 0..8. Excitement is the
  weighted sum (laugh 1, scream 1, cheer 0.7, gasp 0.5, loud 0.5, bang 0.3; set by hand
  before looking at labels), a 3 s trailing mean, and candidates where it is above 3,
  at least 30 s apart.

## Evaluation

`prepare.py` scores each dataset stream and runs the app's chat detector
(`argus.detect.replay.replay`, through `chat_peaks.py`) with the local config and with
the dataset's "relaxed" thresholds (in memory only). `evaluate.py` compares them against
the hand-labelled 30 s windows (`moment` / `maybe` / `no`). Labels are proposed by chat,
loudness and random sources, so they are not a full list of moments. The script reports
window AUC, top-N per hour, and a veto of chat peaks by audio.

### Results (full set: 5 streams, 169 min, 138 labels: 18 moment / 46 maybe / 74 no)

Window ROC AUC, moment vs no (0.5 = chance):

| signal | all | "no" from random only |
|---|---|---|
| audio excitement | 0.52 | 0.54 |
| events only (no loudness) | 0.49 | 0.52 |
| loudness only | 0.55 | 0.62 |
| chat z | **0.79** | **0.86** |
| fused (mean of percentiles) | 0.66 | 0.77 |
| fused (max of percentiles) | 0.74 | 0.80 |

By group: scream 0.66 and gasp 0.63 carry a little signal. Laugh 0.55 and cheer 0.55 are
near chance, and bang 0.39 is inverted (game noise happens in routine play).

Top 12 per hour (label precision counts a maybe as half, over picks that landed on a
labelled window):

| method | picks | label precision | moment recall |
|---|---|---|---|
| chat detector, app config | 6 | 0.50 | 0.11 |
| chat detector, relaxed | 35 | 0.48 | **0.56** |
| audio candidates | 35 (19 on a label) | 0.37 | 0.17 |
| relaxed chat + audio veto | 29 | 0.45 | 0.44 |
| continuous fused | 35 | 0.42 | 0.28 |
| random | 35 (11 on a label) | 0.32 | 0.06 |

Per-source precision, `pool == full_K8` only (125 windows):

| proposed by | n | precision | moments |
|---|---|---|---|
| chat_peak | 6 | 0.50 | 2 |
| chat_rate | 37 | 0.47 | 11 |
| loud (ebur128) | 40 | 0.39 | 8 |
| chat_peak_relaxed | 58 | 0.35 | 9 |
| random | 40 | 0.21 | 3 |
| windows holding an audio candidate | 31 | 0.37 | 5 |
| windows without one | 94 | 0.27 | 10 |

- **Catches chat missed: none at moment level.** No window labelled `moment` has
  `chat_reacted == "no"`. All 13 moments proposed by loud or random were also reacted
  to by chat. Among the `maybe` windows with weak or no chat reaction proposed by
  loud or random (8 windows, about 6 events), an audio candidate fires on 4 (3
  events: a mild self-deprecating joke, a "why do I aim so bad" outburst, a
  "wheee" bit).
- **The veto is weaker than on the partial set.** Relaxed chat peaks on labels whose
  audio is below the stream median get dropped: 12 of 26 "no", 10 of 23 "maybe" and 2 of
  9 "moment". Precision does not improve at the same N (0.48 to 0.45), and recall drops
  0.56 to 0.44.
- The partial set (first 15 min) had looked better (audio AUC 0.61, veto precision
  0.58 to 0.78). That did not hold up.

**Recommendation:** do not adopt audio events, alone or fused. Chat is clearly the
better signal on these streams, and YAMNet's classes are no better than a loudness
baseline, which itself is weak. At most, test a loudness or scream/gasp z as a small
rank factor on a larger set with IRL streams, where non-speech events matter more.

## Cost (8 vCPU, no GPU, WSL2; load average ~7 from other jobs while measuring)

`bench.py` feeds real 720p60 2 s segments through `StreamScorer`, 12 streams in one
process with one shared interpreter:

| part | CPU s per stream-minute |
|---|---|
| YAMNet only (120 frames) | 0.11 to 0.15 |
| + decode with one `ffmpeg` process per segment | 1.9 to 2.1 |
| + decode in-process with PyAV (`decode_pyav`) | 1.1 |
| ffmpeg, 5 segments per call | 0.6 |

Twelve streams cost 13 CPU s per minute with PyAV (0.22 of one core) or 25 (0.42 of one
core) with a spawn per segment. RAM is one process of about 60 MB (110 MB with PyAV) in
total, not per stream. It fits easily. Decoding the AAC costs ten times more than the
model, so it should be batched, or done in-process.

## Files

- `scorer.py` YAMNet wrapper, per-second rows, incremental `StreamScorer`
- `excitement.py` baseline, excitement, candidates
- `score_stream.py` scores a recorded capture buffer (`segments.jsonl` + `segments/`)
- `chat_peaks.py` runs the app's chat detector on a chat log (the app's venv)
- `prepare.py`, `evaluate.py` run the dataset evaluation
- `bench.py` cost
