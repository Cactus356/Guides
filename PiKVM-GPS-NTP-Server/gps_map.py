"""
gps_map_viewer.py

Reads a u-blox GPS through gpsd on a Raspberry Pi 4 running PiKVM and serves a
local web dashboard at http://127.0.0.1:8080 with:

  - A Leaflet map showing the current fix (with a real accuracy circle
    from the receiver's own hAcc estimate)
  - A collapsible left pane: fix status, UTC/local time, position,
    DOPs, and a N/S/E/W sky view of satellites per constellation
  - A "Show current location" checkbox (default: checked). Unchecking it
    removes the marker, zooms out, and masks coordinates -- handy for
    screen recording.

Why UBX instead of NMEA:
  - NAV-SAT reports each satellite's constellation, elevation, azimuth,
    C/N0, and a per-satellite "used in fix" flag. No cross-referencing
    of GSA/GSV sentences (where PRN numbers collide between systems).
  - Binary framing (sync words + length + Fletcher checksum) recovers
    cleanly from corrupted bytes: a bad frame is dropped and the parser
    resynchronizes on the next sync word.
  - NAV-PVT + NAV-SAT + NAV-DOP is a fraction of the NMEA byte volume.

Usage:
    uv run gps_map_viewer.py            # real hardware
    uv run gps_map_viewer.py --demo     # simulated fix, no hardware needed

GPS data path: u-blox UART -> /dev/ttyAMA0 -> gpsd.
PPS timing path: u-blox PPS -> GPIO18 -> /dev/pps0 -> chrony.
"""

import argparse
import calendar
import ipaddress
import json
import math
import os
import random
import struct
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# u-blox default 7-bit I2C address
GPS_ADDR = 0x42

# Set from CLI args in main()
I2C_FREQ = 50000
CLOCK_STRETCH = False

stop_event = threading.Event()

# ---------------------------------------------------------------------------
# Shared GPS state
# ---------------------------------------------------------------------------

state_lock = threading.Lock()
state = {
    "connected": False,
    "error": None,
    "lat": None,
    "lon": None,
    "alt_msl_m": None,       # height above mean sea level
    "geoid_sep_m": None,     # ellipsoid height - MSL height
    "h_acc_m": None,         # receiver's own horizontal accuracy estimate
    "v_acc_m": None,
    "fix_type": 0,           # NAV-PVT fixType: 0 none, 2 2D, 3 3D, 4 GNSS+DR
    "fix_ok": False,         # NAV-PVT flags.gnssFixOK
    "fix_detail": "",        # DGPS / RTK FLT / RTK FIX / ""
    "sats_used": 0,
    "pdop": None,
    "hdop": None,
    "vdop": None,
    "utc_iso": None,         # "2026-07-27T18:12:34Z" (validity-checked)
    "gps_utc_ms": None,      # precise GPS UTC in ms since epoch (incl. nano)
    "host_ms_at_fix": None,  # host clock (ms) when that PVT was parsed
    "time_valid": False,
    "speed_kmh": None,
    "heading_deg": None,
    "sats": [],              # [{gnss, sv, elev, az, cno, used}]
    "frames_ok": 0,
    "frames_bad": 0,
    "cfg_ack": None,         # True/False once the module ACKs/NAKs our config
    "last_fix_age_s": None,
    "chrony_tracking": {},
    "chrony_clients": [],
    "chrony_error": None,
    "chrony_tracking_error": None,
    "chrony_clients_error": None,
    "chrony_updated_age_s": None,
}

_last_fix_time = None

GNSS_NAMES = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou",
              4: "IMES", 5: "QZSS", 6: "GLONASS", 7: "NavIC"}


# ---------------------------------------------------------------------------
# UBX protocol helpers
# ---------------------------------------------------------------------------

def ubx_checksum(body):
    """Fletcher-8 over class..payload."""
    ck_a = ck_b = 0
    for b in body:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])


def ubx_frame(msg_class, msg_id, payload=b""):
    body = bytes([msg_class, msg_id]) + len(payload).to_bytes(2, "little") + payload
    return b"\xb5\x62" + body + ubx_checksum(body)


def build_config_valset():
    """One VALSET (RAM layer): UBX only on I2C, enable PVT/SAT/DOP at 1 Hz."""
    items = [
        (0x10720001, 1),  # CFG-I2COUTPROT-UBX        = on
        (0x10720002, 0),  # CFG-I2COUTPROT-NMEA       = off
        (0x20910006, 1),  # CFG-MSGOUT-UBX_NAV_PVT_I2C = every fix
        (0x20910015, 1),  # CFG-MSGOUT-UBX_NAV_SAT_I2C = every fix
        (0x20910038, 1),  # CFG-MSGOUT-UBX_NAV_DOP_I2C = every fix
    ]
    payload = bytes([0x00, 0x01, 0x00, 0x00])  # version 0, RAM layer
    for key, val in items:
        payload += key.to_bytes(4, "little") + bytes([val])
    return ubx_frame(0x06, 0x8A, payload)


# ---------------------------------------------------------------------------
# UBX message handlers
# ---------------------------------------------------------------------------

def handle_nav_pvt(p):
    global _last_fix_time
    if len(p) < 92:
        return
    (year, month, day, hour, minute, sec, valid) = struct.unpack_from("<HBBBBBB", p, 4)
    fix_type = p[20]
    flags = p[21]
    num_sv = p[23]
    lon, lat, height, hmsl = struct.unpack_from("<iiii", p, 24)
    h_acc, v_acc = struct.unpack_from("<II", p, 40)
    g_speed, head_mot = struct.unpack_from("<ii", p, 60)
    pdop = struct.unpack_from("<H", p, 76)[0]

    gnss_fix_ok = bool(flags & 0x01)
    diff_soln = bool(flags & 0x02)
    carr_soln = (flags >> 6) & 0x03
    time_ok = (valid & 0x03) == 0x03   # validDate + validTime

    now = time.time()
    with state_lock:
        state["fix_type"] = fix_type
        state["fix_ok"] = gnss_fix_ok
        state["fix_detail"] = ("RTK FIX" if carr_soln == 2 else
                               "RTK FLT" if carr_soln == 1 else
                               "DGPS" if diff_soln else "")
        state["sats_used"] = num_sv
        state["pdop"] = pdop / 100.0
        if time_ok:
            state["utc_iso"] = (f"{year:04d}-{month:02d}-{day:02d}"
                                f"T{hour:02d}:{minute:02d}:{sec:02d}Z")
            # Precise GPS UTC (ms since epoch, incl. the nano field) paired
            # with the host clock at parse time -- the browser disciplines
            # its display clock from this pair.
            nano = struct.unpack_from("<i", p, 16)[0]
            epoch = calendar.timegm((year, month, day, hour, minute, sec, 0, 0, 0))
            state["gps_utc_ms"] = epoch * 1000.0 + nano / 1e6
            state["host_ms_at_fix"] = now * 1000.0
        state["time_valid"] = time_ok
        state["speed_kmh"] = g_speed * 0.0036       # mm/s -> km/h
        state["heading_deg"] = head_mot * 1e-5
        if gnss_fix_ok and fix_type >= 2:
            state["lat"] = lat * 1e-7
            state["lon"] = lon * 1e-7
            state["alt_msl_m"] = hmsl / 1000.0
            state["geoid_sep_m"] = (height - hmsl) / 1000.0
            state["h_acc_m"] = h_acc / 1000.0
            state["v_acc_m"] = v_acc / 1000.0
            _last_fix_time = now
        if _last_fix_time:
            state["last_fix_age_s"] = round(now - _last_fix_time, 1)


