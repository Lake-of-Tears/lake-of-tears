"""Password hashing with the bcrypt library, and the 72-byte password limit."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
from schemas import ChangePasswordRequest, RegisterRequest  # noqa: E402

# Hashes made earlier by passlib with bcrypt 3.2.2 (test passwords): standard $2b$ bcrypt that any
# bcrypt library verifies.
PASSLIB_HASHES = {
    "short": (
        "CorrectPassword1!",
        "$2b$12$e8qjhDwBSB4yLdohqTAPcur5dny/5Ud08Lt3/1TQ0rF9DNxQ2V3xq",
    ),
    "unicode": (
        "pässwörd-日本語-🔐1",
        "$2b$12$XY2FF53L96wHfKG4g4MJdexTUreXTP67juujpy1HsHojXySYhi1R6",
    ),
    "exactly_72_bytes": (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "$2b$12$1w/Mu9KOv/mtdXvoedtK3OtYi3r/7rF.iqWTAIhtzjU5G33jWZp8u",
    ),
}


@pytest.mark.parametrize("name", list(PASSLIB_HASHES))
def test_standard_bcrypt_hashes_from_the_previous_library_verify(name):
    password, hashed = PASSLIB_HASHES[name]
    assert auth.verify_password(password, hashed)
    assert not auth.verify_password("wrong-password", hashed)


def test_new_hashes_use_the_same_format_and_cost():
    hashed = auth.hash_password("CorrectPassword1!")
    assert hashed.startswith("$2b$12$")
    assert auth.verify_password("CorrectPassword1!", hashed)
    assert not auth.verify_password("correctpassword1!", hashed)


def test_hashes_are_salted():
    assert auth.hash_password("same-password") != auth.hash_password("same-password")


def test_a_password_of_exactly_72_bytes_works():
    password = "a" * 72
    assert auth.verify_password(password, auth.hash_password(password))


def test_a_password_over_72_bytes_is_never_truncated():
    with pytest.raises(ValueError):
        auth.hash_password("a" * 73)
    hashed = auth.hash_password("a" * 72)
    # logging in with the stored 72 bytes plus extra characters must fail, not match the prefix
    assert auth.verify_password("a" * 73, hashed) is False


def test_a_malformed_hash_is_a_failed_login_not_an_error():
    assert auth.verify_password("anything", "not-a-bcrypt-hash") is False
    assert auth.verify_password("anything", "") is False


@pytest.mark.parametrize(
    ("password", "ok"),
    [
        ("a" * 72, True),
        ("a" * 73, False),
        ("é" * 36, True),  # 2 bytes each = 72
        ("é" * 37, False),
        ("日" * 24, True),  # 3 bytes each = 72
        ("日" * 25, False),
        ("🔐" * 18, True),  # 4 bytes each = 72
        ("🔐" * 19, False),
    ],
)
def test_registration_limit_counts_bytes_not_characters(password, ok):
    if ok:
        assert RegisterRequest(email="a@b.com", password=password).password == password
    else:
        with pytest.raises(ValidationError, match="too long"):
            RegisterRequest(email="a@b.com", password=password)


def test_password_change_rejects_a_new_password_over_72_bytes():
    with pytest.raises(ValidationError, match="72 bytes"):
        ChangePasswordRequest(current_password="OldPassword1!", new_password="n" * 73)
    ok = ChangePasswordRequest(current_password="OldPassword1!", new_password="n" * 72)
    assert ok.new_password == "n" * 72


def test_password_change_still_requires_8_characters():
    with pytest.raises(ValidationError, match="at least 8"):
        ChangePasswordRequest(current_password="OldPassword1!", new_password="short")
