"""Relecture d'une réunion par le serveur : voix, noms proposés, lignes, notes, recherche, export (spec VX, LI, NT).
Sans réseau ni micro : pytest test_relecture.py"""

import io
import json
import time
import urllib.parse
import urllib.request
import zipfile
from unittest import mock

import direct
import relecture
import serveur
import voix
from test_serveur import piste, serveur_local


def reunion(base, nom="261103-1305-point-devis-atelier-nord", ancien=False):
    """Réunion transcrite de 4 voix : Lou (vous, micro), Dominique, Voix 3 proposée Camille, Voix 4 inconnue."""
    d = base / nom
    d.mkdir()
    piste(d / "micro.wav", 0.1, 60)
    blocs = [{"debut": 0, "fin": 4, "loc": "Lou", "texte": "Bonjour, on commence par le devis."},
             {"debut": 5, "fin": 12, "loc": "Dominique", "texte": "On a reçu trois offres pour la charpente."},
             {"debut": 13, "fin": 20, "loc": "Voix 3", "texte": "Je préfère le bois local."},
             {"debut": 21, "fin": 25, "loc": "Voix 4", "texte": "Pour le délai, fin janvier."},
             {"debut": 26, "fin": 30, "loc": "Lou", "texte": "Je vous envoie le devis signé vendredi."}]
    if ancien:  # format d'avant le 03/11 : liste de blocs, « Intervenant N »
        data = [{**b, "loc": b["loc"].replace("Voix", "Intervenant")} for b in blocs]
    else:
        data = {"source": "x", "blocs": blocs, "separation": True, "propositions": {
            "Voix 3": {"nom": "Camille", "confiance": 0.83}},
            "voix": {"Lou": {"numero": 1, "source": "micro"}, "Dominique": {"numero": 2, "source": "systeme"},
                     "Voix 3": {"numero": 3, "source": "systeme"}, "Voix 4": {"numero": 4, "source": "systeme"}}}
    (d / "transcription.json").write_text(json.dumps(data), encoding="utf-8")
    (d / "locuteurs.json").write_text(json.dumps({"Voix 3": [1.0, 0.0], "Voix 4": [0.0, 1.0]}), encoding="utf-8")
    (d / "participants.json").write_text(json.dumps(
        {"titre": "Point devis", "noms": ["Alex", "Camille", "Dominique", "Lou", "Sacha"], "saisis": False}),
        encoding="utf-8")
    return d


def avec_moi(nom="Lou"):
    return mock.patch.object(serveur, "moi", return_value=nom)


def test_lecture_et_suggestions():
    with serveur_local() as (t, get, post, port), avec_moi():
        d = reunion(serveur.BASE)
        autre = serveur.BASE / "261027-0930-atelier"
        autre.mkdir()
        piste(autre / "micro.wav", 0.1)
        (autre / "transcription.json").write_text(json.dumps(
            {"source": "x", "blocs": [{"debut": 0, "fin": 1, "loc": "Morgan", "texte": "oui"}]}), encoding="utf-8")
        r = get(f"/api/reunion/{d.name}")
        assert r["titre"] == "point devis atelier nord" and r["debut"] == "2026-11-03T13:05"
        assert [v["loc"] for v in r["voix"]] == ["Lou", "Dominique", "Voix 3", "Voix 4"]
        v3, v4 = r["voix"][2], r["voix"][3]
        assert v3["proposition"] == {"nom": "Camille", "confiance": 0.83, "niveau": "très probable"}
        assert r["voix"][1]["temps_s"] == 7 and v3["extrait"] == {"debut": 13, "fin": 18}  # VX-15 : 5 s
        # VX-20 : participants sans voix, puis noms portés ou proposés ailleurs, puis réunions passées.
        assert [(s["nom"], s["source"], s["deja"]) for s in v4["suggestions"]] == [
            ("Alex", "participant", None), ("Sacha", "participant", None), ("Camille", "participant", "Voix 3"),
            ("Dominique", "participant", "Voix 2"), ("Morgan", "passe", None)]
        assert v4["suggestions"][2]["proposee"] and not v4["suggestions"][3]["proposee"]
        # VX-23 : le propriétaire, premier pour une voix du micro, jamais pour une autre.
        assert all(s["nom"] != "Lou" for s in v4["suggestions"])
        micro = [{"loc": "Voix 1", "numero": 1, "source": "micro", "proposition": None}]
        # VX-24 : sans participant connu, voix connues puis réunions passées.
        assert [(s["nom"], s["source"]) for s in relecture.suggestions(micro, [], "Lou", ["Camille"],
                                                                       ["Morgan", "Voix 2"])["Voix 1"]] == [
            ("Lou", "vous"), ("Camille", "connue"), ("Morgan", "passe")]
        # VX-19 : la voix connue reconnue en tête des suggestions de sa voix.
        assert v3["suggestions"][0] == {"nom": "Camille", "source": "connue", "deja": None, "proposee": False,
                                        "confiance": 0.83}


