#!/usr/bin/env python3
"""Interface locale de reunion : http://127.0.0.1:8765.

Bibliothèque standard uniquement. N'écoute que sur 127.0.0.1 et refuse les requêtes
venues d'un autre site (en-têtes Host et Origin), pour qu'une page web ne puisse pas
lancer un enregistrement à ton insu.

Toute erreur renvoyée à la page a la forme {"erreur": {code, cause, garde, journal}} : une cause en mots
simples, ce qui est gardé, et le journal où chercher. Aucun message de programme n'est transmis tel quel.
"""

import errno
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from array import array
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

import agenda
import pause
import reglages
import ressources
import relecture
import separation
import transcribe
import voix

ICI = Path(__file__).resolve().parent
MAC = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"
VERSION = "2.0"
# Sous Windows, lancé sans console (icône de la zone de notification) : aucun processus enfant n'ouvre de fenêtre noire.
SANS_FENETRE = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}
DEPOT = "FranckLM3/reunion-local"  # page du projet, contactée seulement sur « Rechercher une mise à jour »
# Python de l'environnement installé (.venv/bin sur Mac et Linux, .venv\Scripts sous Windows).
PY = next((str(p) for p in (ICI / ".venv" / "bin" / "python", ICI / ".venv" / "Scripts" / "python.exe")
           if p.exists()), sys.executable)
VOCABULAIRE = ICI / "vocabulaire.txt"
ESSAI_VITESSE = ICI / "essai-vitesse.wav"  # extrait de 30 s pour mesurer la vitesse à l'installation (PL-11)
# Journal de l'outil (spec ER) ; celui d'une réunion est journal.txt dans son dossier.
LOG = (Path.home() / "Library" / "Logs" / "Réunion" / "reunion.log" if MAC else
       Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Réunion" / "journaux" / "reunion.log" if WINDOWS else
       Path.home() / ".local" / "state" / "reunion" / "reunion.log")


def dossier_reunions() -> Path:
    return Path(os.environ.get("REUNION_DIR") or reglages.lire()["dossier"] or Path.home() / "Reunions").expanduser()


BASE = dossier_reunions()
PORT = 8765  # fixé aussi dans ReunionApp.swift et zone.py
HOTES = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
STATIQUES = {"/": ("index.html", "text/html; charset=utf-8")}

# Jeton de session, tiré à chaque lancement (S2) : passé dans l'adresse ouverte, gardé en cookie HttpOnly et
# exigé sur /api/* et /ecoute/*. None : pas de contrôle (serveur créé par les tests).
JETON: str | None = None
FICHIER_SESSION = reglages.DOSSIER_CONF / "session"  # lu par l'app de la barre des menus, lisible par toi seul

verrou = threading.Lock()
verrou_apercu = threading.Lock()
enfants: set = set()  # transcriptions et téléchargements en cours, arrêtés quand l'outil quitte  # un seul Whisper d'aperçu à la fois : chacun prend plusieurs Go
enregistrement: dict | None = None       # {"proc", "dossier", "debut", "options", ...}
travaux: dict[str, dict] = {}            # nom de réunion -> avancement de sa transcription
preparation: dict | None = None          # téléchargement et essai de vitesse (suite d'accueil, RG-09)
test_son: dict | None = None             # essai du micro et du son de l'ordinateur, sans réunion
installation_pyannote: dict | None = None  # installation de l'option avancée depuis la page
reveil = threading.Event()               # « Réessayer maintenant » pendant une pause réseau
# ponytail: réunions ouvertes depuis le lancement, en mémoire : « 2 nouvelles » repart de zéro au redémarrage.
vues: set[str] = set()
SEUIL_FIGE = 30   # secondes sans que micro.wav ni systeme.wav grossisse avant l'alerte
RAPPEL_FIGE = 120  # secondes entre deux notifications tant que l'enregistrement reste figé
SEUIL_MUET = 60  # secondes sans le son de la réunion (systeme.wav muet ou figé) avant l'alerte
SEUIL_MICRO = 30  # secondes sans rien au micro avant l'alerte (EN-03)
SILENCE_DB = -80  # en visio, la piste système ne reste jamais aussi bas plus de quelques secondes
RIEN_DB = -60     # sous ce niveau, la jauge dit « Rien »
ENTENDU_DB = -50  # au-dessus, un essai du micro ou du son est réussi
ESPACE_MINI = 2e9  # octets libres exigés au démarrage : les deux pistes prennent environ 1,4 Go par heure
OCTETS_PAR_MIN = 1.4e9 / 60
# Raccourcis vers les réglages de confidentialité du système, quand la capture est refusée.
REGLAGES = ({"ecran": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
             "micro": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
             "son": "x-apple.systempreferences:com.apple.Sound-Settings.extension"} if MAC else
            {"micro": "ms-settings:privacy-microphone", "ecran": "ms-settings:sound", "son": "ms-settings:sound"})
LANGUES = {"auto": "Détecter seule", "fr": "Français", "en": "Anglais", "de": "Allemand", "es": "Espagnol",
           "it": "Italien", "nl": "Néerlandais", "pt": "Portugais"}
PAGE_TELECHARGEMENT = f"https://github.com/{DEPOT}"

# ---------------------------------------------------------------- erreurs

CAUSES_TRANSCRIPTION = {
    "son_vide": "le son est vide", "son_illisible": "le fichier son est illisible",
    "fichiers_incomplets": "les fichiers de transcription sont incomplets",
    "memoire": "l'ordinateur a manqué de mémoire (essayez la qualité Rapide)",
    "arret": "l'ordinateur s'est arrêté pendant le calcul", "disque_plein": "le disque est plein",
    "texte_absent": "le texte de la transcription précédente n'a pas été gardé : refaites la transcription complète",
    "inconnue": "une erreur inattendue s'est produite",
}
CAUSES = {
    "reunion_introuvable": ("Cette réunion est introuvable.", ""),
    "invalide": ("Valeur refusée.", ""),
    "deja_en_cours": ("Un enregistrement ou un essai du son est déjà en cours.", ""),
    "aucun_enregistrement": ("Aucun enregistrement en cours.", ""),
    "occupee_enregistrement": ("Cette réunion est en cours d'enregistrement : arrêtez-la d'abord.", ""),
    "occupee_transcription": ("Cette réunion est en cours de transcription : attendez la fin.", ""),
    "micro_refuse": ("Réunion n'a pas accès au micro.", "Rien n'a été enregistré."),
    "son_refuse": ("Réunion n'a pas accès au son de l'ordinateur.", "Rien n'a été enregistré."),
    "aucun_micro": ("Aucun micro trouvé. Branchez un casque ou un micro, puis réessayez.", "Rien n'a été enregistré."),
    "place": ("Plus assez de place : {libre} libres, il en faut 2 Go (une heure de réunion prend environ 1,4 Go).",
              "Rien n'a été enregistré."),
    "dossier": ("Le dossier des réunions est introuvable ou protégé : {chemin}.", "Rien n'a été enregistré."),
    "installation": ("Réunion est incomplète : une partie de l'installation manque.",
                     "Réinstallez Réunion : vos réunions et vos réglages sont gardés."),
    "enregistreur": ("L'enregistreur n'a pas démarré.", "Rien n'a été enregistré."),
    "transcription": ("La transcription a échoué : {cause}.", "Le son est gardé."),
    "protege": ("Pas enregistré : le dossier est protégé.", ""),
    "nom_pris": ("Une réunion porte déjà ce nom.", ""),
    "note_vide": ("La note est vide.", ""),
    "heure_hors_reunion": ("L'heure doit être entre 00:00 et {fin}.", ""),
    "note_introuvable": ("Cette note n'existe plus.", ""),
    "voix_inconnue": ("Cette voix n'existe plus dans la réunion.", ""),
    "apercu_en_cours": ("Une vérification est déjà en cours, patientez quelques secondes.", ""),
    "apercu": ("La vérification n'a pas abouti.", ""),
    "reseau_absent": ("Pas de connexion internet. Réessayez plus tard.", ""),
    "reseau_filtre": ("Le réseau de cet endroit bloque le téléchargement. Essayez depuis une autre connexion, "
                      "par exemple le partage de votre téléphone.", ""),
    "place_preparation": ("Il faut {besoin} libres, il en reste {libre}. Libérez de la place puis réessayez.", ""),
    "telechargement_en_cours": ("Un téléchargement est déjà en cours.", ""),
    "agenda": ("Cette adresse n'est pas celle d'un agenda Google : elle commence par "
               "https://calendar.google.com/calendar/ical/ et finit par .ics.", ""),
    "cle": ("Une clé d'accès Hugging Face commence par hf_ suivie de lettres et de chiffres.", ""),
    "trop_long": ("Texte trop long.", ""),
    "peripherique": ("Le micro ou la sortie audio ne répond pas.", ""),
    "inconnue": ("Une erreur inattendue s'est produite.", ""),
    "session": ("Ouvrez Réunion depuis son icône (barre des menus ou zone de notification).", ""),
}


def erreur(code: str, d: Path | None = None, **valeurs) -> dict:
    """Erreur nommée (spec ER) ; `d` désigne la réunion concernée, dont le journal est cité."""
    cause, garde = CAUSES.get(code, CAUSES["inconnue"])
    return {"erreur": {"code": code, "cause": cause.format(**valeurs), "garde": garde.format(**valeurs),
                       "journal": str(journal_de(d) if d else LOG)}}


def noter(message: str) -> None:
    """Ligne horodatée dans le journal de l'outil (et sur la console quand il y en a une)."""
    print(message, file=sys.stderr)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


def go(octets: float) -> str:
    return f"{octets / 1e9:.1f}".replace(".", ",") + " Go"


# ---------------------------------------------------------------- réunions

def slug(nom: str) -> str:
    return re.sub(r"[^\w-]+", "-", nom.strip()).strip("-")[:60] or "reunion"


def dossier_de(nom: str) -> Path | None:
    if not isinstance(nom, str) or not re.fullmatch(r"\d{6}-\d{4}[\w-]*", nom or ""):
        return None
    d = BASE / nom
    return d if d.is_dir() else None


JOURNAL = "journal.txt"  # enregistrement et transcription réunis ; meetrec.log ou capture.log avant le 03/11


def journal_de(d: Path) -> Path:
    return next((d / n for n in (JOURNAL, "meetrec.log", "capture.log") if (d / n).exists()), d / JOURNAL)


MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def debut_et_titre(nom: str) -> tuple[str | None, str]:
    """« 261103-1305-point-devis » -> ("2026-11-03T13:05", "point devis") ; sans nom : « Réunion du 3 nov. 13:05 »."""
    m = re.match(r"(\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})-?(.*)$", nom)
    if not m or not 1 <= int(m[2]) <= 12:
        return None, nom.replace("-", " ")
    reste = re.sub(r"-\d+$", "", m[6]) if m[6] != "reunion" else ""
    titre = reste.replace("-", " ") if reste and reste != "reunion" else \
        f"Réunion du {int(m[3])} {MOIS[int(m[2]) - 1]} {m[4]}:{m[5]}"
    return f"20{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}", titre


def lire_transcription(d: Path) -> dict | None:
    f = d / "transcription.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:  # fichier abîmé : la réunion reste listée, à retranscrire
        return None
    return relecture.normaliser(data if isinstance(data, dict) else {"source": "", "blocs": data})  # ancien format


def ecrire_transcription(d: Path, t: dict) -> None:
    transcribe.ecrire(d, t["blocs"], t.get("source", ""),
                      **{k: t[k] for k in ("separation", "voix", "propositions", "empreintes") if k in t})


PISTES = ("systeme.wav", "micro.wav")


def est_reunion(d: Path) -> bool:
    return any((d / f).exists() for f in PISTES)


def duree_audio(d: Path) -> float | None:
    """Durée en secondes, d'après la taille des pistes (float32 mono) : juste même pendant l'enregistrement
    ou après un arrêt brutal, quand l'en-tête annonce 0 trame."""
    durees = []
    for f in (d / n for n in PISTES):
        try:
            durees.append(max(f.stat().st_size - 4096, 0) / (transcribe.taux_wav(f) * 4))
        except (OSError, ValueError, ZeroDivisionError):  # piste absente, vide ou illisible
            pass
    return round(max(durees), 1) if durees else None


def lire_echec(d: Path) -> str | None:
    """Cause du dernier échec : JSON {code} depuis le 03/11, texte libre avant (cause inconnue)."""
    f = d / "echec.txt"
    if not f.exists():
        return None
    try:
        code = json.loads(f.read_text(encoding="utf-8")).get("code")
    except (ValueError, AttributeError, OSError):
        code = None
    return code if code in CAUSES_TRANSCRIPTION else "inconnue"


