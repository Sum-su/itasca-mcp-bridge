# -*- coding: utf-8 -*-
"""Start the bridge automatically whenever an ITASCA product starts.

The guide's "open the IPython console and type two lines" is fine once. It
is also why a bridge is only up when someone remembers to start it, which
is the wrong default for a server whose whole point is that a client can
connect to it. This module is the supported way to make the bridge part of
the product's own startup.

Why ``sitecustomize``
---------------------
``exe64/addon.py`` looks like the extension point and is not: nothing
reads it. Measured with a marker-file probe at the top of that file, the
marker never appears even with the GUI fully initialised and a project
loaded, and ``addon.py`` occurs zero times in the product executables.

``sitecustomize.py``, dropped into the product's embedded Python
(``exe64/python36/Lib/site-packages/``), really is imported -- CPython's
``site`` module imports that name automatically at every interpreter
startup. ``install()`` writes one there.

Why the file is a shim and not a copy
-------------------------------------
The shim is four lines that import this module and call :func:`boot`. All
the logic below stays versioned, testable and upgradable with the package;
a copy would freeze at whatever release first wrote it.

The three things a naive hook gets wrong
----------------------------------------
At ``sitecustomize`` time neither of ``start()``'s preconditions holds, and
calling it right there raises ``AttributeError: module 'itasca' has no
attribute 'command'``. So:

1. **Deferred readiness.** Poll on a daemon thread until ``itasca.command``
   exists *and* a GUI Qt application exists.
2. **A hop back to the GUI thread.** ``start()`` installs a ``QTimer`` on
   whatever thread calls it. From the polling thread that produces a timer
   in a thread with no event loop: it never ticks, so ``/health`` answers
   200 while every submitted task hangs -- the one failure mode that looks
   like success. The hop is ``moveToThread(app.thread())`` plus
   ``QMetaObject.invokeMethod(..., QueuedConnection)``.
3. **A module-level reference to the ``QObject``.** A local is collected
   when the polling function returns and the queued call is then silently
   dropped, while ``invokeMethod`` still returns ``True``. Same hazard as
   the note at the top of ``runtime.py``.

Safety
------
This runs inside someone's GUI, so every path is defensively written:

- **Only engine interpreters.** Gated on ``sys.executable``'s *basename*,
  so ``exe64/python36/python.exe`` -- which the package's own self-upgrade
  launches for ``-m pip`` -- is skipped silently rather than polling for
  two minutes in a throwaway process.
- **Console builds are left alone.** They construct a bare
  ``QCoreApplication`` with no event loop; ``runtime._is_qt_gui_app``
  rejects it and the poller gives up.
- **One bridge per machine.** The port is probed first; if something is
  already listening, this hook stands down rather than starting a second
  server. ``itasca-mcp`` talks to a single bridge, so that is the intended
  limit.
- **Nothing here raises into the product.** Every failure is logged and
  swallowed.

Python 3.6 compatible implementation.
"""

import logging
import os
import socket
import sys
import threading
import time

logger = logging.getLogger("itasca-mcp-bridge")

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9001

# How long to wait for the engine bindings and the Qt application. The GUI
# reaches both a couple of seconds in; the margin is for a slow network
# licence check, which is the slowest part of a cold start measured here.
DEFAULT_STARTUP_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 0.5

ENV_PORT = "ITASCA_MCP_BRIDGE_AUTOSTART_PORT"
ENV_HOST = "ITASCA_MCP_BRIDGE_AUTOSTART_HOST"
ENV_LOG = "ITASCA_MCP_BRIDGE_AUTOSTART_LOG"
ENV_TIMEOUT = "ITASCA_MCP_BRIDGE_AUTOSTART_TIMEOUT"
ENV_CLOSE_NOTICE = "ITASCA_MCP_BRIDGE_AUTOSTART_CLOSE_NOTICE"
ENV_ENGINE_HINTS = "ITASCA_MCP_BRIDGE_AUTOSTART_ENGINE_HINTS"

# Written into the file `install()` creates, and the first thing `status()`
# looks for. Tests match on it too.
MARKER = "itasca-mcp-bridge autostart"
FILENAME = "sitecustomize.py"
BACKUP_SUFFIX = ".pre-autostart"

# Interpreter basenames that belong to an ITASCA product GUI or console.
# Matched against the basename only: `exe64/python36/python.exe` lives under
# a product directory but is a plain interpreter, and the self-upgrade runs
# pip with it. A full-path substring test would match that and poll in a
# process that is about to exit.
ENGINE_HINTS = ("itasca", "pfc", "flac", "3dec", "mpoint", "massflow")

