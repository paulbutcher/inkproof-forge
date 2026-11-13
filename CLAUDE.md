# CLAUDE.md

## The human's Forgejo, from the dev container

If `.devcontainer-state/secrets/forgejo/token` exists, it holds a read-only token for the Forgejo the human runs on their Mac, which the dev container reaches at `http://host.docker.internal:3000` (the dev container's own kit uses port 3300). Use it to read issues, pull requests, reviews and comments there rather than asking the human to paste them:

```sh
curl -s -H "Authorization: token $(tr -d '[:space:]' < .devcontainer-state/secrets/forgejo/token)" \
  http://host.docker.internal:3000/api/v1/repos/<org>/<repo>/issues
```

- Never print the token, and never write to that Forgejo: the agents and the human own it.
- The run ledger isn't in Forgejo: it's in the dispatcher's database on the Mac. For it, ask the human to run a command and redirect its output to a file in the checkout.
