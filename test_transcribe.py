"""Vérifications rapides de la logique pure et du choix du moteur (simulé : ni réseau, ni GPU) :
python3 test_transcribe.py"""

import contextlib
import errno
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import reglages
import transcribe
from transcribe import audible, fusionner, hms, retirer_echo


def seg(debut, fin, texte, loc="X"):
    return {"debut": debut, "fin": fin, "texte": texte, "loc": loc}


def test_echo():
    systeme = [seg(10, 12, "on valide le budget", "Intervenant 1"),
               seg(12, 14, "pour la phase deux", "Intervenant 2")]
    micro = [
        seg(10.5, 14, "on valide le budget pour la phase deux"),  # écho réparti sur deux locuteurs
        seg(11, 13, "d'accord mais pas avant la fin du mois"),    # vraie réponse, mots communs épars
        seg(40, 42, "on valide le budget"),                       # même phrase, loin dans le temps
    ]
    garde = [m["texte"] for m in retirer_echo(micro, systeme)]
    assert garde == ["d'accord mais pas avant la fin du mois", "on valide le budget"], garde


def test_fusion_et_hms():
    blocs = fusionner([seg(5, 6, "b", "A"), seg(0, 2, "a", "A"), seg(3, 4, "c", "B"), seg(20, 21, "d", "B")])
    assert [(b["loc"], b["texte"]) for b in blocs] == [("A", "a"), ("B", "c"), ("A", "b"), ("B", "d")]
    assert fusionner([seg(0, 1, "a", "A"), seg(2, 3, "b", "A")])[0]["texte"] == "a b"
    assert hms(3725.9) == "01:02:05" and hms(-1) == "00:00:00"


def test_audible():
    env = [0.0] * 10 + [0.05] * 10 + [1e-5] * 10    # silence, voix (-26 dB), souffle (-100 dB)
    assert not audible(env, 0.0, 0.9)
    assert audible(env, 0.5, 1.2)                    # déborde sur la voix
    assert not audible(env, 2.1, 2.9)
    assert not audible(env, 5.0, 6.0)                # au-delà de la fin du fichier


def test_choix_du_moteur():
    r = dict(reglages.DEFAUTS)
    vrai_plateforme, vrai_machine = sys.platform, transcribe.platform.machine
    try:
        sys.platform, transcribe.platform.machine = "win32", lambda: "AMD64"
        assert transcribe.moteur_auto() == "faster"
        assert transcribe.choix_modele(r) == ("faster", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "fr")
        # Forcé dans les réglages, langue en détection automatique.
        assert transcribe.choix_modele({**r, "moteur": "mlx", "modele": "small", "langue": "auto"}) == \
            ("mlx", "mlx-community/whisper-small-mlx", None)
        assert transcribe.choix_modele({**r, "modele": "inconnu"})[1].endswith("large-v3-turbo")
    finally:
        sys.platform, transcribe.platform.machine = vrai_plateforme, vrai_machine
    # Mac Apple Silicon avec mlx-whisper : le chemin d'origine est gardé.
    if sys.platform == "darwin" and transcribe.platform.machine() == "arm64" and \
            transcribe.importlib.util.find_spec("mlx_whisper"):
        assert transcribe.choix_modele(r)[:2] == ("mlx", transcribe.MODELE)


