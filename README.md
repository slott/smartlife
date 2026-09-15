# Smart Life / Tuya Camera SD Card Manager

A suite of tools to query cameras, list recorded events on their local MicroSD cards, and pull/download the video recordings from the [Tuya / Smart Life IPC Portal](https://protect-eu.ismartlife.me/playback).

---

## Technical Overview & How It Works

Smart Life / Tuya cameras store continuous or motion-triggered video recordings on their local MicroSD cards. Unlike cloud storage subscriptions (which store files on remote AWS S3 / Tuya OSS servers with direct HTTPS download links), Tuya **does not** provide a static HTTP download endpoint for local SD card files.

Instead:
1. **Camera Discovery**: Devices are managed via Tuya's backend APIs (`/api/device/sort/list` and `/api/new/common/homeList`).
2. **SD Card Event Querying**: Recorded time segments on the SD card are queried for any target date via `/api/jarvis/sd/list`.
3. **P2P Video Streaming**: To view or pull content, the web portal negotiates a WebRTC session (`/api/jarvis/sd/allocate` and `/api/jarvis/sd/play`) directly with the camera via Tuya's MQTT signaling broker (`m1.tuyaeu.com`). The camera then streams the H.264 / H.265 video chunks.
4. **Direct Hardware Stream Pulling**: Our tool taps directly into the WebRTC `fmp4Stream` DataChannel to capture the camera's original, untouched 2560x1440 (2K QHD) H.265 / HEVC bitstream and 16-bit 8 kHz PCM audio packets—bypassing browser software downscaling and canvas re-encoding entirely. It dynamically synchronizes the video frame rate against the hardware audio clock and muxes into pristine, QuickTime-ready MP4 files (`hvc1` + AAC).
5. **Direct SD Card Extraction**: If you ever mount the physical MicroSD card directly to your computer, [`extract_tuya_sd.py`](file:///Users/msh/git/smartlife/extract_tuya_sd.py) decodes the camera's raw `.media` binary frames into standard MP4 files in seconds.

---

## Prerequisites

- **Python 3.8+**
- **FFmpeg** (installed on macOS via `brew install ffmpeg`)
- **Dependencies**:
  ```bash
  pip install -r requirements.txt
  playwright install chromium
  ```

---

## Quick Start

### 1. Authenticate (One-time)
Run the login command. A browser window will open to display the Tuya QR code:
```bash
python smartlife.py login
```
*Open your **Smart Life** or **Tuya** mobile app, tap the QR scanner icon, and scan the QR code on your screen.*

Once authorized, your session tokens and cookies are securely stored in [`session.json`](file:///Users/msh/git/smartlife/session.json). All subsequent commands run fully automatically and headlessly.

*(Optional)* If you are running on a remote headless server without GUI:
```bash
python smartlife.py login --headless
```
This saves `login_qr.png` locally for you to scan.

---

### 2. List Connected Cameras
List all cameras registered to your Smart Life account:
```bash
python smartlife.py cameras
```
Output:
```text
======================================================================
 #   Camera Name                  Status     Device ID
======================================================================
 1   Front Porch                  ONLINE     bf0a1...
 2   Driveway                     ONLINE     bf8e2...
 3   Living Room                  ONLINE     bf1c3...
======================================================================
```

---

### 3. Query Recorded Events on SD Card
Query the recorded segments on the SD card for a camera on today's date (or any past date):

```bash
# Today's recorded events
python smartlife.py events --camera "Front Porch"

# Specific date (YYYY-MM-DD)
python smartlife.py events --camera "Front Porch" --date 2026-09-13
```
Output:
```text
===========================================================================
 Recorded Events on SD Card (14 segments found) - Date: 2026-09-13
===========================================================================
 ID    Start Time             End Time               Duration
---------------------------------------------------------------------------
 1     2026-09-13 08:14:22    2026-09-13 08:15:10    48s
 2     2026-09-13 09:32:05    2026-09-13 09:33:15    1m 10s
 3     2026-09-13 14:05:00    2026-09-13 14:06:20    1m 20s
===========================================================================
 Total recorded footage: 3m 18s across 3 event(s).
```

---

### 4. Pull / Download an Event Recording
Pull a specific recorded event by its ID into an MP4 file:

```bash
python smartlife.py pull --camera "Front Porch" --date 2026-09-13 --event 1
```

Options:
- `-o / --output`: Specify custom destination path (e.g. `-o ./porch_morning.mp4`).
- `--max-duration`: Cap the recording duration in seconds (e.g. `--max-duration 30`).

---

### 5. Bulk Pull All Events for a Day
Download every recorded event on a camera's SD card for a given day into a folder:

```bash
python smartlife.py pull-all --camera "Front Porch" --date 2026-09-13 --output-dir ./recordings/porch
```

---

## Direct Physical SD Card Extractor

If you remove the MicroSD card from the camera and plug it into your computer / card reader, you can extract all raw recordings directly without network overhead:

```bash
python extract_tuya_sd.py /Volumes/SD_CARD/ -o ./extracted_videos
```
- Reads the camera's proprietary `.media` and `.info` files.
- Decodes the 24-byte binary frame headers and NAL units.
- Converts each recorded segment directly to `.mp4` using `ffmpeg`.
