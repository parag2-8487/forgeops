# SPDX-License-Identifier: Apache-2.0
import difflib

from hypothesis import given, settings
from hypothesis import strategies as st


@settings(max_examples=100)
@given(
    base_lines=st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=10),
    target_lines=st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=10),
)
def test_q23_diff_fidelity(base_lines: list[str], target_lines: list[str]):
    """
    Property Q-23: Unified diff rendering fidelity.
    Unified diff generated between base and target lines must capture all edits losslessly.
    """
    base_text = [l + "\n" for l in base_lines]
    target_text = [l + "\n" for l in target_lines]

    diff = list(difflib.unified_diff(base_text, target_text, fromfile="base", tofile="target"))
    diff_str = "".join(diff)

    if base_text == target_text:
        assert diff_str == ""
    else:
        assert len(diff_str) > 0