def test_faster_whisper_simule_et_repli_processeur():
    appels = []
    son = [0.0] * 16000  # 1 s ; lu en entier, jamais coupé hors reprise

    class Modele:
        def __init__(self, repo, device, compute_type, **_):
            self.device = device
            appels.append((repo, device, compute_type))

        def transcribe(self, audio, **o):
            assert audio is son  # jamais un chemin : faster-whisper le lirait avec PyAV, incompatible depuis la 19
            assert o["language"] == "fr" and o["word_timestamps"] and o["log_prob_threshold"] == -1.0
            if self.device == "cuda":  # l'erreur CUDA ne sort qu'à la lecture du premier segment
                def casse():
                    raise RuntimeError("Library cublas64_12.dll is not found")
                    yield
                return casse(), None
            mot = types.SimpleNamespace(start=0.5, end=0.9, word=" Bonjour")
            return iter([types.SimpleNamespace(text=" Bonjour", avg_logprob=-0.2, compression_ratio=1.1,
                                               end=0.9, words=[mot])]), None

    faux = {"faster_whisper": types.SimpleNamespace(WhisperModel=Modele),
            "ctranslate2": types.SimpleNamespace(get_cuda_device_count=lambda: 1),
            # Aucun téléchargement réel : la révision figée se résout en un dossier fictif.
            "huggingface_hub": types.SimpleNamespace(snapshot_download=lambda repo, revision=None: repo)}
    avant = {k: sys.modules.get(k) for k in faux}
    sys.modules.update(faux)
    transcribe.modele_faster.cache_clear()
    try:
        with mock.patch.object(transcribe.separation, "lire_16k", lambda wav: son):
            segs = transcribe.segments_faster(Path("x.wav"), "", "Systran/faster-whisper-small", "fr")
    finally:
        for k, v in avant.items():
            if v is None:
                sys.modules.pop(k)
            else:
                sys.modules[k] = v
        transcribe.modele_faster.cache_clear()
    assert [(a[1], a[2]) for a in appels] == [("cuda", "float16"), ("cpu", "int8")]
    assert segs == [{"text": " Bonjour", "avg_logprob": -0.2, "compression_ratio": 1.1, "end": 0.9,
                     "words": [{"start": 0.5, "end": 0.9, "word": " Bonjour"}]}]


def test_en_cache_exige_les_poids():
    """Téléchargement interrompu : config.json est là, le modèle non. Il ne doit pas passer pour téléchargé."""
    with tempfile.TemporaryDirectory() as h, mock.patch.dict(os.environ, {"HF_HOME": h}):
        r = "Systran/faster-whisper-small"
        depot = transcribe.depot_cache(r)
        snap = depot / "snapshots" / "abc"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}", encoding="utf-8")
        (depot / "blobs").mkdir()
        (depot / "blobs" / "x.incomplete").write_bytes(b"0" * 10)
        assert not transcribe.en_cache(r)
        assert transcribe.taille_cache(r) == 10
        (snap / "model.bin").write_bytes(b"0")
        assert not transcribe.en_cache(r)  # un fichier encore en cours
        (depot / "blobs" / "x.incomplete").unlink()
        assert transcribe.en_cache(r)

        mlx = transcribe.depot_cache("mlx-community/whisper-small-mlx") / "snapshots" / "abc"
        mlx.mkdir(parents=True)
        (mlx / "config.json").write_text("{}", encoding="utf-8")
        assert not transcribe.en_cache("mlx-community/whisper-small-mlx")
        (mlx / "weights.safetensors").write_bytes(b"0")
        assert transcribe.en_cache("mlx-community/whisper-small-mlx")

        diar = transcribe.depot_cache(transcribe.MODELE_DIAR) / "snapshots" / "abc"
        (diar / "segmentation").mkdir(parents=True)
        (diar / "config.yaml").write_text("", encoding="utf-8")
        (diar / "segmentation" / "pytorch_model.bin").write_bytes(b"0")
        assert not transcribe.en_cache(transcribe.MODELE_DIAR)  # embedding manquant
        (diar / "embedding").mkdir()
        (diar / "embedding" / "pytorch_model.bin").write_bytes(b"0")
        assert transcribe.en_cache(transcribe.MODELE_DIAR)


