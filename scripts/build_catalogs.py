import json
import os
import re
import shutil
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
TORBOX_SEARCH_URL = "https://search-api.torbox.app"

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

TMDB_SESSION = requests.Session()
if len(TMDB_API_KEY) > 40:
    TMDB_SESSION.headers.update({"Authorization": f"Bearer {TMDB_API_KEY}"})

TRAKT_SESSION = requests.Session()
if TRAKT_CLIENT_ID:
    TRAKT_SESSION.headers.update({
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID,
        "User-Agent": "nuvio-rock-docs/3.0",
    })

TORBOX_SESSION = requests.Session()
if TORBOX_API_KEY:
    TORBOX_SESSION.headers.update({
        "Authorization": f"Bearer {TORBOX_API_KEY}",
        "User-Agent": "nuvio-rock-docs/3.0",
    })


def compile_terms(terms: list[str]) -> re.Pattern:
    escaped = [re.escape(t) for t in terms]
    return re.compile(r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)", re.IGNORECASE)


def tmdb_get(endpoint: str, params: dict | None = None, retries: int = 3) -> dict:
    params = dict(params or {})
    params.setdefault("language", "en-US")
    if len(TMDB_API_KEY) <= 40:
        params["api_key"] = TMDB_API_KEY

    url = f"{TMDB_BASE_URL}{endpoint}"
    for _ in range(retries):
        try:
            resp = TMDB_SESSION.get(url, params=params, timeout=20)
        except requests.RequestException:
            time.sleep(2)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2)) + 1
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 500:
            time.sleep(2)
            continue
        resp.raise_for_status()
        return resp.json()
    return {}


def trakt_get(endpoint: str, params: dict | None = None, retries: int = 3):
    if not TRAKT_CLIENT_ID:
        return None
    url = f"{TRAKT_BASE_URL}{endpoint}"
    for _ in range(retries):
        try:
            resp = TRAKT_SESSION.get(url, params=params or {}, timeout=20)
        except requests.RequestException:
            time.sleep(2)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 3)) + 1
            time.sleep(wait)
            continue
        if resp.status_code in {401, 403, 404}:
            return None
        if resp.status_code >= 500:
            time.sleep(2)
            continue
        resp.raise_for_status()
        return resp.json()
    return None


def torbox_search_get(path: str, params: dict | None = None, retries: int = 3) -> tuple[int, dict]:
    url = f"{TORBOX_SEARCH_URL}{path}"
    for _ in range(retries):
        try:
            resp = TORBOX_SESSION.get(url, params=params or {}, timeout=25)
        except requests.RequestException:
            time.sleep(2)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 4)) + 1
            time.sleep(wait)
            continue
        if resp.status_code in {401, 403}:
            return resp.status_code, {}
        if resp.status_code == 404:
            return 404, {}
        if resp.status_code >= 500:
            time.sleep(2)
            continue
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}
    return 0, {}


def resolve_keyword_ids(names: list[str]) -> list[int]:
    ids = set()
    for name in names:
        data = tmdb_get("/search/keyword", {"query": name})
        for item in data.get("results", []):
            if item.get("name", "").lower() == name.lower():
                ids.add(item["id"])
    return sorted(ids)


def collect_all_pages(endpoint: str, base_params: dict, max_pages: int = 100) -> set[int]:
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

    # Extra guard: ambiguous standalone "goth" or "new wave" tags require Music genre (10402)
    # or another concrete music tag (prevents Tim Burton / surfing docs)
    if is_goth_newwave and not (keywords & (GOTH_NEWWAVE_INDUSTRIAL_TAGS - {"goth", "new wave"})):
        if 10402 not in genre_ids and not (keywords & (MUSIC_GATE_TAGS - {"music"})):
            is_goth_newwave = False

    has_any_target_genre = (
        is_metal or is_punk or is_goth_newwave or is_grunge_alt
        or is_classic_explicit or is_classic_general
    )
    if not has_any_target_genre:
        return None

    # 3. Hard block unrelated genres (pop, rap, EDM, country, jazz, opera, boyband)
    # unless the title carries an explicit rock/metal/punk/grunge/goth/new-wave subgenre tag
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
        time.sleep(0.25)
        for entry in results:
            lst = entry.get("list") or {}
            tid = (lst.get("ids") or {}).get("trakt")
            item_count = lst.get("item_count") or 0
            if tid and tid not in seen_trakt_list_ids and 5 <= item_count <= 500:
                seen_trakt_list_ids.add(tid)
                list_endpoints.append(f"/lists/{tid}/items/movies,shows")

    for ep in list_endpoints:
        items = trakt_get(ep) or []
        time.sleep(0.25)
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

    # 1. Trakt public lists
    trakt_movies, trakt_tv = discover_trakt_candidates(overrides.get("trakt_lists", []))
    movie_ids |= trakt_movies
    tv_ids |= trakt_tv

    # 2. Chunked TMDB keyword discovery across all 6 categories
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

    # Query in batches of 12 IDs so TMDB never silently truncates long keyword pipes
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

    # 3. Broad sweep of English Documentary + Music (catches well-tagged docs with alt keyword IDs)
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