def test_ancien_format_lisible():
    """Les transcriptions déjà faites s'ouvrent : liste de blocs, « Intervenant N » affiché « Voix N »."""
    with serveur_local() as (t, get, post, port):
        d = reunion(serveur.BASE, ancien=True)
        r = get(f"/api/reunion/{d.name}")
        assert [v["loc"] for v in r["voix"]] == ["Voix 3", "Voix 4", "Lou", "Dominique"]  # « Intervenant N » garde N
        assert [v["numero"] for v in r["voix"]] == [3, 4, 5, 6] and r["transcription"]["propositions"] == {}
        assert get("/api/reunions")[0]["locuteurs"] == ["Dominique", "Lou", "Voix 3", "Voix 4"]
        # Un renommage réécrit au format d'aujourd'hui ; l'empreinte « Intervenant 3 » est retrouvée.
        (d / "locuteurs.json").write_text(json.dumps({"Intervenant 3": [1.0, 0.0]}), encoding="utf-8")
        assert post("/api/renommer", {"reunion": d.name, "ancien": "Voix 3", "nouveau": "Camille"})["ok"]
        assert json.loads((d / "locuteurs.json").read_text(encoding="utf-8")) == {"Camille": [1.0, 0.0]}
        assert "Camille" in json.loads((d / "transcription.json").read_text(encoding="utf-8"))["voix"]
        ancien = relecture.normaliser({"blocs": [{"debut": 0, "fin": 1, "loc": "Autres", "texte": "a"}]})
        assert ancien["separation"] is False and ancien["blocs"][0]["loc"] == "Les autres"


def test_accepter_refuser_retenir():
    with serveur_local() as (t, get, post, port), avec_moi():
        d = reunion(serveur.BASE)
        voix.ecrire_banque({"Camille": {"emb": [1.0, 0.0], "n": 6}})
        # VX-04 : Oui, la proposition devient le nom et la voix connue s'enrichit.
        assert post("/api/voix/accepter", {"reunion": d.name, "voix": "Voix 3"}) == {"ok": True, "nom": "Camille"}
        assert voix.lire_banque()["Camille"]["n"] == 7
        r = get(f"/api/reunion/{d.name}")
        assert "Voix 3" not in [v["loc"] for v in r["voix"]] and r["transcription"]["propositions"] == {}
        assert [v["numero"] for v in r["voix"] if v["loc"] == "Camille"] == [3]  # numéro gardé
        # VX-05 : Non, la carte redevient Voix N, la voix connue n'est pas modifiée.
        d2 = reunion(serveur.BASE, "261103-1400-autre")
        assert post("/api/voix/refuser", {"reunion": d2.name, "voix": "Voix 3"}) == {"ok": True}
        assert get(f"/api/reunion/{d2.name}")["voix"][2]["proposition"] is None
        assert voix.lire_banque()["Camille"]["n"] == 7
        # Q1 / VX-08 : un tiers nommé sans la case n'est pas retenu ; avec la case, il l'est.
        assert post("/api/renommer", {"reunion": d2.name, "ancien": "Voix 4", "nouveau": "Sacha"})["ok"]
        assert "Sacha" not in voix.lire_banque()
        d3 = reunion(serveur.BASE, "261103-1500-trois")
        assert post("/api/renommer", {"reunion": d3.name, "ancien": "Voix 4", "nouveau": "Sacha", "retenir": True})["ok"]
        assert "Sacha" in voix.lire_banque()
        # VX-09 : ta propre voix est retenue sans case.
        assert post("/api/renommer", {"reunion": d3.name, "ancien": "Voix 3", "nouveau": "Lou"})["ok"]
        assert "Lou" in voix.lire_banque()
        assert post("/api/voix/accepter", {"reunion": d3.name, "voix": "Voix 9"})["erreur"]["code"] == "voix_inconnue"


