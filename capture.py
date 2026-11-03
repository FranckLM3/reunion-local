#!/usr/bin/env python3
"""Enregistreur hors Mac (Windows) : ton micro et le son de l'ordinateur dans deux pistes séparées.

Même sortie que meetrec sur Mac : micro.wav et systeme.wav (float32 mono 48 kHz), pistes.json (début de
chaque piste, pour les recaler). Le son de l'ordinateur (les participants distants) est capté par la boucle
de sortie WASAPI (loopback) : aucun pilote à installer, aucune sortie audio à changer. Bibliothèque soundcard.

Usage : capture.py <dossier>    Arrêt : fermer l'entrée standard (c'est ce que fait serveur.py) ou Ctrl+C.
        capture.py --peripheriques                   micros et sorties, en JSON
        capture.py --niveau micro|sortie <id>        niveau capté pendant 1 s, en dBFS
Les périphériques viennent des réglages (écran Configuration), sinon ceux par défaut du système.
"""

import json
import math
import sys
import threading
import time
from pathlib import Path

import reglages

TAUX = 48_000
BLOC = TAUX // 10       # 0,1 s lue à chaque tour
TAMPON = TAUX // 2      # 0,5 s de tampon WASAPI : Python peut prendre du retard sans perdre de son
ECART = 0.2             # s : un retard plus long est un trou (son coupé, périphérique en pause), comblé de silence
SYNCHRO = 5.0           # s entre deux mises à jour de l'en-tête : un arrêt brutal laisse un fichier lisible
ATTENTES = (1, 2, 4, 8, 10)  # s avant chaque nouvel essai quand un flux casse (10 s ensuite)


def journal(message: str) -> None:
    """Ligne horodatée dans le journal de l'enregistrement (capture.log, via la sortie d'erreur)."""
    print(f"{time.strftime('%H:%M:%S')} {message}", file=sys.stderr, flush=True)


class Piste:
    """Une piste WAV, comblée de silence quand la source se tait sans rien envoyer, pour que micro et
    système restent alignés : même principe que meetrec, avec l'horloge de réception au lieu des PTS."""

    def __init__(self, chemin: Path, horloge=time.monotonic):
        import soundfile as sf

        # Fichier ouvert par Python : sous Windows, il reste lisible par le serveur pendant l'écriture
        # (niveau du micro, alerte de son coupé).
        self.brut = chemin.open("w+b")
        self.f = sf.SoundFile(self.brut, "w", TAUX, 1, "FLOAT", format="WAV")
        self.horloge = horloge
        self.debut: float | None = None
        self.ecrit = 0
        self.synchro = 0.0

    def ecrire(self, x) -> None:
        import numpy as np

        mono = np.asarray(x, dtype=np.float32)
        if mono.ndim == 2:
            mono = mono.mean(axis=1)
        maintenant = self.horloge()
        debut = maintenant - len(mono) / TAUX  # le bloc vient de finir d'arriver
        if self.debut is None:
            self.debut = debut
        trou = round((debut - self.debut) * TAUX) - self.ecrit
        if ECART * TAUX < trou < 4 * 3600 * TAUX:  # plafond 4 h contre une horloge folle
            minute = np.zeros(60 * TAUX, dtype=np.float32)
            while trou > 0:
                n = min(trou, len(minute))
                self.f.write(minute[:n])
                self.ecrit += n
                trou -= n
        self.f.write(mono)
        self.ecrit += len(mono)
        if maintenant - self.synchro >= SYNCHRO:
            self.f.flush()
            self.synchro = maintenant

    def fermer(self) -> None:
        self.f.close()
        self.brut.close()


def _trouver(ident: str, chercher, defaut, quoi: str):
    # Un périphérique choisi mais débranché est remplacé par celui du système : mieux vaut enregistrer que refuser.
    if ident:
        try:
            return chercher(ident)
        except (IndexError, RuntimeError):
            journal(f"{quoi} introuvable, celui du système est pris à la place.")
    return defaut()


def micro_de(sc, ident: str):
    try:
        return _trouver(ident, sc.get_microphone, sc.default_microphone, "Le micro choisi")
    except Exception as e:
        raise RuntimeError("Aucun micro détecté : branche un micro, ou autorise-le dans Paramètres Windows > "
                           f"Confidentialité > Microphone. ({e})") from e


def sortie_de(sc, ident: str):
    """La sortie (haut-parleurs, casque) vue comme une entrée : sa boucle WASAPI."""
    def boucle(i):
        return sc.get_microphone(i, include_loopback=True)

    try:
        return _trouver(ident, boucle, lambda: boucle(sc.default_speaker().id), "La sortie choisie")
    except Exception as e:
        raise RuntimeError(f"Aucune sortie audio détectée (haut-parleurs ou casque). ({e})") from e


