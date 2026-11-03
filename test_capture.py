"""Capture hors Mac, sans carte son : périphériques simulés. python3 test_capture.py"""

import json
import tempfile
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

import capture
import transcribe


def test_piste_comble_les_trous():
    with tempfile.TemporaryDirectory() as t:
        horloge = [100.0]
        p = capture.Piste(Path(t) / "micro.wav", horloge=lambda: horloge[0])
        bloc = np.full((capture.BLOC, 2), 0.5, dtype=np.float32)  # stéréo : moyenné en mono
        for _ in range(3):
            horloge[0] += 0.1
            p.ecrire(bloc)
        horloge[0] += 2.1  # 2 s sans rien (source coupée), puis un bloc
        p.ecrire(bloc)
        horloge[0] += 0.15  # léger retard de réception : sous l'écart, pas de silence ajouté
        p.ecrire(bloc)
        p.fermer()
        x, taux = sf.read(Path(t) / "micro.wav", dtype="float32")
        assert taux == 48000 and transcribe.taux_wav(Path(t) / "micro.wav") == 48000
        assert len(x) == 5 * capture.BLOC + 2 * 48000, len(x)
        assert np.all(x[3 * capture.BLOC:3 * capture.BLOC + 2 * 48000] == 0)
        assert np.allclose(x[-capture.BLOC:], 0.5)
        assert abs(p.debut - 100.0) < 1e-9


class Enregistreur:
    def __init__(self, source):
        self.source = source

    def __enter__(self):
        self.source.ouvertures += 1
        return self

    def __exit__(self, *a):
        return False

    def record(self, numframes):
        self.source.lus += 1
        if self.source.lus in self.source.pannes:
            raise RuntimeError("AUDCLNT_E_DEVICE_INVALIDATED")
        threading.Event().wait(0.005)
        return np.full((numframes, 1), 0.1, dtype=np.float32)


class Source:
    def __init__(self, nom, pannes=()):
        self.name, self.id, self.pannes, self.lus, self.ouvertures = nom, nom, set(pannes), 0, 0

    def recorder(self, samplerate, blocksize):
        assert samplerate == 48000
        return Enregistreur(self)


class CarteSon:
    """Imite l'API soundcard utilisée par capture.py."""

    def __init__(self, micros, sorties):
        self.micros, self.sorties = micros, sorties

    def get_microphone(self, ident, include_loopback=False):
        for s in self.micros + (self.sorties if include_loopback else []):
            if s.id == ident:
                return s
        raise IndexError(f"no device with id {ident}")

    def default_microphone(self):
        if not self.micros:
            raise RuntimeError("Error 0x80070490")
        return self.micros[0]

    def default_speaker(self):
        return self.sorties[0]


def test_capturer_relance_apres_coupure(capsys=None):
    capture.ATTENTES = (0.01, 0.02)
    with tempfile.TemporaryDirectory() as t:
        micro, casque = Source("Micro casque", pannes={3}), Source("Casque")
        sc = CarteSon([Source("Micro interne"), micro], [casque])
        arret = threading.Event()
        threading.Timer(0.4, arret.set).start()
        code = capture.capturer(Path(t), sc, arret, {"micro": "Micro casque", "sortie": ""})
        assert code == 0
        assert micro.ouvertures == 2  # rouvert après la panne
        assert casque.lus > 0         # boucle de la sortie par défaut
        assert set(json.loads((Path(t) / "pistes.json").read_text(encoding="utf-8"))) == {"micro_debut", "systeme_debut"}
        assert sf.info(str(Path(t) / "systeme.wav")).frames > 0


def test_peripherique_absent():
    sc = CarteSon([Source("Micro interne")], [Source("Haut-parleurs")])
    assert capture.micro_de(sc, "Micro débranché").name == "Micro interne"  # repli sur celui du système
    assert capture.sortie_de(sc, "").name == "Haut-parleurs"
    try:
        capture.micro_de(CarteSon([], []), "")
    except RuntimeError as e:
        assert "Aucun micro" in str(e)
    else:
        raise AssertionError("pas d'erreur sans micro")


if __name__ == "__main__":
    for nom, f in list(globals().items()):
        if nom.startswith("test_"):
            f()
    print("ok")
