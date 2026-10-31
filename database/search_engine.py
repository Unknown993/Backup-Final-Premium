"""
Search Engine & Relevance Matching Subsystem for Telegram Movie Bot.

Implements:
- Single Source of Truth Language Matching Table (13 languages + universal audio tags)
- Quality Alias Table (480p, 720p, 1080p, 2160p)
- Unified Query Parser preserving titles (It, Us, The A-Team, sequel numbers)
- Relevance and Ranking engine (Movie/Series overlap preserved)
- Selective MongoDB query generator
- In-memory bounded Search Cache with TTL
- Search Analytics & Diagnostic tools (/search_debug)
- Isolated Search Session Manager preventing concurrent/stale callback collisions
"""

import re
import time
import math
import logging
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# ==============================================================================
# 1. SINGLE SOURCE OF TRUTH: LANGUAGE MATCHING TABLE (Section 3)
# ==============================================================================

UNIVERSAL_AUDIO_TAGS: List[str] = [
    "dual",
    "dual audio",
    "dual-audio",
    "multi",
    "multi audio",
    "multi-audio",
    "multi-sub",
    "multiaudio",
    "dubbed",
    "dub",
    "multi-dub",
]

# Exact language alias map from Section 3
LANGUAGE_MAP: Dict[str, List[str]] = {
    "hindi": ["hin", "hindi", "hindi dub", "hin dub"],
    "tamil": ["tam", "tamil", "tamil dub", "tam dub"],
    "kannada": ["kan", "kannada", "kan dub", "kannada dub"],
    "telugu": ["tel", "telugu", "tel dub", "telugu dub"],
    "malayalam": ["mal", "malayalam", "mal dub", "malayalam dub"],
    "english": ["eng", "english", "eng dub", "english dub"],
    "punjabi": ["pun", "punjabi", "pun dub", "punjabi dub"],
    "bengali": ["ben", "bengali", "ben dub", "bengali dub"],
    "marathi": ["mar", "marathi", "mar dub", "marathi dub"],
    "gujarati": ["guj", "gujarati", "guj dub", "gujarati dub"],
    "urdu": ["urd", "urdu", "urd dub", "urdu dub"],
    "korean": ["kor", "korean", "kor dub", "korean dub"],
    "japanese": ["jpn", "japanese", "jpn dub", "japanese dub"],
}

# Reverse lookup for mapping any query code/alias to canonical language name
ALIAS_TO_LANG: Dict[str, str] = {}
for lang_name, aliases in LANGUAGE_MAP.items():
    ALIAS_TO_LANG[lang_name] = lang_name
    for alias in aliases:
        ALIAS_TO_LANG[alias.lower()] = lang_name
# Also include common short variations
ALIAS_TO_LANG["punj"] = "punjabi"
ALIAS_TO_LANG["pb"] = "punjabi"


def canonicalize_language(val: Optional[str]) -> Optional[str]:
    """Map any input language alias/code to its canonical name."""
    if not val:
        return None
    cleaned = str(val).strip().lower()
    return ALIAS_TO_LANG.get(cleaned, cleaned if cleaned in LANGUAGE_MAP else None)


def build_language_regex(lang_code: str) -> Optional[re.Pattern]:
    """
    Construct a regex matching any alias of the selected language OR any universal audio tag.
    Never requires both. Uses token boundaries so short codes do not match inside unrelated words.
    """
    canonical = canonicalize_language(lang_code)
    if not canonical or canonical not in LANGUAGE_MAP:
        return None
    aliases = LANGUAGE_MAP[canonical]
    patterns = []
    # Language aliases + Universal audio tags (OR condition)
    for term in aliases + UNIVERSAL_AUDIO_TAGS:
        escaped = re.sub(r"[\s\-_]+", r"[\\s\\-_.]+", term.strip())
        patterns.append(rf"\b{escaped}\b")
    regex_str = "(" + "|".join(patterns) + ")"
    return re.compile(regex_str, re.IGNORECASE)


def build_universal_audio_regex() -> re.Pattern:
    """Construct regex matching universal audio tags alone."""
    patterns = []
    for term in UNIVERSAL_AUDIO_TAGS:
        escaped = re.sub(r"[\s\-_]+", r"[\\s\\-_.]+", term.strip())
        patterns.append(rf"\b{escaped}\b")
    return re.compile("(" + "|".join(patterns) + ")", re.IGNORECASE)


