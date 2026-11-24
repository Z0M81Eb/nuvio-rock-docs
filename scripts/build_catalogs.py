import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID", "").strip()

if not TMDB_API_KEY:
    raise RuntimeError("TMDB_API_KEY environment variable is missing.")

BASE_URL = "https://api.themoviedb.org/3"
TRAKT_BASE_URL = "https://api.trakt.tv"
IMG_POSTER = "https://image.tmdb.org/t/p/w780"
IMG_BACKDROP = "https://image.tmdb.org/t/p/w1280"

# Change to "movie" only if your Nuvio build hides "other" rows on the Home screen.
# Individual items inside each catalog always keep their own "movie" or "series" type.
CATALOG_TYPE = "other"

NOW_UTC = datetime.now(timezone.utc)
TODAY = NOW_UTC.strftime("%Y-%m-%d")
ONE_YEAR_AGO = (NOW_UTC - timedelta(days=365)).strftime("%Y-%m-%d")
SIX_MONTHS_AGO = (NOW_UTC - timedelta(days=180)).strftime("%Y-%m-%d")

ROOT_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT_DIR / "docs"
OVERRIDES_PATH = ROOT_DIR / "overrides.json"

MIN_MOVIE_RUNTIME = 50
MAX_ITEMS_PER_CATALOG = None

SESSION = requests.Session()
if len(TMDB_API_KEY) > 40:
    SESSION.headers.update({"Authorization": f"Bearer {TMDB_API_KEY}"})

TRAKT_SESSION = requests.Session()
if TRAKT_CLIENT_ID:
    TRAKT_SESSION.headers.update({
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID,
        "User-Agent": "nuvio-rock-docs/2.6",
    })


def compile_terms(terms: list[str]) -> re.Pattern:
    escaped = [re.escape(t) for t in terms]
    return re.compile(r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)", re.IGNORECASE)


def tmdb_get(endpoint: str, params: dict | None = None, retries: int = 3) -> dict:
    params = dict(params or {})
    params.setdefault("language", "en-US")
    if len(TMDB_API_KEY) <= 40:
        params["api_key"] = TMDB_API_KEY

    url = f"{BASE_URL}{endpoint}"
    for _ in range(retries):
        resp = SESSION.get(url, params=params, timeout=20)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2)) + 1
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            return {}
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


# --- TRAKT PUBLIC LIST DISCOVERY ---

def discover_trakt_candidates(custom_lists: list[str]) -> tuple[set[int], set[int]]:
    movie_ids: set[int] = set()
    tv_ids: set[int] = set()
    if not TRAKT_CLIENT_ID:
        print("TRAKT_CLIENT_ID not set — skipping Trakt list discovery.")
        return movie_ids, tv_ids

    list_endpoints: list[str] = []

    # 1. Custom Trakt list URLs or user/slug paths from overrides.json
    for raw in custom_lists or []:
        m = re.search(r"users/([^/]+)/lists/([^/?#]+)", raw.strip())
        if m:
            list_endpoints.append(f"/users/{m.group(1)}/lists/{m.group(2)}/items/movies,shows")
        elif "/" in raw.strip() and not raw.strip().startswith("http"):
            user, slug = raw.strip().split("/", 1)
            list_endpoints.append(f"/users/{user}/lists/{slug}/items/movies,shows")

    # 2. Auto-discover top public Trakt lists for metal, rock, and punk documentaries
    trakt_queries = [
        "heavy metal documentaries",
        "metal documentaries",
        "rock documentaries",
        "punk documentaries",
        "music documentaries",
    ]
    seen_trakt_list_ids: set[int] = set()
    for q in trakt_queries:
        results = trakt_get("/search/list", {"query": q, "limit": 8}) or []
        time.sleep(0.25)
        for entry in results:
            lst = entry.get("list") or {}
            tid = (lst.get("ids") or {}).get("trakt")
            item_count = lst.get("item_count") or 0
            if tid and tid not in seen_trakt_list_ids and 5 <= item_count <= 500:
                seen_trakt_list_ids.add(tid)
                list_endpoints.append(f"/lists/{tid}/items/movies,shows")

    # Fetch items from each Trakt list and collect TMDB IDs
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


