#!/usr/bin/env python3
"""Build structured public case intelligence and case-card summaries."""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any


CASE_INTELLIGENCE_FIELDS = (
    "case_theory",
    "current_posture",
    "why_it_matters",
    "latest_change",
    "latest_meaningful_event",
    "latest_meaningful_event_date",
    "claim_category",
    "claims_asserted",
    "ai_conduct_alleged",
    "works_or_data_at_issue",
    "technology_or_model_at_issue",
    "procedural_stage",
    "pending_motion_or_next_event",
    "related_cases",
    "confidence_level",
    "missing_information",
    "source_references",
)

CLAIM_CATEGORIES = {
    "copyright_training_data",
    "copyright_generated_output",
    "copyright_music_or_audio",
    "copyright_news_or_publishing",
    "patent_ai_software",
    "trade_secret_or_transparency",
    "right_of_publicity",
    "privacy_or_consumer_protection",
    "platform_scraping_or_access",
    "securities_or_corporate_ai_disclosure",
    "administrative_or_foia",
    "other_ai_litigation",
    "unknown",
}

PROCEDURAL_STAGES = {
    "newly_filed",
    "service_or_initial_admin",
    "motion_practice",
    "motion_to_dismiss",
    "discovery",
    "stayed",
    "appeal",
    "significant_ruling",
    "settlement_or_voluntary_dismissal",
    "judgment",
    "resolved",
    "unknown",
}

PUBLIC_SUMMARY_BANNED_PHRASES = (
    "the tracker is monitoring",
    "how intellectual property doctrines apply to ai development and use",
    "in a dispute involving artificial intelligence systems, model outputs, or training data",
    "ai systems, model outputs, or training data",
    "artificial intelligence systems, model outputs, or training data",
    "unspecified ip or privacy claims",
    "claims against ai developer claims against",
    "violated copyright infringement rights",
    "violated patent infringement rights",
    "violated trademark rights",
    "violated trade secret rights",
)

TRANSPARENT_FALLBACK_SENTENCE = (
    "The complaint has been docketed, but the available parsed materials do not yet identify "
    "the specific AI system, works, data, or training/output theory at issue."
)

MEANINGFUL_EVENT_THRESHOLD = 60

MEANINGFUL_EVENT_RULES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "judgment",
        95,
        (
            r"\bclerk'?s judgment\b",
            r"\bfinal judgment\b",
            r"\bjudgment (?:is |was |has been )?(?:entered|affirmed|reversed)\b",
            r"\bmandate issued\b",
        ),
    ),
    (
        "settlement_or_voluntary_dismissal",
        94,
        (
            r"\bnotice of voluntary dismissal\b",
            r"\border of dismissal\b",
            r"\bdismissed with prejudice\b",
            r"\bnotice of settlement\b",
            r"\bsettlement (?:has been |was |is )?(?:reached|approved)\b",
        ),
    ),
    (
        "appeal",
        92,
        (
            r"\bnotice of appeal\b",
            r"\bappellant\b",
            r"\bappellee\b",
            r"\bappeal pending\b",
            r"\bstay pending appeal\b",
            r"\boral argument\b",
            r"\banswering brief\b",
            r"\breply brief\b",
            r"\bamicus brief\b",
            r"\bbrief submitted\b",
        ),
    ),
    (
        "stayed",
        90,
        (
            r"\bcase (?:is |was |hereby )?stayed\b",
            r"\ball proceedings and deadlines\b[^.]{0,120}\bstayed\b",
            r"\b(?:proceedings|deadlines|action) (?:are|is|be|shall be) (?:hereby )?stayed\b",
            r"\bstaying (?:this )?(?:case|action|proceedings|deadlines)\b",
            r"\border(?:ed)?[^.]{0,160}\bstay(?:ing|ed)? (?:this )?(?:case|action|proceedings|deadlines)\b",
        ),
    ),
    (
        "motion_to_dismiss",
        86,
        (
            r"\bmotion to dismiss\b",
            r"\bmoved to dismiss\b",
            r"\bintends to file a motion to dismiss\b",
        ),
    ),
    (
        "motion_practice",
        84,
        (
            r"\bmotion for summary judgment\b",
            r"\bsummary judgment motion\b",
            r"\bdaubert\b",
            r"\bopposition to .*motion\b",
            r"\breply in support of .*motion\b",
            r"\bmotion for preliminary injunction\b",
            r"\bpreliminary injunction\b",
        ),
    ),
    (
        "significant_ruling",
        82,
        (
            r"\bthe court (?:granted|denied|held|ordered|approved|rejected|vacated|affirmed|reversed)\b",
            r"\border (?:granting|denying|approving|staying|dismissing)\b",
            r"\bopinion and order\b",
            r"\bmemorandum opinion\b",
        ),
    ),
    (
        "discovery",
        78,
        (
            r"\bdiscovery order\b",
            r"\bmotion to compel\b",
            r"\bstay(?:ing|ed)? (?:all )?discovery\b",
            r"\bdiscovery\b[^.]{0,120}\bstayed\b",
            r"\bdeposition\b",
            r"\bsanctions\b",
            r"\bprotective order\b",
            r"\bexpert discovery\b",
        ),
    ),
    (
        "motion_practice",
        74,
        (
            r"\btransfer\b",
            r"\btransferred\b",
            r"\bconsolidat(?:e|ed|ion)\b",
            r"\bsever(?:ed|ance)?\b",
            r"\brelated to\b",
            r"\bmultidistrict litigation\b",
            r"\bMDL\b",
        ),
    ),
    (
        "newly_filed",
        72,
        (
            r"\bamended complaint\b",
            r"\bclass action complaint\b",
            r"\bcomplaint (?:and demand for jury trial )?against\b",
            r"\bfiled (?:a )?complaint\b",
        ),
    ),
)

