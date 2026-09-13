#!/usr/bin/env python3
"""
Smart Life / Tuya Web Camera & SD Card Client
=============================================
Communicates with https://protect-eu.ismartlife.me to:
1. Authenticate and persist login sessions (QR code scan).
2. Query connected cameras and their online statuses.
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
from datetime import datetime, date
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

            # If redirected to login, find and present the QR code
            if "login" in page.url:
                print("\nWaiting for QR code to render...")
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

                print(f"\n[!] Please scan the QR code using the Smart Life app within {timeout} seconds...")

                # Wait for redirect to /playback
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
        Retrieves the list of cameras associated with the account.
        Returns a list of dictionaries with device details.
        """
        with sync_playwright() as p:
            browser, context, page = self._get_authenticated_context(p)
            try:
                page.goto(self.playback_url, wait_until="networkidle")

                if "login" in page.url:
                    raise PermissionError(
                        "Session expired or invalid. Please re-run 'python smartlife.py login'."
                    )

                # Fetch camera list from internal API using the page context
                device_data = page.evaluate(
                    """async () => {
                        try {
                            const res = await fetch('/api/device/sort/list');
                            if (res.ok) {
                                return await res.json();
                            }
                        } catch (e) {}
                        return null;
                    }"""
                )

                cameras = []
                if isinstance(device_data, list):
                    for dev in device_data:
                        cameras.append(
                            {
                                "devId": dev.get("devId") or dev.get("deviceId"),
                                "deviceName": dev.get("deviceName") or dev.get("name", "Unknown"),
                                "online": dev.get("online", False),
                                "category": dev.get("category", ""),
                                "p2pType": dev.get("p2pType", 4),
                                "productId": dev.get("productId", ""),
                            }
                        )

                # Fallback: scrape from the DOM sidebar if API didn't return a direct list
                if not cameras:
                    page.wait_for_selector("[class*='deviceItem_box']", timeout=10000)
                    items = page.locator("[class*='deviceItem_box']").all()
                    for idx, item in enumerate(items):
                        text = item.inner_text().strip()
                        is_online = "device_online" in (item.get_attribute("class") or "")
                        cameras.append(
                            {
                                "devId": f"camera_{idx}",
                                "deviceName": text or f"Camera {idx + 1}",
                                "online": is_online,
                                "category": "sp",
                                "p2pType": 4,
                                "productId": "",
                            }
                        )

                return cameras
            finally:
                browser.close()

    def get_sd_events(
        self, camera_identifier: str, target_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Queries recorded events on the camera's MicroSD card for a specific date (YYYY-MM-DD).
        Defaults to today.
        Returns list of events with start time, end time, and duration.
        """
        if not target_date:
            target_date = datetime.now().strftime("%Y-%m-%d")

        dt = datetime.strptime(target_date, "%Y-%m-%d")
        year, month, day = dt.year, dt.month, dt.day

        sd_events = []
        captured_response = False

        with sync_playwright() as p:
            browser, context, page = self._get_authenticated_context(p, headless=True)

            def handle_response(response: Response):
                nonlocal sd_events, captured_response
                if "/api/jarvis/sd/list" in response.url:
                    try:
                        data = response.json()
                        if isinstance(data, list):
                            sd_events = data
                            captured_response = True
                    except Exception:
                        pass

            page.on("response", handle_response)

            try:
                page.goto(self.playback_url, wait_until="networkidle")

                if "login" in page.url:
                    raise PermissionError(
                        "Session expired. Please re-run 'python smartlife.py login'."
                    )

                # Switch to SD playback tab: index 2 of typeSwitch_item
                page.wait_for_selector("[class*='typeSwitch_item']", timeout=15000)
                tab_items = page.locator("[class*='typeSwitch_item']").all()
                if len(tab_items) >= 3:
                    tab_items[2].click()
                    page.wait_for_timeout(1000)

                # Find and click the matching camera in the sidebar
                page.wait_for_selector("[class*='deviceItem_box']", timeout=15000)
                device_nodes = page.locator("[class*='deviceItem_box']").all()
                matched_node = None
                for node in device_nodes:
                    text = node.inner_text().strip()
                    if camera_identifier.lower() in text.lower():
                        matched_node = node
                        break

                if not matched_node and device_nodes:
                    # If camera_identifier is an index like '0', '1'
                    try:
                        idx = int(camera_identifier)
                        if 0 <= idx < len(device_nodes):
                            matched_node = device_nodes[idx]
                    except ValueError:
                        pass

                if not matched_node and device_nodes:
                    matched_node = device_nodes[0]

                if matched_node:
                    matched_node.click()
                    page.wait_for_timeout(2000)

                # Change date if needed
                today_str = datetime.now().strftime("%Y-%m-%d")
                if target_date != today_str:
                    date_input = page.locator(".ant-picker-input input")
                    if date_input.count() > 0:
                        date_input.first.click()
                        date_input.first.fill(target_date)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(2000)

                # Wait for the SD list response
                start_w = time.time()
                while not captured_response and time.time() - start_w < 12:
                    page.wait_for_timeout(500)

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
        Plays the event over WebRTC in the browser and captures the decoded stream into an MP4 file.
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
                    page.wait_for_timeout(1000)

                # Select camera
                page.wait_for_selector("[class*='deviceItem_box']", timeout=15000)
                nodes = page.locator("[class*='deviceItem_box']").all()
                for node in nodes:
                    if camera_identifier.lower() in node.inner_text().strip().lower():
                        node.click()
                        break
                page.wait_for_timeout(2000)

                # Select date if needed
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
                    window.__recordedChunks = [];
                    const video = document.querySelector('video') || document.querySelector('canvas');
                    if (!video) return { error: "No video or canvas element found" };

                    let stream;
                    if (video.captureStream) {
                        stream = video.captureStream(30);
                    } else {
                        return { error: "captureStream not supported" };
                    }

                    const mimeType = MediaRecorder.isTypeSupported('video/webm;codecs=vp8,opus')
                        ? 'video/webm;codecs=vp8,opus'
                        : 'video/webm';

                    window.__rec = new MediaRecorder(stream, { mimeType });
                    window.__rec.ondataavailable = (e) => {
                        if (e.data && e.data.size > 0) {
                            window.__recordedChunks.push(e.data);
                        }
                    };
                    window.__rec.start(100);
                    return { ok: true };
                }
                """

                # Trigger seek / play
                page.evaluate(
                    f"""() => {{
                        const canvas = document.querySelector('canvas');
                        if (canvas) canvas.click();
                    }}"""
                )

                # Start recording
                res = page.evaluate(recorder_js)
                if res.get("error"):
                    print(f"Warning: {res['error']}, retrying after video load...")
                    page.wait_for_timeout(3000)
                    page.evaluate(recorder_js)

                print(f"Streaming and capturing video ({record_dur}s)...")
                # Progress display
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
                            const blob = new Blob(window.__recordedChunks, { type: 'video/webm' });
                            const reader = new FileReader();
                            reader.onloadend = () => {
                                const base64 = reader.result.split(',')[1] || "";
                                resolve(base64);
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
                        "-c:a",
                        "aac",
                        str(out_path),
                    ]
                    sub = subprocess.run(cmd, capture_output=True, text=True)
                    if sub.returncode == 0 and out_path.exists():
                        webm_temp.unlink(missing_ok=True)
                        print(f"Successfully saved to: {out_path.resolve()}")
                        return str(out_path)
                    else:
                        print(f"Notice: ffmpeg remux fallback. WebM video saved at: {webm_temp.resolve()}")
                        return str(webm_temp)
                except FileNotFoundError:
                    print(f"ffmpeg not found. Saved as WebM: {webm_temp.resolve()}")
                    return str(webm_temp)
            finally:
                browser.close()