# Windows the product raises that state something and ask nothing. Matched
# by suffix because the product and revision are prefixed at runtime
# ("PFC2D 7.00.161 : Startup"). An unanswered one sits on top of whatever
# the person at the keyboard is doing, and it is raised on every start.
BENIGN_SUFFIX = ": Startup"

# Held at module level: a QObject or QTimer with no owning reference is
# garbage collected and stops working, which is the third trap above.
_boot_object = None
_notice_timer = None


# ---- environment ------------------------------------------------------


def _env_flag(name, default=False):
    # type: (str, bool) -> bool
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name, default):
    # type: (str, float) -> float
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def log_path():
    # type: () -> str
    """Where the hook records what it did. Empty means nowhere."""
    import tempfile

    default = os.path.join(tempfile.gettempdir(), "itasca_mcp_bridge_autostart.log")
    return os.environ.get(ENV_LOG, default)


def _log(message):
    # type: (str) -> None
    """Append one line to the autostart log, and to the bridge's logger.

    A file rather than the logger alone on purpose: sitecustomize runs
    before anything configures logging, so a hook that only logs would
    leave no trace at all on the machine it failed on.
    """
    logger.info("%s", message)
    path = log_path()
    if not path:
        return
    try:
        import datetime

        with open(path, "a") as handle:
            handle.write(
                "{}  {}\n".format(datetime.datetime.now().isoformat(), message)
            )
    except Exception:
        pass


# ---- readiness --------------------------------------------------------


def is_engine_interpreter(executable=None):
    # type: (str) -> bool
    """Whether this process is an ITASCA product, by interpreter basename.

    Basename only -- see ``ENGINE_HINTS``. The product binary is
    ``pfc2d700_gui.exe`` and friends; ``python.exe`` is not, even when it
    sits inside a product's directory.
    """
    name = os.path.basename(executable or sys.executable or "").lower()
    if not name:
        return False
    hints = tuple(
        hint.strip().lower()
        for hint in os.environ.get(ENV_ENGINE_HINTS, "").split(",")
        if hint.strip()
    ) or ENGINE_HINTS
    return any(hint in name for hint in hints)


def port_in_use(host, port, timeout=0.3):
    # type: (str, int, float) -> bool
    """Whether something is already listening. Cheap, and never raises."""
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _application():
    # type: () -> object
    """The GUI QApplication, or None while it does not exist yet.

    Reuses ``runtime``'s binding probe and its GUI test rather than a second
    copy, so the console builds are excluded by the same logic the task pump
    uses: they construct a bare ``QCoreApplication`` that never runs an event
    loop, and hanging a timer on one is the "answers HTTP, pumps nothing"
    failure.
    """
    from . import runtime

    QtCore = runtime._import_qtcore()
    if QtCore is None:
        return None
    try:
        app = QtCore.QCoreApplication.instance()
    except Exception:
        return None
    if app is None:
        return None
    return app if runtime._is_qt_gui_app(app) else None


def _engine_ready():
    # type: () -> object
    """The GUI application once the engine bindings are also complete."""
    try:
        import itasca
    except Exception:
        return None
    if not hasattr(itasca, "command"):
        return None
    return _application()


# ---- the Startup notice -----------------------------------------------


def _qt_widgets():
    # type: () -> object
    from . import runtime

    for binding in runtime._QT_BINDINGS:
        try:
            return __import__(binding + ".QtWidgets", fromlist=["QtWidgets"])
        except Exception:
            continue
    return None


def close_notice_windows():
    # type: () -> list
    """Close the product's revision notice. Returns the titles closed.

    Opt-in, and off by default. It is the product's own window, raised once
    per revision to list what changed, and a bridge that closes windows it
    did not cause is making a call that belongs to the person at the
    keyboard -- the same reasoning ``utils/modal_guard`` uses when it
    declines to answer a dialog offering a choice. Turn it on for an
    unattended or agent-driven start, where nobody is going to read it.

    ``close()`` is checked against ``isVisible()`` afterwards rather than
    trusted: Qt refuses to close a widget that sits inside a modal
    ``exec_()`` and ``close()`` returns without raising, so counting the
    attempt would report a dismissal that never happened.
    """
    widgets = _qt_widgets()
    if widgets is None:
        return []
    closed = []
    try:
        for widget in widgets.QApplication.topLevelWidgets():
            try:
                if not widget.isVisible():
                    continue
                if not widget.windowTitle().endswith(BENIGN_SUFFIX):
                    continue
                widget.close()
                if not widget.isVisible():
                    closed.append(widget.windowTitle())
            except Exception:
                continue
    except Exception:
        return closed
    return closed