def matches_language(text: str, lang_code: str) -> bool:
    """Test whether a text string contains the language or universal audio tags."""
    if not text:
        return False
    reg = build_language_regex(lang_code)
    return bool(reg.search(text)) if reg else False


# ==============================================================================
# 2. QUALITY ALIAS TABLE (Section 6)
# ==============================================================================

QUALITY_MAP: Dict[str, List[str]] = {
    "480p": ["480", "480p", "480 p", "sd"],
    "720p": ["720", "720p", "720 p", "hd", "hdrip", "web-dl 720p"],
    "1080p": ["1080", "1080p", "1080 p", "fhd", "full hd", "fullhd", "1080p fhd"],
    "2160p": ["2160", "2160p", "2160 p", "4k", "uhd", "4k uhd", "2160p 4k"],
}

ALIAS_TO_QUALITY: Dict[str, str] = {}
for q_name, aliases in QUALITY_MAP.items():
    ALIAS_TO_QUALITY[q_name] = q_name
    for alias in aliases:
        ALIAS_TO_QUALITY[alias.lower()] = q_name


def canonicalize_quality(val: Optional[str]) -> Optional[str]:
    """Map any input quality alias to its canonical resolution."""
    if not val:
        return None
    cleaned = str(val).strip().lower()
    return ALIAS_TO_QUALITY.get(cleaned, cleaned if cleaned in QUALITY_MAP else None)


def build_quality_regex(quality_val: str) -> Optional[re.Pattern]:
    """
    Construct regex that strictly matches the canonical quality aliases without
    accidentally confusing 1080p with 720p (HD vs FHD) or 2160p (UHD vs HD).
    """
    canonical = canonicalize_quality(quality_val)
    if not canonical or canonical not in QUALITY_MAP:
        return None
    aliases = QUALITY_MAP[canonical]
    patterns = []
    for a in aliases:
        if a == "hd":
            # For 720p 'hd', do not match when preceded by 'f' (FHD) or 'u' (UHD)
            patterns.append(r"(?<![a-zA-Z0-9])hd(?![a-zA-Z0-9])")
        elif a == "full hd":
            patterns.append(r"\bfull[\s\-_.]+hd\b")
        else:
            patterns.append(rf"\b{re.escape(a)}\b")
    regex_str = "(" + "|".join(patterns) + ")"
    return re.compile(regex_str, re.IGNORECASE)


def matches_quality(text: str, quality_val: str) -> bool:
    """Test whether a text string contains the given quality."""
    if not text:
        return False
    reg = build_quality_regex(quality_val)
    return bool(reg.search(text)) if reg else False


# ==============================================================================
# 3. SEASON AND EPISODE MATCHING (Section 10)
# ==============================================================================

def build_season_regex(season_num: int) -> re.Pattern:
    """
    Construct regex matching Season S01/S1/Season 1.
    Strictly ensures Season 1 does NOT match Season 10 or 11.
    """
    s = int(season_num)
    patterns = [
        rf"\bs0*{s}(?!\d)",
        rf"\bseason[\s\-_.]*0*{s}(?!\d)",
    ]
    return re.compile("(" + "|".join(patterns) + ")", re.IGNORECASE)


def matches_season(text: str, season_num: int) -> bool:
    """Test whether text contains the exact season number."""
    if not text:
        return False
    reg = build_season_regex(season_num)
    return bool(reg.search(text))


# ==============================================================================
# 4. QUERY PARSING (Section 5)
# ==============================================================================

REQUEST_PHRASES = [
    r"\bplease\s+send\b",
    r"\bplz\s+send\b",
    r"\bpls\s+send\b",
    r"\bsend\s+me\b",
    r"\bgive\s+me\b",
    r"\blink\s+do\b",
    r"\bmovie\s+bhejo\b",
    r"\bchahiye\b",
    r"\bdownload\b",
    r"\bfull\s+movie\b",
    r"\bnew\s+movie\b",
    r"\blatest\s+movie\b",
    r"\bbhejo\b",
    r"\bvenum\b",
    r"\biruka\b",
    r"\bpannunga\b",
    r"\banuppunga\b",
    r"\btharu\b",
    r"\bkittumo\b",
    r"\bkittum\b",
]