def etat_reunion(d: Path, transcrite: bool) -> str:
    if enregistrement and enregistrement["dossier"] == d:
        return "enregistrement"
    if travaux.get(d.name, {}).get("en_cours"):
        return "transcription"
    if (d / "echec.txt").exists():  # écrit à l'échec, effacé à la réussite : survit à un redémarrage
        return "echec"
    if not transcrite and (d / "transcription.attente").exists():
        return "attente"
    return "transcrite" if transcrite else "enregistree"


def signature(f: Path) -> tuple[int, int] | None:
    try:
        s = f.stat()
        return s.st_mtime_ns, s.st_size
    except OSError:
        return None


# ponytail: jamais vidé, une entrée par dossier vu depuis le lancement ; négligeable à l'échelle de l'outil.
_resumes: dict[Path, tuple] = {}


def resume(d: Path) -> tuple[bool, float | None, list[str], int, float | None]:
    """(transcrite, durée, locuteurs, propositions à vérifier, heure où elle est prête), relus seulement si la
    transcription ou une piste a changé : la liste est redemandée toutes les 2 s, et une transcription d'une
    heure pèse plusieurs centaines de Ko."""
    cle = tuple(signature(d / n) for n in ("transcription.json", *PISTES))
    if (connu := _resumes.get(d)) and connu[0] == cle:
        return connu[1]
    t = lire_transcription(d)
    valeur = (bool(t), duree_audio(d), sorted({b["loc"] for b in t["blocs"]}) if t else [],
              len(t["propositions"]) if t else 0, cle[0][0] / 1e9 if t and cle[0] else None)
    _resumes[d] = (cle, valeur)
    return valeur


def lister() -> list[dict]:
    if not BASE.exists():
        return []
    sortie = []
    for d in sorted((p for p in BASE.iterdir() if p.is_dir()), reverse=True):
        if not est_reunion(d):
            continue
        try:
            transcrite, duree, locuteurs, propositions, prete = resume(d)
        except Exception as e:  # JSON inattendu : la réunion reste listée, à refaire
            noter(f"Réunion illisible {d.name} : {e}")
            transcrite, duree, locuteurs, propositions, prete = False, None, [], 0, None
        debut, titre = debut_et_titre(d.name)
        sortie.append({"nom": d.name, "titre": titre, "debut": debut, "etat": etat_reunion(d, transcrite),
                       "duree": duree, "locuteurs": locuteurs, "voix": len(locuteurs),
                       "propositions": propositions, "prete_a": prete})
    return sortie


def ouvrir(cible: str) -> None:
    """Ouvre un dossier ou une page de réglages du système avec l'application par défaut."""
    if MAC:
        subprocess.run(["open", cible], check=False)
    elif WINDOWS:
        os.startfile(cible)  # noqa: S606 - cible fixée par le serveur (dossier de réunion ou réglage connu)
    else:
        subprocess.run(["xdg-open", cible], check=False)


def montrer(f: Path) -> None:
    """Ouvre le dossier d'un fichier dans le Finder ou l'Explorateur, le fichier sélectionné (ER-02)."""
    if MAC:
        subprocess.run(["open", "-R", str(f)] if f.exists() else ["open", str(f.parent)], check=False)
    elif WINDOWS:
        subprocess.run(["explorer", f"/select,{f}"] if f.exists() else ["explorer", str(f.parent)], check=False,
                       **SANS_FENETRE)
    else:
        ouvrir(str(f.parent))


def nombre(v) -> int | None:
    """Nombre de personnes ou de voix accepté : entier de 1 à 30, sinon ignoré."""
    return v if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 30 else None


def options_cli(o: dict) -> list[str]:
    args = []
    for cle in ("nb", "personnes", "max"):  # le premier présent l'emporte
        if n := nombre(o.get(cle)):
            args += [f"--{cle}", str(n)]
            break
    if o.get("presentiel"):
        args.append("--presentiel")
    if o.get("sans_locuteurs"):
        args.append("--sans-locuteurs")
    if o.get("chuchote"):
        args.append("--chuchote")
    # « Pistes à écouter » : les deux pistes sont toujours enregistrées, le choix ne vaut que pour la transcription
    # (un mauvais choix se rattrape avec « Refaire »). « deux » ou absent : les deux pistes.
    if o.get("pistes") in ("micro", "systeme"):
        args += ["--pistes", o["pistes"]]
    if o.get("separation_seule") is True:  # texte gardé, seule la séparation des voix est refaite
        args.append("--separation-seule")
    if o.get("langue") in LANGUES:
        args += ["--langue", o["langue"]]
    return args


# ---------------------------------------------------------------- traitements

def piste_ecoute(d: Path) -> None:
    """Mélange les deux pistes, recalées, dans ecoute.m4a : léger, lisible partout, sert à la relecture."""
    debuts = json.loads((d / "pistes.json").read_text(encoding="utf-8")) if (d / "pistes.json").exists() else {}
    origine = min(debuts.values()) if debuts else 0.0
    entrees, filtres = [], []
    for cle in ("systeme", "micro"):
        if (d / f"{cle}.wav").exists():
            retard = int((debuts.get(f"{cle}_debut", origine) - origine) * 1000)
            filtres.append(f"[{len(entrees) // 2}:a]adelay={retard}:all=1[p{len(filtres)}]")
            entrees += ["-i", str(d / f"{cle}.wav")]
    n = len(filtres)
    if not n:
        return
    graphe = ";".join(filtres) + ";" + "".join(f"[p{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0[m]"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *entrees, "-filter_complex", graphe,
                    "-map", "[m]", "-ac", "1", "-c:a", "aac", "-b:a", "64k", str(d / "ecoute.m4a")],
                   check=True, **SANS_FENETRE)


def lire_json(f: Path) -> dict | None:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lire_participants(d: Path) -> dict | None:
    return lire_json(d / "participants.json")


def lire_cr(d: Path) -> dict | None:
    """Dernier compte rendu déposé dans le dossier (CR-*.md), affiché en lecture seule (LI-20)."""
    crs = sorted(d.glob("*CR-*.md"))
    return {"fichier": crs[-1].name, "texte": crs[-1].read_text(encoding="utf-8")} if crs else None


def consulter_agenda(d: Path, debut: datetime, nom_auto: bool) -> Path:
    """Garde titre et invités de l'événement en cours ; renomme le dossier si aucun nom n'a été donné."""
    try:
        infos = agenda.participants(debut)
    except Exception as e:  # agenda injoignable : la transcription se fait quand même
        noter(f"Agenda injoignable : {e}")
        return d
    if not infos:
        return d
    # Les noms donnés au lancement priment : l'agenda dit qui est invité, pas qui est venu.
    saisis = (lire_participants(d) or {}).get("noms") or []
    infos = {**infos, "noms": saisis or infos["noms"], "saisis": bool(saisis)}
    (d / "participants.json").write_text(json.dumps(infos, ensure_ascii=False, indent=2), encoding="utf-8")
    if nom_auto and infos["titre"]:
        cible = d.with_name(f"{d.name[:11]}-{slug(infos['titre'])}")
        if not cible.exists():
            try:
                d = d.rename(cible)
            except OSError as e:  # Windows : un fichier du dossier encore ouvert ; l'ancien nom reste
                noter(f"Renommage de {d.name} impossible : {e}")
    return d


def avancer(travail: dict, fraction: float) -> None:
    """Avancement global (0 à 1). Passé 10 %, l'heure de fin suit la vitesse observée (TR-03, TR-08)."""
    travail["fraction"] = max(travail.get("fraction", 0), min(fraction, 0.99))
    if travail["fraction"] >= 0.10 and travail.get("debut"):
        ecoule = time.time() - travail["debut"]
        travail["fin_estimee"] = travail["debut"] + ecoule / travail["fraction"]
        travail["lent"] = ecoule / travail["fraction"] > 1.5 * max(travail.get("estimation_s") or 0, 1)


# Étapes de transcribe.py (« @etape N ») et leur part de l'avancement global.
_P, _T, _S = transcribe.PART_PREPARATION, transcribe.PART_TRANSCRIPTION, transcribe.PART_SEPARATION
BORNES_ETAPES = {1: (0.0, _P), 2: (_P, _P + _T), 3: (_P + _T, _P + _T + _S), 4: (_P + _T + _S, 1.0)}


def cle_vitesse(etape: int, modele: str) -> str:
    return f"2:{modele}" if etape == 2 else str(etape)  # seule la transcription dépend du modèle Whisper


def estimations_etapes(r: dict, modele: str, duree: float, options: dict) -> dict[int, float]:
    """Durée prévue de chaque étape (s) : moyenne des mesures de cet ordinateur pour l'étape, sinon l'estimation
    globale (vitesse_k) répartie selon la part de l'étape dans l'avancement."""
    k, _ = transcribe.vitesse_k(r, modele)
    mesures = r.get("vitesses_etapes") or {}
    prevu = {}
    for n, (a, b) in BORNES_ETAPES.items():
        m = mesures.get(cle_vitesse(n, modele)) or []
        prevu[n] = (sum(m) / len(m) if m else k * (b - a)) * (duree or 0)
    if options.get("separation_seule"):  # texte gardé : Whisper n'est pas relancé
        prevu[2] = 10.0
    return prevu


def noter_vitesses_etapes(modele: str, chrono: dict, duree: float, options: dict) -> None:
    """Garde les 5 dernières mesures de chaque étape (temps / durée de la réunion)."""
    r = reglages.lire()
    vitesses = dict(r.get("vitesses_etapes") or {})
    for n, c in (chrono or {}).items():
        n = int(n)
        if "fin" not in c or n not in BORNES_ETAPES or (n == 2 and options.get("separation_seule")):
            continue
        temps = c["fin"] - c["debut"]
        if n == 3 and temps < 2:  # pas de séparation cette fois : la mesure fausserait les suivantes
            continue
        cle = cle_vitesse(n, modele)
        vitesses[cle] = ((vitesses.get(cle) or []) + [round(temps / duree, 4)])[-5:]
    reglages.ecrire({**r, "vitesses_etapes": vitesses})


def noter_depart(travail: dict) -> None:
    """Premier avancement de l'étape en cours : point de départ de sa vitesse. Après une reprise, l'étape repart
    à 40 % d'un coup ; mesurer depuis là évite de croire ces 40 % faits en quelques secondes."""
    c = (travail.get("chrono") or {}).get(str(travail.get("etape")))
    if c is not None and "f0" not in c:
        c["f0"], c["t0"] = travail.get("fraction", 0.0), time.time()


def etapes_de(travail: dict) -> list[dict]:
    """Avancement et temps restant de chaque étape, pour la page. L'étape en cours suit sa vitesse observée dès
    3 % ; avant, et pour les étapes à venir, la durée prévue."""
    maintenant = travail.get("pause") or time.time()  # en pause, le temps restant ne bouge plus
    chrono, prevu = travail.get("chrono") or {}, travail.get("prevu") or {}
    cour, fraction = travail.get("etape", 1), travail.get("fraction", 0.0)
    sortie = []
    for n, (a, b) in BORNES_ETAPES.items():
        c = chrono.get(str(n)) or {}
        e = {"numero": n, "prevu_s": round(prevu.get(n, 0))}
        if n < cour or not travail.get("en_cours"):
            e |= {"statut": "fini", "fraction": 1.0, "restant_s": 0,
                  "duree_s": round(c["fin"] - c["debut"]) if "fin" in c else None}
        elif n == cour:
            fe = min(max((fraction - a) / (b - a), 0.0), 1.0)
            ecoule = maintenant - c.get("debut", travail.get("debut", maintenant))
            # Vitesse mesurée depuis le premier avancement de l'étape (f0, t0), pas depuis son début : une reprise
            # ne compte pas ce qui était fait avant l'arrêt.
            f0 = min(max((c.get("f0", a) - a) / (b - a), 0.0), 1.0)
            fait, duree_mesure = fe - f0, maintenant - c.get("t0", maintenant)
            if fait >= 0.03 and duree_mesure >= 20:
                restant = duree_mesure * (1 - fe) / fait
            else:  # pas encore de vitesse : la durée prévue, pour ce qui reste de l'étape
                restant = min(prevu.get(n, 0) * (1 - fe), max(prevu.get(n, 0) - ecoule, 0))
            e |= {"statut": "en_cours", "fraction": round(fe, 3), "ecoule_s": round(ecoule), "restant_s": round(restant)}
        else:
            e |= {"statut": "attente", "fraction": 0.0, "restant_s": round(prevu.get(n, 0))}
        sortie.append(e)
    return sortie


