# Moment judge (LLM) evaluation

`evaluate.py` scores `argus/detect/judge.py` on the labelled stream-signal dataset
(138 windows of 30 s, labelled moment / maybe / no). For each window it builds the
same prompt the app builds (same fixed system prompt, transcript, chat from the window
start to 8 s past its end, bot commands removed, at most 200 lines), asks Claude for a
`Verdict`, and reports AUC (moment vs no, moment+maybe vs no) and precision at the top
10 / 18 / 30 windows, next to baselines computed without the API.

```
~/.venvs/livewatcher/bin/python experiments/signals/llm_judge/evaluate.py <dataset_dir> \
    [--max-calls 140] [--model claude-opus-5-5] [--effort low] [--estimate-only]
```

- Transcript: the dataset's `aux_transcript.json` (faster-whisper small). Use
  `--transcribe small.en` to transcribe each window with the app's transcriber.
- It prints the estimated cost first. Without an Anthropic credential
  (`ANTHROPIC_API_KEY`, or `ant auth login`) it prints the estimate and the baselines
  and stops before any API call.
- Results go to `results.jsonl` here, one line per window. A rerun skips the windows
  already judged.
- The label's `reason` and `sources` are never sent to the model.

## Cost

The prompt sizes it builds: a fixed system prompt of about 730 tokens, plus a user
message of about 610 tokens on average (1,900 at most). On Claude Opus 5.5
($4 / $20 per million input / output tokens) at effort `low`, with 200-500 output
tokens per call (thinking plus the JSON), that is about **$0.009-0.015 per call**,
and about **$1.30-2.10 for all 138 windows**. The input alone is $0.74. Output
dominates, so the real cost depends on how much the model thinks at `low`. A run
prints the tokens it actually used. The system prompt is probably under the model's
minimum cacheable size, so expect no cache hits.

In the app, the cost is the same per auto clip, about 1-1.5 cents.

## Status

Not run yet: this machine has no Anthropic credential. Baselines on the same 138
windows, computed here without the API:

| signal | AUC moment vs no \| moment+maybe vs no | P@10 | P@18 | P@30 (moment \| moment+maybe) |
|---|---|---|---|---|
| speech lexical (`speech_score` on aux transcript) | 0.70 \| 0.65 | .40\|.60 | .28\|.56 | .20\|.60 |
| chat ratio, window +10 s vs trailing 180 s | 0.75 \| 0.61 | .50\|.70 | .33\|.61 | .23\|.50 |
| reference (`../speech/README.md`): chat-rate z | 0.79 \| 0.68 | | | |
| reference: speech lexical (word-level base.en transcript) | 0.76 \| 0.61 | | | |

The speech baseline here is lower than in the speech experiment because the aux
transcript only has segment times (words are spread evenly over each segment).

Caveat: the labels come from one AI labeller who saw the same transcript and chat, so
an LLM judge may agree with them more than a human would.
