"""Crop the delivered whale-girl design sheets into traceable art references.

The source files carry a `.png` extension but contain JPEG bytes, so the loader
sniffs the real format instead of trusting the name. Every output is written as
a real `.jpg`, and `reference-index.json` records where each crop came from.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from PIL import Image

DESIGN_WIDTH = 960

# (x, y, width, height) in 960-wide design-sheet coordinates. Turnaround heights
# stop above the "正视图/侧视图/背视图" captions so no text enters a reference.
CROPS: dict[str, dict[str, tuple[int, int, int, int]]] = {
    "chibi": {
        "turnaround/front": (40, 50, 300, 335),
        "turnaround/side": (320, 50, 280, 335),
        "turnaround/back": (610, 50, 260, 335),
        "expressions/expressions": (25, 480, 910, 150),
    },
    "standard": {
        "turnaround/front": (35, 45, 290, 385),
        "turnaround/side": (315, 45, 285, 385),
        "turnaround/back": (595, 45, 275, 385),
        "details/details": (20, 490, 920, 135),
        "expressions/expressions": (25, 480, 910, 150),
    },
    "slender": {
        "turnaround/front": (20, 45, 300, 417),
        "turnaround/side": (315, 45, 290, 417),
        "turnaround/back": (600, 45, 280, 417),
        "details/details": (20, 500, 920, 130),
        "expressions/expressions": (25, 490, 910, 145),
    },
}

# Which design sheet feeds which crop. Sheets 2/5 carry the detail rows,
# sheets 3/6 carry the expression rows, so the turnaround comes from the
# detail sheet and the expression strip from the expression sheet.
SHEETS: list[tuple[str, str, tuple[str, ...]]] = [
    ("角色设计1", "chibi", ("turnaround/front", "turnaround/side", "turnaround/back", "expressions/expressions")),
    ("角色设计2", "standard", ("turnaround/front", "turnaround/side", "turnaround/back", "details/details")),
    ("角色设计3", "standard", ("expressions/expressions",)),
    ("角色设计5", "slender", ("turnaround/front", "turnaround/side", "turnaround/back", "details/details")),
    ("角色设计6", "slender", ("expressions/expressions",)),
]

PURPOSES = {
    "turnaround/front": "正视图母图：脸型、发型、服装与比例基准",
    "turnaround/side": "侧视图母图：厚度、尾巴根部与裙摆",
    "turnaround/back": "背视图母图：后腰蝴蝶结与发尾渐变",
    "details/details": "细节参考：领结、头饰、围裙鲸鱼图案、裙摆纹样、鞋、尾鳍",
    "expressions/expressions": "表情参考：开心、悲伤、生气、惊讶、害怕、疑惑、害羞、得意",
}


def load_image(path: Path) -> Image.Image:
    with Image.open(path) as handle:
        return handle.convert("RGB")


def clamp_rect(rect: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    left = max(0, min(rect[0], width))
    top = max(0, min(rect[1], height))
    right = max(left, min(rect[0] + rect[2], width))
    bottom = max(top, min(rect[1] + rect[3], height))
    return left, top, right, bottom


def save_jpeg(image: Image.Image, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="JPEG", quality=95, subsampling=0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare whale-girl art references")
    parser.add_argument("--source", required=True, help="directory holding the delivered design sheets")
    parser.add_argument("--output", required=True, help="art reference output directory")
    args = parser.parse_args()

    source_dir = Path(args.source)
    output_dir = Path(args.output)
    if not source_dir.is_dir():
        raise SystemExit(f"source directory not found: {source_dir}")

    entries: list[dict[str, object]] = []
    used_stems: set[str] = set()

    for stem, pack, crop_names in SHEETS:
        source = source_dir / f"{stem}.png"
        if not source.exists():
            raise SystemExit(f"missing design sheet: {source}")
        used_stems.add(stem)
        image = load_image(source)
        scale = image.width / DESIGN_WIDTH
        archived = output_dir / "source" / f"{stem}.jpg"
        save_jpeg(image, archived)
        for crop_name in crop_names:
            rect = CROPS[pack][crop_name]
            scaled = tuple(round(value * scale) for value in rect)
            box = clamp_rect(scaled, image.size)  # type: ignore[arg-type]
            crop = image.crop(box)
            relative = Path(pack) / f"{crop_name}.jpg"
            save_jpeg(crop, output_dir / relative)
            entries.append(
                {
                    "source": f"{stem}.png",
                    "pack": pack,
                    "purpose": PURPOSES[crop_name],
                    "output": relative.as_posix(),
                    "cropRect": list(box),
                    "size": [crop.width, crop.height],
                }
            )

    for candidate in sorted(source_dir.glob("*.png")):
        if candidate.stem in used_stems:
            continue
        image = load_image(candidate)
        relative = Path("chibi") / "poses" / f"{candidate.stem}.jpg"
        save_jpeg(image, output_dir / relative)
        entries.append(
            {
                "source": candidate.name,
                "pack": "chibi",
                "purpose": "Q版动作与表情参考：只提供动作、视角和表情",
                "output": relative.as_posix(),
                "cropRect": [0, 0, image.width, image.height],
                "size": [image.width, image.height],
            }
        )

    index_path = output_dir / "reference-index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"formatVersion": 1, "designWidth": DESIGN_WIDTH, "references": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(entries)} references to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
