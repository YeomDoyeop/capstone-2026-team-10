"""장르별 자동 편집 길이와 중요도 가중치."""

SHORT_FORM_PROFILES = frozenset({"game", "variety"})
SHORT_FORM_DEFAULT_SECONDS = 120
SHORT_FORM_MAX_SECONDS = 180
SHORT_FORM_SCORE_WEIGHTS = {
    "chapter_llm_score": 0.10,
    "llm_score": 0.20,
    "heatmap_score": 0.55,
    "chat_score": 0.05,
    "comment_score": 0.05,
    "volume_score": 0.05,
}


def category_target_seconds(profile: str, seconds: int) -> int:
    if profile in SHORT_FORM_PROFILES:
        return max(60, min(SHORT_FORM_MAX_SECONDS, seconds))
    return seconds
