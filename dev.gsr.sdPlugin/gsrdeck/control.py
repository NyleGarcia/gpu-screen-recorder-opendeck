"""Make GPU Screen Recorder do something.

Three ways in, tried in that order, because a machine can have any of them:

  1. The IPC socket, when the recorder was started with -ipc. This is the only
     one that says what happened -- it replies with the path of the file it
     saved -- and the only one that can save an arbitrary number of seconds.
  2. Signals, which every recorder accepts. There are six fixed replay lengths
     and no reply, so a save is confirmed by watching the output directory for
     a file that was not there before.
  3. gsr-ui-cli, which asks the overlay to start or stop something. This is
     the only way to START anything, because starting a recorder means
     choosing a monitor, a codec, a bitrate and a set of audio tracks, all of
     which the overlay already holds and the deck should not be a second,
     disagreeing copy of.

gsr-ui does not pass -ipc to the recorders it spawns, so on a machine using
the overlay -- which is the common case -- saves go by signal. The socket path
is still preferred whenever one exists, so a hand-started recorder, or a
future overlay that passes -ipc, gets the better behaviour for free.
"""

import json
import os
import signal
import socket
import subprocess
import time

# The fixed replay lengths, in seconds, that a signal can ask for. Anything
# else is snapped to the nearest of these when there is no socket.
PRESETS = {
    10: 1,
    30: 2,
    60: 3,
    300: 4,
    600: 5,
    1800: 6,
}
WHOLE_BUFFER = 0

SIG_STOP = signal.SIGINT
SIG_SAVE = signal.SIGUSR1
SIG_PAUSE = signal.SIGUSR2
SIG_TOGGLE_RECORDING = signal.SIGRTMIN

UI_CLI = "gsr-ui-cli"
# How long to wait for the socket to answer. A save of a long buffer is
# written before the reply comes, and a whole 30 minute buffer on a slow disk
# is not quick.
IPC_TIMEOUT = 60.0
# How long to keep looking for the file a signalled save produced.
SAVE_WATCH = 20.0


class Result:
    """What a control attempt did, in a shape a key can draw.

    `path` is only ever set when the recorder actually said so or a new file
    was found; a save that succeeded but whose file was not identified reports
    ok with no path rather than inventing one.
    """

    def __init__(self, ok, via="", path="", error=""):
        self.ok = ok
        self.via = via
        self.path = path
        self.error = error

    def __repr__(self):                            # pragma: no cover
        return f"Result(ok={self.ok}, via={self.via!r}, path={self.path!r})"


# --------------------------------------------------------------------- ipc
_request_id = 0


def ipc_request(path, name, data=None, timeout=IPC_TIMEOUT):
    """One request on a recorder's unix socket, and its reply.

    Requests and replies are newline-terminated JSON. The reply to a save
    arrives only once the file is written, which is why the timeout is
    generous and why the caller can treat a reply as proof rather than as an
    acknowledgement.
    """
    global _request_id
    if not path or not os.path.exists(path):
        return Result(False, "ipc", error="no socket")
    _request_id += 1
    request = {"id": _request_id, "name": name}
    if data is not None:
        request["data"] = data
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((json.dumps(request) + "\n").encode())
            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
    except (OSError, socket.timeout) as exc:
        return Result(False, "ipc", error=str(exc))
    line = buffer.split(b"\n", 1)[0]
    if not line:
        return Result(False, "ipc", error="no reply")
    try:
        reply = json.loads(line.decode("utf-8", "replace"))
    except ValueError as exc:
        return Result(False, "ipc", error=f"bad reply: {exc}")
    if reply.get("result") != "ok":
        return Result(False, "ipc", error=str(reply.get("data") or "failed"))
    payload = reply.get("data")
    return Result(True, "ipc", path=payload if isinstance(payload, str) else "")


