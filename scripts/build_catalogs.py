import base64
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID", "").strip()
TORBOX_API_KEY = os.environ.get("TORBOX_API_KEY", "").strip()

if not TMDB_API_KEY:
    raise RuntimeError("TMDB_API_KEY environment variable is missing.")

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TRAKT_BASE_URL = "https://api.trakt.tv"
TORBOX_API_URL = "https://api.torbox.app/v1/api"

IMG_POSTER = "https://image.tmdb.org/t/p/w780"
IMG_BACKDROP = "https://image.tmdb.org/t/p/w1280"

# "movie" ensures all 6 rows appear in Android TV Discover -> Movies as well as Home.
# Individual items keep their own "movie" or "series" type so episode pickers work normally.
CATALOG_TYPE = "movie"

NOW_UTC = datetime.now(timezone.utc)
TODAY = NOW_UTC.strftime("%Y-%m-%d")
ONE_YEAR_AGO = (NOW_UTC - timedelta(days=365)).strftime("%Y-%m-%d")
SIX_MONTHS_AGO = (NOW_UTC - timedelta(days=180)).strftime("%Y-%m-%d")

ROOT_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT_DIR / "docs"
OVERRIDES_PATH = ROOT_DIR / "overrides.json"

MIN_MOVIE_RUNTIME = 50
MIN_HEALTHY_SEEDERS = 5
TORBOX_BATCH_SIZE = 60
MAX_ITEMS_PER_CATALOG = None

CATALOG_DEFS = [
    ("genre-era-docs", "Genres, Scenes & Eras", "Genres & Eras"),
    ("classic-rock-docs", "Classic Rock Documentaries", "Classic Rock"),
    ("grunge-alt-docs", "Grunge & Alternative Documentaries", "Grunge & Alternative"),
    ("goth-newwave-docs", "Goth, New Wave & Industrial Documentaries", "Goth, New Wave & Industrial"),
    ("metal-docs", "Metal Documentaries", "Metal"),
    ("punk-docs", "Punk Documentaries", "Punk"),
]
CAT_LABELS = {cid: label for cid, _, label in CATALOG_DEFS}

_THREAD_LOCAL = threading.local()


def get_http_session() -> requests.Session:
    sess = getattr(_THREAD_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "nuvio-rock-docs/3.1"})
        _THREAD_LOCAL.session = sess
    return sess


def compile_terms(terms: list[str]) -> re.Pattern:
    escaped = [re.escape(t) for t in terms]
    return re.compile(r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)", re.IGNORECASE)


def tmdb_get(endpoint: str, params: dict | None = None, retries: int = 3) -> dict:
    params = dict(params or {})
    params.setdefault("language", "en-US")
    headers = {}
    if len(TMDB_API_KEY) > 40:
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    else:
        params["api_key"] = TMDB_API_KEY

    url = f"{TMDB_BASE_URL}{endpoint}"
    sess = get_http_session()
    for _ in range(retries):
        try:
            resp = sess.get(url, params=params, headers=headers, timeout=15)
        except requests.RequestException:
            time.sleep(1.5)
            continue
        if resp.status_code == 429:
            wait = min(int(resp.headers.get("Retry-After", 2)) + 1, 10)
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 500:
            time.sleep(1.5)
            continue
        resp.raise_for_status()
        return resp.json()
    return {}


def trakt_get(endpoint: str, params: dict | None = None, retries: int = 3):
    if not TRAKT_CLIENT_ID:
        return None
    url = f"{TRAKT_BASE_URL}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID,
    }
    sess = get_http_session()
    for _ in range(retries):
        try:
            resp = sess.get(url, params=params or {}, headers=headers, timeout=15)
        except requests.RequestException:
            time.sleep(1.5)
            continue
        if resp.status_code == 429:
            wait = min(int(resp.headers.get("Retry-After", 3)) + 1, 10)
            time.sleep(wait)
            continue
        if resp.status_code in {401, 403, 404}:
            return None
        if resp.status_code >= 500:
            time.sleep(1.5)
            continue
        resp.raise_for_status()
        return resp.json()
    return None


def resolve_keyword_ids(names: list[str]) -> list[int]:
    ids = set()
    for name in names:
        data = tmdb_get("/search/keyword", {"query": name})
        for item in data.get("results", []):
            if item.get("name", "").lower() == name.lower():
                ids.add(item["id"])
    return sorted(ids)


def collect_all_pages(endpoint: str, base_params: dict, max_pages: int = 60) -> set[int]:
    ids: set[int] = set()
    page = 1
    while page <= max_pages:
        data = tmdb_get(endpoint, {**base_params, "page": page})
        for item in data.get("results", []):
            ids.add(item["id"])
        total_pages = data.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1
    return ids


# --- STRICT TMDB KEYWORD SETS (NO PLOT-SUMMARY GUESSING) ---

MUSIC_GATE_TAGS = {
    "music", "music documentary", "rockumentary", "musician", "band", "singer",
    "guitarist", "drummer", "bassist", "songwriter", "record label",
    "recording studio", "music festival", "rock festival", "music history",
    "music industry", "music scene", "rock music", "rock band", "classic rock",
    "hard rock", "heavy metal", "metal band", "metal music", "punk", "punk rock",
    "hardcore punk", "post-punk", "grunge", "alternative rock", "indie rock",
    "shoegaze", "britpop", "goth rock", "gothic rock", "darkwave", "coldwave",
    "new wave music", "synthpop", "synth-pop", "synthwave", "synth rock",
    "industrial music", "industrial rock", "progressive rock", "psychedelic rock",
    "glam rock", "blues rock", "southern rock", "garage rock", "krautrock",
    "rock and roll", "rock 'n' roll", "arena rock", "folk rock", "stoner rock",
    "thrash metal", "death metal", "black metal", "doom metal", "glam metal",
    "hair metal", "nu metal", "power metal", "progressive metal", "speed metal",
    "sludge metal", "stoner metal", "metalcore", "gothic metal", "symphonic metal",
    "industrial metal", "folk metal", "viking metal", "nwobhm", "pop punk",
    "skate punk", "anarcho-punk", "street punk", "riot grrrl", "proto-punk",
    "crust punk", "ska punk", "cbgb", "woodstock",
}

