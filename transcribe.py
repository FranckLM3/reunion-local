#!/usr/bin/env python3
"""Transcrit un enregistrement (systeme.wav + micro.wav), avec séparation des voix.

Tout tourne en local : mlx-whisper (Mac Apple Silicon) ou faster-whisper (Windows, Linux, Mac Intel)
pour le texte, separation.diariser pour les voix (méthode libre par défaut, pyannote avec une clé). Moteur, modèle, langue et séparation des voix viennent
des réglages (écran Configuration) ; les options ci-dessous les remplacent pour une transcription.
Les modèles sont téléchargés une fois, ensuite le mode hors ligne est forcé.

Usage :
  transcribe.py <dossier>                      réunion à distance (Meet, etc.)
  transcribe.py <dossier> --presentiel         plusieurs personnes dans la pièce (distants gardés)
  transcribe.py <dossier> --nb 4               nombre de participants distants, si connu
  transcribe.py <dossier> --sans-locuteurs     sans séparation des voix (« Moi » / « Les autres »)
  transcribe.py <dossier> --moteur faster --modele small --langue en
  transcribe.py --telecharger                  télécharge les modèles à l'avance
"""

import argparse
import errno
import functools
import getpass
import importlib.util
import json
import math
import os
import platform
import re
import struct
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

import reglages
import separation
import voix
from separation import diariser, jeton_hf  # noqa: F401  (diariser : point d'entrée unique, remplaçable)

# Taille Whisper -> moteur -> (dépôt Hugging Face, téléchargement en Mo relevé sur Hugging Face le 03/11/2026).
MODELES = {
    "tiny": {"mlx": ("mlx-community/whisper-tiny-mlx", 74), "faster": ("Systran/faster-whisper-tiny", 78)},
    "base": {"mlx": ("mlx-community/whisper-base-mlx", 144), "faster": ("Systran/faster-whisper-base", 148)},
    "small": {"mlx": ("mlx-community/whisper-small-mlx", 481), "faster": ("Systran/faster-whisper-small", 486)},
    "medium": {"mlx": ("mlx-community/whisper-medium-mlx", 1525), "faster": ("Systran/faster-whisper-medium", 1531)},
    "large-v3": {"mlx": ("mlx-community/whisper-large-v3-mlx", 3084),
                 "faster": ("Systran/faster-whisper-large-v3", 3091)},
    "large-v3-turbo": {"mlx": ("mlx-community/whisper-large-v3-turbo", 1614),
                       "faster": ("mobiuslabsgmbh/faster-whisper-large-v3-turbo", 1622)},
}
MODELE = MODELES["large-v3-turbo"]["mlx"][0]
# Révisions épinglées (S4) : un dépôt modifié en amont n'est jamais pris sans relecture. Relevées le 03/11/2026 dans
# le cache. ponytail: seulement les dépôts dont la révision est connue ; les autres suivent « main ».
# Révisions figées (commit Hugging Face, relevées le 03/11/2026) : un dépôt modifié ne change rien ici.
REVISIONS = {
    "mlx-community/whisper-large-v3-turbo": "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
    "pyannote/speaker-diarization-community-1": "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee",
    "mlx-community/whisper-tiny-mlx": "6caf9c55601caafbe6508a8b0d216bdf4783c4e8",
    "mlx-community/whisper-base-mlx": "1e3e249fb8d01c655324bd6841b1deadffd6d04c",
    "mlx-community/whisper-small-mlx": "45f3915923c7a79a5a5b5a7d909d39aeb0e5630e",
    "mlx-community/whisper-medium-mlx": "7fc08c4eac4c316526498f147dfdee6f6303f975",
    "mlx-community/whisper-large-v3-mlx": "49e6aa286ad60c14352c404340ded53710378a11",
    "Systran/faster-whisper-tiny": "d90ca5fe260221311c53c58e660288d3deb8d356",
    "Systran/faster-whisper-base": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
    "Systran/faster-whisper-small": "536b0662742c02347bc0e980a01041f333bce120",
    "Systran/faster-whisper-medium": "08e178d48790749d25932bbc082711ddcfdfbc4f",
    "Systran/faster-whisper-large-v3": "edaa852ec7e145841d8ffdb056a99866b5f0a478",
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
}
MODELE_DIAR = separation.MODELE_PYANNOTE
ICI = Path(__file__).resolve().parent
if sys.platform == "win32":
    # ffmpeg.exe est rangé à côté du Python de l'environnement par install.ps1 ; les sous-processus en héritent.
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")


def prenom() -> str:
    """Prénom du compte pour la piste micro : nom complet sur Mac et Linux, identifiant de session sous Windows."""
    try:
        import pwd

        return (pwd.getpwuid(os.getuid()).pw_gecos.split() or ["Moi"])[0]
    except (ImportError, KeyError):
        return getpass.getuser() or "Moi"


PRENOM = prenom()

# Niveau sous lequel une tranche ne peut pas contenir de parole (la voix tourne autour de -40 à -10 dBFS).
SEUIL_SILENCE_DB = -55.0
TRAME = 0.1  # secondes par point de l'enveloppe sonore
# Coupe le grave et l'aigu hors voix, débruite, puis remonte le niveau par fenêtres de 3 s (gain max x40).
FILTRE_REHAUSSE = "highpass=f=150,lowpass=f=6000,afftdn=nf=-30,dynaudnorm=f=150:g=15:p=0.9:m=40"

