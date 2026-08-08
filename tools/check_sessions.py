import json, os, sys, time
sys.path.insert(0, r"C:\Users\timot\Desktop\RiotLogin")
from app.api import mint_access_token, SessionExpired

p = os.path.join(os.getenv("APPDATA"), "Riot2FA", "accounts.json")
for a in json.load(open(p, encoding="utf-8")):
    n = a.get("name", "?")
    sso = a.get("sso") or {}
    try:
        tok, ref, exp = mint_access_token(sso)
        rotated = ref.get("ssid") != sso.get("ssid")
        if exp:
            days = (exp - time.time()) / 86400
            print(f"{n:<24} LIVE       token={bool(tok)}  ssid_rotated={rotated}  expires_in={days:.1f}d")
        else:
            # No expiry on the ssid = added without "Stay signed in". Riot never
            # extends these, so refreshing can't keep them alive.
            print(f"{n:<24} FRAGILE    session-scoped ssid - re-add with 'Stay signed in'")
    except SessionExpired as e:
        print(f"{n:<24} EXPIRED    needs one re-add  ({e})")
    except Exception as e:
        print(f"{n:<24} TRANSIENT  ({type(e).__name__}) -> stored cookies left untouched")
