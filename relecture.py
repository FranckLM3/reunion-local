"""Relecture d'une réunion : voix et noms proposés, notes horodatées, recherche, export.

Fonctions sur les fichiers d'un dossier de réunion, sans état : le serveur les appelle sous son verrou.
"""

import io
import json
import re
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import reglages
import voix

# Libellés posés par l'outil quand une voix n'a pas de nom de personne.
LIBELLES_FIXES = {"Moi", "Les autres", "Autres", "Micro", "Salle", "Inconnu"}


def generique(loc: str) -> bool:
    return bool(voix.GENERIQUE.fullmatch(loc)) or loc in LIBELLES_FIXES


def ancien_libelle(loc: str) -> str:
    """Transcriptions d'avant le 03/11 : « Intervenant N » devient « Voix N », « Autres » « Les autres »."""
    return "Les autres" if loc == "Autres" else re.sub(r"^Intervenant (\d+)$", r"Voix \1", loc)


def normaliser(t: dict) -> dict:
    """Transcription lisible par la page, quel que soit son âge : libellés d'aujourd'hui, numéro de chaque voix
    (ordre de première prise de parole, gardé dans le fichier depuis le 03/11), propositions et séparation."""
    blocs = [{**b, "loc": ancien_libelle(b["loc"])} for b in t.get("blocs", [])]
    ordre = list(dict.fromkeys(b["loc"] for b in blocs))
    gardees = {ancien_libelle(k): v for k, v in (t.get("voix") or {}).items() if ancien_libelle(k) in ordre}
    if not t.get("voix"):  # ancien format : « Intervenant N » garde son numéro
        pris = set()
        for loc in ordre:
            if (m := re.fullmatch(r"Voix (\d+)", loc)) and int(m[1]) not in pris:
                gardees[loc] = {"numero": int(m[1]), "source": None}
                pris.add(int(m[1]))
    suivant = max((v["numero"] for v in gardees.values()), default=0)
    for loc in ordre:  # voix créée par une réattribution de ligne, ou ancien format
        if loc not in gardees:
            suivant += 1
            gardees[loc] = {"numero": suivant, "source": None}
    separation = t.get("separation")
    if separation is None:
        separation = not {"Les autres", "Micro"} & set(ordre)
    # Empreintes d'avant le 03/11 : pyannote, seule méthode de l'époque.
    return {**t, "blocs": blocs, "voix": gardees, "propositions": t.get("propositions") or {}, "separation": separation,
            "empreintes": t.get("empreintes") or voix.PYANNOTE}


