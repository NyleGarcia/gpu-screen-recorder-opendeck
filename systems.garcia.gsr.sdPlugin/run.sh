#!/bin/sh
# OpenDeck launches plugins from inside its AppImage, whose AppRun exports a
# python environment of its own. Left in place, PYTHONHOME and PYTHONPATH send
# the system interpreter looking for its standard library inside the AppImage
# and it aborts before running a line; LD_LIBRARY_PATH breaks anything linking
# against the host's OpenSSL or GLib. Scrub them and use the system python.
#
# PATH is set rather than cleared: the plugin runs gsr-ui-cli, which lives in
# /usr/bin on a package install and in ~/.local/bin on some others, so the
# user's own bin directories are kept on the end.
#
# stdin is redirected from /dev/null: nothing here reads it, and leaving it
# inherited means a plugin can block on a descriptor it never intended to use.
exec 0</dev/null

# OpenDeck from Flathub runs plugins inside its sandbox, where /proc lists
# only the sandbox's own handful of processes and the recorder cannot be seen,
# signalled or reached through gsr-ui-cli. The Flatpak is granted
# org.freedesktop.Flatpak, so when it is available the plugin re-runs this
# script on the host with flatpak-spawn. --watch-bus ties the host process to
# OpenDeck: it is killed when the sandboxed side goes away. GSR_DECK_HOST
# stops the host side from trying again.
if [ -z "${GSR_DECK_HOST:-}" ] && [ -f /.flatpak-info ] \
    && command -v flatpak-spawn >/dev/null 2>&1 \
    && flatpak-spawn --host true >/dev/null 2>&1; then
  here=$(cd "$(dirname "$0")" && pwd)
  exec flatpak-spawn --host --watch-bus --directory="$here" \
    --env=GSR_DECK_HOST=1 --env=GSR_DECK_DEBUG="${GSR_DECK_DEBUG:-}" \
    /bin/sh "$here/run.sh" "$@"
fi
exec env \
  -u PYTHONHOME -u PYTHONPATH -u PYTHONDONTWRITEBYTECODE \
  -u LD_LIBRARY_PATH -u LD_PRELOAD \
  -u GIO_EXTRA_MODULES -u GSETTINGS_SCHEMA_DIR -u GDK_PIXBUF_MODULE_FILE \
  -u APPDIR -u APPIMAGE -u ARGV0 -u OWD \
  PATH="/usr/bin:/bin:/usr/local/bin:$HOME/.local/bin" \
  GSR_DECK_DEBUG="${GSR_DECK_DEBUG:-}" \
  /usr/bin/python3 "$(dirname "$0")/plugin.py" "$@"