def test_regrouper_annuler_reattribuer_corriger():
    with serveur_local() as (t, get, post, port), avec_moi():
        d = reunion(serveur.BASE)
        avant = json.loads((d / "transcription.json").read_text(encoding="utf-8"))["blocs"]
        # VX-11, VX-12 : regrouper puis annuler, tout revient comme avant.
        assert post("/api/regrouper", {"reunion": d.name, "de": "Voix 4", "vers": "Dominique"}) == {"ok": True}
        assert [v["loc"] for v in get(f"/api/reunion/{d.name}")["voix"]] == ["Lou", "Dominique", "Voix 3"]
        assert post("/api/annuler-regroupement", {"reunion": d.name}) == {"ok": True}
        assert json.loads((d / "transcription.json").read_text(encoding="utf-8"))["blocs"] == avant
        assert "erreur" in post("/api/annuler-regroupement", {"reunion": d.name})
        # VX-13 : seule la ligne choisie change de voix.
        assert post("/api/attribuer-ligne", {"reunion": d.name, "index": 3, "loc": "Dominique"}) == {"ok": True}
        locs = [b["loc"] for b in get(f"/api/reunion/{d.name}")["transcription"]["blocs"]]
        assert locs == ["Lou", "Dominique", "Voix 3", "Dominique", "Lou"]
        # « Autre personne... » : une voix neuve, numérotée après les autres (Voix 4 n'a plus de ligne).
        assert post("/api/attribuer-ligne", {"reunion": d.name, "index": 2, "loc": "Alex"}) == {"ok": True}
        v = get(f"/api/reunion/{d.name}")["voix"]
        assert [(x["loc"], x["numero"]) for x in v] == [("Lou", 1), ("Dominique", 2), ("Alex", 4)]
        # LI-12 : correction du texte d'une ligne.
        assert post("/api/ligne", {"reunion": d.name, "index": 0, "texte": " Bonjour à tous. "}) == {"ok": True}
        assert get(f"/api/reunion/{d.name}")["transcription"]["blocs"][0]["texte"] == "Bonjour à tous."
        for mauvais in ({"index": 99, "texte": "x"}, {"index": True, "texte": "x"}, {"index": 0, "texte": "  "}):
            assert post("/api/ligne", {"reunion": d.name, **mauvais})["erreur"]["code"] == "invalide"
        # Réunion en cours de transcription : intouchable, message sur place (ER-16).
        serveur.travaux[d.name] = {"en_cours": True, "code": None}
        r = post("/api/ligne", {"reunion": d.name, "index": 0, "texte": "x"})
        assert r["erreur"]["code"] == "occupee_transcription" and r["erreur"]["journal"].endswith("journal.txt")
        serveur.travaux.clear()
        # VX-26 : participants modifiables, les suggestions suivent sans refaire la transcription.
        assert post("/api/participants", {"reunion": d.name, "noms": ["Lou", "Zoé"]}) == {"ok": True}
        p = get(f"/api/reunion/{d.name}")["participants"]
        assert p["noms"] == ["Lou", "Zoé"] and p["titre"] == "Point devis" and p["saisis"]