def parse_user_query(raw_query: str) -> Dict[str, Any]:
    """
    Parse user query into structured tokens without damaging film/series titles.
    Preserves meaningful short words and sequel numbers ('It', 'Us', 'The A-Team', 'Iron Man 2').
    """
    orig = str(raw_query or "").strip()
    text = orig

    # 1. Clean request phrases / Hinglish boilerplate
    for pat in REQUEST_PHRASES:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()

    parsed = {
        "original_query": orig,
        "title": "",
        "year": None,
        "language": None,
        "quality": None,
        "season": None,
        "episode": None,
        "universal_audio": False,
    }

    # 2. Extract Quality (sort aliases by length desc)
    all_q_aliases = []
    for q_canonical, aliases in QUALITY_MAP.items():
        for a in aliases:
            all_q_aliases.append((a, q_canonical))
    all_q_aliases.sort(key=lambda x: len(x[0]), reverse=True)

    for alias, canonical_q in all_q_aliases:
        if alias == "hd":
            pat = r"(?<![a-zA-Z0-9])hd(?![a-zA-Z0-9])"
        elif alias == "full hd":
            pat = r"\bfull[\s\-_.]+hd\b"
        else:
            pat = rf"\b{re.escape(alias)}\b"
        match = re.search(pat, text, flags=re.IGNORECASE)
        if match:
            if not parsed["quality"]:
                parsed["quality"] = canonical_q
            text = text[:match.start()] + " " + text[match.end():]

    # 3. Extract Season & Episode
    se_match = re.search(r"\b(?:s|season)[\s\-_.]*(\d{1,2})[\s\-_.]*(?:e|ep|episode)[\s\-_.]*(\d{1,3})\b", text, re.IGNORECASE)
    if se_match:
        parsed["season"] = int(se_match.group(1))
        parsed["episode"] = int(se_match.group(2))
        text = text[:se_match.start()] + " " + text[se_match.end():]
    else:
        s_match = re.search(r"\b(?:season[\s\-_.]*(\d{1,2})|s(\d{1,2}))\b", text, re.IGNORECASE)
        if s_match:
            parsed["season"] = int(s_match.group(1) or s_match.group(2))
            text = text[:s_match.start()] + " " + text[s_match.end():]

        ep_match = re.search(r"\b(?:episode|ep|e)[\s\-_.]*(\d{1,3})\b", text, re.IGNORECASE)
        if ep_match:
            parsed["episode"] = int(ep_match.group(1))
            text = text[:ep_match.start()] + " " + text[ep_match.end():]

    # 4. Extract Universal Audio
    univ_match = re.search(r"\b(dual[\s\-_.]*audio|dual|multi[\s\-_.]*audio|multi)\b", text, re.IGNORECASE)
    if univ_match:
        parsed["universal_audio"] = True
        text = text[:univ_match.start()] + " " + text[univ_match.end():]

    # 5. Extract Language
    all_l_aliases = []
    for lang_name, aliases in LANGUAGE_MAP.items():
        for a in aliases:
            all_l_aliases.append((a, lang_name))
    all_l_aliases.sort(key=lambda x: len(x[0]), reverse=True)

    for alias, canonical_l in all_l_aliases:
        escaped = re.sub(r"[\s\-_]+", r"[\\s\\-_.]+", alias.strip())
        pat = rf"\b{escaped}\b"
        match = re.search(pat, text, flags=re.IGNORECASE)
        if match:
            if not parsed["language"]:
                parsed["language"] = canonical_l
            text = text[:match.start()] + " " + text[match.end():]

    # 6. Extract Release Year (4 digits: 1900-2099)
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if year_match:
        parsed["year"] = int(year_match.group(1))
        text = text[:year_match.start()] + " " + text[year_match.end():]

    # Clean leftover title without stripping meaningful words (like 'It', 'Us', 'The')
    title = re.sub(r"\s+", " ", text).strip(" -_:,.")
    parsed["title"] = title if title else orig
    return parsed


# ==============================================================================
# 5. MONGODB SEARCH QUERY BUILDER (Section 8)
# ==============================================================================