ROUTINE_EVENT_RULES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "routine_admin",
        25,
        (
            r"\bcivil cover sheet\b",
            r"\bproposed summons\b",
            r"\bsummons (?:issued|requested)\b",
            r"\bcertificate of interested entities\b",
            r"\bcorporate disclosure\b",
            r"\bAO[- ]?121\b",
            r"\bcopyright case-opening form\b",
            r"\bfiling fee\b",
            r"\bclerk'?s notice\b",
            r"\bcase assigned\b",
            r"\border reassigning case\b",
            r"\breassigned this case\b",
            r"\bstanding order\b",
            r"\bpro hac vice\b",
            r"\bnotice of appearance\b",
            r"\bADR certification\b",
            r"\badministrative motion to relate\b",
            r"\backnowledg(?:e)?ment of\b[^.]{0,80}\bhearing notice\b",
        ),
    ),
)

CLAIM_LABEL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("copyright infringement", ("copyright", "17:501")),
    ("patent infringement", ("patent", "35:")),
    ("trade secret", ("trade secret", "dtsa", "defend trade secrets")),
    ("right of publicity", ("right of publicity", "voice cloning", "deepfake")),
    ("DMCA section 1202", ("dmca", "1202")),
    ("trademark", ("trademark", "15:")),
    ("privacy or consumer protection", ("privacy", "consumer protection", "biometric")),
    ("administrative or FOIA", ("foia", "freedom of information", "administrative procedure")),
)

CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("administrative_or_foia", ("foia", "freedom of information", "administrative procedure")),
    (
        "securities_or_corporate_ai_disclosure",
        ("securities", "shareholder", "investor", "10-k", "proxy statement"),
    ),
    ("right_of_publicity", ("right of publicity", "voice cloning", "deepfake", "likeness")),
    (
        "privacy_or_consumer_protection",
        ("privacy", "consumer protection", "biometric", "wiretap", "personal data"),
    ),
    (
        "trade_secret_or_transparency",
        (
            "trade secret",
            "dtsa",
            "transparency law",
            "training-data transparency",
            "training data transparency",
        ),
    ),
    ("patent_ai_software", ("patent", "35:", "patented")),
    (
        "copyright_music_or_audio",
        ("music", "song", "songs", "sound recording", "record label", "musician", "audio", "lyric", "lyrics"),
    ),
    (
        "copyright_news_or_publishing",
        (
            "news",
            "journalism",
            "journalist",
            "newspaper",
            "publisher",
            "publishing",
            "book",
            "books",
            "authors",
            "textbook",
            "educational",
            "cnn",
            "elsevier",
            "britannica",
            "apress",
            "cognella",
        ),
    ),
    (
        "copyright_generated_output",
        ("generated output", "output claims", "ai-generated", "infringing output", "outputs"),
    ),
    (
        "copyright_training_data",
        ("training data", "trained on", "model training", "training corpus", "train ai", "ai training"),
    ),
    (
        "platform_scraping_or_access",
        ("scraping", "scraped", "crawler", "api access", "unauthorized access", "terms of service"),
    ),
)