# --- UNOFFICIAL / BOOTLEG DOCUMENTARY FILTERS ---

UNOFFICIAL_TITLE_RE = compile_terms([
    "unauthorized", "unauthorised", "unofficial", "under review",
    "critical review", "rock milestones", "classic rock legends",
    "a critical look", "the definitive critical review", "maximum",
    "total rock review", "inside track", "uncensored biography",
    "unauthorized biography", "unauthorised biography", "bootleg",
    "up close and personal", "beyond the spotlight", "phenomena",
    "the story so far", "inside the",
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


# --- REAL WATCHABLE RELEASE GATE (BLOCKS FESTIVAL-ONLY PREMIERES) ---

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

    # For 2024+ titles, require EITHER a public release type (3=Theatrical, 4=Digital,
    # 5=Physical, 6=TV) on or before TODAY, OR active streaming/VOD providers on TMDB.
    # This blocks festival-premiere-only titles (e.g. Ballad of Judas Priest) while
    # allowing VOD/streaming releases (e.g. Di'Anno: Iron Maiden's Lost Singer).
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


# --- CLASSIFICATION PATTERNS (WORD-BOUNDARY SAFE) ---

UNRELATED_GENRE_RE = compile_terms([
    "hip-hop", "hip hop", "rapper", "rap music", "k-pop", "kpop",
    "boy band", "boy bands", "boyband", "boybands", "teen pop",
    "opera", "symphony orchestra", "classical composer", "country music",
    "bluegrass", "jazz musician", "jazz pianist", "reggaeton", "edm",
    "techno dj", "house music", "disco diva", "pop star", "pop singer",
])

EXPLICIT_GENRE_ERA_TITLE_RE = [
    re.compile(
        r"^(punk|new wave|heavy metal|metal evolution|krautrock|grunge|britpop|"
        r"post-punk|synthpop|synthwave|goth|darkwave|hardcore|shoegaze|industrial|"
        r"woodstock|glastonbury|sound city|muscle shoals|laurel canyon|salad days|hype!)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:metal evolution|headbanger'?s journey|global metal|"
        r"decline of western civilization|american hardcore|seven ages of rock|"
        r"ages of rock|heavy metal britannia|synth britannia|prog rock britannia|"
        r"punk britannia|krautrock|laurel canyon|muscle shoals|sound city|"
        r"rockfield|wax trax|factory records|sub pop|sst records|epitaph records|"
        r"squaring the circle|hipgnosis|woodstock|glastonbury|festival express|"
        r"long live rock|soaring highs and brutal lows|decade of punk|"
        r"nothin'? but a good time|get thrashed|murder in the front row|"
        r"until the light takes us|once upon a time in norway|heavy metal in baghdad|"
        r"wacken|louder than hell|year that music changed|history of rock|"
        r"history of heavy metal|history of metal|history of punk|history of goth|"
        r"history of new wave|history of synth|history of industrial|"
        r"evolution of rock|evolution of metal|evolution of punk|story of rock|"
        r"story of heavy metal|story of punk|story of goth|rise of synth|"
        r"industrial accident|new wave:|rock school|women in metal|women in rock|"
        r"the sixties|the seventies|the eighties|the nineties|1971|studio on the farm)(?!\w)",
        re.IGNORECASE,
    ),
]

GENRE_ERA_SYNOPSIS_RE = compile_terms([
    "explores the history of", "traces the history of", "traces the evolution of",
    "history of heavy metal", "history of punk", "history of rock",
    "history of goth", "history of new wave", "history of synthpop",
    "history of synthwave", "history of industrial", "history of grunge",
    "birth of punk", "birth of heavy metal", "birth of rock", "birth of goth",
    "rise of punk", "rise of heavy metal", "rise of grunge", "rise of new wave",
    "rise of synthwave", "rise of industrial", "new wave movement",
    "goth subculture", "gothic subculture", "punk subculture", "metal subculture",
    "a decade of punk", "music of the 1960s", "music of the 1970s",
    "music of the 1980s", "music of the 1990s", "sound of the 70s", "sound of the 80s",
    "iconic recording studio", "legendary recording studio", "famous recording studio",
    "iconic record label", "legendary record label", "independent record label",
    "album cover design", "voices of women in metal",
])

SINGLE_ARTIST_TITLE_CUES_RE = compile_terms([
    "music box:", "the making of", "classic albums:", "becoming",
    "super duper", "the sound of my voice", "eight days a week",
    "runnin' down a dream", "let the canary sing",
])

ARTIST_NAMES_RE = compile_terms([
    "metallica", "iron maiden", "di'anno", "paul di'anno", "black sabbath",
    "judas priest", "megadeth", "motorhead", "motörhead", "pantera", "sepultura",
    "ozzy osbourne", "ozzy", "ronnie james dio", "dio", "lemmy", "slipknot",
    "korn", "system of a down", "helloween", "w.a.s.p.", "mastodon", "gojira",
    "lamb of god", "type o negative", "nightwish", "paradise lost",
    "dream theater", "rammstein", "marilyn manson", "twisted sister", "gwar",
    "anvil", "pentagram", "bobby liebling", "ramones", "sex pistols",
    "the clash", "dead kennedys", "black flag", "bad religion", "misfits",
    "minor threat", "fugazi", "the stooges", "iggy pop", "green day",
    "the offspring", "nofx", "social distortion", "bad brains", "descendents",
    "buzzcocks", "joe strummer", "led zeppelin", "pink floyd", "the beatles",
    "beatles", "rolling stones", "the who", "deep purple", "queen",
    "freddie mercury", "ac/dc", "aerosmith", "guns n' roses", "nirvana",
    "kurt cobain", "pearl jam", "soundgarden", "chris cornell",
    "alice in chains", "foo fighters", "dave grohl", "david bowie",
    "jimi hendrix", "the doors", "jim morrison", "fleetwood mac", "rush",
    "genesis", "peter gabriel", "phil collins", "king crimson", "frank zappa",
    "alice cooper", "kiss", "mötley crüe", "motley crue", "bon jovi",
    "van halen", "def leppard", "thin lizzy", "phil lynott", "lynyrd skynyrd",
    "zz top", "velvet underground", "the cure", "depeche mode", "joy division",
    "new order", "bauhaus", "siouxsie", "sisters of mercy", "the cult",
    "billy idol", "gary numan", "kraftwerk", "talking heads", "blondie",
    "tears for fears", "duran duran", "the smiths", "morrissey", "nick cave",
    "nine inch nails", "ministry", "skinny puppy", "killing joke", "radiohead",
    "oasis", "smashing pumpkins", "red hot chili peppers", "r.e.m.",
    "bruce springsteen", "tom petty", "bob seger", "peter frampton",
    "foreigner", "blue öyster cult", "blue oyster cult", "dire straits",
    "paul mccartney", "john lennon", "george harrison", "linda ronstadt",
    "counting crows", "cyndi lauper", "stevie nicks", "elton john",
    "billy joel", "neil young", "bob dylan", "janis joplin", "carlos santana",
    "eric clapton", "jeff beck", "brian wilson", "beach boys", "u2",
    "the police", "sting", "peter green", "eagles of death metal", "scorpions",
    "randy rhoads", "jason becker", "marty friedman", "dimebag", "slayer",
    "anthrax", "exodus", "testament", "kreator", "behemoth", "ghost", "tool",
])

METAL_RE = compile_terms([
    "heavy metal", "thrash metal", "death metal", "black metal", "doom metal",
    "power metal", "progressive metal", "prog metal", "nu metal", "glam metal",
    "hair metal", "speed metal", "sludge metal", "stoner metal", "metalcore",
    "gothic metal", "goth metal", "symphonic metal", "industrial metal",
    "folk metal", "viking metal", "nwobhm", "headbanger", "metal band",
    "norwegian black metal", "metallica", "iron maiden", "di'anno",
    "paul di'anno", "black sabbath", "judas priest", "slayer", "kerry king",
    "tom araya", "megadeth", "dave mustaine", "anthrax", "scott ian",
    "motorhead", "motörhead", "pantera", "dimebag", "sepultura",
    "ozzy osbourne", "ozzy", "randy rhoads", "ronnie james dio", "dio",
    "lemmy kilmister", "lemmy", "chuck schuldiner", "death by metal",
    "euronymous", "burzum", "kreator", "bathory", "meshuggah", "opeth",
    "slipknot", "korn", "system of a down", "maynard james keenan",
    "tobias forge", "helloween", "w.a.s.p.", "mastodon", "gojira",
    "lamb of god", "bruce dickinson", "rob halford", "tony iommi",
    "james hetfield", "type o negative", "peter steele", "nightwish",
    "paradise lost", "dream theater", "rammstein", "marilyn manson",
    "twisted sister", "gwar", "anvil", "pentagram", "bobby liebling",
    "jason becker", "scorpions", "exodus", "testament", "behemoth",
    "i am thor", "jon mikl thor",
])

PUNK_RE = compile_terms([
    "punk", "punk rock", "hardcore punk", "post-punk", "pop punk",
    "skate punk", "anarcho-punk", "street punk", "riot grrrl", "cbgb",
    "no wave", "crust punk", "straight edge", "ska punk", "proto-punk",
    "ramones", "sex pistols", "the clash", "dead kennedys", "black flag",
    "bad religion", "misfits", "minor threat", "fugazi", "the stooges",
    "iggy pop", "green day", "the offspring", "nofx", "social distortion",
    "bad brains", "descendents", "buzzcocks", "dave vanian", "captain sensible",
    "joe strummer", "johnny rotten", "john lydon", "henry rollins",
    "jello biafra", "sid vicious", "joey ramone", "ian mackaye",
])

ROCK_RE = compile_terms([
    "rock music", "classic rock", "hard rock", "rock and roll", "rock 'n' roll",
    "grunge", "alternative rock", "indie rock", "psychedelic rock",
    "progressive rock", "prog rock", "blues rock", "folk rock", "southern rock",
    "garage rock", "glam rock", "arena rock", "aor", "stoner rock", "desert rock",
    "space rock", "krautrock", "shoegaze", "britpop", "heartland rock",
    "rock band", "rock star", "goth rock", "gothic rock", "goth", "darkwave",
    "coldwave", "new wave", "synthpop", "synth-pop", "synthwave", "synth rock",
    "industrial rock", "industrial music", "ebm", "new romantic",
    "eagles of death metal", "led zeppelin", "pink floyd", "the beatles",
    "beatles", "rolling stones", "the who", "deep purple", "queen",
    "freddie mercury", "brian may", "ac/dc", "aerosmith", "guns n' roses",
    "nirvana", "kurt cobain", "pearl jam", "soundgarden", "chris cornell",
    "alice in chains", "foo fighters", "dave grohl", "david bowie",
    "jimi hendrix", "jim morrison", "ray manzarek", "fleetwood mac",
    "rush", "geddy lee", "neil peart", "genesis", "peter gabriel",
    "phil collins", "king crimson", "frank zappa", "alice cooper", "kiss",
    "gene simmons", "paul stanley", "mötley crüe", "motley crue", "bon jovi",
    "jon bon jovi", "van halen", "def leppard", "thin lizzy", "phil lynott",
    "lynyrd skynyrd", "zz top", "velvet underground", "the cure",
    "robert smith", "depeche mode", "dave gahan", "martin gore",
    "joy division", "ian curtis", "new order", "bauhaus", "peter murphy",
    "siouxsie", "sisters of mercy", "andrew eldritch", "the cult",
    "billy idol", "gary numan", "kraftwerk", "talking heads", "blondie",
    "debbie harry", "tears for fears", "duran duran", "the smiths",
    "morrissey", "nick cave", "nine inch nails", "trent reznor", "ministry",
    "al jourgensen", "skinny puppy", "killing joke", "my bloody valentine",
    "radiohead", "oasis", "liam gallagher", "noel gallagher", "damon albarn",
    "smashing pumpkins", "red hot chili peppers", "r.e.m.", "bruce springsteen",
    "tom petty", "robbie robertson", "levon helm", "bob seger",
    "peter frampton", "foreigner", "steve lukather", "blue öyster cult",
    "blue oyster cult", "dire straits", "mark knopfler", "paul mccartney",
    "john lennon", "george harrison", "linda ronstadt", "counting crows",
    "cyndi lauper", "u2", "the police", "sting", "peter green",
])

CONCERT_TITLE_PATTERNS = [
    re.compile(r"^(live\s+(at|in|from|on)\b|in\s+concert\b)", re.IGNORECASE),
    re.compile(
        r"(\blive\s+(at|in|from)\s+[a-z0-9]|\(live\)|\[live\]|:\s*live$|-\s*live$|"
        r"\bworld\s+tour\s+live\b|\btour\s+live\b|\bunplugged\b|"
        r"\binduction\s+ceremony\b|\bhall\s+of\s+fame\b)",
        re.IGNORECASE,
    ),
]

MUSIC_KEYWORDS = {
    "music", "music documentary", "musician", "band", "rock music",
    "heavy metal", "punk", "punk rock", "singer", "guitarist", "drummer",
    "record label", "recording studio", "music festival", "new wave",
    "synthpop", "synthwave", "goth", "goth rock", "gothic rock", "darkwave",
    "industrial music", "industrial rock", "grunge", "hard rock", "post-punk",
    "shoegaze", "britpop", "krautrock", "progressive rock", "glam rock",
}


def is_pure_concert_film(title: str, overview: str) -> bool:
    for pat in CONCERT_TITLE_PATTERNS:
        if pat.search(title.strip()):
            return True

    overview_l = overview.lower()
    concert_phrases = [
        "filmed live at", "recorded live at", "concert filmed at",
        "live performance recorded", "captured live in concert",
        "live concert film", "performs their greatest hits live",
        "induction ceremony",
    ]
    story_phrases = [
        "interviews", "story of", "history of", "chronicles",
        "behind the scenes", "rise of", "archive footage", "archival footage",
        "biography", "explores the", "traces the", "struggles", "journey",
        "therapist", "making of", "band members",
    ]
    if any(p in overview_l for p in concert_phrases) and not any(s in overview_l for s in story_phrases):
        return True

    return False


def has_music_relevance(genre_ids: set[int], keywords: set[str], combined: str) -> bool:
    if 10402 in genre_ids:
        return True
    if keywords & MUSIC_KEYWORDS:
        return True
    return bool(METAL_RE.search(combined) or PUNK_RE.search(combined) or ROCK_RE.search(combined))


def has_core_genre_signal(combined: str) -> bool:
    return bool(METAL_RE.search(combined) or PUNK_RE.search(combined) or ROCK_RE.search(combined))


def is_single_artist_title(title: str) -> bool:
    if ARTIST_NAMES_RE.search(title):
        return True
    if SINGLE_ARTIST_TITLE_CUES_RE.search(title):
        return True
    return False


def is_genre_scene_or_era(title: str, overview: str, keywords: set[str]) -> bool:
    clean_title = title.strip()

    # Check explicit genre/era titles first (e.g. Hype!, Nothin' But a Good Time, Metal Evolution)
    for pat in EXPLICIT_GENRE_ERA_TITLE_RE:
        if pat.search(clean_title):
            return True

    # Never route a single-artist bio or album-making doc into Genres, Scenes & Eras
    if is_single_artist_title(clean_title):
        return False

    combined = f"{title} {overview} {' '.join(keywords)}"
    if GENRE_ERA_SYNOPSIS_RE.search(overview) and has_core_genre_signal(combined):
        return True

    return False


def classify_item(title: str, overview: str, keywords: set[str]) -> str | None:
    if UNRELATED_GENRE_RE.search(title):
        return None

    combined = f"{title} {overview} {' '.join(keywords)}"
    if UNRELATED_GENRE_RE.search(combined) and not has_core_genre_signal(combined):
        return None

    # 1. Genres, Scenes & Eras
    if is_genre_scene_or_era(title, overview, keywords):
        return "genre-era-docs"

    # Strip "Eagles of Death Metal" before Metal matching so it doesn't trigger "death metal"
    safe_title = re.sub(r"eagles of death metal", "", title, flags=re.IGNORECASE)
    safe_combined = re.sub(r"eagles of death metal", "", combined, flags=re.IGNORECASE)

    # 2. TITLE-FIRST classification (prevents Bon Jovi / Deep Purple from being hijacked by synopsis)
    if METAL_RE.search(safe_title):
        return "metal-docs"
    if PUNK_RE.search(safe_title):
        return "punk-docs"
    if ROCK_RE.search(title):
        return "rock-docs"

    # 3. Fallback to overview + keywords
    if METAL_RE.search(safe_combined):
        return "metal-docs"
    if PUNK_RE.search(safe_combined):
        return "punk-docs"
    if ROCK_RE.search(combined):
        return "rock-docs"
    return None


def load_overrides() -> dict:
    if OVERRIDES_PATH.exists():
        return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    return {"trakt_lists": [], "whitelist_add": [], "pin_category": {}, "blacklist": []}


def discover_candidates(overrides: dict) -> tuple[set[int], set[int], set[int], set[int]]:
    movie_ids: set[int] = set()
    tv_ids: set[int] = set()

    # 1. Trakt public lists (human-curated — bypass TMDB vote thresholds)
    trakt_movies, trakt_tv = discover_trakt_candidates(overrides.get("trakt_lists", []))
    movie_ids |= trakt_movies
    tv_ids |= trakt_tv

    kw_ids = resolve_keyword_ids([
        "heavy metal", "thrash metal", "black metal", "death metal", "doom metal",
        "punk", "punk rock", "hardcore punk", "post-punk",
        "rock music", "classic rock", "hard rock", "grunge", "glam rock",
        "progressive rock", "psychedelic rock", "krautrock", "shoegaze",
        "britpop", "new wave", "synthpop", "synthwave", "goth", "goth rock",
        "gothic rock", "darkwave", "industrial music", "music documentary",
    ])
    kw_pipe = "|".join(str(k) for k in kw_ids)

    # 2. English keyword-matched docs (no vote_count floor on /discover so niche releases aren't dropped)
    if kw_pipe:
        movie_ids |= collect_all_pages("/discover/movie", {
            "with_genres": "99",
            "with_keywords": kw_pipe,
            "with_original_language": "en",
            "primary_release_date.lte": TODAY,
            "sort_by": "primary_release_date.desc",
            "include_adult": "false",
        }, max_pages=100)

        tv_ids |= collect_all_pages("/discover/tv", {
            "with_genres": "99",
            "with_keywords": kw_pipe,
            "with_original_language": "en",
            "first_air_date.lte": TODAY,
            "sort_by": "first_air_date.desc",
            "include_adult": "false",
        }, max_pages=100)

    # 3. Broad sweep of English Documentary + Music
    for sort_by in ["primary_release_date.desc", "vote_count.desc"]:
        movie_ids |= collect_all_pages("/discover/movie", {
            "with_genres": "99,10402",
            "with_original_language": "en",
            "primary_release_date.lte": TODAY,
            "vote_count.gte": 2,
            "sort_by": sort_by,
            "include_adult": "false",
        }, max_pages=25)

    # 4. Targeted TMDB searches for genres, eras, and major rock/metal/punk/goth artists
    targeted_queries = [
        # Genres, scenes & eras
        "metal evolution", "punk", "new wave", "goth", "gothic rock", "darkwave",
        "synthpop", "synthwave", "post-punk", "industrial", "heavy metal",
        "history of rock", "ages of rock", "britannia", "krautrock", "hardcore",
        "grunge", "britpop", "shoegaze", "thrash metal", "black metal",
        # Metal icons
        "ozzy osbourne", "black sabbath", "iron maiden", "di'anno", "judas priest",
        "metallica", "some kind of monster", "megadeth", "slayer", "pantera",
        "lemmy", "motorhead", "ronnie james dio", "scorpions", "rammstein",
        "slipknot", "randy rhoads", "jason becker", "sepultura", "anvil",
        "twisted sister", "type o negative", "dream theater", "marilyn manson",
        # Rock, Goth, New Wave & Punk icons
        "depeche mode", "the cure", "joy division", "nine inch nails", "ministry",
        "bon jovi", "beatles anthology", "pink floyd", "led zeppelin", "deep purple",
        "queen", "ac/dc", "rush", "alice cooper", "kiss", "david bowie",
        "ramones", "sex pistols", "the clash", "iggy pop", "green day", "nirvana",
        "foo fighters", "soundgarden", "pearl jam", "thin lizzy", "van halen",
    ]
    targeted_movies: set[int] = set()
    targeted_tv: set[int] = set()
    for q in targeted_queries:
        for page in (1, 2):
            tv_search = tmdb_get("/search/tv", {"query": q, "page": page})
            for item in tv_search.get("results", []):
                if 99 in (item.get("genre_ids") or []) and item.get("original_language") == "en":
                    targeted_tv.add(item["id"])

            mv_search = tmdb_get("/search/movie", {"query": q, "page": page})
            for item in mv_search.get("results", []):
                if 99 in (item.get("genre_ids") or []) and item.get("original_language") == "en":
                    targeted_movies.add(item["id"])

    movie_ids |= targeted_movies
    tv_ids |= targeted_tv

    trusted_movies = trakt_movies | targeted_movies
    trusted_tv = trakt_tv | targeted_tv
    return movie_ids, tv_ids, trusted_movies, trusted_tv


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
    combined = f"{title} {overview} {' '.join(keywords)}"

    effective_date = get_watchable_movie_date(details)

    if not forced_category:
        if original_lang != "en" or not effective_date:
            return None
        if 99 not in genre_ids:
            return None
        if len(overview.strip()) < 40:
            return None
        if 0 < runtime < MIN_MOVIE_RUNTIME:
            return None
        if runtime == 0 and effective_date < SIX_MONTHS_AGO:
            return None
        # Soft quality gate: Trakt-curated & targeted-artist docs pass even with 0-1 TMDB votes
        if not trusted and effective_date < ONE_YEAR_AGO and vote_count < 3:
            return None
        if is_unofficial_doc(title, overview, companies):
            return None
        if not has_music_relevance(genre_ids, keywords, combined):
            return None
        if is_pure_concert_film(title, overview):
            return None
    else:
        effective_date = effective_date or (details.get("release_date") or "")

    category = (
        forced_category
        or pin_map.get(imdb_id)
        or pin_map.get(str(tmdb_id))
        or classify_item(title, overview, keywords)
    )
    if not category:
        return None

    year = effective_date[:4] if len(effective_date) >= 4 else ""
    vote_avg = details.get("vote_average") or 0
    backdrop_path = details.get("backdrop_path")

    cat_labels = {
        "genre-era-docs": "Genres & Eras",
        "rock-docs": "Rock",
        "metal-docs": "Metal",
        "punk-docs": "Punk",
    }

    meta = {
        "id": imdb_id,
        "type": "movie",
        "name": title,
        "poster": f"{IMG_POSTER}{poster_path}",
        "posterShape": "poster",
        "description": overview,
        "releaseInfo": year,
        "runtime": f"{runtime} min" if runtime else None,
        "genres": ["Documentary", "Music", cat_labels.get(category, "Music")],
        "_release_date": effective_date,
        "_score": popularity + (vote_avg * 4),
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
    combined = f"{title} {overview} {' '.join(keywords)}"

    if not forced_category:
        if original_lang != "en":
            return None
        if status in {"Planned", "In Production", "Rumored", "Pilot"} or not first_air or first_air > TODAY:
            return None
        if num_episodes < 1:
            return None
        if 99 not in genre_ids:
            return None
        if len(overview.strip()) < 40:
            return None
        if not trusted and first_air < ONE_YEAR_AGO and vote_count < 2:
            return None
        if is_unofficial_doc(title, overview, companies):
            return None
        if not has_music_relevance(genre_ids, keywords, combined):
            return None
        if is_pure_concert_film(title, overview):
            return None

    category = (
        forced_category
        or pin_map.get(imdb_id)
        or pin_map.get(str(tmdb_id))
        or classify_item(title, overview, keywords)
    )
    if not category:
        return None

    last_air = details.get("last_air_date") or ""
    start_year = first_air[:4] if len(first_air) >= 4 else ""
    end_year = last_air[:4] if len(last_air) >= 4 else ""
    release_info = f"{start_year}-{end_year}" if start_year and end_year and start_year != end_year else start_year

    vote_avg = details.get("vote_average") or 0
    backdrop_path = details.get("backdrop_path")

    cat_labels = {
        "genre-era-docs": "Genres & Eras",
        "rock-docs": "Rock",
        "metal-docs": "Metal",
        "punk-docs": "Punk",
    }

    meta = {
        "id": imdb_id,
        "type": "series",
        "name": title,
        "poster": f"{IMG_POSTER}{poster_path}",
        "posterShape": "poster",
        "description": overview,
        "releaseInfo": release_info,
        "genres": ["Documentary", "Music", cat_labels.get(category, "Music")],
        "_release_date": first_air,
        "_score": popularity + (vote_avg * 4),
    }
    if backdrop_path:
        meta["background"] = f"{IMG_BACKDROP}{backdrop_path}"
    if vote_avg > 0:
        meta["imdbRating"] = f"{vote_avg:.1f}"

    return category, {k: v for k, v in meta.items() if v is not None}


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
    return unique


def main():
    overrides = load_overrides()
    print(f"Discovering official, released English docs from TMDB + Trakt (up to {TODAY})...")
    movie_ids, tv_ids, trusted_movies, trusted_tv = discover_candidates(overrides)
    print(f"Found {len(movie_ids)} candidate movies and {len(tv_ids)} candidate series.")

    catalogs: dict[str, list[dict]] = {
        "genre-era-docs": [],
        "rock-docs": [],
        "metal-docs": [],
        "punk-docs": [],
    }

    for entry in overrides.get("whitelist_add", []):
        mtype = entry.get("media_type")
        tid = entry.get("tmdb_id")
        if not tid and entry.get("imdb_id"):
            mtype, tid = resolve_imdb_whitelist(entry["imdb_id"])
        if not tid:
            continue

        forced_cat = entry.get("category")
        if mtype == "movie":
            res = fetch_movie_meta(int(tid), overrides, trusted=True, forced_category=forced_cat)
            if res:
                cat, meta = res
                catalogs.setdefault(cat, []).append(meta)
            movie_ids.discard(int(tid))
        elif mtype == "series":
            res = fetch_series_meta(int(tid), overrides, trusted=True, forced_category=forced_cat)
            if res:
                cat, meta = res
                catalogs.setdefault(cat, []).append(meta)
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

    out_dir = DOCS_DIR / "catalog" / CATALOG_TYPE
    out_dir.mkdir(parents=True, exist_ok=True)

    for cat_id, items in catalogs.items():
        cleaned = sort_and_clean(items)
        out_path = out_dir / f"{cat_id}.json"
        out_path.write_text(json.dumps({"metas": cleaned}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {len(cleaned)} items -> {out_path}")

    manifest = {
        "id": "org.nuvio.rockmetalpunkdocs",
        "version": "2.6.0",
        "name": "Rock, Metal & Punk Documentaries",
        "description": "Live auto-updating catalog of official, released English Genres/Eras, Rock, Metal, and Punk documentaries & docuseries.",
        "resources": ["catalog"],
        "types": ["movie", "series", "other"],
        "idPrefixes": ["tt"],
        "catalogs": [
            {"type": CATALOG_TYPE, "id": "genre-era-docs", "name": "Genres, Scenes & Eras"},
            {"type": CATALOG_TYPE, "id": "rock-docs", "name": "Rock Documentaries"},
            {"type": CATALOG_TYPE, "id": "metal-docs", "name": "Metal Documentaries"},
            {"type": CATALOG_TYPE, "id": "punk-docs", "name": "Punk Documentaries"},
        ],
    }
    manifest_path = DOCS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote manifest -> {manifest_path}")


if __name__ == "__main__":
    main()