def handle_nav_sat(p):
    if len(p) < 8:
        return
    num_svs = p[5]
    sats = []
    for n in range(num_svs):
        off = 8 + 12 * n
        if off + 12 > len(p):
            break
        gnss_id, sv_id, cno = p[off], p[off + 1], p[off + 2]
        elev = struct.unpack_from("<b", p, off + 3)[0]
        azim = struct.unpack_from("<h", p, off + 4)[0]
        flags = struct.unpack_from("<I", p, off + 8)[0]
        sats.append({
            "gnss": GNSS_NAMES.get(gnss_id, f"ID{gnss_id}"),
            "sv": sv_id,
            "elev": elev if -91 <= elev <= 90 else None,
            "az": azim if 0 <= azim <= 360 else None,
            "cno": cno if cno > 0 else None,
            "used": bool(flags & 0x08),   # svUsed flag, per satellite
        })
    with state_lock:
        state["sats"] = sats


def handle_nav_dop(p):
    if len(p) < 18:
        return
    _, pdop, _, vdop, hdop = struct.unpack_from("<HHHHH", p, 4)
    with state_lock:
        state["pdop"] = pdop / 100.0
        state["vdop"] = vdop / 100.0
        state["hdop"] = hdop / 100.0


def handle_ack(msg_id, p):
    """ACK-ACK (id 1) / ACK-NAK (id 0) for our CFG-VALSET."""
    if len(p) >= 2 and p[0] == 0x06 and p[1] == 0x8A:
        with state_lock:
            state["cfg_ack"] = (msg_id == 0x01)


HANDLERS = {
    (0x01, 0x07): handle_nav_pvt,
    (0x01, 0x35): handle_nav_sat,
    (0x01, 0x04): handle_nav_dop,
}


class UbxParser:
    """Incremental UBX frame parser with resync on corruption."""

    MAX_PAYLOAD = 2048

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        buf = self.buf
        pos = 0
        while True:
            j = buf.find(b"\xb5\x62", pos)
            if j < 0:
                # Keep a trailing 0xB5 in case its 0x62 arrives next read
                tail = buf[-1:] if buf[-1:] == b"\xb5" else b""
                self.buf = bytearray(tail)
                return
            if len(buf) - j < 8:
                self.buf = buf[j:]
                return
            length = buf[j + 4] | (buf[j + 5] << 8)
            if length > self.MAX_PAYLOAD:
                pos = j + 2           # bogus header, resync past it
                continue
            end = j + 6 + length + 2
            if len(buf) < end:
                self.buf = buf[j:]    # wait for the rest of the frame
                return
            frame = bytes(buf[j:end])
            if ubx_checksum(frame[2:-2]) == frame[-2:]:
                msg_class, msg_id = frame[2], frame[3]
                payload = frame[6:-2]
                with state_lock:
                    state["frames_ok"] += 1
                if msg_class == 0x05:
                    handle_ack(msg_id, payload)
                else:
                    handler = HANDLERS.get((msg_class, msg_id))
                    if handler:
                        handler(payload)
                pos = end
            else:
                with state_lock:
                    state["frames_bad"] += 1
                pos = j + 2           # drop frame, resync after sync word


# ---------------------------------------------------------------------------
# GPS reader thread (real hardware)
# ---------------------------------------------------------------------------