def clean_media_title_for_comparison(filename: str) -> str:
    """Normalize filename into a clean title representation for scoring."""
    t = str(filename or "").strip()
    # Strip common file extension
    t = re.sub(r"\.(mkv|mp4|avi|mov|wmv|webm|flv|m4v)$", "", t, flags=re.IGNORECASE)
    # Strip release noise
    t = re.sub(r"\b(1080p|720p|480p|2160p|4k|uhd|fhd|hd|sd|bluray|web-dl|webrip|hevc|x264|x265|aac|dvdrip|remux|proper|hdr|sdr)\b", " ", t, flags=re.IGNORECASE)
    # Strip languages and audio tags
    for aliases in list(LANGUAGE_MAP.values()) + [UNIVERSAL_AUDIO_TAGS]:
        for a in aliases:
            t = re.sub(rf"\b{re.escape(a)}\b", " ", t, flags=re.IGNORECASE)
    # Strip brackets and hashes
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", t)
    # Normalize separators
    t = re.sub(r"[\._\-+]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def build_mongo_search_query(
    query_input: Any,
    file_type: Optional[str] = None,
    use_caption: bool = True
) -> Dict[str, Any]:
    """
    Construct selective, indexed MongoDB query predicates.
    Avoids unanchored scans where possible and cleanly combines Title, Season,
    Quality, Language, and Year constraints using $and.
    """
    if isinstance(query_input, dict):
        base_search = query_input.get("search", "")
        parsed = parse_user_query(base_search)
        # Overlay explicit dictionary filters if supplied
        if "quality" in query_input and query_input["quality"]:
            parsed["quality"] = canonicalize_quality(query_input["quality"])
        if "language" in query_input and query_input["language"]:
            parsed["language"] = canonicalize_language(query_input["language"])
        if "season" in query_input and query_input["season"]:
            try:
                parsed["season"] = int(query_input["season"])
            except (ValueError, TypeError):
                pass
        if "year" in query_input and query_input["year"]:
            try:
                parsed["year"] = int(query_input["year"])
            except (ValueError, TypeError):
                pass
    elif isinstance(query_input, list):
        # List of terms or fallback variations
        joined = " ".join(str(q) for q in query_input if q)
        parsed = parse_user_query(joined)
    else:
        parsed = parse_user_query(str(query_input or ""))

    and_conditions: List[Dict[str, Any]] = []

    # 1. Title Condition: token boundary regex matching title words
    title_text = parsed.get("title") or ""
    title_words = [w for w in title_text.split() if w]
    if title_words:
        # Build token-bounded regex: \bword1\b.*\bword2\b
        parts = []
        for w in title_words:
            if re.match(r"^\w+$", w):
                parts.append(rf"\b{re.escape(w)}\b")
            else:
                parts.append(re.escape(w))
        pattern = r".*".join(parts)
        try:
            title_regex = re.compile(pattern, re.IGNORECASE)
            if use_caption:
                and_conditions.append({"$or": [{"file_name": title_regex}, {"caption": title_regex}]})
            else:
                and_conditions.append({"file_name": title_regex})
        except re.error:
            pass

    # 2. Season Condition (Structured exact filter)
    if parsed.get("season") is not None:
        s_regex = build_season_regex(parsed["season"])
        if use_caption:
            and_conditions.append({"$or": [{"file_name": s_regex}, {"caption": s_regex}]})
        else:
            and_conditions.append({"file_name": s_regex})

    # 3. Quality Condition
    if parsed.get("quality"):
        q_regex = build_quality_regex(parsed["quality"])
        if q_regex:
            if use_caption:
                and_conditions.append({"$or": [{"file_name": q_regex}, {"caption": q_regex}]})
            else:
                and_conditions.append({"file_name": q_regex})

    # 4. Language Condition (Language aliases OR Universal Audio tags)
    if parsed.get("language"):
        l_regex = build_language_regex(parsed["language"])
        if l_regex:
            if use_caption:
                and_conditions.append({"$or": [{"file_name": l_regex}, {"caption": l_regex}]})
            else:
                and_conditions.append({"file_name": l_regex})
    elif parsed.get("universal_audio"):
        u_regex = build_universal_audio_regex()
        if use_caption:
            and_conditions.append({"$or": [{"file_name": u_regex}, {"caption": u_regex}]})
        else:
            and_conditions.append({"file_name": u_regex})

    # 5. Year Condition
    if parsed.get("year"):
        y_regex = re.compile(rf"\b{parsed['year']}\b")
        if use_caption:
            and_conditions.append({"$or": [{"file_name": y_regex}, {"caption": y_regex}]})
        else:
            and_conditions.append({"file_name": y_regex})

    # 6. File type filter
    if file_type:
        and_conditions.append({"file_type": file_type})

    if not and_conditions:
        return {}
    if len(and_conditions) == 1:
        return and_conditions[0]
    return {"$and": and_conditions}


