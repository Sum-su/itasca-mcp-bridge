"""Tests for the autostart hook — starting the bridge with the product.

The hook runs during interpreter startup inside someone else's GUI, so the
tests here are mostly about the ways it must *not* act: a plain interpreter
that happens to live under a product directory (the self-upgrade runs pip
with exactly that), a console build with no event loop, a second product
starting when one bridge is already listening, and a sitecustomize.py that
belongs to somebody else.
"""

from __future__ import annotations

import os

import pytest
from itasca_mcp_bridge import autostart


# ---- which interpreters may arm ---------------------------------------

# Built with the platform's own separator rather than written as Windows
# literals: `os.path.basename` does not treat `\` as one on POSIX, so a
# literal `D:\...\python.exe` is its own basename there and the negative
# cases below would invert on CI. The real spelling on this machine is
# `D:\Program Files\Itasca\PFC700\exe64\pfc2d700_gui.exe` next to
# `...\exe64\python36\python.exe`.
ENGINE = os.path.join("Itasca", "PFC700", "exe64")
EMBEDDED_PYTHON = os.path.join(ENGINE, "python36", "python.exe")


@pytest.mark.parametrize(
    "executable",
    [
        os.path.join(ENGINE, "pfc2d700_gui.exe"),
        os.path.join("Itasca", "FLAC3D700", "exe64", "flac3d700_gui.exe"),
        os.path.join("Itasca", "3DEC700", "3dec700_console"),
        os.path.join("Itasca", "MPoint700", "mpoint_gui.exe"),
    ],
)
def test_engine_binaries_arm(executable):
    assert autostart.is_engine_interpreter(executable) is True


@pytest.mark.parametrize(
    "executable",
    [
        # The package's own self-upgrade runs pip with this one. A substring
        # test on the whole path would match "PFC700" and have a process that
        # is about to exit poll for two minutes.
        EMBEDDED_PYTHON,
        os.path.join("Python36", "python.exe"),
        os.path.join(os.sep, "usr", "bin", "python3"),
        os.path.join("tools", "my_runner.exe"),
    ],
)
def test_plain_interpreters_do_not_arm(executable):
    assert autostart.is_engine_interpreter(executable) is False


def test_engine_hints_are_overridable(monkeypatch):
    monkeypatch.setenv(autostart.ENV_ENGINE_HINTS, "itascasoft")
    assert autostart.is_engine_interpreter("itascasoft_gui.exe") is True
    assert autostart.is_engine_interpreter("pfc2d700_gui.exe") is False


# ---- boot() decides, and does not raise -------------------------------


def test_boot_declines_on_a_plain_interpreter(monkeypatch):
    started = []
    monkeypatch.setattr(autostart.threading, "Thread", lambda **kw: started.append(kw))
    monkeypatch.setattr(autostart.sys, "executable", "python.exe")
    assert autostart.boot() is False
    assert started == []


