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
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext, Response
except ImportError:
    print("Playwright is required. Please install it using: pip install playwright && playwright install chromium")
    sys.exit(1)


DEFAULT_PORTAL_URL = "https://protect-eu.ismartlife.me"
DEFAULT_SESSION_FILE = "session.json"


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
            ],
        )
        context = browser.new_context(
            storage_state=str(self.session_path),
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
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
                        # Map deviceId to roomName
                        for r in rooms:
                            r_name = r.get("roomName", "")
                            if r_name != "All Devices":
                                for d in r.get("deviceList", []):
                                    room_map[d.get("deviceId")] = r_name

                        # Extract from All Devices
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

                # Wait briefly if response is still incoming
                start_w = time.time()
                while not captured and time.time() - start_w < 5:
                    page.wait_for_timeout(300)

                return cameras
            finally:
                browser.close()

    def get_sd_events(
        self, camera_identifier: str, target_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Queries recorded events on the camera's MicroSD card for a specific date (YYYY-MM-DD).
        """
        if not target_date:
            target_date = datetime.now().strftime("%Y-%m-%d")

        sd_events = []
        captured_response = False

        with sync_playwright() as p:
            browser, context, page = self._get_authenticated_context(p, headless=True)

            def handle_response(response: Response):
                nonlocal sd_events, captured_response
                if "/api/jarvis/sd/list" in response.url:
                    try:
                        data = response.json()
                        raw_list = data.get("result", [])
                        if isinstance(raw_list, list):
                            sd_events = raw_list
                            captured_response = True
                    except Exception:
                        pass

            page.on("response", handle_response)

            try:
                page.goto(self.playback_url, wait_until="networkidle")

                if "login" in page.url:
                    raise PermissionError("Session expired. Please re-run 'python smartlife.py login'.")

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
                    page.wait_for_timeout(600)

                # 3. Select target camera
                cam_node = page.locator(f".ant-tree-node-content-wrapper:has-text('{camera_identifier}')")
                if cam_node.count() > 0:
                    cam_node.first.click()
                else:
                    # Try partial match or index
                    titles = page.locator(".ant-tree-title").all()
                    for t in titles:
                        if camera_identifier.lower() in t.inner_text().strip().lower():
                            t.click()
                            break

                # 4. Change date if not today
                today_str = datetime.now().strftime("%Y-%m-%d")
                if target_date != today_str:
                    date_input = page.locator(".ant-picker-input input")
                    if date_input.count() > 0:
                        date_input.first.click()
                        date_input.first.fill(target_date)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(1000)

                # Wait for the SD list response
                start_w = time.time()
                while not captured_response and time.time() - start_w < 10:
                    page.wait_for_timeout(400)

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

    def pull_event(
        self,
        camera_identifier: str,
        target_date: str,
        event_index: int,
        output_path: Optional[str] = None,
        max_duration: Optional[int] = None,
    ) -> str:
        """
        Pulls / records a specific SD event from the camera's MicroSD card.
        Plays the event over WebRTC in the browser and captures the decoded canvas into an MP4 file.
        """
        events = self.get_sd_events(camera_identifier, target_date)
        if not events:
            raise ValueError(f"No SD card events found on camera '{camera_identifier}' for date {target_date}.")

        target_event = None
        for ev in events:
            if ev["id"] == event_index:
                target_event = ev
                break

        if not target_event:
            raise IndexError(f"Event ID {event_index} not found. Available IDs: 1 to {len(events)}.")

        st = target_event["st"]
        ed = target_event["ed"]
        event_dur = target_event["duration"]
        record_dur = min(event_dur, max_duration) if max_duration else event_dur

        if not output_path:
            clean_cam = "".join(c if c.isalnum() else "_" for c in camera_identifier)
            out_name = f"{clean_cam}_{target_date}_{st}.mp4"
            output_dir = Path("recordings")
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(output_dir / out_name)

        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        webm_temp = out_path.with_suffix(".webm")

        print(f"\nPulling Event #{event_index}:")
        print(f"  Time: {target_event['startTime']} -> {target_event['endTime']} ({event_dur}s)")
        print(f"  Recording duration: {record_dur}s")
        print(f"  Output path: {out_path}")

        with sync_playwright() as p:
            browser, context, page = self._get_authenticated_context(p, headless=True)
            try:
                page.goto(self.playback_url, wait_until="networkidle")

                # Switch to SD tab
                page.wait_for_selector("[class*='typeSwitch_item']", timeout=15000)
                tab_items = page.locator("[class*='typeSwitch_item']").all()
                if len(tab_items) >= 3:
                    tab_items[2].click()
                    page.wait_for_timeout(800)

                # Expand All Devices
                all_dev = page.locator(".ant-tree-node-content-wrapper:has-text('All Devices')")
                if all_dev.count() > 0:
                    all_dev.first.click()
                    page.wait_for_timeout(500)

                # Select camera
                cam_node = page.locator(f".ant-tree-node-content-wrapper:has-text('{camera_identifier}')")
                if cam_node.count() > 0:
                    cam_node.first.click()
                else:
                    titles = page.locator(".ant-tree-title").all()
                    for t in titles:
                        if camera_identifier.lower() in t.inner_text().strip().lower():
                            t.click()
                            break

                page.wait_for_timeout(3000)

                # Change date if needed
                today_str = datetime.now().strftime("%Y-%m-%d")
                if target_date != today_str:
                    date_input = page.locator(".ant-picker-input input")
                    if date_input.count() > 0:
                        date_input.first.click()
                        date_input.first.fill(target_date)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(2000)

                # Inject WebRTC / Canvas MediaRecorder
                recorder_js = """
                () => {
                    window.__chunks = [];
                    const canvases = document.querySelectorAll('canvas');
                    let playerCanvas = null;
                    for (const c of canvases) {
                        const rect = c.getBoundingClientRect();
                        if (rect.width > 300 && rect.height > 200) {
                            playerCanvas = c;
                            break;
                        }
                    }
                    if (!playerCanvas) return { error: "Player canvas not ready" };

                    const stream = playerCanvas.captureStream(25);
                    const rec = new MediaRecorder(stream, { mimeType: 'video/webm' });
                    rec.ondataavailable = (e) => {
                        if (e.data && e.data.size > 0) window.__chunks.push(e.data);
                    };
                    window.__rec = rec;
                    rec.start(100);
                    return { ok: true, width: playerCanvas.width, height: playerCanvas.height };
                }
                """

                # Start recording
                res = page.evaluate(recorder_js)
                if res.get("error"):
                    page.wait_for_timeout(3000)
                    res = page.evaluate(recorder_js)

                print(f"Streaming and capturing video ({record_dur}s)...")
                steps = max(1, record_dur)
                for s in range(steps):
                    time.sleep(1)
                    percent = int(((s + 1) / steps) * 100)
                    sys.stdout.write(f"\r  Progress: {percent}% [{s + 1}/{record_dur}s]")
                    sys.stdout.flush()
                print("")

                # Stop recording and extract buffer
                stop_js = """
                async () => {
                    return new Promise((resolve) => {
                        if (!window.__rec) return resolve("");
                        window.__rec.onstop = async () => {
                            const blob = new Blob(window.__chunks, { type: 'video/webm' });
                            const reader = new FileReader();
                            reader.onloadend = () => {
                                resolve(reader.result.split(',')[1] || "");
                            };
                            reader.readAsDataURL(blob);
                        };
                        window.__rec.stop();
                    });
                }
                """
                base64_video = page.evaluate(stop_js)
                if not base64_video:
                    raise RuntimeError("Failed to capture video data from WebRTC stream.")

                video_bytes = base64.b64decode(base64_video)
                with open(webm_temp, "wb") as f:
                    f.write(video_bytes)

                print(f"Captured stream ({len(video_bytes)} bytes). Remuxing to MP4...")

                # Remux WebM to MP4 using ffmpeg
                try:
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(webm_temp),
                        "-c:v",
                        "copy",
                        str(out_path),
                    ]
                    sub = subprocess.run(cmd, capture_output=True, text=True)
                    if sub.returncode == 0 and out_path.exists():
                        webm_temp.unlink(missing_ok=True)
                        print(f"Successfully saved to: {out_path.resolve()}")
                        return str(out_path)
                    else:
                        print(f"Notice: saved as WebM: {webm_temp.resolve()}")
                        return str(webm_temp)
                except FileNotFoundError:
                    print(f"ffmpeg not found. Saved as WebM: {webm_temp.resolve()}")
                    return str(webm_temp)
            finally:
                browser.close()
