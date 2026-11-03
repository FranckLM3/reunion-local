"""Ce que la page lit dans /api/etat et ce que le serveur fait seul : avancement et heure de fin, reprise, alertes
pendant l'enregistrement, ce qui manque, essai du son, mise à jour, effacement. Sans réseau ni micro."""

import io
import json
import sys
import time
import urllib.error
from unittest import mock

import reglages
import serveur
import transcribe
import voix
from test_serveur import piste, serveur_local, windows_simule


def test_avancement_et_heure_de_fin(tmp_path):
    """TR-01 à TR-05 : étapes, pourcentage, heure de fin recalculée sur la vitesse observée, lenteur signalée."""
    script = tmp_path / "faux.py"
    script.write_text("""
import sys, time
print("@etape 1"); print("@etape 2"); print("@piste 0.05 0.75")
print("  50%|#####     | 50/100"); print("Transcription de la piste micro...")
time.sleep(0.3)
print("@repli"); print("@etape 3"); print("@avance 0.9"); print("@echec memoire")
sys.exit(1)
""", encoding="utf-8")
    debut = time.time() - 100
    travail = {"debut": debut, "estimation_s": 50, "fraction": 0.0}
    journal = tmp_path / "journal.txt"
    assert serveur.executer(travail, [str(script)], journal) == 1
    assert travail["etape"] == 3 and travail["fraction"] == 0.9 and travail["repli"] and travail["echec"] == "memoire"
    # 90 % en 100 s : fin vers debut + 111 s, plus de 1,5 fois les 50 s annoncées.
    assert abs(travail["fin_estimee"] - (debut + 100 / 0.9)) < 2 and travail["lent"]
    assert "Transcription de la piste micro..." in journal.read_text(encoding="utf-8")
    t = {"debut": time.time(), "estimation_s": 1000}
    serveur.avancer(t, 0.05)
    assert "fin_estimee" not in t  # avant 10 %, l'estimation de départ reste (TR-03)


def test_transcription_lancee_echec_et_vitesse():
    with serveur_local() as (t, get, post, port):
        d = serveur.BASE / "261103-1305-point"
        d.mkdir()
        piste(d / "micro.wav", 0.1, 120)
        sorties = [1, 0]

        def executer(travail, args, journal=None):
            travail["echec"] = "son_illisible" if sorties[0] else None
            travail["code"] = sorties.pop(0)
            return travail["code"]

        r = {**reglages.DEFAUTS, "modele": "small", "accueil_fini": True}
        with mock.patch.object(serveur, "executer", executer), mock.patch.object(serveur, "piste_ecoute"), \
                mock.patch.object(serveur, "fichiers_prets", return_value=True), \
                mock.patch.object(serveur, "alerter") as alerte:
            assert post("/api/transcrire", {"reunion": d.name, "personnes": 3}) == {"ok": True}
            assert attendre(lambda: not serveur.travaux[d.name]["en_cours"])
            assert json.loads((d / "echec.txt").read_text(encoding="utf-8")) == {"code": "son_illisible"}
            assert not (d / "transcription.en-cours").exists()
            assert json.loads((d / "options.json").read_text(encoding="utf-8"))["personnes"] == 3
            # Estimation de départ : k provisoire de la qualité conseillée x 120 s, dite approximative (TR-05).
            tr = get("/api/etat")["travaux"][d.name]
            k = transcribe.vitesse_k(reglages.lire(), transcribe.taille_modele(reglages.lire()))[0]
            assert tr["approx"] and round(tr["fin_initiale"] - tr["debut"]) == round(k * 120)
            assert post("/api/transcrire", {"reunion": d.name, "personnes": "trois"})["erreur"]["code"] == "invalide"
            reglages.ecrire(r)
            assert post("/api/transcrire", {"reunion": d.name}) == {"ok": True}
            assert attendre(lambda: not serveur.travaux[d.name]["en_cours"])
        assert not (d / "echec.txt").exists()
        assert alerte.call_args_list[-1].args[0] == "Votre réunion est prête : point"  # TR-07
        assert len(reglages.lire()["vitesses"]["small"]) == 1  # k mesuré sur une vraie transcription