def mettre_en_pause(o: dict) -> dict:
    """Pause (o["pause"] vrai) ou reprise d'un traitement : son processus est gelé sur place, rien n'est perdu.
    À la reprise, les chronos sont décalés du temps de pause pour que vitesses et estimations l'ignorent."""
    nom, oui = str(o.get("reunion") or ""), o.get("pause") is True
    with verrou:
        t = travaux.get(nom)
        if not t or not t.get("en_cours") or not t.get("pid"):
            return erreur("invalide")
        if oui and not t.get("pause"):
            pause.geler(t["pid"])
            t["pause"] = time.time()
        elif not oui and t.get("pause"):
            pause.geler(t["pid"], False)
            ecart = time.time() - t.pop("pause")
            for cle in ("debut", "fin_estimee"):
                if t.get(cle):
                    t[cle] += ecart
            for c in (t.get("chrono") or {}).values():
                for cle in ("debut", "t0"):
                    if cle in c and "fin" not in c:
                        c[cle] += ecart
    return {"ok": True}


def executer(travail: dict, args: list[str], journal: Path | None = None) -> int:
    """Lance un script Python de l'environnement et lit son avancement (lignes « @... » et barres « 45%| »).
    La sortie lisible part dans le journal de la réunion."""
    # UTF-8 imposé : sous Windows, la sortie d'un tube serait sinon en cp1252 et les accents illisibles.
    proc = subprocess.Popen([PY, "-u", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                            **SANS_FENETRE)
    enfants.add(proc)
    travail["pid"] = proc.pid  # pour la pause
    j = None
    if journal:
        try:
            j = journal.open("a", encoding="utf-8")
        except OSError:
            pass
    try:
        for ligne in proc.stdout:
            ligne = ligne.strip()
            if not ligne:
                continue
            if m := re.match(r"(\d+)%\|", ligne):  # barre de la transcription d'une piste
                a, b = travail.get("piste", (0, 0))
                avancer(travail, a + (b - a) * int(m[1]) / 100)
                noter_depart(travail)
                continue
            if ligne.startswith("@"):
                cle, _, valeur = ligne[1:].partition(" ")
                try:
                    if cle == "etape":
                        travail["etape"] = int(valeur)
                        chrono, maintenant = travail.setdefault("chrono", {}), time.time()
                        for c in chrono.values():  # l'étape précédente se termine
                            c.setdefault("fin", maintenant)
                        chrono.setdefault(valeur, {"debut": maintenant})
                    elif cle == "piste":
                        travail["piste"] = tuple(map(float, valeur.split()))
                        avancer(travail, travail["piste"][0])
                    elif cle == "avance":
                        avancer(travail, float(valeur))
                        noter_depart(travail)
                    elif cle == "repli":
                        travail["repli"] = True
                    elif cle in ("echec", "k"):
                        travail[cle] = valeur if cle == "echec" else float(valeur)
                except ValueError:
                    pass
            elif "it/s" not in ligne and "%|" not in ligne:
                travail["ligne"] = ligne[:200]
            if j:
                j.write(f"{datetime.now():%H:%M:%S} {ligne}\n")
                j.flush()
    finally:
        if j:
            j.close()
    travail["code"] = proc.wait()
    travail.pop("pause", None)
    for c in (travail.get("chrono") or {}).values():
        c.setdefault("fin", time.time())
    enfants.discard(proc)
    return travail["code"]


def noter_telechargement(repo: str) -> None:
    """Modèles téléchargés par Réunion : les seuls que « Effacer les fichiers de Réunion » supprime (MD-09)."""
    r = reglages.lire()
    if repo not in r["telecharges"]:
        reglages.ecrire({**r, "telecharges": [*r["telecharges"], repo]})


def noter_vitesse(modele: str, k: float) -> None:
    """Garde les 5 dernières mesures de k pour ce modèle : l'estimation suit cet ordinateur (spec 3.2)."""
    r = reglages.lire()
    vitesses = dict(r["vitesses"] or {})
    vitesses[modele] = ((vitesses.get(modele) or []) + [round(k, 3)])[-5:]
    reglages.ecrire({**r, "vitesses": vitesses})


def fichiers_prets(r: dict | None = None) -> bool:
    return transcribe.en_cache(transcribe.choix_modele(r or reglages.lire())[1])


def lancer_transcription(d: Path, options: dict, reprise: bool = False) -> dict | None:
    """Transcription en arrière-plan ; si les fichiers de transcription ne sont pas encore là, la réunion attend
    la fin de la préparation (TR-13). Renvoie une erreur si la réunion est occupée."""
    # Invités de l'agenda : plafond du nombre de voix. Noms saisis à la main : seulement proposés au nommage.
    p = lire_participants(d) or {}
    invites = [] if p.get("saisis") else p.get("noms", [])
    if invites and not options.get("nb") and not options.get("personnes"):
        options = {**options, "max": len(invites)}
    r = reglages.lire()
    if not fichiers_prets(r):
        (d / "transcription.attente").write_text(json.dumps(options, ensure_ascii=False), encoding="utf-8")
        if not (preparation and preparation["en_cours"]):
            lancer_preparation({})
        return None
    modele = transcribe.taille_modele(r)
    k, mesure = transcribe.vitesse_k(r, modele)
    duree = duree_audio(d) or 0
    maintenant = time.time()
    prevu = estimations_etapes(r, modele, duree, options)  # sans mesure par étape : k * duree réparti, même total
    total = sum(prevu.values())
    travail = {"en_cours": True, "etape": 1, "fraction": 0.0, "debut": maintenant, "estimation_s": total,
               "fin_estimee": maintenant + total, "fin_initiale": maintenant + total, "approx": not mesure,
               "repli": False, "lent": False, "reprise": reprise, "code": None, "echec": None, "ligne": "",
               "prevu": prevu, "chrono": {}}

    def tourner(travail):
        (d / "options.json").write_text(json.dumps(options, ensure_ascii=False), encoding="utf-8")
        (d / "transcription.attente").unlink(missing_ok=True)
        # Témoin d'un calcul en cours : s'il reste au lancement suivant, la transcription reprend seule (TR-09).
        (d / "transcription.en-cours").write_text(json.dumps(options, ensure_ascii=False), encoding="utf-8")
        titre = debut_et_titre(d.name)[1]
        try:
            piste_ecoute(d)
        except Exception as e:  # l'écoute est un confort, la transcription passe quand même
            noter(f"Piste d'écoute impossible pour {d.name} : {e}")
        code = executer(travail, [str(ICI / "transcribe.py"), str(d), *options_cli(options)], journal_de(d))
        if code:
            cause = travail["echec"] if travail["echec"] in CAUSES_TRANSCRIPTION else \
                "memoire" if code == -9 else "arret" if code < 0 else "inconnue"
            travail["echec"] = cause
            (d / "echec.txt").write_text(json.dumps({"code": cause}), encoding="utf-8")
            alerter(f"La transcription de « {titre} » a échoué.")
        else:
            (d / "echec.txt").unlink(missing_ok=True)
            if duree >= 60 and not reprise:
                if not options.get("separation_seule"):  # sans Whisper, le total ne dit rien de sa vitesse
                    noter_vitesse(modele, (time.time() - travail["debut"]) / duree)
                noter_vitesses_etapes(modele, travail.get("chrono"), duree, options)
            alerter(f"Votre réunion est prête : {titre}")
        (d / "transcription.en-cours").unlink(missing_ok=True)

    return lancer_travail(d, travail, tourner)


def occupee(d: Path) -> str | None:
    """Code de la raison de ne pas toucher à une réunion (à appeler sous le verrou)."""
    if enregistrement and enregistrement["dossier"] == d:
        return "occupee_enregistrement"
    if travaux.get(d.name, {}).get("en_cours"):
        return "occupee_transcription"
    return None


def lancer_travail(d: Path, travail: dict, fonction) -> dict | None:
    """Lance un traitement en arrière-plan ; refuse si la réunion s'enregistre ou se traite déjà."""
    with verrou:
        if code := occupee(d):
            return erreur(code, d)
        travaux[d.name] = travail

    def tourner():
        try:
            fonction(travail)
        finally:
            travail["en_cours"] = False

    threading.Thread(target=tourner, daemon=True).start()
    return None


def niveau_db(wav: Path) -> float | None:
    """Niveau efficace (dBFS) de la dernière demi-seconde d'une piste (float32 mono de meetrec)."""
    try:
        octets = transcribe.taux_wav(wav) // 2 * 4
        with wav.open("rb") as f:
            if f.seek(0, 2) < octets + 4096:
                return None
            f.seek(-octets, 2)
            a = array("f", f.read(octets))
    except (OSError, ValueError):
        return None
    rms = math.sqrt(sum(x * x for x in a) / len(a))
    return round(20 * math.log10(max(rms, 1e-10)), 1)


def alerter(message: str) -> None:
    # ponytail: notification système sur Mac ; sous Windows, l'icône de la zone de notification s'en charge.
    noter(message)
    if not MAC:
        return
    # Message passé en argument, jamais collé dans le script AppleScript : un nom de réunion peut contenir des guillemets.
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    'display notification (item 1 of argv) with title "Réunion" sound name "Basso"',
                    "-e", "end run", message], check=False)


def son_coupe(e: dict, suivi: dict) -> bool:
    """Vrai une fois quand la piste système (les voix distantes), déjà entendue, se tait SEUIL_MUET s : le 30/10,
    le son de Meet a cessé d'arriver à 10:47 sans aucune erreur, 1 h 30 de voix perdues. Sans tampon reçu,
    meetrec n'écrit rien : un fichier qui ne grossit plus compte comme muet.

    Jamais entendue alors que le micro capte : e["silence"], pour un simple bandeau dans la page, sans
    notification. Le 03/11, c'était normal (atelier dans la pièce diffusé sur Meet, distants muets, case
    « Plusieurs dans la pièce » oubliée) : la transcription sépare alors les voix du micro d'elle-même.

    Tient aussi e["micro_muet"] : instant depuis lequel le micro n'entend rien (EN-03)."""
    maintenant = time.time()
    suivi.setdefault("debut", maintenant)
    f = e["dossier"] / "systeme.wav"
    t = f.stat().st_size if f.exists() else 0
    n = niveau_db(f)
    if t != suivi.get("taille") and n is not None and n > SILENCE_DB:
        suivi["son"], e["muet"], e["son_entendu"] = maintenant, False, True
    suivi["taille"] = t
    m = niveau_db(e["dossier"] / "micro.wav")
    if m is not None and m > SILENCE_DB:
        suivi["micro"] = e["micro_entendu"] = True
    if m is not None and m > RIEN_DB:
        suivi["micro_t"] = maintenant
    depuis_micro = suivi.get("micro_t", suivi["debut"])
    e["micro_muet"] = depuis_micro if maintenant - depuis_micro >= SEUIL_MICRO else None
    e["silence"] = ("son" not in suivi and suivi.get("micro", False) and not e.get("options", {}).get("presentiel")
                    and maintenant - suivi["debut"] >= SEUIL_MUET)
    if "son" in suivi and not e["muet"] and maintenant - suivi["son"] >= SEUIL_MUET:
        e["muet"] = True
        e["muet_depuis"] = suivi["son"]
        return True
    return False


def relances(journal: Path) -> int:
    """Nombre de reprises de capture dans le journal de l'enregistreur (casque débranché, sortie changée)."""
    try:
        texte = journal.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return texte.count("Capture relancée") + texte.count("capture reprise")


def surveiller(e: dict) -> None:
    """Alerte si l'enregistrement se fige : le 13/10, la capture s'est arrêtée sans erreur pendant 31 min."""
    taille, depuis, suivi, vues_relances = -1, time.time(), {}, 0
    while enregistrement is e:
        if e["proc"].poll() is not None:
            e["fige"] = True
            e["arret"] = {"heure": time.strftime("%H:%M"), "minutes": round((time.time() - e["debut"]) / 60)}
            alerter("L'enregistrement s'est arrêté tout seul. Ce qui a été enregistré est gardé.")
            return
        # Les deux pistes : en visio, le micro peut arriver par paquets retardés de plus de 30 s
        # alors que rien n'est perdu (fausse alerte du 03/11) ; figé = plus rien ne grossit.
        t = sum((e["dossier"] / n).stat().st_size for n in ("micro.wav", "systeme.wav")
                if (e["dossier"] / n).exists())
        if t != taille:
            taille, depuis, e["fige"], e["alerte_fige"] = t, time.time(), False, 0
        elif time.time() - depuis >= SEUIL_FIGE:
            e["fige"], e["fige_depuis"] = True, depuis
            if time.time() - e.get("alerte_fige", 0) >= RAPPEL_FIGE:
                # Répétée : le 03/11, l'unique notification est passée inaperçue pendant 1 h 30 de visio.
                e["alerte_fige"] = time.time()
                alerter(f"L'enregistrement est bloqué depuis {round(time.time() - depuis)} s.")
        if son_coupe(e, suivi):
            alerter("Le son de la visio n'arrive plus depuis 1 min : vérifiez la sortie audio de l'ordinateur.")
        if (n := relances(e["dossier"] / JOURNAL)) > vues_relances:
            vues_relances, e["appareil"] = n, time.time()
        try:
            e["libre"] = shutil.disk_usage(e["dossier"]).free
        except OSError:
            pass
        time.sleep(5)