def test_notes():
    with serveur_local() as (t, get, post, port):
        d = reunion(serveur.BASE)  # 60 s de son
        r = post("/api/note", {"reunion": d.name, "action": "ajouter", "t_s": 24.4, "texte": " à rappeler pour le devis "})
        n = r["note"]
        assert n["t_s"] == 24.4 and n["texte"] == "à rappeler pour le devis"
        for mauvais in ({"t_s": 5, "texte": "   "}, {"t_s": 61, "texte": "x"}, {"t_s": -1, "texte": "x"},
                        {"t_s": True, "texte": "x"}):
            e = post("/api/note", {"reunion": d.name, "action": "ajouter", **mauvais})["erreur"]
            assert e["code"] in ("note_vide", "heure_hors_reunion")
        # NT-12 : heure hors de la réunion refusée, avec la durée.
        e = post("/api/note", {"reunion": d.name, "action": "deplacer", "id": n["id"], "t_s": 75})["erreur"]
        assert e["cause"] == "L'heure doit être entre 00:00 et 01:00."
        assert post("/api/note", {"reunion": d.name, "action": "deplacer", "id": n["id"], "t_s": 29.4})["ok"]
        assert post("/api/note", {"reunion": d.name, "action": "modifier", "id": n["id"], "texte": "devis signé"})["ok"]
        notes = get(f"/api/reunion/{d.name}")["notes"]
        assert notes == [{"id": n["id"], "t_s": 29.4, "texte": "devis signé"}]
        # NT-11 : supprimer puis « Annuler » remet la même note.
        assert post("/api/note", {"reunion": d.name, "action": "supprimer", "id": n["id"]})["ok"]
        assert get(f"/api/reunion/{d.name}")["notes"] == []
        assert post("/api/note", {"reunion": d.name, "action": "ajouter", "id": n["id"], "t_s": 29.4,
                                  "texte": "devis signé"})["note"] == notes[0]
        # Pendant l'enregistrement, la durée est celle écoulée.
        en_cours = serveur.BASE / "261103-1600-en-cours"
        en_cours.mkdir()
        serveur.enregistrement = {"dossier": en_cours, "debut": time.time() - 100}
        try:
            assert post("/api/note", {"reunion": en_cours.name, "action": "ajouter", "t_s": 90, "texte": "a"})["ok"]
            assert "erreur" in post("/api/note", {"reunion": en_cours.name, "action": "ajouter", "t_s": 130,
                                                  "texte": "b"})
        finally:
            serveur.enregistrement = None
        assert json.loads((en_cours / "notes.json").read_text(encoding="utf-8"))[0]["t_s"] == 90


def test_recherche_et_export():
    with serveur_local() as (t, get, post, port), avec_moi():
        d = reunion(serveur.BASE)
        relecture.modifier_notes(d, {"action": "ajouter", "t_s": 24.5, "texte": "à rappeler pour le devis"}, None)
        r = get("/api/recherche?q=" + urllib.parse.quote("devis"))
        assert [(x["nom"], x["passages"], x["notes"]) for x in r] == [(d.name, 3, 1)]
        # LI-09 : entre guillemets, seule l'expression exacte.
        assert get("/api/recherche?q=" + urllib.parse.quote('"devis signé"'))[0]["passages"] == 1
        assert get("/api/recherche?q=" + urllib.parse.quote('"signé devis"')) == []
        assert get("/api/recherche?q=") == []

        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/export", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"reunion": d.name, "format": "md"}).encode())
        with urllib.request.urlopen(req) as rep:
            texte = rep.read().decode("utf-8")
            assert "attachment" in rep.headers["Content-Disposition"]
        lignes = texte.splitlines()
        # NT-15, VX-06 : chaque note à sa place, propositions marquées « ? ».
        assert "[00:13] Camille ? : Je préfère le bois local." in lignes
        assert lignes.index("[00:24] Note : à rappeler pour le devis") == lignes.index(
            "[00:21] Voix 4 : Pour le délai, fin janvier.") + 2
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/export", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"reunion": d.name, "format": "docx"}).encode())
        with urllib.request.urlopen(req) as rep, zipfile.ZipFile(io.BytesIO(rep.read())) as z:
            document = z.read("word/document.xml").decode("utf-8")
        assert "Note : à rappeler pour le devis" in document and "point devis atelier nord" in document
        assert post("/api/export", {"reunion": d.name, "format": "pdf"})["erreur"]["code"] == "invalide"


