"""갱신안의 순수 판단 경계. IO·현재 시간·난수 없이 전달된 값만 사용한다."""

from app.refresh import selection as selection_rules
from app.refresh.context import build_context
from app.refresh.evidence import make_chunks
from app.refresh.models import DocumentState
from app.refresh.prompts import build_prompt
from app.refresh.selection import reject
from app.refresh.validation import validate_candidate


class RefreshRules:
    """서비스가 조회한 스냅샷으로 검증·선택·문맥 구성·후보 검증을 수행한다."""

    def validate_input(self, request, source):
        """입력 식별·프로젝트·원문 한도 위반 시 RefreshFailure를 발생시킨다."""
        return selection_rules.validate_input(request, source)

    def select(self, request, source, related):
        """명시적 관계와 요청 대상으로 선택 ID와 추가 조회 ID를 반환한다."""
        return selection_rules.select(request, source, related)

    def assemble(self, request, source, related, selection, fetched):
        """추가 조회 범위를 검증하고 원본 revision을 보존한 실행 스냅샷을 만든다."""
        return selection_rules.assemble(request, source, related, selection, fetched)

    def chunk(self, snapshot):
        """ACTIVE 본문을 빠짐없이 분할하여 원문 구간 근거를 붙인다."""
        return make_chunks(snapshot)

    def build_context(self, snapshot, chunks):
        """대상·변경 문서·관계·출처를 결정적 순서의 JSON 문맥으로 만든다."""
        return build_context(snapshot, chunks)

    def build_prompt(self, context):
        """외부에서 주어진 버전과 문맥 예산으로 제안 프롬프트를 만든다."""
        return build_prompt(context)

    def model_inputs(self, snapshot, chunks):
        """대상별로 모든 변경 자료를 함께 제공하고 전체 호출 예산을 사전 검증한다."""
        active = {
            d.document_id for d in snapshot.documents if d.state == DocumentState.ACTIVE
        }
        if not active.intersection(snapshot.changed_ids):
            return ()
        groups = tuple((target,) for target in snapshot.target_ids)
        if not groups and len(active) >= 2:
            groups = ((),)  # 문서 대상이 없어도 새 관계만 제안할 수 있다.
        if len(groups) > snapshot.request.settings.max_model_calls:
            reject("CALL_BUDGET_EXCEEDED", "Model call budget exceeded")
        return tuple(
            self.build_prompt(build_context(snapshot, chunks, group))
            for group in groups
        )

    def validate_candidate(self, snapshot, candidate, model_input):
        """대상·revision·근거를 검증하고 전체 요청의 처리 결과를 반환한다."""
        return validate_candidate(snapshot, candidate, model_input)
