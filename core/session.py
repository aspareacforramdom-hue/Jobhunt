"""
PHASE 2 — auth, sessions, user scoping.

Three people share one laptop-hosted portal. That is a low threat model,
but the rules are absolute anyway because the cost of getting them wrong
is someone reading another person's job search:

  - passwords are scrypt-hashed with a per-user salt
  - a plaintext password is never written to a file or a log, ever,
    including on the seeding path that generates them
  - the session cookie is signed, so a user id cannot be edited by hand
  - every request resolves to exactly one user id, and every query
    downstream filters on it

The cookie signer is stdlib HMAC rather than a library, so there is one
less dependency to keep current on a machine that will not be patched.
"""

import base64
import hashlib
import hmac
import os
import secrets
import string
import time

from core import db

COOKIE_NAME = "careerloop_session"
SESSION_MAX_AGE = 14 * 24 * 3600          # seconds
SECRET_PATH = os.environ.get("CAREERLOOP_SECRET_FILE", "data/.session_key")

# scrypt parameters. n=2**14 costs ~50ms per hash here, which is
# irrelevant for three users logging in and expensive for a guesser.
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32

# Ambiguous glyphs removed — these get read off a screen and typed.
_ALPHABET = "".join(c for c in string.ascii_letters + string.digits
                    if c not in "O0Il1")


# ------------------------------------------------------------
# Passwords
# ------------------------------------------------------------
def hash_password(password, salt=None):
    """Returns (hash_hex, salt_hex). The password is not retained."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"),
                            salt=bytes.fromhex(salt),
                            n=_N, r=_R, p=_P, dklen=_DKLEN)
    return digest.hex(), salt


def verify_password(password, password_hash, salt):
    try:
        candidate, _ = hash_password(password, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, password_hash or "")


def generate_password(length=14):
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


# ------------------------------------------------------------
# Signing key
# ------------------------------------------------------------
def get_secret():
    """
    From the environment if set, otherwise a generated key on disk.

    The file holds a signing key, not a password — losing it logs
    everyone out and nothing more.
    """
    env = os.environ.get("CAREERLOOP_SECRET")
    if env:
        return env.encode("utf-8")

    key = _read_secret()
    if key:
        return key

    # Created exclusively, so that exactly one caller can win.
    #
    # The portal answers requests on several threads and starts a boot
    # scan on another, all of which reach this within the same few
    # milliseconds on a first run. Plain "check then write" let two of
    # them generate different keys and both write: whoever wrote second
    # invalidated the cookie the first had just issued, and that user
    # was bounced back to the sign-in page with no explanation.
    os.makedirs(os.path.dirname(SECRET_PATH) or ".", exist_ok=True)
    fresh = secrets.token_hex(32).encode("ascii")
    try:
        fd = os.open(SECRET_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Someone else got there first. Theirs is the real key.
        return _read_secret() or fresh
    try:
        os.write(fd, fresh)
    finally:
        os.close(fd)
    try:
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass                       # best effort; Windows ACLs differ
    return fresh


def _read_secret():
    try:
        with open(SECRET_PATH, "rb") as f:
            return f.read().strip() or None
    except OSError:
        return None


# ------------------------------------------------------------
# Session cookie
# ------------------------------------------------------------
def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_session(user_id, issued_at=None):
    issued = time.time() if issued_at is None else issued_at
    payload = f"{int(user_id)}:{int(issued)}".encode("ascii")
    sig = hmac.new(get_secret(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def read_session(token, max_age=SESSION_MAX_AGE):
    """Returns a user id, or None for anything it cannot fully verify."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    try:
        payload = _unb64(body)
        given = _unb64(sig)
    except (ValueError, TypeError):
        return None

    expected = hmac.new(get_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(given, expected):
        return None

    try:
        user_id, issued = payload.decode("ascii").split(":")
        user_id, issued = int(user_id), int(issued)
    except (ValueError, UnicodeDecodeError):
        return None

    if max_age and time.time() - issued > max_age:
        return None
    return user_id


# ------------------------------------------------------------
# Login / user resolution
# ------------------------------------------------------------
def authenticate(conn, username, password):
    """Returns the user row, or None. Never logs the attempt's password."""
    user = db.get_user_by_name(conn, (username or "").strip())
    if not user:
        # Hash anyway so a missing username and a wrong password take
        # the same time to answer.
        hash_password(password or "")
        return None
    if not verify_password(password or "", user["password_hash"],
                           user["salt"]):
        return None
    return user


def user_from_cookie(conn, token):
    user_id = read_session(token)
    return db.get_user(conn, user_id) if user_id else None


# ------------------------------------------------------------
# Seeding
# ------------------------------------------------------------
def create_account(conn, username, password=None, is_admin=False,
                   telegram_chat_id=None):
    """Returns (user_id, password). The caller shows the password once
    and must not persist it."""
    password = password or generate_password()
    pw_hash, salt = hash_password(password)
    user_id = db.create_user(conn, username, pw_hash, salt,
                             telegram_chat_id, int(is_admin))
    return user_id, password


def seed_users(conn, usernames):
    """
    Create any of these accounts that don't exist yet.

    Existing accounts are left alone and reported as skipped — re-running
    this must never reset a password someone is already using.
    """
    created, skipped = [], []
    for i, name in enumerate(usernames):
        if db.get_user_by_name(conn, name):
            skipped.append(name)
            continue
        user_id, password = create_account(conn, name, is_admin=(i == 0))
        created.append({"id": user_id, "username": name,
                        "password": password, "is_admin": i == 0})
    return created, skipped