STRICT_NON_ROCK_TAGS = {
    "hip-hop", "hip hop", "rap", "rap music", "rapper", "k-pop", "kpop",
    "boy band", "boy bands", "teen pop", "edm", "electronic dance music",
    "house music", "techno", "trance", "dubstep", "dj", "rave",
    "country music", "bluegrass", "jazz", "jazz music", "jazz musician",
    "classical music", "opera", "symphony orchestra", "reggaeton", "disco",
    "gospel", "gospel music", "pop star", "pop singer", "pop music", "r&b",
}

NON_MUSIC_METAL_WORDS = (
    "metal detector", "scrap metal", "precious metal", "sheet metal",
    "metalwork", "metal sculpture", "metal shop", "liquid metal",
    "metal gear", "eagles of death metal",
)

METAL_EXPLICIT_TAGS = {
    "heavy metal", "thrash metal", "death metal", "black metal", "doom metal",
    "power metal", "progressive metal", "prog metal", "nu metal", "glam metal",
    "hair metal", "speed metal", "sludge metal", "stoner metal", "metalcore",
    "deathcore", "gothic metal", "goth metal", "symphonic metal",
    "industrial metal", "folk metal", "viking metal", "groove metal",
    "alternative metal", "funk metal", "rap metal", "nwobhm", "metal band",
    "metal music", "headbanger", "norwegian black metal", "extreme metal",
    "crossover thrash", "metal subculture",
}

PUNK_TAGS = {
    "punk", "punk rock", "hardcore punk", "pop punk", "skate punk",
    "anarcho-punk", "street punk", "riot grrrl", "proto-punk", "crust punk",
    "ska punk", "straight edge", "cbgb", "punk band", "punk subculture",
    "punk culture", "punk movement", "hardcore", "hardcore music",
    "horror punk", "folk punk", "garage punk", "d-beat", "oi!",
}

GOTH_NEWWAVE_INDUSTRIAL_TAGS = {
    "goth", "goth rock", "gothic rock", "goth subculture", "gothic subculture",
    "darkwave", "coldwave", "ethereal wave", "deathrock",
    "new wave", "new wave music", "synthpop", "synth-pop", "synthwave",
    "synth rock", "new romantic", "post-punk", "no wave",
    "industrial music", "industrial rock", "ebm", "electronic body music",
    "electro-industrial", "dark ambient",
}

GRUNGE_ALT_TAGS = {
    "grunge", "post-grunge", "alternative rock", "alt-rock", "indie rock",
    "shoegaze", "britpop", "madchester", "college rock", "noise rock",
    "stoner rock", "desert rock", "dream pop", "math rock", "post-rock",
    "garage rock revival", "seattle sound", "slacker rock",
}

CLASSIC_ROCK_EXPLICIT_TAGS = {
    "classic rock", "hard rock", "rock and roll", "rock 'n' roll",
    "progressive rock", "prog rock", "psychedelic rock", "acid rock",
    "blues rock", "southern rock", "glam rock", "arena rock", "aor",
    "folk rock", "garage rock", "heartland rock", "space rock", "krautrock",
    "roots rock", "pub rock", "british invasion", "merseybeat", "surf rock",
    "country rock", "soft rock", "jazz rock", "rock opera", "rockabilly",
}

CLASSIC_ROCK_GENERAL_TAGS = {
    "rock music", "rock band", "rock musician", "rock singer",
    "rock guitarist", "rock drummer", "rockumentary", "rock history", "rock",
}

GENRE_ERA_TAGS = {
    "music history", "rock history", "music scene", "subculture",
    "punk subculture", "goth subculture", "metal subculture",
    "counterculture", "musical movement", "music festival", "rock festival",
    "woodstock", "recording studio", "record label", "independent record label",
    "music industry", "album cover", "heavy metal history",
}

SINGLE_ARTIST_BIO_TAGS = {
    "biography", "biographical", "autobiography", "singer-songwriter",
    "solo artist", "making of",
}

SINGLE_ARTIST_TITLE_CUES_RE = compile_terms([
    "music box:", "classic albums:", "the making of", "becoming",
])

# --- UNOFFICIAL / BOOTLEG & CONCERT FILTERS ---

UNOFFICIAL_TITLE_RE = compile_terms([
    "unauthorized", "unauthorised", "unofficial", "under review",
    "critical review", "rock milestones", "classic rock legends",
    "a critical look", "the definitive critical review", "maximum",
    "total rock review", "inside track", "uncensored biography",
    "unauthorized biography", "unauthorised biography", "bootleg",
    "up close and personal", "beyond the spotlight", "phenomena",
    "the story so far",
])

UNOFFICIAL_OVERVIEW_RE = compile_terms([
    "unauthorized", "unauthorised", "unofficial", "independent critical review",
    "critical review", "not authorized", "not authorised",
    "without the involvement", "retrospective review", "expert panel",
    "music journalists review", "rock critics review", "panel of experts",
])