def gps_reader():
    """
    Read the u-blox receiver through gpsd.

    Hardware:
        u-blox GPS UART -> /dev/ttyAMA0 -> gpsd -> localhost:2947

    PPS:
        u-blox PPS -> GPIO18 -> /dev/pps0 -> chrony

    This program does not discipline the system clock.  chrony remains
    responsible for PPS/NTP time synchronization.
    """
    import datetime
    import socket

    GPSD_HOST = "127.0.0.1"
    GPSD_PORT = 2947

    def parse_gps_time(value):
        if not value:
            return None

        try:
            value = str(value)
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"

            dt = datetime.datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)

            return dt.astimezone(datetime.timezone.utc)
        except (ValueError, TypeError, OverflowError):
            return None

    def handle_tpv(msg):
        gps_dt = parse_gps_time(msg.get("time"))

        with state_lock:
            mode = int(msg.get("mode", 0) or 0)
            state["fix_type"] = mode
            state["fix_ok"] = mode >= 2

            if mode == 0:
                state["fix_detail"] = "NO MODE"
            elif mode == 1:
                state["fix_detail"] = "NO FIX"
            elif mode == 2:
                state["fix_detail"] = "2D FIX"
            elif mode == 3:
                state["fix_detail"] = "3D FIX"
            else:
                state["fix_detail"] = f"MODE {mode}"

            if msg.get("lat") is not None:
                state["lat"] = float(msg["lat"])

            if msg.get("lon") is not None:
                state["lon"] = float(msg["lon"])

            alt_msl = msg.get("altMSL")
            alt_hae = msg.get("altHAE")
            # Older gpsd versions may expose only "alt".
            if alt_msl is None:
                alt_msl = msg.get("alt")
            if alt_msl is not None:
                state["alt_msl_m"] = float(alt_msl)
            if alt_hae is not None and alt_msl is not None:
                state["geoid_sep_m"] = float(alt_hae) - float(alt_msl)

            if msg.get("speed") is not None:
                state["speed_kmh"] = float(msg["speed"]) * 3.6

            if msg.get("track") is not None:
                state["heading_deg"] = float(msg["track"])

            # gpsd provides horizontal error as separate east/west
            # estimates. Combine them into one approximate horizontal
            # accuracy value for the existing dashboard.
            if msg.get("epx") is not None and msg.get("epy") is not None:
                state["h_acc_m"] = math.hypot(
                    float(msg["epx"]),
                    float(msg["epy"])
                )

            if msg.get("epv") is not None:
                state["v_acc_m"] = float(msg["epv"])

            if gps_dt is not None:
                state["utc_iso"] = gps_dt.isoformat().replace(
                    "+00:00", "Z"
                )
                state["gps_utc_ms"] = gps_dt.timestamp() * 1000.0
                state["host_ms_at_fix"] = time.time() * 1000.0
                state["time_valid"] = True

            state["last_fix_age_s"] = 0.0
            state["connected"] = True
            state["error"] = None

    def handle_sky(msg):
        # gpsd can emit several SKY messages for the same epoch. Some contain
        # only DOP values, while one contains nSat/uSat and the satellite
        # array. Do not let a DOP-only message erase the last full sky view.
        sat_report = msg.get("satellites")
        satellites = []
        used = 0

        for sat in sat_report or []:
            # gpsd versions/drivers may expose either gnssid/svid or
            # the older PRN-style information.
            try:
                gnss_id = int(sat.get("gnssid", 0) or 0)
            except (TypeError, ValueError):
                gnss_id = 0

            try:
                svid = int(
                    sat.get("svid", sat.get("PRN", 0)) or 0
                )
            except (TypeError, ValueError):
                svid = 0

            try:
                cno = (float(sat["ss"])
                       if sat.get("ss") is not None else None)
            except (TypeError, ValueError):
                cno = None

            try:
                elevation = (float(sat["el"])
                             if sat.get("el") is not None else None)
            except (TypeError, ValueError):
                elevation = None

            try:
                azimuth = (float(sat["az"])
                           if sat.get("az") is not None else None)
            except (TypeError, ValueError):
                azimuth = None

            is_used = bool(sat.get("used", False))
            used += int(is_used)

            satellites.append({
                "gnss": GNSS_NAMES.get(gnss_id, f"ID{gnss_id}"),
                "sv": svid,
                "cno": cno,
                "elev": elevation,
                "az": azimuth,
                "used": is_used,
            })

        with state_lock:
            # Only a full SKY report may replace satellite state. DOP-only
            # reports intentionally leave the last full report untouched.
            if isinstance(sat_report, list):
                state["sats"] = satellites
                state["sats_used"] = int(msg.get("uSat", used) or used)
            for key in ("hdop", "vdop", "pdop"):
                if msg.get(key) is not None:
                    state[key] = float(msg[key])
            state["connected"] = True
            state["error"] = None

    while not stop_event.is_set():
        sock = None

        try:
            sock = socket.create_connection(
                (GPSD_HOST, GPSD_PORT),
                timeout=5
            )
            sock.settimeout(2.0)

            watch = (
                '?WATCH={"enable":true,"json":true,"scaled":true}\n'
            )
            sock.sendall(watch.encode("ascii"))

            buffer = b""

            with state_lock:
                state["connected"] = True
                state["error"] = None

            while not stop_event.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    with state_lock:
                        if state["host_ms_at_fix"]:
                            state["last_fix_age_s"] = (
                                time.time() * 1000.0
                                - state["host_ms_at_fix"]
                            ) / 1000.0
                    continue

                if not chunk:
                    raise ConnectionError("gpsd closed the connection")

                buffer += chunk

                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    raw = raw.strip()

                    if not raw:
                        continue

                    try:
                        msg = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        with state_lock:
                            state["frames_bad"] += 1
                        continue

                    msg_class = msg.get("class")

                    if msg_class == "TPV":
                        handle_tpv(msg)
                        with state_lock:
                            state["frames_ok"] += 1

                    elif msg_class == "SKY":
                        handle_sky(msg)
                        with state_lock:
                            state["frames_ok"] += 1

                    elif msg_class in (
                        "DEVICE",
                        "VERSION",
                        "WATCH",
                        "DEVICES",
                        "GST",
                    ):
                        with state_lock:
                            state["frames_ok"] += 1

        except Exception as exc:
            with state_lock:
                state["connected"] = False
                state["error"] = f"gpsd: {exc}"

            if not stop_event.wait(2.0):
                continue

        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


def chrony_reader():
    """Poll local chronyd tracking and client statistics."""
    last_success = None
    chronyc = "/usr/bin/chronyc" if os.path.isfile("/usr/bin/chronyc") else "chronyc"

    while not stop_event.is_set():
        tracking = None
        clients = None
        tracking_error = None
        clients_error = None

        try:
            tracking_output = subprocess.check_output(
                [chronyc, "-n", "tracking"],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=4,
            )
            tracking = {}
            for line in tracking_output.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                tracking[key.strip()] = value.strip()
        except Exception as exc:
            tracking_error = str(exc)

        try:
            clients_output = subprocess.check_output(
                [chronyc, "-n", "clients"],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=4,
            )
            clients = []
            for line in clients_output.splitlines():
                stripped = line.strip()
                if (not stripped or stripped.startswith("Hostname") or
                        set(stripped) == {"="}):
                    continue
                fields = stripped.split()
                if len(fields) < 6 or not fields[1].isdigit():
                    continue
                clients.append({
                    "host": fields[0],
                    "ntp_requests": fields[1],
                    "last_seen": fields[5],
                })

            def client_sort_key(client):
                host = client["host"].split("%", 1)[0]
                try:
                    address = ipaddress.ip_address(host)
                    return (0, address.version, int(address))
                except ValueError:
                    return (1, 0, host.lower())

            clients.sort(key=client_sort_key)
        except Exception as exc:
            clients_error = str(exc)

        if tracking is not None or clients is not None:
            last_success = time.time()
        with state_lock:
            if tracking is not None:
                state["chrony_tracking"] = tracking
            if clients is not None:
                state["chrony_clients"] = clients
            state["chrony_tracking_error"] = tracking_error
            state["chrony_clients_error"] = clients_error
            errors = [e for e in (tracking_error, clients_error) if e]
            state["chrony_error"] = "; ".join(errors) if errors else None
            if last_success is not None:
                state["chrony_updated_age_s"] = round(
                    time.time() - last_success, 1
                )

        stop_event.wait(5.0)