def test_journal_et_erreurs_nommees():
    with serveur_local() as (t, get, post, port):
        d = reunion(serveur.BASE)
        (d / "journal.txt").write_text("\n".join(f"ligne {i}" for i in range(300)), encoding="utf-8")
        j = get(f"/api/journal?reunion={d.name}")
        assert j["chemin"] == str(d / "journal.txt") and j["texte"].splitlines() == [f"ligne {i}" for i in range(100, 300)]
        # Ancienne réunion : le journal de l'enregistreur s'appelait meetrec.log.
        vieille = reunion(serveur.BASE, "261013-1806-point")
        (vieille / "meetrec.log").write_text("x", encoding="utf-8")
        assert get(f"/api/journal?reunion={vieille.name}")["chemin"].endswith("meetrec.log")
        # ER-10, ER-11 : échec de transcription avec sa cause en mots simples ; ancien echec.txt en texte libre.
        (d / "echec.txt").write_text(json.dumps({"code": "memoire"}), encoding="utf-8")
        e = get(f"/api/reunion/{d.name}")["echec"]
        assert e == {"code": "transcription", "garde": "Le son est gardé.", "journal": str(d / "journal.txt"),
                     "cause": "La transcription a échoué : l'ordinateur a manqué de mémoire (essayez la qualité Rapide)."}
        (d / "echec.txt").write_text("Échec (Traceback: KeyError 'x')", encoding="utf-8")
        assert "une erreur inattendue" in get(f"/api/reunion/{d.name}")["echec"]["cause"]
        assert get("/api/reunions")[0]["etat"] in ("echec", "transcrite")
        # ER-13 : aucun libellé d'erreur ne cite le terminal, l'installeur ou un chemin à la mode Mac.
        for cause, garde in serveur.CAUSES.values():
            for interdit in ("install.sh", "terminal", "/Users/", "~/", "jeton", "pyannote", "modèle", "whisper"):
                assert interdit not in cause + garde, (interdit, cause)


def test_direct_brouillon():
    assert direct.prochain(0, 5) is None and direct.prochain(0, 8) == (0, False)
    assert direct.prochain(16, 30) == (16, False)
    assert direct.prochain(10, 200) == (192, True)  # DI-05 : plus de 3 min de retard, reprise au présent
    dr = {"morceaux": [], "retard_s": 0}
    for ligne in ('{"pos": 184.0, "dispo": 224.0}', '{"t_s": 192.4, "source": "moi", "texte": "fin janvier"}',
                  '{"t_s": 1, "source": "ailleurs", "texte": "x"}', "pas du json", '{"saut": true}'):
        serveur.lire_direct(dr, ligne)
    assert dr["retard_s"] == 40 and dr["morceaux"] == [{"t_s": 192.4, "source": "moi", "texte": "fin janvier"}]
    assert dr["saut"] > 0
    # DI-07, DI-08 : actif par défaut seulement sur une machine puissante ; le réglage l'emporte.
    with mock.patch.object(serveur.transcribe, "machine", return_value={"carte_graphique": None, "memoire_go": 16}):
        assert not serveur.direct_par_defaut({"direct": None}) and serveur.direct_par_defaut({"direct": True})
    with mock.patch.object(serveur.transcribe, "machine", return_value={"carte_graphique": "apple", "memoire_go": 8}):
        assert serveur.direct_par_defaut({"direct": None}) and not serveur.direct_par_defaut({"direct": False})
