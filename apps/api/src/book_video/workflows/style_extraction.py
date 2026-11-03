"""Deterministic, evidence-backed abstract style extraction.

This workflow turns human observation notes and licensed references into
abstract style features. It only emits fixed, generic descriptions; it never
copies a third-party transcript, full subtitle, video, audio, cover or product
image into a style card. Every feature keeps its supporting evidence and a
sample count so a human can review the basis before the card feeds creation.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# feature_key -> (abstract label, fixed abstract explanation, trigger keywords)
# Labels and explanations are generic templates; source notes are never quoted.
STYLE_PATTERNS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "opening_hook",
        "提问式开场",
        "以提问或反常识悬念开场吸引停留，不先抛结论",
        ("开场", "提问", "反问", "悬念", "反常识", "钩子", "？", "?"),
    ),
    (
        "narrative_structure",
        "观点递进结构",
        "按观察到的痛点、个人观点到行动建议的顺序组织内容",
        ("结构", "章节", "层次", "痛点", "观点", "行动", "步骤", "叙事", "分段"),
    ),
    (
        "pacing",
        "中等平稳节奏",
        "节奏平稳，给观众留出理解时间，不追求急促堆砌",
        ("节奏", "语速", "时长", "紧凑", "舒缓", "停顿"),
    ),
    (
        "visual_style",
        "自制图形表达",
        "用自制文字与图形表达，不使用未授权封面、内文图或商品图",
        ("画面", "图形", "配图", "封面", "视觉", "镜头"),
    ),
    (
        "subtitle_style",
        "简洁字幕",
        "屏幕字幕只保留关键短句，不堆砌长文案",
        ("字幕", "屏幕文字", "标题字", "大字报"),
    ),
    (
        "tone",
        "朋友式语气",
        "语气自然克制，像向朋友推荐一本书，不夸大结论",
        ("语气", "自然", "克制", "轻松", "温和", "亲切", "朋友"),
    ),
    (
        "cta",
        "轻互动号召",
        "结尾用轻量提问邀请分享方法，不诱导刷量或点赞",
        ("互动", "评论", "分享", "点赞", "行动号召", "留言", "关注"),
    ),
    (
        "audience_match",
        "面向泛读者",
        "面向普通读者表达，避免行业黑话与过窄的圈层表述",
        ("受众", "读者", "目标人群", "对象", "普通人"),
    ),
)


def _source_text(record: Mapping[str, Any]) -> str:
    parts = [
        str(record.get("note") or ""),
        str(record.get("source_note") or ""),
        str(record.get("title") or ""),
        str(record.get("explanation") or ""),
    ]
    return " ".join(parts).casefold()


def extract_style_features(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Return ``(features, evidence_ids, reference_ids)`` for the given sources.

    ``records`` may mix ``evidence`` and ``content_reference`` items. Feature
    ``evidence_ids`` only list evidence record ids; references are returned
    separately so the card can link them without treating a reference as evidence.
    """
    evidence_ids: list[str] = []
    reference_ids: list[str] = []
    for record in records:
        record_id = str(record.get("id") or "")
        if not record_id:
            continue
        if record.get("kind") == "reference":
            if record_id not in reference_ids:
                reference_ids.append(record_id)
        else:
            if record_id not in evidence_ids:
                evidence_ids.append(record_id)

    features: list[dict[str, Any]] = []
    for feature_key, label, explanation, keywords in STYLE_PATTERNS:
        supporting_evidence: list[str] = []
        for record in records:
            record_id = str(record.get("id") or "")
            if not record_id:
                continue
            text = _source_text(record)
            if any(keyword.casefold() in text for keyword in keywords):
                if record.get("kind") == "reference":
                    # References support the pattern but are not evidence ids.
                    continue
                if record_id not in supporting_evidence:
                    supporting_evidence.append(record_id)
        if supporting_evidence:
            features.append(
                {
                    "feature_key": feature_key,
                    "label": label,
                    "explanation": explanation,
                    "evidence_ids": supporting_evidence,
                    "sample_count": len(supporting_evidence),
                }
            )

    features.sort(key=lambda item: item["feature_key"])
    return features, evidence_ids, reference_ids


def style_guidance(features: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Abstract, non-binding hints for script generation.

    Only feature keys and generic labels leave the style card; no note text,
    transcript, or third-party wording is copied into the generation context.
    """
    canonical_labels = {key: label for key, label, _explanation, _keywords in STYLE_PATTERNS}
    guidance: list[dict[str, Any]] = []
    for feature in features:
        key = str(feature.get("feature_key") or "")
        if key not in canonical_labels:
            continue
        guidance.append(
            {
                "feature_key": key,
                "label": canonical_labels[key],
                "sample_count": int(feature.get("sample_count") or 0),
            }
        )
    guidance.sort(key=lambda item: item["feature_key"])
    return guidance
