"""Generate a pet clip with the local MiniMax-H3 model and keep the frames.

Two decisions here are worth stating, because the obvious version of this script
is worse in both places.

**It saves frames, not a video.** The graph ends at SaveImage rather than
SaveVideo, so ComfyUI hands back one PNG per frame. That skips video encoding
entirely -- no colour subsampling, no inter-frame compression on soft anime
edges, and no ffmpeg dependency, which this machine does not have. Extracting
frames from an mp4 afterwards would only lose quality that we never needed to
throw away.

**first_frame and last_frame both take the approved master.** The node accepts
either or both. Pinning both ends makes the clip return to the pose it started
from, which is what a looping pet clip needs; pinning only the first end
produces a clip that drifts away and cannot loop. It also anchors identity to
art that a human already signed off on, instead of asking the model to reinvent
the character -- the failure mode that produced a seven-head-tall "chibi" during
the gpt-image-2 pass.

The model emits opaque RGB. Matting is a separate step: run
scripts/remove-image-background.py or normalize-pack-scale.py over the output,
which already use isnet-anime because it is the one model that does not eat the
character's white apron and socks.

Needs a running server:  /media/data/minimax_h3/run_comfyui.sh
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = "http://127.0.0.1:8188"

TEXT_ENCODER = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"

MODES = {
    "i2v": {
        "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "steps": 8,
        "node": "MiniMaxH3ImageToVideo",
    },
    "r2v": {
        "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "lora": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
        "steps": 4,
        "node": "MiniMaxH3ReferenceToVideo",
    },
}

# The model snaps length to a 17k+5 grid; 124 frames is ~5s at 24fps and is the
# bottom of the trained range.
DEFAULT_LENGTH = 124


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{SERVER}{path}", data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as handle:
        return json.loads(handle.read())


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{SERVER}{path}", timeout=60) as handle:
        return json.loads(handle.read())


def upload_image(path: Path) -> str:
    """Upload one PNG and return the name ComfyUI knows it by."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'.encode()
    body += b"Content-Type: image/png\r\n\r\n"
    body += path.read_bytes()
    body += f"\r\n--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{SERVER}/upload/image",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as handle:
        return json.loads(handle.read())["name"]


def build_graph(args, uploaded: dict[str, str]) -> dict:
    mode = MODES[args.mode]
    graph: dict[str, dict] = {
        "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": TEXT_ENCODER, "type": "minimax", "device": "default"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": VIDEO_VAE}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": AUDIO_VAE}},
        "5": {"class_type": "UNETLoader", "inputs": {"unet_name": mode["unet"], "weight_dtype": "default"}},
        "6": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": ["5", 0], "lora_name": mode["lora"], "strength_model": 1.0},
        },
        "7": {"class_type": "BasicGuider", "inputs": {"model": ["6", 0], "conditioning": ["4", 0]}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {
            "class_type": "BasicScheduler",
            "inputs": {"model": ["6", 0], "scheduler": "simple", "steps": args.steps or mode["steps"], "denoise": 1.0},
        },
        "10": {"class_type": "RandomNoise", "inputs": {"noise_seed": args.seed}},
        "11": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {"noise": ["10", 0], "guider": ["7", 0], "sampler": ["8", 0], "sigmas": ["9", 0], "latent_image": ["4", 1]},
        },
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["2", 0]}},
        # SaveImage, not SaveVideo: keep every frame lossless.
        "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": args.prefix}},
    }

    node_inputs: dict = {
        "clip": ["1", 0],
        "vae": ["2", 0],
        "prompt": args.prompt,
        "width": args.width,
        "height": args.height,
        "length": args.length,
    }
    if args.mode == "i2v":
        if "first" in uploaded:
            node_inputs["first_frame"] = ["20", 0]
            graph["20"] = {"class_type": "LoadImage", "inputs": {"image": uploaded["first"]}}
        if "last" in uploaded:
            node_inputs["last_frame"] = ["21", 0]
            graph["21"] = {"class_type": "LoadImage", "inputs": {"image": uploaded["last"]}}
    else:
        node_inputs["audio_vae"] = ["3", 0]
        node_inputs["ref_image_size"] = args.ref_image_size
        for index, key in enumerate(sorted(k for k in uploaded if k.startswith("ref"))):
            slot = f"3{index}"
            graph[slot] = {"class_type": "LoadImage", "inputs": {"image": uploaded[key]}}
            node_inputs[f"ref_image_{index + 1}"] = [slot, 0]

    graph["4"] = {"class_type": MODES[args.mode]["node"], "inputs": node_inputs}
    return graph


