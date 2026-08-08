import re
import threading

from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel
from PyQt6.QtCore import QTimer, QUrl, pyqtSignal
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
)

from app.api import SSO_COOKIE_NAMES, fetch_account_user, riot_id_from_user, puuid_from_user


def _domain_matches(cookie_domain, host):
    d = (cookie_domain or "").lstrip(".")
    return bool(d) and (host == d or host.endswith("." + d))


# Injected into every page: capture the Authorization bearer the account SPA
# sends to api.account.riotgames.com (used later for verify / push). Riot no
# longer exposes an id_token cookie, so this is how we recover an access token.
_BEARER_JS = r"""
(function(){
  if (window.__rso_hook) return; window.__rso_hook = true;
  window.__rso_bearer = window.__rso_bearer || '';
  var keep = function(v){
    if (v && /^Bearer\s+/i.test(v)) window.__rso_bearer = v.replace(/^Bearer\s+/i,'');
  };
  var of = window.fetch;
  if (of) window.fetch = function(input, init){
    try {
      if (init && init.headers) {
        var h = init.headers;
        if (h.get) keep(h.get('authorization') || h.get('Authorization'));
        else keep(h['authorization'] || h['Authorization']);
      }
    } catch(e){}
    return of.apply(this, arguments);
  };
  var os = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function(k, v){
    try { if (String(k).toLowerCase() === 'authorization') keep(v); } catch(e){}
    return os.apply(this, arguments);
  };
})();
"""


# Injected into every page: force Riot's "Stay signed in" on. It defaults to OFF,
# and a login without it yields a *session-scoped* `ssid` — one with no expiry that
# Riot never extends, so the saved session dies within days however often we refresh
# it. With it on Riot issues a 30-day `ssid` that every mint pushes back out again.
_REMEMBER_JS = r"""
(function(){
  if (window.__rso_remember) return; window.__rso_remember = true;

  // 1. Authoritative: the login POST carries `remember`. Force it on the initial
  //    auth prompt only — the re-auth prompt deliberately sends false.
  var force = function(body){
    try {
      if (typeof body !== 'string' || body.indexOf('"type"') < 0) return body;
      var o = JSON.parse(body);
      if (!o || o.type !== 'auth') return body;
      o.remember = true;
      return JSON.stringify(o);
    } catch(e) { return body; }
  };

  var of = window.fetch;
  if (of) window.fetch = function(input, init){
    try {
      if (init && init.body) init = Object.assign({}, init, {body: force(init.body)});
    } catch(e){}
    return of.call(this, input, init);
  };
  var os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(body){
    try { body = force(body); } catch(e){}
    return os.call(this, body);
  };

  // 2. Tick the visible checkbox too. The social sign-in buttons (Google, Xbox, ...)
  //    read `remember` straight off React state into a redirect URL rather than a
  //    request body, so the box itself has to be on for those to persist.
  var tries = 0;
  setInterval(function(){
    var el = document.getElementById('rememberme');
    if (!el || el.checked || tries >= 10) return;
    if (el.closest('[data-testid="mfa-rememberme-checkbox"]')) return;  // a different box
    tries++;
    el.click();  // click(), not .checked = true, so React's onChange actually runs
  }, 300);
})();
"""


def _install_page_scripts(profile):
    for name, source in (("rso_bearer", _BEARER_JS), ("rso_remember", _REMEMBER_JS)):
        script = QWebEngineScript()
        script.setName(name)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(source)
        profile.scripts().insert(script)


def _cookie_expiry(cookie):
    """Unix expiry of a persistent cookie; None for a session cookie.

    A session-scoped `ssid` is the signature of a login without "Stay signed in" —
    it is the one thing that makes a saved account expire.
    """
    if cookie.isSessionCookie():
        return None
    stamp = cookie.expirationDate()
    return stamp.toSecsSinceEpoch() if stamp.isValid() else None


