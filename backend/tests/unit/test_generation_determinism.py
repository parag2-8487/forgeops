# SPDX-License-Identifier: FSL-1.1-ALv2
"""The generation port must not sample.

A user submitted an identical prompt twice and got a different NUMBER OF FILES and different contents.
The port's own comment already promised the opposite — "the same request twice should produce the same
answer" — while the temperature was `0.1`, which is still sampling. The comment and the value
disagreed, and the value won.

These pin the contract so the two cannot drift apart again.
"""

from __future__ import annotations

import inspect

from src.ai.generation_port import RoutedArtifactModel


def test_the_default_temperature_is_exactly_zero() -> None:
    """Greedy decoding, so an identical prompt takes the same path through the model.

    Asserted on the SIGNATURE DEFAULT rather than on a constructed instance, because that default is
    what every composition site inherits — `create_app` does not pass a temperature, so the default is
    the value that ships.
    """
    default = inspect.signature(RoutedArtifactModel).parameters["temperature"].default
    assert default == 0.0, (
        f"the generation port defaults to temperature {default}; anything above zero samples, and a "
        "user who submits the same prompt twice then gets different files"
    )


def test_nothing_is_lost_by_removing_sampling() -> None:
    """The reasoning is recorded in the module, not only in a commit message.

    A future reader raising the temperature for 'more creative output' needs to find the argument
    against it at the site, which is that a deterministic gate judges the result: variety cannot make
    an artifact pass, only make a passing one different.
    """
    source = inspect.getsource(RoutedArtifactModel)
    assert "DETERMINISTIC gate" in source
    # And the honest limit of the claim, so nobody reads this as bit-exactness.
    assert "DOES NOT MAKE GENERATION BIT-EXACT" in source


def test_the_max_tokens_bound_is_still_declared() -> None:
    """Guards the edit above: a careless change to the signature must not drop the other bound."""
    default = inspect.signature(RoutedArtifactModel).parameters["max_tokens"].default
    assert default == 4096
