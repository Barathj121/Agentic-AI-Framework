# Deploy only pushes to main

`.github/workflows/deploy-main.yml` automatically runs on `push` to `main` in
`Barathj121/Agentic-AI-Framework`. Feature-branch pushes, PR events and tag pushes
do not automatically deploy. Merging a PR into `main` counts as a main push.
You can continue committing and pushing feature branches normally.

Manual deployment remains available in two ways:

- **From Codex/the operator terminal:** after explicitly choosing a source, run
  `bash deploy/gcp/manual.sh feature/your-branch` or pass a full commit SHA. It
  resolves and packages that committed revision without committing, pushing or
  merging. Uncommitted working-tree changes are excluded. The source must include
  deployment scripts compatible with this workflow.
- **From GitHub Actions:** run **Deploy main to GCP**, select `main` as the workflow
  branch, and set `source_ref` to the branch, tag or commit to deploy. The trusted
  workflow lives on main; the tested application source can be the explicitly
  selected revision. The GCP identity still rejects workflow controllers running
  from feature branches or forks.

The workflow becomes active only after it is committed and merged into `main`.
Nothing is committed, pushed or deployed by adding these files locally.

## Pipeline

1. Check out the pushed commit (or explicitly requested manual source) and run the existing application tests, including
   real Docker checks. Build the existing sandbox and trusted external-app test image.
   Live Ollama tests are excluded; CI does not call Gemini or download Gemma models.
2. For automatic pushes, reject a stale commit if `main` has advanced. A manual
   deployment intentionally accepts the explicitly selected commit. Package bytes from the exact Git
   commit, excluding local changes, runtime data, `.env`, keys and generated OIDC files.
3. Authenticate with GitHub OIDC and Google Workload Identity Federation. The
   provider accepts only this numeric repository/owner identity, this workflow,
   `refs/heads/main` and either `push` or `workflow_dispatch`. Manual source selection
   does not change the main-only workflow-controller restriction. No service-account JSON key or Gemini key
   belongs in GitHub Actions.
4. Through IAP SSH, prepare a versioned release/venv and rebuild the **same** Docker
   sandbox. Verify the archive checksum. No VM creation, start, resize, billing
   change, model download or cloud API deployment occurs.
5. Briefly stop both existing Streamlit services, switch the stable application
   path to the release, and restart. Failed local startup/health probes restore
   the previous application and sandbox image. Verify the existing public Cloud
   Run health URL afterward. A gateway-only failure after successful VM activation
   fails the job for investigation; it does not roll back a healthy application.

The application flow, prompts, tools, approvals, data formats and sandbox
restrictions are unchanged. Existing systemd environment files, API credentials,
workspace/model directories, external app containers, Nginx, Cloud Run gateway and
URL remain in place. The private ClariFi API and gateway images are infrastructure
outside this VM application update; changing those images needs a separate reviewed
infrastructure change. Do not run the fresh `deploy.sh`/`bootstrap.sh` installer as
an update step.

Deployment restarts disconnect active browser sessions and interrupt active jobs.
Merge during a quiet period. Workspaces remain on the persistent disk. Schema/data
migrations need a separate migration plan; code rollback does not undo user data.
Old releases and images are retained for rollback. The updater checks for at least
8 GiB free disk space; review disk usage and remove obsolete deployment artifacts
as an operator. Financial evidence and downloaded models are never deleted by CI.

## One-time identity setup

An operator with IAM administration runs:

```bash
bash deploy/gcp/setup-main-identity.sh
```

This only configures authentication/permissions; it does not deploy the app. The
CI service account receives Compute Viewer, OS Admin Login on the one VM, IAP SSH
restricted to the VM IP/port 22, and Service Account User on the VM identity.
**OS Admin Login gives root access to that VM.** Protect `main` with PR review and
restrict repository write access accordingly. The deployment identity gets no
billing administration or Cloud Run provisioning role. Root access on the VM can
access runtime credentials; treat maintainers able to merge deployment code as
trusted operators.

The checked-in workflow defaults target the existing trial installation:

| Repository variable (optional override) | Existing target |
| --- | --- |
| `GCP_PROJECT_ID` | `project-834ebdfa-aa5e-417f-b09` |
| `GCP_ZONE` | `us-central1-a` |
| `GCP_VM_NAME` | `clarifi-demo-vm` |
| `GCP_PUBLIC_URL` | `https://clarifi-demo-web-n65xgyqlaa-uc.a.run.app` |
| `GCP_WIF_PROVIDER` | `projects/342614326962/locations/global/workloadIdentityPools/clarifi-github-main/providers/github` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `clarifi-main-deploy@project-834ebdfa-aa5e-417f-b09.iam.gserviceaccount.com` |

These are resource identifiers, not secrets. Change all corresponding values and
run identity setup in the intended project before moving deployments elsewhere.
The workflow is guarded against running in a different repository.

## Sandboxing

Keep it. Generated migration code and prepared ClariFi tools continue executing in
restricted Docker containers on the VM: no network, unprivileged user, dropped
capabilities, read-only root filesystem and bounded CPU/memory/processes. API keys
and the Docker socket are not mounted into execution containers. Cloud Run remains
the public HTTP/WebSocket entry point. Direct ready-to-use investigations retain
their existing reviewed in-process tools; this deployment change does not move them
into Docker or otherwise change their workflow.

## Validation boundary

Local tests can verify branch/event gates, commit-only packaging and mocked gcloud
orchestration. This change must not be described as a successfully deployed CI
pipeline until an authorized main push or manual dispatch has passed GitHub Actions and the
public application check. That first run is intentionally not triggered here.

The verification job uses standard GitHub-hosted Linux runners. For this private
repository, it consumes the owner's included Actions allowance; keep paid Actions
usage disabled to maintain the no-out-of-pocket requirement. VM runtime and retained
release/image storage continue to consume the existing GCP trial credits. CI does
not upgrade either account or enforce their billing controls.

Authentication setup was completed for the existing project on 18 October 2026.
The saved provider was verified `ACTIVE` with repository/owner numeric IDs,
`refs/heads/main`, automatic push/manual dispatch events and this exact workflow path. No application
update was executed as part of setup. First-run GitHub-to-VM deployment validation
remains pending until an authorized main push or manual deployment.

Reference: [Google Workload Identity Federation for deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines).
