"""실제 생성에 명시적으로 허용한 지시문 버전. v11은 비교 기준선이다."""

BASELINE_PROMPT_VERSION = "refresh-prompt-v11"
IMPROVED_PROMPT_VERSION = "refresh-prompt-v12"
PROMPT_VERSION = BASELINE_PROMPT_VERSION
SUPPORTED_PROMPT_VERSIONS = frozenset(
    {BASELINE_PROMPT_VERSION, IMPROVED_PROMPT_VERSION, "refresh-prompt-v13"}
)