# Phrases que Whisper invente sur les silences en français.
HALLUCINATIONS = re.compile(
    r"(sous-titr|amara\.org|merci d'avoir regardé|abonnez-vous|"
    r"n'oubliez pas de vous abonner|ST' ?501|radio-canada)",
    re.IGNORECASE,
)
# Segments entiers que Whisper produit sur un chuchotement ou un bruit (relevé le 13/10 : un toutes les 30 s).
SEULS = re.compile(r"(merci( beaucoup| à tous)?|\.+|…)[.!]?", re.IGNORECASE)


# ---------------------------------------------------------------- modèles

def depot_cache(repo: str) -> Path:
    cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    return cache / ("models--" + repo.replace("/", "--"))


# Poids de pyannote, dans les sous-dossiers de son dépôt (relevé dans le cache le 03/11/2026).
POIDS_DIAR = separation.POIDS_PYANNOTE


def en_cache(repo: str) -> bool:
    """Vrai si les poids sont là : un téléchargement interrompu laisse config.json sans les poids, et des
    fichiers *.incomplete dans blobs/."""
    depot = depot_cache(repo)
    if any((depot / "blobs").glob("*.incomplete")):
        return False
    for snap in (depot / "snapshots").glob("*"):
        if repo == MODELE_DIAR:
            if all((snap / f).is_file() for f in POIDS_DIAR):
                return True
        # model.bin : faster-whisper ; weights.npz ou *.safetensors : mlx-whisper.
        elif (snap / "model.bin").is_file() or (snap / "weights.npz").is_file() or \
                any(f.is_file() for f in snap.glob("*.safetensors")):
            return True
    return False


# Qualité affichée -> taille Whisper. « Légère » n'est retenue que sur une machine à moins de 8 Go de mémoire.
QUALITES = {"legere": ("Légère", "base"), "rapide": ("Rapide", "small"), "precise": ("Précise", "large-v3-turbo"),
            "tres_precise": ("Très précise", "large-v3")}
# Temps de calcul / durée de l'audio tant qu'aucune mesure n'existe (spec, section 3.2). ponytail: valeurs
# provisoires, remplacées par la première mesure ; « Précise » sur un PC sans carte graphique n'y figure pas (2).
K_DEFAUT = {("apple", "precise"): 0.5, ("nvidia", "precise"): 0.3, "tres_precise": 1.2, "rapide": 1.0, "legere": 1.5}


def memoire_go() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class Etat(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("reste", ctypes.c_ulonglong * 6)]

            e = Etat(dwLength=ctypes.sizeof(Etat))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(e))
            return e.ullTotalPhys / 2**30
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (OSError, ValueError, AttributeError):
        return None


@functools.cache
def machine() -> dict:
    """Ce qui décide de la qualité : système, puce, mémoire, carte graphique (spec, section 3.1). La première ligne
    du tableau qui s'applique l'emporte ; une mémoire illisible compte pour 8 Go."""
    systeme = {"darwin": "mac", "win32": "windows"}.get(sys.platform, "linux")
    arch = platform.machine().lower()
    puce = ("apple" if arch == "arm64" else "intel") if systeme == "mac" else arch
    carte = "apple" if puce == "apple" else None
    if systeme != "mac" and importlib.util.find_spec("ctranslate2"):
        try:
            import ctranslate2

            carte = "nvidia" if ctranslate2.get_cuda_device_count() > 0 else None
        except Exception:  # bibliothèques CUDA absentes : pas de carte utilisable
            carte = None
    memoire = memoire_go()
    m = memoire if memoire is not None else 8
    if puce == "apple" and m >= 8 or systeme != "mac" and carte == "nvidia":
        qualite = "precise"
    elif systeme != "mac" and m >= 8 or puce == "intel":
        qualite = "rapide"
    else:
        qualite = "legere"
    return {"systeme": systeme, "puce": puce, "memoire_go": memoire and round(memoire, 1), "carte_graphique": carte,
            "qualite_conseillee": qualite}


def qualite_de(modele: str) -> str | None:
    return next((q for q, (_, t) in QUALITES.items() if t == modele), None)


def k_defaut(modele: str) -> float:
    q = qualite_de(modele)
    return K_DEFAUT.get((machine()["carte_graphique"], q)) or K_DEFAUT.get(q) or 2.0


def vitesse_k(r: dict, modele: str) -> tuple[float, bool]:
    """(k, mesuré) : moyenne des 5 dernières mesures de ce modèle, sinon la valeur provisoire."""
    mesures = (r.get("vitesses") or {}).get(modele) or []
    return (sum(mesures) / len(mesures), True) if mesures else (k_defaut(modele), False)


def taille_modele(r: dict) -> str:
    """Taille Whisper des réglages ; vide ou inconnue : celle de la qualité conseillée pour cette machine."""
    return r["modele"] if r.get("modele") in MODELES else QUALITES[machine()["qualite_conseillee"]][1]


