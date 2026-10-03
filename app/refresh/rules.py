"""갱신안의 순수 판단 경계. IO·현재 시간·난수 없이 전달된 값만 사용한다."""

from app.refresh import selection as selection_rules
from app.refresh.context import build_context
from app.refresh.errors import RulesNotImplemented
from app.refresh.evidence import make_chunks
from app.refresh.prompts import build_prompt


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

    def validate_candidate(self, snapshot, candidate, model_input):
        """후보 검증 구현 전에는 결과를 신뢰하거나 성공 처리하지 않는다."""
        raise RulesNotImplemented("validate_candidate")