UNOFFICIAL_STUDIOS = (
    "chrome dreams", "sexy intellectual", "classic rock productions",
    "entertain me", "stormbird", "edgehill", "passport video",
    "highway 61", "delta music", "mvd visual", "wienerworld",
    "pride dvd", "laser media", "music video distributors",
    "american icons", "rockumentary", "a2zcds", "gravity entertainment",
    "pioneer entertainment", "isis productions", "archive media",
)

CONCERT_TITLE_PATTERNS = [
    re.compile(r"^(live\s+(at|in|from|on)\b|in\s+concert\b|apple\s+music\s+live\b)", re.IGNORECASE),
    re.compile(
        r"(\blive\s+(at|in|from)\s+[a-z0-9]|\(live\)|\[live\]|:\s*live$|-\s*live$|"
        r"\bworld\s+tour\b|\btour\s+live\b|\bcelebration\s+tour\b|\bunplugged\b|"
        r"\binduction\s+ceremony\b|\bhall\s+of\s+fame\b)",
        re.IGNORECASE,
    ),
]

CONCERT_ONLY_TAGS = {"concert film", "live concert", "live performance"}
NARRATIVE_DOC_TAGS = {
    "music documentary", "biography", "biographical", "music history",
    "behind the scenes", "interview", "subculture", "band history",
    "archival footage", "music industry", "record label", "recording studio",
}


def is_unofficial_doc(title: str, overview: str, companies: list[dict]) -> bool:
    if UNOFFICIAL_TITLE_RE.search(title):
        return True
    if UNOFFICIAL_OVERVIEW_RE.search(overview):
        return True
    for comp in companies or []:
        cname = (comp.get("name") or "").lower()
        if any(bad in cname for bad in UNOFFICIAL_STUDIOS):
            return True
    return False


def is_pure_concert_film(title: str, overview: str, keywords: set[str]) -> bool:
    for pat in CONCERT_TITLE_PATTERNS:
        if pat.search(title.strip()):
            return True

    if (keywords & CONCERT_ONLY_TAGS) and not (keywords & NARRATIVE_DOC_TAGS):
        return True

    overview_l = overview.lower()
    concert_phrases = [
        "filmed live at", "recorded live at", "concert filmed at",
        "live performance recorded", "captured live in concert",
        "live concert film", "performs a homecoming concert",
        "performs a special concert", "in a special concert for",
        "performs their greatest hits live", "induction ceremony",
    ]
    story_phrases = [
        "interviews", "story of", "history of", "chronicles",
        "behind the scenes", "rise of", "archive footage", "archival footage",
        "biography", "explores the", "traces the", "struggles", "making of",
    ]
    if any(p in overview_l for p in concert_phrases) and not any(s in overview_l for s in story_phrases):
        return True

    return False


def has_watch_providers(details: dict) -> bool:
    results = (details.get("watch/providers") or {}).get("results") or {}
    for region_data in results.values():
        if any(region_data.get(k) for k in ("flatrate", "rent", "buy", "free", "ads")):
            return True
    return False


def get_watchable_movie_date(details: dict) -> str | None:
    status = details.get("status") or ""
    primary_date = details.get("release_date") or ""
    if status != "Released" or not primary_date or primary_date > TODAY:
        return None

    # For 2024+ releases, require a public release type (3=Theatrical, 4=Digital,
    # 5=Physical, 6=TV) on or before TODAY, OR active streaming/VOD providers on TMDB.
    if primary_date >= "2024-01-01":
        public_dates = []
        rel_results = (details.get("release_dates") or {}).get("results") or []
        for country in rel_results:
            for entry in country.get("release_dates") or []:
                rtype = entry.get("type")
                rdate = (entry.get("release_date") or "")[:10]
                if rtype in {3, 4, 5, 6} and rdate and rdate <= TODAY:
                    public_dates.append(rdate)
        if public_dates:
            return min(public_dates)
        if has_watch_providers(details):
            return primary_date
        return None

    return primary_date


def is_metal_by_tags(keywords: set[str]) -> bool:
    if keywords & METAL_EXPLICIT_TAGS:
        return True
    for kw in keywords:
        if "metal" in kw and not any(bad in kw for bad in NON_MUSIC_METAL_WORDS):
            return True
    return False


def classify_by_tmdb_tags(title: str, genre_ids: set[int], keywords: set[str]) -> str | None:
    # 1. Hard Music Gate: must have TMDB Music genre (10402) or an unambiguous music tag
    if 10402 not in genre_ids and not (keywords & MUSIC_GATE_TAGS):
        return None

    # 2. Detect our 5 core music genre groups strictly via TMDB tags
    is_metal = is_metal_by_tags(keywords)
    is_punk = bool(keywords & PUNK_TAGS)
    is_goth_newwave = bool(keywords & GOTH_NEWWAVE_INDUSTRIAL_TAGS)
    is_grunge_alt = bool(keywords & GRUNGE_ALT_TAGS)
    is_classic_explicit = bool(keywords & CLASSIC_ROCK_EXPLICIT_TAGS)
    is_classic_general = bool(keywords & CLASSIC_ROCK_GENERAL_TAGS)

    # Guard against ambiguous standalone "goth" or "new wave" tags on non-music docs
    if is_goth_newwave and not (keywords & (GOTH_NEWWAVE_INDUSTRIAL_TAGS - {"goth", "new wave"})):
        if 10402 not in genre_ids and not (keywords & (MUSIC_GATE_TAGS - {"music"})):
            is_goth_newwave = False

    has_any_target_genre = (
        is_metal or is_punk or is_goth_newwave or is_grunge_alt
        or is_classic_explicit or is_classic_general
    )
    if not has_any_target_genre:
        return None

    # 3. Hard block unrelated genres unless an explicit rock/metal/punk/grunge/goth subgenre tag exists
    if keywords & STRICT_NON_ROCK_TAGS:
        has_strong_subgenre = (
            is_metal or is_punk or is_goth_newwave or is_grunge_alt or is_classic_explicit
        )
        if not has_strong_subgenre:
            return None

    # 4. Genres, Scenes & Eras (multi-artist movements, studios, labels, festivals, history)
    if (
        (keywords & GENRE_ERA_TAGS)
        and not (keywords & SINGLE_ARTIST_BIO_TAGS)
        and not SINGLE_ARTIST_TITLE_CUES_RE.search(title)
    ):
        return "genre-era-docs"

    # 5. Route to the most specific music row
    if is_metal:
        return "metal-docs"
    if is_punk:
        return "punk-docs"
    if is_goth_newwave:
        return "goth-newwave-docs"
    if is_grunge_alt:
        return "grunge-alt-docs"
    if is_classic_explicit or is_classic_general:
        return "classic-rock-docs"

    return None


