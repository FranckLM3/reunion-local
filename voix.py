"""Voix connues : l'outil retient la voix d'une personne nommée, avec son accord, et la propose ensuite.

Une empreinte est le vecteur que la séparation des voix calcule pour chaque voix (son centroïde).
Quand une voix reçoit un nom et que la case « avec son accord » est cochée (ou que c'est ta voix), l'empreinte
de cette réunion rejoint celle de la personne (moyenne). À la transcription suivante, chaque voix inconnue est
comparée aux voix connues par similarité cosinus : au-dessus du seuil, le nom est proposé, jamais imposé.

Une banque par modèle d'empreintes : celles de la méthode libre (TitaNet, 192 nombres) et de pyannote (256)
ne se comparent pas. ~/.config/reunion/voix.json garde les voix apprises avec pyannote (avant le 03/11) ;
voix-titanet-small.json celles de la méthode libre. Lisibles par toi seul. Par réunion : locuteurs.json.
"""

import json
import os
import re
from datetime import date
from pathlib import Path

import numpy as np

import reglages

BANQUE = Path.home() / ".config" / "reunion" / "voix.json"
PYANNOTE = "pyannote-community-1"
# Seuil de reconnaissance par modèle (évaluation du 03/11) ; « très probable » dès TRES_PROBABLE (spec Q4).
# ponytail: seuils fixes, à recaler sur 20 réunions réelles nommées à la main.
SEUILS = {"titanet-small": 0.65, PYANNOTE: 0.6}
SEUIL = float(os.environ.get("REUNION_SEUIL_VOIX", "0.65"))
TRES_PROBABLE = 0.80
GENERIQUE = re.compile(r"(Voix|Intervenant) \d+")


def niveau(confiance: float, seuil: float | None = None) -> str | None:
    return "très probable" if confiance >= TRES_PROBABLE else "probable" if confiance >= (seuil or SEUIL) else None


def fichier_banque(modele: str = PYANNOTE) -> Path:
    return BANQUE if modele == PYANNOTE else BANQUE.with_name(f"voix-{modele}.json")


def lire_banque(modele: str = PYANNOTE) -> dict:
    f = fichier_banque(modele)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def cosinus(a, b) -> float:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if a.shape != b.shape:  # empreintes de deux modèles différents : jamais la même voix
        return 0.0
    n = np.linalg.norm(a) * np.linalg.norm(b)
    return float(a @ b / n) if n else 0.0


def reconnaitre(empreintes: dict[str, list], banque: dict, seuil: float | None = None) -> dict[str, tuple[str, float]]:
    """{libellé provisoire: (nom connu, similarité)}. Les meilleures paires d'abord, un nom ne sert qu'une fois."""
    seuil = seuil or SEUIL
    paires = sorted(((cosinus(e, v["emb"]), lbl, nom)
                     for lbl, e in empreintes.items() for nom, v in banque.items()), reverse=True)
    choix, pris = {}, set()
    for score, lbl, nom in paires:
        if score < seuil:
            break
        if lbl not in choix and nom not in pris:
            choix[lbl] = (nom, score)
            pris.add(nom)
    return choix


def apprendre(nom: str, emb: list, banque: dict) -> dict:
    """Nouvelle banque où l'empreinte de `nom` intègre `emb` (moyenne des réunions), datée du jour."""
    v = banque.get(nom)
    if v and len(v["emb"]) != len(emb):  # jamais deux modèles d'empreintes mélangés
        raise ValueError(f"empreinte de {len(emb)} nombres pour une voix connue de {len(v['emb'])}")
    if v:
        n = v["n"]
        emb = list((np.asarray(v["emb"]) * n + np.asarray(emb)) / (n + 1))
    return {**banque, nom: {"emb": [float(x) for x in emb], "n": (v["n"] + 1) if v else 1,
                            "vu": date.today().isoformat()}}


def ecrire_banque(banque: dict, modele: str = PYANNOTE) -> None:
    f = fichier_banque(modele)
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    reglages.ecrire_atomique(f, json.dumps(banque))  # fichier temporaire créé lisible par toi seul (0600)
    os.chmod(f, 0o600)


def retenir(dossier: Path, ancien: str, nouveau: str, apprendre_voix: bool = True, modele: str = PYANNOTE) -> None:
    """Après un renommage : l'empreinte suit le nom dans la réunion ; elle ne nourrit la banque des voix connues
    qu'avec `apprendre_voix` (case « avec son accord », ou ta propre voix)."""
    f = dossier / "locuteurs.json"
    if not f.exists():
        return
    loc = json.loads(f.read_text(encoding="utf-8"))
    # Transcriptions d'avant le 03/11 : « Intervenant N » s'affiche « Voix N ».
    emb = loc.pop(ancien, None) or loc.pop(ancien.replace("Voix ", "Intervenant ", 1), None)
    if emb is None:
        return
    # Une voix chuchotée n'a pas de timbre : son empreinte fausserait celle de la voix normale (13/10).
    options = dossier / "options.json"
    chuchote = options.exists() and json.loads(options.read_text(encoding="utf-8")).get("chuchote", False)
    # Banque calculée d'abord : une empreinte refusée (autre modèle) ne laisse rien à moitié écrit.
    banque = apprendre(nouveau, emb, lire_banque(modele)) \
        if apprendre_voix and not GENERIQUE.fullmatch(nouveau) and not chuchote else None
    reglages.ecrire_atomique(f, json.dumps({**loc, nouveau: emb}))
    if banque is not None:
        ecrire_banque(banque, modele)