def demo_reader():
    global _last_fix_time
    with state_lock:
        state["connected"] = True
        state["cfg_ack"] = True
    lat, lon = 38.5816, -121.4944  # arbitrary starting point
    base_sats = [
        ("GPS", 8, 62, 110, True),   ("GPS", 10, 45, 300, True),
        ("GPS", 15, 71, 45, True),   ("GPS", 18, 30, 200, True),
        ("GPS", 23, 55, 85, True),   ("GPS", 24, 12, 340, False),
        ("GLONASS", 3, 40, 150, True), ("GLONASS", 5, 66, 250, True),
        ("GLONASS", 14, 18, 310, False),
        ("Galileo", 13, 25, 20, True), ("Galileo", 15, 50, 190, True),
        ("Galileo", 34, 80, 270, True), ("Galileo", 8, 35, 130, False),
        ("BeiDou", 27, 35, 60, False), ("BeiDou", 5, 44, 220, True),
        ("QZSS", 2, 28, 285, False),
    ]
    while not stop_event.is_set():
        lat += random.uniform(-1, 1) * 2e-6
        lon += random.uniform(-1, 1) * 2e-6
        now = time.time()
        _last_fix_time = now
        with state_lock:
            state.update({
                "lat": lat, "lon": lon,
                "alt_msl_m": 14.0 + math.sin(now / 30) * 2,
                "geoid_sep_m": -31.9,
                "h_acc_m": 1.2 + abs(math.sin(now / 13)),
                "v_acc_m": 2.0 + abs(math.sin(now / 17)),
                "fix_type": 3, "fix_ok": True, "fix_detail": "DGPS",
                "sats_used": sum(1 for s in base_sats if s[4]),
                "pdop": 0.96, "hdop": 0.53, "vdop": 0.79,
                "utc_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "gps_utc_ms": now * 1000.0 + 42.0,   # pretend 42 ms offset
                "host_ms_at_fix": now * 1000.0,
                "time_valid": True,
                "speed_kmh": abs(random.gauss(0.04, 0.02)),
                "heading_deg": None,
                "frames_ok": state["frames_ok"] + 3,
                "last_fix_age_s": 0.0,
                "sats": [
                    {"gnss": g, "sv": sv, "elev": el, "az": az,
                     "cno": max(8, int(30 + 12 * math.sin(now / 7 + sv))),
                     "used": used}
                    for g, sv, el, az, used in base_sats
                ],
            })
        stop_event.wait(1)


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the terminal quiet

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/data":
            with state_lock:
                snapshot = dict(state)
            body = json.dumps(snapshot).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


# ---------------------------------------------------------------------------
# Dashboard page
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PiKVM GPS Ground Station</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  :root {
    --pane-width: min(700px, 100vw);
    --bg: #0b0f14;
    --panel: #101820;
    --line: #20303c;
    --text: #c9d6de;
    --dim: #6b7f8c;
    --amber: #ffb454;
    --green: #58d68d;
    --red: #ef6d6d;
    --mono: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; }
  body {
    font-family: var(--mono);
    background: var(--bg);
    color: var(--text);
    display: flex;
    overflow: hidden;
  }

  /* ---------------- left pane ---------------- */
  #pane {
    width: var(--pane-width);
    min-width: var(--pane-width);
    height: 100%;
    background: var(--panel);
    border-right: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    transition: margin-left 0.25s ease;
    z-index: 1000;
  }
  #pane.collapsed { margin-left: calc(-1 * var(--pane-width)); }

  #pane header {
    padding: 14px 16px 10px;
    border-bottom: 1px solid var(--line);
  }
  #pane header h1 {
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.12em;
    color: var(--amber);
    text-transform: uppercase;
  }
  #pane header .sub {
    font-size: 14px;
    color: var(--dim);
    margin-top: 2px;
  }

  #fixlamp {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 12px 16px;
    border-bottom: 1px solid var(--line);
  }
  #fixlamp .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--red);
    align-self: center;
  }
  #fixlamp.ok .dot { background: var(--green); box-shadow: 0 0 8px var(--green); }
  #fixlamp .label { font-size: 20px; font-weight: 600; }
  #fixUtc {
    margin-left: auto;
    font-size: 20px;
    letter-spacing: 0.04em;
    font-variant-numeric: tabular-nums;
  }
  #fixUtc small { font-size: 10px; color: var(--dim); margin-left: 5px; }

  #scroll {
    overflow-y: auto;
    flex: 1;
    padding: 6px 0 12px;
    scrollbar-width: none;
  }
  #scroll::-webkit-scrollbar { display: none; }
  #summaryGrid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    border-bottom: 1px solid var(--line);
  }
  .summaryCol { min-width: 0; padding: 2px 10px 10px; }
  .summaryCol + .summaryCol { border-left: 1px solid var(--line); }
  .summaryCol section { padding-left: 10px; padding-right: 10px; }
  #skySection { padding: 14px 20px 8px; }
  section { padding: 10px 16px 4px; }
  section h2 {
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--dim);
    margin-bottom: 6px;
  }
  .row {
    display: flex;
    justify-content: space-between;
    font-size: 14px;
    padding: 2px 0;
    gap: 8px;
  }
  .row .k { color: var(--dim); white-space: nowrap; }
  .row .v { font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
  .row .v.masked { color: var(--dim); font-style: italic; }
  .bigtime {
    font-size: 22px;
    letter-spacing: 0.04em;
    padding: 2px 0 0;
    font-variant-numeric: tabular-nums;
  }
  .bigtime small { font-size: 11px; color: var(--dim); margin-left: 6px; }
  .dim2 { color: #4d5f6b !important; }
  .info {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 13px; height: 13px;
    border: 1px solid var(--dim);
    border-radius: 50%;
    font-size: 8px;
    font-style: italic;
    cursor: pointer;
    margin-left: 4px;
    vertical-align: 1px;
    user-select: none;
  }
  .info:hover { border-color: var(--amber); color: var(--amber); }
  #infoPop {
    margin: 6px 0 2px;
    padding: 8px 10px;
    border: 1px solid var(--line);
    border-radius: 4px;
    background: var(--bg);
    font-size: 10px;
    line-height: 1.55;
    color: var(--dim);
  }

  /* satellite sky view */
  #skywrap { display: flex; justify-content: center; padding: 4px 0 2px; }
  #skyview { width: 370px; height: 370px; max-width: 100%; }
  #skyview .ring { fill: none; stroke: var(--line); stroke-width: 1; }
  #skyview .axis { stroke: var(--line); stroke-width: 1; }
  #skyview .compass { fill: var(--dim); font-size: 10px; font-family: var(--mono); }
  #skyview .elevlbl { fill: #3d4f5c; font-size: 7px; font-family: var(--mono); }
  #skyview .sat-vis, #skykey .sat-vis {
    fill: #5a6b76;
    stroke: #0b0f14;
    stroke-width: 1;
  }
  #skyview .sat-used, #skykey .sat-used {
    fill: var(--green);
    stroke: #0b0f14;
    stroke-width: 1;
  }
  #skyview .prnlbl { fill: var(--dim); font-size: 7px; font-family: var(--mono); }
  #skykey {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    justify-content: center;
    padding: 8px 0 2px;
    font-size: 14px;
    color: var(--dim);
  }
  #skykey .item { display: flex; align-items: center; gap: 5px; }
  #skykey svg { width: 10px; height: 10px; overflow: visible; }
  #skykey .sat-none { fill: #263844; stroke: #0b0f14; stroke-width: 1; }
  #skykey .count { color: #8295a1; }
  #skyhint {
    text-align: center;
    padding-top: 3px;
    font-size: 12px;
    color: #4d5f6b;
  }
  #chronyGrid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    border-top: 1px solid var(--line);
    margin-top: 12px;
  }
  .chronyCol { min-width: 0; padding: 4px 10px 12px; }
  .chronyCol + .chronyCol { border-left: 1px solid var(--line); }
  .chronyCol section { padding-left: 10px; padding-right: 10px; }
  .clientHeader, #chronyClients .clientEntry {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 72px 56px;
    gap: 18px;
    align-items: baseline;
    font-variant-numeric: tabular-nums;
  }
  .clientHeader {
    color: var(--dim);
    font-size: 12px;
    padding-bottom: 3px;
  }
  .clientHeader span:not(:first-child),
  #chronyClients .clientEntry span:not(:first-child) { text-align: right; }
  #chronyClients .clientEntry {
    padding: 5px 0;
    border-bottom: 1px solid rgba(32, 48, 60, 0.65);
    font-size: 14px;
  }
  #chronyClients .clientEntry:last-child { border-bottom: 0; }
  #chronyClients .clientHost { color: var(--text); overflow-wrap: anywhere; }
  #chronyClients .clientNtp, #chronyClients .clientLast { color: var(--text); }
  #chronyClients {
    max-height: 190px;
    overflow-y: auto;
    padding-right: 5px;
    scrollbar-color: var(--line) transparent;
    scrollbar-width: thin;
  }
  #chronyStatus { margin-top: 6px; }
  #chronyStatus.error { color: var(--red); }

  #controls {
    border-top: 1px solid var(--line);
    padding: 12px 16px;
    font-size: 12px;
  }
  #controls label {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    user-select: none;
  }
  #controls label + label { margin-top: 8px; }
  #controls input { accent-color: var(--amber); width: 14px; height: 14px; }
  #controls .hint { color: var(--dim); font-size: 10px; margin-top: 6px; }

  /* ---------------- map + toggle ---------------- */
  #map { flex: 1; height: 100%; background: #0d1218; }
  #paneToggle {
    position: absolute;
    top: 12px;
    left: calc(var(--pane-width) + 12px);
    z-index: 1100;
    background: var(--panel);
    color: var(--amber);
    border: 1px solid var(--line);
    border-radius: 4px;
    font-family: var(--mono);
    font-size: 13px;
    padding: 6px 9px;
    cursor: pointer;
    transition: left 0.25s ease;
  }
  #paneToggle.shifted { left: 12px; }
  #paneToggle:hover { border-color: var(--amber); }

  @media (max-width: 620px) {
    #summaryGrid, #chronyGrid { grid-template-columns: 1fr; }
    .summaryCol + .summaryCol, .chronyCol + .chronyCol {
      border-left: 0;
      border-top: 1px solid var(--line);
    }
    #skyview { width: 92vw; height: 92vw; }
  }

  .leaflet-container { font-family: var(--mono); }
