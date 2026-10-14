"""계획과 문단 작성의 별도 계약. 일반 설정·사실 지침은 기존 블록을 재사용한다."""

from app.refresh.prompts import (
    CONFIDENCE_CONTRACT,
    FACT_CONTRACT,
    JUDGMENT_EXAMPLES,
    SETTING_CRITERIA,
    TASK_INSTRUCTION,
    TERM_DEFINITIONS,
)

PLAN_CONTRACT = """## 1단계: 변경 위치 계획

### 목표

변경 원고의 새 사실·정정 사실을 기존 설정에 빠짐없이 반영할 위치를 정한다.
paragraphs는 서버가 기존 본문에 부여한 문단 ID·원문이며 읽기 전용이다.
모든 target_ids의 모든 문단을 변경 원고와 대조한다. reviewed_document_ids에는
검토한 target_ids를 모두 넣는다. 변경 사실 하나가 여러 문서·문단에 있으면 각각 계획한다.
물건 속성이 인물 본문에도 있거나 인물의 업무가 장소·조직 본문에도 있으면 함께 검토한다.

### 교체를 먼저 계획

기존 현재 속성·상태가 변경되면 해당 기존 문단을 REPLACE로 계획한다.
APPEND만 추가하고 낡은 현재 사실을 남기지 않는다. 같은 속성의 모든 등장 위치를 찾는다.
수정할 문단에는 변경된 부분과 보존할 부분을 instruction에 구체적으로 적는다.
예: 기존 문단에 '붉은 망토를 입으며 왼손잡이다'가 있고 새 원고가 파란 망토를 명시하면,
'망토를 파란색으로 교체하고 왼손잡이 속성은 보존'하는 REPLACE를 계획한다.
다른 설정에도 그 망토 색이 적혀 있으면 그 문단에도 동일한 정정을 계획한다.
과거 이력·회상과 현재 사실, 실제 변화와 원고 revision에 의한 정정을 구분한다.

### 생성·추가·삭제

REPLACE는 지정 문단의 일부 사실을 수정하되 그 문단의 무관한 사실을 보존한다.
DELETE는 문단 전체를 제거할 근거가 있을 때만 쓴다. 일부만 낡았으면 REPLACE다.
APPEND는 paragraph_id 뒤에 새 문단을 추가한다. 비어 있는 본문에는 P0000을 쓴다.
각 작업에 고유 edit_id(E1 등), document_id, base_revision_no, paragraph_id,
operation, instruction, source_markers를 넣는다. 문단 ID나 위치를 새로 만들지 않는다.

독립적인 신규 설정은 new_documents에 candidate_id, folder_code, name, instruction,
source_markers를 넣는다. candidate_id는 'new:유형:이름' 형식이다.
설정 본문에서 누락되기 쉬운 기간·경력·현재 상태·부정·조건·권한·목적을 instruction에 적는다.
기존 설정의 다른 이름·상세 내용을 별도의 신규 문서로 중복 생성하지 않는다.

새 관계는 relations에 document_id, target_document_id, source_markers를 넣는다.
기존 관계를 재생성하지 않는다. 기존 활성 문서와 이번 신규 candidate_id만 연결한다.
허용 관계 범위 안에서 본문에 반영할 업무 분담·협력과 독립 신규 설정의 원고 연결도 검토한다.
문서 삭제나 기존 관계 삭제는 이 계약에 없다.

### 근거와 출력

각 작업의 source_markers는 변경 원고의 해당 사실을 실제로 뒷받침하는 C 마커다.
근거 없는 수정·삭제를 계획하지 않는다. 수정할 원고나 title·이름 필드 자체는 계획하지 않는다.
출력은 reviewed_document_ids, edits, new_documents, relations 네 목록의 JSON 객체다.
본문 수정본·구조화 facts는 2단계에서 작성하므로 이 단계에는 출력하지 않는다.
"""

