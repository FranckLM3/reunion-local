#!/usr/bin/env python3
"""Aperçu pendant l'enregistrement : transcrit les dernières secondes du micro et affiche le texte.

Sert à vérifier en direct que ce qui se dit sort lisiblement (voix basses, micro loin).
Même rehaussement et mêmes filtres que la transcription finale.

Usage : apercu.py <micro.wav> [--rehausser]
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

import transcribe

SECONDES = 20


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=Path)
    ap.add_argument("--rehausser", action="store_true")
    a = ap.parse_args()
    moteur, modele, langue = transcribe.choix_modele(transcribe.reglages.lire())
    if transcribe.en_cache(modele):
        os.environ["HF_HUB_OFFLINE"] = "1"

    taux = transcribe.taux_wav(a.wav)  # meetrec écrit du float32 mono, à la fréquence du micro
    n = taux * SECONDES * 4
    with a.wav.open("rb") as f:
        if f.seek(0, 2) < n + 4096:
            print("Pas encore assez d'audio.")
            return 0
        f.seek(-n, 2)
        x = np.frombuffer(f.read(n), dtype=np.float32)

    with tempfile.TemporaryDirectory() as t:
        brut = Path(t) / "apercu.wav"
        sf.write(brut, x, taux)
        wav = transcribe.rehausser(brut) if a.rehausser else brut
        segs = transcribe.transcrire(wav, transcribe.lire_prompt(transcribe.ICI / "vocabulaire.txt"),
                                     modele, moteur, langue)
    texte = " ".join(s["texte"] for s in segs).strip()
    print(texte or "(rien de lisible sur ces secondes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
