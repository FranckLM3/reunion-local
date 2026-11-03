#!/usr/bin/env python3
"""Brouillon en direct pendant l'enregistrement (spec DI) : transcrit par morceaux votre micro (« moi ») et le
son de l'ordinateur (« autres »), à priorité basse. Rien n'est écrit sur le disque : une ligne JSON par
événement sur la sortie standard, lue par le serveur, qui ne la garde qu'en mémoire.

  {"texte": ..., "t_s": 192.4, "source": "moi"}   un morceau, horodaté en temps de réunion
  {"pos": 184.0, "dispo": 224.0}                   où en est le calcul (retard = dispo - pos)
  {"saut": true}                                   trop de retard : reprise au présent, morceaux sautés

Usage : direct.py <dossier>    Arrêt : fermer l'entrée standard (ce que fait le serveur).
"""

import json
import os
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

MORCEAU = 8.0       # s d'audio par passage : un nouveau morceau toutes les 5 à 10 s (DI-02)
RETARD_MAX = 180.0  # s : au-delà, le direct saute au présent (DI-05)
FIGEE = 30.0        # s sans que la piste grossisse : figée


def entete(wav: Path) -> tuple[int, int] | None:
    """(début des données, fréquence) d'un WAV float32 mono en cours d'écriture, None s'il n'est pas encore lisible."""
    try:
        with wav.open("rb") as f:
            tete = f.read(8192)
    except OSError:
        return None
    i, j = tete.find(b"fmt "), tete.find(b"data")
    if i < 0 or j < 0:
        return None
    return j + 8, struct.unpack_from("<I", tete, i + 12)[0]


def duree(wav: Path, e: tuple[int, int]) -> float:
    return max(wav.stat().st_size - e[0], 0) / (e[1] * 4)


def lire(wav: Path, e: tuple[int, int], debut: float, fin: float):
    import numpy as np

    with wav.open("rb") as f:
        f.seek(e[0] + int(debut * e[1]) * 4)
        return np.frombuffer(f.read(int((fin - debut) * e[1]) * 4), dtype=np.float32)


def prochain(pos: float, dispo: float, morceau: float = MORCEAU, retard_max: float = RETARD_MAX):
    """(début du morceau à transcrire, saut) ; None tant qu'il manque de l'audio. L'enregistrement n'attend jamais
    le direct : en retard de plus de `retard_max`, on reprend au présent et le passé n'est pas calculé."""
    if dispo - pos > retard_max:
        return dispo - morceau, True
    return (pos, False) if dispo - pos >= morceau else None


def ecrire(o: dict) -> None:
    print(json.dumps(o, ensure_ascii=False), flush=True)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    if hasattr(os, "nice"):
        os.nice(15)  # sous Windows, le serveur lance ce processus en priorité basse
    import soundfile as sf

    import transcribe

    dossier = Path(sys.argv[1])
    moteur, modele, langue = transcribe.choix_modele(transcribe.reglages.lire())
    os.environ["HF_HUB_OFFLINE"] = "1"
    prompt = transcribe.lire_prompt(transcribe.ICI / "vocabulaire.txt")
    arret = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), arret.set()), daemon=True).start()
    pistes = {"moi": dossier / "micro.wav", "autres": dossier / "systeme.wav"}
    pos = 0.0
    vu: dict[str, tuple[int, float]] = {}  # piste -> (taille, instant où elle a grossi)
    with tempfile.TemporaryDirectory() as t:
        while not arret.is_set():
            maintenant = time.monotonic()
            entetes = {}
            for s, wav in pistes.items():
                if not (e := entete(wav)):
                    continue
                taille = wav.stat().st_size
                if vu.get(s, (None,))[0] != taille:
                    vu[s] = (taille, maintenant)
                # Piste vide ou figée (plus rien n'arrive depuis 30 s) : ignorée, elle ne bloque pas l'autre.
                if duree(wav, e) > 0 and maintenant - vu[s][1] < FIGEE:
                    entetes[s] = e
            if not entetes:
                arret.wait(1)
                continue
            dispo = min(duree(pistes[s], e) for s, e in entetes.items())
            ecrire({"pos": round(pos, 1), "dispo": round(dispo, 1)})
            suivant = prochain(pos, dispo)
            if not suivant:
                arret.wait(1)
                continue
            debut, saut = suivant
            if saut:
                ecrire({"saut": True})
            for source, e in entetes.items():
                x = lire(pistes[source], e, debut, debut + MORCEAU)
                if not x.size:
                    continue
                morceau = Path(t) / f"{source}.wav"
                sf.write(morceau, x, e[1])
                for s in transcribe.transcrire(morceau, prompt, modele, moteur, langue):
                    ecrire({"t_s": round(debut + s["debut"], 1), "source": source, "texte": s["texte"]})
            pos = debut + MORCEAU
    return 0


if __name__ == "__main__":
    sys.exit(main())
