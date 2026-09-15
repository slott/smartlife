#!/usr/bin/env python3
"""
Smart Life / Tuya Web Camera & SD Card Client
=============================================
Communicates with https://protect-eu.ismartlife.me to:
1. Authenticate and persist login sessions (QR code scan).
2. Query connected cameras, rooms, and online statuses.
3. Query recorded events / timeline segments on each camera's MicroSD card.
4. Stream and pull/record selected events from the SD card to MP4 files.
"""

import os
import sys
import time
import json
import base64
import struct
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple, Union

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext, Response
except ImportError:
    print("Playwright is required. Please install it using: pip install playwright && playwright install chromium")
    sys.exit(1)


DEFAULT_PORTAL_URL = "https://protect-eu.ismartlife.me"
DEFAULT_SESSION_FILE = "session.json"

WEBGL_HOOK_SCRIPT = """
const origGetContext = HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext = function(type, attrs) {
    if (typeof type === 'string' && type.includes('webgl')) {
        attrs = Object.assign({}, attrs, { preserveDrawingBuffer: true });
    }
    return origGetContext.call(this, type, attrs);
};

// Keep document visibility active so timers and animations do not get throttled
try {
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible' });
    Object.defineProperty(document, 'hidden', { get: () => false });
    window.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
} catch (e) {}

// Intercept Web Audio API to capture camera audio tracks
window.__recordDests = [];
window.__gainNodes = [];
const origAC = window.AudioContext || window.webkitAudioContext;
if (origAC) {
    const origConnect = AudioNode.prototype.connect;
    AudioNode.prototype.connect = function(destination, ...args) {
        if (destination instanceof AudioDestinationNode || (destination && destination.constructor && destination.constructor.name === 'AudioDestinationNode')) {
            const ctx = this.context;
            if (this instanceof GainNode) {
                // Tame Tuya's default 50x gain to prevent harsh digital clipping
                this.gain.value = 2.5;
                window.__gainNodes.push(this);
            }
            if (!ctx.__recordDest) {
                ctx.__recordDest = ctx.createMediaStreamDestination();
                try {
                    const osc = ctx.createOscillator();
                    osc.frequency.value = 5; // 5 Hz infrasound (inaudible to human ears)
                    const carrierGain = ctx.createGain();
                    carrierGain.gain.value = 0.00001;
                    osc.connect(carrierGain);
                    carrierGain.connect(ctx.__recordDest);
                    osc.start();
                } catch(e) {}
                ctx.addEventListener('statechange', () => {
                    if (ctx.state === 'closed') {
                        const idx = window.__recordDests.indexOf(ctx.__recordDest);
                        if (idx !== -1) window.__recordDests.splice(idx, 1);
                    }
                });
                window.__recordDests.push(ctx.__recordDest);
            }
            try {
                origConnect.call(this, ctx.__recordDest, ...args);
            } catch (e) {}
        }
        return origConnect.call(this, destination, ...args);
    };
}

// Intercept WebRTC DataChannels to capture pristine raw hardware RTP packets (H.265 / G.711)
window.__rtpQueue = [];
window.__rtpCollecting = false;
const origPC = window.RTCPeerConnection;
if (origPC) {
    const origCreateDC = origPC.prototype.createDataChannel;
    origPC.prototype.createDataChannel = function(label, opts) {
        const dc = origCreateDC.apply(this, arguments);
        try {
            dc.addEventListener('message', e => {
                if (window.__rtpCollecting && e.data instanceof ArrayBuffer && e.data.byteLength >= 12) {
                    window.__rtpQueue.push(new Uint8Array(e.data));
                }
            });
        } catch (err) {}
        return dc;
    };
    window.RTCPeerConnection.prototype = origPC.prototype;
}
"""

class SmartLifeError(Exception):
    """Base exception for Smart Life operations."""
    pass


class CameraBusyError(SmartLifeError):
    """Raised when the camera is locked or busy with another session (e.g. Smart Life mobile app)."""
    pass


class EventNotFoundError(SmartLifeError):
    """Raised when the requested event or date has no recordings."""
    pass


