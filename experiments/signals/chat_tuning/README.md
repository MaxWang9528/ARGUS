# Chat peak detector tuning

Goal: catch the sharp chat rises that stand out against the *recent* level. Slow
drifts, slow-chat wandering, and busy chat with nothing happening should not fire.
The work only touches `argus/detect/peak.py` (`PeakTracker`). Nothing under `argus/`
or `tests/` is edited here. The proposed change is in `peak_recommended.diff`.

## Files

| file | what |
|---|---|
| `common.py` | loads the dataset and the unlabelled logs (paths from env vars, no data in the repo), replays one tracker |
| `characterise.py` | per labelled window: rate curve features (peak, baselines, rise, FWHM, decay, new chatters, repetition), their AUCs, and a plot |
| `tracker.py` | `TunedTracker`: a copy of `PeakTracker._close` with hooks for every variant tried. Its defaults reproduce the app exactly (`evaluate.py --selftest`) |
| `evaluate.py` | precision, recall, busy-nothing hits, AUC, and peaks/h on the logs, as one table, a sweep, or leave-one-stream-out |
| `shapes.py` | shapes of fired peaks on the logs (climb time, pre-level / baseline), a plot, and chat text |
| `check_patch.py` | runs a patched copy of `peak.py` and checks it gives the same peaks as the variant |
| `peak_recommended.diff` | the recommended change |

Environment: `ARGUS_DATASET`, `ARGUS_CHATLOGS`, `ARGUS_CHATDAY`, `ARGUS_STALL="t0,t1"`
(the 04:26–04:31 UTC stall is cut out, and the logs on each side are replayed
separately), and `ARGUS_CODE` (only for `check_patch.py`). numpy and matplotlib are
needed only by `characterise.py` and `shapes.py`.

## How it was scored

- **Labelled set**: 5 streams, 169 min, 138 windows. The 18 moment windows are 14
  distinct events (same stream, within 20 s). A hit means a peak's centre
  (`ts_peak - 4 s`) falls inside a label window.
- **P** = moment / (moment + no) over peaks that land on a labelled window. `maybe`
  is left out. Almost every peak lands on some label, because the chat sources built
  the pool.
- **Busy-nothing**: windows labelled `no` with `chat_reacted: yes` (21 of them).
- **AUC**: moment vs no, scored on each window's highest start margin.
- **Logs**: 2026-11-17, 24 channels with chat, about 6.5 h. Each channel is classed by
  its average rate: busy is 3 msg/s or more, slow is 0.4–3, and dead is below 0.4.
  "cov" is the share of 15-min blocks that contain a peak (how evenly peaks spread).
  "drift" counts peaks whose rise took 20 s or more (base + 25% of the rise up to the
  peak). On the labelled set, moments take a median 7 s.
- **LOSO**: for each held-out stream, the variant's one sensitivity knob is chosen on
  the other 4 streams. The choice maximises recall + P within 8–15 peaks/h. The 5
  held-out results are then summed, so no setting is fitted to the moments it is
  scored on.

## What causes false and missed peaks

1. **Missed (slow chats).** In these streams the 180 s median runs at 0.2–0.4 msg/s.
   The user's `min_msgs_per_sec = 2` and `multiplier = 6` need 2–2.4 msg/s over 5 s,
   so most reactions on slow chats never qualify: 6 peaks, 2/14 events, and 0 on one
   stream. The busy logs get 0.2 peaks/h. Moments peak at a median 5.8 msg/s (p25 2.3).
2. **False (wandering).** At 0.4 msg/s a 5 s mean is Poisson noise of about ±0.3, and
   chat comes in clumps. The median sits at the bottom of that noise, and the MAD
   spread is floored at 0.25. Once the thresholds are relaxed enough to catch slow-chat
   moments, ordinary 10–20 s clumps of 1.5–2.5 msg/s pass `multiplier × median`.
   These clumps are most of the "no" hits (relaxed: 26 no vs 9 moment).
