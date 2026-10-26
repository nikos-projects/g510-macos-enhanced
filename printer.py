"""Read print status from a Creality printer on the local network.

The printer serves a WebSocket whose first frame is a complete snapshot of
every field it tracks; everything after that is deltas. Reading one snapshot
and closing costs about 30ms, so there is no persistent connection to manage -
just a poll on a cache.
"""
import json
import os

import config
import refresh

DEFAULT_PORT = 9999
CONNECT_TIMEOUT = 3.0

_source = None
_source_key = None


BACKENDS = ("auto", "creality", "moonraker", "octoprint", "prusalink")


def settings():
    """The printer block from the config, whether or not it is filled in."""
    block = config.load().get("printer") or {}
    kind = (block.get("kind") or "auto").lower()
    return {
        "enabled": bool(block.get("enabled")),
        "host": (block.get("host") or "").strip(),
        "port": int(block.get("port") or DEFAULT_PORT),
        "refresh": float(block.get("refresh_seconds") or 5.0),
        "kind": kind if kind in BACKENDS else "auto",
        "api_key": (block.get("api_key") or "").strip(),
    }


def status(force=False):
    """Latest snapshot, or None if the printer is off, unset or unreachable.

    Never blocks: the network round trip happens on a worker, so a printer
    that has gone away cannot stall the caller for the connect timeout.
    """
    global _source, _source_key
    conf = settings()
    if not conf["enabled"] or not conf["host"]:
        return None
    key = (conf["host"], conf["port"], conf["refresh"], conf["kind"],
           conf["api_key"])
    if key != _source_key:
        _source_key = key
        _source = refresh.Background(
            lambda: _fetch(conf), conf["refresh"])
    return _source.get_now() if force else _source.get()


def last_error():
    return _source.error if _source else None


def _fetch(conf):
    """Try whichever backend is configured, or each in turn."""
    order = ([conf["kind"]] if conf["kind"] != "auto"
             else ["creality", "moonraker", "prusalink", "octoprint"])
    errors = []
    for kind in order:
        reading, error = _BACKENDS[kind](conf)
        if reading:
            return reading, None
        errors.append(f"{kind}: {error}")
    return None, "; ".join(errors)


def _fetch_creality(conf):
    """Creality firmware: a WebSocket whose first frame is a full snapshot."""
    host, port = conf["host"], conf["port"]
    try:
        import websocket
    except ImportError:
        return None, "websocket-client is not installed"
    url = f"ws://{host}:{port}"
    try:
        connection = websocket.create_connection(url, timeout=CONNECT_TIMEOUT)
    except Exception as exc:
        return None, f"could not reach {host}:{port} ({exc})"
    try:
        snapshot = json.loads(connection.recv())
    except (ValueError, OSError) as exc:
        return None, f"bad reply from {host} ({exc})"
    finally:
        try:
            connection.close()
        except Exception:
            pass
    if not isinstance(snapshot, dict):
        return None, "unexpected reply shape"
    return _normalise(snapshot), None


def _get_json(url, timeout=CONNECT_TIMEOUT, headers=None):
    import json as _json
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _json.loads(response.read().decode()), None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(exc)


def _fetch_moonraker(conf):
    """Klipper via Moonraker, which most non-vendor firmware exposes."""
    host = conf["host"]
    port = conf["port"] if conf["kind"] == "moonraker" else 7125
    url = (f"http://{host}:{port}/printer/objects/query"
           "?print_stats&display_status&extruder&heater_bed")
    body, error = _get_json(url)
    if not body:
        return None, error
    status = (body.get("result") or {}).get("status") or {}
    stats = status.get("print_stats") or {}
    display = status.get("display_status") or {}
    extruder = status.get("extruder") or {}
    bed = status.get("heater_bed") or {}
    if not stats and not display:
        return None, "no print_stats in the reply"
    progress = float(display.get("progress") or 0) * 100
    elapsed = float(stats.get("print_duration") or 0)
    # Moonraker reports elapsed, not remaining, so estimate from progress.
    left = int(elapsed / progress * 100 - elapsed) if progress > 1 else 0
    state = {"printing": 1, "paused": 2, "complete": 3, "cancelled": 3,
             "error": 3, "standby": 0}.get(stats.get("state"), 0)
    return {
        "model": conf["host"], "file": str(stats.get("filename") or ""),
        "progress": int(progress), "left": left, "elapsed": int(elapsed),
        "layer": int(stats.get("info", {}).get("current_layer") or 0),
        "layers": int(stats.get("info", {}).get("total_layer") or 0),
        "nozzle": float(extruder.get("temperature") or 0),
        "nozzle_target": float(extruder.get("target") or 0),
        "bed": float(bed.get("temperature") or 0),
        "bed_target": float(bed.get("target") or 0),
        "state": state,
    }, None


