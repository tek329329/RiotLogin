import os
import time
import threading
import webbrowser

from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QMessageBox,
    QDialog,
    QInputDialog,
    QSystemTrayIcon,
    QMenu,
    QApplication,
)
from PyQt6.QtCore import Qt, QTimer, QSize, pyqtSignal
from PyQt6.QtGui import QIcon, QAction

from app.core import load_accounts, save_accounts, PERIOD
from app.core.fcm_service import FcmService
from app.core import updater, autostart
from app.core.storage import APPDATA_DIR
from app.core.paths import resource_path
from app.version import __version__
from app.api import (
    is_valid_jwt,
    fetch_mfa_factors,
    is_email_mfa_enabled,
    enable_mfa,
    verify_mfa,
    register_mfa_push_device,
    mint_access_token,
    SessionExpired,
    parse_qr_login,
    qr_session_info,
    qr_approve,
    fetch_new_csrf_token,
)
from app.ui.toast import Toast
from app.ui.account_card import AccountCard
from app.ui.manual_add_dialog import ManualAddDialog
from app.ui.login_browser_dialog import LoginBrowserDialog
from app.ui.mfa_prompt_dialog import MfaPromptDialog
from app.ui.qr_scanner_dialog import QrScannerDialog
from app.ui.qr_confirm_dialog import QrConfirmDialog

ICON_PATH = resource_path(os.path.join("images", "icon.png"))
QR_ICON_PATH = resource_path(os.path.join("images", "qr.png"))

