"""Reprise d'une transcription interrompue (plantage, arrêt de l'outil, coupure de courant).

Avant le 10/11, un arrêt en cours de route faisait tout recommencer : 1 h 50 de Whisper perdues pour une réunion
de 69 min. Chaque étape longue garde maintenant son résultat dans le dossier « reprise » de la réunion, et le
lancement suivant repart de là :
- Whisper (faster-whisper) : chaque segment est ajouté à whisper-<piste>.jsonl dès qu'il sort ; la reprise ne
  transcrit que la suite de l'audio. mlx-whisper rend la piste d'un bloc : elle est gardée piste par piste.
- Rehaussement du micro : la copie est réutilisée si elle a été écrite en entier.
- Séparation des voix : résultat gardé par piste (le calcul lui-même ne se reprend pas en cours de route).

Une signature (modèle, langue, vocabulaire, options, taille des pistes) invalide tout si quelque chose a changé :
« Refaire » avec d'autres réglages repart de zéro. Le dossier disparaît une fois la transcription écrite."""

import hashlib
import json
import os
import shutil
from pathlib import Path

VERSION = 1  # à augmenter si le format des fichiers gardés change


def signature(modele: str, moteur: str, langue: str | None, prompt: str, options: dict,
              fichiers: dict[str, Path]) -> dict:
    return {"version": VERSION, "modele": modele, "moteur": moteur, "langue": langue,
            "vocabulaire": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "options": options,
            "pistes": {cle: wav.stat().st_size for cle, wav in sorted(fichiers.items())}}


class Reprise:
    def __init__(self, dossier: Path, sig: dict):
        self.d = dossier / "reprise"
        f = self.d / "signature.json"
        try:
            ancienne = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ancienne = None
        self.reprise = ancienne == sig
        if not self.reprise:
            shutil.rmtree(self.d, ignore_errors=True)
            self.d.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(sig), encoding="utf-8")

    # ------------------------------------------------------------ Whisper

    def _whisper(self, piste: str) -> Path:
        return self.d / f"whisper-{piste}.jsonl"

    def segments(self, piste: str) -> tuple[list[dict], bool]:
        """(segments bruts déjà transcrits, piste finie). Une dernière ligne coupée par l'arrêt est écartée."""
        f = self._whisper(piste)
        try:
            lignes = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            return [], False
        segs, fini, propres = [], False, []
        for ligne in lignes:
            try:
                o = json.loads(ligne)
            except ValueError:
                break
            propres.append(ligne)
            if o.get("fin"):
                fini = True
                break
            segs.append(o)
        if len(propres) < len(lignes):  # réécrit sans la ligne abîmée, sinon les ajouts suivraient du charabia
            f.write_text("".join(p + "\n" for p in propres), encoding="utf-8")
        return segs, fini

    def ajouter(self, piste: str, seg: dict) -> None:
        with self._whisper(piste).open("a", encoding="utf-8") as f:
            f.write(json.dumps(seg, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())  # survit à une coupure de courant, pas seulement à un plantage

    def finir(self, piste: str) -> None:
        self.ajouter(piste, {"fin": True})

    def recommencer(self, piste: str) -> None:
        self._whisper(piste).unlink(missing_ok=True)

    # ------------------------------------------------------------ rehaussement du micro

    def rehausse_pret(self, copie: Path) -> bool:
        return copie.exists() and (self.d / "rehausse.ok").exists()

    def rehausse_fait(self) -> None:
        try:
            (self.d / "rehausse.ok").write_text("", encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ séparation des voix

    def separation(self, piste: str) -> tuple[list, dict] | None:
        try:
            o = json.loads((self.d / f"separation-{piste}.json").read_text(encoding="utf-8"))
            return [tuple(t) for t in o["tours"]], o["empreintes"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def garder_separation(self, piste: str, tours: list, empreintes: dict) -> None:
        f = self.d / f"separation-{piste}.json"
        tmp = f.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"tours": [list(t) for t in tours], "empreintes": empreintes}), encoding="utf-8")
            os.replace(tmp, f)
        except OSError:
            pass

    # ------------------------------------------------------------ fin

    def effacer(self) -> None:
        shutil.rmtree(self.d, ignore_errors=True)