def _fetch_octoprint(conf):
    """OctoPrint, which needs an API key from its settings page."""
    host = conf["host"]
    port = conf["port"] if conf["kind"] == "octoprint" else 80
    if not conf["api_key"]:
        return None, "needs an API key (g510 printer key <key>)"
    headers = {"X-Api-Key": conf["api_key"]}
    job, error = _get_json(f"http://{host}:{port}/api/job", headers=headers)
    if not job:
        return None, error
    printer_state, _ = _get_json(f"http://{host}:{port}/api/printer",
                                 headers=headers)
    temps = ((printer_state or {}).get("temperature") or {})
    progress = job.get("progress") or {}
    flags = ((printer_state or {}).get("state") or {}).get("flags") or {}
    state = 1 if flags.get("printing") else 2 if flags.get("paused") else 0
    return {
        "model": conf["host"],
        "file": str(((job.get("job") or {}).get("file") or {}).get("name") or ""),
        "progress": int(progress.get("completion") or 0),
        "left": int(progress.get("printTimeLeft") or 0),
        "elapsed": int(progress.get("printTime") or 0),
        "layer": 0, "layers": 0,
        "nozzle": float((temps.get("tool0") or {}).get("actual") or 0),
        "nozzle_target": float((temps.get("tool0") or {}).get("target") or 0),
        "bed": float((temps.get("bed") or {}).get("actual") or 0),
        "bed_target": float((temps.get("bed") or {}).get("target") or 0),
        "state": state,
    }, None


def _fetch_prusalink(conf):
    """PrusaLink, on MK4/XL/Mini. Its API key is on the printer's screen."""
    host = conf["host"]
    port = conf["port"] if conf["kind"] == "prusalink" else 80
    if not conf["api_key"]:
        return None, "needs an API key (g510 printer key <key>)"
    headers = {"X-Api-Key": conf["api_key"]}
    body, error = _get_json(f"http://{host}:{port}/api/v1/status",
                            headers=headers)
    if not body:
        return None, error
    job = body.get("job") or {}
    printer_state = body.get("printer") or {}
    state = {"PRINTING": 1, "PAUSED": 2, "FINISHED": 3, "STOPPED": 3,
             "IDLE": 0, "READY": 0}.get(
                 str(printer_state.get("state", "")).upper(), 0)
    return {
        "model": conf["host"],
        "file": str((job.get("file") or {}).get("display_name")
                    or (job.get("file") or {}).get("name") or ""),
        "progress": int(job.get("progress") or 0),
        "left": int(job.get("time_remaining") or 0),
        "elapsed": int(job.get("time_printing") or 0),
        "layer": 0, "layers": 0,
        "nozzle": float(printer_state.get("temp_nozzle") or 0),
        "nozzle_target": float(printer_state.get("target_nozzle") or 0),
        "bed": float(printer_state.get("temp_bed") or 0),
        "bed_target": float(printer_state.get("target_bed") or 0),
        "state": state,
    }, None


_BACKENDS = {
    "creality": _fetch_creality,
    "moonraker": _fetch_moonraker,
    "octoprint": _fetch_octoprint,
    "prusalink": _fetch_prusalink,
}


def _normalise(snapshot):
    """Pick out the handful of fields worth showing, with sane types."""
    def number(key, default=0.0):
        try:
            return float(snapshot.get(key, default) or 0)
        except (TypeError, ValueError):
            return default

    name = os.path.basename(str(snapshot.get("printFileName") or ""))
    for suffix in (".gcode", ".gco"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return {
        "model": snapshot.get("model") or snapshot.get("hostname") or "printer",
        "file": name,
        "progress": int(number("printProgress")),
        "left": int(number("printLeftTime")),
        "elapsed": int(number("printJobTime")),
        "layer": int(number("layer")),
        "layers": int(number("TotalLayer")),
        "nozzle": number("nozzleTemp"),
        "nozzle_target": number("targetNozzleTemp"),
        "bed": number("bedTemp0"),
        "bed_target": number("targetBedTemp0"),
        "state": int(number("state")),
    }


# Observed on a K1C rather than documented, so anything unrecognised is
# reported as its raw number instead of being guessed at.
STATES = {0: "idle", 1: "printing", 2: "paused", 3: "stopped"}


def describe_state(reading):
    """A short word for what the printer is doing."""
    if not reading:
        return "offline"
    state = reading.get("state")
    if state == 3 and reading.get("progress", 0) >= 100:
        return "done"
    return STATES.get(state, f"state {state}")


def is_printing(reading):
    return bool(reading) and reading.get("state") == 1
