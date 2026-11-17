"""Score every stream of the labelled dataset: audio rows + chat peaks (two configs).

    python -I prepare.py MODEL.tflite DATASET_DIR WORK_DIR CONFIG.toml APP_PYTHON

Per stream S in DATASET_DIR/manifest.json writes to WORK_DIR:
  S.audio.npz        YAMNet rows from streams/S/audio16k.wav, t = wall_start + row second
  S.chat.json        argus replay with CONFIG.toml's detection settings
  S.chat_relaxed.json  same with the relaxed thresholds (chat_peaks.py --relaxed)
APP_PYTHON is the app's interpreter (has argus) used for chat_peaks.py.
The wav is the same audio the buffer segments hold; StreamScorer fed segment by segment
gives identical rows (checked), so this is only a faster path for the offline run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scorer as S  # noqa: E402


def main() -> None:
    model_path, ds, work, cfg, app_py = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5]
    work.mkdir(parents=True, exist_ok=True)
    man = json.loads((ds / "manifest.json").read_text())
    model = S.Yamnet(model_path)
    for st in man["streams"]:
        name = st["name"]
        wav = ds / st.get("audio_16k_mono", f"streams/{name}/audio16k.wav")
        chat = ds / st.get("chat", f"streams/{name}/chat.jsonl")
        if not wav.exists():
            print(f"{name}: no audio, skipped")
            continue
        c0 = time.process_time()
        res = S.score_file(model, wav)
        res["t"] = st["wall_start"] + res["t"]
        np.savez_compressed(work / f"{name}.audio.npz", **res)
        print(f"{name}: {len(res['rms'])} s audio scored in {time.process_time() - c0:.1f} CPU s")
        for suffix, extra in (("chat", []), ("chat_relaxed", ["--relaxed"])):
            subprocess.run([app_py, "-I", str(HERE / "chat_peaks.py"), cfg, str(chat),
                            str(work / f"{name}.{suffix}.json"), name, *extra], check=True)


if __name__ == "__main__":
    main()
