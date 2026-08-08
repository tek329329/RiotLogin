# RiotLogin — Session Handoff

**Last updated:** 2026-07-29
**Repo:** `C:\Users\timot\Desktop\RiotLogin` (fork of `Askin242/RiotGames-Mobile-2FA-Bypass`)
**Owner's fork:** https://github.com/tek329329/RiotLogin

---

## 1. What this project is

A PyQt6 desktop port of the Riot Games mobile 2FA app. It stores Riot account TOTP
seeds locally and:

- generates live 6-digit 2FA codes for each account,
- receives **push login-approval prompts** (via a Firebase/FCM listener that emulates
  the Riot mobile app) with Allow/Refuse buttons,
- **signs the Riot client in by scanning its on-screen QR code** (no phone needed),
- lives in the system tray and auto-updates from GitHub releases.

Account data lives in `%APPDATA%\Riot2FA\` — **not** in the repo folder. This matters:
you can replace the `.exe` freely without touching accounts.

```
%APPDATA%\Riot2FA\
  accounts.json         # accounts: name, seed, puuid, sso (cookie jar), access_token
  fcm_credentials.json  # one-time FCM device registration, shared by all accounts
  autostart.flag        # marker: "first-run autostart setup already done" (see §6)
```

**Never print or commit `accounts.json`** — `seed` is the TOTP secret and `sso` is a
live session. Inspect keys only, never values.

---

## 2. The original problem (and the fix)

### Symptom
Saved accounts stopped working after ~2 days: *"Session expired — re-add via Add via Login."*

### Root cause
The TOTP **seeds** never expire — that was never the issue. What expired was the saved
**RSO session** (`sso` cookie jar, chiefly `ssid`) that powers QR sign-in and push approval.

`mint_access_token()` in `app/api/riot_api.py` POSTs the stored `ssid` to Riot's authorize
endpoint. **Riot rotates the SSO cookies on that call** (a sliding expiry, exactly like the
real mobile app does). The original code **discarded the response cookies** and kept reusing
the originals — so the saved session was frozen at login time and simply decayed to its fixed
~2-day expiry, even though Riot had been handing back fresh, longer-lived cookies the whole
time. Compounding it, refresh only happened when the user actively did a QR sign-in, so an
idle session was never bumped.

### The fix (two parts)
1. **Persist the rotated cookies.** `mint_access_token()` now returns
   `(access_token, refreshed_sso_cookies)`; the caller writes the refreshed jar back to
   `accounts.json`. This is the actual root-cause fix.
2. **Refresh proactively.** A background timer refreshes every account's session
   **20s after launch** and **every 6 hours** while running, so the sliding window keeps
   moving forward instead of only on manual QR use.

### ✅ Empirically verified 2026-07-29 (live API probe)

An earlier draft of this doc claimed the fix was proven because `accounts.json` got
rewritten after launch. **That reasoning was wrong** — `dirty` is set by a successful
token mint alone, so the write proved the mint worked, *not* that any cookie rotated.
A direct probe against Riot settled it:

| Question | Answer |
|---|---|
| Does Riot rotate `ssid` on the authorize call? | **Yes** — live accounts get a brand-new `ssid`. |
| How long is a session? | **`ssid expires` = exactly 30.0 days**, and every successful mint resets it to a full 30 days. |
| How does a dead session present? | **HTTP 200** with `type:"auth"` (*not* an HTTP error), and Riot omits `ssid`/`csid` from the response. |

**Practical rule: open the app at least once every 30 days and sessions renew forever.**

> ⚠️ **This was only half the story — see §3 Round 3.** Everything above is true *only
> for a session created with "Stay signed in" ticked.* Riot's login page defaults that
> box to **off**, and a login without it yields a **session-scoped `ssid`** that has no
> expiry at all and that Riot never extends — so no amount of refreshing keeps it alive.
> That, not the 30-day window, is why accounts kept expiring after ~a week.

### Important limitation
The fix **prevents** future expiry; it cannot **reverse** expiry that already happened.
An account showing "re-add" has a dead `ssid` — no live session remains to rotate — so it
needs one manual re-add. On 2026-07-29, 4 of 6 accounts were in this state: they were never
re-added after the fix landed on 07-24, so they still carried pre-fix (already dead)
sessions. The 2 that survived were the actively-used ones holding post-fix sessions.

---

## 3. All changes made

### ✅ Already committed **and pushed** — commit `ec7bf67 "fix"` (= `origin/main`)

The **entire core session fix** is committed and self-consistent (verified: the 2-tuple
return and its caller's unpacking are both in `HEAD`).

| File | Change |
|---|---|
| `app/api/riot_api.py` | Added `_merge_sso_cookies()`; `mint_access_token()` returns `(token, refreshed_cookies)`, capturing Riot's rotated jar. |
| `app/ui/main_window.py` | `_valid_access_token()` persists rotated cookies; `_refresh_sessions()` + 6h timer + 20s-after-launch kick. |

### 🔲 Uncommitted working-tree edits (everything else)

| File | Change |
|---|---|
| `app/version.py` | `GITHUB_REPO` repointed `Askin242/...` → `tek329329/RiotLogin` so auto-update won't overwrite the patched build. |
| `app/ui/qr_scanner_dialog.py` | Default scanner **300×300 → 480×510**; opens centered on the cursor's screen. |
| `app/core/autostart.py` | **[new file]** HKCU `Run` key registration; `available/is_enabled/enable/disable`. |
| `app/main.py` | Honors `--startup`: starts hidden in tray instead of showing the window. |
| `app/ui/main_window.py` | *(autostart wiring + tray toggle only — its session-fix half is already committed)* |

> Note: `HEAD` still carries the **old** `GITHUB_REPO`. So the pushed fork build would
> auto-update to the *original author's* release. The repoint is only in the working tree
> (and in the built `.exe`). Commit it before publishing any release from this fork.

### 🔧 Round 2 (2026-07-29) — session-health hardening

Prompted by "unused accounts keep expiring". Diagnosis above; changes:

| File | Change |
|---|---|
| `app/api/riot_api.py` | New `SessionExpired` exception. `mint_access_token()` now returns a **3-tuple** `(token, refreshed, ssid_expires_at)` and **raises** `SessionExpired` on `type != "response"`. Critically, it now returns **no cookies** in that case. |
| `app/ui/main_window.py` | Persists cookies **only on a successful mint**; marks `session_dead`; stores `sso_expires`; tray warnings for dead/expiring-within-5-days accounts; QR error distinguishes expired vs. transient; autostart self-heals. |

**The bug this fixed:** the previous code persisted whatever cookies came back whenever
they differed. On a *failed* auth Riot returns a signed-out cookie set, so a **transient**
network/5xx blip could overwrite part of a still-healthy jar — and every hiccup showed the
alarming "re-add it" dialog, prompting re-adds that were never actually needed.

### 🔑 Round 3 (2026-08-08) — the actual root cause: `remember` at login

Prompted by "an account expired again ~8 days after re-adding", which the 30-day
sliding-window theory cannot explain. Rounds 1–2 were correct but incomplete: they made
cookie *rotation* work, while the thing that actually kills accounts is decided once, at
**login time**, and no refresh can undo it.

#### Evidence (live probe, 2026-08-08, all 8 accounts)

Every account mints fine (`type:"response"`, HTTP 200, `ssid` value rotates). But look at
the `Set-Cookie` Riot returns:

| Account | `ssid` attributes |
|---|---|
| `oliviaa#LOVE` | `Expires=Mon, 07 Sep 2026 …` → **persistent, 30 days** |
| the other 7 | *(no Expires / Max-Age)* → **session cookie** |

