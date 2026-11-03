"""Vérifications du serveur local, sans réseau ni micro : python3 test_serveur.py"""

import array
import contextlib
import json
import os
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import reglages
import serveur
import transcribe
import voix


@contextlib.contextmanager
def serveur_local():
    """Serveur sur un port libre, avec réunions, réglages, jeton, vocabulaire et voix dans un dossier temporaire."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        avant = (serveur.BASE, serveur.HOTES, serveur.VOCABULAIRE, reglages.FICHIER, serveur.separation.FICHIER_JETON,
                 voix.BANQUE, serveur.LOG, serveur.agenda.FICHIER_URL, os.environ.pop("REUNION_DIR", None))
        serveur.BASE = t / "Reunions"
        serveur.BASE.mkdir()
        serveur.VOCABULAIRE = t / "vocabulaire.txt"
        reglages.FICHIER = t / "conf" / "reglages.json"
        serveur.separation.FICHIER_JETON = t / "conf" / "hf_token"
        voix.BANQUE = t / "conf" / "voix.json"
        serveur.LOG = t / "journaux" / "reunion.log"
        serveur.agenda.FICHIER_URL = t / "conf" / "agenda_ics"
        vraie_installation, reglages.installation_existante = reglages.installation_existante, reglages.FICHIER.exists
        http = ThreadingHTTPServer(("127.0.0.1", 0), serveur.Gestionnaire)
        port = http.server_address[1]
        serveur.HOTES = {f"127.0.0.1:{port}"}
        threading.Thread(target=http.serve_forever, daemon=True).start()

        def get(chemin):
            return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}{chemin}"))

        def post(chemin, corps):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{chemin}", data=json.dumps(corps).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                return json.load(urllib.request.urlopen(req))
            except urllib.error.HTTPError as e:
                return json.load(e)

        try:
            yield t, get, post, port
        finally:
            http.shutdown()
            (serveur.BASE, serveur.HOTES, serveur.VOCABULAIRE, reglages.FICHIER, serveur.separation.FICHIER_JETON,
             voix.BANQUE, serveur.LOG, serveur.agenda.FICHIER_URL, reunion_dir) = avant
            if reunion_dir:
                os.environ["REUNION_DIR"] = reunion_dir
            serveur.travaux.clear()
            serveur.vues.clear()
            serveur.preparation = serveur.test_son = None
            reglages.installation_existante = vraie_installation


def piste(f: Path, valeur: float, secondes: int = 1):
    if not f.exists():  # en-tête minimal : taux_wav ne lit que le bloc fmt
        f.write_bytes(b"RIFF\0\0\0\0WAVEfmt " + struct.pack("<IHHIIHH", 16, 3, 1, 48000, 192000, 4, 32) + b"data\0\0\0\0")
    with f.open("ab") as h:
        h.write(array.array("f", [valeur] * 48000 * secondes).tobytes())


def test_routes():
    with serveur_local() as (t, get, post, port):
        accent = serveur.BASE / "261013-1418-Formation-à-l-entretien"
        accent.mkdir()
        (accent / "micro.wav").write_bytes(b"")
        abime = serveur.BASE / "261013-1500-abime"
        abime.mkdir()
        (abime / "micro.wav").write_bytes(b"")
        (abime / "transcription.json").write_text("{coupé", encoding="utf-8")
        (abime / "CR-261013-abime.md").write_text("# CR", encoding="utf-8")
        # Un fichier abîmé ne fait pas disparaître la liste.
        assert {r["nom"] for r in get("/api/reunions")} == {accent.name, abime.name}
        # Un nom accentué s'ouvre, encodé comme le fait l'interface.
        r = get("/api/reunion/" + urllib.parse.quote(accent.name))
        assert r["nom"] == accent.name and r["compte_rendu"] is None
        assert get("/api/reunion/" + abime.name)["compte_rendu"] == {"fichier": "CR-261013-abime.md", "texte": "# CR"}


def test_liste_etats_et_durees():
    with serveur_local() as (t, get, post, port):
        noms = {e: serveur.BASE / f"261103-10{i}0-{e}" for i, e in enumerate(("enregistree", "transcrite", "echec"))}
        for d in noms.values():
            d.mkdir()
            piste(d / "systeme.wav", 0.1, 2)
        (noms["transcrite"] / "transcription.json").write_text(json.dumps(
            {"source": "x", "blocs": [{"debut": 0, "fin": 1, "loc": "Antoine", "texte": "bonjour"}]}), encoding="utf-8")
        (noms["echec"] / "echec.txt").write_text("Échec (disque plein)", encoding="utf-8")
        (serveur.BASE / "pas-une-reunion").mkdir()
        liste = {r["nom"]: r for r in get("/api/reunions")}
        assert {liste[d.name]["etat"] for d in noms.values()} == {"enregistree", "transcrite", "echec"}
        assert all(liste[d.name]["etat"] == e for e, d in noms.items())
        assert abs(liste[noms["transcrite"].name]["duree"] - 2.0) < 0.1  # secondes, d'après la taille de la piste
        assert liste[noms["transcrite"].name]["locuteurs"] == ["Antoine"]
        assert "pas-une-reunion" not in liste


def test_liste_relue_seulement_si_changee():
    with serveur_local() as (t, get, post, port):
        d = serveur.BASE / "261103-1000-point"
        d.mkdir()
        piste(d / "systeme.wav", 0.1)
        (d / "transcription.json").write_text(json.dumps(
            {"source": "x", "blocs": [{"debut": 0, "fin": 1, "loc": "Antoine", "texte": "bonjour"}]}), encoding="utf-8")
        with mock.patch.object(serveur, "lire_transcription", wraps=serveur.lire_transcription) as lu:
            assert serveur.lister()[0]["locuteurs"] == ["Antoine"]
            assert serveur.lister()[0]["duree"] == 1.0
            assert lu.call_count == 1  # second appel servi par le cache
            piste(d / "systeme.wav", 0.1)  # la piste grossit (enregistrement) : durée relue
            assert serveur.lister()[0]["duree"] == 2.0 and lu.call_count == 2
            (d / "transcription.json").write_text(json.dumps(
                {"source": "x", "blocs": [{"debut": 0, "fin": 1, "loc": "Bertrand", "texte": "salut"}]}),
                encoding="utf-8")
            assert serveur.lister()[0]["locuteurs"] == ["Bertrand"]


def test_renommer_et_supprimer():
    with serveur_local() as (t, get, post, port):
        d = serveur.BASE / "261103-1000-reunion"
        d.mkdir()
        piste(d / "micro.wav", 0.1)
        r = post("/api/renommer-reunion", {"reunion": d.name, "titre": "Point hebdo / X-UP"})
        assert r == {"ok": True, "nom": "261103-1000-Point-hebdo-X-UP"}, r
        nouveau = serveur.BASE / r["nom"]
        assert nouveau.is_dir() and not d.exists()
        autre = serveur.BASE / "261103-1000-autre"  # même minute : le nom existe déjà
        autre.mkdir()
        piste(autre / "micro.wav", 0.1)
        assert "erreur" in post("/api/renommer-reunion", {"reunion": autre.name, "titre": "Point hebdo X-UP"})
        # Réunion en cours de transcription : intouchable.
        serveur.travaux[nouveau.name] = {"en_cours": True, "ligne": "", "code": None}
        assert "erreur" in post("/api/supprimer", {"reunion": nouveau.name})
        serveur.travaux.clear()
        # Ni chemin détourné, ni dossier qui n'est pas une réunion.
        assert "erreur" in post("/api/supprimer", {"reunion": "../conf"})
        (serveur.BASE / "divers").mkdir()
        assert "erreur" in post("/api/supprimer", {"reunion": "divers"})
        assert post("/api/supprimer", {"reunion": nouveau.name}) == {"ok": True}
        assert not nouveau.exists() and (serveur.BASE / "divers").exists()


def test_renommage_refuse_par_windows():
    """Windows refuse de renommer un dossier dont un fichier est ouvert : l'ancien nom reste, la transcription
    part quand même, la page reçoit une erreur lisible."""
    with serveur_local() as (t, get, post, port):
        d = serveur.BASE / "261103-1000-reunion"
        d.mkdir()
        piste(d / "micro.wav", 0.1)
        refus = mock.patch.object(Path, "rename", side_effect=PermissionError(13, "Accès refusé"))
        with refus, mock.patch.object(serveur.agenda, "participants", return_value={"titre": "Point", "noms": []}):
            assert serveur.consulter_agenda(d, datetime.now(), True) == d
        with refus:
            assert post("/api/renommer-reunion", {"reunion": d.name, "titre": "X"})["erreur"]["code"] == "protege"
        assert d.is_dir()


FAUX_SOUNDCARD = '''
import os, time
import numpy as np
time.sleep(float(os.environ.get("FAUX_SC_DELAI", "0")))  # import lent, comme sous Windows

class Source:
    def __init__(self, id, name):
        self.id, self.name = id, name
    def recorder(self, samplerate, blocksize):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass
    def record(self, numframes):
        time.sleep(numframes / 48000)
        return np.full((numframes, 1), 0.01, dtype=np.float32)

def _defaut(id, nom):
    if os.environ.get("FAUX_SC_ECHEC"):
        raise RuntimeError("aucun périphérique")
    return Source(id, nom)

default_microphone = lambda: _defaut("m1", "Micro (Réaltek)")
default_speaker = lambda: _defaut("s1", "Haut-parleurs")
get_microphone = lambda id, include_loopback=False: Source(id, "Boucle" if include_loopback else "Micro")
all_microphones = lambda: [default_microphone()]
all_speakers = lambda: [default_speaker()]
'''


@contextlib.contextmanager
def windows_simule(t: Path, **env):
    """capture.py lancé comme sous Windows, avec un soundcard simulé et un HOME jetable."""
    (t / "faux").mkdir(exist_ok=True)
    (t / "faux" / "soundcard.py").write_text(FAUX_SOUNDCARD, encoding="utf-8")
    with mock.patch.object(serveur, "MAC", False), \
            mock.patch.dict(os.environ, {"PYTHONPATH": str(t / "faux"), "HOME": str(t), **env}):
        yield


def test_peripheriques_dans_un_processus_a_part():
    """soundcard initialise COM dans le thread qui l'importe : sous Windows, les threads du serveur ne peuvent pas
    s'en servir. Liste et test de niveau passent par capture.py, appelé à chaque requête."""
    with serveur_local() as (t, get, post, port), windows_simule(t):
        c = get("/api/configuration")
        assert c["peripheriques"]["micros"] == [{"id": "m1", "nom": "Micro (Réaltek)"}], c["erreur_peripheriques"]
        for _ in range(2):  # deux requêtes, deux threads du serveur
            r = post("/api/niveau", {"quoi": "micro", "id": ""})
            assert r["ok"] and abs(r["db"] + 40) < 0.5, r
        with mock.patch.dict(os.environ, {"FAUX_SC_ECHEC": "1"}):
            assert "aucun périphérique" in get("/api/configuration")["erreur_peripheriques"]
            assert post("/api/niveau", {"quoi": "micro", "id": ""})["erreur"]["code"] == "aucun_micro"


