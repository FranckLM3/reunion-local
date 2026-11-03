"""Titre et invités de la réunion en cours, d'après l'adresse secrète iCal de Google Agenda.

Lecture seule : on télécharge le flux depuis Google, rien n'est envoyé. L'adresse est rangée
dans ~/.config/reunion/agenda_ics (lisible par toi seul), car elle donne accès à tout l'agenda.
"""

import urllib.request
from datetime import datetime, timedelta

import reglages

FICHIER_URL = reglages.DOSSIER_CONF / "agenda_ics"
PREFIXE_URL = "https://calendar.google.com/calendar/ical/"
MARGE = timedelta(minutes=15)  # on démarre souvent l'enregistrement un peu avant ou après l'heure


def url_agenda() -> str | None:
    if FICHIER_URL.exists():
        return FICHIER_URL.read_text(encoding="utf-8").strip() or None
    return None


def enregistrer_url(url: str) -> str | None:
    """Range l'adresse ; vide, la retire (« Débrancher ») ; renvoie un message d'erreur si elle n'a pas la forme
    attendue."""
    url = url.strip()
    if not url:
        FICHIER_URL.unlink(missing_ok=True)
        return None
    if not (url.startswith(PREFIXE_URL) and url.endswith(".ics")):
        return f"L'adresse doit commencer par {PREFIXE_URL} et finir par .ics"
    # L'adresse ouvre tout l'agenda : elle ne doit jamais être lisible par un autre compte.
    reglages.ecrire_secret(FICHIER_URL, url)
    return None


def _liste(valeur) -> list:
    if valeur is None:
        return []
    return valeur if isinstance(valeur, list) else [valeur]


def _a_un_meet(ev) -> bool:
    texte = " ".join(str(ev.get(k, "")) for k in ("X-GOOGLE-CONFERENCE", "LOCATION", "DESCRIPTION"))
    return "meet.google.com" in texte


def evenement(ics: bytes, debut: datetime) -> dict | None:
    """L'événement le plus proche de `debut` (heure locale), avec ses invités hors toi et hors salles."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    moi = str(cal.get("X-WR-CALNAME", "")).lower()
    debut = debut.astimezone()
    candidats = [
        ev for ev in recurring_ical_events.of(cal).between(debut - MARGE, debut + MARGE)
        if isinstance(ev.get("DTSTART").dt, datetime)  # pas les journées entières
    ]
    if not candidats:
        return None
    ev = min(candidats, key=lambda e: (not _a_un_meet(e), abs(e.get("DTSTART").dt - debut)))

    noms = []
    for a in _liste(ev.get("ATTENDEE")):
        p = a.params
        courriel = str(a).removeprefix("mailto:").lower()
        if p.get("CUTYPE") in ("RESOURCE", "ROOM") or p.get("PARTSTAT") == "DECLINED" or courriel == moi:
            continue
        nom = str(p.get("CN") or courriel)
        noms.append(courriel.split("@")[0] if nom.lower() == courriel else nom)
    return {"titre": str(ev.get("SUMMARY", "")).strip(), "noms": sorted(set(noms))}


def participants(debut: datetime) -> dict | None:
    url = url_agenda()
    if not url:
        return None
    with urllib.request.urlopen(url, timeout=10) as r:
        return evenement(r.read(), debut)