So `ssid` rotates for all 8, but only one carries an expiry. A session-scoped `ssid` has
**no expiry to slide**; Riot pins its server-side life to a fixed short window that
minting does not extend. `sso_expires` was `None` for exactly those 7 — the tell was
already in `accounts.json` and nothing was reading it.

#### Why

Riot's login SPA (`rso-authenticator-ui`) submits:

```js
{type: "auth", remember: k, language: …, riot_identity: {username, password}}
```

`k` is the **"Stay signed in"** checkbox — `<input id="rememberme">`, label
`REMEMBER_ME {days: 30}`, and it **defaults to `checked: false`** (verified live).
Tick it → 30-day persistent `ssid` that every mint pushes back out. Leave it → session
cookie → dead in days. Nothing about the *reauth* call can change this after the fact:
adding `remember`/`rememberMe`/`persist` to `_REAUTH_BODY` was tested against a live
account and changed the `Set-Cookie` attributes not at all.

#### The fix

| File | Change |
|---|---|
| `app/ui/login_browser_dialog.py` | New `_REMEMBER_JS`, injected alongside `_BEARER_JS` (`_install_bearer_capture` → `_install_page_scripts`). Patches `fetch`/`XHR.send` to force `remember: true` on the `type:"auth"` login body, **and** ticks `#rememberme` via `.click()` so React state updates (the social-login buttons read `remember` off state into a redirect URL, not a body). Also captures the `ssid` cookie's real expiry (`_cookie_expiry`, `_expiry_for_host`) and exposes it as `dlg.sso_expires`. |
| `app/ui/main_window.py` | Stores `sso_expires` at add time; warns immediately if Riot still handed back a non-persistent session; `_refresh_sessions` now reports a third **`fragile`** bucket (`sso_expires` falsy) with a once-per-run tray warning. |
| `tools/check_sessions.py` | Reports **FRAGILE** for a session-scoped `ssid`. |