def test_demarrage_lent_sous_windows():
    """capture.py met plusieurs secondes à charger soundcard : un échec après 3 s passait pour un démarrage."""
    with serveur_local() as (t, get, post, port):
        with windows_simule(t, FAUX_SC_DELAI="4", FAUX_SC_ECHEC="1"):
            r = serveur.demarrer({"nom": "lent"})
        assert r["erreur"]["code"] == "aucun_micro" and r["erreur"]["journal"].endswith("reunion.log"), r
        assert not list(serveur.BASE.iterdir())  # dossier retiré
        with windows_simule(t):
            r = serveur.demarrer({"nom": "ok"})
            e = serveur.enregistrement
            try:
                assert r["ok"] and e and e["proc"].poll() is None, r
            finally:
                serveur.enregistrement = None
                serveur.stopper(e["proc"])
        assert e["proc"].returncode == 0 and (e["dossier"] / "pistes.json").exists()


def test_une_seule_instance():
    """Port déjà pris par une instance : la seconde ouvre la page existante et s'arrête, sans rien toucher."""
    premier = serveur.Serveur(("127.0.0.1", 0), serveur.Gestionnaire)
    try:
        port = premier.server_address[1]
        with mock.patch.object(serveur, "PORT", port), mock.patch.object(serveur.webbrowser, "open") as ouvre, \
                mock.patch.object(serveur, "verifications", side_effect=AssertionError("seconde instance lancée")), \
                mock.patch.dict(os.environ, {"REUNION_SANS_NAVIGATEUR": ""}):
            assert serveur.main() == 0
        ouvre.assert_called_once_with(f"http://127.0.0.1:{port}")
    finally:
        premier.server_close()


