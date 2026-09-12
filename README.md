# loresentry-graph-rag

GraphRAG service for Lore Sentry.

Owns the graph view of a project's creative material and the retrieval that the AI
chat depends on: shaping and querying graph data, RAG/GraphRAG retrieval, Amazon
Neptune access, and consuming content change events so the graph stays in sync with
`loresentry-content`.

Reached only through `loresentry-gateway` — this service is `ClusterIP` and has no
route from outside the cluster.

```
Cloudflare → ALB → gateway → graph-rag
```

Browsers never reach this service, so it has no CORS configuration — the gateway is
the only CORS boundary.

Relationships come from **explicitly stored file references**, never from guessing
at names that happen to appear in body text. Trash, other projects, and data the
caller cannot access must stay out of retrieval results.

## Stack

| | Version | Notes |
| --- | --- | --- |
| Python | **3.12** | Container base is `python:3.12-slim`. |
| FastAPI | 0.141.1 | |
| uvicorn | 0.52.4 (`[standard]`) | ASGI server. |
| pytest | 9.1.1 | |
| httpx | 0.28.1 | Required by `fastapi.testclient`. |
| Port | 8000 | Platform convention, shared with every other service. |

Dependencies are pinned exactly (`==`, not `>=`) so a CI run and a production image
built a month apart resolve to the same tree.

## Endpoints

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` | `/health` | `{"status":"ok"}`. Used by the Kubernetes probes. |
| `GET` | `/` | `{"service":"graph-rag-api"}` |

The gateway exposes this service publicly at `GET /graph`, which calls `/` here and
returns the payload nested under `upstream`.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
curl localhost:8000/health
```

## Test

```bash
pytest
```

No AWS or network access required.

## Deploy

`main` push runs [`.github/workflows/ci-cd.yaml`](.github/workflows/ci-cd.yaml):

```
pytest → docker build → ECR graph-rag/api:build-<run>-<attempt>
       → invoke loresentry-update-gitops → commit to loresentry-gitops → Argo CD
```

CI never touches Kubernetes. The image tag in the GitOps repository's
`workload/overlays/prod/kustomization.yaml` is the deployment record, and a rollback
is `git revert` of that commit.

Deployed to the `prod` namespace of the `lore-sentry-k8s` EKS cluster via Argo CD.

## Not implemented yet

- Amazon Neptune access, and the graph model itself.
- RAG/GraphRAG retrieval.
- The Kafka consumer for `loresentry-content` change events. No broker is running
  in the cluster yet.
