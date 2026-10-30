"""
Modulo di ricerca offerte di lavoro.
Fonti: Indeed, InfoJobs, Jooble, RemoteOK, Adzuna, Talent.com,
       Remotive, We Work Remotely, Monster, LinkedIn (account personale).
Usa httpx per gestire SSL su macOS/Python 3.14.
"""

import json
import logging
import urllib.parse
import feedparser
import httpx

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
TIMEOUT = 15

# Cache client LinkedIn per-utente (keyed by email)
_linkedin_clients: dict = {}


# ---------------------------------------------------------------------------
# Helpers HTTP
# ---------------------------------------------------------------------------

def _get(url: str) -> bytes:
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        r = client.get(url, headers=HEADERS)
        log.info("GET %s → %d (%d bytes)", url, r.status_code, len(r.content))
        r.raise_for_status()
        return r.content


def _parse_rss(url: str, source_name: str) -> list[dict]:
    try:
        content = _get(url)
        feed = feedparser.parse(content)
        log.info("%s: %d entry nel feed", source_name, len(feed.entries))
        results = []
        for entry in feed.entries:
            results.append({
                "title": entry.get("title", "").strip(),
                "company": entry.get("author", ""),
                "description": entry.get("summary", "")[:1500],
                "url": entry.get("link", ""),
                "source": source_name,
            })
        return results
    except Exception as e:
        log.warning("%s RSS error: %s", source_name, e)
        return []


# ---------------------------------------------------------------------------
# Sorgenti
# ---------------------------------------------------------------------------

def fetch_indeed(query: str, location: str = "Italia") -> list[dict]:
    q = urllib.parse.quote_plus(query)
    l = urllib.parse.quote_plus(location)
    url = f"https://it.indeed.com/rss?q={q}&l={l}&sort=date&fromage=14"
    return _parse_rss(url, "Indeed")


def fetch_infojobs(query: str) -> list[dict]:
    q = urllib.parse.quote_plus(query)
    url = f"https://www.infojobs.it/offerte-lavoro/rss.ashx?keyword={q}&provinceIds=0"
    return _parse_rss(url, "InfoJobs")


def fetch_jooble(query: str, location: str, api_key: str) -> list[dict]:
    if not api_key:
        return []
    try:
        url = f"https://it.jooble.org/api/{api_key}"
        payload = json.dumps({
            "keywords": query,
            "location": location,
            "page": 1,
            "resultonpage": 10,
        }).encode()
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(url, content=payload,
                            headers={**HEADERS, "Content-Type": "application/json"})
        results = []
        for job in r.json().get("jobs", []):
            results.append({
                "title": job.get("title", "").strip(),
                "company": job.get("company", ""),
                "description": job.get("snippet", "")[:1500],
                "url": job.get("link", ""),
                "source": "Jooble",
            })
        log.info("Jooble: %d offerte per '%s'", len(results), query)
        return results
    except Exception as e:
        log.warning("Jooble error: %s", e)
        return []


def fetch_remoteok(query: str) -> list[dict]:
    try:
        tag = urllib.parse.quote_plus(query.split()[0].lower())
        url = f"https://remoteok.com/api?tag={tag}"
        content = _get(url)
        data = json.loads(content)
        results = []
        for job in data:
            if not isinstance(job, dict) or "id" not in job:
                continue
            sal_min = job.get("salary_min") or 0
            sal_max = job.get("salary_max") or 0
            sal = f"{sal_min:,}–{sal_max:,}$".replace(",", ".") if sal_min else ""
            results.append({
                "title": job.get("position", "").strip(),
                "company": job.get("company", ""),
                "description": f"{job.get('description', '')[:1200]} {sal}".strip(),
                "url": job.get("url", f"https://remoteok.com/remote-jobs/{job.get('id','')}"),
                "source": "RemoteOK",
            })
        log.info("RemoteOK: %d offerte per '%s'", len(results), query)
        return results[:10]
    except Exception as e:
        log.warning("RemoteOK error: %s", e)
        return []


def fetch_adzuna(query: str, location: str, app_id: str, app_key: str) -> list[dict]:
    if not app_id or not app_key:
        return []
    try:
        q = urllib.parse.quote_plus(query)
        loc = urllib.parse.quote_plus(location or "Italy")
        url = (
            f"https://api.adzuna.com/v1/api/jobs/it/search/1"
            f"?app_id={app_id}&app_key={app_key}"
            f"&results_per_page=10&what={q}&where={loc}&sort_by=date"
        )
        data = json.loads(_get(url))
        results = []
        for job in data.get("results", []):
            results.append({
                "title": job.get("title", "").strip(),
                "company": job.get("company", {}).get("display_name", ""),
                "description": job.get("description", "")[:1500],
                "url": job.get("redirect_url", ""),
                "source": "Adzuna",
            })
        log.info("Adzuna: %d offerte per '%s'", len(results), query)
        return results
    except Exception as e:
        log.warning("Adzuna error: %s", e)
        return []


def fetch_talent(query: str, location: str = "Italy") -> list[dict]:
    q = urllib.parse.quote_plus(query)
    l = urllib.parse.quote_plus(location)
    url = f"https://it.talent.com/rss?k={q}&l={l}"
    return _parse_rss(url, "Talent.com")