class MainWindow(QMainWindow):
    _update_found = pyqtSignal(dict)
    _session_report = pyqtSignal(list, list, list)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Riot 2FA  v{__version__}")
        self.setMinimumSize(560, 300)
        self.resize(560, 400)

        self.accounts = load_accounts()
        self.cards: list[AccountCard] = []
        self._last_step = int(time.time()) // PERIOD

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(0)

        hdr = QHBoxLayout()
        title = QLabel("RIOT 2FA")
        title.setObjectName("titleLabel")
        hdr.addWidget(title)
        hdr.addStretch()

        b1 = QPushButton("Add via Login")
        b1.setObjectName("addLoginBtn")
        b1.setFixedWidth(130)
        b1.clicked.connect(self._add_via_login)
        hdr.addWidget(b1)
        hdr.addSpacing(6)
        b2 = QPushButton("Add Manually")
        b2.setObjectName("addManualBtn")
        b2.setFixedWidth(120)
        b2.clicked.connect(self._add_manually)
        hdr.addWidget(b2)
        hdr.addSpacing(6)
        bqr = QPushButton()
        bqr.setObjectName("qrBtn")
        bqr.setFixedWidth(38)
        bqr.setIcon(QIcon(QR_ICON_PATH))
        bqr.setIconSize(QSize(18, 18))
        bqr.setToolTip("Sign in by scanning a QR code on screen")
        bqr.clicked.connect(self._scan_qr)
        hdr.addWidget(bqr)
        outer.addLayout(hdr)
        outer.addSpacing(12)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_widget)
        self.scroll_layout.setContentsMargins(0, 0, 2, 0)
        self.scroll_layout.setSpacing(6)
        self.scroll_layout.addStretch()
        self.scroll.setWidget(self.scroll_widget)
        outer.addWidget(self.scroll, stretch=1)

        self.toast = Toast(central)

        self._populate()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

        self._active_prompts = []
        self._tray_hint_shown = False
        self._setup_tray()
        self._init_autostart()

        self.fcm = FcmService(self)
        self.fcm.push_received.connect(self._on_push)
        self.fcm.start()

        # Keep saved Riot sessions alive: minting rotates the SSO cookies (sliding
        # expiry), so refreshing every few hours while the app sits in the tray
        # stops QR sign-in / push from expiring after a couple idle days.
        self._fragile_warned = False
        self._session_report.connect(self._on_session_report)
        self._session_timer = QTimer(self)
        self._session_timer.timeout.connect(self._refresh_sessions)
        self._session_timer.start(6 * 60 * 60 * 1000)  # every 6 hours
        QTimer.singleShot(20_000, self._refresh_sessions)  # once shortly after start

        self._update_found.connect(self._on_update_found)
        threading.Thread(target=self._check_update, daemon=True).start()

    def _refresh_sessions(self):
        """Refresh every account's stored RSO session off the GUI thread.

        Each successful mint rotates the SSO cookies and pushes Riot's expiry ~30 days
        out, so running this regularly is what keeps sessions alive indefinitely.
        """
        accounts = [a for a in self.accounts if a.get("sso")]
        if not accounts:
            return

        def work():
            dead, expiring, fragile = [], [], []
            for acct in accounts:
                try:
                    self._valid_access_token(acct)
                except Exception:
                    continue
                name = acct.get("name", "Unknown")
                if acct.get("session_dead"):
                    dead.append(name)
                    continue
                exp = acct.get("sso_expires")
                if not exp:
                    # Session-scoped ssid: added without "Stay signed in", so Riot
                    # never extends it. Refreshing cannot save this one.
                    fragile.append(name)
                elif exp - time.time() < 5 * 86400:
                    expiring.append(name)
            self._session_report.emit(dead, expiring, fragile)

        threading.Thread(target=work, name="session-refresh", daemon=True).start()

    def _on_session_report(self, dead, expiring, fragile):
        """Surface session problems proactively instead of at QR-scan time."""
        if dead:
            self.tray.showMessage(
                "Riot 2FA — re-add needed",
                "Riot ended the session for: "
                + ", ".join(dead)
                + ".\nUse 'Add via Login' once to restore it.",
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )
        elif expiring:
            self.tray.showMessage(
                "Riot 2FA — session expiring",
                "Expiring soon: " + ", ".join(expiring) + ".",
                QSystemTrayIcon.MessageIcon.Information,
                6000,
            )
        if fragile and not self._fragile_warned:
            # Once per run — it can only be cleared by re-adding, so repeating it
            # every 6 hours would just be noise.
            self._fragile_warned = True
            self.tray.showMessage(
                "Riot 2FA — sessions will expire",
                "These were added without 'Stay signed in' and will expire in a few "
                "days no matter what: " + ", ".join(fragile) + ".\nRe-add them once "
                "to make them permanent.",
                QSystemTrayIcon.MessageIcon.Warning,
                10000,
            )

    def _check_update(self):
        info = updater.check_for_update()
        if info:
            self._update_found.emit(info)

    def _on_update_found(self, info):
        reply = QMessageBox.question(
            self,
            "Update available",
            f"A newer version ({info['version']}) is available "
            f"(you have {__version__}).\n\nWould you like to update?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if updater.is_frozen() and info.get("asset_url"):
            try:
                self.fcm.stop()
                updater.apply_exe_update(info["asset_url"])
                return
            except Exception:
                pass
        webbrowser.open(info["url"])

    def _setup_tray(self):
        icon = QIcon(ICON_PATH)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Riot 2FA")
        menu = QMenu()
        menu.addAction("Show", self._show_from_tray)
        if autostart.available():
            self._autostart_action = QAction("Start with Windows", self, checkable=True)
            self._autostart_action.toggled.connect(self._toggle_autostart)
            menu.addAction(self._autostart_action)
        menu.addSeparator()
        menu.addAction("Quit", self._quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _init_autostart(self):
        """Enable launch-at-login on first run (the user asked for it), refresh
        the stored path on later runs, and reflect the state in the tray toggle."""
        if not autostart.available():
            return
        # Track explicit opt-OUT rather than "first run done": if the registry value
        # goes missing for any reason, this re-creates it instead of silently staying
        # off forever. Regular launches are what keep the Riot sessions alive.
        opted_out = os.path.join(APPDATA_DIR, "autostart.optout")
        try:
            if not os.path.exists(opted_out):
                autostart.enable()  # also refreshes the path if the exe moved
        except Exception:
            pass
        self._autostart_action.blockSignals(True)
        self._autostart_action.setChecked(autostart.is_enabled())
        self._autostart_action.blockSignals(False)

    def _toggle_autostart(self, checked):
        """User toggled the tray checkbox — record the choice so it sticks."""
        opted_out = os.path.join(APPDATA_DIR, "autostart.optout")
        try:
            os.makedirs(APPDATA_DIR, exist_ok=True)
            if checked:
                autostart.enable()
                if os.path.exists(opted_out):
                    os.remove(opted_out)
            else:
                autostart.disable()
                open(opted_out, "w").close()
        except Exception:
            pass

    def start_in_tray(self):
        """Launch hidden in the tray (used for --startup at login)."""
        self.tray.showMessage(
            "Riot 2FA",
            "Started in the tray — click the tray icon to open.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self):
        self.fcm.stop()
        self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):

        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(
                "Riot 2FA",
                "Still running in the tray — you'll get login approval prompts here.",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def _populate(self):
        for c in self.cards:
            c.setParent(None)
            c.deleteLater()
        self.cards.clear()

        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        if not self.accounts:
            lbl = QLabel("No accounts yet — add one with the buttons above")
            lbl.setObjectName("emptyLabel")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.scroll_layout.addWidget(lbl)
        else:
            for acct in self.accounts:
                card = AccountCard(acct["name"], acct["seed"])
                card.remove_requested.connect(self._remove_account)
                card.copy_requested.connect(lambda: self.toast.popup("Copied to clipboard"))
                self.cards.append(card)
                self.scroll_layout.addWidget(card)
        self.scroll_layout.addStretch()

    def _tick(self):
        now = time.time()
        elapsed = now % PERIOD
        remaining_frac = 1.0 - elapsed / PERIOD
        remaining_sec = int(PERIOD - elapsed)
        step = int(now // PERIOD)
        code_changed = step != self._last_step
        self._last_step = step

        for card in self.cards:
            card.update_bar(remaining_frac, remaining_sec)
            if code_changed:
                card.refresh_code()

    def _save_and_refresh(self):
        save_accounts(self.accounts)
        self._populate()

    def _remove_account(self, name, seed):
        self.accounts = [
            a for a in self.accounts if not (a["name"] == name and a["seed"] == seed)
        ]
        self._save_and_refresh()

    def _on_push(self, data):
        """A login attempt arrived via push — show the approve/deny prompt."""
        puuid = data.get("puuid")
        account = next(
            (a for a in self.accounts if a.get("puuid") and a["puuid"] == puuid), None
        )
        if account is None:

            return

        self.tray.showMessage(
            "Riot login attempt",
            f"Approve or deny the login for {account.get('name', 'your account')}.",
            QSystemTrayIcon.MessageIcon.Warning,
            5000,
        )

        prompt = MfaPromptDialog(data, account, self)
        self._active_prompts.append(prompt)

        def _cleanup(_result, p=prompt):
            if p in self._active_prompts:
                self._active_prompts.remove(p)
            verb = p.outcome or "dismissed"
            if self.isVisible():
                self.toast.popup(f"Login {verb}")

        prompt.finished.connect(_cleanup)
        prompt.show()
        prompt.raise_()
        prompt.activateWindow()

    def _add_via_login(self):
        dlg = LoginBrowserDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        cookies = dlg.cookies
        csrf = dlg.csrf_token
        if not csrf or not cookies:
            QMessageBox.warning(self, "Error", "Login OK but the session could not be captured.")
            return

        try:
            csrf = fetch_new_csrf_token(cookies)
        except Exception:
            csrf = dlg.csrf_token

        name = dlg.riot_id or "Unknown"
        puuid = dlg.puuid
        bearer = dlg.id_token  # the account SPA's access token (may be None)

        try:
            factors = fetch_mfa_factors(cookies, csrf)
        except Exception:
            factors = None
        if factors is not None and not is_email_mfa_enabled(factors):
            QMessageBox.warning(
                self,
                "Email 2FA required",
                "You must enable email-based Multi-Factor Authentication on your "
                "Riot account before you can add it here.\n\nTurn it on at "
                "account.riotgames.com (Security → Multi-factor authentication), "
                "then try again.",
            )
            return

        try:
            seed = enable_mfa(cookies, csrf)
        except Exception as exc:
            QMessageBox.critical(self, "Enable MFA Failed", str(exc))
            return

        account = {"name": name, "seed": seed}
        if puuid:
            account["puuid"] = puuid
        if dlg.sso_cookies.get("ssid"):
            account["sso"] = dlg.sso_cookies
            account["sso_expires"] = dlg.sso_expires

        access_token = self._valid_access_token(account)
        verify_tok = bearer or access_token
        if verify_tok:
            try:
                verify_mfa(verify_tok, seed)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Verify Warning",
                    f"MFA enabled but verification failed:\n{exc}\n\nSeed saved anyway.",
                )

        push_note = self._register_push(access_token, bearer, puuid)

        self.accounts.append(account)
        self._save_and_refresh()
        QMessageBox.information(self, "Success", f"2FA added for {name}{push_note}")

        if account.get("sso") and not account.get("sso_expires"):
            # Riot gave us a session-scoped ssid, i.e. "Stay signed in" didn't take.
            # That session has a fixed short life no refresh can extend, so say so
            # now rather than letting it quietly die in a few days.
            QMessageBox.warning(
                self,
                "Session won't persist",
                f"{name} was added, but Riot issued a non-persistent session — the "
                "'Stay signed in' option didn't take effect.\n\nIt will stop working "
                "within a few days. Remove the account and add it again, making sure "
                "'Stay signed in' is ticked on the Riot login page.",
            )

    def _register_push(self, access_token, id_tok, puuid):
        """Register this account's FCM device so logins push here. Best-effort."""
        if not puuid:
            return "\n\n(Push approval unavailable: could not read account id.)"

        tokens = [t for t in (access_token, id_tok) if t]
        if not tokens:
            return "\n\n(Push approval unavailable: missing access token.)"
        fcm_token = self.fcm.wait_for_token(30)
        if not fcm_token:
            return "\n\n(Push approval unavailable: listener not ready.)"
        last_exc = None
        for token in tokens:
            try:
                register_mfa_push_device(token, fcm_token)
                return "\n\nPush approval is enabled for this account."
            except Exception as exc:
                last_exc = exc
        return f"\n\n(Push approval registration failed: {last_exc})"

    def _valid_access_token(self, account):
        """A currently-valid access token for the account.

        Mints a fresh token from the stored SSO cookies (the persistent session —
        no re-login needed), so it always carries the current scopes (including
        session.auth for QR). Falls back to a cached token only if minting fails.
        """
        sso = account.get("sso")
        if sso:
            try:
                token, refreshed, expires_at = mint_access_token(sso)
            except SessionExpired:
                # Genuinely dead — mark it so the UI can say "re-add" with confidence.
                if not account.get("session_dead"):
                    account["session_dead"] = True
                    save_accounts(self.accounts)
                return None
            except Exception:
                # Transient (network / Riot 5xx). Leave the stored cookies ALONE and
                # fall through to the cached token; never mark the account dead.
                token, refreshed, expires_at = None, None, None
            if refreshed is not None:  # only ever persist cookies from a successful mint
                account["sso"] = refreshed
                account["sso_expires"] = expires_at
                account.pop("session_dead", None)
                if token:
                    account["access_token"] = token
                save_accounts(self.accounts)
            if token:
                return token
        token = account.get("access_token")
        if token and is_valid_jwt(token):
            return token
        return None

    def _pick_account(self):
        """Choose which stored account to sign in with (the QR doesn't say which).

        Always asks, so you explicitly pick the account every time.
        """
        usable = [a for a in self.accounts if a.get("sso")]
        if not usable:
            QMessageBox.warning(
                self,
                "No usable account",
                "QR sign-in needs an account added via 'Add via Login'. "
                "Accounts added manually or before this feature can't sign in.",
            )
            return None

        labels = [a.get("name", "Unknown") for a in usable]
        for i, label in enumerate(labels):
            if labels.count(label) > 1:
                tail = (usable[i].get("puuid") or "")[:6] or str(i + 1)
                labels[i] = f"{label}  ·  {tail}"
        label, ok = QInputDialog.getItem(
            self, "Sign in with QR", "Choose the account to sign in:", labels, 0, False
        )
        if not ok:
            return None
        return usable[labels.index(label)]

    def _scan_qr(self):
        scanner = QrScannerDialog(self)
        if scanner.exec() != QDialog.DialogCode.Accepted or not scanner.result_text:
            return
        suuid, cluster = parse_qr_login(scanner.result_text)
        if not suuid or not cluster:
            QMessageBox.warning(
                self,
                "Not a Riot QR",
                "That QR code isn't a Riot sign-in code.",
            )
            return

        account = self._pick_account()
        if account is None:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            token = self._valid_access_token(account)
            if not token:
                QApplication.restoreOverrideCursor()
                if account.get("session_dead"):
                    QMessageBox.warning(
                        self,
                        "Session expired",
                        f"Riot ended the saved session for {account.get('name')}.\n\n"
                        "Re-add it once via 'Add via Login' — it will then stay signed "
                        "in as long as you open this app at least once a month.",
                    )
                else:
                    QMessageBox.warning(
                        self,
                        "Couldn't reach Riot",
                        f"Temporary problem refreshing {account.get('name')}.\n\n"
                        "Your saved session is still intact — check your connection "
                        "and try again. No need to re-add the account.",
                    )
                return
            try:
                info = qr_session_info(token, suuid, cluster)
            except Exception as exc:
                info = {}
                self._qr_warn("Could not load the sign-in request", exc)
                return
        finally:
            QApplication.restoreOverrideCursor()

        confirm = QrConfirmDialog(account.get("name", "Account"), info, self)
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = qr_approve(token, suuid, cluster, remember=True)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self._qr_warn("Sign-in failed", exc)
            return
        QApplication.restoreOverrideCursor()

        if result.get("success") is True or result == {}:
            self.toast.popup("Signed in ✓")
            QMessageBox.information(
                self, "Signed in", f"Approved the QR sign-in for {account.get('name')}."
            )
        else:
            QMessageBox.warning(
                self, "Sign-in not confirmed", f"Riot returned: {result}"
            )

    def _qr_warn(self, title, exc):
        detail = str(exc)
        try:
            if hasattr(exc, "response") and exc.response is not None:
                detail = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        except Exception:
            pass
        QMessageBox.warning(self, title, detail)

    def _add_manually(self):
        dlg = ManualAddDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_data:
            self.accounts.append(dlg.result_data)
            self._save_and_refresh()