</style>
</head>
<body>

<aside id="pane">
  <header>
    <h1>PiKVM GPS Ground Station</h1>
    <div class="sub">Raspberry Pi 4 &#183; gpsd &#183; UART /dev/ttyAMA0</div>
  </header>

  <div id="fixlamp">
    <div class="dot"></div>
    <div class="label" id="fixLabel">NO FIX</div>
    <div id="fixUtc">--:--:--<small>UTC</small></div>
  </div>

  <div id="scroll">
    <div id="summaryGrid">
      <div class="summaryCol">
        <section>
          <h2>Time</h2>
          <div class="row"><span class="k">Date</span><span class="v" id="utcDate">&#8212;</span></div>
          <div class="row"><span class="k dim2">Browser local time <span id="infoBtn" class="info" title="What do these values mean?">i</span></span><span class="v dim2" id="sysTime">&#8212;</span></div>
          <div class="row"><span class="k">GPS message latency</span><span class="v" id="clkOffset">&#8212;</span></div>
          <div id="infoPop" hidden>
            GPS message latency is the approximate time between the GPS
            timestamp and when gpsd delivers that report to this program.
            It includes receiver processing, UART transfer, gpsd, and parsing,
            so it is not a measurement of the PiKVM system-clock error.
            Chrony disciplines the PiKVM clock separately using PPS.
            Browser local time comes from the device viewing this page.
          </div>
        </section>

        <section>
          <h2>Position</h2>
          <div class="row"><span class="k">Latitude</span><span class="v" id="lat">&#8212;</span></div>
          <div class="row"><span class="k">Longitude</span><span class="v" id="lon">&#8212;</span></div>
          <div class="row"><span class="k">Altitude (MSL)</span><span class="v" id="alt">&#8212;</span></div>
          <div class="row"><span class="k">Geoid sep</span><span class="v" id="geoid">&#8212;</span></div>
          <div class="row"><span class="k">Est. accuracy</span><span class="v" id="acc">&#8212;</span></div>
          <div class="row"><span class="k">Speed</span><span class="v" id="speed">&#8212;</span></div>
        </section>
      </div>

      <div class="summaryCol">
        <section>
          <h2>Signal</h2>
          <div class="row"><span class="k">Satellites used</span><span class="v" id="satsUsed">&#8212;</span></div>
          <div class="row"><span class="k">In view</span><span class="v" id="satsView">&#8212;</span></div>
          <div class="row" id="missingElevRow" hidden><span class="k">Not plotted</span><span class="v" id="missingElev">&#8212;</span></div>
          <div class="row"><span class="k">HDOP / VDOP / PDOP</span><span class="v" id="dops">&#8212;</span></div>
        </section>

        <section>
          <h2>Stream</h2>
          <div class="row"><span class="k">GPSD reports received</span><span class="v" id="framesOk">0</span></div>
          <div class="row"><span class="k">JSON/stream errors</span><span class="v" id="framesBad">0</span></div>
          <div class="row"><span class="k">GPSD connection</span><span class="v" id="gpsdStatus">&#8212;</span></div>
          <div class="row"><span class="k">Fix age</span><span class="v" id="fixAge">&#8212;</span></div>
          <div class="row"><span class="k">Status</span><span class="v" id="linkStatus">connecting&#8230;</span></div>
        </section>
      </div>
    </div>

    <section id="skySection">
      <h2>Signal sky view</h2>
      <div id="skywrap"><svg id="skyview" viewBox="0 0 220 220"></svg></div>
      <div id="skykey"></div>
      <div id="skyhint">counts: visible / used</div>
    </section>

    <div id="chronyGrid">
      <div class="chronyCol">
        <section>
          <h2>Chrony tracking</h2>
          <div class="row"><span class="k">Reference</span><span class="v" id="chrReference">&#8212;</span></div>
          <div class="row"><span class="k">Stratum</span><span class="v" id="chrStratum">&#8212;</span></div>
          <div class="row"><span class="k">PiKVM system time</span><span class="v" id="chrSystemTime">&#8212;</span></div>
          <div class="row"><span class="k">Last offset</span><span class="v" id="chrLastOffset">&#8212;</span></div>
          <div class="row"><span class="k">RMS offset</span><span class="v" id="chrRmsOffset">&#8212;</span></div>
          <div class="row"><span class="k">Frequency</span><span class="v" id="chrFrequency">&#8212;</span></div>
          <div class="row"><span class="k">Root delay</span><span class="v" id="chrRootDelay">&#8212;</span></div>
          <div class="row"><span class="k">Root dispersion</span><span class="v" id="chrRootDispersion">&#8212;</span></div>
          <div class="row"><span class="k">Leap status</span><span class="v" id="chrLeapStatus">&#8212;</span></div>
        </section>
      </div>

      <div class="chronyCol">
        <section>
          <h2>Chrony clients</h2>
          <div class="clientHeader"><span>IP</span><span>NTP</span><span>Last</span></div>
          <div id="chronyClients"><div class="row"><span class="k">Waiting for chronyd&#8230;</span></div></div>
          <div class="row" id="chronyStatus"><span class="k">Status</span><span class="v" id="chrStatus">waiting&#8230;</span></div>
        </section>
      </div>
    </div>
  </div>

  <div id="controls">
    <label>
      <input type="checkbox" id="showLoc" checked>
      Show current location
    </label>
    <div class="hint">Unchecking hides the marker, zooms out, and masks
    coordinates &#8212; safe for screen recording.</div>
    <label>
      <input type="checkbox" id="showSatBearings" checked>
      Show satellite bearings
    </label>
    <div class="hint">Rays show sky direction from the receiver. Their
    endpoints are not satellite positions on the ground.</div>
  </div>
