"""Start-with-Windows integration via the per-user HKCU 'Run' key.

Registers this app to launch at login with the `--startup` flag, which tells
`app.main` to start hidden in the system tray instead of showing the window.
No-ops on non-Windows platforms.
"""

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "Riot2FA"
STARTUP_FLAG = "--startup"


def available():
    return sys.platform == "win32"


def _launch_command():
    """The command Windows should run at login (quoted, with the startup flag)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {STARTUP_FLAG}'
    # From source: prefer pythonw so no console window flashes at login.
    python = sys.executable
    pyw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.exists(pyw):
        python = pyw
    script = os.path.abspath(sys.argv[0])
    return f'"{python}" "{script}" {STARTUP_FLAG}'


def is_enabled():
    if not available():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except OSError:
        return False


def enable():
    """Add (or refresh) the login entry pointing at the current executable."""
    if not available():
        return False
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _launch_command())
    return True


def disable():
    if not available():
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True