# --- TORBOX PLAYABILITY CHECK (CACHED OR HEALTHY SEEDERS) ---

def _torrents_are_playable(payload: dict) -> bool:
    torrents = (payload.get("data") or {}).get("torrents") or []
    for t in torrents:
        if t.get("cached") is True:
            return True
        seeders = t.get("last_known_seeders")
        if seeders is None:
            seeders = t.get("seeders") or 0
        if int(seeders) >= MIN_HEALTHY_SEEDERS:
            return True
    return False


def _usenet_is_cached(payload: dict) -> bool:
    nzbs = (payload.get("data") or {}).get("nzbs") or []
    return any(n.get("cached") is True for n in nzbs)


def check_torbox_playable(imdb_id: str, media_type: str) -> bool | None:
    """
    Returns True if cached on TorBox or has >= MIN_HEALTHY_SEEDERS,
    False if unplayable, or None if TorBox returned an auth error.
    """
    time.sleep(0.22)  # Stay safely under TorBox's 300 req/min ceiling

    if media_type == "series":
        status, data = torbox_search_get(
            f"/torrents/imdb:{imdb_id}",
            {"season": 1, "episode": 1, "check_cache": "true"},
        )
        if status in {401, 403}:
            return None
        if status == 200 and _torrents_are_playable(data):
            return True

        # Fallback: check full Season 1 pack
        time.sleep(0.22)
        status2, data2 = torbox_search_get(
            f"/torrents/imdb:{imdb_id}",
            {"season": 1, "check_cache": "true"},
        )
        if status2 == 200 and _torrents_are_playable(data2):
            return True

        # Optional Usenet cache check (silently skipped if plan lacks Usenet)
        status_u, data_u = torbox_search_get(
            f"/usenet/imdb:{imdb_id}",
            {"season": 1, "episode": 1, "check_cache": "true"},
        )
        if status_u == 200 and _usenet_is_cached(data_u):
            return True

        return False

    # Movie check
    status, data = torbox_search_get(
        f"/torrents/imdb:{imdb_id}",
        {"check_cache": "true"},
    )
    if status in {401, 403}:
        return None
    if status == 200 and _torrents_are_playable(data):
        return True

    status_u, data_u = torbox_search_get(
        f"/usenet/imdb:{imdb_id}",
        {"check_cache": "true"},
    )
    if status_u == 200 and _usenet_is_cached(data_u):
        return True

    return False


def filter_catalogs_with_torbox(catalogs: dict[str, list[dict]]) -> dict[str, list[dict]]:
    if not TORBOX_API_KEY:
        print("TORBOX_API_KEY not set — skipping TorBox playability filter.")
        return catalogs

    # Deduplicate IMDb IDs across all 6 catalogs so each title is checked only once
    to_check: dict[str, str] = {}
    for items in catalogs.values():
        for item in items:
            if not item.get("_whitelisted"):
                to_check[item["id"]] = item["type"]

    if not to_check:
        return catalogs

    print(
        f"Checking {len(to_check)} unique titles on TorBox "
        f"(cached or >= {MIN_HEALTHY_SEEDERS} seeders)..."
    )

    # Probe one item first to verify the API key before running the full batch
    first_id, first_type = next(iter(to_check.items()))
    probe = check_torbox_playable(first_id, first_type)
    if probe is None:
        print("WARNING: TorBox returned 401/403 (check TORBOX_API_KEY). Skipping TorBox filter.")
        return catalogs

    playable_map: dict[str, bool] = {first_id: probe}
    remaining = [(iid, mtype) for iid, mtype in to_check.items() if iid != first_id]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(check_torbox_playable, iid, mtype): iid
            for iid, mtype in remaining
        }
        for fut in as_completed(futures):
            iid = futures[fut]
            res = fut.result()
            playable_map[iid] = True if res is None else bool(res)

    kept_total = sum(1 for v in playable_map.values() if v)
    print(f"TorBox check complete: {kept_total}/{len(playable_map)} titles playable.")

    filtered: dict[str, list[dict]] = {}
    for cat_id, items in catalogs.items():
        filtered[cat_id] = [
            item for item in items
            if item.get("_whitelisted") or playable_map.get(item["id"], False)
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

    # Final TorBox playability filter (cached OR >= MIN_HEALTHY_SEEDERS)
    catalogs = filter_catalogs_with_torbox(catalogs)

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
        "version": "3.0.0",
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
