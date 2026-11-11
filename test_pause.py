"""Pause d'un traitement (pause.py, /api/pause) : pytest test_pause.py"""
import subprocess
import sys
import time

import pause
import serveur

# Un enfant qui lance un petit-enfant (comme la trampoline uv) ; le petit-enfant écrit un compteur.
ENFANT = """
import subprocess, sys
subprocess.run([sys.executable, "-c", "import time, sys\\nwhile True:\\n print(1, flush=True)\\n time.sleep(0.05)"])
"""


def test_geler_puis_reprendre_tout_l_arbre(tmp_path):
    sortie = tmp_path / "sortie.txt"
    with sortie.open("wb") as f:
        p = subprocess.Popen([sys.executable, "-c", ENFANT], stdout=f)
    try:
        time.sleep(1.5)
        assert len(pause.descendants(p.pid)) >= 2  # l'enfant et le petit-enfant
        pause.geler(p.pid)
        time.sleep(0.3)  # une ligne déjà partie peut arriver
        avant = sortie.stat().st_size
        time.sleep(1.0)
        assert sortie.stat().st_size == avant  # plus rien n'est écrit
        pause.geler(p.pid, False)
        time.sleep(1.0)
        assert sortie.stat().st_size > avant  # le calcul a repris
    finally:
        for q in reversed(pause.descendants(p.pid)):
            subprocess.run(["taskkill", "/F", "/PID", str(q)], capture_output=True) if sys.platform == "win32" \
                else subprocess.run(["kill", "-9", str(q)])


def test_pause_decale_les_chronos(monkeypatch):
    gele = []
    monkeypatch.setattr(pause, "geler", lambda pid, oui=True: gele.append((pid, oui)))
    t0 = time.time() - 100
    t = {"en_cours": True, "pid": 42, "debut": t0, "fin_estimee": t0 + 200, "etape": 3, "fraction": 0.8,
         "chrono": {"2": {"debut": t0, "fin": t0 + 50}, "3": {"debut": t0 + 50, "t0": t0 + 60, "f0": 0.75}},
         "prevu": {1: 1, 2: 50, 3: 100, 4: 1}}
    monkeypatch.setitem(serveur.travaux, "r1", t)
    assert serveur.mettre_en_pause({"reunion": "r1", "pause": True}) == {"ok": True}
    assert serveur.mettre_en_pause({"reunion": "r1", "pause": True}) == {"ok": True}  # pas de double gel
    assert gele == [(42, True)]
    t["pause"] -= 30  # 30 s de pause
    reste_en_pause = [e["restant_s"] for e in serveur.etapes_de(t)]
    serveur.mettre_en_pause({"reunion": "r1", "pause": False})
    assert gele == [(42, True), (42, False)] and "pause" not in t
    assert abs(t["debut"] - (t0 + 30)) < 1 and abs(t["chrono"]["3"]["t0"] - (t0 + 90)) < 1
    assert t["chrono"]["2"]["debut"] == t0  # étape finie : inchangée
    assert [e["restant_s"] for e in serveur.etapes_de(t)] == reste_en_pause  # la pause ne compte pas
    assert "erreur" in serveur.mettre_en_pause({"reunion": "inconnue", "pause": True})
