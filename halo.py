#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Halo — a floating Google search popup for Fedora Workstation.
# Copyright (C) 2026 Choder7 <https://github.com/Choder7/Halo>
#
# SPDX-License-Identifier: GPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License, version 3, as published by the
# Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.
#
# ADDITIONAL TERM under section 7(b) of the GNU General Public License v3:
# if you redistribute this program, with or without modification, you must
# preserve the attribution above — the author name and the address of the
# original repository — in the source and in any "Appropriate Legal Notices"
# the work displays. Removing it is not permitted.
"""
Halo — a floating Google search popup for Fedora Workstation (GNOME / Wayland).

A Spotlight/Raycast-style translucent search pill that appears on a keybind,
expands into native dark-mode Google results, and floats above everything
without stealing your clipboard or your patience.

────────────────────────────────────────────────────────────────────────────
INSTALL (no terminal required)
────────────────────────────────────────────────────────────────────────────
  1. Right-click halo.py in Files → Properties → Permissions →
     tick "Allow executing file as program".
  2. Double-click it (choose "Run" if asked).
  3. In the popup, click the ⋯ button → "Finish setup".
     That single button adds Halo to your Applications grid, starts it at
     login, and binds a global shortcut (Super+G by default).

Everything Halo needs is already on a stock Fedora Workstation install:
GTK 4, libadwaita and WebKitGTK 6 ship with GNOME Shell itself, and
PyGObject arrives via gnome-browser-connector. No pip, no dnf, no Flatpak.

────────────────────────────────────────────────────────────────────────────
DESIGN NOTES (the non-obvious bits)
────────────────────────────────────────────────────────────────────────────
* User agent: we deliberately DO NOT spoof it. Claiming to be Chrome or
  Firefox while running the WebKit engine is a fingerprint contradiction and
  Google answers it with an instant /sorry/index CAPTCHA. WebKitGTK's honest
  Safari-family UA sails straight through. Verified empirically.

* Dark mode is genuinely native: forcing the libadwaita dark scheme makes
  WebKit report `prefers-color-scheme: dark`, and Google serves its real dark
  theme. No injected stylesheets, no invert filters.

* Cookies live in a persistent SQLite jar, and a one-time invisible warm-up
  visit auto-dismisses the EU consent banner ("Reject all"), so you never see
  it. That warm-up also earns the NID cookie that keeps CAPTCHAs away.

* We run on XWayland on purpose. Wayland forbids a client from positioning
  itself or asking to stay on top; X11 lets us do both via EWMH. That buys
  mouse-aware placement, always-on-top, and a click-through shadow margin.
  If XWayland is missing we fall back to Wayland and simply skip those.
"""

from __future__ import annotations

# Only what the constants below and the fast path need. Everything else is
# imported further down, after the fast path has had its chance to exit: the
# shortcut spawns a fresh interpreter on every keypress, and json, subprocess,
# shutil, uuid and urllib.request together cost 66ms of import that a process
# whose whole job is one D-Bus call never touches.
import os
import sys
from pathlib import Path

APP_ID = "io.github.choder7.Halo"
APP_NAME = "Halo"
VERSION = "1.35.1"

AUTHOR = "Choder7"
PROJECT_URL = "https://github.com/Choder7/Halo"

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "halo"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "halo"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.json"


def _own_dir(path: "Path") -> None:
    """Make a directory of ours, and make it nobody else's.

    DATA_DIR holds cookies.sqlite. That file *is* the signed-in session: anything
    that can read it can be you, on every site you are logged into, without
    needing a password or a second factor. Halo used to create the directory
    with the default umask — 0755 here — and the cookie jar 0644, which is
    readable by every other account on the machine. It happened to be safe on
    this box because ~/.local/share is itself 0700, but that is somebody else's
    decision and it is not guaranteed; Firefox and Chrome both make their own
    profile directories private for exactly this reason.

    chmod as well as mkdir, because mode= only applies to a directory being
    created and every existing install already has the loose one.
    """
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    except Exception:
        pass


def _own_file(path: "Path") -> None:
    """...and the same for a file we wrote, best effort."""
    try:
        if path.exists():
            path.chmod(0o600)
    except Exception:
        pass
SELF_PATH = Path(__file__).resolve()

DEFAULTS = {
    "panel_height": 620,
    "window_width": 760,
    "suggestions": True,
    "remember_position": False,
    "position": None,          # [x, y] in device pixels, when remembered
    "safe_search": False,
    "compact_results": True,   # hide Google's duplicate search box
    "warmed_up": False,        # consent banner already dismissed once
    "shortcuts": ["<Super>g"],   # any number of keys may open Halo
    "custom_shortcuts": [],      # accelerators added by hand, beyond the presets
    "parked_arrow": True,        # the detached ↓ button for a collapsed page
    "vertical_anchor": 0.26,     # fraction of screen height for the pill's top
    "reveal_ms": 280,            # how long the panel takes to open; 0 is instant
    "idle_release_min": 10,      # give the browser engine back after this long
                                 # hidden; 0 keeps it resident for ever
    "history": True,             # keep a list of what has been searched
    "history_in_suggestions": True,   # surface close past searches while typing
    "history_days": 60,          # forget entries older than this; 0 keeps them
    # The picture the pill is carrying, as {"path", "name", "uri", "origin"} —
    # or None. State rather than a setting, and kept here for the same reason
    # "position" is: this is the one file Halo already writes on a change and
    # reads before the window exists.
    #
    # It is NOT put back on the field when Halo opens. A picture is part of a
    # session, like the parked page and the last query, and a chip sitting on a
    # pill that was opened to search for something else is a chip nobody asked
    # for — reported exactly that way. What this holds is the *stash*: what ↓
    # and a double tap of the shortcut bring back along with the page. See
    # _stash_attachment(), _unstash_attachment() and _seed_stash().
    "attached_image": None,
    # The colour the results page last told us it paints, as "#rrggbb", or None
    # before anything has ever been asked. State rather than a setting, kept
    # here for the same reason "position" and "attached_image" are: this is the
    # one file Halo already writes on a change and reads before the window
    # exists.
    #
    # Without it the panel opens in the PAGE_BG constant, and the toolbar sits
    # visibly darker than Google until the first load answers — and again after
    # every idle release, because rebuilding the WebView goes back to the
    # constant. Remembering what was adopted last time means the very first
    # frame is already right. _adopt_page_bg() still asks the page and still
    # wins, so a Google restyle simply overwrites this on the next load.
    "page_bg": None,
}

# Offered in the settings menu. The Copilot key on newer keyboards sends
# Shift+Super+F23 — nothing on Linux claims it, which makes it an ideal way to
# open Halo instead of leaving the key dead.
# Tick as many as you like. All of these are unclaimed on a stock GNOME 44
# except Super+Space, which GNOME uses to switch input source — it stays on the
# list because people ask for it, and the menu flags the clash on its own row.
SHORTCUT_PRESETS = [
    ("<Super>g", "Super + G"),
    ("<Shift><Super>F23", "Copilot key"),
    ("<Control><Alt>space", "Ctrl + Alt + Space"),
    ("<Control><Super>space", "Ctrl + Super + Space"),
    ("<Super>slash", "Super + /"),
    ("<Super>space", "Super + Space"),
]

# What the manual is called in the ⋯ menu. It was "Manual, shortcuts & what Halo
# installs…", which is a table of contents rather than a name: too long for the
# row, and nothing in it is the word the eye goes looking for. Named in one place
# because the setup notification points at it too, and the two had already
# drifted apart.
MANUAL_LABEL = "Manual & shortcuts…"

# Keys whose printed name tells you nothing. GTK spells the Copilot key
# "Shift+Super+F23" — accurate, and no help at all to anyone wondering which
# key that is, least of all in a locale where it reads "Umschalt+Super+F23".
# accel_label() appends the alias wherever an accelerator is shown, so the
# manual, the status line and the conflict warnings all name the actual button.
ACCEL_ALIASES = {
    "<Shift><Super>F23": "Copilot key",
}


# ══════════════════════════════════════════════════════════════════════════
#  Bootstrap: verify the runtime, then pick a display backend
# ══════════════════════════════════════════════════════════════════════════

def _notify(summary: str, body: str) -> None:
    """Desktop notification via gdbus, which ships in glib2 and is always there."""
    try:
        subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.Notifications",
             "--object-path", "/org/freedesktop/Notifications",
             "--method", "org.freedesktop.Notifications.Notify",
             APP_NAME, "0", "system-search", summary, body, "[]", "{}", "5000"],
            check=False, capture_output=True, timeout=5)
    except Exception:
        pass


def _offer_gui_install(packages: list[str]) -> None:
    """Ask PackageKit to install what's missing, with GNOME's own GUI prompt."""
    spec = "['" + "', '".join(packages) + "']"
    _notify(f"{APP_NAME} needs one component",
            "Opening the Software installer for: " + ", ".join(packages))
    try:
        subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.PackageKit",
             "--object-path", "/org/freedesktop/PackageKit",
             "--method", "org.freedesktop.PackageKit.Modify2.InstallPackageNames",
             "0", spec, "", ""],
            check=False, timeout=600)
    except Exception:
        pass


def _bootstrap() -> None:
    missing: list[str] = []
    try:
        import gi  # noqa: F401
    except ImportError:
        _offer_gui_install(["python3-gobject-base"])
        print("Halo: PyGObject is missing. A graphical installer was opened.\n"
              "      Re-launch Halo once it finishes.", file=sys.stderr)
        raise SystemExit(1)

    import gi
    for name, ver, pkg in (("Gtk", "4.0", "gtk4"),
                           ("Adw", "1", "libadwaita"),
                           ("WebKit", "6.0", "webkitgtk6.0")):
        try:
            gi.require_version(name, ver)
        except (ValueError, ImportError):
            missing.append(pkg)
    if missing:
        _offer_gui_install(missing)
        print(f"Halo: missing {', '.join(missing)}. A graphical installer was opened.",
              file=sys.stderr)
        raise SystemExit(1)


def _select_backend() -> bool:
    """Prefer XWayland so we can position ourselves and stay on top.

    Returns True when the X11 backend was selected. We probe the X socket with
    libX11 first, so we never hand GTK a backend that cannot start.
    """
    if os.environ.get("GDK_BACKEND"):
        return os.environ["GDK_BACKEND"] == "x11"
    if not os.environ.get("DISPLAY"):
        return False
    try:
        import ctypes
        lib = ctypes.CDLL("libX11.so.6")
        lib.XOpenDisplay.restype = ctypes.c_void_p
        dpy = lib.XOpenDisplay(None)
        if not dpy:
            return False
        lib.XCloseDisplay(ctypes.c_void_p(dpy))
    except Exception:
        return False
    os.environ["GDK_BACKEND"] = "x11"
    return True


# There used to be a _pin_cursor_size() here, setting XCURSOR_SIZE from GNOME's
# cursor-size before GTK started, to stop the pointer going huge over Halo.
# It never worked, and it is worth saying why so it does not come back. GTK4
# does not consult XCURSOR_SIZE: GDK takes its cursor size from the XSettings
# key Gtk/CursorThemeSize and hands libXcursor an explicit size, and libXcursor
# only falls back to the environment when the size it is given is zero. Measured
# on GTK 4.22 — with XCURSOR_SIZE=99 in the environment, gtk-cursor-theme-size
# still reads 24. So the whole function was a no-op with one real side effect:
# os.environ leaks into every process Halo starts, which is the WebKit web and
# network processes and the user's own browser on Ctrl+Enter.
#
# The pointer really does come out the wrong size, though, and HaloWindow's
# _sync_cursor_size() is the part that fixes it, at runtime and per monitor,
# where the information needed to get it right actually exists.


# ══════════════════════════════════════════════════════════════════════════
#  The shortcut's fast path: answer before loading the GUI stack at all
# ══════════════════════════════════════════════════════════════════════════
#
# The keybind runs `halo.py --toggle` — a whole new Python process whose only job
# is to nudge the resident one into view. Loading GTK, libadwaita and WebKitGTK to
# make a single D-Bus call cost about 0.4s of dead time between the keypress and
# the popup (measured 0.43 / 0.44 / 0.37 / 0.41s wall), and importing
# gi.repository.Adw alone is 114ms of typelib this process never uses. Gio on its
# own is 24ms and can place the call perfectly well.
#
# Every branch here degrades to "do it the long way", so a missing PyGObject, a
# stale bus name, or an older resident copy that has never heard of these actions
# all end up on exactly the path they took before.

def _remote_action(argv: list[str]) -> tuple[str | None, list[str]]:
    """The action a copy already running could carry out for this command line."""
    args = argv[1:]
    if not args:
        return "show", []
    if len(args) == 1 and args[0] in ("--toggle", "--show"):
        return args[0].lstrip("-"), []
    if len(args) == 2 and args[0] in ("-q", "--query"):
        return "search", [args[1]]
    if len(args) == 1 and args[0].startswith("--query="):
        return "search", [args[0].split("=", 1)[1]]
    return None, []


def _handle_without_gui(argv: list[str]) -> bool:
    """True when this invocation is finished and the process may just exit."""
    if len(argv) == 2 and argv[1] == "--version":
        print(f"{APP_NAME} {VERSION}")
        return True

    action, params = _remote_action(argv)
    if action is None:
        return False
    try:
        import gi  # noqa: F401
        from gi.repository import Gio, GLib
    except Exception:
        return False        # let _bootstrap() explain what is missing

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # NO_AUTO_START: never spawn a service, only talk to a live one.
        owned = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (APP_ID,)), GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
        if not owned.unpack()[0]:
            return False    # nobody home; this process has to become Halo itself

        path = "/" + APP_ID.replace(".", "/")
        # Ask what the resident copy can actually do before asking it to do it.
        # An older Halo exports no actions at all, and GApplication answers an
        # unknown action by quietly doing nothing — which would turn the shortcut
        # into a no-op for anyone who upgraded without restarting.
        known = bus.call_sync(
            APP_ID, path, "org.gtk.Actions", "DescribeAll", None,
            GLib.VariantType("(a{s(bgav)})"), Gio.DBusCallFlags.NONE, 2000, None)
        if action not in known.unpack()[0]:
            return False

        bus.call_sync(
            APP_ID, path, "org.freedesktop.Application", "ActivateAction",
            GLib.Variant("(sava{sv})",
                         (action, [GLib.Variant("s", p) for p in params], {})),
            None, Gio.DBusCallFlags.NONE, 5000, None)
        return True
    except Exception:
        return False


# Only when run as a program: importing halo.py must never exit the interpreter.
if __name__ == "__main__" and _handle_without_gui(sys.argv):
    raise SystemExit(0)

# The rest of the standard library, now that we know this process is going to be
# a real Halo. The functions above are only ever *called* from here on, so they
# find these names in the module namespace exactly as before.
import base64         # noqa: E402
import gc             # noqa: E402
import json           # noqa: E402
import math           # noqa: E402
import re             # noqa: E402
import shutil         # noqa: E402
import subprocess     # noqa: E402
import threading      # noqa: E402
import time           # noqa: E402
import urllib.parse   # noqa: E402
import uuid           # noqa: E402
import warnings       # noqa: E402

_bootstrap()
ON_X11 = _select_backend()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")
# Graphene carries the point compute_point() maps between coordinate
# spaces. Pinned like the rest, or PyGObject prints a PyGIWarning about
# an unversioned import on every launch.
gi.require_version("Graphene", "1.0")
# GdkPixbuf scales and crops the attached picture down to the chip beside the
# search field. Gdk.Texture can be loaded from a file but not resized, and a
# 4000px photo handed to a 26px GtkImage is 60MB of texture for a thumbnail.
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import (Adw, Gdk, GdkPixbuf, Gio, GLib,  # noqa: E402
                           GObject, Graphene, Gtk, Pango, WebKit)

if ON_X11:
    try:
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11  # noqa: F401,E402
    except Exception:
        ON_X11 = False


# ══════════════════════════════════════════════════════════════════════════
#  X11 window management: always-on-top, placement, click-through margin
# ══════════════════════════════════════════════════════════════════════════

class X11WM:
    """Thin ctypes shim over libX11 for the things Wayland will not allow.

    Everything here is best-effort: each method degrades to a no-op rather
    than raising, so the app behaves sanely under plain Wayland too.
    """

    _NET_WM_STATE_ADD = 1
    _CLIENT_MESSAGE = 33
    _SUBSTRUCTURE = (1 << 20) | (1 << 19)

    def __init__(self) -> None:
        self.ok = False
        if not ON_X11:
            return
        try:
            import ctypes

            self.ct = ctypes
            self.x11 = ctypes.CDLL("libX11.so.6")
            self.x11.XOpenDisplay.restype = ctypes.c_void_p
            self.x11.XInternAtom.restype = ctypes.c_ulong
            self.x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
            self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            self.x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.x11.XChangeProperty.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            self.x11.XGetWindowProperty.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
            self.dpy = self.x11.XOpenDisplay(None)
            if not self.dpy:
                return
            self.root = self.x11.XDefaultRootWindow(ctypes.c_void_p(self.dpy))

            class ClientMessage(ctypes.Structure):
                _fields_ = [("type", ctypes.c_int),
                            ("serial", ctypes.c_ulong),
                            ("send_event", ctypes.c_int),
                            ("display", ctypes.c_void_p),
                            ("window", ctypes.c_ulong),
                            ("message_type", ctypes.c_ulong),
                            ("format", ctypes.c_int),
                            ("data", ctypes.c_long * 5)]

            class XEvent(ctypes.Union):
                _fields_ = [("xclient", ClientMessage), ("pad", ctypes.c_long * 24)]

            self._ClientMessage = ClientMessage
            self._XEvent = XEvent
            self.ok = True
        except Exception:
            self.ok = False

        self.xext = None
        if self.ok:
            try:
                self.xext = self.ct.CDLL("libXext.so.6")
            except Exception:
                self.xext = None

    # ── atoms ────────────────────────────────────────────────────────────
    def _atom(self, name: str) -> int:
        return self.x11.XInternAtom(self.ct.c_void_p(self.dpy), name.encode(), False)

    def _send_root(self, xid: int, message: str, data: list[int]) -> None:
        ev = self._XEvent()
        cm = ev.xclient
        cm.type = self._CLIENT_MESSAGE
        cm.serial = 0
        cm.send_event = 1
        cm.display = self.dpy
        cm.window = xid
        cm.message_type = self._atom(message)
        cm.format = 32
        for i, v in enumerate(data[:5]):
            cm.data[i] = v
        self.x11.XSendEvent(self.ct.c_void_p(self.dpy), self.ct.c_ulong(self.root),
                            False, self._SUBSTRUCTURE, self.ct.byref(ev))
        self.x11.XFlush(self.ct.c_void_p(self.dpy))

    # ── public helpers ───────────────────────────────────────────────────
    def sync(self) -> None:
        """Wait until the server has actually processed what we have sent.

        XFlush pushes our buffer out and returns; it does not wait. That is
        enough to order our own requests against each other, because they travel
        one connection and the server takes them in order. It is not enough to
        order them against GTK's, and GTK is the other half of every interesting
        case here: this class talks to X over its own XOpenDisplay, while
        mapping, moving and resizing windows happens on GDK's. Two connections
        have no ordering guarantee between them at all — the server is free to
        take a request sent later on one before a request sent earlier on the
        other.

        A round trip closes that. Once this returns, our shape is on the server,
        so a map GTK sends afterwards cannot overtake it.
        """
        if not self.ok:
            return
        try:
            self.x11.XSync(self.ct.c_void_p(self.dpy), 0)
        except Exception:
            pass

    def float_above(self, xid: int) -> None:
        """Keep the popup over every other window, on every workspace, and out
        of the dock and window switcher."""
        if not self.ok:
            return
        try:
            for state in ("_NET_WM_STATE_ABOVE", "_NET_WM_STATE_STICKY",
                          "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER"):
                self._send_root(xid, "_NET_WM_STATE",
                                [self._NET_WM_STATE_ADD, self._atom(state), 0, 1, 0])
        except Exception:
            pass

    def keep_above(self, xid: int) -> None:
        """Above other windows, but otherwise an ordinary window.

        For our secondary windows: they have to clear the pill, which holds
        _NET_WM_STATE_ABOVE, but they should still appear in the dock and the
        window switcher and stay on the workspace they were opened on.
        """
        if not self.ok:
            return
        try:
            self._send_root(xid, "_NET_WM_STATE",
                            [self._NET_WM_STATE_ADD,
                             self._atom("_NET_WM_STATE_ABOVE"), 0, 1, 0])
        except Exception:
            pass

    # XA_ATOM, from Xatom.h. Hardcoded because it is a predefined atom and
    # interning it would be a round trip to learn a constant.
    _XA_ATOM = 4
    # ...and XA_CARDINAL beside it, for the same reason. set_frame_extents().
    _XA_CARDINAL = 6

    def set_frame_extents(self, xid: int, left: int, right: int,
                          top: int, bottom: int) -> None:
        """Say how much of this window is not the window, in device pixels.

        `_GTK_FRAME_EXTENTS` is what GNOME's own client-side-decorated windows
        use to declare their invisible margin. mutter subtracts it from the X
        window to get the **frame rect**, and the frame rect is what every
        window-manager-side measurement is taken against: edge snapping,
        tiling, keep-on-screen, and the `_NET_FRAME_EXTENTS` it publishes back.

        Halo needs it because the pill's window is permanently taller than the
        pill. The apron is 31 logical px of transparent window below the
        painted edge, always in the layout, carrying the detached down-arrow
        and the resize band; nothing in X said so, so mutter measured this
        window 31px too tall and placed it that much too low.

        ── measured ──
        Nested headless mutter 50.4, work area 1920x1080, scale 1, panel open
        (window 760x719, painted pill 688 rows), real injected pointer drag of
        the pill downward in 2px steps. Without the property the drag sticks
        with the *X window's* foot on the work area's, y=361, for 32px of
        pointer travel, leaving the painted pill 31px short of the bottom —
        identical on 1.24.0 and 1.26.0. With `[0, 0, 0, 31]` it sticks with the
        *painted* foot on the work area's, y=392, gap 0.

        It costs the apron nothing. Frame extents and the input region are
        different questions: the effective input region is
        ShapeInput n ShapeBounding and this property is in neither. Measured
        with real injected pointer events at five points around the bottom
        edge, with the property and without: the band 5 rows below the painted
        edge, the corner nook at x=6, and the corner grab 4 rows ABOVE the
        painted edge all delivered the same events, and a press in the nook
        still began a resize. gnome-text-editor on this machine is the same
        arrangement from the other end — `_GTK_FRAME_EXTENTS = [50, 50, 50,
        50]` with an input region inset only 26px, i.e. 24px of live input
        inside its own declared margin.

        Device pixels, like every other figure X holds about this window, so
        the apron's own device height goes in unscaled.
        """
        if not self.ok:
            return
        try:
            ct = self.ct
            vals = (ct.c_ulong * 4)(max(0, int(left)), max(0, int(right)),
                                    max(0, int(top)), max(0, int(bottom)))
            self.x11.XChangeProperty(
                ct.c_void_p(self.dpy), ct.c_ulong(xid),
                ct.c_ulong(self._atom("_GTK_FRAME_EXTENTS")),
                ct.c_ulong(self._XA_CARDINAL), 32, 0,
                ct.byref(vals), 4)
            self.x11.XFlush(ct.c_void_p(self.dpy))
        except Exception:
            pass

    def set_type_dock(self, xid: int) -> None:
        """Declare this window a dock, so nothing ever tries to focus it.

        The gentler _NET_WM_USER_TIME=0 route works — the window does not get the
        keyboard — but it works by making mutter *refuse* focus to a window that
        asked for it, and refusing is exactly what GNOME Shell announces with a
        "Halo is ready" notification. One per collapse is not a fix, it is a new
        bug wearing the old one's coat.

        A dock is never a focus candidate in the first place, so the question is
        never asked and there is nothing to announce. It is also above ordinary
        windows and out of the switcher by definition, which is the rest of what
        this window needs. Set before the window is mapped: after it, it is
        advice about a decision the window manager has already taken, and the
        X input focus goes to the disc the moment it appears — measured, and it
        kills every key the pill is listening for. ParkedArrow.show() realises
        the window by hand for exactly this reason.

        Nothing sets _NET_WM_USER_TIME any more; that route was dropped whole
        rather than kept alongside, so there is no second answer to this
        question anywhere in the file.
        """
        if not self.ok:
            return
        try:
            ct = self.ct
            value = ct.c_ulong(self._atom("_NET_WM_WINDOW_TYPE_DOCK"))
            self.x11.XChangeProperty(
                ct.c_void_p(self.dpy), ct.c_ulong(xid),
                ct.c_ulong(self._atom("_NET_WM_WINDOW_TYPE")),
                ct.c_ulong(self._XA_ATOM), 32, 0,
                ct.byref(value), 1)
            self.x11.XFlush(ct.c_void_p(self.dpy))
        except Exception:
            pass

    def activate(self, xid: int) -> None:
        """Ask the window manager for focus the EWMH-approved way."""
        if not self.ok:
            return
        try:
            self._send_root(xid, "_NET_ACTIVE_WINDOW", [1, 0, 0, 0, 0])
            self.x11.XRaiseWindow(self.ct.c_void_p(self.dpy), self.ct.c_ulong(xid))
            self.x11.XFlush(self.ct.c_void_p(self.dpy))
        except Exception:
            pass

    def move(self, xid: int, x: int, y: int) -> None:
        if not self.ok:
            return
        try:
            self.x11.XMoveWindow(self.ct.c_void_p(self.dpy), self.ct.c_ulong(xid),
                                 int(x), int(y))
            self.x11.XFlush(self.ct.c_void_p(self.dpy))
        except Exception:
            pass

    # CWX and nothing else. Not CWY: the top edge is not this request's
    # business and a corner drag's vertical half must not be fought. Not
    # CWWidth either, any more — see move_x().
    _CW_X = (1 << 0)

    def move_x(self, xid: int, x: int) -> None:
        """Set the left edge, and only the left edge.

        This used to carry CWWidth as well, on the theory that one
        ConfigureWindow carrying both cannot be seen half-applied. It cannot
        — and it is still seen half-applied, because the half the server
        applies at once is the geometry and the half that takes a frame is
        the buffer behind it. So the atomicity bought nothing, and the width
        in the mask cost something real: WM_NORMAL_HINTS says min = max = the
        current width while set_resizable(False) is in force, and a request
        whose width violates that can be dropped whole, x included. Measured
        on the setuptest check that guards this corner — "moved 0px for 100px
        of width", 6 runs in 10 on the untouched file.

        Dropping the width from the mask loses nothing, because it was never
        what resized the window. Measured: with the mask cut to CWX the drag
        widens exactly as it did, since GTK's own set_default_size() is what
        does the resizing on GTK's connection. What is left here is a pure
        move, which the window manager has no size hint to refuse it by.

        Two X connections still cannot be ordered against each other, and
        this still does not need them to be: _pin_right_edge() places the
        left edge for a width it has WATCHED arrive, not one it has asked
        for. See there.
        """
        if not self.ok:
            return
        try:
            ct = self.ct

            class XWindowChanges(ct.Structure):
                _fields_ = [("x", ct.c_int), ("y", ct.c_int),
                            ("width", ct.c_int), ("height", ct.c_int),
                            ("border_width", ct.c_int),
                            ("sibling", ct.c_ulong), ("stack_mode", ct.c_int)]

            chg = XWindowChanges()
            chg.x = int(x)
            self.x11.XConfigureWindow(ct.c_void_p(self.dpy), ct.c_ulong(xid),
                                      ct.c_uint(self._CW_X), ct.byref(chg))
            self.x11.XFlush(ct.c_void_p(self.dpy))
        except Exception:
            pass

    def get_position(self, xid: int) -> tuple[int, int] | None:
        """Where the window actually is, in root coordinates.

        GdkSurface exposes width/height/scale but no x/y, so this is the only
        way to learn our own position — which is what "remember position" needs.
        Asking the X server also copes with the window manager reparenting us.
        """
        if not self.ok:
            return None
        try:
            ct = self.ct
            x, y = ct.c_int(), ct.c_int()
            child = ct.c_ulong()
            ok = self.x11.XTranslateCoordinates(
                ct.c_void_p(self.dpy), ct.c_ulong(xid), ct.c_ulong(self.root),
                0, 0, ct.byref(x), ct.byref(y), ct.byref(child))
            return (x.value, y.value) if ok else None
        except Exception:
            return None

    def workarea(self) -> tuple[int, int, int, int] | None:
        """The desktop's usable area — the screen minus GNOME's top bar and docks.

        GdkMonitor.get_geometry() is the whole panel, top bar included, so sizing
        and placing against it lets the pill sit underneath the bar and lets the
        results run right off the bottom edge. _NET_WORKAREA is what the window
        manager itself reserves, and it is the number every other application
        uses to behave.
        """
        if not self.ok:
            return None
        try:
            ct = self.ct
            kind = ct.c_ulong(); fmt = ct.c_int()
            count = ct.c_ulong(); rest = ct.c_ulong()
            data = ct.POINTER(ct.c_ubyte)()
            self.x11.XGetWindowProperty(
                ct.c_void_p(self.dpy), ct.c_ulong(self.root),
                ct.c_ulong(self._atom("_NET_WORKAREA")), 0, 4, False, 0,
                ct.byref(kind), ct.byref(fmt), ct.byref(count),
                ct.byref(rest), ct.byref(data))
            if not data or count.value < 4:
                return None
            vals = ct.cast(data, ct.POINTER(ct.c_ulong))
            rect = (int(vals[0]), int(vals[1]), int(vals[2]), int(vals[3]))
            try:
                self.x11.XFree(data)
            except Exception:
                pass
            return rect if rect[2] > 0 and rect[3] > 0 else None
        except Exception:
            return None

    def pointer(self) -> tuple[int, int] | None:
        """Pointer position in device pixels, or None."""
        if not self.ok:
            return None
        try:
            ct = self.ct
            root_ret = ct.c_ulong()
            child = ct.c_ulong()
            rx, ry, wx, wy = (ct.c_int() for _ in range(4))
            mask = ct.c_uint()
            got = self.x11.XQueryPointer(
                ct.c_void_p(self.dpy), ct.c_ulong(self.root),
                ct.byref(root_ret), ct.byref(child),
                ct.byref(rx), ct.byref(ry), ct.byref(wx), ct.byref(wy), ct.byref(mask))
            return (rx.value, ry.value) if got else None
        except Exception:
            return None

    @staticmethod
    def _rounded_scanlines(w: int, h: int, radius: int
                           ) -> list[tuple[int, int, int, int]]:
        """A rounded rectangle as a list of (x, y, width, height) rectangles.

        X11 shapes are built from rectangles, so the curved corners become one
        one-pixel-tall row each. ~2·radius rows is nothing to the X server.

        This used to take a `top_rows` argument that stretched the top two
        corners, to keep a *bounding* clip inside a frame the compositor was
        stretching over a window that had already grown. Nothing sets a
        bounding shape any more — see set_rounded_shape() — so there is no clip
        to keep inside anything, and the stretched frame's corners are
        transparent in the stretched frame too. The black top corners that
        machinery existed to hide cannot happen, and it went with them.
        """
        radius = max(0, min(radius, w // 2, h // 2))
        rects: list[tuple[int, int, int, int]] = []
        if h - 2 * radius > 0:
            rects.append((0, radius, w, h - 2 * radius))
        for i in range(radius):
            dy = radius - i - 0.5
            inset = radius - math.sqrt(max(0.0, radius * radius - dy * dy))
            x = int(round(inset))
            width = w - 2 * x
            if width <= 0:
                continue
            rects.append((x, i, width, 1))                  # top corner row
        for i in range(radius):
            dy = radius - i - 0.5
            inset = radius - math.sqrt(max(0.0, radius * radius - dy * dy))
            x = int(round(inset))
            width = w - 2 * x
            if width <= 0:
                continue
            rects.append((x, h - 1 - i, width, 1))          # bottom corner row
        return rects

    def set_rounded_shape(self, xid: int, w: int, h: int, radius: int,
                          top: int = 0, disc: tuple | None = None,
                          grab: int = 0, corners: tuple | None = None) -> None:
        """Give the window a rounded-rectangle INPUT region, and nothing else.

        This sets ShapeInput only. It deliberately does **not** set the
        bounding shape, and that is the whole of what makes an invisible resize
        border outside the painted pill possible — see `grab` below.

        `top` cuts that many pixels off the top of the result. A `top` that
        covers the whole height leaves an empty region, and an empty input
        region is how X is told the window takes no pointer events at all.

        `disc` is (x, y, diameter) in device pixels and adds a circle, the
        detached ↓ that hangs below the pill in the pill's own window. Every
        span of it is clipped to y >= h, so no part of the circle can ever
        claim pointer events over the slab above it whatever the arithmetic
        upstream believes. The circle is *painted* by CSS (.halo-parked's
        border-radius) and clipped by the apron's Overflow.HIDDEN; all this
        adds is the input to go with it.

        `grab` is a full-width band of that many rows immediately BELOW the
        slab — the panel's resize border, standing outside the painted pill
        exactly the way every other GNOME window's does.

        ── why the corners are see-through without a bounding shape ──
        Because the window is a 32-bit ARGB toplevel and GTK4 paints alpha 0
        outside the pill. This contradicts what this docstring used to say, so
        here is the measurement, on this machine, at scale 2:

          - A stock GTK4 app under XWayland (gnome-text-editor, GDK_BACKEND=x11)
            is depth 32, XShapeQueryExtents reports bounding_shaped = FALSE,
            and it carries _GTK_FRAME_EXTENTS = [50, 50, 50, 50] with an input
            region inset only 26px — i.e. 24 device px of live input lying
            *outside* its visible pixels, on every side. That is GNOME's own
            invisible resize border, and it works because there is no bounding
            shape to intersect it away.
          - A minimal GTK4 window with a transparent background and an opaque
            child inset 20 logical px: XGetImage on its own depth-32 drawable
            reads (A,R,G,B) = (0,0,0,0) in the margin and (255,·,·,·) in the
            middle. The client really does commit alpha 0.
          - GTK4 hands the compositor _NET_WM_OPAQUE_REGION itself: [88,40,624,520]
            for that window (the margin and the corner arcs excluded), against
            [0,0,800,600] for an opaque one. mutter reads that property to
            decide what to blend.

        The old claim — "the surface always comes back with alpha 255" — came
        from snapshotting the widget tree, which has no alpha to report. It was
        measuring the wrong object.

        ── what stays true ──
        The effective input region is still ShapeInput ∩ ShapeBounding, and
        that is exactly why `grab` works only now. Measured, and unchanged:
        with bounding = the top 150 rows and input = the top 160,
        XShapeGetRectangles confirmed the server was holding the larger input
        region and pointer delivery still stopped dead at row 150 (row 148
        delivered, row 152 did not). So a band below a *bounded* window is
        impossible, and that is why the band lived inside the slab for several
        versions. Remove the bounding shape and the intersection has nothing
        left to remove: with no bounding shape, an input region 20 device px
        into a fully transparent margin delivered a GTK enter event (control:
        the same point with the margin cut out of the input region delivered
        none, and a point over the opaque middle delivered one).
        """
        if not self.ok or self.xext is None:
            return
        try:
            ct = self.ct

            class XRectangle(ct.Structure):
                _fields_ = [("x", ct.c_short), ("y", ct.c_short),
                            ("width", ct.c_ushort), ("height", ct.c_ushort)]

            spans = self._rounded_scanlines(int(w), int(h), int(radius))
            if not spans:
                return
            top = max(0, int(top))
            if top:
                spans = [(x, max(y, top), rw, rh - max(0, top - y))
                         for (x, y, rw, rh) in spans
                         if rh - max(0, top - y) > 0]
            if int(grab) > 0:
                # Full width, and below the slab rather than inside it, so the
                # page keeps every row it paints. The corners of the pill curve
                # away above this band; the band itself is a plain rectangle
                # because there is nothing painted down here for it to follow.
                spans.append((0, int(h), int(w), int(grab)))
            if corners:
                # The two bottom corners, reaching back UP over the last rows
                # of the slab. Those rows are already in the region where the
                # arc covers them — but the arc has curved as much as `radius`
                # inward by the bottom row, and the pointer standing AT the
                # visible corner is in the nook outside it, which fell through
                # to the desktop. These two rectangles are that nook, and the
                # reason the corner grips in the slab can be reached at all.
                cw, up = int(corners[0]), int(corners[1])
                up = max(0, min(up, int(h)))
                cw = max(0, min(cw, int(w) // 2))
                if cw > 0 and up > 0:
                    spans.append((0, int(h) - up, cw, up))
                    spans.append((int(w) - cw, int(h) - up, cw, up))
            if disc:
                dx, dy, side = int(disc[0]), int(disc[1]), int(disc[2])
                # A rounded rectangle whose radius is half its side is a circle,
                # so the same scanline walk draws it.
                for (cx, cy, cw, ch) in self._rounded_scanlines(
                        side, side, side // 2):
                    y = cy + dy
                    cut = max(0, int(h) - y)
                    if ch - cut > 0:
                        spans.append((cx + dx, y + cut, cw, ch - cut))
            def _pack(rects):
                out = (XRectangle * len(rects))()
                for i, (x, y, rw, rh) in enumerate(rects):
                    out[i] = XRectangle(x, y, max(1, rw), max(1, rh))
                return out

            SHAPE_INPUT, SHAPE_SET, UNSORTED = 2, 0, 0
            packed = _pack(spans)
            self.xext.XShapeCombineRectangles(
                ct.c_void_p(self.dpy), ct.c_ulong(xid), SHAPE_INPUT, 0, 0,
                packed, len(spans), SHAPE_SET, UNSORTED)
            self.x11.XFlush(ct.c_void_p(self.dpy))
        except Exception:
            pass


WM = X11WM()


# ══════════════════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════════════════

def accel_id(accel: str):
    """Reduce an accelerator to the (keyval, modifiers) pair it really means.

    One key has several spellings — "<Primary>g", "<Ctrl>g" and "<Control>g" are
    the same shortcut, and so are "<Shift><Super>F23" and "<Super><Shift>F23" —
    and every place that compares, deduplicates or looks one up has to agree
    about that. Returns None for anything GTK cannot parse, so a hand-edited
    config full of nonsense compares as "not a key" rather than raising.
    """
    try:
        ok, keyval, mods = Gtk.accelerator_parse(accel)
        return (keyval, int(mods)) if ok and keyval else None
    except Exception:
        return None


def accel_label(accel: str) -> str:
    """Human-readable form, e.g. "Super+G" — with the key's alias if it has one."""
    text = accel
    try:
        ok, keyval, mods = Gtk.accelerator_parse(accel)
        if ok and keyval:
            text = Gtk.accelerator_get_label(keyval, mods)
    except Exception:
        pass
    want = accel_id(accel)
    for spelling, alias in ACCEL_ALIASES.items():
        if want is not None and accel_id(spelling) == want and alias not in text:
            return f"{text} ({alias})"
    return text


class Config:
    def __init__(self) -> None:
        self.data = dict(DEFAULTS)
        try:
            if CONFIG_FILE.exists():
                self.data.update(json.loads(CONFIG_FILE.read_text()))
        except Exception:
            pass
        # Halo 1.0 stored a single "shortcut"; 1.1 allows several.
        legacy = self.data.pop("shortcut", None)
        if legacy and self.data.get("shortcuts") == DEFAULTS["shortcuts"]:
            self.data["shortcuts"] = [legacy]
        self._sanitise()

    def _sanitise(self) -> None:
        """Force every value we read into the shape the code expects.

        A config file can arrive from an older Halo, a newer one, or a text editor.
        Nothing here used to check: a string where a number belonged raised
        straight out of expand(), which is not a state a search popup should be
        able to get into. Keys we do not know are deliberately left untouched, so
        going back to an older version does not lose its settings.
        """
        def number(key: str, low, high, cast) -> None:
            try:
                value = cast(self.data.get(key, DEFAULTS[key]))
            except (TypeError, ValueError):
                value = DEFAULTS[key]
            self.data[key] = max(low, min(high, value))

        number("panel_height", 260, 4000, int)
        number("window_width", 420, 4000, int)
        number("vertical_anchor", 0.0, 0.9, float)
        # 0 means keep for ever; ten years is the ceiling because a retention
        # nobody will ever reach is the same thing said less clearly.
        number("history_days", 0, 3650, int)
        # A day is the ceiling: past that "release when idle" is indistinguishable
        # from never, and saying so plainly is better than pretending.
        number("idle_release_min", 0, 1440, int)
        # A second is already absurd for a drawer; past that it is a fault, not
        # a preference.
        number("reveal_ms", 0, 1000, int)
        for key in ("suggestions", "remember_position", "safe_search",
                    "compact_results", "warmed_up", "history",
                    "history_in_suggestions", "parked_arrow"):
            self.data[key] = bool(self.data.get(key, DEFAULTS[key]))

        # A remembered attachment names a file, and a file can be deleted
        # between two runs. Anything that is not a dict with a usable path is
        # dropped here rather than left for the window to trip over; whether
        # the file is still *there* is asked later, by _seed_stash(),
        # because this runs before there is a filesystem answer worth acting on.
        attached = self.data.get("attached_image")
        if isinstance(attached, dict) and isinstance(attached.get("path"), str) \
                and attached["path"]:
            uri = attached.get("uri")
            origin = attached.get("origin")
            self.data["attached_image"] = {
                "path": attached["path"],
                "name": str(attached.get("name") or "")[:200] or "image",
                "uri": uri if isinstance(uri, str) and uri else None,
                # Where the picture came from, when that was a file of the
                # user's own rather than one of Halo's scratch copies. This is
                # what an image in the history points at — see
                # SearchHistory and _remember_image_search().
                "origin": origin if isinstance(origin, str) and origin else None,
            }
        else:
            self.data["attached_image"] = None

        # A remembered page colour is painted before any page can correct it, so
        # a bad one is visible for as long as Halo is open and a light one would
        # flash the whole panel white. Accept only "#rrggbb", and only if it is
        # dark by the same luminance test _adopt_page_bg() applies before
        # adopting anything — a value from a hand-edited file, or from some
        # future Halo, gets the constant instead of the benefit of the doubt.
        remembered = self.data.get("page_bg")
        self.data["page_bg"] = None
        if isinstance(remembered, str) and re.fullmatch(r"#[0-9a-fA-F]{6}",
                                                        remembered):
            r = int(remembered[1:3], 16) / 255
            g = int(remembered[3:5], 16) / 255
            b = int(remembered[5:7], 16) / 255
            if r * 0.299 + g * 0.587 + b * 0.114 <= 0.35:
                self.data["page_bg"] = remembered.lower()

        position = self.data.get("position")
        if (isinstance(position, (list, tuple)) and len(position) == 2
                and all(isinstance(v, (int, float)) for v in position)):
            self.data["position"] = [int(position[0]), int(position[1])]
        else:
            self.data["position"] = None

        for key in ("shortcuts", "custom_shortcuts"):
            value = self.data.get(key)
            if (not isinstance(value, list)
                    or not all(isinstance(s, str) and s for s in value)):
                self.data[key] = list(DEFAULTS[key])
            else:
                # Deduplicate by what the key *is*, not how it is spelled:
                # "<Primary>g" and "<Control>g" are one shortcut, and two rows
                # for one key is a list that cannot be reasoned about.
                seen, unique = set(), []
                for accel in value:
                    ident = accel_id(accel)
                    mark = ident if ident else accel
                    if mark in seen:
                        continue
                    seen.add(mark)
                    unique.append(accel)
                self.data[key] = unique

    def __getitem__(self, key: str):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        try:
            _own_dir(CONFIG_DIR)
            CONFIG_FILE.write_text(json.dumps(self.data, indent=2))
            _own_file(CONFIG_FILE)
        except Exception:
            pass


CFG = Config()


# ══════════════════════════════════════════════════════════════════════════
#  Search history
# ══════════════════════════════════════════════════════════════════════════

def relative_time(when: float, now: float | None = None) -> str:
    """"3 min", "yesterday", "6 Mar" — whichever tells you the most in one glance.

    Deliberately not a timestamp: the question a history list answers is "was
    that this morning or last month", and a clock time makes the reader do the
    subtraction themselves.
    """
    if not when:
        return ""
    now = time.time() if now is None else now
    gap = now - when
    if gap < 0:
        return "just now"          # a clock that went backwards, not the future
    if gap < 60:
        return "just now"
    if gap < 3600:
        return f"{int(gap // 60)} min"
    if gap < 86400:
        return f"{int(gap // 3600)} h"
    days = int(gap // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days"
    try:
        stamp = time.localtime(when)
        # The year only earns its space once it is not this one.
        if stamp.tm_year == time.localtime(now).tm_year:
            return time.strftime("%-d %b", stamp)
        return time.strftime("%-d %b %Y", stamp)
    except Exception:
        return f"{days} days"


# Words that carry no weight in a search box. Typing "how to keep a window on
# top" and typing "keep window top" mean the same thing, so neither should be the
# reason a past search fails to match the other.
#
# A fixed list on purpose. Learning which words a particular person skips would
# mean keeping statistics and rebuilding them, and this has to run on every
# keystroke — a frozenset lookup does not.
STOPWORDS = frozenset("""
a about an and any are as at be been but by can do does for from get got has have
how i if in into is it its just like me my no not of on or so that the their them
then there these this to too up use using was what when where which who why will
with you your
""".split())

_WORD = re.compile(r"\w+", re.UNICODE)


def tokens_of(text: str) -> frozenset:
    r"""The words in `text` that are worth matching on.

    \w with the unicode flag rather than [a-z0-9]: "Bundesländer" and "naïve" are
    single words and splitting them on their own letters would be worse than not
    tokenising at all. Stopwords go, and so do bare single characters, which match
    far too much to be evidence of anything.
    """
    words = {w for w in _WORD.findall((text or "").casefold()) if len(w) > 1}
    stripped = words - STOPWORDS
    # ...unless that leaves nothing: a search for "the who" is all stopwords, and
    # matching everything would be worse than matching literally.
    return frozenset(stripped or words)


class SearchHistory:
    """Every query the pill has run, newest first, kept between sessions.

    One flat JSON list rather than a database: this is a few hundred short
    strings, and opening sqlite for that would cost more than parsing the whole
    file. Entries are deduplicated on key_of(), so searching the same thing
    twice moves it up the list instead of filling it.

    A picture searched with Lens is remembered the same way, and this is the
    whole of the rule that keeps it cheap: **an image entry stores a reference,
    never a copy.** Either the address the picture lives at, or the path of the
    user's own file it was read from — a hundred bytes, in the file that is
    already being written. Halo never accumulates a folder of thumbnails, and
    it never keeps a second copy of anybody's photographs. The cost of that is
    honest and visible: a reference can go stale, so a row whose file has been
    moved says so when it is picked rather than searching the wrong thing.

    Pictures with nothing durable behind them — pixels dragged straight out of
    a web page, a data: URI, a screen capture — are not recorded at all. The
    only thing that could be stored for those is a copy, and a row that cannot
    be run is worse than no row.

    Nothing here is allowed to raise. A history is a convenience, and a corrupt
    or unreadable file must never be the reason a search does not happen — every
    path degrades to an empty list.
    """

    MAX_ENTRIES = 600           # past this the oldest go, whatever retention says
    SAVE_DEBOUNCE_MS = 400      # a burst of searches writes the file once

    def __init__(self) -> None:
        self.items: list[dict] = []
        self._save_timer = 0
        # query -> its tokens. Tokenising 600 short strings on every keystroke
        # would be the only expensive part of matching, so it happens once each.
        self._tok_cache: dict[str, frozenset] = {}
        self._load()

    def tokens(self, query: str) -> frozenset:
        cached = self._tok_cache.get(query)
        if cached is None:
            cached = tokens_of(query)
            if len(self._tok_cache) > 4 * self.MAX_ENTRIES:
                self._tok_cache.clear()      # bounded, and cheap to refill
            self._tok_cache[query] = cached
        return cached

    @staticmethod
    def _covers(needle: frozenset, hay: frozenset) -> bool:
        """Is every word typed present in `hay`, whole or as the start of one?

        Prefix rather than equality, so a half-typed word still matches what it
        is on the way to becoming: "pyth decor" finds "python decorators".
        """
        for want in needle:
            if want in hay:
                continue
            if not any(word.startswith(want) for word in hay):
                return False
        return True

    # ── on disk ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            raw = json.loads(HISTORY_FILE.read_text())
        except Exception:
            raw = []
        items: list[dict] = []
        if isinstance(raw, list):
            seen = set()
            for entry in raw:
                clean = self._clean(entry)
                if clean is None:
                    continue
                key = self.key_of(clean)
                if key in seen:          # a hand-edited file may repeat itself
                    continue
                seen.add(key)
                items.append(clean)
        items.sort(key=lambda e: e["at"], reverse=True)
        self.items = items[:self.MAX_ENTRIES]
        # Retention is enforced on the way in as well as on the way out: a copy
        # opened once after the window has passed should forget, even if nothing
        # is ever searched again to trigger a write.
        if self.prune() or len(items) != len(self.items):
            self._write()

    @staticmethod
    def _clean(entry) -> dict | None:
        """Force one stored entry into the shape the rest of this assumes."""
        if not isinstance(entry, dict):
            return None
        query = entry.get("q")
        if not isinstance(query, str) or not query.strip():
            return None
        try:
            at = float(entry.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        try:
            hits = max(1, int(entry.get("n") or 1))
        except (TypeError, ValueError):
            hits = 1
        kind = entry.get("kind")
        kind = kind if kind in ("url", "image") else "search"
        clean = {"q": query.strip()[:400], "at": at, "n": hits, "kind": kind}
        if kind == "image":
            img = SearchHistory._clean_img(entry.get("img"))
            if img is None:
                return None     # nothing to point at; see the class docstring
            clean["img"] = img
        return clean

    @staticmethod
    def _clean_img(img) -> dict | None:
        """The reference half of an image entry, or None if it cannot be run.

        `uri` is a public address — the picture is fetched from it, which is
        what a right-clicked or dragged web image searches by, and it stays
        valid for as long as the page it lives on does. `path` is a file of the
        user's own. At least one has to be there.

        Both may be, and that is not redundancy: a picture dropped from a
        browser can have been saved locally as well, and the local copy is the
        faster and more private of the two to search again.
        """
        if not isinstance(img, dict):
            return None
        path = img.get("path")
        uri = img.get("uri")
        path = path if isinstance(path, str) and path.strip() else None
        uri = uri if isinstance(uri, str) and uri.strip() else None
        if not path and not uri:
            return None
        return {"name": str(img.get("name") or "")[:200] or "image",
                # The words the picture was searched with, kept apart from the
                # label rather than read back out of it. The label is the words
                # *or* the file's name, so inferring one from the other gets a
                # picture called "Hey" searched for "Hey" wrong — rare, but it
                # is a guess where a recorded fact costs a short string that is
                # empty in the common case.
                "words": str(img.get("words") or "")[:400],
                "path": path[:1000] if path else None,
                "uri": uri[:2000] if uri else None}

    @staticmethod
    def key_of(entry: dict) -> str:
        """What makes two entries the same entry.

        The query alone, for words and for addresses. For a picture, the
        picture *and* the words: two different photographs searched with no
        query at all would otherwise be one row that kept changing which
        picture it meant, and one photograph searched first bare and then with
        "wo ist das" is two searches, not one renamed.

        Prefixed so an image entry can never collide with a typed query that
        happens to read the same — searching for the word "cat" and searching a
        picture called cat.png are different rows.
        """
        if entry.get("kind") != "image":
            return (entry.get("q") or "").strip().casefold()
        img = entry.get("img") or {}
        # The words rather than the label, so a picture searched bare and one
        # searched for its own filename are not the same row.
        return "\x00img\x00" + (img.get("uri") or img.get("path") or "") \
            + "\x00" + (img.get("words") or "").strip().casefold()

    @staticmethod
    def _hay_text(entry: dict) -> str:
        """What a search over the history matches this entry against.

        The label, plus the picture's file name for an image entry. An image
        search is remembered as "the one of the mushroom", and the words typed
        alongside it — often none — may say nothing about which picture it was.
        """
        if entry.get("kind") != "image":
            return entry["q"]
        name = (entry.get("img") or {}).get("name") or ""
        return f"{entry['q']} {name}" if name and name != entry["q"] else entry["q"]

    def _write(self) -> None:
        """Write via a temporary file, so a crash mid-save cannot truncate it."""
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._save_timer = 0
        try:
            _own_dir(DATA_DIR)
            tmp = HISTORY_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.items))
            _own_file(tmp)          # before the rename, so it is never briefly loose
            tmp.replace(HISTORY_FILE)
        except Exception:
            pass

    def _save_soon(self) -> None:
        """Coalesce writes: typing three searches in a row is one file write."""
        if self._save_timer:
            return

        def flush() -> bool:
            self._save_timer = 0
            self._write()
            return False

        self._save_timer = GLib.timeout_add(self.SAVE_DEBOUNCE_MS, flush)

    def flush(self) -> None:
        if self._save_timer:
            self._write()

    # ── contents ────────────────────────────────────────────────────────
    def add(self, query: str, kind: str = "search",
            img: dict | None = None) -> None:
        if not CFG["history"]:
            return
        query = (query or "").strip()
        if not query:
            return
        fresh = {"q": query[:400], "at": time.time(), "n": 1, "kind": kind}
        if kind == "image":
            clean = self._clean_img(img)
            if clean is None:
                return          # nothing durable to point at; see the docstring
            fresh["img"] = clean
        key = self.key_of(fresh)
        # Same search again: keep its count and its place in the list, but take
        # the new spelling — the capitalisation someone just typed is the one
        # they will recognise.
        for i, entry in enumerate(self.items):
            if self.key_of(entry) == key:
                fresh["n"] = entry["n"] + 1
                # And the newer reference. A picture searched again from a file
                # that has since been moved should point at where it is now.
                del self.items[i]
                break
        self.items.insert(0, fresh)
        del self.items[self.MAX_ENTRIES:]
        self._save_soon()

    def remove(self, query: str) -> None:
        """Forget the typed query `query`, if it is in here.

        Matches on the key, so it takes the text or address entry and leaves an
        image entry that happens to be labelled the same alone. Forgetting one
        of those needs the entry itself — its picture is half of what it is.
        """
        self._drop_key((query or "").strip().casefold())

    def remove_entry(self, entry: dict) -> None:
        """Forget exactly this row, picture and all."""
        if isinstance(entry, dict):
            self._drop_key(self.key_of(entry))

    def _drop_key(self, key: str) -> None:
        before = len(self.items)
        self.items = [e for e in self.items if self.key_of(e) != key]
        if len(self.items) != before:
            self._save_soon()

    def clear(self) -> None:
        self.items = []
        self._write()
        try:
            HISTORY_FILE.unlink()
        except OSError:
            pass

    def prune(self, days: int | None = None) -> bool:
        """Drop anything past the retention window. True when something went."""
        days = int(CFG["history_days"]) if days is None else int(days)
        if days <= 0:
            return False
        cutoff = time.time() - days * 86400
        before = len(self.items)
        # An entry with no usable timestamp is kept: it cannot be shown to be
        # old, and silently deleting what we simply failed to read would be the
        # one behaviour a retention setting must not have.
        self.items = [e for e in self.items if not e["at"] or e["at"] >= cutoff]
        return len(self.items) != before

    # ── looking through it ──────────────────────────────────────────────
    def similar(self, needle: str, limit: int = 3) -> list[dict]:
        """Past searches close enough to be worth putting among the suggestions.

        Stricter than search(): this competes for room with Google's own
        suggestions, so it only answers when every word typed is accounted for,
        and it answers with very few. Word order is irrelevant — "revealer gtk4"
        finds "gtk4 revealer" — and stopwords are already gone from both sides.

        Ranked by how little the past search adds to what was typed, so the
        closest thing comes first, then by recency.
        """
        if not CFG["history_in_suggestions"] or limit <= 0:
            return []
        wanted = tokens_of(needle)
        if not wanted:
            return []
        hits = []
        for entry in self.items:
            if entry["kind"] == "image":
                # A suggestion is a phrase the field can be completed to, and
                # picking one types it. A picture is neither, and offering it
                # here would put a row in the list that Enter could not honour.
                continue
            hay = self.tokens(entry["q"])
            if not hay or not self._covers(wanted, hay):
                continue
            # How much the remembered search says beyond what was typed. Zero
            # means the same words in some order, which is as close as it gets.
            extra = len(hay - wanted)
            hits.append((extra, -entry["at"], entry))
            if len(hits) > 64:      # plenty to rank three out of; stop early
                break
        hits.sort(key=lambda row: row[:2])
        return [row[2] for row in hits[:limit]]

    def search(self, needle: str, limit: int = 200) -> list[dict]:
        """Entries matching `needle`, best first; the whole list when it is empty.

        Ranked rather than merely filtered, because a substring match over a
        long history puts the thing you meant in the middle. A query that starts
        with what was typed comes first, then one where a word does, then a plain
        substring — recency breaking every tie, so the ordering of an unfiltered
        list is exactly the ordering of a filtered one with the misses removed.
        """
        needle = (needle or "").strip().casefold()
        if not needle:
            return self.items[:limit]
        wanted = tokens_of(needle)
        scored = []
        for entry in self.items:
            text = self._hay_text(entry)
            hay = text.casefold()
            at = hay.find(needle)
            if at >= 0:
                # A literal hit, ranked by where it lands: at the front, at the
                # start of a word, or buried mid-word.
                if at == 0:
                    rank = 0
                elif hay[at - 1].isspace() or not hay[at - 1].isalnum():
                    rank = 1
                else:
                    rank = 2
            elif wanted and self._covers(wanted, self.tokens(text)):
                # No literal hit, but every word typed is in there somewhere —
                # "revealer gtk4" finding "gtk4 revealer natural size". Ranked
                # below the literal matches, because those are what someone
                # typing a phrase in order is looking at.
                rank = 3
            else:
                continue
            scored.append((rank, -entry["at"], entry))
        scored.sort(key=lambda row: row[:2])
        return [row[2] for row in scored[:limit]]


HISTORY = SearchHistory()


# ══════════════════════════════════════════════════════════════════════════
#  Appearance
# ══════════════════════════════════════════════════════════════════════════

# No painted margin around the slab, and that is now a choice rather than a
# limit. The window IS per-pixel transparent — depth 32, alpha 0 outside the
# pill, no bounding shape (X11WM.set_rounded_shape carries the measurement) —
# so a soft shadow would render correctly. It is left off because an
# always-on-top pill with a drop shadow reads as a second window sitting on the
# desktop; the rim and the inner highlight do that job. What the window does
# have outside the pill is the apron: 31 logical px of transparent window whose
# first ten rows are the panel's resize border.
CORNER_RADIUS = 22          # keep in step with .halo-glow's border-radius
BAR_HEIGHT = 60             # nominal pill height, reserved when capping the panel
                            # (the real size is measured — see _window_size_device)
SUBTITLE_CHARS = 34         # how wide a settings-row subtitle may get before it
                            # wraps. Without a cap the widest one decides the
                            # whole menu's width — see _build_menu().
MARK_PX = 20                # the Google mark's drawn size. Anything up to the
                            # entry's 34px min-height leaves the pill at
                            # BAR_HEIGHT; past that the pill grows to suit.

# The colour Google's own dark results page paints itself. Everything behind the
# page — the results toolbar, the loading cover, the WebView's own base, and
# therefore the scroll gutter — is painted in exactly this, so the panel reads as
# one continuous surface and none of the pill's sheen wraps around the results.
# It is only a starting value: _adopt_page_bg() samples the real page after every
# load and follows it, so a Google restyle cannot leave a mismatched band behind.
PAGE_BG = "#1f1f1f"


def remembered_page_bg() -> str:
    """What to paint behind the page before the page has been asked.

    The colour adopted on the last run, when there is one, and the constant
    otherwise. CFG has already thrown out anything that is not a dark
    "#rrggbb", so this is always safe to hand straight to CSS.
    """
    return CFG["page_bg"] or PAGE_BG

# $PAGE_BG$ is substituted with PAGE_BG in load_css(). A plain %-format would
# fight every "0%" stop in the gradients below.
CSS = """
@define-color halo_bg rgba(18,19,24,0.92);
@define-color halo_page $PAGE_BG$;

/* Every node that could paint a toplevel background, neutralised — and this is
   load-bearing, not tidiness. The toplevel is a 32-bit ARGB window with no
   bounding shape, so whatever these nodes do NOT paint is alpha 0 and the
   desktop shows through it: the rounded corners, and every row of apron below
   the pill. Put an opaque background back on any one of them and the corners
   become square and the resize border becomes a visible slab.
   .halo-glow rounds the pill itself; X is told only which of these pixels take
   the pointer — see X11WM.set_rounded_shape. */
window.halo-window,
window.halo-window.background,
window.halo-window.csd,
window.halo-window.solid-csd,
window.halo-window > decoration {
  background-color: transparent;
  background-image: none;
  box-shadow: none;
  border: none;
}

/* The 1.5px animated ring. Its background IS the border; the body sits on top.
   No drop shadow: it could only be drawn onto an opaque black margin, which is
   exactly the grey box we are getting rid of. Depth comes from the rim and an
   inner highlight instead. */
/* 90deg, not the 115 it used to be, and that is a bug fix rather than a taste.
   A tilted gradient's colour at any point depends on the *length of the
   gradient line*, which depends on the box's height as well as its width — so
   every keystroke that changed the suggestion list re-tinted the whole rim,
   including the top edge of the pill, which had not moved. Measured: opening a
   list took the window from 91px to 462 and shifted the colours along the top
   edge by 59 steps; a shorter list shifted them 33 more. That is the "weird
   looking issues at the top search bar" while typing.

   A horizontal gradient's line is exactly the width, so the rim is now the same
   at every height and a growing panel cannot touch it. On a slab twelve times
   wider than it is tall the tilt was nearly horizontal anyway. */
.halo-glow {
  border-radius: 22px;
  padding: 1.5px;
  background-image: linear-gradient(90deg,
      rgba(66,133,244,0.95)  0%,
      rgba(219,68,55,0.80)  33%,
      rgba(244,180,0,0.80)  66%,
      rgba(15,157,88,0.95) 100%);
  animation-name: halo-sweep;
  animation-duration: 9s;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
}

/* Held still whenever the pill is not the active window — see _set_rim_running().
   This is not a cosmetic choice. An animation that never ends means the surface
   asks for a frame forever, and a compositor stops answering for a window that is
   not in front: measured, GDK then waits out a one-second timeout on every frame
   cycle, and since every window in the process shares one main loop, the manual
   opened on top of the pill became unusable — 981ms of blocking per cycle against
   0.04ms with the sweep stopped. Freezing the rim while nothing is focused on it
   also stops an always-on-top overlay redrawing a gradient around the clock. */
.halo-glow.halo-still {
  /* The unfocused rim. $DRAINED$ is substituted in load_css() with the same
     gradient .halo-glow starts from, drained of colour — see _drained_still().

     It has to be a plain declaration on this class, and it only draws because
     _apply_rim_phase() writes `animation-name: none` in the same breath as
     this class goes on. An animation outranks every normal declaration in the
     cascade whatever its play state, so while one is named here nothing
     written in this block reaches the screen at all.

     What used to be here on its own was `animation-play-state: paused`, which
     stopped the sweep and changed nothing about how the rim looked. Stopping
     is not an indicator: a nine-second sweep is indistinguishable from a still
     one over the second anybody spends deciding whether Halo has the keyboard.
     That is the whole of "I would like some kind of noticeable indicator for
     when Halo is unfocused".

     The pause stays, and that is not belt and braces. Adding this class is how
     anything that wants the rim held still says so — halo-setuptest.py freezes
     it exactly that way, so that two renders of a pill nobody has touched can
     be compared — and taking the pause out made the class stop meaning that.
     Measured: four render-comparison checks went red the moment it went,
     because the sweep carried on underneath them. With no animation named the
     pause does nothing, so it costs one line and keeps a promise.

     Killing the animation rather than pausing it used to be wrong, because it
     restarted the sweep from 0% and the rim snapped back to its launch
     colours. It is not wrong any more: the phase is carried in _rim_phase and
     put back with a negative animation-delay, which is machinery this file
     already has and which the pause was relying on anyway. And
     `animation-name: none` asks for no frames at all — strictly more than a
     paused animation promises, and this class exists to stop the frames. */
  animation-play-state: paused;
  background-image: $DRAINED$;
}

/* The rim deliberately has no focus variant.
   There used to be a :focus-within rule setting a brighter background-image, but
   a running animation outranks any normal declaration in the cascade and this one
   runs forever, so it never drew — it was dead the whole time. Naming a second
   set of keyframes on focus does draw, but changing animation-name restarts an
   animation from 0%: measured, the sweep snapped back to its starting colour on
   every focus gain *and* loss, which is far more noticeable than the effect was
   ever worth. One animation, never interrupted, is the point of this rim. */

/* Two names for one sweep, alternated on every resume — see
   _set_rim_running(). Changing animation-delay on its own does not restart a
   GTK CSS animation, so the delay that carries the phase across a pause is
   simply ignored; changing animation-name does restart it, and the negative
   delay then places it exactly where it was frozen. The two blocks must stay
   identical, which is why they are generated from one string in load_css(). */
@keyframes halo-sweep {
  0% { background-image: linear-gradient(90deg,
        rgba(66,133,244,0.90) 0%, rgba(219,68,55,0.75) 33%,
        rgba(244,180,0,0.75) 66%, rgba(15,157,88,0.90) 100%); }
  25% { background-image: linear-gradient(90deg,
        rgba(15,157,88,0.90) 0%, rgba(66,133,244,0.90) 33%,
        rgba(219,68,55,0.75) 66%, rgba(244,180,0,0.75) 100%); }
  50% { background-image: linear-gradient(90deg,
        rgba(244,180,0,0.75) 0%, rgba(15,157,88,0.90) 33%,
        rgba(66,133,244,0.90) 66%, rgba(219,68,55,0.75) 100%); }
  75% { background-image: linear-gradient(90deg,
        rgba(219,68,55,0.75) 0%, rgba(244,180,0,0.75) 33%,
        rgba(15,157,88,0.90) 66%, rgba(66,133,244,0.90) 100%); }
  100% { background-image: linear-gradient(90deg,
        rgba(66,133,244,0.90) 0%, rgba(219,68,55,0.75) 33%,
        rgba(244,180,0,0.75) 66%, rgba(15,157,88,0.90) 100%); }
}

/* Frosted slab. GNOME cannot blur what is behind a window, so depth comes
   from a translucent base plus a soft top-left sheen. */
.halo-body {
  border-radius: 21px;
  background-color: @halo_bg;
  background-image: linear-gradient(158deg,
      rgba(255,255,255,0.075) 0%,
      rgba(255,255,255,0.024) 38%,
      rgba(255,255,255,0.000) 68%);
  /* Inset highlight along the top edge reads as thickness now that there is
     no drop shadow to separate the slab from the desktop. */
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.07),
              inset 0 -1px 0 rgba(0,0,0,0.35);
}

.halo-bar { padding: 10px 10px 10px 16px; }

.halo-entry, .halo-entry > text {
  background: none;
  background-image: none;
  border: none;
  box-shadow: none;
  outline: none;
  min-height: 34px;
  color: #f3f4f7;
  caret-color: #8ab4f8;
  font-size: 16.5px;
  font-weight: 400;
}
.halo-entry > text > placeholder { color: rgba(255,255,255,0.38); }
.halo-entry > text > selection { background-color: rgba(66,133,244,0.40); }

/* Find mode. The field IS the find box — see toggle_find() — so besides the
   placeholder and the count in the toolbar, this tint is the only thing saying
   so. Google's own yellow, already present in the rim, and applied to the text
   rather than the background so nothing moves or grows. */
.halo-entry.halo-finding, .halo-entry.halo-finding > text {
  color: #f4d35e;
}
.halo-entry.halo-finding > text > placeholder {
  color: rgba(244,211,94,0.45);
}
.halo-entry.halo-finding > text > selection {
  background-color: rgba(244,180,0,0.38);
}

.halo-icon { color: rgba(255,255,255,0.55); }

/* A MenuButton wraps its own <button> node, so both must be neutralised or
   it keeps libadwaita's raised pill. */
button.halo-chip,
menubutton.halo-chip > button {
  min-width: 30px;
  min-height: 30px;
  padding: 0;
  margin: 0 1px;
  border-radius: 15px;
  border: none;
  background: none;
  background-image: none;
  box-shadow: none;
  text-shadow: none;
  color: rgba(255,255,255,0.55);
  transition: background-color 140ms ease, color 140ms ease;
}
button.halo-chip:hover,
menubutton.halo-chip > button:hover {
  background-color: rgba(255,255,255,0.10);
  color: #ffffff;
}
button.halo-chip:active,
menubutton.halo-chip > button:active,
menubutton.halo-chip > button:checked {
  background-color: rgba(255,255,255,0.16);
  color: #ffffff;
}
button.halo-chip:disabled,
menubutton.halo-chip > button:disabled {
  color: rgba(255,255,255,0.18);
  background: none;
}
/* The copy chip's acknowledgement. Green rather than the plain white hover, so
   a glance tells you the click landed without having to check the clipboard. */
button.halo-chip.halo-copied,
button.halo-chip.halo-copied:hover {
  color: #57c98a;
  background-color: rgba(87,201,138,0.16);
}
button.halo-close:hover {
  background-color: rgba(232,80,72,0.85);
  color: #ffffff;
}
menubutton.halo-chip { margin: 0; }

/* ── the picture clipped to the search field ──────────────────────────────
   A square, not a circle like the chips either side of it: a chip is an icon
   and this is a photograph, and a round crop of a photograph reads as an
   avatar. Google's own attachment thumbnail is a rounded square, and so is
   this one.

   The rounding is CSS on the button and the picture is clipped to it by
   set_overflow(HIDDEN) in code — not baked into the texture, because the
   texture is also what the hover state dims, and a corner that was already
   transparent would dim to a grey notch instead of to nothing. */
button.halo-thumb {
  min-width: 26px;
  min-height: 26px;
  padding: 0;
  margin: 0 2px 0 1px;
  border-radius: 8px;
  border: none;
  background: none;
  background-image: none;
  box-shadow: none;
  transition: none;
}
/* No :hover background of its own. The scrim below is the hover state, and a
   button background behind an opaque photograph would never be seen anyway. */

/* The picture on a history row, standing in for the clock face an ordinary
   search gets. Rounded a little less than the chip on the bar, because it is a
   little smaller — and given a size here as well as in code, so a row cannot
   change height on the frame before the texture arrives. */
.halo-hist-thumb {
  border-radius: 5px;
  min-width: 22px;
  min-height: 22px;
}

/* The two overlay children, both always present and both invisible until the
   pointer is on the chip. Revealed with opacity rather than by being shown,
   so the chip cannot change size when the pointer crosses it — a thumbnail
   that grew a pixel on hover would nudge the whole search field sideways. */
.halo-thumb-scrim {
  background-color: rgba(0,0,0,0);
  border-radius: 8px;
  transition: background-color 120ms ease;
}
.halo-thumb-x {
  color: #ffffff;
  opacity: 0;
  transition: opacity 120ms ease;
}
button.halo-thumb:hover .halo-thumb-scrim {
  background-color: rgba(0,0,0,0.55);
}
button.halo-thumb:hover .halo-thumb-x {
  opacity: 1;
}

.halo-sep {
  background-color: rgba(255,255,255,0.075);
  min-height: 1px;
}

/* Suggestions */
.halo-suggest { background: none; }
.halo-suggest > row {
  padding: 8px 16px;
  border-radius: 12px;
  margin: 1px 8px;
  background: none;
  color: rgba(255,255,255,0.80);
  transition: background-color 120ms ease;
}
/* Google blue rather than plain white: it ties the list to the animated rim
   and reads as a deliberate accent instead of a generic highlight. */
.halo-suggest > row:hover {
  background-color: rgba(255,255,255,0.07);
  color: #ffffff;
}
.halo-suggest > row:selected {
  background-color: rgba(66,133,244,0.22);
  color: #ffffff;
  box-shadow: inset 2px 0 0 rgba(96,160,255,0.85);
}
.halo-suggest > row:selected image { color: rgba(138,180,248,0.95); }
.halo-suggest-hint { color: rgba(255,255,255,0.32); font-size: 11px; }

/* The Google mark doubles as the history button, so it needs a hover state and a
   held-open one — but deliberately the SAME 30px circle as .halo-chip, because
   anything else shows.
   min-width/min-height in GTK CSS size the CONTENT box, so the padding this
   started with was added on top: measured, 28px plus "2px 3px" gave a 34x32
   border box, which a 14px radius draws as a rounded rectangle rather than a
   circle — and the hit area then reached 2px further out on each side than the
   shape the eye was aiming at. No padding, matched radius, square again. */
button.halo-mark {
  min-width: 30px;
  min-height: 30px;
  padding: 0;
  margin: 0;
  border: none;
  border-radius: 15px;
  background: none;
  background-image: none;
  box-shadow: none;
  transition: background-color 140ms ease;
}
button.halo-mark:hover { background-color: rgba(255,255,255,0.10); }
button.halo-mark:active,
button.halo-mark.halo-mark-on { background-color: rgba(255,255,255,0.16); }

/* Search history. The rows ARE the suggestion rows — same list, same blue
   accent, so moving between the two lists does not feel like moving between two
   different widgets — with a relative time and a forget button riding on the
   right. Only the padding differs, and this comes after .halo-suggest > row so
   it wins at equal specificity. */
.halo-history > row { padding: 5px 8px 5px 16px; }
.halo-hist-head { padding: 8px 8px 0 16px; }
.halo-hist-when {
  color: rgba(255,255,255,0.30);
  font-size: 11px;
  padding: 0 4px;
}
.halo-history > row:selected .halo-hist-when { color: rgba(255,255,255,0.55); }
button.halo-hist-clear {
  min-height: 20px;
  padding: 1px 9px;
  border: none;
  border-radius: 10px;
  background-color: rgba(255,255,255,0.06);
  background-image: none;
  box-shadow: none;
  color: rgba(255,255,255,0.50);
  font-size: 11px;
}
button.halo-hist-clear:hover {
  background-color: rgba(255,255,255,0.14);
  color: #ffffff;
}
/* Armed, not libadwaita's .destructive-action: the rules above are more
   specific than a bare class, so that one would not have shown at all. */
button.halo-hist-clear.halo-armed,
button.halo-hist-clear.halo-armed:hover {
  background-color: rgba(232,80,72,0.85);
  color: #ffffff;
}
button.halo-hist-del {
  min-width: 22px;
  min-height: 22px;
  padding: 0;
  margin: 0;
  border: none;
  border-radius: 11px;
  background: none;
  background-image: none;
  box-shadow: none;
  /* Nearly invisible until the row is under the pointer: a column of crosses
     down the side of the list reads as the point of the list. */
  color: rgba(255,255,255,0.16);
  transition: color 120ms ease, background-color 120ms ease;
}
.halo-history > row:hover button.halo-hist-del,
.halo-history > row:selected button.halo-hist-del {
  color: rgba(255,255,255,0.50);
}
button.halo-hist-del:hover {
  background-color: rgba(232,80,72,0.85);
  color: #ffffff;
}

/* Results chrome.

   The whole panel is a flat sheet in Google's own page colour — deliberately
   NOT the pill's translucent gradient. The WebView's base is the same colour,
   so the transparent scroll gutter shows this and nothing else: previously the
   pill's 158° sheen shone through the gutter, brightest at the top, and read as
   a stray light line running down the side of the results. */
.halo-results { background-color: @halo_page; }
.halo-toolbar {
  /* 3px, not 5: the chips are 30px tall and carry their own optical padding, so
     the outer two pixels on each side only ever added to the gap between this
     row and the page's own tab strip. Keep PANEL_CHROME in step. */
  padding: 3px 10px;
  background-color: @halo_page;
  /* Lifts the toolbar off the results below it, so the panel reads as two
     layers rather than one flat sheet. */
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.045),
              0 1px 0 rgba(0,0,0,0.30);
}
.halo-url {
  color: rgba(255,255,255,0.40);
  font-size: 11.5px;
}
.halo-web { border-radius: 0 0 21px 21px; }

/* Covers the page while a search is in flight. Painted in the page's colour, not
   the slab's, so uncovering is a spinner disappearing rather than a sheet of a
   different shade being pulled away. */
.halo-loading { background-color: @halo_page; }
.halo-loading label { color: rgba(255,255,255,0.50); font-size: 13px; }

.halo-popover > contents {
  background-color: rgba(28,29,35,0.98);
  border-radius: 16px;
  border: 1px solid rgba(255,255,255,0.08);
  padding: 6px;
}
.halo-popover label { color: rgba(255,255,255,0.88); }
.halo-dim { color: rgba(255,255,255,0.42); font-size: 11.5px; }
.halo-title { font-weight: 700; font-size: 12px; letter-spacing: 0.6px;
              color: rgba(255,255,255,0.40); }
.halo-warn { color: #f0b429; font-size: 11px; }
.halo-ok { color: #57c98a; font-size: 11.5px; }
.halo-key { font-family: monospace; font-size: 11.5px;
            color: rgba(255,255,255,0.92); }
.halo-path { font-family: monospace; font-size: 10.5px;
             color: rgba(120,170,255,0.75); }
/* A shortcut the user typed in, rather than one of the offered presets. It is
   tickable exactly like a preset, so the difference has to be visible somewhere
   or the delete buttons look arbitrary: the tag says which rows are the user's
   own, and it is the rows with a tag that can be removed. */
/* The detached ↓ button. The circle is CSS: a border-radius of half the box's
   own side, clipped by the apron's Overflow.HIDDEN, on a window that paints
   alpha 0 around it. X is told about the circle only so that it takes the
   pointer — the shape has not cut the paint since 1.21.0.
   No animation here on purpose: an always-on-top
   window with a running animation asks for a frame for ever, and .halo-glow's
   comment records what that costs when the compositor stops answering. */
/* The same material as the pill's body and the same ink as the chip it stands
   in for, so it reads as a piece of Halo that has come loose rather than as a
   button someone has left on the desktop. It had a bright ring and a brighter
   glyph, and the result demanded attention it has no right to.
   "No background at all" IS available — the window is per-pixel transparent —
   but a bare glyph floating under the pill has nothing to click and nothing to
   read as a control. So: as little background as will still be a button, which
   is why this is 24px around a 16px glyph rather than 30 around the same. */
.halo-parked {
  background-color: @halo_bg;
  border-radius: 12px;
}
button.halo-parked-btn {
  min-width: 24px;
  min-height: 24px;
  padding: 0;
  margin: 0;
  border-radius: 12px;
  border: none;
  background: none;
  background-image: none;
  box-shadow: none;
  /* Exactly .halo-chip's colour: this is the collapse chip, standing outside. */
  color: rgba(255,255,255,0.55);
  transition: background-color 140ms ease, color 140ms ease;
}
/* The page behind this one has been unloaded, so ↓ means a fetch rather than a
   reveal. Said with ink alone, and only just: the disc's whole virtue is that it
   does not ask for attention, so the difference has to be findable rather than
   noticeable. Hover and press are deliberately identical to the warm state —
   once the pointer is on it, what it looks like is no longer the message. */
button.halo-parked-btn.halo-parked-cold {
  color: rgba(255,255,255,0.34);
}
button.halo-parked-btn:hover,
button.halo-parked-btn.halo-parked-cold:hover {
  background-color: rgba(255,255,255,0.10);
  color: #ffffff;
}
button.halo-parked-btn:active,
button.halo-parked-btn.halo-parked-cold:active {
  background-color: rgba(255,255,255,0.16);
  color: #ffffff;
}
/* The panel's grab band. It hangs in the apron BELOW the pill's painted edge,
   on transparent window, which is why it has no colour of its own to give it —
   painting it at all would put a slab of background outside the pill where the
   desktop is meant to show. It is also why the page keeps every row it paints:
   the band is not over the page any more, it is past it. The cursor is its only
   appearance. */
.halo-grip { background-color: transparent; }

.halo-tag { font-size: 9.5px; font-weight: bold; letter-spacing: 0.4px;
            color: rgba(120,170,255,0.9);
            border: 1px solid rgba(120,170,255,0.35);
            border-radius: 5px; padding: 0 4px; }
.halo-drop { min-width: 22px; min-height: 22px; padding: 0; }
"""

# The Google mark, embedded so there is nothing to install.
#
# Google's 2025 refresh replaced the four flat wedges with one continuous
# gradient flowing red → yellow → green → blue around the ring. SVG has no
# conic gradient, so each arc gets its own linear gradient and neighbouring
# arcs meet on a shared seam colour. Making that meeting actually invisible is
# the fiddly part, and the comment in <defs> below says how.
_G_RY = "#F4A62A"   # red    ↔ yellow
_G_YG = "#9FBA3C"   # yellow ↔ green
_G_GB = "#2C9BC6"   # green  ↔ blue
ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48">
  <defs>
    <!-- Each vector stops where its arc's seam *starts*, not at the seam's
         midpoint. A seam is a radial line, so a linear gradient's iso-colour
         bands cross it at an angle: endpoints on the midpoint made the shared
         colour land on one point of the seam and drift either side of it, up
         to 57/255 of a channel apart at the inner edge of the ring — three
         visible diagonal creases, worst on the yellow arc. Ending each ramp at
         the seam's nearest point instead puts the whole seam inside the
         gradient's clamped (pad) region, so both arcs paint the identical flat
         colour along all of it and the joins really do vanish. Measured after:
         every seam matches to 0/255, and the largest colour step anywhere
         inside the mark is 6/255, which is just antialiasing. -->
    <linearGradient id="gBlue" gradientUnits="userSpaceOnUse"
                    x1="34" y1="20.5" x2="34.76" y2="34.93">
      <stop offset="0" stop-color="#4285F4"/>
      <stop offset="0.5" stop-color="#4189EE"/>
      <stop offset="1" stop-color="%(gb)s"/>
    </linearGradient>
    <linearGradient id="gGreen" gradientUnits="userSpaceOnUse"
                    x1="31.09" y1="36.73" x2="10.74" y2="31.57">
      <stop offset="0" stop-color="%(gb)s"/>
      <stop offset="0.5" stop-color="#34A853"/>
      <stop offset="1" stop-color="%(yg)s"/>
    </linearGradient>
    <linearGradient id="gYellow" gradientUnits="userSpaceOnUse"
                    x1="8.1" y1="28.2" x2="8.1" y2="19.8">
      <stop offset="0" stop-color="%(yg)s"/>
      <stop offset="0.5" stop-color="#FBBC05"/>
      <stop offset="1" stop-color="%(ry)s"/>
    </linearGradient>
    <linearGradient id="gRed" gradientUnits="userSpaceOnUse"
                    x1="10.87" y1="16.49" x2="35.6" y2="11">
      <stop offset="0" stop-color="%(ry)s"/>
      <stop offset="0.55" stop-color="#EE5038"/>
      <stop offset="1" stop-color="#EA4335"/>
    </linearGradient>
  </defs>
  <!-- The yellow arc's outer edge is a true circular arc of the same radius as
       the corners it shares with red and green (21.645 about 24,24). It used to
       bend in to x=3, a full unit inside the rest of the ring, which flattened
       the mark's left side and put a kink in the outline at both corners; the
       ring now measures a constant 22.1 (stroke included) right across the
       yellow sector instead of dipping to 21.45 at nine o'clock.

       Each arc is also stroked in its own gradient. Four separately filled
       paths leave hairline gaps where their antialiased edges meet; a thin
       stroke fattens every segment by a fraction of a pixel so neighbours
       overlap. The seam colours already match, so the overlap is invisible
       while the lines through the logo disappear. -->
  <g stroke-width="0.9" stroke-linejoin="round" stroke-linecap="round">
    <path fill="url(#gBlue)" stroke="url(#gBlue)" d="M45.1 24.5c0-1.6-.1-2.8-.5-4H24v7.6h11.9c-.2 2-1.5 5-4.4 7l6.7 5.2c4-3.7 6.9-9.1 6.9-15.8z"/>
    <path fill="url(#gGreen)" stroke="url(#gGreen)" d="M24 46c6 0 11-2 14.2-5.7l-6.7-5.2c-1.8 1.3-4.3 2.2-7.5 2.2-5.8 0-10.7-3.8-12.4-9.1l-7 5.4C8 41.2 15.4 46 24 46z"/>
    <path fill="url(#gYellow)" stroke="url(#gYellow)" d="M11.6 28.2c-.5-1.3-.7-2.7-.7-4.2s.3-2.9.7-4.2l-7-5.4C3.12 17.38 2.35 20.67 2.35 24s.77 6.62 2.25 9.6l7-5.4z"/>
    <path fill="url(#gRed)" stroke="url(#gRed)" d="M24 10.7c4.1 0 6.9 1.8 8.5 3.3l6.2-6C34.9 4.5 30 2 24 2 15.4 2 8 6.8 4.6 14.4l7 5.4C13.3 14.5 18.2 10.7 24 10.7z"/>
  </g>
</svg>
""" % {"ry": _G_RY, "yg": _G_YG, "gb": _G_GB}


_RGBA_RE = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
                      r"\s*([0-9.]+)\s*\)")


def _drain(text: str, alpha: float = 0.42, grey: float = 0.72) -> str:
    """Every rgba() in `text`, mixed towards its own grey and made fainter.

    Each colour keeps its position and its relation to the others: this is the
    same rim, quietened, not a different one.
    """
    def one(m: "re.Match") -> str:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        a = float(m.group(4))
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        mix = lambda c: max(0, min(255, int(round(c + (lum - c) * grey))))
        return "rgba(%d,%d,%d,%.3f)" % (mix(r), mix(g), mix(b), a * alpha)

    return _RGBA_RE.sub(one, text)


def _drained_still(css: str) -> str:
    """What .halo-still paints: .halo-glow's own gradient, drained.

    Read out of the sheet rather than written a second time beside it, so there
    is one palette and the unfocused rim cannot end up describing a rim that no
    longer exists.
    """
    at = css.find(".halo-glow {")
    if at < 0:
        return "none"
    key = css.find("background-image:", at)
    end = css.find(";", key)
    if key < 0 or end < 0:
        return "none"
    return _drain(" ".join(css[key + len("background-image:"):end].split()))


# The sweep's own keyframe stops and the drained gradient .halo-still paints,
# both read out of the stylesheet by load_css() rather than written a second
# time. The rim's fades interpolate between them, so there is still exactly one
# palette and the unfocused rim still cannot describe a rim that no longer
# exists — see _drained_still().
RIM_STOPS: "list[tuple[float, str]]" = []
RIM_DRAINED = "none"


def _rim_keyframe_stops(css: str, name: str = "halo-sweep") -> "list":
    """`@keyframes name`, as (offset 0..1, gradient) pairs.

    Read rather than restated for the same reason _drained_still() reads: a
    fade that starts from the wrong colour is a snap with extra steps.
    """
    head = "@keyframes " + name + " {"
    at = css.find(head)
    if at < 0:
        return []
    depth, i = 0, at + len(head) - 1
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    else:
        return []
    out = []
    for m in re.finditer(r"(\d+)%\s*\{\s*background-image:\s*([^;]+);",
                         css[at:i + 1]):
        out.append((int(m.group(1)) / 100.0, " ".join(m.group(2).split())))
    return sorted(out)


def _mix_rgba(a: str, b: str, t: float) -> str:
    """`a`'s colours moved `t` of the way to `b`'s, the way GTK moves them.

    Premultiplied by alpha, and that is not a taste: measured against the
    rendered rim at seven phases of the sweep, straight interpolation was up to
    nine channel steps out and premultiplied was exact. The rim's fades have to
    begin on the pixels that are already on screen or the first frame is the
    snap this exists to remove.

    Everything but the colours — the angle, the stop positions — is kept from
    `a`, so the two gradients have to be the same shape. They are: both come
    from .halo-glow's own 90deg four-stop rim.
    """
    src = _RGBA_RE.findall(b)
    if len(src) != len(_RGBA_RE.findall(a)):
        return b if t >= 0.5 else a          # not the same shape; do not lie
    it = iter(src)

    def one(m: "re.Match") -> str:
        r1, g1, b1 = (float(m.group(i)) for i in (1, 2, 3))
        a1 = float(m.group(4))
        r2, g2, b2, a2 = (float(v) for v in next(it))
        aa = a1 + (a2 - a1) * t
        if aa <= 0.0:
            rr = gg = bb = 0.0
        else:
            rr = (r1 * a1 * (1.0 - t) + r2 * a2 * t) / aa
            gg = (g1 * a1 * (1.0 - t) + g2 * a2 * t) / aa
            bb = (b1 * a1 * (1.0 - t) + b2 * a2 * t) / aa
        return "rgba(%d,%d,%d,%.4f)" % (
            max(0, min(255, int(round(rr)))),
            max(0, min(255, int(round(gg)))),
            max(0, min(255, int(round(bb)))), aa)

    return _RGBA_RE.sub(one, a)


def _rim_gradient(phase: float, period: float) -> str:
    """What the sweep is painting `phase` seconds in.

    The sweep's timing function is linear, so this is a straight walk between
    the two keyframe stops the phase falls between.
    """
    if not RIM_STOPS:
        return RIM_DRAINED
    f = (phase % period) / period if period > 0 else 0.0
    for (o1, g1), (o2, g2) in zip(RIM_STOPS, RIM_STOPS[1:]):
        if o1 <= f <= o2:
            span = o2 - o1
            return g1 if span <= 0 else _mix_rgba(g1, g2, (f - o1) / span)
    return RIM_STOPS[-1][1]


def _twin_keyframes(css: str, name: str, twin: str) -> str:
    """Append an identical copy of one @keyframes block under a second name.

    Written rather than pasted so the two cannot drift apart. The rim needs two
    interchangeable names because changing animation-name is the only thing
    that restarts a GTK CSS animation, and a restart is what makes the negative
    animation-delay that carries the sweep's phase across a pause take effect —
    see HaloWindow._set_rim_running().
    """
    head = "@keyframes " + name + " {"
    at = css.find(head)
    if at < 0:
        return css
    depth, i = 0, at + len(head) - 1
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    else:
        return css
    block = css[at:i + 1]
    return css + "\n" + block.replace(head, "@keyframes " + twin + " {", 1) + "\n"


def load_css() -> None:
    provider = Gtk.CssProvider()
    css = _twin_keyframes(CSS.replace("$PAGE_BG$", remembered_page_bg()),
                          "halo-sweep", "halo-sweep-b")
    # The unfocused rim, derived from the focused one rather than written out
    # again — see _drained_still(). Kept in RIM_DRAINED as well as substituted,
    # because the fade that reaches it has to end on exactly the string this
    # class paints statically, or the handoff at the end of the fade is a snap
    # of its own. Measured: it moves 0 channel steps.
    global RIM_DRAINED, RIM_STOPS
    RIM_DRAINED = _drained_still(css)
    RIM_STOPS = _rim_keyframe_stops(css)
    css = css.replace("$DRAINED$", RIM_DRAINED)
    try:
        provider.load_from_string(css)
    except AttributeError:                       # GTK < 4.12
        provider.load_from_data(css.encode())
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


# ══════════════════════════════════════════════════════════════════════════
#  Google plumbing
# ══════════════════════════════════════════════════════════════════════════

# Dismiss the EU consent banner by choosing "Reject all". Runs on every
# Google page; the accepted state persists in the cookie jar afterwards.
CONSENT_JS = r"""
(function () {
  // Google's banner, on Google's own domains only. Links open in the panel now,
  // so this script is injected into every site the user browses to — and
  // "Reject all" is a label plenty of other sites put on buttons of their own.
  // Clicking those uninvited is not Halo's business.
  if (!/(^|\.)google\.[a-z][a-z.]{1,7}$/.test(location.hostname)) return;
  var tries = 0;
  function hunt() {
    if (tries++ > 24) return;
    var reject = document.querySelector('#W0wltc, button#W0wltc, div#W0wltc');
    if (!reject) {
      var labels = ['reject all', 'alle ablehnen', 'ablehnen', 'refuser tout',
                    'rechazar todo', 'rifiuta tutto', 'afwijzen'];
      var nodes = document.querySelectorAll('button, div[role="button"], input[type="submit"]');
      for (var i = 0; i < nodes.length; i++) {
        var t = (nodes[i].innerText || nodes[i].value || '').trim().toLowerCase();
        if (t && labels.indexOf(t) !== -1) { reject = nodes[i]; break; }
      }
    }
    if (reject) { reject.click(); return; }
    setTimeout(hunt, 250);
  }
  hunt();
})();
"""

# Cosmetics only: match WebKit's scrollbars to the popup instead of the
# stock light ones, and stop Google's own page background from punching a
# bright rectangle through our rounded corners.
#
# The gutter is left transparent on purpose: the WebView's base is painted in the
# page's own colour (see _adopt_page_bg), so an unstyled gutter reads as more
# page rather than as a channel with its own shade.
POLISH_CSS = """
html { color-scheme: dark; }

/* No ::selection rule here on purpose. Restyling the selection restyles every
   selection, on every page, with find mode switched off — ordinary drag-select
   came out amber instead of the platform blue, which is not Halo's call to
   make. The current match is marked by being *selected*, and the platform
   colour is what makes it read as the current one. */
/* Lens results open scrolled to the top and then animate down to the matches,
   which reads as a big blank gap for a couple of seconds. Landing instantly is
   what you want in a popup.

   Every element, not just html and body: Lens scrolls an inner container, so
   naming only the document left the actual scroller animating. This is still
   only half the job — a script that asks for {behavior:'smooth'} outright wins
   over this property by definition, which is why HOLD_SCROLL_JS exists. */
* { scroll-behavior: auto !important; }
::-webkit-scrollbar {
  width: 11px;
  height: 11px;
  background-color: transparent;
}
/* Styling ::-webkit-scrollbar does not stop WebKit reserving its stepper
   buttons, and an unstyled button paints as a small block at the very top of the
   track — the stray line at the top of the scrollbar. Nothing else removes it:
   the buttons have to be given no box at all. */
::-webkit-scrollbar-button,
::-webkit-scrollbar-button:start,
::-webkit-scrollbar-button:end {
  display: none;
  width: 0;
  height: 0;
}
::-webkit-scrollbar-track,
::-webkit-scrollbar-track-piece {
  background-color: transparent;
  border: none;
  box-shadow: none;
}
::-webkit-scrollbar-thumb {
  background-color: rgba(255,255,255,0.18);
  border-radius: 6px;
  border: 3px solid transparent;
  background-clip: content-box;
  box-shadow: none;
}
::-webkit-scrollbar-thumb:hover { background-color: rgba(255,255,255,0.30);
  border: 3px solid transparent; background-clip: content-box; }
::-webkit-scrollbar-corner { background-color: transparent; }
"""

# Make every programmatic scroll land instantly.
#
# CSS scroll-behavior only decides what an *unspecified* scroll does: per spec a
# caller passing {behavior:'smooth'} overrides the property outright, and that is
# exactly what Lens does on its results page. So the stylesheet alone could never
# have stopped it, and the symptom survived: the results open scrolled to the top
# showing a tall empty band above the Ai mode / All / Visual matches strip, then
# glide down to the matches about half a second later.
#
# Rewriting the argument at the four entry points that can animate is the only
# thing that actually settles it. Nothing is prevented — every scroll still goes
# exactly where it was going, it simply arrives at once, which is what a popup
# that is only on screen for a moment wants. Injected at document start so it is
# in place before any of Google's own code can call them.
# Reports the first frame of every new top-level document.
#
# WebKit keeps the OUTGOING page on screen until the incoming one has produced a
# frame of its own, and nothing in the GTK API says when that happens: COMMITTED
# is the point at which the new document becomes the view's, not the point at
# which it is what you can see. Two nested requestAnimationFrame callbacks are —
# the first runs before the frame is composited, the second after it — so the
# second is the earliest moment the page can honestly be said to be visible.
# Everything that lifts the loading cover waits for this; see _arm_paint_wait().
PAINT_JS = r"""
(function () {
  var tell = function () {
    try { window.webkit.messageHandlers.halo.postMessage('painted'); } catch (e) {}
  };
  try {
    requestAnimationFrame(function () { requestAnimationFrame(tell); });
  } catch (e) {
    tell();       /* no frame callbacks here; better early than never */
  }
})();
"""

# Whether the document is scrolled to its top, which is what decides if ↑ means
# "scroll up" or "leave the page". Reported rather than polled: asking WebKit for
# scrollY needs an async JavaScript round trip, and a key press cannot wait for
# one. Only the document's own scroll position — a page whose inner div scrolls
# while the document does not will report top, and ↑ will step out of it.
SCROLL_JS = r"""
(function () {
  var last = null;
  var tell = function () {
    var y = window.scrollY || document.documentElement.scrollTop || 0;
    var top = y <= 1;
    if (top === last) return;           /* only the crossings, not every pixel */
    last = top;
    try { window.webkit.messageHandlers.halo.postMessage(top ? 'top' : 'away'); }
    catch (e) {}
  };
  window.addEventListener('scroll', tell, {passive: true});
  window.addEventListener('load', tell);
  tell();
})();
"""

HOLD_SCROLL_JS = r"""
(function () {
  // Lens' own landing animation is what this defeats, so it stops at Google's
  // domains: forcing every site the user browses to to scroll instantly would be
  // overriding page behaviour that was never in Halo's way.
  if (!/(^|\.)google\.[a-z][a-z.]{1,7}$/.test(location.hostname)) return;
  function instant(opts) {
    if (opts && typeof opts === 'object') {
      var copy = {};
      for (var k in opts) copy[k] = opts[k];
      copy.behavior = 'instant';
      return copy;
    }
    return opts;
  }
  function patch(proto, name) {
    var original = proto && proto[name];
    if (typeof original !== 'function') return;
    proto[name] = function () {
      var args = [].slice.call(arguments);
      if (args.length === 1 && args[0] && typeof args[0] === 'object') {
        args[0] = instant(args[0]);
      }
      return original.apply(this, args);
    };
  }
  ['scroll', 'scrollTo', 'scrollBy'].forEach(function (n) {
    patch(window, n);
    patch(Element.prototype, n);
  });
  patch(Element.prototype, 'scrollIntoView');
})();
"""

# What colour is the page actually painting itself? Google's dark theme has moved
# shade more than once, and a hardcoded guess that drifts out of step shows up as
# a band down the scroll gutter and a seam under the toolbar. Ask the page and
# follow it instead. documentElement usually has no background of its own — the
# colour lives on body — so fall back to body.
# What is selected in the page right now, for middle-click-to-search. Plain
# expression rather than a script: evaluate_javascript hands back its value.
SELECTION_JS = "(window.getSelection() ? window.getSelection().toString() : '')"

# Web Audio is the half of "still making noise" that no amount of walking the DOM
# will find: an AudioContext is not an element, is not reachable from the
# document, and cannot be enumerated. The only way to be able to stop one later
# is to be there when it is made — so this goes in at DOCUMENT_START, in every
# frame, and keeps a register.
#
# Measured before it existed: with the pill dismissed, a page's AudioContext
# stayed `running` indefinitely, which keeps a real-time audio thread alive and
# keeps WebKit reporting is-playing-audio, which is what stopped the engine ever
# being released. See _release_blocked().
#
# The wrapper is a wrapper and not a replacement: `Halo.prototype` IS the real
# prototype and `Object.setPrototypeOf` gives it the real constructor's statics,
# so `instanceof`, subclassing and `AudioContext.prototype` all keep working, and
# what comes back from `new` is a genuine AudioContext rather than a stand-in.
# Anything less than that breaks the sites this is meant to be invisible to.
AUDIO_HOOK_JS = r"""
(function () {
  if (window.__haloQuiet) { return; }
  /* The register is WEAK, and pruned, and both matter. A strong array that is
     only ever appended to is a leak with the page's own lifetime: every
     AudioContext the page has ever made is kept reachable by us, including the
     ones it has closed and dropped, so they can never be collected and their
     audio threads and buffers stay with them. Sites that make a context per
     sound effect — a game, a messenger's notification chime, an ad player that
     rebuilds itself on every rotation — grow it without limit, and nothing on
     the Python side can see it happening: it is memory inside the web process,
     charged to a page that is doing nothing wrong.
     So: hold a WeakRef where the engine has them, drop anything closed or
     collected whenever the list is walked or grows past CAP, and keep a STRONG
     reference only to the handful we have actually suspended — those we have
     promised to resume, and a promise cannot be kept to something collected. */
  var live = [], held = [], CAP = 64;
  var Weak = (typeof WeakRef === 'function') ? WeakRef : null;
  function box(c) { try { return Weak ? new Weak(c) : c; } catch (e) { return c; } }
  function deref(b) {
    try { return Weak && b && b.deref ? b.deref() : b; } catch (e) { return null; }
  }
  function prune() {
    var keep = [];
    for (var i = 0; i < live.length; i++) {
      var c = deref(live[i]);
      try {
        if (c && c.state !== 'closed') { keep.push(live[i]); }
      } catch (e) {}
    }
    live = keep;
  }
  var api = {
    hush: function () {
      prune();
      var n = 0;
      for (var i = 0; i < live.length; i++) {
        var c = deref(live[i]);
        try {
          if (c && c.state === 'running') {
            c.suspend();
            if (held.indexOf(c) < 0) { held.push(c); }
            n++;
          }
        } catch (e) {}
      }
      return n;
    },
    // Only the ones we put to sleep. A context the page suspended itself is
    // its own business, and waking it would be us turning the sound back on.
    wake: function () {
      var n = held.length;
      for (var i = 0; i < held.length; i++) {
        try { held[i].resume(); } catch (e) {}
      }
      held.length = 0;
      return n;
    }
  };
  try {
    Object.defineProperty(window, '__haloQuiet',
                          {value: api, enumerable: false, configurable: true});
  } catch (e) { window.__haloQuiet = api; }
  ['AudioContext', 'webkitAudioContext'].forEach(function (name) {
    var Real = window[name];
    if (typeof Real !== 'function') { return; }
    function Halo() {
      var made = new (Function.prototype.bind.apply(
          Real, [null].concat([].slice.call(arguments))))();
      try {
        live.push(box(made));
        /* Bounded even on a page that never stops making them: the prune is
           what keeps the list the size of what is actually alive, and CAP is
           only how often it is worth asking. */
        if (live.length > CAP) { prune(); }
      } catch (e) {}
      return made;
    }
    Halo.prototype = Real.prototype;
    try { Object.setPrototypeOf(Halo, Real); } catch (e) {}
    try { window[name] = Halo; } catch (e) {}
  });
})();
"""

# Stop whatever the page is playing, everywhere it might be playing it, and say
# how many things were actually stopped. WebKitGTK 6.0 exposes no
# pause_all_media() — only set_is_muted(), which silences the sound while the
# video goes on decoding and the desktop goes on listing it as playing — so this
# asks the page itself.
#
# It walks *frames*, and that is not a refinement. The version that looked only
# at the top document left an embedded player running with the pill dismissed —
# measured, an iframe's <audio> still reporting playing six seconds after the
# pill was gone — and most video on the web is in an iframe. Cross-origin frames
# throw on the first property touched and are skipped; nothing can script those,
# which is what set_is_muted() is for.
# What WebKit's find leaves on the page is the document's own selection — the
# grey box behind the current match. search_finish() ends the find operation but
# does NOT drop that selection: verified by rendering the panel before and after
# leaving find mode, where the box behind the match was pixel-identical. So the
# selection is cleared explicitly, or every find leaves a mark on the page for as
# long as it stays loaded.
CLEAR_SELECTION_JS = ("(function(){ try {"
                      " window.getSelection().removeAllRanges();"
                      " } catch (e) {} return 1; })()")

STOP_MEDIA_JS = r"""
(function () {
  var stopped = 0;
  function quiet(w, depth) {
    if (depth > 6) { return; }
    try {
      var media = w.document.querySelectorAll('video, audio');
      for (var i = 0; i < media.length; i++) {
        try {
          if (!media[i].paused && !media[i].ended) {
            media[i].pause();
            stopped++;
          }
        } catch (e) {}
      }
    } catch (e) { return; }   // cross-origin: not ours to touch, and muted anyway
    try { if (w.__haloQuiet) { stopped += w.__haloQuiet.hush(); } } catch (e) {}
    try {
      for (var k = 0; k < w.frames.length; k++) { quiet(w.frames[k], depth + 1); }
    } catch (e) {}
  }
  quiet(window, 0);
  // Tell the desktop this page is not a player any more. Only the state, not
  // the metadata: the point is that the entry stops offering a Play button
  // nobody asked for, not that the page forgets what it was.
  try {
    if (navigator.mediaSession) { navigator.mediaSession.playbackState = 'none'; }
  } catch (e) {}
  return stopped;
})()
"""

# The other half of a pause, and the reason it is a pause rather than a kill. A
# suspended AudioContext that is never resumed is a page whose sound is broken
# for good, with nothing on screen to say why.
WAKE_MEDIA_JS = r"""
(function () {
  var woken = 0;
  function wake(w, depth) {
    if (depth > 6) { return; }
    try { if (w.__haloQuiet) { woken += w.__haloQuiet.wake(); } } catch (e) { return; }
    try {
      for (var k = 0; k < w.frames.length; k++) { wake(w.frames[k], depth + 1); }
    } catch (e) {}
  }
  wake(window, 0);
  return woken;
})()
"""

# Find-in-page, done by Halo rather than by WebKit, and the reason is contrast.
#
# WebKit paints its own match markers in an opaque yellow and offers no way to
# change it: there is no colour in WebKitFindOptions, nothing in WebKitSettings,
# and none of the 489 runtime features touches it. Halo forces a dark scheme —
# Adw.ColorScheme.FORCE_DARK plus `html { color-scheme: dark }` — so page text
# is near-white, and white on that yellow is a contrast ratio of **1.07:1**.
# That is not "hard to read", it is the same colour twice, and it is what was
# reported as "so bright you can barely read them".
#
# The CSS Custom Highlight API is the way out: ranges registered in
# CSS.highlights are painted as page content and styled by ::highlight(), so the
# foreground is ours to set — but only from an author-origin sheet, which is
# why FIND_CSS below is a sheet of its own and not part of POLISH_CSS.
#
# The current match is also left *selected*, which is not decoration: it is what
# makes Ctrl+C copy the match, and what a page script asking getSelection() sees.
#
# One thing WebKit's find did that this does not: search inside frames. A range
# from a subframe cannot go into the parent's CSS.highlights, so each frame
# would need its own registration and its own share of the index — which is a
# lot of machinery for a results panel. Top document only, deliberately.
# Author level, not user level, and that is the whole fix for "the highlights
# are unreadable". `background-color` inside ::highlight() applies from a
# user-origin sheet; `color` does NOT — WebKit 2.52 drops it, `!important` and
# all. Snapshots of the real web process: with this rule at USER level the
# glyphs stayed the page's own near-white and measured **1.29:1** on the amber,
# which is the reported "so yellow it is unreadable". The identical rule at
# AUTHOR level paints the ink and measures **13.98:1**. Nothing on the web
# styles ::highlight(halo-find), so there is nothing for an author sheet to
# collide with.
FIND_CSS = """
::highlight(halo-find) {
  background-color: #ffe082;
  color: #14161c;
}
"""

FIND_JS = r"""
(function () {
  if (window.__haloFind) { return; }
  var ranges = [], at = -1, CAP = 1000;

  function collect(needle) {
    ranges = []; at = -1;
    var q = String(needle || '').toLowerCase();
    if (!q || !document.body) { return; }
    var walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (!n.data) { return NodeFilter.FILTER_REJECT; }
        var p = n.parentNode, tag = p ? p.nodeName : '';
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT'
            || tag === 'TEXTAREA') {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var n;
    while ((n = walk.nextNode())) {
      var hay = n.data.toLowerCase(), from = 0, i;
      while ((i = hay.indexOf(q, from)) >= 0) {
        try {
          var r = document.createRange();
          r.setStart(n, i); r.setEnd(n, i + q.length);
          ranges.push(r);
        } catch (e) {}
        from = i + q.length;
        if (ranges.length >= CAP) { return; }
      }
    }
  }

  function paint() {
    try {
      if (!ranges.length) { CSS.highlights.delete('halo-find'); return; }
      CSS.highlights.set('halo-find', new Highlight(...ranges));
    } catch (e) {}
  }

  function show(i) {
    if (!ranges.length) { return; }
    at = ((i % ranges.length) + ranges.length) % ranges.length;
    var r = ranges[at];
    try {
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(r.cloneRange());
    } catch (e) {}
    try {
      var el = r.startContainer.parentElement;
      if (el && el.scrollIntoView) {
        el.scrollIntoView({block: 'center', inline: 'nearest'});
      }
    } catch (e) {}
  }

  function said() {
    return JSON.stringify({count: ranges.length, index: at, cap: CAP});
  }

  window.__haloFind = {
    run: function (q) { collect(q); paint(); show(0); return said(); },
    step: function (fwd) { show(at + (fwd ? 1 : -1)); return said(); },
    clear: function () {
      ranges = []; at = -1;
      try { CSS.highlights.delete('halo-find'); } catch (e) {}
      try { window.getSelection().removeAllRanges(); } catch (e) {}
      return said();
    }
  };
})();
"""

# Google's silent spelling correction, which is the one correction the address
# does not record. "Meintest du: X" and "Enthält auch Ergebnisse für X" are
# ordinary links to /search with a corrected q=, so clicking either moves the
# address and _sync_query_field follows it for free — Back included, because the
# address is what Back moves. This third one is not a link at all: Google applied
# the correction itself and the results already *are* for the corrected words,
# while q= still holds what was typed. The corrected words exist nowhere but the
# page, so the page is where they have to be read.
#
# Read out of the link's href, not its text. The text is marked up — <b><i>
# around the letters Google changed — and sits behind whatever the locale calls
# it ("Ergebnisse für", "Results for"), while the href carries the query verbatim
# and in every language.
#
# The hook is the id. Google's class names here (QRYxYe, NNMgCf, AwaEsc) are
# generated and change; #fprs / #fprsl have outlived them. The id is also the
# whole discrimination: "Meintest du" must NOT be copied into the field, because
# that bar offers a correction the page has *not* applied — the results under it
# are still for what was typed — and it is a plain anchor with no id at all.
# Checked against live google.com in German, 2026-08: every silent correction put
# the corrected query in a#fprsl inside div#fprs, and "Meintest du" put it in a
# bare <a> inside <p class="QRYxYe NNMgCf">.
#
# Deliberately no hostname test, though CONSENT_JS has one and needs it: clicking
# a button labelled "Reject all" on someone else's site would be Halo acting on a
# page that never asked. This only ever *reads*, and what it reads has to survive
# google_query() on the Python side before it can reach the field — so a site
# with an #fprsl of its own can post a message and get nowhere with it.
SPELL_JS = r"""
(function () {
  /* What this document's correction bar said, remembered for as long as the
     document lives. Google's own script *deletes* the bar when it is clicked:
     measured against live google.com, the click neither navigates nor reloads
     — history.length does not move and no load event fires — it only removes
     #fprs. WebKit may then keep that stripped document in its page cache, and
     coming back to it runs no user script again, so a look at the DOM would
     find nothing and the address, which still holds the misspelling, would win
     back a field the results on screen have long since moved past. */
  var known = null;
  function tell() {
    var a = document.getElementById('fprsl');
    if (a) {
      try {
        var link = new URL(a.href, location.href);
        if (link.pathname === '/search'
            && link.searchParams.get('spell') === '1') {
          known = link.searchParams.get('q') || known;
        }
      } catch (e) {}
    }
    if (!known) { return; }          /* no correction on this page, or not one */
    try {
      var here = new URL(location.href);
      /* This address told Google to leave the words alone, so it is the other
         half of the pair and nothing remembered here describes it. */
      if (here.searchParams.get('nfpr')) { return; }
      /* Nothing to say when the address already agrees — which is every page
         reached by clicking one of the correction links that do navigate. */
      if (known === (here.searchParams.get('q') || '')) { return; }
      window.webkit.messageHandlers.halo.postMessage(
        'spell ' + JSON.stringify({uri: location.href, q: known}));
    } catch (e) {}
  }
  /* Injected at END, and #fprs is in the HTML Google serves, so one look is
     enough — no polling, on any page. */
  tell();
  /* Back or forward onto a document WebKit kept: notify::uri has just put the
     misspelling back in the field from the address, and no user script runs
     again to undo that. pageshow is the only thing that fires. */
  window.addEventListener('pageshow', function (e) { if (e.persisted) tell(); });
})();
"""

PAGE_BG_JS = r"""
(function () {
  function bg(el) {
    if (!el) return '';
    var c = getComputedStyle(el).backgroundColor || '';
    return (c && c !== 'transparent' && c.indexOf('rgba(0, 0, 0, 0)') !== 0) ? c : '';
  }
  return bg(document.documentElement) || bg(document.body) || '';
})();
"""

# Halo already shows the query in its own pill, so Google's duplicate search
# box is 72px of wasted panel. Verified against a live results page: the tab
# strip (Images / Videos / News) lives in a sibling node, not inside
# #searchform, so this reclaims the space without losing navigation. If Google
# ever restructures, the selector simply stops matching and nothing breaks.
COMPACT_CSS = """
/* Hide the whole top-level header block that holds Google's search box.
   Hiding #searchform alone is not enough — its wrappers still reserve ~70px.
   The block is a direct child of <body>, so we select it structurally with
   :has(); Google's class names are obfuscated and rotate, so matching on them
   would rot. The result tabs live in a different body child (#main), which is
   why navigation survives.
   The :not() guards are deliberate: should Google ever move #searchform inside
   the results container, this rule must quietly stop applying rather than hide
   the results themselves. */
/* ...and never a block that contains the results. The :not() list above is
   the four ids of the CLASSIC results page, so it guards against #searchform
   moving into one of those and nothing else: a surface that wraps its search
   box and its answer together in a body-level element with any other id — the
   app-shell shape — would have had the whole page hidden, leaving a results
   area that is not empty but absent, with no "no results found" either,
   because there is no page left to say it.

   Reported as exactly that on a tab reached from a visual search. Whether it
   was this rule or a malformed address was not settled, so this is written as
   the guard that makes the question moot: #main is where Google puts the
   results on every surface measured here, and a rule for hiding the search box
   has no business matching an element that contains them. It costs the
   measured pages nothing — a container inside #main cannot contain #main. */
body > :has(#searchform):not(#main):not(#cnt):not(#rcnt):not(#center_col):not(:has(#main)) {
  display: none !important;
}

/* Lens keeps its search box in #sfcnt, well inside body > #main > #cnt, so the
   child combinator above never reached it and image searches kept Google's box
   after all. Measured on a live Lens result: #sfcnt renders 386px tall while the
   matches stream in and only collapses to ~18px seconds later, which is the tall
   empty band above the AI mode / All / Visual matches strip — and the smaller one
   left behind afterwards. The page is not scrolling at any point; the strip
   itself moves, from 404px down the viewport to 34px.

   This is the same thing the rule above hides — Google's own search box, which
   Halo's pill already replaces — and #sfcnt is a stable structural id, not one of
   the obfuscated class names that rotate. The tab strip is its sibling, so
   navigation is untouched. */
#sfcnt:not(:has(#main)) { display: none !important; }

/* Google parks an empty 16px spacer div immediately above #main, and with its
   search box gone that band is the whole gap between Halo's own toolbar and the
   AI Mode / All / Images / Videos strip: measured identically on live web, image
   and video results, the strip opens 18px down the page, of which 16 is this
   spacer and 2 is #cnt's own padding. Under a popup toolbar it reads as wasted
   panel rather than as breathing room.

   Matched structurally, because the class name (xrOgrb today) is one of the
   obfuscated ones that rotate: an element-less, text-less div that precedes
   #main can only be a spacer. Verified against all three result pages — it
   selects that div and nothing else, in particular never the absolutely
   positioned sibling that carries the account and apps controls. If Google drops
   the spacer or fills it, the rule simply stops matching and the page keeps its
   own spacing rather than being pulled up into the toolbar, which is why this is
   a hidden spacer and not a negative margin on #main. */
body > div:empty:has(~ #main) { display: none !important; }
"""

# Hand an image to Lens by driving its real upload form.
#
# Posting to /v3/upload ourselves does not work — Lens rejects it and bounces to
# google.com/?olud even with the session's own cookies. What does work is
# clicking the page's own hidden file input: WebKit then emits
# run-file-chooser, which we answer with the path (see _on_file_chooser). The
# form is submitted by Google's own JavaScript, so nothing here depends on a
# private endpoint.
#
# The input is named encoded_image; the page has other file inputs, and picking
# the wrong one silently fails, so match the name first. Lens renders late, so
# keep looking for a few seconds.
# Click the upload input exactly once, after the page has settled.
#
# Timing is the whole trick. The input appears in the DOM long before Lens binds
# the handler that submits it, so an early click is silently dropped and Google
# answers with /?olud. Hammering the input does not help either — repeated
# clicks keep failing on the same spent page. What works is one click on a
# freshly loaded page once it has gone quiet, so the wait below is measured, not
# guessed (see LENS_SETTLE_MS).
CLICK_LENS_UPLOAD_JS = r"""
(function () {
  if (window.__haloClicked) return;
  var tries = 0;
  function hunt() {
    if (tries++ > 40) return;
    var input = document.querySelector('input[name="encoded_image"]')
             || document.querySelector('input[type=file][accept*="image"]');
    if (input) { window.__haloClicked = 1; input.click(); return; }
    setTimeout(hunt, 100);
  }
  hunt();
})();
"""

# How long to let a freshly loaded Lens page settle before clicking, and how
# long to give the upload before assuming the click was too early.
LENS_SETTLE_MS = 1500
LENS_RETRY_MS = 4500

# Refine a visual search with words, the way Lens' own "Add to your search" box
# does. Driving the real input keeps us on Google's supported path; if the box
# has not rendered, the caller falls back to putting q= in the URL.
ADD_TEXT_TO_LENS_JS = r"""
(function () {
  var text = %(text)s;
  var box = document.querySelector('textarea[aria-label], input[aria-label]');
  var fields = [].slice.call(document.querySelectorAll('textarea, input[type=text], input[type=search]'));
  for (var i = 0; i < fields.length; i++) {
    var f = fields[i];
    if (f.offsetParent === null) continue;
    var hint = ((f.getAttribute('aria-label') || '') + ' ' +
                (f.placeholder || '')).toLowerCase();
    if (hint.indexOf('add') !== -1 || hint.indexOf('search') !== -1 ||
        hint.indexOf('suche') !== -1 || hint.indexOf('hinzu') !== -1) { box = f; break; }
  }
  if (!box) return 'no-box';
  box.focus();
  box.value = text;
  box.dispatchEvent(new Event('input', {bubbles: true}));
  var form = box.form;
  if (form) { form.submit(); return 'submitted'; }
  box.dispatchEvent(new KeyboardEvent('keydown',
      {key: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  return 'entered';
})();
"""

# Lens answers on www.google.com these days, not lens.google.com.
LENS_RESULT_MARKERS = ("vsrid=", "udm=26", "tbs=sbi", "lens.google.com/search")

# Searching an image that already has a public address needs none of the upload
# dance above: Google fetches it itself. Measured against both entry points —
# this one redirects once, straight onto
# www.google.com/search?vsrid=...&udm=26&lns_vfs=d, and the older
# www.google.com/searchbyimage?image_url= takes three hops to the same page.
# Note what that address carries: vsrid= and udm=26 are two of
# LENS_RESULT_MARKERS, so every rule already written against a Lens result page
# recognises one that arrived this way without being told about it.
LENS_BY_URL = "https://lens.google.com/uploadbyurl?url="

# The extensions worth writing for a data: image. Deliberately a table rather
# than the mimetypes module: that reads the system's MIME database on first use,
# to answer a question with seven possible answers.
DATA_URI_EXTS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                 "image/webp": ".webp", "image/svg+xml": ".svg",
                 "image/bmp": ".bmp", "image/avif": ".avif"}

SELECTION_WATCH_JS = r"""
(function () {
  /* The right-click menu wants to offer to search the selected words, and to
     say which words those are. WebKit's hit test reports THAT there is a
     selection but never what it says, and the menu is built synchronously — so
     there is no asking the page once the click has happened. Instead the page
     volunteers its selection as it changes, and the answer is already waiting.

     Only the changes, like SCROLL_JS: selectionchange fires for every caret
     move in every text field, and an unchanged selection is not news. */
  var last = null;
  var timer = 0;
  var tell = function () {
    timer = 0;
    var text = '';
    try { text = String(window.getSelection()); } catch (e) { return; }
    text = text.replace(/\s+/g, ' ').trim().slice(0, 2000);
    if (text === last) return;
    last = text;
    try { window.webkit.messageHandlers.halo.postMessage('sel:' + text); }
    catch (e) {}
  };
  document.addEventListener('selectionchange', function () {
    if (timer) return;              /* one report per burst, not per keystroke */
    timer = setTimeout(tell, 120);
  });
})();
"""

# What counts as a droppable / pickable image. Kept in one place so the file
# dialog's filter and the drop target cannot drift apart — they had, and a .avif
# or .tiff the dialog happily offered was then rejected on drop.
IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif",
               "image/bmp", "image/avif", "image/tiff", "image/heif",
               # .heic and .ico were droppable but missing here, so the file
               # dialog filtered out the very files a drop would have accepted.
               "image/heic", "image/x-icon", "image/vnd.microsoft.icon")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp",
              ".avif", ".tif", ".tiff", ".heic", ".heif", ".ico")


# ── dragging a picture out of a page ────────────────────────────────────
#
# Two things went wrong with that, and both of them are upstream of Halo.
#
# 1. The icon under the pointer was twice the size it should be — but only on
#    a scaled display, and not by the factor an earlier note in this file
#    claimed. Measured again in round 13, with a REAL injected pointer drag in
#    a nested mutter and the drag icon's own X window sampled mid-drag, a
#    1200x900 picture shown 300 wide:
#
#                      scale 1              scale 2
#      WebKit's own    200x150 device px    800x600 device px  (400x300 logical)
#      with this JS    200x150 device px    400x300 device px  (200x150 logical)
#
#    The chain is DragController::doImageDrag, which rasterises the image's OWN
#    pixels with createDragImageFromImage() — and then hands them to
#    fitDragImageToMaxSize(), which scales them down to the LAYOUT box and
#    again to fit maxDragImageSize(), 200x200 layout units here. So the icon
#    never carried the picture's own thousands of pixels: measured, it came out
#    200x150 whether the picture was drawn 300, 600 or 1200 wide. What is left
#    is platformAdjustDragImageForDeviceScaleFactor(), which multiplies those
#    200 units by the device scale, and DragSourceGtk4's
#    gtk_picture_new_for_paintable(), where GtkPicture takes a paintable's size
#    in PIXELS as its natural size in LOGICAL pixels. Those two do not cancel:
#    the icon comes out one device scale too big — exactly right at scale 1
#    and exactly twice too big at scale 2. This session's scale flips between
#    the two on its own, which is why it was a complaint some days and not
#    others.
#
# 2. Dropping it back on the page left it on screen for ever. WebKit's
#    DropTargetGtk4 ends a drop with
#        gdk_drop_finish(drop, gdk_drop_get_actions(drop))
#    — the source's whole action mask. GTK 4.22's gdk_drop_finish() begins
#        g_return_if_fail (gdk_drag_action_is_unique (action));
#    and a drag whose effectAllowed was never set carries COPY|MOVE|LINK.
#    Measured: Gdk.drag_action_is_unique(COPY|LINK) is False. The precondition
#    fails, the drop is never finished, no XdndFinished is ever sent, and
#    gdk_x11_drag_handle_finished() — the only caller of gdk_drag_drop_done(),
#    which is the only thing that hides the drag surface — never runs. The
#    picture hangs there until Halo is closed.
#
# Both are answered from inside the page, with the two web APIs that exist for
# exactly this. setDragImage() replaces the icon with a thumbnail Halo sizes;
# effectAllowed names one action, which is all GTK will accept.
#
# The two listeners sit on opposite phases on purpose. The drag image is set in
# the CAPTURE phase, ahead of the page's own dragstart handlers, so a page that
# wants its own icon still wins by setting it afterwards. effectAllowed is
# normalised in the BUBBLE phase, after them, and only when what they left is a
# mask GTK would refuse — so a page that chose one is never overruled.
#
# The thumbnail is a canvas and not a second <img>: an <img> would have to load
# before it could be rasterised, and the rasterising happens inside this same
# event dispatch. drawImage() from a cross-origin picture taints the canvas,
# which stops toDataURL() and nothing else — painting it is still fine, and
# painting it is all this does.
DRAG_JS = r"""
(function () {
  if (window.__haloDrag) { return; }
  window.__haloDrag = true;

  var CAP = 200;        // how wide the icon should end up, in logical pixels

  function thumbFor(img) {
    var box = img.getBoundingClientRect();
    var w = box.width, h = box.height;
    if (!(w > 0 && h > 0)) { w = img.naturalWidth; h = img.naturalHeight; }
    if (!(w > 0 && h > 0)) { return null; }
    // CAP is logical pixels; this element is measured in CSS pixels. WebKit
    // rasterises a drag image at the device scale factor and GTK then draws one
    // logical pixel per texture pixel, so what reaches the screen is
    // devicePixelRatio times what is asked for here.
    var dpr = window.devicePixelRatio || 1;
    var k = Math.min(1, (CAP / dpr) / Math.max(w, h));
    var cw = Math.max(1, Math.round(w * k)), ch = Math.max(1, Math.round(h * k));
    var c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(cw * dpr));
    c.height = Math.max(1, Math.round(ch * dpr));
    c.setAttribute('aria-hidden', 'true');
    c.style.cssText = 'position:fixed;left:-20000px;top:0;margin:0;border:0;'
      + 'padding:0;max-width:none;max-height:none;pointer-events:none;'
      + 'width:' + cw + 'px;height:' + ch + 'px;';
    try {
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    } catch (e) {
      return null;      // not decoded yet: leave WebKit its own icon
    }
    return c;
  }

  document.addEventListener('dragstart', function (ev) {
    var dt = ev.dataTransfer;
    if (!dt || !ev.target || !ev.target.closest) { return; }
    var img = ev.target.closest('img');
    if (!img) { return; }
    var root = document.body || document.documentElement;
    if (!root) { return; }
    var c = thumbFor(img);
    if (!c) { return; }
    root.appendChild(c);
    try {
      dt.setDragImage(c, Math.round(c.offsetWidth / 2),
                      Math.round(c.offsetHeight / 2));
    } catch (e) {}
    // It only has to survive being rasterised, which happens in this same
    // dispatch. Both a timer and dragend, because a drag that is refused after
    // all never fires dragend.
    //
    // The listener takes itself off again. It used to stay, one more per
    // picture ever dragged, each closure keeping its canvas alive after the
    // canvas had left the page — measured over three real drags: three added,
    // none removed. Whichever of the two fires first removes it, so the pair
    // costs one listener for the length of one dispatch and nothing after.
    var drop = function () {
      window.removeEventListener('dragend', drop, true);
      if (c.parentNode) { c.parentNode.removeChild(c); }
    };
    setTimeout(drop, 0);
    window.addEventListener('dragend', drop, true);
  }, true);

  // The only four values whose GDK action mask has at most one bit set, which
  // is what gdk_drop_finish() demands: copy -> COPY, link -> LINK, move ->
  // MOVE (WebCore's Generic does not map to a GDK bit), none -> nothing.
  var SINGLE = { copy: 1, link: 1, move: 1, none: 1 };
  document.addEventListener('dragstart', function (ev) {
    var dt = ev.dataTransfer;
    if (!dt) { return; }
    if (SINGLE[dt.effectAllowed]) { return; }   // the page already chose one
    var editable = false;
    try {
      editable = document.designMode === 'on'
        || !!(ev.target && ev.target.closest && ev.target.closest(
              'input,textarea,[contenteditable=""],[contenteditable="true"]'));
    } catch (e) {}
    // Move inside a text field, so dragging words about still moves them
    // instead of copying them; copy everywhere else, which is what a picture
    // dragged to a file manager or a chat window should be.
    try { dt.effectAllowed = editable ? 'move' : 'copy'; } catch (e) {}
  }, false);
})();
"""

# Without this the thumbnail above is built and thrown away. WebKit only looks
# at a drag image the page supplied when the dragged element is draggable as an
# ELEMENT rather than as an image: DragController::startDrag reads
# dataTransfer->createDragImage() under `state.type == DragSourceAction::DHTML`,
# and draggableElement() only sets that from -webkit-user-drag. An <img> is
# `auto` by default, which is the image path — the one that drags the full
# decoded picture. Saying `element` moves it onto the path that asks the page.
#
# It costs nothing when the script does not answer: EventHandler then sets the
# drag image to the element itself, which is the rendered box rather than the
# original pixels, and that alone is already the smaller of the two icons.
#
# USER level and only `img`, so a page that turns dragging off, or names its own
# behaviour, still outranks it.
DRAG_CSS = "img { -webkit-user-drag: element; }"


# Pango ends a line at any of these however the label is configured: they are
# UAX #14 *mandatory* breaks, and ellipsize only shortens a line that is too
# long — it never rejoins one that was deliberately ended. Measured with
# ellipsize=MIDDLE on at a fixed width: a 400-character address comes back one
# line 21px tall, and the same address with a newline in it comes back two lines
# and 42px. That is the whole of the two-line address bar, and the extra 21px
# also squeezes the page, because the panel's height maths reserves a fixed
# PANEL_CHROME for that toolbar.
#
# parse_qs() percent-decodes, so a %0A in ?q= arrives here as a real newline,
# and .strip() only trims the ends. The drop and middle-click routes already
# flatten text on the way in (_search_text); typing, pasting and --query never
# did, which is why it was intermittent.
HARD_BREAKS = re.compile(r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+")


def one_line(text: str) -> str:
    """Text a Gtk.Label cannot break, whatever it arrived as."""
    return HARD_BREAKS.sub(" ", text)


def search_url(query: str, verbatim: bool = False) -> str:
    # A query is one line by definition, so this is also where a pasted
    # paragraph stops being able to reach the address at all.
    query = one_line(query).strip()
    params = {"q": query}
    if CFG["safe_search"]:
        params["safe"] = "active"
    if verbatim:
        # Google's own switch for "these words, exactly", and the one its
        # "Stattdessen suchen nach" link carries. Measured against live
        # google.com in German, 2026-08, over four misspellings:
        # ?q=krankenhauss came back with results for "krankenhaus" behind an
        # "Ergebnisse für" bar, and ?q=krankenhauss&nfpr=1 came back with no
        # such bar at all — a "Meintest du" offer instead, which is Google
        # asking rather than deciding. That is what makes a step back out of a
        # silent correction possible: the page to go back to can be asked for.
        params["nfpr"] = "1"
    return "https://www.google.com/search?" + urllib.parse.urlencode(params)


# Google answers on the searcher's own country domain, and it redirects there by
# itself, so the whole google.<tld> family has to match: one label (com, de, fr),
# or a two-level public suffix (co.uk, com.au, com.br). Anchored, and with the dot
# spelled out, because a plain endswith() would call evilgoogle.com one of
# Google's own.
_GOOGLE_DOMAIN = re.compile(
    r"(?:^|\.)google\.(?:[a-z]{2,4}|(?:com|co|net|org|edu|gov)\.[a-z]{2})$")


def _is_google_page(uri: str) -> bool:
    """Is this one of Google's own pages?

    This used to be the guest list — Google in the panel, everything else pushed
    out to the real browser. Links open here now, so the only thing it still
    decides is which pages Google's own structural CSS is aimed at, which is why
    it no longer counts gstatic, youtube or googleusercontent as Google: those
    serve Google's assets and embeds, not the search results COMPACT_CSS was
    measured against.
    """
    try:
        host = urllib.parse.urlparse(uri).netloc.split("@")[-1]
        host = host.split(":")[0].lower().rstrip(".")
    except Exception:
        return False
    return bool(host) and bool(_GOOGLE_DOMAIN.search(host))


def _is_lens_result(uri: str) -> bool:
    """Is this one of Google's own visual-search result pages?

    The host test is the point of this existing, and leaving it out was a real
    hole rather than an untidiness. Every one of LENS_RESULT_MARKERS is a bare
    substring of the whole address — "vsrid=", "udm=26", "tbs=sbi" — and any
    site may carry any of them in its own query string, or merely quote a
    Google link inside one. Whatever answers True here is a page Halo will
    type the user's words into and submit, and whose address it will rewrite,
    so answering True for somebody else's site means doing both to them.
    """
    return _is_google_page(uri) and any(m in uri for m in LENS_RESULT_MARKERS)


def google_query(uri: str) -> str | None:
    """The words a Google results page is showing, or None if it is not one.

    Google's spelling corrections — "Did you mean", "Showing results for",
    "Ergebnisse für" — are ordinary links to /search with a corrected q=, so the
    query the page actually ran can be read straight out of the address. No page
    scraping, and nothing that has to be translated: the address says the same
    thing in every locale.

    None for everything else, and each rejection earns its place:
      · another site's ?q= is that site's parameter, not Halo's query — the
        panel browses now, so this is reached constantly;
      · a Lens results page is a Google page and can carry a q=, because
        _apply_lens_text() appends one to refine a visual search by text;
      · /sorry/ (the CAPTCHA), Maps, Images' own chrome — anything that is not
        /search — is not a search Halo can claim to have run;
      · a blank q= would empty the field for nothing.
    """
    if not uri or not _is_google_page(uri):
        return None
    if any(marker in uri for marker in LENS_RESULT_MARKERS):
        return None
    try:
        parsed = urllib.parse.urlparse(uri)
        if parsed.path.rstrip("/") != "/search":
            return None
        query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
    except Exception:
        return None
    return one_line(query).strip() or None


def _no_correction(uri: str) -> bool:
    """Does this address tell Google to run the words exactly as typed?

    The two halves of a corrected search carry the *same* q= — the misspelling —
    and differ in nothing else, so this is the only thing that tells them apart.
    That matters twice over: it is how the panel knows which half is on screen,
    and it is why a spelling report may never be believed on an address that
    carries it.
    """
    try:
        args = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
    except Exception:
        return False
    return args.get("nfpr", [""])[0] == "1"


# Schemes the panel deals with itself. Anything else — mailto:, tel:, magnet:, an
# app's own handler — is not a page to browse but a request for another program,
# so it goes to whatever the desktop has registered for it.
#
# javascript: belongs on this list even though it is not a page: it is WebKit's
# own to run or refuse, and "javascript:void(0)" is how a great deal of the web
# writes a button — treating those as external would have blocked them outright.
_WEB_SCHEMES = ("http", "https", "about", "data", "blob", "javascript")
# Handed to nobody, ever. One click on a page must not be able to make Halo open
# a local file in another program.
_UNLAUNCHABLE_SCHEMES = ("file", "resource")


def _uri_scheme(uri: str) -> str:
    try:
        return urllib.parse.urlparse(uri).scheme.lower()
    except Exception:
        return ""


def _uri_names_an_image(uri: str) -> bool:
    """Does this web address name a picture?

    The PATH only. A query string routinely carries an unrelated file name
    ("?next=/photo.jpg") and just as routinely carries none at all, so
    matching the whole address both invents pictures and misses them.

    Deliberately conservative: an address that is a picture but does not say
    so is searched as a page instead, which shows the picture. An address that
    is a page but ends in .png would be handed to Lens, which is the wrong way
    round, and only an extension test keeps that from happening.
    """
    if _uri_scheme(uri) not in ("http", "https"):
        return False
    try:
        path = urllib.parse.urlparse(uri).path
    except Exception:
        return False
    return path.lower().endswith(IMAGE_EXTS)


def _is_web_uri(uri: str) -> bool:
    """Can the panel itself show this, or does it belong to another program?"""
    return _uri_scheme(uri) in _WEB_SCHEMES


# A dot does not make an address. "node.js", "vue.js", "Math.random", "os.path",
# "np.array", "str.format" and "README.md" are all things people search for, and
# every one of them used to be loaded as a host instead — landing on a DNS error
# instead of results, which is about the most annoying way a search box can fail.
#
# So the last label has to be a real top-level domain before we treat the input
# as an address: every ISO 3166-1 alpha-2 country code plus the generic domains
# people actually type. Anything else is a search, which is also what Chrome
# does with the same input.
_CC_TLDS = (
    "ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi "
    "bj bm bn bo br bs bt bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv "
    "cw cx cy cz de dj dk dm do dz ec ee eg er es et eu fi fj fk fm fo fr ga gd "
    "ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il "
    "im in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc "
    "li lk lr ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo mp mq mr ms mt mu "
    "mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl "
    "pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sk sl sm sn "
    "so sr ss st su sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz "
    "ua ug uk us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw"
)
# Deliberately short. Many delegated gTLDs are ordinary English words — .run,
# .new, .one, .zone, .link, .email — and every one of those turns a perfectly
# normal search into a failed page load: Date.now, Promise.all, asyncio.run,
# Array.new. Only the generics people genuinely type as addresses are listed,
# because a wrongly-searched address costs one extra click while a wrongly-loaded
# search costs the result entirely.
_GENERIC_TLDS = (
    "com org net edu gov mil int info biz name pro mobi asia jobs travel "
    "app dev page site online store shop blog cloud tech xyz top club"
)
KNOWN_TLDS = frozenset(_CC_TLDS.split()) | frozenset(_GENERIC_TLDS.split())

# Country codes that are also the file extensions people put into a search box
# far more often than they type that country's domains. The comment at the top
# of this block names "README.md" as exactly the thing that must not be loaded
# as a host — and .md is Moldova, so it was, along with setup.py (Paraguay),
# build.sh (Saint Helena), main.rs (Serbia), file.cc (Cocos Islands),
# Foo.pm (Saint Pierre) and libfoo.so (Somalia). The rule that was written to
# stop node.js becoming a DNS error never covered its own example.
#
# Only when the input is nothing but `stem.tld`, so every ordinary way of
# meaning the address still works: a scheme, a "www.", a path, a port, or one
# more label ahead of it. Poland, India, Italy and Iceland are deliberately NOT
# here — .pl, .in, .it and .is are typed as real addresses constantly and are
# not extensions anybody searches for — and the trade this makes is the one the
# block above already states: a wrongly-searched address costs one extra click,
# a wrongly-loaded search costs the result entirely.
FILE_LIKE_TLDS = frozenset("md py sh rs cc pm so".split())


def _is_ipv4(host: str) -> bool:
    """Four parts, each 0-255 — an address and nothing anybody searches for."""
    parts = host.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        # isascii(), because str.isdigit() is true of Arabic-Indic and other
        # digits that int() will then happily parse into something nobody typed.
        if not (part.isascii() and part.isdigit()) or len(part) > 3:
            return False
        if not 0 <= int(part) <= 255:
            return False
    return True


def looks_like_url(text: str) -> str | None:
    """Return a loadable URL when the user clearly typed an address."""
    t = text.strip()
    if not t or " " in t:
        return None
    # Case-folded: a scheme typed in capitals is still a scheme, and
    # "HTTP://example.com" fell all the way through to being searched for.
    if t.lower().startswith(("http://", "https://")):
        return t
    head = t.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    # Strip credentials and any port before looking at the labels.
    host = head.split("@")[-1].split(":", 1)[0]
    # Two different questions, and the rules below need them apart: `whole` is
    # "the input is only a host part" — no path, no query, no fragment — and
    # `plain` narrows that to "and no port and no credentials either".
    whole = t == head
    plain = whole and host == head
    # An email address is not an address to open. Left alone the credential
    # stripping above turned "user@example.com" into "https://user@example.com":
    # the local part thrown away, the domain's front page loaded instead of the
    # search, and the engine asked about credentials nobody typed.
    if whole and "@" in head:
        return None
    low = host.lower()
    # http, not https, for the two kinds of host that are reached over a network
    # nobody bought a certificate for. This is what every browser does with the
    # same input, and https here would be a certificate warning instead of a
    # page on most routers and every dev server.
    if low == "localhost" or low.endswith(".localhost"):
        return "http://" + t
    if _is_ipv4(low):
        # The digits test below exists to keep "3.14" and bare numbers out, and
        # it was catching every IPv4 address with them — so a router's admin
        # page at 192.168.1.1, a dev server at 127.0.0.1:3000 and 8.8.8.8 were
        # all searched for on Google rather than opened.
        return "http://" + t
    if "." not in host or host.endswith(".") or host.startswith("."):
        return None
    if host.replace(".", "").isdigit():
        return None             # a bare number, or a version like 3.14
    if low.startswith("www."):
        return "https://" + t   # nobody types "www." meaning anything else
    labels = low.split(".")
    if plain and len(labels) == 2 and labels[-1] in FILE_LIKE_TLDS:
        return None             # README.md, setup.py, build.sh — see above
    if labels[-1] in KNOWN_TLDS:
        return "https://" + t
    return None


# How long a keystroke waits before it turns into a request.
#
# Zero, and measured rather than argued. A debounce only saves a request when it
# is LONGER than the gap between two keystrokes; otherwise its timer expires
# between them and fires anyway. Typing "wetter berlin" and "python decorators"
# through the real handler at 90ms and at 160ms a key, 60 and 0 issue the same
# 11-12 and 15-16 requests. It saved nothing at any speed a person types at.
#
# And it was not free. An answer is thrown away if the field has moved on while
# it was in flight, so it reaches the screen only while the debounce plus the
# round trip fits between two keystrokes. Against the real endpoint on a
# kept-alive connection that round trip is 64ms median (56-140ms over 28
# prefixes), so 60 + 64 = 124ms — longer than a fast typist's 90-110. Measured
# at 110ms a key, *every* answer of a burst was discarded and the drawer stood
# on the three past searches the local lead had put there until the typing
# slowed: six keystrokes of nothing but history, which is what "there are only
# 3, and the rest turn up later" was. At 0 the same burst is full from the
# second keystroke on.
#
# Nothing becomes unbounded. _SuggestionService keeps only the newest query
# while its worker is busy, so what reaches the wire is capped at one request
# per round trip however fast fetch() is called — the coalescing this constant
# was written for, done one layer down by the part that knows how slow the
# network actually is.
SUGGEST_DEBOUNCE_MS = 0

_SUGGEST_HOST = "suggestqueries.google.com"
_SUGGEST_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/18.3 Safari/605.1.15")


class _SuggestionService:
    """Autocomplete over one connection that stays open between keystrokes.

    A fresh urlopen() per keystroke pays DNS, TCP and a TLS handshake every time:
    measured against Google's endpoint that is 139ms median, against 71ms once a
    connection is being reused. Since the wait people notice is the debounce plus
    that round trip, keeping the socket alive is worth more than any amount of
    tuning around it.

    One worker thread owns the connection, so requests are serialised and never
    interleave on it. Only the newest query is kept while the worker is busy —
    every earlier keystroke is already obsolete by the time its turn would come.
    Answers are remembered, which is what makes backspacing instant rather than
    another round trip for something already seen.
    """

    CACHE_MAX = 96
    IDLE_TIMEOUT = 50.0         # past this, assume the far end has hung up
    TIMEOUT = 3.5

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._pending: tuple[str, object] | None = None
        self._warm = False
        self._running = False
        self._conn = None
        self._used = 0.0
        self._cache: dict[str, list[str]] = {}
        self._cache_lock = threading.Lock()

    # ── main thread ──────────────────────────────────────────────────────
    def cached(self, query: str) -> list[str] | None:
        """An answer already in hand, or None. Never blocks."""
        with self._cache_lock:
            return self._cache.get(query)

    def warm(self) -> None:
        """Open the connection now, so the first real query does not pay for it.

        Called on the first typed character. Suggestions start at the second, so
        the TLS handshake overlaps the time between two keystrokes instead of
        landing in front of the first list. Nothing is sent, and this is only
        ever reached while the user is already typing into a live search box, so
        it contacts Google exactly when the feature says it does.
        """
        with self._cv:
            self._warm = True
            self._start()
            self._cv.notify()

    def fetch(self, query: str, callback) -> None:
        with self._cv:
            self._pending = (query, callback)
            self._start()
            self._cv.notify()

    def _start(self) -> None:
        """Caller holds the lock."""
        if not self._running:
            self._running = True
            threading.Thread(target=self._serve, daemon=True).start()

    # ── worker thread ────────────────────────────────────────────────────
    def _serve(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._warm:
                    if self._conn is None:
                        self._cv.wait()
                        continue
                    # There is a socket open, and waiting on it for ever is what
                    # kept a TLS connection to Google standing for the whole life
                    # of a resident daemon: one search after breakfast, one
                    # connection until the machine is shut down. IDLE_TIMEOUT was
                    # already the rule — it was only ever applied on the way into
                    # the NEXT query, which for a pill nobody touches again never
                    # comes. So wake up when it goes stale and close it then.
                    left = self.IDLE_TIMEOUT - (time.monotonic() - self._used)
                    if left <= 0:
                        self._drop()
                        continue
                    self._cv.wait(left)
                job, self._pending = self._pending, None
                warm, self._warm = self._warm, False
            if job is None:
                if warm:
                    self._connect()          # nothing to ask yet; just be ready
                continue
            query, callback = job
            items = self._lookup(query)
            if items:
                self._remember(query, items)
            GLib.idle_add(callback, query, items or [])

    def _connect(self):
        # http.client rather than urllib.request: urlopen() has no way to reuse a
        # connection, which is the entire point here. Imported on the worker so
        # the cost never lands in front of the first popup.
        import http.client
        if self._conn is not None and time.monotonic() - self._used > self.IDLE_TIMEOUT:
            self._drop()
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(_SUGGEST_HOST, timeout=self.TIMEOUT)
            self._conn.connect()
            self._used = time.monotonic()
        return self._conn

    def _drop(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _lookup(self, query: str) -> list[str]:
        path = "/complete/search?" + urllib.parse.urlencode(
            {"client": "firefox", "q": query})
        headers = {"User-Agent": _SUGGEST_UA, "Accept": "*/*",
                   "Connection": "keep-alive"}
        # Two goes: a kept-alive connection Google has quietly closed fails on
        # the request rather than the connect, and that must not cost a keystroke.
        for attempt in (0, 1):
            try:
                conn = self._connect()
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()          # drained in full, or it cannot be reused
                self._used = time.monotonic()
                if resp.status != 200:
                    return []
                payload = json.loads(body.decode("utf-8", "replace"))
                return [s for s in payload[1] if isinstance(s, str)][:6]
            except Exception:
                self._drop()
                if attempt:
                    return []
        return []

    def _remember(self, query: str, items: list[str]) -> None:
        with self._cache_lock:
            self._cache[query] = items
            while len(self._cache) > self.CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))


SUGGESTIONS = _SuggestionService()


def fetch_suggestions(query: str, callback) -> None:
    """Google's autocomplete endpoint, off the main thread."""
    SUGGESTIONS.fetch(query, callback)


# ══════════════════════════════════════════════════════════════════════════
#  Desktop integration (all of it driven from the GUI)
# ══════════════════════════════════════════════════════════════════════════

class DesktopIntegration:
    DESKTOP_ID = f"{APP_ID}.desktop"
    APPS_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "applications"
    AUTOSTART_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "autostart"
    ICON_DIR = (Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share"))
                / "icons/hicolor/scalable/apps")
    KEY_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
    KEY_CHILD = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
    # One dconf entry per shortcut: …/custom-keybindings/halo0/, halo1/, …
    KEY_PATH_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/halo"

    @staticmethod
    def _exec_command(extra: str = "") -> str:
        launcher = f'"{SELF_PATH}"' if " " in str(SELF_PATH) else str(SELF_PATH)
        interp = "" if os.access(SELF_PATH, os.X_OK) else f"{sys.executable} "
        return f"{interp}{launcher}{extra}".strip()

    @classmethod
    def icon_file(cls) -> Path:
        return cls.ICON_DIR / f"{APP_ID}.svg"

    @classmethod
    def install_icon(cls) -> None:
        try:
            cls.ICON_DIR.mkdir(parents=True, exist_ok=True)
            cls.icon_file().write_text(ICON_SVG)
        except Exception:
            pass

    @classmethod
    def _icon_value(cls) -> str:
        """What goes after Icon= — the icon’s own path, not its themed name.

        The themed name (“io.github.halo.Search”, resolved through the icon
        theme) is the tidier spelling, and it does not work here, for a reason
        worth writing down. Writing a .desktop file makes GAppInfoMonitor fire
        immediately, so GNOME Shell resolves the icon in that same instant —
        against a GtkIconTheme it loaded at login. Measured on GTK 4.22: that
        theme only re-stats <search path>/hicolor, only on a lookup at least
        five seconds after its previous check, and never at all for a file that
        appears inside scalable/apps once hicolor itself exists. So the icon
        written moments ago is invisible, the lookup falls back to the generic
        placeholder, and Shell caches that under the name we asked for. Nothing
        repairs it afterwards: when the rescan does notice, it revalidates
        silently, without the “changed” signal that would make Shell drop the
        cached placeholder — which is why the wrong icon survives until the
        next login.

        An absolute path is what the Desktop Entry specification offers for
        exactly this (“if the name is an absolute path, the given file will be
        used”), it passes desktop-file-validate, and it resolves as a GFileIcon
        straight off disk with no icon theme in the way — so it is right on the
        first lookup, which is the only one we get. The icon still goes to its
        proper hicolor path, for anything that does look Halo up by name.
        """
        return str(cls.icon_file())

    @classmethod
    def install_launcher(cls) -> bool:
        cls.install_icon()
        body = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "GenericName=Google Search\n"
            "Comment=Floating Google search popup\n"
            f"Exec={cls._exec_command()}\n"
            f"Icon={cls._icon_value()}\n"
            "Terminal=false\n"
            # One main category only, or the launcher can show up twice.
            "Categories=Utility;\n"
            "Keywords=search;google;web;lens;\n"
            "StartupNotify=false\n"
            f"StartupWMClass={APP_ID}\n"
            "X-GNOME-UsesNotifications=false\n"
        )
        try:
            cls.APPS_DIR.mkdir(parents=True, exist_ok=True)
            (cls.APPS_DIR / cls.DESKTOP_ID).write_text(body)
            if shutil.which("update-desktop-database"):
                subprocess.run(["update-desktop-database", str(cls.APPS_DIR)],
                               check=False, capture_output=True, timeout=20)
            return True
        except Exception:
            return False

    @classmethod
    def launcher_installed(cls) -> bool:
        return (cls.APPS_DIR / cls.DESKTOP_ID).exists()

    @classmethod
    def set_autostart(cls, enabled: bool) -> bool:
        path = cls.AUTOSTART_DIR / f"{APP_ID}-autostart.desktop"
        try:
            if not enabled:
                path.unlink(missing_ok=True)
                return True
            cls.AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={APP_NAME} (background)\n"
                f"Exec={cls._exec_command(' --daemon')}\n"
                f"Icon={cls._icon_value()}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                "NoDisplay=true\n")
            return True
        except Exception:
            return False

    @classmethod
    def autostart_enabled(cls) -> bool:
        return (cls.AUTOSTART_DIR / f"{APP_ID}-autostart.desktop").exists()

    # ── GNOME global shortcut ────────────────────────────────────────────
    @classmethod
    def _schema_available(cls, schema: str) -> bool:
        src = Gio.SettingsSchemaSource.get_default()
        return bool(src and src.lookup(schema, True))

    @classmethod
    def shortcut_supported(cls) -> bool:
        return cls._schema_available(cls.KEY_SCHEMA) and cls._schema_available(cls.KEY_CHILD)

    @classmethod
    def _path_for(cls, index: int) -> str:
        return f"{cls.KEY_PATH_BASE}{index}/"

    @classmethod
    def _our_paths(cls, media: Gio.Settings) -> list[str]:
        """Custom-keybinding paths that belong to Halo.

        The prefix also catches Halo 1.0's single `…/halo/` entry, so upgrading
        cleans the old one up instead of leaving a duplicate behind.
        """
        return [p for p in media.get_strv("custom-keybindings")
                if p.startswith(cls.KEY_PATH_BASE)]

    # ── recognising a Halo shortcut wherever GNOME filed it ──────────────
    # Halo writes its own slots as …/custom-keybindings/halo0/, halo1/, … But a
    # shortcut the user adds by hand in Settings ▸ Keyboard lands at …/custom0/,
    # named and numbered by GNOME, and looking only under our own prefix meant
    # Halo could not see it: the row stayed unticked, the status line said "no
    # shortcut", and pressing the key worked anyway. So identify a binding by
    # what it *runs*, not by where it is filed.
    @classmethod
    def _command_script(cls, command: str) -> Path | None:
        """The halo.py the command runs, whether or not it is the one running."""
        if not command:
            return None
        try:
            argv = GLib.shell_parse_argv(command)[1]
        except Exception:
            argv = command.split()
        wanted = SELF_PATH.name
        for token in argv:
            # Matched on the file name alone, so a copy that has been moved is
            # still recognised as Halo rather than read as somebody else's key.
            if token and Path(token).name == wanted:
                return Path(token)
        return None

    # Exactly what set_shortcuts() writes into a slot's name.
    ENTRY_NAME = f"{APP_NAME} — Google search"

    @classmethod
    def _command_targets_halo(cls, command: str, name: str = "") -> bool:
        """Does this custom keybinding start Halo — this copy or another one?

        The name is compared exactly, and that matters more than it looks. This
        used to accept any name *starting* with "Halo", and everything claimed
        here is something set_shortcuts() will delete — so ticking a key in the
        menu quietly destroyed the user's own shortcuts if they happened to be
        called "Halo Infinite" or "Halogen lamp toggle". Verified against six
        realistic decoys: three were deleted. What a binding *runs* is the honest
        signal; a name only counts when it is the one Halo writes itself.
        """
        if cls._command_script(command) is not None:
            return True
        return name == cls.ENTRY_NAME

    @classmethod
    def _command_is_current(cls, command: str) -> bool:
        """Does it run *this* file, at the path this process was started from?

        Compared on the resolved script path, not on the whole command string.
        Comparing strings would call somebody's hand-written
        "python3 …/halo.py --toggle" stale merely for being spelled differently,
        and repair would then rewrite a shortcut that works — including whichever
        flag they chose. The only thing worth repairing here is the path, because
        a wrong path is the one version of this that cannot work.
        """
        script = cls._command_script(command)
        if script is None:
            return False
        try:
            return script.resolve() == SELF_PATH
        except Exception:
            return False

    @classmethod
    def halo_bindings(cls) -> list[dict]:
        """Every custom keybinding that opens Halo, ours or hand-made.

        Each entry: path, accel, name, command, `ours` (filed under our own
        prefix, so we may rewrite the slot freely) and `stale` (it starts a Halo
        at some other path — what moving halo.py leaves behind).
        """
        if not cls.shortcut_supported():
            return []
        found: list[dict] = []
        try:
            media = Gio.Settings.new(cls.KEY_SCHEMA)
            for path in media.get_strv("custom-keybindings"):
                try:
                    entry = Gio.Settings.new_with_path(cls.KEY_CHILD, path)
                    binding = entry.get_string("binding")
                    command = entry.get_string("command")
                    name = entry.get_string("name")
                except Exception:
                    continue
                ours = path.startswith(cls.KEY_PATH_BASE)
                if not binding:
                    continue
                if not (ours or cls._command_targets_halo(command, name)):
                    continue
                found.append({"path": path, "accel": binding, "name": name,
                              "command": command, "ours": ours,
                              "stale": not cls._command_is_current(command)})
        except Exception:
            pass
        return found

    @classmethod
    def foreign_bindings(cls) -> list[dict]:
        """Halo shortcuts somebody added outside Halo, in GNOME's own slots."""
        return [b for b in cls.halo_bindings() if not b["ours"]]

    @classmethod
    def drop_binding(cls, path: str) -> bool:
        """Delete one custom keybinding outright, slot and registration both."""
        try:
            media = Gio.Settings.new(cls.KEY_SCHEMA)
            rest = [p for p in media.get_strv("custom-keybindings") if p != path]
            media.set_strv("custom-keybindings", rest)
            cls._reset_path(path)
            Gio.Settings.sync()
            return True
        except Exception:
            return False

    @classmethod
    def _reset_path(cls, path: str) -> None:
        try:
            entry = Gio.Settings.new_with_path(cls.KEY_CHILD, path)
            for key in ("name", "command", "binding"):
                entry.reset(key)
        except Exception:
            pass

    # How many halo* slots to sweep when cleaning up. Deliberately generous:
    # cleanup must not depend on reading back our own paths, because a GSettings
    # read straight after a write can still return the pre-write value.
    MAX_SHORTCUT_SLOTS = 16

    @classmethod
    def set_shortcuts(cls, accels: list[str]) -> bool:
        """Make exactly these accelerators open Halo — one dconf entry each.

        Slots are rebuilt from scratch rather than diffed against what we read
        back, so no stale entry can survive and nothing depends on read-after-
        write consistency.
        """
        if not cls.shortcut_supported():
            return False
        try:
            # A shortcut created in Settings ▸ Keyboard lives in a slot GNOME
            # named, not one of ours. "Exactly these" has to cover those too:
            # left in place, a key the user has just unticked here would go on
            # opening Halo from a slot this list cannot reach, and a key that is
            # ticked would be bound twice — which is the very thing GNOME
            # resolves by picking a winner at random.
            for binding in cls.foreign_bindings():
                cls.drop_binding(binding["path"])
            media = Gio.Settings.new(cls.KEY_SCHEMA)
            wanted = [cls._path_for(i) for i in range(len(accels))]

            for index, accel in enumerate(accels):
                entry = Gio.Settings.new_with_path(cls.KEY_CHILD, cls._path_for(index))
                entry.set_string("name", cls.ENTRY_NAME)
                entry.set_string("command", cls._exec_command(" --toggle"))
                entry.set_string("binding", accel)

            # Blank every slot we are not using, plus Halo 1.0's single entry.
            for index in range(len(accels), cls.MAX_SHORTCUT_SLOTS):
                cls._reset_path(cls._path_for(index))
            cls._reset_path(cls.KEY_PATH_BASE + "/")

            # Keep everyone else's shortcuts, drop all of ours, then add ours
            # back in the wanted order.
            others = [p for p in media.get_strv("custom-keybindings")
                      if not p.startswith(cls.KEY_PATH_BASE)]
            media.set_strv("custom-keybindings", others + wanted)
            Gio.Settings.sync()
            return True
        except Exception:
            return False

    @classmethod
    def active_shortcuts(cls) -> list[str]:
        """Accelerators currently registered to open Halo, however they got there.

        Ours first, so the order the user ticked them in survives; anything
        hand-made in GNOME's own slots follows.
        """
        bindings = cls.halo_bindings()
        seen, found = set(), []
        for binding in sorted(bindings, key=lambda b: not b["ours"]):
            ident = accel_id(binding["accel"]) or binding["accel"]
            if ident in seen:
                continue
            seen.add(ident)
            found.append(binding["accel"])
        return found

    @classmethod
    def shortcut_active(cls) -> bool:
        return bool(cls.active_shortcuts())

    # ── conflict detection ───────────────────────────────────────────────
    # GNOME happily stores two actions on one accelerator and then picks a
    # winner arbitrarily — usually the built-in one, leaving Halo looking
    # broken with no error anywhere. So check before claiming a shortcut.
    CONFLICT_SCHEMAS = {
        "org.gnome.desktop.wm.keybindings": "Windows & workspaces",
        "org.gnome.shell.keybindings": "GNOME Shell",
        "org.gnome.mutter.keybindings": "Mutter",
        "org.gnome.mutter.wayland.keybindings": "Mutter (Wayland)",
        "org.gnome.settings-daemon.plugins.media-keys": "System & media keys",
    }

    # Both live at module scope, because Config() compares accelerators while
    # it loads and that happens long before this class is defined. Kept here as
    # names too, so every existing call site reads the same as it always did.
    _accel_id = staticmethod(accel_id)
    accel_label = staticmethod(accel_label)

    @classmethod
    def conflict_map(cls) -> dict[tuple, list[tuple[str, str]]]:
        """Every accelerator GNOME has already spoken for -> [(area, action)].

        Built in one sweep and handed to every row at once. Asking per row meant
        re-reading five schemas — several hundred dconf round-trips — once for
        each preset key, which was long enough to stall the ⋯ menu as it opened.
        """
        owners: dict[tuple, list[tuple[str, str]]] = {}
        source = Gio.SettingsSchemaSource.get_default()
        if source is None:
            return owners

        def claim(accel: str, area: str, action: str) -> None:
            ident = cls._accel_id(accel)
            if ident:
                owners.setdefault(ident, []).append((area, action))

        for schema, area in cls.CONFLICT_SCHEMAS.items():
            schema_obj = source.lookup(schema, True)
            if schema_obj is None:
                continue
            settings = Gio.Settings.new(schema)
            for key in schema_obj.list_keys():
                try:
                    value = settings.get_value(key)
                except Exception:
                    continue
                kind = value.get_type_string()
                if kind == "as":
                    bound = list(value)
                elif kind == "s":
                    bound = [value.get_string()]
                else:
                    continue
                for item in bound:
                    if item:
                        claim(item, area, key.replace("-", " "))

        # Other people's custom shortcuts count too — but not any of Halo's.
        # Skipping only our own halo* prefix used to report a shortcut the user
        # had added by hand in Settings ▸ Keyboard as a clash with itself, which
        # is both alarming and untrue.
        try:
            media = Gio.Settings.new(cls.KEY_SCHEMA)
            for path in media.get_strv("custom-keybindings"):
                if path.startswith(cls.KEY_PATH_BASE):
                    continue        # ours; not a conflict with ourselves
                entry = Gio.Settings.new_with_path(cls.KEY_CHILD, path)
                command = entry.get_string("command")
                name = entry.get_string("name")
                if cls._command_targets_halo(command, name):
                    continue        # also Halo, just filed by GNOME
                claim(entry.get_string("binding"), "Your custom shortcuts",
                      name or "unnamed")
        except Exception:
            pass
        return owners

    @classmethod
    def find_conflicts(cls, accel: str,
                       owners: dict | None = None) -> list[tuple[str, str]]:
        """Who already owns this accelerator? -> [(area, action)]

        Pass a conflict_map() when checking several keys in a row, so the sweep
        happens once rather than once per key.
        """
        want = cls._accel_id(accel)
        if not want:
            return []
        if owners is None:
            owners = cls.conflict_map()
        return list(owners.get(want, ()))

    # ── noticing changes made outside Halo ──────────────────────────────
    # GNOME's keyboard panel writes the same dconf keys Halo does, so a shortcut
    # can be added, re-bound or deleted with Halo none the wiser: the ⋯ menu only
    # re-read them when it opened. Watching the keys instead means the tick boxes,
    # the status line and the saved config follow whatever the user just did in
    # Settings, live.
    _watchers: list = []

    @classmethod
    def watch_shortcuts(cls, callback) -> None:
        """Call `callback` whenever any custom keybinding changes, anywhere.

        Two things have to be watched, because they change independently: the
        list of registered paths (a shortcut added or deleted) and the contents
        of each slot (a shortcut re-bound in place, which leaves the list
        untouched). The per-slot watch covers every slot we might ever own rather
        than only the ones we own now, so a key typed into a slot Halo had
        released is still noticed.
        """
        if cls._watchers or not cls.shortcut_supported():
            return
        try:
            media = Gio.Settings.new(cls.KEY_SCHEMA)
            media.connect("changed::custom-keybindings", lambda *_a: callback())
            cls._watchers.append(media)
            for index in range(cls.MAX_SHORTCUT_SLOTS):
                entry = Gio.Settings.new_with_path(cls.KEY_CHILD,
                                                   cls._path_for(index))
                entry.connect("changed", lambda *_a: callback())
                cls._watchers.append(entry)
        except Exception:
            # Watching is an improvement, not a requirement: without it the menu
            # simply re-reads on open, exactly as it always did.
            cls._watchers = []

    # ── is what we wrote still right? ───────────────────────────────────
    # Everything setup writes records the absolute path of halo.py, because that
    # is the only way a .desktop file or a dconf command can name a script. Move
    # the file and all of it still exists and all of it points at nothing. This
    # is how that gets noticed, and named, rather than just failing quietly.
    @classmethod
    def _desktop_exec(cls, path: Path) -> str | None:
        try:
            for line in path.read_text().splitlines():
                if line.startswith("Exec="):
                    return line[5:].strip()
        except Exception:
            pass
        return None

    @classmethod
    def audit(cls) -> list[dict]:
        """What setup has written, and whether each piece is still correct.

        `present` is what is on disk; `stale` means it is there but points
        somewhere else — a launcher for a halo.py that has moved, an icon from an
        older version. Absent is not reported as a fault: each piece has its own
        switch in the menu, so "not there" is usually a choice, and a check that
        nags about a switch the user turned off is a check nobody reads.
        """
        launcher = cls.APPS_DIR / cls.DESKTOP_ID
        autostart = cls.AUTOSTART_DIR / f"{APP_ID}-autostart.desktop"
        icon = cls.icon_file()
        items = []

        exec_line = cls._desktop_exec(launcher)
        items.append({
            "key": "launcher", "what": "Application launcher",
            "where": str(launcher), "present": launcher.exists(),
            "stale": bool(exec_line) and exec_line != cls._exec_command(),
            "detail": f"still starts {exec_line}" if exec_line else ""})

        try:
            icon_stale = icon.exists() and icon.read_text() != ICON_SVG
        except Exception:
            icon_stale = False
        items.append({
            "key": "icon", "what": "Application icon", "where": str(icon),
            "present": icon.exists(), "stale": icon_stale,
            "detail": "drawn by an older version of Halo" if icon_stale else ""})

        exec_line = cls._desktop_exec(autostart)
        items.append({
            "key": "autostart", "what": "Start-at-login entry",
            "where": str(autostart), "present": autostart.exists(),
            "stale": bool(exec_line) and exec_line != cls._exec_command(" --daemon"),
            "detail": f"still starts {exec_line}" if exec_line else ""})

        bindings = cls.halo_bindings()
        stale = [b for b in bindings if b["stale"]]
        items.append({
            "key": "shortcuts", "what": "Keyboard shortcuts",
            "where": "dconf: " + cls.KEY_PATH_BASE + "0/, halo1/, …",
            "present": bool(bindings), "stale": bool(stale),
            "detail": ("; ".join(sorted({b["command"] for b in stale}))
                       if stale else "")})
        return items

    @classmethod
    def repair(cls, keys: list[str] | None = None) -> list[str]:
        """Rewrite whatever is present but wrong. Returns what was put right.

        Only ever repairs, never creates: a piece that is absent stays absent,
        because absent is what its switch being off looks like.
        """
        fixed = []
        for item in cls.audit():
            if not (item["present"] and item["stale"]):
                continue
            key = item["key"]
            if key == "launcher":
                if cls.install_launcher():       # rewrites the icon as well
                    fixed.append(item["what"])
            elif key == "icon":
                # install_icon(), not install_launcher(): the icon can be stale
                # while the launcher is deliberately switched off, and repairing
                # one must not create the other.
                if cls.icon_file().exists():
                    cls.install_icon()
                    fixed.append(item["what"])
            elif key == "autostart":
                if cls.set_autostart(True):
                    fixed.append(item["what"])
            elif key == "shortcuts":
                # Re-point the keys that are live now, whichever slot they came
                # from, so a hand-made one is repaired rather than orphaned.
                live = keys if keys is not None else cls.active_shortcuts()
                for binding in cls.foreign_bindings():
                    cls.drop_binding(binding["path"])
                if live and cls.set_shortcuts(live):
                    fixed.append(item["what"])
        return fixed

    # ── inventory & removal, so nothing is a mystery ─────────────────────
    @classmethod
    def installed_items(cls) -> list[tuple[str, str, bool, str]]:
        """(what, where, present, how to undo it) for every file we create."""
        launcher = cls.APPS_DIR / cls.DESKTOP_ID
        autostart = cls.AUTOSTART_DIR / f"{APP_ID}-autostart.desktop"
        icon = cls.icon_file()
        return [
            ("Application launcher", str(launcher), launcher.exists(),
             "Switch off “Add to Applications”, or “Remove Halo’s setup”."),
            ("Start-at-login entry", str(autostart), autostart.exists(),
             "Switch off “Start at login”."),
            ("Application icon", str(icon), icon.exists(),
             "Removed together with the launcher."),
            ("Keyboard shortcuts", "dconf: " + cls.KEY_PATH_BASE + "0/, halo1/, …",
             cls.shortcut_active(),
             "Untick it under “Keys that open Halo”, or use the × on a key of "
             "your own. They are also editable at Settings ▸ Keyboard ▸ View "
             "and Customize Shortcuts ▸ Custom Shortcuts."),
            ("Settings file", str(CONFIG_FILE), CONFIG_FILE.exists(),
             "Deleted by “Remove Halo’s setup”."),
            ("Search history", str(HISTORY_FILE), HISTORY_FILE.exists(),
             "“Clear all” in the history list, “Forget all searches” in the "
             "menu, or leave it to the retention setting."),
            ("Cookies, cache, captures", str(DATA_DIR), DATA_DIR.exists(),
             "“Clear cookies and cache”, or “Remove Halo’s setup”."),
        ]

    @classmethod
    def remove_launcher(cls) -> None:
        """Drop the launcher and its icon, leaving shortcuts alone."""
        for path in (cls.APPS_DIR / cls.DESKTOP_ID, cls.icon_file()):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    @classmethod
    def remove_setup(cls, include_data: bool = False) -> list[str]:
        """Undo everything setup created. Returns what was actually removed."""
        removed: list[str] = []
        # Unconditional: never gate cleanup on a read-back that may be stale.
        had_shortcuts = bool(cls.active_shortcuts())
        # set_shortcuts([]) clears the hand-made slots as well as ours, which is
        # what "remove Halo's setup" has to mean: leaving a working Halo shortcut
        # behind would leave one nothing in this menu can reach afterwards.
        cls.set_shortcuts([])
        if had_shortcuts:
            removed.append("keyboard shortcuts")
        for label, path in (("launcher", cls.APPS_DIR / cls.DESKTOP_ID),
                            ("autostart entry",
                             cls.AUTOSTART_DIR / f"{APP_ID}-autostart.desktop"),
                            ("icon", cls.icon_file())):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(label)
            except Exception:
                pass
        if include_data:
            for label, path in (("settings", CONFIG_DIR), ("browsing data", DATA_DIR)):
                try:
                    if path.exists():
                        shutil.rmtree(path)
                        removed.append(label)
                except Exception:
                    pass
        return removed


# ══════════════════════════════════════════════════════════════════════════
#  Screen-region capture (Circle to Search)
# ══════════════════════════════════════════════════════════════════════════

def _discard_portal_capture(src: Path) -> None:
    """Delete the file the screenshot portal just made for us.

    GNOME's portal writes the capture into ~/Pictures/Screenshots as a side
    effect. The user asked to search an image, not to keep a screenshot, so it
    would otherwise litter that folder on every Circle to Search. Deliberately
    narrow: only the screenshots folder and the usual temp locations are ever
    touched, never an arbitrary path handed to us.
    """
    try:
        resolved = src.resolve()
        allowed = [Path(p).resolve() for p in (
            "/tmp", "/var/tmp", "/run", HOME / ".cache") if Path(p).exists()]
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        if pictures:
            shots = Path(pictures) / "Screenshots"
            if shots.exists():
                allowed.append(shots.resolve())
            allowed.append(Path(pictures).resolve())
        if any(resolved.is_relative_to(root) for root in allowed):
            resolved.unlink(missing_ok=True)
    except Exception:
        pass


def capture_region(callback) -> None:
    """Interactive area capture through the XDG desktop portal.

    GNOME shows its own selection UI, so this works identically on Wayland
    and X11 and needs no screenshot tool installed. callback(path|None).
    """
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception:
        callback(None)
        return

    token = "halo_" + uuid.uuid4().hex[:12]
    state = {"sub": 0, "done": False}

    def finish(path: str | None) -> None:
        if state["done"]:
            return
        state["done"] = True
        if state["sub"]:
            try:
                conn.signal_unsubscribe(state["sub"])
            except Exception:
                pass
        callback(path)

    def on_response(_c, _sender, _path, _iface, _signal, params) -> None:
        try:
            code, results = params.unpack()
            uri = results.get("uri") if code == 0 else None
            if not uri:
                finish(None)
                return
            src = Path(urllib.parse.unquote(urllib.parse.urlparse(uri).path))
            _own_dir(DATA_DIR)
            dest = DATA_DIR / "capture.png"
            shutil.copyfile(src, dest)
            _discard_portal_capture(src)
            finish(str(dest))
        except Exception:
            finish(None)

    try:
        options = {"interactive": GLib.Variant("b", True),
                   "handle_token": GLib.Variant("s", token),
                   "modal": GLib.Variant("b", True)}
        reply = conn.call_sync(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Screenshot", "Screenshot",
            GLib.Variant("(sa{sv})", ("", options)),
            GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None)
        handle = reply.unpack()[0]
        state["sub"] = conn.signal_subscribe(
            "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
            "Response", handle, None, Gio.DBusSignalFlags.NONE, on_response)
        GLib.timeout_add_seconds(180, lambda: (finish(None), False)[1])
    except Exception:
        finish(None)


# ══════════════════════════════════════════════════════════════════════════
#  The popup
# ══════════════════════════════════════════════════════════════════════════

class PanelClamp(Gtk.Widget):
    """A one-child holder whose natural height is its minimum.

    WebKitWebView answers measure() with the height of the whole *document*, not
    of the view: a Google image grid measured 3982px natural against a 260px
    minimum. Halo's toplevel is set_resizable(False), so GTK sizes it to its
    natural height and mutter then trims that to the work area — which is exactly
    the vertical sizing bug. Measured, on a 1920x1001 screen with a 969px work
    area: one Ctrl+↑ took the window from 721px at y=283 to the full 969px pinned
    at y=32, and every Ctrl+↓ after that walked panel_height down in the config
    while the window stayed 969, because set_size_request() only ever moved the
    *minimum* and natural never came back down. Image results hit it first and
    hardest for the dull reason that those documents are the tallest ones Google
    serves; a short text page can sit under the panel height and never show it.

    Clamping natural to minimum makes the panel exactly as tall as expand() and
    _resize_panel() asked for, so the window tracks its own size request in both
    directions. Only the vertical axis is touched: the WebView asks for nothing
    unreasonable horizontally, and the pill decides the panel's width anyway.
    """
    __gtype_name__ = "HaloPanelClamp"

    def __init__(self, child: Gtk.Widget) -> None:
        super().__init__()
        self._kid: Gtk.Widget | None = child
        child.set_parent(self)

    def do_measure(self, orientation, for_size):
        if self._kid is None:
            return 0, 0, -1, -1
        minimum, natural, min_base, nat_base = self._kid.measure(orientation, for_size)
        if orientation == Gtk.Orientation.VERTICAL:
            natural = minimum
        return minimum, natural, min_base, nat_base

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        if self._kid is not None:
            self._kid.allocate(width, height, baseline, None)

    def do_dispose(self) -> None:
        # GTK warns loudly about a widget finalised with children still parented.
        if self._kid is not None:
            self._kid.unparent()
            self._kid = None
        Gtk.Widget.do_dispose(self)


class WidthClamp(Gtk.Widget):
    """A one-child holder that asks for no width on its child's behalf.

    The sibling of PanelClamp, on the other axis and for the same reason: a
    child of the pill must not get to decide how wide the pill is allowed to be.

    What it is for. The apron below the pill is a GtkFixed, and GtkFixed
    measures itself to CONTAIN its children wherever they have been put — while
    ParkedArrow._move() deliberately puts the disc at the pill's right-hand
    end, x = width - (SIZE + INSET). So the moment the disc is placed for a
    760px pill the apron's minimum width becomes 721 + 36 = 757, the Box and
    the Overlay above it inherit it, and so does the toplevel. GTK will not
    allocate a window under its own minimum however small a width
    set_default_size() is handed, so the pill could never be narrowed again:
    the left corner is dragged and nothing moves at all.

    And it ratchets. Widen the pill to 1200 first and the disc is re-placed at
    1161, so the floor becomes 1197 and stays there, because _move() only ever
    runs for the width the pill has at the time. That is the "minimum suddenly
    way too big" half of the same fault.

    It only bites once the disc has been placed, which is why it looks like it
    has to do with closing and reopening with a page open: that is exactly when
    _update_arrow() puts the disc on screen. Reproduced in fifteen lines of
    plain GTK — a GtkFixed with one 24px child put at x=721 reports a minimum
    width of 745, and so does the window around it.

    Fixing it in the apron itself is not possible from the widget: GtkFixed
    measures through a GtkFixedLayout, so a do_measure() override on a
    Gtk.Fixed subclass is a method nothing calls (measured: the answer did not
    change), and overriding GtkFixedLayout.do_measure() crashed the process.
    Wrapping is what PanelClamp already does here and it works.

    The child is still allocated the full width, so the disc lands exactly
    where it is put — verified, x=721 before and after. Only the asking changes.
    The vertical axis is chained through untouched: the apron's height is a
    real requirement and it still states it.
    """
    __gtype_name__ = "HaloWidthClamp"

    def __init__(self, child: Gtk.Widget) -> None:
        super().__init__()
        self._kid: Gtk.Widget | None = child
        child.set_parent(self)

    def do_measure(self, orientation, for_size):
        if self._kid is None or orientation == Gtk.Orientation.HORIZONTAL:
            return 0, 0, -1, -1
        return self._kid.measure(orientation, for_size)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        if self._kid is not None:
            self._kid.allocate(width, height, baseline, None)

    def do_dispose(self) -> None:
        if self._kid is not None:
            self._kid.unparent()
            self._kid = None
        Gtk.Widget.do_dispose(self)


class ParkedArrow:
    """The ↓ back-to-the-page button, in an apron below the pill.

    Collapsing the panel leaves the page loaded and one keystroke away, and the
    only thing that ever said so was the manual. The obvious fix is a chip in the
    pill, and that is the one thing the pill has no room for: every chip on that
    row is width taken from the query, permanently, to advertise a state that
    only sometimes exists.

    So it hangs underneath instead — a strip of the pill's own window, twenty-
    four pixels across and cut to a circle, parked just under the bottom-right
    corner. That is where the collapse chip was standing a moment earlier, so
    the button reads as coming off the panel as the panel folds away, and it
    costs the pill nothing. It exists only while there is a page to go back to,
    and it is a little smaller than the chip it stands in for — SIZE says why.

    **It used to be a window of its own, and three separate bugs were that one
    decision.** A second toplevel has to be told where the first one went, and
    told over a different X connection, and the window manager is under no
    obligation to do either in any particular order:

    - Dragging the pill moved a window this one only found out about afterwards,
      so the disc lurched along behind the pointer and was left standing where
      the pill used to be when the drag stopped moving. There is no rate at
      which chasing a window fixes that; the disc is always a frame behind at
      best, and behind by the whole drag at worst.
    - A map on GDK's connection could beat a shape on ours, and what got drawn
      was the shape the window still had — its last one, a whole disc, at the
      start of a slide, which is *behind* the pill. It was a dock, so it painted
      on top. Reported three times as "it spawns on top of the pill".
    - The same race on a brand new window, which has no shape at all, drew the
      plain opaque square. Reported as "a clear rectangle around the button".

    None of the three survives being one window. The pill and the disc move
    together because there is nothing to move; the disc cannot be painted over
    the pill because the pill is a sibling drawn after it *and* the apron is
    Overflow.HIDDEN, so a disc that has not emerged yet is clipped away by GSK
    in the same frame that draws it; and there is no second window to catch
    without a shape. What is left for the X shape to get wrong is a crescent of
    background below the pill, and set_rounded_shape() bounds even that.

    Two things it deliberately does not do. It never takes focus, so clicking it
    cannot disturb the caret in the field — and now that it is inside the pill's
    own window there is no focus to take, which is a whole handler gone. And
    nothing about it animates in CSS or on a frame clock: .halo-glow's comment
    records what an always-on-top window asking for frames for ever costs when
    the compositor stops answering — 981ms of blocking per cycle, shared with
    every other window in the process. The slide below is a bounded timeout that
    stops when it arrives.
    """

    # 24, not the chip's 30: this one sits below the pill rather than inside it,
    # so its background is the whole of its presence. A 16px glyph in 24px
    # leaves four pixels of surround — visible enough to click, quiet enough not
    # to read as a second window sitting there.
    SIZE = 24
    GAP = 7             # below the pill's bottom edge, once it has settled
    INSET = 15          # keeps its centre under the ⋯ / × column
    STEP_MS = 16
    # How long _move_when_sized() waits for the toplevel to be allocated before
    # it gives up. Generous next to the one frame this normally takes, because
    # the cost of waiting is a disc that is not on screen yet and the cost of
    # giving up early is one parked at the wrong end of the pill.
    SIZED_WAIT_MS = 1000

    # The page is still loaded and ↓ only has to show it again.
    WARM_TIP = "Bring the page back  ·  ↓"
    # The engine was handed back while the pill sat idle, so the same ↓ means a
    # fetch rather than a reveal. Worth saying, since one is instant and the
    # other is not — and worth saying quietly, which is what the dimmer ink in
    # .halo-parked-cold is for. A second glyph would make a button whose whole
    # virtue is not asking for attention start asking for it.
    COLD_TIP = "Load the page again  ·  ↓"

    def __init__(self, parent: "HaloWindow") -> None:
        self.parent = parent
        self.apron: Gtk.Fixed | None = None
        self.disc: Gtk.Box | None = None
        self.button: Gtk.Button | None = None
        self._timer = 0
        self._sizing = 0          # waiting for the toplevel to have a width
        self._visible = False
        self._cold = False
        # Where the disc's top edge sits inside the apron, in logical pixels
        # from the apron's own top — which is the pill's bottom edge. GAP at
        # rest; -SIZE when it is wholly behind the pill and invisible.
        self._offset = float(-self.SIZE)
        # What the X shape was last cut for, which is deliberately not the same
        # number — see shape_disc().
        self._shaped_offset: float | None = None

    # ── the widgets ──────────────────────────────────────────────────────
    def build(self) -> Gtk.Widget:
        """The apron below the pill, and the disc inside it."""
        button = Gtk.Button(icon_name="pan-down-symbolic")
        button.add_css_class("halo-parked-btn")
        button.set_can_focus(False)
        button.connect("clicked", self._on_click)
        # The disc is a box rather than the apron's own background: the apron is
        # the full width of the pill and almost all of it is shaped away, so
        # what paints the circle has to be exactly the circle.
        disc = Gtk.Box()
        disc.add_css_class("halo-parked")
        disc.append(button)
        disc.set_size_request(self.SIZE, self.SIZE)

        apron = Gtk.Fixed()
        # The whole of the guarantee that the disc cannot appear on the pill,
        # and the reason this is a Fixed rather than anything that lays out.
        # A child at a negative y is clipped by GSK in the frame that draws it,
        # so "still behind the pill" is not a position that has to be kept in
        # step with anything — it is a position that does not get drawn. The
        # slide can then start from wholly hidden without a single frame in
        # which the timing has to be right.
        apron.set_overflow(Gtk.Overflow.HIDDEN)
        apron.set_size_request(-1, self.SIZE + self.GAP)
        apron.put(disc, 0, float(-self.SIZE))
        # Always in the layout, even with no page to go back to, and that is a
        # deliberate 31px of window that is usually showing nothing.
        #
        # The alternative — putting the apron in only when the disc is wanted —
        # makes the toplevel 31px taller and shorter again on every drawer that
        # opens and closes, which while somebody is typing is every keystroke
        # that changes the suggestion list. Every frame in which the height
        # changes is a frame the X11 clip disagrees with the window (see
        # _watch_surface), so it is a frame with square, rim-coloured corners.
        # Measured: it took a typing burst from 1.8 motion frames per keystroke
        # to 2.4. Costing 31px of invisible, click-through window is much the
        # cheaper of the two — and it is the same 31px the disc's own window
        # used to occupy anyway.
        #
        # Invisible because nothing paints it: the toplevel is ARGB and every
        # node that could give it a background is transparent, so an apron with
        # no disc in it is alpha 0 and the desktop shows through. The input
        # region does not include it either, so clicks go to whatever is behind.
        #
        # The apron carries the disc and the panel's grab band, the band hung in
        # an Overlay wrapped round this — see HaloWindow._build_ui. That
        # arrangement was tried once before and reverted, on the finding that a
        # strip below the pill's painted edge could not be made clickable by the
        # X input shape alone. The finding was right and is still right: the
        # effective region is ShapeInput ∩ ShapeBounding. The mistake was
        # setting a bounding shape at all. There is none now, so the
        # intersection has nothing to remove and the band is live —
        # set_rounded_shape() carries both measurements.
        self.apron = apron
        self.disc, self.button = disc, button
        self._paint_cold()
        # Wrapped, so that where the disc sits cannot become a floor under the
        # pill's width. self.apron stays the Fixed — everything that positions
        # the disc or measures the apron's height goes on talking to it, and
        # only what the layout is ASKED for changes. See WidthClamp.
        return WidthClamp(apron)

    def set_cold(self, cold: bool) -> None:
        """Say whether ↓ will reveal a live page or have to fetch it again."""
        if cold == self._cold:
            return
        self._cold = cold
        self._paint_cold()

    def _paint_cold(self) -> None:
        """Put the difference on the button, whenever there is a button."""
        if self.button is None:
            return
        if self._cold:
            self.button.add_css_class("halo-parked-cold")
        else:
            self.button.remove_css_class("halo-parked-cold")
        self.button.set_tooltip_text(self.COLD_TIP if self._cold
                                     else self.WARM_TIP)

    def _on_click(self, *_a) -> None:
        # Exactly what ↓ on the bare pill does, and nothing else: this is a
        # second door onto that one behaviour, not a behaviour of its own.
        self.parent.expand()

    # ── what the window has to know about it ─────────────────────────────
    def apron_height(self) -> int:
        """How much taller than the pill the window stands, in device pixels.

        Always the same figure, because the apron is always there — see build().

        The apron's *allocation*, not its nominal height, and the difference is
        the difference between a shape that fits the window and one two pixels
        short of it. GTK allocates the toplevel and its children in the same
        pass, so this figure and the window's own height can never disagree
        about the same frame — subtracting one from the other therefore gives
        the slab exactly, including whatever the window itself contributes above
        and below its child. Measuring the slab widget instead looked equivalent
        and was 2px out, because the window is not only its child.

        The nominal figure stands in for the one frame between being made
        visible and being allocated, and only in the direction that is safe: it
        says the apron is there before it is, which makes the slab shorter, and
        a clip that lags the window has always been the harmless way round.
        """
        if self.apron is None:
            return 0
        allocated = self.apron.get_height()
        if allocated > 0:
            return allocated * max(1, self.parent._scale())
        return (self.SIZE + self.GAP) * max(1, self.parent._scale())

    def shape_disc(self) -> tuple[int, int, int] | None:
        """The circle to cut into the window's shape: (x, y, side), device px.

        y is measured from the apron's top edge, so the window adds its own pill
        height to it and set_rounded_shape() can bound the result against that
        same figure.

        Cut for where the disc has been *painted*, never for where it is going.
        The shape goes out on our X connection and the frame on GDK's, and a
        shape that leads the paint exposes window background — below the pill
        that is a grey crescent rather than a disc on the slab, but it is the
        same mistake in the same direction, and the rule that has always held
        here is that a clip may lag and may never lead. One 16ms step behind is
        not visible; the reveal takes about nine of them.

        None while the disc is wholly behind the pill, which is both frames of
        the answer that matter: nothing is cut, so nothing can be shown.
        """
        if not self._visible or self.apron is None:
            return None
        off = self._shaped_offset
        if off is None or off <= -self.SIZE:
            return None
        scale = max(1, self.parent._scale())
        width = max(0, self.parent.get_width()) * scale
        # Floored like the width beside it. Before the first allocation, or on a
        # window narrower than the disc's own inset, this goes negative — and
        # set_rounded_shape packs it into an XRectangle's c_short, where a
        # negative x is perfectly legal and silently cuts the circle off the
        # LEFT edge instead of putting it on the right. Twice as far negative at
        # 2x as at 1x. Guarded in practice by _visible and _shaped_offset, so
        # this is a floor rather than a fix.
        x = max(0, width - (self.SIZE + self.INSET) * scale)
        return int(x), int(round(off * scale)), self.SIZE * scale

    # ── showing, sliding, hiding ─────────────────────────────────────────
    def show(self, animate: bool = True) -> None:
        if self.apron is None:
            return
        if self._visible:
            if self._timer:
                return              # already on its way in; let it arrive
            self._settle()
            return
        self._visible = True
        duration = self.parent._drawer_ms() if animate else 0
        # Behind the pill, and shaped to nothing, before the apron is anything
        # the window has to make room for. There is no frame in which the disc
        # exists and its position has not been decided.
        self._offset = float(-self.SIZE)
        self._shaped_offset = float(-self.SIZE)
        self._move()
        # No resize to follow — the apron is always in the layout — so the shape
        # is all there is to bring up to date, and _reshape() does that itself.
        self.parent._shape_now()
        if duration > 0:
            self._slide(duration)
        else:
            self._settle()

    def _move(self) -> None:
        """Put the disc where _offset says, at the pill's right-hand end.

        The same width shape_disc() reads, deliberately: the disc is painted by
        GTK and the circle is cut by us, and the two ends of that have to agree
        about how wide the pill is or the hole is not over the disc.

        Which is exactly what went wrong. get_width() is 0 until the toplevel
        has been allocated, and show() can be reached from an idle that beats
        the allocation — measured on the Circle to Search path, where _move()
        ran 6ms after the window came back, read 0, and floored the disc at
        x=0. shape_disc() is read later, by which time the width is real, so it
        cut the circle at x=721: the disc was sitting at the left-hand end of
        the pill with the hole in the shape at the right. And it stayed that
        way, because nothing calls _move() again until the disc is hidden and
        shown or the pill is dragged — which is why dragging Halo "fixed" it.

        So: no width, no placement. Ask again on the next frame instead.
        """
        if self.apron is None or self.disc is None:
            return
        width = self.parent.get_width()
        if width <= 0:
            self._move_when_sized()
            return
        x = float(max(0, width - (self.SIZE + self.INSET)))
        self.apron.move(self.disc, x, float(self._offset))

    def _move_when_sized(self) -> None:
        """Place the disc as soon as the toplevel has a width to place it by.

        A bounded timeout for the reason the slide uses one — see the class
        docstring on what asking the compositor for frames for ever costs a
        window that may not be in front. The bound matters twice over here,
        because this runs while the pill is being shown: if a width never
        arrives, this has to stop asking rather than poll for the session.
        """
        # hide() parks the disc behind the pill through _move() too, and a disc
        # that is not on screen has nothing to be misplaced about.
        if self._sizing or not self._visible:
            return
        deadline = GLib.get_monotonic_time() + self.SIZED_WAIT_MS * 1000

        def again() -> bool:
            if self.apron is None or not self._visible:
                self._sizing = 0
                return False
            if self.parent.get_width() > 0:
                self._sizing = 0
                self._move()
                return False
            if GLib.get_monotonic_time() > deadline:
                self._sizing = 0
                return False
            return True

        self._sizing = GLib.timeout_add(self.STEP_MS, again)

    def _reshape(self, offset: float) -> None:
        self._shaped_offset = float(offset)
        self.parent._shape_now()

    def _slide(self, duration: int) -> None:
        """Ease the disc out from behind the pill."""
        self._stop()
        began = GLib.get_monotonic_time()
        start, rest = float(-self.SIZE), float(self.GAP)

        def step() -> bool:
            if not self._visible:
                self._timer = 0
                return False
            t = (GLib.get_monotonic_time() - began) / (duration * 1000.0)
            if t >= 1.0:
                self._timer = 0
                self._settle()
                return False
            # Ease-IN-out cubic, and the "in" half is the whole point.
            #
            # This was ease-out — "quick away from the pill, gentle as it
            # settles" — which is exactly backwards for something emerging from
            # behind an occluder. Ease-out spends its speed at the start, while
            # the disc is still clipped away to nothing, so the entire reveal
            # happened between two frames and the settle everybody could see was
            # the last three pixels. Measured: the first frame the user actually
            # saw already had 39% of the travel done and 12 of 24px showing at
            # once, which is the "it just appears" this is here to fix.
            eased = (4.0 * t ** 3 if t < 0.5
                     else 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0)
            # Shape for the frame already on screen, then move for the next one.
            # That ordering is the lag, and it is the whole of it.
            self._reshape(self._offset)
            self._offset = start + (rest - start) * eased
            self._move()
            return True

        # A timeout rather than a frame-clock tick, deliberately. A tick callback
        # keeps asking the compositor for frames, and the pill is by definition a
        # window that may not be in front; its own rim animation already had to be
        # frozen for that reason. A bounded timeout cannot get into that state.
        self._timer = GLib.timeout_add(self.STEP_MS, step)

    def _settle(self) -> None:
        """Arrive: the disc at rest, and the shape one step behind it."""
        self._stop()
        self._offset = float(self.GAP)
        self._move()
        self._timer = GLib.timeout_add(self.STEP_MS, self._settle_shape)

    def _settle_shape(self) -> bool:
        self._timer = 0
        if self._visible:
            self._reshape(float(self.GAP))
        return False

    def _stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        # The wait for a width goes with it: whatever calls _stop() either has
        # no disc to place any more, or is about to call _move() itself and
        # will start the wait again if it is still needed.
        if self._sizing:
            GLib.source_remove(self._sizing)
            self._sizing = 0

    def hide(self) -> None:
        """Take the disc away, and the apron with it."""
        self._stop()
        was, self._visible = self._visible, False
        self._offset = float(-self.SIZE)
        self._shaped_offset = None
        if self.apron is None or not was:
            return
        self._move()
        self.parent._shape_now()

    def destroy(self) -> None:
        self._stop()
        self._visible = False


class HaloWindow(Gtk.ApplicationWindow):

    def __init__(self, app: "HaloApp") -> None:
        super().__init__(application=app)
        self.app = app
        self.expanded = False
        # The right-click menu is rebuilt from scratch each time it opens, out
        # of GActions that have to outlive the call that made them.
        self._ctx_actions = []                 # this menu's actions, kept alive
        self._page_selection = ""              # what SELECTION_WATCH_JS last reported
        self._downloads = []                   # saves in flight, so not collected
        self._own_download = 0                 # depth: a fetch Halo asked for
        self.pending_lens: str | None = None   # image awaiting Lens' file chooser
        # The picture clipped to the search field: {"path", "name", "uri"} or
        # None. Distinct from pending_lens, which is only ever set while a
        # hand-off is in flight — this outlives the search, the panel and the
        # process, because it is what the next query is *about*.
        self.attached: dict | None = None
        # Bumped by every attachment. A by-URL chip fetch is asynchronous, and
        # its answer must not overwrite a picture attached while it was in the
        # air: DATA_DIR holds ONE attachment under one fixed basename, so a
        # late download would unlink or overwrite the very file a pending
        # upload is about to be asked for. The token is how a stale answer
        # recognises itself.
        self._attach_serial = 0
        # Which attachment the results now on screen belong to, by path. What
        # tells a second query against the same picture (a refinement, one
        # navigation) from a query against a new one (a fresh upload, ten
        # seconds). None whenever nothing on screen answers for the attachment.
        self._lens_shown_for: str | None = None
        # The exact address of those results, so a later query can tell "the
        # same page with a different query" from "somewhere the user has since
        # clicked". See _lens_surface() for why the address alone is not enough.
        self._lens_result_uri: str | None = None
        # A by-URL image search is out and its results have not arrived yet:
        # the monotonic microsecond it stops being worth waiting for, or 0.
        # pending_lens says the same thing for the upload route; this route
        # uploads nothing, so it needs its own word for "expecting a landing".
        # Without one, ANY page carrying vsrid= was adopted as the attached
        # picture's results — including the All tab, which is how the bug this
        # replaced got in a second time.
        #
        # A DEADLINE rather than a flag, because a flag never expires. The
        # by-URL route does not always reach a results page at all — Google
        # answers /?olud, or a CAPTCHA, or refuses to fetch the image — and a
        # flag left standing would adopt whatever marker page the user happened
        # to open minutes later, which is the same hole again by a longer road.
        self._awaiting_lens_url_until = 0
        self.attach_button: Gtk.Button | None = None
        self.attach_thumb: Gtk.Image | None = None
        self.lens_attempts = 0
        self._lens_await_load = False          # holding the cover over the hand-off
        # A load we asked for that has not appeared on screen yet. WebKit keeps
        # painting the page before it until the new one commits, so nothing may
        # lift the loading cover while this is set — see _navigate().
        self._nav_pending = False
        self._nav_uri = ""            # which load that is, so a stray failure
                                      # cannot be mistaken for its own
        # Committed, but not yet proven to be on screen. Between these two the
        # view is still painting the page before it, which is the flash the
        # loading cover exists to hide — see _arm_paint_wait().
        self._await_paint = False
        self._paint_timer = 0
        # Every page starts at its top, and SCROLL_JS corrects this the moment it
        # is not. True by default so ↑ can still leave a page that never reports —
        # being unable to get back to the field is the worse failure.
        self._page_at_top = True
        # Find-in-page borrows the pill's own field rather than opening a box of
        # its own, so it has to remember what the field was holding.
        self.finding = False
        self._find_query = ""
        self._find_label = ""
        # Whether the page in the view is Google's, and therefore whether the
        # compact-results rules belong on it — see _apply_user_styles().
        self._styles_google = True
        self.lens_text = ""                    # words to refine the image search
        # First run only: hold searches until the Google handshake is done.
        self.warm_pending = False
        self.deferred_query: str | None = None
        self.suggest_items: list[str] = []
        self.suggest_rows: list[tuple] = []      # the same, plus where each came from
        # What is actually built into suggest_list right now. Not the same thing
        # as suggest_rows any more: the rows outlive the list being closed, so
        # the drawer has something to slide shut with — see _shut_suggestions().
        self._suggest_built: list[tuple] = []
        # The history dropdown: open or not, and the entries currently listed in
        # it, which is the filtered view rather than the whole store.
        self.history_open = False
        self.history_rows: list[dict] = []
        self._hist_clear_armed = False
        self.last_query = ""
        # Google's silent correction, and the step back it does not leave behind.
        # One slot, never a stack: see _history(). None when the view is not on
        # either half of a corrected pair.
        self._spell_slot: dict | None = None
        self._suggest_timer = 0
        self._geom_timer = 0
        self._geom_until = 0
        self._xid_surface = None      # surface the cached xid belongs to
        self._xid_cache: int | None = None
        self._info_window: Gtk.Window | None = None
        # One live timer each, so a redirect chain cannot stack them up.
        self._cover_timer = 0         # hard limit on the loading cover
        self._cover_deadline = 0      # monotonic µs the cover may never outlive
        self._lens_click_timer = 0
        self._lens_retry_timer = 0
        self._lens_text_timer = 0
        # The WebKit half is built on first use, so a resident copy that has not
        # searched yet does not carry a browser engine around. Everything that
        # touches these has to cope with None.
        self.web: WebKit.WebView | None = None
        self.session: WebKit.NetworkSession | None = None
        self.ucm: WebKit.UserContentManager | None = None
        self.web_overlay: Gtk.Overlay | None = None
        # Where the panel was when the engine was handed back, so ↓ can reopen it.
        self._parked_uri: str | None = None
        # ...and the history behind it, serialised. A released engine is a
        # *destroyed* WebView, and a fresh one starts with an empty back/forward
        # list — which silently disarms WebKit's own back/forward swipe gesture,
        # because ViewGestureController::canSwipeInDirection is nothing but
        # `return !!backForwardList->backItem()`. Measured: two pages loaded,
        # can_go_back True, list length 2; release_engine() then _reopen_parked()
        # put the same page back on screen with can_go_back False, list length 1
        # and back_item None, and the swipe had nothing left to answer. The page
        # looked identical, so the only thing the user could see was a gesture
        # that used to work and now does nothing.
        self._parked_state: GLib.Bytes | None = None
        self._idle_timer = 0
        self._shaped_key = None       # last input region we actually sent to X
        self._extents_key = None      # last frame extents we told the WM
        self._last_size = None        # what _sync_geometry last measured
        self._suggest_hold = 0        # a brief empty answer, not yet acted on
        self._suggest_hold_since = 0  # when the first of a run of them came in
        self._suggest_pending = ""    # a text whose answer has not come back yet
        self._squeeze_floor = None    # foot of the work area, per animation
        self._squeeze_seen = None     # the size the squeeze was last checked at
        self._squeeze_left = 0        # frames of that check still owed
        self._pill_y: int | None = None   # where the user wants the pill's top
        self._anchor_timer = 0
        self._copy_timer = 0          # the copy button's brief "copied" state
        # Set while the "Add a shortcut" dialog is listening, so _on_key hands it
        # the keystrokes instead of acting on them. See _on_add_shortcut.
        self._accel_capture = None
        self._accel_dialog = None     # the "Add a shortcut" dialog, while open
        self._placed_target = None    # where the last WM.move asked for
        self._reveal_deadline = 0
        # Where to come back to after a hide that is not hide_popup() — see
        # on_circle_to_search() and _resume_placement().
        self._resume_at = None
        self._bar_dragging = False    # a drag started on the pill's own bar
        self._swallow_click = False   # ...and the release that ends it is not a click
        # (zone, width, panel height, window position) while a bottom-edge or
        # corner resize is in flight, and the cursor that grab last asked for.
        self._resizing: tuple | None = None
        self._resize_cursor: str | None = None
        # Where the right edge stood when a left-corner drag began, and the
        # window width the pin has already been applied for — see
        # _pin_right_edge().
        self._resize_right: int | None = None
        self._pinned_w: int | None = None
        # Where the grip this drag is on stood when the press landed, in the
        # window's own coordinates — see _gesture_origin_x().
        self._resize_ox: float | None = None
        # The one width the pin's grace is allowed to act on, and the place
        # the drag left the window in — see _pin_grace_allows().
        self._pin_wait_w: int | None = None
        self._pin_wait_x: int | None = None
        # The window the pin holds an edge of, the frame clock and handler id
        # it watches the width arrive on, and the grace timer that keeps that
        # watch up for a moment after the button comes up. See _pin_soon().
        self._pin_xid = 0
        self._pin_clock = None
        self._pin_hid = 0
        self._pin_grace = 0
        # The shortcut, pressed twice — see toggle() and _double_tap().
        self._tapped_at = 0.0
        self._closed_with = ""
        # path -> texture (or None where the file would not decode), filled as
        # history rows are built and never written to disk. See _history_thumb.
        self._hist_thumbs: dict[str, Gdk.Texture | None] = {}
        # ...and the picture that was on the field beside those words, with
        # enough of the Lens bookkeeping to put it back as it was rather than
        # as a fresh attachment. See _stash_attachment().
        self._closed_with_image: dict | None = None
        self._shortcut_sync_pending = False
        self._arrow = ParkedArrow(self)   # the detached ↓, when a page is parked
        # A pending re-decision about the disc, and whether it should slide when
        # it is made. See _arrow_soon(), which is what these are for.
        self._arrow_idle = 0
        self._arrow_animate = False
        # A fold has been asked for and the disc should slide when it finishes.
        # Set before the fold starts, because with the instant reveal preset the
        # revealer completes inside set_reveal_child() and the handler that
        # reads this runs before the next statement would have set it.
        self._arrow_slide_pending = False
        self._outputs_cache = None    # mutter's per-output scales, cached
        self._cursor_px = None        # cursor size we last decided on
        # What was last *handed* to GDK, as (theme, size), or None while GDK is
        # still holding whatever GtkSettings pushed onto it. This is not the
        # same thing as _cursor_px, and conflating the two is what let the
        # pointer stay the wrong size: set_cursor_theme() does not write back
        # into gtk-cursor-theme-size, so that property is no evidence at all
        # about what GDK is currently drawing with.
        self._cursor_live: tuple[str, int] | None = None
        # The monitors whose geometry is being followed, so that plugging one in
        # does not double up handlers on the ones already there.
        self._watched_monitors: set = set()
        self._page_bg = remembered_page_bg()   # what the page last painted
        self._bg_provider: Gtk.CssProvider | None = None
        # The rim's sweep is frozen whenever Halo is not the active window, and
        # these are what let it be picked up again where it was left. See
        # _set_rim_running().
        self._rim_provider: Gtk.CssProvider | None = None
        self._rim_phase = 0.0         # seconds into the 9s cycle when it stopped
        self._rim_since: int | None = None   # monotonic µs it was last started
        self._rim_alt = False         # which of the two twin keyframes is on
        # ...and the fade between lit and drained. The phase is held still for
        # the whole of it — see _set_rim_running().
        self._rim_fade: str | None = None      # "out", "in", or nothing
        self._rim_fade_id: int | None = None   # the timeout that settles it
        self._rim_fade_t0 = 0                  # monotonic µs it began
        self._rim_fade_from = ""               # gradient it started on
        self._rim_fade_to = ""                 # gradient it ends on
        self._rim_fade_s = 0.0                 # how long it runs

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_title(APP_NAME)
        self.add_css_class("halo-window")
        self.set_default_size(CFG["window_width"], -1)

        self._build_ui()
        # The menu exists now, so it is safe to start following the dconf keys.
        DesktopIntegration.watch_shortcuts(self._shortcuts_changed_outside)
        self._build_cover()
        self._wire_keys()
        # After the bar, because the stash is measured against a chip that has
        # to exist by the time ↓ puts a picture back on it.
        self._seed_stash()

        self.connect("notify::is-active", self._on_active_changed)
        self.connect("close-request", self._on_close_request)
        # Plugging a screen in or out, or changing its scale, invalidates both
        # the cached output list and whatever cursor size was chosen from it.
        self._watch_monitor_layout()
        # Changing the cursor theme or its size in Settings makes GTK push
        # XSettings' figure onto the GdkDisplay itself, which throws away the
        # size this window chose for its own monitor. Measured: after
        # set_cursor_theme(theme, 13) the display's live Xcursor default size
        # reads 13, and setting gtk-cursor-theme-size to 32 leaves it reading
        # 32. Nothing told Halo, and _cursor_px still claimed the old value was
        # in force, so it never asked again.
        try:
            _cursor_settings = Gtk.Settings.get_default()
            for _key in ("notify::gtk-cursor-theme-size",
                         "notify::gtk-cursor-theme-name"):
                _cursor_settings.connect(_key, self._cursor_theme_pushed)
        except Exception:
            pass

        # Ask for a cursor by name rather than inheriting the X root window's,
        # which can be left oversized after a resolution change.
        try:
            self.set_cursor(Gdk.Cursor.new_from_name("default", None))
        except Exception:
            pass

    # ── construction ────────────────────────────────────────────────────
    # Reveal timing.
    #
    # Measured on this machine: a 280ms panel reveal commits only six to eleven
    # distinct window sizes — the count moves with system load — out of the ~22
    # frames a 75Hz display offers, because the animation grows the TOPLEVEL and
    # mutter commits a resize of this window on the order of twenty times a second
    # whatever Halo does. Verified by disabling the X11 reshape and the geometry
    # ticker separately, neither of which changed the count. At 0ms it is two:
    # the collapsed size and the expanded one, with nothing in between to step.
    # So the steppiness is not something the app can smooth out by working
    # faster; the honest options are fewer, larger steps or none at all, and that
    # is a choice worth handing over rather than guessing at.
    REVEAL_PRESETS = [(280, "Smooth"), (180, "Quick"), (0, "Instant")]

    def _reveal_ms(self) -> int:
        return int(CFG["reveal_ms"])

    def _drawer_ms(self) -> int:
        """The suggestion and history drawers slide a fraction of the distance,
        so they take a fraction of the time — 140ms against the panel's 280 is
        the ratio they were built with."""
        return int(CFG["reveal_ms"]) // 2

    def _apply_reveal_speed(self) -> None:
        self.result_reveal.set_transition_duration(self._reveal_ms())
        self.suggest_reveal.set_transition_duration(self._drawer_ms())
        self.history_reveal.set_transition_duration(self._drawer_ms())

    def _build_ui(self) -> None:
        # The window is the slab plus the apron below it — the apron being
        # transparent window, which is what the panel's resize border stands in.
        # Two children, and the second is usually not there: the slab, and the
        # apron the detached ↓ hangs in. The apron is a sibling *after* the slab
        # rather than anything overlaid on it, which is what makes the pill draw
        # over a disc that has not emerged yet — a paint order rather than a
        # stacking request to a window manager, and the difference between those
        # two is three bugs' worth. See ParkedArrow.
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(shell)
        self.shell = shell

        glow = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        glow.add_css_class("halo-glow")
        # Wrapped in an Overlay again, and for a much smaller reason than the
        # last one. The band itself still hangs in the apron below the pill and
        # still costs the page nothing; what this carries is the two corner
        # grips, which have to reach a few rows UP into the slab or the corner
        # grab can only be taken from under the corner and never at it. An
        # Overlay measures only its main child, so the slab is the size it
        # always was and the corners cost the window no height.
        slab_wrap = Gtk.Overlay()
        slab_wrap.set_child(glow)
        shell.append(slab_wrap)
        self.slab_wrap = slab_wrap
        self.glow = glow

        # The apron, wrapped so the panel's grab band can hang in the rows
        # BELOW the pill's painted edge. A Gtk.Overlay measures only its main
        # child unless an overlay child is explicitly marked measured, so this
        # wrapper is exactly the apron's size and the band costs the window no
        # height of its own — see the band itself, further down.
        apron_wrap = Gtk.Overlay()
        apron_wrap.set_child(self._arrow.build())
        shell.append(apron_wrap)
        self.apron_wrap = apron_wrap

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.add_css_class("halo-body")
        # Clip to the CSS corner radius, otherwise the WebView paints square
        # corners over the bottom of the slab.
        body.set_overflow(Gtk.Overflow.HIDDEN)
        glow.append(body)
        self.body = body

        # ── the pill itself, doubling as the drag handle ──
        # Deliberately NOT a Gtk.WindowHandle, and that is a bug fix rather than
        # a preference. A WindowHandle asks the window manager to move the
        # window and tells the application nothing about it, so every drag
        # started anywhere on the pill's own bar left _pill_y holding the
        # position from *before* the drag. The next thing to run the geometry
        # ticker then restored the window to where it used to be — measured,
        # dragged from y=520 up to y=38 and put straight back to 520 the moment
        # the panel was collapsed, which is "drag it to the top, collapse, and
        # the pill jumps down" exactly. Only the entry's dead-space drag was
        # ever noticed, because that one is ours; this makes them both ours.
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("halo-bar")
        body.append(bar)
        bar_drag = Gtk.GestureDrag()
        bar_drag.connect("drag-update", self._on_bar_drag_update)
        bar_drag.connect("drag-end", self._on_bar_drag_end)
        bar.add_controller(bar_drag)
        self.bar = bar

        # The mark is the history button. Clicking Google's own logo to see what
        # you have looked up is where a hand goes looking for it, and the pill has
        # no room for a chip that would do nothing else.
        self.mark = self._brand_icon()
        self.mark.set_halign(Gtk.Align.CENTER)
        self.mark_button = Gtk.Button(child=self.mark)
        self.mark_button.add_css_class("halo-mark")
        self.mark_button.set_valign(Gtk.Align.CENTER)
        self.mark_button.set_can_focus(False)
        self.mark_button.set_tooltip_text("Your searches  ·  ↑")
        self.mark_button.connect("clicked",
                                 self._guard_click(lambda *_: self.toggle_history()))
        bar.append(self.mark_button)

        # Left of the field, which is where an attached picture sits in
        # Google's own search box. Built now and hidden; nothing is attached
        # yet, and a hidden child asks a GtkBox for no width at all, so the
        # pill is exactly the size it always was until a picture arrives.
        self.attach_button = self._build_attach_chip()
        bar.append(self.attach_button)

        self.entry = Gtk.Entry(hexpand=True)
        self.entry.add_css_class("halo-entry")
        self.entry.set_has_frame(False)
        self.entry.set_placeholder_text("Search Google…")
        self.entry.connect("activate", lambda *_: self.run_search())
        # The id is kept so _set_entry_text() can silence it — see there.
        self._entry_changed_id = self.entry.connect("changed",
                                                    self._on_entry_changed)
        bar.append(self.entry)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        bar.append(self.spinner)

        self.btn_lens = self._chip("edit-select-all-symbolic",
                                   "Circle to Search — grab a screen region  ·  Ctrl+Shift+S",
                                   self.on_circle_to_search)
        bar.append(self.btn_lens)
        self.btn_upload = self._chip("image-x-generic-symbolic",
                                     "Search an image with Lens  ·  Ctrl+U",
                                     self.on_pick_image)
        bar.append(self.btn_upload)

        self.menu_button = Gtk.MenuButton()
        self.menu_button.add_css_class("halo-chip")
        self.menu_button.set_icon_name("view-more-symbolic")
        self.menu_button.set_tooltip_text("Settings")
        self.menu_button.set_popover(self._build_menu())
        bar.append(self.menu_button)

        close = self._chip("window-close-symbolic", "Close  ·  Esc", lambda *_: self.hide_popup())
        close.add_css_class("halo-close")
        bar.append(close)

        # ── the panel's bottom grab band ──
        # A widget in the layout, not a capture-phase controller on the window.
        # The controller worked, and cost two things it should not have: the
        # pill stopped holding the X input focus (measured — the focus checks
        # went red the moment it was added and green again the moment it was
        # taken away), and a motion handler on the toplevel sets the cursor for
        # everything under it, so the results page lost its own I-beams and
        # link pointers. A strip that occupies the bottom of the slab has
        # neither problem: it gets its events because it is there, and its
        # cursor is its own.
        #
        # An **overlay** over the slab's last rows, and both halves of that
        # matter. OUTSIDE the pill, in the first RESIZE_GRAB_PX rows of apron
        # below its painted edge — which is where a hand that has resized any
        # other window on this desktop already reaches, and which gives the page
        # back the ten rows the band used to take off the bottom of every
        # result.
        #
        # It spent several versions inside the slab because outside was thought
        # to be unreachable: rows below the pill could only be made clickable by
        # an input shape the bounding shape did not have, and X intersects the
        # two. That intersection is real and still is. What was wrong was the
        # belief that the bounding shape had to exist at all — the window is a
        # 32-bit ARGB toplevel that already commits alpha 0 out here, so the
        # shape was cutting away pixels that were transparent anyway. With no
        # bounding shape there is nothing left to intersect the input region
        # against, and the band is simply live. set_rounded_shape() carries the
        # measurement, and _shape_now() is what hands X these rows.
        #
        # Still an overlay rather than a row: an overlay child costs no layout
        # space and forces no minimum, so the apron stays the 31px it already
        # was and the window does not grow by the height of its own resize
        # border. .halo-grip paints nothing; the cursor is the whole of its
        # appearance.
        #
        # Hidden unless the panel is open — see _set_grip_visible(). That also
        # keeps it clear of the detached ↓, which lives in this same apron but
        # only ever while the panel is shut (expand() hides the disc in the
        # line above the one that shows this).
        self.grip = Gtk.Box()
        self.grip.add_css_class("halo-grip")
        self.grip.set_size_request(-1, self.RESIZE_GRAB_PX)
        self.grip.set_halign(Gtk.Align.FILL)
        self.grip.set_valign(Gtk.Align.START)
        self.grip.set_visible(False)
        self.apron_wrap.add_overlay(self.grip)
        grip_drag = Gtk.GestureDrag()
        grip_drag.connect("drag-begin", self._on_resize_begin)
        grip_drag.connect("drag-update", self._on_resize_update)
        grip_drag.connect("drag-end", self._on_resize_end)
        self.grip.add_controller(grip_drag)
        grip_hover = Gtk.EventControllerMotion()
        grip_hover.connect("motion", self._on_resize_motion)
        # "enter" as well as "motion", and it is not belt and braces: the
        # pointer can arrive on the band without moving, because the band can
        # move under *it* — the panel grows to meet a stationary pointer on
        # every Ctrl+↓ and every drawer that opens. Motion-only left the arrow
        # cursor sitting on a resize edge until the mouse was jiggled.
        grip_hover.connect("enter", self._on_resize_motion)
        self.grip.add_controller(grip_hover)

        # ── the two corners, straddling the pill's own bottom edge ──
        # RESIZE_CORNER_UP_PX rows tall and RESIZE_CORNER_PX wide, in the SLAB
        # rather than the apron, so together with the band below them each
        # corner hotspot is continuous across the edge the eye sees. Reported
        # three times: a hand goes to the corner, finds nothing there, and has
        # to hunt for the strip underneath it.
        #
        # Three things had to agree, and only the first two are widgets:
        # _resize_zone() answers here now, these children are what GTK's hit
        # test finds here, and _shape_now() adds the matching rectangles to the
        # X input region — because these rows lie inside the pill's 22px corner
        # arc, and outside the arc the input region did not reach, so the
        # pointer never arrived at all. A rect that was sent is not a draggable
        # edge; all three, or none of it works.
        #
        # Their cursor is set once and never changes: a corner grip is only
        # ever its own corner, so there is nothing for a motion handler to
        # decide. The straight band keeps its own, which does have to change.
        self.corner_grips = []
        for _side, _align in (("left", Gtk.Align.START),
                              ("right", Gtk.Align.END)):
            corner = Gtk.Box()
            corner.add_css_class("halo-grip")
            corner.set_size_request(self.RESIZE_CORNER_PX,
                                    self.RESIZE_CORNER_UP_PX)
            corner.set_halign(_align)
            corner.set_valign(Gtk.Align.END)
            corner.set_visible(False)
            self.slab_wrap.add_overlay(corner)
            corner_drag = Gtk.GestureDrag()
            corner_drag.connect("drag-begin", self._on_corner_begin, _side)
            corner_drag.connect("drag-update", self._on_resize_update)
            corner_drag.connect("drag-end", self._on_resize_end)
            corner.add_controller(corner_drag)
            try:
                corner.set_cursor(
                    Gdk.Cursor.new_from_name(self._RESIZE_CURSORS[_side], None))
            except Exception:
                pass
            self.corner_grips.append(corner)

        # ── suggestions ──
        self.suggest_reveal = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=self._drawer_ms())
        body.append(self.suggest_reveal)
        sug_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sep = Gtk.Box()
        sep.add_css_class("halo-sep")
        sug_box.append(sep)
        self.suggest_list = Gtk.ListBox()
        self.suggest_list.add_css_class("halo-suggest")
        self.suggest_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.suggest_list.set_margin_top(4)
        self.suggest_list.set_margin_bottom(6)
        self.suggest_list.connect("row-activated", self._on_suggest_activated)
        # The same propagate_natural_height-with-a-maximum the history list has
        # always used, and for the same reason: a short list is exactly as tall as
        # it needs to be, a long one scrolls rather than making the window taller
        # than the room under the pill. The maximum is set by _cap_drawers(),
        # which explains what goes wrong without it.
        self.suggest_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_height=True)
        self.suggest_scroll.set_child(self.suggest_list)
        sug_box.append(self.suggest_scroll)
        self._sug_box = sug_box
        self.suggest_reveal.set_child(sug_box)
        # The rows are cleared when the slide has finished, not when it starts.
        self.suggest_reveal.connect("notify::child-revealed",
                                    self._on_suggest_revealed)

        # ── search history ──
        # Its own revealer rather than a second mode of the suggestions list:
        # the two show different things, one is filtered locally and the other
        # fetched, and sharing a widget between them meant every keystroke had to
        # work out which of the two it was talking to.
        self.history_reveal = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=self._drawer_ms())
        body.append(self.history_reveal)
        hist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        hsep = Gtk.Box()
        hsep.add_css_class("halo-sep")
        hist_box.append(hsep)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.add_css_class("halo-hist-head")
        self.history_title = Gtk.Label(xalign=0.0, hexpand=True)
        self.history_title.add_css_class("halo-title")
        self.history_title.set_ellipsize(Pango.EllipsizeMode.END)
        head.append(self.history_title)
        self.history_clear_btn = Gtk.Button(label="Clear all")
        self.history_clear_btn.add_css_class("halo-hist-clear")
        self.history_clear_btn.set_valign(Gtk.Align.CENTER)
        self.history_clear_btn.set_can_focus(False)
        self.history_clear_btn.connect("clicked", self._on_clear_history)
        head.append(self.history_clear_btn)
        hist_box.append(head)

        self.history_list = Gtk.ListBox()
        # Both classes: the suggestion look, plus the few overrides the extra
        # columns need.
        self.history_list.add_css_class("halo-suggest")
        self.history_list.add_css_class("halo-history")
        self.history_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.history_list.connect("row-activated", self._on_history_activated)
        # propagate_natural_height with a maximum, so a short history is exactly
        # as tall as it needs to be and a long one scrolls instead of pushing the
        # pill off the screen.
        self.history_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_height=True)
        self.history_scroll.set_max_content_height(self.HISTORY_MAX_PX)
        self.history_scroll.set_child(self.history_list)
        self.history_scroll.set_margin_top(2)
        # 8, not 6: the slab's bottom corners are rounded at 22px, and the last
        # row's descenders were sitting in the curve.
        self.history_scroll.set_margin_bottom(8)
        hist_box.append(self.history_scroll)

        self.history_empty = Gtk.Label(xalign=0.0, wrap=True)
        self.history_empty.add_css_class("halo-suggest-hint")
        self.history_empty.set_margin_start(16)
        self.history_empty.set_margin_end(16)
        self.history_empty.set_margin_bottom(10)
        self.history_empty.set_visible(False)
        hist_box.append(self.history_empty)
        self.history_reveal.set_child(hist_box)
        self._hist_box = hist_box
        self.history_reveal.connect("notify::child-revealed",
                                    self._on_history_revealed)

        # ── results ──
        # SLIDE_DOWN, not CROSSFADE: GtkRevealer only scales its size request
        # for slide transitions. With a crossfade the collapsed revealer keeps
        # reserving the child's full height, which leaves dead space in the
        # pill and stops the window shrinking again. Sliding also allocates the
        # child at natural size and clips it, so WebKit lays out once instead
        # of on every animation frame.
        self.result_reveal = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=self._reveal_ms())
        self.result_reveal.connect("notify::child-revealed",
                                   self._on_result_revealed)
        body.append(self.result_reveal)
        self.result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # The panel is its own flat surface in the page's colour. Without this the
        # pill's sheen gradient carried on behind the toolbar and out through the
        # transparent scroll gutter, so the slab appeared to wrap around the
        # results instead of ending above them.
        self.result_box.add_css_class("halo-results")
        self.result_reveal.set_child(self.result_box)

        sep2 = Gtk.Box()
        sep2.add_css_class("halo-sep")
        self.result_box.append(sep2)

        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tb.add_css_class("halo-toolbar")
        self.result_box.append(tb)
        self.btn_back = self._chip("go-previous-symbolic", "Back  ·  Alt+←",
                                   lambda *_: self._history(-1))
        self.btn_fwd = self._chip("go-next-symbolic", "Forward  ·  Alt+→",
                                  lambda *_: self._history(1))
        tb.append(self.btn_back)
        tb.append(self.btn_fwd)
        self.btn_reload = self._chip("view-refresh-symbolic", "Reload  ·  Ctrl+R",
                                     lambda *_: self._reload_or_stop())
        tb.append(self.btn_reload)
        self.url_label = Gtk.Label(hexpand=True, xalign=0.0)
        self.url_label.add_css_class("halo-url")
        self.url_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        # Single-line mode fixes the label's height at the font's own ascent and
        # descent rather than at its layout's, so a break that ever gets past
        # one_line() cannot grow the toolbar past the PANEL_CHROME the panel's
        # height maths reserves for it. It does not stop Pango breaking — that
        # is one_line()'s job — it stops the break costing the page 21px.
        self.url_label.set_single_line_mode(True)
        tb.append(self.url_label)
        self.btn_copy = self._chip("edit-copy-symbolic",
                                   "Copy this link  ·  Ctrl+Shift+C",
                                   self.on_copy_url)
        tb.append(self.btn_copy)
        tb.append(self._chip("view-fullscreen-symbolic",
                             "Open in your browser  ·  Ctrl+Return", self.on_open_external))
        tb.append(self._chip("pan-up-symbolic",
                             "Collapse to the pill  ·  ↑ from the field, or "
                             "Ctrl+↓ / Ctrl+↑ to resize",
                             lambda *_: self.collapse()))

    def _brand_icon(self) -> Gtk.Widget:
        """Google's mark when librsvg can render it, a glyph otherwise."""
        try:
            _own_dir(DATA_DIR)
            svg = DATA_DIR / "mark.svg"
            # Rewritten every launch, not just when missing: otherwise an
            # existing install would keep serving an older logo forever.
            if not svg.exists() or svg.read_text() != ICON_SVG:
                svg.write_text(ICON_SVG)
            texture = Gdk.Texture.new_from_filename(str(svg))
            # GtkImage, not GtkPicture: set_size_request() only raises a widget's
            # *minimum* size, and a GtkPicture's natural size is its texture's —
            # 48px here. The mark therefore rendered at 48 rather than MARK_PX,
            # which dwarfed the 16.5px query text and pushed the pill to 70px,
            # ten past the BAR_HEIGHT the panel clamp reserves for it.
            # set_pixel_size() sets minimum and natural together, so the mark is
            # the size it is asked to be.
            img = Gtk.Image.new_from_paintable(texture)
            img.set_pixel_size(MARK_PX)
            img.set_valign(Gtk.Align.CENTER)
            return img
        except Exception:
            img = Gtk.Image.new_from_icon_name("system-search-symbolic")
            img.add_css_class("halo-icon")
            return img

    def _chip(self, icon: str, tip: str, handler) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("halo-chip")
        btn.set_icon_name(icon)
        btn.set_tooltip_text(tip)
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_can_focus(False)
        btn.connect("clicked", self._guard_click(handler))
        return btn

    def _guard_click(self, handler):
        """Wrap a button handler so a drag's release is not also a press of it.

        The pill is dragged by its own bar, and the bar is mostly buttons — so
        the obvious place to grab it is a chip, and grabbing a chip used to move
        the window *and* fire the chip when the hand let go. Picking the window
        up by the image button ran an image search at the end of it.

        Claiming the gesture (see _on_bar_drag_update) is what should prevent
        that, and is done as well; this is the belt to that pair of braces,
        because whether GTK cancels a button's own click gesture when an
        ancestor claims the sequence out from under it is a detail of gesture
        arbitration, and there is no way to press a real mouse button here to
        find out — XTest is a no-op under this compositor and there is no
        headless X server on this machine to fall back to. So the outcome is
        made certain at the one place it is visible: the handler itself.

        The flag is dropped at idle rather than at drag-end, because both the
        release that ends the drag and the click it would otherwise be are
        dispatched from the same event, and their order is not ours to choose.
        Anything at idle is after both.
        """
        def guarded(*args):
            if self._swallow_click:
                return None
            return handler(*args)
        return guarded

    def _swallow_next_click(self) -> None:
        if self._swallow_click:
            return
        self._swallow_click = True
        GLib.idle_add(self._release_click_guard, priority=GLib.PRIORITY_LOW)

    def _release_click_guard(self) -> bool:
        self._swallow_click = False
        return False

    def _build_menu(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.add_css_class("halo-popover")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_size_request(288, -1)
        # The menu measures ~750px tall with every section expanded, and a
        # GtkPopover that will not fit is clamped to the work area and simply
        # clips what is left over — it does not scroll on its own. On a 1366×768
        # laptop the pill sits ~200px down, so DATA and "Quit Halo" fell off the
        # bottom with no way to reach them. Scroll instead of losing them; the
        # natural-size propagation keeps the popover exactly as tall as the menu
        # wherever there is room, so nothing changes on a big screen.
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_width=True,
            propagate_natural_height=True)
        scroller.set_child(box)
        pop.set_child(scroller)
        pop.connect("show", lambda *_: self._cap_menu_height(scroller))

        def heading(text: str) -> None:
            lbl = Gtk.Label(label=text, xalign=0.0)
            lbl.add_css_class("halo-title")
            lbl.set_margin_top(8)
            lbl.set_margin_start(10)
            lbl.set_margin_bottom(2)
            box.append(lbl)

        def toggle(label: str, subtitle: str, active: bool, on_change) -> Gtk.Switch:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.set_margin_start(10)
            row.set_margin_end(8)
            row.set_margin_top(3)
            row.set_margin_bottom(3)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            text.append(Gtk.Label(label=label, xalign=0.0))
            if subtitle:
                sub = Gtk.Label(label=subtitle, xalign=0.0)
                sub.add_css_class("halo-dim")
                sub.set_wrap(True)
                sub.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                # set_wrap() alone does not stop a long subtitle widening the
                # whole popover: a wrapping label still asks for its full single
                # line as its natural width. This is the cap that holds the menu
                # at the width the box asked for.
                sub.set_max_width_chars(SUBTITLE_CHARS)
                text.append(sub)
            row.append(text)
            sw = Gtk.Switch(active=active, valign=Gtk.Align.CENTER)
            # The handler id is kept because gtk_switch_set_active() emits
            # state-set exactly as a click does — verified: deleting the launcher
            # behind Halo's back and then merely opening this menu re-ran
            # remove_launcher(), because _refresh_setup_status() copied the new
            # reality into the switch and the switch performed it again. Anything
            # that syncs a switch from reality has to block this first. The accel
            # checkbuttons below were always guarded this way; the switches were
            # the half that was missed.
            sw.halo_handler = sw.connect(
                "state-set", lambda _s, st: (on_change(st), False)[1])
            row.append(sw)
            box.append(row)
            return sw

        def choice(label: str, subtitle: str, widget: Gtk.Widget) -> None:
            """A row shaped like toggle()'s, with something other than a switch."""
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.set_margin_start(10)
            row.set_margin_end(8)
            row.set_margin_top(3)
            row.set_margin_bottom(3)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            text.append(Gtk.Label(label=label, xalign=0.0))
            if subtitle:
                note = Gtk.Label(label=subtitle, xalign=0.0)
                note.add_css_class("halo-dim")
                note.set_wrap(True)
                note.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                note.set_max_width_chars(SUBTITLE_CHARS)
                text.append(note)
            row.append(text)
            widget.set_valign(Gtk.Align.CENTER)
            row.append(widget)
            box.append(row)

        def action(label: str, handler) -> Gtk.Button:
            btn = Gtk.Button(label=label)
            btn.set_margin_start(8)
            btn.set_margin_end(8)
            btn.set_margin_top(4)
            btn.connect("clicked", handler)
            box.append(btn)
            return btn

        heading("SETUP")
        # Label and tooltip are both set by _refresh_setup_button(), which reads
        # the audit: this button is a different offer depending on what is
        # actually on disk. "Set up Halo…" is only the first-run wording.
        self.setup_btn = action("Set up Halo…", self._on_finish_setup)
        self.setup_status = Gtk.Label(xalign=0.0)
        self.setup_status.add_css_class("halo-dim")
        self.setup_status.set_margin_start(10)
        self.setup_status.set_margin_end(10)
        self.setup_status.set_wrap(True)
        box.append(self.setup_status)
        # Shown only when something Halo wrote has gone stale, so the reason the
        # button says "Repair" is on screen next to it rather than inside it.
        self.setup_repair_label = Gtk.Label(xalign=0.0)
        self.setup_repair_label.add_css_class("halo-warn")
        self.setup_repair_label.set_margin_start(10)
        self.setup_repair_label.set_margin_end(10)
        self.setup_repair_label.set_wrap(True)
        self.setup_repair_label.set_visible(False)
        box.append(self.setup_repair_label)

        # Each piece of setup is also individually switchable, so nothing has to
        # be taken as a bundle.
        self.sw_launcher = toggle(
            "Add to Applications", "A .desktop launcher in your app grid",
            DesktopIntegration.launcher_installed(), self._on_launcher_toggled)
        self.sw_autostart = toggle(
            "Start at login", "Keeps Halo warm so it opens instantly",
            DesktopIntegration.autostart_enabled(), self._on_autostart_toggled)
        # Global shortcuts: tick as many as you like, each becomes its own GNOME
        # custom shortcut. A key already claimed elsewhere says so on its row,
        # because GNOME silently picks a winner rather than warning you.
        keys_label = Gtk.Label(label="Keys that open Halo", xalign=0.0)
        keys_label.add_css_class("halo-dim")
        keys_label.set_margin_start(10)
        keys_label.set_margin_top(6)
        box.append(keys_label)

        # Filled by _rebuild_accel_rows() rather than built once here: the list is
        # the presets plus whatever the user has added, and it changes length
        # while the menu exists — from the button below, and from someone adding
        # or deleting a shortcut in GNOME's keyboard panel behind Halo's back.
        self.keys_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(self.keys_box)
        self.accel_rows: list[tuple[str, Gtk.CheckButton, Gtk.Label]] = []
        self._rebuild_accel_rows()
        add_key = action("Add a shortcut…", self._on_add_shortcut)
        add_key.set_tooltip_text(
            "Press any combination you like — it joins the list above, "
            "tickable and removable like the rest")

        self.conflict_label = Gtk.Label(xalign=0.0)
        self.conflict_label.add_css_class("halo-dim")
        self.conflict_label.set_margin_start(10)
        self.conflict_label.set_margin_end(10)
        self.conflict_label.set_margin_top(2)
        self.conflict_label.set_wrap(True)
        self.conflict_label.set_visible(False)
        box.append(self.conflict_label)

        heading("BEHAVIOUR")
        toggle("Search suggestions", "Live autocomplete as you type",
               CFG["suggestions"], self._on_suggestions_toggled)
        toggle("Remember position", "Reuse where you dragged it, instead of "
                                    "following the mouse",
               CFG["remember_position"],
               lambda st: CFG.__setitem__("remember_position", st))
        toggle("Compact results", "Hide Google's own search box — yours is right "
                                  "above it", CFG["compact_results"],
               self._on_compact_toggled)
        toggle("Floating ↓ button",
               "A small arrow below the pill while a page is waiting behind it. "
               "Same as pressing ↓, and it takes no room in the pill.",
               CFG["parked_arrow"], self._on_parked_arrow_toggled)
        toggle("SafeSearch", "", CFG["safe_search"],
               self._on_safe_search_toggled)
        reveal_opts = list(self.REVEAL_PRESETS)
        ms = int(CFG["reveal_ms"])
        if ms not in [m for m, _ in reveal_opts]:
            reveal_opts.append((ms, f"{ms} ms"))
            reveal_opts.sort(key=lambda opt: -opt[0])
        self.reveal_options = reveal_opts
        reveal_picker = Gtk.DropDown.new_from_strings(
            [label for _, label in reveal_opts])
        reveal_picker.set_selected([m for m, _ in reveal_opts].index(ms))
        reveal_picker.connect("notify::selected", self._on_reveal_changed)
        choice("Opening animation",
               "Instant skips it. Growing the window is what makes a slide look "
               "stepped, and that part is the compositor's to give.",
               reveal_picker)

        heading("MEMORY")
        idle_opts = list(self.IDLE_RELEASE_PRESETS)
        mins = int(CFG["idle_release_min"])
        if mins not in [m for m, _ in idle_opts]:
            idle_opts.append((mins, f"{mins} minutes"))
            idle_opts.sort(key=lambda opt: (opt[0] != 0, opt[0]))
        self.idle_options = idle_opts
        idle_picker = Gtk.DropDown.new_from_strings(
            [label for _, label in idle_opts])
        idle_picker.set_selected([m for m, _ in idle_opts].index(mins))
        idle_picker.connect("notify::selected", self._on_idle_release_changed)
        choice("Release the engine",
               "Hands the browser back after this long hidden, freeing a few "
               "hundred MB. ↓ reopens the last page.", idle_picker)
        action("Release it now", self._on_release_now)

        heading("HISTORY")
        toggle("Remember searches",
               "Click the Google mark, or press ↑, to look through them",
               CFG["history"], self._on_history_toggled)
        toggle("Show them while typing",
               "A close past search appears among the suggestions, marked with "
               "a clock", CFG["history_in_suggestions"],
               self._on_history_suggest_toggled)
        # A list rather than a number field: the useful answers are few, and
        # "how many days" is not how anyone thinks about forgetting things.
        options = list(self.HISTORY_RETENTIONS)
        days = int(CFG["history_days"])
        if days not in [d for d, _ in options]:
            # A hand-edited config gets a row of its own rather than being
            # silently rounded to whichever preset happens to be nearest.
            options.append((days, f"{days} days"))
            options.sort(key=lambda opt: (opt[0] != 0, opt[0]))
        self.retention_options = options
        picker = Gtk.DropDown.new_from_strings([label for _, label in options])
        picker.set_selected([d for d, _ in options].index(days))
        picker.connect("notify::selected", self._on_retention_changed)
        choice("Forget after",
               "Older entries go at the next start, and when you change this",
               picker)
        action("Forget all searches", self._on_forget_all)

        heading("INFORMATION")
        # The manual lives in this file, so a lone halo.py is still documented.
        manual = action(MANUAL_LABEL, self._on_show_info)
        manual.set_tooltip_text(
            "Every key Halo listens for, every file it writes and where, why it "
            "behaves the way it does, and what to do when it misbehaves")
        source = action(f"{APP_NAME} on GitHub", self._on_open_project)
        source.set_tooltip_text(
            f"{PROJECT_URL} — opens in Halo, like any other link")

        heading("DATA")
        action("Clear cookies and cache", self._on_clear_data)
        action("Remove Halo's setup…", self._on_remove_setup)
        action(f"Quit {APP_NAME}", lambda *_: self.app.quit())

        pop.connect("show", lambda *_: self._refresh_setup_status())
        return pop

    def _cap_menu_height(self, scroller: Gtk.ScrolledWindow) -> None:
        """Let the ⋯ menu be as tall as the screen genuinely allows, no taller.

        Worked out from where the ⋯ button actually is, because that is what the
        popover anchors to, and GTK will open it on whichever side is roomier.

        Deliberately NOT from the window's height. That was the bug: with the
        results panel open the window is the whole slab, so the "pill" was taken
        to be ~717px tall, its bottom edge landed near the foot of the screen, and
        the space below it came out at almost nothing — leaving the menu capped to
        the room above instead and clipped to about 260px. The button is 30px tall
        wherever the panel happens to be.
        """
        cap = 560                       # a sane floor if none of this is knowable
        try:
            rect, scale = self._usable_pick(WM.pointer())
            scale = max(1, scale)
            top, height = rect[1] // scale, rect[3] // scale
            xid = self._xid()
            where = WM.get_position(xid) if xid else None
            if where:
                win_top = where[1] // scale
                anchor_top, anchor_bottom = win_top, win_top + BAR_HEIGHT
                try:
                    ok, bounds = self.menu_button.compute_bounds(self)
                    if ok:
                        anchor_top = win_top + int(bounds.origin.y)
                        anchor_bottom = anchor_top + int(bounds.size.height)
                except Exception:
                    pass
                cap = max(anchor_top - top, top + height - anchor_bottom) - 24
            else:
                cap = height - 96
        except Exception:
            pass
        scroller.set_max_content_height(max(240, int(cap)))

    def _build_cover(self) -> None:
        """The results area and its loading cover — plain GTK, always present.

        Split out of _build_webview so the panel, the cover and the toolbar exist
        before any WebKit object does. The WebView is slotted into this overlay
        later, on first use.
        """
        overlay = Gtk.Overlay()
        self.web_overlay = overlay
        self.loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.loading.add_css_class("halo-loading")
        self.loading.set_valign(Gtk.Align.FILL)
        self.loading.set_halign(Gtk.Align.FILL)
        centre = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                         vexpand=True, valign=Gtk.Align.CENTER)
        self.loading_spinner = Gtk.Spinner(width_request=26, height_request=26,
                                           halign=Gtk.Align.CENTER)
        centre.append(self.loading_spinner)
        self.loading_label = Gtk.Label(halign=Gtk.Align.CENTER)
        centre.append(self.loading_label)
        self.loading.append(centre)
        self.loading.set_visible(False)
        overlay.add_overlay(self.loading)
        self.result_box.append(overlay)
        # Paint the panel now, so it is the right colour even before a page exists.
        self._paint_page_bg(remembered_page_bg())

    def _ensure_webview(self) -> bool:
        """Build the WebKit half on first use; True when it is ready.

        A resident Halo that has not searched yet has no use for a browser engine,
        and measured it is not cheap to keep one waiting: the WebView plus the
        network process it spawns account for ~68MB of the daemon's resident
        memory (35MB without them, 103MB with). Building takes 58ms and happens
        behind a keypress and a network round trip, where it disappears into the
        noise — measured first-load commit times overlap either way.
        """
        if self.web is not None:
            return True
        try:
            self._build_webview()
        except Exception:
            # Half-built is worse than not built: a retry would otherwise open a
            # second cookie session against the same files.
            self.web = self.session = self.ucm = None
        if self.web is not None and not self.get_visible():
            # Built without the pill being on screen — the first-run handshake
            # does exactly this — so nothing would otherwise start the clock.
            self._arm_idle_release()
        return self.web is not None

    def _build_webview(self) -> None:
        _own_dir(DATA_DIR)
        session = WebKit.NetworkSession.new(str(DATA_DIR / "web"), str(DATA_DIR / "cache"))
        # Intelligent Tracking Prevention would periodically evict the very
        # cookies that keep the consent banner and CAPTCHAs away.
        session.set_itp_enabled(False)
        cookies = session.get_cookie_manager()
        cookies.set_persistent_storage(str(DATA_DIR / "cookies.sqlite"),
                                       WebKit.CookiePersistentStorage.SQLITE)
        # WebKit creates the jar itself, so tighten it after the fact as well as
        # relying on the directory. The directory is the guarantee; this is for
        # anything that ever moves the file somewhere less careful.
        _own_file(DATA_DIR / "cookies.sqlite")
        cookies.set_accept_policy(WebKit.CookieAcceptPolicy.ALWAYS)
        # Every download the page starts arrives here and nowhere else — a
        # <a download> link never reaches ::decide-policy at all. Without it
        # WebKit picked the destination on its own, silently.
        session.connect("download-started", self._on_download_started)
        self.session = session

        ucm = WebKit.UserContentManager()
        # START, before any of Google's own scripts exist to call these.
        ucm.add_script(WebKit.UserScript.new(
            HOLD_SCROLL_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START, None, None))
        ucm.add_script(WebKit.UserScript.new(
            CONSENT_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.END, None, None))
        # START as well: the point is to catch the FIRST frame of a document, and
        # a script that waits for the parser to finish has already missed it.
        ucm.add_script(WebKit.UserScript.new(
            PAINT_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START, None, None))
        ucm.add_script(WebKit.UserScript.new(
            SCROLL_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START, None, None))
        # ALL_FRAMES, and the only script here that is: an embedded player is a
        # frame of its own, and this has to be in the frame that makes the
        # AudioContext to be able to stop it. START, because a hook that arrives
        # after the page's own scripts have already built one has missed it.
        ucm.add_script(WebKit.UserScript.new(
            AUDIO_HOOK_JS, WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.START, None, None))
        # END, not START: it walks document.body, which does not exist yet at
        # the top of a parse. It only ever runs when Ctrl+F asks it to.
        ucm.add_script(WebKit.UserScript.new(
            FIND_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.END, None, None))
        # START: it only installs a listener, and a selection made before this
        # ran would go unreported for as long as it stayed unchanged.
        ucm.add_script(WebKit.UserScript.new(
            SELECTION_WATCH_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START, None, None))
        # ALL_FRAMES: a picture worth dragging is as likely to be inside an
        # embedded frame as in the page itself, and dragstart is dispatched in
        # the frame the picture lives in. START, because the listener has to be
        # there before the first drag, not after the first paint.
        ucm.add_script(WebKit.UserScript.new(
            DRAG_JS, WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.START, None, None))
        # END too, and for the same reason: it reads an element out of the
        # results page, which has to have been parsed for the element to exist.
        ucm.add_script(WebKit.UserScript.new(
            SPELL_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.END, None, None))
        try:
            ucm.register_script_message_handler("halo", None)
        except TypeError:      # webkit2gtk before 6.0 took no world argument
            ucm.register_script_message_handler("halo")
        ucm.connect("script-message-received::halo", self._on_page_message)
        self.ucm = ucm
        self._apply_user_styles()

        settings = WebKit.Settings()
        # Deliberately no set_user_agent(): WebKit's own UA matches its engine
        # and therefore does not trip Google's bot heuristics.
        settings.set_enable_developer_extras(False)
        settings.set_enable_smooth_scrolling(True)
        settings.set_enable_back_forward_navigation_gestures(True)
        settings.set_media_playback_requires_user_gesture(True)
        settings.set_enable_write_console_messages_to_stdout(False)
        # No set_enable_hyperlink_auditing(False) here, though <a ping> beacons
        # are worth turning off and Firefox does. WebKitGTK 2.52's setter is a
        # no-op that says so: "webkit_settings_set_enable_hyperlink_auditing is
        # deprecated and does nothing", printed once per view. There is no
        # runtime feature for it either — of 489, none mentions ping, beacon or
        # auditing except the Beacon API itself, which is a different thing that
        # ordinary sites rely on. So this is WebKit's call, not Halo's, and the
        # dead line that used to be here only added a warning to the log.
        settings.set_property("enable-page-cache", True)

        self.web = WebKit.WebView(network_session=session, settings=settings,
                                  user_content_manager=ucm, vexpand=True)
        self.web.add_css_class("halo-web")
        # Opaque, in the page's own colour — NOT transparent. A transparent base
        # let the slab's sheen gradient show through everything the page does not
        # paint itself, and the scroll gutter is exactly that: an 11px strip down
        # the side of the results glowing brighter at the top. The rounded corners
        # are safe regardless, because .halo-body clips its children (overflow
        # HIDDEN) to the same radius.
        self._paint_page_bg(remembered_page_bg())
        self.web.connect("load-changed", self._on_load_changed)
        # COMMITTED is what clears _nav_pending, and a load that fails never gets
        # there — without this the cover would sit out its whole grace period on
        # every DNS error and every cancelled navigation.
        self.web.connect("load-failed", self._on_load_failed)
        self.web.connect("notify::estimated-load-progress", self._on_load_progress)
        self.web.connect("notify::is-loading", self._on_loading_changed)
        self.web.connect("notify::uri", self._on_uri_changed)
        self.web.connect("decide-policy", self._on_decide_policy)
        self.web.connect("run-file-chooser", self._on_file_chooser)
        # Without this, target="_blank" links (and window.open) silently do
        # nothing: WebKit asks for a second view and gets none.
        self.web.connect("create", self._on_create_view)
        # WebKit's own menu is built from stock actions, and those are localised
        # to $LANG and worded for a browser with tabs and windows. On this desktop
        # it offered "Verweis in neuem Reiter öffnen" — a new *tab* — in the middle
        # of an otherwise English UI, for a panel that has never opened a tab in
        # its life and quietly loads such links in the very view the menu was
        # opened over. So Halo builds the menu itself; see _on_context_menu.
        self.web.connect("context-menu", self._on_context_menu)
        self.web.connect("context-menu-dismissed", self._on_context_menu_dismissed)

        # The WebView must stay mapped while we drive Lens — an unmapped WebKit
        # view does not lay out, so Lens never initialises and the upload always
        # fails. So the panel opens straight away and the cover built above hides
        # the page until there is something worth looking at. It doubles as cover
        # for the brief blank flash at the start of any page load.
        # Through PanelClamp, so the page's own document height cannot decide
        # how tall the window is — see the class for what that looked like.
        self.web_overlay.set_child(PanelClamp(self.web))

    # ── giving the engine back when nobody is using it ──────────────────
    #
    # Measured on this machine with a results-sized page loaded: the pill alone is
    # ~172MB resident, and the engine takes it to ~469MB — 166MB of web process,
    # 64MB of network process, and ~54MB that the app process itself grows by.
    # Releasing gets 244MB of that back, and rebuilding costs 3ms. Which makes the
    # trade a good one: what it costs to come back is a page load, and a resident
    # search popup that nobody has touched for ten minutes has no business holding
    # a quarter of a gigabyte to show a page nobody is looking at.
    #
    # Dropping the references is NOT enough on its own — verified: the web process
    # went on running at 166MB, and the next _ensure_webview() started a SECOND
    # one beside it. terminate_web_process() is what actually ends it, and it has
    # to come first, while there is still a view to ask.
    IDLE_RETRY_S = 60           # something was in the way; look again in a minute

    def _arm_idle_release(self) -> None:
        """Start the clock, if there is an engine to release and a setting to do it."""
        if self._idle_timer or self.web is None:
            return
        minutes = int(CFG["idle_release_min"])
        if minutes <= 0:
            return
        self._idle_timer = GLib.timeout_add_seconds(minutes * 60,
                                                    self._idle_release_due)

    def _cancel_idle_release(self) -> None:
        if self._idle_timer:
            GLib.source_remove(self._idle_timer)
            self._idle_timer = 0

    def _release_blocked(self) -> str | None:
        """Why now is the wrong moment, or None if it is a fine one."""
        # getattr, because the self-test drives a HaloWindow under a plain
        # Gtk.Application rather than HaloApp, and a missing helper there should
        # not be an exception on a path whose whole job is to be cautious.
        warm_live = getattr(self.app, "warm_view_live", None)
        if self.warm_pending or (warm_live is not None and warm_live()):
            # The first-run handshake is using the session we would be tearing
            # down, and it is the one thing that must not have to happen twice.
            return "the first-run handshake is still going"
        if self.pending_lens:
            return "an image search is still in flight"
        try:
            if (self.web is not None
                    and self.web.get_property("is-playing-audio")
                    and not self.web.get_is_muted()):
                # Closing the pill on a playing video is not the same as asking
                # for it to be killed, and killing it is not undoable.
                #
                # ...but only while the page can actually be heard, and that
                # qualifier is the whole of a bug. Dismissing the pill mutes the
                # view and asks every frame to stop, so a page still claiming to
                # play after that is one that did not listen — an embedded
                # player, or a Web Audio graph nobody can reach. Without the
                # mute test this returned a reason for ever: _idle_release_due()
                # retried every 60s, was told the same thing every 60s, and the
                # engine was never handed back. The page therefore never
                # unloaded, so its media session stayed registered, so the
                # desktop went on listing Halo as paused music you could press
                # Play on — for the rest of the session. Reported exactly that
                # way, and the "permanently" in the report was literal.
                return "the page is still playing audio"
        except Exception:
            pass
        return None

    def _idle_release_due(self) -> bool:
        self._idle_timer = 0
        if self.get_visible():
            return False        # back in use; hide_popup will start the clock again
        blocked = self._release_blocked()
        if blocked:
            self._idle_timer = GLib.timeout_add_seconds(self.IDLE_RETRY_S,
                                                        self._idle_release_due)
            return False
        self.release_engine()
        return False

    def release_engine(self) -> bool:
        """Hand the browser engine back. True if there was one to hand back.

        Refuses while the panel is open: whatever is on screen is being read.
        """
        if self.web is None or self.expanded:
            return False
        self._cancel_idle_release()
        # Remembered before anything is torn down, so ↓ still has somewhere to go.
        parked = self.web.get_uri() or ""
        self._parked_uri = parked if _is_web_uri(parked) else None
        # The whole back/forward list with it, and before terminate_web_process()
        # rather than after: a released engine has no list to ask for. 237 bytes
        # for a two-page history, measured, so this costs the release nothing.
        self._parked_state = None
        if self._parked_uri is not None:
            try:
                state = self.web.get_session_state()
                blob = state.serialize() if state is not None else None
                if blob is not None and blob.get_size():
                    self._parked_state = blob
            except Exception:
                self._parked_state = None
        self._hide_loading()            # also cancels the cover and paint timers
        self._set_busy(False)
        self._nav_pending = False
        self._nav_uri = ""
        try:
            self.web.terminate_web_process()
        except Exception:
            pass
        # Unparent before dropping the reference: PanelClamp holds the view, and a
        # widget finalised with a parented child makes GTK complain loudly.
        try:
            self.web_overlay.set_child(None)
        except Exception:
            pass
        self.web = self.session = self.ucm = None
        self._styles_google = True
        self._page_at_top = True
        # There is no back/forward list to compose with any more.
        self._spell_slot = None
        self.btn_back.set_sensitive(False)
        self.btn_fwd.set_sensitive(False)
        # There is no load to cancel once the engine is gone, and the face is
        # the only thing that would still be claiming otherwise.
        self._set_stop_face(False)
        # Deterministic rather than eventual: the signal handlers we connected hold
        # the window from the view's side, and waiting for a generational sweep to
        # notice would mean the network process outliving the request by minutes.
        gc.collect()
        # The address is parked, so ↓ still has somewhere to go and the disc
        # still has a job. Nothing about releasing the engine takes the way back
        # away; it only makes it slower.
        self._update_arrow()
        return True

    # A pill closed on top of a playing video leaves it playing, and leaves the
    # desktop's media controls showing it. Pausing keeps the position — ↓ brings
    # the page back where it was — but a *paused* media session still sits in
    # those controls until the page itself is gone, which is why a page we had to
    # pause has its engine released sooner than the setting would otherwise say.
    MEDIA_IDLE_S = 45

    def _media_idle_wait(self) -> int | None:
        """How long a page we just paused may sit there, or None to leave it.

        The desktop's media controls keep listing a paused page until the page
        goes away, so a page we had to pause is let go sooner than the setting
        would otherwise allow — but never when the setting says to keep the
        engine resident, because that is a decision the user made.
        """
        minutes = int(CFG["idle_release_min"])
        if minutes <= 0:
            return None
        return min(minutes * 60, self.MEDIA_IDLE_S)

    def _quiet_page(self) -> None:
        """Put the page down once nobody is watching.

        Two mechanisms, because neither is enough on its own. Muting the view is
        immediate, total and needs nobody's cooperation — it is the only thing
        that reaches a cross-origin frame, which is where most video on the web
        actually lives. And asking the page to stop is what makes it a pause
        rather than a gag: a muted video still decodes every frame, still holds
        the engine, and is still listed by the desktop as something playing.
        """
        if self.web is None:
            return
        try:
            # First and synchronously. Everything below is a round trip into the
            # web process, and the sound has to stop on this side of it.
            self.web.set_is_muted(True)
        except Exception:
            pass

        def got(web, result, _data) -> None:
            try:
                value = web.evaluate_javascript_finish(result)
                stopped = int(float(value.to_string())) if value else 0
            except Exception:
                return
            if stopped <= 0 or self.get_visible():
                return
            wait = self._media_idle_wait()
            if wait is None:
                return
            self._cancel_idle_release()
            self._idle_timer = GLib.timeout_add_seconds(wait,
                                                        self._idle_release_due)

        try:
            self.web.evaluate_javascript(STOP_MEDIA_JS, -1, None, None, None,
                                         got, None)
        except Exception:
            pass

    def _wake_page(self) -> None:
        """Undo _quiet_page(), as far as it is ours to undo.

        The mute comes off, and the AudioContexts we suspended are resumed —
        those, and only those, so a context the page had suspended itself is not
        started by us coming back.

        Media elements are deliberately *not* resumed. A video that starts
        playing again because the search pill was summoned is a surprise, and
        the pill is summoned to search rather than to resume anything; Play is
        one click away and it is the user's to press. Web Audio is the opposite
        case: nothing on screen says the sound was suspended, and a page left
        like that is silently broken.
        """
        if self.web is None:
            return
        try:
            self.web.set_is_muted(False)
        except Exception:
            pass
        try:
            self.web.evaluate_javascript(WAKE_MEDIA_JS, -1, None, None, None,
                                         None, None)
        except Exception:
            pass

    def _reopen_parked(self) -> None:
        """Put back the page the engine was carrying when it was released.

        Only when the fresh view is still blank — anything that navigated on its
        own has already said where it wants to be, and _navigate() clears the
        parked address for exactly that reason.
        """
        if self._parked_uri is None or self.web is None:
            return
        if self.web.get_uri():
            self._parked_uri = self._parked_state = None
            return
        uri, self._parked_uri = self._parked_uri, None
        state, self._parked_state = self._parked_state, None
        self._set_busy(True)
        # The history first, and only then the page — see _restore_history for
        # what a plain load_uri() costs. A restore that does not take falls
        # straight through to the load that was here before it.
        if not self._restore_history(state, uri):
            self._navigate(uri)
        # The page usually comes back out of the disk cache, which survives the
        # release — so this is normally a flicker of cover rather than a wait.
        self._show_loading("Reopening…")

    def _restore_history(self, state, uri: str) -> bool:
        """Reopen `uri` as the page it was, with everything behind it. True if done.

        The back/forward list is not a nicety here: it is the whole of what the
        back/forward *swipe* is allowed to do. WebKit refuses the gesture on an
        empty list — canSwipeInDirection() is `return !!backForwardList->
        backItem()` and nothing else — so an engine released while nobody was
        looking used to take the gesture with it, permanently, on a page that
        came back looking exactly the same.

        Not load_uri(): loading the address would push a *new* entry on top of
        the restored list, so Back would land on the page it was already showing.
        go_to_back_forward_list_item() walks to the entry instead, which is a
        navigation to the same page that leaves the list alone. Measured: a
        two-page history serialises to 237 bytes, comes back as length 2, and
        can_go_back() is True again on the other side.
        """
        if state is None or self.web is None:
            return False
        try:
            self.web.restore_session_state(WebKit.WebViewSessionState.new(state))
            item = self.web.get_back_forward_list().get_current_item()
            if item is None or item.get_uri() != uri:
                return False
            # _navigate's bookkeeping, since we are not going through it: the
            # cover comes off on COMMITTED and nothing else would arm it.
            self._nav_pending = True
            self._nav_uri = uri
            self.web.go_to_back_forward_list_item(item)
        except Exception:
            return False
        return True

    def _apply_user_styles(self, google: bool | None = None) -> None:
        """(Re)install our page stylesheets; called again when a toggle flips.

        `google` says whether the page in the view is one of Google's own, and the
        compact-results rules only go on when it is. They are written against
        Google's structure — #searchform, #sfcnt, an empty spacer before #main —
        and now that links open in the panel they would otherwise be aimed at
        pages they were never measured against, where an id that happens to match
        is a piece of somebody else's site hidden for no reason. POLISH_CSS stays
        on everything: it is scrollbars and a colour scheme, which the panel wants
        whatever is loaded in it.

        Scoped here rather than through WebKit's own URL patterns because Google
        answers on the searcher's country domain, and those patterns cannot say
        google.<any tld> — the same reason _GOOGLE_DOMAIN is a regex.
        """
        if self.ucm is None:
            return              # nothing loaded yet; the build will apply them
        if google is None:
            google = self._styles_google
        self._styles_google = google
        self.ucm.remove_all_style_sheets()
        css = POLISH_CSS + (COMPACT_CSS if CFG["compact_results"] and google else "")
        self.ucm.add_style_sheet(WebKit.UserStyleSheet.new(
            css, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserStyleLevel.USER, None, None))
        # Separate sheet because it needs the other level — see FIND_CSS. The
        # rest of POLISH_CSS stays at USER level, where a page cannot outrank it.
        self.ucm.add_style_sheet(WebKit.UserStyleSheet.new(
            FIND_CSS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserStyleLevel.AUTHOR, None, None))
        # ALL_FRAMES, to match DRAG_JS: the rule has to be in the same frame as
        # the picture for WebKit to read it off that picture's style.
        self.ucm.add_style_sheet(WebKit.UserStyleSheet.new(
            DRAG_CSS, WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserStyleLevel.USER, None, None))

    # ── one surface: panel, cover and page share the page's own colour ──
    def _paint_page_bg(self, colour: str) -> None:
        """Paint everything behind Google's page in Google's own colour.

        The toolbar, the loading cover and the WebView's own base all take this,
        so the panel is a single flat sheet. What the scroll gutter reveals is
        then more of the same colour instead of the pill's gradient.
        """
        rgba = Gdk.RGBA()
        if not rgba.parse(colour):
            return
        rgba.alpha = 1.0
        self._page_bg = colour
        try:
            if self.web is not None:
                self.web.set_background_color(rgba)
        except Exception:
            pass
        css = (".halo-results, .halo-toolbar, .halo-loading { background-color: "
               f"{colour}; }}")
        if self._bg_provider is None:
            self._bg_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), self._bg_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        try:
            self._bg_provider.load_from_string(css)
        except AttributeError:                   # GTK < 4.12
            self._bg_provider.load_from_data(css.encode())

    def _adopt_page_bg(self) -> None:
        """Follow the page's real background instead of trusting a constant.

        Google has changed its dark shade more than once. A hardcoded guess that
        drifts shows up as a band down the scroll gutter and a seam under the
        toolbar, so ask the page after each load and adopt what it says.
        """
        def got(web, result, _data) -> None:
            try:
                value = web.evaluate_javascript_finish(result)
                colour = value.to_string() if value else ""
            except Exception:
                return
            rgba = Gdk.RGBA()
            if not colour or not rgba.parse(colour) or rgba.alpha < 0.99:
                return
            # Only ever follow a dark page. A light one — a CAPTCHA interstitial,
            # a network error page — would flash the whole panel white.
            if rgba.red * 0.299 + rgba.green * 0.587 + rgba.blue * 0.114 > 0.35:
                return
            hexed = "#%02x%02x%02x" % (round(rgba.red * 255),
                                       round(rgba.green * 255),
                                       round(rgba.blue * 255))
            if hexed != self._page_bg:
                self._paint_page_bg(hexed)
                # Remember it, so the next panel — and the next rebuild after an
                # idle release — opens in this colour instead of the constant.
                # Guarded by the same inequality, so this writes the config only
                # when Google actually changes shade, not once per load.
                if CFG["page_bg"] != hexed:
                    CFG["page_bg"] = hexed

        if self.web is None:
            return
        try:
            self.web.evaluate_javascript(PAGE_BG_JS, -1, None, None, None,
                                         got, None)
        except Exception:
            pass

    def _wire_keys(self) -> None:
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        # Files and bare textures both: dragging a picture out of a web page or an
        # image viewer hands over pixels, not a path, and the file-only target
        # silently refused those drops.
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        # Text as well as files and pixels: dragging a phrase out of a page onto
        # the pill is the obvious way to look it up, and it used to do nothing.
        #
        # The ORDER is the whole of it. gtk_drop_target_match() walks these
        # gtypes and takes the first one the drag can be read as — the target's
        # order, not the drag's. A picture dragged out of any browser offers
        # both its pixels and its address:
        #
        #   GdkTexture GdkPixbuf GdkFileList GFile gchararray ...
        #   text/html text/uri-list image/png text/plain ...
        #
        # and GTK deserialises that text/uri-list into a GFile quite happily —
        # a GDaemonFile for "https://.../pic.png", whose get_path() is None.
        # With GFile asked for first that is what arrived, there was no path to
        # give Lens, and the drop was refused without a sound after showing the
        # copy cursor all the way across the screen. Measured with a real
        # injected pointer drag out of a WebKitWebView.
        #
        # Nothing that used to arrive as a file stops doing so: a file
        # manager's drag advertises GdkFileList, text/uri-list and
        # text/plain, and no image type at all, so there is no texture for
        # this order to prefer.
        drop.set_gtypes([Gdk.Texture, Gio.File, GObject.TYPE_STRING])
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self._wire_entry_drag()
        self._wire_entry_drop()
        self._wire_mouse()

    # Buttons 8 and 9 are back and forward on every mouse that has them. GTK
    # hands them through as plain button numbers and WebKit does nothing with
    # them on its own, so reaching for them on a page — which is exactly what a
    # hand does after a few Wikipedia links — did nothing at all.
    MOUSE_BACK = 8
    MOUSE_FORWARD = 9

    # Every spelling of Tab that reaches a key handler. ISO_Left_Tab is what
    # Shift+Tab arrives as — a different keyval, not a modifier on this one —
    # and leaving it out would have left the focus a Shift away from the same
    # dead end. KP_Tab is the keypad's, on the layouts that have one.
    _TAB_KEYS = (Gdk.KEY_Tab, Gdk.KEY_KP_Tab, Gdk.KEY_ISO_Left_Tab)

    def _wire_mouse(self) -> None:
        click = Gtk.GestureClick()
        click.set_button(0)         # 0 means every button, not just the primary
        # CAPTURE: the page is a child of this window and would otherwise see the
        # press first.
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_mouse_button)
        self.add_controller(click)

    def _on_mouse_button(self, gesture, n_press: int, x: float,
                         y: float) -> None:
        button = gesture.get_current_button()
        if button == Gdk.BUTTON_MIDDLE:
            self._on_middle_click(gesture, n_press, x, y)
            return
        if button not in (self.MOUSE_BACK, self.MOUSE_FORWARD):
            return                  # every ordinary click carries on untouched
        # Claimed so the press does not also reach the page, and only acted on
        # once however many times it is clicked in a row.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        if n_press != 1:
            return
        self._history(-1 if button == self.MOUSE_BACK else 1)

    # ── resizing the panel by its bottom edge and corners ───────────────
    #
    # Only while the results panel is open, and only along the bottom: the pill
    # is anchored by its top edge and dragged by its middle, so a top or side
    # grab would be two ways to do the same thing fighting each other. The
    # bottom edge and the two bottom corners are the three that mean something.
    #
    # Not begin_resize(). The toplevel is set_resizable(False) — PanelClamp says
    # why, and it is load-bearing — so the window manager is told the window has
    # one legal size and would refuse. Halo owns its own geometry anyway: the
    # panel's height is a size request on the view and the width is the pill's,
    # so a resize here is the same arithmetic Ctrl+↓ already does, driven by a
    # pointer instead of a key.
    # How deep the grab band is. Ten, because that is mutter's own
    # draggable-border-width — the depth a hand that resizes other windows on
    # this desktop has already learned. It hangs in the apron *below* the pill's
    # painted edge, so it costs the layout nothing, paints nothing, and costs
    # the page nothing either: every row an open panel paints is the page's
    # again. The apron is SIZE + GAP = 31 logical px, so ten of them fit with
    # room to spare, and the band is clamped to the window's own foot anyway —
    # see _shape_now().
    RESIZE_GRAB_PX = 10
    RESIZE_CORNER_PX = 28       # ...and how far in from each end a corner runs
    # How far a CORNER reaches up into the pill, on top of the band below it.
    # The straight edge gets none of this and is unchanged: a bottom edge that
    # ate the page's last rows is the bug this band was moved out to fix, and
    # the report that asked for the corners asked for the corners only.
    #
    # Eight, because that is what makes the hotspot straddle the edge rather
    # than hang under it. The pill's corner radius is 22, so at the very last
    # row the painted edge has already curved 17px inward and the pointer at
    # the *visible* corner is standing in a nook that is outside the pill
    # altogether — reaching eight rows up covers the whole of that nook and
    # the first rows where the arc is still a corner, and stops well short of
    # anything a hand aiming at the page would be near.
    RESIZE_CORNER_UP_PX = 8
    MIN_WINDOW_W = 420          # matches the floor the config already enforces

    def _resize_zone(self, x: float, y: float) -> str | None:
        """Which grab the point is in — 'bottom', 'left', 'right' — or None.

        Measured in the window's own coordinates. The band is the first
        RESIZE_GRAB_PX rows of apron *below* the pill's painted edge — outside
        everything the page draws, the way every other GNOME window's resize
        border is outside its own visible frame. Rows out here are reachable
        because the window has no bounding shape for X to intersect the input
        region against; X11WM.set_rounded_shape carries that measurement, and
        _shape_now() is what hands X the rows.
        """
        if not self.expanded:
            return None
        slab = self.glow.get_height() if self.glow is not None else 0
        width = self.get_width()
        if slab <= 0 or width <= 0:
            return None
        side = ("left" if x < self.RESIZE_CORNER_PX
                else "right" if x > width - self.RESIZE_CORNER_PX else None)
        if slab <= y < slab + self.RESIZE_GRAB_PX:
            return side or "bottom"
        # ...and a corner reaches up into the pill as well, so it can be taken
        # AT the visible corner and not only from under it. Reported three
        # times: "the cursor has to be UNDER the corner of the window to resize
        # at the corner, it can't be AT the corner right now." Only the two
        # ends, and only RESIZE_CORNER_UP_PX rows of them — every row in
        # between is still the page's, which is the half of the last move that
        # must not be given back.
        if side and slab - self.RESIZE_CORNER_UP_PX <= y < slab:
            return side
        return None

    _RESIZE_CURSORS = {"bottom": "ns-resize", "left": "sw-resize",
                       "right": "se-resize"}

    def _grip_zone(self, x: float) -> str | None:
        """The zone for a point in the grip's own coordinates.

        The band spans the full width just below the slab's bottom edge, so its
        x is the window's and its y is a foregone conclusion — any row of the
        band is the same answer, which lets both it and _resize_zone() be asked
        the same question.
        """
        slab = self.glow.get_height() if self.glow is not None else 0
        return self._resize_zone(x, slab)

    def _on_resize_motion(self, _c, x: float, _y: float) -> None:
        if self._resizing is not None:
            return                      # mid-drag; the cursor is already right
        want = self._RESIZE_CURSORS.get(self._grip_zone(x) or "", "ns-resize")
        if want == self._resize_cursor:
            return
        self._resize_cursor = want
        try:
            self.grip.set_cursor(Gdk.Cursor.new_from_name(want, None))
        except Exception:
            pass

    def _grip_live(self) -> bool:
        """Whether the grab band is up, and therefore wants rows from X.

        Asked by _shape_now() on every frame of every animation, so it is a
        widget lookup and nothing else. getattr, because the shape is set from
        an idle callback that can beat _build_ui() to the window.
        """
        grip = getattr(self, "grip", None)
        return grip is not None and grip.get_visible()

    def _set_grip_visible(self, showing: bool) -> None:
        # The corners go up and down with the band: they are the same grab,
        # and a corner left live over a shut panel would take two nooks off
        # the bare pill for a resize that cannot happen.
        for corner in getattr(self, "corner_grips", ()):
            if corner.get_visible() != showing:
                corner.set_visible(showing)
        grip = getattr(self, "grip", None)
        if grip is not None and grip.get_visible() != showing:
            grip.set_visible(showing)
            # The band is rows of X input, not only a widget: it has to be
            # handed to the server as it comes up and taken back as it goes,
            # or the strip below a *collapsed* pill keeps swallowing clicks
            # that belong to the desktop.
            self._shape_now()

    def _on_corner_begin(self, gesture, _x: float, _y: float,
                         zone: str) -> None:
        """drag-begin for one of the two corner grips inside the slab.

        The zone is the widget's, not the pointer's: a corner grip is only
        ever its own corner, and asking _grip_zone() would answer for the
        band below the pill using an x measured inside a 28px child.
        """
        self._begin_resize(gesture, zone)

    def _on_resize_begin(self, gesture, x: float, _y: float) -> None:
        self._begin_resize(gesture, self._grip_zone(x))

    def _gesture_origin_x(self, gesture) -> float | None:
        """Where this gesture's own grip starts, in the window's coordinates.

        A GestureDrag offset is measured from where the WIDGET stood when the
        press landed, so a grip that moves when the window resizes subtracts
        its own effect from every offset it reports. Which grips move:

            the straight band   halign FILL, origin.x 0 at every width
            the left corner     halign START, origin.x 0 at every width
            the right corner    halign END, origin.x = width - 28

        so only the right corner does — and it moves one for one with the
        width the drag is setting, which is a closed feedback loop with a gain
        of one and no damping at all. Measured with real pointer events
        injected through mutter's RemoteDesktop, scale 1, a flat 8 px/frame
        drag of 192 device px on the right corner grip of 1.26.0: the width
        went 768, 776, 768, 784, 768, 792, 768 ... alternating every frame
        between 768 and an ever wider value, worst single-frame step 176
        device px, and 192 px of hand bought 8 px of window. The same drag on
        the band's right end — the widget that does not move — was 192 -> 192
        with no astray frame, on 1.26.0 and on 1.24.0 both.

        Taken from the grip's ACTUAL laid-out origin rather than from
        CFG["window_width"], for the reason _pinned_w exists: the config is
        written the instant a frame decides a width, and a compensation
        computed from a width that has not been laid out yet runs ahead of the
        movement it is compensating for. compute_bounds() is a CSS border box
        and a gesture's point is in the widget's own space, but only the
        DELTA of this is ever used and the two differ by a constant.
        """
        try:
            widget = gesture.get_widget()
            if widget is None:
                return None
            ok, bounds = widget.compute_bounds(self)
            return bounds.origin.x if ok else None
        except Exception:
            return None

    def _begin_resize(self, gesture, zone: str | None) -> None:
        if zone is None:
            self._resizing = None
            return
        xid = self._xid()
        where = WM.get_position(xid) if xid else None
        self._resizing = (zone, self._width_in_force(),
                          int(CFG["panel_height"]), where)
        # Where this drag's own grip stands right now. Anything it moves by
        # from here is movement the gesture will report as the hand's and is
        # not — see _gesture_origin_x().
        self._resize_ox = self._gesture_origin_x(gesture)
        # ── the edge the left corner promises not to move ──
        # Written down once, in device pixels, and held by _pin_right_edge()
        # off the frame clock rather than moved from here. Two X connections
        # cannot be ordered against each other, and the fix is not to need
        # them ordered: the walk is placed for a width that has been WATCHED
        # arrive, in the phase of the frame it arrives in, and never for one
        # that has only been asked for. _pin_right_edge() carries the
        # measurements; the short version is that a placement may lag the
        # resize it compensates for and may never lead it, which is the same
        # rule the XShape clip in this file already lives by.
        self._release_pin()
        if zone == "left" and where is not None:
            size = self._window_size_device()
            if size is not None:
                self._resize_right = where[0] + size[0]
                self._pinned_w = size[0]
                self._pin_xid = xid
                self._pin_watch(True)
        # The top edge does not move while the bottom is dragged, so the anchor
        # is still the user's own choice and has to be written down before the
        # window changes height under _restore_pill_y().
        self._note_pill_y()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_resize_update(self, _gesture, dx: float, dy: float) -> None:
        if self._resizing is None:
            return
        zone, w0, h0, where = self._resizing
        scale = max(1, self._scale())
        # ── take our own movement back out of the offsets ──
        # A GestureDrag offset is measured from where the *widget* was when the
        # press landed, and this gesture moves that widget: growing the panel
        # carries the band down with the bottom edge, and dragging the left
        # corner walks the whole window sideways. So every pixel applied comes
        # straight back out of the next offset — the panel grows, the report
        # shrinks by what it grew, the next frame gives it back, and the size
        # sits between the pointer and where it started, flickering between the
        # two and landing on whichever it happened to be on at the release.
        #
        # Both drifts are ours and both are known exactly, so adding them back
        # leaves the pointer's own movement and nothing else. It is a closed
        # loop rather than an accumulation: each update is recomputed from the
        # size that is actually applied, so a frame clamped at a floor or a
        # screen edge does not bend the ones after it.
        dy += int(CFG["panel_height"]) - h0
        # ...and the horizontal half of the same drift, for a grip that is
        # pinned to the very edge it is dragging. The right corner grip is
        # halign END: it slides right by every pixel the window widens, inside
        # a window whose left edge never moves, so the offset comes back short
        # by exactly what the last frame applied. Zero for the straight band
        # and for the left corner, which both start at 0 at every width, so
        # this line changes only the grip that actually moves.
        ox = self._gesture_origin_x(_gesture)
        if ox is not None and self._resize_ox is not None:
            dx += ox - self._resize_ox
        if zone == "left":
            # ...and the horizontal drift is the walk _pin_right_edge() has
            # ACTUALLY applied, not the width the config has been told about.
            # Those are two different numbers and using the wrong one is what
            # was left of this bug: the config is written the instant a frame
            # decides a width, while the walk was being computed from a size
            # GTK had not applied yet, so the compensation ran ahead of the
            # movement it was compensating for and the width oscillated around
            # the pointer instead of tracking it. Measured at 8 device px per
            # frame: the width went 700, 708, 724, 740, 748, 748, 748, 756 for
            # a pointer moving a flat 8 a frame. `_pinned_w` is the width the
            # walk was last done for, so `right - _pinned_w` is where the
            # window is, exactly, with no round trip and no guessing.
            walked = (self._pinned_w // scale) if self._pinned_w else w0
            dx -= walked - w0
        height = self._clamp_panel(int(round(h0 + dy)))
        width = w0
        if zone == "right":
            width = int(round(w0 + dx))
        elif zone == "left":
            width = int(round(w0 - dx))
        width = max(self.MIN_WINDOW_W, min(4000, width))
        try:
            rect = self._usable_rect(where or WM.pointer())
            width = min(width, max(self.MIN_WINDOW_W, rect[2] // scale))
            if zone == "left" and where is not None:
                # The left corner keeps the RIGHT edge still by walking the
                # window left, and it can only walk as far as the work area
                # goes. Without this the window against the left edge went on
                # widening while the move it depends on was refused — so the
                # one edge the gesture promises not to touch slid out from
                # under the other hand. Stop growing where the walk stops.
                room = (where[0] - rect[0]) // scale
                width = min(width, max(self.MIN_WINDOW_W, w0 + max(0, room)))
        except Exception:
            pass
        # The size request goes out and the walk does not follow it — the
        # walk follows the width's ARRIVAL, from the frame clock, a frame or
        # so later. Calling the pin here as well is not a second placement:
        # it is a no-op on every frame in which the width GTK reports has not
        # changed, and it is the only thing that places the edge at all if
        # there is no frame clock to hang the watch on.
        self._apply_panel_size(width, height)
        if zone == "left":
            self._pin_right_edge()

    def _pin_right_edge(self, *_a) -> None:
        """Walk the window left so the right edge stands still. Idempotent.

        Placed for the width the window HAS, never for the width it has just
        been asked for, and that distinction is the whole of this fix.

        ── what was wrong ──
        The old version placed the left edge for the width the frame had just
        decided on, and carried both in one ConfigureWindow so the server
        could not apply half of it. The server cannot; the screen can. X
        geometry changes the instant the request lands and the buffer behind
        it is a ConfigureNotify, a layout and a frame later, so for that frame
        the compositor shows the OLD width at the NEW x — the right edge one
        step of the hand to the left, every frame of the drag, breathing with
        the speed of the hand and snapping back whenever it paused.

        Measured on 1.24.0, sampling both pairs on every after-paint over
        seven drag shapes, 489 frames:

            server x + server width      2 frames astray, worst 16 device px
            server x + GDK    width    233 frames astray, worst 40 device px

        The first pair is the one the old check measured. It is also the one
        that is never composited: it is a report of two numbers that were
        true at different times.

        ── which width, and when ──
        The width GTK is about to paint is knowable, but not from the phase
        the motion handler runs in. Asking GdkSurface its width at each phase
        of the frame clock, against what that frame actually painted, over 39
        frames of a ramp:

            flush-events   4/39      layout       39/39
            before-paint   9/39      paint        39/39
            update         9/39      after-paint  39/39

        `update` is where tick callbacks and motion handlers run, and it is
        a full step behind. `layout` is the first phase that agrees, and it
        is still before the frame is committed — so the walk is done from
        there, for what the surface reports there, and the move reaches the
        server ahead of the buffer it belongs to. Nothing is predicted and
        nothing is counted, which matters because frames get dropped: a
        version of this that counted requests instead of watching widths was
        exact at scale 1 and astray on 34 of 341 frames at scale 2.

        `_pinned_w` is the width the walk has already been done for, in
        device pixels, so a frame in which the width has not moved costs one
        comparison and no X traffic at all — and it is also what
        _on_resize_update() takes the gesture's own movement back out with,
        which is only correct because it is a width that really arrived.

        WM.get_position() is deliberately not consulted: a synchronous round
        trip, and one that is not needed here at all. (The figure this used to
        quote — 6.1ms against a 13.35ms frame — did not survive re-measurement:
        see _track_geometry(), where it is timed properly. The reason stands on
        its own without it; nothing is gained by asking.) Neither
        is y — the request carries CWX and nothing else, so the top edge
        cannot be disturbed by it.
        """
        right, xid = self._resize_right, self._pin_xid
        if right is None or not xid:
            return
        size = self._window_size_device()
        if size is None or size[0] <= 0 or size[0] == self._pinned_w:
            return
        if self._resizing is None and not self._pin_grace_allows(size[0], xid):
            return
        self._pinned_w = size[0]
        WM.move_x(xid, right - size[0])

    def _pin_grace_allows(self, width: int, xid: int) -> bool:
        """Whether a width arriving AFTER the button came up is the drag's own.

        The grace exists for one thing: the last width the drag asked for is
        still a frame or two from being painted when the button comes up, and
        the walk follows the paint. It is not a licence to move the window for
        whatever width turns up in the next 300ms, and `_resize_right` is not a
        promise that survives the window being moved — it is an absolute root
        x, written down once at the press.

        Measured on 1.26.0 with real injected pointer events: a left-corner
        drag, then the window moved 300 px right inside the grace, then a +40
        width change for an unrelated reason — the pin walked the window from
        x=784 to x=444, a 340 device px sideways jump in one frame. An
        unrelated -60 width change inside the grace slid it 60 px with no drag
        in sight. Both are the "teleport".

        So: one width, the one the drag last asked for, and only while the
        window still stands where the drag left it. Anything else voids the
        promise and the edge is let go rather than chased. The position round
        trip happens at most once per grace and never on a frame of the drag
        itself — this is not consulted while `_resizing` is set — which was
        worth arranging when the trip was thought to cost 6.1ms and is simply
        free now that it is known to cost 0.08ms. See _track_geometry() for the
        measurement; the arrangement is kept because it is also the clearer
        rule.
        """
        if width != self._pin_wait_w:
            self._release_pin()
            return False
        if self._pin_wait_x is not None:
            here = WM.get_position(xid)
            if here is None or here[0] != self._pin_wait_x:
                self._release_pin()
                return False
        return True

    def _pin_watch(self, on: bool) -> None:
        """Put the walk on the frame clock's layout phase, or take it off.

        A signal on the clock rather than a tick callback: a tick callback
        forces frames, and this must not — it has nothing to say on a frame
        that would not have happened anyway, and it runs in the wrong phase
        besides.
        """
        clock = self._pin_clock
        if on:
            if clock is not None:
                return
            try:
                clock = self.get_frame_clock()
            except Exception:
                clock = None
            if clock is None:
                return
            self._pin_clock = clock
            self._pin_hid = clock.connect("layout", self._pin_right_edge)
            return
        if clock is not None and self._pin_hid:
            try:
                clock.disconnect(self._pin_hid)
            except Exception:
                pass
        self._pin_clock = None
        self._pin_hid = 0

    def _release_pin(self) -> None:
        """Let go of the edge, and of the frame clock with it."""
        if self._pin_grace:
            GLib.source_remove(self._pin_grace)
            self._pin_grace = 0
        self._pin_watch(False)
        self._resize_right = None
        self._pin_xid = 0
        self._pinned_w = None
        self._pin_wait_w = None
        self._pin_wait_x = None

    # How long the walk keeps watching after the button comes up. The width
    # the drag last asked for is still a frame or two from being painted at
    # that moment, and the walk follows the paint — so letting go here would
    # be the one frame of lag made permanent, which is the mark the old code
    # left behind. A ceiling rather than a wait: nothing happens on the
    # frames after the size has stopped moving.
    RESIZE_PIN_GRACE_MS = 300

    def _on_resize_end(self, *_a) -> None:
        if self._resizing is None:
            return
        self._resizing = None
        # The one width the grace may act on, written down before the pin is
        # asked one last time — that call is inside the grace too, because
        # _resizing has just been cleared. The position is written down after
        # it, because that call is the walk whose landing place this is.
        self._pin_wait_w = self._width_in_force() * max(1, self._scale())
        self._pin_wait_x = None
        self._pin_right_edge()
        if self._resize_right is not None:
            here = WM.get_position(self._pin_xid) if self._pin_xid else None
            self._pin_wait_x = here[0] if here else None
            if self._pin_grace:
                GLib.source_remove(self._pin_grace)
            self._pin_grace = GLib.timeout_add(self.RESIZE_PIN_GRACE_MS,
                                               self._pin_grace_over)
        CFG.save()
        self._cap_drawers()

    def _pin_grace_over(self) -> bool:
        self._pin_grace = 0
        if self._resizing is None:
            self._release_pin()
        return False

    def _apply_panel_size(self, width: int, height: int) -> None:
        """Put a width and a panel height on the window, skipping the no-ops."""
        # Both, and they are two different things: what the window is wearing
        # is what the next frame of this drag measures from, while the config is
        # the width the user has just chosen and is what _clamp_width() narrows
        # from on some other screen. Each is skipped when it is already right.
        if width != self._width_in_force():
            self.set_default_size(width, -1)
        if width != int(CFG["window_width"]):
            CFG.data["window_width"] = width
        if height != int(CFG["panel_height"]):
            # The config is what expand() reads, so it is written whether or not
            # there is a view to put the request on yet.
            CFG.data["panel_height"] = height
            if self.web is not None:
                self.web.set_size_request(-1, height)
        self._track_geometry()

    # ── dragging the pill by its own text field ─────────────────────────
    #
    # The bar carries a drag gesture of its own, but a GtkEntry consumes the
    # button press before the bar ever sees it — so the widest part of the pill,
    # and the obvious place to grab it, was the one part that would not move the
    # window.
    #
    # The rule is simply whether there is anything there to interact with. An
    # empty field has nothing to select anywhere, so all of it is a handle. Once
    # there is text, only the run of emptiness past its last letter is; a drag
    # that starts within reach of the text still selects backwards from the end,
    # because that is what someone reaching for the end of what they typed means.
    ENTRY_DEAD_ZONE_PX = 48     # how far past the last glyph text-land extends
    ENTRY_DRAG_SLOP_PX = 4      # below this it is still a click, not a drag

    def _wire_entry_drag(self) -> None:
        self._entry_drag_armed = False
        self._entry_dragging = False
        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        # CAPTURE: this has to decide before the inner GtkText starts selecting.
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._on_entry_drag_begin)
        drag.connect("drag-update", self._on_entry_drag_update)
        drag.connect("drag-end", self._on_entry_drag_end)
        self.entry.add_controller(drag)

    def _wire_entry_drop(self) -> None:
        """Own the drops that land on the search field itself.

        This is the whole of the first drag-and-drop bug. GtkText installs a
        drop target of its own — measured on a bare GtkEntry: the inner GtkText
        carries `DropTarget gtypes=['gchararray'] actions=3` — and a child's
        target is reached before this window's. So a picture dragged onto the
        field was read as the one thing GtkText asks for, its address, and
        typed into the box: the entire gesture produced the literal text
        "file:///home/…/image.png" and no search at all. The window's target,
        with its carefully ordered gtypes, never saw the drop, because the
        field is what a hand aims at.

        GtkText's is silenced rather than layered over, so that exactly one
        drop target on this widget is live and there is no propagation order to
        reason about. Words dropped on the field still land in the field —
        _on_entry_drop() puts them there — so that behaviour is kept
        deliberately, not lost by accident.

        Silenced by its PHASE, not by remove_controller(). GtkText holds its
        own pointer to that target in its private struct and goes on using it
        (gtk_text_set_editable() reaches for it, among others); removing the
        controller drops the widget's reference and leaves that pointer behind,
        which is a crash waiting for whichever code path asks next. Setting the
        phase to NONE takes it out of event propagation completely while
        leaving the object alive and owned exactly as GtkText expects.

        COPY only, where GtkText offered COPY|MOVE. A move would have to delete
        the words from wherever they came from, and nothing here can: dragging
        a selection within the field now copies it, which is the honest
        outcome rather than one that half-happens.
        """
        inner = self.entry.get_first_child()        # the GtkText holding the caret
        if inner is None:
            return
        try:
            controllers = inner.observe_controllers()
            existing = [controllers.get_item(i)
                        for i in range(controllers.get_n_items())]
        except Exception:
            existing = []
        for controller in existing:
            if isinstance(controller, Gtk.DropTarget):
                controller.set_propagation_phase(Gtk.PropagationPhase.NONE)
        drop = Gtk.DropTarget.new(Gdk.Texture, Gdk.DragAction.COPY)
        # Pixels before paths, for the reason set out at length in _wire_keys():
        # gtk_drop_target_match() walks the TARGET's list, and a picture dragged
        # out of a web page offers its address as well as its pixels.
        drop.set_gtypes([Gdk.Texture, Gio.File, GObject.TYPE_STRING])
        drop.connect("drop", self._on_entry_drop)
        inner.add_controller(drop)
        self._entry_drop = drop

    def _entry_text_end_x(self) -> float:
        """Where the last glyph ends, in the entry's own coordinates."""
        start = 0.0
        inner = self.entry.get_first_child()        # the GtkText holding the caret
        if inner is not None:
            try:
                ok, bounds = inner.compute_bounds(self.entry)
                if ok:
                    start = bounds.origin.x
            except Exception:
                pass
        text = self.entry.get_text()
        if not text:
            return start
        try:
            # Measured with the entry's own style, so the 16.5px face in the CSS
            # is what gets measured rather than some default.
            return start + self.entry.create_pango_layout(text).get_pixel_size()[0]
        except Exception:
            return start

    def _entry_dead_space(self, x: float) -> bool:
        if not self.entry.get_text():
            return True
        return x > self._entry_text_end_x() + self.ENTRY_DEAD_ZONE_PX

    def _on_entry_drag_begin(self, gesture, x: float, _y: float) -> None:
        self._entry_dragging = False
        self._entry_drag_armed = self._entry_dead_space(x)
        # Claim only what we mean to handle; everything else is GtkText's, so
        # clicking into words and selecting them behave exactly as before.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED if self._entry_drag_armed
                          else Gtk.EventSequenceState.DENIED)

    def _on_entry_drag_update(self, gesture, dx: float, dy: float) -> None:
        if not self._entry_drag_armed or self._entry_dragging:
            return
        if abs(dx) < self.ENTRY_DRAG_SLOP_PX and abs(dy) < self.ENTRY_DRAG_SLOP_PX:
            return                      # indistinguishable from a click so far
        self._entry_dragging = True
        self._begin_window_move(gesture, None, dx, dy)

    def _after_drag(self) -> None:
        """Everything a finished drag means, wherever it was started from.

        Where the pill has just been dropped is where the user wants it, and
        until this was written down nothing knew. The anchor still held the
        position from before the drag, so the next thing to run the geometry
        ticker — typing, which opens the suggestion list, or collapsing the
        panel — ended in _restore_pill_y() yanking the window back to where it
        used to be. Measured: dragged to the top of the screen, one keystroke
        later it was 247px lower down.

        On a short delay because the move belongs to the window manager and is
        not necessarily finished the moment the gesture is.
        """
        GLib.timeout_add(120, self._note_dropped_position)
        # Dragging is the one way the pill crosses to another monitor while it is
        # open, so it is the one place the cursor size can go stale on screen.
        GLib.idle_add(self._sync_cursor_size)
        GLib.idle_add(lambda: (self._update_arrow(), False)[1])

    def _on_bar_drag_update(self, gesture, dx: float, dy: float) -> None:
        if self._bar_dragging:
            return
        if abs(dx) < self.ENTRY_DRAG_SLOP_PX and abs(dy) < self.ENTRY_DRAG_SLOP_PX:
            return                      # indistinguishable from a click so far
        self._bar_dragging = True
        # Take the sequence away from whatever it started on. Most of the bar is
        # buttons, and up to here the press belongs to one of them.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._swallow_next_click()
        # A MenuButton pops its popover on the *press*, before there was any way
        # to know a drag was coming, so the ⋯ menu is already open by now.
        self._dismiss_menu()
        self._begin_window_move(gesture, self.bar, dx, dy)

    def _on_bar_drag_end(self, _gesture, _dx: float, _dy: float) -> None:
        if not self._bar_dragging:
            return                      # a click on the bar, not a drag
        self._bar_dragging = False
        self._after_drag()

    def _on_entry_drag_end(self, _gesture, _dx: float, _dy: float) -> None:
        self._after_drag()
        armed, dragged = self._entry_drag_armed, self._entry_dragging
        self._entry_drag_armed = self._entry_dragging = False
        if armed and not dragged:
            # A plain click on dead space. GtkText never received it, so place
            # the caret where clicking past the end of a line always puts it.
            self.set_focus(self.entry)
            self.entry.grab_focus()
            self.entry.set_position(-1)

    def _note_dropped_position(self) -> bool:
        """Adopt the spot a drag left the pill in, and re-cap the drawers for it."""
        self._note_pill_y()
        # The drawers are capped against the room under the pill, so a pill that
        # has just moved needs that worked out again — otherwise the first list
        # opened after a drag is sized for where the pill used to be.
        self._cap_drawers()
        # ...and so does the panel, which is the same argument about a much
        # bigger thing: a drag can cross onto a shorter screen. See _fit_panel().
        self._fit_panel()
        return False

    def _begin_window_move(self, gesture, widget=None,
                           dx: float = 0.0, dy: float = 0.0) -> None:
        """Hand the drag to the window manager, the way GtkWindowHandle does.

        `widget` is whose coordinates the gesture's start point is in — the
        entry for a drag on its dead space, the bar for one anywhere else — and
        it has to be the right one, because the point handed to begin_move is
        where the window manager believes the pointer grabbed the window. It is
        mapped into this window's coordinates with compute_point(), which is
        the same transform an arriving event goes through; see below for what
        taking the origin off compute_bounds() instead cost.

        `dx`/`dy` are how far the pointer has moved since that start point, and
        they are added to it, so what the window manager is told is where the
        pointer *is*. Neither gesture hands over on the first motion — both wait
        out ENTRY_DRAG_SLOP_PX to tell a drag from a click — so by the time this
        runs the start point is already stale by up to that much, and mutter
        closes the gap the only way it can: by moving the window that far, in
        one step, before the drag has visibly begun. That is the pill jumping a
        few pixels the moment it is picked up.
        """
        surface = self.get_surface()
        if surface is None:
            return
        ok, x, y = gesture.get_start_point()
        if not ok:
            return
        # compute_point(), and NOT compute_bounds().origin, because those are
        # two different rectangles. A gesture reports its point in its widget's
        # own coordinate space — the space gtk_widget_pick() puts an arriving
        # event into — and compute_point() is the transform from that space to
        # this window's. compute_bounds() hands back the widget's CSS *border
        # box*, which does not begin at the widget's own origin whenever the
        # CSS box is not the allocation. Measured on the styled pill:
        #
        #     widget   compute_bounds().origin   own origin      error
        #     bar        (1, 1)                    (17, 11)   (-16, -10)
        #     entry      (55, 11)                  (57, 13)   ( -2,  -2)
        #     a chip     (638, 15)                 (638, 15)  (  0,   0)
        #
        # The bar's 16 and 10 are its own padding, .halo-bar's 10px 10px 10px
        # 16px: the bar is allocated its content box, so its border box starts
        # up and to the left of where its coordinates do.
        #
        # That is the whole bug. The window manager anchors the drag on the
        # point it is given and keeps that point under the pointer, so a point
        # short of the pointer puts the window that much further right and
        # down the moment the drag begins. The bar is the origin widget for
        # every drag that does not start in the entry — a chip, the mark, the
        # ⋯ menu, the bar's own padding — so all of those jumped 16px right
        # and 10px down, every time, whichever way the hand went; the entry's
        # dead space jumped its own 2px. "Always when dragging on one of the
        # icons, sometimes otherwise" is those two numbers.
        #
        # Measured with a real injected pointer drag, X window origin sampled
        # on after-paint: (16, 10) and (2, 2) before, (0, 0) for both after.
        try:
            got, at = (widget or self.entry).compute_point(
                self, Graphene.Point().init(x + dx, y + dy))
            if not got:
                return
        except Exception:
            return
        try:
            event = gesture.get_last_event(gesture.get_current_sequence())
            surface.begin_move(gesture.get_device(),
                               int(gesture.get_current_button()),
                               at.x, at.y,
                               event.get_time() if event else Gdk.CURRENT_TIME)
            # The disc used to be hidden here, because it was a window of its
            # own that could only trail a frame behind a drag it was never told
            # about. It is a widget in this window now, so it comes along by
            # itself — and taking it away and putting it back is a blink, which
            # is what "the floating button blinks a single time when dragging"
            # was. Nothing to do.
        except Exception:
            return
        finally:
            gesture.reset()

    # ── keyboard ────────────────────────────────────────────────────────
    def _on_key(self, _c, keyval: int, _code: int, state: Gdk.ModifierType) -> bool:
        # The "Add a shortcut" dialog wants the raw keystroke, and this handler
        # sits above it in the capture phase, so it has to hand it over rather
        # than act on it — otherwise Escape hides Halo and ↑ opens the history
        # list while somebody is trying to press a shortcut into a dialog.
        if self._accel_capture is not None:
            return self._accel_capture(keyval, state)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        kv = keyval

        if kv == Gdk.KEY_Escape and self.finding:
            self.leave_find()
            return True
        if self.finding and kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            # Intercepted here so the entry never activates and runs a search.
            self._find_step(forward=not shift)
            return True
        if ctrl and kv in (Gdk.KEY_F, Gdk.KEY_f):
            if not self.expanded:
                return False        # nothing to search; leave the key alone
            self.toggle_find()
            return True

        if kv == Gdk.KEY_Escape:
            # The history is a layer over the pill, so Escape peels that off
            # first. Having one key dismiss both meant the only way out of the
            # list was to lose the popup with it.
            if self.history_open:
                # close_history() puts the suggestions back; see there.
                self.close_history()
                return True
            self.hide_popup()
            return True

        if ctrl and shift and kv in (Gdk.KEY_S, Gdk.KEY_s):
            self.on_circle_to_search()
            return True
        # Shift, so plain Ctrl+C is left to the page for copying a selection.
        if ctrl and shift and kv in (Gdk.KEY_C, Gdk.KEY_c):
            self.on_copy_url()
            return True
        # Before the plain Ctrl+V branch below, which would otherwise claim this
        # too — both spellings of the key are in its tuple.
        if ctrl and shift and kv in (Gdk.KEY_V, Gdk.KEY_v):
            self._paste_and_search()
            return True
        if ctrl and kv in (Gdk.KEY_U, Gdk.KEY_u):
            self.on_pick_image()
            return True
        if ctrl and kv in (Gdk.KEY_L, Gdk.KEY_l, Gdk.KEY_K, Gdk.KEY_k):
            self.focus_entry(select_all=True)
            return True
        if (ctrl and kv in (Gdk.KEY_R, Gdk.KEY_r)) or kv == Gdk.KEY_F5:
            if not self.expanded:
                return False        # nothing to reload; leave the key alone
            self._reload()
            return True
        if ctrl and kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.on_open_external()
            return True
        if ctrl and kv in (Gdk.KEY_V, Gdk.KEY_v) and self._typing_in_entry():
            if self._try_clipboard_image():
                return True
            return False
        if alt and kv == Gdk.KEY_Left:
            self._history(-1)
            return True
        if alt and kv == Gdk.KEY_Right:
            self._history(1)
            return True
        if ctrl and kv in (Gdk.KEY_Up, Gdk.KEY_Down) and self.expanded:
            # DOWN grows, up shrinks, and the direction is the panel's rather
            # than the size's: the panel only ever grows downward, out of the
            # pill's bottom edge, so the key that means "more of it" is the one
            # pointing the way it goes. Naming the *size* instead — up for
            # taller — reads fine in a table and backwards under the hand, which
            # is how it was reported.
            #
            # This has been turned round twice now, so: every place that states
            # the direction is listed in the README section on the panel, and
            # they have to move together. A shortcut whose tooltip disagrees
            # with the key is worse than either choice.
            self._resize_panel(-80 if kv == Gdk.KEY_Up else 80)
            return True

        if kv in (Gdk.KEY_Up, Gdk.KEY_Down) and self._on_arrow(kv == Gdk.KEY_Up):
            return True

        # Tab completes whichever list is showing, and → does the same once a row
        # is picked — which is what a hand reaches for after arrowing down into
        # one. Only once it is picked, though: → is the caret's own key, and
        # taking it from an unselected list would break moving through text.
        tab = kv in self._TAB_KEYS
        if tab or kv in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            # Shift+Tab arrives as ISO_Left_Tab and means "the other way", which
            # is not a completion — it is caught by the focus rule below with
            # the rest of Tab.
            if kv != Gdk.KEY_ISO_Left_Tab and self._complete_from_list(
                    picked_only=not tab):
                return True
            # ── and Tab never carries the caret out of the field ──
            #
            # This used to say so only while a list was showing, which is the
            # half of the sentence the comment could see. The other half: press
            # Tab on a pill with no list up — an empty one, or a query Google
            # has no suggestions for — and the focus went to the ⋯ button.
            # Every chip on that bar is set_can_focus(False); GtkMenuButton's
            # inner toggle is not, and it was the one Tab target besides the
            # field. Measured with a real child_focus(TAB_FORWARD): the caret
            # left the entry and landed on that toggle.
            #
            # What made it worse than a stray focus ring is what happened next.
            # Nothing on that button answers a letter, and the rescue at the
            # foot of this handler — the one that puts a stray character into
            # the field — only fires when NOTHING holds the focus. The button
            # does. So every printable key after that Tab went nowhere at all:
            # typing into the pill was simply dead until it was clicked or
            # Ctrl+L pressed, with no sign of why.
            #
            # So Tab belongs to the field unless the PAGE has the caret, where a
            # form on a result page still needs it. With nothing focused at all
            # it belongs to the field too — which is the useful answer as well
            # as the safe one, since that is the state a freshly mapped window
            # is in.
            if tab and not self._focus_in_page():
                if not self._typing_in_entry():
                    self.focus_entry()
                return True

        # Backspace at the very start of the field takes the attached picture
        # off — the second way out of it, and the one a hand finds without
        # being told, because it is what every field with a chip in it does.
        #
        # Three guards, and each of them is the difference between this and a
        # key that eats characters. The caret must be in the field, so
        # Backspace on a focused page is still the page's. Nothing may be
        # selected, or this would delete the picture instead of the selection.
        # And the caret must be at position zero — where Backspace had nothing
        # to delete anyway, whether the field is empty or the caret simply sits
        # in front of the first character. get_selection_bounds() returns an
        # empty tuple when there is no selection, not a flag; measured.
        if (kv == Gdk.KEY_BackSpace and self.attached
                and self._typing_in_entry()
                and not self.entry.get_selection_bounds()
                and self.entry.get_position() == 0):
            self.detach_image()
            return True

        # Forgetting one entry from the history list. Shift, following every
        # browser: plain Delete is the caret's key, and stealing it would delete
        # an entry every time someone edited what they had typed. Intercepted
        # here rather than left to the list, because the caret stays in the field.
        if self.history_open and shift and kv in (Gdk.KEY_Delete,
                                                  Gdk.KEY_KP_Delete):
            self._forget_selected()
            return True

        # A printable key that arrived while nothing at all holds the caret. That
        # is the cold-map window: the surface exists and is taking keys, but the
        # compositor has not handed focus over yet, so the character had nowhere
        # to go and was simply lost. Since nothing else is focused it can only
        # have been meant for the search field — put it there, so typing straight
        # into a popup that is still animating in behaves like typing into one
        # that has settled. A focused WebView or entry never reaches this.
        if self.get_focus() is None and not (ctrl or alt):
            char = Gdk.keyval_to_unicode(kv)
            if char and chr(char).isprintable():
                self.entry.set_text(self.entry.get_text() + chr(char))
                self.set_focus(self.entry)
                self.entry.grab_focus()
                self.entry.set_position(-1)
                return True
        return False

    # There was a _list_showing() here, and it is worth saying where it went.
    # It answered "is there a list to complete from", and Tab used it to decide
    # whether to keep the focus — which made holding the caret conditional on a
    # drawer being open, and that was the bug. Whether a list is showing is
    # still _complete_from_list()'s question, asked and answered there; it was
    # never the right question for the focus, so nothing asks it twice now.

    def _complete_from_list(self, picked_only: bool) -> bool:
        """Put the highlighted suggestion or past search into the field.

        `picked_only` is what keeps → usable for ordinary typing: with nothing
        highlighted it declines, so → still moves the caret. Tab does not need
        that guard and completes the first row when none is picked, which is what
        it has always done for suggestions.
        """
        if self.history_open:
            texts = [entry["q"] for entry in self.history_rows]
            listbox, refill = self.history_list, True
        elif self.suggest_reveal.get_reveal_child() and self.suggest_items:
            texts = list(self.suggest_items)
            listbox, refill = self.suggest_list, False
        else:
            return False
        row = listbox.get_selected_row()
        idx = row.get_index() if row is not None else -1
        if idx < 0:
            if picked_only:
                return False
            idx = 0
        if not (0 <= idx < len(texts)):
            return False
        self._set_entry_text(texts[idx])
        if refill:
            # The field is the history's filter, so completing into it re-filters.
            self._fill_history()
            self._track_geometry()
        return True

    def _on_arrow(self, up: bool) -> bool:
        """↑ and ↓, as one ladder. True when the key has been dealt with.

        Halo stacks four things on top of each other, and ↑ steps out of them in
        order while ↓ steps back in:

            the page (once it is scrolled to its top)
              → the search field
                → the bare pill
                  → the history list

        Written as one function on purpose. This logic used to be a chain of
        elifs hanging off "are suggestions showing", which meant the panel's own
        ↑ was silently eaten whenever a suggestion list happened to be live —
        search for something, press ↑, and nothing happened until the field was
        emptied, because emptying it was what closed the list. A ladder cannot go
        wrong that way: each rung is asked in order and says whether it took the
        key.
        """
        # ── the history list ──
        if self.history_open:
            if not up:
                self._scroll_history_to(self._move_row(self.history_list, 1))
                return True
            row = self.history_list.get_selected_row()
            if row is None:
                # Nothing picked, so there is nowhere up to go inside the list:
                # ↑ leaves it. This is the way back out that the list had no key
                # for at all — ↑ opened it and then no arrow would close it.
                # close_history() puts the suggestions back; see there.
                self.close_history()
                return True
            if row.get_index() == 0:
                # Off the top of the list, but not out of it yet: one more ↑
                # closes. Forgiving, since ↑ is being held down to get here.
                self.history_list.unselect_all()
                return True
            self._scroll_history_to(self._move_row(self.history_list, -1))
            return True

        # ── a suggestion list is a list; the arrows walk it ──
        if self.suggest_reveal.get_reveal_child() and self.suggest_items:
            row = self.suggest_list.get_selected_row()
            if up and row is None:
                # Nothing picked yet, so ↑ is not "the previous suggestion" — it
                # is the reach backwards for history, filtered by what was typed.
                self.open_history()
                return True
            if up and row.get_index() == 0:
                # Off the top of the list and back to the field, which is where
                # what you typed still is — and it is highlighted already, so
                # letting go of the row is the whole of it. Sticking on the first
                # row instead meant ↓ into the list was a one-way trip.
                self.suggest_list.unselect_all()
                return True
            self._move_suggestion(-1 if up else 1)
            return True

        # ── the results panel ──
        if self.expanded:
            if not up:
                return False            # ↓ is the page's, for scrolling
            if self._typing_in_entry():
                self.collapse()
                return True
            if self._page_at_top:
                # The caret is in the page and the page has nowhere further up to
                # go, so ↑ hands it back to the field — which is where writing
                # continues and where the pill's own shortcuts live. A second ↑
                # then collapses. Without this there was no way back at all: once
                # the page took focus, every ↑ went to a page that would not move.
                self.focus_entry()
                return True
            return False                # mid-page: ↑ scrolls, as it must

        # ── the bare pill ──
        if up:
            self.open_history()
            return True
        text = self.entry.get_text().strip()
        if ((not text or text == self.last_query)
                and (self._parked_uri
                     or (self.web is not None and self.web.get_uri()))):
            # Bring back the results without retyping — the other half of ↑, and
            # it has to work with the query still sitting in the field, because
            # that is where ↑ leaves it. This used to demand an empty field, so
            # searching something and closing the panel meant the only way back
            # was to delete what you had just typed.
            #
            # Not unconditional, though. ↓ still does nothing while a *different*
            # query is being typed: reopening the last search under a half-typed
            # new one is the surprise the empty-field rule was guarding against,
            # and one character is too few for a suggestion list to have claimed
            # the key first. So the rule is "↓ shows the results for whatever is
            # in the field", which covers both an empty pill and the moment just
            # after a search.
            #
            # The picture that was clipped to the field when this was closed
            # belongs to the page now coming back, so it comes back with it —
            # and before expand(), because expand() is what reopens the parked
            # page and _navigate() drops the stash on its way through.
            self._unstash_attachment()
            self.expand()
            return True
        return False

    def _point_in_page(self, x: float, y: float) -> bool:
        """Is this window coordinate inside the results page?

        Measured against the view's own allocation rather than asked of
        Gtk.Widget.pick(): pick depends on a widget being targetable and on the
        window being mapped, and it answered "not the page" for a point plainly
        inside it. A rectangle is a rectangle.
        """
        if self.web is None or not self.expanded:
            return False
        try:
            ok, bounds = self.web.compute_bounds(self)
        except Exception:
            return False
        if not ok:
            return False
        return (bounds.origin.x <= x <= bounds.origin.x + bounds.size.width
                and bounds.origin.y <= y <= bounds.origin.y + bounds.size.height)

    def _on_middle_click(self, gesture, n_press: int, x: float,
                         y: float) -> None:
        """Middle-click searches whatever is selected.

        Two places, two sources. On the pill it is the X11 primary selection, so
        selecting a phrase in any window and middle-clicking here looks it up. In
        the page it is the page's own selection, which reads a sentence out of an
        article and searches it — landing in the panel, so mouse-back and Alt+←
        come straight back to what you were reading.
        """
        if n_press != 1:
            return
        if self._point_in_page(x, y):
            # Deliberately NOT claimed. Middle-click inside a page is also the
            # X11 paste gesture and a text field there is entitled to it; asking
            # the page for its selection is asynchronous in any case, so there is
            # nothing left to claim by the time the answer comes back.
            self._search_page_selection()
            return
        # On the pill, claimed: the alternative is GtkEntry pasting the very same
        # text and then waiting for Enter, which is this with a step added.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._search_primary_selection()

    def _typing_in_entry(self) -> bool:
        """True when the caret is in the search field.

        GtkEntry delegates focus to an inner GtkText, so entry.has_focus() is
        always False here — ask where the focus actually sits instead.
        """
        focus = self.get_focus()
        return focus is not None and (focus is self.entry
                                      or focus.is_ancestor(self.entry))

    def _focus_in_page(self) -> bool:
        """True when the caret is in the results page rather than in the pill.

        Deliberately not the negation of _typing_in_entry(): with nothing
        focused at all both are False, and that third state is a real one —
        it is what a freshly mapped window is in, and it is the state the
        printable-key rescue at the end of _on_key() exists for.
        """
        focus = self.get_focus()
        if focus is None or self.web is None:
            return False
        return focus is self.web or focus.is_ancestor(self.web)

    def _move_suggestion(self, delta: int) -> None:
        row = self._move_row(self.suggest_list, delta)
        self._scroll_into_view(self.suggest_scroll, self.suggest_list, row)

    @staticmethod
    def _move_row(listbox: Gtk.ListBox, delta: int) -> Gtk.ListBoxRow | None:
        """Move the selection, clamped at both ends. Returns where it landed.

        Deliberately no wrapping: in a list this short, arriving back at the top
        after the last row reads as the key having done nothing.
        """
        rows = listbox.observe_children().get_n_items()
        if not rows:
            return None
        current = listbox.get_selected_row()
        idx = (current.get_index() if current else -1) + delta
        idx = max(0, min(rows - 1, idx))
        row = listbox.get_row_at_index(idx)
        if row:
            listbox.select_row(row)
        return row

    # ── entry / suggestions ─────────────────────────────────────────────
    def _set_entry_text(self, text: str, notify: bool = True) -> None:
        """Put text in the field as one change, not as a delete and an insert.

        GtkEditable.set_text() does exactly that — deletes what is there, then
        inserts the new text — and emits "changed" for each half. So the handler
        saw an EMPTY field before it saw the new text, and an empty field closes
        the suggestion list: traced, completing with Tab took the list from seven
        rows to none and back again, which is the shrink-then-grow that looked
        like the window glitching. Typing a character by hand only ever inserts,
        which is why it showed on Tab and not on ordinary typing.
        """
        handler = getattr(self, "_entry_changed_id", 0)
        if handler:
            self.entry.handler_block(handler)
        try:
            self.entry.set_text(text)
            self.entry.set_position(-1)
        finally:
            if handler:
                self.entry.handler_unblock(handler)
        if handler and notify:
            # Once, with the text that is actually there now.
            self._on_entry_changed()

    def _on_entry_changed(self, *_a) -> None:
        text = self.entry.get_text().strip()
        if self._suggest_timer:
            GLib.source_remove(self._suggest_timer)
            self._suggest_timer = 0
        if self.finding:
            self._find_update()
            return
        if self.history_open:
            # While the list is down the field is its filter, and nothing else:
            # no fetch, and no debounce either, because this is a substring match
            # over a few hundred strings held in memory and it should feel like
            # typing rather than like waiting.
            self._reset_clear_confirm()
            self._fill_history()
            self._track_geometry()
            return
        if not CFG["suggestions"] or len(text) < 2:
            # One character is not enough to ask about, but it is the moment to
            # open the connection: the handshake then overlaps the gap to the
            # next keystroke instead of delaying the first list.
            if CFG["suggestions"] and text:
                SUGGESTIONS.warm()
            self._show_suggestions([])
            return

        known = SUGGESTIONS.cached(text)
        if known is None and not self.suggest_reveal.get_reveal_child():
            # Nothing on screen yet, so lead with whatever is known locally
            # rather than letting a round trip decide when anything appears.
            #
            # Only when the list is closed, though. Doing it while a list is
            # already up meant two updates per keystroke — the local matches,
            # then the merged answer a moment later — so the window resized twice
            # for every character typed, which is what made it look chopped. And
            # completing with Tab replaced five rows with the one remembered match
            # before the new suggestions landed, which is the shrink-then-grow
            # that looked like a glitch. The worker always calls back, even with
            # an empty list, so keeping the old rows cannot strand them.
            local = self._suggestion_rows(text, [])
            if local:
                # ...and say that an answer is on its way, so the drawer opens
                # at the height that answer will want rather than the height of
                # these few rows and is not retargeted mid-slide. _reserve_rows.
                self._suggest_pending = text
                self._show_suggestions(local)
        if known is not None:
            # Already answered once — backspacing, or retyping what was just
            # deleted. Waiting out a debounce and a round trip for something in
            # hand is exactly what makes a suggestion list feel sluggish.
            #
            # This answer is the final one for this text, so nothing is on its
            # way and the flag has to come down. It is set on every keystroke
            # that starts a fetch, so without this a cached hit inherits the
            # previous keystroke's flag — type "wetterx", backspace to a cached
            # "wetter" — and _reserve_rows holds the drawer at the full budget
            # for an answer that is never coming.
            self._suggest_pending = ""
            self._show_suggestions(self._suggestion_rows(text, known))
            return

        def kick() -> bool:
            self._suggest_timer = 0      # cleared here, or the next keystroke
            fetch_suggestions(text, self._got_suggestions)   # removes a dead id
            return False

        # An answer is outstanding from here until one comes back for this text,
        # and that is true whether the drawer was shut when it was asked for or
        # already open. It used to be recorded only above, in the local lead —
        # the one case where the drawer was shut — so for every other keystroke
        # of a burst _reserve_rows had nothing to hold the height with, and a
        # list that came back short mid-word shrank the drawer instead of
        # standing at the budget the next answer is going to fill.
        self._suggest_pending = text
        self._suggest_timer = GLib.timeout_add(SUGGEST_DEBOUNCE_MS, kick)

    def _got_suggestions(self, query: str, items: list[str]) -> bool:
        text = self.entry.get_text().strip()
        if query != text:
            # The field moved on while this was in flight. Discarding it whole
            # is what left a typing burst showing nothing but past searches:
            # measured at 90-110ms a key, the round trip never fits between two
            # keystrokes, so every answer of the burst was dropped and the
            # drawer sat on the local lead until the typing slowed down.
            #
            # An answer for a *prefix* of what is now in the field is not junk.
            # It is a list of completions of a shorter prefix, and the ones that
            # still begin with the whole of what has been typed are completions
            # of that too — so every row that survives the filter says what is
            # in the field, exactly as a fresh answer's would. Measured over the
            # 26 prefixes of "wetter berlin" and "python decorators" against
            # real answers, one character behind leaves a median of 5 of the 6.
            #
            # Anything else — a different query, or nothing left after the
            # filter — is still ignored, and the reserve is still left standing
            # for the answer that is actually on its way. _suggest_pending is
            # deliberately NOT cleared here: while it is set, _reserve_rows
            # holds the drawer at the budget, so a short filtered list can fill
            # the rows it has without the height moving under the pill.
            if not (items and self._suggest_pending and text.startswith(query)):
                return False
            low = text.casefold()
            items = [s for s in items if s.casefold().startswith(low)]
            if not items:
                return False
            self._show_suggestions(self._suggestion_rows(text, items))
            return False
        self._suggest_pending = ""
        self._show_suggestions(self._suggestion_rows(text, items))
        return False

    # At most this many past searches among the suggestions. They are competing
    # for the same few rows as Google's, and the point is a reminder that you have
    # been here before — not a second history drawer.
    HISTORY_IN_SUGGEST = 3

    # How many rows the drawer shows in all, past searches and Google's
    # together. A constant, and being a constant is the whole of the point.
    #
    # Google's answer is already cut to six by _SuggestionService, and measured
    # against the real endpoint it comes back with all six for 137 of the 151
    # prefixes of ten ordinary queries, and changes length from one prefix to
    # the next 3 times in 141. Google is therefore not what moves the drawer.
    # The past searches are: HISTORY.similar() matches on word prefixes, so it
    # answers with three rows early in a word and none by the end of it, and
    # each of those rows arriving or leaving changed the height of an already
    # open drawer — which a Gtk.Revealer does not animate. Measured typing
    # "wetter berlin" a character every 160ms: three unanimated height changes
    # totalling 252px, 72 of which were history rows dropping out one by one.
    #
    # Sharing one fixed budget between the two lists takes that away for
    # nothing: a past search that stops matching frees a slot the next of
    # Google's fills, so the row count does not move and neither does the
    # drawer. Six rather than nine because six is what Google reliably supplies,
    # and a budget bigger than the supply is not a constant.
    #
    # The cost is real and worth saying plainly: with three close past searches
    # you see three of Google's suggestions rather than six.
    SUGGEST_ROWS = 6

    def _suggestion_rows(self, text: str, remote: list[str]) -> list[tuple]:
        """Close past searches first, then Google's, without repeating either.

        Two lists, one drawer, and the rows say which is which. Kept separate
        from the history drawer on purpose: that one is for browsing everything,
        this is for noticing that what you are typing is something you already
        looked up.
        """
        rows: list[tuple[str, bool]] = []
        seen: set[str] = set()
        if CFG["history_in_suggestions"] and text:
            for entry in HISTORY.similar(text, self.HISTORY_IN_SUGGEST):
                key = entry["q"].casefold()
                if key not in seen:
                    seen.add(key)
                    rows.append((entry["q"], True))
        for item in remote:
            key = item.casefold()
            if key not in seen:
                seen.add(key)
                rows.append((item, False))
        return rows[:self.SUGGEST_ROWS]

    def _show_suggestions(self, items: list, animate: bool = True) -> None:
        # Whether the list should be on screen depends on more than the items:
        # the history drawer being down hides it while keeping them, and so did
        # expanding, before expand() started coming through here itself. Skipping
        # the work on identical items alone therefore stranded the revealer shut —
        # search something, collapse the results, and typing the same query again
        # brought no suggestions back, because the cached list matched and this
        # returned before ever reopening it. Compare the visible state too.
        # ...and not while the history is down: a fetch started a keystroke
        # before the list opened still lands here, and two drawers at once is
        # both of them half visible.
        # Rows arrive either as plain strings or as (text, came-from-history)
        # pairs, so every existing caller and every test that passes a list of
        # strings still means what it did.
        rows = [(r, False) if isinstance(r, str) else (r[0], bool(r[1]))
                for r in items]
        texts = [text for text, _ in rows]
        show = bool(rows) and not self.expanded and not self.history_open
        # An answer that came back empty in the middle of a word is not a reason
        # to shut the drawer. Type far enough into a word that Google runs out of
        # suggestions and then keep going until it finds some again, and the
        # drawer slammed shut and reopened a keystroke later: measured, 371px
        # down and 371 back up, which is half the total travel of a whole typing
        # burst spent on an answer the user never saw.
        #
        # That travel is the thing worth removing, because steps and frames are
        # one axis and not two — frames * step = travel, so nothing that only
        # redistributes it can improve both ends, and pacing the height was
        # exactly that mistake. Holding the drawer open across the gap removes
        # the travel instead: no close, no reopen, no reversal in between.
        #
        # Only for an answer arriving on its own, and only while there is still a
        # query to answer. Escape, a search, the panel and the history drawer all
        # come through here with animate=False and shut it at once, as they must.
        if (not rows and animate
                and not self.expanded and not self.history_open
                and self._reveal_on_screen(self.suggest_reveal)
                and self.entry.get_text().strip()):
            self._hold_suggestions()
            return
        self._cancel_suggest_hold()
        if (texts == self.suggest_items
                and rows == self.suggest_rows
                and show == self.suggest_reveal.get_reveal_child()):
            return
        self.suggest_items = texts
        self.suggest_rows = rows
        # Only when they really changed, and never on the way out: emptying the
        # list is what _shut_suggestions() exists to stop happening early.
        if rows and rows != self._suggest_built:
            self._build_suggest_rows(rows)
        if show:
            self._cap_drawers()
            self._reserve_rows(len(rows))
            self.suggest_reveal.set_reveal_child(True)
        else:
            self._shut_suggestions(animate)
        self._arrow_soon()
        # Track the whole 140ms slide. Reshaping once at idle happened *before* the
        # window had grown, so the X11 bounding shape stayed at the height of the
        # bare pill while the window became a list taller — and X clips to that
        # shape, so the suggestions were cut off below it. Measured: window 140 ->
        # 522px, clip left at 140px, and it stayed wrong until some later keystroke
        # happened to resync it, which is why the list appeared to lag a character
        # behind. The ticker follows the animation and settles on the real size.
        self._track_geometry()

    # How long a drawer stays open on nothing, waiting to see whether the next
    # keystroke brings suggestions back. Long enough to cover a keystroke and a
    # round trip at ordinary typing speed; short enough that a query which
    # genuinely has no suggestions does not leave a stale list sitting there.
    SUGGEST_HOLD_MS = 320
    # ...and the ceiling on that, measured from the first empty answer of a run.
    #
    # The wait is re-armed by each empty *answer*, not by each keystroke — only
    # an answer reaches _show_suggestions(), and a keystroke on its own never
    # gets there. Answers arrive one per keystroke offset by the debounce and a
    # round trip, so the gap between two of them is the gap between two keys:
    # 150-250ms at ordinary speed, comfortably inside SUGGEST_HOLD_MS. Type
    # slower than that and the wait expires between keys, which is the right
    # answer — a pause that long means the stale list has stopped being about
    # what is in the field.
    #
    # Re-arming with no ceiling would make a query that genuinely has no
    # suggestions hold a stale list open for as long as the typing went on,
    # which is worse than the close it replaced. Hence a ceiling, and hence it
    # runs from the first empty answer of the run rather than the last.
    SUGGEST_HOLD_MAX_MS = 900

    def _hold_suggestions(self) -> None:
        """Keep the drawer as it is for a moment, in case suggestions return."""
        now = GLib.get_monotonic_time()
        if self._suggest_hold:
            if now - self._suggest_hold_since >= self.SUGGEST_HOLD_MAX_MS * 1000:
                return           # held long enough; let the pending close land
            GLib.source_remove(self._suggest_hold)
            self._suggest_hold = 0
        else:
            self._suggest_hold_since = now

        def give_up() -> bool:
            self._suggest_hold = 0
            # Something else may have shut it in the meantime, or opened it with
            # real rows — in which case there is nothing left to do here.
            if self._reveal_on_screen(self.suggest_reveal) and self.suggest_rows:
                self.suggest_items = []
                self.suggest_rows = []
                self._shut_suggestions(True)
                self._arrow_soon()
                self._track_geometry()
            return False

        self._suggest_hold = GLib.timeout_add(self.SUGGEST_HOLD_MS, give_up)

    def _cancel_suggest_hold(self) -> None:
        if self._suggest_hold:
            GLib.source_remove(self._suggest_hold)
            self._suggest_hold = 0

    # ── opening at the height the answer will need ──────────────────────
    #
    # A Gtk.Revealer animates the open and the close and nothing in between. A
    # change in the child's natural height while the revealer is open is a
    # relayout rather than a transition, so it lands whole in a single frame;
    # and a change that arrives while the opening slide is still running
    # retargets that slide in flight. The second of those is what "it still
    # jumps while the animation plays" was, and it had nothing to do with
    # typing — the app does it to itself.
    #
    # _maybe_suggest leads with the past searches it can match locally, so that
    # something is on screen before a round trip decides when anything appears.
    # That opens the drawer at the height of one, two or three rows. Google's
    # six land about 105ms later — a 60ms debounce and the fetch — while the
    # 140ms slide is still on its way down, and the drawer is retargeted from
    # under it. Measured typing "wetter berlin" a character every 160ms against
    # recorded answers from the real endpoint: 180px of retarget out of 252px of
    # unanimated movement in the whole burst, i.e. the single largest thing
    # there was to look at.
    #
    # So open at the height the answer is going to need instead of the height of
    # the placeholder, and the retarget has nothing left to do. The height is
    # *measured* rather than worked out, for the reason _cap_drawers is: the
    # rows are not all one height — measured, 59px for one row and 227 for six,
    # which is not six times anything — so the list is asked what it will stand
    # at with a full budget in it. The extra rows are appended and taken out
    # again with no main loop iteration in between, so no frame can contain
    # them, and the drawer is still shut at this point in any case.
    #
    # Only while an answer is actually outstanding. A list that is short because
    # Google has nothing more to say is short honestly, and holding that one
    # tall would be an empty box with nothing on its way to fill it — which is
    # exactly what the height hold this replaces got wrong.
    #
    # Clamped to the cap _cap_drawers() has just worked out, every time. A
    # drawer that outgrows the room under the pill takes the whole window up the
    # screen, and that is the one rule here that must not bend.
    def _reserve_rows(self, n: int) -> None:
        """Open the drawer at the height a full list of rows will need."""
        if n >= self.SUGGEST_ROWS or not self._suggest_pending:
            self.suggest_scroll.set_min_content_height(-1)
            return
        width = self.get_width()
        width = width if width > 0 else -1
        pad: list = []
        try:
            for _ in range(self.SUGGEST_ROWS - n):
                row = Gtk.ListBoxRow()
                line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
                icon.add_css_class("halo-icon")
                line.append(icon)
                line.append(Gtk.Label(label="x", xalign=0.0, hexpand=True))
                row.set_child(line)
                self.suggest_list.append(row)
                pad.append(row)
            full = self.suggest_list.measure(Gtk.Orientation.VERTICAL, width)[1]
        finally:
            for row in pad:
                self.suggest_list.remove(row)
        cap = self.suggest_scroll.get_max_content_height()
        if cap > 0:
            full = min(full, cap)
        if full > 0:
            self.suggest_scroll.set_min_content_height(full)

    def _build_suggest_rows(self, rows: list[tuple]) -> None:
        """Put a fresh set of rows in the list, replacing whatever was there."""
        while (child := self.suggest_list.get_first_child()) is not None:
            self.suggest_list.remove(child)
        for text, from_history in rows:
            row = Gtk.ListBoxRow()
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            # The same clock face the history drawer uses, so a row you have
            # seen before is recognisable without reading it.
            icon = Gtk.Image.new_from_icon_name(
                "document-open-recent-symbolic" if from_history
                else "system-search-symbolic")
            icon.add_css_class("halo-icon")
            line.append(icon)
            label = Gtk.Label(label=text, xalign=0.0, hexpand=True)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            line.append(label)
            row.set_child(line)
            self.suggest_list.append(row)
        self._suggest_built = list(rows)

    def _shut_suggestions(self, animate: bool = True) -> None:
        """Slide the drawer away with its rows still in it.

        Emptying the list first is what turned every backspace into a jump
        instead of a slide. The revealer then has nothing left to animate, so the
        window loses the entire list between two frames and eases the last few
        pixels shut on its own: measured, 431px straight down to 119px, against
        431 -> 226 -> 116 -> 70 -> 61 -> 60 for the same close with the rows left
        where they were.

        That jump costs more than smoothness, and this is the reason it is worth
        a method of its own. The X11 clip is a separate request from the resize
        and cannot land in the same instant as it (see _watch_surface), so a
        step the window takes in one frame is a step the clip is briefly wrong
        by. Wrong by three pixels nobody can see; wrong by 312 is a slab of
        square, rim-coloured corner across the bottom of the pill, which is
        exactly what "the edges go square and the bottom takes the colour of the
        edges" was. Small steps make the same gap invisible.

        The rows go once the slide has actually finished — _on_suggest_revealed.
        """
        if not animate:
            # About to be hidden, or covered by the panel or the other drawer: a
            # transition nobody will see still has to finish somewhere, and a
            # revealer stranded mid-slide by a frame clock going away comes back
            # half open. The same reasoning as close_history(animate=False).
            self.suggest_reveal.set_transition_duration(0)
            self.suggest_reveal.set_reveal_child(False)
            self.suggest_reveal.set_transition_duration(self._drawer_ms())
            self._clear_suggest_rows()
            return
        # Whatever the slide had reached, it now runs back down from there. This
        # deliberately does NOT ask whether the drawer finished opening first:
        # child_revealed only turns true at the end of the slide, so treating a
        # half-open drawer as "nothing to animate" snapped it shut in one step —
        # which is precisely the fg-then-backspace-immediately case, the one
        # place the jump was reported from. Measured with that test: a 369px step
        # became 369px of square corner for a frame.
        self.suggest_reveal.set_reveal_child(False)
        if not self.suggest_reveal.get_child_revealed():
            # It was already shut, so no slide will happen and nothing will come
            # back to tell us the rows can go.
            self._clear_suggest_rows()

    def _on_suggest_revealed(self, *_a) -> None:
        """Drop the rows, but only once the drawer has finished sliding shut.

        Guarded on both flags: reveal_child is where it is heading and
        child_revealed is where it is, and a list reopened mid-slide has to keep
        what it is showing.
        """
        if (not self.suggest_reveal.get_reveal_child()
                and not self.suggest_reveal.get_child_revealed()):
            self._clear_suggest_rows()
            # The window is short again, so the disc's resting spot is finally
            # the one under the pill rather than under the list.
            self._arrow_soon()

    def _clear_suggest_rows(self) -> None:
        # Before the early return, and not after it: every path that shuts the
        # drawer comes through here, including the ones that never built a row,
        # and a reserve left behind is a pill that will not shrink back.
        self._suggest_pending = ""
        self.suggest_scroll.set_min_content_height(-1)
        if not self._suggest_built:
            return
        while (child := self.suggest_list.get_first_child()) is not None:
            self.suggest_list.remove(child)
        self._suggest_built = []

    def _on_suggest_activated(self, _list, row: Gtk.ListBoxRow) -> None:
        idx = row.get_index()
        if 0 <= idx < len(self.suggest_items):
            self._set_entry_text(self.suggest_items[idx])
            self.run_search()

    # ── search history ──────────────────────────────────────────────────
    #
    # The dropdown under the mark, and the entry doubling as its filter. Two
    # things make this worth its own layer rather than a menu: it has to be
    # typed into, and it has to be reachable without the mouse — which is what
    # ↑ is for, the pill having no address bar to give the key a better job.
    HISTORY_MAX_PX = 300        # how tall the list may grow before it scrolls
    # Google returns ten suggestions and up to three past searches join them, so
    # the drawer can ask for around thirteen rows. Past this it is not a list
    # anyone reads, and the pill has nowhere left to sit.
    SUGGEST_MAX_PX = 400
    # Slack, on top of whatever the drawer's own chrome measures — being a few
    # pixels shorter than the room costs nothing and being a few pixels taller
    # costs the whole bug below.
    DRAWER_CHROME = 8
    # The smallest cap worth asking for. Below this a drawer is a scrollbar with
    # a sliver of list beside it, and asking for less buys nothing anyway: a
    # scrolled window will not measure shorter than its own scrollbar, which is
    # 58px in this theme. See _cap_drawers, where exceeding the room under the
    # pill is the one thing that must not happen.
    DRAWER_FLOOR_PX = 24

    def _cap_drawers(self) -> None:
        """Hold both drawers to the room actually under the pill.

        A drawer that outgrows that room makes the window taller than the screen
        allows; the window manager slides the whole window up so it fits, and
        _restore_pill_y() slides it back the instant the drawer shortens again.
        Hold Backspace on a long query and the list length changes with every
        repeat, so the pill jumps up and comes back — which is what "it resizes
        from the top as well as the bottom" is. Measured before this, with the
        pill parked 140px above the foot of the work area: six moves during one
        hold, the top edge travelling 861 -> 753 -> 642 -> 570 and back.

        Nothing moves the window to make room for a drawer, so the drawer is what
        has to give. Worked out from where the pill wants to be rather than from
        a constant, because a constant is only ever right for one position on one
        screen — and from the anchor rather than the live position, so the cap
        does not drift along with a window the compositor has already nudged.

        The cap itself is *measured* into place rather than calculated. Two goes
        at calculating it both got the arithmetic right and the answer wrong: one
        constant for the chrome of both drawers is 9px for the suggestion list
        and 39 for the history one, which has a title and a Clear all button in
        its header, so the history drawer opened 33px past the room and took the
        pill up the screen with it. Asking the widget what it will actually
        stand at cannot be out of date in that way, and a row added to either
        header cannot put it back.
        """
        room = None                 # logical px from the pill's top to the floor
        try:
            rect, ms = self._usable_pick(self._anchor_point())
            ms = max(1, ms)
            top = self._pill_y
            if top is None:
                xid = self._xid()
                where = WM.get_position(xid) if xid else None
                top = where[1] if where else None
            if top is not None:
                # rect and _pill_y are device pixels; widget heights are logical.
                room = (rect[1] + rect[3] - top) // ms
        except Exception:
            room = None
        width = self.get_width()
        width = width if width > 0 else -1
        for scroller, box, ceiling in (
                (self.suggest_scroll, self._sug_box, self.SUGGEST_MAX_PX),
                (self.history_scroll, self._hist_box, self.HISTORY_MAX_PX)):
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            if room is None:
                scroller.set_max_content_height(ceiling)
                continue
            # An upper bound that does not depend on what is in the list right
            # now, so a cap set while the drawer is empty is still a promise
            # about the drawer full. The squeeze below then takes it further
            # where the widget itself will not shrink to arithmetic.
            top_cap = min(ceiling, max(self.DRAWER_FLOOR_PX, room - BAR_HEIGHT
                                       - self._drawer_chrome(box, scroller)))
            if self._squeeze_drawer(scroller, box, top_cap, room, width):
                # Out of cap and still too tall for the room. A scrolled window
                # will not measure shorter than the scrollbar it is showing —
                # 58px in this theme, whatever the cap says — so the scrollbar is
                # the last thing left to give. EXTERNAL keeps the list scrolling,
                # by wheel and by ↑↓, and simply does not draw the bar. Only ever
                # reached with the pill within about 140px of the foot of the
                # screen, where the alternative is the window manager lifting the
                # whole window to make room and the pill riding up with it.
                scroller.set_policy(Gtk.PolicyType.NEVER,
                                    Gtk.PolicyType.EXTERNAL)
                self._squeeze_drawer(scroller, box, top_cap, room, width)

    def _drawer_chrome(self, box, scroller) -> int:
        """Everything in a drawer that is not the scrolling list, plus slack.

        One constant could not answer this for both of them, and using one is
        what let the history drawer push the pill up the screen. The suggestion
        drawer is a separator and a list; the history drawer is a separator, a
        title, a Clear all button and two more margins — measured, 39px against
        9, so a cap worked out with 8px of chrome in mind overshot the room by
        the whole of that header. Reproduced with the pill parked 241px above the
        foot of the work area: the drawer opened 274px tall, the window manager
        slid the window up 33px to fit, and it stayed up until the list closed.

        Measured rather than tabulated, so a row added to either header cannot
        put that back.
        """
        try:
            width = self.get_width()
            width = width if width > 0 else -1
            whole = box.measure(Gtk.Orientation.VERTICAL, width)[1]
            inner = scroller.measure(Gtk.Orientation.VERTICAL, width)[1]
        except Exception:
            return self.DRAWER_CHROME
        return max(0, whole - inner) + self.DRAWER_CHROME

    def _squeeze_drawer(self, scroller, box, ceiling: int, room: int,
                        width: int) -> int:
        """Cap one drawer to the room, and report what it could not give back.

        Down in steps of exactly what it overshot by. A scrolling list gives back
        a pixel of height per pixel of cap, so this lands in one pass whenever it
        can land at all; the other goes are for the boundaries where it cannot.
        """
        cap = ceiling
        scroller.set_max_content_height(cap)
        for _ in range(4):
            try:
                want = (BAR_HEIGHT + self.DRAWER_CHROME
                        + box.measure(Gtk.Orientation.VERTICAL, width)[1])
            except Exception:
                return 0
            over = want - room
            if over <= 0:
                return 0
            if cap <= self.DRAWER_FLOOR_PX:
                return over
            cap = max(self.DRAWER_FLOOR_PX, cap - over)
            scroller.set_max_content_height(cap)
        return max(0, over)

    HISTORY_LIMIT = 250         # rows built at once; the filter reaches the rest

    def toggle_history(self) -> None:
        if self.history_open:
            self.close_history()
        else:
            self.open_history()

    def open_history(self) -> None:
        """Drop the list down, filtered by whatever is already in the field."""
        if self.expanded:
            # One drawer at a time. The results panel and a 300px list would
            # together be taller than the screen on a laptop.
            self.collapse()
        self._show_suggestions([], animate=False)
        self.history_open = True
        self.mark_button.add_css_class("halo-mark-on")
        self._fill_history()
        self._cap_drawers()
        self.history_reveal.set_reveal_child(True)
        self._update_arrow()
        self._track_geometry()
        # Keep the caret where the typing goes: the list is navigated from the
        # entry, so focus must not move into it.
        self.focus_entry()

    def close_history(self, animate: bool = True) -> None:
        if not self.history_open:
            return
        self.history_open = False
        self._reset_clear_confirm()
        self.mark_button.remove_css_class("halo-mark-on")
        if not animate:
            # About to be hidden or covered: a transition nobody will see still
            # has to finish somewhere, and a revealer stranded mid-slide by a
            # frame clock going away comes back half open.
            self.history_reveal.set_transition_duration(0)
            self.history_reveal.set_reveal_child(False)
            self.history_reveal.set_transition_duration(self._drawer_ms())
        else:
            self.history_reveal.set_reveal_child(False)
        self._arrow_soon()
        self._track_geometry()
        if animate:
            # Put the suggestion list back as it was, so stepping out of the
            # history is not also losing the list you were typing against.
            #
            # Here rather than at the call sites, because there are three doors
            # out of the list and only two of them ever did this: Esc and ↑ off
            # the top each called _on_entry_changed() by hand, and a second
            # click on the Google mark — the same gesture that opened the list —
            # did not. Measured: "wet" with three suggestions showing, mark,
            # mark, and the drawer came back shut with suggest_items empty,
            # where Esc and ↑ from the identical state came back with all three.
            # One behaviour, three doors, so it lives in the door rather than in
            # each hand that opens one.
            #
            # Only the animated close. Every other caller — expand(),
            # run_search(), toggle_find(), _search_text() and hide_popup() —
            # passes animate=False precisely because it is about to put
            # something else on screen, and a suggestion list opening underneath
            # that is the bug this would otherwise become.
            self._on_entry_changed()

    def _fill_history(self) -> None:
        """(Re)build the rows for the current filter, and label what is shown."""
        needle = self.entry.get_text().strip()
        items = HISTORY.search(needle, self.HISTORY_LIMIT)
        self.history_rows = items
        while (child := self.history_list.get_first_child()) is not None:
            self.history_list.remove(child)
        for entry in items:
            self.history_list.append(self._history_row(entry))

        total = len(HISTORY.items)
        if needle:
            self.history_title.set_text(
                f"MATCHING “{needle}” · {len(items)}" if items
                else f"NO MATCH FOR “{needle}”")
        else:
            self.history_title.set_text(
                f"YOUR SEARCHES · {total}" if total else "YOUR SEARCHES")

        self.history_scroll.set_visible(bool(items))
        self.history_clear_btn.set_visible(bool(total))
        if items:
            self.history_empty.set_visible(False)
        else:
            if not total and not CFG["history"]:
                hint = ("History is switched off in ⋯ → HISTORY, so "
                        "nothing is being recorded.")
            elif not total:
                hint = "Nothing here yet — what you search will show up."
            else:
                hint = "Nothing matches. Clear the field to see everything."
            self.history_empty.set_text(hint)
            self.history_empty.set_visible(True)

    HIST_THUMB_PX = 22

    def _history_thumb(self, entry: dict) -> Gtk.Widget:
        """The little picture on a history row, or a stand-in for it.

        Decoded from the file the row points at as the list is filled, and kept
        in RAM for as long as the process lives. Nothing is written: a folder of
        cached thumbnails is exactly the per-image storage this design exists to
        avoid, and a history list is twenty rows on screen, not a gallery.

        A row that points only at a web address gets the generic icon. Fetching
        the picture to draw it would put the network in the middle of opening a
        list, and the row says which picture it was in words beside it.

        The misses are cached too — None is an answer, and a file that has been
        deleted should not be re-stat'ed on every keystroke while the list is
        being filtered.
        """
        img = entry.get("img") or {}
        path = img.get("path") or ""
        texture = None
        if path:
            if path in self._hist_thumbs:
                texture = self._hist_thumbs[path]
            else:
                try:
                    if Path(path).is_file():
                        texture = self._thumb_texture(path, self.HIST_THUMB_PX)
                except Exception:
                    texture = None
                if len(self._hist_thumbs) > 4 * HISTORY.MAX_ENTRIES:
                    self._hist_thumbs.clear()       # bounded, and cheap to refill
                self._hist_thumbs[path] = texture
        if texture is None:
            icon = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
            icon.add_css_class("halo-icon")
            return icon
        pic = Gtk.Image.new_from_paintable(texture)
        pic.set_pixel_size(self.HIST_THUMB_PX)
        pic.add_css_class("halo-hist-thumb")
        pic.set_overflow(Gtk.Overflow.HIDDEN)       # the CSS rounding is a clip
        return pic

    def _history_row(self, entry: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        # A globe for an address, a clock face for a query, the picture itself
        # for a visual search: the three kinds of entry do different things when
        # picked, so they must not look alike.
        if entry["kind"] == "image":
            icon = self._history_thumb(entry)
        else:
            icon = Gtk.Image.new_from_icon_name(
                "web-browser-symbolic" if entry["kind"] == "url"
                else "document-open-recent-symbolic")
            icon.add_css_class("halo-icon")
        line.append(icon)
        label = Gtk.Label(label=entry["q"], xalign=0.0, hexpand=True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        line.append(label)
        # Which picture, when the row is labelled with the words searched
        # alongside it rather than with the picture's own name. Without this a
        # row reads "Hey" and gives no way to tell two of them apart.
        img = entry.get("img") or {}
        name = img.get("name") or ""
        if entry["kind"] == "image" and name and img.get("words"):
            which = Gtk.Label(label=name, xalign=1.0)
            which.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            which.set_max_width_chars(18)
            which.add_css_class("halo-hist-when")
            line.append(which)
        if entry["n"] > 1:
            times = Gtk.Label(label=f"×{entry['n']}")
            times.add_css_class("halo-hist-when")
            line.append(times)
        when = Gtk.Label(label=relative_time(entry["at"]))
        when.add_css_class("halo-hist-when")
        line.append(when)
        drop = Gtk.Button(icon_name="window-close-symbolic")
        drop.add_css_class("halo-hist-del")
        drop.set_valign(Gtk.Align.CENTER)
        drop.set_can_focus(False)
        drop.set_tooltip_text("Forget this search  ·  Shift+Delete")
        drop.connect("clicked", lambda _b, e=entry: self._forget(e))
        line.append(drop)
        row.set_child(line)
        return row

    def _forget(self, entry: dict) -> None:
        """Drop one row. The entry rather than its text, because two rows can
        read the same and mean different things — a typed query and a picture
        whose file is called that."""
        HISTORY.remove_entry(entry)
        self._fill_history()
        self._track_geometry()

    def _forget_selected(self) -> None:
        row = self.history_list.get_selected_row()
        idx = row.get_index() if row else -1
        if 0 <= idx < len(self.history_rows):
            # Keep the place in the list rather than jumping back to the top, so
            # clearing out a run of entries is one key held down.
            self._forget(self.history_rows[idx])
            landed = self.history_list.get_row_at_index(
                min(idx, max(0, len(self.history_rows) - 1)))
            if landed:
                self.history_list.select_row(landed)

    def _reset_clear_confirm(self) -> None:
        if not self._hist_clear_armed:
            return
        self._hist_clear_armed = False
        self.history_clear_btn.set_label("Clear all")
        self.history_clear_btn.remove_css_class("halo-armed")

    def _on_clear_history(self, *_a) -> None:
        if not self._hist_clear_armed:
            # Two clicks, because there is no undo for this and the button sits a
            # few pixels above rows that get clicked all day.
            self._hist_clear_armed = True
            self.history_clear_btn.set_label("Really clear?")
            self.history_clear_btn.add_css_class("halo-armed")
            return
        self._reset_clear_confirm()
        HISTORY.clear()
        self._fill_history()
        self._track_geometry()

    def _on_history_activated(self, _list, row: Gtk.ListBoxRow) -> None:
        idx = row.get_index()
        if not (0 <= idx < len(self.history_rows)):
            return
        entry = self.history_rows[idx]
        if entry["kind"] == "image":
            self.close_history(animate=False)
            self._run_history_image(entry)
            return
        self._set_entry_text(entry["q"])
        self.close_history(animate=False)
        self.run_search()

    def _run_history_image(self, entry: dict) -> None:
        """Run a remembered visual search again from its row.

        The words go back into the field first, whichever route follows: both
        of them read what is being searched with from there or are handed it,
        and a row that searched without putting anything in the field would
        look like a row that had done nothing.

        The local file wins over the address when a row has both. It is the
        faster of the two by a long way — no fetch on Google's side — and it
        does not hand a third party an address to go and look at.
        """
        img = entry.get("img") or {}
        name = img.get("name") or ""
        # Read back, not inferred from the label: see SearchHistory._clean_img.
        words = img.get("words") or ""
        path = img.get("path") or ""
        uri = img.get("uri") or ""
        # Whatever is clipped to the field now is not this row's picture.
        self.detach_image()
        self._set_entry_text(words, notify=False)
        try:
            there = bool(path) and Path(path).is_file()
        except Exception:
            there = False
        if there:
            self.load_lens(path, text=words)
            return
        if uri:
            if not self._ensure_webview():
                _notify(APP_NAME, "Could not start the web view.")
                return
            self._search_image(uri, text=words)
            # After the navigation, as _drop_image() does it: _navigate() clears
            # the parked page, so expanding cannot pull it back over Lens.
            self.expand()
            return
        # The reference has gone stale: the file was moved, renamed or deleted
        # since. Said out loud rather than quietly searching the words instead,
        # which would be a different search wearing this row's clothes. The row
        # stays — a drive that is not mounted today may be tomorrow, and the ✕
        # beside it is how somebody says otherwise.
        self._reset_placeholder()
        _notify(APP_NAME,
                f"“{name or 'That picture'}” is not at {path} any more."
                if path else "That picture is no longer available.")

    def _scroll_history_to(self, row: Gtk.ListBoxRow | None) -> None:
        self._scroll_into_view(self.history_scroll, self.history_list, row)

    @staticmethod
    def _scroll_into_view(scroller: Gtk.ScrolledWindow, listbox: Gtk.ListBox,
                          row: Gtk.ListBoxRow | None) -> None:
        """Follow the keyboard selection, which GTK only does for focused lists.

        Focus stays in the entry — that is the whole point of driving the list
        from there — so the scrolled window has to be moved by hand. Both drawers
        need this now: the suggestion list can scroll too, and walking off the
        bottom of it with ↓ used to be possible only because it never scrolled.
        """
        if row is None:
            return
        adj = scroller.get_vadjustment()
        if adj is None:
            return
        try:
            ok, rect = row.compute_bounds(listbox)
        except Exception:
            return
        if not ok:
            return
        top = rect.origin.y
        bottom = top + rect.size.height
        page = adj.get_page_size()
        if top < adj.get_value():
            adj.set_value(top)
        elif bottom > adj.get_value() + page:
            adj.set_value(bottom - page)

    # ── searching ───────────────────────────────────────────────────────
    def run_search(self) -> None:
        if self.finding:
            # The field holds a needle, not a query. Reachable if something else
            # activates the entry; Enter itself is caught in _on_key.
            self._find_step(forward=True)
            return
        if self.history_open:
            # Only an explicitly moved selection wins over what is in the field.
            # Nothing is preselected, so typing a new query and pressing Enter
            # searches for that, exactly as it does with suggestions open.
            row = self.history_list.get_selected_row()
            idx = row.get_index() if row else -1
            if 0 <= idx < len(self.history_rows):
                picked = self.history_rows[idx]
                if picked["kind"] == "image":
                    self.close_history(animate=False)
                    self._run_history_image(picked)
                    return
                self._set_entry_text(picked["q"])
            self.close_history(animate=False)

        row = self.suggest_list.get_selected_row()
        if row and self.suggest_reveal.get_reveal_child() and self.suggest_items:
            idx = row.get_index()
            if 0 <= idx < len(self.suggest_items):
                self._set_entry_text(self.suggest_items[idx])

        text = self.entry.get_text().strip()
        # A picture clipped to the field IS the search, so an empty field is
        # not the "nothing to do" it is without one — and it is the ordinary
        # case: drop a picture on the pill and press Enter. Above the empty
        # check for exactly that reason, and above the marker test below
        # because _search_attached() makes the finer distinction that one
        # cannot: whether the results on screen are *this* picture's.
        if self.attached:
            if self._suggest_timer:
                GLib.source_remove(self._suggest_timer)
                self._suggest_timer = 0
            self._show_suggestions([], animate=False)
            self.close_history(animate=False)
            if not looks_like_url(text):
                self._search_attached(text)
                return
            # An address is not a refinement. Typing one into the field means
            # "go there" whatever else is clipped to it — the same reasoning
            # _search_text() applies to words that named themselves — so the
            # picture comes off and the panel browses. Without this, pasting a
            # link with a picture attached ran a visual search for the link as
            # words, and the only way out was to notice the chip first.
            self.detach_image()
        if not text:
            return
        # A fetch still in flight is for the query being searched right now, so
        # its answer could only arrive as a list of suggestions for something
        # already on screen. Drop it with the list.
        if self._suggest_timer:
            GLib.source_remove(self._suggest_timer)
            self._suggest_timer = 0
        self._show_suggestions([], animate=False)

        # There WAS a block here that refined the visual search whenever the
        # address on screen looked like a Lens result, and it is worth saying
        # where it went, because deleting it is a fix rather than a tidy-up.
        #
        # It asked the page, not the pill. So it went on refining after the
        # picture had been taken off: remove the chip, type a word, press
        # Enter, and the field answered "Lens: <word>" and rewrote the Lens
        # address instead of running an ordinary search — reported exactly that
        # way, ending on a Google 400. The chip is the whole of the question
        # now. A picture attached means a visual search, and _search_attached()
        # above has already returned; no chip means an ordinary one, and there
        # is nothing left here to decide.

        # On a first ever run, wait for the offscreen handshake instead of
        # racing it. Two simultaneous requests from a session with no cookies at
        # all is exactly what makes Google serve a CAPTCHA, and the handshake is
        # also what earns dark mode. Costs a couple of seconds, once.
        if self.warm_pending and not looks_like_url(text):
            self.deferred_query = text
            self._set_busy(True)
            return

        if not self._ensure_webview():
            _notify(APP_NAME, "Could not start the web view.")
            return
        direct = looks_like_url(text)
        target = direct or search_url(text)
        self.last_query = text
        # Recorded here rather than on Enter, so a query held back by the
        # first-run handshake is remembered once — when it actually runs — and a
        # Lens refinement, which returns above, is not remembered as a search.
        HISTORY.add(text, "url" if direct else "search")
        self._set_busy(True)
        self._navigate(target)
        # Cover the panel whether it is opening or already open. A fresh WebView
        # is white for a frame or two, which flashes badly against a dark popup —
        # and a used one is worse: it goes on painting the *previous* search until
        # the new page commits, so searching again from an open panel showed the
        # old results sitting there as though Enter had done nothing.
        self._show_loading("Searching…")
        self.expand()

    # A cover is only ever allowed to bridge the gap until there is something
    # worth looking at. It is opaque and it swallows clicks, so one that outlives
    # its load makes live results look frozen — every path that raises it gets a
    # deadline, and a plain search lifts it as soon as the page is mostly parsed.
    COVER_CAP_MS = 2500          # plain search: expand animation plus a margin
    COVER_CAP_LENS_MS = 22000    # Lens legitimately takes seconds, plus 2 retries
    COVER_CAP_LENS_LAND_MS = 4000  # results are in flight: deadline, not a wait
    UNCOVER_PROGRESS = 0.55

    # How long past its deadline a cover may be held while the page it is waiting
    # for still has not appeared. Coming off on time is the whole point of the
    # deadline — but uncovering onto the *previous* page is not showing the user
    # anything, so a slow navigation buys a little more, and never more than this.
    COVER_GRACE_MS = 3000
    COVER_RECHECK_MS = 100

    def _arm_cover(self, delay_ms: int) -> None:
        """One live cover timer, and a ceiling it can never outlive."""
        if self._cover_timer:
            GLib.source_remove(self._cover_timer)
        self._cover_deadline = (GLib.get_monotonic_time()
                                + (delay_ms + self.COVER_GRACE_MS) * 1000)
        self._cover_timer = GLib.timeout_add(delay_ms, self._cover_expired)

    def _show_loading(self, message: str, cap_ms: int | None = None) -> None:
        self.loading_label.set_text(message)
        self.loading.set_visible(True)
        self.loading_spinner.start()
        self._arm_cover(cap_ms or self.COVER_CAP_MS)

    def _cover_expired(self) -> bool:
        """Deadline reached. Whatever the page is doing, stop hiding it.

        Unless the page being waited for is not on screen yet: the view is still
        painting the one before it, so lifting now would not show the new search,
        it would show the last one. Check back in a moment instead — bounded by
        the ceiling _arm_cover set, so a navigation that never commits at all
        cannot leave the cover up for good.
        """
        self._cover_timer = 0       # cleared first: this source is ending anyway
        if self._view_stale() and GLib.get_monotonic_time() < self._cover_deadline:
            self._cover_timer = GLib.timeout_add(self.COVER_RECHECK_MS,
                                                 self._cover_expired)
            return False
        if self._nav_pending:
            # Out of grace with nothing committed. Some responses end a load
            # without ever replacing the page — a 204, a hand-off to another
            # program — and neither COMMITTED nor load-failed is coming to say so.
            self._nav_abandoned()
        self._hide_loading()
        return False

    def _uncover_after(self, delay_ms: int) -> None:
        """Lift the cover in this many ms — and not a moment later."""
        self._arm_cover(delay_ms)

    def _navigate(self, uri: str, *, lens: bool = False) -> None:
        """Load a URI, and remember the view is still showing the old page.

        `lens` marks the two navigations that belong to an image hand-off —
        load_lens() starting one and _lens_retry() taking another run at it.
        Every other navigation abandons a hand-off in flight, and that is a fix
        rather than bookkeeping: nothing used to cancel one, so typing a search
        while an image was still uploading left pending_lens set, and
        _lens_retry then yanked the panel off the user's results and back onto
        Google's uploader a few seconds after they pressed Enter. The spinner
        never stopped either, because every gate that waits on pending_lens
        went on waiting.

        Everything that lifts the loading cover has to know this. WebKit goes on
        painting the outgoing document until the new one commits, and two of the
        outgoing page's own events routinely arrive *after* the cover goes up: its
        load-finished, and its parting progress reading of 1.0. Uncovering on
        either put the previous search back on screen for a few frames — which is
        the flash this flag exists to stop, and why it is cleared on COMMITTED
        (the moment the new document is what the view is painting) rather than
        when we ask for the load. A load that fails clears it too, since nothing
        will ever commit for it.
        """
        if self.web is None:
            return
        if not lens and self.pending_lens:
            self._abandon_lens()
        # Whatever was parked, this is where the panel is going instead — and
        # a stashed picture belonged to that parked page, so it goes too.
        self._parked_uri = self._parked_state = None
        self._drop_stash()
        self._nav_pending = True
        self._nav_uri = uri
        # Asking for the page already on screen is a reload, and load_uri()
        # records it as a fresh navigation — WebKit pushes a back/forward entry
        # for it, so Back then lands on the page it was already showing, once
        # per repeat. reload() fetches the same page and leaves the list alone.
        #
        # Two live callers walk into this: _lens_retry(), which re-navigates to
        # lens.google.com to get a form that has not been spent (a reload
        # written as a navigation, up to twice per image search), and run_search
        # with the same query twice over. Fragments come off both sides —
        # page#a to page#b is a real navigation and has to stay one.
        if self._is_current_uri(uri):
            self.web.reload_bypass_cache()
            return
        self.web.load_uri(uri)

    # Once a load has FINISHED, this is all the time a frame report gets before
    # the cover comes off regardless. A page that was going to paint paints within
    # a frame or two of finishing, and a page that will never report — WebKit's
    # own error pages, anything that does not run scripts — must not be left under
    # a cover for the whole ceiling on account of it.
    PAINT_GRACE_MS = 250

    def _view_stale(self) -> bool:
        """True while the view may still be showing the page before this load.

        Two distinct waits, and the cover has to outlast both. Until COMMITTED the
        new document is not even the view's yet. After COMMITTED it is, but WebKit
        goes on presenting the outgoing page's last frame until the incoming one
        produces one of its own — and Google's results pass the uncover threshold
        well inside that gap, which is exactly how a fresh search came off the
        cover showing the *previous* search for a few frames.
        """
        return self._nav_pending or self._await_paint

    def _arm_paint_wait(self) -> None:
        """Committed. From here the cover waits for the page to actually paint.

        Deliberately without a deadline of its own while the load is still in
        flight. A page held back by a slow stylesheet is committed, still loading,
        and still showing the page before it — measured against a stylesheet that
        took a second to arrive, a 900ms limit expired 140ms too early and put the
        previous search back on screen, which is the exact bug this exists to
        stop. The cover's own ceiling in _cover_expired is the hard bound, and
        _paint_grace() is the soft one for a page that has finished loading.
        """
        self._cancel_paint_wait()
        self._await_paint = True

    def _cancel_paint_wait(self) -> None:
        self._await_paint = False
        if self._paint_timer:
            GLib.source_remove(self._paint_timer)
            self._paint_timer = 0

    def _paint_grace(self) -> None:
        """Loaded, but nothing has reported a frame. Allow a little, then stop."""
        if not self._await_paint or self._paint_timer:
            return
        self._paint_timer = GLib.timeout_add(self.PAINT_GRACE_MS,
                                             self._paint_gave_up)

    def _paint_gave_up(self) -> bool:
        self._paint_timer = 0
        self._await_paint = False
        self._recheck_cover()
        return False

    def _on_page_message(self, _ucm, value) -> None:
        """PAINT_JS reporting that a new document has reached the screen."""
        try:
            # WebKit 6 hands over a JSCValue directly; older bindings wrap it.
            if hasattr(value, "get_js_value"):
                value = value.get_js_value()
            text = value.to_string()
        except Exception:
            return
        if text.startswith("sel:"):
            self._page_selection = text[4:]
            return
        if text in ("top", "away"):
            self._page_at_top = (text == "top")
            return
        if text.startswith("spell "):
            self._adopt_spelling(text[6:])
            return
        if text != "painted":
            return
        self._cancel_paint_wait()
        self._recheck_cover()

    def _recheck_cover(self) -> None:
        """The page just became visible — lift the cover if nothing else holds it.

        Routed back through _on_load_progress rather than uncovering here, so the
        "is it readable yet" threshold and the Lens hand-off rules stay in one
        place. A page that has painted but is only a tenth loaded still waits.
        """
        if self.web is None or not self.loading.get_visible():
            return
        if self.pending_lens or self._view_stale():
            return
        self._on_load_progress(self.web, None)

    def _hide_loading(self) -> None:
        self._lens_await_load = False
        self._cancel_paint_wait()
        if self._cover_timer:
            GLib.source_remove(self._cover_timer)
            self._cover_timer = 0
        self.loading_spinner.stop()
        self.loading.set_visible(False)

    # ── find in page ────────────────────────────────────────────────────
    #
    # Deliberately no widget and no button. The pill already has the one text
    # field on screen, and a floating find bar over a 760px panel would cover the
    # very results it is searching — so Ctrl+F turns the field into the find box,
    # says so in the placeholder, and reports the count where the address already
    # lives. Leaving find mode puts the query back exactly as it was.
    # The cap FIND_JS stops collecting at, so the two agree about "1000+".
    FIND_MAX = 1000

    def _ask_find(self, call: str, report: bool = True) -> None:
        """Run one of __haloFind's methods, and say what it found.

        `report` is False for clear(), which answers with a count of zero
        because there is nothing left — and a zero written to the label reads as
        "no matches" for a query nobody has typed yet. It also arrives *after*
        the label has been set to "Find in page…", so it overwrote it.
        """
        if self.web is None:
            return

        def got(web, result, _data) -> None:
            try:
                value = web.evaluate_javascript_finish(result)
                said = json.loads(value.to_string()) if value else {}
            except Exception:
                return
            # The field is re-read here rather than captured: these answers come
            # back out of order often enough, and a count belongs to whatever is
            # in the field now or to nothing at all.
            if not report or not self.finding or not self.entry.get_text():
                return
            self._show_match_count(int(said.get("count", 0)))

        try:
            self.web.evaluate_javascript(
                "(window.__haloFind ? window.__haloFind.%s : '{}')" % call,
                -1, None, None, None, got, None)
        except Exception:
            pass

    def toggle_find(self) -> None:
        """Ctrl+F. Only meaningful with a page open to search."""
        if not self.expanded or self.web is None:
            return
        if self.finding:
            # Already in find mode: put the caret back and select what is there,
            # which is what a second Ctrl+F is reaching for.
            self.focus_entry(select_all=True)
            return
        self.finding = True
        self._find_query = self.entry.get_text()
        self._find_label = self.url_label.get_text()
        self._show_suggestions([], animate=False)
        self.close_history(animate=False)
        self.entry.set_text("")
        self.entry.set_placeholder_text("Find in page…")
        self.entry.add_css_class("halo-finding")
        self.url_label.set_text("Find in page  ·  Enter and Shift+Enter step "
                                "through, Esc leaves")
        self.focus_entry()

    def leave_find(self) -> None:
        """Back to being a search field, with the query as it was."""
        if not self.finding:
            return
        self.finding = False
        # Ends the find AND clears the selection it drew; see _clear_find_marks.
        self._clear_find_marks()
        self.entry.remove_css_class("halo-finding")
        self._reset_placeholder()
        # notify=False: putting the query back is not typing it. Letting it look
        # like typing fetched suggestions for a query already on screen, and the
        # list that opened then owned ↓ — so leaving find mode and pressing ↓ to
        # reopen the panel moved a suggestion instead, and Ctrl+F did nothing
        # because the panel was still shut. Measured with suggestions on.
        self._set_entry_text(self._find_query, notify=False)
        self.url_label.set_text(self._find_label)
        self.focus_entry(select_all=True)

    def _clear_find_marks(self) -> None:
        """Drop what a previous find left drawn on the page.

        Two halves, and both are needed: the highlight registered in
        CSS.highlights, and the document's own selection, which is what marks
        the current match and is not dropped by clearing the highlight.
        """
        if self.finding or self.web is None:
            return
        self._ask_find("clear()", report=False)
        # Belt and braces: clear() drops the selection it made, but a page can
        # have a selection of its own that a find scrolled past, and this is the
        # one path that is supposed to leave nothing behind.
        try:
            self.web.evaluate_javascript(CLEAR_SELECTION_JS, -1, None, None,
                                         None, None, None)
        except Exception:
            pass

    def _find_update(self) -> None:
        """The field changed while finding: search from the top and count."""
        if self.web is None:
            return
        text = self.entry.get_text()
        if not text:
            self._ask_find("clear()", report=False)
            self.url_label.set_text("Find in page…")
            return
        self._ask_find("run(%s)" % json.dumps(text))

    def _find_step(self, forward: bool) -> None:
        if self.web is None or not self.entry.get_text():
            return
        self._ask_find("step(%s)" % ("true" if forward else "false"))

    def _show_match_count(self, count: int) -> None:
        # The other thing written into the address label, and the other way a
        # break reaches it: a needle pasted with a newline in it.
        needle = one_line(self.entry.get_text())
        if count <= 0:
            self.url_label.set_text(f"“{needle}”  ·  no matches")
        elif count >= self.FIND_MAX:
            self.url_label.set_text(f"“{needle}”  ·  {self.FIND_MAX}+ matches")
        else:
            self.url_label.set_text(f"“{needle}”  ·  {count} "
                                    + ("match" if count == 1 else "matches"))

    def _reload(self) -> None:
        if self.web is not None:
            self.web.reload()

    RELOAD_FACE = ("view-refresh-symbolic", "Reload  ·  Ctrl+R")
    STOP_FACE = ("process-stop-symbolic", "Stop loading")

    def _reload_or_stop(self) -> None:
        """One button, two jobs — the arrangement every browser already has.

        Asked while a page is arriving, it cancels; asked otherwise, it fetches
        again. The button's face says which of the two it is about to do, so the
        question of what it means is never open.
        """
        if self.web is None:
            return
        if self._page_loading():
            self.web.stop_loading()
            # stop_loading() ends the load without WebKit reporting a state
            # change for it in every case, so the face is put back here rather
            # than left to notify::is-loading. The notify, when it does come,
            # asks for the face it already has and _set_stop_face returns.
            self._set_stop_face(False)
        else:
            self._reload()

    def _page_loading(self) -> bool:
        if self.web is None:
            return False
        try:
            return bool(self.web.get_property("is-loading"))
        except Exception:
            return False

    def _set_stop_face(self, stopping: bool) -> None:
        """Show the stop face or the reload face, skipping the no-ops.

        Driven from is-loading rather than from _set_busy, which also stays on
        through a Lens hand-off and through the paint wait after a load has
        finished — moments when there is nothing left to cancel and an X would
        be an offer the button cannot keep.
        """
        btn = getattr(self, "btn_reload", None)
        if btn is None:
            return
        icon, tip = self.STOP_FACE if stopping else self.RELOAD_FACE
        if btn.get_icon_name() == icon:
            return
        btn.set_icon_name(icon)
        btn.set_tooltip_text(tip)

    def _on_loading_changed(self, web, _p) -> None:
        self._set_stop_face(bool(web.get_property("is-loading")))

    # ── the step back out of a silent spelling correction ──────────────
    #
    # Google's third correction style ("Ergebnisse für X") is the one the
    # address does not record, and it is also the one Back could not follow.
    # Measured against live google.com, 2026-08: the results already *are* for
    # the corrected words while q= still holds the misspelling, and clicking the
    # bar does not navigate or even reload — history.length does not move, no
    # load event fires, the bar simply disappears. So WebKit's back list has
    # nothing in it to go back to, and on a first search can_go_back() is false
    # and the button is dead.
    #
    # Halo can offer the step anyway, because it knows both queries: the one the
    # user typed, which is what q= still holds, and the one Google substituted,
    # which SPELL_JS reads off the page. Google serves the typed words verbatim
    # when the address carries &nfpr=1 — that is Google's own "Stattdessen
    # suchen nach" link — so the page to go back to exists and can be asked for.
    #
    # The record is one slot rather than a stack, and it holds no addresses:
    # both halves are recognised from the live address, which carries the same
    # q= for each and differs only in nfpr. Google rewrites its own URL after
    # the fact (an &sei= turns up on the first search of a session) and a page
    # is still the same search when it does, so matching on the query survives
    # what matching on the string would not. The slot is re-affirmed by SPELL_JS
    # on every arrival at the corrected half — including a page WebKit restores
    # from its cache — and dropped the moment a load *commits* to anything else.
    # Commits, not notify::uri: a provisional load that is abandoned must not be
    # able to take the step away from a page the user never left.
    #
    # Inside the slot Back and Forward are exact inverses, which is the property
    # the pair has to have — "back and forward again" is where this was reported
    # broken. Back on the corrected half shows the verbatim page; Forward on the
    # verbatim half returns to the corrected one; Back on the verbatim half
    # steps *over* the corrected page to whatever came before it, since the
    # verbatim page stands in for that page rather than following it. For the
    # same reason there is nothing symmetric to offer on the corrected half
    # going forward: while the verbatim page is the only thing in WebKit's
    # forward list, Forward is not a step past this page and stays greyed.
    #
    # And the pair only claims the buttons while the corrected page is still the
    # newest thing in the list. Once the user has followed a result off it and
    # come back, there is a real page ahead of them: loading the verbatim page
    # then would prune that entry away, and hijacking Forward would put it out
    # of reach. Both directions go back to being WebKit's own — which is also
    # the honest reading of the gesture, since a user who has been somewhere and
    # returned is navigating, not still reacting to the correction.
    def _correction_half(self, uri: str) -> str | None:
        """Which half of the remembered corrected pair this address is, if any."""
        slot = self._spell_slot
        if slot is None:
            return None
        query = google_query(uri)
        if query is None or query != slot["typed"]:
            return None
        return "verbatim" if _no_correction(uri) else "corrected"

    def _track_correction(self, uri: str) -> None:
        """Follow the pair as the view moves, and forget it on the way out."""
        if self._spell_slot is None:
            return
        half = self._correction_half(uri)
        if half is None:
            self._spell_slot = None
        else:
            self._spell_slot["state"] = half

    def _nth_half(self, offset: int) -> str | None:
        """Which half, if either, sits `offset` steps along WebKit's own list."""
        if self.web is None:
            return None
        try:
            item = self.web.get_back_forward_list().get_nth_item(offset)
        except Exception:
            return None
        return self._correction_half(item.get_uri() or "") if item else None

    def _nth_exists(self, offset: int) -> bool:
        if self.web is None:
            return False
        try:
            return self.web.get_back_forward_list().get_nth_item(offset) is not None
        except Exception:
            return False

    def _at_correction(self) -> bool:
        """On the corrected page, with the undo still the thing ahead of it.

        Nothing past this page in WebKit's list, or the only thing past it is
        the verbatim page it pairs with. Anywhere else the user has been forward
        of here and come back, and the pair must not take the buttons: loading
        the verbatim page would prune the real entry ahead away.
        """
        slot = self._spell_slot
        if slot is None or slot["state"] != "corrected":
            return False
        return not self._nth_exists(1) or self._nth_half(1) == "verbatim"

    def _at_verbatim(self) -> bool:
        """On the verbatim page, with the corrected page it stands in for just
        behind it and nothing ahead — the state Back put the panel in."""
        slot = self._spell_slot
        if slot is None or slot["state"] != "verbatim":
            return False
        return self._nth_half(-1) == "corrected" and not self._nth_exists(1)

    def _can_history(self, direction: int) -> bool:
        """Is there a step this way?

        The single answer for the toolbar buttons, the Alt+arrow keys, the mouse
        thumb buttons and the right-click menu. It is more than WebKit's own
        can_go_back(): the correction pair adds a step WebKit's list does not
        have, and hides one it does.
        """
        if self.web is None:
            return False
        if self._at_correction():
            # Back is the undo, and always available. Forward is not a step at
            # all: the verbatim page substitutes for this one rather than
            # following it, and there is nothing else past here.
            return direction < 0
        if self._at_verbatim():
            return True if direction > 0 else self._nth_exists(-2)
        return self.web.can_go_back() if direction < 0 else self.web.can_go_forward()

    def _history(self, direction: int) -> None:
        """Back or forward, when there is a step to take."""
        if not self.expanded or self.web is None:
            return
        if self._at_correction():
            if direction < 0:
                if self._nth_half(1) == "verbatim":
                    # Loaded once already and stepped away from; going forward to
                    # it is the same page without a second entry for it.
                    self.web.go_forward()
                else:
                    self._navigate(search_url(self._spell_slot["typed"],
                                              verbatim=True))
            return                      # and nothing follows a correction
        if self._at_verbatim():
            if direction > 0:
                self.web.go_back()      # the corrected page: back in the list,
                return                  # forward in the pair
            try:
                item = self.web.get_back_forward_list().get_nth_item(-2)
            except Exception:
                item = None
            if item is not None:
                self.web.go_to_back_forward_list_item(item)
            return
        if direction < 0 and self.web.can_go_back():
            self.web.go_back()
        elif direction > 0 and self.web.can_go_forward():
            self.web.go_forward()

    def _update_nav_buttons(self) -> None:
        """One place the two chips are set from, so they cannot disagree with
        what _history() will actually do."""
        self.btn_back.set_sensitive(self._can_history(-1))
        self.btn_fwd.set_sensitive(self._can_history(1))

    def _set_busy(self, busy: bool) -> None:
        """The small spinner in the bar, which tracks work rather than painting."""
        if busy:
            self.spinner.set_visible(True)
            self.spinner.start()
        else:
            self.spinner.stop()
            self.spinner.set_visible(False)

    def resume_after_warm(self) -> None:
        """Handshake finished (or gave up): run whatever the user asked for."""
        if not self.warm_pending:
            return
        self.warm_pending = False
        query, self.deferred_query = self.deferred_query, None
        if query:
            self.entry.set_text(query)
            self.run_search()
        else:
            self._set_busy(False)

    # ── the picture clipped to the search field ─────────────────────────
    #
    # Every way of searching a picture used to leave the pill looking exactly
    # as it does for a text search: the field was cleared, a placeholder said
    # "Searching this image with Lens…" for as long as the load took, and then
    # the pill was blank again above a page of visual matches. Nothing on
    # screen said which picture those matches were of, nothing said an image
    # was still in play, and typing the next query threw it away.
    #
    # An attachment is the fix for all three at once, and it is what Google
    # itself shows: a thumbnail beside the words, removable, and the thing the
    # next Enter is about. It is state, not decoration — see run_search().

    THUMB_PX = 26           # the chip's side in logical pixels, next to a 30px
                            # chip and a 34px field: square, and a shade smaller
    ATTACH_NAME = "attached"    # one fixed basename in DATA_DIR

    def _build_attach_chip(self) -> Gtk.Button:
        """The thumbnail button, built once and shown when there is a picture.

        Three layers in an overlay, because the hover state has to darken the
        picture *and* draw an ✕ on it, and neither can be done to a GtkImage's
        own texture without rebuilding the texture on every pointer crossing.
        The scrim and the ✕ are ordinary widgets that CSS fades in, so hover
        costs a repaint and nothing else — no pixbuf work, no reallocation.

        set_overflow(HIDDEN) is what rounds the photograph's corners: the
        radius is on the button in CSS and GTK clips the children to it. The
        alternative — baking the rounding into the texture — would have left
        transparent corners for the scrim to darken into grey notches.
        """
        btn = Gtk.Button()
        btn.add_css_class("halo-thumb")
        btn.set_valign(Gtk.Align.CENTER)
        # Like every other chip on this bar: a focusable widget here would be
        # one more Tab stop between the field and the page, and _on_key's Tab
        # rule exists because that dead-ends typing. See the comment there.
        btn.set_can_focus(False)
        btn.set_overflow(Gtk.Overflow.HIDDEN)

        stack = Gtk.Overlay()
        stack.set_overflow(Gtk.Overflow.HIDDEN)
        self.attach_thumb = Gtk.Image()
        # Not Gtk.Picture: set_size_request() only raises a Picture's minimum,
        # so it renders at the texture's natural size and pushes the pill past
        # BAR_HEIGHT. _brand_icon() carries the measurement.
        self.attach_thumb.set_pixel_size(self.THUMB_PX)
        stack.set_child(self.attach_thumb)

        scrim = Gtk.Box()
        scrim.add_css_class("halo-thumb-scrim")
        scrim.set_can_target(False)     # the button underneath takes the click
        stack.add_overlay(scrim)

        cross = Gtk.Image.new_from_icon_name("window-close-symbolic")
        cross.add_css_class("halo-thumb-x")
        cross.set_pixel_size(14)
        cross.set_halign(Gtk.Align.CENTER)
        cross.set_valign(Gtk.Align.CENTER)
        cross.set_can_target(False)
        stack.add_overlay(cross)

        btn.set_child(stack)
        # Through _guard_click like every other button on the bar, or the
        # release that ends a drag of the pill would also throw the picture
        # away — which is the one click here that cannot be taken back.
        btn.connect("clicked", self._guard_click(lambda *_: self.detach_image()))
        btn.set_visible(False)
        return btn

    def _thumb_texture(self, path: str,
                       size_px: int | None = None) -> Gdk.Texture | None:
        """A small square of the picture, or None if it cannot be read.

        Scaled to *cover* the square and then centre-cropped, rather than
        letterboxed: 26 pixels have none to spend on bars, and a centre crop is
        what an attachment thumbnail shows everywhere else.

        At the display's scale, so the chip is not soft on a HiDPI screen. The
        widget is asked for THUMB_PX logical pixels and handed a texture of
        THUMB_PX x scale real ones, which is the one direction GtkImage's
        pixel-size does not fight.
        """
        px = max(1, (size_px or self.THUMB_PX) * self._scale())
        try:
            # Not new_from_file_at_scale(): its "preserve aspect ratio" fits
            # the picture *inside* the box, so a wide photo came back 26x10 and
            # the chip was a letterboxed slot rather than a square.
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        except Exception:
            return None
        if pixbuf is None:
            return None
        width, height = pixbuf.get_width(), pixbuf.get_height()
        if width <= 0 or height <= 0:
            return None
        ratio = max(px / width, px / height)
        wide, tall = max(1, round(width * ratio)), max(1, round(height * ratio))
        try:
            scaled = pixbuf.scale_simple(wide, tall,
                                         GdkPixbuf.InterpType.BILINEAR)
            if scaled is None:
                return None
            side = min(px, wide, tall)
            square = GdkPixbuf.Pixbuf.new_subpixbuf(
                scaled, (wide - side) // 2, (tall - side) // 2, side, side)
            return Gdk.Texture.new_for_pixbuf(square or scaled)
        except Exception:
            return None

    def attach_image(self, path: str, *, name: str | None = None,
                     uri: str | None = None) -> bool:
        """Clip a picture to the search field. Always True, and says why below.

        Copied into DATA_DIR rather than referred to where it lies. Most of
        what gets attached is already temporary — pixels written out of a drop,
        a screen capture, a data: URI out of the right-click menu, each of them
        on a fixed name the next one overwrites — and a dropped file may be in
        /tmp, or on a stick that is pulled out. One copy under a name of Halo's
        own is what lets the chip survive a restart, which is the whole of what
        remembering it means.

        One attachment at a time, and one file: the copies under any other
        extension are removed on the way in, so this cannot become a directory
        that only ever grows. DATA_DIR is 0700 and the copy 0600 — an
        attachment is as private as the cookie jar beside it.

        The bool is kept for the callers that read it, but nothing fails any
        more: neither an unreadable format nor a copy that could not be written
        stops the picture being attached, because a search that runs with no
        chip is the worse outcome of the two. See the comment on the thumbnail.
        """
        source = Path(path)
        ext = source.suffix.lower()
        if ext not in IMAGE_EXTS:
            ext = ".png"
        kept = DATA_DIR / (self.ATTACH_NAME + ext)
        try:
            _own_dir(DATA_DIR)
            if os.path.abspath(path) != str(kept):
                for old in DATA_DIR.glob(self.ATTACH_NAME + ".*"):
                    if old != kept:
                        old.unlink()
                shutil.copyfile(path, kept)
                _own_file(kept)
        except Exception:
            # The copy is what makes it durable, not what makes it work. A
            # failure here costs the attachment its next restart and nothing
            # else, so carry on with the file where it lies.
            kept = source
        # A thumbnail that cannot be built does NOT fail the attachment.
        #
        # Not a common case, and an earlier version of this comment was wrong
        # about which formats it covers: measured on Fedora 44, GdkPixbuf has
        # loaders for avif, heic/heif, jxl, webp, svg, tiff and raw as well as
        # the obvious ones, and a real .avif and a real .heic both thumbnail
        # correctly. What is left is a file that is corrupt or truncated (a
        # download that died halfway), one whose extension lies about its
        # contents, or a format whose loader is not installed on this
        # particular machine — gdk-pixbuf loaders are separate packages.
        #
        # Refusing left the worst possible state: the picture was uploaded and
        # searched, but the pill did not know it had one — no chip, no way to
        # take it off, and the next Enter quietly ran a plain web search
        # instead of refining. The chip falls back to a generic picture icon,
        # so "an image search always has an attachment" holds without
        # exception.
        texture = self._thumb_texture(str(kept))
        self._attach_serial += 1
        self.attached = {"path": str(kept),
                         "name": name or source.name or "image",
                         "uri": uri or None,
                         "origin": self._durable_origin(path)}
        # Whatever is on screen was found for some other picture, or for none.
        self._forget_lens_page()
        self._show_attachment(texture)
        CFG["attached_image"] = dict(self.attached)
        return True

    def _durable_origin(self, path: str) -> str | None:
        """The file this picture can be found at again, or None if there is none.

        History stores a reference and never a copy, so it needs somewhere that
        will still hold this picture tomorrow. A file of the user's own is
        exactly that. Everything Halo writes into DATA_DIR is not: attached.*,
        dropped.png, incoming-attach.* and the data: URI scratch file are each
        on a fixed name that the next attachment overwrites, so a row pointing
        at one would quietly come to mean a different picture.

        Re-attaching the picture already on the field keeps the origin it came
        in with. That case is not rare — it is what a refinement does when it
        has to fall back to a fresh upload, and it hands attach_image() the
        DATA_DIR copy rather than the file the user dropped.
        """
        try:
            full = os.path.abspath(path)
        except Exception:
            return None
        try:
            inside = Path(full).is_relative_to(DATA_DIR)
        except Exception:
            inside = full.startswith(str(DATA_DIR) + os.sep)
        if not inside:
            return full
        current = self.attached or {}
        if current.get("path") == full:
            return current.get("origin") or None
        return None

    def _show_attachment(self, texture: Gdk.Texture | None) -> None:
        if self.attach_button is None or self.attach_thumb is None:
            return
        if texture is not None:
            self.attach_thumb.set_from_paintable(texture)
        else:
            # No loader for this format. The chip still has to be there, and
            # has to be removable — see attach_image().
            self.attach_thumb.set_from_icon_name("image-x-generic-symbolic")
        self.attach_thumb.set_pixel_size(self.THUMB_PX)
        # Both ways out of it, in the order a hand finds them.
        self.attach_button.set_tooltip_text(
            f"{self.attached['name']}\nClick to remove  ·  Backspace")
        self.attach_button.set_visible(True)
        self._reset_placeholder()

    def detach_image(self, *, forget: bool = True) -> None:
        """Take the picture off the field again.

        `forget` is False only where the attachment is being replaced rather
        than dropped, so the config is written once instead of twice.

        Returns early when there is nothing on the field, which is not merely
        tidiness: _search_text() calls this for every phrase dropped,
        middle-clicked or pasted, and CFG.__setitem__ writes config.json on
        each assignment. Unguarded, an afternoon of middle-click searches would
        rewrite the settings file a few hundred times to store the same None.
        """
        if self.attached is None:
            return
        self.attached = None
        self._awaiting_lens_url_until = 0
        self._forget_lens_page()
        if self.attach_button is not None:
            self.attach_button.set_visible(False)
        if self.attach_thumb is not None:
            self.attach_thumb.clear()
        if forget:
            CFG["attached_image"] = None
        self._reset_placeholder()

    def _stash_attachment(self) -> None:
        """Take the picture off the field, keeping it for ↓ to put back.

        Closing Halo is not the gesture that throws an attachment away — the ✕
        on the chip is, and Backspace is. But it is not the gesture that keeps
        one on the field either. A picture is part of a session, exactly like
        the parked page and the words that were in the field, and the three of
        them come back together or not at all.

        This replaced the opposite rule, and the report is what a chip that
        outlives its session actually looks like: attach a picture, close the
        pill, open it again to look something else up, and yesterday's
        photograph is still clipped to an empty field — so the next Enter runs a
        visual search for a query that has nothing to do with it.

        The whole of the attachment goes into the stash, not just its path.
        _lens_shown_for and _lens_result_uri are what let a word typed against
        results already on screen refine them instead of uploading again, and
        coming back through attach_image() would clear both — so a picture
        restored together with its own matches would have spent another ten
        seconds re-uploading itself the first time it was refined.
        """
        keep = self.attached
        self._closed_with_image = None if keep is None else {
            "attached": dict(keep),
            "serial": self._attach_serial,
            "shown_for": self._lens_shown_for,
            "result_uri": self._lens_result_uri,
        }
        if keep is not None:
            self.attached = None
            if self.attach_button is not None:
                self.attach_button.set_visible(False)
            if self.attach_thumb is not None:
                self.attach_thumb.clear()
            self._reset_placeholder()
        # The copy under DATA_DIR stays where it is, and so does the config key.
        # Between them they are what lets a stash outlive the process at all.
        # Written only when it changes, because CFG writes config.json on every
        # assignment and closing the pill is not a rare event.
        want = dict(keep) if keep is not None else None
        if CFG["attached_image"] != want:
            CFG["attached_image"] = want

    def _unstash_attachment(self) -> bool:
        """Put the stashed picture back on the field. True if there was one.

        Called from the one rung of the ladder that brings a session back — ↓
        on a bare pill, which a double tap of the shortcut goes through as well.
        The picture arrives with the page it was searching, which is the only
        arrangement that is not a lie: a chip sitting beside results it does not
        belong to would make the next Enter refine the wrong thing.

        Restored verbatim rather than through attach_image(), so the link
        between this picture and the results on screen survives the round trip.
        """
        stash, self._closed_with_image = self._closed_with_image, None
        if not stash:
            return False
        saved = stash.get("attached") or {}
        path = saved.get("path") or ""
        try:
            there = bool(path) and Path(path).is_file()
        except Exception:
            there = False
        if not there:
            # Cleared out of DATA_DIR from the ⋯ menu, or gone with the /tmp it
            # was copied from. Nothing to show, and nothing left to search.
            if CFG["attached_image"] is not None:
                CFG["attached_image"] = None
            return False
        self.attached = dict(saved)
        self._attach_serial = stash.get("serial", self._attach_serial)
        self._lens_shown_for = stash.get("shown_for")
        self._lens_result_uri = stash.get("result_uri")
        self._show_attachment(self._thumb_texture(path))
        return True

    def _drop_stash(self) -> None:
        """The stashed session is over: the panel is going somewhere new.

        The same moment the parked page is let go of, and for the same reason.
        Without this, searching something else after a reopen and then stepping
        ↑ out of it and ↓ back in would have pulled a picture from the session
        before onto results that were nothing to do with it.
        """
        if self._closed_with_image is None:
            return
        self._closed_with_image = None
        # Only when nothing has taken its place: load_lens() attaches the new
        # picture and writes the key before it navigates.
        if self.attached is None and CFG["attached_image"] is not None:
            CFG["attached_image"] = None

    def _seed_stash(self) -> None:
        """Load the picture the last run was carrying — into the stash, not onto
        the field.

        Called once, after the bar exists, and what it produces is exactly what
        closing the pill would have left behind: the first ↓ of a new process
        reaches the same place the first ↓ of the old one would have.

        The file is checked here rather than in Config._sanitise() because a
        config read happens before there is anything to show it on — and
        because a missing file is a perfectly ordinary thing to find after
        somebody has cleared DATA_DIR from the ⋯ menu, not a corrupt setting.

        In practice this only reaches anybody when the process was replaced
        under a session that was still going on, since the parked page it would
        come back beside lives in RAM and dies with the daemon. The durable way
        back to a picture searched last week is its row in the history, which
        costs a hundred bytes and cannot go stale in silence — see
        SearchHistory.
        """
        saved = CFG["attached_image"]
        if not isinstance(saved, dict):
            return
        path = saved.get("path") or ""
        try:
            there = bool(path) and Path(path).is_file()
        except Exception:
            there = False
        if not there:
            CFG["attached_image"] = None
            return
        self._closed_with_image = {"attached": dict(saved),
                                   "serial": self._attach_serial,
                                   "shown_for": None, "result_uri": None}

    def _reset_placeholder(self) -> None:
        """What the empty field offers, which depends on what is clipped to it.

        With a picture attached the field is not a search box any more, it is
        Lens' "Add to your search" — and saying so is what stops the chip
        looking like a decoration that Enter will ignore.
        """
        if self.finding:
            return              # find mode owns the placeholder; leave it alone
        self.entry.set_placeholder_text("Add to your search…" if self.attached
                                        else "Search Google…")

    def _attach_from_uri(self, uri: str) -> None:
        """Fetch a public picture far enough to show it as a chip.

        The search does not need the bytes — Google fetches the image from its
        own address, which is why _search_image() navigates instead of
        uploading — but the chip does, and so does having it still there after
        a restart. Through the page's own session rather than through GIO, so
        an image behind a login arrives rather than a 403; _download_to()
        carries that argument in full.

        Best-effort by design: if it never lands, the search still ran and the
        only thing missing is the thumbnail.
        """
        try:
            name = Path(urllib.parse.urlparse(uri).path).name
        except Exception:
            name = ""
        ext = Path(name).suffix.lower()
        if ext not in IMAGE_EXTS:
            ext = ".png"
        try:
            _own_dir(DATA_DIR)
        except Exception:
            return
        # Its own name, not ATTACH_NAME: attach_image() clears the other
        # extensions out of DATA_DIR on the way in, and a download writing into
        # the file it is about to be handed would be a race with itself.
        landing = DATA_DIR / ("incoming-attach" + ext)
        # What the field held when this fetch went out. If anything has been
        # attached since, this answer is stale and must not touch the file.
        serial = self._attach_serial

        def done(ok: bool) -> None:
            # Whether it worked or not, the scratch file goes: nothing else
            # ever removes it (attach_image's sweep only knows "attached.*"),
            # so a failed fetch used to leave one behind for good.
            try:
                if ok and serial == self._attach_serial \
                        and landing.is_file() and landing.stat().st_size:
                    self.attach_image(str(landing), name=name or "image",
                                      uri=uri)
                    # A download and a navigation are racing, and this one can
                    # land second. attach_image() clears _lens_shown_for on the
                    # way in — rightly, since a new picture cannot claim the
                    # old one's results — so if the matches are already up when
                    # the bytes arrive, say so again. Without this the fast
                    # refinement was lost to a race and the next query silently
                    # re-uploaded a picture Google had already been shown.
                    if self.attached and self.web is not None:
                        # Through the same gate as every other navigation, so a
                        # download that lands while the user is already on
                        # another tab cannot claim that tab as the results.
                        self._track_lens_page(self.web.get_uri() or "")
            except Exception:
                pass
            try:
                landing.unlink()
            except OSError:
                pass

        self._download_to(uri, str(landing), done)

    def _remember_image_search(self, words: str, *, name: str,
                               path: str | None, uri: str | None) -> None:
        """Put this visual search in the history, as a reference to the picture.

        The label is the words it was searched with, or the picture's own name
        when there were none — a row has to read as something, and "" does not.
        _run_history_image() undoes that choice by the same test, so a row
        labelled with a filename puts nothing in the field when it is picked.

        The picture itself is never copied anywhere. SearchHistory carries the
        whole of that argument, and the short version is that a reference costs
        a hundred bytes in a file Halo is already writing, while a thumbnail
        cache is a folder of somebody's photographs that only ever grows.

        Nothing here can stop a search: HISTORY.add() drops an entry with no
        durable reference on the floor, which is the right answer for pixels
        dragged out of a page or a data: URI, and it is the caller's business
        neither way.
        """
        words = (words or "").strip()
        label = words or (name or "").strip() or "Image"
        HISTORY.add(label, "image",
                    {"name": name or "image", "words": words,
                     "path": path, "uri": uri})

    def _search_attached(self, text: str) -> None:
        """Search the attached picture, with whatever words are in the field.

        Two routes, and choosing between them is the whole of the second bug
        this was written for. A query typed against a picture whose matches are
        already on screen is a *refinement* — Lens' own "Add to your search" —
        and re-uploading for it would throw the visual search away and spend
        another ten seconds arriving back where it started. Anything else, a
        first query or a query against a picture that has just been attached,
        is a fresh search.

        _showing_attachment_results() is what tells the two apart, and it is
        deliberately strict: the picture on the field must be the one those
        results are for, AND the view must still be on the surface it landed
        on. Click AI Mode or the All tab and it says no, because rewriting
        their address is what produced an empty page and then a 400.
        """
        path = self.attached["path"]
        if text and self.expanded and self._showing_attachment_results():
            self.lens_text = ""
            self._set_busy(True)
            self._show_loading(f"Adding “{text}”…",
                               cap_ms=self.COVER_CAP_LENS_LAND_MS)
            # A refinement is a search of its own — the same picture with
            # different words is a different row, and it is the one somebody
            # will want back.
            refined = self.attached or {}
            self._remember_image_search(text, name=refined.get("name") or "",
                                        path=refined.get("origin"),
                                        uri=refined.get("uri"))
            self._apply_lens_text(text)
            return
        self.load_lens(path, text=text)

    def load_lens(self, path: str, text: str | None = None) -> None:
        """Reverse-search a local image with Google Lens."""
        try:
            size = Path(path).stat().st_size
        except OSError:
            _notify(APP_NAME, "Could not read that image.")
            return
        if size > 20 * 1024 * 1024:
            _notify(APP_NAME, "That image is too large for Lens (20 MB limit).")
            return

        if not self._ensure_webview():
            _notify(APP_NAME, "Could not start the web view.")
            return
        # The words first. Attaching rewrites the field's placeholder and a
        # caller may have passed text of its own, so what is being searched
        # with has to be read before anything can touch the field.
        words = (text if text is not None else self.entry.get_text()).strip()
        # The chip beside the field, and the copy under DATA_DIR that outlives
        # the process. Re-attaching the picture already attached is harmless
        # and is the common case: it is what a refinement that had to fall back
        # to a fresh upload does.
        self.attach_image(path)
        if self.attached:
            path = self.attached["path"]
        self.pending_lens = path
        self.lens_attempts = 0
        self._lens_await_load = False
        self._awaiting_lens_url_until = 0   # the upload route owns this search now
        self._forget_lens_page()
        # Words typed before the capture refine the visual search.
        self.lens_text = words
        # Remembered here rather than when the results land: this is the moment
        # the search is definitely happening, and the one place where both the
        # picture's own reference and the words are in hand.
        att = self.attached or {}
        self._remember_image_search(words, name=att.get("name") or "",
                                    path=att.get("origin"),
                                    uri=att.get("uri"))
        self._set_busy(True)
        # The field keeps its words, because the chip beside them now says what
        # they are refining. Clearing it and announcing the image in the
        # placeholder was the only way to say so when there was nothing to see,
        # and it cost the user their own query the moment the search landed.
        # notify=False: putting text back that is already there is not typing,
        # and letting it look like typing opened a suggestion list over the
        # results.
        #
        # No "if there is a chip" branch any more: attach_image() falls back to
        # a generic icon rather than refusing, so by here there always is one.
        self._set_entry_text(words, notify=False)
        self._reset_placeholder()
        self._navigate("https://lens.google.com/", lens=True)
        self.expand()
        self._show_loading(f"Searching Lens for “{self.lens_text}”…"
                           if self.lens_text else "Searching this image with Lens…",
                           cap_ms=self.COVER_CAP_LENS_MS)
        # From here it is event-driven: load-changed starts the click, and
        # notify::uri detects the landing. WebKit emits load-finished even for
        # failed loads, so there is always something to kick off the chain.

    def _on_create_view(self, _web, navigation_action):
        """A page asked for a new window. Honour it without spawning one.

        target="_blank", window.open() and a middle-clicked link all arrive here,
        and they all continue in the view the user is looking at: there is one
        panel, and a second window would be one nobody asked for, opening behind
        a popup that floats above it. Only what the panel cannot show goes out.
        Returning None declines to create a WebView.
        """
        try:
            uri = navigation_action.get_request().get_uri() or ""
        except Exception:
            uri = ""
        if uri:
            if _is_web_uri(uri):
                # ...unless it is the page already on screen. A site that
                # re-opens itself on load — a pop-under, an interstitial, an
                # "open in a new tab" widget — is harmless in a browser, where
                # it makes a tab. Here it was a second main-frame navigation to
                # the same address, so it pushed a duplicate back/forward entry
                # and Back landed on the page it was already showing. It reran
                # on every reload, because the script that opens it runs again,
                # which is why the report was about the reload button.
                #
                # Ignored rather than sent through _navigate(): that now turns a
                # request for the current page into a reload, and a reload runs
                # the script that opened it, which asks again. This is the line
                # that stops that being a loop.
                if not self._is_current_uri(uri):
                    self._navigate(uri)
            else:
                self._hand_to_browser(uri)
        return None

    def _is_current_uri(self, uri: str) -> bool:
        """Is this the address the panel is already showing? Fragments aside."""
        if self.web is None:
            return False
        here = urllib.parse.urldefrag(self.web.get_uri() or "")[0]
        return bool(here) and here == urllib.parse.urldefrag(uri)[0]

    def _on_file_chooser(self, _web, request) -> bool:
        """Answer the chooser ourselves while a Lens search is in flight.

        Two conditions, not one. A hand-off has to be in flight, and the page
        asking has to be Google's — because what this does is hand a file off
        the user's disk to whatever page asked for it, without asking them.

        The second condition guards a window that is small but real: a hand-off
        navigates to lens.google.com and waits, and what actually arrives is
        not always Lens. A consent interstitial, a /sorry/ CAPTCHA or an
        outright redirect can land in that window, and an auto-answered chooser
        on a page Halo did not mean to be talking to would upload the picture
        somewhere nobody chose. Google's own pages are the only ones this
        hand-off can possibly be talking to, so that is the whole guest list.
        """
        path = self.pending_lens
        if not path:
            return False        # an ordinary upload: let WebKit ask the user
        if not _is_google_page(self.web.get_uri() or ""
                               if self.web is not None else ""):
            return False
        # The attachment lives under one fixed name in DATA_DIR, so a picture
        # attached while this hand-off was in flight can have replaced or
        # removed the file it names. Selecting a path that is not there makes
        # the upload fail silently three times over and end in a notification
        # about Lens not taking the image, which is a confusing way to say
        # "the file moved".
        try:
            there = os.path.isfile(path)
        except Exception:
            there = False
        if not there:
            self._abandon_lens()
            self._set_busy(False)
            self._hide_loading()
            return False
        try:
            request.select_files([path])
        except Exception:
            request.cancel()
        return True

    # The Lens hand-off is event-driven, not timed. Blind waits were what made
    # it feel like watching a slow macro: we now click the upload input the
    # moment the page reports load-finished (the script itself polls for the
    # input every 60ms) and detect success from notify::uri instead of sleeping.
    #
    # We never check which URL we are on before clicking, because
    # lens.google.com redirects to www.google.com/?olud and that page carries
    # the very same upload form.
    def _arm_lens_click(self) -> None:
        """Schedule the one click this page gets, replacing any pending one.

        lens.google.com redirects on the way in, and every hop reports
        load-finished. Each hop used to arm its own click *and* its own retry, so
        three attempts were spent racing each other on the same page instead of
        being three clean tries.
        """
        if self._lens_click_timer:
            GLib.source_remove(self._lens_click_timer)
        self._lens_click_timer = GLib.timeout_add(LENS_SETTLE_MS, self._lens_click)

    def _cancel_lens_timers(self) -> None:
        for name in ("_lens_click_timer", "_lens_retry_timer",
                     "_lens_text_timer"):
            timer = getattr(self, name)
            if timer:
                GLib.source_remove(timer)
                setattr(self, name, 0)

    def _lens_click(self) -> bool:
        """Click once, then check back; a fresh page per attempt."""
        self._lens_click_timer = 0
        if not self.pending_lens:
            return False
        if self.web is None:
            # No engine to hand the picture to, and no timer left armed either.
            # Returning here without finishing left pending_lens set for ever
            # with the state machine dead: the spinner never stopped, and every
            # later file chooser in the session was auto-answered.
            self._finish_lens()
            self._set_busy(False)
            return False
        # CLICK_LENS_UPLOAD_JS hunts for any file input that takes an image and
        # clicks it, and _on_file_chooser then answers with the user's picture.
        # On a page that is not Google's, that pair is Halo uploading a private
        # file to somebody else's form with nothing on screen to say so. The
        # hand-off legitimately hops through several Google hosts, which is why
        # this is a host test and not a URL match.
        if not _is_google_page(self.web.get_uri() or ""):
            return False
        self.web.evaluate_javascript(CLICK_LENS_UPLOAD_JS, -1, None, None,
                                     None, None, None)
        if self._lens_retry_timer:
            GLib.source_remove(self._lens_retry_timer)
        self._lens_retry_timer = GLib.timeout_add(LENS_RETRY_MS, self._lens_retry)
        return False

    def _lens_retry(self) -> bool:
        """Nothing happened, so the click was too early: start over cleanly."""
        self._lens_retry_timer = 0
        if not self.pending_lens:
            return False
        if self.web is None:
            self._finish_lens()         # see _lens_click: never leave it hanging
            self._set_busy(False)
            return False
        if _is_lens_result(self.web.get_uri() or ""):
            return False
        self.lens_attempts += 1
        if self.lens_attempts >= 3:
            self._finish_lens()
            # Uncover Lens' own page, so the user lands somewhere they can act:
            # it accepts a drop or a file picker.
            self.expand()
            self._hide_loading()
            # Nothing else will stop the bar spinner from here — no further load
            # is coming — so it would have kept turning for the rest of the session.
            self._set_busy(False)
            _notify(APP_NAME, "Lens did not take the image automatically — drop "
                              "it onto the page, or use its “upload a file” button.")
            return False
        # A rejected upload leaves a spent form behind, so reload for a new one.
        self._navigate("https://lens.google.com/", lens=True)
        return False

    def _lens_landed(self, uri: str = "") -> None:
        """Reached a Lens results page. Open up and apply any typed words."""
        # What is on screen now answers for the attached picture, so the next
        # query against it is a refinement rather than another upload. Set
        # before _finish_lens(), which is where the hand-off stops being one,
        # and with the address as well as the picture: which page it was is
        # what later tells a refinement from a click onto another tab.
        self._note_lens_page(uri or (self.web.get_uri() or ""
                                     if self.web is not None else ""))
        text, self.lens_text = self.lens_text, ""
        self._finish_lens()
        if text:
            self._apply_lens_text(text)     # keeps the cover up a moment longer
        else:
            # The URI flips at the *start* of the navigation to the results, not
            # when they arrive, so a short fixed wait here uncovered the uploader
            # we were in the middle of leaving — half a second of Google's upload
            # page between the spinner and the matches. Wait for the incoming page
            # to actually be readable instead (_on_load_progress lifts it), and
            # keep this only as the deadline it was always meant to be.
            self._lens_await_load = True
            self._uncover_after(self.COVER_CAP_LENS_LAND_MS)

    def _apply_lens_text(self, text: str) -> None:
        """Add words to the visual search currently on screen."""
        if self.web is None:
            return
        # Which page these words are for. Checked again before the script runs
        # and again before the address is rewritten, because 900ms is long
        # enough for the view to have moved.
        target = self.web.get_uri() or ""
        script = ADD_TEXT_TO_LENS_JS % {"text": json.dumps(text)}

        def still_there() -> bool:
            """Is the view still on the page these words were typed against?

            By surface rather than by exact address, so Google adding a
            parameter of its own between the Enter and the injection does not
            silently drop the query — and so that a page which is genuinely
            somewhere else never gets the script. ADD_TEXT_TO_LENS_JS types
            into the first plausible search box it finds and submits the form,
            so on somebody else's site it is Halo filling in their search for
            them and pressing Enter.
            """
            if self.web is None:
                return False
            here = self.web.get_uri() or ""
            return (_is_lens_result(here)
                    and self._lens_surface(here) == self._lens_surface(target))

        def applied(web, result, _data) -> None:
            outcome = ""
            try:
                value = web.evaluate_javascript_finish(result)
                outcome = value.to_string() if value else ""
            except Exception:
                outcome = ""
            if outcome == "submitted":
                # Lens' own box took the words and its form is on its way. The
                # cover has to outlast that load rather than come off 400ms
                # later on the page being left behind — which is exactly what
                # "I don't know if anything changed" looked like: the
                # un-refined results sat there, then flipped a second later.
                # The page submitted its own form, so _nav_pending is False and
                # nothing here is holding the cover for it; this deadline is.
                self._uncover_after(self.COVER_CAP_LENS_LAND_MS)
                return
            if not still_there():
                self._set_busy(False)
                self._hide_loading()
                return
            # No usable input, or the keypress did nothing: ask for the
            # refinement through the URL instead.
            #
            # REPLACING q=, not appending one. This used to decline the
            # fallback outright on any URL that already carried a q= — which is
            # precisely the URL a refinement leaves behind. So the first typed
            # query against a picture worked and every one after it did nothing
            # at all: the box Lens renders on its own results page is not the
            # one this script finds, the fallback was refused, and the cover
            # came off four hundred milliseconds later on the same page.
            uri = self.web.get_uri() or ""
            refined = self._lens_url_with_query(uri, text)
            if refined and refined != uri:
                self._navigate(refined)
                # Our own navigation, so _view_stale() holds the cover until
                # the new page commits AND paints; this is the ceiling on that,
                # not the wait. Armed after _navigate(), which is what sets the
                # flag the cover reads.
                self._uncover_after(self.COVER_CAP_LENS_LAND_MS)
                return
            # Nothing was navigated to, so nothing is coming that would turn
            # the bar spinner off — FINISHED is what normally does it, and
            # there is no load to finish. This call is the only thing that
            # stops it turning for the rest of the session, which is what it
            # did once _search_attached() began setting it before calling here.
            self._set_busy(False)
            self._uncover_after(400)

        def drive() -> bool:
            self._lens_text_timer = 0
            if not still_there():
                self._set_busy(False)
                self._hide_loading()
                return False
            self.web.evaluate_javascript(script, -1, None, None, None,
                                         applied, None)
            return False

        # Give Lens a moment to build its own search box before driving it.
        # Held by its source id so it can be cancelled: an untracked timer went
        # on firing after the pill was closed and after the engine was handed
        # back, and reaching through self.web from inside it raised
        # AttributeError in a GLib callback — a traceback on stderr and a cover
        # nothing ever lifted. _cancel_lens_timers() takes it down with the
        # rest, so hide_popup() and _finish_lens() both reach it.
        if self._lens_text_timer:
            GLib.source_remove(self._lens_text_timer)
        self._lens_text_timer = GLib.timeout_add(900, drive)
        # Backstop, in case the evaluation never calls back at all. The Lens
        # landing deadline rather than 2200ms, so it cannot undercut the wait
        # that a refinement in flight legitimately needs — _uncover_after()
        # REPLACES the armed timer rather than taking the longer of the two.
        self._uncover_after(self.COVER_CAP_LENS_LAND_MS)

    @staticmethod
    def _lens_surface(uri: str) -> tuple | None:
        """Which visual search on which Google surface: (host, path, udm, vsrid).

        This is what tells a refinement from a click. `udm` names the surface —
        26 is Lens’ visual matches, 50 is AI Mode, 2 is Images, and the All
        tab carries none — and `vsrid` names the visual search itself, so two
        addresses with the same four are one page showing a different query,
        and two with different ones are different pages or different pictures.

        vsrid is in here and the rest of the query string is not, and the
        difference is not arbitrary. Google adds and drops parameters of its
        own between one navigation and the next (sca_esv, ved, oq,
        gsessionid), so comparing whole query strings would decide the page had
        changed a moment after landing on it and throw away every refinement
        that followed. vsrid does not churn like that: it is the identity of
        the search. Leaving it out meant that clicking one of Google’s own
        "search this image instead" matches — a new visual search on the same
        surface — read as "the same page with a different query", so the words
        typed next refined a picture the chip was not showing.

        This exists because LENS_RESULT_MARKERS cannot answer the question.
        "vsrid=" stays in the address for every tab reached from a visual
        search, so AI Mode and the All tab both matched, and a query typed on
        either of them bolted a q= onto a udm=50 address — a combination Google
        never produces. Reported as an empty results page and then a 400.
        """
        try:
            parts = urllib.parse.urlsplit(uri)
        except Exception:
            return None
        if not parts.netloc:
            return None
        try:
            query = dict(urllib.parse.parse_qsl(parts.query,
                                                keep_blank_values=True))
        except Exception:
            query = {}
        return (parts.netloc, parts.path.rstrip("/"),
                query.get("udm", ""), query.get("vsrid", ""))

    def _showing_attachment_results(self) -> bool:
        """Is the view showing the attached picture’s own visual matches?

        Deliberately stricter than _on_lens_results(), and the two are not
        interchangeable. That one asks "does this address look like it came
        from a visual search", which is the right question for lifting the
        loading cover during a hand-off and the wrong one for deciding whether
        a page may be rewritten. This asks whether the page is still the very
        results Halo landed on, for the picture that is still on the field.

        Three things have to hold, and each rules out a real case:
          · a picture is attached and these results are that picture’s
            — otherwise the chip has been taken off or replaced;
          · the address still looks like a visual search at all;
          · and it is the same surface as the results we landed on — otherwise
            the user has clicked into AI Mode, Images or the All tab, and the
            answer is a fresh search rather than a rewrite of their address.
        """
        if self.web is None or not self.attached or not self._lens_result_uri:
            return False
        if self._lens_shown_for != self.attached["path"]:
            return False
        uri = self.web.get_uri() or ""
        if not _is_lens_result(uri):
            return False
        return self._lens_surface(uri) == self._lens_surface(self._lens_result_uri)

    # How long a by-URL image search may still claim the next Lens result page
    # as its own. Generous, because Google fetching the image itself and
    # redirecting twice is genuinely slower than a local upload — and bounded,
    # because an expectation that never expires is a page adopted by mistake.
    LENS_BY_URL_WINDOW_MS = 30000

    def _expecting_lens_url(self) -> bool:
        return (self._awaiting_lens_url_until > 0
                and GLib.get_monotonic_time() < self._awaiting_lens_url_until)

    def _note_lens_page(self, uri: str) -> None:
        """Remember that this address is the attached picture’s results."""
        self._lens_result_uri = uri
        self._lens_shown_for = (self.attached or {}).get("path")

    def _forget_lens_page(self) -> None:
        self._lens_result_uri = None
        self._lens_shown_for = None

    def _track_lens_page(self, uri: str) -> None:
        """Follow the view on and off the visual search, on every navigation.

        Three outcomes: the results are arriving (remember them), the same page
        is showing a different query (a refinement — follow the address), or the
        view has gone somewhere else entirely (forget them, so the next query
        starts a fresh search instead of rewriting an address that is no longer
        a Lens result).

        The last of those is what was missing, and it is why clicking AI Mode
        and then typing did nothing anybody could see.
        """
        lensish = _is_lens_result(uri)
        if self.pending_lens and lensish:
            # Landed: stop auto-answering file choosers, and refine by text if
            # the user typed something before capturing.
            self._lens_landed(uri)
            return
        if self._lens_result_uri is None:
            # The by-URL route uploads nothing, so it has no hand-off to
            # finish and this is the first sight of its results. Only ever a
            # landing Halo actually asked for, and recently: the deadline in
            # _expecting_lens_url() is what says so, and without it the All tab
            # — which still carries vsrid= — was adopted as the picture's own
            # results.
            #
            # Not cleared when a page arrives that is NOT lensish, because the
            # by-URL route redirects through lens.google.com/uploadbyurl, which
            # matches no marker; clearing there would throw the expectation
            # away one hop before the results it is waiting for.
            if (lensish and self._expecting_lens_url()
                    and self.attached is not None):
                self._awaiting_lens_url_until = 0
                self._note_lens_page(uri)
            return
        if self._lens_surface(uri) == self._lens_surface(self._lens_result_uri):
            self._lens_result_uri = uri     # same page, different query
        else:
            self._forget_lens_page()

    @staticmethod
    def _lens_url_with_query(uri: str, text: str) -> str:
        """A Lens results address carrying `text` as its query, or "" .

        Rebuilt from its parts rather than concatenated, which is what makes it
        safe to call on a URL that already has a q=. Everything else in the
        query string is kept in the order it arrived: vsrid, udm and the rest
        are the visual search itself, and dropping any of them turns a
        refinement into a plain web search for the words.

        Only ever on a page LENS_RESULT_MARKERS already recognises. A q=
        bolted onto some other address would be a search for the words on
        whatever site the panel happened to be showing.
        """
        if not _is_lens_result(uri):
            return ""
        try:
            parts = urllib.parse.urlsplit(uri)
            kept = [(k, v) for k, v in
                    urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                    if k != "q"]
            kept.append(("q", text))
            return urllib.parse.urlunsplit(
                parts._replace(query=urllib.parse.urlencode(kept)))
        except Exception:
            return ""

    def _abandon_lens(self) -> None:
        """Give up a hand-off because the panel is going somewhere else.

        Distinct from _finish_lens(), which is what a hand-off that *arrived*
        calls. This is the one that was missing entirely: a hand-off had no way
        to end except by landing, by exhausting its three attempts, or by the
        pill being closed.
        """
        if not self.pending_lens:
            return
        self._finish_lens()             # clears pending_lens and both timers
        self._lens_await_load = False
        self.lens_text = ""
        self.lens_attempts = 0
        self._awaiting_lens_url_until = 0

    def _finish_lens(self) -> None:
        self.pending_lens = None
        self._cancel_lens_timers()
        self._reset_placeholder()

    # ── the right-click menu ────────────────────────────────────────────
    #
    # WebKit's stock menu was wrong for Halo in three ways at once, and all three
    # arrived as a single complaint. It is localised to $LANG, so on a German
    # desktop it read "Bild speichern unter" and "Verweisziel kopieren" inside an
    # English UI. It describes a browser Halo is not — "open in a new tab", for
    # something that has no tabs and loads the link in this panel instead. And it
    # cannot search an image, which is the one thing a search popup should be
    # better at than a browser, not worse.
    #
    # The menu is therefore rebuilt rather than edited. Editing means knowing what
    # WebKit put there — which varies by port, version and page — and leaves every
    # label in the wrong language anyway. Building it means the menu can only ever
    # say what Halo will actually do.
    #
    # Two kinds of entry cannot be rebuilt, because their text or their state comes
    # from the web process and nothing in the hit test carries it: the spelling
    # suggestions (whose labels are the suggested words) and the media controls
    # (which know whether this video is playing, muted or looping). Those are
    # lifted out of WebKit's menu before it is cleared and put back afterwards —
    # in WebKit's wording, which is the price of them being right.
    _CTX_CARRY = (
        WebKit.ContextMenuAction.SPELLING_GUESS,
        WebKit.ContextMenuAction.NO_GUESSES_FOUND,
        WebKit.ContextMenuAction.LEARN_SPELLING,
        WebKit.ContextMenuAction.IGNORE_SPELLING,
        WebKit.ContextMenuAction.IGNORE_GRAMMAR,
        WebKit.ContextMenuAction.MEDIA_PLAY,
        WebKit.ContextMenuAction.MEDIA_PAUSE,
        WebKit.ContextMenuAction.MEDIA_MUTE,
        WebKit.ContextMenuAction.TOGGLE_MEDIA_CONTROLS,
        WebKit.ContextMenuAction.TOGGLE_MEDIA_LOOP,
        WebKit.ContextMenuAction.ENTER_VIDEO_FULLSCREEN,
    )
    _CTX_SPELLING = _CTX_CARRY[:5]
    _CTX_MEDIA_CONTROLS = _CTX_CARRY[5:]

    # How much of a selection fits in a menu entry before it starts setting the
    # width of the whole menu.
    CTX_LABEL_MAX = 32

    @classmethod
    def _ctx_label(cls, text: str) -> str:
        """Page text, made safe to put in a menu label.

        Two hazards, both of them real. GTK reads "_" in a label as a mnemonic —
        WebKit's own entries use it that way — so a selection of "read_me" would
        appear as "readme" with the m underlined, and a literal underscore has to
        be written twice. And a selection can be a whole paragraph, which would
        make the menu as wide as the screen.
        """
        text = " ".join(text.split())
        if len(text) > cls.CTX_LABEL_MAX:
            text = text[:cls.CTX_LABEL_MAX - 1].rstrip() + "…"
        return text.replace("_", "__")

    @staticmethod
    def _ctx_stock(action, label: str):
        """WebKit's own behaviour, under a name that describes what Halo does."""
        return WebKit.ContextMenuItem.new_from_stock_action_with_label(action, label)

    def _ctx_custom(self, label: str, handler, target: str | None = None,
                    enabled: bool = True):
        """One of Halo's own entries.

        In WebKit 6 a custom item is a GAction plus the GVariant it is activated
        with — the GtkAction constructor WebKit2GTK 4.x used for this is gone. The
        action has to stay alive for as long as the menu can be clicked, which is
        what _ctx_actions is for; it is emptied when the menu closes.
        """
        name = "halo-ctx-%d" % len(self._ctx_actions)
        if target is None:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p: handler())
            variant = None
        else:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, p: handler(p.get_string()))
            variant = GLib.Variant.new_string(target)
        action.set_enabled(enabled)
        self._ctx_actions.append(action)
        return WebKit.ContextMenuItem.new_from_gaction(action, label, variant)

    # There is no _ctx_enable here any more, and the reason is worth keeping.
    # It used to take a stock item and grey it out "if the action supports it",
    # checking isinstance(action, Gio.SimpleAction) and quietly doing nothing
    # otherwise. Nothing ever supported it: WebKit wraps a stock action in a
    # WebKitContextMenuGAction, which implements GAction but is not a
    # GSimpleAction and has no set_enabled method at all. So the check was false
    # every single time and the call was a no-op every single time — while
    # reading, at the call site, exactly as though Back had been greyed out on a
    # page with nothing behind it. It had not. Both entries were always live.
    #
    # An audit that drove _on_context_menu with a synthetic hit test and read the
    # composed menu back is what found it; the code alone reads as correct. Only
    # Halo's own items, built on a real GSimpleAction by _ctx_custom, can be
    # disabled — which is why Back and Forward are now Halo's own.

    def _on_context_menu_dismissed(self, *_a) -> None:
        # These belong to the menu that has just closed. Holding them would keep a
        # chain of closures — and the image URI they were built around — alive
        # until some later right-click happened to overwrite them.
        self._ctx_actions.clear()

    def _on_context_menu(self, _web, menu, hit) -> bool:
        """Build the menu from the hit test.

        The WebKit 6 signature is (view, menu, hit_test_result) — three arguments.
        WebKit2GTK 4.x passed the GdkEvent as well, between the last two, and
        writing that version here does not fail: it binds the hit test to the name
        `event` and then reads link and image URIs off a GdkEvent, which has none.
        The event moved onto the menu; menu.get_event() is where it lives now.

        Returning False shows the menu as we leave it. True would show nothing.
        """
        link_uri = hit.get_link_uri() if hit.context_is_link() else None
        image_uri = hit.get_image_uri() if hit.context_is_image() else None
        media_uri = hit.get_media_uri() if hit.context_is_media() else None
        editable = hit.context_is_editable()
        selection = self._page_selection if hit.context_is_selection() else ""

        # Taken before the menu is cleared, and by identity: remove_all() drops
        # WebKit's reference to each item, and this list is what keeps the ones
        # worth keeping alive across it.
        carried: dict[int, list] = {}
        offered = set()
        for item in menu.get_items():
            if item.is_separator():
                continue
            action = int(item.get_stock_action())
            offered.add(action)
            if action in {int(a) for a in self._CTX_CARRY}:
                carried.setdefault(action, []).append(item)

        def take(*actions) -> list:
            out = []
            for action in actions:
                out += carried.pop(int(action), [])
            return out

        self._ctx_actions.clear()
        menu.remove_all()

        A = WebKit.ContextMenuAction
        groups: list[list] = []

        # The image group first, and the search first within it. This is the entry
        # the menu was rebuilt for, and burying it under four things the user did
        # not come for would be answering the complaint on paper only.
        if image_uri:
            group = []
            if self._image_search_kind(image_uri):
                group.append(self._ctx_custom("Search image with Google",
                                              self._search_image, image_uri))
            group += [
                # WebKit calls this "Open Image in New Window". There is no second
                # window; it opens here, which is what was wanted anyway.
                self._ctx_stock(A.OPEN_IMAGE_IN_NEW_WINDOW, "Open image"),
                # Not the stock COPY_IMAGE_TO_CLIPBOARD: see _copy_image.
                self._ctx_custom("Copy image", self._copy_image, image_uri),
                self._ctx_stock(A.COPY_IMAGE_URL_TO_CLIPBOARD, "Copy image address"),
                # Not the stock DOWNLOAD_IMAGE_TO_DISK: that writes into
                # ~/Downloads without asking and without saying so.
                self._ctx_custom("Save image as…", self._save_uri_as, image_uri),
            ]
            groups.append(group)

        if link_uri:
            groups.append([
                self._ctx_stock(A.OPEN_LINK, "Open"),
                # What Ctrl+click already does, finally said out loud. WebKit's
                # "open in a new tab" did the opposite of what it promised: the
                # create handler catches the request and loads it in this panel.
                self._ctx_custom("Open in browser", self._hand_to_browser, link_uri),
                self._ctx_stock(A.COPY_LINK_TO_CLIPBOARD, "Copy link"),
                self._ctx_custom("Save link as…", self._save_uri_as, link_uri),
            ])

        if media_uri:
            # Which of the two it is, read off what WebKit itself offered: the hit
            # test says "media" and no more, and calling a song a video is the kind
            # of small wrongness this whole change exists to remove.
            video = (int(A.OPEN_VIDEO_IN_NEW_WINDOW) in offered
                     or int(A.COPY_VIDEO_LINK_TO_CLIPBOARD) in offered
                     or int(A.DOWNLOAD_VIDEO_TO_DISK) in offered)
            kind = "video" if video else "audio"
            controls = take(*self._CTX_MEDIA_CONTROLS)
            if controls:
                groups.append(controls)
            groups.append([
                self._ctx_stock(A.OPEN_VIDEO_IN_NEW_WINDOW if video
                                else A.OPEN_AUDIO_IN_NEW_WINDOW, f"Open {kind}"),
                self._ctx_stock(A.COPY_VIDEO_LINK_TO_CLIPBOARD if video
                                else A.COPY_AUDIO_LINK_TO_CLIPBOARD,
                                f"Copy {kind} address"),
                self._ctx_custom(f"Save {kind} as…", self._save_uri_as, media_uri),
            ])

        if editable:
            spelling = take(*self._CTX_SPELLING)
            if spelling:
                groups.append(spelling)
            if selection:
                groups.append([self._ctx_search_item(selection)])
            groups.append([
                self._ctx_stock(A.CUT, "Cut"),
                self._ctx_stock(A.COPY, "Copy"),
                self._ctx_stock(A.PASTE, "Paste"),
                self._ctx_stock(A.PASTE_AS_PLAIN_TEXT, "Paste as plain text"),
                self._ctx_stock(A.DELETE, "Delete"),
            ])
            groups.append([
                self._ctx_stock(A.SELECT_ALL, "Select all"),
                self._ctx_stock(A.INSERT_EMOJI, "Insert emoji"),
            ])
            # Deliberately not the font submenu, the input-method list or the
            # Unicode-control-character list. Halo edits a search box on somebody
            # else's page; none of them has ever been the reason for a right-click
            # here, and all three were in the way of the entries that are.
        elif hit.context_is_selection():
            # On the hit test's word, not on the reported text: the two can
            # disagree — a selection inside a frame is not what SELECTION_WATCH_JS
            # watches — and a right-click on selected words that offered Back and
            # Forward instead of Copy would be a worse menu than the stock one.
            group = []
            if selection:
                group.append(self._ctx_search_item(selection))
            group.append(self._ctx_stock(A.COPY, "Copy"))
            groups.append(group)

        # Only when the click landed on the page itself, which is where WebKit puts
        # them too — and where the toolbar's own buttons are the alternative, so
        # they are shown greyed rather than lying about being available.
        if not groups:
            loading = self.web is not None and self.web.is_loading()
            # _can_history, not web.can_go_back: after Google silently corrects
            # a spelling there is a step back that WebKit's own list does not
            # know about, and a menu that disagreed with the toolbar chip about
            # whether Back exists would be the same class of small wrongness
            # this menu was rebuilt to remove.
            back = self._can_history(-1)
            fwd = self._can_history(1)
            groups.append([
                # Halo's own, not A.GO_BACK / A.GO_FORWARD — see the note where
                # _ctx_enable used to be. These run the same _history() the
                # toolbar buttons and Alt+← run, so the three cannot drift apart,
                # and being GSimpleActions they can genuinely be shown as
                # unavailable rather than merely described that way.
                self._ctx_custom("Back", lambda: self._history(-1), None, back),
                self._ctx_custom("Forward", lambda: self._history(1), None, fwd),
                self._ctx_stock(A.STOP, "Stop") if loading
                else self._ctx_stock(A.RELOAD, "Reload"),
            ])

        for n, group in enumerate(groups):
            if n:
                menu.append(WebKit.ContextMenuItem.new_separator())
            for item in group:
                menu.append(item)
        return False

    def _ctx_search_item(self, selection: str):
        """The selected words, offered as a search.

        Straight into _search_text, which is where dropped, pasted and
        middle-clicked text already goes: it flattens the text, caps it at
        QUERY_LIMIT and runs it exactly as though it had been typed. Doing this
        one differently would mean a selection searched from the menu behaving
        unlike the same selection middle-clicked.
        """
        return self._ctx_custom(
            "Search Google for “%s”" % self._ctx_label(selection),
            self._search_text, selection)

    # ── what those entries do ───────────────────────────────────────────

    def _image_search_kind(self, uri: str) -> str:
        """How this image could be searched, if at all: "url", "data" or "".

        data: is not a corner case here, it is the common one: Google's own image
        results are a page full of data: thumbnails, so the place a user is most
        likely to try "search this image" is exactly the place a by-URL endpoint
        cannot be used. Those go through the file upload Halo already has.

        blob: would need the page to hand its bytes back through JavaScript before
        anything could be done with them, which is a round trip a menu being built
        cannot wait for — so no entry is offered at all, rather than one that lands
        on a Google error page.
        """
        scheme = _uri_scheme(uri)
        if scheme in ("http", "https"):
            return "url"
        if scheme == "data" and self._decode_data_uri(uri) is not None:
            return "data"
        return ""

    def _search_image(self, uri: str, text: str = "") -> None:
        """Reverse-search a picture that already has an address.

        Right-clicking one in the page is where this started; a picture dragged
        in from a browser and a history row with no local file behind it come
        through here too, and those two can carry words with them.
        """
        kind = self._image_search_kind(uri)
        if kind == "data":
            path = self._data_uri_to_file(uri)
            if path:
                self.load_lens(path, text=text)
            else:
                _notify(APP_NAME, "Could not read that image.")
            return
        if kind != "url" or self.web is None:
            return
        # A public address needs no upload: Google fetches the image itself, and
        # LENS_BY_URL lands on a results page LENS_RESULT_MARKERS already matches.
        # The cover gets Lens' own longer deadline because this is the same wait.
        #
        # The words, if any, are applied when the results land — the same hand-
        # off _track_lens_page() performs for an upload. Right-clicking a
        # picture carries none, which is why this used to be a bare "".
        self.lens_text = (text or "").strip()
        try:
            picture_name = Path(urllib.parse.urlparse(uri).path).name
        except Exception:
            picture_name = ""
        self._remember_image_search(self.lens_text, name=picture_name,
                                    path=None, uri=uri)
        # Nothing is uploaded on this route, so nothing has put the picture in
        # front of the user either. Fetched separately and only for the chip —
        # if it never arrives the search is unaffected. Clears _lens_shown_for
        # on the way in, which is why the landing below sets it again.
        self._attach_from_uri(uri)
        self._awaiting_lens_url_until = (GLib.get_monotonic_time()
                                         + self.LENS_BY_URL_WINDOW_MS * 1000)
        self._set_busy(True)
        self._show_loading("Searching this image with Google…",
                           cap_ms=self.COVER_CAP_LENS_MS)
        self._navigate(LENS_BY_URL + urllib.parse.quote(uri, safe=""))

    @staticmethod
    def _decode_data_uri(uri: str):
        """(bytes, mime) for a data: URI, or None if it is not one we can read."""
        if not uri.startswith("data:"):
            return None
        head, sep, payload = uri.partition(",")
        if not sep or not payload:
            return None
        head = head[5:]
        mime = head.split(";")[0].strip().lower() or "text/plain"
        try:
            if ";base64" in head.lower():
                raw = base64.b64decode(payload, validate=False)
            else:
                raw = urllib.parse.unquote_to_bytes(payload)
        except Exception:
            return None
        return (raw, mime) if raw else None

    def _data_uri_to_file(self, uri: str) -> str | None:
        """Write a data: image out, so the Lens upload can take it from there."""
        decoded = self._decode_data_uri(uri)
        if decoded is None:
            return None
        raw, mime = decoded
        # Where _lens_texture already puts loose pixels, under a fixed name for
        # the same reason it uses one: the next right-click overwrites this
        # instead of leaving a directory that only ever grows.
        try:
            _own_dir(DATA_DIR)
            path = DATA_DIR / ("context" + DATA_URI_EXTS.get(mime, ".png"))
            path.write_bytes(raw)
        except Exception:
            return None
        return str(path)

    def _suggested_name(self, uri: str) -> str:
        """What to open the save dialog on."""
        decoded = self._decode_data_uri(uri)
        if decoded is not None:
            return "image" + DATA_URI_EXTS.get(decoded[1], ".png")
        try:
            name = urllib.parse.unquote(
                Path(urllib.parse.urlparse(uri).path).name).strip()
        except Exception:
            name = ""
        # An address ending in a slash, or in a script with no name of its own.
        return name or "download"

    def _ask_save_path(self, suggested: str, chosen) -> None:
        """GNOME's save dialog, on the name whatever is being saved suggests.

        The one dialog both routes use — the menu’s "Save … as", and a download
        the page itself started — so there is one title, one place the default
        name comes from and one answer to a cancel. `chosen` is called exactly
        once, with an absolute filesystem path or with None.

        One turn late, on purpose. The menu this can be chosen from is a popover
        with a grab of its own, and a modal dialog opened while it is still up
        fights it for the keyboard — the same failure _dismiss_menu exists to
        prevent for the ⋯ menu. That one Halo owns and can pop down; this one
        belongs to WebKit, so we wait for it to go instead.
        """
        dialog = Gtk.FileDialog(title="Save from this page")
        dialog.set_initial_name(suggested or "download")

        def answered(dlg, result) -> None:
            try:
                gfile = dlg.save_finish(result)
            except GLib.Error:
                chosen(None)        # cancelled, which is not a failure
                return
            path = gfile.get_path() if gfile else None
            if not path:
                # Somewhere with no local path — a remote GVfs mount. WebKit
                # writes to a filesystem path and to nothing else.
                _notify(APP_NAME, "Halo can only save to this computer.")
                chosen(None)
                return
            chosen(path)

        GLib.idle_add(lambda: (dialog.save(self, None, answered), False)[-1])

    def _save_uri_as(self, uri: str) -> None:
        """Save something out of the page, through GNOME's own save dialog.

        The dialog comes first and the download second, which is the only order
        that works here. WebKit asks where to put a download from
        ::decide-destination, a handler that has to answer before it returns,
        and a file dialog cannot answer before it returns. Asking first means
        the destination is already known by the time WebKit wants it.

        A download the PAGE starts cannot be done in that order — the transfer
        already exists by the time Halo hears about it — so it goes the other
        way round; see _on_download_started.
        """
        if not uri:
            return
        self._ask_save_path(
            self._suggested_name(uri),
            lambda path: self._save_uri_to(uri, path) if path else None)

    def _download_to(self, uri: str, path: str, done) -> None:
        """Fetch through the page’s own session and write to an absolute path.

        Through WebKit rather than through GIO because this is the session that
        holds the page’s cookies: an image behind a login, or one a server only
        hands over for a referring page, is exactly the kind a right-click lands
        on. `done` is called once, on the main loop, with True or False.
        """
        if self.web is None:
            done(False)
            return
        # download-started fires from inside download_uri(), before it returns,
        # and it fires for this one too. The counter is what tells Halo’s own
        # fetches from the page’s, which are the ones that need a dialog.
        self._own_download += 1
        try:
            download = self.web.download_uri(uri)
        finally:
            self._own_download -= 1
        # Held for the length of the transfer: nothing else refers to it, and a
        # collected WebKitDownload takes its handlers — and the file — with it.
        self._downloads.append(download)
        state = {"over": False}

        def settle(ok: bool) -> None:
            if state["over"]:
                return              # ::failed is emitted first, then ::finished
            state["over"] = True
            if download in self._downloads:
                self._downloads.remove(download)
            done(ok)

        def decide(dl, _suggested) -> bool:
            # An absolute filesystem path, NOT a URI. webkit_download_set_destination
            # asserts g_path_is_absolute() and abandons the download on a file:// one
            # — WebKit2GTK 4.x took a URI here and 6.0 does not, which is the kind of
            # difference that fails as a warning on stderr and a file that never
            # appears. The dialog has already asked about overwriting.
            dl.set_allow_overwrite(True)
            dl.set_destination(path)
            return True

        download.connect("decide-destination", decide)
        download.connect("failed", lambda dl, _e: settle(False))
        download.connect("finished", lambda dl: settle(True))

    def _save_uri_to(self, uri: str, path: str) -> None:
        """Fetch and write. A data: URI is already in hand; anything else is a
        download."""
        decoded = self._decode_data_uri(uri)
        if decoded is not None:
            try:
                Path(path).write_bytes(decoded[0])
            except OSError:
                _notify(APP_NAME, "Could not save that file.")
                return
            _notify(APP_NAME, f"Saved as {Path(path).name}.")
            return
        self._download_to(uri, path, lambda ok: _notify(
            APP_NAME, f"Saved as {Path(path).name}." if ok
            else "Could not save that file."))

    def _on_download_started(self, _session, download) -> None:
        """A download the page started, given the dialog Halo’s own saves get.

        A link with download=, a response marked Content-Disposition: attachment,
        a type WebKit will not render — measured, all three arrive here and
        nowhere else. A <a download> never even reaches ::decide-policy. Until
        now nothing was listening, so WebKit wrote them wherever its own default
        pointed, with no dialog, no notification and nothing on screen: "when
        pressing the download button on a website, there is no dialog for where
        to save the file to."

        The order has to be the opposite of _save_uri_as’s. ::decide-destination
        must answer before it returns and Gtk.FileDialog.save() answers long
        after, so the transfer is pointed at a scratch file straight away and
        moved to the chosen name once both it and the dialog are done — in
        whichever order that happens, which is why neither half acts alone.
        Asking first is not open to this route: the transfer already exists, and
        re-fetching the address would repeat a POST or spend a one-shot link.
        """
        if self._own_download:
            return              # _download_to made this one; it has a name
        self._downloads.append(download)
        job = {"tmp": None, "dest": None, "done": False, "ok": False,
               "closed": False}     # closed: settled, or given up on

        def forget() -> None:
            if download in self._downloads:
                self._downloads.remove(download)

        def scrub() -> None:
            if job["tmp"]:
                try:
                    os.unlink(job["tmp"])
                except OSError:
                    pass
                job["tmp"] = None

        def settle() -> None:
            # Both halves have to be in. The dialog can be answered long after a
            # small file has finished, and a large one can still be arriving
            # when it is — so whichever lands second is the one that moves it.
            if job["closed"] or not job["done"] or job["dest"] is None:
                return
            job["closed"] = True
            dest = job["dest"]
            moved = False
            if job["ok"] and job["tmp"]:
                try:
                    shutil.move(job["tmp"], dest)   # across filesystems too
                    job["tmp"] = None
                    moved = True
                except (OSError, shutil.Error):
                    moved = False
            scrub()
            _notify(APP_NAME, f"Saved as {Path(dest).name}." if moved
                    else "Could not save that file.")

        def picked(path) -> None:
            if path is None:
                # Nothing was chosen, so nothing is kept. The scratch file is
                # only removed once the transfer is actually over, because
                # ::cancel is a request and WebKit goes on writing until it
                # answers it — deleting the file under a live transfer would
                # simply leave a second one behind.
                job["closed"] = True
                try:
                    download.cancel()
                except Exception:
                    pass
                if job["done"]:
                    scrub()     # already over, so nothing else will come
                return
            job["dest"] = path
            settle()

        def decide(dl, suggested) -> bool:
            # The first point at which the name the server suggested is known,
            # so the first point at which the dialog can open on it.
            name = Path(suggested or "").name or self._suggested_name(
                dl.get_request().get_uri() or "")
            try:
                _own_dir(DATA_DIR)
                incoming = DATA_DIR / "incoming"
                _own_dir(incoming)
                job["tmp"] = str(incoming / ("%d-%d" % (os.getpid(), id(dl))))
            except OSError:
                return False        # nowhere to put it; WebKit keeps its default
            dl.set_allow_overwrite(True)
            dl.set_destination(job["tmp"])
            self._ask_save_path(name, picked)
            return True

        def failed(dl, _error) -> None:
            job["done"] = True
            job["ok"] = False
            forget()
            if job["closed"]:
                scrub()             # dismissed dialog; say nothing about it
                return
            settle()

        def finished(dl) -> None:
            forget()
            if job["done"]:
                return              # ::failed is emitted first, then ::finished
            job["done"] = True
            job["ok"] = True
            if job["closed"]:
                scrub()
                return
            settle()

        download.connect("decide-destination", decide)
        download.connect("failed", failed)
        download.connect("finished", finished)

    # ── copying a picture out of the page ───────────────────────────────
    def _copy_image(self, uri: str) -> None:
        """Put the picture itself on the clipboard, not its address.

        WebKit’s own COPY_IMAGE_TO_CLIPBOARD writes a SelectionData holding the
        URL, the surrounding markup and — only if it can build one — a GdkPixbuf
        of the decoded image (Pasteboard::write(PasteboardImage&), then
        Clipboard::write). When it cannot, and a picture the renderer is not
        holding as a plain decoded bitmap is exactly when it cannot, the pixbuf
        is simply left out and the offer is the text flavours alone. That is the
        report: "it copies as a text string instead of an image."

        So Halo fetches the bytes itself and offers a real GdkTexture, with the
        address beside it as text. Image first, so anything that can take pixels
        takes pixels and anything that can only take words still gets the
        address — which is also what every browser puts there.
        """
        if not uri:
            return
        decoded = self._decode_data_uri(uri)
        if decoded is not None:
            self._offer_image(decoded[0], uri)
            return
        if _uri_scheme(uri) not in ("http", "https"):
            # blob: has no bytes anything outside the page can read, and there
            # is no round trip a clipboard write can wait for.
            self._offer_image(b"", uri)
            return
        try:
            _own_dir(DATA_DIR)
        except OSError:
            self._offer_image(b"", uri)
            return
        # One fixed name, for the reason _lens_texture uses one: the next copy
        # overwrites this instead of leaving a directory that only ever grows.
        path = DATA_DIR / "copied-image"

        def done(ok: bool) -> None:
            raw = b""
            if ok:
                try:
                    raw = path.read_bytes()
                except OSError:
                    raw = b""
            self._offer_image(raw, uri)

        self._download_to(uri, str(path), done)

    def _offer_image(self, raw: bytes, uri: str) -> None:
        """One clipboard offer: the pixels, then the address."""
        texture = None
        if raw:
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
            except (GLib.Error, TypeError, AttributeError):
                texture = None
        providers = []
        if texture is not None:
            # Typed as GdkTexture and not as whatever concrete texture came
            # back: new_for_value takes the value's exact type, and a provider
            # advertising GdkMemoryTexture matches nothing that asks for a
            # GdkTexture. Measured — with the value typed, the clipboard offers
            # GdkTexture and image/png and the picture reads back; without it,
            # it offers GdkMemoryTexture and nothing takes it.
            providers.append(Gdk.ContentProvider.new_for_value(
                GObject.Value(Gdk.Texture, texture)))
        if uri:
            providers.append(Gdk.ContentProvider.new_for_value(
                GObject.Value(str, uri)))
        if not providers:
            return
        if texture is None:
            _notify(APP_NAME, "Could not copy that image; copied its address.")
        provider = (providers[0] if len(providers) == 1
                    else Gdk.ContentProvider.new_union(providers))
        self.get_clipboard().set_content(provider)

    # ── image sources ───────────────────────────────────────────────────
    def on_circle_to_search(self, *_a) -> None:
        """Hide, let GNOME capture a region, then Lens-search it."""
        # Grab any words now: they refine the visual search once it lands.
        seed = self.entry.get_text().strip()
        # And where the pill is standing, because hiding it is about to lose
        # that. This is not hide_popup(), so nothing else records it, and the
        # window is unmapped either way: mutter places it afresh on the way
        # back and whatever it was told before the hide is gone. See
        # _resume_placement().
        xid = self._xid()
        self._resume_at = WM.get_position(xid) if xid else None
        self.set_visible(False)

        def done(path: str | None) -> None:
            self.show_popup(reposition=False)
            if path:
                self.load_lens(path, text=seed)

        GLib.timeout_add(180, lambda: (capture_region(
            lambda p: GLib.idle_add(done, p)), False)[1])

    def on_pick_image(self, *_a) -> None:
        dialog = Gtk.FileDialog(title="Search an image with Google Lens")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        images = Gtk.FileFilter()
        images.set_name("Images")
        for mime in IMAGE_MIMES:
            images.add_mime_type(mime)
        filters.append(images)
        dialog.set_filters(filters)

        def chosen(dlg, result) -> None:
            try:
                gfile = dlg.open_finish(result)
            except GLib.Error:
                return
            if gfile and gfile.get_path():
                self.load_lens(gfile.get_path())

        dialog.open(self, None, chosen)

    def _paste_and_search(self) -> None:
        """Ctrl+Shift+V: whatever is on the clipboard, searched straight away."""
        try:
            clipboard = Gdk.Display.get_default().get_clipboard()
        except Exception:
            return

        def got(cb, result) -> None:
            try:
                text = cb.read_text_finish(result)
            except Exception:
                return
            if text:
                self._search_text(text)

        clipboard.read_text_async(None, got)

    def _search_primary_selection(self) -> None:
        """Middle-click on the pill: search whatever is selected anywhere.

        The primary selection is the X11 convention — select text in any window,
        middle-click, and it is pasted. Pasting it into the field and stopping
        there would be the same number of gestures as typing it, so this searches.
        """
        try:
            clipboard = Gdk.Display.get_default().get_primary_clipboard()
        except Exception:
            return

        def got(cb, result) -> None:
            try:
                text = cb.read_text_finish(result)
            except Exception:
                return
            if text:
                self._search_text(text)

        clipboard.read_text_async(None, got)

    def _search_page_selection(self) -> None:
        """Middle-click inside the page: search what is selected in it."""
        if self.web is None:
            return

        def got(web, result, _data) -> None:
            try:
                value = web.evaluate_javascript_finish(result)
                text = value.to_string() if value else ""
            except Exception:
                return
            if text and text.strip():
                self._search_text(text)

        try:
            self.web.evaluate_javascript(SELECTION_JS, -1, None, None, None,
                                         got, None)
        except Exception:
            pass

    def _try_clipboard_image(self) -> bool:
        """Ctrl+V with a picture in the clipboard becomes a Lens search."""
        clipboard = self.get_clipboard()
        formats = clipboard.get_formats()
        if not formats.contain_gtype(Gdk.Texture.__gtype__):
            return False
        # Text wins. Copying out of a rich page — including Halo's own results,
        # which is a documented reason the popup does not dismiss on focus loss —
        # offers the selection as an image *as well as* text. Hijacking that into
        # a reverse-image search threw the paste away and searched a screenshot
        # of the words instead. Only an image-only clipboard means "search this".
        for mime in ("text/plain;charset=utf-8", "text/plain", "text/uri-list"):
            if formats.contain_mime_type(mime):
                return False

        def got(cb, result) -> None:
            try:
                texture = cb.read_texture_finish(result)
            except GLib.Error:
                return
            if texture:
                self._lens_texture(texture, "clipboard.png")

        clipboard.read_texture_async(None, got)
        return True

    def _drop_image(self, value) -> bool:
        """The picture half of a drop. True if `value` was one and was taken.

        Shared by the pill and the search field, which is the point of it
        existing: the two disagree about what a drop of *words* means — a
        search on the pill, an insertion in the field — and agree completely
        about what a drop of a picture means. Splitting it here is what keeps
        the second from drifting away from the first, which is how the field
        came to have no image handling at all.

        False for anything that is not a picture, including a local file that
        is not one. The caller decides what that means where it landed.
        """
        if isinstance(value, Gdk.Texture):
            return self._lens_texture(value, "dropped.png")
        if not isinstance(value, Gio.File):
            return False
        try:
            path, uri = value.get_path(), value.get_uri() or ""
        except Exception:
            return False
        if path:
            if path.lower().endswith(IMAGE_EXTS):
                self.load_lens(path)
                return True
            return False
        if uri and _uri_names_an_image(uri):
            # Google fetches the picture itself from a public address — no
            # upload, and the same route the right-click search takes.
            if not self._ensure_webview():
                _notify(APP_NAME, "Could not start the web view.")
                return True
            self._search_image(uri)
            # After the navigation, as load_lens does it: _navigate() clears
            # the parked page, so expanding cannot pull it back over Lens.
            self.expand()
            return True
        return False

    def _on_drop(self, _target, value, _x, _y) -> bool:
        if isinstance(value, str):
            return self._search_text(value)
        if self._drop_image(value):
            return True
        if not isinstance(value, Gio.File):
            return False
        try:
            uri = value.get_uri() or ""
        except Exception:
            uri = ""
        # A GFile with no local path is not a file at all: it is an address
        # that GTK turned into a GFile because text/uri-list is the one
        # flavour every drag agrees on. A link dragged out of a page arrives
        # here, and so does a picture from anything that publishes only its
        # URL. Both used to be declined in silence, which is what a drag that
        # "does nothing" is. The picture is _drop_image()'s above; this is
        # what is left, which is the link.
        if not uri:
            return False
        if _uri_scheme(uri) in ("http", "https"):
            # looks_like_url() recognises it, so this navigates rather than
            # searching for the address as words.
            return self._search_text(uri)
        # A local file that is not a picture lands here too, as file:// — and a
        # gvfs mount (sftp:, mtp:, dav:) has bytes, but only behind a read that
        # would block the main loop for as long as the network takes. Both
        # declined, as they always were.
        return False

    def _on_entry_drop(self, _target, value, _x, _y) -> bool:
        """A drop onto the search field. See _wire_entry_drop() for why.

        A picture is a search, exactly as it is on the rest of the pill.
        Anything else is words, and words dropped into a text field belong in
        the text field — that is what GtkText did with them before this took
        the drop over, and it is still what a hand dragging a phrase into a
        search box means. The one thing that never happens again is a picture
        being spelled out as its own address.
        """
        if self._drop_image(value):
            return True
        text = value if isinstance(value, str) else ""
        if not text and isinstance(value, Gio.File):
            # A non-image file, or a mount with no path. Its name is the only
            # useful thing about it here, and dropping it is at least a
            # deliberate act — so it goes in as words rather than nowhere.
            try:
                text = value.get_path() or value.get_uri() or ""
            except Exception:
                text = ""
        if not text:
            return False
        self._insert_in_entry(text)
        return True

    def _insert_in_entry(self, text: str) -> None:
        """Put dropped words at the caret, replacing any selection.

        Flattened first: a query is one line by definition, and a paragraph
        dragged out of a page arrives with its newlines in it. QUERY_LIMIT is
        deliberately not applied — this is not a search yet, it is text in a
        field that the user is still free to edit, and truncating what they
        can see is worse than a long line they can trim.
        """
        flat = " ".join((text or "").split())
        if not flat:
            return
        bounds = self.entry.get_selection_bounds()      # () when collapsed
        if bounds:
            self.entry.delete_text(bounds[0], bounds[1])
            at = bounds[0]
        else:
            at = self.entry.get_position()
        # insert_text() returns where the text now ends but does NOT move the
        # caret there — measured. Leaving it behind would put the next typed
        # character in front of what was just dropped.
        self.entry.set_position(self.entry.insert_text(flat, at))
        self.set_focus(self.entry)
        self.entry.grab_focus()

    # A dropped paragraph or a middle-clicked sentence can be any length and can
    # carry newlines; a query cannot. Collapsed to single spaces and cut to the
    # length Google itself stops reading at.
    QUERY_LIMIT = 400

    def _search_text(self, text: str) -> bool:
        """Run loose text — dropped, pasted, or selected — as a search.

        Every caller of this is a command that arrived from OUTSIDE the search
        field and named its own words: a phrase dropped on the pill, a
        middle-clicked selection, Ctrl+Shift+V, "Search Google for …" in the
        right-click menu, a dropped link. None of them mean "add these words to
        the picture" — they mean "search this, now" — so an attached picture is
        taken off rather than silently turned into the thing being searched.

        The field is where refining happens, and it is where the chip is: type
        in it and Enter refines the picture, because the picture is sitting
        right there beside what is being typed. Reach in from anywhere else and
        the picture goes. Caught by the suite, which found a selected phrase
        running as a visual search because the check before it had attached one.
        """
        self.detach_image()
        flat = " ".join((text or "").split())
        # QUERY_LIMIT is a limit on a QUERY: it is where Google stops reading.
        # An address is not read, it is navigated to whole, so cutting one at
        # four hundred characters navigates to the first four hundred — and a
        # dropped link comes through here now. Anything with a space in it is
        # not an address and is still cut; looks_like_url() decides, exactly as
        # run_search() does a moment later.
        cleaned = flat if looks_like_url(flat) else flat[:self.QUERY_LIMIT]
        if not cleaned:
            return False
        self._show_suggestions([], animate=False)
        self.close_history(animate=False)
        self._set_entry_text(cleaned)
        self.run_search()
        return True

    def _lens_texture(self, texture: Gdk.Texture, name: str) -> bool:
        """Put loose pixels on disk, then hand the file to Lens."""
        try:
            _own_dir(DATA_DIR)
            path = DATA_DIR / name
            texture.save_to_png(str(path))
        except Exception:
            return False
        self.load_lens(str(path))
        return True

    # ── webview callbacks ───────────────────────────────────────────────
    def _on_lens_results(self) -> bool:
        """Is the view actually showing Lens' results, rather than its uploader?"""
        uri = (self.web.get_uri() or "") if self.web is not None else ""
        return _is_lens_result(uri)

    def _on_load_changed(self, web, event) -> None:
        if event == WebKit.LoadEvent.STARTED:
            # Links open in the panel now, so a load is no longer always one the
            # pill started and already turned the spinner on for. Without this a
            # click on a slow site looked like a click that did nothing.
            self._set_busy(True)
        if event == WebKit.LoadEvent.REDIRECTED:
            # Follow the load we are waiting for as it moves, so that if it does
            # fail we still recognise the failure as belonging to it.
            self._nav_uri = web.get_uri() or self._nav_uri
        if event == WebKit.LoadEvent.COMMITTED:
            # The moment the new document becomes what the view is painting. Up to
            # here it was still showing the page before it, and the cover had to
            # stay up whatever else arrived — see _navigate().
            self._nav_pending = False
            self._page_at_top = True        # a new document opens at its top
            self._page_selection = ""       # ...and with nothing selected in it
            # ...but not yet what is on screen. The second wait starts here and
            # the cover stays up through it — see _view_stale().
            self._arm_paint_wait()
            # Google's structure is only on Google's pages, and links open here
            # now, so the rules written against it follow the page.
            google = _is_google_page(web.get_uri() or "")
            if google != self._styles_google:
                self._apply_user_styles(google)
            # Which half of a corrected pair the view is on now — or neither, in
            # which case the pair is forgotten. Here rather than in
            # _on_uri_changed because notify::uri also fires for a provisional
            # load, and one that is abandoned must not take the step back with it.
            self._track_correction(web.get_uri() or "")
        if event == WebKit.LoadEvent.FINISHED:
            # A Lens hand-off is still in flight even though this page loaded,
            # so keep the bar spinner going until results actually arrive. So is a
            # load we asked for that has not committed: this FINISHED belongs to
            # the page being navigated away from, not to the one being waited for,
            # and acting on it stopped the spinner and lifted the cover off the
            # previous search — which is precisely what the user then saw.
            if not self.pending_lens and not self._nav_pending:
                self._set_busy(False)
                # While the hand-off is mid-flight only the results page may lift
                # the cover. _finish_lens() clears pending_lens the instant the
                # results URL appears, and the uploader we are navigating away
                # from finishes its own load a moment later — which used to land
                # here and uncover Google's upload page for a few frames. It only
                # showed when that FINISHED beat the new page's first progress
                # report, which is why it appeared perhaps one search in three.
                if not self._lens_await_load or self._on_lens_results():
                    self._lens_await_load = False
                    # Loaded is not the same as visible. If the frame report has
                    # not arrived yet the cover stays, and _recheck_cover lifts it
                    # the moment it does — everything else about FINISHED still
                    # happens now, or a page that never reports would leave the
                    # bar spinning and the panel the wrong colour.
                    if not self._await_paint:
                        self._hide_loading()
                    else:
                        # Loaded, but not yet seen to paint. Give the report the
                        # moment it needs; if it never comes at all, the cover
                        # still comes off — see _paint_grace().
                        self._paint_grace()
                # Match our own surfaces to whatever shade the page settled on.
                self._adopt_page_bg()
            # Let the freshly loaded uploader settle, then click it once.
            if self.pending_lens:
                self._arm_lens_click()
        self._update_nav_buttons()

    def _on_load_failed(self, _web, _event, uri: str, _error) -> bool:
        """Nothing will commit for this one, so stop waiting on it.

        Only if it is the load actually being waited for, though — and measured,
        most failures are not. Navigating away cancels whatever the outgoing page
        still had in flight, and WebKit reports that cancellation here: asking for
        a new page while the old one was still fetching an image produced a
        load-failed for the *old* URI a tenth of a second later. Clearing the flag
        on that lifted the cover onto the previous search for the whole 1.2s the
        new page took to arrive, which is the very flash all of this exists to
        prevent — measured against a local server, so it was not subtle.

        False, not True: WebKit goes on to show its own error page, which is more
        use than a blank panel. load-changed FINISHED follows either way, and with
        the flag cleared that is what lifts the cover.
        """
        if urllib.parse.urldefrag(uri or "")[0] == urllib.parse.urldefrag(
                self._nav_uri)[0]:
            self._nav_pending = False
            # Nothing of ours will paint either; WebKit's error page will commit
            # in a moment and arm its own wait.
            self._cancel_paint_wait()
        return False

    def _on_load_progress(self, web, _p) -> None:
        """Uncover as soon as the page is readable, not when the last beacon lands.

        load-changed FINISHED waits for every image, font, ad frame and analytics
        beacon on the page. Google's results themselves are server-rendered and
        painted long before that, so keying the cover to FINISHED left it sitting
        on top of results the user could otherwise already be reading — seconds of
        it on a page with many thumbnails.
        """
        if self.pending_lens or not self.loading.get_visible():
            return
        if self._view_stale():
            # The page on screen is still the old one — either the new document
            # has not committed, or it has committed but has not painted. Either
            # way this reading belongs to the outgoing page, and uncovering on it
            # is the flash: the general case of the Lens race handled below.
            return
        try:
            progress = web.get_estimated_load_progress()
        except Exception:
            return
        if self._lens_await_load:
            # Just landed on Lens' results URL. The outgoing uploader is still
            # sitting at 1.0, and uncovering on that reading is precisely how it
            # got shown. Wait for the reading to drop, which only happens once
            # the incoming page has actually begun loading.
            if progress < 0.4:
                self._lens_await_load = False
            return
        if progress >= self.UNCOVER_PROGRESS:
            self._hide_loading()

    def _on_uri_changed(self, web, _p) -> None:
        uri = web.get_uri() or ""
        pretty = uri
        try:
            parsed = urllib.parse.urlparse(uri)
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            pretty = f"{parsed.netloc}  ·  {query}" if query else parsed.netloc + parsed.path
        except Exception:
            pass
        self.url_label.set_text(one_line(pretty))
        self._sync_query_field(uri)
        # Arriving at the visual search, moving within it, or leaving it —
        # all three, because leaving it is a state too and nothing used to
        # notice. See _track_lens_page().
        self._track_lens_page(uri)
        if "/sorry/" in uri:
            self.url_label.set_text("Google is asking for a CAPTCHA — solve it once "
                                    "and it will stick")


    def _sync_query_field(self, uri: str) -> None:
        """Follow the page's own query back into the search field.

        Clicking Google's "Did you mean" (or "Ergebnisse für") searches
        something the field never said, and the pill then sat there insisting on
        the misspelling that had already been corrected on screen.

        Driven from the address on *every* navigation rather than from the
        correction link in particular, which is what makes Back free: Back moves
        the address, the address moves the field, and the old query comes back
        with the old page. A parallel stack of queries would have to be kept in
        step with WebKit's own history — including the entries a released engine
        takes with it — and this has nothing to keep in step with anything.
        """
        query = google_query(uri)
        if query is None:
            return                  # not a search of ours to follow
        self._adopt_query(query)

    def _adopt_query(self, query: str) -> None:
        """Write a query the page is running into the field, if the field is free.

        Split out of _sync_query_field because the address is no longer the only
        thing that knows what the page ran: Google's silent correction leaves q=
        holding the misspelling, so SPELL_JS reads the corrected words out of the
        page instead and arrives here too. One list of the things that must not
        be written over, rather than two that drift apart.
        """
        if self.finding or self.history_open:
            # The field is on loan: to a find needle, which leave_find() puts
            # back from _find_query, or to the history list's filter. Writing
            # over either destroys what the user is in the middle of.
            return
        if self.pending_lens or self.lens_text:
            return                  # a visual search is in flight; the field is its caption
        text = self.entry.get_text().strip()
        if self._typing_in_entry() and text and text != self.last_query:
            # They are writing the next query while this page is still arriving,
            # and their text wins. Note what does *not* count as typing: the
            # caret is still in the field for a second after Enter, so guarding
            # on _typing_in_entry() alone would refuse the correction in exactly
            # the case this exists for. Empty, or still holding last_query, is
            # the same "not being typed in" that ↓ uses on the collapsed pill.
            return
        # In step, or ↓ would refuse to bring this very page back: its rule is
        # that the field must be empty or match last_query, and the field is
        # about to stop matching.
        self.last_query = query
        if text == query:
            return
        # notify=False: this is not typing. Letting it look like typing fetches
        # a suggestion list for a query already on screen, and that list then
        # owns ↓ — the same trap leave_find() documents.
        self._set_entry_text(query, notify=False)

    def _adopt_spelling(self, payload: str) -> None:
        """SPELL_JS reporting words the page ran that the address does not carry.

        The rest of this is about making sure the report still belongs to the
        page on screen. It is the one query that arrives out of band — every
        other one comes from notify::uri and is therefore true by construction —
        so a report from a search the user has already left has to be dropped
        instead of written over the search they are on now.

        Compared as queries rather than as strings: Google rewrites its own
        address after the fact (an &sei= turns up on the first result of a
        session), and a page is still the same search when it does.
        """
        if self.web is None:
            return
        try:
            said = json.loads(payload)
            query = one_line(str(said.get("q", ""))).strip()
            seen = str(said.get("uri", ""))
        except Exception:
            return
        if not query:
            return
        current = self.web.get_uri() or ""
        here = google_query(current)
        if here is None or here != google_query(seen):
            return
        if query == here:
            return              # nothing was substituted after all
        if _no_correction(current):
            # The two halves of a corrected pair carry the same q=, so comparing
            # queries cannot tell them apart — and this half is the one that
            # asked Google not to correct. A report read off the corrected page
            # that lands just as the verbatim one commits would otherwise be
            # written straight over the words the user went there to see. This
            # is reachable by pressing Back and Forward quickly: the report is
            # posted from the page, and the navigation does not wait for it.
            return
        # Both queries are known now, which is the whole of what Back needs.
        self._spell_slot = {"typed": here, "fixed": query, "state": "corrected"}
        self._adopt_query(query)
        # The button has to come alive with the step, not one load event later.
        self._update_nav_buttons()

    def _nav_abandoned(self) -> None:
        """This load is not coming after all, so stop waiting for it.

        Ignoring a policy decision cancels the load, and _nav_pending is cleared
        by COMMITTED or by load-failed — neither of which is guaranteed to arrive
        for a decision we declined ourselves. Left set, it would hold the loading
        cover for its whole grace period and leave the bar's spinner turning for
        as long as the panel stayed open.
        """
        self._nav_pending = False
        self._nav_uri = ""
        self._cancel_paint_wait()
        self._set_busy(False)

    def _hand_to_browser(self, uri: str) -> None:
        """Give a URI to whatever the desktop has registered for it.

        Unlike Ctrl+Enter this does not hide Halo: the user did not ask to leave,
        they clicked something the panel cannot show, and closing the search they
        were reading as a side effect of that would be its own surprise.
        """
        if not uri or _uri_scheme(uri) in _UNLAUNCHABLE_SCHEMES:
            return
        try:
            Gtk.UriLauncher(uri=uri).launch(self, None, None)
        except Exception:
            pass

    def _on_decide_policy(self, _web, decision, decision_type) -> bool:
        """Follow links in the panel; hand out only what it cannot show.

        Every non-Google link used to be pushed to the real browser, which made a
        result page a dead end: one click and the popup being read vanished,
        taking its always-on-top panel with it, and coming back meant searching
        again. This is a browser engine with history, a reload and an address
        label — so it browses, and the two ways out stay deliberate (Ctrl+Enter,
        or the ⤢ button) instead of happening on their own.

        Two kinds of target genuinely have to leave. Schemes WebKit cannot load —
        mailto:, tel:, magnet:, an app's own handler — belong to another program.
        And a response it cannot render is a download: nothing here has a place to
        put a file or any UI to show one arriving, and silently doing nothing is
        how a click on a PDF looks broken.
        """
        if decision_type == WebKit.PolicyDecisionType.RESPONSE:
            # A response the server marked to keep rather than show is a
            # download whatever its type, and WebKit turns it into one by
            # itself — measured, for both a type it could render and one it
            # could not. Halo only has to stay out of the way, because handing
            # it to the browser instead would send the click somewhere else
            # entirely and leave ::download-started with nothing to catch.
            if self._is_attachment(decision):
                return False
            try:
                if decision.is_mime_type_supported():
                    return False
                uri = decision.get_request().get_uri() or ""
            except Exception:
                return False
            # Only the page the user asked for. Response decisions are made for
            # subresources too, and launching a browser for one of those would be
            # a window nobody asked for, over something they cannot even see.
            if not uri or not self._is_page_response(decision, uri):
                return False
            self._hand_to_browser(uri)
            self._nav_abandoned()
            decision.ignore()
            return True
        if decision_type != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        try:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
        except Exception:
            return False
        # Checked for every navigation type, not only a click: a page that
        # redirects itself to mailto: needs the same answer as a link to one.
        if uri and not _is_web_uri(uri):
            self._hand_to_browser(uri)
            self._nav_abandoned()
            decision.ignore()
            return True
        # Ctrl+click is the deliberate way out for one link, next to Ctrl+Enter
        # for the whole page: the panel stays exactly where it is and the link
        # opens in the real browser. Only a click, so a page redirecting itself
        # while Ctrl happens to be held is unaffected.
        try:
            action = decision.get_navigation_action()
            clicked = (action.get_navigation_type()
                       == WebKit.NavigationType.LINK_CLICKED)
            ctrl_held = bool(int(action.get_modifiers())
                             & int(Gdk.ModifierType.CONTROL_MASK))
        except Exception:
            clicked = ctrl_held = False
        if uri and clicked and ctrl_held:
            self._hand_to_browser(uri)
            self._nav_abandoned()
            decision.ignore()
            return True
        return False

    @staticmethod
    def _is_attachment(decision) -> bool:
        """Did the server say to keep this file rather than show it?

        Read off Content-Disposition, through whatever binding is there: the
        headers come back as a SoupMessageHeaders, and a build without the Soup
        typelib installed hands back something with no get_one() on it. An
        answer of "no" then only means the response is treated the way it always
        was, which is why nothing here raises.
        """
        try:
            headers = decision.get_response().get_http_headers()
            value = headers.get_one("Content-Disposition") or ""
        except Exception:
            return False
        return value.split(";")[0].strip().lower() == "attachment"

    def _is_page_response(self, decision, uri: str) -> bool:
        """Is this response the page itself, rather than something inside it?

        WebKit can answer directly, and does from 2.40 — but the binding is only
        present if the installed library has it, so fall back to comparing the
        response's URI with the one the view is loading. That comparison is sound
        because the view's uri is set at provisional load, which happens before
        any response arrives, and it follows server redirects. Fragments come off
        both sides first: a request carries none, while the address may.
        """
        try:
            return decision.is_main_frame_main_resource()
        except (AttributeError, TypeError):
            pass
        if self.web is None:
            return False
        try:
            here = urllib.parse.urldefrag(uri)[0]
            there = urllib.parse.urldefrag(self.web.get_uri() or "")[0]
        except Exception:
            return False
        return bool(here) and here == there

    def _current_uri(self) -> str | None:
        """The link the toolbar is talking about.

        The loaded page while the panel is open; otherwise whatever the pill would
        search for, so the toolbar's URL actions mean the same thing wherever they
        are used from.
        """
        uri = (self.web.get_uri()
               if self.expanded and self.web is not None else None)
        if not uri:
            text = self.entry.get_text().strip()
            uri = (looks_like_url(text) or search_url(text)) if text else None
        if not uri:
            # A bare pill with a page waiting behind it, which is the state the
            # round ↓ exists to advertise. Both of this function's callers used
            # to answer that with nothing at all: Ctrl+Shift+C copied nothing
            # and said nothing, and Ctrl+Enter opened nothing — while the disc
            # under the pill was pointing at the very page they are about to be
            # asked about. The field is empty for an ordinary reason (every
            # reopen clears it), so "there is no link here" was never true.
            #
            # The live page first and the parked address second, in that order,
            # because those are the two halves of _page_to_return_to() and this
            # has to agree with the disc about whether there is a page. http(s)
            # only: a view that has been built but never navigated reports
            # about:blank, which is not a link anybody meant to copy.
            live = (self.web.get_uri() or "") if self.web is not None else ""
            uri = (live if _uri_scheme(live) in ("http", "https")
                   else self._parked_uri)
        return uri or None

    COPIED_MS = 1400            # long enough to read, short enough not to linger

    def on_copy_url(self, *_a) -> None:
        """Put the current link on the clipboard, and say so on the button.

        The popup is always-on-top and dismisses itself on "open in browser", so
        the clipboard is how a result gets out of Halo and into anything else.
        Without any acknowledgement a copy button on a translucent popup is a
        coin toss — the chip goes green with a tick for a moment instead.
        """
        uri = self._current_uri()
        if not uri:
            return
        clipboard = self.get_clipboard()
        try:
            clipboard.set_content(
                Gdk.ContentProvider.new_for_value(GObject.Value(str, uri)))
        except Exception:
            try:
                clipboard.set(uri)      # PyGObject convenience, older bindings
            except Exception:
                return
        self.btn_copy.set_icon_name("object-select-symbolic")
        self.btn_copy.add_css_class("halo-copied")
        self.btn_copy.set_tooltip_text("Link copied")
        if self._copy_timer:
            GLib.source_remove(self._copy_timer)
        self._copy_timer = GLib.timeout_add(self.COPIED_MS, self._copy_settled)

    def _copy_settled(self) -> bool:
        self._copy_timer = 0
        self.btn_copy.set_icon_name("edit-copy-symbolic")
        self.btn_copy.remove_css_class("halo-copied")
        self.btn_copy.set_tooltip_text("Copy this link  ·  Ctrl+Shift+C")
        return False

    def on_open_external(self, *_a) -> None:
        uri = self._current_uri()
        if not uri:
            return
        Gtk.UriLauncher(uri=uri).launch(self, None, None)
        # "Open in my browser" says where the user wants to be, and Halo is
        # always-on-top: staying up means hovering over the page they just asked
        # for. Step aside — the results are still loaded, so ↓ on the empty pill
        # brings them straight back.
        self.hide_popup()

    # ── expand / collapse ───────────────────────────────────────────────
    # The chrome above the panel: the toolbar, its separator and the slab's own
    # 2px edge. Measured — a 260px panel makes a 357px window, of which
    # BAR_HEIGHT is 60. Was 41 while .halo-toolbar carried 5px of padding; the
    # row is 4px shorter now, so a 620px panel makes a 717px window.
    PANEL_CHROME = 37
    # Most of the usable height Halo will ever take. It is a popup: something has
    # to be left of the screen behind it, and on a short one that has to be a
    # share rather than a fixed margin.
    MAX_WINDOW_SHARE = 0.85

    def _anchor_point(self) -> tuple[int, int] | None:
        """The point that decides which monitor the panel is sized against.

        Our own position while we are on screen, the pointer otherwise. Sizing
        always from the pointer is wrong the moment the two disagree: leave the
        mouse on a tall display with the pill on a short one and the panel was
        capped for the screen it is not on, so it ran off the bottom of the
        screen it is. Everything else vertical already reads the window's own
        position (see _note_pill_y and _restore_pill_y) — this brings the height
        cap into line with them.
        """
        xid = self._xid()
        if xid is not None and self.get_visible():
            where = WM.get_position(xid)
            if where:
                return where
        return WM.pointer()

    def _expanded_height(self, win_h: int, at=None) -> int:
        """How tall the window stands with the results panel open, in device px."""
        panel = self._clamp_panel(int(CFG["panel_height"]), at)
        return max(win_h, (panel + BAR_HEIGHT + self.PANEL_CHROME) * self._scale())

    def _clamp_panel(self, height: int, at=None) -> int:
        """Keep the results panel inside the screen it will open on.

        A height saved on a large display would otherwise run off the bottom of a
        smaller one — easy to hit by changing resolution between sessions, and
        easier still by changing the display scale, because panel_height is in
        logical pixels and raising the scale shrinks the logical screen underneath
        a value saved at the old one.

        The reserve used to be a flat 150px, which is generous on a tall screen
        and nothing at all on a short one: measured, a saved 620 gave a window
        filling 90% of an 800px-logical screen and 93% of a 720px one — which is
        what "expands from the top to the bottom of the screen" is. Reserving a
        share instead holds the same 620 on a normal display and actually leaves
        room on a scaled one.
        """
        limit = 1400
        try:
            rect, ms = self._usable_pick(
                at if at is not None else self._anchor_point())
            usable = rect[3] // max(1, ms)
            limit = max(260, int(usable * self.MAX_WINDOW_SHARE)
                        - BAR_HEIGHT - self.PANEL_CHROME)
        except Exception:
            pass
        return max(260, min(limit, height))

    def _clamp_width(self, at=None) -> int:
        """Keep the pill inside the screen it will open on, in logical pixels.

        The twin of _clamp_panel(), and it was simply missing. A width is saved
        in logical pixels for exactly the reason a panel height is — see
        _remember_position() — so it goes wrong in exactly the same two ways,
        and the docstring above already names both: opening on a monitor
        smaller than the one the width was chosen on, and, the easier one to
        hit, raising the display scale, which shrinks the logical screen
        underneath a number that does not move. 1800 logical on a 1920 screen
        is still 1800 when 150% turns that screen into 1280.

        What it looks like is not a pill that is merely too wide.
        place_near_pointer() centres the window, so the overflow is split
        between the two sides and BOTH ends hang off the screen at once — the ⋯
        menu past one edge, the brand past the other, neither reachable, and no
        way to drag it anywhere that helps because the pill is wider than
        anywhere it could go.

        The saved width is deliberately not written back. It is the width the
        user chose, and choosing it on a big screen should survive a visit to a
        small one; only the width in force is narrowed — see
        _width_in_force(), which is what "in force" is read from.
        """
        width = int(CFG["window_width"])
        try:
            rect, ms = self._usable_pick(
                at if at is not None else self._anchor_point())
            # max() before min(), the same way _on_resize_update() already
            # does it: a screen narrower than the pill's own floor still
            # clamps, it just clamps to the floor. Skipping the clamp
            # altogether there left the saved width untouched — so the one
            # screen most in need of narrowing was the one that got none.
            width = min(width, max(self.MIN_WINDOW_W, rect[2] // max(1, ms)))
        except Exception:
            pass
        return max(self.MIN_WINDOW_W, width)

    def _width_in_force(self) -> int:
        """The width the window is actually wearing, in logical pixels.

        Which is not always the width that was chosen — that is
        CFG["window_width"], and _clamp_width() is why the two can differ.

        Read back off the window rather than kept in a field beside it. Every
        path that changes the width goes through set_default_size(), including
        ones that never touch the config at all, so a shadow copy is a second
        answer that can only ever be the wrong one. It was, immediately, and the
        way it showed is worth writing down: a resize drag takes its baseline
        from here, and a baseline left stale by one section of the suite sent a
        760px window to 940 for a 120px drag. The window is the only thing that
        knows, so ask it.
        """
        try:
            width = int(self.get_default_size()[0])
        except Exception:
            width = 0
        return width if width > 0 else int(CFG["window_width"])

    def _fit_width(self, at=None) -> None:
        """Wear the saved width, narrowed to today's screen. Idempotent.

        A no-op on every ordinary desktop — which is the point, the same way
        _sync_cursor_size() is a no-op on a desktop of one scale — so it costs
        a comparison on the summon path and nothing else.
        """
        want = self._clamp_width(at)
        if want != self._width_in_force():
            self.set_default_size(want, -1)

    # Growing the panel makes the window taller than the space below the pill, so
    # the window manager slides the whole window up so it still fits. That part is
    # right. What it does not do is slide back when the panel shrinks again, so the
    # pill ended up stranded at the top of the screen and stayed there: measured,
    # five Ctrl+↓ walked the window from y=582 to y=78, and eight Ctrl+↑ brought
    # the height all the way back down while y stayed at 78. Hence an anchor of our
    # own, restored after every size change.
    def _resize_panel(self, delta: int) -> None:
        # Nothing to resize without a view. Only reachable with the panel open
        # today, and the panel cannot be open without one — but this is the last
        # place in the file that dereferenced the lazily-built view unguarded, and
        # a released engine is one more way for it to be absent.
        if self.web is None:
            return
        current = int(CFG["panel_height"])
        height = self._clamp_panel(current + delta)
        if height == current:
            return                      # already at the limit; nothing to do
        self._note_pill_y()
        CFG["panel_height"] = height
        self.web.set_size_request(-1, height)
        self._track_geometry()
        # Once at the next frame or two (so the compositor has had its say) and
        # again when the geometry ticker finishes, which is idempotent.
        if self._anchor_timer:
            GLib.source_remove(self._anchor_timer)
        self._anchor_timer = GLib.timeout_add(120, self._restore_pill_y)

    def _note_pill_y(self) -> None:
        """Record where the pill sits, while that is still the user's own choice.

        Only while the window is not being squeezed by the bottom of the screen:
        once the window manager has pushed it up to make a tall panel fit, its y is
        the compositor's opinion rather than the user's, and adopting it would make
        the drift permanent.
        """
        xid = self._xid()
        size = self._frame_size_device() if xid else None
        if not xid or size is None:
            return
        where = WM.get_position(xid)
        if not where:
            return
        rect = self._usable_rect(where)
        if where[1] + size[1] < rect[1] + rect[3] - 2:
            self._pill_y = where[1]

    def _restore_pill_y(self, squeezed_only: bool = False) -> bool:
        """Put the pill back on its anchor, as far as the screen allows.

        squeezed_only is for the frame ticker. There is exactly one way the y can
        change without anybody here asking — the window manager sliding the whole
        window up so a growing drawer still fits — and it always moves it *up*.
        Restricting the ticker to that case lets the pill come back the frame
        after it was pushed, instead of after the settle, without the ticker also
        undoing a drag that happens to overlap a reveal.
        """
        if not squeezed_only:
            self._anchor_timer = 0
        if self._pill_y is None or not self.get_visible():
            return False
        xid = self._xid()
        size = self._frame_size_device() if xid else None
        if not xid or size is None:
            return False
        where = WM.get_position(xid)
        if not where:
            return False
        if squeezed_only and where[1] >= self._pill_y:
            return False
        rect = self._usable_rect(where)
        y = max(rect[1], min(self._pill_y, rect[1] + rect[3] - size[1]))
        if y != where[1]:
            WM.move(xid, where[0], int(y))
        return False

    def expand(self) -> None:
        if self.expanded:
            return
        if not self._ensure_webview():
            return
        # Wherever the pill sits right now is where the user wants it back, so
        # take the anchor from reality rather than from whatever was recorded
        # last. Only _resize_panel used to set it, so dragging the pill and then
        # opening the panel restored it to the place it was first put — measured,
        # a pill dragged to y=520 came back to 283. The Circle to Search path hit
        # the same thing from the other side: it re-shows without repositioning,
        # so the anchor was still the one from before the screenshot.
        self._note_pill_y()
        self._reopen_parked()
        self._clear_find_marks()
        self.close_history(animate=False)
        self.expanded = True
        self._arrow.hide()          # the panel is the way back now
        # Through the same door as every other close, so the rows and the
        # keyboard's idea of what is on screen go with it.
        self._show_suggestions([], animate=False)
        # Clamp on the way in too: the saved value may predate a screen change.
        CFG.data["panel_height"] = self._clamp_panel(int(CFG["panel_height"]))
        self.web.set_size_request(-1, int(CFG["panel_height"]))
        self._set_grip_visible(True)
        self.result_reveal.set_reveal_child(True)
        self._track_geometry()

    def collapse(self) -> None:
        if not self.expanded:
            return
        self.leave_find()       # no page to search once this is shut
        self.expanded = False
        # Animated only here: this is the moment the chip comes away from the
        # panel, and the one place the movement means anything. Asked for before
        # the fold starts, because with the instant reveal preset the revealer
        # finishes inside the next line and _on_result_revealed() runs before
        # anything after it would have got the chance to set this.
        self._arrow_slide_pending = True
        self._set_grip_visible(False)
        self.result_reveal.set_reveal_child(False)
        self._track_geometry()
        # Not shown here. The panel is still its full height for the length of
        # the fold, and the disc hangs off the bottom of the window: placing it
        # now put it 626px below where it belongs — off the foot of the work
        # area — and left it to crawl back up in 200px steps as the panel shut.
        # _on_result_revealed() shows it, and slides it, once there is room.
        self._update_arrow()
        self.focus_entry(select_all=True)

    # How long past the end of an animation the shape keeps being resampled.
    GEOM_SETTLE_MS = 220

    # How many frames after a size change the ticker keeps watching for the
    # window manager having slid the window up. Four rather than one: the move
    # can arrive a frame or two behind the resize that caused it, and four
    # frames is 53ms at 75Hz — far longer than mutter takes — while still
    # costing nothing on the frames where the size is standing still.
    SQUEEZE_WATCH_FRAMES = 4

    def _track_geometry(self) -> None:
        """Follow the window through a resize animation, one frame at a time.

        GTK4 gives no size-allocate signal on a window, so the X11 clip has to be
        resampled while the reveal animates. Doing that on a 16ms timeout meant it
        was never in step with anything: this display refreshes every 13.35ms, so
        a 16ms timer slides against vsync and the clipped edge lags the painted
        slab by an amount that changes every frame — which is what made the bottom
        edge shimmer as the panel opened. The frame clock ticks once per rendered
        frame by definition, so following it keeps the shape and the paint on the
        same cadence whatever the refresh rate happens to be.
        """
        # A deadline, not a frame count. This used to run for a fixed 32 frames,
        # which is 0.43s at 75Hz but only 0.22s at 144Hz and 0.13s at 240Hz —
        # shorter than the reveal itself, so on any display above about 114Hz the
        # ticker stopped resampling the X11 clip while the window was still
        # growing and left the panel clipped short of its real size. Configurable
        # reveal durations made it worse still: nothing about 32 frames knows that
        # the animation might last a second. An animation is measured in time, so
        # this follows time and is right on every monitor.
        self._geom_until = max(self._geom_until, GLib.get_monotonic_time()
                               + (max(self._reveal_ms(), self._drawer_ms())
                                  + self.GEOM_SETTLE_MS) * 1000)
        if self._geom_timer:
            return          # a tick callback is already running; it just got longer

        # Where the foot of the work area is, worked out once for the whole
        # animation. The tick only wants it to answer "could the window manager
        # have pushed us up?", and that question must not cost an X round trip on
        # every frame — see below.
        self._squeeze_floor = self._work_area_bottom()
        # A fresh burst has seen no size yet, so the first frame always looks.
        self._squeeze_seen = None
        self._squeeze_left = 0

        def tick(_widget, _clock) -> bool:
            self._sync_geometry()
            # Not only once it has all settled. A drawer that outgrows the room
            # under the pill makes the window manager slide the whole window up,
            # and waiting out the settle to slide it back means the pill visibly
            # rides up and drops again — the other half of "it resizes from the
            # top too". _cap_drawers() is what stops that happening at all; this
            # is what keeps it to a single frame when something gets past it.
            #
            # Guarded by arithmetic, because the check itself is a synchronous
            # X round trip and this runs on every frame of every animation.
            #
            # ── what that round trip actually costs ──
            # This file used to say 6.1ms, against a frame that is 33ms long,
            # and every guard around it was sized for that. Re-measured in
            # 2026-09 on a real mapped window on the live display, 200 calls
            # each, both while idle and during a live reveal:
            #
            #     WM.get_position()      0.083ms median, 0.409ms worst
            #     WM.workarea()          0.110ms median
            #     _usable_pick()         0.306ms median
            #     _cap_drawers()         0.374ms median
            #
            # So the true cost of one squeeze check is about 0.39ms — get_position
            # plus the _usable_rect() inside _restore_pill_y() — which is 6% of a
            # 144Hz frame rather than the 46% the old figure implied. The guards
            # are kept because less work is still less work and they cost one
            # comparison, but nobody should size a future decision off 6.1ms:
            # measure it again rather than trusting either number. The
            # window manager only ever moves us to make a window fit, so if the
            # window would still fit below the anchor there is nothing to undo
            # and nothing to ask the server about.
            # Without the apron, because placement is without the apron — see
            # _frame_size_device(), which says so at length. Counting it made
            # this fire apron_height() pixels early: 31 device px at 1x, 62 at
            # 2x. It then did nothing, because _restore_pill_y() has its own
            # guard — but only after paying the synchronous X round trip
            # measured two comments above, on every frame of every animation,
            # whenever the pill sits within an apron's height of the foot of the
            # work area. Which is where people park it.
            #
            # ...and gated on the size having MOVED, which is the other half of
            # the same economy. The arithmetic guard above asks "could the
            # window manager have pushed us up?", and the honest answer on a
            # pill parked low with the panel open is yes — for every frame of
            # the animation and for all 220ms of the settle after it, because
            # the guard is a statement about the anchor and the size and not
            # about where the window actually is. So once the pill was down
            # there, the guard stopped guarding: _restore_pill_y() paid the
            # synchronous round trip on every single frame, found nothing to do
            # on all but one of them, and paid a second one inside
            # _usable_rect() for the work area while it was there.
            #
            # The window manager only ever moves us to make a resize fit, so a
            # frame in which the size has not changed is a frame in which it had
            # no reason to. A few frames of grace after each change, because the
            # move can land a frame or two behind the size that caused it — and
            # then nothing, which is what takes the whole settle tail down to
            # no X traffic at all. The settle's own _restore_pill_y() below is
            # unconditional and is still the backstop for anything else.
            size = self._last_size
            if size != self._squeeze_seen:
                self._squeeze_seen = size
                self._squeeze_left = self.SQUEEZE_WATCH_FRAMES
            if (self._squeeze_left > 0 and self._pill_y is not None
                    and size is not None
                    and self._squeeze_floor is not None
                    and (self._pill_y + size[1] - self._arrow.apron_height()
                         > self._squeeze_floor)):
                self._squeeze_left -= 1
                self._restore_pill_y(squeezed_only=True)
            if GLib.get_monotonic_time() < self._geom_until:
                return GLib.SOURCE_CONTINUE
            self._geom_timer = 0
            # Settled: collapsing and expanding move the bottom edge too, so the
            # anchor is restored here as well as on an explicit resize.
            self._restore_pill_y()
            return GLib.SOURCE_REMOVE

        try:
            self._geom_timer = self.add_tick_callback(tick)
        except Exception:
            # No frame clock (unrealized, or a backend without one): the old
            # timeout is still better than never reshaping at all.
            self._geom_timer = GLib.timeout_add(
                16, lambda: tick(None, None) == GLib.SOURCE_CONTINUE)

    def _work_area_bottom(self) -> int | None:
        """The bottom of the usable screen, in device pixels, or None."""
        try:
            rect, _ms = self._usable_pick(self._anchor_point())
            return rect[1] + rect[3]
        except Exception:
            return None

    # Note that nothing here tries to keep a *growing* window on screen: the window
    # manager already does that. Moving a window mostly off-screen is honoured (so
    # dragging the pill where you like still works), but growing one past the work
    # area makes mutter reposition it to fit — verified by asking for a 10000px
    # panel with the pill 200px from the bottom edge, which came back flush with the
    # bottom rather than overflowing. All we add is the way back.

    # ── geometry: placement, on-top, click-through margin ───────────────
    def _xid(self) -> int | None:
        """Our X11 window id, cached per surface.

        PyGObject flags get_xid() as deprecated because GTK is retiring its X11
        backend, but nothing replaces it — an X11 window id is exactly what
        EWMH needs for always-on-top and placement. Silence that one warning
        rather than print it on every launch, and cache the result: this is
        called from the 16ms geometry ticker, so it runs often.

        The cache is keyed on the surface object so it heals itself if GTK ever
        re-realizes the window and hands out a new id.
        """
        if not ON_X11:
            return None
        try:
            surface = self.get_surface()
            if surface is None:
                return None
            if surface is not self._xid_surface:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    self._xid_cache = surface.get_xid()
                self._xid_surface = surface
                self._watch_surface(surface)
            return self._xid_cache
        except Exception:
            return None

    def _watch_surface(self, surface) -> None:
        """Reshape whenever the surface actually changes size.

        The frame ticker follows an animation smoothly, but it only runs for a
        fixed burst after something we already know about. Any resize landing
        outside one of those bursts left the X11 clip at the old height — and X
        clips to whatever it was last told, so the window came back cropped along
        the bottom with its lower part simply missing. The Lens path is full of
        exactly those late resizes: results replacing the uploader seconds after
        the panel opened, a retry reloading the page, the cover coming off.

        The surface knows precisely when it resized and says so, so make that the
        authority. The ticker then has nothing to guarantee — it only keeps the
        in-between frames of an animation smooth.
        """
        for prop in ("width", "height"):
            try:
                surface.connect(f"notify::{prop}", lambda *_a: self._sync_geometry())
            except Exception:
                pass
        # Deliberately NOT hooked to compute-size as well.
        #
        # That was tried, to get the clip down *before* the window shrank under
        # it, and it is the wrong way round. The compositor does not show a
        # resize until the frame that matches it has been drawn — so between the
        # two, what is on screen is still the old, taller frame, and a clip that
        # has already moved to the new height cuts the bottom off it. The rim
        # lives in that bottom 1.5px, so the whole animation ran with no colour
        # along its bottom edge. Measured against 1.7.3, which never did this:
        # its clip stood taller than the window for 7% of a collapse and shorter
        # for none of it, and that is the version that looked right. So the clip
        # follows the surface and is allowed to arrive late; what must not
        # happen is the window arriving somewhere in one jump, which is what
        # _shut_suggestions() is for.

    # ── the pointer's size, on a desktop of mixed scales ─────────────────
    # An X11 client gets one cursor size for the whole session: GDK reads the
    # XSettings key Gtk/CursorThemeSize, which GNOME sets from cursor-size times
    # the scale of the primary monitor. Where every monitor has the same scale
    # that is right everywhere. Where they differ — a 4K laptop panel at 200%
    # beside a 1080p screen at 100% — it is right on one of them and wrong on the
    # other by the ratio between the two, and Halo is the window it shows up on,
    # because Halo is the window that follows the pointer across the boundary.
    # Reported as the pointer becoming about four times the size (twice in each
    # direction) the moment it crosses onto the pill.
    #
    # GNOME's own pointer does not have this problem: it is drawn per monitor.
    # So do the same — work out what size this monitor wants and set the cursor
    # theme to it. Two things have to be known, and neither is available from
    # GDK, because on X11 every monitor reports the session's single scale
    # factor: what each output's real scale is, and how many X pixels one of its
    # device pixels is worth. mutter knows both and says so on the bus.
    #
    # When the answer matches what XSettings already said, nothing is called.
    # That covers the ordinary one-scale desktop, and it covers the case where
    # mutter is scaling the X framebuffer per monitor itself — in which case the
    # single size is already correct on every screen and interfering would break
    # what works.
    _MUTTER = ("org.gnome.Mutter.DisplayConfig",
               "/org/gnome/Mutter/DisplayConfig")
    _MUTTER_STATE = ("(ua((ssss)a(siiddada{sv})a{sv})"
                     "a(iiduba(ssss)a{sv})a{sv})")

    def _mutter_outputs(self) -> dict:
        """connector -> (scale, device_width, device_height), from GNOME itself.

        Cached until the monitor layout changes, because this is a D-Bus round
        trip and the popup is on a latency budget. A failure caches an empty
        answer too: on anything that is not GNOME there is nothing to ask, and
        asking again on every keypress would be the only cost.
        """
        if self._outputs_cache is not None:
            return self._outputs_cache
        outputs: dict = {}
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = bus.call_sync(
                self._MUTTER[0], self._MUTTER[1], self._MUTTER[0],
                "GetCurrentState", None, GLib.VariantType(self._MUTTER_STATE),
                Gio.DBusCallFlags.NONE, 400, None)
            _serial, monitors, logical, _props = reply.unpack()
            modes = {}
            for spec, mode_list, _mprops in monitors:
                for mode in mode_list:
                    if mode[6].get("is-current"):
                        modes[spec[0]] = (mode[1], mode[2])
                        break
            for _x, _y, scale, _transform, _primary, specs, _lprops in logical:
                for spec in specs:
                    size = modes.get(spec[0])
                    if size and scale > 0:
                        outputs[spec[0]] = (float(scale), size[0], size[1])
        except Exception:
            pass
        self._outputs_cache = outputs
        return outputs

    def _forget_outputs(self, *_a) -> None:
        self._outputs_cache = None
        self._cursor_px = None      # the layout changed; re-decide the size
        GLib.idle_add(self._sync_cursor_size)
        GLib.idle_add(self._refit_to_screen)

    def _refit_to_screen(self) -> bool:
        """Re-fit the pill AND the open panel to a screen that changed under them.

        The summon path fits the width already and expand() clamps the panel on
        the way in, so this is only for the case neither of them covers: the
        screen changing while the pill is already up. Turning 150% on in
        Settings shrinks the logical screen without the pill moving anywhere,
        and unplugging a monitor does the same to whatever is left.

        Not while a resize is in flight: the hand on the grip is setting the
        size, and a second opinion arriving mid-drag is the one thing that
        makes a drag unusable.
        """
        if self._resizing is None:
            self._fit_width()
            self._fit_panel()
        return False

    def _fit_panel(self) -> None:
        """Clamp an ALREADY OPEN results panel to the screen it is now on.

        _clamp_panel() is applied on the way into expand(), as a resize drag
        goes, and on Ctrl+↑/↓ — and between those three there is a gap with
        nothing in it: the panel that is already open when the screen under it
        changes. Drag the pill from a tall monitor to a short one with the
        results up, or raise the scale while they are up, and a height that was
        legal where it was set runs off the bottom of where it is now. The
        window manager then lifts the whole window to make it fit, which pins
        the pill to the top of the screen with the panel hanging off the foot —
        and _restore_pill_y() cannot put it back, because there is nowhere to
        put it back to.

        The dropped-position path already re-caps the *drawers* for exactly
        this reason ("otherwise the first list opened after a drag is sized for
        where the pill used to be"); the panel is the much bigger thing it was
        not doing it for.

        Note what this costs, because it is the same trade expand() already
        makes and it is worth saying out loud: the clamped height is written
        back, so a tall panel narrowed on a short screen does not grow again on
        the way back to the tall one. Fixing that means keeping the chosen
        height apart from the height in force, the way _clamp_width() and
        _width_in_force() do for the width — and there is no equivalent of
        get_default_size() to read the panel's back off, because the height in
        force lives on a WebView that may not exist. Left as it is rather than
        guessed at.
        """
        if not self.expanded or self.web is None:
            return
        want = self._clamp_panel(int(CFG["panel_height"]))
        if want == int(CFG["panel_height"]):
            return
        CFG.data["panel_height"] = want
        self.web.set_size_request(-1, want)
        self._track_geometry()

    def _watch_monitor_layout(self) -> None:
        """Notice a display change, including the ones items-changed misses.

        Plugging a screen in or out changes the monitor *list* and
        `items-changed` catches it. Changing a monitor's scale, or moving one,
        does not. Measured on mutter 50.4, nested headless with two virtual
        monitors, applying 2.0/1.0 then 1.0/1.0 then 2.0/1.0 then 1.5/1.0:
        `items-changed` fired zero times across all of it, while
        `notify::geometry` fired on every monitor whose X rectangle changed.

        That matters because `_outputs_cache` holds mutter's per-output scale,
        and that scale is the whole of the cursor arithmetic. Cached at 2.0 on a
        screen that is now 1.0, `_wanted_cursor_px()` asks for 48 where 24 is
        right, and keeps asking for the rest of the process's life. So follow
        each monitor's geometry and scale factor too, and re-follow the list
        whenever it changes so a newly plugged screen is covered as well.
        """
        try:
            monitors = Gdk.Display.get_default().get_monitors()
        except Exception:
            return
        try:
            monitors.connect("items-changed", self._monitor_list_changed)
        except Exception:
            return
        self._monitor_list_changed(monitors)

    def _monitor_list_changed(self, monitors, *_a) -> None:
        """Wire up any monitor not being followed yet, then re-decide."""
        try:
            count = monitors.get_n_items()
        except Exception:
            count = 0
        for i in range(count):
            monitor = monitors.get_item(i)
            if monitor is None or monitor in self._watched_monitors:
                continue
            self._watched_monitors.add(monitor)
            for key in ("notify::geometry", "notify::scale-factor"):
                try:
                    monitor.connect(key, self._forget_outputs)
                except Exception:
                    pass
        self._forget_outputs()

    def _cursor_theme_pushed(self, *_a) -> None:
        """GTK has just re-applied XSettings' cursor theme to the display.

        Whatever size was chosen for this monitor is gone, so stop claiming it
        is in force and work the answer out again from where GDK actually is.
        """
        self._cursor_live = None
        self._cursor_px = None
        GLib.idle_add(self._sync_cursor_size)

    def _cursor_monitor(self):
        """The GDK monitor the pill is on — where its pointer will be."""
        display = Gdk.Display.get_default()
        if display is None:
            return None
        surface = self.get_surface()
        if surface is not None:
            try:
                monitor = display.get_monitor_at_surface(surface)
                if monitor is not None:
                    return monitor
            except Exception:
                pass
        # Not mapped yet: the pointer is where we are about to open.
        point = WM.pointer()
        try:
            monitors = display.get_monitors()
            # Each monitor's own scale below, not the window's: on X11 they are
            # the same number today, and the per-monitor one is the one that
            # stays right if that ever stops being true.
            for i in range(monitors.get_n_items()):
                monitor = monitors.get_item(i)
                geo = monitor.get_geometry()
                ms = max(1, monitor.get_scale_factor())
                if point and (geo.x * ms <= point[0] < (geo.x + geo.width) * ms
                              and geo.y * ms <= point[1]
                              < (geo.y + geo.height) * ms):
                    return monitor
            return monitors.get_item(0) if monitors.get_n_items() else None
        except Exception:
            return None

    def _wanted_cursor_px(self) -> int | None:
        """The cursor size to hand GDK so the pointer matches GNOME here.

        In *logical* pixels, which is not what the arithmetic below works in and
        is the whole of a bug this used to have. GDK multiplies whatever it is
        given by the scale factor of the surface the cursor is drawn on, so a
        size worked out in device pixels — which is what a monitor scale and a
        framebuffer ratio naturally produce — is scaled a second time. Measured
        directly, with XFixesGetCursorImage reading the pointer actually on
        screen: at scale 2, set_cursor_theme(theme, 24) draws 48px, which is
        right, and set_cursor_theme(theme, 48) draws 96px, which is the pointer
        "about four times the size" it was reported as. Twice in each direction.

        It only ever fired on the scaled screen, and that is not a coincidence:
        on an unscaled one the device figure and the logical one are the same
        number, it matches what XSettings already said, and the call below is
        skipped entirely. So the fix ran on every desktop that did not need it
        and broke the one that did.
        """
        monitor = self._cursor_monitor()
        if monitor is None:
            return None
        try:
            connector = monitor.get_connector()
            scale, device_w, _device_h = self._mutter_outputs()[connector]
            geo = monitor.get_geometry()
            x_width = geo.width * max(1, monitor.get_scale_factor())
            if x_width <= 0 or device_w <= 0:
                return None
            # How many X pixels one device pixel of this output is worth. 1 when
            # the X framebuffer is unscaled; 1/scale when mutter is upscaling it.
            x_per_device = x_width / float(device_w)
            # get_int, not get_uint: the key's type is "i", and asking for a
            # uint32 fails the type assertion inside GVariant and quietly hands
            # back 0 — which then looks like "no answer" rather than a mistake.
            base = Gio.Settings.new("org.gnome.desktop.interface").get_int(
                "cursor-size")
            if not 8 <= base <= 256:
                return None
            want_device = round(base * scale * x_per_device)
            if not 8 <= want_device <= 512:
                return None
            # Back out the multiplication GDK is about to do. Its factor is the
            # surface's, and on X11 every surface carries the session's single
            # scale, so the monitor's own is the same number and is the one that
            # stays right if that ever stops being true.
            return max(1, round(want_device / max(1, monitor.get_scale_factor())))
        except Exception:
            return None

    def _sync_cursor_size(self) -> bool:
        """Make the pointer over Halo the size GNOME draws it everywhere else."""
        if not ON_X11:
            return False
        wanted = self._wanted_cursor_px()
        if wanted is None:
            return False
        display = Gdk.Display.get_default()
        settings = Gtk.Settings.get_default()
        try:
            current = int(settings.get_property("gtk-cursor-theme-size"))
            theme = settings.get_property("gtk-cursor-theme-name") or "Adwaita"
        except Exception:
            return False
        self._cursor_px = wanted
        # What GDK is holding right now. Until this has written to it that is
        # GtkSettings' figure, because GtkSettings is what pushed it there; from
        # then on it is whatever was handed over last, and GtkSettings knows
        # nothing about it. Both figures are logical: the property reads back
        # divided by the scale, which is the unit _wanted_cursor_px() answers in.
        #
        # Comparing against the property instead is a factor-of-two bug in its
        # own right, and it was the live one. With GNOME's cursor-size at 24 and
        # a 2x screen beside a 1x screen: on the 2x screen 24 is wanted, it
        # matches, nothing is called; dragged to the 1x screen 12 is wanted and
        # written; dragged back, 24 is wanted, it matches the property again and
        # the call is skipped — while GDK is still holding the 12. Half size on
        # the screen that needs the most, until something else happens to move
        # the number. The same skip in reverse leaves a 1x screen on the 2x
        # figure, which is the doubling that was reported.
        live = self._cursor_live if self._cursor_live is not None else (
            theme, current)
        if (theme, wanted) == live:
            # Already what GDK has. Leave it alone rather than re-asserting: on
            # an ordinary one-scale desktop this is every call, and the whole
            # point of the check is that such a desktop is never interfered with.
            return False
        try:
            with warnings.catch_warnings():
                # Deprecated for the same reason get_xid() is — GTK is retiring
                # its X11 backend — and, like get_xid(), nothing replaces it.
                warnings.simplefilter("ignore", DeprecationWarning)
                display.set_cursor_theme(theme, wanted)
            self._cursor_live = (theme, wanted)
        except Exception:
            pass
        return False

    def _scale(self) -> int:
        try:
            surface = self.get_surface()
            return max(1, surface.get_scale_factor()) if surface else 1
        except Exception:
            return 1

    def _window_size_device(self) -> tuple[int, int] | None:
        """Our size in device pixels, or None while it cannot be known.

        get_width()/get_height() are 0 until GTK has allocated the toplevel — and
        that is exactly the state show_popup() runs in, because the shape was
        being set from an idle callback fired before the first allocation. The old
        code filled the gap with the BAR_HEIGHT constant, which is 10 logical
        pixels shorter than the pill actually is, so X clipped the bottom edge off
        every reopen until the next resize happened to resync it.

        measure() knows the natural size without an allocation, so ask the widget
        rather than guess, and give up rather than invent a number.

        The surface is asked first *once the toplevel has been allocated*: the
        shape exists to clip the X window to its own bounds, so the window's real
        size is the answer by definition, and it is right even on a frame where
        the widget tree has not caught up with a resize already in flight.

        Before the first allocation it is the wrong answer, and confidently so. A
        surface that has been created but never configured reports 1x1 — a real
        pair of positive numbers, so the old test for one sailed straight past —
        and that is what the cold first summon of a session ran on: the pill was
        mapped one pixel tall and completely unshaped for ~35ms, then snapped to
        its full 60. That is the "cut off while summoning" report. get_width()
        being 0 is exactly the state "GTK has not allocated this yet", and a
        surface belonging to an unallocated toplevel has not been configured
        either, so fall through to measure() and ask the widget instead.
        """
        scale = self._scale()
        surface = self.get_surface()
        allocated = self.get_width() > 0 and self.get_height() > 0
        if surface is not None and allocated:
            try:
                sw, sh = surface.get_width(), surface.get_height()
                if sw > 0 and sh > 0:
                    return sw * scale, sh * scale
            except Exception:
                pass
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            try:
                if width <= 0:
                    # The default size, not the natural one. The pill's width is
                    # set from CFG["window_width"] and is deliberately wider than
                    # its contents ask for, so measure() answers ~380 against the
                    # 760 the window is actually mapped at — and a shape cut to
                    # 380 takes the right-hand half of the pill off. A clip may
                    # stand wider than the window it belongs to (X simply draws
                    # the whole window); it may never stand narrower.
                    width = self.get_default_size()[0]
                    if width <= 0:
                        width = self.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
                if height <= 0:
                    height = self.measure(Gtk.Orientation.VERTICAL, width or -1)[1]
            except Exception:
                return None
        if width <= 0 or height <= 0:
            return None
        return width * scale, height * scale

    def _pill_height_device(self) -> int:
        """The slab's own height in device pixels — the window without the apron.

        The window's height less the apron's, both taken from the same layout
        pass, so the two cannot be describing different frames. Asking the slab
        widget for its own height reads like the same thing and is not: the
        window is not only its child, and the 2px it keeps for itself went
        missing from every clip — which is the clip arriving *ahead* of the
        window, the one direction it may never go.
        """
        size = self._window_size_device()
        if size is None:
            return 0
        return max(1, size[1] - self._arrow.apron_height())

    def _frame_size_device(self) -> tuple[int, int] | None:
        """The window as far as *placement* is concerned: the slab, no apron.

        The apron is allowed to hang past the foot of the work area, exactly as
        the disc's own window was before it moved inside. Counting it would make
        the window 31px taller the moment a page was parked, and every rule that
        keeps the window on screen would then shove the pill up by 31px under
        the user's hand — for a strip that is empty three quarters of the time.
        """
        size = self._window_size_device()
        if size is None:
            return None
        return size[0], max(1, size[1] - self._arrow.apron_height())

    def _frame_extents_now(self) -> None:
        """Tell the window manager the apron is not part of the pill.

        The apron hangs below the painted edge and is always there, so without
        this every WM-side placement of this window is the apron's height too
        low: a drag toward the foot of the screen snaps the *apron's* bottom to
        the work area and leaves the pill visibly short of it. Measured at 31
        device px at scale 1, 62 at scale 2 — and the same on 1.24.0, so this
        is a standing defect rather than anything round 12 did.

        Declared rather than given up, because those rows are wanted: they are
        the resize band and the two corner nooks, which is the whole of what
        makes the bottom edge and the bottom corners grabbable. Frame extents
        do not touch the input region — X11WM.set_frame_extents() carries the
        measurement both ways.

        ── the margin is taken from the ALLOCATION, and it has to be ──
        A bottom margin taller than the window is not a placement that comes
        out a few pixels wrong. It leaves mutter holding a frame rectangle of
        NEGATIVE height, and a window with no frame to present is a window it
        stops presenting: the next eglSwapBuffers blocks in
        dri3_wait_for_event and never returns. Measured, with the margin taken
        from apron_height() alone: the first summon publishes 31 while the
        toplevel still carries GTK's unconfigured 1px surface, so the manager
        is handed a frame 30px tall in the negative, and the selftest wedged
        there three runs out of three (0 of 2 for the untouched file).

        `self.get_height()` is the figure that cannot do that. It is set from
        the ConfigureNotify the server itself sent, so a non-zero allocation is
        proof the server has already applied that size — where GdkSurface's own
        width and height are what GDK has *asked* for and led X by a whole map
        in the measurement above. Nothing is declared until there is an
        allocation to declare it against, and it is clamped to one row short of
        it, so the margin can lag the window and can never lead it — the same
        rule the XShape clip in this file already lives by.

        Cached on the result, which moves only when the display scale does, so
        the steady state is one comparison and no X traffic. Keyed on the xid
        as well, because a hide takes the surface away and the window comes
        back with a new one and no properties on it.
        """
        xid = self._xid()
        if not xid:
            return
        alloc = self.get_height() * max(1, self._scale())
        if alloc <= 1:
            return
        apron = max(0, min(self._arrow.apron_height(), alloc - 1))
        key = (xid, apron)
        if key == self._extents_key:
            return
        self._extents_key = key
        WM.set_frame_extents(xid, 0, 0, 0, apron)

    def _shape_now(self) -> None:
        """Hand X the window's input region: the slab, the disc, the grab band.

        The *paint* needs no help. The window is a 32-bit ARGB toplevel, every
        node that could put a background on it is `background: transparent`
        (see the CSS), and .halo-glow rounds its own corners to CORNER_RADIUS —
        so the corners, and every row of apron below the pill, are already
        alpha 0 in the frame the client commits. mutter blends them. There is
        no bounding shape and there has not been one since 1.21.0;
        set_rounded_shape() carries the measurement.

        What X still has to be told is which of those pixels take the pointer,
        because "transparent" and "click-through" are different questions and
        only the second one is X's. So:

          - the rounded slab, so a click on a corner falls through to whatever
            is behind rather than onto a corner of window that paints nothing;
          - the disc, when one has emerged, so the detached ↓ can be pressed;
          - `grab`: the panel's resize border, RESIZE_GRAB_PX rows of apron
            immediately below the pill's painted edge. That band is the whole
            point of dropping the bounding shape. It is outside everything the
            page paints, so the page's last ten rows scroll and click again,
            and it is where a hand that has resized any other GNOME window
            already reaches.

        Most calls ask for a region X already has: the ticker resamples every
        frame for a little over a reveal animation. Sending it anyway meant
        ~50 XShapeCombineRectangles round-trips, each rebuilding ~90
        rectangles, for every keystroke that changed the suggestion list —
        hence the cache key.
        """
        xid = self._xid()
        if not xid:
            return
        size = self._window_size_device()
        if size is None:
            return
        # The apron is window the manager must not count — see
        # _frame_extents_now(), which declines to answer until there is an
        # allocation to measure against. Here because this already runs on
        # every frame in which the geometry can have moved, and it is cached
        # on a figure that changes only with the display scale.
        self._frame_extents_now()
        pill = self._pill_height_device()
        if pill <= 0:
            return
        disc = self._arrow.shape_disc()
        if disc is not None:
            disc = (disc[0], pill + disc[1], disc[2])
        # Never more apron than there is: the band is bounded by the window's
        # own foot, so a frame in which the apron has not been allocated yet
        # cannot hand X rows that are not part of the window.
        grab = 0
        corners = None
        if self._grip_live():
            grab = max(0, min(size[1] - pill,
                              self.RESIZE_GRAB_PX * self._scale()))
            # ...and the nook outside each bottom corner arc, which is where a
            # hand aiming at the corner actually stands. Without these two the
            # corner grips added to the slab are widgets the pointer can never
            # reach: the region follows the arc, and outside the arc the click
            # goes to the desktop. Clamped to the slab, like the band is to the
            # window, so a frame that has not been laid out yet cannot hand X
            # rows the pill does not have.
            corners = (min(self.RESIZE_CORNER_PX * self._scale(), size[0] // 2),
                       min(self.RESIZE_CORNER_UP_PX * self._scale(), pill))
        key = (xid, size[0], pill, disc, grab, corners)
        if key == self._shaped_key:
            return
        self._shaped_key = key
        WM.set_rounded_shape(xid, size[0], pill,
                             CORNER_RADIUS * self._scale(),
                             disc=disc, grab=grab, corners=corners)

    def _sync_geometry(self) -> bool:
        """Keep the window's rounded clip in step with its current size."""
        if not self._xid():
            return False
        size = self._window_size_device()
        if size is None:
            return False
        self._last_size = size
        self._shape_now()
        # The left-corner walk is deliberately NOT redone here any more. This
        # runs a frame behind, from a size GTK has already moved on from, and
        # a walk computed from a stale width undoes the one the drag just
        # placed: measured, it reset the pin to the previous frame's width and
        # put the oscillation straight back. The drag places its own left edge
        # and its own width in one request; there is nothing left to settle.
        return False

    # How long the first map may stay invisible waiting to be placed. Generous
    # against the ~110ms it actually takes, and a ceiling rather than a delay:
    # the pill is revealed the moment it is where it belongs, and this only
    # decides how long a desktop that never agrees can hold it up.
    FIRST_MAP_HOLD_MS = 400

    def _move_window(self, xid: int, x: int, y: int) -> None:
        """Move, and remember where we asked for — see _reveal_when_placed."""
        self._placed_target = (int(x), int(y))
        WM.move(xid, int(x), int(y))

    def _resume_placement(self, xid: int) -> None:
        """Put the pill back where it stood before it was hidden.

        "Do not reposition" cannot mean "do not move", and that was the bug.
        A hide unmaps the window, so the position is not being kept for us by
        anyone: mutter places it again on the way back, exactly as it does on a
        first map, and the old code then *adopted* whatever that placement was
        as the target it had supposedly asked for. Nothing put the pill right
        again except _restore_pill_y(), which runs off the frame ticker and
        only corrects y.

        Measured on the Circle to Search path, pill parked at (580, 283): it
        came back at (580, 486) — mutter's idea of the middle — and stood there
        fully opaque for 27 of the 44 frames sampled, about 530ms, before the
        ticker dragged it up. That is the whole of "spawns in the middle of the
        screen, then teleports up to where it was".

        So the return is a placement like any other: ask for the old spot,
        record it as the target, and let _hold_until_placed() keep the pill off
        the screen until it is actually there.
        """
        want = self._resume_at
        self._resume_at = None
        if want is None:
            # Hidden by some path that did not record a spot. Nothing better to
            # do than take where we are, which is what this always did.
            self._note_pill_y()
            self._placed_target = WM.get_position(xid)
            return
        # The screen may have changed shape while the portal had the display.
        size = self._frame_size_device()
        if size is not None:
            want = self._clamp_to_monitors(
                list(want), size[0], size[1], self._scale()) or want
        self._move_window(xid, int(want[0]), int(want[1]))
        self._pill_y = int(want[1])

    def _hold_until_placed(self) -> None:
        """Keep the first frame off the screen until the pill is placed.

        mutter places a window that asks for no position wherever it likes, and
        it does so *after* the map — measured on this machine, 33ms after,
        overriding the position we had already asked for. The compositor shows
        an X client the moment it commits a buffer, and the first buffer lands
        at ~77ms, so there is a window of about 30ms in which the pill is on
        screen in the middle of the display before the placement that follows at
        ~107ms puts it right. That is the whole of "it pops up in the middle for
        a brief moment and then teleports".

        Neither of the two ways of asking mutter not to do that works here: a
        pre-map XMoveWindow is simply overridden, and the USPosition hint that
        is meant to settle it does not survive GTK rewriting WM_NORMAL_HINTS as
        it maps. So the pill is held transparent instead and revealed once it is
        actually where it was put — which needs no cooperation from the window
        manager at all, only that we can see where the window ended up.

        Opacity, not an empty shape, because the window is a 32-bit ARGB
        drawable that already paints alpha 0 outside the pill; an opacity of 0
        is genuinely nothing on screen rather than a black rectangle. The
        deadline exists so that no failure of this — a desktop that never
        settles, a move that is refused for ever — can leave the pill invisible.
        """
        self._reveal_deadline = (GLib.get_monotonic_time()
                                 + self.FIRST_MAP_HOLD_MS * 1000)
        GLib.timeout_add(16, self._reveal_when_placed)

    def _reveal_when_placed(self) -> bool:
        xid = self._xid()
        want = self._placed_target
        if not xid or want is None:
            self.set_opacity(1.0)
            return False
        if WM.get_position(xid) == want:
            self.set_opacity(1.0)
            return False
        if GLib.get_monotonic_time() > self._reveal_deadline:
            # Whatever is happening out there, the pill is not staying hidden.
            self.set_opacity(1.0)
            return False
        # mutter has moved it since; ask again. Its placement happens once, so
        # this converges rather than fighting.
        WM.move(xid, want[0], want[1])
        return True

    def place_near_pointer(self) -> None:
        """Centre horizontally on the display holding the mouse, a little
        above the middle — where the eye already is."""
        xid = self._xid()
        if not xid:
            return
        scale = self._scale()
        size = self._frame_size_device()
        if size is None:
            return
        win_w, win_h = size

        pointer = WM.pointer()

        # Wherever it is about to open, the pointer over it has to be the size
        # that screen wants. Cheap unless the answer has changed.
        #
        # ABOVE the remembered-position branch, because that branch *returns*.
        # Below it, the whole per-monitor cursor fix — the one written for the
        # pointer coming out four times too big on a scaled screen — never ran
        # on an ordinary summon for anyone with "remember position" on, which is
        # the default and the exact path the report came from. It still fired
        # after a drag and on a monitor change, so it looked like it worked
        # whenever anyone went looking for it. The tests measured the arithmetic
        # of _wanted_cursor_px() thoroughly and never once asked whether it was
        # called.
        GLib.idle_add(self._sync_cursor_size)

        if CFG["remember_position"] and CFG["position"]:
            saved = self._clamp_to_monitors(
                self._saved_position(scale), win_w, win_h, scale)
            if saved:
                self._move_window(xid, saved[0], saved[1])
                self._pill_y = saved[1]
                return
            # The saved spot no longer exists — a smaller screen, or a monitor
            # that has been unplugged. Fall through and place it afresh rather
            # than opening off-screen where it cannot be found.

        rect = self._usable_rect(pointer)
        x = rect[0] + (rect[2] - win_w) // 2
        y = rect[1] + int(rect[3] * float(CFG["vertical_anchor"]))
        # Reserve the height the window has with the panel *open*, not the pill's
        # own. Clamping to the collapsed height put the pill somewhere the results
        # cannot open without the window manager moving the whole window up — and
        # _note_pill_y() deliberately refuses to record a position it was moved
        # to, so the anchor was stranded and collapsing returned the pill to the
        # wrong place. Measured on a 969px work area: the 0.26 anchor put the pill
        # at y=284, a 620px panel makes the window 721px, and 284+721 overshoots
        # the bottom by 3px — so it opened 3px above where it was put, every time.
        y = max(rect[1], min(y, rect[1] + rect[3]
                             - self._expanded_height(win_h, pointer)))
        self._move_window(xid, int(x), int(y))
        # Remember the anchor: growing the results panel makes the window taller
        # than the room below the pill, so the window has to slide up to fit, and
        # this is where it belongs again once the panel shrinks.
        self._pill_y = int(y)

    def _clamp_to_monitors(self, position, win_w: int, win_h: int,
                           scale: int) -> tuple[int, int] | None:
        """Nudge a remembered position back onto a monitor that still exists.

        Screens change: a spot saved on a 4K display can be far outside a 1080p
        one, which would open Halo where nobody can see it. Returns None when the
        position does not belong to any current monitor at all.
        """
        try:
            x, y = int(position[0]), int(position[1])
        except Exception:
            return None
        best = None
        for rect in self._all_monitor_rects(scale):
            mx, my, mw, mh = rect
            # Does the window's top-left still sit on this monitor?
            if mx - 40 <= x <= mx + mw and my - 40 <= y <= my + mh:
                best = rect
                break
        if best is None:
            return None
        mx, my, mw, mh = best
        return (max(mx, min(x, mx + mw - win_w)),
                max(my, min(y, my + mh - win_h)))

    def _all_monitor_rects(self, scale: int) -> list[tuple[int, int, int, int]]:
        rects = []
        try:
            monitors = Gdk.Display.get_default().get_monitors()
            for i in range(monitors.get_n_items()):
                mon = monitors.get_item(i)
                geo = mon.get_geometry()
                ms = max(1, mon.get_scale_factor())
                rects.append((geo.x * ms, geo.y * ms, geo.width * ms, geo.height * ms))
        except Exception:
            pass
        return rects

    def _usable_rect(self, pointer) -> tuple[int, int, int, int]:
        """The monitor under the pointer, trimmed to what the desktop leaves free.

        Everything vertical is decided from this rather than the raw monitor, so
        the pill cannot be placed under GNOME's top bar and the panel cannot be
        sized to run past the bottom of the work area.
        """
        return self._usable_pick(pointer)[0]

    def _usable_pick(self, pointer):
        """_usable_rect() together with the scale it is measured in."""
        rect, ms = self._monitor_pick(pointer, self._scale())
        work = WM.workarea()
        if not work:
            return rect, ms
        x0, y0 = max(rect[0], work[0]), max(rect[1], work[1])
        x1 = min(rect[0] + rect[2], work[0] + work[2])
        y1 = min(rect[1] + rect[3], work[1] + work[3])
        if x1 - x0 < 200 or y1 - y0 < 200:
            # No sensible overlap. _NET_WORKAREA is one rect for the whole
            # desktop, so on a multi-monitor setup it need not describe this
            # screen at all; the monitor is the better answer then.
            return rect, ms
        return (x0, y0, x1 - x0, y1 - y0), ms

    def _monitor_pick(self, pointer, scale: int):
        """The monitor under the pointer: its device-pixel rect and its scale.

        The scale is returned with the rect because it is the factor the rect was
        built with, and anything converting that rect back to logical pixels has
        to divide by exactly that. Reaching for the window surface's scale instead
        is a real difference and not a theoretical one: Circle to Search hides the
        window and expands the instant it comes back, so the surface can be absent
        or not yet rescaled and answer 1 while the monitor is 2. The usable height
        then reads double, the clamp finds nothing to clamp, and the panel keeps a
        saved height meant for a screen twice as tall.
        """
        display = Gdk.Display.get_default()
        best = None
        try:
            monitors = display.get_monitors()
            for i in range(monitors.get_n_items()):
                mon = monitors.get_item(i)
                geo = mon.get_geometry()
                ms = max(1, mon.get_scale_factor())
                rect = (geo.x * ms, geo.y * ms, geo.width * ms, geo.height * ms)
                if best is None:
                    best = (rect, ms)
                if pointer and (rect[0] <= pointer[0] < rect[0] + rect[2]
                                and rect[1] <= pointer[1] < rect[1] + rect[3]):
                    return rect, ms
        except Exception:
            pass
        return best or ((0, 0, 1920 * scale, 1080 * scale), max(1, scale))

    def _monitor_rect_for(self, pointer, scale: int) -> tuple[int, int, int, int]:
        """Monitor geometry in device pixels, chosen by pointer location."""
        return self._monitor_pick(pointer, scale)[0]

    def _remember_position(self) -> None:
        if not CFG["remember_position"]:
            return
        xid = self._xid()
        if not xid:
            return
        where = WM.get_position(xid)
        if where:
            # Logical, not device — and stamped with the scale it was taken at,
            # so a config written by an older build is not read as the wrong
            # unit exactly once. This outlives the session, and the device frame
            # doubles the day the display scale does: a pill parked near the
            # foot of a 1x screen reopened 40% of the way down the same screen
            # at 2x, which is a silent relocation nobody can explain.
            # panel_height and window_width are already logical for the same
            # reason — see _clamp_panel().
            ms = max(1, self._scale())
            CFG["position"] = [where[0] // ms, where[1] // ms]
            CFG.data["position_scale"] = ms

    def _saved_position(self, scale: int) -> list:
        """The remembered spot, in device pixels for the screen we have now.

        Two formats, told apart by whether position_scale is there at all:

        · Stamped — written by this build or later, so the numbers are logical
          and want multiplying up by today's scale. This is the case the whole
          change exists for: the spot survives a change of display scale.
        · Unstamped — written before, so the numbers are already device pixels,
          at whatever scale was current then. Handed back untouched, which is
          exactly what the old code did and is right in every case except the
          one nobody can detect: an old config carried across a scale change.
          Guessing there would misplace the pill for everyone whose scale never
          changed, which is nearly everyone.
        """
        saved = list(CFG["position"])
        try:
            x, y = int(saved[0]), int(saved[1])
        except Exception:
            return saved
        if not CFG.data.get("position_scale"):
            return [x, y]                       # legacy: already device
        return [x * max(1, int(scale)), y * max(1, int(scale))]

    # ── show / hide ─────────────────────────────────────────────────────
    def show_popup(self, reposition: bool = True) -> None:
        self._cancel_idle_release()
        # Before the realise below, so the first allocation is already the right
        # size and nothing measures, shapes or places the window at a width the
        # screen it is opening on cannot hold. The pointer is the anchor because
        # we are not on screen yet, which is what _anchor_point() answers with.
        self._fit_width()
        # Whatever hide_popup() silenced, it silenced on the way out. Nothing
        # else takes the mute off, so a pill dismissed once and summoned again
        # would be a panel that never made a sound for the rest of the session.
        self._wake_page()
        self._set_rim_running(True)
        first_map = not self.get_realized()
        # A hidden window has no surface, so the shape it had is gone with it and
        # the cached key must not stop us reshaping on the way back up.
        self._shaped_key = None
        # ...and what the window manager was told about the apron with it:
        # the property lives on the surface, so it goes when the surface does.
        self._extents_key = None
        if first_map:
            # Realise and clip *before* the map. present() on an unrealised
            # window creates the surface and maps it in one go, so the first
            # thing handed to the compositor was an unconfigured, unshaped
            # window — square corners, one pixel tall, for ~35ms. Realising by
            # hand separates the two, and _sync_geometry() then measures the
            # pill and cuts its corners while there is still nothing on screen
            # to see it. The same split ParkedArrow.show() makes, for the same
            # reason.
            try:
                self.realize()
            except Exception:
                pass
            self._sync_geometry()
            # Invisible until placed. See _hold_until_placed.
            self._placed_target = None
            self.set_opacity(0.0)
        elif not reposition:
            # A reopen that has to land on an exact spot needs the same hold as
            # a first map, and for the same reason: this window has been
            # unmapped, so the map below is a map mutter will place, and the
            # frame it paints in the meantime is one the user can see. It is
            # only the *first* map that a surviving surface saves us from, not
            # the placement.
            self._placed_target = None
            self.set_opacity(0.0)
        self.present()
        if first_map:
            # Above the redraw, which is the right order but is NOT on its own
            # enough — see _reveal_when_placed, which is what actually fixes the
            # pill appearing in the middle of the screen first. GTK's frame
            # clock paints at GDK_PRIORITY_REDRAW (120) and a plain idle runs at
            # PRIORITY_DEFAULT_IDLE (200), so a default idle is guaranteed to be
            # late; this only stops it being late twice over.
            #
            # What it cannot help with is that mutter places the window *itself*
            # some time after the map, overriding whatever we asked for.
            # Measured on this machine, first summon: our move lands, mutter
            # overrides it at 33ms with its own (1160, 1094), the first frame is
            # painted at 77ms — on screen, in the wrong place — and the position
            # is only ours again at 107ms.
            #
            # Placing it before the map does not work either: a pre-map
            # XMoveWindow to (700, 300) came back (1160, 1094), and the
            # USPosition/PPosition hint that is meant to stop mutter placing a
            # window never survives the map, because GTK rewrites
            # WM_NORMAL_HINTS as it maps — the flags went 0x8 → 0xd when pinned
            # by hand and came back 0x30 the moment the window was shown.
            GLib.idle_add(self._post_map, reposition,
                          priority=GLib.PRIORITY_HIGH_IDLE)
        else:
            xid = self._xid()
            if xid:
                WM.float_above(xid)
                WM.activate(xid)
            if reposition:
                # Above the redraw for the same reason as the first map: a
                # reopen that repositions would otherwise paint one frame where
                # the pill used to be.
                GLib.idle_add(lambda: (self.place_near_pointer(), False)[1],
                              priority=GLib.PRIORITY_HIGH_IDLE)
            elif xid:
                # Back up exactly where it was — which takes a move, because
                # the map above has just handed the placing of this window to
                # mutter again. Above the redraw like the branch beside it, and
                # held invisible until it lands. See _resume_placement().
                GLib.idle_add(lambda: (self._resume_placement(xid),
                                       self._hold_until_placed(), False)[2],
                              priority=GLib.PRIORITY_HIGH_IDLE)
            else:
                # No window to ask about, so nothing can confirm a placement and
                # the hold above would never be lifted. _post_map ends the same
                # way, for the same reason: an invisible pill is far worse than
                # one that opens where the desktop felt like putting it.
                self.set_opacity(1.0)
                self._note_pill_y()
            # Track, not sample: at this point the toplevel has no allocation yet,
            # and one shot at an idle shaped the pill short every single reopen.
            self._track_geometry()
        # Animated: being summoned is the disc arriving, exactly as a collapse
        # is, and it read as simply being there already.
        GLib.idle_add(lambda: (self._update_arrow(animate=True), False)[1])
        self.focus_entry(select_all=True)

    def _post_map(self, reposition: bool) -> bool:
        xid = self._xid()
        if xid:
            WM.float_above(xid)
            if reposition:
                self.place_near_pointer()
            else:
                self._resume_placement(xid)
            WM.activate(xid)
            self._track_geometry()
            self._hold_until_placed()
        else:
            self.set_opacity(1.0)
        self._update_arrow(animate=True)
        self.focus_entry(select_all=True)
        return False

    def hide_popup(self) -> None:
        self._remember_position()
        # A spot recorded for a Circle to Search that then went through an
        # ordinary dismiss instead. Left set, it would drag the pill back to
        # wherever it stood before that screenshot on some later reopen.
        self._resume_at = None
        self.leave_find()
        self._show_suggestions([], animate=False)
        self.close_history(animate=False)
        # The debounced write is the only copy of the last search or two, and the
        # daemon may not be quit before the machine is.
        HISTORY.flush()
        # Abandon any Lens hand-off. Left set, it would keep the "Searching…"
        # cover up on the next open and answer a later file chooser with a
        # stale image.
        self.pending_lens = None
        self.lens_text = ""
        self.lens_attempts = 0
        self._lens_await_load = False
        self._awaiting_lens_url_until = 0
        self._nav_pending = False
        self._cancel_lens_timers()
        self._hide_loading()
        self._set_busy(False)
        self._arrow.hide()
        if self.expanded:
            # Snap shut without animating: the window is about to vanish, and a
            # transition would only be seen as a flicker on the next open.
            self.expanded = False
            self.result_reveal.set_transition_duration(0)
            self.result_reveal.set_reveal_child(False)
            self.result_reveal.set_transition_duration(self._reveal_ms())
        # What a double tap of the shortcut puts back. Read before the field is
        # cleared, which is the only moment it still says what was being looked
        # at — see _double_tap().
        #
        # Falling back to the last search when the field is empty, because the
        # field is empty for an ordinary reason: every reopen clears it, so a
        # page brought back with ↓ sits above a blank field. "Fill the search
        # bar again" means the query that fetched what is on screen, and ↓'s own
        # rule already treats an empty field and one holding last_query as the
        # same thing — so this is that rule stated once more rather than a new
        # one.
        self._closed_with = self.entry.get_text().strip() or self.last_query
        self._tapped_at = 0.0
        self.entry.set_text("")
        # The words go and the picture goes with them, into the same stash the
        # parked page is already in — ↓ brings all three back together. Not the
        # same as throwing it away, and _stash_attachment() says why.
        self._stash_attachment()
        self._reset_placeholder()
        self.set_visible(False)
        # The closes above each asked for a re-decision, and this is the line
        # that makes the answer "no": whatever they left pending is settled
        # against a window that is now hidden. See _arrow_soon().
        # Nothing to animate for, and _on_active_changed bails out early once we
        # are hidden, so it cannot do this for us.
        self._set_rim_running(False)
        self._arm_idle_release()
        # After set_visible(False), so the callback can tell that nobody came
        # back in the meantime.
        self._quiet_page()

    # The grab is retried once a frame until it sticks, rather than once at 90ms.
    # A single late attempt had to guess when the window becomes focusable, and
    # on a cold map it guessed wrong in both directions — too early and it did
    # nothing, too late and the popup had already finished animating in. Those
    # were the milliseconds a keystroke could fall into.
    FOCUS_RETRY_MS = 16
    FOCUS_RETRY_LIMIT = 25          # ~0.4s, then stop rather than fight the user

    def focus_entry(self, select_all: bool = False) -> None:
        baseline = self.entry.get_text()
        tries = [0]

        def do_focus(late: bool) -> bool:
            # The late pass exists for a cold map, where the first grab can land
            # before the window is focusable. But 90ms is well inside the time a
            # fast typist needs to start, and grab_focus() on an entry selects
            # everything in it — so the late pass used to re-select what had just
            # been typed, and the next keystroke wiped it.
            #
            # It is the grab that has to be skipped, not just the select: guarding
            # only select_region() still left grab_focus() selecting the field.
            # And the signal to skip on is that the text changed, which is true
            # whether or not the compositor has got round to activating us —
            # is_active() alone was not enough, because a window can hold the
            # caret before it reports itself active.
            typed = self.entry.get_text() != baseline
            if late and (typed or self._typing_in_entry()):
                return False        # already where it needs to be; leave it be
            self.set_focus(self.entry)
            self.entry.grab_focus()
            if select_all and not typed:
                self.entry.select_region(0, -1)
            else:
                # Caret at the end, so a keystroke appends instead of replacing.
                self.entry.set_position(-1)
            if not late:
                return False
            tries[0] += 1
            # Keep going only until the caret is genuinely in the field.
            return (tries[0] < self.FOCUS_RETRY_LIMIT
                    and not self._typing_in_entry())

        GLib.idle_add(do_focus, False)
        GLib.timeout_add(self.FOCUS_RETRY_MS, do_focus, True)

    # ── the shortcut, pressed twice ─────────────────────────────────────
    #
    # A press summons the pill; a second one hard on its heels puts back the
    # panel and the query it was closed with, which is the whole of what "open
    # it again" usually means. Long enough for a deliberate double tap, short
    # enough that nobody reaching for the shortcut to *close* the pill can fall
    # into it — that hand has read the screen first, and 450ms is less than the
    # time that takes.
    DOUBLE_TAP_MS = 450

    def toggle(self) -> None:
        # Asked before is_active(), not after. A window one press old may not
        # have been given the focus yet, and that is exactly when the second
        # press lands — routing it by activeness sent the fast half of every
        # double tap down the "summon" branch and re-armed it, so the feature
        # worked only when it was not needed.
        if self.get_visible() and self._double_tap():
            return
        if self.get_visible() and self.is_active():
            self.hide_popup()
        else:
            self.show_popup()
            self._tapped_at = GLib.get_monotonic_time()

    def _double_tap(self) -> bool:
        """The second press. True when it has been dealt with as one.

        Goes through _on_arrow() rather than calling expand() directly, so this
        is ↓ — the same rung of the same ladder, with the same rules about
        parked pages and half-typed queries. A second door onto one behaviour,
        which is what the disc below the pill is too. The field is put back
        after ↓ has answered, never before; see below for what happened when it
        was the other way round.

        Disarms whatever the answer is: one press can only ever be the second
        half of one other.
        """
        armed, self._tapped_at = self._tapped_at, 0.0
        if not armed or self.expanded:
            return False
        if GLib.get_monotonic_time() - armed > self.DOUBLE_TAP_MS * 1000:
            return False
        # Anything typed in between is the search they mean, and putting the old
        # query back over it would be the worst thing this could do.
        if self.entry.get_text().strip():
            return False
        # ↓ is asked first, and asked of the field as hide_popup() left it —
        # empty. The order is the whole of a bug.
        #
        # The query used to go back before the question was put, and _on_arrow()
        # answers it by reading the field: its rule is "↓ shows the results for
        # whatever is in the field", so it refuses while a query that is not the
        # one on screen is being typed. Restoring _closed_with put exactly such a
        # query there. So closing the pill with something typed but never
        # searched — "reise nach berlin" over a wikipedia page — armed a double
        # tap that filled the field, was then refused by ↓ for having filled it,
        # and fell through to "close". Reproduced against a live resident driven
        # by two real `halo.py --toggle` processes 200ms apart: the pill opened
        # and shut again on the second press, while ↓ pressed by hand brought the
        # page straight back, because by then the field was still empty. That is
        # the report — "the shortcut opens and closes it, the button and ↓ work".
        # Self-perpetuating as well: closing recorded the same text once more, so
        # it stayed broken until the next search.
        #
        # The launcher's own latency is not in it, and was measured out rather
        # than assumed: both presses pay the same ~0.4s of process start, so it
        # cancels — 200ms between the two spawns arrived as 206ms between the two
        # toggle() calls, well inside DOUBLE_TAP_MS.
        #
        # And the decision is ↓'s, not this function's. Guarding on there being
        # a query to put back was wrong, and wrong in exactly the case the whole
        # feature is for: reopen a pill with a page parked behind it and the
        # field is *empty* — hide_popup() cleared it — so bringing the page back
        # with ↓ and then closing recorded nothing to restore, and the next
        # double tap fell through to "close". It needs a page to come back to,
        # which is a different question, and _on_arrow() is where it is asked.
        took = self._on_arrow(False)
        # Only once there is something to put the query above. Nothing came back
        # means nothing to restore it over, and leaving the field alone is what
        # lets the *next* double tap work: the close that follows then records an
        # empty field rather than writing the same bad text back.
        if took and self._closed_with:
            self.entry.set_text(self._closed_with)
            self.entry.set_position(-1)
            # The text change asks for suggestions, and a list arriving over the
            # panel that has just opened would be the drawer fighting the page.
            self._show_suggestions([], animate=False)
        return took

    # The sweep's period, and it has to match .halo-glow's animation-duration.
    RIM_PERIOD_S = 9.0

    # How long the rim takes to go grey, and to come back.
    #
    # 320ms out: long enough to read as a fade rather than a change — measured,
    # thirteen frames of it — and short enough that the answer to "has Halo got
    # the keyboard" is fully on screen inside a third of a second, which is
    # about as long as anyone spends asking. Slower drifts into "the rim is
    # doing something", which is the opposite of an indicator.
    #
    # 200ms back: shorter on purpose. Coming back is a reply to something the
    # user just did — a click, an Alt-Tab — and a reply that takes as long as a
    # departure feels laggy where the departure felt gentle. The pair reads as
    # "settles down slowly, wakes up promptly".
    RIM_DRAIN_S = 0.32
    RIM_LIFT_S = 0.20

    def _set_rim_running(self, running: bool) -> None:
        """Run the rim's sweep only while there is somebody looking at it.

        The animation is endless, so the surface never stops asking for frames —
        and a compositor stops delivering them to a window that is not in front,
        at which point GDK blocks for a second per cycle waiting. That stalls the
        whole process, not just the pill. See the .halo-still rule.

        **The phase is carried across the pause by hand, and it has to be.**
        `animation-play-state: paused` does hold the rim still — verified, the
        rim renders identically two and a half seconds apart while paused. What
        it does not do is come back where it left off, and the reason is the
        very optimisation the pause exists for: GTK shifts a paused animation's
        start time forward as timestamps arrive, and a paused animation is
        static, so the frame clock stops and no timestamps arrive. The whole
        pause is therefore applied in one go on the way back. Measured across a
        2.5s pause: the rim resumed on a colour a third of a cycle away, which
        is "the gradient jumps to a different state instead of continuing".

        So the elapsed time is kept here, and the sweep is restarted with a
        negative `animation-delay` — the CSS way of saying "begin this many
        seconds in". Nothing else can do it: GTK exposes no handle on a running
        CSS animation, and the alternative is to keep the frame clock alive,
        which is the 981ms-per-cycle stall this all exists to avoid.
        """
        glow = getattr(self, "glow", None)
        if glow is None:
            return
        now = GLib.get_monotonic_time()
        if running:
            if self._rim_since is not None or self._rim_fade == "in":
                return                  # already sweeping; nothing to restart
            lit = _rim_gradient(self._rim_phase, self.RIM_PERIOD_S)
            from_ = self._rim_painted(lit)
            self._cancel_rim_fade()
            if from_ == lit or not self._rim_fade_wanted():
                self._apply_rim_phase(self._rim_phase)
                self._rim_since = now
                glow.remove_css_class("halo-still")
            else:
                # The fade animation outranks .halo-still whatever the class
                # says, but the class is also the answer to "is the rim held
                # still", so it comes off as the rim starts coming back.
                self._start_rim_fade("in", from_, lit, self.RIM_LIFT_S)
                glow.remove_css_class("halo-still")
        else:
            if self._rim_since is None and glow.has_css_class("halo-still"):
                # Already quiet, or on the way there. The one thing left
                # to do is cut short a fade nobody can see any more:
                # hide_popup() calls through here after set_visible(False),
                # and a fade that ran on past that would be an animation on
                # a window that is gone.
                if self._rim_fade == "out" and not self._rim_fade_wanted():
                    self._cancel_rim_fade()
                    self._apply_rim_phase(self._rim_phase, stop=True)
                return                  # already quiet; nothing to restate
            if self._rim_since is not None:
                spent = (now - self._rim_since) / 1_000_000.0
                self._rim_phase = (self._rim_phase + spent) % self.RIM_PERIOD_S
                self._rim_since = None
            from_ = self._rim_painted(
                _rim_gradient(self._rim_phase, self.RIM_PERIOD_S))
            self._cancel_rim_fade()
            if from_ == RIM_DRAINED or not self._rim_fade_wanted():
                # Take the animation away first, then put the class on: the
                # class is what paints the drained rim and it cannot outrank an
                # animation that is still named. Both happen before GTK
                # validates the style again, so there is no frame in which
                # either shows on its own.
                self._apply_rim_phase(self._rim_phase, stop=True)
            else:
                self._start_rim_fade("out", from_, RIM_DRAINED,
                                     self.RIM_DRAIN_S)
            # On as the rim starts going grey, not when it gets there. The
            # class is what everything else reads to mean "Halo is not the
            # active window" — the self-test asks the instant focus goes — and
            # the fade animation outranks its background-image until it ends,
            # so saying it early costs nothing on screen.
            glow.add_css_class("halo-still")

    def _rim_fade_wanted(self) -> bool:
        """Whether there is anybody to see the rim change.

        A pill that is not on screen has nothing to soften and animating one is
        frames spent on nothing, which is the one thing freezing the rim exists
        to avoid. show_popup() and hide_popup() both come through
        _set_rim_running() with the window down, so this is the ordinary case,
        not an edge one: summoning a pill lights its rim at once.
        """
        return bool(self.get_mapped() and self.get_visible())

    def _rim_painted(self, lit: str) -> str:
        """The gradient the rim is showing this instant.

        Mid-fade that is a mix, and it has to be read rather than assumed. A
        focus that flaps inside the fade — click away, click back — must carry
        on from the colour that is up; restarting from either end of the fade
        would put the snap back in exactly the case that is most likely to be
        noticed.
        """
        if self._rim_fade is not None:
            spent = ((GLib.get_monotonic_time() - self._rim_fade_t0)
                     / 1_000_000.0)
            f = (1.0 if self._rim_fade_s <= 0.0
                 else max(0.0, min(1.0, spent / self._rim_fade_s)))
            return _mix_rgba(self._rim_fade_from, self._rim_fade_to, f)
        if self._rim_since is None and self.glow.has_css_class("halo-still"):
            return RIM_DRAINED
        return lit

    def _start_rim_fade(self, kind: str, from_: str, to: str,
                        secs: float) -> None:
        """Cross the rim from one gradient to another over `secs`.

        A one-shot @keyframes written into the same runtime provider the phase
        goes through, because the cascade leaves no other way in: an animation
        outranks a normal declaration whether it runs or not, so the only thing
        that can repaint a node an animation owns is another animation. Here
        that is the point rather than the obstacle — the animation IS the fade.

        `animation-fill-mode: forwards` holds its last frame until the timeout
        below hands over to the static rule, so there is no flash of the rim's
        starting colour in between.
        """
        self._rim_fade = kind
        self._rim_fade_t0 = GLib.get_monotonic_time()
        self._rim_fade_from, self._rim_fade_to = from_, to
        self._rim_fade_s = secs
        self._apply_rim_phase(self._rim_phase, fade=(from_, to, secs))
        # A little past the end, so the swap can only ever happen after the
        # animation has reached its last frame and never a frame before it.
        self._rim_fade_id = GLib.timeout_add(int(secs * 1000.0) + 30,
                                             self._settle_rim_fade)

    def _cancel_rim_fade(self) -> None:
        """Forget a fade in progress, without touching what is on screen."""
        if self._rim_fade_id is not None:
            GLib.source_remove(self._rim_fade_id)
        self._rim_fade_id = None
        self._rim_fade = None

    def _settle_rim_fade(self) -> bool:
        """Hand the finished fade over to whatever should hold the rim next.

        Going quiet, that is `animation-name: none` and .halo-still's own
        gradient, which is where the fade already is — measured, the handoff
        moves 0 channel steps — and which asks for no frames at all. Coming
        back, it is the sweep, resumed at the phase it was frozen on: the fade
        held that phase still for its whole run, so this is the same instant
        the freeze stopped at and "picks the sweep up where it left off" stays
        literally true.
        """
        kind, self._rim_fade, self._rim_fade_id = self._rim_fade, None, None
        if kind == "in":
            self._apply_rim_phase(self._rim_phase)
            self._rim_since = GLib.get_monotonic_time()
        else:
            self._apply_rim_phase(self._rim_phase, stop=True)
        return GLib.SOURCE_REMOVE

    def _apply_rim_phase(self, phase: float, stop: bool = False,
                         fade: "tuple[str, str, float] | None" = None) -> None:
        """Start the sweep `phase` seconds in, rather than from the beginning.

        `stop` writes no animation at all instead, which is what lets
        .halo-still's drained gradient reach the screen: an animation outranks
        a normal declaration in the cascade whether it is running or paused, so
        while one is named there nothing that class says can draw. It also asks
        for no frames, which is what freezing the rim is for.

        `fade` — (from, to, seconds) — writes a one-shot @keyframes between two
        gradients instead of either, which is how the rim changes colour
        gradually at all. It is named for both `.halo-glow` and
        `.halo-glow.halo-still` and says `animation-play-state: running`,
        because the fade out runs with .halo-still already on and that class
        pauses animations: measured, a provider at a higher priority overrides
        a more specific rule from a lower one, so `running` here beats `paused`
        there.
        """
        if self._rim_provider is None:
            self._rim_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), self._rim_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        # The name has to change, or the delay is ignored: GTK restarts a CSS
        # animation when its name changes and not when its delay does, and
        # without a restart there is nothing for a negative delay to offset.
        # Measured — with the delay alone, the rim still resumed a third of a
        # cycle away from where it was frozen.
        self._rim_alt = not self._rim_alt
        if fade is not None:
            from_, to, secs = fade
            name = "halo-fade-b" if self._rim_alt else "halo-fade"
            css = ("@keyframes %s {\n"
                   "  0%% { background-image: %s; }\n"
                   "  100%% { background-image: %s; }\n"
                   "}\n"
                   ".halo-glow, .halo-glow.halo-still {"
                   " animation-name: %s;"
                   " animation-duration: %.3fs;"
                   " animation-delay: 0s;"
                   " animation-timing-function: linear;"
                   " animation-iteration-count: 1;"
                   " animation-fill-mode: forwards;"
                   " animation-play-state: running; }"
                   % (name, from_, to, name, max(0.001, secs)))
        elif stop:
            css = ".halo-glow { animation-name: none; }"
        else:
            css = (".halo-glow { animation-name: %s; animation-delay: -%.3fs; }"
                   % ("halo-sweep-b" if self._rim_alt else "halo-sweep",
                      phase % self.RIM_PERIOD_S))
        try:
            self._rim_provider.load_from_string(css)
        except AttributeError:                   # GTK < 4.12
            self._rim_provider.load_from_data(css.encode())

    def _on_active_changed(self, *_a) -> None:
        # Focus loss must NOT dismiss: the whole point is copying text out of
        # the results. We only keep ourselves above the stack.
        self._set_rim_running(self.is_active() and self.get_visible())
        if not self.get_visible():
            return
        xid = self._xid()
        if xid:
            WM.float_above(xid)
        # Coming back to the front with nothing holding the caret — the ordinary
        # state once the compositor hands focus over — belongs in the search
        # field, so typing works without clicking first. Focus that is already
        # somewhere, the results page in particular, is the user's own choice
        # and is left exactly where they put it.
        if self.is_active() and self.get_focus() is None:
            self.focus_entry()

    def _on_close_request(self, *_a) -> bool:
        self.hide_popup()
        return True     # never destroy; the daemon stays warm

    # ── menu actions ────────────────────────────────────────────────────
    def _refresh_setup_status(self, sync_checks: bool = True) -> None:
        """Re-sync the menu with reality, including who owns which key.

        sync_checks is False right after the user ticks a box: a GSettings read
        immediately following a write can still return the old value, which
        would flip the box straight back off under their cursor.
        """
        launcher = DesktopIntegration.launcher_installed()
        autostart = DesktopIntegration.autostart_enabled()
        if sync_checks and self._sync_shortcuts_from_system():
            # A key appeared, changed or vanished outside Halo. The row list is
            # built from that, so it has to be rebuilt before anything below
            # walks it — and this is also what puts a shortcut somebody made in
            # Settings ▸ Keyboard on screen as a row of its own.
            self._rebuild_accel_rows()
        live = DesktopIntegration.active_shortcuts() if sync_checks else CFG["shortcuts"]

        for switch, state in ((self.sw_launcher, launcher),
                              (self.sw_autostart, autostart)):
            if switch.get_active() != state:
                # Blocked, or this is a write dressed up as a read — see toggle().
                switch.handler_block(switch.halo_handler)
                switch.set_active(state)
                switch.handler_unblock(switch.halo_handler)

        live_ids = {DesktopIntegration._accel_id(a) for a in live}
        owners = DesktopIntegration.conflict_map()      # one sweep for all rows
        clashes = []
        for accel, check, warn in self.accel_rows:
            wanted = DesktopIntegration._accel_id(accel) in live_ids
            if sync_checks and check.get_active() != wanted:
                check.handler_block_by_func(self._on_accel_toggled)
                check.set_active(wanted)
                check.handler_unblock_by_func(self._on_accel_toggled)
            wanted = check.get_active()
            conflicts = DesktopIntegration.find_conflicts(accel, owners)
            if conflicts:
                area, action = conflicts[0]
                warn.set_text(f"⚠ {action}")
                warn.set_tooltip_text(
                    f"Already used by {area}: {action}. GNOME will pick one of "
                    f"them unpredictably — change it in Settings ▸ Keyboard, or "
                    f"choose a different key here.")
                if wanted:
                    clashes.append(f"{DesktopIntegration.accel_label(accel)} "
                                   f"({action})")
            else:
                warn.set_text("")
                warn.set_tooltip_text(None)

        self._refresh_setup_button()

        if not DesktopIntegration.shortcut_supported():
            self.setup_status.set_text(
                "Shortcuts need GNOME's settings-daemon; add one manually in "
                "Settings ▸ Keyboard.")
        elif clashes:
            self.setup_status.set_text("In use elsewhere: " + ", ".join(clashes))
        else:
            bits = [("in Applications" if launcher else "not in Applications"),
                    ("starts at login" if autostart else "no autostart")]
            bits.append(", ".join(DesktopIntegration.accel_label(a) for a in live)
                        if live else "no shortcut")
            self.setup_status.set_text(" · ".join(bits))

        self.conflict_label.set_visible(bool(clashes))
        if clashes:
            self.conflict_label.set_text(
                "A ticked key is already taken. GNOME does not warn about this "
                "and picks a winner unpredictably, so Halo may not open. Untick "
                "it, or clear the other one in Settings ▸ Keyboard.")

    # ── following the system, rather than arguing with it ────────────────
    def _sync_shortcuts_from_system(self) -> bool:
        """Adopt whatever GNOME actually has bound. True if that changed us.

        A Halo shortcut can be added, re-bound or deleted in Settings ▸ Keyboard
        at any moment, and the right answer to that is not to argue: the dconf
        keys are what the keyboard does, so Halo's list follows them rather than
        overwriting them the next time the menu opens. A key that arrives this
        way and is not one of the presets becomes one of the user's own rows —
        the only place a list can put it honestly.

        Only ever called with sync_checks on, i.e. never straight after a write
        of our own, because a GSettings read that soon can still answer with the
        pre-write value and this would then undo the tick that caused it.
        """
        if not DesktopIntegration.shortcut_supported():
            # Nothing to follow. Without this, active_shortcuts() answers "no
            # keys" on a desktop that simply has no settings-daemon schemas, and
            # adopting that answer would quietly erase the user's own list.
            return False
        live = DesktopIntegration.active_shortcuts()
        changed = False
        if ({accel_id(a) for a in live}
                != {accel_id(a) for a in CFG["shortcuts"]}):
            CFG["shortcuts"] = live
            changed = True
        preset_ids = {accel_id(a) for a, _label in SHORTCUT_PRESETS}
        mine = {accel_id(a) for a in CFG["custom_shortcuts"]}
        extra = [a for a in live if accel_id(a) not in preset_ids
                 and accel_id(a) not in mine]
        if extra:
            CFG["custom_shortcuts"] = list(CFG["custom_shortcuts"]) + extra
            changed = True
        return changed

    def _dismiss_menu(self) -> None:
        """Close the ⋯ menu before opening a dialog from it.

        The menu is taller than the pill, so GTK gives the popover a surface of
        its own rather than drawing it inside the window — and that surface is
        stacked above the window, while an AdwDialog is drawn *inside* it. So a
        dialog opened from the menu appeared behind the menu, and the popover
        kept the keyboard grab, which is fatal for the one dialog whose whole job
        is to read a keystroke: "Add a shortcut…" could neither be seen nor
        typed into until the menu was clicked away by hand.
        """
        try:
            self.menu_button.popdown()
        except Exception:
            pass

    # ── the detached ↓ button ───────────────────────────────────────────
    def _page_to_return_to(self) -> bool:
        """Is there a page the pill can be opened back onto?

        The same test ↓ makes on the bare pill, and deliberately the same one: a
        button that appears when ↓ would do nothing, or fails to appear when it
        would work, is worse than no button.
        """
        return bool(self._parked_uri
                    or (self.web is not None and self.web.get_uri()))

    @staticmethod
    def _reveal_on_screen(revealer: Gtk.Revealer) -> bool:
        """Is any of this revealer's child still taking room in the window?

        Both flags, and the second is the one that matters here: reveal_child is
        where the revealer is heading, child_revealed is where it has got to, and
        it is where it has got to that decides how tall the window is *now*.
        """
        return bool(revealer.get_reveal_child() or revealer.get_child_revealed())

    def _arrow_belongs(self) -> bool:
        """Should the detached ↓ be on screen, as things stand right now?"""
        if not (CFG["parked_arrow"] and ON_X11 and WM.ok):
            return False
        # Not while anything taller than the bare pill is on screen — either
        # drawer, or the panel — for two reasons. The disc hangs off the bottom
        # of the *window*, so it would ride that thing's bottom edge: measured,
        # 371px down the screen and back while a suggestion list opened and
        # closed, which is a button bouncing around under someone who is typing.
        # And it would be lying: with a list open ↓ walks the list, so a button
        # whose whole promise is "the same as ↓" would be offering something the
        # keyboard is not.
        #
        # "On screen" is child_revealed, not reveal_child, and that distinction
        # is the whole of the second half of this bug. A revealer *told* to fold
        # reports reveal_child False on the instant, and stays its full height
        # for the length of the slide — so asking only that put the disc back at
        # the foot of a drawer that was still open, and _resting() then walked it
        # up frame by frame as the window shrank. Measured with the pill at its
        # default spot: the suggestion list sent the disc from y=350 to 721 and
        # back, the history drawer 48px, and collapsing the panel 648px in steps
        # of about 200 — a teleport and a crawl, in place of the 31px the slide
        # below is for. The handlers on each revealer's notify::child-revealed
        # are what bring it back once there is really room for it.
        busy = (self.expanded
                or self.history_open
                or self._reveal_on_screen(self.suggest_reveal)
                or self._reveal_on_screen(self.history_reveal)
                or self._reveal_on_screen(self.result_reveal))
        return bool(self.get_visible() and not busy
                    and self._page_to_return_to())

    def _page_is_live(self) -> bool:
        """Is the page still loaded, or only remembered?

        _release_engine() drops the whole web view when the pill has sat idle,
        keeping only the address, so ↓ still works but has to fetch again. The
        disc says which of the two it is.
        """
        return self.web is not None and bool(self.web.get_uri())

    def _apply_arrow(self, animate: bool) -> None:
        if self._arrow_belongs():
            self._arrow.set_cold(not self._page_is_live())
            self._arrow.show(animate=animate)
        else:
            self._arrow.hide()

    def _update_arrow(self, animate: bool = False) -> None:
        """Decide about the disc now. For callers whose state is already whole."""
        self._cancel_arrow_soon()
        self._apply_arrow(animate)

    def _arrow_soon(self, animate: bool = False) -> None:
        """Decide about the disc once this change of state has finished.

        Both drawers re-check the disc from inside their own close, and the
        caller doing the closing has usually not yet set the flag that decides
        whether the disc belongs on screen. expand() closes the history drawer
        three lines before it sets self.expanded; open_history() closes the
        suggestion list before it sets self.history_open; run_search() has a
        whole search between its close and the expand() that puts the disc away.
        Deciding then and there answered from half-made state and raised the disc
        only to take it away again — a build, map, move, 90-rectangle shape and
        unmap of an always-on-top window per close, and on the run_search() path
        long enough to be seen.

        An idle is the fix rather than a flag on each caller, because there is no
        list of callers to keep: the closes are shared and the next one written
        would have the same hole. It runs after the current call has finished, so
        the question is asked once, of state that is whole, and it is asked
        again from scratch — nothing is remembered but whether to slide.

        A *hide* is not deferred. The worst it can be is early, and a disc that
        is away for one frame longer than it had to be is invisible, whereas one
        that lingers over a drawer opening underneath it is not.
        """
        if not self._arrow_belongs():
            self._cancel_arrow_soon()
            self._arrow.hide()
            return
        self._arrow_animate = self._arrow_animate or animate
        if not self._arrow_idle:
            self._arrow_idle = GLib.idle_add(self._arrow_now)

    def _arrow_now(self) -> bool:
        self._arrow_idle = 0
        animate = self._arrow_animate
        self._arrow_animate = False
        self._apply_arrow(animate)
        return False

    def _cancel_arrow_soon(self) -> None:
        if self._arrow_idle:
            GLib.source_remove(self._arrow_idle)
            self._arrow_idle = 0
        self._arrow_animate = False

    def _on_history_revealed(self, *_a) -> None:
        """Bring the disc back once the history drawer has finished folding."""
        if not self.history_reveal.get_child_revealed():
            self._arrow_soon()

    def _on_result_revealed(self, *_a) -> None:
        """The panel has finished opening or folding.

        The geometry sync is why this signal was connected in the first place.
        The disc is the other half: collapse() asks for the slide and this is
        where it is honoured, because this is the first moment the window is
        short enough for the disc's resting spot to be the real one.
        """
        self._sync_geometry()
        if self.result_reveal.get_child_revealed():
            return
        animate = self._arrow_slide_pending
        self._arrow_slide_pending = False
        self._arrow_soon(animate=animate)

    def _shortcuts_changed_outside(self) -> None:
        """A shortcut was added, re-bound or deleted somewhere other than here.

        Coalesced onto a short timer: GNOME's keyboard panel writes the slot's
        binding and the registration list as separate operations, so one edit on
        screen arrives here as several notifications, and rebuilding the row list
        per notification would do it three times over for one change.
        """
        if self._shortcut_sync_pending:
            return
        self._shortcut_sync_pending = True
        GLib.timeout_add(250, self._apply_outside_shortcuts)

    def _apply_outside_shortcuts(self) -> bool:
        self._shortcut_sync_pending = False
        try:
            if self._sync_shortcuts_from_system():
                self._rebuild_accel_rows()
            # sync_checks=False: the sync above has just read the keys, so the
            # rows already agree with them, and reading them again here would
            # only risk catching dconf mid-write.
            self._refresh_setup_status(False)
        except Exception:
            pass
        return False

    # ── the setup button, which has to earn its place ────────────────────
    # It used to promise to set Halo up and then, nine times out of ten, open a
    # dialog headed "Halo is already set up" — because every switch in this menu
    # writes its own file the moment it is flipped, so by the time anyone presses
    # the button there is usually nothing left for it to do. A button whose only
    # message is "nothing to do here" is a button in the way.
    #
    # So it now says which of three jobs it is actually for, and each is a job
    # the switches above cannot do:
    #
    #   nothing installed  →  "Set up Halo…"     turn the whole lot on at once
    #   something wrong    →  "Repair Halo's…"   rewrite what has gone stale
    #   all correct        →  "Check Halo's…"    account for it, and write nothing
    #
    # The middle state is the one that matters, and it is not hypothetical:
    # everything setup writes records the absolute path of halo.py, so moving the
    # file leaves a launcher, an autostart entry and a shortcut that all still
    # exist and all point at nothing.
    def _setup_state(self) -> tuple[str, list[dict], list[dict]]:
        """("fresh" | "broken" | "ok", audit items, the ones needing repair)."""
        items = DesktopIntegration.audit()
        broken = [i for i in items if i["present"] and i["stale"]]
        if broken:
            return "broken", items, broken
        if not any(i["present"] for i in items):
            return "fresh", items, []
        return "ok", items, []

    SETUP_LABELS = {"fresh": "Set up Halo…",
                    "broken": "Repair Halo’s setup…",
                    "ok": "Check Halo’s setup…"}

    def _refresh_setup_button(self) -> None:
        state, _items, broken = self._setup_state()
        self.setup_btn.set_label(self.SETUP_LABELS[state])
        # Suggested only while there is something to do. Painting "check" as the
        # primary action is how the button came to look like the way in, when it
        # is the switches below that do the work.
        if state == "ok":
            self.setup_btn.remove_css_class("suggested-action")
        else:
            self.setup_btn.add_css_class("suggested-action")
        self.setup_btn.set_tooltip_text({
            "fresh": "Creates the launcher, the icon, the start-at-login entry "
                     "and your ticked shortcuts — after showing you the list",
            "broken": "Some of what Halo installed points at the wrong file. "
                      "This rewrites exactly those, and nothing else.",
            "ok": "Accounts for every file Halo has written and checks each one "
                  "still points here. Writes nothing.",
        }[state])
        self.setup_repair_label.set_visible(bool(broken))
        if broken:
            self.setup_repair_label.set_text(
                "⚠ " + ", ".join(i["what"].lower() for i in broken)
                + (" no longer point here" if len(broken) > 1
                   else " no longer points here")
                + " — press “Repair Halo’s setup” above.")

    def _setup_items(self, keys: list[str]) -> list[tuple[str, str, bool]]:
        """(what, where, is it already there) for each piece of setup."""
        launcher = DesktopIntegration.APPS_DIR / DesktopIntegration.DESKTOP_ID
        icon = DesktopIntegration.icon_file()
        autostart = (DesktopIntegration.AUTOSTART_DIR
                     / f"{APP_ID}-autostart.desktop")
        pretty = ", ".join(DesktopIntegration.accel_label(k) for k in keys)
        # Compared by id rather than by string, because the same key can be
        # spelled more than one way and two spellings are not two shortcuts.
        live = {accel_id(a) for a in DesktopIntegration.active_shortcuts()}
        wanted = {accel_id(a) for a in keys}
        return [
            ("A launcher", str(launcher), launcher.exists()),
            ("An icon", str(icon), icon.exists()),
            ("A start-at-login entry", str(autostart), autostart.exists()),
            (f"GNOME custom shortcuts for {pretty}",
             f"dconf: {DesktopIntegration.KEY_PATH_BASE}0/ …",
             bool(wanted) and wanted <= live),
        ]

    def _ticked_keys(self) -> list[str]:
        """Exactly the keys that are ticked — possibly none.

        This used to fall back to CFG["shortcuts"] and then to Super+G, so with
        every box unticked the setup dialog announced it would create a Super+G
        shortcut. Nobody had asked for that: the whole point of the dialog is
        that it lists precisely what is about to be written, and inventing an
        entry is the one thing it must not do.
        """
        return [a for a, check, _w in self.accel_rows if check.get_active()]

    def _on_finish_setup(self, *_a) -> None:
        """Whichever of the three jobs the button is currently offering."""
        state, items, broken = self._setup_state()
        if state == "broken":
            self._offer_repair(broken)
        elif state == "fresh":
            self._offer_setup()
        else:
            self._show_setup_report(items)

    def _offer_setup(self) -> None:
        """Turn the switches on, in one press, having listed what that writes.

        Worth being exact about what this is for, because it is easy to read as
        something cleverer. Every switch below it already writes its own file the
        moment it is flipped, so this creates nothing they could not; what it
        does is spare a fresh install three separate flips. That also means the
        switches reading "off" is the *reason* there is something to do here, not
        a preference being overridden — so the dialog says "switched on" rather
        than pretending the switches are not involved.
        """
        keys = self._ticked_keys()
        lines = ["+  Add to Applications — switched on, which writes:"
                 f"\n     {DesktopIntegration.APPS_DIR / DesktopIntegration.DESKTOP_ID}"
                 f"\n     {DesktopIntegration.icon_file()}",
                 "+  Start at login — switched on, which writes:"
                 f"\n     {DesktopIntegration.AUTOSTART_DIR}"
                 f"/{APP_ID}-autostart.desktop"]
        if keys:
            pretty = ", ".join(DesktopIntegration.accel_label(k) for k in keys)
            lines.append(f"+  The keys you have ticked — {pretty}"
                         f"\n     dconf: {DesktopIntegration.KEY_PATH_BASE}0/ …")
        else:
            lines.append("–  No keys are ticked, so no shortcut is created."
                         "\n     Tick one under “Keys that open Halo” first, or "
                         "add one of your own.")
        body = ("Halo will write exactly these, and nothing else:\n\n"
                + "\n\n".join(lines) + "\n\n"
                + "Each one has its own switch in this menu, so any of it can be "
                  "turned straight back off afterwards. No system files are "
                  "touched, nothing is installed with a package manager, and no "
                  "root access is used. Everything lives in your home folder and "
                  "can be undone with “Remove Halo’s setup”.")
        dialog = Adw.AlertDialog(heading="Switch all of this on?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Switch them on")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("apply")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_setup_response, keys)
        self._dismiss_menu()
        dialog.present(self)

    def _offer_repair(self, broken: list[dict]) -> None:
        """Name what has gone stale and what rewriting it will change."""
        lines = []
        for item in broken:
            detail = f"\n     {item['detail']}" if item["detail"] else ""
            lines.append(f"·  {item['what']}\n     {item['where']}{detail}")
        body = ("These exist but no longer point at this copy of Halo — the "
                "usual cause is halo.py having been moved or renamed since it "
                f"was set up. It now runs from:\n\n     {SELF_PATH}\n\n"
                + "\n\n".join(lines) + "\n\n"
                + "Repairing rewrites just these, in place, to point here. "
                  "Nothing new is created and nothing that is switched off is "
                  "switched on.")
        dialog = Adw.AlertDialog(heading="Repair Halo’s setup?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("repair", "Repair these")
        dialog.set_response_appearance("repair", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("repair")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_repair_response)
        self._dismiss_menu()
        dialog.present(self)

    def _on_repair_response(self, _dialog, response: str) -> None:
        if response != "repair":
            return
        # None, not [], when nothing is ticked: repair() reads that as "use
        # whatever is bound now", where [] would unbind the lot.
        fixed = DesktopIntegration.repair(self._ticked_keys() or None)
        GLib.idle_add(self._refresh_setup_status, False)
        if fixed:
            _notify(f"{APP_NAME}’s setup is repaired",
                    ", ".join(fixed) + " now point at " + str(SELF_PATH))
        else:
            _notify(f"{APP_NAME} could not repair everything",
                    "Try “Remove Halo’s setup”, then set it up again.")

    def _show_setup_report(self, items: list[dict]) -> None:
        """An account of what is on disk. Deliberately writes nothing at all."""
        lines = []
        for item in items:
            if item["present"]:
                lines.append(f"✓  {item['what']} — in place, points here"
                             f"\n     {item['where']}")
            else:
                lines.append(f"–  {item['what']} — switched off"
                             f"\n     {item['where']}")
        off = [i for i in items if not i["present"]]
        body = ("\n\n".join(lines) + "\n\n"
                + ("Everything above is in place and points at this copy of "
                   f"Halo:\n     {SELF_PATH}"
                   if not off else
                   "What is switched off is switched off because a switch in "
                   "this menu says so, not because anything is wrong — turn it "
                   "on there and it is written straight away. Everything that "
                   "is present points at this copy of Halo:\n"
                   f"     {SELF_PATH}"))
        dialog = Adw.AlertDialog(heading="Halo’s setup checks out", body=body)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self._dismiss_menu()
        dialog.present(self)

    def _on_setup_response(self, _dialog, response: str, keys: list[str]) -> None:
        if response != "apply":
            return
        DesktopIntegration.install_launcher()
        DesktopIntegration.set_autostart(True)
        # Only when there are keys. set_shortcuts([]) means "unbind everything",
        # which is not what an empty tick list in a setup dialog is asking for.
        ok = True
        if keys:
            CFG["shortcuts"] = keys
            ok = DesktopIntegration.set_shortcuts(keys)
        # sync_checks=False, for the reason _refresh_setup_status documents: a
        # GSettings read straight after a write can still answer with the old
        # value, and here that would untick the very keys just written. The two
        # switches are still synced from the filesystem, which does not lie.
        GLib.idle_add(self._refresh_setup_status, False)
        pretty = ", ".join(DesktopIntegration.accel_label(k) for k in keys)
        _notify(f"{APP_NAME} is set up",
                (f"Press {pretty} anywhere to search. See “{MANUAL_LABEL}” "
                 f"in the ⋯ menu." if ok and keys
                 else "Added to Applications and set to start at login. Add a "
                      "shortcut in Settings ▸ Keyboard if it did not appear."))

    def _on_autostart_toggled(self, state: bool) -> None:
        DesktopIntegration.set_autostart(state)
        # The summary line under "Set up Halo…" must follow, or it keeps
        # claiming "no autostart" after the switch has been flipped.
        GLib.idle_add(self._refresh_setup_status)

    def _on_launcher_toggled(self, state: bool) -> None:
        if state:
            DesktopIntegration.install_launcher()
        else:
            # Only the launcher and its icon — not the shortcuts, which are a
            # separate choice the user made.
            DesktopIntegration.remove_launcher()
        GLib.idle_add(self._refresh_setup_status)

    # ── the user's own keys, alongside the offered ones ──────────────────
    def _custom_accels(self) -> list[str]:
        """Keys that are the user's own rather than one of the presets.

        Two sources, deliberately merged: what was added in this menu, and any
        live Halo shortcut that is not a preset — which is how a shortcut created
        in Settings ▸ Keyboard turns into a row here instead of being invisible.
        """
        preset_ids = {accel_id(a) for a, _label in SHORTCUT_PRESETS}
        out: list[str] = []
        seen: set = set()
        for accel in (list(CFG["custom_shortcuts"])
                      + DesktopIntegration.active_shortcuts()):
            ident = accel_id(accel)
            mark = ident if ident else accel
            if ident in preset_ids or mark in seen:
                continue
            seen.add(mark)
            out.append(accel)
        return out

    def _rebuild_accel_rows(self) -> None:
        """Redraw the key list: presets first, then the user's own, each tickable.

        A custom row carries a tag and a remove button; a preset row carries
        neither, because a preset cannot be removed — only unticked. That is the
        whole visual difference, and it is the honest one: the rows you can
        delete are exactly the rows marked as yours.
        """
        while True:
            child = self.keys_box.get_first_child()
            if child is None:
                break
            self.keys_box.remove(child)
        self.accel_rows = []
        active = {accel_id(a) for a in (CFG["shortcuts"] or [])}
        listing = ([(a, label, False) for a, label in SHORTCUT_PRESETS]
                   + [(a, accel_label(a), True) for a in self._custom_accels()])
        for accel, label, custom in listing:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_start(10)
            row.set_margin_end(8)
            check = Gtk.CheckButton(label=label)
            check.set_active(accel_id(accel) in active)
            check.connect("toggled", self._on_accel_toggled, accel)
            row.append(check)
            if custom:
                tag = Gtk.Label(label="YOURS", valign=Gtk.Align.CENTER)
                tag.add_css_class("halo-tag")
                row.append(tag)
            warn = Gtk.Label(xalign=0.0, hexpand=True)
            warn.add_css_class("halo-warn")
            warn.set_ellipsize(Pango.EllipsizeMode.END)
            row.append(warn)
            if custom:
                drop = Gtk.Button(icon_name="window-close-symbolic",
                                  valign=Gtk.Align.CENTER)
                drop.set_tooltip_text(f"Remove {accel_label(accel)} from the list")
                drop.add_css_class("flat")
                drop.add_css_class("halo-drop")
                drop.connect("clicked", self._on_drop_custom, accel)
                row.append(drop)
            self.keys_box.append(row)
            self.accel_rows.append((accel, check, warn))

    def _on_drop_custom(self, _button, accel: str) -> None:
        """Forget one of the user's keys — from the list and from GNOME both.

        Removing it only from Halo's list would leave the key still bound and
        still opening Halo, with nothing on screen admitting it. So the dconf
        entry goes too, whichever slot it happens to live in.
        """
        wanted = accel_id(accel)
        same = (lambda a: accel_id(a) == wanted) if wanted else (lambda a: a == accel)
        CFG["custom_shortcuts"] = [a for a in CFG["custom_shortcuts"] if not same(a)]
        for binding in DesktopIntegration.foreign_bindings():
            if same(binding["accel"]):
                DesktopIntegration.drop_binding(binding["path"])
        CFG["shortcuts"] = [a for a in CFG["shortcuts"] if not same(a)]
        DesktopIntegration.set_shortcuts(CFG["shortcuts"])
        self._rebuild_accel_rows()
        GLib.idle_add(self._refresh_setup_status, False)

    # Held-down modifiers arrive as key presses of their own. Reporting
    # "Super" the moment Super goes down, before the letter that follows, made
    # the dialog look like it had already captured something.
    MODIFIER_KEYS = frozenset((
        Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
        Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R,
        Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
        Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_Caps_Lock, Gdk.KEY_Num_Lock,
        Gdk.KEY_Scroll_Lock, Gdk.KEY_Shift_Lock))

    @staticmethod
    def _usable_bare(keyval: int) -> bool:
        """May this key be a shortcut with no modifier at all?

        Only if pressing it alone cannot cost anything: a function key, or one of
        the XF86 extras a keyboard sends and nothing claims. A bare letter would
        be swallowed session-wide, which is not a thing to let anyone do by
        accident.
        """
        return (Gdk.KEY_F1 <= keyval <= Gdk.KEY_F35) or keyval >= 0x1008FF00

    def _on_add_shortcut(self, *_a) -> None:
        """Type in a key, see it named back, then confirm — as GNOME's panel does.

        Two details decide whether this works or merely looks like it does. The
        compositor has to be asked to stand aside, because the combinations worth
        binding are exactly the ones something already grabs: press Super+K
        without that and the overview opens over the top of the dialog. And the
        window's own key handler has to be told to keep its hands off, because it
        listens in the capture phase — above this dialog, not below it — so Escape
        would hide Halo and ↑ would open the history list instead of being read as
        the shortcut somebody is trying to set.
        """
        dialog = Adw.AlertDialog(
            heading="Add a shortcut",
            body="Press the combination that should open Halo.\n\n"
                 "It needs Ctrl, Alt, Shift or Super in it — unless it is a "
                 "function key, like the Copilot key.")
        shown = Gtk.Label(label="waiting for a key…", xalign=0.5)
        shown.add_css_class("halo-key")
        note = Gtk.Label(xalign=0.5, wrap=True)
        note.add_css_class("halo-dim")
        note.set_max_width_chars(40)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        holder.set_margin_top(8)
        holder.append(shown)
        holder.append(note)
        dialog.set_extra_child(holder)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_enabled("add", False)
        # A default response even though it starts disabled: without one the
        # dialog has nothing to focus, and a dialog that never takes focus never
        # receives the keystroke it exists to read.
        dialog.set_default_response("add")
        dialog.set_close_response("cancel")
        picked = {"accel": None}

        def capture(keyval: int, state) -> bool:
            # Escape and Enter stay the dialog's own, or there is no way out of
            # it that does not involve setting a shortcut.
            if keyval in (Gdk.KEY_Escape, Gdk.KEY_Return, Gdk.KEY_KP_Enter,
                          Gdk.KEY_ISO_Enter):
                return False
            if keyval in self.MODIFIER_KEYS:
                return True
            mods = state & Gtk.accelerator_get_default_mod_mask()
            lower = Gdk.keyval_to_lower(keyval)
            if lower != keyval:
                # Shift+7 arrives as "/" on this layout and as something else on
                # the next one. Store the unshifted key plus Shift, which is what
                # GNOME stores and what survives a layout change.
                mods |= Gdk.ModifierType.SHIFT_MASK
                keyval = lower
            accel = Gtk.accelerator_name(keyval, mods)
            shown.set_text(accel_label(accel) if accel else "?")
            if not accel or not Gtk.accelerator_valid(keyval, mods):
                note.set_text("Not a combination GNOME can store.")
                dialog.set_response_enabled("add", False)
                return True
            if not mods and not self._usable_bare(keyval):
                note.set_text("Add Ctrl, Alt, Shift or Super — on its own this "
                              "key would stop working everywhere else.")
                dialog.set_response_enabled("add", False)
                return True
            ident = accel_id(accel)
            known = ({accel_id(a) for a, _l in SHORTCUT_PRESETS}
                     | {accel_id(a) for a in self._custom_accels()})
            if ident in known:
                note.set_text("Already in the list above — tick it there.")
                dialog.set_response_enabled("add", False)
                return True
            clash = DesktopIntegration.find_conflicts(accel)
            if clash:
                area, what = clash[0]
                note.set_text(f"Already used by {area}: {what}. You can add it "
                              f"anyway; the list will keep flagging it.")
            else:
                note.set_text("Nothing else uses this.")
            picked["accel"] = accel
            dialog.set_response_enabled("add", True)
            return True

        def release() -> None:
            self._accel_capture = None
            self._accel_dialog = None
            surface = held.pop("surface", None)
            if surface is not None:
                try:
                    surface.restore_system_shortcuts()
                except Exception:
                    pass

        def finish(_dialog, response: str) -> None:
            release()
            if response != "add" or not picked["accel"]:
                return
            accel = picked["accel"]
            CFG["custom_shortcuts"] = list(CFG["custom_shortcuts"]) + [accel]
            CFG["shortcuts"] = list(CFG["shortcuts"]) + [accel]
            DesktopIntegration.set_shortcuts(CFG["shortcuts"])
            self._rebuild_accel_rows()
            GLib.idle_add(self._refresh_setup_status, False)

        held: dict = {}

        def grab() -> bool:
            """Give the dialog the keyboard, once it is mapped and the menu is gone.

            Everything here is about the dialog's *own* window, and that is the
            correction that made this work at all. AdwDialog only draws itself
            inside its parent when that parent is an AdwWindow or
            AdwApplicationWindow; HaloWindow is a plain GtkApplicationWindow, so
            libadwaita gives the dialog a toplevel of its own — measured, a
            separate 350x227 GdkSurface, not a widget inside the pill. Which
            means the pill's own key controller, which is what _on_key hangs off,
            is on the wrong window and never sees any of this.

            Two things follow. The inhibit has to be asked for on the dialog's
            surface, or the compositor stands aside for a window that is not
            receiving the keys. And the dialog's window has to be raised and
            activated exactly the way the manual's window is, because the pill
            holds _NET_WM_STATE_ABOVE and a new window opening over it is
            otherwise stacked underneath and left without focus.
            """
            if self._accel_dialog is not dialog:
                return False        # closed again before the idle came round
            native = dialog.get_native()
            if native is None:
                return False
            if isinstance(native, Gtk.Window):
                self._float_info_window(native)
            try:
                surface = native.get_surface()
                if surface is not None:
                    surface.inhibit_system_shortcuts(None)
                    held["surface"] = surface
            except Exception:
                pass
            return False

        # The controller that actually does the work, on the dialog itself,
        # because that is the widget in the toplevel the keystrokes go to. In the
        # capture phase so it reads the key before a response button treats it as
        # an activation — capture() hands Escape and Enter back for exactly that.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed",
                     lambda _c, keyval, _code, state: capture(keyval, state))
        dialog.add_controller(keys)
        # And _on_key keeps its hook as well: it costs nothing, it stops the pill
        # acting on a stray keystroke that does reach it, and it is what would
        # carry this if HaloWindow ever becomes an AdwApplicationWindow and the
        # dialog starts being drawn inside it after all.
        self._accel_capture = capture
        self._accel_dialog = dialog
        dialog.connect("response", finish)
        dialog.connect("closed", lambda *_a: release())
        self._dismiss_menu()
        dialog.present(self)
        GLib.idle_add(grab)

    def _on_accel_toggled(self, _check: Gtk.CheckButton, _accel: str) -> None:
        keys = [a for a, box, _ in self.accel_rows if box.get_active()]
        CFG["shortcuts"] = keys
        DesktopIntegration.set_shortcuts(keys)
        # sync_checks=False: trust the tick the user just made, not a read that
        # may not have caught up yet.
        GLib.idle_add(self._refresh_setup_status, False)

    # ── information ─────────────────────────────────────────────────────
    # Grouped rather than one flat run of twenty-odd rows, which read as a wall
    # and buried the useful half. The arrows appear under more than one heading on
    # purpose: what they do depends on which layer you are in, and that is the
    # thing worth learning, so each heading says it in its own context.
    KEY_HELP = [
        ("OPENING HALO", [
            ("Your shortcut", "Show or hide the pill"),
            ("…pressed twice", "Straight back to the panel and the query you "
                               "closed it with — the second press is the same "
                               "↓ that brings a parked page back"),
        ]),
        ("TYPING A SEARCH", [
            ("Enter", "Search — the pill unfolds into the results"),
            ("↑ / ↓", "Move through the suggestions"),
            ("→ or Tab", "Complete the highlighted suggestion"),
            ("Ctrl + L / Ctrl + K", "Back to the search field"),
        ]),
        ("YOUR PAST SEARCHES", [
            ("↑", "Open the list — keep typing to filter it"),
            ("Click the Google mark", "The same list"),
            ("↑ / ↓", "Walk it; ↑ off the top steps back out"),
            ("Shift + Delete", "Forget the highlighted one"),
        ]),
        ("THE RESULTS PANEL", [
            ("↑", "Collapse back to the pill"),
            ("↓", "Bring the results back — or click the round ↓ that appears "
                  "under the pill while a page is waiting behind it"),
            ("↑ in the page", "At the top of the page, hands the caret back to "
                              "the field; below it, scrolls as usual"),
            ("Click a result", "Opens here rather than in your browser"),
            ("Alt + ← / →", "Back / forward"),
            ("Mouse back / forward", "The side buttons, in the panel too"),
            ("Swipe sideways", "Two fingers on a touchpad, or one on a "
                               "touchscreen — right for back, left for "
                               "forward; a carousel or a wide table under the "
                               "pointer scrolls itself instead"),
            ("Ctrl + F", "Find in the page — the search field becomes the "
                         "find box; Enter and Shift+Enter step through it"),
            ("Ctrl + R", "Reload"),
            ("Ctrl + ↓ / ↑", "Grow / shrink the panel"),
            ("Drag below the panel", "Resize it by the bottom edge or either "
                                     "bottom corner — the border is just past "
                                     "the edge, as on any other window"),
            ("Ctrl + Shift + C", "Copy the current link"),
            ("Ctrl + Enter", "Hand the page to your browser — Halo steps aside"),
        ]),
        ("SEARCHING SOMETHING YOU ALREADY HAVE", [
            ("Middle-click the pill", "Search whatever is selected in any "
                                      "window — the X11 primary selection"),
            ("Middle-click a selection", "In the panel, searches that "
                                         "selection; Alt + ← comes back"),
            ("Drop text on the pill", "Drag a phrase out of any page to look "
                                      "it up"),
            ("Ctrl + Shift + V", "Paste and search in one key"),
            ("Ctrl + click a link", "Opens in your real browser; the panel "
                                    "stays put"),
        ]),
        ("SEARCHING A PICTURE", [
            ("Ctrl + Shift + S", "Circle to Search — grab a region of the screen"),
            ("Ctrl + U", "Search an image file with Lens"),
            ("Ctrl + V", "Search an image from the clipboard — copied text "
                         "still pastes as text"),
            ("Drop an image", "A file, or one dragged straight out of a page"),
        ]),
        ("THE WINDOW ITSELF", [
            ("Esc", "Hide Halo"),
            ("Drag the bar", "Move it — it stays above other windows"),
        ]),
    ]

    def _on_manual_link(self, _label, uri: str) -> bool:
        """The address in the manual, opened in the panel rather than a browser.

        Returning True stops GTK handing it to gtk_show_uri(), which is what
        would otherwise send it to Firefox — the one link in Halo that left.
        """
        if uri == PROJECT_URL:
            self._on_open_project()
            return True
        return False

    def _on_open_project(self, *_a) -> None:
        """Open the project's page in Halo's own panel.

        In Halo rather than the browser because the panel IS a browser, and
        sending the user out to Firefox to look at the app they are already
        holding would be the one link in the app that behaves differently from
        every other. Ctrl+Enter still hands it to the real browser from there,
        the same as any page.
        """
        if not self._ensure_webview():
            _notify(APP_NAME, "Could not start the web view.")
            return
        self.show_popup()
        self._set_busy(True)
        self._navigate(PROJECT_URL)
        self._show_loading("Opening GitHub…")
        self.expand()

    def _on_show_info(self, *_a) -> None:
        """A window that accounts for every file Halo creates, and how to undo
        each one from a GUI."""
        # Reuse the existing window rather than stacking duplicates, and keep a
        # reference so it cannot be collected while open.
        if getattr(self, "_info_window", None) is not None:
            self._info_window.present()
            return
        win = Gtk.Window(title=f"{APP_NAME} {VERSION} — manual")
        win.set_transient_for(self)
        win.set_default_size(680, 640)
        win.set_modal(False)
        self._info_window = win
        win.connect("close-request", self._on_info_closed)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(20)
        outer.set_margin_end(20)
        scroller.set_child(outer)
        win.set_child(scroller)

        def section(text: str) -> None:
            lbl = Gtk.Label(label=text, xalign=0.0)
            lbl.add_css_class("halo-title")
            lbl.set_margin_top(16)
            outer.append(lbl)

        def para(text: str, dim: bool = False) -> None:
            lbl = Gtk.Label(label=text, xalign=0.0, wrap=True)
            lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if dim:
                lbl.add_css_class("halo-dim")
            outer.append(lbl)

        # ── whose it is, and where it lives ──────────────────────────────
        # Three lines, not a sentence: a name, an author and an address are what
        # someone opening this window scans for, and prose makes all three
        # harder to find than they are apart.
        title = Gtk.Label(xalign=0.0)
        title.set_markup(
            f"<span size='xx-large' weight='bold'>{GLib.markup_escape_text(APP_NAME)}</span>"
            f"  <span size='small' alpha='55%'>{GLib.markup_escape_text(VERSION)}</span>")
        outer.append(title)

        author = Gtk.Label(label=AUTHOR, xalign=0.0)
        author.add_css_class("halo-dim")
        outer.append(author)

        link = Gtk.Label(xalign=0.0)
        # The scheme is not shown: it is the one part of an address nobody reads,
        # and the label is narrower without it. The href keeps it.
        link.set_markup(
            f"<a href='{GLib.markup_escape_text(PROJECT_URL)}'>"
            f"{GLib.markup_escape_text(PROJECT_URL.split('://', 1)[-1])}</a>")
        link.set_margin_top(2)
        link.connect("activate-link", self._on_manual_link)
        outer.append(link)

        section("KEYBOARD")
        live = DesktopIntegration.active_shortcuts()
        para("Opens Halo from anywhere: "
             + (", ".join(DesktopIntegration.accel_label(a) for a in live)
                if live else "no global shortcut set yet"), dim=True)
        para("Tick the keys you want in the ⋯ menu, or press “Add a shortcut…” "
             "there to use any combination of your own — it joins the list with "
             "a YOURS tag, and only tagged keys can be removed. Shortcuts you "
             "add or change in Settings ▸ Keyboard show up in that list too, as "
             "soon as you make them.", dim=True)
        # One grid for every group, with the headings spanning both columns, so
        # all the descriptions line up down the page. A grid per group let each
        # size its own key column, and the second column then started at a
        # different place in every section.
        grid = Gtk.Grid(column_spacing=18, row_spacing=4)
        grid.set_margin_top(6)
        line = 0
        for group, rows in self.KEY_HELP:
            head = Gtk.Label(label=group, xalign=0.0)
            head.add_css_class("halo-dim")
            head.set_margin_top(14 if line else 4)
            head.set_margin_bottom(2)
            grid.attach(head, 0, line, 2, 1)
            line += 1
            for keys, what in rows:
                k = Gtk.Label(label=keys, xalign=0.0, yalign=0.0)
                k.add_css_class("halo-key")
                grid.attach(k, 0, line, 1, 1)
                # Wrapped and capped: one of these runs long, and an uncapped
                # label would set the whole window's width from its single line.
                what_lbl = Gtk.Label(label=what, xalign=0.0, yalign=0.0,
                                     wrap=True)
                what_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                what_lbl.set_max_width_chars(46)
                grid.attach(what_lbl, 1, line, 1, 1)
                line += 1
        outer.append(grid)

        section("WHAT HALO PUTS ON YOUR SYSTEM")
        para("Everything is a plain file in your home folder. No package is "
             "installed, no system directory is written, and root is never "
             "used. Paths can be selected and copied.", dim=True)
        for what, where, present, undo in DesktopIntegration.installed_items():
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            row.set_margin_top(9)
            # Words, not ballot-box glyphs: those are missing from many fonts and
            # render as empty placeholder squares.
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            head.append(Gtk.Label(label=what, xalign=0.0))
            state = Gtk.Label(label="present" if present else "not created",
                              xalign=0.0)
            state.add_css_class("halo-ok" if present else "halo-dim")
            head.append(state)
            row.append(head)
            loc = Gtk.Label(label=where, xalign=0.0, wrap=True, selectable=True)
            loc.set_wrap_mode(Pango.WrapMode.CHAR)
            loc.add_css_class("halo-path")
            loc.set_margin_start(2)
            # Selectable labels highlight their whole text the moment they take
            # focus, so the first path would open pre-selected in blue. Dropping
            # focusability keeps mouse selection for copying without that.
            loc.set_focusable(False)
            row.append(loc)
            how = Gtk.Label(label="Remove: " + undo, xalign=0.0, wrap=True)
            how.set_margin_start(2)
            how.add_css_class("halo-dim")
            row.append(how)
            outer.append(row)

        section("REMOVING IT ALL")
        para("The ⋯ menu has “Remove Halo’s setup”, which deletes the launcher, "
             "the icon, the start-at-login entry and the shortcuts in one go, "
             "and can delete your settings and browsing data too. After that, "
             "deleting halo.py itself leaves nothing behind.", dim=True)
        para("The shortcuts are ordinary GNOME custom shortcuts, so they are "
             "also visible and editable at Settings ▸ Keyboard ▸ View and "
             "Customize Shortcuts ▸ Custom Shortcuts.", dim=True)

        # Selectable labels select their whole text when focused, so the first
        # path would open highlighted in blue. Start with nothing focused.
        #
        # The manual also has to be told to float: the pill holds
        # _NET_WM_STATE_ABOVE, so an ordinary window opening on top of it is drawn
        # underneath it instead, and reading the manual meant dragging the pill
        # out of the way first.
        win.connect("map", lambda *_: (win.set_focus(None),
                                       self._float_info_window(win)))

        section("PRIVACY")
        para("Halo talks to Google and nothing else: search, autocomplete "
             "suggestions as you type, and Lens when you search an image. There "
             "is no telemetry and no update check. Cookies are kept so the "
             "consent banner and CAPTCHAs stay away, and can be cleared from "
             "the ⋯ menu at any time.", dim=True)

        section("WHY IT BEHAVES THIS WAY")
        for title, text in (
            ("The user agent is not spoofed",
             "This is the whole anti-CAPTCHA story. Claiming to be Chrome or "
             "Firefox while running the WebKit engine is a fingerprint "
             "contradiction, and Google answers it with an instant CAPTCHA on "
             "the very first search. WebKit's own Safari-family agent passes."),
            ("Dark mode is real, not a filter",
             "Google ignores prefers-color-scheme on the server: a cold search "
             "returns a light page even with the dark media query set. It "
             "detects the scheme with JavaScript once, on its homepage, and "
             "remembers it in a cookie. So on first launch Halo visits Google "
             "in an offscreen view, which also answers the consent banner with "
             "“Reject all”. That is why the very first search waits a moment."),
            ("It runs on XWayland on purpose",
             "Wayland forbids a window from positioning itself or staying on "
             "top; X11 allows both. That buys mouse-aware placement, real "
             "always-on-top and presence on every workspace — and it costs "
             "nothing in looks: the window is a 32-bit ARGB toplevel, so its "
             "rounded corners and the invisible resize border below the pill "
             "are real transparency, not a cut-out."),
            ("Lens uses Google's own upload form",
             "Posting to Lens' upload endpoint directly fails even with valid "
             "cookies. Halo instead clicks the page's own file input and "
             "answers WebKit's file-chooser request with your image, so "
             "Google's JavaScript performs the upload."),
            ("The way back from a collapsed panel is a window of its own",
             "Collapsing leaves the page loaded and one keystroke away, and "
             "nothing on screen said so — ↓ was in the manual and nowhere else. "
             "A chip in the pill would say it, and the pill is the one place "
             "with no room: every chip on that row costs the query width, "
             "permanently, to advertise a state that only sometimes exists. So "
             "the button detaches instead — twenty-four pixels across, cut to a "
             "circle, parked under the pill's bottom-right corner, which is "
             "where the collapse chip was standing a moment earlier. It takes "
             "no space in the pill because it is not in the pill, it never "
             "takes the keyboard, and it exists only while there is a page to "
             "go back to. Switch it off under “Floating ↓ button”."),
            ("The engine is not started until you search",
             "A copy sitting in the background has no use for a browser engine. "
             "Measured, the web view and the network process it spawns are about "
             "56MB of the daemon's resident memory — it idles at 78MB instead of "
             "134MB — and building them takes 58ms, which lands behind a keypress "
             "and a network round trip rather than in front of the popup."),
            ("The keybind does not load the toolkit",
             "The shortcut runs a second copy of halo.py whose only job is to "
             "nudge the resident one. Loading GTK, libadwaita and WebKitGTK for "
             "that cost about 0.4s between the keypress and the popup, so it now "
             "makes the same request over D-Bus with Gio alone, in about half the "
             "time. If the resident copy is too old to understand it, the request "
             "falls back to the old route rather than doing nothing."),
            ("The setup button offers three different things",
             "Every switch in the ⋯ menu writes its own file the moment it is "
             "flipped, so by the time anyone presses the button at the top there "
             "is usually nothing left to create — and it used to answer with a "
             "dialog headed “Halo is already set up”, which is a button whose "
             "only message is that it was not needed. It now reads what is "
             "actually on disk and offers whichever job fits: setting Halo up "
             "when nothing is there, repairing what has gone stale, or simply "
             "accounting for it all and writing nothing. Only the first two are "
             "painted as something to do."),
            ("Shortcuts are read from GNOME, not remembered instead of it",
             "The keys live in the same dconf entries GNOME's keyboard panel "
             "writes, so they can be added, re-bound or deleted there with Halo "
             "not involved. Halo watches those entries and follows them rather "
             "than overwriting them the next time its menu opens — a key that "
             "turns up this way appears in the list as one of yours. It also "
             "recognises a shortcut by the command it runs rather than by the "
             "slot GNOME filed it in, which is how one made by hand is found at "
             "all: those go to “custom0”, not to Halo's own “halo0”."),
            ("Replacing halo.py retires the copy that is running",
             "A resident copy goes on running the code it started with, so "
             "dropping a newer halo.py in place would otherwise change nothing "
             "until the next login. Halo watches its own file and steps down when "
             "it changes — never while the popup is open — and the next keypress "
             "starts the new version."),
            ("The results panel is painted in Google's own colour",
             "The page sits on the pill's own frosted gradient, so that "
             "gradient showed through everything Google's page does not paint "
             "itself — most visibly the scrollbar gutter, which turned into a "
             "strip of gradient running down the side of the results. Rather "
             "than override Google's own rotating selectors, Halo asks the page "
             "what colour it is painting and matches the toolbar, the loading "
             "cover and the view's own base to it. The panel is one flat sheet, "
             "and it follows Google if that shade ever changes."),
            ("Links open in the panel, not in your browser",
             "Pushing every non-Google link out to a browser made a result page "
             "a dead end: one click and the popup being read vanished, taking "
             "its always-on-top panel with it, and coming back meant searching "
             "again. It is a browser engine with history, a reload and an "
             "address of its own, so it browses — and the ways out stay "
             "deliberate: Ctrl+Enter or ⤢ hands the page to your real browser, "
             "Alt+← comes back. Only what the panel genuinely cannot show still "
             "leaves on its own: a mailto: or another app's link, and a file to "
             "download, which needs somewhere to put it."),
            ("The cover comes off before the page has finished loading",
             "A results page reports “finished” only once every image, "
             "font and beacon has arrived, but Google renders the results "
             "themselves on the server and they are readable long before that. "
             "Halo lifts its loading cover as soon as the document is mostly "
             "parsed, and never holds it longer than a fixed deadline — an "
             "opaque cover over live results is worse than a brief flash. What it "
             "will not do is lift onto a page that is not there yet: WebKit goes "
             "on painting the previous page until the new one commits, and the "
             "old page's own parting events arrive after the cover goes up. "
             "Acting on those was what flashed the previous search on screen for "
             "a moment after pressing Enter."),
        ):
            head = Gtk.Label(label=title, xalign=0.0)
            head.set_margin_top(9)
            outer.append(head)
            para(text, dim=True)

        section("IF SOMETHING MISBEHAVES")
        for text in (
            "A shortcut does nothing — check its row above for a ⚠ conflict "
            "marker. GNOME stores two actions on one key without warning and "
            "then picks a winner unpredictably.",
            "Halo was moved or renamed — nothing to do. The launcher, the "
            "start-at-login entry and the shortcuts all have to record an "
            "absolute path, so moving the file breaks all three at once. Halo "
            "checks its own paperwork a second after it starts and re-points "
            "whatever is wrong, then says so. If it could not, the ⋯ menu's "
            "button reads “Repair Halo’s setup…” and names what is stale.",
            "Results look light instead of dark — the first-launch handshake "
            "did not finish. Use “Clear cookies and cache”, relaunch, and give "
            "it a few seconds.",
            "Google shows a CAPTCHA — solve it once and the cookie is kept. A "
            "shared or VPN address makes this likelier and is out of Halo's "
            "hands.",
            "The pointer looks the wrong size — an X11 window gets one cursor "
            "size for the whole session, and GNOME sets it from the primary "
            "monitor, so on screens of different scale it is right on one and "
            "wrong on the others. Halo asks GNOME what each screen's real scale "
            "is and sizes its own pointer per monitor to match. If it still "
            "looks wrong, it is following the cursor-size in Settings ▸ "
            "Accessibility, which you can change there.",
        ):
            para("·  " + text, dim=True)

        section("COMMAND LINE")
        for line, what in (("halo.py", "show the popup"),
                           ("halo.py --toggle", "show or hide (what the shortcut runs)"),
                           ("halo.py --daemon", "start hidden and stay resident"),
                           ("halo.py -q \"query\"", "search immediately"),
                           ("halo.py --version", "print the version")):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            cmd = Gtk.Label(label=line, xalign=0.0, width_request=190)
            cmd.add_css_class("halo-key")
            row.append(cmd)
            note = Gtk.Label(label=what, xalign=0.0)
            note.add_css_class("halo-dim")
            row.append(note)
            outer.append(row)

        win.present()

    @staticmethod
    def _float_info_window(win: Gtk.Window) -> None:
        """Lift the manual above the pill, which is itself above everything."""
        if not ON_X11 or not WM.ok:
            return
        try:
            surface = win.get_surface()
            if surface is None:
                return
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                xid = surface.get_xid()
        except Exception:
            return
        if xid:
            WM.keep_above(xid)
            WM.activate(xid)

    def _on_info_closed(self, *_a) -> bool:
        self._info_window = None
        return False        # let it close

    def _on_remove_setup(self, *_a) -> None:
        items = [f"·  {what}" for what, _w, present, _u
                 in DesktopIntegration.installed_items()[:4] if present]
        body = ("This removes:\n\n" + ("\n".join(items) if items
                                       else "·  nothing is currently installed")
                + "\n\nHalo keeps running, and halo.py itself is left alone — "
                  "delete that file yourself when you no longer want it."
                # Said plainly because the short button underneath cannot: what
                # "keep" keeps is not only preferences. It keeps the cookie jar,
                # and the cookie jar IS the signed-in session. The button used to
                # read "keep my settings", which names the smallest of the four
                # things it spares and quietly leaves a Google login on disk for
                # anyone who chose it meaning "keep my preferences".
                + "\n\nKeeping your data keeps your settings, your search "
                  "history, and the cookies that keep you signed in. Removing "
                  "everything deletes all three.")
        dialog = Adw.AlertDialog(heading="Remove Halo's setup?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("keep", "Remove, keep my data")
        dialog.add_response("all", "Remove everything")
        dialog.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_remove_response)
        self._dismiss_menu()
        dialog.present(self)

    def _on_remove_response(self, _dialog, response: str) -> None:
        if response not in ("keep", "all"):
            return
        removed = DesktopIntegration.remove_setup(include_data=(response == "all"))
        if response == "all" and self.session is not None:
            try:
                self.session.get_cookie_manager().delete_all_cookies()
            except Exception:
                pass
        self._refresh_setup_status()
        _notify(f"{APP_NAME} setup removed",
                ("Removed: " + ", ".join(removed)) if removed
                else "There was nothing left to remove.")

    # 0 is "keep for ever". Two months is the default because it is long enough
    # to still hold what you looked up at the start of a project, and short
    # enough that the list is not an archive of everything you ever wondered.
    HISTORY_RETENTIONS = [(0, "Keep for ever"), (7, "1 week"), (14, "2 weeks"),
                          (30, "1 month"), (60, "2 months"), (90, "3 months"),
                          (180, "6 months"), (365, "1 year")]

    # 10 minutes by default: long enough that closing the pill to read something
    # and coming back is free, short enough that a resident copy is not sitting on
    # a browser engine all day. 0 keeps it for ever, which is what every version
    # before this did.
    IDLE_RELEASE_PRESETS = [(0, "Keep it resident"), (2, "2 minutes"),
                            (5, "5 minutes"), (10, "10 minutes"),
                            (30, "30 minutes"), (60, "1 hour"), (240, "4 hours")]

    def _on_reveal_changed(self, picker: Gtk.DropDown, _p) -> None:
        idx = picker.get_selected()
        if not (0 <= idx < len(self.reveal_options)):
            return          # GTK_INVALID_LIST_POSITION, while the model changes
        CFG["reveal_ms"] = self.reveal_options[idx][0]
        self._apply_reveal_speed()

    def _on_idle_release_changed(self, picker: Gtk.DropDown, _p) -> None:
        idx = picker.get_selected()
        if not (0 <= idx < len(self.idle_options)):
            return          # GTK_INVALID_LIST_POSITION, while the model changes
        CFG["idle_release_min"] = self.idle_options[idx][0]
        # Re-time from now. The menu can only be reached with the pill on screen,
        # so in practice hide_popup() is what starts the new clock.
        self._cancel_idle_release()
        if not self.get_visible():
            self._arm_idle_release()

    def _on_release_now(self, *_a) -> None:
        if self.web is None:
            _notify(APP_NAME, "There is no browser engine loaded to release.")
            return
        blocked = self._release_blocked()
        if blocked:
            _notify(APP_NAME, f"Not just now — {blocked}.")
            return
        if self.expanded:
            self.collapse()
            # Let the panel finish sliding shut before the page vanishes out of it.
            GLib.timeout_add(300, self._release_now_settled)
            return
        self._release_now_settled()

    def _release_now_settled(self) -> bool:
        if self.release_engine():
            _notify(APP_NAME, "Browser engine released. The next search builds a "
                              "new one in a few milliseconds.")
        return False

    def _on_history_toggled(self, state: bool) -> None:
        CFG["history"] = state
        # Switching it off stops recording; it does not delete what is already
        # there, because those are two different requests and only one of them
        # was made. "Forget all searches" is right underneath for the other one.
        if self.history_open:
            self._fill_history()
            self._track_geometry()

    def _on_history_suggest_toggled(self, state: bool) -> None:
        CFG["history_in_suggestions"] = state
        # Put the current text back through the new rule, so the switch shows its
        # effect without waiting for another keystroke.
        self._on_entry_changed()

    def _on_retention_changed(self, picker: Gtk.DropDown, _p) -> None:
        idx = picker.get_selected()
        if not (0 <= idx < len(self.retention_options)):
            return          # GTK_INVALID_LIST_POSITION, while the model changes
        days = self.retention_options[idx][0]
        CFG["history_days"] = days
        # Applied now rather than at the next start: a retention that visibly
        # does nothing until a restart reads as a setting that did not take.
        if HISTORY.prune(days):
            HISTORY.flush()
            if self.history_open:
                self._fill_history()
                self._track_geometry()

    def _on_forget_all(self, *_a) -> None:
        count = len(HISTORY.items)
        HISTORY.clear()
        if self.history_open:
            self._fill_history()
            self._track_geometry()
        _notify(APP_NAME, f"Forgot {count} searches." if count
                else "There was nothing to forget.")

    def _on_parked_arrow_toggled(self, state: bool) -> None:
        CFG["parked_arrow"] = state
        self._update_arrow(animate=state)

    def _on_suggestions_toggled(self, state: bool) -> None:
        """Live autocomplete, applied to the field as it stands.

        The switch used to be a bare write to the config, so flipping it while
        a query was already typed changed nothing until the next keystroke: off
        left the list sitting there, and on left the field bare until a
        character was added and the whole thing looked like a switch that had
        not taken. Its neighbour "Show them while typing" was fixed for exactly
        this a version ago and this one was missed — so it is the same line.
        """
        CFG["suggestions"] = state
        self._on_entry_changed()

    def _on_safe_search_toggled(self, state: bool) -> None:
        """SafeSearch, applied to the results already on screen.

        Same finding as the two switches above, and it needs a different
        answer. Compact results is a stylesheet, so reloading is enough;
        SafeSearch is a parameter on the ADDRESS — `safe=active`, which
        search_url() adds — so the page in the view was fetched under the old
        rule and refetching it would fetch the same page again. The search has
        to be asked for afresh.

        The same words, and the same half of a corrected pair: a panel sitting
        on the verbatim results of a misspelling must not be quietly moved onto
        the corrected ones by a switch about adult content. _no_correction()
        reads which half is on screen and search_url() puts it back.

        Only a Google results page, because only a Google results page has a
        query to re-run — SafeSearch means nothing to a site the panel has
        browsed to, and reloading one for this would be a switch reaching
        somewhere it was never about.
        """
        CFG["safe_search"] = state
        if not (self.expanded and self.web is not None):
            return
        here = self.web.get_uri() or ""
        query = google_query(here)
        if not query:
            return
        self._set_busy(True)
        self._navigate(search_url(query, verbatim=_no_correction(here)))
        self._show_loading("Searching…")

    def _on_compact_toggled(self, state: bool) -> None:
        CFG["compact_results"] = state
        self._apply_user_styles()
        if self.expanded:
            self._reload()

    def _on_clear_data(self, *_a) -> None:
        # Build the session if it does not exist yet, so clearing means the same
        # thing whether or not this copy has searched.
        if not self._ensure_webview():
            _notify(APP_NAME, "Could not open the cookie store.")
            return
        try:
            self.session.get_cookie_manager().delete_all_cookies()
        except Exception:
            pass
        try:
            self.session.get_website_data_manager().clear(
                WebKit.WebsiteDataTypes.ALL, 0, None, None, None)
        except Exception:
            pass
        CFG["warmed_up"] = False
        _notify(APP_NAME, "Cookies and cache cleared.")


# ══════════════════════════════════════════════════════════════════════════
#  Application
# ══════════════════════════════════════════════════════════════════════════

class HaloApp(Adw.Application):

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window: HaloWindow | None = None
        self._warm_view: WebKit.WebView | None = None
        # Whether that view has reached the end of its load and is inside
        # settle()'s wait. _abandon_warm_up() reads it to tell a handshake that
        # is nearly done from one that is never going to finish.
        self._warm_settling = False
        self._source_monitor = None
        self._retiring = False
        self.add_main_option("daemon", 0, GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Start hidden and stay resident", None)
        self.add_main_option("toggle", 0, GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Show or hide the popup", None)
        self.add_main_option("query", ord("q"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING, "Search immediately", "TEXT")
        self.add_main_option("version", 0, GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Print version", None)

    # ── lifecycle ───────────────────────────────────────────────────────
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Adw.init()
        # Google keys its dark theme off prefers-color-scheme, which WebKit
        # derives from the GTK theme. Forcing dark here is what makes the
        # results render natively dark.
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        load_css()
        self._export_actions()
        self._watch_own_source()
        # Not on the critical path: a shortcut press wants the popup, not a dconf
        # write. A second in is long after the window is up and long before
        # anyone has gone looking for the menu.
        GLib.timeout_add_seconds(1, self._repair_stale_setup)
        # The last search or two may only exist in the debounced write.
        def _bow_out(*_a) -> None:
            HISTORY.flush()
            if self.window is not None:
                self.window._cancel_arrow_soon()
                self.window._arrow.destroy()
        self.connect("shutdown", _bow_out)

    def _export_actions(self) -> None:
        """Expose what we do as D-Bus actions, for the launcher's fast path.

        These are what `halo.py --toggle` drives without loading GTK at all (see
        _handle_without_gui). They are additions, not replacements: the full
        command line still works, so a mixture of old and new copies behaves.
        """
        def add(name: str, handler, param: str | None = None) -> None:
            action = Gio.SimpleAction.new(
                name, GLib.VariantType(param) if param else None)
            action.connect("activate", handler)
            self.add_action(action)

        add("toggle", lambda *_: self._ensure_window().toggle())
        add("show", lambda *_: self._ensure_window().show_popup())
        add("quit", lambda *_: self.quit())
        add("search", self._on_search_action, "s")

    def _on_search_action(self, _action, param) -> None:
        window = self._ensure_window()
        window.show_popup()
        # Words that came from outside the field, so they are the search rather
        # than a refinement of whatever picture was left on it. See
        # HaloWindow._search_text() for the whole of that argument.
        window.detach_image()
        window.entry.set_text(param.get_string() if param else "")
        window.run_search()

    # ── keeping our own paperwork pointed at us ─────────────────────────
    def _repair_stale_setup(self) -> bool:
        """Re-point anything that still names a halo.py which has since moved.

        Every piece of setup has to record an absolute path, because that is the
        only thing a .desktop file or a dconf command can name. So moving or
        renaming halo.py breaks the launcher, the start-at-login entry and the
        shortcut all at once, and breaks them silently: the files are all still
        there, the key still appears in Settings ▸ Keyboard, and pressing it does
        nothing whatsoever. The manual used to answer this with "run Set up Halo
        again from its new location", which is a fair instruction and no use at
        all to someone who does not know that is what happened.

        There is nothing to ask. The right path is not in doubt — it is the file
        this process is running from — and every one of these is something the
        user already asked for; re-pointing it keeps a promise rather than making
        a new one. Nothing absent is created, so a switch that is off stays off,
        and if there is nothing stale this does nothing and says nothing.
        """
        try:
            fixed = DesktopIntegration.repair()
        except Exception:
            return False
        if fixed:
            _notify(f"{APP_NAME} repaired its own setup",
                    f"{SELF_PATH.name} has moved, so " +
                    ", ".join(f.lower() for f in fixed) +
                    f" now point at {SELF_PATH}.")
        return False        # once per start is enough

    # ── stepping aside for a newer copy of ourselves ────────────────────
    def _watch_own_source(self) -> None:
        """Retire when halo.py itself is replaced, so an update takes effect.

        A resident copy goes on running the code it started with. Drop a new
        halo.py in place over an older one and the shortcut would keep running the
        old build until the next login — the update appears to have done nothing,
        and any new fix looks broken. Watching our own file and bowing out means
        the next keypress starts the new code.
        """
        self._source_monitor = None
        try:
            monitor = Gio.File.new_for_path(str(SELF_PATH)).monitor_file(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
        except Exception:
            return
        monitor.connect("changed", self._on_source_changed)
        self._source_monitor = monitor

    def _on_source_changed(self, _monitor, _file, _other, event) -> None:
        # CHANGES_DONE_HINT covers a copy in place; the move events cover editors
        # and package tools that write a new file and rename it over the old one.
        if event not in (Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                         Gio.FileMonitorEvent.DELETED,
                         Gio.FileMonitorEvent.MOVED_OUT,
                         Gio.FileMonitorEvent.MOVED_IN,
                         Gio.FileMonitorEvent.RENAMED,
                         Gio.FileMonitorEvent.CREATED):
            return
        if self._retiring:
            return
        self._retiring = True
        self._retire_when_idle()

    def _retire_when_idle(self) -> bool:
        """Quit — but never out from under someone who is mid-search."""
        window = self.window
        if window is not None and window.get_visible():
            GLib.timeout_add_seconds(5, self._retire_when_idle)
            return False
        self.quit()
        return False

    def _ensure_window(self) -> HaloWindow:
        if self.window is None:
            self.window = HaloWindow(self)
            if not CFG["warmed_up"]:
                # Searches queue behind the handshake; see run_search().
                self.window.warm_pending = True
                GLib.timeout_add_seconds(1, self._warm_up)
                # Never leave the user waiting on a network that is not there.
                GLib.timeout_add_seconds(12, self._abandon_warm_up)
        return self.window

    def warm_view_live(self) -> bool:
        """Is the first-run handshake still holding the window's network session?

        Asked before the engine is released, because tearing the session out from
        under the handshake would mean doing the whole thing again — and the
        handshake is what buys dark mode and answers the consent banner.
        """
        return self._warm_view is not None

    def _abandon_warm_up(self) -> bool:
        """The handshake's deadline: let the queued search go, and let go of the
        view that was holding it up.

        Releasing only the search was half the job, and the missing half is the
        expensive one. `_warm_view` is a second WebView — engine, web process,
        network process and all — and nothing else ever clears it: settle() does,
        four seconds after a load that FINISHES, and a load that never finishes
        never reaches it. A captive portal, a DNS server that accepts the
        connection and then says nothing, a laptop opened somewhere with no
        network: each of them leaves the view standing for the rest of the
        session, and the twelve-second net that exists for exactly those cases
        was letting it stand.

        Two costs, and the second is the one that gets reported as "it just keeps
        growing". The view itself is resident memory nobody can see or reach —
        measured in _ensure_webview(), a view and its processes are ~68MB. And
        `warm_view_live()` is what `_release_blocked()` asks first, so while it
        answers True the idle release is refused every single time it is tried,
        at 60s intervals, for ever — and the ~244MB that handing the engine back
        recovers is never recovered at all. The setting says "release the engine
        after ten minutes" and it silently never does.
        """
        if self.window is not None and self.window.warm_pending:
            self.window.resume_after_warm()
        if self._warm_view is not None and not self._warm_settling:
            # Only a handshake that has not reached FINISHED. One that has is
            # inside settle()'s own four-second wait, where the cookies the
            # whole exercise went to fetch are still being written, and cutting
            # that short would throw away the thing it is for.
            view, self._warm_view = self._warm_view, None
            # stop_loading first: terminating the process under a live load is
            # what the engine reports as a crash rather than a cancellation.
            for call in ("stop_loading", "terminate_web_process"):
                try:
                    getattr(view, call)()
                except Exception:
                    pass
        return False

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict()
        if options.contains("version"):
            command_line.print_literal(f"{APP_NAME} {VERSION}\n")
            return 0

        window = self._ensure_window()

        if options.contains("query"):
            text = options.lookup_value("query", GLib.VariantType("s")).get_string()
            window.show_popup()
            window.detach_image()       # -q names its own words; see _search_text
            window.entry.set_text(text)
            window.run_search()
            return 0
        if options.contains("daemon"):
            # Realize off-screen so the first real invocation is instant, but
            # never show anything at login.
            window.set_visible(False)
            return 0
        if options.contains("toggle"):
            window.toggle()
            return 0

        window.show_popup()
        return 0

    def do_activate(self) -> None:
        self._ensure_window().show_popup()

    # ── one-time invisible consent warm-up ──────────────────────────────
    def _warm_up(self) -> bool:
        """Negotiate with Google once, in a throwaway offscreen WebView.

        This does two jobs that a first-ever search cannot do for itself:

        * answers the EU consent banner with "Reject all", and
        * earns dark mode. Google does NOT honour prefers-color-scheme on the
          server; it detects the scheme with JavaScript on the homepage and
          remembers the answer in its cookies. Verified: a cold search comes
          back light even with the dark media query set, while any search after
          this warm-up comes back dark.

        The view is never parented, which is fine — WebKit loads and runs user
        scripts regardless — so it cannot disturb whatever the user is doing.
        """
        if CFG["warmed_up"] or self.window is None:
            return False
        window = self.window
        # The handshake is the one thing that needs the engine before the user has
        # asked for anything — it is what earns dark mode and dismisses consent.
        if not window._ensure_webview():
            return False

        ucm = WebKit.UserContentManager()
        ucm.add_script(WebKit.UserScript.new(
            CONSENT_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.END, None, None))
        warm = WebKit.WebView(network_session=window.session, user_content_manager=ucm)
        self._warm_view = warm          # keep alive until it has finished

        def on_load(view, event) -> None:
            if event != WebKit.LoadEvent.FINISHED:
                return
            view.disconnect(handler)
            # From here the handshake is finishing rather than hanging, so the
            # deadline below must not pull the view out from under it.
            self._warm_settling = True

            def settle() -> bool:
                CFG["warmed_up"] = True
                self._warm_view = None
                self._warm_settling = False
                # Release any search the user typed while we were handshaking.
                window.resume_after_warm()
                # Belt and braces: if a light results page somehow got loaded
                # first, refresh it now that dark mode is agreed.
                if (window.web is not None
                        and "google.com/search" in (window.web.get_uri() or "")):
                    window.web.reload()
                return False

            GLib.timeout_add_seconds(4, settle)

        handler = warm.connect("load-changed", on_load)
        warm.load_uri("https://www.google.com/")
        return False


def main() -> int:
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    return HaloApp().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
