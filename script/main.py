"""
Now-playing sender for the ESP8266 OLED widget (Windows).

Reads the current track (title, artist, position, duration) from the Windows
media session of the allowed apps and sends it to the board over serial.

Install:   python -m pip install winsdk pyserial
Run:       python now_playing.py
Debug:     python now_playing.py --list     (shows media sessions and their app ids)
Background: run with pythonw.exe (no console window), logs go to now_playing.log

Packet: AA 55 | type (1 byte) | length (2 bytes, little-endian) | payload
  type 1 - title  (UTF-8)
  type 2 - artist (UTF-8)
  type 4 - time: position (uint16, sec) | duration (uint16, sec) | flags (bit0 = playing)
"""

import asyncio
import logging
import re
import struct
import sys
import time
import unicodedata
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import serial
from serial.tools import list_ports
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
)

# ---------------------------- settings ----------------------------
PORT = "COM6"          # board COM port; set to None to auto-detect CP210x/CH340/FTDI boards
BAUD = 460800          # must match BAUD in the firmware
POLL_SECONDS = 1.0

# Apps to listen to: substrings of the app id, case-insensitive.
# Everything else (YouTube in a normal browser tab, other players) is ignored.
# Find your ids with:  python now_playing.py --list
# Empty list [] = listen to everything.
ALLOWED_APPS = [
    "eikjhbkpemcmdeeeamdpkgabmk",  # SoundCloud (installed Chrome web app)
    "pjibgcllelbfgfagdaldikeohf",  # Spotify (installed Chrome web app)
]

IDLE_TITLE = "Nothing playing..."

# If the allowed app stays paused/stopped longer than this many seconds, show the idle
# screen. 0 = never: a paused track stays on the display as long as its window exists.
PAUSE_TIMEOUT_SECONDS = 0
TEXT_REFRESH_SECONDS = 10  # resend title/artist periodically in case the board rebooted
# ------------------------------------------------------------------

T_TITLE, T_ARTIST, T_TIME = 1, 2, 4
PLAYING = 4  # PlaybackStatus.PLAYING value in the Windows media API
USB_UART_VIDS = {0x10C4, 0x1A86, 0x0403}  # CP210x, CH340, FTDI

log = logging.getLogger("now_playing")


# ----------------------------- helpers ----------------------------
def app_dir() -> Path:
    # When built with PyInstaller, __file__ points to a temp folder, so use the exe's folder
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def setup_logging():
    # The log file lives next to the script/exe and is capped in size
    handler = RotatingFileHandler(
        app_dir() / "now_playing.log",
        maxBytes=200_000,
        backupCount=1,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    if sys.stderr is not None:  # under pythonw there is no console
        log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)


def packet(ptype: int, payload: bytes) -> bytes:
    return b"\xAA\x55" + bytes([ptype]) + struct.pack("<H", len(payload)) + payload


def clean_text(text: str) -> str:
    """Turn fancy Unicode (fraktur, bold, fullwidth...) into plain letters and
    drop characters the display font cannot render."""
    original = text or ""
    text = unicodedata.normalize("NFKC", original)
    out = []
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF or 0x400 <= cp <= 0x4FF:
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
        # everything else (emoji, CJK, decorative symbols) is skipped
    result = re.sub(r"\s+", " ", "".join(out)).strip()
    if not result and original.strip():
        return "?"
    return result


def safe_text(text: str, limit: int) -> bytes:
    """Clean the text and cut it by bytes without splitting a UTF-8 character."""
    raw = clean_text(text).encode("utf-8")[:limit]
    return raw.decode("utf-8", "ignore").encode("utf-8")


def resolve_port():
    if PORT:
        return PORT
    for p in list_ports.comports():
        if p.vid in USB_UART_VIDS:
            return p.device
    return None


# ------------------------- media sessions -------------------------
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
    """Only allowed apps are considered; if several, prefer the one that is playing."""
    allowed = [s for s in mgr.get_sessions() if is_allowed(s)]
    for s in allowed:
        if is_playing(s):
            return s
    return allowed[0] if allowed else None


def get_times(session):
    """Returns (position_sec, duration_sec, playing)."""
    try:
        tl = session.get_timeline_properties()
        playing = is_playing(session)

        start = tl.start_time.total_seconds()
        dur = max(0.0, tl.end_time.total_seconds() - start)
        pos = max(0.0, tl.position.total_seconds() - start)

        # Players don't update the position every second, so add the elapsed time
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


async def list_sessions():
    mgr = await MediaManager.request_async()
    sessions = list(mgr.get_sessions())
    if not sessions:
        print("No active media sessions. Start some music and run this again.")
        return
    for s in sessions:
        p = await s.try_get_media_properties_async()
        state = "playing" if is_playing(s) else "paused/stopped"
        mark = "ALLOWED" if is_allowed(s) else "ignored"
        print(f"[{mark}] app id: {s.source_app_user_model_id}  ({state})  {p.artist} - {p.title}")


# ----------------------------- sending ----------------------------
def send_text(ser, title: str, artist: str):
    ser.write(packet(T_TITLE, safe_text(title, 90)))
    ser.write(packet(T_ARTIST, safe_text(artist, 60)))


def send_time(ser, pos: int, dur: int, playing: bool, idle: bool = False):
    flags = (1 if playing else 0) | (2 if idle else 0)
    ser.write(packet(T_TIME, struct.pack("<HHB", pos, dur, flags)))


async def run(ser: serial.Serial):
    mgr = await MediaManager.request_async()
    last_key = None
    last_text_sent = 0.0
    idle_key = (IDLE_TITLE, "")
    paused_since = None
    errors = 0

    while True:
        try:
            now = time.monotonic()
            session = pick_session(mgr)

            # Paused/stopped for too long (or a leftover session after the window
            # was closed) is treated as "nothing playing"
            if session is not None:
                if is_playing(session):
                    paused_since = None
                else:
                    if paused_since is None:
                        paused_since = now
                    if PAUSE_TIMEOUT_SECONDS and now - paused_since > PAUSE_TIMEOUT_SECONDS:
                        session = None
            else:
                paused_since = None

            if session is None:
                key = idle_key
            else:
                # a WinRT call must never be able to hang the whole loop
                props = await asyncio.wait_for(session.try_get_media_properties_async(), 5)
                key = (props.title or "", props.artist or "")

            if key != last_key or now - last_text_sent > TEXT_REFRESH_SECONDS:
                last_key = key
                last_text_sent = now
                send_text(ser, *key)

            if session is None:
                send_time(ser, 0, 0, False, idle=True)
            else:
                send_time(ser, *get_times(session))
            errors = 0
        except serial.SerialException:
            raise  # let main() reconnect
        except Exception:
            errors += 1
            log.exception("Media session error (%d)", errors)
            if errors >= 5:
                raise  # let main() restart everything, including the media manager

        await asyncio.sleep(POLL_SECONDS)


async def main():
    warned = False
    while True:
        try:
            port = resolve_port()
            if port is None:
                raise serial.SerialException("no board found")
            with serial.Serial(port, BAUD, timeout=1) as ser:
                log.info("Connected to %s", port)
                warned = False
                await asyncio.sleep(2)  # the board resets when the port opens
                await run(ser)
        except serial.SerialException as e:
            if not warned:  # don't spam the log while the board is unplugged
                log.info("Port unavailable (%s), retrying...", e)
                warned = True
        except Exception:
            log.exception("Unexpected error, restarting")
        await asyncio.sleep(3)


if __name__ == "__main__":
    setup_logging()
    try:
        if "--list" in sys.argv:
            asyncio.run(list_sessions())
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        pass