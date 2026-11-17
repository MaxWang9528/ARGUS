# Video motion as a moment signal (experiment)

Question: can cheap video analysis find moments that chat rate misses, or reject
chat peaks where nothing happens?

**Answer, on this data: a weak signal (AUC about 0.6), weaker than chat (0.68 to
0.80), and adding it to chat gives no significant gain. Facecam motion is noise.
It costs about 1 core for 12 streams. Not worth adding to the app now.**

## What it computes

`extract.py`: one ffmpeg run decodes the video to 4 fps, 160x90 grey frames, piped
to numpy. For each frame it computes:

- `motion`: the mean absolute frame difference;
- `face`: the same inside the facecam box. `facefind.py` finds the box with OpenCV's
  bundled Haar face cascade on one frame per 20 s and grows the face into a
  head-and-shoulders box. The configured `stack_top` regions are the same
  placeholder for 64 of the 66 streamers, so they cannot be used;
- `flash`: the biggest brightness jump against the previous second;
- `cuts`: the 32-bin histogram distance above 0.6, which `score.py` turns into
  cuts per 10 s.

`score.py`: a per-stream causal baseline for each channel. It uses the median and
MAD of the last 5 minutes, skips the last 5 s and needs at least 60 s of history.
The z-scores are clipped at 0..8 and combined with weights (`combo` = motion +
face + 0.5 flash + 0.5 cutrate), a 3 s max is applied, and the result goes through
greedy non-maximum suppression (45 s) to give candidate moments.
`kf_combo` is the same idea computed from keyframes only (`-skip_frame nokey`,
0.5 fps), the cheap variant.

## Evaluation (`evaluate.py`, `labels.py`, `chat_peaks.py`)

The test set's labels are a sample of 30 s windows proposed by chat, loudness and
random draws, each judged moment, maybe or no. They are not an exhaustive list of
moments. So the main metric is ranking: for each window, take the signal's maximum
in that window and convert it to a percentile among all 30 s windows of the same
stream. Then compute the ROC AUC of moment versus no. Chance is estimated by
circularly shifting the signal (200 times). For chat, `argus.detect.replay.replay`
is run with the app's config, and the per-second z is moved back by the 3-10 s
chat lag. Fusion is the mean or max of the chat and video percentiles.

## Results (full test set: 5 streams, 169 min, 138 windows: 18 moment, 46 maybe, 74 no)

AUC of window percentile, moment vs no (maybe dropped) and moment+maybe vs no:

| signal | m vs no | m+maybe vs no |
|---|---|---|
| motion (whole frame) | 0.61 | 0.60 |
| facecam motion | 0.46 | 0.50 |
| flash | 0.57 | 0.58 |
| cut rate | 0.65 | 0.61 |
| combo | 0.61 | 0.60 |
| combo from keyframes only | 0.55 | 0.53 |
| **chat z (app detector)** | **0.80** | **0.68** |
| fusion: mean(chat, combo) | 0.76 | 0.68 |
| fusion: max(chat, combo) | 0.82 | 0.70 |

- Chance level, from 200 circular shifts: mean 0.49, 95th percentile 0.58. Combo
  0.60 (p = 0.015) and motion 0.60 (p = 0.02) are above chance. Face 0.50 is not.
- Confound check: within chat- or loudness-proposed windows only, combo scores
  0.63 (chat also 0.63). Within loudness-proposed windows it scores 0.68. Within
  random windows it scores 0.43 (only 9 positives). Video percentiles are similar
  for proposed and random windows (0.50 vs 0.46), so the effect is not just
  "proposed vs random".
- Fusion vs chat alone (paired bootstrap of the AUC difference): max +0.025
  (95% CI -0.04 to +0.10), mean +0.004 (-0.08 to +0.08). No significant gain.
- Moments chat missed: 15 moment or maybe windows had weak or no chat reaction.
  Video ranks 3 of them in its top 20% of windows, but two are the same event and
  all three are `maybe`. Chat's own z ranks 4 of them that high.
- Chat peaks video would reject: of the chat-proposed windows, those with a low
  video score (below the median) are 18/36 `no`, and the rest are 16/40 `no`.
  Rejecting the low-video ones would also drop 7 of the 15 chat-proposed moments.
- Top 8 per hour as standalone candidates: most picks land in unlabelled time
  (18 of 23 for combo), so precision and recall cannot be measured that way here.
  Of the 5 that landed in labelled windows, 1 was a moment, 4 were maybe and 0
  were no.
- Partial set (first 15 min, 41 windows): combo AUC 0.53 (p = 0.38), chat 0.67.
  The weak effect only appears with more data.
- Caveats: one AI labeller, labels lean on speech, and chat and loudness chose
  most windows. No IRL stream was live. IRL streams, where the camera *is* the
  content, are where motion would most plausibly help, and they are untested.

## Cost (8 cores, WSL2)

Almost all of the cost is the H.264 decode. The numpy work is about 0.1% of a core.

| input | CPU per stream | 12 streams |
|---|---|---|
| 720p60 buffer (the app's quality), full decode, 1 thread | 8.8% of a core alone, 9.4% with 12 running at once | ~1.1 cores (14% of the machine) |
| same, `-skip_loop_filter all` | ~8.6% | ~1.0 core |
| 480p30 segments, full decode | ~4% | ~0.5 cores |
| keyframes only (`-skip_frame nokey`, 1 frame per 2 s segment) | 0.6% | ~0.07 cores |
| Haar facecam search, 1 frame per few minutes | ~0.2 s per frame | negligible |

Downscaling does not reduce the decode cost, because every frame must be decoded
to compute any frame difference. Only the keyframe variant is close to free, and
at 0.5 fps it loses the motion signal (it scored worse).

## Notes

- Run with the scratch venv (`numpy`, `opencv-python-headless==4.10.0.84` for the
  cascade files), with `python -I`. `chat_peaks.py` runs in the app's environment.
- Facecam autodetection worked on 4 of 5 streams. It failed on the VTuber stream,
  as expected.
- Gaming footage dominates the full-frame motion. Camera motion, menus and map
  changes look like "events", and a streamer shouting at a static screen does
  not.

## Recommendation

Don't add it now. Chat already does better, and the fusion gain is within noise.
The only part worth keeping in mind is whole-frame motion or cut rate as a
tie-breaker among chat candidates, which costs ~1 core of decoding for 12
streams. The facecam channel did nothing, even with the box found automatically.
Retest on IRL streams and with labels that are not proposed by chat or loudness
before spending CPU on it.

## Reproduce

```
uv venv venv && VIRTUAL_ENV=venv uv pip install numpy opencv-python-headless==4.10.0.84
venv/bin/python -I evaluate.py <dataset dir> <work dir outside the dataset>
venv/bin/python -I extract.py <video> <out.npz> [--face x,y,w,h]   # also prints its CPU cost
```
