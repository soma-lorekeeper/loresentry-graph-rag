# 문서·관계 투영: fake consumer와 Neptune 저장

Kafka 수신 대신 fixture를 전달하는 fake consumer와, 공통 처리기·실제 Neptune HTTPS
Gremlin 저장 어댑터를 구현했다. 그래프와 Inbox의 원자성은 요청 하나로 유지한다.
현재 FastAPI 앱에 쓰기 API나 Kafka consumer를 연결하지 않았으며, fixture 실행은
명시적으로 호출하는 개발 도구다.

모델은 [노드·엣지 설계](../../../docs/NEPTUNE_GRAPH_MODEL.md), 처리 경계는
[Inbox 계약](../../../docs/GRAPH_INBOX_PATTERN.md)을 따른다.

## 코드와 호출 경계

| 코드 | 책임 |
|---|---|
| [projection/models.py](../../app/projection/models.py) | 내부 변경 묶음, UUID·타입·문서 쌍·삭제 충돌 검증 |
| [projection/service.py](../../app/projection/service.py) | 프로젝트 key 확인 후 원자적 저장 계약 호출 |
| [projection/fake.py](../../app/projection/fake.py) | 순차 전달, 실패 파티션 중단, 성공 후 메모리 offset 진행, 명시적 resume |
| [adapters/projection_gremlin.py](../../app/adapters/projection_gremlin.py) | 중복 조건 분기·모든 변경·Inbox를 한 Gremlin traversal로 구성 |
| [adapters/neptune_graph.py](../../app/adapters/neptune_graph.py) | 재사용 HTTP 클라이언트, 설정·SigV4·응답 확인·실패 변환 |
| [adapters/neptune_client.py](../../app/adapters/neptune_client.py) | 쓰기·refresh 원홉 읽기가 공유하는 HTTP·설정·SigV4 |
| [consume_graph_fixture.py](../../scripts/consume_graph_fixture.py) | fixture 검증과 실제 어댑터 조립·호출·종료 |

향후 실제 Kafka consumer도 `GraphProjectionHandler.handle(change, key=...)`을 호출한다.
헤더·wire payload를 내부 `ProjectionChange`로 변환하고, 반환값 `APPLIED`·`DUPLICATE`를
확인한 뒤 offset을 커밋해야 한다. fake의 offset과 실패 상태는 프로세스 메모리에만 있다.
Kafka의 rebalance·poll·영속 offset·DLQ·자동 재시도를 구현한 것으로 간주하지 않는다.

## 내부 입력 모델

이 모델은 저장 어댑터를 개발하기 위한 내부 계약이다. Content가 발행할 최종 Kafka
payload나 JSON Schema로 확정한 것은 아니다. 원본 트랜잭션 하나의 실제 변경을 한 묶음으로 전달한다.

| 필드 | 의미 |
|---|---|
| `event_id`, `project_id` | 정규 소문자 UUID 문자열 |
| `source` | `/content` 고정 |
| `event_type` | `FileChanged` 또는 `ProjectDeleted` |
| `files` | upsert할 문서의 현재 `id`, `title`, `folder_code`, `description`, `trashed` |
| `relations` | upsert할 현재 관계의 `id`, `low_document_id`, `high_document_id`, `description` |
| `deleted_file_ids` | 영구 삭제할 문서 ID |
| `deleted_relation_ids` | 실제로 삭제된 관계 ID |

각 변경 목록은 해당 이벤트가 반영할 항목이다. 목록에 없는 노드·엣지를 삭제하지 않는다.
특히 Content 조회에서 휴지통 관계가 숨겨졌다는 이유로 삭제 목록을 만들면 안 된다.
휴지통·복원은 `files[].trashed`로 현재 상태를 갱신하고, 관계는 보존한다.
복원에 누락 관계 보충이 필요하면 그 현재 관계를 `relations`에 함께 넣는다.

`ProjectDeleted`에는 항목별 변경 목록을 넣지 않는다. 해당 프로젝트의 `REFERENCES`와
`File`만 지우고 기존 Inbox와 이번 삭제 이벤트의 Inbox를 보존한다.
양 끝 노드가 없는 관계는 placeholder 없이 실패하며, 같은 쌍의 다른 관계 ID도 거부한다.
분류·설명 등 현재 속성은 기존 값을 교체한다.

Inbox에 정규화한 내부 입력의 `payload_sha256`도 저장한다. 같은 ID로 다른 업무 내용을
보내면 `PROJECTION_INBOX_CONFLICT`로 거부한다. hash는 payload나 그래프 버전 이력이 아니다.
내부 모델을 변경할 때에는 기존 이벤트의 정규화·hash 호환성도 유지해야 한다.

## 실행과 연결 설정

Linux 호스트의 이 저장소 루트에서 프로젝트 `.venv`를 사용한다.
먼저 AWS 연결 없이 [예제 fixture](../examples/graph-projection/changes.json)를 검증한다.