TECHNOLOGY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ChatGPT", ("chatgpt",)),
    ("Claude", ("claude",)),
    ("Grok", ("grok",)),
    ("Gemini", ("gemini",)),
    ("Stable Diffusion", ("stable diffusion",)),
    ("Adobe Firefly", ("firefly", "adobe firefly")),
    ("Microsoft Copilot", ("copilot", "microsoft copilot")),
    ("Apple Intelligence", ("apple intelligence",)),
    ("Perplexity AI", ("perplexity ai",)),
    ("Project Giraffe", ("project giraffe",)),
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(clean_text(item) for item in value)
    elif isinstance(value, dict):
        value = " ".join(clean_text(item) for item in value.values())
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_sentence(sentence: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()


def normalize_for_match(value: Any) -> str:
    return clean_text(value).lower()


def sentence_split(value: Any) -> list[str]:
    text = clean_text(value)
    return [sentence.strip() for sentence in re.findall(r"[^.!?]+(?:[.!?]+|$)", text) if clean_text(sentence)]


def repeated_summary_text(value: Any) -> bool:
    sentences = sentence_split(value)
    if len(sentences) < 2:
        return False
    normalized = [normalize_sentence(sentence) for sentence in sentences]
    for left, right in zip(normalized, normalized[1:]):
        if left and left == right:
            return True
    for block_size in range(1, (len(normalized) // 2) + 1):
        if len(normalized) % block_size != 0:
            continue
        block = normalized[:block_size]
        if block and normalized == block * (len(normalized) // block_size):
            return True
    return False


def dedupe_repeated_summary_sentences(summary: Any) -> str:
    sentences = sentence_split(summary)
    if len(sentences) < 2:
        return clean_text(summary)
    deduped: list[str] = []
    previous = ""
    for sentence in sentences:
        normalized = normalize_sentence(sentence)
        if normalized and normalized != previous:
            deduped.append(sentence)
        previous = normalized
    return " ".join(deduped)


def listify(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif value:
        values = [value]
    else:
        values = []
    cleaned: list[str] = []
    for item in values:
        text = clean_text(item)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def english_join(items: list[str]) -> str:
    cleaned = [clean_text(item) for item in items if clean_text(item)]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"


def term_in_text(text: str, term: str) -> bool:
    normalized_term = normalize_for_match(term)
    if not normalized_term:
        return False
    if re.fullmatch(r"[a-z0-9 /:-]+", normalized_term):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])", text))
    return normalized_term in text


def match_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns)


def parse_iso_date(value: Any) -> date | None:
    text = clean_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
    return None


def event_sort_key(event: dict[str, Any]) -> tuple[date, int, int]:
    parsed = parse_iso_date(event.get("date")) or date.min
    entry_number = clean_text(event.get("entry_number"))
    number_match = re.search(r"\d+", entry_number)
    number = int(number_match.group(0)) if number_match else 0
    return parsed, int(event.get("score") or 0), number


def first_sentence(value: Any) -> str:
    sentences = sentence_split(value)
    return sentences[0] if sentences else clean_text(value)


def finish_sentence(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return text if text[-1] in ".!?" else f"{text}."


def public_summary_is_generic(value: Any) -> bool:
    text = normalize_for_match(value)
    if not text:
        return True
    if any(phrase in text for phrase in PUBLIC_SUMMARY_BANNED_PHRASES):
        return True
    return False


def legacy_public_summary_source_text(case: dict[str, Any]) -> str:
    if isinstance(case.get("case_intelligence"), dict):
        return ""
    summary = clean_text(case.get("plain_language_summary"))
    if not summary:
        return ""
    useful_sentences: list[str] = []
    for sentence in sentence_split(summary):
        lowered = sentence.lower()
        if any(phrase in lowered for phrase in PUBLIC_SUMMARY_BANNED_PHRASES):
            continue
        if clean_text(sentence):
            useful_sentences.append(sentence)
    text = " ".join(useful_sentences)
    return "" if public_summary_is_generic(text) else text


def existing_public_summary_can_be_source(case: dict[str, Any]) -> bool:
    return bool(legacy_public_summary_source_text(case))


def source_text_for_case(case: dict[str, Any], case_updates: list[dict[str, Any]] | None = None) -> str:
    parts: list[str] = []
    for key in ("name", "court", "court_full", "docket_number", "claims", "legal_theories", "status", "procedural_posture"):
        parts.append(clean_text(case.get(key)))
    existing_summary = legacy_public_summary_source_text(case)
    if existing_summary:
        parts.append(existing_summary)
    for ruling in case.get("key_rulings", []):
        if isinstance(ruling, dict):
            parts.append(clean_text(ruling.get("description")))
            parts.append(clean_text(ruling.get("summary")))
    for entry in case.get("docket_entries", []):
        if isinstance(entry, dict):
            parts.append(clean_text(entry.get("raw_text")))
            parts.append(clean_text(entry.get("summary")))
    for update in case_updates or []:
        if isinstance(update, dict):
            parts.append(clean_text(update.get("summary")))
    return " ".join(part for part in parts if part)


def substantive_source_text_for_case(
    case: dict[str, Any],
    updates: list[dict[str, Any]] | None = None,
    latest_event: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    for key in ("name", "court", "court_full", "docket_number", "claims", "legal_theories"):
        parts.append(clean_text(case.get(key)))
    existing_summary = legacy_public_summary_source_text(case)
    if existing_summary:
        parts.append(existing_summary)
    for ruling in case.get("key_rulings", []):
        if isinstance(ruling, dict):
            parts.append(clean_text(ruling.get("description")))
            parts.append(clean_text(ruling.get("summary")))
    for event in candidate_events(case, updates):
        if int(event.get("score") or 0) >= MEANINGFUL_EVENT_THRESHOLD:
            parts.append(clean_text(event.get("raw_text")))
            parts.append(clean_text(event.get("summary")))
    if latest_event:
        parts.append(clean_text(latest_event.get("raw_text")))
        parts.append(clean_text(latest_event.get("summary")))
    return " ".join(part for part in parts if part)


def normalize_claim_label(value: Any) -> str | None:
    text = normalize_for_match(value)
    if not text:
        return None
    for label, terms in CLAIM_LABEL_RULES:
        if any(term_in_text(text, term) for term in terms):
            return label
    if "ip" in text and "privacy" in text:
        return "privacy or consumer protection"
    if "ai" in text:
        return "other AI litigation"
    return clean_text(value)


def normalize_claims(value: Any) -> list[str]:
    labels: list[str] = []
    for item in listify(value):
        label = normalize_claim_label(item)
        if label and label not in labels:
            labels.append(label)
    return labels


def claim_text(claims: list[str]) -> str:
    return english_join(claims) or "legal"


def classify_claim_category(case: dict[str, Any], text: str | None = None) -> str:
    haystack = normalize_for_match(text if text is not None else source_text_for_case(case))
    claims = normalize_for_match(case.get("claims"))
    combined = f"{haystack} {claims}"
    for category, terms in CATEGORY_RULES:
        if any(term_in_text(combined, term) for term in terms):
            if category.startswith("copyright") and "copyright" not in combined:
                continue
            return category
    if "copyright" in combined:
        return "unknown"
    if "ai" in combined or "artificial intelligence" in combined or "machine learning" in combined:
        return "other_ai_litigation"
    return "unknown"


def extract_ai_conduct(text: str) -> str | None:
    haystack = normalize_for_match(text)
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "challenge to AI training-data disclosure obligations",
            ("transparency law", "training-data transparency", "training data transparency", "compelled disclosure"),
        ),
        (
            "use of materials as AI training data",
            ("training data", "trained on", "model training", "training corpus", "ai training"),
        ),
        (
            "AI-generated output",
            ("generated output", "output claims", "ai-generated", "infringing output", "outputs"),
        ),
        (
            "scraping or automated access to online content",
            ("scraping", "scraped", "crawler", "api access", "automated access"),
        ),
        (
            "voice cloning or deepfake technology",
            ("voice cloning", "deepfake", "digital replica"),
        ),
        (
            "AI or machine-learning software functionality",
            ("machine learning", "neural network", "artificial intelligence software", "ai software"),
        ),
        (
            "collection or use of personal data in AI systems",
            ("biometric", "personal data", "privacy", "consumer protection"),
        ),
    )
    for label, terms in rules:
        if any(term_in_text(haystack, term) for term in terms):
            return label
    return None


def extract_works_or_data(text: str) -> str | None:
    haystack = normalize_for_match(text)
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "music, audio, or sound-recording works",
            ("music", "song", "songs", "sound recording", "record label", "musician", "audio", "lyrics"),
        ),
        (
            "news or journalism content",
            ("news", "journalism", "journalist", "newspaper", "cnn", "article", "articles"),
        ),
        (
            "books, educational, or publishing materials",
            ("book", "books", "authors", "textbook", "educational", "publisher", "publishing", "apress", "elsevier", "britannica", "cognella"),
        ),
        (
            "visual works or image data",
            ("image", "images", "photograph", "visual art", "stable diffusion", "artist"),
        ),
        (
            "software or source code",
            ("source code", "api", "computer program"),
        ),
        (
            "patented software or technology",
            ("patent", "patented"),
        ),
        (
            "personal or biometric data",
            ("personal data", "biometric", "privacy"),
        ),
        (
            "confidential or trade-secret information",
            ("trade secret", "confidential information", "proprietary information"),
        ),
    )
    for label, terms in rules:
        if any(term_in_text(haystack, term) for term in terms):
            return label
    return None


