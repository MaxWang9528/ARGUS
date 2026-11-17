# Speech-excitement signal (experiment)

Transcribe the streamer live with faster-whisper (CTranslate2, int8, CPU) and score
excitement per second from the words: exclamation phrases, swearing, laughter,
repeated short words / "!", words per second against the speaking rate, and
speech-band loudness. Candidate moments are local maxima 30 s apart.

Nothing here is wired into the app. Models, audio, transcripts and results live
outside the repo.

## Files

| file | what |
|---|---|
| `speech.py` | decoding (ffmpeg), rolling-chunk transcription, features, score, candidates |
| `buffer_stream.py` | runs on a capture buffer folder (`segments/` + `segments.jsonl`) like a live worker: every 4 new HLS segments (8 s) it decodes the last 5 (10 s, 2 s overlap) and transcribes them. `--follow` tails a recording in real time and logs latency |
| `chat_peaks.py` | runs `argus.detect.replay.replay` on chat logs (app venv), dumps peaks + per-bin chat-rate z. `RELAXED=1` uses the dataset's relaxed thresholds (in memory only) |
| `evaluate.py` | AUC on the judged windows, top-N per hour, fusion, confirm/reject split |
| `bench.py` | CPU real-time factor, N concurrent streams, confirm-window latency |

Setup (scratch dir, not the app venv):

```
uv venv venv --python 3.12 && VIRTUAL_ENV=venv uv pip install faster-whisper numpy
venv/bin/python -I buffer_stream.py <stream_dir> out.json --model base.en --root <models> [--follow]
```

Models come from the official Systran faster-whisper repos on Hugging Face
(`tiny.en`, `base.en`, `small.en`).

## Settings

- `base.en`, int8, `cpu_threads=1`, beam 1, temperature 0, Silero VAD on
  (min silence 300 ms), word timestamps, `condition_on_previous_text=False`,
  `hallucination_silence_threshold=2`.
- 10 s chunks, 8 s hop. A word belongs to the chunk that owns its midpoint, so the
  overlap never counts a word twice.
- Segments with `compression_ratio > 2.4` (repetition loops on music) are dropped,
  and a word repeated more than twice in a row is cut.
- Live latency (last segment arrived -> its words scored): ~1.6-2.0 s median with
  four streams running in parallel.

Model choice (rolling 10 s chunks, 1 thread, CPU seconds per audio second):

| model | CPU-RTF | notes |
|---|---|---|
| tiny.en | 0.16 | more hallucinated loops on music |
| base.en | 0.24 | used |
| small.en | 0.59 | best words; too expensive to run continuously on many streams |

## Findings

Final run on the complete labelled set: 5 live streams recorded together (3 gaming,
2 just chatting), 169 min, 138 judged 30 s windows (18 moment, 46 maybe, 74 no).
Speech weights were fixed before the labels arrived; the one change after the first
partial look (v2: words/s measured against the speaking rate instead of all
seconds, a fix for a saturating feature) is shown next to v1. Chat = the app's
detector run through `replay` on the same chat (relaxed thresholds: the user's
thresholds give only 6 peaks in 169 min on these slow chats), or the per-second
chat-rate z the detector computes.

**1. Ranking the judged windows (AUC; moment vs no | moment+maybe vs no)**

| signal | AUC |
|---|---|
| chat-rate z (window +10 s) | **0.79** \| 0.68 |
| speech, full score v2 (v1) | 0.57 (0.47) \| 0.52 |
| speech, lexical only (exclaims, swears, repeats, "!") | **0.76** \| 0.61 |
| speech, words/s | 0.45 \| 0.46 |
| speech, loudness | 0.55 \| 0.57 |
| fusion, mean of ranks (chat, full speech) | 0.76 \| 0.64 |
| windows chat did not propose (22 vs 40): chat / lexical / full speech | 0.66 / 0.67 / 0.55 |

Words per second is anti-informative (fast talk is storytelling and reading chat;
moments have pauses and outbursts), and speech-band loudness is weak. The useful
part is the lexicon. Per stream, lexical AUC was 0.82 and 0.71 on the two
reaction-heavy gaming streams, but 0.43-0.57 on the talk streams and the
calm stream, where swearing and "oh my god" are routine.