3. **False (drift).** Chat climbs over 20–60 s, the median lags, the climb crosses the
   threshold, and the baseline freezes for the peak. `shapes.py` finds this in about
   3–4% of relaxed peaks on the logs, plus steps up to a new level that then plateau.
   Labelled moments rise faster (median 7 s, against 5–8 s for false peaks), so climb
   time alone separates poorly. Fast rises are necessary but not sufficient.
4. **Busy, nothing happening.** These look like moments in rate terms. Peak height, rise
   and duration all overlap. The best single separator was the share of messages from
   chatters not seen in 10 min (AUC 0.79 moment vs busy-nothing). As a filter it cost
   recall, though (below). Copy-paste detection never triggered on this data, because
   the waves are short and mostly emotes.
5. **Unavoidable misses.** Some moments got only weak chat (peak 1.4–2.4 msg/s, as high
   as noise in the same stream). Others fall inside the 60 s warm-up after the chat
   recording started, at 0 msg/s before.

Window features (AUC moment vs no, n = 18 vs 74): rise in 10 s / sqrt(pre-level) 0.86,
peak rate 0.84, Poisson z against the 180 s median 0.84, peak / 180 s median 0.80,
FWHM 0.75, peak / pre-window 0.73, climb time 0.65. With n = 18, the standard error is
about 0.05, so none of these is clearly better than the others.

## Variants (full labelled set; logs: peaks/h busy / slow; drift = slow-climb peaks / all)