def test_taille_cache_fichier_disparu():
    with tempfile.TemporaryDirectory() as h, mock.patch.dict(os.environ, {"HF_HOME": h}):
        blobs = transcribe.depot_cache("a/b") / "blobs"
        blobs.mkdir(parents=True)
        (blobs / "x.incomplete").write_bytes(b"0" * 5)
        (blobs / "y").write_bytes(b"0" * 7)
        vrai = Path.stat

        def stat(p, *a, **k):  # x.incomplete renommé entre le parcours et la lecture de sa taille
            if p.name == "x.incomplete":
                raise FileNotFoundError(p)
            return vrai(p, *a, **k)

        with mock.patch.object(Path, "stat", stat):
            assert transcribe.taille_cache("a/b") == 7


def test_ecriture_atomique_reessaie():
    """Sous Windows, os.replace refuse tant que le serveur lit la cible : réessayé, puis abandonné proprement."""
    with tempfile.TemporaryDirectory() as t:
        cible = Path(t) / "transcription.json"
        vrai, echecs = os.replace, []

        def replace(a, b):
            if len(echecs) < 2:
                echecs.append(a)
                raise PermissionError(13, "Accès refusé")
            vrai(a, b)

        with mock.patch.object(os, "replace", replace), mock.patch("time.sleep"):
            reglages.ecrire_atomique(cible, "Ïsmaël")
        assert cible.read_text(encoding="utf-8") == "Ïsmaël" and len(echecs) == 2
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "Accès refusé")), \
                mock.patch("time.sleep"):
            try:
                reglages.ecrire_atomique(cible, "autre")
                raise AssertionError("échec silencieux")
            except PermissionError:
                pass
        assert [f.name for f in Path(t).iterdir()] == ["transcription.json"]  # aucun .tmp laissé
        assert cible.read_text(encoding="utf-8") == "Ïsmaël"


def transcrire_simule(dossier: Path, *options, systeme_parle=False) -> tuple[set, list, list, str]:
    """main() sans moteur ni réseau : (locuteurs écrits, pistes séparées, pistes rehaussées, sortie)."""
    separees, rehaussees = [], []

    def transcrire(wav, *_):
        if wav.name.startswith("systeme"):
            return [{"debut": 5, "fin": 6, "texte": "oui", "mots": [{"debut": 5, "fin": 6, "mot": " oui"}]}] \
                if systeme_parle else []
        return [{"debut": 0, "fin": 3, "texte": "x", "mots": [{"debut": 0, "fin": 1, "mot": " on commence"},
                                                               {"debut": 2, "fin": 3, "mot": " d'accord"}]}]

    def diariser(wav, nb, *_):
        separees.append((wav.name, nb) if nb else wav.name)
        return [(0, 1.5, "S0"), (1.5, 4, "S1")], {"S0": [1.0, 0.0], "S1": [0.0, 1.0]}

    def rehausser(wav):
        rehaussees.append(wav.name)
        return wav

    sortie = io.StringIO()
    with mock.patch.object(sys, "argv", ["transcribe.py", str(dossier), *options]), \
            mock.patch.dict(os.environ), \
            mock.patch.object(transcribe.reglages, "lire", return_value={**reglages.DEFAUTS, "moteur": "faster"}), \
            mock.patch("importlib.util.find_spec", return_value=True), \
            mock.patch.multiple(transcribe, en_cache=lambda r: True, reparer=lambda w: None, transcrire=transcrire,
                                diariser=diariser, rehausser=rehausser,
                                enveloppe=lambda w: [0.1] * (1000 if systeme_parle else 100)), \
            mock.patch.object(transcribe.voix, "lire_banque", return_value={"Lou": {"emb": [1.0, 0.0], "n": 1}}), \
            contextlib.redirect_stdout(sortie):
        assert transcribe.main() == 0
    blocs = json.loads((dossier / "transcription.json").read_text(encoding="utf-8"))["blocs"]
    return {b["loc"] for b in blocs}, separees, rehaussees, sortie.getvalue()