**2. Top-N per hour** (precision over judged detections, moment | moment+maybe;
recall of the 18 moments)

| per hour | random | chat detector | chat-rate z | speech | fusion (sum) | detector or speech |
|---|---|---|---|---|---|---|
| 8 | P .12\|.44 | P .30\|.70 R .50 | P .14\|.68 R .28 | P .27\|.60 R .22 | P .25\|.60 R .33 | P .37\|.74 R .50 |
| 16 | P .14\|.51 | P .20\|.62 R .61 | P .26\|.71 R .72 | P .19\|.56 R .28 | P .19\|.59 R .44 | P .24\|.65 R .61 |
| 30 | P .11\|.46 | P .16\|.55 R .61 | P .18\|.61 R .83 | P .18\|.55 R .50 | P .22\|.60 R .89 | P .17\|.58 R .72 |

Speech alone is better than random but below chat at every N. Speech's top-N caught
2-3 moments chat-rate's top-N missed (rage lines on the gaming streams). Fusion helps
only at a generous budget (30/h: recall .89 vs .83). Only 50-65 % of speech
detections land in a judged window, so its precision is the least certain.

**3. Confirm / reject chat candidates.** No labelled moment was one chat ignored
(all 18 had chat react), so "speech finds what chat misses" could not be shown on
this set beyond the 2-3 above. The other complaint can: 21 windows had chat busy
with nothing happening (spam waves, alerts). The frozen full-score gate (>= 2)
rejects almost nothing (0 moment / 4 maybe / 7 no of 76 chat windows). A
lexical gate (post hoc, threshold 0.5 = roughly one excitement hit in the
window) rejects 22 chat windows: 0 moments, 9 maybe, 13 no, and 9 of the 21
"chat busy, nothing happening" windows. With threshold 1.5 it rejects 16/21 busy
ones but also 4 of 15 moments.

**Cost** (8 cores, no GPU; CPU seconds per audio second, rolling chunks):

- continuous `base.en`: 0.18-0.26 per stream alone (0.24 typical), so 12 streams
  is ~2.9 of 8 cores. Measured with 12 concurrent streams on one shared model while
  other jobs loaded the machine: kept up (wall/audio 0.83) using 4.8 cores (0.33
  per stream under contention). It fits on paper, with little headroom next to
  capture, cutting and rendering. `tiny.en` (0.16) is about 2 cores but worse text.
- confirm-only (transcribe 30 s around a candidate): `base.en` 2 CPU-s (0.8 s wall
  on 2 threads), `small.en` 6.5 CPU-s (3 s wall on 2 threads). At 30
  candidates/hour on each of 12 streams that is 0.2 core (base) or 0.65 core
  (small), and the small-model text is good enough for caption drafts.

**Recommendation.** Do not run speech continuously as a detector: it costs a third
to a half of the CPU for a signal weaker than chat. Run it confirm-only: when chat
(or another cheap signal) proposes a window, transcribe 30 s with `small.en`, use
the lexical score as a tie-break / soft gate on low-ranked chat peaks (it removes
spam-wave false positives and lost no moment at a low threshold), and keep the
transcript for the TikTok caption. Drop words/s and loudness from the speech
score. The gate threshold needs a second labelled set before it is trusted, and
the gate was measured on `base.en` text (small should only help).

## Caveats

- One AI labeller, small counts. The labels were judged mostly from a whisper
  transcript, so they favour moments you can hear in words.
- The judged windows were proposed by chat detectors, loudness and random picks,
  never by this signal, so most top speech candidates fall outside judged windows
  and are not scored (only 50-65 % were judged). AUC on the judged windows
  is the fairer comparison.
- The `loud` candidate source is EBU R128 loudness, close to this signal's loudness
  feature, which inflates that feature a little.
- Whisper (any size) almost never writes laughter tokens in this setup, so the
  laughter cue barely fires; laughter needs an audio-event model.