def alertes(e: dict) -> list[dict]:
    """Alertes en cours d'enregistrement (EN-03 à EN-08, ER-09), de la plus grave à la moins grave."""
    maintenant, a = time.time(), []
    if e.get("arret"):
        a.append({"code": "arret", "niveau": "rouge", "depuis_s": 0, **e["arret"]})
    elif e.get("fige"):
        a.append({"code": "fige", "niveau": "rouge", "depuis_s": round(maintenant - e.get("fige_depuis", maintenant))})
    if e.get("micro_muet"):
        a.append({"code": "micro_muet", "niveau": "jaune", "depuis_s": round(maintenant - e["micro_muet"])})
    if e.get("muet"):
        a.append({"code": "son_coupe", "niveau": "jaune",
                  "depuis_s": round(maintenant - e.get("muet_depuis", maintenant))})
    if e.get("libre") is not None and e["libre"] < 1e9:
        a.append({"code": "place", "niveau": "jaune", "depuis_s": 0,
                  "minutes": max(0, round(e["libre"] / OCTETS_PAR_MIN))})
    if e.get("silence"):
        a.append({"code": "son_jamais", "niveau": "neutre", "depuis_s": round(maintenant - e["debut"])})
    if e.get("appareil") and maintenant - e["appareil"] < 60:
        a.append({"code": "appareil", "niveau": "neutre", "depuis_s": round(maintenant - e["appareil"])})
    return a