# --- TRAKT PUBLIC LIST DISCOVERY ---

def discover_trakt_candidates(custom_lists: list[str]) -> tuple[set[int], set[int]]:
    movie_ids: set[int] = set()
    tv_ids: set[int] = set()
    if not TRAKT_CLIENT_ID:
        print("TRAKT_CLIENT_ID not set — skipping Trakt list discovery.")
        return movie_ids, tv_ids

    list_endpoints: list[str] = []

    for raw in custom_lists or []:
        m = re.search(r"users/([^/]+)/lists/([^/?#]+)", raw.strip())
        if m:
            list_endpoints.append(f"/users/{m.group(1)}/lists/{m.group(2)}/items/movies,shows")
        elif "/" in raw.strip() and not raw.strip().startswith("http"):
            user, slug = raw.strip().split("/", 1)
            list_endpoints.append(f"/users/{user}/lists/{slug}/items/movies,shows")

    trakt_queries = [
        "heavy metal documentaries",
        "metal documentaries",
        "classic rock documentaries",
        "rock documentaries",
        "grunge documentaries",
        "punk documentaries",
        "new wave documentaries",
        "goth documentaries",
        "music documentaries",
    ]
    seen_trakt_list_ids: set[int] = set()
    for q in trakt_queries:
        results = trakt_get("/search/list", {"query": q, "limit": 6}) or []
        time.sleep(0.2)
        for entry in results:
            lst = entry.get("list") or {}
            tid = (lst.get("ids") or {}).get("trakt")
            item_count = lst.get("item_count") or 0
            if tid and tid not in seen_trakt_list_ids and 5 <= item_count <= 500:
                seen_trakt_list_ids.add(tid)
                list_endpoints.append(f"/lists/{tid}/items/movies,shows")

    for ep in list_endpoints:
        items = trakt_get(ep) or []
        time.sleep(0.2)
        for item in items:
            itype = item.get("type")
            if itype == "movie":
                tmdb_id = ((item.get("movie") or {}).get("ids") or {}).get("tmdb")
                if tmdb_id:
                    movie_ids.add(int(tmdb_id))
            elif itype == "show":
                tmdb_id = ((item.get("show") or {}).get("ids") or {}).get("tmdb")
                if tmdb_id:
                    tv_ids.add(int(tmdb_id))

    print(f"Trakt added {len(movie_ids)} candidate movies and {len(tv_ids)} candidate series.")
    return movie_ids, tv_ids


def load_overrides() -> dict:
    if OVERRIDES_PATH.exists():
        return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    return {"trakt_lists": [], "whitelist_add": [], "pin_category": {}, "blacklist": []}


def discover_candidates(overrides: dict) -> tuple[set[int], set[int], set[int], set[int]]:
    movie_ids: set[int] = set()
    tv_ids: set[int] = set()

    trakt_movies, trakt_tv = discover_trakt_candidates(overrides.get("trakt_lists", []))
    movie_ids |= trakt_movies
    tv_ids |= trakt_tv

    discovery_keywords = [
        # Metal
        "heavy metal", "thrash metal", "death metal", "black metal", "doom metal",
        "glam metal", "hair metal", "nu metal", "power metal", "progressive metal",
        "speed metal", "sludge metal", "stoner metal", "metalcore", "gothic metal",
        "symphonic metal", "industrial metal", "metal band", "nwobhm",
        # Punk
        "punk", "punk rock", "hardcore punk", "pop punk", "skate punk",
        "anarcho-punk", "riot grrrl", "proto-punk", "cbgb", "punk band",
        # Goth, New Wave & Industrial
        "goth", "goth rock", "gothic rock", "darkwave", "new wave", "new wave music",
        "synthpop", "synth-pop", "synthwave", "post-punk", "industrial music",
        "industrial rock", "new romantic", "no wave",
        # Grunge & Alternative
        "grunge", "alternative rock", "indie rock", "shoegaze", "britpop",
        "stoner rock", "noise rock", "college rock", "madchester",
        # Classic Rock & General Rock
        "classic rock", "hard rock", "rock music", "rock band", "rock and roll",
        "progressive rock", "psychedelic rock", "glam rock", "blues rock",
        "southern rock", "garage rock", "krautrock", "rockumentary", "music documentary",
    ]
    kw_ids = resolve_keyword_ids(discovery_keywords)
    print(f"Resolved {len(kw_ids)} TMDB discovery keyword IDs.")

    chunk_size = 12
    for i in range(0, len(kw_ids), chunk_size):
        kw_pipe = "|".join(str(k) for k in kw_ids[i:i + chunk_size])
        movie_ids |= collect_all_pages("/discover/movie", {
            "with_genres": "99",
            "without_genres": "35",
            "with_keywords": kw_pipe,
            "with_original_language": "en",
            "primary_release_date.lte": TODAY,
            "sort_by": "primary_release_date.desc",
            "include_adult": "false",
        }, max_pages=60)

        tv_ids |= collect_all_pages("/discover/tv", {
            "with_genres": "99",
            "without_genres": "35",
            "with_keywords": kw_pipe,
            "with_original_language": "en",
            "first_air_date.lte": TODAY,
            "sort_by": "first_air_date.desc",
            "include_adult": "false",
        }, max_pages=60)

    for sort_by in ("primary_release_date.desc", "vote_count.desc"):
        movie_ids |= collect_all_pages("/discover/movie", {
            "with_genres": "99,10402",
            "without_genres": "35",
            "with_original_language": "en",
            "primary_release_date.lte": TODAY,
            "vote_count.gte": 2,
            "sort_by": sort_by,
            "include_adult": "false",
        }, max_pages=25)

    return movie_ids, tv_ids, trakt_movies, trakt_tv