def test_boot_declines_when_a_bridge_is_already_listening(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setattr(autostart.sys, "executable", "pfc2d700_gui.exe")
    monkeypatch.setattr(autostart, "port_in_use", lambda host, port, timeout=0.3: True)
    monkeypatch.setattr(autostart.threading, "Thread", lambda **kw: started.append(kw))

    assert autostart.boot() is False
    assert started == []
    assert "already in use" in (tmp_path / "autostart.log").read_text()


def test_boot_arms_a_daemon_thread(monkeypatch, tmp_path):
    created = {}

    class _Thread:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def start(self):
            created["started"] = True

        def __setattr__(self, name, value):
            created[name] = value

    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setattr(autostart.sys, "executable", "pfc2d700_gui.exe")
    monkeypatch.setattr(autostart, "port_in_use", lambda host, port, timeout=0.3: False)
    monkeypatch.setattr(autostart.threading, "Thread", _Thread)

    assert autostart.boot() is True
    assert created["started"] is True
    assert created["daemon"] is True
    assert created["name"] == "mcp-bridge-autostart"


def test_boot_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setattr(autostart.sys, "executable", "pfc2d700_gui.exe")

    def _explode(name, default):
        raise RuntimeError("bad environment")

    monkeypatch.setattr(autostart, "_env_float", _explode)
    assert autostart.boot() is False


# ---- the notice window ------------------------------------------------


class _FakeButton:
    """One button on a dialog. Clicking the right one is what dismisses it."""

    def __init__(self, text, owner, visible=True, dismisses=True):
        self._text = text
        self._owner = owner
        self._visible = visible
        self._dismisses = dismisses
        self.clicks = 0

    def text(self):
        return self._text

    def isVisible(self):
        return self._visible

    def click(self):
        self.clicks += 1
        if self._dismisses:
            self._owner._visible = False


class _FakeWidget:
    def __init__(self, title, visible=True, refuses=False, buttons=()):
        self._title = title
        self._visible = visible
        self._refuses = refuses
        self.buttons = list(buttons)
        self.closes = 0

    def windowTitle(self):
        return self._title

    def isVisible(self):
        return self._visible

    def close(self):
        self.closes += 1
        if not self._refuses:
            self._visible = False

    def findChildren(self, kind):
        return list(self.buttons) if kind is _FakeButton else []


class _FakeDialog(_FakeWidget):
    """A widget that asks something. `QDialog` is the hook's only test for it."""


def _dialog(title, *labels, **kwargs):
    """A dialog carrying buttons, so `dismissible` has something to read."""
    dialog = _FakeDialog(title, **kwargs)
    dialog.buttons = [_FakeButton(label, dialog) for label in labels]
    return dialog


class _FakeApplication:
    def __init__(self, widgets):
        self._widgets = widgets

    def topLevelWidgets(self):
        return self._widgets


class _FakeWidgets:
    QDialog = _FakeDialog
    QAbstractButton = _FakeButton
    QApplication = None

    def __init__(self, widgets):
        self.QApplication = _FakeApplication(widgets)


def _with_widgets(monkeypatch, widgets):
    monkeypatch.setattr(autostart, "_qt_widgets", lambda: _FakeWidgets(widgets))
    monkeypatch.setattr(autostart, "_reported_dialogs", set())
    monkeypatch.setattr(autostart, "_attempted_dismissals", set())


def test_closes_only_the_revision_notice(monkeypatch):
    notice = _FakeWidget("PFC2D 7.00.161 : Startup")
    document = _FakeWidget("Model - PFC2D 7.00.161")
    _with_widgets(monkeypatch, [notice, document])

    assert autostart.close_notice_windows() == ["PFC2D 7.00.161 : Startup"]
    assert notice.closes == 1
    assert document.closes == 0


def test_a_notice_qt_refused_is_not_reported_as_closed(monkeypatch):
    # Qt will not close a widget sitting inside a modal exec_(), and close()
    # returns without raising, so the attempt alone proves nothing.
    notice = _FakeWidget("PFC2D 7.00.161 : Startup", refuses=True)
    _with_widgets(monkeypatch, [notice])

    assert autostart.close_notice_windows() == []
    assert notice.closes == 1


def test_hidden_windows_are_skipped(monkeypatch):
    notice = _FakeWidget("PFC2D 7.00.161 : Startup", visible=False)
    _with_widgets(monkeypatch, [notice])

    assert autostart.close_notice_windows() == []
    assert notice.closes == 0


def test_a_broken_widget_does_not_stop_the_sweep(monkeypatch):
    class _Broken:
        def isVisible(self):
            raise RuntimeError("no")

    notice = _FakeWidget("PFC2D 7.00.161 : Startup")
    _with_widgets(monkeypatch, [_Broken(), notice])

    assert autostart.close_notice_windows() == ["PFC2D 7.00.161 : Startup"]


def test_no_qt_binding_is_not_an_error(monkeypatch):
    monkeypatch.setattr(autostart, "_qt_widgets", lambda: None)
    assert autostart.close_notice_windows() == []


def test_notice_closing_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv(autostart.ENV_DISMISS_WINDOWS, raising=False)
    assert autostart._env_flag(autostart.ENV_DISMISS_WINDOWS, False) is False
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "1")
    assert autostart._env_flag(autostart.ENV_DISMISS_WINDOWS, False) is True
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "off")
    assert autostart._env_flag(autostart.ENV_DISMISS_WINDOWS, False) is False


# ---- dialogs the hook will not answer ---------------------------------


def test_a_dialog_is_reported_and_the_window_behind_it_is_not(monkeypatch):
    dialog = _FakeDialog("Recover Project File")
    document = _FakeWidget("Model - PFC2D 7.00.161")
    _with_widgets(monkeypatch, [dialog, document])

    # A plain top-level window is not a question, and reporting it would
    # bury the one that is.
    assert autostart.waiting_dialogs() == ["Recover Project File"]
    # Reported, not answered: this one is offering a choice.
    assert dialog.closes == 0


def test_the_startup_notice_is_not_a_waiting_dialog(monkeypatch):
    _with_widgets(monkeypatch, [_FakeDialog("PFC2D 7.00.161 : Startup")])
    assert autostart.waiting_dialogs() == []