def moteur_auto() -> str:
    """mlx-whisper sur Mac Apple Silicon (puce graphique), faster-whisper partout ailleurs."""
    if sys.platform == "darwin" and platform.machine() == "arm64" and importlib.util.find_spec("mlx_whisper"):
        return "mlx"
    return "faster"


def choix_modele(r: dict) -> tuple[str, str, str | None]:
    """(moteur, dépôt, langue) d'après les réglages ; langue None = détection automatique."""
    moteur = moteur_auto() if r["moteur"] == "auto" else r["moteur"]
    return moteur, MODELES[taille_modele(r)][moteur][0], None if r["langue"] == "auto" else r["langue"]


def taille_cache(repo: str) -> int:
    """Octets déjà téléchargés pour un dépôt, fichiers en cours compris : sert à la barre de progression."""
    blobs = depot_cache(repo) / "blobs"
    total = 0
    for f in blobs.glob("*"):
        try:  # un .incomplete renommé à la fin de son téléchargement disparaît pendant le parcours
            total += f.stat().st_size
        except OSError:
            pass
    return total


def telecharger(moteur: str, repo: str) -> None:
    """Fichiers de transcription seulement : aucun compte ni clé n'est demandé (dépôts publics)."""
    print(f"Téléchargement de {repo}...")
    from huggingface_hub import snapshot_download

    snapshot_download(repo, revision=REVISIONS.get(repo))  # révision figée, mlx comme faster-whisper
    print("Fichiers de transcription prêts. Les prochaines transcriptions tourneront hors ligne.")


def essai_vitesse(wav: Path, moteur: str, repo: str, langue: str | None) -> float:
    """Rapport temps de calcul / durée de l'audio sur un extrait livré avec l'outil (spec, section 3.2)."""
    import soundfile as sf
    import time

    duree = sf.info(str(wav)).duration
    debut = time.monotonic()
    transcrire(wav, "", repo, moteur, langue)
    return (time.monotonic() - debut) / duree


# ---------------------------------------------------------------- texte

def taux_wav(wav: Path) -> int:
    """Fréquence d'échantillonnage lue dans l'en-tête WAV (bloc fmt), lisible même pendant l'enregistrement."""
    with wav.open("rb") as f:
        tete = f.read(4096)
    i = tete.find(b"fmt ")
    if i < 0:
        raise ValueError(f"{wav.name} : en-tête WAV illisible")
    return struct.unpack_from("<I", tete, i + 12)[0]


def reparer(wav: Path) -> None:
    """meetrec tué sans pouvoir fermer ses fichiers : l'en-tête annonce 0 trame alors que l'audio est là.
    soundfile lit alors un fichier vide et toute la transcription disparaît ; ffmpeg réécrit l'en-tête."""
    import soundfile as sf

    if sf.info(str(wav)).frames or wav.stat().st_size < 8192:
        return
    print(f"Réparation de {wav.name} (enregistrement interrompu)...")
    tmp = wav.with_name(wav.stem + "-repare.wav")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-c", "copy", str(tmp)], check=True)
    os.replace(tmp, wav)


def lire_prompt(vocab: Path) -> str:
    if not vocab.exists():
        return ""
    termes = [l.strip() for l in vocab.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.startswith("#")]
    return "Réunion de travail. Termes : " + ", ".join(termes) + "." if termes else ""


def enveloppe(wav: Path) -> list[float]:
    """Niveau efficace (RMS) par tranche de TRAME secondes, lu par blocs pour ne pas charger l'heure entière."""
    import numpy as np
    import soundfile as sf

    n = int(sf.info(str(wav)).samplerate * TRAME)
    return [float(np.sqrt(np.mean(b ** 2))) for b in sf.blocks(str(wav), blocksize=n, dtype="float32")]


def part_audible(wav: Path) -> float:
    """Part de la durée où la piste dépasse SEUIL_SILENCE_DB."""
    env = enveloppe(wav)
    seuil = 10 ** (SEUIL_SILENCE_DB / 20)
    return sum(x > seuil for x in env) / len(env) if env else 0.0


def audible_s(wav: Path) -> float:
    """Secondes où la piste dépasse SEUIL_SILENCE_DB."""
    seuil = 10 ** (SEUIL_SILENCE_DB / 20)
    return sum(x > seuil for x in enveloppe(wav)) * TRAME


def audible(env: list[float], debut: float, fin: float) -> bool:
    i = int(debut / TRAME)
    tranche = env[i:max(int(fin / TRAME) + 1, i + 1)]
    return bool(tranche) and 20 * math.log10(max(max(tranche), 1e-10)) > SEUIL_SILENCE_DB


# Horodatage au mot toujours : les segments Whisper englobent les silences qui les précèdent
# (une phrase dite à 18 s peut être datée de 0 s), ce qui casserait l'ordre entre les pistes.
# Mêmes réglages pour les deux moteurs ; seul le nom du seuil de log-probabilité diffère.
OPTIONS_WHISPER = {"condition_on_previous_text": False, "no_speech_threshold": 0.6,
                   "compression_ratio_threshold": 2.4, "word_timestamps": True,
                   "hallucination_silence_threshold": 2.0}


