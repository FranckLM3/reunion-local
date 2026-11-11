"""Part de l'ordinateur laissée à la transcription : nombre de cœurs et priorité.

Le 10/11, la séparation des voix occupait les 20 fils du processeur pendant plus d'une heure et l'ordinateur
finissait par planter. Le réglage « Puissance pour la transcription » (10 à 100 % des cœurs, par pas de 10) et
la case « Priorité basse » sont relus toutes les quelques secondes par transcribe.py, qui se les applique
lui-même : un changement dans les réglages vaut tout de suite, en cours de transcription. Whisper et la séparation
des voix prennent autant de fils de calcul que de cœurs permis (nombre fixé au début de chaque piste ; les cœurs
permis, eux, changent aussitôt).

- Windows : affinité du processus (les cœurs permis) et classe de priorité. Les sous-processus (ffmpeg) héritent.
- Linux : sched_setaffinity et nice.
- Mac : pas d'affinité possible ; seule la priorité change en cours de route, le nombre de fils de calcul suit
  le réglage au début de chaque étape."""

import os
import sys
import threading
import time

import reglages

PARTS = tuple(range(10, 101, 10))  # 10 %, 20 %... 100 % des cœurs
INTERVALLE_S = 3.0


def coeurs_total() -> int:
    return os.cpu_count() or 1


def coeurs_alloues(r: dict | None = None) -> int:
    r = r or reglages.lire()
    part = r.get("ressources") if r.get("ressources") in PARTS else 100
    return max(1, round(coeurs_total() * part / 100))


def appliquer(r: dict | None = None) -> str:
    """Applique le réglage au processus courant ; rend une phrase pour le journal."""
    r = r or reglages.lire()
    n, basse = coeurs_alloues(r), bool(r.get("priorite_basse"))
    try:
        if sys.platform == "win32":
            import ctypes

            k = ctypes.windll.kernel32
            k.GetCurrentProcess.restype = ctypes.c_void_p
            moi = k.GetCurrentProcess()
            # Les premiers processeurs logiques : sur les puces Intel hybrides, ce sont les cœurs performants.
            masque = (1 << n) - 1 if n < 64 else (1 << 64) - 1
            k.SetProcessAffinityMask(ctypes.c_void_p(moi), ctypes.c_size_t(masque))
            k.SetPriorityClass(ctypes.c_void_p(moi), 0x4000 if basse else 0x20)  # BELOW_NORMAL / NORMAL
        else:
            if hasattr(os, "sched_setaffinity"):
                os.sched_setaffinity(0, range(n))
            courant = os.getpriority(os.PRIO_PROCESS, 0)
            voulu = 10 if basse else 0
            if voulu > courant:  # baisser la priorité est toujours permis, la remonter non (sans droits)
                os.setpriority(os.PRIO_PROCESS, 0, voulu)
    except (OSError, AttributeError, ValueError) as e:
        return f"Ressources : réglage impossible ({e})."
    return f"Ressources : {n} cœurs sur {coeurs_total()}, priorité {'basse' if basse else 'normale'}."


def surveiller() -> None:
    """Applique le réglage maintenant, puis à chaque changement du fichier de réglages (fil en arrière-plan)."""
    r = reglages.lire()
    print(appliquer(r), flush=True)

    def boucle(avant=(r.get("ressources"), r.get("priorite_basse"))):  # référence : ce qui vient d'être appliqué
        while True:
            time.sleep(INTERVALLE_S)
            try:
                r = reglages.lire()
            except Exception:
                continue
            cle = (r.get("ressources"), r.get("priorite_basse"))
            if cle != avant:
                avant = cle
                print(appliquer(r), flush=True)

    threading.Thread(target=boucle, daemon=True, name="ressources").start()
