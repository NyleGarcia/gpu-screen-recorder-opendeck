"""What GPU Screen Recorder is doing right now, read from /proc.

There is no status request. gpu-screen-recorder's IPC socket accepts commands
and answers them, but it will not describe itself, and gsr-ui -- which is what
spawns the recorder on a machine using the overlay -- does not pass -ipc at
all. So the state a key shows is read from the process rather than asked for:

  * the command line says which mode it is in, how long the replay buffer is,
    which monitor it captures, and at what frame rate and bitrate,
  * an open write-only file descriptor says a file is being written right now,
    which is the only reliable sign that a recording is running -- during
    replay, recording is started with a signal and changes nothing else that
    is visible from outside,
  * the process start time gives how long it has been up, which is also how
    full a replay buffer is until it has run longer than the buffer,
  * VmRSS gives what a RAM-backed buffer is costing.

Everything here is read-only and cheap enough to run once a second: a scan
touches one small file per process and only opens /proc/<pid>/fd for the
recorders it found.

Two things genuinely cannot be seen from outside and are not guessed at:
whether a recording is paused, and which streaming service a stream is going
to when the URL carries a key rather than a host that names it.
"""

import os
import time

PROC = "/proc"
NAME = "gpu-screen-recorder"
# Options whose value is the next argument. Everything gpu-screen-recorder
# takes is of this shape apart from the --long information commands, which a
# running recorder never has.
_URL_SCHEMES = ("rtmp://", "rtmps://", "srt://", "udp://", "tcp://",
                "rtsp://", "http://", "https://", "whip://")
# A file that has been open for less than this is not called a recording. A
# replay save opens its output, writes the buffer and closes it, which on a
# long buffer takes long enough to be caught by a poll; a recording keeps its
# file open until it is stopped.
RECORDING_SETTLE = 1.5
# Paths a recorder writes to that are not the recording: shader caches, the
# kms socket, and anything under /dev or /proc.
_IGNORED = ("/.cache/", "/dev/", "/proc/", "/sys/", "/tmp/.gsr-",
            "/memfd:", "/run/user/")

HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# When a write fd was first seen, keyed by (pid, path), so a file that has
# only just appeared is not reported as a recording that has been running for
# no time at all.
_first_seen = {}


