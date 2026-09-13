#!/usr/bin/env python3
"""
Tuya / Smart Life SD Card Direct Extractor
==========================================
Extracts and converts raw recordings directly from a physical MicroSD card
mounted on your computer (or a directory dump of the card).

Reverse engineered from the Tuya/Smart Life Web Player protocol:
- SD cards contain directory structures with `.info` metadata and `.media` raw binary chunks.
- Each frame in `.media` files has a 24-byte binary header:
    Offset 0-3:   int32 (LE) frameType (0: P-frame, 1: I-frame/Keyframe, 3: Audio)
    Offset 4-7:   int32 (LE) frameLength (payload size)
    Offset 8-15:  int64 (LE) timestamp (milliseconds)
    Offset 16-23: int64 (LE) reserved
    Offset 24+:   Raw NAL units (H.264 / H.265 stream)
"""

import os
import sys
import json
import struct
import argparse
import subprocess
from pathlib import Path
from datetime import datetime


def parse_media_file(media_path):
    """
    Parses a Tuya .media binary file and extracts video and audio frames.
    Yields (frame_type, timestamp_ms, payload_bytes)
    """
    with open(media_path, "rb") as f:
        data = f.read()

    length = len(data)
    offset = 0

    while offset + 24 <= length:
        frame_type, frame_len, timestamp = struct.unpack_from("<iiq", data, offset)
        offset += 24  # 4 + 4 + 8 + 8 reserved

        if offset + frame_len > length:
            break

        payload = data[offset : offset + frame_len]
        offset += frame_len

        yield frame_type, timestamp, payload


def extract_recording_directory(dir_path, output_mp4_path):
    """
    Extracts all .media files in a Tuya recording directory into an MP4 file.
    """
    dir_path = Path(dir_path)
    info_file = dir_path / ".info"
    codec = "h264"
    if info_file.exists():
        try:
            with open(info_file, "r") as f:
                info = json.load(f)
                codec_id = info.get("codec", 2)
                if codec_id == 4:
                    codec = "hevc"
                elif codec_id == 2:
                    codec = "h264"
        except Exception:
            pass

    media_files = sorted(dir_path.glob("*.media"), key=lambda p: p.name)
    if not media_files:
        return False

    raw_stream_path = output_mp4_path.with_suffix(f".{codec}")
    first_ts = None
    last_ts = None
    frame_count = 0

    with open(raw_stream_path, "wb") as out_f:
        for media_file in media_files:
            for frame_type, ts, payload in parse_media_file(media_file):
                if frame_type in (0, 1):  # Video frame
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                    frame_count += 1
                    # Ensure start code exists (Annex B format)
                    if not (payload.startswith(b"\x00\x00\x00\x01") or payload.startswith(b"\x00\x00\x01")):
                        out_f.write(b"\x00\x00\x00\x01")
                    out_f.write(payload)

    if frame_count == 0:
        if raw_stream_path.exists():
            raw_stream_path.unlink()
        return False

    # Convert raw H.264/H.265 to MP4 with ffmpeg if available
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            codec,
            "-i",
            str(raw_stream_path),
            "-c:v",
            "copy",
            str(output_mp4_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            raw_stream_path.unlink(missing_ok=True)
            duration_s = (last_ts - first_ts) / 1000.0 if (last_ts and first_ts) else 0.0
            print(f"  Extracted: {output_mp4_path.name} ({frame_count} frames, ~{duration_s:.1f}s)")
            return True
        else:
            print(f"  ffmpeg remux notice: {res.stderr.strip()}, raw stream kept at {raw_stream_path}")
            return True
    except FileNotFoundError:
        print(f"  Saved raw stream: {raw_stream_path} (install ffmpeg to auto-convert to MP4)")
        return True


def scan_and_extract_card(sd_card_root, output_dir):
    """
    Recursively finds all Tuya recording folders on the SD card and extracts them.
    """
    sd_root = Path(sd_card_root)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Scanning SD card at: {sd_root}")
    # Tuya cameras organize recordings by date and hour / event
    candidate_dirs = []
    for root, dirs, files in os.walk(sd_root):
        if any(f.endswith(".media") for f in files) or ".info" in files:
            candidate_dirs.append(Path(root))

    if not candidate_dirs:
        print("No Tuya recording directories (.media / .info) found on the provided path.")
        return

    print(f"Found {len(candidate_dirs)} recording segment directory(ies). Extracting...")
    for idx, cdir in enumerate(candidate_dirs, start=1):
        rel_name = "_".join(cdir.relative_to(sd_root).parts) or f"rec_{idx}"
        output_file = out_dir / f"{rel_name}.mp4"
        print(f"[{idx}/{len(candidate_dirs)}] Processing {cdir} -> {output_file.name}")
        extract_recording_directory(cdir, output_file)

    print(f"\nExtraction complete! Files saved to: {out_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract video recordings directly from a physical Tuya/Smart Life MicroSD card."
    )
    parser.add_argument("sd_card_path", help="Path to mounted SD card or dump folder")
    parser.add_argument(
        "--output-dir",
        "-o",
        default="./extracted_sd_recordings",
        help="Destination directory for MP4 videos (default: ./extracted_sd_recordings)",
    )
    args = parser.parse_args()

    scan_and_extract_card(args.sd_card_path, args.output_dir)


if __name__ == "__main__":
    main()
