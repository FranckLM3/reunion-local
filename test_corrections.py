"""Corrections de la relecture du 03/11 : un contrôle par correction. pytest test_corrections.py"""

import json
import sys
from unittest import mock

import reglages
import relecture
import serveur
import transcribe
import voix
from test_relecture import reunion
from test_serveur import piste, serveur_local
from test_transcribe import transcrire_simule


def test_1_empreintes_jamais_melangees():
    """Réunion de la méthode libre (192 nombres), banque pyannote (256) déjà remplie : rien ne casse, rien ne se
    mélange, `empreintes` survit à une correction de ligne (scénario t1.py)."""
    with serveur_local() as (t, get, post, port):
        d = reunion(serveur.BASE)
        data = json.loads((d / "transcription.json").read_text(encoding="utf-8"))
        (d / "transcription.json").write_text(json.dumps({**data, "empreintes": "titanet-small"}), encoding="utf-8")
        (d / "locuteurs.json").write_text(json.dumps({"Voix 3": [0.1] * 192, "Voix 4": [0.2] * 192}), encoding="utf-8")
        voix.ecrire_banque({"Alice": {"emb": [0.3] * 256, "n": 3}})
        assert post("/api/ligne", {"reunion": d.name, "index": 0, "texte": "corrigé"}) == {"ok": True}
        assert json.loads((d / "transcription.json").read_text(encoding="utf-8"))["empreintes"] == "titanet-small"
        assert post("/api/renommer", {"reunion": d.name, "ancien": "Voix 3", "nouveau": "Alice", "retenir": True}) \
            == {"ok": True, "nom": "Alice"}
        assert len(voix.lire_banque()["Alice"]["emb"]) == 256 and len(voix.lire_banque("titanet-small")["Alice"]["emb"]) == 192
        assert set(json.loads((d / "locuteurs.json").read_text(encoding="utf-8"))) == {"Alice", "Voix 4"}
        # Une empreinte d'un autre modèle sous le même nom est refusée, sans rien écrire à moitié.
        voix.ecrire_banque({"Sacha": {"emb": [0.3] * 256, "n": 1}}, "titanet-small")
        avant = (d / "locuteurs.json").read_text(encoding="utf-8")
        r = post("/api/renommer", {"reunion": d.name, "ancien": "Voix 4", "nouveau": "Sacha", "retenir": True})
        assert r["erreur"]["code"] == "invalide" and (d / "locuteurs.json").read_text(encoding="utf-8") == avant


def test_2_3_defauts_d_une_installation_existante(tmp_path, monkeypatch):
    monkeypatch.setattr(reglages, "FICHIER", tmp_path / "reglages.json")
    monkeypatch.setattr(reglages, "installation_existante", lambda: True)
    r = reglages.lire()
    assert r["langue"] == "fr" and r["accueil_fini"] and r["modele"] == (
        "large-v3-turbo" if sys.platform == "darwin" else "small")
    with mock.patch.object(reglages.locale, "getlocale", return_value=("ja_JP", "UTF-8")):
        assert reglages.langue_systeme() == "fr"  # langue non proposée : français
    with mock.patch.object(reglages.locale, "getlocale", return_value=("en_GB", "UTF-8")):
        assert reglages.langue_systeme() == "en"


def test_4_5_liste_robuste_et_refaire_sauvegarde():
    with serveur_local() as (t, get, post, port):
        d = reunion(serveur.BASE)
        abime = serveur.BASE / "261103-0800-abime"
        abime.mkdir()
        piste(abime / "micro.wav", 0.1)
        (abime / "transcription.json").write_text("[1, 2]", encoding="utf-8")  # JSON valide, forme inattendue
        assert {r["nom"] for r in get("/api/reunions")} == {d.name, abime.name}
        with mock.patch.object(serveur, "lancer_transcription", return_value=None) as lance:
            assert post("/api/transcrire", {"reunion": d.name, "chuchote": False}) == {"ok": True}
        assert (d / "transcription.avant-refaire.json").exists() and (d / "locuteurs.avant-refaire.json").exists()
        assert lance.call_args.args[1]["chuchote"] is False  # une option d'origine se décoche


def test_6_pas_de_separation_du_micro_sans_son_de_l_ordinateur(tmp_path):
    (tmp_path / "micro.wav").write_bytes(b"\0" * 8192)
    locs, separees, rehaussees, _ = transcrire_simule(tmp_path)
    assert separees == [] and rehaussees == [] and locs == {transcribe.PRENOM}  # voix seule, non séparée