def test_hidden_dialogs_are_not_waiting(monkeypatch):
    # A dialog the product has already dismissed is not holding anything.
    _with_widgets(monkeypatch, [_FakeDialog("Recover Project File", visible=False)])
    assert autostart.waiting_dialogs() == []


def _log_of(tmp_path):
    return (tmp_path / "autostart.log").read_text()


def test_a_waiting_dialog_is_logged_once_not_once_a_second(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    _with_widgets(monkeypatch, [_FakeDialog("Recover Project File")])

    autostart._tick_windows()
    autostart._tick_windows()
    autostart._tick_windows()

    # The dialog is still up on every tick. A log that says so thirty times a
    # minute is a log nobody reads, which is the same as no log.
    assert _log_of(tmp_path).count("Recover Project File") == 1


def test_a_dialog_is_reported_even_when_closing_is_turned_off(monkeypatch, tmp_path):
    # The two halves of the pass are independent: this is the one that has to
    # survive on a default install, because it is the only symptom of a
    # bridge whose HTTP server answers while every task hangs.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.delenv(autostart.ENV_DISMISS_WINDOWS, raising=False)
    notice = _FakeWidget("PFC2D 7.00.161 : Startup")
    _with_widgets(monkeypatch, [notice, _FakeDialog("Recover Project File")])

    autostart._tick_windows()

    assert "Recover Project File" in _log_of(tmp_path)
    assert "closed the product's notice window" not in _log_of(tmp_path)
    assert notice.closes == 0


def test_closing_is_the_opt_in_half_of_the_same_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "1")
    notice = _FakeWidget("PFC2D 7.00.161 : Startup")
    _with_widgets(monkeypatch, [notice])

    autostart._tick_windows()

    assert notice.closes == 1
    assert "closed the product's notice window: PFC2D 7.00.161 : Startup" in _log_of(tmp_path)


# ---- dialogs that ask nothing -----------------------------------------


def test_a_box_with_one_ok_is_answered(monkeypatch):
    # Measured on PFC2D 7.00.161: this is the box that blocks the product and
    # offers no way past it.
    dialog = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == ["PFC2D 7.00"]
    assert dialog.buttons[0].clicks == 1


def test_a_box_that_offers_a_choice_is_left_standing(monkeypatch):
    # `close()` cannot dismiss these -- Qt refuses inside a modal exec_() --
    # and answering one means picking for somebody. So they stay.
    dialog = _dialog("Recover Project File", "Open", "Discard")
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == []
    assert dialog.buttons[0].clicks == 0
    assert dialog.buttons[1].clicks == 0


def test_a_yes_is_a_choice_even_with_no_visible_no(monkeypatch):
    # "Yes" implies a "No" exists somewhere, so it is not in the
    # acknowledgement set even when this particular box does not draw it.
    dialog = _dialog("Restore save file initial.sav?", "Yes", "No")
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == []
    assert dialog.buttons[0].clicks == 0


def test_an_ok_cancel_box_is_a_choice(monkeypatch):
    dialog = _dialog("Are you sure you want to restore save file initial.sav?", "OK", "Cancel")
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == []
    assert dialog.buttons[0].clicks == 0


def test_the_notice_is_not_a_dialog_to_answer(monkeypatch):
    # It is closed by the other half of the pass; answering it here would
    # have the same window logged twice under two different verbs.
    notice = _dialog("PFC2D 7.00.161 : Startup", "Ok")
    _with_widgets(monkeypatch, [notice])

    assert autostart.dismiss_dialogs() == []
    assert notice.buttons[0].clicks == 0


def test_a_box_with_no_visible_buttons_is_not_answered(monkeypatch):
    # Nothing to click means nothing to read: an empty label set is not the
    # same as a set of acknowledgements, and treating it as one would have
    # the hook guessing at a window it cannot see into.
    hidden = _dialog("PFC2D 7.00", "Ok")
    hidden.buttons[0]._visible = False
    _with_widgets(monkeypatch, [hidden])

    assert autostart.dismiss_dialogs() == []


def test_a_click_that_does_not_dismiss_is_not_reported_as_one(monkeypatch):
    # Real Qt can refuse. `isVisible()` is re-read instead of the click
    # being trusted, exactly as the notice sweep does.
    dialog = _dialog("PFC2D 7.00", "Ok")
    dialog.buttons[0]._dismisses = False
    _with_widgets(monkeypatch, [dialog])

    assert autostart.dismiss_dialogs() == []
    assert dialog.buttons[0].clicks == 1


