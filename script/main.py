"""
Sends the current track's title, artist and time to the board (Windows).

Install:  python -m pip install winsdk pyserial
Run:      python now_playing.py

Packet: AA 55 | type (1 byte) | length (2 bytes, little-endian) | data
  type 1 - title (UTF-8)
  type 2 - artist (UTF-8)
  type 4 - time: position (uint16, sec) | duration (uint16, sec) | flags (bit0 = playing)
"""

import asyncio
import re
import struct
import sys
import unicodedata
from datetime import datetime

import serial
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
)

# ---------- settings ----------
PORT = "COM6"          # board's COM port (check Device Manager)
BAUD = 460800          # must match BAUD in the firmware
POLL_SECONDS = 1.0

# Which apps to listen to: substrings of the app id, case-insensitive.
# Everything else (YouTube in another browser, any other players) is ignored.
# To find your app ids:  python now_playing.py --list
# Empty list [] = listen to everything.
ALLOWED_APPS = ["eikjhbkpemcmdeeeamdpkgabmk"]  # SoundCloud (installed Chrome web app)
# To also add Spotify:  ["eikjhbkpemcmdeeeamdpkgabmk", "spotify"]
# --------------------------------

T_TITLE, T_ARTIST, T_TIME = 1, 2, 4
NO_PLAYBACK = ("Nothing playing", "")
PLAYING = 4  # PlaybackStatus.PLAYING value in Windows


def packet(ptype: int, payload: bytes) -> bytes:
    return b"\xAA\x55" + bytes([ptype]) + struct.pack("<H", len(payload)) + payload


def clean_text(text: str) -> str:
    """Converts "fancy" Unicode (gothic, bold, fullwidth, etc.) into plain letters
    and drops characters that are missing from the display font."""
    original = text or ""
    text = unicodedata.normalize("NFKC", original)
    out = []
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF or 0x400 <= cp <= 0x4FF:
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
        # everything else (emoji, CJK, decorations) is skipped
    result = re.sub(r"\s+", " ", "".join(out)).strip()
    if not result and original.strip():
        return "?"
    return result


def safe_text(text: str, limit: int) -> bytes:
    """Cleans the text and truncates UTF-8 by bytes without splitting a character."""
    raw = clean_text(text).encode("utf-8")[:limit]
    return raw.decode("utf-8", "ignore").encode("utf-8")


def app_id(session) -> str:
    return (session.source_app_user_model_id or "").lower()


def is_allowed(session) -> bool:
    if not ALLOWED_APPS:
        return True
    aid = app_id(session)
    return any(a.lower() in aid for a in ALLOWED_APPS)


def is_playing(session) -> bool:
    try:
        return int(session.get_playback_info().playback_status) == PLAYING
    except Exception:
        return False


def pick_session(mgr):
    """Takes only allowed apps; if there are several, prefers the one that is playing."""
    allowed = [s for s in mgr.get_sessions() if is_allowed(s)]
    for s in allowed:
        if is_playing(s):
            return s
    return allowed[0] if allowed else None


async def list_sessions():
    mgr = await MediaManager.request_async()
    sessions = list(mgr.get_sessions())
    if not sessions:
        print("No active media sessions. Start some music and run again.")
        return
    for s in sessions:
        p = await s.try_get_media_properties_async()
        state = "playing" if is_playing(s) else "paused/stopped"
        mark = "MATCH" if is_allowed(s) else "ignored"
        print(f"[{mark}] app id: {s.source_app_user_model_id}  ({state})  {p.artist} - {p.title}")


def get_times(session):
    """Returns (position_sec, duration_sec, playing)."""
    try:
        tl = session.get_timeline_properties()
        playing = int(session.get_playback_info().playback_status) == PLAYING

        start = tl.start_time.total_seconds()
        dur = max(0.0, tl.end_time.total_seconds() - start)
        pos = max(0.0, tl.position.total_seconds() - start)

        # the player doesn't update the position every second, so add the elapsed time
        if playing:
            updated = tl.last_updated_time
            now = datetime.now(updated.tzinfo) if updated.tzinfo else datetime.now()
            delta = (now - updated).total_seconds()
            if 0 <= delta < 6 * 3600:
                pos += delta

        if dur > 0:
            pos = min(pos, dur)
        return min(int(pos), 65535), min(int(dur), 65535), playing
    except Exception:
        return 0, 0, False


def send_text(ser, title: str, artist: str):
    ser.write(packet(T_TITLE, safe_text(title, 90)))
    ser.write(packet(T_ARTIST, safe_text(artist, 60)))


def send_time(ser, pos: int, dur: int, playing: bool):
    ser.write(packet(T_TIME, struct.pack("<HHB", pos, dur, 1 if playing else 0)))


async def run(ser: serial.Serial):
    mgr = await MediaManager.request_async()
    last_key = None

    while True:
        session = pick_session(mgr)

        if session is None:
            if last_key != NO_PLAYBACK:
                last_key = NO_PLAYBACK
                send_text(ser, *NO_PLAYBACK)
            send_time(ser, 0, 0, False)
        else:
            props = await session.try_get_media_properties_async()
            key = (props.title or "", props.artist or "")
            if key != last_key:
                last_key = key
                send_text(ser, *key)
            send_time(ser, *get_times(session))

        await asyncio.sleep(POLL_SECONDS)


async def main():
    while True:
        try:
            with serial.Serial(PORT, BAUD, timeout=1) as ser:
                print(f"Connected to {PORT}")
                await asyncio.sleep(2)  # the board resets when the port is opened
                await run(ser)
        except serial.SerialException as e:
            print(f"Port unavailable ({e}), retrying in 3 s...")
            await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        if "--list" in sys.argv:
            asyncio.run(list_sessions())
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        pass