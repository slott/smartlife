#!/usr/bin/env python3
"""
Smart Life / Tuya Web Camera SD Card Tool
=========================================
CLI tool to query cameras, list SD card recorded events, and download/pull recordings.

Usage Examples:
  # 1. Login with QR code (scanned via Smart Life app)
  python smartlife.py login

  # 2. List all cameras
  python smartlife.py cameras

  # 3. List recorded events on a camera's SD card for today or a specific date
  python smartlife.py events --camera "Living Room"
  python smartlife.py events --camera "Living Room" --date 2026-09-13

  # 4. Pull/download a specific recorded event to MP4
  python smartlife.py pull --camera "Living Room" --date 2026-09-13 --event 1

  # 5. Pull all events for a given date
  python smartlife.py pull-all --camera "Living Room" --date 2026-09-13 --output-dir ./recordings
"""

import sys
import argparse
from datetime import datetime
from pathlib import Path
from smartlife_client import SmartLifeClient, SmartLifeError, CameraBusyError, EventNotFoundError


def cmd_login(args, client: SmartLifeClient):
    print("=" * 60)
    print(" Smart Life / Tuya Camera Authentication")
    print("=" * 60)
    success = client.login(headed=not args.headless, timeout=args.timeout)
    if success:
        print("\nAuthentication complete! You can now run other commands.")
    else:
        print("\nAuthentication failed or timed out. Please try again.")
        sys.exit(1)


def cmd_cameras(args, client: SmartLifeClient):
    print("Fetching cameras from Smart Life portal...")
    cameras = client.get_cameras()
    if not cameras:
        print("No cameras found in account.")
        return

    print("\n" + "=" * 80)
    print(f" {'#':<3} {'Camera Name':<20} {'Room':<18} {'Status':<10} {'Device ID'}")
    print("=" * 80)
    for idx, cam in enumerate(cameras, start=1):
        status = "ONLINE" if cam.get("online") else "OFFLINE"
        name = cam.get("deviceName", "Unknown")
        room = cam.get("room", "Unknown")
        dev_id = cam.get("devId", "N/A")
        print(f" {idx:<3} {name:<20} {room:<18} {status:<10} {dev_id}")
    print("=" * 80)


def cmd_events(args, client: SmartLifeClient):
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    print(f"Querying SD card events for camera '{args.camera}' on date {date_str}...")

    events = client.get_sd_events(args.camera, date_str)
    if not events:
        print(f"No recorded events found on SD card for {date_str}.")
        return

    print("\n" + "=" * 75)
    print(f" Recorded Events on SD Card ({len(events)} segments found) - Date: {date_str}")
    print("=" * 75)
    print(f" {'ID':<5} {'Start Time':<22} {'End Time':<22} {'Duration'}")
    print("-" * 75)
    total_dur = 0
    for ev in events:
        total_dur += ev["duration"]
        mins, secs = divmod(ev["duration"], 60)
        dur_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        print(f" {ev['id']:<5} {ev['startTime']:<22} {ev['endTime']:<22} {dur_str}")
    print("=" * 75)
    total_m, total_s = divmod(total_dur, 60)
    print(f" Total recorded footage: {total_m}m {total_s}s across {len(events)} event(s).")
    print(f"\n To pull an event, run:\n   python smartlife.py pull --camera \"{args.camera}\" --date {date_str} --event <ID>")


def cmd_pull(args, client: SmartLifeClient):
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    print(f"Pulling event #{args.event} from camera '{args.camera}' on {date_str}...")
    out_file = client.pull_event(
        camera_identifier=args.camera,
        target_date=date_str,
        event_index=args.event,
        output_path=args.output,
        max_duration=args.max_duration,
    )
    print(f"\nDone! Video saved to: {out_file}")


def cmd_pull_all(args, client: SmartLifeClient):
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    saved = client.pull_all_events(
        camera_identifier=args.camera,
        target_date=date_str,
        output_dir=args.output_dir,
        max_duration=args.max_duration,
    )
    print(f"\nAll done! Processed {len(saved)} recording(s).")


def main():
    parser = argparse.ArgumentParser(
        description="Query and pull video recordings from Tuya / Smart Life cameras SD cards."
    )
    parser.add_argument(
        "--portal-url",
        default="https://protect-eu.ismartlife.me",
        help="Tuya portal URL (default: https://protect-eu.ismartlife.me)",
    )
    parser.add_argument(
        "--session",
        default="session.json",
        help="Path to session credentials file (default: session.json)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # login
    p_login = subparsers.add_parser("login", help="Log in by scanning a QR code with the Smart Life app.")
    p_login.add_argument("--headless", action="store_true", help="Run without GUI browser (saves login_qr.png)")
    p_login.add_argument("--timeout", type=int, default=120, help="Login timeout in seconds (default: 120)")

    # cameras
    subparsers.add_parser("cameras", help="List all cameras in the account.")

    # events
    p_events = subparsers.add_parser("events", help="List recorded events on a camera's SD card.")
    p_events.add_argument("--camera", "-c", required=True, help="Camera name or ID")
    p_events.add_argument("--date", "-d", help="Date in YYYY-MM-DD format (default: today)")

    # pull
    p_pull = subparsers.add_parser("pull", help="Pull/download a specific SD card recording.")
    p_pull.add_argument("--camera", "-c", required=True, help="Camera name or ID")
    p_pull.add_argument("--event", "-e", type=int, required=True, help="Event ID (from 'events' command)")
    p_pull.add_argument("--date", "-d", help="Date in YYYY-MM-DD format (default: today)")
    p_pull.add_argument("--output", "-o", help="Output MP4 file path")
    p_pull.add_argument("--max-duration", type=int, help="Cap recording duration to N seconds")

    # pull-all
    p_pull_all = subparsers.add_parser("pull-all", help="Pull all SD card recordings for a given day.")
    p_pull_all.add_argument("--camera", "-c", required=True, help="Camera name or ID")
    p_pull_all.add_argument("--date", "-d", help="Date in YYYY-MM-DD format (default: today)")
    p_pull_all.add_argument("--output-dir", "-o", default="./recordings", help="Output directory")
    p_pull_all.add_argument("--max-duration", type=int, help="Cap each recording duration to N seconds")

    args = parser.parse_args()
    client = SmartLifeClient(portal_url=args.portal_url, session_path=args.session)

    try:
        if args.command == "login":
            cmd_login(args, client)
        elif args.command == "cameras":
            cmd_cameras(args, client)
        elif args.command == "events":
            cmd_events(args, client)
        elif args.command == "pull":
            cmd_pull(args, client)
        elif args.command == "pull-all":
            cmd_pull_all(args, client)
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        sys.exit(130)
    except CameraBusyError as e:
        print(f"\n[Camera Busy] {e}")
        sys.exit(1)
    except EventNotFoundError as e:
        print(f"\n[Not Found] {e}")
        sys.exit(1)
    except SmartLifeError as e:
        print(f"\n[Error] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[Error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