def _schedule_notice_sweep(interval_ms=1000):
    # type: (int) -> bool
    """Close notices for the life of the process, not just at startup.

    A one-shot sweep at start-up is not enough, and that is what makes this
    worth a timer: the notice is not necessarily up when the bridge comes
    up. Observed on PFC2D 7.00.161 -- the bridge logged "start() returned
    OK" and the notice appeared 25 seconds later, after a "Recover Project
    File" prompt was answered. A window nobody dismissed keeps coming back
    to the front, so the only reliable answer is to keep looking.
    """
    global _notice_timer

    from . import runtime

    QtCore = runtime._import_qtcore()
    if QtCore is None:
        return False
    try:
        if _notice_timer is not None:
            _notice_timer.stop()
        timer = QtCore.QTimer()
        timer.setInterval(int(interval_ms))
        timer.timeout.connect(_tick_notice)
        timer.start()
    except Exception as exc:
        _log("notice sweep could not start: {!r}".format(exc))
        return False
    _notice_timer = timer
    return True


def _tick_notice():
    # type: () -> None
    for title in close_notice_windows():
        _log("closed the product's notice window: {}".format(title))


# ---- boot -------------------------------------------------------------


def _on_application_thread(host, port):
    # type: (str, int) -> None
    """Runs on the GUI thread, with its event loop alive. Never raises."""
    try:
        from . import __version__
        from . import start

        _log("starting the bridge on {}:{} (bridge {})".format(host, port, __version__))
        # auto_upgrade stays at its default: an autostart that silently
        # changes version between two runs is worse than one that is a
        # release behind. Pin it with ITASCA_MCP_BRIDGE_AUTO_UPGRADE=0.
        start(host=host, port=port, mode="gui")
        _log("start() returned")
    except Exception as exc:
        _log("start() failed: {!r}".format(exc))
    finally:
        # Outside the try: a notice in the user's face is worth closing
        # whether or not the bridge came up.
        if _env_flag(ENV_CLOSE_NOTICE, False):
            if _schedule_notice_sweep():
                _log("watching for the product's notice window")


def _watch(host, port, timeout_s):
    # type: (str, int, float) -> None
    """Poll for readiness on a daemon thread, then hop to the GUI thread."""
    global _boot_object

    from . import runtime

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if port_in_use(host, port):
                _log(
                    "{}:{} is already in use; not starting a second bridge".format(
                        host, port
                    )
                )
                return
            app = _engine_ready()
            if app is None:
                time.sleep(POLL_INTERVAL_S)
                continue

            QtCore = runtime._import_qtcore()
            if QtCore is None:
                _log("engine is ready but no Qt binding is importable; giving up")
                return

            slot = QtCore.Slot
            QObject = QtCore.QObject
            Qt = QtCore.Qt

            class _Boot(QObject):
                @slot()
                def go(self):
                    _on_application_thread(host, port)

            # Module level: a local would be collected when _watch returns and
            # the queued call would be dropped while invokeMethod still
            # reported success.
            _boot_object = _Boot()
            _boot_object.moveToThread(app.thread())
            _log("engine and Qt are ready; queueing start() onto the GUI thread")

            # QueuedConnection is what puts the call on the GUI thread. Calling
            # start() from this thread instead would pass the bridge's own
            # check and then install a QTimer in a thread that never runs it.
            queued = QtCore.QMetaObject.invokeMethod(
                _boot_object, "go", Qt.QueuedConnection
            )
            _log("invokeMethod returned {!r}".format(queued))
            return
        except Exception as exc:
            _log("autostart watcher error: {!r}".format(exc))
            time.sleep(POLL_INTERVAL_S)
    _log("gave up waiting for the engine and Qt after {:.0f}s".format(timeout_s))