def wait_for(prompt_id: str, poll_s: float = 5.0, timeout_s: float = 3600.0) -> dict:
    started = time.monotonic()
    while True:
        history = get(f"/history/{prompt_id}")
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error" or status.get("completed") is False and status.get("messages"):
                for message in status.get("messages", []):
                    if message[0] in {"execution_error", "execution_interrupted"}:
                        raise SystemExit(f"generation failed: {json.dumps(message[1])[:600]}")
            if entry.get("outputs"):
                return entry
        if time.monotonic() - started > timeout_s:
            raise SystemExit("timed out waiting for the generation")
        elapsed = int(time.monotonic() - started)
        print(f"  … {elapsed}s", flush=True)
        time.sleep(poll_s)


def download_frames(entry: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for node_output in entry["outputs"].values():
        for image in node_output.get("images", []):
            query = urllib.parse.urlencode(
                {"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")}
            )
            with urllib.request.urlopen(f"{SERVER}/view?{query}", timeout=120) as handle:
                payload = handle.read()
            target = out_dir / image["filename"]
            temp = target.with_suffix(".tmp.png")
            temp.write_bytes(payload)
            temp.replace(target)
            written.append(target)
    return sorted(written)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a pet clip with local MiniMax-H3 and keep the frames")
    parser.add_argument("--mode", choices=sorted(MODES), default="i2v")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--first-frame", help="PNG pinned as frame 0 (i2v)")
    parser.add_argument("--last-frame", help="PNG pinned as the final frame (i2v); pass the same file to loop")
    parser.add_argument("--ref", action="append", default=[], help="reference image (r2v), repeatable up to 9")
    parser.add_argument("--ref-image-size", choices=("match", "max"), default="max")
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--length", type=int, default=DEFAULT_LENGTH)
    parser.add_argument("--steps", type=int, default=0, help="0 uses the turbo LoRA's step count")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--prefix", default="petclip/clip")
    parser.add_argument("--out", required=True, help="directory for the PNG frames")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.mode == "i2v" and not args.first_frame:
        raise SystemExit("i2v needs --first-frame")
    if args.mode == "r2v" and not args.ref:
        raise SystemExit("r2v needs at least one --ref")

    uploaded: dict[str, str] = {}
    if not args.dry_run:
        try:
            get("/system_stats")
        except Exception as error:  # noqa: BLE001
            raise SystemExit(f"ComfyUI is not reachable at {SERVER} ({error}). Run run_comfyui.sh first.")
        if args.first_frame:
            uploaded["first"] = upload_image(Path(args.first_frame))
        if args.last_frame:
            uploaded["last"] = upload_image(Path(args.last_frame))
        for index, ref in enumerate(args.ref):
            uploaded[f"ref{index}"] = upload_image(Path(ref))
    else:
        if args.first_frame:
            uploaded["first"] = Path(args.first_frame).name
        if args.last_frame:
            uploaded["last"] = Path(args.last_frame).name
        for index, ref in enumerate(args.ref):
            uploaded[f"ref{index}"] = Path(ref).name

    graph = build_graph(args, uploaded)
    if args.dry_run:
        print(json.dumps(graph, indent=2, ensure_ascii=False)[:2400])
        print(f"\ndry run: {args.mode}, {args.length} frames at {args.width}x{args.height}; nothing was queued")
        return 0

    print(f"queueing {args.mode}: {args.length} frames at {args.width}x{args.height}, seed {args.seed}")
    response = post("/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex})
    prompt_id = response["prompt_id"]
    entry = wait_for(prompt_id)
    frames = download_frames(entry, Path(args.out))
    if not frames:
        raise SystemExit("the run produced no frames")
    print(f"wrote {len(frames)} frames to {args.out}")
    print("next: matte them, e.g.")
    print(f"  python scripts/remove-image-background.py --input {args.out} --output {args.out}-rgba")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
