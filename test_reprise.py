"""Reprise d'une transcription interrompue (reprise.py)."""
import json
import tempfile
from pathlib import Path

import reprise


def sig(**autres):
    return {"version": reprise.VERSION, "modele": "m", "options": {}, **autres}


def test_segments_gardes_et_ligne_coupee():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        r = reprise.Reprise(d, sig())
        assert not r.reprise and r.segments("micro") == ([], False)
        r.ajouter("micro", {"text": "a", "end": 1.0})
        r.ajouter("micro", {"text": "b", "end": 2.0})
        with (d / "reprise" / "whisper-micro.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"text": "coup')  # arrêt en pleine écriture
        r2 = reprise.Reprise(d, sig())
        assert r2.reprise
        assert r2.segments("micro") == ([{"text": "a", "end": 1.0}, {"text": "b", "end": 2.0}], False)
        r2.ajouter("micro", {"text": "c", "end": 3.0})  # la ligne abîmée a été retirée : l'ajout reste lisible
        r2.finir("micro")
        segs, fini = r2.segments("micro")
        assert [s["text"] for s in segs] == ["a", "b", "c"] and fini


def test_reglage_change_tout_recommence():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        r = reprise.Reprise(d, sig())
        r.ajouter("micro", {"text": "a", "end": 1.0})
        r.garder_separation("micro", [(0.0, 1.0, "SPEAKER_00")], {"SPEAKER_00": [1.0]})
        r2 = reprise.Reprise(d, sig(langue="en"))
        assert not r2.reprise
        assert r2.segments("micro") == ([], False) and r2.separation("micro") is None


def test_separation_et_rehaussement():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        r = reprise.Reprise(d, sig())
        copie = d / "micro-rehausse.wav"
        copie.write_bytes(b"x")
        assert not r.rehausse_pret(copie)  # copie présente mais peut-être coupée par l'arrêt
        r.rehausse_fait()
        assert r.rehausse_pret(copie)
        r.garder_separation("micro", [(0.0, 1.5, "SPEAKER_01")], {"SPEAKER_01": [0.5, 0.5]})
        assert reprise.Reprise(d, sig()).separation("micro") == ([(0.0, 1.5, "SPEAKER_01")], {"SPEAKER_01": [0.5, 0.5]})
        r.effacer()
        assert not (d / "reprise").exists()


def test_signature_suit_les_pistes_et_le_vocabulaire():
    with tempfile.TemporaryDirectory() as t:
        wav = Path(t) / "micro.wav"
        wav.write_bytes(b"1234")
        a = reprise.signature("m", "faster", "fr", "Kentin", {}, {"micro": wav})
        assert a == reprise.signature("m", "faster", "fr", "Kentin", {}, {"micro": wav})
        assert a != reprise.signature("m", "faster", "fr", "Aymeric", {}, {"micro": wav})
        wav.write_bytes(b"123456")
        assert a != reprise.signature("m", "faster", "fr", "Kentin", {}, {"micro": wav})
        json.dumps(a)
