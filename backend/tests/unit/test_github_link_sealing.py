# SPDX-License-Identifier: FSL-1.1-ALv2
"""The GitHub link's cryptography and parsing, without a database or a network.

WHAT THESE PIN, and why each one is here rather than assumed:

* the key is DOMAIN-SEPARATED from the envelope-key KEK. One secret serves three purposes, and the
  whole safety of that rests on three different labels producing three different keys. Asserted
  against the real `ENVELOPE_KEY_LABEL` rather than against a copy of it, so renaming either label
  into the other's value fails here.
* the seal is BOUND TO THE USER. A ciphertext moved between users must not open, which is the second
  of the two independent defences behind cross-user isolation — the first being the WHERE clause.
* an empty pepper is REFUSED rather than silently deriving a well-known key from the empty string.
* `expires_in` becomes an absolute moment, and a MISSING one becomes `None` rather than a guess. A
  guessed expiry either reports a live token as dead or a dead one as live.
* a repository with no language yields an EMPTY STRING, so the UI is handed absence rather than a
  word placed where a measurement goes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from src.auth.devices import ENVELOPE_KEY_LABEL, derive_key_encryption_key
from src.integrations.github_link import (
    LINK_KEY_LABEL,
    SEAL_NONCE_BYTES,
    GitHubLinkError,
    UserToken,
    _expiry,
    _repository,
    derive_link_key,
    seal_token,
    state_key,
    unseal_token,
)

pytestmark = pytest.mark.mandatory

PEPPER = "unit-test-pepper-not-a-real-value-0123456789"
USER = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
OTHER_USER = uuid.UUID("11111111-2222-3333-4444-555555555555")
TOKEN = "a-github-user-to-server-value-for-this-unit-test"


class TestTheKey:
    def test_it_is_thirty_two_bytes(self) -> None:
        assert len(derive_link_key(PEPPER)) == 32

    def test_it_is_deterministic_for_one_pepper(self) -> None:
        assert derive_link_key(PEPPER) == derive_link_key(PEPPER)

    def test_it_differs_from_the_envelope_key_encryption_key(self) -> None:
        """Domain separation, asserted between the two real derivations rather than between labels."""
        assert derive_link_key(PEPPER) != derive_key_encryption_key(PEPPER)
        assert LINK_KEY_LABEL != ENVELOPE_KEY_LABEL

    def test_it_changes_with_the_pepper(self) -> None:
        assert derive_link_key(PEPPER) != derive_link_key(PEPPER + "x")

    def test_an_empty_pepper_is_refused(self) -> None:
        with pytest.raises(GitHubLinkError, match="ENVELOPE_PEPPER is empty"):
            derive_link_key("")


class TestTheSeal:
    def test_it_round_trips(self) -> None:
        key = derive_link_key(PEPPER)
        sealed = seal_token(TOKEN, user_id=USER, key=key)

        assert unseal_token(sealed, user_id=USER, key=key) == TOKEN

    def test_the_ciphertext_does_not_contain_the_plaintext(self) -> None:
        sealed = seal_token(TOKEN, user_id=USER, key=derive_link_key(PEPPER))

        assert TOKEN.encode("utf-8") not in sealed
        assert len(sealed) > SEAL_NONCE_BYTES

    def test_two_seals_of_one_value_differ(self) -> None:
        """A fresh nonce per seal, so equal tokens are not recognisable as equal in the database."""
        key = derive_link_key(PEPPER)

        assert seal_token(TOKEN, user_id=USER, key=key) != seal_token(TOKEN, user_id=USER, key=key)

    def test_another_users_id_does_not_open_it(self) -> None:
        key = derive_link_key(PEPPER)
        sealed = seal_token(TOKEN, user_id=USER, key=key)

        with pytest.raises(GitHubLinkError, match="did not authenticate"):
            unseal_token(sealed, user_id=OTHER_USER, key=key)

    def test_another_pepper_does_not_open_it(self) -> None:
        sealed = seal_token(TOKEN, user_id=USER, key=derive_link_key(PEPPER))

        with pytest.raises(GitHubLinkError, match="did not authenticate"):
            unseal_token(sealed, user_id=USER, key=derive_link_key(PEPPER + "x"))

    def test_a_truncated_column_is_refused_without_revealing_which_failure_it_was(self) -> None:
        key = derive_link_key(PEPPER)
        sealed = seal_token(TOKEN, user_id=USER, key=key)

        with pytest.raises(GitHubLinkError, match="missing or truncated"):
            unseal_token(sealed[:8], user_id=USER, key=key)

    def test_a_flipped_bit_is_refused(self) -> None:
        key = derive_link_key(PEPPER)
        sealed = bytearray(seal_token(TOKEN, user_id=USER, key=key))
        sealed[-1] ^= 0x01

        with pytest.raises(GitHubLinkError, match="did not authenticate"):
            unseal_token(bytes(sealed), user_id=USER, key=key)

    def test_an_empty_token_is_refused(self) -> None:
        with pytest.raises(GitHubLinkError, match="empty token"):
            seal_token("", user_id=USER, key=derive_link_key(PEPPER))

    def test_a_wrong_length_key_is_refused_rather_than_padded(self) -> None:
        with pytest.raises(GitHubLinkError, match="must be 32 bytes"):
            seal_token(TOKEN, user_id=USER, key=b"short")


class TestExpiry:
    def test_seconds_become_an_absolute_moment(self) -> None:
        now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

        assert _expiry(now, 3600) == now + timedelta(seconds=3600)

    def test_a_missing_value_is_none_rather_than_a_guess(self) -> None:
        now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

        assert _expiry(now, None) is None
        assert _expiry(now, "not-a-number") is None
        assert _expiry(now, 0) is None

    def test_a_token_with_no_stated_expiry_is_usable(self) -> None:
        """A GitHub App without expiring tokens issues none; treating that as expired would break it."""
        token = UserToken(access_token=TOKEN, expires_at=None, refresh_token=None, refresh_expires_at=None, scopes="")

        assert token.usable_at(datetime.now(UTC)) is True

    def test_a_token_inside_the_refresh_margin_is_not_usable(self) -> None:
        now = datetime.now(UTC)
        token = UserToken(
            access_token=TOKEN,
            expires_at=now + timedelta(minutes=1),
            refresh_token="r",
            refresh_expires_at=None,
            scopes="",
        )

        assert token.usable_at(now) is False

    def test_the_repr_withholds_the_token(self) -> None:
        """`repr` reaches logs, tracebacks and assertion output, all of which are read by people."""
        token = UserToken(access_token=TOKEN, expires_at=None, refresh_token=TOKEN, refresh_expires_at=None, scopes="")

        rendered = repr(token)
        assert TOKEN not in rendered
        assert "withheld" in rendered


class TestRepositoryParsing:
    def test_a_repository_with_no_language_yields_an_empty_string(self) -> None:
        parsed = _repository({"full_name": "o/n", "name": "n", "owner": {"login": "o"}, "language": None})

        assert parsed.language == ""

    def test_the_owner_falls_back_to_the_full_name(self) -> None:
        """GitHub always sends an owner object; a missing one must not produce an empty owner."""
        parsed = _repository({"full_name": "octo-org/deploy-me", "name": "deploy-me"})

        assert parsed.owner == "octo-org"

    def test_the_default_branch_is_not_assumed_to_be_main(self) -> None:
        parsed = _repository({"full_name": "o/n", "name": "n", "default_branch": "trunk"})

        assert parsed.default_branch == "trunk"

    def test_flags_and_sizes_are_carried(self) -> None:
        parsed = _repository({"full_name": "o/n", "name": "n", "private": True, "archived": True, "size": 42})

        assert (parsed.private, parsed.archived, parsed.size_kb) == (True, True, 42)


class TestTheStateKey:
    def test_the_state_does_not_appear_in_its_own_redis_key(self) -> None:
        """A Redis dump must not carry replayable states, so the key is a hash of the state."""
        state = "a-state-value-for-this-unit-test"

        key = state_key(state)

        assert state not in key
        assert key.startswith("githublink:state:")

    def test_two_states_give_two_keys(self) -> None:
        assert state_key("one") != state_key("two")
