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

Relationships come from **explicitly stored document references**, never from guessing
at names that happen to appear in body text. Trash, other projects, and data the
caller cannot access must stay out of retrieval results.

## Documentation

See [the documentation guide](docs/README.md) for the current implementation,
provided and outgoing APIs, code navigation, and verification scope.
The [Content domain glossary](docs/domain-glossary.md) defines document, body text,
relation keys, revisions, and proposals across the internal contracts.

## Refresh implementation

The internal refresh service validates changed documents, builds source-preserving
context, plans paragraph edits across all selected settings in one LLM call, then writes and assembles those edits in a second call. It validates existing-document updates, new setting-document candidates, and relation proposals. Manuscript bodies remain read-only. New setting candidates use temporary
IDs so relations can reference them before Content approval. Results serialize to JSON; completion values carry the result location.
Generation defaults to `refresh-prompt-v11`; `refresh-prompt-v12` is an explicit
option. Both use the unchanged `refresh-candidate-v5` contract, with
structured facts referenced by every proposal. [prompt_versions.py](app/refresh/prompt_versions.py)
defines the supported live versions. Each fact selects one source marker;
the server restores the full original chunk and builds relation descriptions and evidence
from the referenced facts. Results use `refresh-result-v3`. Historical candidate readers
and response replay retain older contracts without relaxing current live validation.
The v12 instructions structure facts and compare them with the source before writing
new or changed body claims. They preserve ownership, negation, conditions, scope,
time, and certainty; preserved existing body text does not require fact extraction again.
`chunk_overlap_chars` defaults to `0`, preserving the earlier chunks and serialized
settings. A positive value below `chunk_chars` repeats complete sentences from the
previous chunk when space permits. Original source positions and the chunk size
limit remain exact; overlapping text counts toward the context budget.
Current candidates with specific validation diagnostics can receive up to two feedback
repair per call (`refresh-validation-repair-v1`). Only that call is regenerated;
successful calls are retained, and the replacement and final aggregate are validated.
Repairs share the request's call budget and reserve calls for remaining targets.
Saved results, including failures, remain immutable and are reused without generation.
The OpenAI Responses adapter and local HTTP integration are implemented. Conversation-authored
responses verify the refresh flow. Real OpenAI calls are also evaluated against synthetic
novel fixtures; see [refresh quality changes and validation](docs/migration/refresh-quality.md)
for measured results and limits. A real [S3 artifact adapter](docs/implementation/S3_ARTIFACTS.md) is available through
service injection; the evaluation CLI still uses fake stores. Kafka refresh entry points are connected through the business runtime;
Content owns the request HTTP API. See
[OpenAI migration results](docs/migration/openai-adapter.md).

The Responses adapter provides no seed setting and leaves `reasoning.effort` at the
model default (`medium`). A real `gpt-6-luna` smoke run using v11 completed one
record-and-consent case with four calls, including one successful repair. Its final
result was `PROPOSED` with human semantic review pending. Repeated semantic A/B
trials did not establish a v12 advantage, so v11 remains the default. Long-input
trials reached the earlier output limit; the example and local output budget now
allow 32,768 output tokens, 922,000 input tokens (each call may use 80% of it, counted
with the OpenAI input token count API), a 1,050,000-token context budget, and a
240-second request timeout. This budget change is separate from prompt quality.

Each evaluation run also writes `report.md` with proposal diffs,
check results, latency, and known token usage. To render existing evaluation JSON
without model calls, run `.venv/bin/python -m scripts.report_refresh .evaluation/live-2`
from this repository, replacing the directory with your evaluation output.
See [report usage and limits](docs/migration/openai-adapter.md#markdown으로-결과-검토).
The report separates candidate counts across attempts (including regeneration
duplicates), validation failures, repair attempts and successes, and final proposals.
For independent repeated v11/v12 trials, run the following from this repository:

```bash
.venv/bin/python -m scripts.evaluate_refresh_ab \
  --live --env-file .env --suite semantic --repeats 2 --max-calls 64 \
  --max-total-calls 256 --trial semantic-ab --output .evaluation/semantic-ab
```

This live command reserves the full call budget, including repairs, for both
conditions and both repetitions. `--repeats` must be at least two. Add
`--include-overlap --overlap-chars 200` for a third v12 condition and raise the
total reservation to at least 384 for this example. The private comparison summary
contains counts and status labels, without source or proposal bodies, and leaves
the quality decision pending human review. See [A/B usage](docs/migration/openai-adapter.md#프롬프트와-청크-겹침-반복-비교).

## Graph projection implementation

The [Kafka polling worker](docs/implementation/KAFKA_POLLING.md) uses
`confluent-kafka==2.16.0` for real polling and manual commits, with fake message
handling and publishing. FastAPI starts and stops it automatically when
`KAFKA_POLLING_ENABLED=true`; GitOps supplies the broker address and sandbox topic.
The standalone `scripts.poll_kafka` CLI is also available.
The [Kafka transport adapters](docs/implementation/KAFKA_TRANSPORT.md) implement
Content envelope validation, acknowledged completion/DLQ publishing, explicit business
handler injection, and bounded retries. Business payload mapping, ordered projection, refresh assembly, and FastAPI runtime
wiring are implemented. Business consumers are opt-in; the sandbox retains fake processing.

The [graph projection adapter](docs/implementation/GRAPH_PROJECTION.md) writes
Content document nodes, undirected relation pairs, and completed-event Inbox
records in one Neptune HTTPS Gremlin request. A fake consumer and fixture CLI
exercise the shared handler without Kafka. Input batches are an internal contract;
the live Content consumer uses the sequenced snapshot path with deletion tombstones.

Local TinkerTransactionGraph tests verify mutations, rollback, project isolation,
duplicates, and lost commit responses. AWS Neptune deployment validation remains
separate. The existing FastAPI app continues to expose status endpoints only.

The [refresh relation adapter](docs/implementation/REFRESH_NEPTUNE.md) reads active
one-hop neighbors from the same nodes and edges. `neptune_refresh_service(...)`
wires it into the existing refresh flow; missing bodies still go through the
[Content HTTP adapter](docs/implementation/CONTENT_HTTP.md) when `documents` is
omitted and a trusted `user_id` is supplied. It calls the existing project and
document APIs, preserves body text and revision (including zero), and checks scope.
Query failures or missing projected seeds stop generation.

Refresh recovery uses a [completion Inbox](docs/implementation/REFRESH_INBOX.md)
in Neptune and immutable S3 artifacts. Completion and a pending Outbox event are
recorded atomically before the request offset is committed. A separate relay
retries Kafka delivery and marks the event sent only after acknowledgment;
unfinished requests reuse saved results or run again.
There is no separate job-state store or execution lease. Fact validation keeps
source wording and known document references, while surface-to-document-name or
alias equality no longer blocks a proposal.

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
.venv/bin/ruff check --fix app tests evaluation scripts
.venv/bin/ruff format app tests evaluation scripts
```

Check without changing files, using the same commands as CI:

```bash
.venv/bin/ruff check app tests evaluation scripts
.venv/bin/ruff format --check app tests evaluation scripts
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

- User-facing graph queries against Amazon Neptune. Projection writes and Inbox
  are implemented through the explicit fixture entry point; AWS validation remains.
- General RAG/GraphRAG retrieval beyond refresh's one-hop relation lookup.
- Production refresh activation requires the private S3/Pod Identity infrastructure and GitOps enablement described in [deployment](deploy/README.md).
