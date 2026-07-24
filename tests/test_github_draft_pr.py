import pytest

from services.github_draft_pr import DraftPrError, _validate_patch_target, apply_unified_patch


def test_apply_unified_patch_replaces_and_adds_lines() -> None:
    original = "first\nold\nlast\n"
    patch = "--- a/example.txt\n+++ b/example.txt\n@@ -1,3 +1,4 @@\n first\n-old\n+new\n+added\n last\n"
    assert apply_unified_patch(original, patch) == "first\nnew\nadded\nlast\n"


def test_apply_unified_patch_rejects_a_stale_context() -> None:
    with pytest.raises(DraftPrError, match="conflicts"):
        apply_unified_patch("changed\n", "@@ -1,1 +1,1 @@\n-old\n+new\n")


def test_apply_unified_patch_rejects_multiple_file_headers() -> None:
    patch = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-old\n+new\n--- a/two.txt\n+++ b/two.txt\n"
    with pytest.raises(DraftPrError, match="exactly one file"):
        apply_unified_patch("old\n", patch)


def test_patch_target_must_match_selected_path() -> None:
    patch = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-old\n+new\n"
    with pytest.raises(DraftPrError, match="does not match"):
        _validate_patch_target(patch, "two.txt")