def segments_mlx(wav: Path, prompt: str, repo: str, langue: str | None) -> list[dict]:
    import mlx_whisper

    # verbose=False : barre d'avancement sur la sortie d'erreur, lue par le serveur (« 45%| »).
    # Révision épinglée : le dossier de cette révision, pas « main ».
    epingle = depot_cache(repo) / "snapshots" / REVISIONS.get(repo, "")
    chemin = str(epingle) if repo in REVISIONS and epingle.is_dir() else repo
    return mlx_whisper.transcribe(str(wav), path_or_hf_repo=chemin, language=langue, initial_prompt=prompt or None,
                                  logprob_threshold=-1.0, verbose=False, **OPTIONS_WHISPER)["segments"]


@functools.cache
def modele_faster(repo: str, appareil: str, calcul: str):
    from faster_whisper import WhisperModel
    from huggingface_hub import snapshot_download

    # Dossier local de la révision figée : WhisperModel(repo) irait chercher la dernière version du dépôt.
    return WhisperModel(snapshot_download(repo, revision=REVISIONS.get(repo)), device=appareil, compute_type=calcul)


def segments_faster(wav: Path, prompt: str, repo: str, langue: str | None) -> list[dict]:
    """faster-whisper rendu sous la forme de mlx-whisper. Carte graphique NVIDIA si elle répond,
    sinon processeur en int8 (sous Windows, les bibliothèques CUDA manquent souvent)."""
    import ctranslate2

    essais = [("cpu", "int8")]
    if not os.environ.get("REUNION_CPU") and ctranslate2.get_cuda_device_count() > 0:
        essais.insert(0, ("cuda", "float16"))
    for i, (appareil, calcul) in enumerate(essais):
        try:
            segs, info = modele_faster(repo, appareil, calcul).transcribe(
                str(wav), language=langue, initial_prompt=prompt or None, log_prob_threshold=-1.0,
                vad_filter=False, **OPTIONS_WHISPER)
            duree = getattr(info, "duration", 0) or 0
            sortie = []
            # Consommé ici : une erreur CUDA (bibliothèque absente) ne survient qu'au premier segment.
            for s in segs:
                sortie.append({"text": s.text, "avg_logprob": s.avg_logprob, "compression_ratio": s.compression_ratio,
                               "words": [{"start": w.start, "end": w.end, "word": w.word} for w in s.words or []]})
                if duree:
                    print(f"{min(100, round(s.end / duree * 100))}%|", flush=True)  # même forme que mlx
            return sortie
        except Exception as e:
            if i == len(essais) - 1:
                raise
            print("@repli")  # jamais de repli silencieux (ER-14)
            print(f"  Carte graphique inutilisable ({str(e)[:120]}), passage sur le processeur...")
    return []


def transcrire(wav: Path, prompt: str, repo: str, moteur: str = "mlx", langue: str | None = "fr") -> list[dict]:
    brut = (segments_mlx if moteur == "mlx" else segments_faster)(wav, prompt, repo, langue)
    # Whisper invente du texte sur le silence (« Grazie a tutti », « Sous-titrage... ») et part parfois
    # en boucle (« WB, WB, WB... ») : on écarte les tranches muettes et les segments trop répétitifs.
    env = enveloppe(wav)
    segs = []
    for s in brut:
        t = s["text"].strip()
        mots = [{"debut": w["start"], "fin": w["end"], "mot": w["word"]} for w in s.get("words", [])]
        if not t or not mots or HALLUCINATIONS.search(t) or SEULS.fullmatch(t):
            continue
        # Mêmes seuils que les relances de Whisper : s'il les dépasse encore après toutes ses
        # relances (bruit de fond, souffle), le texte est du charabia.
        if s.get("avg_logprob", 0) < -1.0 or s.get("compression_ratio", 0) > 2.4:
            continue
        mots = [w for w in mots if audible(env, w["debut"], w["fin"])]
        if not mots:
            continue
        t = "".join(w["mot"] for w in mots).strip()
        segs.append({"debut": mots[0]["debut"], "fin": mots[-1]["fin"], "texte": t, "mots": mots})
    return segs


def rehausser(wav: Path) -> Path:
    """Copie du micro amplifiée et débruitée (ffmpeg) : rend lisible ce qui est dit bas ou loin du Mac.

    Le 13/10, un atelier chuchoté ne donnait que des « Merci. » inventés ; après ce filtre, des phrases
    entières. Même durée que l'original : les minutages ne bougent pas."""
    sortie = wav.with_name(wav.stem + "-rehausse.wav")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-af", FILTRE_REHAUSSE,
                    "-ar", "16000", "-ac", "1", str(sortie)], check=True)
    return sortie


# ---------------------------------------------------------------- locuteurs

def locuteur_de(debut: float, fin: float, tours) -> str | None:
    meilleur, recouv = None, 0.0
    for d, f, lbl in tours:
        r = min(fin, f) - max(debut, d)
        if r > recouv:
            meilleur, recouv = lbl, r
    if meilleur is None and tours:
        milieu = (debut + fin) / 2
        meilleur = min(tours, key=lambda x: min(abs(x[0] - milieu), abs(x[1] - milieu)))[2]
    return meilleur