def heure(s: float) -> str:
    """Heure courte d'une ligne : « 12:04 », « 1:02:05 » au-delà d'une heure."""
    s = int(max(s, 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- notes

def lire_notes(d: Path) -> list[dict]:
    """Notes valides seulement ; un fichier illisible est mis de côté en notes.json.bak, jamais perdu."""
    f = d / "notes.json"
    try:
        notes = json.loads(f.read_text(encoding="utf-8"))
    except OSError:
        return []
    except ValueError:
        try:
            f.replace(f.with_name("notes.json.bak"))
        except OSError:
            pass
        return []
    if not isinstance(notes, list):
        return []
    valides = [n for n in notes if isinstance(n, dict) and isinstance(n.get("id"), str)
               and isinstance(n.get("texte"), str) and isinstance(n.get("t_s"), (int, float))]
    return sorted(valides, key=lambda n: n["t_s"])


def _instant(t_s, duree: float | None) -> float | None:
    if isinstance(t_s, bool) or not isinstance(t_s, (int, float)):
        return None
    return round(float(t_s), 1) if 0 <= t_s <= (duree if duree is not None else float("inf")) else None


def modifier_notes(d: Path, o: dict, duree: float | None) -> tuple[dict | None, str | None]:
    """(note concernée, code d'erreur). Actions : ajouter, modifier, deplacer, supprimer (spec NT)."""
    notes = lire_notes(d)
    action, ident = o.get("action"), o.get("id")
    texte = o.get("texte")
    if action in ("ajouter", "modifier"):
        if not isinstance(texte, str) or not texte.strip() or len(texte) > 2000:
            return None, "note_vide"
        texte = texte.strip()
    if action in ("ajouter", "deplacer"):
        t_s = _instant(o.get("t_s"), duree)
        if t_s is None:
            return None, "heure_hors_reunion"
    if action == "ajouter":
        # L'identifiant peut venir de la page : « Annuler » après une suppression remet la même note.
        ident = ident if isinstance(ident, str) and re.fullmatch(r"\w{1,32}", ident) else uuid.uuid4().hex[:8]
        if any(n["id"] == ident for n in notes):
            return None, "invalide"
        note = {"id": ident, "t_s": t_s, "texte": texte}
        notes.append(note)
    else:
        note = next((n for n in notes if n.get("id") == ident), None)
        if note is None:
            return None, "note_introuvable"
        if action == "modifier":
            note["texte"] = texte
        elif action == "deplacer":
            note["t_s"] = t_s
        elif action == "supprimer":
            notes.remove(note)
        else:
            return None, "invalide"
    reglages.ecrire_atomique(d / "notes.json", json.dumps(sorted(notes, key=lambda n: n["t_s"]), ensure_ascii=False,
                                                          indent=2))
    return note, None


# ---------------------------------------------------------------- voix et noms

def voix_de(t: dict) -> list[dict]:
    """Une entrée par voix, dans l'ordre des numéros : temps de parole, extrait de 5 s et proposition."""
    sortie = []
    for loc, v in sorted(t["voix"].items(), key=lambda kv: kv[1]["numero"]):
        blocs = [b for b in t["blocs"] if b["loc"] == loc]
        longs = sorted((b for b in blocs if b["fin"] - b["debut"] >= 5), key=lambda b: b["fin"] - b["debut"])
        typique = longs[len(longs) // 2] if longs else max(blocs, key=lambda b: b["fin"] - b["debut"], default=None)
        p = t["propositions"].get(loc)
        sortie.append({
            "loc": loc, "numero": v["numero"], "source": v.get("source"),
            "temps_s": round(sum(b["fin"] - b["debut"] for b in blocs)),
            "extrait": typique and {"debut": typique["debut"], "fin": min(typique["fin"], typique["debut"] + 5)},
            "proposition": p and {**p, "niveau": voix.niveau(p["confiance"], voix.SEUILS.get(t["empreintes"]))},
        })
    return sortie


def suggestions(liste: list[dict], participants: list[str], moi: str, connues: list[str],
                passes: list[str]) -> dict[str, list[dict]]:
    """Noms proposés pour chaque voix, par ordre de confiance (spec VX-19 à VX-24) : vous (voix du micro), la voix
    connue reconnue, les participants sans voix, les noms déjà portés ou proposés ailleurs, les voix connues, les
    noms des réunions passées. Aucun nom n'est interdit."""
    portes = {v["loc"]: v["numero"] for v in liste if not generique(v["loc"])}
    proposes = {v["proposition"]["nom"]: v["numero"] for v in liste if v["proposition"]}
    sortie = {}
    for v in liste:
        vus, s = {v["loc"]}, []

        def ajouter(nom, source, deja=None, proposee=False, confiance=None):
            if nom and nom not in vus:
                vus.add(nom)
                s.append({"nom": nom, "source": source, "deja": deja and f"Voix {deja}", "proposee": proposee,
                          "confiance": confiance})

        if v["source"] == "micro":
            ajouter(moi, "vous")
        vus.add(moi)  # le propriétaire n'est proposé que pour une voix du micro
        if v["proposition"]:
            ajouter(v["proposition"]["nom"], "connue", confiance=v["proposition"]["confiance"])
        for nom in sorted(participants, key=str.casefold):
            if nom not in portes and nom not in proposes:
                ajouter(nom, "participant")
        ailleurs = {**{n: (k, True) for n, k in proposes.items() if k != v["numero"]},
                    **{n: (k, False) for n, k in portes.items() if k != v["numero"]}}
        for nom in sorted(ailleurs, key=str.casefold):
            source = "participant" if nom in participants else "connue" if nom in connues else "passe"
            ajouter(nom, source, deja=ailleurs[nom][0], proposee=ailleurs[nom][1])
        for nom in sorted(connues, key=str.casefold):
            ajouter(nom, "connue")
        for nom in sorted(passes, key=str.casefold):
            if not generique(nom):
                ajouter(nom, "passe")
        sortie[v["loc"]] = s
    return sortie


# ---------------------------------------------------------------- recherche et export

def correspond(q: str):
    """Filtre d'une recherche : expression exacte entre guillemets, sinon tous les mots, sans tenir compte de
    la casse (spec LI-08, LI-09)."""
    q = q.strip()
    exacte = len(q) > 1 and q[0] in "\"«“" and q[-1] in "\"»”"
    mots = [q.strip("\"«»“” ").lower()] if exacte else q.lower().split()
    return lambda texte: bool(mots) and all(m in texte.lower() for m in mots)


def passages(t: dict | None, notes: list[dict], q: str) -> tuple[int, int]:
    """(lignes, notes) qui contiennent la recherche."""
    ok = correspond(q)
    return sum(ok(b["texte"]) for b in (t or {}).get("blocs", [])), sum(ok(n["texte"]) for n in notes)


def lignes_texte(t: dict | None, notes: list[dict]) -> list[str]:
    """Texte exporté : une ligne par prise de parole, chaque note à sa place (spec NT-15, VX-06)."""
    t = t or {"blocs": [], "propositions": {}}
    elements = [(b["debut"], 0, f"[{heure(b['debut'])}] "
                 + (f"{t['propositions'][b['loc']]['nom']} ?" if b["loc"] in t["propositions"] else b["loc"])
                 + f" : {b['texte']}") for b in t["blocs"]]
    elements += [(n["t_s"], 1, f"[{heure(n['t_s'])}] Note : {n['texte']}") for n in notes]
    return [ligne for *_, ligne in sorted(elements, key=lambda e: e[:2])]


_INVISIBLES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def docx(titre: str, lignes: list[str]) -> bytes:
    """Document Word minimal (un paragraphe par ligne), sans bibliothèque : un .docx est un zip de XML."""
    def p(texte, gras=False):
        rpr = '<w:rPr><w:b/><w:sz w:val="32"/></w:rPr>' if gras else ""
        return f'<w:p><w:r>{rpr}<w:t xml:space="preserve">{escape(_INVISIBLES.sub("", texte))}</w:t></w:r></w:p>'

    tete = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    parties = {
        "[Content_Types].xml": tete + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": tete + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": tete + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/'
        'main"><w:body>' + p(titre, True) + "".join(p(l) for l in lignes) + "</w:body></w:document>",
    }
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for nom, contenu in parties.items():
            z.writestr(nom, contenu)
    return tampon.getvalue()
