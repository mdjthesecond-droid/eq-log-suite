"""Watches item_capture.screenshot_dir (config/local.yaml) for new item-window
screenshots, OCRs each one, and lands the raw result in `item_captures` as
'pending'. Nothing here writes to the actual item catalog (`item_info`) --
EQ's stat-window font/layout isn't clean enough to trust unreviewed, so a
human confirms/corrects each capture on /items/review before it counts.

Usage:
    python -m eq_log_suite.item_capture_watcher

screenshot_dir (config/local.yaml) is a dedicated, manually-curated folder
-- deliberately not EQL's own live screenshot function/folder, which
captures the whole screen (chat, achievements, other overlapping item
tooltips) and meant a lot of after-the-fact cleanup here. Take a tight,
single-item screenshot with a rectangle-select tool and drop it in; this
still watches and OCRs automatically, only the source changed.
"""

import time
from pathlib import Path

import pytesseract
from PIL import Image, ImageEnhance, ImageOps

from eq_log_suite import db

POLL_INTERVAL_SECONDS = 2.0
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")

# Confirmed real via A/B test against an actual capture (2026-08-05,
# "Diamondine Earring +3"): raw tesseract on these screenshots as-is (source
# images are small, ~420x490px item-display windows, likely well under
# tesseract's preferred ~20-30px cap-height) badly garbled several lines --
# dropped "Race: ALL" and "Ear" entirely, mangled "This item can be
# upgraded." beyond recognition, lost the decimal in "Weight: 0.1" and
# digits in "HP: 15". 4x upscale + grayscale + contrast boost, with --psm 6
# (tesseract's default full-page auto-segmentation was fighting this
# panel's compact multi-column label/value layout), fixed all of the above
# with only trivial errors left (a "5" read as ")", one roman numeral "I"
# read as "|"). A harder binary threshold on top of that was tried too and
# made a couple of lines worse (broke otherwise-correct text), so skipped.
_UPSCALE_FACTOR = 4
_CONTRAST_FACTOR = 2.0
_TESSERACT_CONFIG = "--psm 6"


def _preprocess(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")
    image = image.resize((image.width * _UPSCALE_FACTOR, image.height * _UPSCALE_FACTOR), Image.LANCZOS)
    image = ImageOps.grayscale(image)
    return ImageEnhance.Contrast(image).enhance(_CONTRAST_FACTOR)


def _already_captured(conn, path: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM item_captures WHERE screenshot_path=%s", (path,))
        return cur.fetchone() is not None


def _guess_name(ocr_text: str) -> str | None:
    # Best-effort only -- just the first non-blank line. No stat-field
    # parsing here: the real OCR'd layout hasn't been seen yet, so guessing
    # a regex for AC/stats/etc. without real captures to check it against
    # would just be a heuristic dressed up as a parser. The review UI is
    # where a human turns this raw text into something trustworthy.
    for line in ocr_text.splitlines():
        line = line.strip()
        if line:
            return line[:255]
    return None


def process_file(conn, path: Path):
    image = _preprocess(Image.open(path))
    ocr_text = pytesseract.image_to_string(image, config=_TESSERACT_CONFIG)
    parsed_name = _guess_name(ocr_text)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO item_captures (screenshot_path, raw_ocr_text, parsed_name) "
            "VALUES (%s, %s, %s)",
            (str(path), ocr_text, parsed_name),
        )
    conn.commit()
    print(f"[item_capture_watcher] captured {path.name} -> {parsed_name!r}")


def watch(screenshot_dir: str):
    watch_path = Path(screenshot_dir)
    watch_path.mkdir(parents=True, exist_ok=True)
    print(f"[item_capture_watcher] watching {watch_path}")

    conn = db.get_connection()
    while True:
        for path in sorted(watch_path.iterdir()):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if _already_captured(conn, str(path)):
                continue
            try:
                process_file(conn, path)
            except Exception as e:
                print(f"[item_capture_watcher] failed on {path.name}: {type(e).__name__}: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


def main():
    cfg = db.config().get("item_capture", {})
    screenshot_dir = cfg.get("screenshot_dir")
    if not screenshot_dir:
        print("No item_capture.screenshot_dir set in config/local.yaml -- nothing to watch.")
        return
    watch(screenshot_dir)


if __name__ == "__main__":
    main()