def resolve_imdb_whitelist(imdb_id: str) -> tuple[str | None, int | None]:
    found = tmdb_get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
    if found.get("movie_results"):
        return "movie", found["movie_results"][0]["id"]
    if found.get("tv_results"):
        return "series", found["tv_results"][0]["id"]
    return None, None


def fetch_movie_meta(
    tmdb_id: int,
    overrides: dict,
    trusted: bool = False,
    forced_category: str | None = None,
) -> tuple[str, dict] | None:
    blacklist = set(overrides.get("blacklist", []))
    pin_map = overrides.get("pin_category", {})

    if str(tmdb_id) in blacklist:
        return None

    details = tmdb_get(
        f"/movie/{tmdb_id}",
        {"append_to_response": "external_ids,keywords,release_dates,watch/providers"},
    )
    if not details:
        return None

    imdb_id = (details.get("external_ids") or {}).get("imdb_id")
    if not imdb_id or not imdb_id.startswith("tt") or imdb_id in blacklist:
        return None

    poster_path = details.get("poster_path")
    if not poster_path:
        return None

    original_lang = details.get("original_language") or ""
    title = details.get("title") or details.get("original_title") or ""
    overview = details.get("overview") or ""
    runtime = details.get("runtime") or 0
    vote_count = details.get("vote_count") or 0
    popularity = details.get("popularity") or 0
    companies = details.get("production_companies") or []
    genre_ids = {g["id"] for g in (details.get("genres") or [])}
    keywords = {k["name"].lower() for k in ((details.get("keywords") or {}).get("keywords") or [])}

    effective_date = get_watchable_movie_date(details)

    if not forced_category:
        if original_lang != "en" or not effective_date:
            return None
        if 99 not in genre_ids or 35 in genre_ids:
            return None
        if len(overview.strip()) < 40:
            return None
        if 0 < runtime < MIN_MOVIE_RUNTIME:
            return None
        if runtime == 0 and effective_date < SIX_MONTHS_AGO:
            return None
        if not trusted and effective_date < ONE_YEAR_AGO and vote_count < 2:
            return None
        if is_unofficial_doc(title, overview, companies):
            return None
        if is_pure_concert_film(title, overview, keywords):
            return None
    else:
        effective_date = effective_date or (details.get("release_date") or "")

    category = (
        forced_category
        or pin_map.get(imdb_id)
        or pin_map.get(str(tmdb_id))
        or classify_by_tmdb_tags(title, genre_ids, keywords)
    )
    if not category or category not in CAT_LABELS:
        return None

    year = effective_date[:4] if len(effective_date) >= 4 else ""
    vote_avg = details.get("vote_average") or 0
    backdrop_path = details.get("backdrop_path")

    meta = {
        "id": imdb_id,
        "type": "movie",
        "name": title,
        "poster": f"{IMG_POSTER}{poster_path}",
        "posterShape": "poster",
        "description": overview,
        "releaseInfo": year,
        "runtime": f"{runtime} min" if runtime else None,
        "genres": ["Documentary", "Music", CAT_LABELS[category]],
        "_release_date": effective_date,
        "_score": popularity + (vote_avg * 4),
        "_whitelisted": bool(forced_category),
    }
    if backdrop_path:
        meta["background"] = f"{IMG_BACKDROP}{backdrop_path}"
    if vote_avg > 0:
        meta["imdbRating"] = f"{vote_avg:.1f}"

    return category, {k: v for k, v in meta.items() if v is not None}


