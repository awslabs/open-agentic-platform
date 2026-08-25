# Connecting the AWS DevOps Agent & AWS Security Agent to the Dark Factory

The Dark Factory pipeline reviews every PR with two managed AWS agents:

- **AWS DevOps Agent** — *Release Readiness* code review: cross-repo dependency risk, standards
  compliance, access-control correctness, and (optionally) build+test in an AWS-managed environment.
  Verdict: **BLOCK / Proceed with Caution / Safe to Release**.
- **AWS Security Agent** — code security review: OWASP Top 10, hardcoded secrets, IAM misuse,
  dependency risk, with inline findings + recommended fixes.

Both integrate with GitHub the same way: **install a GitHub App, connect the repo to an Agent
Space, enable review.** After that, every pull request is reviewed automatically and the agents post
their verdicts back onto the PR. This page is the end-to-end setup — follow it top to bottom.

> **Prerequisites:** an AWS account with the DevOps Agent + Security Agent enabled, admin access to
> that account, and **owner/admin** rights on the GitHub org or user that owns your repo (needed to
> install a GitHub App). The examples use account `<ACCOUNT_ID>`, region `us-west-2`, repo
> `elamaran11/dark-factory-sandbox` — substitute your own.

---

## Part A — AWS DevOps Agent (Release Readiness code review)

### A1. Create / open an Agent Space
1. AWS Console → **AWS DevOps Agent**.
2. Create an Agent Space (or open an existing one), e.g. **`dark-factory`**.

### A2. Register GitHub (account level)
1. In the Agent Space → **Capabilities** tab → **Pipeline** section → **Add** → **GitHub**.
2. Choose **User** or **Organization** (must match where the repo lives), then submit.
3. GitHub opens its **authorize** screen → authorize AWS DevOps Agent.
4. On the **Install & Authorize** page, choose **Only select repositories → your repo** (or All),
   then **Install & Authorize**. You're returned to the console with GitHub registered.
   > Permission level: **Read & Write** (default) — lets the agent post PR comments, check-runs, and
   > optional remediation PRs. Read-Only disables those write actions.

### A3. Connect the repo to the Agent Space + enable review
1. In the Agent Space → **Capabilities** → **Pipeline** → **Add** → **GitHub** → pick the
   registration → select your repo → **Add**.
2. In **Code Review and Automated Testing**, per repo:
   - **Auto trigger change review** = **ON** — reviews every PR automatically.
   - **Automated verification testing** = ON (optional) — builds/tests the change in an AWS-managed
     verification environment (deeper than static analysis).
   - **Runtime role** (optional) — an IAM role the agent assumes for private-registry/artifact
     access during builds.
3. **Save.**

### A4. What you'll see on a PR
On every new/updated PR the DevOps Agent posts a status/check-run
**`aws-devops-agent/release-readiness-review`** (`pending` → `success`/`failure`) with a link to the
full report, plus inline comments for any risks it finds (e.g. an unpinned image, a missing variable
default, an over-broad IAM change). A clean change gets **"change approved"**.

---

## Part B — AWS Security Agent (code security review)

The Security Agent can review a PR **two ways** — you can use either or both:

### Path B1 (recommended) — GitHub App: inline bot findings
1. AWS Console → **AWS Security Agent** → your Agent Space (e.g. **`dark-factory`**).
2. **Integrations → GitHub → Connect** (or open
   `https://github.com/apps/aws-security-agent/installations/new`). Authorize + install the App on
   your repo, **Read & Write** (so it can post inline comments + optional fix PRs).
3. Back in the Security Agent console, **add the GitHub integration to your Agent Space** and
   **enable code review** on the repo. *(Installing the App on GitHub and connecting it to the Agent
   Space are two distinct steps — do both.)*
4. On the next PR the agent posts as **`aws-security-agent[bot]`**: an "AWS Security Agent is
   reviewing…" notice, then inline findings (or **"No issues identified"**), with recommended fixes.

