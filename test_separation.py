"""Séparation des voix interchangeable (separation.diariser), sans réseau ni calcul audio : pytest test_separation.py"""

import hashlib
import json
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import separation
import transcribe
import voix
from test_transcribe import transcrire_simule


def test_regrouper_fusion_petites_voix_et_plafond():
    a, b, c = np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0])
    proche_a = np.array([0.95, 0.31, 0]) / np.linalg.norm([0.95, 0.31, 0])
    tours = [(0, 20, 0), (20, 40, 1), (40, 60, 2), (60, 65, 3), (65, 65.5, 4), (70, 90, 5)]
    vecs = [a, proche_a, b, c, None, c]
    t, e = separation.regrouper(tours, vecs, None)
    # 0 et 1 se ressemblent au-delà de 0,7 : une seule voix ; 3 (5 s) rejoint 5 (même empreinte) ; le tour
    # de 0,5 s sans empreinte prend la voix de son voisin.
    voix_de = [lbl for *_, lbl in t]
    assert voix_de[0] == voix_de[1] and voix_de[3] == voix_de[5] and len(set(voix_de)) == 3
    assert set(e) == set(voix_de) and all(abs(np.linalg.norm(v) - 1) < 1e-9 for v in e.values())
    assert voix_de[0] == "SPEAKER_00"  # la voix qui parle le plus d'abord
    # Plafond (nombre de personnes connu) : les deux plus proches fusionnent jusqu'à 2 voix.
    assert len({lbl for *_, lbl in separation.regrouper(tours, vecs, 2)[0]}) == 2


def test_methode_choisie_sans_cle():
    with tempfile.TemporaryDirectory() as t, mock.patch.object(separation, "FICHIER_JETON", Path(t) / "hf_token"):
        trouve = {"sherpa_onnx": True, "pyannote": True}
        with mock.patch("importlib.util.find_spec", lambda n: trouve.get(n)):
            assert separation.methode({"locuteurs": True}) == "libre"  # par défaut, sans compte ni clé
            assert separation.methode({"locuteurs": False}) is None
            separation.FICHIER_JETON.write_text("hf_x", encoding="utf-8")
            assert separation.methode({"locuteurs": True}) == "pyannote"  # option avancée, avec la clé
            trouve["sherpa_onnx"] = trouve["pyannote"] = None
            assert separation.methode({"locuteurs": True}) is None  # Q3 : « Moi » / « Les autres »
    assert separation.modele_empreintes("libre") == "titanet-small"
    assert separation.modele_empreintes(None) == voix.PYANNOTE


def test_cache_windows_accentue_et_depot_a_la_main(monkeypatch, tmp_path):
    monkeypatch.delenv("REUNION_MODELES_DIAR", raising=False)
    monkeypatch.setattr(separation.sys, "platform", "win32")
    monkeypatch.setattr(separation.Path, "home", lambda: Path("C:/Users/Hélène"))
    # sherpa-onnx #3885 : chemin court 8.3 du même dossier, dans le compte (S3), jamais ProgramData.
    monkeypatch.setattr(separation, "chemin_court", lambda d: Path("C:/Users/HLNE~1") / ".cache" / "diar-libre")
    assert separation.cache_libre() == Path("C:/Users/HLNE~1") / ".cache" / "diar-libre"
    monkeypatch.setenv("REUNION_MODELES_DIAR", str(tmp_path))
    assert len(separation.a_deposer()) == 2 and all(u.startswith(separation.URL) for u in separation.a_deposer())
    # Fichiers déposés à la main, d'empreinte attendue : rien n'est téléchargé.
    bon = hashlib.sha256(b"onnx").hexdigest()
    monkeypatch.setattr(separation, "SEGMENTATION", (separation.SEGMENTATION[0], "x", 7))
    monkeypatch.setattr(separation, "EMPREINTE", (separation.EMPREINTE[0], bon, 40))
    monkeypatch.setattr(separation, "SHA_SEGMENTATION_ONNX", bon)
    for rel, *_ in (separation.SEGMENTATION, separation.EMPREINTE):
        f = separation.fichier_libre(rel)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"onnx")
    with mock.patch.object(separation.urllib.request, "urlopen", side_effect=AssertionError("téléchargé")):
        separation.telecharger_libre()
    assert separation.prete("libre") and separation.a_deposer() == []
    # Fichier altéré (S3) : refusé, et le téléchargement échoue sans laisser de .part.
    separation.fichier_libre(separation.EMPREINTE[0]).write_bytes(b"autre chose")
    assert not separation.prete("libre")
    with mock.patch.object(separation.urllib.request, "urlopen", side_effect=OSError("réseau bloqué")):
        with pytest.raises(OSError):
            separation.telecharger_libre()
    assert not list(tmp_path.glob("*.part"))