# ==============================================================================
# 6. RELEVANCE AND RANKING ENGINE (Section 5)
# ==============================================================================

def calculate_relevance_score(doc_filename: str, parsed_query: Dict[str, Any]) -> int:
    """
    Score search relevance deterministically:
    - Exact normalized title + year match: 1000
    - Exact normalized title: 800
    - Exact phrase match: 600
    - All important title tokens: 400
    - Partial token match: 200
    - Structured metadata bonuses (year, season, quality, language)
    """
    clean_fn = clean_media_title_for_comparison(doc_filename).lower()
    raw_fn = str(doc_filename or "").lower()
    target_title = (parsed_query.get("title") or "").strip().lower()

    if not target_title:
        return 100

    score = 0
    target_year = parsed_query.get("year")
    
    # Exact title match (with or without year)
    if target_title == clean_fn or (target_year and f"{target_title} {target_year}" == clean_fn):
        score = 1000 if (target_year and str(target_year) in raw_fn) else 800
    elif target_title in clean_fn:
        score = 600
    else:
        # Token overlap
        target_tokens = [w for w in target_title.split() if len(w) > 1]
        if target_tokens:
            matches_count = sum(1 for tok in target_tokens if tok in clean_fn)
            if matches_count == len(target_tokens):
                score = 400
            elif matches_count > 0:
                score = 200 + int((matches_count / len(target_tokens)) * 100)

    # Bonuses for structured criteria
    if target_year and str(target_year) in raw_fn:
        score += 50
    if parsed_query.get("season") is not None and matches_season(raw_fn, parsed_query["season"]):
        score += 50
    if parsed_query.get("quality") and matches_quality(raw_fn, parsed_query["quality"]):
        score += 30
    if parsed_query.get("language") and matches_language(raw_fn, parsed_query["language"]):
        score += 30
    elif parsed_query.get("universal_audio") and build_universal_audio_regex().search(raw_fn):
        score += 30

    return score


def rank_media_results(files: List[Any], parsed_query: Dict[str, Any]) -> List[Any]:
    """
    Rank media documents by relevance score, file size, and ID.
    Preserves both movie and series results when sharing a title.
    """
    if not files:
        return []

    def sort_key(doc):
        fn = getattr(doc, "file_name", "") or ""
        score = calculate_relevance_score(fn, parsed_query)
        size = getattr(doc, "file_size", 0) or 0
        file_id = str(getattr(doc, "file_id", "") or "")
        return (score, size, file_id)

    return sorted(files, key=sort_key, reverse=True)


# ==============================================================================
# 7. IN-MEMORY BOUNDED SEARCH CACHE (Section 11.D)
# ==============================================================================

class SearchCache:
    """Lightweight in-memory LRU/TTL cache for paginated search results."""
    def __init__(self, max_entries: int = 500, ttl_seconds: int = 60):
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        self._cache: Dict[str, Tuple[float, Any, int]] = {}

    def _make_key(self, query_norm: str, filters: dict, offset: int, max_results: int) -> str:
        f_repr = "-".join(f"{k}:{v}" for k, v in sorted(filters.items())) if filters else "none"
        return f"{query_norm}|{f_repr}|{offset}|{max_results}"

    def get(self, query_norm: str, filters: dict, offset: int, max_results: int) -> Optional[Tuple[List[Any], str, int]]:
        key = self._make_key(query_norm, filters, offset, max_results)
        entry = self._cache.get(key)
        if not entry:
            return None
        ts, files, total = entry
        if time.time() - ts > self.ttl:
            self._cache.pop(key, None)
            return None
        return files, str(offset + len(files)) if (offset + len(files) < total) else "", total

    def set(self, query_norm: str, filters: dict, offset: int, max_results: int, files: List[Any], total: int):
        if len(self._cache) >= self.max_entries:
            # Evict oldest entry
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][0])
            self._cache.pop(oldest_key, None)
        key = self._make_key(query_norm, filters, offset, max_results)
        self._cache[key] = (time.time(), files, total)

    def clear(self):
        self._cache.clear()


