# GPU Screen Recorder for OpenDeck

An [OpenDeck](https://github.com/nekename/OpenDeck) plugin that puts
[GPU Screen Recorder](https://git.dec05eba.com/gpu-screen-recorder/about/) on a
Stream Deck: the replay buffer, the recording and the stream visible on the
keys without leaving the game, and one physical key that saves the buffer.

Linux only, Python only, no dependencies — it runs against whatever `python3`
the distribution ships.

## Actions

| Action | What the key shows | What pressing it does |
| --- | --- | --- |
| **Replay** | Whether the buffer is running, how long it is, which storage it uses, and how full it is while it is still filling | Starts or stops the replay (settable to start-only or stop-only) |
| **Save Replay** | How much it will save, and the name of the file it just wrote | Saves the buffer — whole, or the last 10s / 30s / 1m / 5m / 10m / 30m |
| **Record** | `REC` with elapsed time and the file's size, or `PAUSED` | Starts or stops a recording, including one inside a running replay; can be set to pause instead |
| **Stream** | `LIVE`, the service, and how long it has been up | Starts or stops streaming |
| **Screenshot** | The area it captures and the last file written | Takes a screenshot of the screen, a region or a window |
| **Status** | Mode, monitor, frame rate, codec, bitrate, audio tracks and the buffer's RAM | Nothing. It is a readout, on purpose |

Every key draws itself from live state, so a replay started from the overlay's
hotkey shows on the deck without the deck being touched.

## How it talks to the recorder

There is no status request in GPU Screen Recorder's protocol, so state is read
from `/proc`: the command line gives the mode, buffer length, capture target,
frame rate, codec and bitrate; an open write-only file descriptor is what says
a recording is actually running; the process start time gives its age.

Control uses whichever of three routes exists, in this order:

1. **The IPC socket**, when the recorder was started with `-ipc`. The only one
   that saves an arbitrary number of seconds and the only one that reports the
   path of the file it wrote.
2. **Signals**, which every recorder accepts. Six fixed replay lengths and no
   reply, so a save is confirmed by watching the output directory. A key set to
   a length the signals cannot express shows it rounded, marked `*`.
3. **`gsr-ui-cli`**, which asks the overlay to start or stop something.

Starting anything goes through the overlay deliberately. Which monitor, which
codec, which audio tracks and where files land are chosen in GPU Screen
Recorder's own UI; a deck holding a second, disagreeing copy of that is how you
end up streaming to the wrong account or recording the wrong screen.

`gsr-ui` does not pass `-ipc` to the recorders it spawns, so on a machine using
the overlay saves go by signal today. The socket is preferred whenever one
exists, so a hand-started recorder gets the better behaviour with no change
here.

### What it cannot see

Two things are not readable from outside the recorder and are not guessed at:
whether a recording is **paused** (a Record key set to pause shows what it last
asked for, and a pause from a hotkey will disagree until the key is pressed
again), and which service a stream goes to when the URL is a custom one — that
comes from the config file instead.

## Requirements

- `gpu-screen-recorder`
- `gpu-screen-recorder-ui` for `gsr-ui-cli`, if the deck should be able to
  *start* things rather than only save, stop and report
- OpenDeck 2.14 or newer, running unsandboxed. Under Flatpak the plugin cannot
  read `/proc` for other processes or signal them, which is most of what it
  does.

## Install

```sh
make install     # copies the bundle into ~/.config/opendeck/plugins
```

Then restart OpenDeck. To build the archive OpenDeck's installer takes:

```sh
make package     # dist/gpu-screen-recorder-opendeck-<version>.zip
```

## Development

```sh
make check       # bundle validation, byte-compile, and the test suite
make icons       # redraw the action icons from the key glyphs
```

The tests fabricate a `/proc` tree and stand up a real unix socket speaking the
real IPC protocol, so the whole suite runs with no deck, no GPU and no
recorder.

## Layout

```
dev.gsr.sdPlugin/
  manifest.json      the actions OpenDeck offers
  run.sh             launcher; scrubs the AppImage's Python environment
  plugin.py          the event loop and one renderer per action
  gsrdeck/
    state.py         what the recorder is doing, read from /proc
    control.py       the three ways to drive it
    config.py        GPU Screen Recorder's own settings, read-only
    render.py        the key art
    ws.py            a minimal RFC 6455 client
  pi/                property inspectors
```