def test_8_9_un_seul_direct_et_etat_arret():
    with serveur_local() as (t, get, post, port):
        d = serveur.BASE / "261103-1400-direct"
        d.mkdir()
        e = serveur.enregistrement = {"dossier": d, "debut": 0, "direct": {"actif": False}, "options": {},
                                      "proc": mock.Mock(), "nom_auto": False}
        try:
            with mock.patch.object(serveur, "fichiers_prets", return_value=True), \
                    mock.patch.object(serveur.subprocess, "Popen") as popen:
                popen.return_value.stdout = []
                serveur.activer_direct()
                serveur.activer_direct()
            assert popen.call_count == 1
            vu = {}
            with mock.patch.object(serveur, "stopper"), mock.patch.object(serveur, "couper_direct"), \
                    mock.patch.object(serveur, "consulter_agenda", side_effect=lambda d, *a: (
                        vu.update(arret=serveur.travaux.get(d.name, {}).get("arret")), d)[1]), \
                    mock.patch.object(serveur, "lancer_transcription"):
                assert serveur.arreter()["ok"]
            assert vu["arret"] is True and d.name not in serveur.travaux  # posé tout de suite, retiré ensuite
        finally:
            serveur.enregistrement = None


def test_10_annuler_seulement_juste_apres():
    with serveur_local() as (t, get, post, port):
        d = reunion(serveur.BASE)
        assert post("/api/regrouper", {"reunion": d.name, "de": "Voix 4", "vers": "Dominique"}) == {"ok": True}
        assert post("/api/ligne", {"reunion": d.name, "index": 0, "texte": "autre"}) == {"ok": True}
        assert "erreur" in post("/api/annuler-regroupement", {"reunion": d.name})


def test_12_quitter_arrete_les_calculs():
    p = mock.Mock()
    serveur.enfants.add(p)
    try:
        serveur.garder_enregistrement()
    finally:
        serveur.enfants.discard(p)
    p.terminate.assert_called_once()


def test_13_voix_des_deux_banques():
    with serveur_local() as (t, get, post, port):
        voix.ecrire_banque({"Camille": {"emb": [1.0] * 256, "n": 2}})
        voix.ecrire_banque({"Camille": {"emb": [1.0] * 192, "n": 1}}, "titanet-small")
        assert {(v["nom"], v["modele"]) for v in get("/api/voix")} == {("Camille", voix.PYANNOTE),
                                                                         ("Camille", "titanet-small")}
        assert post("/api/voix/oublier", {"nom": "Camille", "modele": "titanet-small"}) == {"ok": True}
        assert voix.lire_banque() and not voix.lire_banque("titanet-small")


def test_notes_illisibles_mises_de_cote(tmp_path):
    (tmp_path / "notes.json").write_text("{coupé", encoding="utf-8")
    assert relecture.lire_notes(tmp_path) == [] and (tmp_path / "notes.json.bak").exists()
    (tmp_path / "notes.json").write_text(json.dumps([{"id": "a", "t_s": 1, "texte": "ok"}, {"texte": "sans id"}]),
                                         encoding="utf-8")
    assert [n["id"] for n in relecture.lire_notes(tmp_path)] == ["a"]


def test_pyannote_installe_a_la_demande():
    """Décision (b) : pyannote facultatif partout ; la page propose de l'installer quand la clé est rangée."""
    with serveur_local() as (t, get, post, port):
        assert "pyannote_installe" in get("/api/configuration")
        with mock.patch.object(serveur.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            assert post("/api/installer-pyannote", {}) == {"ok": True}
            assert attendre(lambda: not get("/api/etat")["installation_pyannote"]["en_cours"])
        assert "pyannote.audio>=4" in run.call_args.args[0]
        assert get("/api/etat")["installation_pyannote"]["code"] == 0


def attendre(condition, delai=5.0):
    import time

    fin = time.time() + delai
    while time.time() < fin:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_securite_session_cadre_et_autre_site():
    """S1, S2 : jeton de session exigé, échangé contre un cookie HttpOnly ; aucune page dans un cadre ; requête
    d'un autre site refusée ; corps borné ; seul un nom de réunion désigne un dossier."""
    import http.client

    with serveur_local() as (t, get, post, port):
        serveur.JETON = "jeton-essai"
        try:
            def requete(methode, chemin, corps=None, **entetes):
                c = http.client.HTTPConnection("127.0.0.1", port)
                c.request(methode, chemin, body=corps, headers={"Host": f"127.0.0.1:{port}", **entetes})
                r = c.getresponse()
                r.read()
                return r

            ok = {"Cookie": "reunion=jeton-essai"}
            assert requete("GET", "/api/etat").status == 401
            r = requete("GET", "/?cle=jeton-essai")
            assert r.status == 303 and "HttpOnly" in r.getheader("Set-Cookie") and "SameSite=Strict" in r.getheader(
                "Set-Cookie")
            assert requete("GET", "/api/etat", **ok).status == 200
            assert requete("GET", "/ecoute/261103-1000-x.m4a", Cookie="reunion=faux").status == 401
            r = requete("GET", "/", **ok)
            assert r.getheader("X-Frame-Options") == "DENY"
            assert "frame-ancestors 'none'" in r.getheader("Content-Security-Policy")
            assert requete("GET", "/api/etat", **ok, **{"Sec-Fetch-Site": "cross-site"}).status == 403
            assert requete("POST", "/api/note", corps=b"{}", **ok, **{
                "Content-Type": "application/json", "Content-Length": "2000000"}).status == 413
        finally:
            serveur.JETON = None
    assert serveur.dossier_de("Reunions") is None and serveur.nombre(31) is None and serveur.nombre(True) is None