def extract_technology_or_model(text: str) -> str | None:
    haystack = normalize_for_match(text)
    matches: list[str] = []
    for label, terms in TECHNOLOGY_RULES:
        if any(term_in_text(haystack, term) for term in terms) and label not in matches:
            matches.append(label)
    return english_join(matches[:3]) or None


def extract_related_cases(case: dict[str, Any], text: str) -> list[str]:
    own = normalize_for_match(case.get("docket_number"))
    found: list[str] = []
    for match in re.findall(r"\b\d{1,2}:\d{2}-(?:cv|md|ca|ap|cr)-\d{3,6}(?:-[A-Z0-9-]+)?\b", text, flags=re.IGNORECASE):
        normalized = clean_text(match)
        if normalize_for_match(normalized) != own and normalized not in found:
            found.append(normalized)
    for match in re.findall(r"\b\d{2}-(?:cv|md|ca|ap|cr)-\d{3,6}(?:-[A-Z0-9-]+)?\b", text, flags=re.IGNORECASE):
        normalized = clean_text(match)
        if normalize_for_match(normalized) not in own and normalized not in found:
            found.append(normalized)
    return found[:10]


def score_event_text(text: str, source: str = "docket_entry", significance: str | None = None) -> tuple[int, str]:
    haystack = clean_text(text)
    best_score = 0
    best_type = "routine_admin"
    for event_type, score, patterns in MEANINGFUL_EVENT_RULES:
        if match_any(haystack, patterns) and score > best_score:
            best_score = score
            best_type = event_type
    routine_score = 0
    for event_type, score, patterns in ROUTINE_EVENT_RULES:
        if match_any(haystack, patterns) and score > routine_score:
            routine_score = score
            if not best_score:
                best_type = event_type
    if routine_score and match_any(haystack, (r"\backnowledg(?:e)?ment of\b[^.]{0,80}\bhearing notice\b",)):
        return routine_score, "routine_admin"
    if routine_score and (not best_score or best_type == "significant_ruling"):
        return routine_score, "routine_admin"
    if clean_text(significance) == "case_resolved":
        if best_score and best_type in {"judgment", "settlement_or_voluntary_dismissal"}:
            return 100, best_type
        return 96, "resolved"
    if clean_text(significance) == "significant_ruling" or source == "key_ruling":
        if best_score:
            return 100, best_type
        return 100, "significant_ruling"

    if best_score:
        return best_score, best_type
    if routine_score:
        return routine_score, best_type
    return 45, "minor_update"