```bash
.venv/bin/python -m scripts.consume_graph_fixture docs/examples/graph-projection/changes.json --dry-run
```

환경변수로 writer를 설정한 경우 다음 명령은 **실제 Neptune에 쓰기**를 수행한다.
이 환경에서는 대상 VPC에 대한 DNS·네트워크 경로와 8182 접근이 필요하다.
`.env` 파일은 자동으로 읽지 않으므로 실행 프로세스에 환경변수를 주입한다.

```bash
.venv/bin/python -m scripts.consume_graph_fixture docs/examples/graph-projection/changes.json
```

| 설정 | 값·기본값 |
|---|---|
| `NEPTUNE_ENDPOINT` | 필수 writer 호스트명. 스킴·경로 없이 지정. 미설정 시 localhost로 대체하지 않음 |
| `NEPTUNE_PORT` | `8182` |
| `NEPTUNE_TIMEOUT_SECONDS` | `10`. connect timeout은 이 값과 3초 중 작은 값 |
| `NEPTUNE_IAM_AUTH` | `false`. `true`면 요청마다 SigV4 서명 |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | 이 순서로 사용. 미설정 시 `ap-northeast-2` |
| `--endpoint` | 환경변수의 host·port 대신 사용할 writer 기본 URL |

기존 GitOps의 writer 환경변수를 재사용한다. reader endpoint로 Inbox를 판정하지 않는다.
IAM 인증 사용 시 boto3 기본 credential chain에서 자격 증명을 얻고 요청마다 현재 자격
증명으로 `neptune-db` 서명을 생성한다. 코드·fixture에 AWS 키를 넣지 않는다.
기존 인프라 문서는 IAM DB 인증 비활성 상태를 기록하지만, 실제 배포 시 클러스터 설정을 확인한다.

HTTP는 loopback 테스트 URL에만 허용하며 그 외에는 HTTPS를 사용한다.
클라이언트는 context manager에서 종료하고, 주입받은 HTTP 클라이언트의 소유권은 호출 측에 둔다.
어댑터 자체는 재시도하지 않는다. timeout·커밋 응답 유실도 성공으로 바꾸지 않으며
같은 이벤트 ID로 전체를 재시도해야 한다.

예제 fixture는 문서 두 개와 관계 하나를 만들고 같은 관계 이벤트를 재전달한다.
처음 실행하면 결과는 `APPLIED`, `APPLIED`, `DUPLICATE`, 메모리의 다음 offset은 3이다.
같은 fixture를 다시 실행하면 모두 `DUPLICATE`다. 실제 데이터를 사용하려면 Content가
확정한 ID·현재 상태로 fixture를 구성한다.

## 검증

기본 테스트는 AWS나 Gremlin 서버 없이 입력 검증·HTTP 응답·SigV4·fake의 실패 중단을 확인한다.

```bash
.venv/bin/python -m pytest tests/test_projection.py -q
```

실제 traversal·롤백 검증에는 전용 TinkerTransactionGraph 3.7.3 컨테이너를 사용한다.
이 서버는 임시 테스트 데이터만 저장한다. 다음 명령을 같은 저장소 루트에서 실행하고,
서버 로그에서 시작 완료를 확인한 뒤 통합 테스트를 실행한다.

```bash
docker compose -p graph-projection-verification -f tests/gremlin/compose.yaml up -d
docker compose -p graph-projection-verification -f tests/gremlin/compose.yaml logs --tail 30
GREMLIN_TEST_ENDPOINT=http://127.0.0.1:18182 .venv/bin/python -m pytest tests/integration/test_projection_gremlin.py -q
docker compose -p graph-projection-verification -f tests/gremlin/compose.yaml down
```

통합 테스트는 자신이 만든 임의 프로젝트만 정리하며, endpoint가 없으면 건너뛴다.
노드·엣지 생성·설명 수정·삭제, 휴지통·복원, 프로젝트 격리, Inbox 보존, 중복 동시 처리,
실패 시 부분 반영 방지, 커밋 응답 유실 후 재처리, ANTLR 파싱·문자열 escaping을 확인한다.

이는 로컬 Gremlin의 실제 동작 검증이다. AWS Neptune의 TLS·VPC·IAM 권한·서비스 고유의
동시성 및 실패 응답까지 검증한 결과는 아니다. 배포 전 접근 가능한 개발 환경에서
전용 검증 프로젝트로 실제 Neptune 연결·쓰기·롤백을 추가 확인한다.

refresh의 실제 원홉 읽기와 Content 조회 포트 연결은 [별도 구현 문서](REFRESH_NEPTUNE.md)를 따른다.

Content의 Outbox INSERT·publisher, 최종 wire schema, 실제 Kafka consumer, 초기 적재·
재동기화 운영 절차, 사용자용 그래프 조회 API는 후속 작업이다.