def lisser(morceaux: list[dict]) -> list[dict]:
    """Un mot seul de moins d'une seconde, isolé par la séparation au milieu d'une phrase, reste à la
    personne qui parle juste avant (juste après en début de phrase) : la séparation se trompe surtout là."""
    court = lambda m: m["fin"] - m["debut"] < 1.0 and len(m["texte"].split()) <= 1
    if len(morceaux) > 1 and court(morceaux[0]):
        morceaux = [{**morceaux[1], "debut": morceaux[0]["debut"],
                     "texte": morceaux[0]["texte"] + morceaux[1]["texte"]}] + morceaux[2:]
    sortie: list[dict] = []
    for m in morceaux:
        if sortie and (sortie[-1]["loc"] == m["loc"] or court(m)):
            sortie[-1]["texte"] += m["texte"]
            sortie[-1]["fin"] = m["fin"]
        else:
            sortie.append(m)
    return sortie


def attribuer(segs: list[dict], tours, noms: dict, piste: str) -> list[dict]:
    """Découpe les segments Whisper mot par mot selon les tours de parole pyannote."""
    sortie: list[dict] = []
    for s in segs:
        morceaux: list[dict] = []
        for w in s["mots"]:
            lbl = locuteur_de(w["debut"], w["fin"], tours)
            # Libellé provisoire : les numéros définitifs suivent l'ordre de parole sur toute la réunion (numeroter).
            nom = noms.setdefault((piste, lbl), f"?{len(noms) + 1}") if lbl else "Inconnu"
            # Un mot que Whisper coupe en deux (« C » puis « 'est ») reste à la même personne.
            if morceaux and (morceaux[-1]["loc"] == nom or not w["mot"][:1].isspace()):
                morceaux[-1]["texte"] += w["mot"]
                morceaux[-1]["fin"] = w["fin"]
            else:
                morceaux.append({"debut": w["debut"], "fin": w["fin"], "loc": nom, "texte": w["mot"]})
        sortie += lisser(morceaux)
    for o in sortie:
        o["texte"] = o["texte"].strip()
    return [o for o in sortie if o["texte"]]


# ---------------------------------------------------------------- fusion

def mots_de(texte: str) -> list[str]:
    return re.findall(r"\w+", texte.lower())


def retirer_echo(micro: list[dict], systeme: list[dict]) -> list[dict]:
    """Sans casque, le micro reprend la voix des autres : on retire ces doublons.

    La piste système est souvent découpée par locuteur en morceaux plus courts que le segment
    micro : on mesure la part du texte micro retrouvée dans tout le texte système voisin."""
    garde = []
    for m in micro:
        voisin = mots_de(" ".join(s["texte"] for s in systeme
                                  if not (s["fin"] < m["debut"] - 2 or s["debut"] > m["fin"] + 2)))
        texte = mots_de(m["texte"])
        # Suites d'au moins deux mots seulement : des mots isolés (« de », « la ») ne font pas un écho.
        blocs = SequenceMatcher(None, texte, voisin, autojunk=False).get_matching_blocks()
        commun = sum(b.size for b in blocs if b.size >= min(2, len(texte)))
        if not texte or commun / len(texte) <= 0.6:
            garde.append(m)
    return garde


def fusionner(segs: list[dict]) -> list[dict]:
    blocs: list[dict] = []
    for s in sorted(segs, key=lambda x: x["debut"]):
        if blocs and blocs[-1]["loc"] == s["loc"] and s["debut"] - blocs[-1]["fin"] < 4:
            blocs[-1]["texte"] += " " + s["texte"]
            blocs[-1]["fin"] = s["fin"]
        else:
            blocs.append({"debut": s["debut"], "fin": s["fin"], "loc": s["loc"], "texte": s["texte"]})
    return blocs


def hms(sec: float) -> str:
    sec = int(max(sec, 0))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


GENERIQUE = voix.GENERIQUE


def numeroter(blocs: list[dict], sources: dict[str, str]) -> tuple[dict[str, str], dict[str, dict]]:
    """Numérote les voix dans l'ordre de première prise de parole sur toute la réunion ; les libellés provisoires
    deviennent « Voix N ». Renvoie (ancien -> nouveau libellé, {libellé: {numero, source}}), blocs modifiés."""
    ordre = list(dict.fromkeys(b["loc"] for b in blocs))
    noms = {loc: f"Voix {i}" if loc.startswith("?") else loc for i, loc in enumerate(ordre, 1)}
    for b in blocs:
        b["loc"] = noms[b["loc"]]
    return noms, {noms[loc]: {"numero": i, "source": sources.get(loc)} for i, loc in enumerate(ordre, 1)}


def ecrire(dossier: Path, blocs: list[dict], source: str, **autres) -> None:
    """Écrit transcription.json (avec `autres` : voix, propositions, separation) et transcription.md.
    Rappelée par le serveur après un renommage, un regroupement ou une correction."""
    # Relue toutes les 2 s par le serveur : jamais à moitié écrite (voir ecrire_atomique).
    reglages.ecrire_atomique(dossier / "transcription.json",
                             json.dumps({"source": source, "blocs": blocs, **autres}, ensure_ascii=False, indent=2))
    locuteurs = sorted({b["loc"] for b in blocs})
    lignes = [
        f"# Transcription {dossier.name}",
        "",
        f"Durée : {hms(max((b['fin'] for b in blocs), default=0))}. "
        f"Transcription automatique locale ({source}).",
        "Locuteurs : " + ", ".join(locuteurs) + ".",
    ]
    if any(GENERIQUE.fullmatch(l) for l in locuteurs):
        lignes.append("Les « Voix N » sont à identifier à la relecture.")
    lignes.append("")
    for b in blocs:
        lignes += [f"**[{hms(b['debut'])}] {b['loc']}** : {b['texte']}", ""]
    (dossier / "transcription.md").write_text("\n".join(lignes), encoding="utf-8")