def test_une_banque_par_modele(tmp_path, monkeypatch):
    monkeypatch.setattr(voix, "BANQUE", tmp_path / "voix.json")
    voix.ecrire_banque({"Camille": {"emb": [1.0] * 256, "n": 3}})  # appris avec pyannote, avant le 03/11
    voix.ecrire_banque({"Sacha": {"emb": [1.0] * 192, "n": 1}}, "titanet-small")
    assert (tmp_path / "voix-titanet-small.json").exists() and set(voix.lire_banque()) == {"Camille"}
    # Empreintes de tailles différentes : jamais reconnues l'une pour l'autre, rien ne plante.
    assert voix.reconnaitre({"V": [1.0] * 192}, voix.lire_banque()) == {}
    assert voix.reconnaitre({"V": [1.0] * 192}, voix.lire_banque("titanet-small"))["V"][0] == "Sacha"
    d = tmp_path / "reunion"
    d.mkdir()
    (d / "locuteurs.json").write_text(json.dumps({"Voix 2": [0.5] * 192}), encoding="utf-8")
    voix.retenir(d, "Voix 2", "Alex", modele="titanet-small")
    assert set(voix.lire_banque("titanet-small")) == {"Sacha", "Alex"} and set(voix.lire_banque()) == {"Camille"}
    # Seuil par modèle : 0,6 suffit avec pyannote, pas avec la méthode libre.
    assert voix.niveau(0.62, voix.SEUILS[voix.PYANNOTE]) == "probable"
    assert voix.niveau(0.62, voix.SEUILS["titanet-small"]) is None


def test_sans_separation_moi_et_les_autres():
    """Q3 : sans méthode de séparation, le micro est « Moi » et le son de l'ordinateur « Les autres »."""
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        (d / "systeme.wav").write_bytes(b"\0" * 8192)
        (d / "micro.wav").write_bytes(b"\0" * 8192)
        with mock.patch.object(transcribe.separation, "methode", return_value=None):
            locs, separees, *_ = transcrire_simule(d, systeme_parle=True)
        assert locs == {"Moi", "Les autres"} and not separees
        assert json.loads((d / "transcription.json").read_text(encoding="utf-8"))["separation"] is False


def test_separer_en_nombre_connu():
    """Nombre de personnes connu : deux voix proches (cosinus ~0,75) mais distinctes restent deux, à parts réalistes,
    même si une seule domine les groupes de départ ; les longs tours sont coupés et les tours sans empreinte suivent
    leur voisin."""
    rng = np.random.default_rng(3)
    a = np.zeros(16); a[0] = 1
    b = np.zeros(16); b[0], b[1] = 0.75, np.sqrt(1 - 0.75 ** 2)
    tours, vecs = [], []
    for i in range(60):
        base = a if i % 3 else b  # a parle 2/3 du temps
        v = base + rng.normal(0, 0.05, 16)
        tours.append((i * 5.0, i * 5.0 + 4, 0))  # sherpa a tout mis dans un seul groupe
        vecs.append(v / np.linalg.norm(v))
    tours.append((300.0, 300.5, 0)); vecs.append(None)
    t, e = separation.separer_en(tours, vecs, 2)
    voix_de = [lbl for *_, lbl in t]
    assert voix_de[:60] == ["SPEAKER_01" if i % 3 == 0 else "SPEAKER_00" for i in range(60)]
    assert voix_de[60] == voix_de[59] and set(e) == {"SPEAKER_00", "SPEAKER_01"}
    assert separation.decouper([(0, 20, 7)]) == [(0, 8, 7), (8, 16, 7), (16, 20, 7)]