WRITE_CONTRACT = """## 2단계: 확정 계획의 문단 작성

### 범위

change_plan은 서버가 검증한 변경 작업 목록이다. 전체 계획을 정확히 수행한다.
작업을 추가·누락하거나 문서·문단·작업 종류·신규 후보의 정체를 바꾸지 않는다.
paragraphs의 원문을 기준으로 각 instruction을 적용한다. 변경한 속성의 옛 현재 설명은
해당 문단에서 교체하고, 경력·기간·조건 등 변경과 무관한 기존 사실은 보존한다.

### 문단별 출력

paragraph_edits에는 모든 edit_id를 정확히 한 번 넣는다.
각 원소는 edit_id, text, fact_ids다. REPLACE의 text는 그 문단 전체의 수정본이며,
APPEND의 text는 추가할 새 문단이다. DELETE의 text는 빈 문자열이다.
서버가 지정 문단을 교체·삭제·추가하고 나머지 본문을 원문 그대로 조립한다.
문서 전체의 document_proposals나 원문 위치 숫자는 반환하지 않는다.
지정한 문단 외의 기존 문단을 text에 함께 복사하지 않는다. APPEND는 새 내용만 쓴다.
변경 없는 기존 문단의 사실을 facts로 다시 입증할 필요는 없다.

### 신규 설정과 관계

new_document_proposals에는 계획의 모든 new_documents를 같은 candidate_id·유형·이름으로
작성한다. 각 body_text에 계획 instruction의 사실·기간·부정·조건을 빠짐없이 반영한다.
원고에 없는 보충이나 불필요한 요약으로 확인된 내용을 지우지 않는다.
relation_proposals에는 계획의 모든 relations를 한 번씩 작성한다.
관계 키·revision·operation·fact_ids 계약은 일반 관계 계약과 같다.
설명과 evidence는 참조 사실에서 서버가 복원하므로 따로 출력하지 않는다.
각 문단·신규 문서·관계의 fact_ids는 현재 변경 원고의 사실을 참조한다.
계획의 source_markers는 우선 확인할 근거다. 추가 근거가 필요하면 입력에 있는 다른 변경
원고 마커도 사용할 수 있고, 서버는 그 확장을 기록한다. 기존 설정 원문을 새 사실의 근거로 쓰지 않는다.

### 최종 점검

facts의 각 action·surface는 선택한 마커 원문에서 그대로 고른다.
statement는 요약할 수 있으며 역할·부정·시점·범위를 유지한다.
각 edit_id·신규 candidate_id·관계쌍의 누락 여부와 계획에 없는 출력 여부를 대조한다.
계획 instruction의 교체 내용이 실제 text에 반영됐는지 확인한다.
출력은 facts, paragraph_edits, new_document_proposals, relation_proposals 네 목록의 JSON이다.
"""


def plan_instructions():
    return TERM_DEFINITIONS + TASK_INSTRUCTION + SETTING_CRITERIA + PLAN_CONTRACT


def write_instructions(confidence=False):
    fact_contract = FACT_CONTRACT.replace(JUDGMENT_EXAMPLES, "")
    # 실제 관계 전송 계약을 재사용하되 기존 본문 전체 교체 계약을 중첩하지 않는다.
    relation_contract = """\n## 관계 출력 필드\n
각 관계는 document_id, target_document_id, base_revision_no, target_base_revision_no,
relation_key, reverse_relation_key, operation=ADD, fact_ids를 넣는다.
기존 문서는 실제 revision_no, 새 후보는 null이다. 관계 키는 상대 문서 유형의
FOLDER_RELATION_KEYS 값이고 역방향 키는 자기 문서 유형의 값이다.
신규 설정은 candidate_id, folder_code, name, body_text, evidence=[{marker: C마커}], fact_ids다.
"""
    return (
        TERM_DEFINITIONS
        + TASK_INSTRUCTION
        + fact_contract
        + (CONFIDENCE_CONTRACT if confidence else "")
        + relation_contract
        + WRITE_CONTRACT
    )