# ---------------------------------------------------------------- main

# ---------------------------------------------------------------- main

# Avancement lu par le serveur : « @etape N » (1 à 4, spec TR-02), « @piste a b » (part de l'avancement global
# couverte par la transcription d'une piste, que la barre « 45%| » remplit), « @avance f » (0 à 1).
PART_PREPARATION, PART_TRANSCRIPTION, PART_SEPARATION = 0.05, 0.70, 0.22


def signaler(ligne: str) -> None:
    print(ligne, flush=True)


def reconnaitre(empreintes: dict[str, list], anciens: dict[str, list], banque: dict,
                exclus: set[str], seuil: float | None = None) -> tuple[dict[str, str], dict[str, dict]]:
    """(noms appliqués, propositions). Une voix qui ressemble à un nom déjà donné dans cette réunion le reprend
    (refaire la transcription garde les noms, TR-10) ; une voix qui ressemble à une voix connue est seulement
    proposée, avec sa confiance (VX-02, VX-03)."""
    appliques = {lbl: nom for lbl, (nom, _) in
                 voix.reconnaitre(empreintes, {n: {"emb": e} for n, e in anciens.items()}, seuil).items()}
    reste = {lbl: e for lbl, e in empreintes.items() if lbl not in appliques}
    pris = exclus | set(appliques.values())
    props = {lbl: {"nom": nom, "confiance": round(score, 2)} for lbl, (nom, score) in
             voix.reconnaitre(reste, {n: v for n, v in banque.items() if n not in pris}, seuil).items()}
    return appliques, props


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dossier", type=Path, nargs="?")
    ap.add_argument("--presentiel", action="store_true", help="plusieurs personnes parlent dans ton micro")
    ap.add_argument("--nb", type=int, help="nombre de locuteurs à séparer sur le son de l'ordinateur, si connu")
    ap.add_argument("--personnes", type=int, help="nombre de personnes qui ont parlé, toi compris")
    ap.add_argument("--max", type=int, help="nombre maximal de locuteurs (invités de l'agenda)")
    ap.add_argument("--sans-locuteurs", action="store_true", help="désactive la séparation des voix")
    ap.add_argument("--chuchote", action="store_true",
                    help="voix faibles ou lointaines : micro rehaussé même en visio (le présentiel l'est toujours)")
    r = reglages.lire()
    ap.add_argument("--moi", default=r["moi"] or PRENOM, help="nom affiché pour la piste micro")
    ap.add_argument("--moteur", choices=["mlx", "faster"], help="moteur de transcription (par défaut : réglages)")
    ap.add_argument("--modele", choices=list(MODELES), help="taille du modèle Whisper (par défaut : réglages)")
    ap.add_argument("--langue", help="code de langue (fr, en...) ou auto (par défaut : réglages)")
    ap.add_argument("--telecharger", action="store_true", help="télécharge les fichiers de transcription puis quitte")
    ap.add_argument("--telecharger-separation", action="store_true",
                    help="télécharge les fichiers de séparation des voix puis quitte")
    ap.add_argument("--essai-vitesse", type=Path, metavar="WAV",
                    help="mesure le rapport temps de calcul / durée sur cet extrait, puis quitte")
    ap.add_argument("--vocabulaire", type=Path, default=ICI / "vocabulaire.txt",
                    help="noms propres et sigles, un par ligne (l'outil Réunions fournit le sien)")
    ap.add_argument("--sans-empreintes", action="store_true",
                    help="ne reconnaît pas les voix connues (pas de consentement enregistré)")
    a = ap.parse_args()
    moteur, modele, langue = choix_modele({**r, **{k: v for k, v in
                                                    {"moteur": a.moteur, "modele": a.modele, "langue": a.langue}.items() if v}})
    if moteur == "mlx" and not importlib.util.find_spec("mlx_whisper"):
        print("mlx-whisper n'est pas installé (il ne tourne que sur Mac Apple Silicon) : choisis faster-whisper "
              "dans Configuration.", file=sys.stderr)
        signaler("@echec fichiers_incomplets")
        return 1

    os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")

    if a.telecharger:
        telecharger(moteur, modele)
        return 0
    if a.telecharger_separation:
        m = separation.methode(r)
        if not m:
            signaler("@echec separation_indisponible")
            return 1
        separation.telecharger(m)
        return 0
    if a.essai_vitesse:
        os.environ["HF_HUB_OFFLINE"] = "1"
        signaler(f"@k {essai_vitesse(a.essai_vitesse, moteur, modele, langue):.3f}")
        return 0
    if a.dossier is None:
        ap.error("dossier manquant")

    # Méthode de séparation (Q3) : sans elle, « Moi » pour le micro et « Les autres » pour le son de l'ordinateur.
    diar = None if a.sans_locuteurs else separation.methode(r)
    if diar is None and not a.sans_locuteurs and r["locuteurs"]:
        print("Séparation des voix indisponible : votre micro est noté « Moi », le son de l'ordinateur « Les autres ».")
    if diar == "libre" and not separation.prete(diar):
        try:
            separation.telecharger(diar)
        except Exception as e:  # réseau bloqué : la transcription passe sans séparation
            print(f"Fichiers de séparation des voix absents ({e}) : transcription sans séparation.")
            diar = None

    if en_cache(modele) and (diar != "pyannote" or en_cache(MODELE_DIAR)):
        os.environ["HF_HUB_OFFLINE"] = "1"
    else:
        print("Premier lancement : téléchargement des modèles manquants.")

    dossier = a.dossier.expanduser().resolve()
    fichiers = {"systeme": dossier / "systeme.wav", "micro": dossier / "micro.wav"}
    fichiers = {cle: f for cle, f in fichiers.items() if f.exists()}
    if not fichiers:
        print(f"Aucun fichier audio dans {dossier}", file=sys.stderr)
        signaler("@echec son_illisible")
        return 1

    debuts = {}
    if (dossier / "pistes.json").exists():
        debuts = json.loads((dossier / "pistes.json").read_text(encoding="utf-8"))
    origine = min(debuts.values()) if debuts else 0.0

    signaler("@etape 1")
    for wav in fichiers.values():
        reparer(wav)
    durees = {cle: max(wav.stat().st_size - 4096, 0) for cle, wav in fichiers.items()}  # proportions seulement
    if not sum(durees.values()):
        signaler("@echec son_vide")
        return 1

    signaler("@etape 2")
    prompt = lire_prompt(a.vocabulaire)
    pos = PART_PREPARATION
    traitees: dict[str, tuple] = {}  # piste -> (wav transcrit, segments, à séparer, nom fixe, nombre de voix)
    for cle, wav in fichiers.items():
        # La piste du Mac est gardée en présentiel : réunion mixte (salle + visio). Sans distants, elle est
        # silencieuse et le filtre de silence n'en tire rien.
        # Son de l'ordinateur vide : tout s'est dit dans la pièce, le micro porte plusieurs voix. Le 03/11, un
        # atelier diffusé sur Meet (distants muets, case présentiel oubliée) a été attribué en entier au
        # propriétaire. Le micro est alors traité comme en présentiel ; son nom ne revient que par son empreinte.
        # Seulement si le son de l'ordinateur existe et n'a presque rien entendu (moins de 20 s ou de 1 %).
        salle = cle == "micro" and not a.presentiel and "systeme" in fichiers and (
            audible_s(fichiers["systeme"]) < 20 or part_audible(fichiers["systeme"]) < 0.01)
        if salle:
            print("Son de l'ordinateur vide : voix séparées sur le micro." if diar else
                  "Son de l'ordinateur vide : piste micro notée « Moi » (séparation des voix indisponible).")
        separer = bool(diar) and (cle == "systeme" or a.presentiel or salle)
        # Sans séparation : « Moi » et « Les autres » (Q3) ; avec, la voix seule du micro porte ton nom (VX-23).
        nom_fixe = "Les autres" if cle == "systeme" else (
            "Salle" if a.presentiel else a.moi if diar and not salle else "Moi")
        # « Combien de personnes ont parlé ? » (TR-12) : toi compris. En visio, ta voix est seule sur le micro.
        nb = (a.nb or (a.personnes - 1 if a.personnes and a.personnes > 1 else None)) if cle == "systeme" else (
            a.personnes if salle else None)

        if cle == "micro" and (a.presentiel or a.chuchote or salle):
            print("Rehaussement du micro (voix basses et lointaines)...")
            try:
                wav = rehausser(wav)
            except (OSError, subprocess.CalledProcessError) as e:
                print(f"  Rehaussement impossible ({e}), piste brute gardée.")
        part = PART_TRANSCRIPTION * durees[cle] / sum(durees.values())
        signaler(f"@piste {pos:.3f} {pos + part:.3f}")
        print(f"Transcription de la piste {cle}...")
        traitees[cle] = (wav, transcrire(wav, prompt, modele, moteur, langue), separer, nom_fixe, nb)
        # TR-06 : texte déjà prêt, sans noms, lu par la page pendant la suite du calcul.
        partiel = sorted(({"debut": s["debut"] + debuts.get(f"{c}_debut", origine) - origine, "texte": s["texte"]}
                          for c, t in traitees.items() for s in t[1]), key=lambda b: b["debut"])
        reglages.ecrire_atomique(dossier / "transcription.partielle.json", json.dumps(partiel, ensure_ascii=False))
        pos += part

    signaler("@etape 3")
    signaler(f"@avance {pos:.3f}")
    noms: dict = {}
    sources: dict[str, str] = {}
    empreintes: dict[str, list] = {}  # libellé provisoire -> empreinte vocale
    pistes: dict[str, list[dict]] = {}
    a_separer = [cle for cle, t in traitees.items() if t[2] and t[1]]
    for cle, (wav, segs, separer, nom_fixe, nb) in traitees.items():
        tours = None
        if cle in a_separer:
            print(f"Séparation des voix, piste {cle}...")
            try:
                # --max compte les invités de l'agenda : il ne vaut que pour le son de l'ordinateur.
                tours, embs = diariser(wav, nb, a.max if cle == "systeme" else None, diar)
            except Exception as e:
                print(f"  Séparation impossible ({e}). Piste gardée sans séparation.")
            pos += PART_SEPARATION / len(a_separer)
            signaler(f"@avance {pos:.3f}")
        if tours:
            etiq = attribuer(segs, tours, noms, cle)
            empreintes |= {noms[(cle, lbl)]: e for lbl, e in embs.items() if (cle, lbl) in noms}
        else:
            etiq = [{"debut": s["debut"], "fin": s["fin"], "loc": nom_fixe, "texte": s["texte"]} for s in segs]
        sources |= {s["loc"]: cle for s in etiq}

        decalage = debuts.get(f"{cle}_debut", origine) - origine
        for s in etiq:
            s["debut"] += decalage
            s["fin"] += decalage
        pistes[cle] = etiq

    signaler("@etape 4")
    signaler(f"@avance {max(pos, 1 - 0.03):.3f}")
    try:
        anciens = json.loads((dossier / "locuteurs.json").read_text(encoding="utf-8"))
        anciens = {n: e for n, e in anciens.items() if not GENERIQUE.fullmatch(n)}
    except (OSError, ValueError):
        anciens = {}
    fixes = {s["loc"] for etiq in pistes.values() for s in etiq if not s["loc"].startswith("?")}
    try:
        modele_voix = separation.modele_empreintes(diar)
        appliques, props = ({}, {}) if a.sans_empreintes or not empreintes else reconnaitre(
            empreintes, anciens, voix.lire_banque(modele_voix), fixes, voix.SEUILS.get(modele_voix))
    except Exception as e:  # la reconnaissance est un confort, la transcription passe quand même
        print(f"Reconnaissance des voix impossible ({e}).")
        appliques, props = {}, {}
    for etiq in pistes.values():
        for s in etiq:
            s["loc"] = appliques.get(s["loc"], s["loc"])
    sources |= {nom: sources[lbl] for lbl, nom in appliques.items()}

    micro = retirer_echo(pistes.get("micro", []), pistes.get("systeme", []))
    blocs = fusionner(micro + pistes.get("systeme", []))
    libelles, voix_reunion = numeroter(blocs, sources)
    propositions = {libelles[lbl]: p for lbl, p in props.items() if lbl in libelles}
    if propositions:
        print("Voix proposées : " + ", ".join(f"{k} = {p['nom']} ?" for k, p in propositions.items()))
    nouveaux = {libelles[appliques.get(k, k)]: e for k, e in empreintes.items() if appliques.get(k, k) in libelles}
    # Empreintes déjà nommées et non retrouvées : gardées (même modèle seulement), jamais remplacées.
    taille = len(next(iter(nouveaux.values()), []))
    gardes = {n: e for n, e in anciens.items() if n not in nouveaux and (not taille or len(e) == taille)}
    reglages.ecrire_atomique(dossier / "locuteurs.json", json.dumps({**gardes, **nouveaux}))

    source = modele + (f", voix {diar}" if noms else "")
    (dossier / "transcription.partielle.json").unlink(missing_ok=True)
    ecrire(dossier, blocs, source, separation=bool(diar), voix=voix_reunion, propositions=propositions,
           empreintes=separation.modele_empreintes(diar))
    print(f"Transcription écrite : {dossier / 'transcription.md'}")
    return 0