class SmartLifeClient:
    def __init__(
        self,
        portal_url: str = DEFAULT_PORTAL_URL,
        session_path: str = DEFAULT_SESSION_FILE,
        headless: bool = True,
    ):
        self.portal_url = portal_url.rstrip("/")
        self.playback_url = f"{self.portal_url}/playback"
        self.login_url = f"{self.portal_url}/login"
        self.session_path = Path(session_path)
        self.headless = headless

    def is_logged_in(self) -> bool:
        """Checks whether a valid saved session exists."""
        if not self.session_path.exists():
            return False
        try:
            with open(self.session_path, "r") as f:
                data = json.load(f)
                cookies = data.get("cookies", [])
                has_ssid = any(c.get("name") == "s-sid" and c.get("value") for c in cookies)
                return has_ssid
        except Exception:
            return False

    def login(self, headed: bool = True, timeout: int = 120) -> bool:
        """
        Interactive login flow:
        - Opens the portal in a browser window (or headless with saved QR code).
        - Waits for the user to scan the QR code with Smart Life or Tuya app.
        - Once logged in, persists cookies and local storage to session_path.
        """
        print(f"Opening login page at: {self.login_url}")
        print("Please have your Smart Life or Tuya mobile app ready to scan the QR code.")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not headed,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--autoplay-policy=no-user-gesture-required"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            page.goto(self.playback_url, wait_until="networkidle")

            if "login" in page.url:
                print("\nWaiting for QR code to render...")
                try:
                    qr_img = page.wait_for_selector("img[src^='data:image']", timeout=15000)
                    if qr_img:
                        src = qr_img.get_attribute("src") or ""
                        if "base64," in src:
                            b64_data = src.split("base64,")[1]
                            qr_bytes = base64.b64decode(b64_data)
                            qr_path = Path("login_qr.png")
                            with open(qr_path, "wb") as f:
                                f.write(qr_bytes)
                            print(f"QR code saved to: {qr_path.resolve()}")
                            if sys.platform == "darwin" and not headed:
                                subprocess.run(["open", str(qr_path)], check=False)
                except Exception as e:
                    print("Notice while looking for QR code:", e)

                print(f"\n[!] Please scan the QR code using the Smart Life app within {timeout} seconds...")

                start_time = time.time()
                while time.time() - start_time < timeout:
                    if "playback" in page.url:
                        break
                    page.wait_for_timeout(1000)

            if "playback" not in page.url:
                print("Login timed out or was not completed.")
                browser.close()
                return False

            print("Login successful! Saving session state...")
            context.storage_state(path=str(self.session_path))
            print(f"Session saved to: {self.session_path.resolve()}")
            browser.close()
            return True

    def _get_authenticated_context(self, p, headless: Optional[bool] = None) -> tuple:
        """Launches browser with saved session."""
        if not self.session_path.exists():
            raise FileNotFoundError(
                f"Session file '{self.session_path}' not found. Please run 'python smartlife.py login' first."
            )

        is_headless = self.headless if headless is None else headless
        browser = p.chromium.launch(
            headless=is_headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenFrame",
            ],
        )
        context = browser.new_context(
            storage_state=str(self.session_path),
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        context.add_init_script(WEBGL_HOOK_SCRIPT)
        page = context.new_page()
        return browser, context, page

    def get_cameras(self) -> List[Dict[str, Any]]:
        """
        Retrieves the list of cameras and rooms associated with the account.
        """
        cameras = []
        captured = False

        with sync_playwright() as p:
            browser, context, page = self._get_authenticated_context(p, headless=True)

            def handle_response(response: Response):
                nonlocal cameras, captured
                if "/api/new/common/roomList" in response.url:
                    try:
                        data = response.json()
                        rooms = data.get("result", [])
                        room_map = {}
                        for r in rooms:
                            r_name = r.get("roomName", "")
                            if r_name != "All Devices":
                                for d in r.get("deviceList", []):
                                    room_map[d.get("deviceId")] = r_name

                        for r in rooms:
                            if r.get("roomId") == "ALL_DEVICE" or r.get("roomName") == "All Devices":
                                for dev in r.get("deviceList", []):
                                    dev_id = dev.get("deviceId")
                                    cameras.append(
                                        {
                                            "devId": dev_id,
                                            "deviceName": dev.get("deviceName", "Unknown"),
                                            "room": room_map.get(dev_id, "Default Room"),
                                            "online": dev.get("online", False),
                                            "category": dev.get("category", ""),
                                            "p2pType": dev.get("p2pType", 4),
                                            "productId": dev.get("productId", ""),
                                        }
                                    )
                                captured = True
                                break
                    except Exception:
                        pass

            page.on("response", handle_response)

            try:
                page.goto(self.playback_url, wait_until="networkidle")

                if "login" in page.url:
                    raise PermissionError(
                        "Session expired or invalid. Please re-run 'python smartlife.py login'."
                    )

                start_w = time.time()
                while not captured and time.time() - start_w < 5:
                    page.wait_for_timeout(300)

                return cameras
            finally:
                browser.close()

    def _select_sd_date(self, page: Page, target_date_str: str) -> bool:
        """
        Helper to select a specific date on the SD card playback calendar.
        """
        picker_selector = "[class*='coreGrid_video_sd_active'] .ant-picker:not(.ant-picker-disabled)"
        try:
            page.wait_for_selector(picker_selector, state="visible", timeout=15000)
        except Exception:
            pass

        sd_picker = page.locator(picker_selector)
        if sd_picker.count() == 0:
            return False

        # Click picker to open calendar dropdown
        sd_picker.click()
        page.wait_for_timeout(400)

        dropdown = page.locator(".ant-picker-dropdown:not(.ant-picker-dropdown-hidden)")
        dropdown.wait_for(state="visible", timeout=6000)

        target_dt = target_date_str.split("-")
        target_year = int(target_dt[0])

        for _ in range(36):
            target_cell = page.locator(f".ant-picker-cell[title='{target_date_str}']")
            if target_cell.count() > 0 and target_cell.first.is_visible():
                target_cell.first.dispatch_event("click")
                page.wait_for_timeout(600)
                return True

            year_btn = page.locator(".ant-picker-year-btn")
            if year_btn.count() > 0:
                cur_year_str = "".join(c for c in year_btn.first.inner_text() if c.isdigit())
                cur_year = int(cur_year_str) if cur_year_str else target_year
                if target_year < cur_year:
                    page.locator(".ant-picker-header-super-prev-btn").first.click()
                    page.wait_for_timeout(200)
                    continue
                elif target_year > cur_year:
                    page.locator(".ant-picker-header-super-next-btn").first.click()
                    page.wait_for_timeout(200)
                    continue

            prev_btn = page.locator(".ant-picker-header-prev-btn")
            if prev_btn.count() > 0 and prev_btn.first.is_visible():
                prev_btn.first.click()
                page.wait_for_timeout(200)
            else:
                break

        return False

    def get_sd_events(
        self, camera_identifier: str, target_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Queries recorded events on the camera's MicroSD card for a specific date (YYYY-MM-DD).
        """
        if not target_date:
            target_date = datetime.now().strftime("%Y-%m-%d")

        target_dt = target_date.split("-")
        exp_year = int(target_dt[0])
        exp_month = int(target_dt[1])
        exp_day = int(target_dt[2])

        sd_events = []
        initial_list_done = False
        target_list_done = False

        busy_detected = False

        with sync_playwright() as p:
            browser, context, page = self._get_authenticated_context(p, headless=True)

            def handle_response(response: Response):
                nonlocal sd_events, initial_list_done, target_list_done, busy_detected
                if "allocate" in response.url:
                    try:
                        data = response.json()
                        if data.get("status") == "error" or data.get("errorCode") == "THIRD_SERVICE_ERROR" or data.get("success") is False:
                            busy_detected = True
                    except Exception:
                        pass
                if "/api/jarvis/sd/list" in response.url:
                    try:
                        req_data = json.loads(response.request.post_data or "{}") if response.request.post_data else {}
                        initial_list_done = True
                        data = response.json()
                        if data.get("status") == "error" or data.get("success") is False or data.get("errorCode") == "THIRD_SERVICE_ERROR":
                            busy_detected = True
                        elif (
                            not busy_detected
                            and req_data.get("year") == exp_year
                            and req_data.get("month") == exp_month
                            and req_data.get("day") == exp_day
                        ):
                            raw_list = data.get("result", [])
                            if isinstance(raw_list, list):
                                sd_events = raw_list
                                target_list_done = True
                    except Exception:
                        pass

            page.on("response", handle_response)

            try:
                page.goto(self.playback_url, wait_until="networkidle")

                if "login" in page.url:
                    raise PermissionError("Session expired. Please re-run 'python smartlife.py login'.")

                today_str = datetime.now().strftime("%Y-%m-%d")

                for attempt in range(1, 4):
                    busy_detected = False
                    target_list_done = False
                    initial_list_done = False
                    sd_events = []

                    if attempt > 1:
                        print(f"Retrying connection for camera '{camera_identifier}' (attempt {attempt}/3)...")
                        page.wait_for_timeout(6000)
                        page.reload(wait_until="networkidle")

                    # 1. Switch to SD tab (third item in typeSwitch_item)
                    page.wait_for_selector("[class*='typeSwitch_item']", timeout=15000)
                    tab_items = page.locator("[class*='typeSwitch_item']").all()
                    if len(tab_items) >= 3:
                        tab_items[2].click()
                        page.wait_for_timeout(800)

                    # 2. Expand 'All Devices' tree node
                    all_devices = page.locator(".ant-tree-node-content-wrapper:has-text('All Devices')")
                    if all_devices.count() > 0:
                        all_devices.first.click()
                        page.wait_for_timeout(500)

                    # 3. Select target camera
                    cam_node = page.locator(f".ant-tree-node-content-wrapper:has-text('{camera_identifier}')")
                    if cam_node.count() > 0:
                        cam_node.first.click()
                    else:
                        titles = page.locator(".ant-tree-title").all()
                        for t in titles:
                            if camera_identifier.lower() in t.inner_text().strip().lower():
                                t.click()
                                break

                    # 4. Wait for initial camera connection & timeline to render
                    start_init = time.time()
                    while not initial_list_done and time.time() - start_init < 25:
                        if busy_detected:
                            break
                        page.wait_for_timeout(200)

                    if busy_detected:
                        print(f"Camera reported busy (attempt {attempt}/3). Retrying in 6s (ensure mobile app is closed)...")
                        continue

                    page.wait_for_timeout(4000)

                    # 5. Change date if not today
                    if target_date != today_str:
                        self._select_sd_date(page, target_date)
                        start_w = time.time()
                        while not target_list_done and time.time() - start_w < 15:
                            if busy_detected:
                                break
                            page.wait_for_timeout(200)

                        if busy_detected:
                            print(f"Camera reported busy (attempt {attempt}/3) while switching date. Retrying in 6s...")
                            continue
                    else:
                        target_list_done = True

                    if target_list_done and not busy_detected:
                        break

                if busy_detected or not target_list_done:
                    raise CameraBusyError(
                        f"Camera '{camera_identifier}' is busy or in use by another session (e.g. Smart Life app). "
                        f"Please ensure the mobile app is closed and retry in 10-15 seconds."
                    )

                # Format results
                formatted = []
                for i, ev in enumerate(sd_events, start=1):
                    st = ev.get("st", 0)
                    ed = ev.get("ed", 0)
                    duration = max(0, ed - st)
                    st_str = datetime.fromtimestamp(st).strftime("%Y-%m-%d %H:%M:%S") if st else "N/A"
                    ed_str = datetime.fromtimestamp(ed).strftime("%Y-%m-%d %H:%M:%S") if ed else "N/A"
                    formatted.append(
                        {
                            "id": i,
                            "st": st,
                            "ed": ed,
                            "duration": duration,
                            "startTime": st_str,
                            "endTime": ed_str,
                        }
                    )

                return formatted
            finally:
                browser.close()

    def _record_canvas(
        self,
        page: Page,
        record_dur: int,
        out_path: Union[str, Path],
    ) -> str:
        """
        Captures video from the decoded WebRTC player canvas using MediaRecorder
        and remuxes it to an MP4 file using ffmpeg.
        """
        out_path = Path(out_path)
        webm_temp = out_path.with_suffix(".webm")

        # Wait for player canvas to be visible with dimensions > 300x200
        try:
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('canvas')).some(c => { const r = c.getBoundingClientRect(); return r.width > 300 && r.height > 200; })",
                timeout=10000
            )
        except Exception:
            pass

        # Brief settle time for active streaming
        time.sleep(1.0)

        # Ensure any AudioContexts are resumed and gains are tamed
        try:
            page.evaluate("""() => {
                if (window.__gainNodes) {
                    for (const g of window.__gainNodes) {
                        try { g.gain.value = 2.5; } catch(e) {}
                    }
                }
                if (window.__recordDests) {
                    for (const d of window.__recordDests) {
                        if (d.context && d.context.state === 'suspended') {
                            d.context.resume();
                        }
                    }
                }
            }""")
        except Exception:
            pass

        recorder_js = """() => {
            const canvases = Array.from(document.querySelectorAll('canvas')).filter(c => {
                const r = c.getBoundingClientRect();
                return r.width > 300 && r.height > 200;
            });
            if (canvases.length === 0) return { error: "no_canvas" };
            const canvas = canvases[0];

            let vStream;
            try {
                vStream = canvas.captureStream(20);
            } catch (e) {
                return { error: "captureStream_failed", details: String(e) };
            }

            const combinedStream = new MediaStream();
            vStream.getVideoTracks().forEach(t => combinedStream.addTrack(t));

            // Attach Web Audio track from the latest active AudioContext
            const runningDests = (window.__recordDests || []).filter(d => d.context && d.context.state === 'running');
            if (runningDests.length > 0) {
                const activeDest = runningDests[runningDests.length - 1];
                const aTracks = activeDest.stream.getAudioTracks();
                if (aTracks.length > 0) {
                    combinedStream.addTrack(aTracks[0]);
                }
            }

            const chunks = [];
            window.__recordedChunks = chunks;

            let rec;
            try {
                const mime = MediaRecorder.isTypeSupported('video/webm;codecs=vp8,opus')
                    ? 'video/webm;codecs=vp8,opus'
                    : 'video/webm';
                rec = new MediaRecorder(combinedStream, { mimeType: mime });
            } catch (e) {
                rec = new MediaRecorder(combinedStream);
            }

            rec.ondataavailable = (e) => {
                if (e.data && e.data.size > 0) chunks.push(e.data);
            };

            rec.onerror = (err) => {
                window.__recError = String(err);
            };

            rec.start(100);
            window.__rec = rec;
            return {
                ok: true,
                audioTracks: combinedStream.getAudioTracks().length,
                videoTracks: combinedStream.getVideoTracks().length
            };
        }"""
        res = page.evaluate(recorder_js)
        if not isinstance(res, dict) or not res.get("ok"):
            raise RuntimeError(f"Failed to initialize MediaRecorder: {res}")

        has_audio = res.get("audioTracks", 0) > 0
        audio_info = "with audio" if has_audio else "video-only"
        print(f"Streaming and capturing video ({record_dur}s, {audio_info})...")

        steps = max(1, record_dur)
        for s in range(1, steps + 1):
            page.wait_for_timeout(1000)
            pct = int((s / steps) * 100)
            print(f"\r  Progress: {pct}% [{s}/{steps}s]", end="")
            sys.stdout.flush()
        print("")

        stop_js = """() => {
            return new Promise((resolve) => {
                const rec = window.__rec;
                const chunks = window.__recordedChunks || [];
                if (!rec) return resolve({ error: "no_recorder" });

                let finished = false;
                const extract = () => {
                    if (finished) return;
                    finished = true;
                    try {
                        if (chunks.length === 0) {
                            return resolve({
                                error: "empty_blob",
                                chunkCount: chunks.length,
                                recState: rec.state
                            });
                        }
                        const blob = new Blob(chunks, { type: 'video/webm' });
                        const reader = new FileReader();
                        reader.onloadend = () => {
                            resolve({ b64: reader.result });
                        };
                        reader.onerror = (err) => resolve({ error: "reader_error", details: String(err) });
                        reader.readAsDataURL(blob);
                    } catch (e) {
                        resolve({ error: "extract_exception", details: String(e) });
                    }
                };

                // Fallback timer to prevent hanging
                setTimeout(extract, 2500);

                if (rec.state === 'inactive') {
                    extract();
                } else {
                    rec.onstop = () => extract();
                    try {
                        rec.stop();
                    } catch (e) {
                        extract();
                    }
                }
            });
        }
        """
        stop_result = page.evaluate(stop_js)
        if not isinstance(stop_result, dict) or not stop_result.get("b64"):
            print(f"Stop result diagnostic: {stop_result}")
            raise RuntimeError(f"Failed to capture video data: {stop_result.get('error', 'unknown error') if isinstance(stop_result, dict) else stop_result}")

        base64_video = stop_result["b64"]
        if "," in base64_video:
            base64_video = base64_video.split(",", 1)[1]
        video_bytes = base64.b64decode(base64_video)
        with open(webm_temp, "wb") as f:
            f.write(video_bytes)

        print(f"Captured stream ({len(video_bytes)} bytes). Remuxing to MP4...")

        # Detect any initial AV offset so video and audio start in exact synchronization
        start_seek = 0.0
        try:
            probe_proc = subprocess.run([
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "packet=pts_time",
                "-read_intervals",
                "%+2",
                "-of",
                "json",
                str(webm_temp),
            ], capture_output=True, text=True)
            probe_data = json.loads(probe_proc.stdout or "{}")
            pkts = probe_data.get("packets", [])
            if pkts and "pts_time" in pkts[0]:
                first_v_pts = float(pkts[0]["pts_time"])
                if first_v_pts > 0.05:
                    start_seek = first_v_pts
        except Exception:
            pass

        # Remux WebM to MP4 using ffmpeg with even padding filter and AAC audio
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(webm_temp),
            ]
            if start_seek > 0.0:
                cmd.extend(["-ss", str(start_seek)])

            cmd.extend([
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2,fps=fps=20",
                "-fps_mode",
                "cfr",
                "-map",
                "0:v",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-af",
                "aresample=async=1:first_pts=0",
                str(out_path),
            ])
            sub = subprocess.run(cmd, capture_output=True, text=True)
            if sub.returncode == 0 and out_path.exists():
                webm_temp.unlink(missing_ok=True)
                print(f"Successfully saved to: {out_path.resolve()} ({out_path.stat().st_size} bytes)")
                return str(out_path)
            else:
                print(f"Notice: remux failed ({sub.stderr}), kept as WebM: {webm_temp.resolve()}")
                return str(webm_temp)
        except FileNotFoundError:
            print(f"ffmpeg not found. Saved as WebM: {webm_temp.resolve()}")
            return str(webm_temp)

    @staticmethod
    def _unpack_rtp_packets(packets: List[bytes]) -> Tuple[bytes, bytes, int, int, float]:
        """
        Unpacks raw RFC 7798 / RFC 6184 RTP packets into Annex-B H.265 bitstream and PCM audio.
        Also calculates the exact hardware frame rate (FPS) from the RTP timestamps.
        """
        h265_bytes = bytearray()
        audio_bytes = bytearray()
        start_code = b"\x00\x00\x00\x01"

        fu_buffer = bytearray()
        has_seen_keyframe = False
        v_frames = 0
        a_packets = 0
        v_timestamps = []

        for data in packets:
            if len(data) < 12:
                continue

            cc = data[0] & 0x0F
            has_ext = (data[0] & 0x10) != 0
            header_len = 12 + 4 * cc
            if has_ext and len(data) >= header_len + 4:
                ext_len = struct.unpack(">H", data[header_len + 2:header_len + 4])[0]
                header_len += 4 + 4 * ext_len

            if len(data) <= header_len:
                continue

            pt = data[1] & 0x7F
            ts = struct.unpack(">I", data[4:8])[0]
            payload = data[header_len:]

            if pt == 0:  # 16-bit 8kHz PCM audio
                if not has_seen_keyframe:
                    continue
                audio_bytes.extend(payload)
                a_packets += 1
            elif pt in (95, 96, 105):  # Video
                if len(payload) < 2:
                    continue
                nal_type = (payload[0] >> 1) & 0x3F

                if nal_type in (32, 33, 34, 19, 20):
                    has_seen_keyframe = True

                if nal_type == 49:  # Fragmentation Unit (FU)
                    if len(payload) < 3:
                        continue
                    fu_header = payload[2]
                    start_bit = (fu_header & 0x80) != 0
                    end_bit = (fu_header & 0x40) != 0
                    orig_nal = fu_header & 0x3F

                    if orig_nal in (19, 20):
                        has_seen_keyframe = True

                    if not has_seen_keyframe:
                        continue

                    if start_bit:
                        nal_hdr0 = (payload[0] & 0x81) | ((orig_nal & 0x3F) << 1)
                        nal_hdr1 = payload[1]
                        fu_buffer = bytearray(start_code + bytes([nal_hdr0, nal_hdr1]) + payload[3:])
                    else:
                        fu_buffer.extend(payload[3:])

                    if end_bit:
                        h265_bytes.extend(fu_buffer)
                        fu_buffer = bytearray()
                        v_frames += 1
                        v_timestamps.append(ts)
                elif nal_type <= 47:  # Single NAL
                    if nal_type in (32, 33, 34, 19, 20):
                        has_seen_keyframe = True
                    if not has_seen_keyframe:
                        continue
                    h265_bytes.extend(start_code + payload)
                    if nal_type in (1, 19, 20):
                        v_frames += 1
                        v_timestamps.append(ts)

        # Determine exact hardware framerate:
        # 1. If audio is available, the continuous 8kHz audio stream provides ground-truth real-world duration:
        #    audio_dur = len(audio_bytes) / 16000.0 (8000 Hz, 16-bit mono = 16000 bytes/sec)
        #    Setting fps = (v_frames - 1) / audio_dur ensures exact 1:1 real-time sync with zero drift or speedup!
        # 2. If audio is not available, calculate from the median delta of consecutive video RTP timestamps.
        # 3. Fallback to 20.0 fps if all else fails.
        fps = 20.0
        audio_dur = len(audio_bytes) / 16000.0 if len(audio_bytes) >= 16000 else 0.0
        if audio_dur > 2.0 and v_frames > 5:
            calc_fps = (v_frames - 1) / audio_dur
            if 5.0 <= calc_fps <= 60.0:
                fps = calc_fps
        elif len(v_timestamps) > 1:
            deltas = [(v_timestamps[i + 1] - v_timestamps[i]) & 0xFFFFFFFF for i in range(len(v_timestamps) - 1)]
            valid_deltas = [d for d in deltas if 1500 <= d <= 18000]
            if valid_deltas:
                import statistics
                med = statistics.median(valid_deltas)
                if med > 0:
                    calc_fps = 90000.0 / med
                    if 5.0 <= calc_fps <= 60.0:
                        fps = calc_fps

        return bytes(h265_bytes), bytes(audio_bytes), v_frames, a_packets, fps

    def _record_direct_stream(
        self,
        page: Page,
        record_dur: int,
        out_path: Union[str, Path],
    ) -> Optional[str]:
        """
        Captures the pristine native 2560x1440 H.265 hardware video stream directly
        from the WebRTC DataChannel (fmp4Stream), completely bypassing browser
        software decoding and canvas rendering.
        """
        out_path = Path(out_path)
        # Reset queue and activate collection
        page.evaluate("() => { window.__rtpQueue = []; window.__rtpCollecting = true; }")

        all_packets = []
        try:
            for s in range(record_dur):
                time.sleep(1)
                b64 = page.evaluate("""() => {
                    if (!window.__rtpQueue || window.__rtpQueue.length === 0) return '';
                    const q = window.__rtpQueue;
                    window.__rtpQueue = [];
                    let total = 0;
                    for (let i = 0; i < q.length; i++) total += 2 + q[i].length;
                    const merged = new Uint8Array(total);
                    let offset = 0;
                    for (let i = 0; i < q.length; i++) {
                        const p = q[i];
                        merged[offset++] = (p.length >> 8) & 0xFF;
                        merged[offset++] = p.length & 0xFF;
                        merged.set(p, offset);
                        offset += p.length;
                    }
                    let binary = '';
                    const len = merged.byteLength;
                    const chunkSize = 0x8000;
                    for (let i = 0; i < len; i += chunkSize) {
                        binary += String.fromCharCode.apply(null, merged.subarray(i, Math.min(i + chunkSize, len)));
                    }
                    return btoa(binary);
                }""")
                if b64:
                    raw = base64.b64decode(b64)
                    offset = 0
                    raw_len = len(raw)
                    while offset + 2 <= raw_len:
                        plen = (raw[offset] << 8) | raw[offset + 1]
                        offset += 2
                        all_packets.append(raw[offset:offset+plen])
                        offset += plen

                pct = int(((s + 1) / record_dur) * 100)
                print(f"  Progress: {pct}% [{s+1}/{record_dur}s, {len(all_packets)} packets]...", end="\r", flush=True)

            print()
        finally:
            try:
                page.evaluate("() => { window.__rtpCollecting = false; }")
            except Exception:
                pass

        if len(all_packets) < 50:
            return None

        v_bytes, a_bytes, v_frames, a_pkts, fps = self._unpack_rtp_packets(all_packets)
        if v_frames < 10 or len(v_bytes) < 50000:
            return None

        print(f"Captured {v_frames} native 2K frames ({len(v_bytes)} bytes H.265) @ {fps:.2f} fps, {a_pkts} audio packets ({len(a_bytes)} bytes PCM). Muxing to MP4...")

        temp_h265 = out_path.with_suffix(".temp.h265")
        temp_pcm = out_path.with_suffix(".temp.pcm")
        temp_h265.write_bytes(v_bytes)
        temp_pcm.write_bytes(a_bytes)

        try:
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-r", f"{fps:.4f}",
                "-i", str(temp_h265),
                "-f", "s16le", "-ar", "8000", "-ac", "1",
                "-i", str(temp_pcm),
                "-map", "0:v:0",
                "-map", "1:a:0?",
                "-c:v", "copy",
                "-tag:v", "hvc1",
                "-bsf:v", f"setts=ts=N/{fps:.4f}/TB",
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "48000",
                "-af", "volume=2.5",
                "-shortest",
                str(out_path)
            ]
            sub = subprocess.run(cmd, capture_output=True, text=True)
            if sub.returncode == 0 and out_path.exists():
                print(f"Successfully saved pristine hardware video to: {out_path.resolve()} ({out_path.stat().st_size} bytes)")
                return str(out_path)
            else:
                print(f"  Direct mux notice: {sub.stderr}")
                return None
        finally:
            temp_h265.unlink(missing_ok=True)
            temp_pcm.unlink(missing_ok=True)

    def _record_stream(
        self,
        page: Page,
        record_dur: int,
        out_path: Union[str, Path],
    ) -> str:
        """
        Records an SD card event using the direct hardware H.265 stream if available,
        falling back transparently to canvas capture if direct stream extraction fails.
        """
        try:
            saved = self._record_direct_stream(page, record_dur, out_path)
            if saved:
                return saved
        except Exception as e:
            print(f"  Direct hardware capture notice: {e}")

        print("  Falling back to canvas recording...")
        return self._record_canvas(page, record_dur, out_path)

    def pull_all_events(
        self,
        camera_identifier: str,
        target_date: Optional[str] = None,
        output_dir: Optional[str] = "./recordings",
        max_duration: Optional[int] = None,
        target_event_index: Optional[int] = None,
    ) -> List[str]:
        """
        Pulls and records all SD card events for a specific camera and date in a single browser session.
        """
        if not target_date:
            target_date = datetime.now().strftime("%Y-%m-%d")

        target_dt = target_date.split("-")
        exp_year = int(target_dt[0])
        exp_month = int(target_dt[1])
        exp_day = int(target_dt[2])

        sd_events = []
        initial_list_done = False
        target_list_done = False

        out_dir = Path(output_dir or "./recordings")
        out_dir.mkdir(parents=True, exist_ok=True)
        saved_files = []

        print(f"Connecting to camera '{camera_identifier}' for date {target_date}...")

        busy_detected = False

        current_dev_id = None
        current_session_id = None

        with sync_playwright() as p:
            browser, context, page = self._get_authenticated_context(p, headless=True)

            def handle_response(response: Response):
                nonlocal sd_events, initial_list_done, target_list_done, busy_detected, current_dev_id, current_session_id
                if "allocate" in response.url:
                    try:
                        data = response.json()
                        if data.get("result", {}).get("sessionId"):
                            current_session_id = data["result"]["sessionId"]
                        if data.get("status") == "error" or data.get("errorCode") == "THIRD_SERVICE_ERROR" or data.get("success") is False:
                            busy_detected = True
                    except Exception:
                        pass
                if "/api/jarvis/sd/list" in response.url:
                    try:
                        req_data = json.loads(response.request.post_data or "{}") if response.request.post_data else {}
                        if req_data.get("devId"):
                            current_dev_id = req_data["devId"]
                        initial_list_done = True
                        data = response.json()
                        if data.get("status") == "error" or data.get("success") is False or data.get("errorCode") == "THIRD_SERVICE_ERROR":
                            busy_detected = True
                        elif (
                            not busy_detected
                            and req_data.get("year") == exp_year
                            and req_data.get("month") == exp_month
                            and req_data.get("day") == exp_day
                        ):
                            raw_list = data.get("result", [])
                            if isinstance(raw_list, list):
                                sd_events = raw_list
                                target_list_done = True
                    except Exception:
                        pass

            page.on("response", handle_response)

            try:
                page.goto(self.playback_url, wait_until="networkidle")

                if "login" in page.url:
                    raise PermissionError("Session expired. Please re-run 'python smartlife.py login'.")

                today_str = datetime.now().strftime("%Y-%m-%d")

                max_attempts = 4
                for attempt in range(1, max_attempts + 1):
                    busy_detected = False
                    target_list_done = False
                    initial_list_done = False
                    sd_events = []

                    if attempt > 1:
                        backoff = (attempt - 1) * 8
                        print(f"Retrying connection for camera '{camera_identifier}' (attempt {attempt}/{max_attempts}, waiting {backoff}s)...")
                        page.wait_for_timeout(backoff * 1000)
                        page.reload(wait_until="networkidle")

                    # 1. Switch to SD tab
                    page.wait_for_selector("[class*='typeSwitch_item']", timeout=15000)
                    tab_items = page.locator("[class*='typeSwitch_item']").all()
                    if len(tab_items) >= 3:
                        tab_items[2].click()
                        page.wait_for_timeout(800)

                    # 2. Expand All Devices
                    all_devices = page.locator(".ant-tree-node-content-wrapper:has-text('All Devices')")
                    if all_devices.count() > 0:
                        all_devices.first.click()
                        page.wait_for_timeout(500)

                    # 3. Select target camera
                    cam_node = page.locator(f".ant-tree-node-content-wrapper:has-text('{camera_identifier}')")
                    if cam_node.count() > 0:
                        cam_node.first.click()
                    else:
                        titles = page.locator(".ant-tree-title").all()
                        for t in titles:
                            if camera_identifier.lower() in t.inner_text().strip().lower():
                                t.click()
                                break

                    # 4. Wait for initial camera connection & timeline to render
                    start_init = time.time()
                    while not initial_list_done and time.time() - start_init < 25:
                        if busy_detected:
                            break
                        page.wait_for_timeout(200)

                    if busy_detected:
                        print(f"Camera reported busy (attempt {attempt}/{max_attempts}). Retrying in 6s (ensure mobile app is closed)...")
                        continue

                    page.wait_for_timeout(4000)

                    # 5. Change date if not today
                    if target_date != today_str:
                        self._select_sd_date(page, target_date)
                        start_w = time.time()
                        while not target_list_done and time.time() - start_w < 15:
                            if busy_detected:
                                break
                            page.wait_for_timeout(200)

                        if busy_detected:
                            print(f"Camera reported busy (attempt {attempt}/{max_attempts}) while switching date. Retrying in 6s...")
                            continue
                    else:
                        target_list_done = True

                    if target_list_done and not busy_detected:
                        break

                if busy_detected or not target_list_done:
                    raise CameraBusyError(
                        f"Camera '{camera_identifier}' is busy or in use by another session (e.g. Smart Life app) after {max_attempts} attempts. "
                        f"Please ensure the mobile app is closed and retry in 10-15 seconds."
                    )

                if not sd_events:
                    print(f"No recorded events found on SD card for {target_date}.")
                    return []

                print(f"Found {len(sd_events)} event(s) on SD card for {target_date}.")

                if target_event_index is not None and (target_event_index < 1 or target_event_index > len(sd_events)):
                    raise EventNotFoundError(
                        f"Event #{target_event_index} does not exist on {target_date} for camera '{camera_identifier}' (available events: 1 to {len(sd_events)})."
                    )

                for idx, ev in enumerate(sd_events, start=1):
                    if target_event_index is not None and idx != target_event_index:
                        continue

                    st = ev.get("st", 0)
                    ed = ev.get("ed", 0)
                    duration = max(0, ed - st)
                    st_str = datetime.fromtimestamp(st).strftime("%Y-%m-%d %H:%M:%S") if st else "N/A"
                    clean_cam = "".join(c if c.isalnum() else "_" for c in camera_identifier)
                    clean_time = st_str.replace(" ", "_").replace(":", "-")
                    out_name = f"{clean_cam}_{target_date}_event_{idx}_{clean_time}.mp4"
                    out_path = out_dir / out_name

                    print(f"\n[{idx}/{len(sd_events)}] Event #{idx}: {st_str} ({duration}s)")
                    if out_path.exists():
                        print(f"  Skipping: {out_path} already exists.")
                        saved_files.append(str(out_path))
                        continue

                    # Seek SD playback to this specific event
                    if current_dev_id and current_session_id and st:
                        try:
                            page.evaluate("""(params) => {
                                return fetch('/api/jarvis/sd/play', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify(params)
                                }).then(r => r.json());
                            }""", {
                                "devId": current_dev_id,
                                "sessionId": current_session_id,
                                "startTime": st,
                                "endTime": ed,
                                "playTime": st
                            })
                            page.wait_for_timeout(3000)
                        except Exception as e:
                            print(f"  Warning: failed to seek playback: {e}")

                    record_dur = min(duration, max_duration) if max_duration else duration
                    saved = self._record_stream(page, record_dur, out_path)
                    saved_files.append(saved)

                return saved_files
            finally:
                browser.close()

    def pull_event(
        self,
        camera_identifier: str,
        target_date: str,
        event_index: int = 1,
        target_event: Optional[Dict[str, Any]] = None,
        output_path: Optional[str] = None,
        max_duration: Optional[int] = None,
    ) -> str:
        """
        Pulls / records a specific SD event from the camera's MicroSD card.
        """
        out_dir = Path(output_path).parent if output_path else Path("./recordings")
        saved = self.pull_all_events(
            camera_identifier=camera_identifier,
            target_date=target_date,
            output_dir=str(out_dir),
            max_duration=max_duration,
            target_event_index=event_index,
        )
        if saved:
            return saved[0]
        raise EventNotFoundError(f"No recordings pulled for camera '{camera_identifier}' on {target_date} (event #{event_index}).")