### Path B2 — headless code-review API (no GitHub App)
Fully API-driven — useful for automation that shouldn't depend on a GitHub App. The Dark Factory's
`security-agent` step already implements this (`scripts/security-agent.sh`): stage the diff to S3,
then:
```
aws securityagent create-code-review   --agent-space-id <id> --assets '{"sourceCode":[{"s3Location":"s3://.../src.zip"}]}' --service-role <role>
aws securityagent start-code-review-job --agent-space-id <id> --code-review-id <cr> --diff-source '{"s3Uri":"s3://.../diff.patch"}'
aws securityagent list-findings        --agent-space-id <id> --code-review-job-id <job>
```
Findings come back with `riskType`, `riskLevel` (INFORMATIONAL→CRITICAL), and `confidence`. The IAM
this needs (a service role trusting `securityagent.amazonaws.com` + an IRSA role for the workflow +
an S3 bucket) is committed as Terraform in `gitops/addons/charts/dark-factory/iam/securityagent.tf`,
and the Agent Space is reconciled by the PreSync bootstrap Job
(`templates/06-securityagent-bootstrap.yaml`). **The Dark Factory runs both paths** — the App for
inline bot findings and the headless path for the merge-gate signal.

---

## Part C — How the Dark Factory pipeline uses the agents

Once the agents are connected, the `df-run` workflow wires them into the PR lifecycle (ordering:
**DevOps first, then Security**):

1. Coder opens the PR → posts `dark-factory:coding` + `dark-factory:local-test` comments.
2. **`devops-gate`** waits for the DevOps Agent's `aws-devops-agent/release-readiness-review` verdict.
   On a clear verdict it applies the **`needs-security-review`** label. *(Config:
   `devopsAgent.checkContext` / `devopsAgent.checkRunName` in `values.yaml` — the check name the gate
   watches for.)*
3. **`security-agent`** runs (gated on that label) → the Security Agent reviews the diff; the
   `aws-security-agent[bot]` also comments inline (Path B1).
4. **`deploy-test`** runs for deployable changes (`terraform validate` for `*.tf`, ephemeral-namespace
   apply for k8s).
5. **Sticky status** rewrites the PR body into one board (build+tests, security, devops, deploy-test).
6. A human **approves** the PR → the `df-merge-teardown` workflow **squash-merges** and reaps the
   sandbox. *(GitHub blocks a PR author from approving their own PR, so the approver must be a
   different identity than the one the coder opens PRs as.)*

### Relevant `values.yaml` knobs
```yaml
devopsAgent:
  enabled: true
  gate: check                       # wait for the DevOps Agent check-run (native model)
  checkContext: "(aws-devops-agent/release-readiness-review|...)"   # JS regex (no (?i) flag)
  checkRunName: "aws-devops-agent/release-readiness-review"          # exact check name
securityAgent:
  enabled: true                     # headless S3-diff path (Path B2)
  app:
    enabled: true                   # GitHub App inline bot (Path B1)
    checkContext: ""                # set only if the App posts a check/status to gate on
    checkRunName: ""
```

---

## Verify the setup

```bash
# Open a PR in the connected repo, then within a few minutes:

# DevOps Agent posted its review?
gh api repos/<owner>/<repo>/commits/<PR_HEAD_SHA>/status \
  --jq '.statuses[] | select(.context|test("devops")) | {context,state,description}'

# Security Agent bot commented inline?
gh api repos/<owner>/<repo>/issues/<PR>/comments \
  --jq '.[] | select(.user.login=="aws-security-agent[bot]") | .body[0:80]'

# Security Agent integration recorded on the space (headless path)?
aws securityagent list-integrations --query 'integrationSummaries[].provider'
aws securityagent list-integrated-resources --agent-space-id <id>
```

If the DevOps check never appears, the repo isn't connected to the Agent Space (Part A3). If the
Security bot never comments, the GitHub App isn't connected to the Agent Space (Part B1 step 3) —
installing the App on GitHub alone is not enough.