**Verified live against `authenticate.riotgames.com`** (the injected script run verbatim
in a real page): checkbox `false → true`; `type:"auth"` body rewritten to
`remember:true`; `type:"re-auth"`, `type:"multifactor"` and non-JSON bodies all passed
through byte-identical. Confirmed from the bundle that the SPA calls
`fetch(url, {method:"PUT", body:"<json string>"})`, which is exactly the shape patched.

#### Limitation (same shape as before)

This fixes sessions created **from now on**. The 7 existing fragile accounts must be
re-added once each — their session-scoped `ssid` cannot be upgraded in place.

### Key code notes
- `_merge_sso_cookies()` only overlays cookie names in `SSO_COOKIE_NAMES` and preserves
  any Riot didn't re-send. Unit-tested against a real `requests` jar including the tricky
  same-name-different-domain case (`ssid` on `auth.riotgames.com` vs `tdid` on `.riotgames.com`).
- `mint_access_token()` is a **3-tuple now** `(token, refreshed, expires_at)` and **raises
  `SessionExpired`**. One caller (`main_window._valid_access_token`). New callers must
  unpack all three *and* catch `SessionExpired` separately from generic exceptions —
  conflating them is exactly the bug that caused spurious "re-add" prompts.
- **Never persist cookies from a non-`response` authorize reply.** They're a signed-out
  set and will corrupt a good jar.
- `_refresh_sessions()` runs network I/O on a worker thread; it calls `_valid_access_token`,
  which mutates `self.accounts` and calls `save_accounts`. Single background thread, so no
  concurrent writers in practice.

---

## 4. Build / run

**Environment:** Python 3.14.6, venv at `.\.venv\`. All deps (PyQt6 6.11, WebEngine,
opencv-python 5.0, firebase-messaging 0.4.5, PyInstaller 6.21) install cleanly on 3.14.

```powershell
# run from source
.\.venv\Scripts\python.exe main.py

# build the exe  (~3-5 min, produces a ~258 MB onefile)
.\.venv\Scripts\python.exe -m PyInstaller --clean -y --workpath build\_work --distpath dist build\Riot2FA.spec
```

**Build gotchas:**
- **PyInstaller cannot overwrite a running exe** (`PermissionError: Access is denied`).
  Fully **Quit from the tray** first (closing the window only hides it), or build to a
  different `--distpath` (that's why `dist_new\` exists) and copy over later.
- `build/build.py` also runs **PyArmor** obfuscation, which upstream used to reduce AV
  false positives. It was **skipped** (`--no-obfuscate`) — unneeded and riskier on 3.14.
  Consequence: the unsigned exe may trip SmartScreen ("More info → Run anyway"). Functionally identical.
- **Verifying a string is baked into a onefile exe by scanning raw bytes does not work** —
  the bytecode is zlib-compressed. But you *can* verify directly by reading the PYZ:

  ```python
  from PyInstaller.archive.readers import ZlibArchiveReader
  import types
  r = ZlibArchiveReader(r"build/_work/Riot2FA/PYZ-00.pyz")
  code = r.extract("app.ui.login_browser_dialog")   # returns a code object
  ```
  Walk `co_consts` recursively for string literals, and `co_names`/`co_varnames` for
  attribute and variable names (an attribute like `isSessionCookie` is **not** a const,
  so its absence from `co_consts` means nothing). `build/_work/Riot2FA/PYZ-00.toc` also
  lists every packed module.
- **Check `build/obf/` does not exist before building.** The spec silently prefers
  `build/obf/main.py` (PyArmor output) as the entry point if present, so a stale one
  would pack old code over your edits without a word.

---

## 5. Current state

| Item | State |
|---|---|
| `dist\Riot2FA.exe` | ✅ Round-3 build (2026-08-08 14:02). Fix verified present in `PYZ-00.pyz`, not just by provenance. In use. |
| `dist_new\Riot2FA.exe` | **Stale** (2026-07-31, pre-round-3). Delete it — it is now a trap. |
| Running now | Relaunched 2026-08-08 14:03 with `--startup` |
| Accounts | 8. **1 healthy** (`oliviaa#LOVE`, 30.0d rolling); **7 FRAGILE** — session-scoped `ssid`, need one re-add each on this build. |
| Git | On `main`, ahead of `origin/main` by the round-1/2/3 commit. Remotes: `origin`=user's fork, `upstream`=original |

