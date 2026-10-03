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

## Documentation

See [the documentation guide](docs/README.md) for the current implementation,
provided and outgoing APIs, code navigation, and verification scope.

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
| `GET` | `/health/db` | Checks Neptune `/status`; returns diagnostic fields or `503`. See [API details](docs/API.md). |
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

## Lint and format

[Ruff](https://docs.astral.sh/ruff/) checks Python code and formats `app/` and
`tests/`. Install the pinned development dependencies using the setup above.
Configuration lives in `pyproject.toml`: Python 3.12, a target line length of 88,
basic error checks, unused imports, import sorting, bug-prone patterns (`B`),
and modern Python syntax (`UP`). Docstring rules are not enabled.

Apply safe lint fixes and formatting locally:

```bash
.venv/bin/ruff check --fix app tests
.venv/bin/ruff format app tests
```

Check without changing files, using the same commands as CI:

```bash
.venv/bin/ruff check app tests
.venv/bin/ruff format --check app tests
```

## Test

```bash
.venv/bin/python -m pytest -q
```

No AWS or external network access required. Integration tests use a loopback HTTP
server and need local socket access. Run only pure rules with
`.venv/bin/python -m pytest tests/test_graph.py -q`, or select real HTTP integration
with `.venv/bin/python -m pytest -m integration -q`.

## Deploy

`main` push runs [`.github/workflows/ci-cd.yaml`](.github/workflows/ci-cd.yaml):

```
ruff check + format --check → pytest → docker build → ECR graph-rag/api:build-<run>-<attempt>
       → invoke loresentry-update-gitops → commit to loresentry-gitops → Argo CD
```

CI never touches Kubernetes. The image tag in the GitOps repository's
`workload/overlays/prod/kustomization.yaml` is the deployment record, and a rollback
is `git revert` of that commit.

Deployed to the `prod` namespace of the `lore-sentry-k8s` EKS cluster via Argo CD.

## Not implemented yet

- Graph queries and writes against Amazon Neptune, and the graph model itself. Neptune status checks are implemented.
- RAG/GraphRAG retrieval.
- The Kafka consumer for `loresentry-content` change events. No broker is running
  in the cluster yet.