def capturer(dossier: Path, sc, arret: threading.Event, r: dict | None = None) -> int:
    """Enregistre jusqu'à `arret` ; 0 si tout s'est bien passé."""
    r = r if r is not None else reglages.lire()
    trouver = {"micro": lambda: micro_de(sc, r.get("micro", "")),
               "systeme": lambda: sortie_de(sc, r.get("sortie", ""))}
    premiers = {cle: f() for cle, f in trouver.items()}  # aucun périphérique : échec immédiat, message clair
    pistes = {"micro": Piste(dossier / "micro.wav"), "systeme": Piste(dossier / "systeme.wav")}
    erreurs: list[str] = []

    def lire(cle: str) -> None:
        """Lit une source ; si le flux casse (périphérique débranché, changement de sortie, pilote qui
        décroche), relance seul avec une attente croissante. Les pistes restent alignées : la reprise est
        précédée du silence correspondant à la coupure (voir Piste)."""
        source, attente, coupe = premiers[cle], ATTENTES[0], False
        while not arret.is_set():
            try:
                source = source or trouver[cle]()
                with source.recorder(samplerate=TAUX, blocksize=TAMPON) as rec:
                    while not arret.is_set():
                        x = rec.record(numframes=BLOC)
                        try:
                            pistes[cle].ecrire(x)
                        except Exception as e:  # disque plein : relancer ne servirait à rien
                            erreurs.append(f"[{cle}] écriture impossible : {e}")
                            arret.set()
                            return
                        if coupe:
                            journal(f"[{cle}] capture reprise ({source.name}).")
                            coupe, attente = False, ATTENTES[0]
            except Exception as e:
                journal(f"[{cle}] flux interrompu ({e}) : nouvel essai dans {attente} s.")
                source, coupe = None, True  # le périphérique est recherché à nouveau (celui du système s'il a disparu)
                arret.wait(attente)
                attente = next((a for a in ATTENTES if a > attente), ATTENTES[-1])

    fils = [threading.Thread(target=lire, args=(cle,), daemon=True) for cle in pistes]
    for f in fils:
        f.start()
    journal(f"Enregistrement en cours dans {dossier}.")  # attendue par serveur.capture_prete : ne pas reformuler
    journal(f"Micro : {premiers['micro'].name}. Son de l'ordinateur : {premiers['systeme'].name}.")
    try:
        while not arret.wait(0.5):
            pass
    except KeyboardInterrupt:  # Ctrl+C dans la console : arrêt normal, fichiers finalisés
        arret.set()
    for f in fils:
        f.join(timeout=3)
    for p in pistes.values():
        p.fermer()
    (dossier / "pistes.json").write_text(json.dumps(
        {f"{cle}_debut": p.debut for cle, p in pistes.items() if p.debut is not None}, indent=2), encoding="utf-8")
    for e in erreurs:
        journal(f"Capture arrêtée : {e}")
    return 1 if erreurs else 0


def peripheriques() -> dict:
    """Micros et sorties détectés, pour l'écran Configuration."""
    import soundcard as sc

    return {
        "micros": [{"id": m.id, "nom": m.name} for m in sc.all_microphones()],
        "sorties": [{"id": s.id, "nom": s.name} for s in sc.all_speakers()],
        "micro_defaut": sc.default_microphone().name,
        "sortie_defaut": sc.default_speaker().name,
    }


def niveau(quoi: str, ident: str, secondes: float = 1.0) -> float:
    """Niveau efficace (dBFS) capté pendant `secondes` sur le micro ou la sortie choisis."""
    import numpy as np
    import soundcard as sc

    source = micro_de(sc, ident) if quoi == "micro" else sortie_de(sc, ident)
    with source.recorder(samplerate=TAUX, blocksize=TAMPON) as rec:
        x = rec.record(numframes=int(TAUX * secondes))
    rms = float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0
    return round(20 * math.log10(max(rms, 1e-10)), 1)


def garder_eveille() -> None:
    """Ni veille ni écran éteint tant que la capture tourne (équivalent de caffeinate sur Mac)."""
    if sys.platform == "win32":
        import ctypes

        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED, valable jusqu'à la fin du processus.
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)


def main() -> int:
    # Configuration : serveur.py appelle ces deux commandes dans un processus à part (voir serveur.capture_cli).
    if sys.argv[1:2] in (["--peripheriques"], ["--niveau"]):
        try:
            if len(sys.argv) == 2 and sys.argv[1] == "--peripheriques":
                print(json.dumps(peripheriques(), ensure_ascii=False))
            elif len(sys.argv) == 4:
                print(niveau(sys.argv[2], sys.argv[3]))
            else:
                print("Usage : capture.py --niveau micro|sortie <identifiant>", file=sys.stderr)
                return 2
        except Exception as e:
            print(str(e), file=sys.stderr)
            return 1
        return 0
    if len(sys.argv) != 2:
        print("Usage : capture.py <dossier> | --peripheriques | --niveau micro|sortie <identifiant>", file=sys.stderr)
        return 2
    dossier = Path(sys.argv[1])
    dossier.mkdir(parents=True, exist_ok=True)
    arret = threading.Event()

    def attendre_fin_entree():
        sys.stdin.read()  # serveur.py ferme l'entrée standard pour arrêter
        arret.set()

    threading.Thread(target=attendre_fin_entree, daemon=True).start()
    try:
        import soundcard as sc
    except Exception as e:
        print(f"Bibliothèque audio absente ou inutilisable ({e}) : relance l'installation.", file=sys.stderr)
        return 1
    garder_eveille()
    try:
        return capturer(dossier, sc, arret)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
