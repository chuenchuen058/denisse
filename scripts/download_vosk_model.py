"""
Denisse -- one-time setup: download the offline Vosk speech model used by
stt_vosk.py / wake_word.py for live, local speech-to-text.

Not committed to git (~200MB unpacked, see .gitignore) -- run this once per
machine:
    python scripts/download_vosk_model.py

Model: vosk-model-en-us-0.22-lgraph (~128MB zipped), a middle ground between
Vosk's small model (40MB, noticeably less accurate) and its full-size 0.22
model (1.8GB, marginal accuracy gain for a lot more disk/RAM). See
https://alphacephei.com/vosk/models for other sizes if accuracy still isn't
good enough after real-world use -- swap the URL below and re-run.
"""

import os
import sys
import zipfile
from urllib.request import urlretrieve

MODEL_NAME = "vosk-model-en-us-0.22-lgraph"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(ROOT, "models")
MODEL_DIR = os.path.join(MODELS_DIR, MODEL_NAME)


def _progress(block_num, block_size, total_size):
    if total_size <= 0:
        return
    pct = min(100, block_num * block_size * 100 // total_size)
    print(f"\rDownloading {MODEL_NAME}... {pct}%", end="", flush=True)


def main() -> None:
    if os.path.isdir(MODEL_DIR):
        print(f"Already present: {MODEL_DIR}")
        return

    os.makedirs(MODELS_DIR, exist_ok=True)
    zip_path = os.path.join(MODELS_DIR, f"{MODEL_NAME}.zip")
    try:
        urlretrieve(MODEL_URL, zip_path, _progress)
        print()
        print("Extracting...")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(MODELS_DIR)
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

    if os.path.isdir(MODEL_DIR):
        print(f"Done: {MODEL_DIR}")
    else:
        print("Extraction finished but expected folder not found -- check the zip's top-level name.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