def fetch_remotive(query: str) -> list[dict]:
    try:
        q = urllib.parse.quote_plus(query)
        url = f"https://remotive.com/api/remote-jobs?search={q}&limit=10"
        data = json.loads(_get(url))
        results = []
        for job in data.get("jobs", []):
            results.append({
                "title": job.get("title", "").strip(),
                "company": job.get("company_name", ""),
                "description": job.get("description", "")[:1500],
                "url": job.get("url", ""),
                "source": "Remotive",
            })
        log.info("Remotive: %d offerte per '%s'", len(results), query)
        return results
    except Exception as e:
        log.warning("Remotive error: %s", e)
        return []


def fetch_weworkremotely(query: str) -> list[dict]:
    try:
        content = _get("https://weworkremotely.com/remote-jobs.rss")
        feed = feedparser.parse(content)
        words = query.lower().split()
        results = []
        for entry in feed.entries:
            title = entry.get("title", "")
            if any(w in title.lower() for w in words):
                results.append({
                    "title": title.strip(),
                    "company": entry.get("author", ""),
                    "description": entry.get("summary", "")[:1500],
                    "url": entry.get("link", ""),
                    "source": "WeWorkRemotely",
                })
        log.info("WeWorkRemotely: %d offerte per '%s'", len(results), query)
        return results[:8]
    except Exception as e:
        log.warning("WeWorkRemotely error: %s", e)
        return []


def fetch_monster(query: str) -> list[dict]:
    q = urllib.parse.quote_plus(query)
    url = f"https://www.monster.it/rss/l-italia-q-{q}.aspx"
    return _parse_rss(url, "Monster")


def fetch_linkedin(query: str, location: str, email: str, password: str) -> list[dict]:
    """Ricerca offerte LinkedIn tramite account personale (API non ufficiale)."""
    if not email or not password:
        return []
    try:
        if email not in _linkedin_clients:
            from linkedin_api import Linkedin
            _linkedin_clients[email] = Linkedin(email, password, authenticate=True)
            log.info("LinkedIn: login effettuato come %s", email)

        jobs = _linkedin_clients[email].search_jobs(
            keywords=query,
            location_name=location or "Italy",
            limit=10,
        )
        results = []
        for job in jobs:
            entity = job.get("entityUrn", "")
            job_id = entity.split(":")[-1] if entity else ""
            title = job.get("title", "").strip()
            company = job.get("companyName", "")
            desc = job.get("description", "")
            if isinstance(desc, dict):
                desc = desc.get("text", "")
            results.append({
                "title": title,
                "company": company,
                "description": str(desc)[:1500],
                "url": f"https://www.linkedin.com/jobs/view/{job_id}" if job_id else "",
                "source": "LinkedIn",
            })
        log.info("LinkedIn: %d offerte per '%s'", len(results), query)
        return results
    except Exception as e:
        log.warning("LinkedIn error: %s", e)
        _linkedin_clients.pop(email, None)  # reset per riprovare al prossimo giro
        return []


# ---------------------------------------------------------------------------
# Osservatori mercato del lavoro italiano
# ---------------------------------------------------------------------------

def fetch_market_insights(role: str) -> list[dict]:
    """Articoli e dati di mercato da osservatori italiani (ANSA, Sole24Ore, Excelsior)."""
    sources = [
        ("ANSA Lavoro",    "https://www.ansa.it/sito/notizie/economia/lavoro/lavoro_rss.xml"),
        ("Il Sole 24 Ore", "https://www.ilsole24ore.com/rss/lavoro.xml"),
        ("Excelsior",      "https://excelsior.unioncamere.net/rss"),
    ]
    market_terms = {"mercato lavoro", "occupazione", "stipendi", "retribuz", "assunzioni", "lavoro", "skill"}
    keywords = [w.lower() for w in role.split() if len(w) > 3]
    results = []

    for source_name, url in sources:
        try:
            content = _get(url)
            feed = feedparser.parse(content)
            for entry in feed.entries[:20]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                combined = (title + " " + summary).lower()
                role_match = any(kw in combined for kw in keywords)
                market_match = any(t in combined for t in market_terms)
                if not role_match and not market_match:
                    continue
                results.append({
                    "title": title.strip(),
                    "description": summary[:400],
                    "url": entry.get("link", ""),
                    "source": source_name,
                })
        except Exception as e:
            log.warning("%s insights error: %s", source_name, e)

    log.info("Market insights: %d articoli per '%s'", len(results), role)
    return results[:10]


# ---------------------------------------------------------------------------
# Helpers pubblici
# ---------------------------------------------------------------------------

def linkedin_search_url(role: str, mode: str = "") -> str:
    """URL di ricerca LinkedIn precompilato (fallback senza login)."""
    q = urllib.parse.quote_plus(role)
    loc = urllib.parse.quote_plus("Italy")
    mode_lower = mode.lower()
    if "remoto" in mode_lower or "remote" in mode_lower:
        wt = "&f_WT=2"
    elif "ibrido" in mode_lower or "hybrid" in mode_lower:
        wt = "&f_WT=3"
    else:
        wt = ""
    return f"https://www.linkedin.com/jobs/search/?keywords={q}&location={loc}{wt}&f_TPR=r604800&sortBy=DD"


def deduplicate(offers: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for o in offers:
        key = o.get("title", "").lower()[:60]
        if key and key not in seen:
            seen.add(key)
            unique.append(o)
    return unique