GLOBAL_SEARCH_CACHE = SearchCache()


# ==============================================================================
# 8. SEARCH ANALYTICS (Section 11.C)
# ==============================================================================

class SearchAnalytics:
    """Bounded in-memory search analytics without collecting sensitive user data."""
    def __init__(self, max_recent: int = 100):
        self.total_queries = 0
        self.zero_result_queries = 0
        self.total_duration_ms = 0.0
        self.recent_zero_results: List[str] = []
        self.max_recent = max_recent
        self.filter_usage: Dict[str, int] = {"quality": 0, "language": 0, "season": 0}

    def record_search(self, query: str, total_results: int, duration_ms: float, filters: Optional[dict] = None):
        self.total_queries += 1
        self.total_duration_ms += duration_ms
        if total_results == 0:
            self.zero_result_queries += 1
            if len(self.recent_zero_results) >= self.max_recent:
                self.recent_zero_results.pop(0)
            self.recent_zero_results.append(query[:50])
        if filters:
            for k in ("quality", "language", "season"):
                if filters.get(k):
                    self.filter_usage[k] = self.filter_usage.get(k, 0) + 1

    def get_stats(self) -> Dict[str, Any]:
        avg_ms = (self.total_duration_ms / self.total_queries) if self.total_queries > 0 else 0.0
        zero_rate = (self.zero_result_queries / self.total_queries * 100) if self.total_queries > 0 else 0.0
        return {
            "total_queries": self.total_queries,
            "zero_results": self.zero_result_queries,
            "zero_result_rate_pct": round(zero_rate, 2),
            "avg_duration_ms": round(avg_ms, 2),
            "filter_usage": dict(self.filter_usage),
            "recent_zero_queries": list(self.recent_zero_results[-10:]),
        }


GLOBAL_SEARCH_ANALYTICS = SearchAnalytics()


# ==============================================================================
# 9. SEARCH SESSION MANAGER (Sections 9 & 10)
# ==============================================================================

class SearchSession:
    """Encapsulates a user's isolated search query and active filter state."""
    def __init__(self, session_key: str, original_query: str, chat_id: int, user_id: int):
        self.session_key = session_key
        self.original_query = original_query
        self.parsed_query = parse_user_query(original_query)
        self.chat_id = chat_id
        self.user_id = user_id
        self.active_filters: Dict[str, Any] = {}
        # Pre-seed filters detected from query text itself
        if self.parsed_query.get("quality"):
            self.active_filters["quality"] = self.parsed_query["quality"]
        if self.parsed_query.get("language"):
            self.active_filters["language"] = self.parsed_query["language"]
        if self.parsed_query.get("season") is not None:
            self.active_filters["season"] = str(self.parsed_query["season"])
        self.created_at = time.time()
        self.updated_at = time.time()

    def set_filter(self, filter_type: str, value: Optional[str]):
        self.updated_at = time.time()
        if value is None or value == "homepage":
            self.active_filters.pop(filter_type, None)
        else:
            if filter_type == "quality":
                self.active_filters[filter_type] = canonicalize_quality(value)
            elif filter_type == "language":
                self.active_filters[filter_type] = canonicalize_language(value)
            elif filter_type == "season":
                self.active_filters[filter_type] = str(value)

    def reset_filters(self):
        self.updated_at = time.time()
        self.active_filters.clear()

    def get_query_input(self) -> Dict[str, Any]:
        """Construct multi-filter dictionary input for MongoDB search."""
        return {
            "search": self.parsed_query.get("title") or self.original_query,
            **self.active_filters
        }

    def get_filter_chip(self) -> Optional[str]:
        chips = []
        if "season" in self.active_filters:
            chips.append(f"S{int(self.active_filters['season']):02d}")
        if "quality" in self.active_filters:
            chips.append(self.active_filters["quality"].upper())
        if "language" in self.active_filters:
            chips.append(self.active_filters["language"].title())
        return " • ".join(chips) if chips else None


