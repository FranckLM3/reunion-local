"""Réglages de l'outil, dans un seul fichier local : ~/.config/reunion/reglages.json.

Modifiés depuis l'écran Configuration de l'interface ; lus par serveur.py, transcribe.py et capture.py.
Le jeton Hugging Face et l'adresse de l'agenda restent à part, dans des fichiers lisibles par toi seul.
"""

import json
import locale
import os
import sys
import tempfile
import time
from pathlib import Path

DOSSIER_CONF = Path.home() / ".config" / "reunion"
FICHIER = DOSSIER_CONF / "reglages.json"


CODES_LANGUE = {"fr", "en", "de", "es", "it", "nl", "pt"}


def langue_systeme() -> str:
    """Langue du système (« fr » pour fr_FR), français si elle est illisible ou non proposée. Sous Windows, d'après
    la langue d'affichage du compte. Ne sert qu'au premier lancement (Q6)."""
    code = None
    if sys.platform == "win32":
        try:
            import ctypes

            code = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage())
        except (AttributeError, OSError):
            code = None
    code = (code or locale.getlocale()[0] or os.environ.get("LANG") or "fr")[:2].lower()
    return code if code in CODES_LANGUE else "fr"


DEFAUTS = {
    "dossier": "",               # vide : ~/Reunions (ou $REUNION_DIR)
    "micro": "",                 # identifiant du périphérique, vide : celui par défaut (hors Mac)
    "sortie": "",                # sortie audio captée en boucle, vide : celle par défaut (hors Mac)
    "moteur": "auto",            # auto, mlx (Mac Apple Silicon) ou faster (faster-whisper)
    "modele": "",                # vide : la qualité conseillée pour cette machine (transcribe.machine)
    "langue": "fr",              # code ISO, ou auto ; la langue du système n'est prise qu'au premier lancement
    "locuteurs": True,           # séparation des voix
    "moi": "",                   # nom de la piste micro, vide : prénom du compte
    "accueil_fini": None,        # None : vrai si le fichier existait déjà (installation antérieure à l'accueil)
    "accueil_etape": 1,          # étape atteinte de la suite d'accueil, pour la reprendre
    "etat_micro": "",            # "ok" une fois le micro entendu, "refuse" s'il a été refusé ou passé
    "etat_son": "",              # même chose pour le son de l'ordinateur
    "direct": None,              # brouillon pendant l'enregistrement ; None : selon la machine
    "vitesses": {},              # modèle -> 5 dernières mesures de (temps de calcul / durée de l'audio)
    "vitesses_etapes": {},       # étape (« 2:modèle » pour Whisper) -> 5 dernières mesures, même rapport
    "ressources": 100,           # part des cœurs laissée à la transcription : 10 à 100, par pas de 10 (ressources.py)
    "priorite_basse": False,     # transcription en priorité basse : l'ordinateur reste réactif
    "telecharges": [],           # dépôts téléchargés par Réunion elle-même (les seuls qu'elle efface)
}


def installation_existante() -> bool:
    """Réunion déjà utilisée avant la suite d'accueil : réglages, voix connues ou dossier des réunions."""
    return FICHIER.exists() or (FICHIER.parent / "voix.json").exists() or (Path.home() / "Reunions").exists()


def lire() -> dict:
    try:
        sur_disque = json.loads(FICHIER.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # absent ou abîmé : valeurs par défaut, l'outil marche quand même
        sur_disque = {}
    if not isinstance(sur_disque, dict):
        sur_disque = {}
    r = {**DEFAUTS, **{k: v for k, v in sur_disque.items() if k in DEFAUTS}}
    if r["accueil_fini"] is None:  # réglages d'avant la suite d'accueil : l'outil était déjà installé
        r["accueil_fini"] = installation_existante()
    if "modele" not in sur_disque and r["accueil_fini"]:  # défaut d'avant le 03/11, inchangé pour ces installations
        r["modele"] = "large-v3-turbo" if sys.platform == "darwin" else "small"
    return r


def ecrire_atomique(fichier: Path, texte: str) -> None:
    """Fichier temporaire (nom unique : deux écritures simultanées ne se mélangent pas) puis remplacement :
    une écriture coupée ne laisse jamais un fichier à moitié écrit. Sous Windows, le remplacement échoue
    si la cible est lue à cet instant : cinq essais espacés de 0,1 s."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=fichier.parent, prefix=fichier.name + ".",
                                     suffix=".tmp", delete=False) as f:
        f.write(texte)
    for essai in range(5):
        try:
            os.replace(f.name, fichier)
            return
        except PermissionError:
            if essai == 4:
                Path(f.name).unlink(missing_ok=True)
                raise
            time.sleep(0.1)


def ecrire(r: dict) -> None:
    FICHIER.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ecrire_atomique(FICHIER, json.dumps({k: r[k] for k in DEFAUTS if k in r}, ensure_ascii=False, indent=2))


def ecrire_secret(fichier: Path, valeur: str) -> None:
    """Fichier créé d'emblée lisible par toi seul (jeton, adresse d'agenda)."""
    fichier.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(fichier, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(valeur)
    os.chmod(fichier, 0o600)  # fichier déjà existant avec d'autres droits
