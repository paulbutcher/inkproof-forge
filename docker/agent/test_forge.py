"""Unit tests for forge's version (scripts/test-hook.sh runs them): the
version agents head CHANGELOG.md with must be the one the gatekeeper
releases, or the release is refused (demos#105)."""
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HERE = os.path.dirname(os.path.abspath(__file__))
forge = load("forge", os.path.join(HERE, "bin", "forge"))
os.environ.setdefault("FORGE_ORG", "org")
GATEKEEPER = os.environ.get("GATEKEEPER_PY", os.path.join(HERE, "..", "gatekeeper", "gatekeeper.py"))
gatekeeper = load("gatekeeper", GATEKEEPER)

A, B, C = "def a : Nat\n", "def b : Nat\n", "def c : Nat\n"


class SameRuleAsTheGatekeeper(unittest.TestCase):
    def test_every_case_agrees(self):
        for prev in (None, (0, 20, 0), (0, 3, 7), (1, 0, 0), (2, 4, 1)):
            for old, new in ((A + B, A + B), (A + B, A + B + C), (A + B, A), (A + B, A + C), (A, " " + A),
                             (A + B, B + A + "\n")):
                self.assertEqual(forge.next_version(prev, old, new), gatekeeper.next_version(prev, old, new),
                                 (prev, old, new))

    def test_a_site_with_an_unchanged_api_lock_is_a_patch(self):
        self.assertEqual(forge.next_version((0, 20, 0), A, A)[0], (0, 20, 1))


class InACheckout(unittest.TestCase):
    """forge version over a checkout, with git stubbed: its tags, API.lock at
    each, and the working tree's API.lock."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tags = {}
        real = forge.git
        self.addCleanup(setattr, forge, "git", real)

        def git(*args):
            if args[0] == "rev-parse":
                return 0, self.dir + "\n"
            if args[0] == "fetch":
                return 1, ""  # offline: the tags it has
            if args[0] == "tag":
                return 0, "\n".join(self.tags) + "\n"
            if args[0] == "show":
                tag, _, path = args[1].partition(":")
                return 0, self.tags[tag]
            raise AssertionError(args)
        forge.git = git

    def working_tree(self, lock):
        with open(os.path.join(self.dir, "API.lock"), "w") as f:
            f.write(lock)

    def version(self):
        out = io.StringIO()
        with redirect_stdout(out):
            forge.version()
        return out.getvalue().split(":")[0]

    def test_from_the_highest_tag_and_the_working_tree(self):
        self.tags = {"v0.9.0": A, "v0.20.0": A, "v0.19.0": A, "not-a-version": B, "v1.0": B}
        self.working_tree(A)
        self.assertEqual(self.version(), "v0.20.1")
        self.working_tree(A + B)  # the pull request being written
        self.assertEqual(self.version(), "v0.21.0")

    def test_the_first_release(self):
        self.working_tree(A)
        self.assertEqual(self.version(), "v0.1.0")

    def test_no_api_lock(self):
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                forge.version()


if __name__ == "__main__":
    unittest.main()