def test_configuration():
    with serveur_local() as (t, get, post, port):
        c = get("/api/configuration")
        assert c["reglages"] == {**reglages.DEFAUTS, "accueil_fini": False} and c["dossier"] == str(serveur.BASE)
        assert [m["taille"] for m in c["modeles"]][-1] == "large-v3-turbo" and all(m["mo"] > 0 for m in c["modeles"])
        assert {v["titre"] for v in c["verifications"]} >= {"Conversion du son", "Place libre"}
        assert c["jeton"] is False

        nouveau = t / "ailleurs" / "Reunions"
        r = post("/api/configuration", {"moteur": "faster", "modele": "small", "langue": "en", "locuteurs": False,
                                         "dossier": str(nouveau), "moi": "  Lou  ", "inconnu": 1})
        assert r == {"ok": True}, r
        lu = json.loads(reglages.FICHIER.read_text(encoding="utf-8"))
        assert lu == {**reglages.DEFAUTS, "moteur": "faster", "modele": "small", "langue": "en", "locuteurs": False,
                      "dossier": str(nouveau), "moi": "Lou", "accueil_fini": False}
        assert serveur.BASE == nouveau and nouveau.is_dir()
        assert get("/api/configuration")["modeles"][2] == {"taille": "small", "mo": 486, "telecharge": False}

        for mauvais in ({"modele": "énorme"}, {"langue": "klingon"}, {"moteur": "autre"},
                        {"dossier": "relatif/Reunions"}, {"micro": 3}):
            assert "erreur" in post("/api/configuration", mauvais), mauvais
        assert json.loads(reglages.FICHIER.read_text(encoding="utf-8"))["modele"] == "small"  # rien changé par un refus
        # Réglages d'avant la suite d'accueil (installation antérieure) : l'accueil ne s'affiche pas (PL-02).
        reglages.FICHIER.write_text(json.dumps({"modele": "large-v3-turbo"}), encoding="utf-8")
        assert get("/api/etat")["accueil_fini"] is True and reglages.lire()["modele"] == "large-v3-turbo"