class LoginBrowserDialog(QDialog):
    """Embedded Riot login.

    Riot dropped the `id_token` cookie in favour of a `sid` session cookie, so
    login is no longer detected by a cookie name. Instead, once we're on
    account.riotgames.com we probe `account/v1/user`; a 200 means signed in.
    Cookies are kept with their domain so same-named cookies from different Riot
    subdomains don't clobber each other (`cookies` = account jar for the API,
    `sso_cookies` = auth.riotgames.com jar with the `ssid` for later minting).
    """

    _probe_done = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Riot Account Login")
        self.resize(960, 720)
        self.cookies = {}
        self.sso_cookies = {}
        self.sso_expires = None
        self.csrf_token = None
        self.id_token = None
        self.puuid = None
        self.riot_id = None
        self.user_info = None
        self._all = []
        self._detected = False
        self._busy = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.status = QLabel("  Waiting for login...")
        self.status.setFixedHeight(28)
        self.status.setStyleSheet(
            "background-color:#111118; color:#666677; font-size:11px; padding-left:10px;"
        )
        lay.addWidget(self.status)

        # Off-the-record profile: a fresh session each time (so you can add a
        # different account), cookies captured live via cookieAdded.
        self.profile = QWebEngineProfile(self)
        _install_page_scripts(self.profile)
        self.profile.cookieStore().cookieAdded.connect(self._cookie_added)

        self._page = QWebEnginePage(self.profile, self)
        self.browser = QWebEngineView(self)
        self.browser.setPage(self._page)
        lay.addWidget(self.browser)

        self._probe_done.connect(self._on_probe_done)
        self.browser.setUrl(QUrl("https://account.riotgames.com/"))

        # Poll for the signed-in state (cheap GET) instead of guessing cookies.
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._try_detect)
        self._poll.start(1500)

    def _cleanup_browser(self):
        if self._page is None:
            return
        self._poll.stop()
        self._page.disconnect()
        self.browser.setPage(None)
        self._page.deleteLater()
        self._page = None

    def done(self, result):
        self._cleanup_browser()
        super().done(result)

    def _cookie_added(self, cookie):
        name = bytes(cookie.name()).decode("utf-8", errors="replace")
        value = bytes(cookie.value()).decode("utf-8", errors="replace")
        self._all.append((cookie.domain(), name, value, _cookie_expiry(cookie)))

    def _cookies_for_host(self, host):
        relevant = [t for t in self._all if _domain_matches(t[0], host)]
        relevant.sort(key=lambda t: len(t[0].lstrip(".")))
        jar = {}
        for _domain, name, value, _expiry in relevant:
            jar[name] = value
        return jar

    def _expiry_for_host(self, host, name):
        """Expiry of the cookie `name` that wins for `host` (None = session cookie)."""
        relevant = [
            t for t in self._all if t[1] == name and _domain_matches(t[0], host)
        ]
        relevant.sort(key=lambda t: len(t[0].lstrip(".")))
        return relevant[-1][3] if relevant else None

    def _try_detect(self):
        if self._detected or self._busy or self._page is None:
            return
        if self._page.url().host() != "account.riotgames.com":
            return
        self._busy = True
        self._page.toHtml(self._html_ready)

    def _html_ready(self, html):
        if self._detected or self._page is None:
            self._busy = False
            return
        m = re.search(
            r"""<meta\s+name=['"]csrf-token['"]\s+content=['"]([^'"]+)['"]""", html
        )
        account = self._cookies_for_host("account.riotgames.com")
        csrf_meta = m.group(1) if m else None
        csrf_cookie = account.get("a12l-csrf-prod")
        has_session = any(account.get(k) for k in ("sid", "id_token", "ssid"))
        if not (csrf_meta or csrf_cookie) or not has_session:
            self._busy = False
            return
        threading.Thread(
            target=self._probe, args=(account, csrf_meta, csrf_cookie), daemon=True
        ).start()

    def _probe(self, cookies, csrf_meta, csrf_cookie):
        user, used = None, None
        for csrf in [c for c in (csrf_meta, csrf_cookie) if c]:
            try:
                user = fetch_account_user(cookies, csrf)
                used = csrf
                break
            except Exception:
                continue
        self._probe_done.emit({"user": user, "cookies": cookies, "csrf": used})

    def _on_probe_done(self, res):
        self._busy = False
        if self._detected:
            return
        user = res.get("user")
        if not user or not puuid_from_user(user):
            return  # not signed in yet — the poll timer will try again
        self._detected = True
        self._poll.stop()
        self.cookies = res["cookies"]
        self.csrf_token = res["csrf"]
        auth = self._cookies_for_host("auth.riotgames.com")
        self.sso_cookies = {k: auth[k] for k in SSO_COOKIE_NAMES if auth.get(k)}
        self.sso_expires = self._expiry_for_host("auth.riotgames.com", "ssid")
        self.user_info = user
        self.riot_id = riot_id_from_user(user)
        self.puuid = puuid_from_user(user)
        self.status.setText("  Signed in — capturing session...")
        self.status.setStyleSheet(
            "background-color:#0e1a0e; color:#55aa55; font-size:11px; padding-left:10px;"
        )
        self._grab_bearer()

    def _grab_bearer(self, attempt=0):
        if self._page is None:
            self.accept()
            return

        def _got(token):
            if token:
                self.id_token = token
            if self.id_token or attempt >= 6:
                QTimer.singleShot(120, self.accept)
            else:
                QTimer.singleShot(400, lambda: self._grab_bearer(attempt + 1))

        self._page.runJavaScript("window.__rso_bearer || ''", _got)