def socket_alive(path):
    """Whether anything is listening, which is what gsr-cli status asks.

    A socket file left behind by a recorder that was killed still exists;
    connecting to it is what tells the two apart.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(path)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------- signals
def send_signal(pid, sig):
    try:
        os.kill(pid, sig)
        return True
    except (OSError, ProcessLookupError):
        return False


def snap_seconds(seconds):
    """The preset a signalled save will actually produce.

    Returned so the caller can say so. A key set to 45 seconds that silently
    saves 30 is a key that lies; one that shows "30s (nearest)" is honest
    about the limit of the transport.
    """
    if not seconds:
        return WHOLE_BUFFER
    return min(PRESETS, key=lambda preset: abs(preset - seconds))


# --------------------------------------------------------------- directory
def newest_file(directory):
    """(path, mtime) of the newest file in a directory, or (None, 0).

    Used to identify the file a signalled save produced: the recorder does not
    say, and the alternative is a key that cannot tell a save that worked from
    one that never happened.
    """
    try:
        entries = [os.path.join(directory, name)
                   for name in os.listdir(directory)]
    except OSError:
        return None, 0.0
    newest, when = None, 0.0
    for path in entries:
        try:
            if not os.path.isfile(path):
                continue
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > when:
            newest, when = path, mtime
    return newest, when


# -------------------------------------------------------------- gsr-ui-cli
_ui_cli_path = None


def ui_cli_available():
    global _ui_cli_path
    if _ui_cli_path is None:
        _ui_cli_path = _which(UI_CLI) or ""
    return bool(_ui_cli_path)


def _which(name):
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ui_running():
    """Whether the overlay is up, which is what can start a recorder.

    Read from /proc rather than by running gsr-ui-cli: the CLI exits zero
    whether or not anything received the command, so calling it proves
    nothing, and this runs once a second.
    """
    try:
        pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm") as handle:
                if handle.read().strip() == "gsr-ui":
                    return True
        except OSError:
            continue
    return False


def ui_cli(command, timeout=5.0):
    """Ask the overlay to do something. It does not report back.

    gsr-ui-cli writes the command to the running overlay and exits; a non-zero
    exit means the command could not be delivered, not that it failed, and a
    zero exit does not mean a recorder started. The caller confirms by looking
    at what is running a moment later.
    """
    if not ui_cli_available():
        return Result(False, "ui", error="gsr-ui-cli is not installed")
    if not ui_running():
        return Result(False, "ui", error="gsr-ui is not running")
    try:
        finished = subprocess.run(
            [_ui_cli_path, command], timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(False, "ui", error=str(exc))
    if finished.returncode != 0:
        message = (finished.stderr or b"").decode("utf-8", "replace").strip()
        return Result(False, "ui", error=message or "gsr-ui-cli failed")
    return Result(True, "ui")


# ------------------------------------------------------------------ verbs
def save_replay(inst, seconds=WHOLE_BUFFER, restart=None):
    """Save the replay buffer, by socket when there is one and signal when not.

    `seconds` of 0 means the whole buffer, which is the only length both
    transports agree on exactly.
    """
    if inst is None:
        return Result(False, error="replay is not running")
    if socket_alive(inst.get("ipc")):
        data = {}
        if seconds:
            data["seconds"] = int(seconds)
        if restart is not None:
            data["restart-replay"] = bool(restart)
        return ipc_request(inst["ipc"], "save-replay", data or None)

    directory = inst.get("output") or ""
    _, before = newest_file(directory)
    if seconds:
        sig = signal.SIGRTMIN + PRESETS[snap_seconds(seconds)]
    else:
        sig = SIG_SAVE
    if not send_signal(inst["pid"], sig):
        return Result(False, "signal", error="the recorder went away")
    return Result(True, "signal", path=await_new_file(directory, before))


def await_new_file(directory, before, deadline=SAVE_WATCH):
    """The file a signalled save produced, or "" if none appeared in time.

    Polled rather than watched: a save takes as long as the buffer is long,
    inotify would mean a second file descriptor in the event loop for one
    cosmetic string, and an empty answer degrades to a key that says "saved"
    without naming the file.
    """
    if not directory:
        return ""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        newest, when = newest_file(directory)
        if newest and when > before:
            return newest
        time.sleep(0.4)
    return ""


def toggle_recording(inst):
    """Start or stop a recording inside a running replay or stream."""
    if inst is None:
        return Result(False, error="nothing is running")
    if not inst.get("record_output"):
        return Result(False, error="the recorder has no -ro directory")
    if socket_alive(inst.get("ipc")):
        return ipc_request(inst["ipc"], "toggle-replay-recording")
    if not send_signal(inst["pid"], SIG_TOGGLE_RECORDING):
        return Result(False, "signal", error="the recorder went away")
    return Result(True, "signal")


def stop(inst):
    """Stop and save a standalone recording or stream."""
    if inst is None:
        return Result(False, error="nothing is running")
    if socket_alive(inst.get("ipc")):
        return ipc_request(inst["ipc"], "stop")
    if not send_signal(inst["pid"], SIG_STOP):
        return Result(False, "signal", error="the recorder went away")
    return Result(True, "signal")


def toggle_pause(inst):
    """Pause or unpause a recording.

    Neither transport reports the resulting state and it cannot be read from
    the process, so the caller tracks what it asked for rather than the key
    claiming to know.
    """
    if inst is None:
        return Result(False, error="nothing is running")
    if socket_alive(inst.get("ipc")):
        return ipc_request(inst["ipc"], "toggle-pause")
    if not send_signal(inst["pid"], SIG_PAUSE):
        return Result(False, "signal", error="the recorder went away")
    return Result(True, "signal")