def test_jeton_jamais_renvoye():
    with serveur_local() as (t, get, post, port):
        jeton = "hf_" + "a1B2" * 9
        assert "erreur" in post("/api/jeton", {"jeton": "mot de passe"})
        assert post("/api/jeton", {"jeton": jeton}) == {"ok": True}
        assert serveur.separation.FICHIER_JETON.read_text(encoding="utf-8") == jeton
        if os.name == "posix":
            assert serveur.separation.FICHIER_JETON.stat().st_mode & 0o777 == 0o600
        for chemin in ("/api/configuration", "/api/etat"):
            texte = json.dumps(get(chemin))
            assert jeton not in texte and '"jeton": true' in texte
        assert post("/api/jeton", {"jeton": ""}) == {"ok": True} and not serveur.separation.FICHIER_JETON.exists()


def test_vocabulaire_et_voix():
    with serveur_local() as (t, get, post, port):
        assert get("/api/vocabulaire") == {"texte": ""}
        assert post("/api/vocabulaire", {"texte": "Acme\nProjet Nord\n"}) == {"ok": True}
        assert get("/api/vocabulaire") == {"texte": "Acme\nProjet Nord\n"}
        assert "erreur" in post("/api/vocabulaire", {"texte": ["pas", "du", "texte"]})

        voix.ecrire_banque({"Antoine": {"emb": [1.0], "n": 3}, "Bertrand": {"emb": [0.0], "n": 1}})
        assert get("/api/voix") == [{"nom": "Antoine", "reunions": 3, "vu": None, "modele": voix.PYANNOTE},
                                    {"nom": "Bertrand", "reunions": 1, "vu": None, "modele": voix.PYANNOTE}]
        assert post("/api/voix/oublier", {"nom": "Antoine"}) == {"ok": True}
        assert set(voix.lire_banque()) == {"Bertrand"}
        assert "erreur" in post("/api/voix/oublier", {"nom": "Personne"})
        assert "erreur" in post("/api/voix/oublier", {"nom": ["pas", "hachable"]})
        for corps in ([1, 2], "texte", 3):  # corps JSON valide mais pas un objet : 400, pas de plantage
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/voix/oublier", data=json.dumps(corps).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req)
                raise AssertionError(f"{corps!r} accepté")
            except urllib.error.HTTPError as e:
                assert e.code == 400