def fetch_series_meta(
    tmdb_id: int,
    overrides: dict,
    trusted: bool = False,
    forced_category: str | None = None,
) -> tuple[str, dict] | None:
    blacklist = set(overrides.get("blacklist", []))
    pin_map = overrides.get("pin_category", {})

    if str(tmdb_id) in blacklist:
        return None

    details = tmdb_get(f"/tv/{tmdb_id}", {"append_to_response": "external_ids,keywords"})
    if not details:
        return None

    imdb_id = (details.get("external_ids") or {}).get("imdb_id")
    if not imdb_id or not imdb_id.startswith("tt") or imdb_id in blacklist:
        return None

    poster_path = details.get("poster_path")
    if not poster_path:
        return None

    first_air = details.get("first_air_date") or ""
    status = details.get("status") or ""
    original_lang = details.get("original_language") or ""
    title = details.get("name") or details.get("original_name") or ""
    overview = details.get("overview") or ""
    vote_count = details.get("vote_count") or 0
    popularity = details.get("popularity") or 0
    num_episodes = details.get("number_of_episodes") or 0
    companies = details.get("production_companies") or []
    genre_ids = {g["id"] for g in (details.get("genres") or [])}
    keywords = {k["name"].lower() for k in ((details.get("keywords") or {}).get("results") or [])}

    if not forced_category:
        if original_lang != "en":
            return None
        if status in {"Planned", "In Production", "Rumored", "Pilot"} or not first_air or first_air > TODAY:
            return None
        if num_episodes < 1:
            return None
        if 99 not in genre_ids or 35 in genre_ids:
            return None
        if len(overview.strip()) < 40:
            return None
        if not trusted and first_air < ONE_YEAR_AGO and vote_count < 2:
            return None
        if is_unofficial_doc(title, overview, companies):
            return None
        if is_pure_concert_film(title, overview, keywords):
            return None

    category = (
        forced_category
        or pin_map.get(imdb_id)
        or pin_map.get(str(tmdb_id))
        or classify_by_tmdb_tags(title, genre_ids, keywords)
    )
    if not category or category not in CAT_LABELS:
        return None

    last_air = details.get("last_air_date") or ""
    start_year = first_air[:4] if len(first_air) >= 4 else ""
    end_year = last_air[:4] if len(last_air) >= 4 else ""
    release_info = f"{start_year}-{end_year}" if start_year and end_year and start_year != end_year else start_year

    vote_avg = details.get("vote_average") or 0
    backdrop_path = details.get("backdrop_path")

    meta = {
        "id": imdb_id,
        "type": "series",
        "name": title,
        "poster": f"{IMG_POSTER}{poster_path}",
        "posterShape": "poster",
        "description": overview,
        "releaseInfo": release_info,
        "genres": ["Documentary", "Music", CAT_LABELS[category]],
        "_release_date": first_air,
        "_score": popularity + (vote_avg * 4),
        "_whitelisted": bool(forced_category),
    }
    if backdrop_path:
        meta["background"] = f"{IMG_BACKDROP}{backdrop_path}"
    if vote_avg > 0:
        meta["imdbRating"] = f"{vote_avg:.1f}"

    return category, {k: v for k, v in meta.items() if v is not None}


# --- PUBLIC INDEXER HASH HARVESTING + TORBOX /checkcached BATCH FILTER ---

BTIH_RE = re.compile(r"btih:([a-fA-F0-9]{40}|[A-Za-z2-7]{32})", re.IGNORECASE)
STOP_WORDS = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "at", "by", "from"}


def normalize_hex_hash(raw_hash: str | None, magnet_url: str | None = None) -> str | None:
    candidate = (raw_hash or "").strip()
    if not candidate and magnet_url:
        m = BTIH_RE.search(magnet_url)
        if m:
            candidate = m.group(1)

    if not candidate:
        return None

    if len(candidate) == 40 and all(c in "0123456789abcdefABCDEF" for c in candidate):
        h = candidate.lower()
        return None if h == "0" * 40 else h

    if len(candidate) == 32:
        try:
            h = base64.b32decode(candidate.upper()).hex().lower()
            return None if h == "0" * 40 else h
        except Exception:
            return None

    return None


def _tokenize_title(text: str) -> list[str]:
    clean = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return [w for w in clean.split() if w]


def _knaben_hit_matches(hit_title: str, doc_title: str, year: str, media_type: str) -> bool:
    if not hit_title:
        return False

    hit_tokens = set(_tokenize_title(hit_title))
    raw_tokens = _tokenize_title(doc_title)
    sig_tokens = [w for w in raw_tokens if w not in STOP_WORDS and len(w) >= 2] or raw_tokens
    if not sig_tokens:
        return False

    # Require all significant title words (or the subtitle/main-title clause if colon-separated)
    full_match = all(w in hit_tokens for w in sig_tokens)
    clause_match = False
    if not full_match and ":" in doc_title:
        parts = [p.strip() for p in doc_title.split(":") if p.strip()]
        for part in parts:
            ptoks = [w for w in _tokenize_title(part) if w not in STOP_WORDS and len(w) >= 2]
            if len(ptoks) >= 2 and all(w in hit_tokens for w in ptoks):
                clause_match = True
                break

    if not (full_match or clause_match):
        return False

    if media_type == "movie" and year.isdigit():
        y = int(year)
        valid_years = {str(y - 1), str(y), str(y + 1)}
        if not (hit_tokens & valid_years):
            return False
    elif media_type == "series" and len(sig_tokens) <= 1:
        # Extra guard for single-word series titles (e.g. "Punk")
        has_tv_marker = bool(re.search(r"\b(s\d{1,2}|season|complete|720p|1080p|2160p|webrip|hdtv)\b", hit_title, re.I))
        has_year = year.isdigit() and year in hit_tokens
        if not (has_tv_marker or has_year):
            return False

    return True