| variant | peaks/h | events | P | busy-nothing hit | AUC | logs busy / slow (cov) | drift |
|---|---|---|---|---|---|---|---|
| current config.toml | 2.1 | 2/14 | 0.50 (2/4) | 1/21 | 0.82 | 0.2 / 1.3 (5% / 20%) | 3/43 |
| relaxed (dataset's) | 21.0 | 9/14 | 0.26 | 14/21 | 0.79 | 9.2 / 8.8 | 12/370 |
| relaxed, multiplier 3.5 | 12.8 | 8/14 | 0.36 | 7/21 | 0.78 | 3.8 / 4.8 (71% / 63%) | 9/221 |
| + pre-window mean 40–15 s (max with median) | 12.1 | 8/14 | 0.40 | 8/21 | 0.78 | 3.4 / 4.4 | 5/184 |
| + rise ≥ 60% of excess within 15 s | 12.8 | 8/14 | 0.36 | 7/21 | 0.78 | 3.8 / 4.8 | 8/220 |
| Poisson spread, z 4 | 13.5 | 8/14 | 0.40 | 7/21 | 0.80 | 6.1 / 6.2 (86% / 75%) | 9/229 |
| Poisson + pre-window mean, z 4 | 12.1 | 8/14 | 0.42 | 7/21 | 0.80 | 5.4 / 5.6 | 8/196 |
| **Poisson + pre-window median 45–15 s + rise in 10 s, z 4** | **12.1** | **8/14** | **0.44** | **7/21** | 0.79 | 4.9 / 4.8 (76% / 73%) | **5/177** |
| Poisson, smooth_s 10, z 3 | 13.5 | 8/14 | 0.38 | 7/21 | 0.84 | 5.8 / 6.2 | 5/217 |
| + reject plateaus (hit max_clip_s) | 11.7 | 6/14 | 0.35 | 7/21 | 0.79 | | |
| + new-chatter share ≥ 15% | 9.9 (z 3) | 6/14 | 0.33 | 5/21 | 0.78 | | |
| + copy-paste share ≤ 30% | identical to without | | | | | | |

Leave-one-stream-out:

| variant | knob chosen per fold | peaks/h | events | P | busy-nothing hit |
|---|---|---|---|---|---|
| relaxed, multiplier swept | 4, 3.5, 3.5, 3.5, 3.5 | 11.7 | 6/14 | 0.32 (6/19) | 7/21 |
| pre-window mean, multiplier swept | 4, 3.5, 3.5, 3.5, 3.5 | 11.4 | 6/14 | 0.33 | 8/21 |
| Poisson only, z swept | 4.5, 3.5, 3.5, 3.5, 3.5 | 13.1 | 7/14 | 0.39 | 7/21 |
| Poisson, smooth 10 | 3.5 × 5 | 11.0 | 8/14 | 0.44 | 5/21 |
| **recommended** | **4 × 5** | **12.1** | **8/14** | **0.44 (8/18)** | **7/21** |

The recommended knob is the same in every fold. Speed-scaled noise lets one threshold
transfer between chats; a ratio threshold does not.

## Recommendation

Apply `peak_recommended.diff` to `argus/detect/peak.py`. It does three things:

1. **Spread ≥ sqrt(baseline / smooth_s)**: the counting noise of the smoothed rate.
   Thresholds scale with chat speed, so slow chats need a statistically real rise and
   busy chats aren't held to a fixed 2 msg/s.
2. **Pre-window baseline**: the median of the rate from 45 to 15 s ago. If it is higher
   than the long or recent median, it becomes the baseline. A peak must stand out
   against what chat was doing just before it.
3. **Sharp-rise gate**: the rate must have risen by more than `z_threshold × spread`
   within the last 10 s (`JUMP_S`). Slow climbs and wandering past the threshold don't
   start a peak.

Then set `[detection]` in config.toml:

```toml
multiplier = 2.5
z_threshold = 4
min_msgs_per_sec = 0.6
min_unique_chatters = 3
cooldown_s = 45
min_sustain_s = 6
# unchanged: baseline_window_s = 180, end_ratio = 2, max_clip_s = 45, smooth_s = 5
```

`check_patch.py` confirms that the patched `peak.py` gives exactly the variant's peaks
(213 of 213 on the dataset and logs). The existing `tests/detect` pass against the
patched copy (32 passed).

| | before (current) | after |
|---|---|---|
| labelled peaks/h | 2.1 | 12.1 (per stream 10–26; lowest-rate stream 0 → 12) |
| events caught | 2/14 | 8/14 |
| P (moment / labelled hits) | 0.50 (2/4) | 0.44 (8/18) |
| busy-nothing windows rejected | 20/21 | 14/21 |
| logs, busy chats | 0.2/h, 5% of 15-min blocks | 4.9/h, 76% |
| logs, slow chats | 1.3/h, 20% | 4.8/h, 73% |
| slow-climb peaks on logs | 3/43 | 5/177 (2.8%) |

The fair comparison is with the same pick rate and thresholds changed alone (relaxed,
multiplier 3.5). The code change catches the same 8 events, has 10 instead of 14 `no`
hits, and halves the drift share of `Poisson only`. Held out, it scores 8/14 at P 0.44,
against 6/14 at P 0.32.

## Uncertainty

- There are 14 moment events. Recall 8/14 has a 95% Wilson interval of 0.33–0.79, and
  P 8/18 has 0.25–0.66. Every variant at about 12/h catches the same 8 events.
  Differences between variants are 1–4 `no` hits and 0–2 events, which is within
  noise. The decisive part is the operating point (2/h → 12/h). The code change is
  backed by its stability under LOSO (the same z in every fold), the logs
  (more even spread, half the drift peaks), and the fact that it targets the reported
  failure by construction. The labels alone don't prove it.
- One AI labeller, labels that lean on speech, and the labelled pool came from these
  same chat sources. P only covers peaks on labelled windows, and there was 1
  unlabelled hit.
- About a third of busy-nothing windows still fire. Rate shape can't separate
  them, and the new-chatter share was the only promising text feature. A ranking tweak
  is worth trying next, instead of a filter.
- On the logs, busy and slow chats both come out at about 5/h, below the 8–15/h on the
  labelled streams. Logs include idle stretches. For more picks, z 3 gives about 5.8/h
  there and 16/h on the labelled streams, with 9/14 events at P 0.35.