def test_son_de_l_ordinateur_vide_micro_separe():
    """Cas du 03/11 : atelier dans la pièce diffusé sur Meet, son de l'ordinateur en zéros, case présentiel
    oubliée. Le micro est séparé et rehaussé ; le propriétaire n'est que proposé, d'après sa voix connue."""
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        (d / "systeme.wav").write_bytes(b"\0" * 8192)
        (d / "micro.wav").write_bytes(b"\0" * 8192)
        locs, separees, rehaussees, sortie = transcrire_simule(d)
        assert locs == {"Voix 1", "Voix 2"} and separees == ["micro.wav"] and rehaussees == ["micro.wav"]
        t_json = json.loads((d / "transcription.json").read_text(encoding="utf-8"))
        # Voix reconnue : proposée avec sa confiance (VX-02), jamais imposée.
        assert t_json["propositions"] == {"Voix 1": {"nom": "Lou", "confiance": 1.0}}
        assert t_json["voix"] == {"Voix 1": {"numero": 1, "source": "micro"}, "Voix 2": {"numero": 2, "source": "micro"}}
        assert t_json["separation"] is True
        assert not (d / "transcription.partielle.json").exists()  # TR-06 : texte partiel retiré à la fin
        assert "Son de l'ordinateur vide : voix séparées sur le micro." in sortie
        # Avancement lu par le serveur : quatre étapes dans l'ordre (TR-02).
        assert [l for l in sortie.splitlines() if l.startswith("@etape")] == [f"@etape {i}" for i in (1, 2, 3, 4)]
        # Sans séparation possible : « Moi » (Q3), jamais un nom de personne.
        assert transcrire_simule(d, "--sans-locuteurs")[0] == {"Moi"}
        # « Combien de personnes ont parlé ? » (TR-12) : toutes sur le micro en salle.
        assert transcrire_simule(d, "--personnes", "3")[1] == [("micro.wav", 3)]

        # Visio normale : la piste micro garde le nom du propriétaire, sans séparation ; les voix sont numérotées
        # dans l'ordre de première prise de parole sur toute la réunion (glossaire).
        locs, separees, rehaussees, sortie = transcrire_simule(d, "--moi", "Lou", systeme_parle=True)
        assert locs == {"Lou", "Voix 2"} and separees == ["systeme.wav"] and not rehaussees
        assert "vide" not in sortie
        # Toi compris : une personne de moins à séparer sur le son de l'ordinateur.
        assert transcrire_simule(d, "--personnes", "4", systeme_parle=True)[1] == [("systeme.wav", 3)]

        # Refaire la transcription garde les noms donnés dans la réunion (TR-10) : la voix qui ressemble à
        # « Sacha » de locuteurs.json reprend son nom, sans passer par une proposition.
        (d / "locuteurs.json").write_text(json.dumps({"Sacha": [0.0, 1.0], "Voix 7": [1.0, 0.0]}), encoding="utf-8")
        assert transcrire_simule(d, "--moi", "Lou", systeme_parle=True)[0] == {"Lou", "Sacha"}


def test_son_vide_et_causes_d_echec():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        (d / "micro.wav").write_bytes(b"\0" * 100)  # en-tête seul : rien d'enregistré
        sortie = io.StringIO()
        with mock.patch.object(sys, "argv", ["transcribe.py", str(d), "--sans-locuteurs"]), \
                mock.patch.multiple(transcribe, en_cache=lambda r: True, reparer=lambda w: None), \
                mock.patch("importlib.util.find_spec", return_value=True), contextlib.redirect_stdout(sortie):
            assert transcribe.main() == 1
        assert "@echec son_vide" in sortie.getvalue()
    assert transcribe.code_echec(MemoryError()) == "memoire"
    assert transcribe.code_echec(type("LibsndfileError", (Exception,), {})()) == "son_illisible"
    absent = type("LocalEntryNotFoundError", (Exception,), {})()
    assert transcribe.code_echec(absent) == "fichiers_incomplets"
    assert transcribe.code_echec(absent, telechargement=True) == "reseau_absent"
    assert transcribe.code_echec(type("SSLError", (Exception,), {})(), telechargement=True) == "reseau_filtre"
    assert transcribe.code_echec(ValueError()) == "inconnue"