def attendre(condition, delai=5.0):
    fin = time.time() + delai
    while time.time() < fin:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_preparation_simulee():
    """Téléchargement sans clé, pause et reprise seule au retour du réseau, réseau filtré, place insuffisante."""
    with serveur_local() as (t, get, post, port):
        lance, sorties, telecharges = [], [], []

        def executer(travail, args, journal=None):
            lance.append(args)
            code, travail["echec"] = sorties.pop(0) if sorties else (0, None)
            if not code:
                telecharges.append(args)
            return code

        # Sans extrait de vitesse : ce test compte seulement les téléchargements.
        with mock.patch.object(serveur, "executer", executer), \
                mock.patch.object(serveur, "ESSAI_VITESSE", serveur.ICI / "absent.wav"), \
                mock.patch.object(serveur.separation, "methode", lambda: None), \
                mock.patch.object(serveur.transcribe, "en_cache", lambda r: bool(telecharges)):
            assert post("/api/telecharger", {"modele": "tiny"}) == {"ok": True}
            assert attendre(lambda: not serveur.preparation["en_cours"])
            assert lance[0][1:3] == ["--telecharger", "--moteur"] and lance[0][-2:] == ["--modele", "tiny"]
            p = get("/api/etat")["preparation"]
            assert p["modele"] == "tiny" and p["etape"] == "fini" and p["erreur"] is None and p["fait_mo"] == 0

            # PL-13 : pas de réseau, pause ; « Réessayer maintenant » relance sans attendre les 30 s.
            lance.clear()
            telecharges.clear()
            sorties[:] = [(1, "reseau_absent")]
            assert post("/api/preparer", {"modele": "tiny"}) == {"ok": True}
            assert attendre(lambda: get("/api/etat")["preparation"]["erreur"])
            assert get("/api/etat")["preparation"]["erreur"]["code"] == "reseau_absent"
            assert post("/api/preparer", {"reessayer": True}) == {"ok": True}
            assert attendre(lambda: not serveur.preparation["en_cours"]) and len(lance) == 2
            assert serveur.preparation["erreur"] is None

            # PL-14 : réseau filtré, la préparation s'arrête avec sa cause.
            telecharges.clear()
            sorties[:] = [(1, "reseau_filtre")]
            assert post("/api/preparer", {"modele": "tiny"}) == {"ok": True}
            assert attendre(lambda: not serveur.preparation["en_cours"])
            assert get("/api/etat")["preparation"]["erreur"]["code"] == "reseau_filtre"
        assert post("/api/telecharger", {"modele": "inexistant"})["erreur"]["code"] == "invalide"

        # PL-15 : place libre inférieure à la taille + 2 Go, le téléchargement ne démarre pas.
        with mock.patch.object(serveur.shutil, "disk_usage", return_value=mock.Mock(free=1.2e9)):
            r = post("/api/preparer", {"modele": "large-v3"})
        assert r["erreur"]["code"] == "place_preparation" and "il en reste 1,2 Go" in r["erreur"]["cause"], r


