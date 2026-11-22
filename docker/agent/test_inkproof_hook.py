"""Unit tests for inkproof-hook's guard on the human's sessions
(scripts/test-hook.sh runs them)."""
import importlib.machinery
import importlib.util
import os
import unittest

_loader = importlib.machinery.SourceFileLoader("inkproof_hook", os.path.join(os.path.dirname(__file__), "bin", "inkproof-hook"))
_spec = importlib.util.spec_from_loader("inkproof_hook", _loader)
hook = importlib.util.module_from_spec(_spec)
_loader.exec_module(hook)

REPO = "/workspace/inkproof-demos"
SCRATCH = "/tmp/demos-95"


def bash(command, cwd=REPO):
    return hook.guard({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})


class Reading(unittest.TestCase):
    def test_reading_the_checkout_is_allowed(self):
        for c in ["git status", "git log --oneline -5", "git diff origin/main -- app", "git fetch origin",
                  "git branch", "git branch --list 'agent/*'", "git stash list", "git worktree list",
                  "git tag -l", "cat Inkproof/Demos/Frame.lean", "grep -rn scissor app | head",
                  "lake env printenv LEAN_PATH", "ls -la .lake > /tmp/out.txt 2>&1",
                  "git clone --shared /workspace/inkproof-demos /tmp/demos-95",
                  "cp app/browser.test.mjs /tmp/x.mjs", "timeout 300 git log", "echo hi >/dev/null"]:
            self.assertIsNone(bash(c), c)

    def test_anything_in_a_scratch_clone_is_allowed(self):
        for c in ["git checkout -b try", "git commit -am wip", "lake build", "npm ci",
                  "scripts/build-site.sh", "sed -i s/a/b/ x.lean", "echo x > notes.txt", "rm -rf _site"]:
            self.assertIsNone(bash(c, SCRATCH), c)

    def test_cd_into_a_scratch_clone_first(self):
        self.assertIsNone(bash("cd /tmp/demos-95 && lake build && git stash"))

    def test_editing_outside_the_checkouts(self):
        self.assertIsNone(hook.guard({"tool_name": "Write", "tool_input": {"file_path": "/tmp/demos-95/a.lean"},
                                      "cwd": REPO}))


class Writing(unittest.TestCase):
    def test_git_writes_in_the_checkout_are_refused(self):
        for c in ["git checkout -b agent/demos/x origin/main", "git switch main", "git stash",
                  "git commit -m x", "git pull", "git reset --hard", "git add .", "git branch new",
                  "git branch -D old", "git worktree add ../x", "git tag v1",
                  "git -C /workspace/inkproof-demos checkout main"]:
            self.assertIsNotNone(bash(c), c)
        self.assertIsNotNone(bash("git -C /workspace/inkproof-web restore x", SCRATCH))
        self.assertIsNotNone(bash("cd /workspace/inkproof-demos && git checkout main", SCRATCH))

    def test_push_is_refused_anywhere(self):
        self.assertIsNotNone(bash("git push -u origin try", SCRATCH))

    def test_builds_in_the_checkout_are_refused(self):
        for c in ["lake build", "lake update", "npm ci", "npm run build", "npx vite build",
                  "scripts/build-site.sh", "bash scripts/build-site.sh", "env CI=1 lake build",
                  "timeout 600 lake build Inkproof.Demos"]:
            self.assertIsNotNone(bash(c), c)
        r = hook.guard({"tool_name": "mcp__lean-lsp__lean_build", "tool_input": {}, "cwd": REPO})
        self.assertIsNotNone(r)

    def test_file_writes_in_the_checkout_are_refused(self):
        for c in ["echo x > notes.txt", "cat a >> app/web/index.html", "rm -rf _site", "touch x",
                  "sed -i s/a/b/ Inkproof/Demos/Frame.lean", "perl -pi -e s/a/b/ x", "mv a b",
                  "cp /tmp/x.lean Inkproof/Demos/x.lean", "printf x | tee app/x.txt",
                  "bash -c 'git checkout main'", "echo $(git stash)"]:
            self.assertIsNotNone(bash(c), c)
        self.assertIsNotNone(bash("cp /tmp/a /workspace/inkproof-demos/a", SCRATCH))
        self.assertIsNotNone(bash("echo x > /workspace/inkproof-demos/a", SCRATCH))

    def test_edit_tools_in_the_checkout_are_refused(self):
        for tool in ("Edit", "Write", "MultiEdit"):
            r = hook.guard({"tool_name": tool, "tool_input": {"file_path": f"{REPO}/Inkproof/Demos/Frame.lean"},
                            "cwd": SCRATCH})
            self.assertIsNotNone(r, tool)

    def test_a_heredoc_body_is_data(self):
        self.assertIsNone(bash("cat > /tmp/body.md <<'EOF'\ngit checkout main > x\nEOF\n"))


class Forgejo(unittest.TestCase):
    def test_issues_and_comments_are_allowed(self):
        for c in ["tea issues create --repo tentenths/inkproof-demos --title 'Migrate' --description 'x'",
                  "tea issue create --title t", "tea comment 95 'Still visible in Safari'",
                  "tea issues ls --repo tentenths/inkproof-web --state open", "tea issue 95 --comments",
                  "tea pr --repo tentenths/inkproof-demos 79 --comments", "tea pr ls", "tea labels ls",
                  "tea issue --repo tentenths/inkproof-web 95", "tea whoami"]:
            self.assertIsNone(bash(c, SCRATCH), c)

    def test_other_writes_are_refused(self):
        for c in ["tea issues close 95", "tea issue reopen 95", "tea issues edit 95 --add-labels verified",
                  "tea pr create --title x", "tea pr checkout 79", "tea pr close 79", "tea labels create x",
                  "tea milestones create x", "tea issues --repo tentenths/inkproof-web close 95"]:
            self.assertIsNotNone(bash(c, SCRATCH), c)

    def test_forge_only_reads(self):
        for c in ["forge ci inkproof-demos 79 --log", "forge reviews inkproof-demos 79", "forge ledger 2", "forge"]:
            self.assertIsNone(bash(c, SCRATCH), c)
        for c in ["forge pr automerge 5", "forge pr merge 5", "forge ci inkproof-demos main --rerun",
                  "forge review inkproof-demos 5 f.json", "forge release inkproof-web note 95"]:
            self.assertIsNotNone(bash(c, SCRATCH), c)


class Sessions(unittest.TestCase):
    def test_runs_are_not_guarded(self):
        os.environ["INKPROOF_RUN"] = "1"
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"
        try:
            self.assertFalse(hook.human())
        finally:
            del os.environ["INKPROOF_RUN"], os.environ["CLAUDE_CODE_ENTRYPOINT"]

    def test_the_humans_sessions_are(self):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
        try:
            self.assertTrue(hook.human())
        finally:
            del os.environ["CLAUDE_CODE_ENTRYPOINT"]


if __name__ == "__main__":
    unittest.main()
