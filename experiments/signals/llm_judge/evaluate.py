"""Score the moment judge (argus/detect/judge.py) on the labelled stream-signal dataset.

  ~/.venvs/livewatcher/bin/python experiments/signals/llm_judge/evaluate.py <dataset_dir>
      [--max-calls 140] [--model claude-opus-5-5] [--effort low] [--out results.jsonl]
      [--concurrency 4] [--estimate-only] [--transcribe MODEL]

For every labelled 30 s window it builds the exact prompt the app builds (same system
prompt, same chat filter and caps) from the window's transcript and chat, asks Claude
for a Verdict, and reports
  1. AUC of the judge's score: moment vs no, and moment+maybe vs no;
  2. precision at the top N windows (moment | moment+maybe);
next to two baselines computed here without the API: the speech lexical score
(argus.detect.speech.speech_score) and a chat-rate ratio over the window +10 s.

* Transcript: ``aux_transcript.json`` (faster-whisper small, segment times) by default;
  ``--transcribe small.en`` transcribes each window from ``media.mp4`` with the app's
  transcriber instead (needs faster-whisper; slow on CPU).
* It prints the estimated cost up front from the prompts it built. With no Anthropic
  credential (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an ``ant auth login`` profile)
  it stops after the estimate and the baselines, without calling the API.
* Results are appended to ``--out`` one JSON per window; a rerun skips windows already
  there, so ``--max-calls`` can be spent in parts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from argus.chat.chatlog import read_chat_log  # noqa: E402
from argus.core.config import Detection, Judge  # noqa: E402
from argus.core.models import ChatMessage, Clip, Word  # noqa: E402
from argus.detect import judge as jd  # noqa: E402
from argus.detect.speech import speech_score  # noqa: E402

# Claude Opus 5.5 list prices, USD per million tokens.
PRICE_IN, PRICE_OUT, PRICE_CACHE_WRITE, PRICE_CACHE_READ = 4.00, 20.00, 5.00, 0.20
CHARS_PER_TOKEN = 3.2      # conservative for English chat + transcript text
EST_OUTPUT_TOKENS = (200, 500)  # thinking at effort low + the JSON verdict (a guess; a run measures it)
CHAT_BASELINE_S = 180
CHAT_LAG_S = 10            # chat-rate baseline reads this far past the window
REFERENCE = "experiments/signals/speech/README.md on the same windows: chat-rate z 0.79 | 0.68, " \
            "speech lexical 0.76 | 0.61 (AUC moment vs no | moment+maybe vs no)"


def auc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def precision_at(rows: list[tuple[float, str]], n: int) -> tuple[float, float]:
    top = sorted(rows, key=lambda r: -r[0])[:n]
    if not top:
        return float("nan"), float("nan")
    return (sum(lab == "moment" for _, lab in top) / len(top),
            sum(lab in ("moment", "maybe") for _, lab in top) / len(top))


def report(name: str, rows: list[tuple[float, str]], ns: tuple[int, ...] = (10, 18, 30)) -> None:
    by = {k: [s for s, lab in rows if lab == k] for k in ("moment", "maybe", "no")}
    a1 = auc(by["moment"], by["no"])
    a2 = auc(by["moment"] + by["maybe"], by["no"])
    ps = "  ".join(f"P@{n} {m:.2f}|{mm:.2f}" for n in ns for m, mm in [precision_at(rows, n)])
    print(f"  {name:<22} n={len(rows):<4} AUC {a1:.2f} | {a2:.2f}   {ps}")


# ---------------------------------------------------------------- dataset

def load(dataset: Path) -> tuple[dict, list[dict]]:
    man = json.loads((dataset / "manifest.json").read_text())
    labels = [json.loads(x) for x in (dataset / man["labels"]).read_text().splitlines() if x.strip()]
    return man, labels


def aux_words(path: Path) -> list[Word]:
    """Segment-level transcript -> words spread evenly over each segment (media seconds)."""
    out: list[Word] = []
    for seg in json.loads(path.read_text())["segments"]:
        toks = seg["text"].split()
        if not toks:
            continue
        step = (seg["end"] - seg["start"]) / len(toks)
        out += [Word(w=t, t=seg["start"] + i * step, e=seg["start"] + (i + 1) * step) for i, t in enumerate(toks)]
    return out


class Stream:
    def __init__(self, dataset: Path, s: dict, transcribe: str = "") -> None:
        self.name, self.category, self.wall_start = s["name"], s.get("category", ""), s["wall_start"]
        self.media = dataset / s["media"]
        self.chat: list[ChatMessage] = list(read_chat_log(dataset / s["chat"]))
        self.chat.sort(key=lambda m: m.ts)
        self.words = aux_words(dataset / s["transcript_aux"])
        self.transcribe = transcribe
        self._tr = None

    def window_words(self, a_media: float, b_media: float) -> list[Word]:
        if self.transcribe:
            from argus.detect.speech import FasterWhisperTranscriber

            if self._tr is None:
                self._tr = FasterWhisperTranscriber(self.transcribe)
            return self._tr.transcribe_sync(self.media, a_media, b_media - a_media).words
        return [Word(w=w.w, t=w.t - a_media, e=w.e - a_media) for w in self.words if a_media <= w.t < b_media]

    def window_chat(self, a: float, b: float, ignore: set[str]) -> list[ChatMessage]:
        msgs = [m for m in self.chat if a <= m.ts <= b and m.text.strip()
                and not m.text.lstrip().startswith("!") and m.user.lower() not in ignore]
        return msgs[-jd.MAX_CHAT_LINES:]

    def chat_ratio(self, a: float, b: float) -> float:
        n = sum(a <= m.ts <= b + CHAT_LAG_S for m in self.chat) / (b + CHAT_LAG_S - a)
        base = sum(a - CHAT_BASELINE_S <= m.ts < a for m in self.chat) / CHAT_BASELINE_S
        return n / (base + 0.05)


def build_items(dataset: Path, transcribe: str) -> list[dict]:
    man, labels = load(dataset)
    streams = {s["name"]: Stream(dataset, s, transcribe) for s in man["streams"]}
    ignore = {u.lower() for u in Detection().ignore_users}
    items = []
    for lab in labels:
        st = streams[lab["stream"]]
        a, b = lab["window_wall"]
        am, bm = lab["window_media"]
        words = st.window_words(am, bm)
        text = " ".join(w.w for w in words)
        # The same window a clip would have: chat from its start to CHAT_LAG_S past its end.
        clip = Clip(streamer=st.name, window_start=a, window_end=b, transcript=text)
        chat = st.window_chat(a, b + jd.CHAT_LAG_S, ignore)
        system, user = jd.build_prompt(clip, text, chat, st.name, st.category)
        items.append({"id": lab["id"], "stream": st.name, "label": lab["label"], "system": system,
                      "user": user, "speech": speech_score(words, bm - am).score,
                      "chat": st.chat_ratio(a, b)})
    return items


# ---------------------------------------------------------------- cost

def estimate(items: list[dict], n_calls: int) -> None:
    sys_tok = len(jd.SYSTEM_PROMPT) / CHARS_PER_TOKEN
    user_tok = [len(it["user"]) / CHARS_PER_TOKEN for it in items[:n_calls]]
    if not user_tok:
        print("nothing to call")
        return
    mean_user = sum(user_tok) / len(user_tok)
    lo, hi = (((sys_tok + mean_user) * PRICE_IN + o * PRICE_OUT) / 1e6 for o in EST_OUTPUT_TOKENS)
    n = len(user_tok)
    print(f"prompt size: system ~{sys_tok:.0f} tokens (fixed), user ~{mean_user:.0f} tokens on average "
          f"(max {max(user_tok):.0f}); output {EST_OUTPUT_TOKENS[0]}-{EST_OUTPUT_TOKENS[1]} tokens assumed")
    print(f"estimated cost: ${lo:.4f}-${hi:.4f} per call, ${lo * n:.2f}-${hi * n:.2f} for {n} calls "
          f"(input alone ${(sys_tok + mean_user) * PRICE_IN / 1e6 * n:.2f}; no cache hits assumed: the "
          f"system prompt is likely under the model's minimum cacheable size)")


def has_credentials() -> bool:
    try:
        import anthropic
    except ImportError:
        print("the anthropic package is not installed (uv pip install anthropic)")
        return False
    client = anthropic.AsyncAnthropic()
    return any(getattr(client, a, None) for a in ("api_key", "auth_token", "credentials"))


# ---------------------------------------------------------------- calls

async def run_calls(items: list[dict], out: Path, model: str, effort: str, concurrency: int) -> None:
    import anthropic

    client = anthropic.AsyncAnthropic()
    cfg = Judge(enabled=True, model=model, effort=effort)
    sem = asyncio.Semaphore(concurrency)
    spent = {"in": 0, "out": 0, "cw": 0, "cr": 0}

    async def one(it: dict) -> None:
        async with sem:
            t0 = time.perf_counter()
            row = {"id": it["id"], "stream": it["stream"], "label": it["label"]}
            try:
                response = await client.messages.parse(
                    model=cfg.model, max_tokens=jd.MAX_TOKENS, output_config={"effort": cfg.effort},
                    system=[{"type": "text", "text": it["system"], "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": it["user"]}],
                    output_format=jd.Verdict, timeout=jd.REQUEST_TIMEOUT_S)
                u = response.usage
                spent["in"] += u.input_tokens
                spent["out"] += u.output_tokens
                spent["cw"] += u.cache_creation_input_tokens or 0
                spent["cr"] += u.cache_read_input_tokens or 0
                row["usage"] = {"in": u.input_tokens, "out": u.output_tokens,
                                "cache_write": u.cache_creation_input_tokens or 0,
                                "cache_read": u.cache_read_input_tokens or 0}
                row["stop_reason"] = response.stop_reason
                if response.stop_reason not in ("refusal", "max_tokens") and response.parsed_output:
                    v = response.parsed_output
                    row.update(score=max(0, min(10, v.score)), is_moment=v.is_moment, reason=v.reason,
                               caption=jd.clean_caption(v.caption))
            except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            row["wall_s"] = round(time.perf_counter() - t0, 2)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            print(f"  {it['id']:<10} {it['label']:<7} -> {row.get('score', row.get('error', row.get('stop_reason')))}")

    await asyncio.gather(*(one(it) for it in items))
    cost = (spent["in"] * PRICE_IN + spent["out"] * PRICE_OUT + spent["cw"] * PRICE_CACHE_WRITE
            + spent["cr"] * PRICE_CACHE_READ) / 1e6
    print(f"spent: {spent} tokens, ~${cost:.2f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--max-calls", type=int, default=140, help="API calls at most (one per window)")
    ap.add_argument("--model", default=Judge().model)
    ap.add_argument("--effort", default=Judge().effort)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path(__file__).with_name("results.jsonl"))
    ap.add_argument("--estimate-only", action="store_true")
    ap.add_argument("--transcribe", default="", help="faster-whisper model to transcribe windows with "
                                                     "(default: use aux_transcript.json)")
    args = ap.parse_args(argv)

    items = build_items(args.dataset, args.transcribe)
    done: dict[str, dict] = {}
    if args.out.exists():
        for line in args.out.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if "score" in r:
                    done[r["id"]] = r
    todo = [it for it in items if it["id"] not in done][:max(0, args.max_calls)]
    print(f"{len(items)} windows, {len(done)} already judged in {args.out}, {len(todo)} to call")
    estimate(items, len(todo) or len(items))

    print("\nbaselines (no API):")
    report("speech lexical", [(it["speech"], it["label"]) for it in items])
    report("chat ratio (+10 s)", [(it["chat"], it["label"]) for it in items])
    print(f"  reference: {REFERENCE}")

    if todo and not args.estimate_only:
        if not has_credentials():
            print("\nno Anthropic credential (ANTHROPIC_API_KEY or `ant auth login`): not calling the API")
            return 0
        print(f"\ncalling {args.model} (effort {args.effort}) for {len(todo)} windows ...")
        asyncio.run(run_calls(todo, args.out, args.model, args.effort, args.concurrency))
        for line in args.out.read_text().splitlines():
            r = json.loads(line) if line.strip() else {}
            if "score" in r:
                done[r["id"]] = r

    if done:
        print(f"\nmoment judge ({len(done)} windows judged):")
        report("judge score", [(r["score"], r["label"]) for r in done.values()])
        report("judge score+is_moment", [(r["score"] + 0.5 * r["is_moment"], r["label"]) for r in done.values()])
        sub = [it for it in items if it["id"] in done]
        report("speech, same windows", [(it["speech"], it["label"]) for it in sub])
        report("chat, same windows", [(it["chat"], it["label"]) for it in sub])
        ranks = {it["id"]: it for it in sub}
        fused = [((r["score"] / 10) + min(ranks[r["id"]]["chat"], 10) / 10, r["label"]) for r in done.values()]
        report("judge + chat", fused)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
