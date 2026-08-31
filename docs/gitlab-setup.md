# GitLab Setup for Pinard

## Create the `pinard` user

1. Create a GitLab account named `pinard` (or ask your admin to create a service account)
2. Set a profile picture/avatar so it's recognizable in MR activity

## Role: Developer

Add `pinard` as **Developer** to each project it will work on. This grants:

- Create branches and push commits
- Open merge requests
- Merge MRs (when branch protection allows Developers to merge — GitLab default)
- Post comments on MRs and issues
- Read and trigger pipelines
- Create and close issues

**Maintainer is NOT required** as long as the `main` branch protection has "Allowed to merge: Developers + Maintainers" (this is the GitLab default).

## Personal Access Token

Create a PAT for the `pinard` user:

1. Log in as `pinard` → Settings → Access Tokens
2. Name: `pinard-automation`
3. Expiration: set a reasonable expiry (or maximum allowed)
4. Scopes:
   - `api` — full API access (MR create/merge, issues, comments, pipelines)
   - `read_repository` — clone repos
   - `write_repository` — push branches

Save the token. Set it on the machine running pinard:
```bash
export PINARD_GITLAB_TOKEN="glpat-xxxxx"
```

## SSH Key

Generate a key for the `pinard` user:
```bash
ssh-keygen -t ed25519 -C "pinard" -f ~/.ssh/pinard_id_ed25519 -N ""
```

Add the public key to the `pinard` GitLab account:
1. Log in as `pinard` → Settings → SSH Keys
2. Paste the contents of `~/.ssh/pinard_id_ed25519.pub`

## Credentials file

On the machine running pinard, create `~/.config/pinard/credentials.yaml`:

```yaml
gitlab:
  host: gitlab.example.com
  user: pinard
  token_env: PINARD_GITLAB_TOKEN
  ssh_key: ~/.ssh/pinard_id_ed25519

nats:
  url: wss://nats.example.com
  user: lelongs
  password_env: PINARD_NATS_PASSWORD
```

## Branch protection (verify)

For each project pinard works on, verify the `main` branch protection:

1. Project → Settings → Repository → Protected branches
2. `main` should have:
   - Allowed to merge: **Developers + Maintainers**
   - Allowed to push and merge: **No one** (force push disabled)
   - Allowed to push: **Developers + Maintainers** (or just Maintainers — pinard merges via API, not push)

If "Allowed to merge" is set to "Maintainers" only, pinard (Developer) won't be able to auto-merge. Change it to "Developers + Maintainers".

## MR approvals

If the project requires MR approvals before merge:
- Pinard will NOT approve its own MRs (it opens them and waits)
- A human must approve → pinard's auto-merge kicks in after approval
- The `needs_approval` event notifies the conductor when an MR is ready for human review

## What pinard does with these permissions

| Action | API call | Required |
|--------|----------|----------|
| Push branch | `git push` via SSH | SSH key + Developer |
| Open MR | `POST /merge_requests` | PAT + Developer |
| Merge MR | `PUT /merge_requests/:iid/merge` | PAT + Developer + branch allows |
| Comment on MR | `POST /merge_requests/:iid/notes` | PAT + Developer |
| Create issue | `POST /issues` | PAT + Developer |
| Read pipeline status | `GET /pipelines` | PAT + Developer |
| Read MR notes | `GET /merge_requests/:iid/notes` | PAT + Developer |