</aside>

<button id="paneToggle" title="Toggle info pane">&#9664;</button>
<div id="map"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map = L.map('map', { zoomControl: true }).setView([20, 0], 2);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

let marker = null, circle = null;
const satelliteBearingLayer = L.layerGroup().addTo(map);
let firstFix = true;

const pane = document.getElementById('pane');
const toggle = document.getElementById('paneToggle');
toggle.addEventListener('click', () => {
  pane.classList.toggle('collapsed');
  toggle.classList.toggle('shifted');
  toggle.innerHTML = pane.classList.contains('collapsed') ? '&#9654;' : '&#9664;';
  setTimeout(() => map.invalidateSize(), 300);
});

const showLoc = document.getElementById('showLoc');
const showSatBearings = document.getElementById('showSatBearings');
showLoc.addEventListener('change', () => {
  if (!showLoc.checked) {
    if (marker) { map.removeLayer(marker); marker = null; }
    if (circle) { map.removeLayer(circle); circle = null; }
    satelliteBearingLayer.clearLayers();
    map.setView([20, 0], 2);            // generic world view
  } else {
    firstFix = true;                    // fly back in on next update
  }
});
showSatBearings.addEventListener('change', () => {
  if (!showSatBearings.checked) satelliteBearingLayer.clearLayers();
});

function set(id, val, masked=false) {
  const el = document.getElementById(id);
  el.textContent = val;
  el.classList.toggle('masked', masked);
}

function destinationPoint(lat, lon, bearingDeg, distanceM) {
  const earthRadiusM = 6371000;
  const angularDistance = distanceM / earthRadiusM;
  const bearing = bearingDeg * Math.PI / 180;
  const lat1 = lat * Math.PI / 180;
  const lon1 = lon * Math.PI / 180;
  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(angularDistance) +
    Math.cos(lat1) * Math.sin(angularDistance) * Math.cos(bearing)
  );
  const lon2 = lon1 + Math.atan2(
    Math.sin(bearing) * Math.sin(angularDistance) * Math.cos(lat1),
    Math.cos(angularDistance) - Math.sin(lat1) * Math.sin(lat2)
  );
  return [lat2 * 180 / Math.PI, lon2 * 180 / Math.PI];
}

function drawSatelliteBearings(lat, lon, sats) {
  satelliteBearingLayer.clearLayers();
  if (!showLoc.checked || !showSatBearings.checked) return;

  const origin = [lat, lon];
  for (const sat of sats) {
    if (sat.az === null || sat.az === undefined ||
        sat.elev === null || sat.elev === undefined || sat.elev < 0) continue;

    // Mirror the sky plot: overhead satellites remain near the receiver,
    // while satellites near the horizon extend farther along their bearing.
    const elevation = Math.max(0, Math.min(90, sat.elev));
    const distanceM = 60 + 340 * (90 - elevation) / 90;
    const endpoint = destinationPoint(lat, lon, sat.az, distanceM);
    const color = sat.used ? '#58d68d' : '#738692';
    const tooltip = `${sat.gnss} ${sat.sv} · elevation ${sat.elev}°` +
      ` · azimuth ${sat.az}° · C/N0 ${sat.cno ?? '—'} dB-Hz` +
      (sat.used ? ' · used in fix' : ' · visible');

    const ray = L.polyline([origin, endpoint], {
      color: color,
      weight: sat.used ? 2.5 : 1.5,
      opacity: sat.used ? 0.85 : 0.55,
      dashArray: sat.used ? null : '5 6',
    }).addTo(satelliteBearingLayer);
    ray.bindTooltip(tooltip, {sticky: true});

    const endpointMarker = L.circleMarker(endpoint, {
      radius: sat.used ? 5 : 4,
      color: '#0b0f14',
      weight: 1,
      fillColor: color,
      fillOpacity: sat.used ? 0.95 : 0.75,
    }).addTo(satelliteBearingLayer);
    endpointMarker.bindTooltip(tooltip, {direction: 'top'});
  }
}

const FIX_LABELS = {0:'NO FIX', 1:'DR ONLY', 2:'2D FIX', 3:'3D FIX',
                    4:'3D + DR', 5:'TIME ONLY'};

// ---- Sky view ------------------------------------------------------------
const CONSTELLATIONS = {
  'GPS':     {shape: 'circle'},
  'SBAS':    {shape: 'hexagon'},
  'GLONASS': {shape: 'square'},
  'Galileo': {shape: 'triangle'},
  'BeiDou':  {shape: 'diamond'},
  'QZSS':    {shape: 'pentagon'},
};
const OTHER = {shape: 'circle'};

function shapePath(shape, x, y, r) {
  switch (shape) {
    case 'square':
      return `<rect x="${x-r}" y="${y-r}" width="${2*r}" height="${2*r}" rx="1"`;
    case 'triangle':
      return `<polygon points="${x},${y-r*1.2} ${x-r*1.1},${y+r*0.9} ${x+r*1.1},${y+r*0.9}"`;
    case 'diamond':
      return `<polygon points="${x},${y-r*1.3} ${x+r*1.05},${y} ${x},${y+r*1.3} ${x-r*1.05},${y}"`;
    case 'pentagon': {
      const pts = [];
      for (let i = 0; i < 5; i++) {
        const a = -Math.PI/2 + i * 2*Math.PI/5;
        pts.push(`${x + r*1.15*Math.cos(a)},${y + r*1.15*Math.sin(a)}`);
      }
      return `<polygon points="${pts.join(' ')}"`;
    }
    case 'hexagon': {
      const pts = [];
      for (let i = 0; i < 6; i++) {
        const a = i * Math.PI / 3;
        pts.push(`${x + r*1.1*Math.cos(a)},${y + r*1.1*Math.sin(a)}`);
      }
      return `<polygon points="${pts.join(' ')}"`;
    }
    default:
      return `<circle cx="${x}" cy="${y}" r="${r}"`;
  }
}
function closeTag(shape) {
  return shape === 'circle' ? '</circle>' :
         shape === 'square' ? '</rect>' : '</polygon>';
}