def apercu() -> dict:
    """Transcrit les 20 dernières secondes de l'enregistrement en cours, pour vérifier que ça passe (EN-09)."""
    e = enregistrement
    if not e:
        return erreur("aucun_enregistrement")
    o = e["options"]
    args = ["--rehausser"] if o.get("presentiel") or o.get("chuchote") else []
    if not verrou_apercu.acquire(blocking=False):
        return erreur("apercu_en_cours")
    try:
        r = subprocess.run([PY, str(ICI / "apercu.py"), str(e["dossier"] / "micro.wav"), *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **SANS_FENETRE)
    except subprocess.TimeoutExpired:
        return erreur("apercu", e["dossier"])
    finally:
        verrou_apercu.release()
    lignes = [l for l in r.stdout.splitlines() if l.strip()]
    if r.returncode or not lignes:
        noter(f"Aperçu impossible : {(r.stderr.strip().splitlines() or ['?'])[-1][:300]}")
        return erreur("apercu", e["dossier"])
    return {"ok": True, "texte": lignes[-1]}


def lancer_capture(d: Path) -> subprocess.Popen:
    """meetrec sur Mac, capture.py ailleurs. Entrée standard en tube : capture.py s'arrête quand on la ferme
    (pas de SIGINT vers un autre processus sous Windows)."""
    # Messages dans un fichier : un tube jamais lu se remplit et bloque la capture.
    with (d / JOURNAL).open("w", encoding="utf-8") as log:
        if MAC:
            return subprocess.Popen([str(ICI / "meetrec"), str(d)], stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=log)
        return subprocess.Popen([PY, "-u", str(ICI / "capture.py"), str(d)], stdin=subprocess.PIPE,
                                stdout=log, stderr=log, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **SANS_FENETRE)


def capture_prete(proc: subprocess.Popen, d: Path) -> bool:
    """Faux si l'enregistreur s'arrête au démarrage (autorisation ou périphérique manquant). meetrec échoue en
    moins de 1,5 s ; capture.py met plusieurs secondes à charger soundcard sous Windows : on attend sa ligne
    « Enregistrement en cours », écrite une fois les périphériques trouvés (10 s au plus)."""
    if MAC:
        try:
            proc.wait(timeout=1.5)
            return False
        except subprocess.TimeoutExpired:
            return True
    limite = time.time() + 10
    while time.time() < limite and proc.poll() is None:
        if "Enregistrement en cours" in (d / JOURNAL).read_text(encoding="utf-8", errors="replace"):
            return True
        time.sleep(0.1)
    return proc.poll() is None


def cause_demarrage(journal: str) -> str:
    """Code d'un démarrage refusé, d'après la première ligne du journal de l'enregistreur (ER-04 à ER-06)."""
    t = (journal.strip().splitlines() or [""])[0].lower()
    if "aucun micro" in t:
        return "aucun_micro"
    if "bibliothèque audio" in t:
        return "installation"
    if "microphone" in t or "micro" in t and "refus" in t:
        return "micro_refuse"
    if any(m in t for m in ("tcc", "declined", "refus", "not authorized", "autoris", "permission")):
        return "son_refuse"
    return "enregistreur"


def stopper(proc: subprocess.Popen) -> None:
    """Arrêt propre : l'enregistreur finalise ses fichiers ; tué au bout de 30 s s'il ne répond pas."""
    try:
        proc.stdin.close()
    except OSError:
        pass
    if MAC:
        proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def direct_par_defaut(r: dict) -> bool:
    """Brouillon actif par défaut seulement sur une machine puissante (DI-07, DI-08)."""
    if r.get("direct") is not None:
        return bool(r["direct"])
    m = transcribe.machine()
    return m["carte_graphique"] in ("apple", "nvidia") and (m["memoire_go"] or 8) >= 8


def lire_direct(dr: dict, ligne: str) -> None:
    """Une ligne de direct.py : morceau, position du calcul ou saut (DI-02, DI-04, DI-05)."""
    try:
        o = json.loads(ligne)
    except ValueError:
        return
    if "texte" in o and o.get("source") in ("moi", "autres"):
        dr["morceaux"].append({"t_s": o.get("t_s", 0), "source": o["source"], "texte": str(o["texte"])})
    elif "pos" in o:
        dr["retard_s"] = round(max(0.0, o.get("dispo", 0) - o["pos"]))
    elif o.get("saut"):
        dr["saut"] = time.time()


def lancer_direct(e: dict) -> None:
    """Brouillon en direct dans un processus à priorité basse ; jamais écrit sur le disque. Un seul à la fois,
    seulement pour l'enregistrement en cours (à appeler sous le verrou)."""
    if enregistrement is not e or (e.get("direct") or {}).get("proc"):
        return
    if not fichiers_prets():
        e["direct"] = {"actif": False, "raison": "preparation", "morceaux": [], "retard_s": 0}
        return
    options = {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW} if WINDOWS else {}
    proc = subprocess.Popen([PY, "-u", str(ICI / "direct.py"), str(e["dossier"])], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **options)
    dr = e["direct"] = {"actif": True, "morceaux": [], "retard_s": 0, "saut": 0, "proc": proc}

    def lire():
        for ligne in proc.stdout:
            lire_direct(dr, ligne)

    threading.Thread(target=lire, daemon=True).start()


def couper_direct(e: dict) -> None:
    proc = (e.get("direct") or {}).get("proc")
    if proc:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.terminate()


def demarrer(o: dict) -> dict:
    global enregistrement
    with verrou:
        if enregistrement or (test_son and test_son["en_cours"]):
            return erreur("deja_en_cours")
        if MAC and not (ICI / "meetrec").exists():
            return erreur("installation")
        try:
            BASE.mkdir(parents=True, exist_ok=True)
            libre = shutil.disk_usage(BASE).free
        except OSError as e:
            noter(f"Dossier des réunions inaccessible ({BASE}) : {e}")
            return erreur("dossier", chemin=BASE)
        if libre < ESPACE_MINI:
            return erreur("place", libre=go(libre))
        # Dossier neuf à chaque fois : une relance dans la même minute ne doit pas écraser l'audio précédent.
        base, n = f"{time.strftime('%y%m%d-%H%M')}-{slug(o.get('nom') or '')}", 1
        d = BASE / base
        while d.exists():
            n += 1
            d = BASE / f"{base}-{n}"
        try:
            d.mkdir(parents=True)
        except OSError:
            return erreur("dossier", chemin=BASE)
        proc = lancer_capture(d)
        if not capture_prete(proc, d):
            texte = (d / JOURNAL).read_text(encoding="utf-8", errors="replace")
            noter(f"Démarrage refusé : {texte.strip()[:500]}")
            shutil.rmtree(d, ignore_errors=True)
            code = cause_demarrage(texte)
            if code in ("micro_refuse", "aucun_micro"):
                reglages.ecrire({**reglages.lire(), "etat_micro": "refuse"})
            return {**erreur(code), "autorisations": code in ("micro_refuse", "son_refuse")}
        if MAC:
            # Ni veille ni écran éteint tant que meetrec tourne (-w : s'arrête avec lui) : le 13/10, l'écran
            # éteint a figé la capture de 10:03 à 10:34. Hors Mac, capture.py s'en charge lui-même.
            subprocess.Popen(["caffeinate", "-dimsu", "-w", str(proc.pid)])
        noms = sorted({n.strip()[:60] for n in str(o.get("participants") or "").split(",") if n.strip()})
        if noms:
            (d / "participants.json").write_text(json.dumps({"titre": "", "noms": noms, "saisis": True},
                                                            ensure_ascii=False, indent=2), encoding="utf-8")
        e = enregistrement = {"proc": proc, "dossier": d, "debut": time.time(), "options": o,
                              "nom_auto": not (o.get("nom") or "").strip(), "fige": False, "muet": False,
                              "direct": {"actif": False, "raison": "machine", "morceaux": [], "retard_s": 0}}
    if direct_par_defaut(reglages.lire()):
        with verrou:
            lancer_direct(e)
    threading.Thread(target=surveiller, args=(e,), daemon=True).start()
    return {"ok": True, "nom": d.name}


def arreter() -> dict:
    global enregistrement
    with verrou:
        e, enregistrement = enregistrement, None
        if e:  # la page voit « Transcription » dès l'arrêt, jamais un trou entre les deux états
            maintenant = time.time()
            arret = travaux[e["dossier"].name] = {
                "en_cours": True, "arret": True, "etape": 1, "fraction": 0.0, "debut": maintenant, "estimation_s": 0,
                "fin_estimee": maintenant, "fin_initiale": maintenant, "approx": True, "repli": False, "lent": False,
                "reprise": False, "code": None, "echec": None, "ligne": ""}
    if not e:
        return erreur("aucun_enregistrement")
    couper_direct(e)
    stopper(e["proc"])
    d = consulter_agenda(e["dossier"], datetime.fromtimestamp(e["debut"]), e["nom_auto"])
    # Micro ou son entendus : l'accueil ne réclame plus leur autorisation (AC-05, AC-07).
    maj = {k: "ok" for k, ok in (("etat_micro", e.get("micro_entendu")), ("etat_son", e.get("son_entendu"))) if ok}
    if maj:
        reglages.ecrire({**reglages.lire(), **maj})
    with verrou:
        if travaux.get(e["dossier"].name) is arret:
            del travaux[e["dossier"].name]
    lancer_transcription(d, {k: v for k, v in e["options"].items() if k not in ("nom", "participants")})
    return {"ok": True, "nom": d.name}


def retranscrire(d: Path, o: dict) -> dict | None:
    """Reprend les options d'origine de la réunion ; la fenêtre « Refaire » n'ajoute que ce qu'elle remplit
    (nombre de personnes, voix faibles, langue)."""
    sauvees = lire_json(d / "options.json") or {}
    if "personnes" in o or "nb" in o:  # un nouveau nombre remplace l'ancien
        sauvees = {k: v for k, v in sauvees.items() if k not in ("nb", "personnes", "max")}
    sauvees.pop("separation_seule", None)  # vaut pour une seule fois : un « Refaire » complet ensuite relance Whisper
    for nom in ("transcription.json", "locuteurs.json"):  # le texte remplacé reste récupérable
        if (d / nom).exists():
            shutil.copyfile(d / nom, d / nom.replace(".json", ".avant-refaire.json"))
    return lancer_transcription(d, {**sauvees, **{k: v for k, v in o.items() if k != "reunion" and v is not None}})


# ---------------------------------------------------------------- relecture : voix, lignes, notes

def moi() -> str:
    return reglages.lire()["moi"] or transcribe.PRENOM


def modifier(o: dict, fonction) -> dict:
    """Applique `fonction(d, t, o)` à la transcription d'une réunion, sous le verrou, puis l'écrit. La fonction
    renvoie un code d'erreur, ou un dict ajouté à la réponse."""
    d = dossier_de(o.get("reunion", ""))
    if not d:
        return erreur("reunion_introuvable")
    with verrou:
        if code := occupee(d):
            return erreur(code, d)
        t = lire_transcription(d)
        if not t:
            return erreur("reunion_introuvable", d)
        try:
            res = fonction(d, t, o)
            if isinstance(res, str):
                return erreur(res, d)
            res = dict(res or {})
            apres = res.pop("_apres", None)
            ecrire_transcription(d, t)
            if apres:  # empreintes et voix connues, une fois la transcription écrite
                apres()
            if fonction is not _regrouper:  # « Annuler » ne vaut que juste après un regroupement
                (d / "transcription.avant-regroupement.json").unlink(missing_ok=True)
        except OSError as e:
            noter(f"Écriture impossible dans {d} : {e}")
            return erreur("protege", d)
        except ValueError as e:
            noter(f"Modification refusée dans {d} : {e}")
            return erreur("invalide", d)
    return {"ok": True, **res}


def _renommer(d: Path, t: dict, o: dict) -> str | dict | None:
    ancien, nouveau = o.get("ancien"), str(o.get("nouveau") or "").strip()[:60]
    if ancien not in t["voix"] or not nouveau:
        return "voix_inconnue" if nouveau else "invalide"
    for b in t["blocs"]:
        if b["loc"] == ancien:
            b["loc"] = nouveau
    if nouveau in t["voix"] and nouveau != ancien:  # même nom qu'une autre voix : regroupées (VX-10)
        del t["voix"][ancien]
    else:
        t["voix"][nouveau] = t["voix"].pop(ancien)
    t["propositions"].pop(ancien, None)
    t["blocs"] = transcribe.fusionner(t["blocs"])
    # La voix d'un tiers n'est retenue qu'avec son accord (Q1) ; la tienne, toujours (VX-09).
    apprendre = bool(o.get("retenir")) or nouveau == moi()
    return {"nom": nouveau, "_apres": lambda: voix.retenir(d, ancien, nouveau, apprendre, t["empreintes"])}


def _accepter(d: Path, t: dict, o: dict):
    p = t["propositions"].get(o.get("voix"))
    if not p:
        return "voix_inconnue"
    # Voix déjà connue, donc retenue avec accord : elle s'enrichit de cette réunion (VX-04).
    return _renommer(d, t, {"ancien": o["voix"], "nouveau": p["nom"], "retenir": True})


def _refuser(d: Path, t: dict, o: dict):
    if t["propositions"].pop(o.get("voix"), None) is None:
        return "voix_inconnue"
    return None


def _regrouper(d: Path, t: dict, o: dict):
    de, vers = o.get("de"), o.get("vers")
    if de not in t["voix"] or vers not in t["voix"] or de == vers:
        return "voix_inconnue"
    # Copie gardée pour « Annuler » (VX-12) : les deux voix et leurs lignes reviennent comme avant.
    shutil.copyfile(d / "transcription.json", d / "transcription.avant-regroupement.json")
    for b in t["blocs"]:
        if b["loc"] == de:
            b["loc"] = vers
    del t["voix"][de]
    t["propositions"].pop(de, None)
    return None


def annuler_regroupement(o: dict) -> dict:
    d = dossier_de(o.get("reunion", ""))
    if not d:
        return erreur("reunion_introuvable")
    copie = d / "transcription.avant-regroupement.json"
    with verrou:
        if code := occupee(d):
            return erreur(code, d)
        if not copie.exists():
            return erreur("invalide", d)
        try:
            reglages.ecrire_atomique(d / "transcription.json", copie.read_text(encoding="utf-8"))
            ecrire_transcription(d, lire_transcription(d))  # transcription.md suit
            copie.unlink()
        except (OSError, ValueError, TypeError) as e:
            noter(f"Annulation impossible dans {d} : {e}")
            return erreur("protege", d)
    return {"ok": True}


def _index(t: dict, o: dict) -> int | None:
    i = o.get("index")
    return i if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(t["blocs"]) else None


def _attribuer_ligne(d: Path, t: dict, o: dict):
    i, loc = _index(t, o), str(o.get("loc") or "").strip()[:60]
    if i is None or not loc:
        return "invalide"
    ancien = t["blocs"][i]["loc"]
    t["blocs"][i]["loc"] = loc  # seule cette ligne change (VX-13)
    if loc not in t["voix"]:
        t["voix"][loc] = {"numero": max(v["numero"] for v in t["voix"].values()) + 1,
                          "source": t["voix"].get(ancien, {}).get("source")}
    if not any(b["loc"] == ancien for b in t["blocs"]):
        t["voix"].pop(ancien, None)
    return None


def _ligne(d: Path, t: dict, o: dict):
    i, texte = _index(t, o), o.get("texte")
    if i is None or not isinstance(texte, str) or not texte.strip():
        return "invalide"
    if len(texte) > 10000:
        return "trop_long"
    t["blocs"][i]["texte"] = texte.strip()
    return None


def note(o: dict) -> dict:
    """Notes horodatées (NT) : pendant l'enregistrement, l'instant vient de la page (première frappe)."""
    d = dossier_de(o.get("reunion", ""))
    if not d:
        return erreur("reunion_introuvable")
    with verrou:
        e = enregistrement
        duree = time.time() - e["debut"] if e and e["dossier"] == d else duree_audio(d)
        try:
            n, code = relecture.modifier_notes(d, o, duree)
        except OSError as ex:
            noter(f"Note impossible dans {d} : {ex}")
            return erreur("protege", d)
    if code:
        return erreur(code, d, fin=relecture.heure(duree or 0))
    return {"ok": True, "note": n}


def participants(o: dict) -> dict:
    """Liste des participants d'une réunion, modifiable (VX-26) ; le titre de l'agenda est gardé."""
    d = dossier_de(o.get("reunion", ""))
    noms = o.get("noms")
    if not d:
        return erreur("reunion_introuvable")
    if not isinstance(noms, list) or not all(isinstance(n, str) for n in noms) or len(noms) > 50:
        return erreur("invalide", d)
    p = lire_participants(d) or {"titre": ""}
    p.update(noms=sorted({n.strip()[:60] for n in noms if n.strip()}, key=str.casefold), saisis=True)
    try:
        (d / "participants.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return erreur("protege", d)
    return {"ok": True}


def noms_passes(sauf: str) -> list[str]:
    """Noms portés dans les autres réunions (résumés en cache) : dernière source de suggestions (VX-19)."""
    return sorted({loc for r in lister() if r["nom"] != sauf for loc in r["locuteurs"]
                   if not relecture.generique(loc)})


def detail(d: Path) -> dict:
    t = lire_transcription(d)
    notes = relecture.lire_notes(d)
    p = lire_participants(d) or {}
    r = reglages.lire()
    liste = relecture.voix_de(t) if t else []
    if t:
        try:
            connues = sorted(voix.lire_banque(t["empreintes"]))
        except (OSError, ValueError):
            connues = []
        s = relecture.suggestions(liste, p.get("noms") or [], moi(), connues, noms_passes(d.name))
        for v in liste:
            v["suggestions"] = s[v["loc"]]
    duree = duree_audio(d)
    k, mesure = transcribe.vitesse_k(r, transcribe.taille_modele(r))
    echec = lire_echec(d)
    debut, titre = debut_et_titre(d.name)
    vues.add(d.name)
    return {"nom": d.name, "titre": titre, "debut": debut, "chemin": str(d), "transcription": t, "notes": notes,
            "voix": liste, "duree": duree, "ecoute": (d / "ecoute.m4a").exists(), "participants": p, "moi": moi(),
            "compte_rendu": lire_cr(d), "journal": str(journal_de(d)),
            "echec": echec and erreur("transcription", d, cause=CAUSES_TRANSCRIPTION[echec])["erreur"],
            "estimation_s": round(k * (duree or 0)), "estimation_approx": not mesure,
            "separation_disponible": separation_disponible(),
            "pistes": (lire_json(d / "options.json") or {}).get("pistes") or "deux",
            "texte_garde": (d / transcribe.TEXTE_BRUT).exists(),
            "partiel": travaux.get(d.name, {}).get("en_cours") and lire_json(d / "transcription.partielle.json")}


def recherche(q: str) -> list[dict]:
    """Réunions qui contiennent la recherche, avec leur nombre de passages ; jamais le brouillon (AC-11, DI-13)."""
    if not q.strip():
        return []
    sortie = []
    for r in lister():
        d = BASE / r["nom"]
        lignes, notes = relecture.passages(lire_transcription(d), relecture.lire_notes(d), q)
        if lignes + notes:
            sortie.append({**r, "passages": lignes + notes, "notes": notes})
    return sortie


def exporter(o: dict) -> tuple[bytes, str, str] | dict:
    d = dossier_de(o.get("reunion", ""))
    if not d:
        return erreur("reunion_introuvable")
    if o.get("format") not in ("docx", "md"):
        return erreur("invalide", d)
    debut, titre = debut_et_titre(d.name)
    lignes = relecture.lignes_texte(lire_transcription(d), relecture.lire_notes(d))
    if o["format"] == "md":
        texte = "\n\n".join([f"# {titre}", *lignes]) + "\n"
        return texte.encode("utf-8"), f"{d.name}.md", "text/markdown; charset=utf-8"
    return (relecture.docx(titre, lignes), f"{d.name}.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def journal(params: dict) -> dict:
    """Les 200 dernières lignes du journal d'une réunion, ou de celui de l'outil (ER-03)."""
    nom = (params.get("reunion") or [""])[0]
    d = dossier_de(nom) if nom else None
    if nom and not d:
        return erreur("reunion_introuvable")
    f = journal_de(d) if d else LOG
    try:
        lignes = f.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    except OSError:
        lignes = []
    return {"chemin": str(f), "texte": "\n".join(lignes)}


def ajouter_mot(mot) -> dict:
    """« Ajouter aux mots à bien écrire » depuis la lecture (LI-23)."""
    if not isinstance(mot, str) or not mot.strip() or len(mot) > 100:
        return erreur("invalide")
    source = VOCABULAIRE.read_text(encoding="utf-8") if VOCABULAIRE.exists() else ""
    if mot.strip() not in source.splitlines():
        try:
            VOCABULAIRE.write_text(source.rstrip("\n") + ("\n" if source.strip() else "") + mot.strip() + "\n",
                                   encoding="utf-8")
        except OSError:
            return erreur("protege")
    return {"ok": True}


# ---------------------------------------------------------------- enregistrement : actions

def renommer_reunion(o: dict) -> dict:
    """Change le titre du dossier ; la date et l'heure (AAMMJJ-HHMM) restent en tête."""
    d = dossier_de(o.get("reunion", ""))
    titre = str(o.get("titre") or "").strip()
    if not d or not titre:
        return erreur("reunion_introuvable" if not d else "invalide", d)
    m = re.match(r"\d{6}-\d{4}", d.name)
    cible = d.with_name(f"{m[0]}-{slug(titre)}" if m else slug(titre))
    with verrou:
        if code := occupee(d):
            return erreur(code, d)
        if cible != d:
            if cible.exists():
                return erreur("nom_pris", d)
            try:
                d.rename(cible)
            except OSError as e:  # Windows : fichier ouvert (lecture audio, dossier affiché)
                noter(f"Renommage de {d.name} impossible : {e}")
                return erreur("protege", d)
            if d.name in travaux:
                travaux[cible.name] = travaux.pop(d.name)
    return {"ok": True, "nom": cible.name}


def supprimer(o: dict) -> dict:
    d = dossier_de(o.get("reunion", ""))
    if not d or not est_reunion(d):  # jamais un dossier qui n'a pas l'air d'une réunion
        return erreur("reunion_introuvable")
    with verrou:
        if code := occupee(d):
            return erreur(code, d)
        try:
            shutil.rmtree(d)
        except OSError as e:  # Windows : fichier encore ouvert (lecture audio en cours)
            noter(f"Suppression incomplète de {d} : {e}")
            return erreur("protege", d)
        travaux.pop(d.name, None)
    return {"ok": True}


def relancer() -> dict:
    """« Relancer l'enregistrement » (EN-07) : arrête, garde, démarre une nouvelle réunion."""
    fin = arreter()
    if "erreur" in fin:
        return fin
    return {**demarrer({}), "precedente": fin["nom"]}


def activer_direct() -> dict:
    """« L'activer quand même » (DI-08), pour l'enregistrement en cours."""
    with verrou:
        e = enregistrement
        if not e:
            return erreur("aucun_enregistrement")
        lancer_direct(e)
    return {"ok": True}


# ---------------------------------------------------------------- essai du son

def lancer_test_son(o: dict) -> dict:
    """Essai du micro et du son de l'ordinateur, sans réunion (PL-07, PL-09, RG-11, RG-12) : l'enregistreur tourne
    quelques secondes dans un dossier jetable ; la page lit les niveaux dans /api/etat. Sur Mac, c'est aussi ce
    qui fait apparaître la demande d'accès du système."""
    global test_son
    if o.get("arreter"):
        if test_son:
            test_son["fin"] = 0
        return {"ok": True}
    secondes = o.get("secondes", 10)
    if not isinstance(secondes, (int, float)) or isinstance(secondes, bool) or not 1 <= secondes <= 15:
        return erreur("invalide")
    with verrou:
        if enregistrement or (test_son and test_son["en_cours"]):
            return erreur("deja_en_cours")
        d = Path(tempfile.mkdtemp(prefix="reunion-essai-"))
        t = test_son = {"en_cours": True, "niveau_micro": None, "niveau_systeme": None, "micro_entendu": False,
                        "son_entendu": False, "erreur": None, "fin": time.time() + secondes}

    def tourner():
        try:
            proc = lancer_capture(d)
            if not capture_prete(proc, d):
                t["erreur"] = erreur(cause_demarrage((d / JOURNAL).read_text(encoding="utf-8", errors="replace")))
                return
            while time.time() < t["fin"] and proc.poll() is None:
                t["niveau_micro"], t["niveau_systeme"] = niveau_db(d / "micro.wav"), niveau_db(d / "systeme.wav")
                t["micro_entendu"] |= (t["niveau_micro"] or -200) > ENTENDU_DB
                t["son_entendu"] |= (t["niveau_systeme"] or -200) > ENTENDU_DB
                time.sleep(0.25)
            stopper(proc)
            r = reglages.lire()
            maj = {k: "ok" for k, ok in (("etat_micro", t["micro_entendu"]), ("etat_son", t["son_entendu"])) if ok}
            if maj:
                reglages.ecrire({**r, **maj})
        except Exception as e:
            noter(f"Essai du son impossible : {e}")
            t["erreur"] = erreur("peripherique")
        finally:
            t["en_cours"] = False
            shutil.rmtree(d, ignore_errors=True)

    threading.Thread(target=tourner, daemon=True).start()
    return {"ok": True}


def installer_pyannote() -> dict:
    """Clé rangée mais pyannote absent (facultatif partout, plus de 1 Go) : installation à la demande, en arrière-plan."""
    global installation_pyannote
    with verrou:
        if installation_pyannote and installation_pyannote["en_cours"]:
            return erreur("telechargement_en_cours")
        travail = installation_pyannote = {"en_cours": True, "code": None, "ligne": "", "echec": None}
    commande = ([PY, "-m", "pip", "install", "pyannote.audio>=4"] if importlib.util.find_spec("pip")
                else ["uv", "pip", "install", "--python", PY, "pyannote.audio>=4"])

    def tourner():
        try:
            r = subprocess.run(commande, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               **SANS_FENETRE)
            travail["code"] = r.returncode
            noter(f"Installation de pyannote : code {r.returncode} {r.stderr[-500:]}")
            importlib.invalidate_caches()
        except OSError as e:
            noter(f"Installation de pyannote impossible : {e}")
            travail["code"] = 1
        finally:
            travail["en_cours"] = False

    threading.Thread(target=tourner, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------- préparation : téléchargement et vitesse

def separation_disponible() -> bool:
    return separation.methode() is not None


def mo_separation() -> int:
    """Taille des fichiers de séparation des voix encore à télécharger, 0 s'ils sont là ou indisponibles."""
    m = separation.methode()
    return 0 if not m or separation.prete(m) else separation.METHODES[m][1]


def modele_voix() -> str:
    """Banque des voix connues de la méthode de séparation en cours."""
    return separation.modele_empreintes(separation.methode())


def lancer_preparation(o: dict) -> dict:
    """Téléchargement des fichiers de transcription (sans compte ni clé), de la séparation des voix, puis essai de
    vitesse (PL-03, PL-11 à PL-16, RG-09). Reprend seul au retour du réseau."""
    global preparation
    r = reglages.lire()
    if o.get("reessayer") and preparation and preparation["en_cours"]:
        reveil.set()
        return {"ok": True}
    taille = o.get("modele") or transcribe.taille_modele(r)
    if taille not in transcribe.MODELES:
        return erreur("invalide")
    moteur = transcribe.choix_modele(r)[0]
    repo, mo = transcribe.MODELES[taille][moteur]
    total = (0 if transcribe.en_cache(repo) else mo) + mo_separation()
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        libre = shutil.disk_usage(BASE if BASE.exists() else Path.home()).free
    except OSError:
        libre = None
    if libre is not None and libre < total * 1e6 + ESPACE_MINI:
        return erreur("place_preparation", besoin=go(total * 1e6 + ESPACE_MINI), libre=go(libre))
    with verrou:
        if preparation and preparation["en_cours"]:
            return erreur("telechargement_en_cours")
        p = preparation = {"en_cours": True, "etape": "telechargement", "modele": taille, "depot": repo,
                           "mo": mo, "total_mo": total or mo, "erreur": None, "separation_ok": True, "code": None,
                           "echec": None, "ligne": ""}
    reveil.clear()
    threading.Thread(target=preparer, args=(p, moteur, taille, repo), daemon=True).start()
    return {"ok": True}


def preparer(p: dict, moteur: str, taille: str, repo: str) -> None:
    try:
        while not transcribe.en_cache(repo):
            p["echec"] = None
            if executer(p, [str(ICI / "transcribe.py"), "--telecharger", "--moteur", moteur, "--modele", taille],
                        LOG) == 0:
                noter_telechargement(repo)
                break
            if p["echec"] != "reseau_absent":
                p["erreur"] = p["echec"] if p["echec"] in CAUSES else "inconnue"
                return
            p["erreur"] = "reseau_absent"  # PL-13 : en pause, reprise seule toutes les 30 s ou sur « Réessayer »
            reveil.wait(30)
            reveil.clear()
            p["erreur"] = None
        m = separation.methode()
        if m and not separation.prete(m):
            p["separation_ok"] = any(executer(p, [str(ICI / "transcribe.py"), "--telecharger-separation"], LOG) == 0
                                     for _ in range(3))  # PL-16 : trois essais, puis on continue sans
            p["separation_echec"] = None if p["separation_ok"] else p["echec"]
        p["etape"] = "preparation"
        if ESSAI_VITESSE.exists():
            p["etape"] = "vitesse"
            if executer(p, [str(ICI / "transcribe.py"), "--essai-vitesse", str(ESSAI_VITESSE), "--modele", taille],
                        LOG) == 0 and p.get("k"):
                noter_vitesse(taille, p["k"])
        p["etape"] = "fini"
        for d in sorted(BASE.glob("*/transcription.attente")):  # réunions enregistrées pendant la préparation
            lancer_transcription(d.parent, lire_json(d) or {})
    except Exception as e:
        noter(f"Préparation interrompue : {e}")
        p["erreur"] = "inconnue"
    finally:
        p["en_cours"] = False


def etat_preparation() -> dict | None:
    p = preparation
    if not p:
        return None
    fait = (transcribe.taille_cache(p["depot"]) + separation.octets_recus(separation.methode())) / 1e6
    return {k: p.get(k) for k in ("en_cours", "etape", "modele", "mo", "total_mo", "separation_ok",
                                  "separation_echec")} | {
        "fait_mo": round(min(fait, p["total_mo"])), "erreur": p["erreur"] and erreur(p["erreur"])["erreur"]}


# ---------------------------------------------------------------- état et configuration

def manque() -> list[dict]:
    """Ce qui empêche d'enregistrer (bloquant) ou ce qui est conseillé (AC-05 à AC-08, ER-07, ER-08, ER-12). Le
    libellé est choisi par la page d'après le code ; agenda, séparation et clé n'y figurent jamais."""
    r, m = reglages.lire(), []
    enregistreur = (ICI / "meetrec").exists() if MAC else importlib.util.find_spec("soundcard") is not None
    if not enregistreur or not shutil.which("ffmpeg"):
        m.append({"code": "installation", "niveau": "bloquant", "action": "telecharger"})
    try:
        cible = BASE if BASE.exists() else BASE.parent
        libre = shutil.disk_usage(cible).free
        if not os.access(cible, os.W_OK):
            m.append({"code": "dossier", "niveau": "bloquant", "action": "choisir_dossier", "chemin": str(BASE)})
        elif libre < ESPACE_MINI:
            m.append({"code": "place", "niveau": "bloquant", "action": "ouvrir_dossier", "libre": go(libre)})
    except OSError:
        m.append({"code": "dossier", "niveau": "bloquant", "action": "choisir_dossier", "chemin": str(BASE)})
    if r["etat_micro"] == "refuse":
        m.append({"code": "micro", "niveau": "bloquant", "action": "reglages_micro"})
    if r["etat_son"] == "refuse":
        m.append({"code": "son", "niveau": "conseille", "action": "reglages_son"})
    return m


def etat() -> dict:
    r = reglages.lire()
    with verrou:
        e = enregistrement
        dr = (e or {}).get("direct") or {}
        enr = e and {
            "nom": e["dossier"].name, "titre": debut_et_titre(e["dossier"].name)[1], "depuis": time.time() - e["debut"],
            "niveau_micro": niveau_db(e["dossier"] / "micro.wav"),
            "niveau_systeme": niveau_db(e["dossier"] / "systeme.wav"),
            "alertes": alertes(e), "arret": e.get("arret"),
            "direct": {k: v for k, v in dr.items() if k != "proc"} | {
                "saut": bool(dr.get("saut")) and time.time() - dr["saut"] < 15}}
        if enr:
            enr["niveau"] = enr["niveau_micro"]
        t = test_son and {k: v for k, v in test_son.items() if k != "fin"}
        return {
            "enregistrement": enr,
            "travaux": {k: {**v, "etapes": (e := etapes_de(v)),
                            "fin_etapes": (v.get("pause") or time.time()) + sum(x["restant_s"] for x in e)}
                        for k, v in travaux.items() if v["en_cours"] or v["code"]},
            "preparation": etat_preparation(),
            "fichiers_prets": fichiers_prets(r),
            "test_son": t,
            "jeton": bool(separation.jeton_hf()),
            "agenda": bool(agenda.url_agenda()),
            "plateforme": sys.platform,
            "manque": manque(),
            "accueil_fini": r["accueil_fini"], "accueil_etape": r["accueil_etape"],
            "nouvelles": sum(1 for k, v in travaux.items() if not v["en_cours"] and v["code"] == 0 and k not in vues),
            "direct_par_defaut": direct_par_defaut(r),
            "installation_pyannote": installation_pyannote and {k: v for k, v in installation_pyannote.items()
                                                                if k != "proc"},
        }


def verifications() -> list[dict]:
    """État de l'installation en mots simples (RG-14), avec ce qui manque."""
    r = reglages.lire()
    moteur, repo, _ = transcribe.choix_modele(r)
    taille = transcribe.taille_modele(r)
    mo = transcribe.MODELES[taille][moteur][1]
    trouve = importlib.util.find_spec
    v = []

    def ajouter(titre: str, ok: bool, si_ok: str, si_manque: str, action: str | None = None) -> None:
        v.append({"titre": titre, "ok": bool(ok), "detail": si_ok if ok else si_manque, "action": None if ok else action})

    ajouter("Enregistrement", (ICI / "meetrec").exists() if MAC else trouve("soundcard"), "prêt", "absent",
            "telecharger")
    ajouter("Conversion du son", shutil.which("ffmpeg"), "prête", "absente", "telecharger")
    moteur_ok = trouve("mlx_whisper" if moteur == "mlx" else "faster_whisper")
    ajouter("Fichiers de transcription", moteur_ok and transcribe.en_cache(repo), f"prêts ({mo} Mo)",
            f"à télécharger ({mo} Mo)" if moteur_ok else "absents", "preparer" if moteur_ok else "telecharger")
    if r["locuteurs"]:
        m = separation.methode(r)
        ajouter("Séparation des voix", m and separation.prete(m), "prête",
                "à télécharger" if m else "indisponible", "preparer" if m else "telecharger")
    try:
        cible = BASE if BASE.exists() else BASE.parent
        libre = shutil.disk_usage(cible).free
        ajouter("Place libre", os.access(cible, os.W_OK) and libre >= ESPACE_MINI, go(libre).replace(",0 Go", " Go"),
                go(libre), "ouvrir_dossier")
    except OSError:
        ajouter("Place libre", False, "", "dossier des réunions inaccessible", "choisir_dossier")
    return v


def capture_cli(*args: str) -> str:
    """Dernière ligne de `capture.py <args>`. soundcard initialise COM dans le thread qui l'importe : appelé
    depuis les threads du serveur, il échoue sous Windows dès la deuxième requête. Un processus par appel."""
    try:
        r = subprocess.run([PY, str(ICI / "capture.py"), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **SANS_FENETRE)
    except subprocess.TimeoutExpired:
        raise RuntimeError("le périphérique ne répond pas") from None
    if r.returncode:
        raise RuntimeError((r.stderr.strip().splitlines() or ["?"])[-1][:300])
    return (r.stdout.strip().splitlines() or [""])[-1]


def qualites(r: dict, moteur: str) -> list[dict]:
    """Trois qualités au plus (RG-09) ; « Très précise » seulement sur une machine puissante à 16 Go ou plus."""
    m = transcribe.machine()
    codes = ["legere", "rapide"] if m["qualite_conseillee"] == "legere" else ["rapide", "precise"]
    if m["qualite_conseillee"] == "precise" and (m["memoire_go"] or 0) >= 16:
        codes.append("tres_precise")
    sortie = []
    for code in codes:
        nom, taille = transcribe.QUALITES[code]
        repo, mo = transcribe.MODELES[taille][moteur]
        sortie.append({"code": code, "nom": nom, "modele": taille, "mo": mo, "telecharge": transcribe.en_cache(repo),
                       "k": round(transcribe.vitesse_k(r, taille)[0], 2),
                       "conseillee": code == m["qualite_conseillee"]})
    return sortie


def configuration() -> dict:
    r = reglages.lire()
    moteur, _, _ = transcribe.choix_modele(r)
    taille = transcribe.taille_modele(r)
    k, mesure = transcribe.vitesse_k(r, taille)
    perif, erreur_perif = None, None
    if not MAC:  # sur Mac, meetrec prend le micro par défaut et tout le son du Mac
        try:
            perif = json.loads(capture_cli("--peripheriques"))
        except Exception as e:
            noter(f"Périphériques audio illisibles : {e}")
            erreur_perif = f"Périphériques audio illisibles : {e}"
    return {
        "reglages": r,
        "dossier": str(BASE),
        "dossier_impose": bool(os.environ.get("REUNION_DIR")),
        "plateforme": sys.platform,
        "coeurs": ressources.coeurs_total(),
        "prenom": transcribe.PRENOM,
        "moi": moi(),
        "machine": transcribe.machine(),
        "modele": taille,
        "qualite": transcribe.qualite_de(taille),
        "qualites": qualites(r, moteur),
        "mo_separation": mo_separation(),
        "vitesse_k": round(k, 3),
        "vitesse_mesuree": mesure,
        "moteur_auto": transcribe.moteur_auto(),
        "moteur": moteur,
        "modeles": [{"taille": t, "mo": m[moteur][1], "telecharge": transcribe.en_cache(m[moteur][0])}
                    for t, m in transcribe.MODELES.items()],
        "langues": LANGUES,
        "langue_systeme": reglages.langue_systeme(),
        "jeton": bool(separation.jeton_hf()),
        "separation_disponible": separation_disponible(),
        "pyannote_installe": importlib.util.find_spec("pyannote") is not None,
        # Dépôt à la main si le réseau bloque le téléchargement de la méthode libre (proxy, antivirus).
        "separation": {"methode": separation.methode(r), "dossier": str(separation.cache_libre()),
                       "a_deposer": separation.a_deposer()},
        # Voix apprises avec l'ancienne méthode : non reconnues par la méthode libre (dit une fois dans la page).
        "voix_anciennes": len(voix.lire_banque(voix.PYANNOTE)) if modele_voix() != voix.PYANNOTE else 0,
        "modele_voix": modele_voix(),
        "peripheriques": perif,
        "erreur_peripheriques": erreur_perif,
        "verifications": verifications(),
        "journal_outil": str(LOG),
        "version": VERSION,
        "page_telechargement": PAGE_TELECHARGEMENT,
    }


def enregistrer_configuration(o: dict) -> dict:
    """Valide puis range les réglages envoyés (seuls les champs présents changent)."""
    global BASE
    r = reglages.lire()
    if "moteur" in o:
        if o["moteur"] not in ("auto", "mlx", "faster"):
            return erreur("invalide")
        if o["moteur"] == "mlx" and not importlib.util.find_spec("mlx_whisper"):
            return erreur("invalide")
        r["moteur"] = o["moteur"]
    if "modele" in o:
        if o["modele"] not in transcribe.MODELES:
            return erreur("invalide")
        r["modele"] = o["modele"]
    if "langue" in o:
        if o["langue"] not in LANGUES:
            return erreur("invalide")
        r["langue"] = o["langue"]
    if o.get("accueil_fini") is False and not reglages.FICHIER.exists():
        r["langue"] = reglages.langue_systeme()  # premier lancement : la langue du système, une fois (Q6)
    for cle in ("locuteurs", "accueil_fini", "priorite_basse"):
        if cle in o:
            r[cle] = bool(o[cle])
    if "ressources" in o:  # appliqué en cours de transcription par transcribe.py (ressources.surveiller)
        if o["ressources"] not in ressources.PARTS or isinstance(o["ressources"], bool):
            return erreur("invalide")
        r["ressources"] = o["ressources"]
    if "direct" in o:
        r["direct"] = None if o["direct"] is None else bool(o["direct"])
    if "accueil_etape" in o:
        if not isinstance(o["accueil_etape"], int) or not 1 <= o["accueil_etape"] <= 5:
            return erreur("invalide")
        r["accueil_etape"] = o["accueil_etape"]
    for cle in ("etat_micro", "etat_son"):
        if cle in o:
            if o[cle] not in ("", "ok", "refuse"):
                return erreur("invalide")
            r[cle] = o[cle]
    for cle in ("micro", "sortie", "moi"):
        if cle in o:
            if not isinstance(o[cle], str) or len(o[cle]) > 300:
                return erreur("invalide")
            r[cle] = o[cle].strip()[:60] if cle == "moi" else o[cle]
    if "dossier" in o:
        texte = str(o["dossier"] or "").strip()
        if texte:
            p = Path(texte).expanduser()
            if not p.is_absolute():
                return erreur("invalide")
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                return erreur("dossier", chemin=p)
            if not os.access(p, os.W_OK):
                return erreur("dossier", chemin=p)
        with verrou:
            if enregistrement:
                return erreur("occupee_enregistrement")
        r["dossier"] = texte
    try:
        reglages.ecrire(r)
    except OSError:
        return erreur("protege")
    BASE = dossier_reunions()
    return {"ok": True}


def enregistrer_jeton(jeton) -> dict:
    """Range la clé d'accès (lisible par toi seul) ; vide, la retire. Elle n'est jamais renvoyée à la page."""
    jeton = (jeton or "").strip() if isinstance(jeton, str) else None
    if jeton is None:
        return erreur("invalide")
    if not jeton:
        separation.FICHIER_JETON.unlink(missing_ok=True)
        return {"ok": True}
    if not re.fullmatch(r"hf_[A-Za-z0-9]{20,}", jeton):
        return erreur("cle")
    reglages.ecrire_secret(separation.FICHIER_JETON, jeton)
    return {"ok": True}


def tester_niveau(o: dict) -> dict:
    if MAC:
        return erreur("invalide")
    if o.get("quoi") not in ("micro", "sortie") or not isinstance(o.get("id", ""), str):
        return erreur("invalide")
    try:
        return {"ok": True, "db": float(capture_cli("--niveau", o["quoi"], o.get("id", "")))}
    except Exception as e:
        noter(f"Test du niveau impossible : {e}")
        return erreur("aucun_micro" if "Aucun micro" in str(e) else "peripherique")


def lire_voix() -> list[dict]:
    """Voix connues des deux banques (méthode libre et pyannote), chacune avec son modèle."""
    sortie = []
    for modele, _ in separation.METHODES.values():
        try:
            banque = voix.lire_banque(modele)
        except (OSError, ValueError):
            continue
        sortie += [{"nom": n, "reunions": v.get("n", 1), "vu": v.get("vu"), "modele": modele}
                   for n, v in sorted(banque.items())]
    return sortie


def oublier_voix(nom, modele=None) -> dict:
    """Oublie la voix dans la banque indiquée, sinon dans toutes celles où elle figure."""
    if not isinstance(nom, str):  # une liste n'est pas hachable : TypeError sinon
        return erreur("voix_inconnue")
    trouvee = False
    for m, _ in separation.METHODES.values():
        banque = voix.lire_banque(m)
        if (modele in (None, m)) and nom in banque:
            del banque[nom]
            voix.ecrire_banque(banque, m)
            trouvee = True
    return {"ok": True} if trouvee else erreur("voix_inconnue")


def enregistrer_vocabulaire(texte) -> dict:
    if not isinstance(texte, str):
        return erreur("invalide")
    if len(texte) > 100_000:
        return erreur("trop_long")
    try:
        VOCABULAIRE.write_text(texte, encoding="utf-8")
    except OSError:
        return erreur("protege")
    return {"ok": True}


# ---------------------------------------------------------------- mise à jour et effacement

def version_nombres(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def mise_a_jour() -> dict:
    """Dernière version publiée sur la page du projet : rien d'autre n'est envoyé (MD-01 à MD-04)."""
    try:
        with urllib.request.urlopen(f"https://api.github.com/repos/{DEPOT}/releases/latest", timeout=10) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # aucune version publiée : celle-ci est la dernière
            return {"ok": True, "version": VERSION, "derniere": VERSION, "a_jour": True}
        return erreur("reseau_filtre")
    except (urllib.error.URLError, OSError, ValueError):
        return erreur("reseau_absent")
    derniere = str(data.get("tag_name") or VERSION).lstrip("vV")
    return {"ok": True, "version": VERSION, "derniere": derniere,
            "a_jour": version_nombres(derniere) <= version_nombres(VERSION),
            "page": page if (page := str(data.get("html_url") or "")).startswith("https://github.com/")
            else PAGE_TELECHARGEMENT}


def depots_modeles() -> list[str]:
    return [repo for m in transcribe.MODELES.values() for repo, _ in m.values()] + [transcribe.MODELE_DIAR]


def taille_dossier(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def tailles_effacement() -> dict:
    reunions = [d for d in BASE.iterdir() if d.is_dir() and est_reunion(d)] if BASE.exists() else []
    return {"modeles_mo": round(sum(transcribe.taille_cache(r) for r in depots_modeles()) / 1e6),
            "reunions_go": round(sum(taille_dossier(d) for d in reunions) / 1e9, 1), "reunions": len(reunions)}


def effacer(o: dict) -> dict:
    """« Effacer les fichiers de Réunion » (MD-09, MD-10) : fichiers de transcription et de séparation, réglages,
    voix connues, clés et journaux ; les réunions seulement sur demande, dossier par dossier."""
    if o.get("confirme") is not True:
        return erreur("invalide")
    with verrou:
        if enregistrement or any(v["en_cours"] for v in travaux.values()):
            return erreur("occupee_transcription" if not enregistrement else "occupee_enregistrement")
        for repo in reglages.lire()["telecharges"]:  # jamais un modèle déjà présent avant Réunion
            if repo in depots_modeles():
                shutil.rmtree(transcribe.depot_cache(repo), ignore_errors=True)
        for f in (reglages.FICHIER, separation.FICHIER_JETON, agenda.FICHIER_URL,
                  *(voix.fichier_banque(m) for m, _ in separation.METHODES.values())):
            f.unlink(missing_ok=True)
        shutil.rmtree(separation.cache_libre(), ignore_errors=True)
        shutil.rmtree(LOG.parent, ignore_errors=True)
        if o.get("reunions") is True and BASE.exists():
            for d in [d for d in BASE.iterdir() if d.is_dir() and est_reunion(d)]:  # jamais un autre dossier
                shutil.rmtree(d, ignore_errors=True)
            try:
                BASE.rmdir()
            except OSError:
                pass
    return {"ok": True}


# ---------------------------------------------------------------- HTTP

class Gestionnaire(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def sur(self) -> bool:
        if self.headers.get("Host") not in HOTES:
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):  # navigateur, autre site
            return False
        origine = self.headers.get("Origin")
        return origine is None or origine.split("//", 1)[-1] in HOTES

    def end_headers(self):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        super().end_headers()

    def session(self) -> bool:
        """Cookie de session exigé sur /api/* et /ecoute/* (S2)."""
        if JETON is None:
            return True
        biscuits = dict(c.strip().split("=", 1) for c in self.headers.get("Cookie", "").split(";") if "=" in c)
        return secrets.compare_digest(biscuits.get("reunion", ""), JETON)

    def json(self, data, code=200):
        corps = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def octets(self, corps: bytes, type_: str, nom: str):
        self.send_response(200)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(nom)}")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def fichier(self, chemin: Path, type_: str):
        """Sert un fichier avec prise en charge de Range : sans elle, le lecteur audio ne sait pas avancer."""
        taille = chemin.stat().st_size
        debut, fin = 0, taille - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):
            if m[1]:
                debut, fin = int(m[1]), min(int(m[2]) if m[2] else fin, fin)
            else:
                debut = max(taille - int(m[2]), 0)
            if debut > fin:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{taille}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {debut}-{fin}/{taille}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", type_)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fin - debut + 1))
        self.end_headers()
        with chemin.open("rb") as f:
            f.seek(debut)
            reste = fin - debut + 1
            while reste > 0 and (bloc := f.read(min(reste, 1 << 16))):
                self.wfile.write(bloc)
                reste -= len(bloc)

    def do_GET(self):
        if not self.sur():
            return self.send_error(403)
        chemin, _, requete = self.path.partition("?")
        chemin, params = unquote(chemin), parse_qs(requete)  # noms de réunion accentués
        if chemin == "/" and JETON and secrets.compare_digest((params.get("cle") or [""])[0], JETON):
            # Adresse ouverte par l'icône : le jeton passe en cookie et quitte la barre d'adresse.
            self.send_response(303)
            self.send_header("Set-Cookie", f"reunion={JETON}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if chemin.startswith(("/api/", "/ecoute/")) and not self.session():
            return self.json(erreur("session"), 401)
        if chemin in STATIQUES:
            nom, type_ = STATIQUES[chemin]
            return self.fichier(ICI / "ui" / nom, type_)
        routes = {
            "/api/etat": etat, "/api/reunions": lister, "/api/configuration": configuration, "/api/voix": lire_voix,
            "/api/effacer": tailles_effacement,
            "/api/recherche": lambda: recherche((params.get("q") or [""])[0]),
            "/api/journal": lambda: journal(params),
        }
        if chemin in routes:
            return self.json(routes[chemin]())
        if chemin == "/api/vocabulaire":
            # Pas encore de vocabulaire : l'exemple sert de modèle (il n'est rangé qu'au premier enregistrement).
            source = VOCABULAIRE if VOCABULAIRE.exists() else VOCABULAIRE.with_name("vocabulaire.exemple.txt")
            texte = source.read_text(encoding="utf-8") if source.exists() else ""  # commentaires # : pour les développeurs
            return self.json({"texte": "".join(l for l in texte.splitlines(True) if not l.startswith("#"))})
        if m := re.fullmatch(r"/api/reunion/([\w-]+)", chemin):
            d = dossier_de(m[1])
            return self.json(detail(d) if d else erreur("reunion_introuvable"), 200 if d else 404)
        if m := re.fullmatch(r"/ecoute/([\w-]+)\.m4a", chemin):
            d = dossier_de(m[1])
            if d and (d / "ecoute.m4a").exists():
                return self.fichier(d / "ecoute.m4a", "audio/mp4")
        self.send_error(404)

    def do_POST(self):
        # Content-Type JSON imposé : un formulaire d'un autre site ne peut pas l'envoyer sans pré-vérification.
        if not self.sur() or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_error(403)
        if not self.session():
            return self.json(erreur("session"), 401)
        try:
            taille = int(self.headers.get("Content-Length") or 0)
            if not 0 <= taille <= 1_000_000:  # 1 Mo au plus, rien de négatif
                return self.json(erreur("trop_long"), 413)
            o = json.loads(self.rfile.read(taille) or b"{}")
        except ValueError:
            return self.json(erreur("invalide"), 400)
        if not isinstance(o, dict):  # une liste ou un nombre ferait planter chaque route (o.get)
            return self.json(erreur("invalide"), 400)
        if self.path == "/api/export":
            r = exporter(o)
            return self.json(r) if isinstance(r, dict) else self.octets(r[0], r[2], r[1])
        if self.path == "/api/reglages":
            if o.get("quoi") not in REGLAGES:
                return self.json(erreur("invalide"))
            ouvrir(REGLAGES[o["quoi"]])
            return self.json({"ok": True})
        if self.path == "/api/agenda":
            return self.json(erreur("agenda") if agenda.enregistrer_url(str(o.get("url") or "")) else {"ok": True})
        routes = {
            "/api/demarrer": demarrer, "/api/pause": mettre_en_pause, "/api/arreter": lambda o: arreter(), "/api/apercu": lambda o: apercu(),
            "/api/relancer": lambda o: relancer(), "/api/direct": lambda o: activer_direct(),
            "/api/renommer": lambda o: modifier(o, _renommer),
            "/api/voix/accepter": lambda o: modifier(o, _accepter),
            "/api/voix/refuser": lambda o: modifier(o, _refuser),
            "/api/regrouper": lambda o: modifier(o, _regrouper),
            "/api/annuler-regroupement": annuler_regroupement,
            "/api/attribuer-ligne": lambda o: modifier(o, _attribuer_ligne),
            "/api/ligne": lambda o: modifier(o, _ligne),
            "/api/note": note, "/api/participants": participants,
            "/api/configuration": enregistrer_configuration,
            "/api/jeton": lambda o: enregistrer_jeton(o.get("jeton")),
            "/api/vocabulaire": lambda o: enregistrer_vocabulaire(o.get("texte")),
            "/api/mots": lambda o: ajouter_mot(o.get("mot")),
            "/api/voix/oublier": lambda o: oublier_voix(o.get("nom"), o.get("modele")),
            "/api/telecharger": lancer_preparation, "/api/preparer": lancer_preparation,
            "/api/test-son": lancer_test_son, "/api/niveau": tester_niveau,
            "/api/installer-pyannote": lambda o: installer_pyannote(),
            "/api/renommer-reunion": renommer_reunion, "/api/supprimer": supprimer,
            "/api/mise-a-jour": lambda o: mise_a_jour(), "/api/effacer": effacer,
        }
        if self.path in routes:
            return self.json(routes[self.path](o))
        if self.path == "/api/ouvrir":
            if o.get("cible") in ("reunions", "journal_outil"):
                ouvrir(str(BASE)) if o["cible"] == "reunions" else montrer(LOG)
                return self.json({"ok": True})
            d = dossier_de(o.get("reunion", ""))
            if not d:
                return self.json(erreur("reunion_introuvable"), 404)
            montrer(journal_de(d)) if o.get("journal") else ouvrir(str(d))
            return self.json({"ok": True})
        if self.path == "/api/transcrire":
            d = dossier_de(o.get("reunion", ""))
            if not d:
                return self.json(erreur("reunion_introuvable"), 404)
            o = {k: v for k, v in o.items() if k in ("personnes", "chuchote", "langue", "pistes", "separation_seule")}
            if o.get("separation_seule") is not True:
                o.pop("separation_seule", None)
            if "personnes" in o and (not isinstance(o["personnes"], int) or not 1 <= o["personnes"] <= 30):
                return self.json(erreur("invalide", d))
            if "pistes" in o and o["pistes"] not in ("deux", "micro", "systeme"):
                return self.json(erreur("invalide", d))
            return self.json(retranscrire(d, o) or {"ok": True})
        self.send_error(404)


class Serveur(ThreadingHTTPServer):
    # Sous Windows, SO_REUSEADDR laisse un second serveur prendre le port déjà écouté : deux instances, et
    # « Arrêter » tombe sur celle qui n'enregistre pas. Ailleurs, il sert seulement à relancer tout de suite.
    allow_reuse_address = sys.platform != "win32"


def reprendre() -> None:
    """Au lancement : transcriptions interrompues par l'arrêt de l'outil (TR-09), réunions en attente des fichiers
    de transcription (TR-13), téléchargement d'une suite d'accueil commencée (PL-17)."""
    if not BASE.exists():
        return
    for f in sorted(BASE.glob("*/transcription.en-cours")):
        noter(f"Reprise de la transcription interrompue : {f.parent.name}")
        lancer_transcription(f.parent, lire_json(f) or {}, reprise=True)
    r = reglages.lire()
    m = separation.methode(r)
    if not fichiers_prets(r) and (r["accueil_etape"] >= 2 or list(BASE.glob("*/transcription.attente"))) or \
            r["accueil_fini"] and m and not separation.prete(m):  # PL-16 : « réessaiera au prochain lancement »
        lancer_preparation({})
    elif fichiers_prets(r):
        for f in sorted(BASE.glob("*/transcription.attente")):
            lancer_transcription(f.parent, lire_json(f) or {})


def ouvrir_serveur() -> Serveur | None:
    """Serveur prêt à servir, ou None si une instance tourne déjà (sa page est alors ouverte). Partagé par la
    console (main) et l'icône de la zone de notification sous Windows (zone.py)."""
    global JETON
    url = f"http://127.0.0.1:{PORT}"
    try:
        serveur = Serveur(("127.0.0.1", PORT), Gestionnaire)
    except OSError as e:
        if e.errno != errno.EADDRINUSE and getattr(e, "winerror", None) != 10048:  # WSAEADDRINUSE
            raise
        # Déjà lancé (autre fenêtre, double-clic répété) : on ouvre la page de l'instance qui tourne.
        print(f"Réunion tourne déjà : {url}")
        if not os.environ.get("REUNION_SANS_NAVIGATEUR"):
            webbrowser.open(adresse_page(lire_session()))
        return None
    JETON = secrets.token_urlsafe(24)
    try:
        reglages.ecrire_secret(FICHIER_SESSION, JETON)
    except OSError as e:
        noter(f"Jeton de session non rangé : {e}")
    # Ce qui manque est affiché dans la page ; ici, seulement une trace dans le journal de l'outil.
    for v in verifications():
        if not v["ok"]:
            noter(f"À régler : {v['titre']} - {v['detail']}")
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        BASE.chmod(0o700)  # enregistrements et transcriptions : pas lisibles par les autres comptes
    except OSError as e:  # dossier choisi devenu inaccessible (disque débranché) : à changer dans les Réglages
        noter(f"Dossier des réunions inaccessible ({BASE}) : {e}")
    reprendre()
    print(f"Interface : {url}  (Ctrl+C pour quitter)")
    if not os.environ.get("REUNION_SANS_NAVIGATEUR"):
        webbrowser.open(adresse_page(JETON))
    return serveur


def lire_session() -> str | None:
    try:
        return FICHIER_SESSION.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def adresse_page(jeton: str | None, ancre: str = "") -> str:
    """Adresse de la page, avec le jeton de session qui sera changé en cookie."""
    return f"http://127.0.0.1:{PORT}" + (f"/?cle={jeton}" if jeton else "") + ancre


def garder_enregistrement() -> None:
    """À la fermeture : l'enregistreur finalise ses fichiers ; la transcription se lance depuis la page."""
    for t in list(travaux.values()):  # un processus gelé ne recevrait pas l'ordre de s'arrêter (SIGTERM)
        if t.get("pause") and t.get("pid"):
            pause.geler(t["pid"], False)
    for p in list(enfants):  # repris au lancement suivant (témoin transcription.en-cours, TR-09)
        p.terminate()
    if enregistrement:
        couper_direct(enregistrement)
        stopper(enregistrement["proc"])
        noter(f"Enregistrement gardé dans {enregistrement['dossier']}, à transcrire depuis la page.")


def main() -> int:
    serveur = ouvrir_serveur()
    if not serveur:
        return 0
    # Arrêt par l'app de la barre des menus (SIGTERM) traité comme un Ctrl+C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        garder_enregistrement()
    return 0


if __name__ == "__main__":
    sys.exit(main())
