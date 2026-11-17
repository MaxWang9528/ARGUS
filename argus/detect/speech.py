"""Speech check: transcribe an auto clip's raw cut and score how excited the streamer sounds.

Chat alone can't tell a real moment from chat that is busy with nothing happening (spam
waves, alerts). What the streamer says can: exclamations ("no way", "oh my god", "what
the ...", "let's go") and swearing are rare when nothing happens. Measured on a small
labelled set, this lexical score ranked moments above non-moments almost as well as the
chat rate did, while words per second and loudness did not help, so they are not used.

- ``speech_score`` is pure: words in, ``SpeechScore`` out.
- ``FasterWhisperTranscriber`` runs faster-whisper (CTranslate2, int8, CPU) in a worker
  thread. The model loads lazily, once. Needs the optional ``faster-whisper`` package;
  model files download to the Hugging Face cache on first use.
- ``SpeechCheck`` is what the clip pipeline calls: transcribe, score, adjust the rank,
  and optionally gate. It never raises (except on cancel): a failed check leaves the
  clip as it was.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import ConfigStore
from ..core.config import Speech as SpeechConfig
from ..core.interfaces import Transcriber
from ..core.models import Clip, Transcript, Word

log = logging.getLogger("argus.speech")

SR = 16000

# ---------------------------------------------------------------- scoring

# Things people say when something happens. Game-neutral, English. Matched against the
# lower-cased text of a word and the two after it, punctuation removed. Kept to phrases;
# everyday single words ("go", "wait", "bro") are left to the repeat cue.
EXCLAIM = re.compile(r"""(?x)^(?:
      oh\ my\ (?:god|gosh|lord|days) | oh\ (?:no|shit|crap|fuck|god|wow|hell) | no\ way
    | let'?s\ (?:go+|fucking) | what\ the\ (?:fuck|hell|heck|f) | holy\ (?:shit|crap|cow|moly|fuck)
    | are\ you\ (?:kidding|serious) | you'?re\ kidding | jesus\ christ | get\ out\ of\ here
    | i\ can'?t\ believe | bro\ what | dude\ what | wtf | omg | wo+w | who+a | sheesh | ye+s+\ ye+s
    | no+\ no+ | ah+ | aa+h+ | oh+ )\b""")
SWEAR = re.compile(r"(?i)\b(?:f+u+c+k+\w*|shit\w*|damn\w*|bitch\w*|asshole|crap|"
                   r"motherf\w*|goddamn\w*|bastard)\b")
LAUGH = re.compile(r"(?i)(?:\b(?:ha){2,}h?\b|\bhe(?:he)+\b|\blol\b|[\[(](?:laugh\w*|chuckl\w*)[\])])")

WINDOW_S = 5        # cues are counted over this many seconds
BASELINE_S = 180    # a streamer who says these all the time gets less for each
W_EXCLAIM, W_SWEAR, W_LAUGH = 1.5, 1.0, 1.0


@dataclass
class SpeechScore:
    score: float = 0.0
    hits: list[str] = field(default_factory=list)  # the phrases and words that counted


def _norm(w: str) -> str:
    return re.sub(r"[^a-z' ]", "", w.lower()).strip()


def _w(word: Word | dict) -> tuple[str, float]:
    return (word.w, word.t) if isinstance(word, Word) else (word["w"], word["t"])


def _lex(x: list[float], cap: float) -> list[float]:
    """Per second: the count over the last WINDOW_S s, capped, divided by 1 + the usual
    count per WINDOW_S s over the last BASELINE_S s."""
    out, run, base = [], 0.0, 0.0
    for i, v in enumerate(x):
        run += v - (x[i - WINDOW_S] if i >= WINDOW_S else 0.0)
        base += v - (x[i - BASELINE_S] if i >= BASELINE_S else 0.0)
        out.append(min(run, cap) / (1.0 + base / BASELINE_S * WINDOW_S))
    return out


def speech_score(words: list[Word] | list[dict], duration: float | None = None) -> SpeechScore:
    """Excitement of a transcript: the highest WINDOW_S-second score of exclamation
    phrases, swears, laughter, short words said twice ("no no", "go go") and "!".
    About 1-1.5 for one exclamation or swear; 0 for calm talk. Words/s and loudness are
    left out on purpose (they did not separate moments from non-moments)."""
    items = [_w(w) for w in words]
    if not items:
        return SpeechScore()
    n = int(max(duration or 0.0, max(t for _, t in items))) + 1
    ex, sw, la, rep = ([0.0] * n for _ in range(4))
    toks = [_norm(w) for w, _ in items]
    hits: list[str] = []
    for i, (w, t) in enumerate(items):
        s = int(t)
        if not 0 <= s < n:
            continue
        if SWEAR.search(w):
            sw[s] += 1
            hits.append(toks[i])
        if LAUGH.search(w):
            la[s] += 1
            hits.append(toks[i])
        if "!" in w:
            rep[s] += 1
        m = EXCLAIM.match(" ".join(toks[i:i + 3]))
        if m:
            ex[s] += 1
            hits.append(m.group(0))
        if (i and toks[i] and toks[i] == toks[i - 1] and len(toks[i]) <= 5
                and t - items[i - 1][1] < 1.0):
            rep[s] += 1
    e, r, s_, l_ = _lex(ex, 3), _lex(rep, 3), _lex(sw, 3), _lex(la, 2)
    best = max(W_EXCLAIM * (e[i] + 0.5 * r[i]) + W_SWEAR * s_[i] + W_LAUGH * l_[i] for i in range(n))
    return SpeechScore(score=round(best, 3), hits=list(dict.fromkeys(hits)))


def rank_factor(score: float, weight: float) -> float:
    """What the clip's rank is multiplied by: 1 at a score of 0.5 (about half an
    exclamation), up to 1 + 2.5 * weight at 3 or more, down to 1 - 0.5 * weight at 0.
    Always within 0.5-2."""
    return max(0.5, min(2.0, 1.0 + weight * (min(score, 3.0) - 0.5)))


# ---------------------------------------------------------------- transcription

def faster_whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def decode_audio(path: str | Path, start: float = 0.0, duration: float | None = None) -> Any:
    """Decode audio to 16 kHz mono float32 (a numpy array) with ffmpeg."""
    import numpy as np

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [ffmpeg, "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, check=False, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode audio: {proc.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


class FasterWhisperTranscriber:
    """faster-whisper on the CPU, int8. One model, loaded on first use; every call runs
    in a worker thread. Settings from the experiment: beam 1, temperature 0, Silero VAD,
    word timestamps, and filters against the repetition loops whisper makes on music."""

    def __init__(self, model: str = "small.en", threads: int = 2, download_root: str = "") -> None:
        self.model_name, self.threads, self.download_root = model, threads, download_root
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                t0 = time.perf_counter()
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                           cpu_threads=self.threads, num_workers=1,
                                           download_root=self.download_root or None)
                log.info("speech model %s loaded in %.1f s", self.model_name, time.perf_counter() - t0)
            return self._model

    def transcribe_sync(self, path: str | Path, start: float = 0.0, duration: float | None = None) -> Transcript:
        model = self._load()
        t0 = time.perf_counter()
        audio = decode_audio(path, start, duration)
        segs, _ = model.transcribe(
            audio, language="en", beam_size=1, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300}, word_timestamps=True,
            condition_on_previous_text=False, temperature=0.0, hallucination_silence_threshold=2.0)
        words: list[Word] = []
        for s in segs:
            if s.no_speech_prob > 0.8 and s.avg_logprob < -1.0:
                continue
            if s.compression_ratio > 2.4:  # a repetition loop (music, "go go go go ...")
                continue
            run, prev = 0, None
            for w in s.words or []:
                key = re.sub(r"\W", "", w.word.lower())
                run = run + 1 if key == prev else 0
                prev = key
                if run >= 2 or w.probability < 0.15:  # keep at most two repeats in a row
                    continue
                words.append(Word(w=w.word.strip(), t=round(w.start, 2), e=round(w.end, 2),
                                  p=round(w.probability, 3)))
        return Transcript(text=" ".join(w.w for w in words), words=words, duration=len(audio) / SR,
                          model=self.model_name, wall_s=round(time.perf_counter() - t0, 2))

    async def transcribe(self, path: Path, start: float = 0.0, duration: float | None = None) -> Transcript:
        return await asyncio.to_thread(self.transcribe_sync, path, start, duration)


# ---------------------------------------------------------------- the pipeline step

class SpeechCheck:
    """The clip pipeline's speech step. ``transcriber`` fixes the transcriber (tests,
    demo); otherwise a ``FasterWhisperTranscriber`` is made from ``[speech]`` and made
    again when its model, threads or download_root change."""

    def __init__(self, store: ConfigStore, transcriber: Transcriber | None = None) -> None:
        self.store = store
        self._fixed = transcriber
        self._made: FasterWhisperTranscriber | None = None
        self._warned = False
        self._running = 0
        self._cond = asyncio.Condition()

    def _transcriber(self, cfg: SpeechConfig) -> Transcriber | None:
        if self._fixed is not None:
            return self._fixed
        if not faster_whisper_available():
            if not self._warned:
                self._warned = True
                log.warning("speech check off: faster-whisper is not installed "
                            "(uv pip install faster-whisper, or set speech.enabled = false)")
            return None
        m = self._made
        if m is None or (m.model_name, m.threads, m.download_root) != (cfg.model, cfg.threads, cfg.download_root):
            self._made = FasterWhisperTranscriber(cfg.model, cfg.threads, cfg.download_root)
        return self._made

    async def check(self, clip: Clip) -> str | None:
        """Transcribe ``clip.raw_path``, store the transcript and score on the clip and
        multiply its rank by ``rank_factor``. Returns a reason to drop the clip when it
        scores under ``gate_threshold``, else None. A failure is logged and changes nothing."""
        cfg = self.store.get().speech
        if not cfg.enabled or not clip.raw_path:
            return None
        tr = self._transcriber(cfg)
        if tr is None:
            return None
        duration = clip.window_end - clip.window_start
        try:
            async with self._cond:
                await self._cond.wait_for(lambda: self._running < cfg.max_concurrent)
                self._running += 1
            try:
                result = await asyncio.wait_for(tr.transcribe(Path(clip.raw_path)),
                                                timeout=300 + 10 * duration)
            finally:
                async with self._cond:
                    self._running -= 1
                    self._cond.notify_all()
            sc = speech_score(result.words, result.duration or duration)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the clip goes on without it
            log.warning("clip %s: speech check skipped (%s: %s)", clip.id, type(exc).__name__, exc,
                        extra={"streamer": clip.streamer})
            return None
        clip.transcript, clip.speech_score, clip.speech_hits = result.text, sc.score, sc.hits
        factor = rank_factor(sc.score, cfg.rank_weight)
        clip.rank = round(clip.rank * factor, 4)
        log.info("clip %s: speech score %.2f (%s), rank x%.2f, %.1f s", clip.id, sc.score,
                 ", ".join(sc.hits) or "no excitement", factor, result.wall_s,
                 extra={"streamer": clip.streamer})
        if cfg.gate_threshold > 0 and sc.score < cfg.gate_threshold:
            return (f"speech check: score {sc.score:.2f} is under speech.gate_threshold "
                    f"{cfg.gate_threshold:g} (no exclamations or swearing heard)")
        return None
