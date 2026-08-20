"""Build a high-resolution rig master from a low-resolution runtime frame.

The chibi pack ships at 195x260, which is fine for a frame-sequence renderer
that only ever blits the frame 1:1. A bone rig is different: it rotates, scales
and shears each layer independently, so it resamples the source constantly and
195px of line art turns to mush the first time the head tilts.

The 512 packs were generated large and downscaled, so they already have the
detail. chibi does not -- and there is no higher-resolution chibi standing pose
in art-references (the pose sheets are stickers with text and props, and the
turnaround is 300x335). Regenerating it through gpt-image-2 would risk the same
proportion drift the samples already showed, so the character is upscaled
instead: RealESRGAN anime 6B sharpens the existing approved pixels without
inventing a new character.

Alpha is resized separately with LANCZOS rather than pushed through the network,
which is trained on RGB and would fray the matte edge.

Run with the CUDA art environment:
    /home/jinpei/anaconda3/envs/dsh-art/bin/python scripts/upscale-rig-master.py ...
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from spandrel import ModelLoader

ROOT = Path(__file__).resolve().parent.parent
MODEL_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth"
DEFAULT_CACHE = Path.home() / ".cache" / "dsh-art" / "RealESRGAN_x4plus_anime_6B.pth"

TARGET = (512, 512)
FOOT_ANCHOR_Y = 0.97
MAX_HEIGHT_RATIO = 0.90
MAX_WIDTH_RATIO = 0.94


def ensure_model(path: Path) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {MODEL_URL}")
        urllib.request.urlretrieve(MODEL_URL, path)
    return path


def upscale(image: Image.Image, model_path: Path, device: str) -> Image.Image:
    model = ModelLoader().load_from_file(str(model_path))
    model.to(device).eval()
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    tensor = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).to(device)
    with torch.no_grad():
        out = model(tensor)
    out = out.clamp(0, 1).squeeze(0).permute(1, 2, 0).cpu().numpy()
    big = Image.fromarray((out * 255).round().astype(np.uint8))
    # The network never sees alpha; resizing it separately keeps the matte crisp.
    big.putalpha(image.split()[3].resize(big.size, Image.LANCZOS))
    return big


def fit_canvas(image: Image.Image) -> Image.Image:
    box = image.split()[3].getbbox()
    if box is None:
        raise SystemExit("source frame is fully transparent")
    character = image.crop(box)
    scale = min(
        (TARGET[1] * MAX_HEIGHT_RATIO) / character.height,
        (TARGET[0] * MAX_WIDTH_RATIO) / character.width,
    )
    size = (max(1, round(character.width * scale)), max(1, round(character.height * scale)))
    character = character.resize(size, Image.LANCZOS)
    canvas = Image.new("RGBA", TARGET, (0, 0, 0, 0))
    left = max(0, (TARGET[0] - size[0]) // 2)
    top = max(0, min(round(TARGET[1] * FOOT_ANCHOR_Y) - size[1], TARGET[1] - size[1]))
    canvas.paste(character, (left, top), character)
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description="Upscale a runtime frame into a 512x512 rig master")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=str(DEFAULT_CACHE))
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--keep-native", help="also write the raw 4x result here")
    args = parser.parse_args()

    device = args.device if torch.cuda.is_available() else "cpu"
    if device != args.device:
        print(f"warning: CUDA unavailable, falling back to {device}")

    source = Image.open(ROOT / args.input if not Path(args.input).is_absolute() else args.input).convert("RGBA")
    model_path = ensure_model(Path(args.model))
    big = upscale(source, model_path, device)
    print(f"{args.input}: {source.size} -> {big.size} on {device}")
    if args.keep_native:
        Image.Image.save(big, args.keep_native)

    fitted = fit_canvas(big)
    assert fitted.size == TARGET, f"expected {TARGET}, got {fitted.size}"
    assert fitted.mode == "RGBA"
    assert fitted.split()[3].getextrema()[0] == 0, "no transparent pixel survived"
    box = fitted.split()[3].getbbox()
    out = Path(args.output)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_suffix(".tmp.png")
    fitted.save(temp, "PNG")
    temp.replace(out)
    print(f"wrote {out} character {box[2]-box[0]}x{box[3]-box[1]} at bottom {box[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