def case_updates_for(case: dict[str, Any], updates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    case_id = clean_text(case.get("id"))
    if not case_id or not isinstance(updates, list):
        return []
    return [update for update in updates if isinstance(update, dict) and clean_text(update.get("case_id")) == case_id]


def candidate_events(case: dict[str, Any], updates: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for ruling in case.get("key_rulings", []):
        if not isinstance(ruling, dict):
            continue
        summary = clean_text(ruling.get("summary")) or clean_text(ruling.get("description"))
        if not summary:
            continue
        score, event_type = score_event_text(summary, source="key_ruling", significance="significant_ruling")
        events.append(
            {
                "source": "key_ruling",
                "date": clean_text(ruling.get("date")),
                "summary": summary,
                "score": score,
                "event_type": event_type,
                "reference": {
                    "type": "key_ruling",
                    "date": clean_text(ruling.get("date")) or None,
                },
            }
        )

    for entry in case.get("docket_entries", []):
        if not isinstance(entry, dict):
            continue
        raw = clean_text(entry.get("raw_text"))
        summary = clean_text(entry.get("summary"))
        text = f"{raw} {summary}".strip()
        if not text:
            continue
        score, event_type = score_event_text(text, source="docket_entry", significance=clean_text(entry.get("significance")))
        events.append(
            {
                "source": "docket_entry",
                "date": clean_text(entry.get("date")),
                "entry_number": clean_text(entry.get("entry_number")),
                "summary": summary or first_sentence(raw),
                "raw_text": raw,
                "score": score,
                "event_type": event_type,
                "reference": {
                    "type": "docket_entry",
                    "entry_number": clean_text(entry.get("entry_number")) or None,
                    "date": clean_text(entry.get("date")) or None,
                },
            }
        )

    for update in case_updates_for(case, updates):
        summary = clean_text(update.get("summary"))
        if not summary:
            continue
        score, event_type = score_event_text(summary, source="update", significance=clean_text(update.get("significance")))
        events.append(
            {
                "source": "update",
                "date": clean_text(update.get("entry_date")),
                "entry_number": clean_text(update.get("entry_number")),
                "summary": summary,
                "score": score,
                "event_type": event_type,
                "reference": {
                    "type": "update",
                    "entry_number": clean_text(update.get("entry_number")) or None,
                    "date": clean_text(update.get("entry_date")) or None,
                },
            }
        )

    if not events and clean_text(case.get("date_filed")):
        plaintiff = party_name(case, "plaintiff") or "The plaintiff"
        defendant = party_name(case, "defendant") or "the defendant"
        events.append(
            {
                "source": "case_metadata",
                "date": clean_text(case.get("date_filed")),
                "summary": f"{plaintiff} filed the case against {defendant}.",
                "score": 65,
                "event_type": "newly_filed",
                "reference": {
                    "type": "case_metadata",
                    "date_filed": clean_text(case.get("date_filed")),
                },
            }
        )
    return events


def select_latest_meaningful_event(
    case: dict[str, Any], updates: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    events = candidate_events(case, updates)
    if not events:
        return None
    meaningful = [event for event in events if int(event.get("score") or 0) >= MEANINGFUL_EVENT_THRESHOLD]
    pool = meaningful or events
    return sorted(pool, key=event_sort_key, reverse=True)[0]


def has_text_event_type(case: dict[str, Any], event_type: str, updates: list[dict[str, Any]] | None = None) -> bool:
    return any(event.get("event_type") == event_type for event in candidate_events(case, updates))


def detect_procedural_stage(
    case: dict[str, Any],
    latest_event: dict[str, Any] | None = None,
    updates: list[dict[str, Any]] | None = None,
) -> str:
    status = normalize_for_match(case.get("status"))
    posture = normalize_for_match(case.get("procedural_posture"))
    text = normalize_for_match(source_text_for_case(case, case_updates_for(case, updates)))
    latest_type = clean_text((latest_event or {}).get("event_type"))

    if status == "stayed" or latest_type == "stayed" or "stayed" in posture:
        return "stayed"
    if status == "resolved":
        if latest_type == "settlement_or_voluntary_dismissal" or match_any(text, (r"\bsettlement\b", r"\bvoluntary dismissal\b", r"\bdismissed\b")):
            return "settlement_or_voluntary_dismissal"
        if latest_type == "judgment" or "judgment" in posture:
            return "judgment"
        return "resolved"
    if "appeal" in posture or latest_type == "appeal" or match_any(text, (r"\bnotice of appeal\b", r"\bappellant\b", r"\bappellee\b")):
        return "appeal"
    if latest_type == "motion_to_dismiss" or "motion to dismiss" in text:
        return "motion_to_dismiss"
    if "discovery" in posture or latest_type == "discovery":
        return "discovery"
    if latest_type == "motion_practice" or "motion" in posture or "summary judgment" in posture:
        return "motion_practice"
    if latest_type == "significant_ruling" or case.get("key_rulings"):
        return "significant_ruling"
    if latest_type == "newly_filed":
        return "newly_filed"
    if match_any(text, ROUTINE_EVENT_RULES[0][2]):
        return "service_or_initial_admin"
    return "unknown"


def party_name(case: dict[str, Any], role: str) -> str:
    parties = case.get("parties")
    if isinstance(parties, dict):
        value = clean_text(parties.get(role))
        if value:
            return value
    name = clean_text(case.get("name"))
    match = re.split(r"\s+v\.?\s+", name, maxsplit=1, flags=re.IGNORECASE)
    if len(match) == 2:
        return clean_text(match[0] if role == "plaintiff" else match[1])
    return ""


def build_case_theory(
    case: dict[str, Any],
    claims: list[str],
    ai_conduct: str | None,
    works_or_data: str | None,
    technology_or_model: str | None,
) -> str:
    plaintiff = party_name(case, "plaintiff") or "The plaintiff"
    defendant = party_name(case, "defendant") or "the defendant"
    claims_text = claim_text(claims)
    if ai_conduct == "challenge to AI training-data disclosure obligations":
        detail = f" involving {works_or_data}" if works_or_data else ""
        return (
            f"{plaintiff} challenges AI training-data disclosure obligations in litigation against "
            f"{defendant} that includes {claims_text} issues{detail}."
        )
    if ai_conduct:
        detail_parts = [part for part in (works_or_data, technology_or_model) if part]
        detail = f" involving {english_join(detail_parts)}" if detail_parts else ""
        return f"{plaintiff} asserts {claims_text} claims against {defendant} over alleged {ai_conduct}{detail}."
    if works_or_data or technology_or_model:
        detail = english_join([part for part in (works_or_data, technology_or_model) if part])
        return f"{plaintiff} asserts {claims_text} claims against {defendant} involving {detail}."
    return (
        f"{plaintiff} asserts {claims_text} claims against {defendant}, but the parsed materials do not yet "
        "identify the specific AI conduct at issue."
    )


def build_current_posture(stage: str, latest_event: dict[str, Any] | None, case: dict[str, Any]) -> str:
    event_summary = clean_text((latest_event or {}).get("summary"))
    posture = clean_text(case.get("procedural_posture"))
    if stage == "stayed":
        if event_summary and "stay" in event_summary.lower():
            return f"The case is stayed: {event_summary}"
        return "The case is stayed."
    if stage == "appeal":
        return "The case is on appeal."
    if stage == "motion_to_dismiss":
        return "The case is in motion-to-dismiss practice."
    if stage == "motion_practice":
        return "The case is in motion practice."
    if stage == "discovery":
        return "The case is in discovery."
    if stage in {"settlement_or_voluntary_dismissal", "judgment", "resolved"}:
        return "The case is resolved." if not event_summary else f"The case is resolved: {event_summary}"
    if stage == "significant_ruling":
        return "The case has a recent significant ruling."
    if stage == "service_or_initial_admin":
        return "The case is in early service or case-administration steps."
    if stage == "newly_filed":
        return "The case is newly filed."
    return posture or "The current procedural posture is not clear from the parsed materials."


def build_why_it_matters(category: str, stage: str, claims: list[str], related_cases: list[str]) -> str:
    if stage == "stayed":
        if related_cases:
            return "The case matters because it may affect how related AI litigation is coordinated before merits rulings."
        return "The case matters because the stay controls when the parties can reach merits discovery or dispositive motions."
    if stage == "appeal":
        return "The case matters because the appellate posture may shape what district-court orders remain in effect while AI litigation proceeds."
    if category == "unknown":
        claims_text = claim_text(claims)
        return (
            "The case matters because future filings may clarify the specific AI-related conduct and legal theory "
            f"in a case asserting {claims_text} claims."
        )

    category_text = {
        "copyright_training_data": "how copyright law applies to alleged use of protected works as AI training data",
        "copyright_generated_output": "how courts treat claims that AI-generated outputs infringe protected works",
        "copyright_music_or_audio": "how copyright and music-rights claims are applied to AI tools and audio works",
        "copyright_news_or_publishing": "how copyright claims by publishers, authors, or media owners are managed in AI litigation",
        "patent_ai_software": "how patent infringement theories are applied to AI or machine-learning software",
        "trade_secret_or_transparency": "how AI transparency obligations interact with trade-secret or confidential-information claims",
        "right_of_publicity": "how right-of-publicity law applies to alleged AI replicas of identity, likeness, or voice",
        "privacy_or_consumer_protection": "how privacy or consumer-protection law applies to AI products and data practices",
        "platform_scraping_or_access": "how platform-access and scraping theories are used in disputes over AI-related data collection",
        "securities_or_corporate_ai_disclosure": "how AI-related disclosures are tested in corporate or securities litigation",
        "administrative_or_foia": "how administrative-law or public-records rules apply to AI-related information requests",
        "other_ai_litigation": "how courts characterize AI-related legal theories outside the core IP categories",
        "unknown": "the specific AI-related conduct and legal theory",
    }.get(category, "how courts characterize the AI-related claims")
    claims_text = claim_text(claims)
    return f"The case matters because it may affect {category_text} in a case asserting {claims_text} claims."


def build_latest_change(latest_event: dict[str, Any] | None) -> str | None:
    if not latest_event:
        return None
    summary = clean_text(latest_event.get("summary"))
    if not summary:
        return None
    event_date = clean_text(latest_event.get("date"))
    if event_date:
        return f"Latest meaningful event ({event_date}): {summary}"
    return f"Latest meaningful event: {summary}"


def build_pending_motion_or_next_event(stage: str, latest_event: dict[str, Any] | None, source_text: str) -> str | None:
    text = normalize_for_match(clean_text((latest_event or {}).get("summary")))
    broader_text = normalize_for_match(source_text)
    if stage == "motion_to_dismiss":
        if "intends to file a motion to dismiss" in f"{text} {broader_text}":
            return "anticipated motion to dismiss"
        return "motion to dismiss"
    if "summary judgment motions" in f"{text} {broader_text}":
        return "summary judgment motions"
    if "case management conference" in text:
        match = re.search(r"case management conference[^.]*", clean_text((latest_event or {}).get("summary")), re.IGNORECASE)
        return clean_text(match.group(0)) if match else "case management conference"
    if "briefing schedule" in text:
        return "briefing schedule"
    return None


def confidence_level(
    claims: list[str],
    ai_conduct: str | None,
    works_or_data: str | None,
    technology_or_model: str | None,
    latest_event: dict[str, Any] | None,
) -> str:
    if claims and ai_conduct and (works_or_data or technology_or_model) and latest_event:
        return "high"
    if claims and latest_event and (ai_conduct or technology_or_model):
        return "medium"
    return "low"


def missing_information(
    claims: list[str],
    ai_conduct: str | None,
    works_or_data: str | None,
    technology_or_model: str | None,
    latest_event: dict[str, Any] | None,
) -> list[str]:
    missing: list[str] = []
    if not claims:
        missing.append("Specific claims are not identified in the parsed materials.")
    if not ai_conduct:
        missing.append("Specific AI-related conduct is not identified in the parsed docket metadata.")
    if not works_or_data:
        missing.append("Specific works, data, or trade secrets at issue are not identified in the parsed materials.")
    if not technology_or_model:
        missing.append("Specific AI system or model is not identified in the parsed materials.")
    if not latest_event:
        missing.append("No docket entries, key rulings, or update records are available yet.")
    return missing


def source_references(case: dict[str, Any], latest_event: dict[str, Any] | None, existing_summary_used: bool) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = [
        {
            "type": "case_metadata",
            "docket_number": clean_text(case.get("docket_number")) or None,
            "court": clean_text(case.get("court")) or None,
            "courtlistener_url": clean_text(case.get("courtlistener_url")) or None,
        }
    ]
    if latest_event and isinstance(latest_event.get("reference"), dict):
        refs.append(latest_event["reference"])
    if existing_summary_used:
        refs.append({"type": "existing_public_summary"})
    return refs


def build_case_intelligence(case: dict[str, Any], updates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    relevant_updates = case_updates_for(case, updates)
    full_text = source_text_for_case(case, relevant_updates)
    existing_summary_used = existing_public_summary_can_be_source(case)
    claims = normalize_claims(case.get("claims"))
    latest_event = select_latest_meaningful_event(case, updates)
    substantive_text = substantive_source_text_for_case(case, updates, latest_event)
    category = classify_claim_category(case, substantive_text)
    ai_conduct = extract_ai_conduct(substantive_text)
    works_or_data = extract_works_or_data(substantive_text)
    technology_or_model = extract_technology_or_model(substantive_text)
    if category != "trade_secret_or_transparency" and works_or_data == "confidential or trade-secret information":
        works_or_data = None
    related = extract_related_cases(case, substantive_text)
    stage = detect_procedural_stage(case, latest_event, updates)
    theory = build_case_theory(case, claims, ai_conduct, works_or_data, technology_or_model)
    posture = build_current_posture(stage, latest_event, case)
    latest_change = build_latest_change(latest_event)
    why = build_why_it_matters(category, stage, claims, related)
    confidence = confidence_level(claims, ai_conduct, works_or_data, technology_or_model, latest_event)
    missing = missing_information(claims, ai_conduct, works_or_data, technology_or_model, latest_event)

    intelligence = {
        "case_theory": theory,
        "current_posture": posture,
        "why_it_matters": why,
        "latest_change": latest_change,
        "latest_meaningful_event": clean_text((latest_event or {}).get("summary")) or None,
        "latest_meaningful_event_date": clean_text((latest_event or {}).get("date")) or None,
        "claim_category": category,
        "claims_asserted": claims,
        "ai_conduct_alleged": ai_conduct,
        "works_or_data_at_issue": works_or_data,
        "technology_or_model_at_issue": technology_or_model,
        "procedural_stage": stage,
        "pending_motion_or_next_event": build_pending_motion_or_next_event(stage, latest_event, full_text),
        "related_cases": related,
        "confidence_level": confidence,
        "missing_information": missing,
        "source_references": source_references(case, latest_event, existing_summary_used),
    }
    return {field: intelligence.get(field) for field in CASE_INTELLIGENCE_FIELDS}


def top_level_posture_for_stage(stage: str, existing: Any = None) -> str:
    existing_text = clean_text(existing)
    mapping = {
        "newly_filed": "Filed",
        "service_or_initial_admin": "Filed",
        "motion_practice": "Motion Practice",
        "motion_to_dismiss": "Motion Practice",
        "discovery": "Discovery",
        "stayed": "Stayed",
        "appeal": "Appeal",
        "significant_ruling": existing_text or "Motion Practice",
        "settlement_or_voluntary_dismissal": "Settled",
        "judgment": "Judgment",
        "resolved": existing_text or "Judgment",
        "unknown": existing_text or "Filed",
    }
    return mapping.get(stage, existing_text or "Filed")


def generate_case_summary(case: dict[str, Any], intelligence: dict[str, Any] | None = None) -> str:
    intel = intelligence if isinstance(intelligence, dict) else build_case_intelligence(case)
    theory = clean_text(intel.get("case_theory"))
    posture = clean_text(intel.get("current_posture"))
    why = clean_text(intel.get("why_it_matters"))
    latest = clean_text(intel.get("latest_change"))
    confidence = clean_text(intel.get("confidence_level")).lower()
    stage = clean_text(intel.get("procedural_stage"))

    if confidence == "low" and stage in {"newly_filed", "service_or_initial_admin", "unknown"}:
        plaintiff = party_name(case, "plaintiff") or "The plaintiff"
        defendant = party_name(case, "defendant") or "the defendant"
        claims = normalize_claims(case.get("claims"))
        intro = finish_sentence(f"{plaintiff} filed {claim_text(claims)} claims against {defendant}")
        status = "Newly filed case." if stage == "newly_filed" else "Early-stage case."
        return f"{intro} {status} {TRANSPARENT_FALLBACK_SENTENCE}"

    sentences = [theory, posture, why, latest]
    summary = " ".join(sentence for sentence in sentences if sentence)
    summary = re.sub(r"\s+", " ", summary).strip()
    if not summary or public_summary_is_generic(summary):
        plaintiff = party_name(case, "plaintiff") or "The plaintiff"
        defendant = party_name(case, "defendant") or "the defendant"
        claims = normalize_claims(case.get("claims"))
        summary = f"{finish_sentence(f'{plaintiff} filed {claim_text(claims)} claims against {defendant}')} {TRANSPARENT_FALLBACK_SENTENCE}"
    return summary


def refresh_case_intelligence(case: dict[str, Any], updates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    normalized_claims = normalize_claims(case.get("claims"))
    if normalized_claims:
        case["claims"] = normalized_claims
    intelligence = build_case_intelligence(case, updates)
    case["case_intelligence"] = intelligence
    case["plain_language_summary"] = generate_case_summary(case, intelligence)

    stage = clean_text(intelligence.get("procedural_stage"))
    if stage in PROCEDURAL_STAGES:
        case["procedural_posture"] = top_level_posture_for_stage(stage, case.get("procedural_posture"))
        if stage == "stayed":
            case["status"] = "stayed"
        elif stage in {"settlement_or_voluntary_dismissal", "judgment", "resolved"}:
            case["status"] = "resolved"
        elif clean_text(case.get("status")) == "stayed" and stage != "stayed":
            case["status"] = "active"
    return case


def refresh_all_case_intelligence(cases: list[dict[str, Any]], updates: list[dict[str, Any]] | None = None) -> None:
    for case in cases:
        if isinstance(case, dict):
            refresh_case_intelligence(case, updates)


def summary_case_specific_terms(case: dict[str, Any], intelligence: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for value in (
        case.get("name"),
        case.get("docket_number"),
        party_name(case, "plaintiff"),
        party_name(case, "defendant"),
        intelligence.get("ai_conduct_alleged"),
        intelligence.get("works_or_data_at_issue"),
        intelligence.get("technology_or_model_at_issue"),
        intelligence.get("latest_meaningful_event_date"),
    ):
        text = clean_text(value)
        if text:
            terms.append(text)
    terms.extend(normalize_claims(case.get("claims")))
    return terms


def summary_has_case_specific_fact(case: dict[str, Any], intelligence: dict[str, Any]) -> bool:
    summary = normalize_for_match(case.get("plain_language_summary"))
    for term in summary_case_specific_terms(case, intelligence):
        normalized = normalize_for_match(term)
        if normalized and normalized in summary:
            return True
        words = [word for word in re.split(r"[^a-z0-9]+", normalized) if len(word) >= 4]
        if words and any(word in summary for word in words[:3]):
            return True
    return False


def validate_case_summary(case: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    summary = clean_text(case.get("plain_language_summary"))
    lowered = summary.lower()
    label = clean_text(case.get("name")) or clean_text(case.get("id")) or "case"
    if not summary:
        errors.append(f"{label}: missing plain_language_summary.")
        return errors
    for phrase in PUBLIC_SUMMARY_BANNED_PHRASES:
        if phrase in lowered:
            errors.append(f"{label}: plain_language_summary contains banned boilerplate phrase {phrase!r}.")
    for claim in listify(case.get("claims")):
        claim_text_value = normalize_for_match(claim)
        if any(phrase in claim_text_value for phrase in ("unspecified ip or privacy claims", "claims against ai developer")):
            errors.append(f"{label}: claims contain malformed classifier prose {clean_text(claim)!r}.")
    if repeated_summary_text(summary):
        errors.append(f"{label}: plain_language_summary repeats the same sentence block.")
    if re.search(r"\b[a-z]+(?: [a-z]+){2,} claims against ai developer\b", lowered):
        errors.append(f"{label}: plain_language_summary exposes malformed lowercase classifier prose.")

    intelligence = case.get("case_intelligence")
    if not isinstance(intelligence, dict):
        errors.append(f"{label}: missing case_intelligence.")
        return errors
    missing_fields = [field for field in CASE_INTELLIGENCE_FIELDS if field not in intelligence]
    if missing_fields:
        errors.append(f"{label}: case_intelligence missing fields: {', '.join(missing_fields)}.")
    category = clean_text(intelligence.get("claim_category"))
    if category not in CLAIM_CATEGORIES:
        errors.append(f"{label}: unsupported claim_category {category!r}.")
    stage = clean_text(intelligence.get("procedural_stage"))
    if stage not in PROCEDURAL_STAGES:
        errors.append(f"{label}: unsupported procedural_stage {stage!r}.")
    confidence = clean_text(intelligence.get("confidence_level")).lower()
    if confidence not in {"high", "medium", "low"}:
        errors.append(f"{label}: unsupported confidence_level {confidence!r}.")
    if confidence == "low" and not listify(intelligence.get("missing_information")):
        errors.append(f"{label}: low-confidence case_intelligence must explain missing_information.")
    if (clean_text(case.get("status")) == "stayed" or stage == "stayed") and "stay" not in f"{summary} {clean_text(intelligence.get('current_posture'))}".lower():
        errors.append(f"{label}: stayed case must mention the stay in current_posture or plain_language_summary.")
    if not summary_has_case_specific_fact(case, intelligence):
        errors.append(f"{label}: plain_language_summary lacks a case-specific fact.")
    return errors


def summary_boilerplate_fingerprint(case: dict[str, Any]) -> str:
    text = normalize_for_match(case.get("plain_language_summary"))
    replacements = [case.get("name"), party_name(case, "plaintiff"), party_name(case, "defendant")]
    replacements.extend(normalize_claims(case.get("claims")))
    for value in replacements:
        normalized = normalize_for_match(value)
        if normalized:
            text = text.replace(normalized, "<case-fact>")
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<date>", text)
    text = re.sub(r"\b\d{1,2}:\d{2}-(?:cv|md|ca|ap|cr)-\d{3,6}\b", "<docket>", text)
    return re.sub(r"\s+", " ", text).strip()