def test_securite():
    with serveur_local() as (t, get, post, port):
        # Autre site : refusé (Origin), même pour lire la configuration.
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/jeton", data=b'{"jeton": ""}',
                                     headers={"Content-Type": "application/json", "Origin": "https://exemple.com"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("requête d'un autre site acceptée")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/configuration", headers={"Host": "evil.com"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("Host étranger accepté")
        except urllib.error.HTTPError as e:
            assert e.code == 403


def test_son_coupe():
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "systeme.wav"
        e, suivi = {"dossier": Path(t), "muet": False}, {}
        piste(f, 0.0)
        assert not serveur.son_coupe(e, suivi)  # jamais entendue (présentiel, visio pas encore rejointe) : pas d'alerte
        piste(f, 0.1)
        assert not serveur.son_coupe(e, suivi)
        suivi["son"] -= serveur.SEUIL_MUET  # une minute plus tard, plus rien n'arrive (fichier figé)
        assert serveur.son_coupe(e, suivi) and e["muet"]
        assert not serveur.son_coupe(e, suivi)  # une seule alerte
        piste(f, 0.1)
        assert not serveur.son_coupe(e, suivi) and not e["muet"]  # le son revient : réarmée
        piste(f, 0.0)
        suivi["son"] -= serveur.SEUIL_MUET  # silence numérique écrit (cas du 30/10)
        assert serveur.son_coupe(e, suivi)
        assert not e["silence"]  # déjà entendue : c'est une coupure, pas un silence de départ


def test_son_de_l_ordinateur_jamais_entendu():
    """Cas du 03/11 : piste système en zéros, micro audible. Bandeau au bout de 60 s, jamais de notification,
    et rien en présentiel coché. Exposé, avec « muet » et « figé », dans /api/etat."""
    with serveur_local() as (t, get, post, port):
        d = serveur.BASE / "261103-0900-atelier"
        d.mkdir()
        piste(d / "systeme.wav", 0.0)
        piste(d / "micro.wav", 0.01)  # -40 dB
        e, suivi = {"dossier": d, "muet": False, "fige": False, "options": {}, "debut": 0.0}, {}
        assert not serveur.son_coupe(e, suivi) and not e["silence"]
        suivi["debut"] -= serveur.SEUIL_MUET
        assert not serveur.son_coupe(e, suivi)  # aucune notification
        assert e["silence"] and not e["muet"]
        serveur.enregistrement = e
        try:
            r = get("/api/etat")["enregistrement"]
        finally:
            serveur.enregistrement = None
        assert [a["code"] for a in r["alertes"]] == ["son_jamais"], r
        e2, suivi2 = {**e, "options": {"presentiel": True}}, {"debut": suivi["debut"]}
        serveur.son_coupe(e2, suivi2)
        assert not e2["silence"]
        piste(d / "systeme.wav", 0.1)  # le son de l'ordinateur arrive : bandeau retiré
        serveur.son_coupe(e, suivi)
        assert not e["silence"]


if __name__ == "__main__":
    for nom, f in list(globals().items()):
        if nom.startswith("test_"):
            f()
    print("ok")
