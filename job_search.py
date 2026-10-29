"""
Modulo di ricerca offerte di lavoro.
Fonti: Indeed RSS, InfoJobs RSS, Jooble API.
Usa httpx (già dipendenza di groq) per gestire SSL su macOS.
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


def _get(url: str) -> bytes:
    """HTTP GET tramite httpx — gestisce SSL correttamente su macOS/Python 3.14."""
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


def fetch_indeed(query: str, location: str = "Italia") -> list[dict]:
    q = urllib.parse.quote_plus(query)
    l = urllib.parse.quote_plus(location)
    # Indeed espone RSS per query di ricerca lavoro
    url = f"https://it.indeed.com/rss?q={q}&l={l}&sort=date&fromage=14"
    return _parse_rss(url, "Indeed")


def fetch_infojobs(query: str) -> list[dict]:
    q = urllib.parse.quote_plus(query)
    # InfoJobs RSS pubblico
    url = f"https://www.infojobs.it/offerte-lavoro/rss.ashx?keyword={q}&provinceIds=0"
    return _parse_rss(url, "InfoJobs")


def fetch_remoteok(query: str) -> list[dict]:
    """RemoteOK API pubblica — solo offerte remote, nessuna auth richiesta."""
    try:
        tag = urllib.parse.quote_plus(query.split()[0].lower())
        url = f"https://remoteok.com/api?tag={tag}"
        content = _get(url)
        data = json.loads(content)
        results = []
        for job in data:
            if not isinstance(job, dict) or "id" not in job:
                continue  # salta l'elemento "legal" iniziale
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
        log.info("RemoteOK: %d offerte per tag '%s'", len(results), tag)
        return results[:10]
    except Exception as e:
        log.warning("RemoteOK error: %s", e)
        return []


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
            r = client.post(
                url,
                content=payload,
                headers={**HEADERS, "Content-Type": "application/json"},
            )
        results = []
        for job in r.json().get("jobs", []):
            results.append({
                "title": job.get("title", "").strip(),
                "company": job.get("company", ""),
                "description": job.get("snippet", "")[:1500],
                "url": job.get("link", ""),
                "source": "Jooble",
            })
        return results
    except Exception:
        return []


def linkedin_search_url(role: str, mode: str = "") -> str:
    """Genera un URL di ricerca LinkedIn precompilato basato su ruolo e modalità."""
    q = urllib.parse.quote_plus(role)
    loc = urllib.parse.quote_plus("Italy")
    mode_lower = mode.lower()
    if "remoto" in mode_lower or "remote" in mode_lower:
        work_type = "&f_WT=2"      # Remote
    elif "ibrido" in mode_lower or "hybrid" in mode_lower:
        work_type = "&f_WT=3"      # Hybrid
    else:
        work_type = ""             # Tutti
    # Offerte dell'ultima settimana, ordinate per data
    return (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={q}&location={loc}{work_type}&f_TPR=r604800&sortBy=DD"
    )


def deduplicate(offers: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for o in offers:
        key = o.get("title", "").lower()[:60]
        if key and key not in seen:
            seen.add(key)
            unique.append(o)
    return unique


def search_all(query: str, location: str, jooble_key: str) -> list[dict]:
    results: list[dict] = []
    results.extend(fetch_indeed(query, location))
    results.extend(fetch_infojobs(query))
    results.extend(fetch_jooble(query, location, jooble_key))
    results.extend(fetch_remoteok(query))
    return deduplicate(results)
