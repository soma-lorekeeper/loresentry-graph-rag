#!/bin/bash
# 컨테이너 시작 스크립트. 이미지 CMD(uvicorn)를 EDOT Python distro로 띄운다(docs LOG_SCHEMA.md 7절 "추적 엔진").
#
# - 추적·지표 기본값: 아래 OTEL_* 값은 비어 있을 때만 넣는다. gitops overlay가 준 값(dev의 OTEL_TRACES_EXPORTER=otlp,
#   OTEL_METRICS_EXPORTER=otlp, collector 주소, resource의 환경·파드 이름)이 먼저다. 그래서 여기에는 어디서 돌아도 안전한 값만 둔다.
#   - 이름: span resource의 service.name은 로그의 service.name(app/main.py의 SERVICE)과 같은 Kubernetes 이름 graph-rag-api다
#   - span은 보내지 않음(prod 포함). 꺼 두어도 trace ID는 만들어지고 전파된다. 로그는 stdout → Filebeat가 모은다
#   - 지표도 보내지 않음(prod 포함). dev overlay가 OTEL_METRICS_EXPORTER=otlp를 주면 span과 같은 collector를 거쳐 Elasticsearch
#     (metrics-*.otel-*)에 쌓인다(docs ALERTING.md 7절). 계측의 HTTP 지표(FastAPI 서버, httpx의 content 호출·Neptune 쿼리),
#     system_metrics의 런타임 지표(process.runtime.cpython.*, cpython.gc.*), 로깅 팩토리(app/logs/_metrics.py)의 지표다
#   - 보낼 때는 OTLP/HTTP protobuf(collector 4318). distro의 기본값(grpc)을 쓰지 않는다
#   - sampler는 parentbased_always_on(LOG_SCHEMA.md 7절 "span 저장"의 dev 100%). distro 기본 sampler(composite)는 root span
#     문맥에 tracestate(ot=th:0)를 넣는데 우리 규약은 traceparent 하나다. 전파기도 W3C traceparent(tracecontext)만 쓴다(baggage는
#     쓰지 않는다)
#   - span 속성은 OpenTelemetry 안정 이름(http.request.method, url.path, http.route, url.full 등)
#   - GenAI 대화 내용(프롬프트, 모델 응답)은 span에 담지 않는다(collector 규칙은 잘못 켜졌을 때의 두 번째 방어선)
#   - 계측은 requirements.txt에 둔 것만 설치하고, distro가 저절로 켜는 것 중 쓰지 않는 것을 끈다:
#     fastapi(로깅 팩토리의 setup_tracing이 앱에 직접 단다), httpx2(OpenAI SDK의 HTTP client. OpenAI 호출에는 traceparent를
#     싣지 않는다), logging(로그 줄의 ID는 로깅 팩토리가 넣는다),
#     confluent_kafka(설치하지 않는다. traceparent를 바꾸지 않고 하나 더 붙여, Kafka는 로깅 팩토리가 맡는다).
#     켜지는 것은 httpx(content 호출, Neptune 쿼리), botocore(S3, KMS)와 system_metrics(distro가 함께 설치하는 지표 계측.
#     distro가 process.runtime.cpython.*·cpython.gc.*만 남기고, system.*은 내지 않는다)다
#   - resource detector: distro는 Kubernetes 안에서 클라우드 detector(gcp_resource_detector, aws_eks)를 더한다. aws_eks는 파드의
#     service account로 클러스터 정보를 읽다가 403을 받고 stderr에 평문 줄(AwsEksResourceDetector failed: HTTP Error 403)을 남긴다.
#     그 줄은 ECS JSON이 아니라 log.level unknown이 된다. 그래서 distro의 기본 목록에서 클라우드 detector만 뺀 목록을 쓴다.
#     otel(OTEL_SERVICE_NAME·OTEL_RESOURCE_ATTRIBUTES를 읽는 detector)을 마지막에 두어 환경 변수의 resource 값이 이긴다
# - service.version: 이미지 태그(APP_VERSION, Dockerfile의 ENV)를 span resource 앞에 더한다. 로그의 service.version과 같은 값이다.
#   overlay가 준 OTEL_RESOURCE_ATTRIBUTES가 있으면 그 뒤에 이어 붙인다.
# - exec: opentelemetry-instrument가 distro를 붙인 뒤 명령을 exec하므로 그 프로세스(uvicorn)가 PID 1이 되어 Kubernetes의 종료
#   신호(SIGTERM)를 직접 받는다(graceful shutdown).
set -eo pipefail

export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-graph-rag-api}"
export OTEL_TRACES_EXPORTER="${OTEL_TRACES_EXPORTER:-none}"
export OTEL_METRICS_EXPORTER="${OTEL_METRICS_EXPORTER:-none}"
export OTEL_LOGS_EXPORTER="${OTEL_LOGS_EXPORTER:-none}"
export OTEL_EXPORTER_OTLP_PROTOCOL="${OTEL_EXPORTER_OTLP_PROTOCOL:-http/protobuf}"
export OTEL_TRACES_SAMPLER="${OTEL_TRACES_SAMPLER:-parentbased_always_on}"
export OTEL_PROPAGATORS="${OTEL_PROPAGATORS:-tracecontext}"
export OTEL_SEMCONV_STABILITY_OPT_IN="${OTEL_SEMCONV_STABILITY_OPT_IN:-http,database}"
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT:-false}"
export OTEL_PYTHON_DISABLED_INSTRUMENTATIONS="${OTEL_PYTHON_DISABLED_INSTRUMENTATIONS:-fastapi,httpx2,logging,confluent_kafka}"
export OTEL_EXPERIMENTAL_RESOURCE_DETECTORS="${OTEL_EXPERIMENTAL_RESOURCE_DETECTORS:-process_runtime,os,telemetry_distro,service_instance,containerid,otel}"
export OTEL_RESOURCE_ATTRIBUTES="service.version=${APP_VERSION:-unknown}${OTEL_RESOURCE_ATTRIBUTES:+,${OTEL_RESOURCE_ATTRIBUTES}}"

exec opentelemetry-instrument "$@"
