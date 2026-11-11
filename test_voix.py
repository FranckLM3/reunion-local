"""Vérifications rapides de la logique pure : python3 test_voix.py"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import voix


def test_reconnaitre():
    banque = {"Antoine": {"emb": [1, 0, 0], "n": 1}, "Bertrand": {"emb": [0, 1, 0], "n": 1}}
    emp = {"Intervenant 1": [0.9, 0.1, 0], "Intervenant 2": [0.95, 0.05, 0], "Intervenant 3": [0, 0, 1]}
    # Les deux premiers ressemblent à Antoine : le plus proche le prend, l'autre reste inconnu.
    r = voix.reconnaitre(emp, banque)
    assert list(r) == ["Intervenant 2"] and r["Intervenant 2"][0] == "Antoine" and r["Intervenant 2"][1] > 0.99
    assert voix.reconnaitre({"Intervenant 1": [0.1, 0.9, 0]}, banque)["Intervenant 1"][0] == "Bertrand"
    assert voix.reconnaitre(emp, {}) == {}
    # Seuils de confiance (Q4) : très probable dès 0,80, probable dès 0,65, rien en dessous.
    assert [voix.niveau(c) for c in (0.80, 0.79, 0.65, 0.64)] == ["très probable", "probable", "probable", None]
    v = {"V": [1, 1.2, 0]}  # cosinus 0,64 avec Antoine, 0,77 avec Bertrand
    assert voix.reconnaitre(v, {"Antoine": banque["Antoine"]}) == {}
    assert voix.niveau(voix.reconnaitre(v, banque)["V"][1]) == "probable"


def test_apprendre_et_retenir():
    b = voix.apprendre("Antoine", [1.0, 0.0], {})
    b = voix.apprendre("Antoine", [0.0, 1.0], b)
    assert b["Antoine"]["emb"] == [0.5, 0.5] and b["Antoine"]["n"] == 2 and b["Antoine"]["vu"]  # date (VX-16)
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        voix.BANQUE = d / "voix.json"
        (d / "locuteurs.json").write_text(json.dumps({"Intervenant 1": [1.0, 0.0], "Franck": [0.0, 1.0]}), encoding="utf-8")
        voix.retenir(d, "Intervenant 1", "Intervenant 9")   # renommage provisoire : rien appris
        assert not voix.BANQUE.exists()
        # Sans la case « avec son accord » : l'empreinte suit le nom dans la réunion, rien n'est retenu (Q1).
        voix.retenir(d, "Voix 9", "Antoine", apprendre_voix=False)  # « Intervenant 9 » affiché « Voix 9 »
        assert set(json.loads((d / "locuteurs.json").read_text(encoding="utf-8"))) == {"Antoine", "Franck"}
        assert not voix.BANQUE.exists()
        voix.retenir(d, "Antoine", "Antoine")  # avec accord
        assert voix.lire_banque()["Antoine"]["emb"] == [1.0, 0.0] and voix.lire_banque()["Antoine"]["n"] == 1
        voix.retenir(d, "Personne", "X")                    # nom absent : sans effet
        (d / "options.json").write_text(json.dumps({"chuchote": True}), encoding="utf-8")
        voix.retenir(d, "Franck", "Bertrand")               # atelier chuchoté : rien appris
        assert set(voix.lire_banque()) == {"Antoine"}


def test_utf8_partout():
    """Sous Windows, un fichier texte ouvert sans encodage l'est en cp1252 : « Álvaro » relu devient illisible.
    Python signale chaque ouverture sans encodage (EncodingWarning), changée ici en erreur, dans un HOME jetable."""
    script = """
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
import agenda, reglages, separation, transcribe, voix
(d / "locuteurs.json").write_text(json.dumps({"Intervenant 1": [1.0]}), encoding="utf-8")
voix.retenir(d, "Intervenant 1", "Álvaro")
voix.retenir(d, "Álvaro", "Ïsmaël")
assert set(voix.lire_banque()) == {"Álvaro", "Ïsmaël"}
reglages.ecrire({**reglages.lire(), "moi": "Ïsmaël"})
assert reglages.lire()["moi"] == "Ïsmaël"
reglages.ecrire_secret(agenda.FICHIER_URL, "https://calendar.google.com/calendar/ical/álvaro/basic.ics")
assert "álvaro" in agenda.url_agenda()
reglages.ecrire_secret(separation.FICHIER_JETON, "hf_x")
assert transcribe.jeton_hf() == "hf_x"
transcribe.ecrire(d, [{"debut": 0, "fin": 1, "loc": "Ïsmaël", "texte": "à toi Álvaro"}], "x")
assert "Ïsmaël" in (d / "transcription.md").read_text(encoding="utf-8")
"""
    with tempfile.TemporaryDirectory() as t:
        r = subprocess.run([sys.executable, "-X", "warn_default_encoding", "-W", "error::EncodingWarning",
                            "-c", script, t], cwd=Path(__file__).parent, env={**os.environ, "HOME": t, "USERPROFILE": t},  # Windows : Path.home() lit USERPROFILE
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr


if __name__ == "__main__":
    for nom, f in list(globals().items()):
        if nom.startswith("test_"):
            f()
    print("ok")