def boot(host=None, port=None, timeout=None):
    # type: (str, int, float) -> bool
    """Entry point for the ``sitecustomize`` shim. Returns whether it armed.

    Never raises and never blocks: readiness is polled on a daemon thread.
    Returns True when the watcher was started, not when the bridge is up --
    at this point in interpreter startup the engine does not exist yet, and
    waiting for it here would freeze the product.
    """
    try:
        if not is_engine_interpreter():
            return False

        host = host or os.environ.get(ENV_HOST, DEFAULT_HOST)
        port = int(port or os.environ.get(ENV_PORT, DEFAULT_PORT))
        timeout = float(timeout or _env_float(ENV_TIMEOUT, DEFAULT_STARTUP_TIMEOUT_S))

        _log("sitecustomize ran; interpreter={}".format(sys.executable))

        try:
            from . import __version__  # noqa: F401
        except Exception as exc:
            _log("the bridge package is not importable here: {!r}".format(exc))
            return False

        if port_in_use(host, port):
            _log(
                "{}:{} is already in use; not starting a second bridge".format(
                    host, port
                )
            )
            return False

        thread = threading.Thread(
            target=_watch, args=(host, port, timeout), name="mcp-bridge-autostart"
        )
        thread.daemon = True
        thread.start()
        return True
    except Exception as exc:
        try:
            _log("autostart could not arm: {!r}".format(exc))
        except Exception:
            pass
        return False


# ---- the shim ---------------------------------------------------------

SHIM = '''# {marker} -- installed by `python -m itasca_mcp_bridge autostart install`
# Remove with:  python -m itasca_mcp_bridge autostart remove
#
# Keep this file a shim. The readiness polling, the hop to the GUI thread
# and the notices are implemented in itasca_mcp_bridge.autostart, so they
# stay versioned and upgradable with the package; a copy here would freeze
# at whichever release first wrote it.
try:
    from itasca_mcp_bridge.autostart import boot
except Exception:
    pass
else:
    boot()
'''.format(marker=MARKER)


# ---- installation -----------------------------------------------------


def product_python_dirs(roots=None):
    # type: (list) -> list
    """Yield ``(product, site_packages)`` for every ITASCA embedded Python.

    ``Lib`` and ``lib`` are the same directory on Windows, so candidates are
    de-duplicated by normalised real path or every product is reported --
    and installed to -- twice.
    """
    import glob

    roots = roots or default_roots()
    seen = set()
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for product in sorted(os.listdir(root)):
            exe64 = os.path.join(root, product, "exe64")
            if not os.path.isdir(exe64):
                continue
            for python in sorted(glob.glob(os.path.join(exe64, "python*"))):
                if not os.path.isdir(python):
                    continue
                for site_packages in (
                    os.path.join(python, "Lib", "site-packages"),
                    os.path.join(python, "lib", "site-packages"),
                ):
                    if not os.path.isdir(site_packages):
                        continue
                    key = os.path.normcase(os.path.realpath(site_packages))
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append((product, site_packages))
    return found


def default_roots():
    # type: () -> list
    """Install roots to search, overridable with ITASCA_MCP_BRIDGE_ROOTS."""
    override = os.environ.get("ITASCA_MCP_BRIDGE_ROOTS", "")
    if override:
        return [part for part in override.split(os.pathsep) if part]
    return [
        r"C:\Program Files\Itasca",
        r"D:\Program Files\Itasca",
        r"C:\Itasca",
        r"D:\Itasca",
        "/Applications/Itasca",
        os.path.expanduser("~/Itasca"),
    ]


def target_of(site_packages):
    # type: (str) -> str
    return os.path.join(site_packages, FILENAME)


def state_of(path):
    # type: (str) -> str
    """One of: absent, ours, foreign, unreadable."""
    if not os.path.exists(path):
        return "absent"
    try:
        with open(path, "r") as handle:
            head = handle.read(4096)
    except Exception:
        return "unreadable"
    return "ours" if MARKER in head else "foreign"


def install(site_packages):
    # type: (str) -> str
    """Write the shim into one site-packages. Returns what it did.

    A ``sitecustomize.py`` that is not ours is backed up rather than
    silently replaced: other tools legitimately use that filename, and
    clobbering one would break something outside this package.
    """
    import shutil

    path = target_of(site_packages)
    state = state_of(path)
    if state == "foreign":
        backup = path + BACKUP_SUFFIX
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
        with open(path, "w") as handle:
            handle.write(SHIM)
        return "replaced (backup at {})".format(os.path.basename(backup))
    with open(path, "w") as handle:
        handle.write(SHIM)
    return "refreshed" if state == "ours" else "installed"


def remove(site_packages):
    # type: (str) -> str
    """Undo :func:`install`, restoring a pre-existing file if there was one."""
    import shutil

    path = target_of(site_packages)
    state = state_of(path)
    if state == "absent":
        return "nothing to remove"
    if state != "ours":
        return "left alone (not this package's file)"
    os.remove(path)
    backup = path + BACKUP_SUFFIX
    if os.path.exists(backup):
        shutil.move(backup, path)
        return "removed (restored the previous file)"
    return "removed"