def fetch_title_torrents(imdb_id: str, media_type: str, title: str, year: str) -> tuple[set[str], int]:
    """
    Collects (infohashes, max_seeders) from APIBay (IMDb), YTS (IMDb, movies),
    and Knaben (video categories, title-validated).
    """
    hashes: set[str] = set()
    max_seeders = 0
    sess = get_http_session()

    # 1. APIBay (exact IMDb ID lookup — zero false positives)
    try:
        r = sess.get("https://apibay.org/q.php", params={"q": imdb_id}, timeout=8)
        if r.status_code == 200:
            rows = r.json()
            if isinstance(rows, list):
                for row in rows:
                    if str(row.get("id", "0")) == "0":
                        continue
                    h = normalize_hex_hash(row.get("info_hash"))
                    if h:
                        hashes.add(h)
                        seeds = int(row.get("seeders") or 0)
                        if seeds > max_seeders:
                            max_seeders = seeds
    except Exception:
        pass

    # 2. YTS (exact IMDb ID lookup — movies only)
    if media_type == "movie" and max_seeders < MIN_HEALTHY_SEEDERS:
        try:
            r = sess.get(
                "https://yts.mx/api/v2/list_movies.json",
                params={"query_term": imdb_id, "limit": 1},
                timeout=8,
            )
            if r.status_code == 200:
                movies = ((r.json().get("data") or {}).get("movies")) or []
                for mv in movies:
                    if (mv.get("imdb_code") or "").lower() != imdb_id.lower():
                        continue
                    for tor in mv.get("torrents") or []:
                        h = normalize_hex_hash(tor.get("hash"))
                        if h:
                            hashes.add(h)
                            seeds = int(tor.get("seeds") or 0)
                            if seeds > max_seeders:
                                max_seeders = seeds
        except Exception:
            pass

    # 3. Knaben (aggregates 1337x, TPB, RuTracker, EZTV, LimeTorrents — video categories only)
    if max_seeders < MIN_HEALTHY_SEEDERS:
        clean_q = re.sub(r"[^a-zA-Z0-9\s'-]+", " ", title).strip()
        query_str = f"{clean_q} {year}".strip() if (media_type == "movie" and year.isdigit()) else clean_q
        if query_str:
            try:
                r = sess.post(
                    "https://api.knaben.org/v1",
                    json={
                        "query": query_str,
                        "order_by": "seeders",
                        "size": 15,
                        "categories": [2000000, 3000000],
                    },
                    timeout=10,
                )
                if r.status_code == 200:
                    hits = (r.json() or {}).get("hits") or []
                    for hit in hits:
                        htitle = hit.get("title") or ""
                        if not _knaben_hit_matches(htitle, title, year, media_type):
                            continue
                        h = normalize_hex_hash(hit.get("hash"), hit.get("magnetUrl"))
                        if h:
                            hashes.add(h)
                            seeds = int(hit.get("seeders") or 0)
                            if seeds > max_seeders:
                                max_seeders = seeds
            except Exception:
                pass

    return hashes, max_seeders