def code_echec(e: BaseException, telechargement: bool = False) -> str:
    """Cause connue d'un échec (spec ER-11, PL-13, PL-14), lue par le serveur sur la ligne « @echec <code> »."""
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return "disque_plein"
    if isinstance(e, MemoryError):
        return "memoire"
    # Noms comparés plutôt qu'importés : huggingface_hub, httpx et soundfile ne sont pas chargés à ce stade.
    noms = {c.__name__ for c in type(e).__mro__}
    if noms & {"LibsndfileError", "SoundFileRuntimeError", "SoundFileError"}:
        return "son_illisible"
    filtre = {"SSLError", "HTTPError", "HfHubHTTPError", "HTTPStatusError", "ProxyError"}
    absent = {"LocalEntryNotFoundError", "OfflineModeIsEnabled", "ConnectionError", "ConnectTimeout", "ConnectError",
              "gaierror", "TimeoutException", "ReadTimeout", "URLError", "timeout"}
    if telechargement:
        return "reseau_filtre" if noms & filtre else "reseau_absent" if noms & absent else "inconnue"
    return "fichiers_incomplets" if noms & (absent | filtre) else "inconnue"


def expliquer(e: BaseException) -> str | None:
    """Message lisible pour les pannes attendues, dans le journal de la réunion."""
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return "Disque plein : libère de la place, puis relance la transcription."
    if code_echec(e) == "fichiers_incomplets":
        return ("Modèle de transcription absent et téléchargement impossible : vérifie la connexion, "
                "ou télécharge le modèle depuis Configuration.")
    return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        signaler(f"@echec {code_echec(e, any(x.startswith('--telecharger') for x in sys.argv))}")
        if not (message := expliquer(e)):
            raise
        print(message, file=sys.stderr)
        sys.exit(1)