def attendre(condition, delai=5.0):
    fin = time.time() + delai
    while time.time() < fin:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_reprise_et_attente_de_la_preparation():
    """TR-09 : transcription interrompue reprise au lancement ; TR-13 : enregistrée pendant la préparation."""
    with serveur_local() as (t, get, post, port):
        a, b = serveur.BASE / "261103-0900-a", serveur.BASE / "261103-1000-b"
        for d in (a, b):
            d.mkdir()
            piste(d / "micro.wav", 0.1)
        (a / "transcription.en-cours").write_text("{}", encoding="utf-8")
        lancees = []
        with mock.patch.object(serveur, "lancer_transcription", lambda d, o, reprise=False: lancees.append(
                (d.name, reprise))), mock.patch.object(serveur, "fichiers_prets", return_value=True):
            serveur.reprendre()
        assert lancees == [(a.name, True)]
        # Fichiers de transcription absents : la réunion attend, la préparation démarre.
        with mock.patch.object(serveur, "fichiers_prets", return_value=False), \
                mock.patch.object(serveur, "lancer_preparation") as prepa:
            assert serveur.lancer_transcription(b, {}) is None
            assert prepa.called and (b / "transcription.attente").exists()
            assert {r["nom"]: r["etat"] for r in get("/api/reunions")}[b.name] == "attente"


def test_alertes_pendant_l_enregistrement():
    with serveur_local() as (t, get, post, port):
        d = serveur.BASE / "261103-1305-point"
        d.mkdir()
        piste(d / "micro.wav", 0.0)
        piste(d / "systeme.wav", 0.1)
        e, suivi = {"dossier": d, "muet": False, "fige": False, "options": {}, "debut": time.time()}, {}
        serveur.son_coupe(e, suivi)
        assert e["micro_muet"] is None
        suivi["debut"] -= serveur.SEUIL_MICRO  # micro silencieux depuis 30 s (EN-03)
        serveur.son_coupe(e, suivi)
        assert [a["code"] for a in serveur.alertes(e)] == ["micro_muet"]
        e.update(libre=0.5e9, appareil=time.time(), fige=True, fige_depuis=time.time() - 40)
        a = serveur.alertes(e)
        # EN-12 : la plus grave en haut ; EN-08 : minutes restantes ; EN-06 : changement d'appareil.
        assert [x["code"] for x in a] == ["fige", "micro_muet", "place", "appareil"]
        assert a[0]["niveau"] == "rouge" and a[2]["minutes"] == 21 and a[0]["depuis_s"] == 40
        piste(d / "micro.wav", 0.1)  # le micro entend : l'alerte s'efface seule
        serveur.son_coupe(e, suivi)
        assert e["micro_muet"] is None and e["micro_entendu"]
        (d / "journal.txt").write_text("[10:01:02] Capture relancée.\n", encoding="utf-8")
        assert serveur.relances(d / "journal.txt") == 1
        # ER-09 : enregistreur arrêté, heure et minutes gardées.
        e["arret"] = {"heure": "14:52", "minutes": 22}
        assert serveur.alertes(e)[0] == {"code": "arret", "niveau": "rouge", "depuis_s": 0, "heure": "14:52",
                                         "minutes": 22}