def test_a_refusing_button_is_clicked_once_not_once_a_second(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "1")
    dialog = _dialog("PFC2D 7.00", "Ok")
    dialog.buttons[0]._dismisses = False
    _with_widgets(monkeypatch, [dialog])

    autostart._tick_windows()
    autostart._tick_windows()
    autostart._tick_windows()

    assert dialog.buttons[0].clicks == 1
    # And what survives the click is reported, because it is still in the way.
    assert "a dialog is waiting for a human" in _log_of(tmp_path)


def test_a_chain_of_boxes_is_cleared_in_one_tick(monkeypatch, tmp_path):
    # Answering the first box on PFC2D 7.00.161 produced two more, so the
    # pass has to repeat. Each tick re-reads the widget list, so a queue
    # that grows while it is being drained still gets drained.
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.setenv(autostart.ENV_DISMISS_WINDOWS, "1")
    first = _dialog("Recover Project File", "Open", "Discard")
    second = _dialog("Are you sure you want to restore save file initial.sav?", "OK", "Cancel")
    third = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [first, second, third])

    autostart._tick_windows()

    # The two that ask something are untouched; the one that does not is gone.
    assert first.buttons[0].clicks == 0
    assert second.buttons[0].clicks == 0
    assert third.buttons[0].clicks == 1
    # And the two that were answered by nobody are named in the log.
    log = _log_of(tmp_path)
    assert "Recover Project File" in log
    assert "save file initial.sav" in log


def test_answering_is_opt_in_and_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "log_path", lambda: str(tmp_path / "autostart.log"))
    monkeypatch.delenv(autostart.ENV_DISMISS_WINDOWS, raising=False)
    dialog = _dialog("PFC2D 7.00", "Ok")
    _with_widgets(monkeypatch, [dialog])

    autostart._tick_windows()

    assert dialog.buttons[0].clicks == 0
    assert "PFC2D 7.00" in _log_of(tmp_path)


# ---- installing the shim ----------------------------------------------


def test_shim_carries_the_marker_and_imports_the_module():
    assert autostart.MARKER in autostart.SHIM
    assert "from itasca_mcp_bridge.autostart import boot" in autostart.SHIM
    # A shim, not a copy: the logic has to keep upgrading with the package.
    assert "def _watch" not in autostart.SHIM


def test_install_then_status_then_remove(tmp_path):
    site_packages = str(tmp_path / "Lib" / "site-packages")
    os.makedirs(site_packages)
    path = autostart.target_of(site_packages)

    assert autostart.state_of(path) == "absent"
    assert autostart.install(site_packages) == "installed"
    assert autostart.state_of(path) == "ours"
    assert autostart.install(site_packages) == "refreshed"
    assert autostart.remove(site_packages) == "removed"
    assert autostart.state_of(path) == "absent"
    assert autostart.remove(site_packages) == "nothing to remove"


def test_a_foreign_sitecustomize_is_backed_up_not_lost(tmp_path):
    site_packages = str(tmp_path / "Lib" / "site-packages")
    os.makedirs(site_packages)
    path = autostart.target_of(site_packages)
    original = "# somebody else's sitecustomize\nX = 1\n"
    with open(path, "w") as handle:
        handle.write(original)

    assert autostart.state_of(path) == "foreign"
    result = autostart.install(site_packages)
    assert result.startswith("replaced (backup at ")
    assert autostart.state_of(path) == "ours"

    # And removing ours puts theirs back, rather than leaving them without one.
    assert autostart.remove(site_packages) == "removed (restored the previous file)"
    with open(path) as handle:
        assert handle.read() == original


def test_remove_leaves_a_foreign_file_alone(tmp_path):
    site_packages = str(tmp_path / "Lib" / "site-packages")
    os.makedirs(site_packages)
    path = autostart.target_of(site_packages)
    with open(path, "w") as handle:
        handle.write("SOMEONE_ELSES = True\n")

    assert autostart.remove(site_packages) == "left alone (not this package's file)"
    assert os.path.exists(path)


# ---- finding the products ---------------------------------------------


def test_products_are_found_once_each(tmp_path):
    # Both spellings are probed because the case of that directory is not
    # guaranteed, and on Windows they are the same directory. Without the
    # de-duplication every product is reported -- and installed to -- twice.
    python36 = tmp_path / "PFC700" / "exe64" / "python36"
    os.makedirs(str(python36 / "Lib" / "site-packages"))
    try:
        (python36 / "lib").symlink_to(python36 / "Lib", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot link lib -> Lib here to exercise the aliasing")
    # A product whose embedded Python is not there yet.
    os.makedirs(str(tmp_path / "FLAC3D700" / "exe64"))

    found = autostart.product_python_dirs([str(tmp_path)])
    assert [product for product, _ in found] == ["PFC700"]