function drawSkyView(sats) {
  const svg = document.getElementById('skyview');
  const cx = 110, cy = 110, R = 92;
  let out = '';

  for (const elev of [0, 30, 60]) {
    const r = R * (90 - elev) / 90;
    out += `<circle class="ring" cx="${cx}" cy="${cy}" r="${r}"/>`;
    if (elev > 0)
      out += `<text class="elevlbl" x="${cx + 2}" y="${cy - r - 2}">${elev}&#176;</text>`;
  }
  out += `<line class="axis" x1="${cx}" y1="${cy-R}" x2="${cx}" y2="${cy+R}"/>`;
  out += `<line class="axis" x1="${cx-R}" y1="${cy}" x2="${cx+R}" y2="${cy}"/>`;
  out += `<text class="compass" x="${cx}" y="${cy-R-4}" text-anchor="middle">N</text>`;
  out += `<text class="compass" x="${cx}" y="${cy+R+11}" text-anchor="middle">S</text>`;
  out += `<text class="compass" x="${cx+R+5}" y="${cy+3}">E</text>`;
  out += `<text class="compass" x="${cx-R-11}" y="${cy+3}">W</text>`;

  // Grey (visible) first so green (used) draws on top
  const sorted = [...sats].sort((a,b) => (a.used ? 1 : 0) - (b.used ? 1 : 0));
  for (const s of sorted) {
    if (s.az === null || s.az === undefined || s.elev === null || s.elev === undefined) continue;
    if (s.elev < 0) continue;   // below horizon
    const rad = R * (90 - Math.min(90, s.elev)) / 90;
    const a = (s.az - 90) * Math.PI / 180;   // 0deg az = north = up
    const x = cx + rad * Math.cos(a);
    const y = cy + rad * Math.sin(a);
    const c = CONSTELLATIONS[s.gnss] ?? OTHER;
    const cls = s.used ? 'sat-used' : 'sat-vis';
    out += shapePath(c.shape, x, y, 4.2) +
           ` class="${cls}"><title>${s.gnss} ${s.sv} &#183; el ${s.elev}&#176; az ${s.az}&#176; &#183; C/N0 ${s.cno ?? '&#8211;'} dBHz</title>` +
           closeTag(c.shape);
    out += `<text class="prnlbl" x="${x + 5.5}" y="${y + 2.5}">${s.sv}</text>`;
  }
  svg.innerHTML = out;
}

function buildSkyKey(sats) {
  const items = [];
  for (const name of ['GPS', 'SBAS', 'GLONASS', 'Galileo', 'BeiDou', 'QZSS']) {
    const visible = sats.filter(s => s.gnss === name).length;
    const used = sats.filter(s => s.gnss === name && s.used).length;
    const cls = used > 0 ? 'sat-used' : visible > 0 ? 'sat-vis' : 'sat-none';
    items.push(`<span class="item"><svg viewBox="-6 -6 12 12">` +
               shapePath(CONSTELLATIONS[name].shape, 0, 0, 4) +
               ` class="${cls}"/></svg>${name} <span class="count">${visible}/${used}</span></span>`);
  }
  document.getElementById('skykey').innerHTML = items.join('');
}
buildSkyKey([]);
drawSkyView([]);

// ---- Chrony -------------------------------------------------------------
const CHRONY_FIELDS = {
  chrReference: 'Reference ID',
  chrStratum: 'Stratum',
  chrSystemTime: 'System time',
  chrLastOffset: 'Last offset',
  chrRmsOffset: 'RMS offset',
  chrFrequency: 'Frequency',
  chrRootDelay: 'Root delay',
  chrRootDispersion: 'Root dispersion',
  chrLeapStatus: 'Leap status',
};
const CHRONY_DURATION_FIELDS = new Set([
  'System time', 'Last offset', 'RMS offset', 'Root delay', 'Root dispersion'
]);

function compactChronyDuration(value) {
  if (!value) return '\u2014';
  const match = value.trim().match(/^([+-]?\d+(?:\.\d+)?)\s+seconds?(?:\s+(.*))?$/i);
  if (!match) return value;

  const seconds = Number(match[1]);
  if (!Number.isFinite(seconds)) return value;
  const useNs = Math.abs(seconds) < 0.001;
  const scaled = seconds * (useNs ? 1e9 : 1e3);
  const magnitude = Math.abs(scaled);
  const decimals = magnitude >= 100 ? 0 : magnitude >= 10 ? 1 : magnitude >= 1 ? 2 : 3;
  let number = scaled.toFixed(decimals).replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1');
  if (match[1].startsWith('+') && scaled >= 0) number = '+' + number;

  const suffixText = (match[2] || '').toLowerCase();
  const direction = suffixText.includes('fast') ? ' fast' :
                    suffixText.includes('slow') ? ' slow' : '';
  return `${number} ${useNs ? 'ns' : 'ms'}${direction}`;
}

function renderChrony(d) {
  const tracking = d.chrony_tracking || {};
  for (const [id, field] of Object.entries(CHRONY_FIELDS)) {
    const value = tracking[field] || '\u2014';
    set(id, CHRONY_DURATION_FIELDS.has(field) ? compactChronyDuration(value) : value);
  }

  const container = document.getElementById('chronyClients');
  container.replaceChildren();
  const clients = Array.isArray(d.chrony_clients) ? d.chrony_clients : [];
  if (clients.length === 0) {
    const row = document.createElement('div');
    row.className = 'row';
    const label = document.createElement('span');
    label.className = 'k';
    label.textContent = d.chrony_clients_error ? 'Client data unavailable' : 'No clients recorded';
    row.appendChild(label);
    container.appendChild(row);
  } else {
    for (const client of clients) {
      const entry = document.createElement('div');
      entry.className = 'clientEntry';
      const host = document.createElement('div');
      host.className = 'clientHost';
      host.textContent = client.host;
      const ntp = document.createElement('span');
      ntp.className = 'clientNtp';
      ntp.textContent = client.ntp_requests;
      const last = document.createElement('span');
      last.className = 'clientLast';
      last.textContent = client.last_seen;
      entry.append(host, ntp, last);
      container.appendChild(entry);
    }
  }

  const statusRow = document.getElementById('chronyStatus');
  statusRow.classList.toggle('error', Boolean(d.chrony_error));
  statusRow.title = d.chrony_error || '';
  const age = d.chrony_updated_age_s;
  const bothFailed = d.chrony_tracking_error && d.chrony_clients_error;
  const partlyFailed = d.chrony_tracking_error || d.chrony_clients_error;
  set('chrStatus', bothFailed ? 'unavailable' : partlyFailed ? 'partial' :
      (age === null || age === undefined ? 'waiting\u2026' :
       age < 1 ? 'live' : `${age.toFixed(1)} s ago`));
}