def _read(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def parse_argv(argv):
    """A recorder's arguments as {option: value}, with -a collected.

    Values are taken positionally rather than from a table of known options:
    gpu-screen-recorder gains options every release, and a table that has not
    been updated would silently swallow the next option as the previous one's
    value. An option followed by another option simply has no value, which is
    what the flagless information commands look like.
    """
    options = {}
    audio = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            index += 1
            continue
        value = ""
        if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
            value = argv[index + 1]
            index += 1
        if token == "-a":
            audio.append(value)
        else:
            options[token] = value
        index += 1
    options["-a"] = audio
    return options


def is_url(target):
    return any(target.startswith(scheme) for scheme in _URL_SCHEMES)


def classify(options):
    """replay, stream or record -- the three things a key can be about.

    Replay wins over stream when both look true: -r is what makes a buffer,
    and a buffer being streamed somewhere is still, to the person holding the
    deck, the thing their Save Replay key acts on.
    """
    if options.get("-r"):
        return "replay"
    if is_url(options.get("-o", "")):
        return "stream"
    return "record"


def _number(text, default=0):
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _uptime():
    raw = _read("/proc/uptime")
    if not raw:
        return None
    try:
        return float(raw.split()[0])
    except (IndexError, ValueError):
        return None


def process_age(pid, boot=None):
    """Seconds since the process started, or None.

    Field 22 of /proc/<pid>/stat is the start time in clock ticks since boot.
    The fields before it include the executable name in parentheses, which can
    itself contain spaces, so the line is split after the closing bracket
    rather than on whitespace from the left.
    """
    raw = _read(f"{PROC}/{pid}/stat")
    boot = _uptime() if boot is None else boot
    if not raw or boot is None:
        return None
    try:
        fields = raw.decode("utf-8", "replace").rsplit(") ", 1)[1].split()
        started = float(fields[19]) / HZ
    except (IndexError, ValueError):
        return None
    return max(0.0, boot - started)


def rss_kb(pid):
    raw = _read(f"{PROC}/{pid}/status")
    if not raw:
        return 0
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("VmRSS:"):
            return _number(line.split()[1])
    return 0


def _writable(pid, fd):
    """True when this descriptor was opened for writing.

    Checked rather than assumed: a recorder holds read handles on shader
    caches and on the file it is capturing from, and treating one of those as
    an output would show a recording that is not happening.
    """
    raw = _read(f"{PROC}/{pid}/fdinfo/{fd}")
    if not raw:
        return False
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("flags:"):
            try:
                return int(line.split()[1], 8) & 0o3 in (1, 2)
            except (IndexError, ValueError):
                return False
    return False


def open_outputs(pid, now=None):
    """Files this process is writing to, as [(path, bytes, seconds open)].

    The age is measured from when this plugin first saw the descriptor, not
    from the file's own timestamps: a recording appended to an existing name
    would look old, and what is being asked is "has this been open long enough
    to be a recording rather than a save in progress".
    """
    now = time.monotonic() if now is None else now
    found = []
    live = set()
    try:
        entries = os.listdir(f"{PROC}/{pid}/fd")
    except OSError:
        return found
    for entry in entries:
        try:
            target = os.readlink(f"{PROC}/{pid}/fd/{entry}")
        except OSError:
            continue
        if not target.startswith("/") or any(part in target
                                             for part in _IGNORED):
            continue
        if not _writable(pid, entry):
            continue
        key = (pid, target)
        live.add(key)
        first = _first_seen.setdefault(key, now)
        try:
            size = os.path.getsize(target)
        except OSError:
            size = 0
        found.append((target, size, now - first))
    for key in [k for k in _first_seen if k[0] == pid and k not in live]:
        del _first_seen[key]
    return found


def _real(path):
    """A path with its symlinks resolved, or itself when it cannot be."""
    if not path:
        return ""
    try:
        return os.path.realpath(path)
    except OSError:                                # pragma: no cover
        return path


def _pids():
    try:
        entries = os.listdir(PROC)
    except OSError:
        return []
    return [entry for entry in entries if entry.isdigit()]


def instances(now=None):
    """Every gpu-screen-recorder currently running, newest information first.

    Sorted by mode so a deck showing several keys agrees with itself when more
    than one recorder is up -- a replay and a separate window recording is a
    normal thing to be doing.
    """
    now = time.monotonic() if now is None else now
    boot = _uptime()
    found = []
    for pid in _pids():
        raw = _read(f"{PROC}/{pid}/cmdline")
        if not raw:
            continue
        argv = [part for part in raw.decode("utf-8", "replace").split("\0")
                if part]
        if not argv or os.path.basename(argv[0]) != NAME:
            continue
        options = parse_argv(argv)
        mode = classify(options)
        outputs = open_outputs(int(pid), now)
        replay_dir = options.get("-ro") or ""
        target = options.get("-o", "")
        recording = None
        # Compared after resolving symlinks. The recorder is told to write to
        # ~/games/videos and its descriptor reads back as /mnt/games/videos,
        # because the home directory holds a link to the disk the videos are
        # actually on -- a plain prefix match calls that a different place and
        # shows a recording that is running as stopped.
        replay_real = _real(replay_dir)
        target_real = _real(target)
        for path, size, age in outputs:
            real = _real(path)
            # In replay mode the recording goes to -ro; in the other modes the
            # output IS the recording. A file under the replay directory that
            # has only just been opened is a save being written, not a
            # recording that started.
            if mode == "replay":
                belongs = bool(replay_real) and real.startswith(
                    replay_real.rstrip("/") + "/")
            else:
                belongs = real == target_real or real.startswith(
                    target_real.rstrip("/") + "/")
            if belongs and age >= RECORDING_SETTLE:
                recording = (path, size, age)
                break
        found.append({
            "pid": int(pid),
            "mode": mode,
            "options": options,
            "ipc": options.get("-ipc") or "",
            "replay_seconds": _number(options.get("-r"), 0),
            "replay_storage": options.get("-replay-storage") or "ram",
            "output": target,
            "record_output": replay_dir,
            "capture": options.get("-w", ""),
            "fps": _number(options.get("-f"), 0),
            "codec": options.get("-k", "") or "auto",
            "container": options.get("-c", ""),
            "bitrate": _number(options.get("-q"), 0),
            "bitrate_mode": options.get("-bm", ""),
            "audio": list(options.get("-a") or []),
            "age": process_age(pid, boot) or 0.0,
            "rss_kb": rss_kb(pid),
            "recording": recording,
            "outputs": outputs,
        })
    order = {"replay": 0, "stream": 1, "record": 2}
    found.sort(key=lambda inst: (order.get(inst["mode"], 9), inst["pid"]))
    return found


def pick(instances_, mode):
    """The instance a key of this mode should act on, or None.

    A Record key falls back to the replay instance because recording during
    replay is the same button to the person pressing it: the recorder is
    already up, and the recording is started inside it.
    """
    for inst in instances_:
        if inst["mode"] == mode:
            return inst
    if mode == "record":
        for inst in instances_:
            if inst["mode"] in ("replay", "stream") and inst["record_output"]:
                return inst
    return None


def buffer_fill(inst):
    """How much of the replay buffer holds video, from 0.0 to 1.0.

    A buffer that has been running for less time than its length is not full,
    and saving it gives a shorter clip than the key claims. Showing the fill
    is the difference between a key that lies for the first minute and one
    that does not.
    """
    length = inst.get("replay_seconds") or 0
    if length <= 0:
        return 0.0
    return max(0.0, min(1.0, (inst.get("age") or 0.0) / length))