def test_manque_et_accueil():
    with serveur_local() as (t, get, post, port):
        with mock.patch.object(serveur.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(serveur, "MAC", False), mock.patch("importlib.util.find_spec", return_value=True):
            assert serveur.manque() == []
            # PL-08 « Passer cette étape » : l'accueil dit « Il manque une étape » (AC-05) ; AC-07 conseillé.
            assert post("/api/configuration", {"etat_micro": "refuse", "etat_son": "refuse", "accueil_etape": 4}) \
                == {"ok": True}
            assert [(m["code"], m["niveau"]) for m in get("/api/etat")["manque"]] == [
                ("micro", "bloquant"), ("son", "conseille")]
            assert get("/api/etat")["accueil_etape"] == 4
            with mock.patch.object(serveur.shutil, "disk_usage", return_value=mock.Mock(free=1.2e9)):
                assert serveur.manque()[0] == {"code": "place", "niveau": "bloquant", "action": "ouvrir_dossier",
                                               "libre": "1,2 Go"}
        with mock.patch.object(serveur.shutil, "which", return_value=None):
            assert serveur.manque()[0]["code"] == "installation"  # ER-12
        for mauvais in ({"etat_micro": "peut-être"}, {"accueil_etape": 9}, {"accueil_etape": "2"}):
            assert "erreur" in post("/api/configuration", mauvais)
        # AC-08 : agenda, séparation et clé n'apparaissent jamais dans ce qui manque.
        assert not {m["code"] for m in serveur.manque()} & {"agenda", "jeton", "cle", "separation"}


def test_demarrage_refuse_erreurs_nommees():
    with serveur_local() as (t, get, post, port):
        with mock.patch.object(serveur.shutil, "disk_usage", return_value=mock.Mock(free=1.2e9)), \
                mock.patch.object(serveur, "MAC", False):
            r = serveur.demarrer({})
        assert r["erreur"]["code"] == "place" and r["erreur"]["cause"].startswith("Plus assez de place : 1,2 Go libres")
        assert serveur.cause_demarrage("Impossible de démarrer : The user declined TCCs for application\nDans "
                                       "Réglages Système, autorise Micro") == "son_refuse"
        assert serveur.cause_demarrage("Aucun micro détecté : branche un micro") == "aucun_micro"
        assert serveur.cause_demarrage("Impossible de démarrer : microphone access denied") == "micro_refuse"
        assert serveur.cause_demarrage("Impossible de démarrer : autre chose") == "enregistreur"


def test_essai_du_son_simule():
    """PL-07, PL-10, RG-12 : l'enregistreur tourne quelques secondes, la page lit les niveaux, le micro entendu
    est noté (l'accueil ne réclame plus son autorisation)."""
    with serveur_local() as (t, get, post, port), windows_simule(t):
        assert post("/api/test-son", {"secondes": 3}) == {"ok": True}
        assert post("/api/test-son", {"secondes": 3})["erreur"]["code"] == "deja_en_cours"
        assert attendre(lambda: not get("/api/etat")["test_son"]["en_cours"], 20)
        ts = get("/api/etat")["test_son"]
        assert ts["erreur"] is None and ts["micro_entendu"] and abs(ts["niveau_micro"] + 40) < 1, ts
        assert reglages.lire()["etat_micro"] == "ok"
        assert post("/api/test-son", {"secondes": 60})["erreur"]["code"] == "invalide"


def test_mise_a_jour():
    with serveur_local() as (t, get, post, port):
        reponse = io.BytesIO(json.dumps({"tag_name": "v2.1", "html_url": "https://github.com/x"}).encode())
        with mock.patch.object(serveur.urllib.request, "urlopen", return_value=reponse) as appel:
            r = serveur.mise_a_jour()
        assert appel.call_args.args[0].startswith("https://api.github.com/")  # rien d'autre n'est contacté (MD-01)
        assert r == {"ok": True, "version": serveur.VERSION, "derniere": "2.1", "a_jour": False,
                     "page": "https://github.com/x"}
        erreur_404 = urllib.error.HTTPError("u", 404, "absent", {}, None)
        with mock.patch.object(serveur.urllib.request, "urlopen", side_effect=erreur_404):
            assert serveur.mise_a_jour()["a_jour"] is True
        with mock.patch.object(serveur.urllib.request, "urlopen", side_effect=urllib.error.URLError("hors ligne")):
            assert serveur.mise_a_jour()["erreur"]["code"] == "reseau_absent"  # MD-04


def test_effacer_ne_touche_que_nos_fichiers(tmp_path):
    with serveur_local() as (t, get, post, port), mock.patch.dict("os.environ", {"HF_HOME": str(tmp_path / "hf")}):
        reunion = serveur.BASE / "261103-1305-point"
        reunion.mkdir()
        piste(reunion / "micro.wav", 0.1)
        divers = serveur.BASE / "photos"
        divers.mkdir()
        reglages.ecrire(reglages.lire())
        voix.ecrire_banque({"Camille": {"emb": [1.0], "n": 1}})
        blobs = transcribe.depot_cache(transcribe.MODELE_DIAR) / "blobs"
        blobs.mkdir(parents=True)
        (blobs / "x").write_bytes(b"0" * 2_000_000)
        autre = tmp_path / "hf" / "hub" / "models--autre--modele"
        autre.mkdir(parents=True)
        serveur.noter_telechargement(transcribe.MODELE_DIAR)  # téléchargé par Réunion : effaçable
        tailles = get("/api/effacer")
        assert tailles["modeles_mo"] == 2 and tailles["reunions"] == 1
        assert "erreur" in post("/api/effacer", {"reunions": True})  # confirmation exigée
        assert post("/api/effacer", {"confirme": True}) == {"ok": True}
        assert not reglages.FICHIER.exists() and not voix.BANQUE.exists() and not blobs.exists()
        assert autre.exists() and reunion.exists()  # MD-09 : réunions gardées sans la case
        assert post("/api/effacer", {"confirme": True, "reunions": True}) == {"ok": True}
        assert not reunion.exists() and divers.exists()  # jamais un dossier qui n'est pas une réunion


if __name__ == "__main__":
    sys.exit("pytest test_etat.py")