def test_machine_et_qualite():
    """Section 3.1 : la première ligne du tableau qui s'applique."""
    cas = [("darwin", "arm64", 16, None, "precise"), ("darwin", "arm64", 4, None, "legere"),
           ("darwin", "x86_64", 16, None, "rapide"), ("win32", "AMD64", 4, 1, "precise"),
           ("win32", "AMD64", 8, 0, "rapide"), ("win32", "AMD64", 6, 0, "legere")]
    for plateforme, arch, memoire, cuda, attendu in cas:
        faux = types.SimpleNamespace(get_cuda_device_count=lambda: cuda)
        transcribe.machine.cache_clear()
        with mock.patch.object(sys, "platform", plateforme), mock.patch.object(transcribe.platform, "machine",
                                                                               return_value=arch), \
                mock.patch.object(transcribe, "memoire_go", return_value=memoire), \
                mock.patch("importlib.util.find_spec", return_value=cuda is not None), \
                mock.patch.dict(sys.modules, {"ctranslate2": faux}):
            assert transcribe.machine()["qualite_conseillee"] == attendu, (plateforme, arch, memoire, cuda)
    transcribe.machine.cache_clear()
    # Vitesse : valeur provisoire, puis moyenne des 5 dernières mesures (section 3.2).
    with mock.patch.object(transcribe, "machine", return_value={"carte_graphique": "apple"}):
        assert transcribe.vitesse_k({"vitesses": {}}, "large-v3-turbo") == (0.5, False)
        assert transcribe.vitesse_k({"vitesses": {"large-v3-turbo": [0.4, 0.6]}}, "large-v3-turbo") == (0.5, True)
        assert transcribe.vitesse_k({}, "small") == (1.0, False) and transcribe.vitesse_k({}, "large-v3")[0] == 1.2


def test_messages_d_erreur():
    assert "Disque plein" in transcribe.expliquer(OSError(errno.ENOSPC, "No space left on device"))
    absent = type("LocalEntryNotFoundError", (Exception,), {})
    assert "téléchargement impossible" in transcribe.expliquer(absent("modèle absent du cache"))
    assert transcribe.expliquer(ValueError("autre")) is None


if __name__ == "__main__":
    for nom, f in list(globals().items()):
        if nom.startswith("test_"):
            f()
    print("ok")


def test_attribuer_garde_les_mots_courts_et_coupes_a_la_meme_personne():
    import transcribe
    mots = [(0.0, 0.4, " Ouais"), (0.4, 0.8, " t'as"), (0.8, 1.2, " accès."), (1.2, 1.4, " C"),
            (1.4, 1.6, "'est"), (1.6, 2.0, " public,"), (2.0, 2.4, " je"), (2.4, 2.8, " les"),
            (2.8, 3.2, " ai"), (3.2, 3.6, " mis"), (3.6, 4.4, " là.")]
    tours = [(0.0, 1.2, "A"), (1.2, 1.4, "B"), (1.4, 1.6, "A"), (1.6, 3.2, "B"), (3.2, 3.6, "A"), (3.6, 4.4, "B")]
    segs = [{"debut": 0.0, "fin": 4.4, "mots": [{"debut": d, "fin": f, "mot": m} for d, f, m in mots]}]
    sortie = transcribe.attribuer(segs, tours, {}, "micro")
    # « 'est » suit « C » sans espace : même mot ; « mis » (un mot, 0,4 s) reste à la phrase en cours.
    assert [o["texte"] for o in sortie] == ["Ouais t'as accès.", "C'est public, je les ai mis là."]
    assert sortie[0]["loc"] != sortie[1]["loc"]