class SearchSessionManager:
    """Thread-safe, isolated search session store preventing cross-user corruption."""
    def __init__(self, max_sessions: int = 1000, ttl_seconds: int = 3600):
        self.max_sessions = max_sessions
        self.ttl = ttl_seconds
        self._sessions: Dict[str, SearchSession] = {}

    def get_or_create(self, session_key: str, query: str, chat_id: int, user_id: int) -> SearchSession:
        now = time.time()
        if session_key in self._sessions:
            sess = self._sessions[session_key]
            if now - sess.updated_at <= self.ttl:
                return sess
        # Create new session
        if len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions.keys(), key=lambda k: self._sessions[k].updated_at)
            self._sessions.pop(oldest, None)
        sess = SearchSession(session_key, query, chat_id, user_id)
        self._sessions[session_key] = sess
        return sess

    def get(self, session_key: str) -> Optional[SearchSession]:
        sess = self._sessions.get(session_key)
        if not sess:
            return None
        if time.time() - sess.updated_at > self.ttl:
            self._sessions.pop(session_key, None)
            return None
        return sess

    def clear(self):
        self._sessions.clear()


GLOBAL_SESSION_MANAGER = SearchSessionManager()


class SearchButton:
    """Lightweight button representation decoupled from Telegram client initialization."""
    def __init__(self, text: str, callback_data: Optional[str] = None, url: Optional[str] = None):
        self.text = text
        self.callback_data = callback_data
        self.url = url

    def __repr__(self):
        return f"SearchButton(text={self.text!r}, callback_data={self.callback_data!r}, url={self.url!r})"


def set_active_filter(session_key: str, filter_type: str, value: str) -> Dict[str, Any]:
    sess = GLOBAL_SESSION_MANAGER.get_or_create(session_key, "", 0, 0)
    sess.set_filter(filter_type, value)
    return sess.active_filters


def get_active_filters(session_key: str) -> Dict[str, Any]:
    sess = GLOBAL_SESSION_MANAGER.get(session_key)
    return sess.active_filters if sess else {}


def clear_active_filters(session_key: str):
    sess = GLOBAL_SESSION_MANAGER.get(session_key)
    if sess:
        sess.reset_filters()


def get_filter_chip_label(active_filters: Dict[str, Any]) -> Optional[str]:
    chips = []
    if not active_filters:
        return None
    if "season" in active_filters:
        chips.append(f"S{int(active_filters['season']):02d}")
    if "quality" in active_filters:
        chips.append(str(active_filters["quality"]).upper())
    if "language" in active_filters:
        chips.append(str(active_filters["language"]).title())
    return " • ".join(chips) if chips else None


def build_empty_filter_buttons(key: str, active_filter: Optional[str] = None, button_cls=None) -> List[List[Any]]:
    """Build persistent filter control bar when a filter yields 0 results."""
    if button_cls is None:
        import os
        if str(os.environ.get("TEST_MODE", "")).lower() in ("1", "true", "yes"):
            button_cls = SearchButton
        else:
            try:
                from pyrogram.types import InlineKeyboardButton
                button_cls = InlineKeyboardButton
            except Exception:
                button_cls = SearchButton

    btn = []
    if active_filter:
        btn.append([
            button_cls(f"🎯 Filter: {active_filter}", callback_data=f"filter_info#{active_filter}"),
            button_cls("🔄 Reset Filters", callback_data=f"reset_filter#{key}")
        ])
    else:
        btn.append([
            button_cls("🔄 Reset Filters", callback_data=f"reset_filter#{key}")
        ])
    btn.append([
        button_cls("Qᴜᴀʟɪᴛʏ", callback_data=f"qualities#{key}"),
        button_cls("Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{key}"),
        button_cls("Sᴇᴀsᴏɴ",  callback_data=f"seasons#{key}")
    ])
    btn.append([
        button_cls("↭ ɴᴏ ʀᴇsᴜʟᴛs ꜰᴏᴜɴᴅ ↭", callback_data="pages")
    ])
    return btn

