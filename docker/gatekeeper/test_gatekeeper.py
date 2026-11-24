"""Unit tests for the gatekeeper's webhooks: where each says to look, that
only Forgejo's signature is accepted, and that one look is queued however
many webhooks name the same place. Run by scripts/test-dispatcher.sh;
scripts/test-stage6.sh tests the approvals themselves against Forgejo."""
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest

os.environ.setdefault("FORGE_ORG", "org")
SECRET = tempfile.NamedTemporaryFile("w", delete=False)
SECRET.write("s3cret\n")
SECRET.close()
os.environ["WEBHOOK_SECRET_FILE"] = SECRET.name

import gatekeeper as g  # noqa: E402

REPO = {"name": "inkproof-core", "owner": {"login": "org"}}


def run_payload(pr=None):
    return {"run": {"repository": REPO,
                    "event_payload": json.dumps({"pull_request": {"number": pr}} if pr else {})}}


class Where(unittest.TestCase):
    def test_a_finished_run_names_its_pull_request(self):
        self.assertEqual(g.where("action_run_success", run_payload(7)), [("inkproof-core", 7)])

    def test_a_run_on_main_names_the_repository(self):
        self.assertEqual(g.where("action_run_success", run_payload()), [("inkproof-core", None)])

    def test_a_pull_request_opened_or_pushed_to(self):
        for event, action in (("pull_request", "opened"), ("pull_request", "reopened"),
                              ("pull_request_sync", "synchronized")):
            self.assertEqual(g.where(event, {"repository": REPO, "action": action, "pull_request": {"number": 3}}),
                             [("inkproof-core", 3)], event)

    def test_not_its_labels_comments_or_reviews(self):
        for event, action in (("pull_request_label", "label_updated"), ("pull_request_review_request",
                              "review_requested"), ("pull_request", "closed"), ("pull_request_comment", "created")):
            self.assertEqual(g.where(event, {"repository": REPO, "action": action, "pull_request": {"number": 3}}),
                             [], event)

    def test_only_a_push_to_main(self):
        self.assertEqual(g.where("push", {"repository": REPO, "ref": "refs/heads/main"}), [("inkproof-core", None)])
        self.assertEqual(g.where("push", {"repository": REPO, "ref": "refs/heads/agent/x"}), [])

    def test_another_organisation_or_event_names_nothing(self):
        self.assertEqual(g.where("pull_request", {"repository": {"name": "x", "owner": {"login": "other"}},
                                                  "pull_request": {"number": 1}}), [])
        self.assertEqual(g.where("issues", {"repository": REPO, "issue": {"number": 1}}), [])


class Hook(unittest.TestCase):
    def setUp(self):
        g.WORK = g.queue.Queue()
        g._waiting.clear()

    def post(self, body, signature, event="pull_request"):
        h = g.Handler.__new__(g.Handler)
        h.headers = {"Content-Length": str(len(body)), "X-Forgejo-Signature": signature, "X-Forgejo-Event": "pull_request",
                     "X-Forgejo-Event-Type": event}
        h.rfile = io.BytesIO(body)
        replies = []
        h.reply = lambda code, obj: replies.append(code)
        h.hook()
        return replies[0]

    def queued(self):
        out = []
        while not g.WORK.empty():
            out.append(g.WORK.get())
        return out

    def test_a_signed_webhook_is_queued_once(self):
        body = json.dumps({"repository": REPO, "action": "opened", "pull_request": {"number": 3}}).encode()
        sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertEqual(self.post(body, sig), 200)
        self.assertEqual(self.post(body, sig), 200)
        self.assertEqual(self.queued(), [("inkproof-core", 3)])

    def test_a_bad_signature_is_refused(self):
        body = json.dumps({"repository": REPO, "action": "opened", "pull_request": {"number": 3}}).encode()
        self.assertEqual(self.post(body, "0" * 64), 403)
        self.assertEqual(self.queued(), [])


if __name__ == "__main__":
    unittest.main()