def torbox_check_cached_batches(hashes: list[str]) -> tuple[set[str], bool]:
    """
    Batch-checks hashes via TorBox's unrestricted main API (/v1/api/torrents/checkcached).
    Returns (cached_hashes, api_ok).
    """
    cached: set[str] = set()
    if not TORBOX_API_KEY or not hashes:
        return cached, True

    sess = get_http_session()
    headers = {"Authorization": f"Bearer {TORBOX_API_KEY}"}
    url = f"{TORBOX_API_URL}/torrents/checkcached"

    total_batches = (len(hashes) + TORBOX_BATCH_SIZE - 1) // TORBOX_BATCH_SIZE
    print(f"Checking {len(hashes)} hashes on TorBox /checkcached ({total_batches} batch requests)...")

    for idx in range(0, len(hashes), TORBOX_BATCH_SIZE):
        batch = hashes[idx:idx + TORBOX_BATCH_SIZE]
        try:
            resp = sess.get(
                url,
                headers=headers,
                params={
                    "hash": ",".join(batch),
                    "format": "object",
                    "list_files": "false",
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            print(f"WARNING: TorBox request failed on batch {idx // TORBOX_BATCH_SIZE + 1}: {exc}")
            if idx == 0:
                return set(), False
            continue

        if resp.status_code in {401, 403}:
            print(f"WARNING: TorBox returned {resp.status_code} (check TORBOX_API_KEY). Skipping cache filter.")
            return set(), False

        if resp.status_code == 429:
            print("WARNING: TorBox rate-limited /checkcached (429). Aborting remaining cache batches.")
            return (cached, idx > 0)

        if resp.status_code != 200:
            print(f"WARNING: TorBox returned HTTP {resp.status_code} on batch {idx // TORBOX_BATCH_SIZE + 1}.")
            if idx == 0:
                return set(), False
            continue

        try:
            payload = resp.json()
        except ValueError:
            continue

        data = payload.get("data")
        if isinstance(data, dict):
            for req_hash, info in data.items():
                if info:
                    cached.add(req_hash.lower())
                    if isinstance(info, dict) and info.get("hash"):
                        cached.add(str(info["hash"]).lower())
        elif isinstance(data, list):
            for info in data:
                if isinstance(info, dict) and info.get("hash"):
                    cached.add(str(info["hash"]).lower())

        time.sleep(0.25)

    return cached, True


def filter_catalogs_by_playability(catalogs: dict[str, list[dict]]) -> dict[str, list[dict]]:
    if not TORBOX_API_KEY:
        print("TORBOX_API_KEY not set — skipping TorBox/seeder playability filter.")
        return catalogs

    # Deduplicate non-whitelisted titles across all 6 catalogs
    to_check: dict[str, tuple[str, str, str]] = {}
    for items in catalogs.values():
        for item in items:
            if not item.get("_whitelisted") and item["id"] not in to_check:
                year = (item.get("_release_date") or "")[:4]
                to_check[item["id"]] = (item["type"], item["name"], year)

    if not to_check:
        return catalogs

    print(
        f"Harvesting torrent hashes & seeders for {len(to_check)} unique titles "
        f"(APIBay + YTS + Knaben)..."
    )

    title_hashes: dict[str, set[str]] = {}
    playable: dict[str, bool] = {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(fetch_title_torrents, iid, mtype, title, year): iid
            for iid, (mtype, title, year) in to_check.items()
        }
        for fut in as_completed(futures):
            iid = futures[fut]
            try:
                hashes, max_seeds = fut.result()
            except Exception:
                hashes, max_seeds = set(), 0
            title_hashes[iid] = hashes
            if max_seeds >= MIN_HEALTHY_SEEDERS:
                playable[iid] = True

    titles_with_hashes = sum(1 for h in title_hashes.values() if h)
    passed_by_seeders = len(playable)
    print(
        f"Indexer pass complete: found hashes for {titles_with_hashes}/{len(to_check)} titles; "
        f"{passed_by_seeders} already pass via >= {MIN_HEALTHY_SEEDERS} seeders."
    )

    # Safety fuse: if public indexers returned almost nothing (network block/outage), don't wipe catalogs
    if len(to_check) >= 20 and titles_with_hashes == 0:
        print("WARNING: Indexers returned 0 hashes across all titles. Keeping catalogs unfiltered.")
        return catalogs

    # Collect hashes ONLY for titles that haven't already passed via healthy seeders
    hashes_to_check_set: set[str] = set()
    for iid, hashes in title_hashes.items():
        if iid not in playable and hashes:
            hashes_to_check_set.update(hashes)

    cached_hashes, torbox_ok = torbox_check_cached_batches(sorted(hashes_to_check_set))
    if not torbox_ok:
        print("WARNING: TorBox check aborted — keeping catalogs unfiltered to avoid dropping cached titles.")
        return catalogs

    rescued_by_cache = 0
    for iid, hashes in title_hashes.items():
        if iid not in playable and (hashes & cached_hashes):
            playable[iid] = True
            rescued_by_cache += 1

    print(
        f"Playability filter summary: {passed_by_seeders} via healthy seeders + "
        f"{rescued_by_cache} rescued by TorBox cache = {len(playable)}/{len(to_check)} playable."
    )

    filtered: dict[str, list[dict]] = {}
    for cat_id, items in catalogs.items():
        filtered[cat_id] = [
            item for item in items
            if item.get("_whitelisted") or playable.get(item["id"], False)
        ]
    return filtered


def sort_and_clean(items: list[dict]) -> list[dict]:
    seen = {}
    for item in items:
        seen[item["id"]] = item
    unique = list(seen.values())

    unique.sort(key=lambda x: (x.get("_release_date", ""), x.get("_score", 0)), reverse=True)

    if MAX_ITEMS_PER_CATALOG:
        unique = unique[:MAX_ITEMS_PER_CATALOG]

    for item in unique:
        item.pop("_release_date", None)
        item.pop("_score", None)
        item.pop("_whitelisted", None)
    return unique


def main():
    overrides = load_overrides()
    print(f"Discovering official, released English docs via TMDB tags + Trakt (up to {TODAY})...")
    movie_ids, tv_ids, trusted_movies, trusted_tv = discover_candidates(overrides)
    print(f"Found {len(movie_ids)} candidate movies and {len(tv_ids)} candidate series.")

    catalogs: dict[str, list[dict]] = {cid: [] for cid, _, _ in CATALOG_DEFS}

    for entry in overrides.get("whitelist_add", []):
        mtype = entry.get("media_type")
        tid = entry.get("tmdb_id")
        if not tid and entry.get("imdb_id"):
            mtype, tid = resolve_imdb_whitelist(entry["imdb_id"])
        if not tid:
            continue

        forced_cat = entry.get("category")
        if forced_cat not in catalogs:
            continue
        if mtype == "movie":
            res = fetch_movie_meta(int(tid), overrides, trusted=True, forced_category=forced_cat)
            if res:
                cat, meta = res
                catalogs[cat].append(meta)
            movie_ids.discard(int(tid))
        elif mtype == "series":
            res = fetch_series_meta(int(tid), overrides, trusted=True, forced_category=forced_cat)
            if res:
                cat, meta = res
                catalogs[cat].append(meta)
            tv_ids.discard(int(tid))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(fetch_movie_meta, mid, overrides, mid in trusted_movies)
            for mid in movie_ids
        ]
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                cat, meta = res
                catalogs[cat].append(meta)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(fetch_series_meta, tid, overrides, tid in trusted_tv)
            for tid in tv_ids
        ]
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                cat, meta = res
                catalogs[cat].append(meta)

    # Final playability filter: cached on TorBox OR >= MIN_HEALTHY_SEEDERS
    catalogs = filter_catalogs_by_playability(catalogs)

    # Clean old catalog folder (removes stale docs/catalog/other/ files)
    shutil.rmtree(DOCS_DIR / "catalog", ignore_errors=True)
    out_dir = DOCS_DIR / "catalog" / CATALOG_TYPE
    out_dir.mkdir(parents=True, exist_ok=True)

    for cat_id, _, _ in CATALOG_DEFS:
        cleaned = sort_and_clean(catalogs[cat_id])
        out_path = out_dir / f"{cat_id}.json"
        out_path.write_text(json.dumps({"metas": cleaned}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {len(cleaned)} items -> {out_path}")

    manifest = {
        "id": "org.nuvio.rockmetalpunkdocs",
        "version": "3.1.0",
        "name": "Rock, Metal & Punk Documentaries",
        "description": "Live auto-updating catalog of official, playable English rock, metal, punk, grunge, goth, and new wave documentaries & docuseries.",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": [
            {"type": CATALOG_TYPE, "id": cid, "name": cname}
            for cid, cname, _ in CATALOG_DEFS
        ],
    }
    manifest_path = DOCS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote manifest -> {manifest_path}")


if __name__ == "__main__":
    main()