**Cleanup candidates** (~2 GB): `dist_new\`, `build\_work\`, `build\_*.log`, and `.venv\`
(only if you won't rebuild).

---

## 6. ✅ RESOLVED: autostart entry going missing

**Observed right now:** the app **is** running with `--startup` (so Windows *did* launch it
at login today), but `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Riot2FA`
**no longer exists**. So it will **not** autostart at the next login.

Cause is unconfirmed — most likely the "Start with Windows" tray checkbox was unchecked, or
a cleanup/security tool removed the value. (Task Manager's *Startup apps* toggle would **not**
delete it; it writes a separate `StartupApproved` flag.)

**This will not self-heal, by design flaw.** `_init_autostart()` logic:

```python
if not os.path.exists(marker):   # first run only
    autostart.enable(); create marker
elif autostart.is_enabled():     # only refreshes if ALREADY enabled
    autostart.enable()
```

Once `autostart.flag` exists and the registry value is gone, **neither branch ever
re-enables it**. It silently stays off forever.

**Fixed in round 2.** The marker's meaning is inverted: `autostart.optout` is written only
when the user *unchecks* the tray box, and every launch re-enables unless that file exists.
This self-heals a missing registry value while still honouring a deliberate opt-out.
(`autostart.flag` is now unused and can be deleted.) The tray checkbox also uses
`blockSignals()` when syncing so reflecting state can't itself trigger a toggle.

---

## 7. Behavioral notes for the user

- **Tick "Stay signed in" when adding an account.** As of round 3 the app forces this
  for you, but it is the single thing that decides whether an account lasts a month or a
  week. `tools/check_sessions.py` says **FRAGILE** if one ever slips through.
- **The app does not need to run 24/7.** Sessions live on disk; shutting down is fine.
  Each launch refreshes 20s in, and each successful refresh resets Riot's clock to a
  **full 30 days**. So the only real rule is: **open the app at least once a month.**
  Autostart handles this automatically. A 30+ day gap with the app never opened is the
  one thing that still forces a re-add.
- **Push approval prompts only arrive while the app is running** — that's the one reason
  to keep it in the tray during play.
- **Auto-update now points at the user's own fork**, which has no releases. So the update
  prompt should simply never appear. If releases are ever published there, tags must be
  `>` `app/version.py`'s `__version__` (currently `2.0.3`) to trigger, and the asset must
  end in `.exe`.

---

## 8. Suggested next steps

1. **Rebuild the exe, then re-add the 7 FRAGILE accounts** (everything except
   `oliviaa#LOVE`). Run `tools/check_sessions.py` for the current list. Unavoidable — a
   session-scoped `ssid` can't be upgraded in place, and re-adding on a *pre*-round-3
   build would just recreate the same fragile session.
2. **Commit the remaining work** (QR sizing, autostart, updater repoint, round-2 session
   hardening) — only the original core fix is committed so far (`ec7bf67`).
3. **Optional cleanup** of `dist_new\` / `build\_work\` / logs, and the now-unused
   `%APPDATA%\Riot2FA\autostart.flag`.
4. **Validate at ~35 days**: if the re-added accounts are still alive, the 30-day rolling
   renewal is fully proven end to end.

### Handy diagnostic — `tools/check_sessions.py`
Checks session health any time without touching the app, printing **no** secrets:

```powershell
.\.venv\Scripts\python.exe tools\check_sessions.py
```

Reports each account as **LIVE** (with `ssid_rotated` and days-until-expiry), **EXPIRED**
(needs one re-add), or **TRANSIENT** (network issue — stored cookies untouched). Fastest
way to answer "is this account actually dead, or was that just a blip?"

### Known cosmetic issues (deliberately left alone — not bugs)
- `riot_api.py` has inconsistent User-Agent strings (a `Chrome/149` constant vs. hardcoded
  `Chrome/150`/Brave in the header dict).
- `fetch_new_csrf_token()` parses HTML by string-splitting (fragile, but guarded by
  try/except at its only call site).
