# Minikube deployment

Prerequisites: Docker Desktop (Linux containers) or another working Minikube
driver, plus `minikube` and `kubectl` on PATH.

For the first deployment, copy the credential template from the repository root:

```powershell
Copy-Item deployment/secrets.env.example deployment/secrets.env
```

Edit `deployment/secrets.env` locally. Use one `KEY=value` per line without quotes;
fill in the Discord webhook and, for a private league, `ESPN_S2` and `SWID`.
Leave the cookie values empty for a public league. This file is ignored by Git
and excluded from Docker builds. Never put credentials in `deployment/k8.yaml`;
that tracked manifest contains only references to a Kubernetes Secret.

Then run PowerShell:

```powershell
.\deployment\deploy-minikube.ps1 -LeagueId 12345678
```

Replace `12345678` with your ESPN league ID. The script imports the local env file
into the `fantasy-football-private-env` Kubernetes Secret without printing its
contents. It uses the existing Secret if the local file is absent; a new cluster
requires the local file. Review your settings before running. Starting the bot
enables its scheduled chat messages. The script itself does not send a test message.

The script starts the `minikube` profile, builds the root Dockerfile directly on
all Minikube nodes with a unique image tag, substitutes the image and league ID
in memory, applies the Deployment, and waits up to 180 seconds for rollout.
It preserves your current kubectl context and explicitly targets the selected
profile. A Recreate strategy prevents old and new bot pods from overlapping.
The original `k8.yaml` is not modified. The root `.dockerignore` keeps deployment
credentials and local environment files out of the image. Updating `secrets.env`
and rerunning the script updates the Secret and recreates the bot pod. Keep a
private backup of credentials: GitHub contains only `secrets.env.example`.

To use a different profile or an existing namespace:

```powershell
.\deployment\deploy-minikube.ps1 -LeagueId 12345678 -Profile football -Namespace default
```

No Service or ingress is needed: this bot makes outbound requests.

The manifest uses the host's LM Studio / Bionic server at
`http://host.docker.internal:1234/v1` and the downloaded model
`google/gemma-4-12b-qat`. It no longer uses an OpenAI key or Kubernetes AI Secret.
Keep LM Studio running. To start its server and load the model after an app/host restart:

```powershell
lms server start --port 1234
lms load google/gemma-4-12b-qat --context-length 32768 --yes
```

Check `lms ps` first; avoid loading another copy if the model is already loaded.
The bot disables reasoning for its own requests. Its generation lock prevents
scheduled jobs from simultaneously querying the model; busy jobs send their normal
report. If the local server is unavailable, normal ESPN reports continue.

The deployment uses unbuffered Python output so the scheduler's `Ready!` message
appears promptly in container logs. Run the deployment script after code changes
to rebuild the image; restarting alone does not build updated code.

```powershell
kubectl --context minikube logs deployment/fantasy-football-bot --follow
kubectl --context minikube get pods -l app=fantasy-football-bot
# Stop the bot's scheduled messages:
kubectl --context minikube scale deployment/fantasy-football-bot --replicas=0
```

Add `--namespace YOUR_NAMESPACE` to these commands for a non-default namespace.
Rerunning the deployment script restores one replica. A successful rollout means
the container started; inspect logs to confirm ESPN authentication and settings.

Minikube reference: https://minikube.sigs.k8s.io/docs/commands/image/
