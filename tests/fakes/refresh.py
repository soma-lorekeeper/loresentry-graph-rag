"""테스트와 명시적 평가가 같은 메모리 IO 대체 구현을 공유한다."""

from types import SimpleNamespace

from evaluation.fakes import (
    Calls as Calls,
)
from evaluation.fakes import (
    FakeArtifacts as FakeArtifacts,
)
from evaluation.fakes import (
    FakeDocuments as FakeDocuments,
)
from evaluation.fakes import (
    FakeInbox as FakeInbox,
)
from evaluation.fakes import (
    FakeJsonArtifacts as FakeJsonArtifacts,
)
from evaluation.fakes import (
    FakeModel as FakeModel,
)
from evaluation.fakes import (
    FakePublisher as FakePublisher,
)
from evaluation.fakes import (
    FakeRelations as FakeRelations,
)
from evaluation.fakes import (
    identity as identity,
)


def count_input_tokens(**arguments):
    """OpenAI `responses.input_tokens.count` 대체. 원격 토크나이저 없이 결정적으로 센다.

    지시문과 사용자 입력의 글자 수 합을 토큰 수로 돌려준다. 테스트가 입력 길이만으로
    토큰 예산 경계를 계산할 수 있게 한다. model·text 같은 나머지 인자는 무시한다.
    """
    tokens = len(arguments["instructions"]) + len(arguments["input"][0]["content"])
    return SimpleNamespace(input_tokens=tokens)