// ---- Disciplined clock ---------------------------------------------------
// Free-runs on the browser's clock (same system clock Python stamps
// host_ms_at_fix with, so HTTP polling latency cancels out) and slews
// toward each GPS time sample.
let gpsOffset = null;      // smoothed (GPS - system clock) in ms
let lastFixHostMs = null;

setInterval(() => {
  const nowMs = Date.now();
  if (gpsOffset !== null) {
    const t = new Date(nowMs + gpsOffset).toISOString().slice(11, 19);
    document.getElementById('fixUtc').innerHTML = t + '<small>UTC</small>';
    const latency = Math.max(0, Math.round(-gpsOffset));
    set('clkOffset', latency + ' ms');
  }
  set('sysTime', new Date(nowMs).toLocaleTimeString(), false);
}, 100);

const infoBtn = document.getElementById('infoBtn');
const infoPop = document.getElementById('infoPop');
infoBtn.addEventListener('click', () => { infoPop.hidden = !infoPop.hidden; });

// ---- Poll loop -----------------------------------------------------------
async function poll() {
  let d;
  try {
    const r = await fetch('/data', {cache: 'no-store'});
    d = await r.json();
    set('linkStatus', d.error ? d.error : (d.connected ? 'live' : 'waiting for gpsd'));
  } catch (e) {
    set('linkStatus', 'server unreachable');
    return;
  }

  const hasFix = d.fix_ok && d.lat !== null && d.lon !== null;
  const lamp = document.getElementById('fixlamp');
  lamp.classList.toggle('ok', hasFix);
  const fixLabel = FIX_LABELS[d.fix_type] ?? ('FIX ' + d.fix_type);
  document.getElementById('fixLabel').textContent = fixLabel;

  // Time: feed the disciplining loop; the ticker below does the painting
  if (d.gps_utc_ms !== null && d.host_ms_at_fix !== null
      && d.host_ms_at_fix !== lastFixHostMs) {
    lastFixHostMs = d.host_ms_at_fix;
    const sample = d.gps_utc_ms - d.host_ms_at_fix;   // GPS minus system clock
    if (gpsOffset === null || Math.abs(sample - gpsOffset) > 1000) {
      gpsOffset = sample;                 // step on first sample / big jumps
    } else {
      gpsOffset += 0.2 * (sample - gpsOffset);        // gentle slew
    }
  }
  if (d.utc_iso) {
    const [year, month, day] = d.utc_iso.slice(0, 10).split('-');
    const displayDate = `${Number(month)}-${Number(day)}-${year}`;
    set('utcDate', displayDate + (d.time_valid ? '' : ' (unconfirmed)'));
  }

  // Position (respect privacy toggle)
  const hide = !showLoc.checked;
  set('lat',   hide ? 'hidden' : (hasFix ? d.lat.toFixed(5) + '\u00b0' : '\u2014'), hide);
  set('lon',   hide ? 'hidden' : (hasFix ? d.lon.toFixed(5) + '\u00b0' : '\u2014'), hide);
  set('alt',   hide ? 'hidden' : (d.alt_msl_m !== null ? d.alt_msl_m.toFixed(1) + ' m' : '\u2014'), hide);
  set('geoid', hide ? 'hidden' : (d.geoid_sep_m !== null ? d.geoid_sep_m.toFixed(1) + ' m' : '\u2014'), hide);
  set('acc',   d.h_acc_m !== null ? '\u00b1' + d.h_acc_m.toFixed(1) + ' m H / \u00b1' +
               (d.v_acc_m !== null ? d.v_acc_m.toFixed(1) : '\u2013') + ' m V' : '\u2014');
  set('speed', d.speed_kmh !== null ? d.speed_kmh.toFixed(2) + ' km/h' : '\u2014');

  // Signal
  set('satsUsed', d.sats_used || '\u2014');
  const inView = d.sats.length;
  set('satsView', inView || '\u2014');
  const missingElev = d.sats.filter(s => s.elev === null || s.elev === undefined).length;
  document.getElementById('missingElevRow').hidden = missingElev === 0;
  set('missingElev', missingElev === 1 ? '1 satellite' : missingElev + ' satellites');
  const dops = [d.hdop, d.vdop, d.pdop].map(x => x === null || x === undefined ? '\u2013' : x.toFixed(2));
  set('dops', dops.join(' / '));

  drawSkyView(d.sats);
  buildSkyKey(d.sats);

  // Stream
  set('framesOk', d.frames_ok);
  set('framesBad', d.frames_bad);
  set('gpsdStatus', d.connected ? 'connected' : 'disconnected');
  set('fixAge', d.last_fix_age_s !== null
      ? (d.last_fix_age_s < 1 ? '<1 s' : d.last_fix_age_s.toFixed(1) + ' s')
      : '\u2014');

  renderChrony(d);

  // Map
  if (hasFix && showLoc.checked) {
    const pos = [d.lat, d.lon];
    if (!marker) {
      marker = L.circleMarker(pos, {
        radius: 7, color: '#ffb454', weight: 2,
        fillColor: '#ffb454', fillOpacity: 0.6
      }).addTo(map);
    } else {
      marker.setLatLng(pos);
    }
    const acc = d.h_acc_m !== null ? Math.max(2, d.h_acc_m) : 10;
    if (!circle) {
      circle = L.circle(pos, { radius: acc, color: '#ffb454', weight: 1,
                               fillOpacity: 0.08 }).addTo(map);
    } else {
      circle.setLatLng(pos);
      circle.setRadius(acc);
    }
    if (firstFix) {
      map.setView(pos, 17);
      firstFix = false;
    }
    drawSatelliteBearings(d.lat, d.lon, d.sats);
  } else {
    satelliteBearingLayer.clearLayers();
  }
}

poll();
setInterval(poll, 500);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="u-blox GPS web dashboard using gpsd"
    )
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    reader = demo_reader if args.demo else gps_reader
    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    chrony_thread = threading.Thread(target=chrony_reader, daemon=True)
    chrony_thread.start()

    url = f"http://0.0.0.0:{args.port}"
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)

    print(f"GPS viewer running at {url}  (Ctrl+C to stop)")

    if args.demo:
        print("Running in demo mode.")
    else:
        print("GPS input: gpsd -> /dev/ttyAMA0")
        print("PPS input: /dev/pps0 -> chrony")
        print("Python will not discipline the system clock.")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_event.set()
        reader_thread.join(timeout=3)
        chrony_thread.join(timeout=3)
        server.server_close()
        print("Done.")


if __name__ == "__main__":
    main()
