"""Part de l'ordinateur laissée à la transcription (ressources.py)."""
import sys
import time
from unittest import mock

import reglages
import ressources


def test_coeurs_alloues():
    with mock.patch.object(ressources.os, "cpu_count", return_value=20):
        assert ressources.coeurs_alloues({"ressources": 100}) == 20
        assert ressources.coeurs_alloues({"ressources": 30}) == 6
        assert ressources.coeurs_alloues({"ressources": 33}) == 20  # valeur inconnue : tout
    with mock.patch.object(ressources.os, "cpu_count", return_value=2):
        assert ressources.coeurs_alloues({"ressources": 10}) == 1  # jamais zéro


def masque_courant():
    import ctypes

    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    proc, sys_ = ctypes.c_size_t(), ctypes.c_size_t()
    k.GetProcessAffinityMask(ctypes.c_void_p(k.GetCurrentProcess()), ctypes.byref(proc), ctypes.byref(sys_))
    return proc.value, sys_.value


def test_reglage_change_en_cours_de_route(tmp_path):
    """Le réglage est relu et réappliqué sans relancer le processus (Windows : affinité)."""
    if sys.platform != "win32":
        return
    avant_masque = masque_courant()[0]
    with mock.patch.object(reglages, "FICHIER", tmp_path / "reglages.json"), \
            mock.patch.object(ressources, "INTERVALLE_S", 0.05), mock.patch.object(ressources.os, "cpu_count", return_value=4):
        try:
            reglages.ecrire({**reglages.lire(), "ressources": 100})
            ressources.surveiller()
            assert masque_courant()[0] == 0b1111
            reglages.ecrire({**reglages.lire(), "ressources": 50})
            for _ in range(100):
                if masque_courant()[0] == 0b11:
                    break
                time.sleep(0.02)
            assert masque_courant()[0] == 0b11
        finally:  # le processus de test retrouve tous ses cœurs
            import ctypes

            k = ctypes.windll.kernel32
            k.SetProcessAffinityMask(ctypes.c_void_p(k.GetCurrentProcess()), ctypes.c_size_t(avant_masque))
            k.SetPriorityClass(ctypes.c_void_p(k.GetCurrentProcess()), 0x20)
