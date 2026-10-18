"""Password hashing moved from passlib to the bcrypt library: old hashes must keep working."""

from __future__ import annotations

import sys
from pathlib import Path

import bcrypt
import pytest

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402

# Hashes created by the previous implementation (passlib 1.7.4 with bcrypt 3.2.2), test passwords only.
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
    "long_100_bytes": (
        "LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL",
        "$2b$12$8ZSoMWBGveKMQX63KhZgqeRrXIc14T/Cm5VUGu8IZJHFpbMnSDxjW",
    ),
}


@pytest.mark.parametrize("name", list(PASSLIB_HASHES))
def test_hashes_made_by_the_previous_implementation_still_verify(name):
    password, hashed = PASSLIB_HASHES[name]
    assert auth.verify_password(password, hashed)
    assert not auth.verify_password(password + "x" if len(password) < 72 else "wrong", hashed)


def test_new_hashes_use_the_same_format_and_cost():
    hashed = auth.hash_password("CorrectPassword1!")
    assert hashed.startswith("$2b$12$")
    assert auth.verify_password("CorrectPassword1!", hashed)
    assert not auth.verify_password("correctpassword1!", hashed)


def test_hashes_are_salted():
    assert auth.hash_password("same-password") != auth.hash_password("same-password")


def test_passwords_over_72_bytes_work_and_only_the_first_72_count_as_before():
    long_password = (
        "L" * 100
    )  # accepted by the API (up to 128 characters); bcrypt 5 alone would raise
    hashed = auth.hash_password(long_password)
    assert auth.verify_password(long_password, hashed)
    assert auth.verify_password("L" * 72 + "different tail", hashed)  # unchanged behaviour
    assert not auth.verify_password("L" * 71, hashed)


def test_a_malformed_hash_is_a_failed_login_not_an_error():
    assert auth.verify_password("anything", "not-a-bcrypt-hash") is False
    assert auth.verify_password("anything", "") is False


def test_the_library_itself_rejects_long_input_which_is_why_we_truncate():
    with pytest.raises(ValueError):
        bcrypt.hashpw(b"L" * 100, bcrypt.gensalt(rounds=4))
