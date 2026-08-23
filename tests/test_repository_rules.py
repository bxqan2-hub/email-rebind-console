from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REMOTE = "https://github.com/bxqan2-hub/email-rebind-console.git"
EXPECTED_BRANCH = "codex/email-rebind-console"
MAIN_REMOTE = "https://github.com/bxqan2-hub/-.git"


def test_repository_rule_targets_only_the_rebind_repository():
    rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    post_commit = (ROOT / ".githooks" / "post-commit").read_text(encoding="utf-8")
    pre_push = (ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")

    assert EXPECTED_REMOTE in rules
    assert EXPECTED_REMOTE in post_commit
    assert EXPECTED_REMOTE in pre_push
    assert EXPECTED_BRANCH in rules
    assert EXPECTED_BRANCH in post_commit
    assert EXPECTED_BRANCH in pre_push
    assert MAIN_REMOTE in rules
    assert MAIN_REMOTE not in post_commit
    assert MAIN_REMOTE not in pre_push


def test_setup_script_enables_the_tracked_hook_directory():
    setup = (ROOT / "setup-git-hooks.bat").read_text(encoding="utf-8")
    assert "git config --local core.hooksPath .githooks" in setup
    assert EXPECTED_REMOTE in setup
    assert EXPECTED_BRANCH in setup
