"""Séparation des voix, interchangeable derrière une seule fonction : diariser(wav, nb, maxi, methode).

Sortie commune : tours [(début s, fin s, libellé)] et empreintes {libellé: [float]} (une par voix).

- « libre » (par défaut) : sherpa-onnx (Apache-2.0), segmentation pyannote 3.0 convertie en ONNX (MIT) et empreintes
  NVIDIA TitaNet-small (CC-BY-4.0). Fichiers publiés dans les versions GitHub de sherpa-onnx : ni compte, ni clé.
- « pyannote » : pyannote/speaker-diarization-community-1, seulement si une clé d'accès Hugging Face est rangée dans
  les réglages avancés (jamais demandée à l'arrivée).

Les empreintes des deux méthodes ne se comparent pas (192 et 256 nombres) : chaque méthode a sa banque de voix
connues (voix.py).
"""

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

import reglages

# ---------------------------------------------------------------- méthode libre (sherpa-onnx)

URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
# (chemin dans les versions publiées, sha256, Mo) : l'empreinte protège d'un fichier remplacé ou tronqué.
# Empreinte du model.onnx extrait de l'archive de segmentation (relevée le 03/11/2026), vérifiée elle aussi.
SHA_SEGMENTATION_ONNX = "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
SEGMENTATION = ("speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
                "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488", 7)
EMPREINTE = ("speaker-recongition-models/nemo_en_titanet_small.onnx",
             "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e", 40)
SR = 16000
SEUIL = float(os.environ.get("REUNION_SEUIL_DIAR", "0.3"))  # sherpa sur-découpe, on fusionne ensuite
DUREE_MIN_EMPREINTE = 1.0  # s : en dessous, l'empreinte d'un tour est trop bruitée
# ponytail: seuils fixes recalés sur 3 extraits de nos réunions ; une voix de moins de DUREE_MIN_VOIX est fondue.
FUSION = float(os.environ.get("REUNION_FUSION_DIAR", "0.7"))  # cosinus au-delà duquel deux groupes = une voix
DUREE_MIN_VOIX = 10.0  # s

# Méthode -> (modèle d'empreintes, Mo à télécharger). Le modèle d'empreintes désigne aussi la banque de voix.
METHODES = {"libre": ("titanet-small", SEGMENTATION[2] + EMPREINTE[2]),
            "pyannote": ("pyannote-community-1", 33)}
MODELE_PYANNOTE = "pyannote/speaker-diarization-community-1"
POIDS_PYANNOTE = ("segmentation/pytorch_model.bin", "embedding/pytorch_model.bin")


def cache_libre() -> Path:
    """Dossier des fichiers de la méthode libre. Sous Windows, sherpa-onnx ne lit pas un chemin non ASCII (nom
    d'utilisateur accentué) : repli sur %PROGRAMDATA%\\reunion."""
    if os.environ.get("REUNION_MODELES_DIAR"):
        return Path(os.environ["REUNION_MODELES_DIAR"])
    d = Path.home() / ".cache" / "reunion" / "diar-libre"
    if sys.platform == "win32" and not str(d).isascii():
        d = chemin_court(d)  # dans le compte, jamais un dossier partagé (S3)
    return d


def chemin_court(d: Path) -> Path:
    """Chemin court 8.3 de Windows (ASCII) du même dossier ; inchangé s'il n'y en a pas."""
    import ctypes

    d.mkdir(parents=True, exist_ok=True)
    tampon = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(d), tampon, len(tampon))
    return Path(tampon.value) if n and tampon.value.isascii() else d


def fichier_libre(rel: str) -> Path:
    nom = rel.rsplit("/", 1)[1]
    if nom.endswith(".tar.bz2"):
        return cache_libre() / nom.removesuffix(".tar.bz2") / "model.onnx"
    return cache_libre() / nom


_verifies: dict[tuple, bool] = {}


def fichier_sain(f: Path, sha: str) -> bool:
    """Fichier présent et d'empreinte attendue (résultat gardé tant que le fichier ne change pas)."""
    try:
        s = f.stat()
    except OSError:
        return False
    cle = (str(f), s.st_size, s.st_mtime_ns)
    if cle not in _verifies:
        h = hashlib.sha256()
        with f.open("rb") as flux:
            for bloc in iter(lambda: flux.read(1 << 20), b""):
                h.update(bloc)
        _verifies[cle] = h.hexdigest() == sha
    return _verifies[cle]


def sha_modele(rel: str, sha: str) -> str:
    return SHA_SEGMENTATION_ONNX if rel.endswith(".tar.bz2") else sha


def a_deposer() -> list[str]:
    """Fichiers à déposer à la main dans cache_libre() si le réseau bloque le téléchargement."""
    return [URL + rel for rel, sha, _ in (SEGMENTATION, EMPREINTE) if not fichier_sain(fichier_libre(rel), sha_modele(rel, sha))]


def telecharger_libre() -> None:
    """Fichiers en cache, téléchargés sans compte et vérifiés ; une archive est extraite à côté. Un fichier déposé
    à la main (archive ou modèle) est accepté s'il a la bonne empreinte."""
    cache = cache_libre()
    cache.mkdir(parents=True, exist_ok=True)
    for rel, sha, _ in (SEGMENTATION, EMPREINTE):
        cible = fichier_libre(rel)
        if fichier_sain(cible, sha_modele(rel, sha)):
            continue
        nom = rel.rsplit("/", 1)[1]
        dest = cache / nom
        if not fichier_sain(dest, sha):  # absent, ou déposé à la main mais altéré : on le reprend
            print(f"Téléchargement de {nom}...", flush=True)
            tmp = dest.with_name(nom + ".part")
            try:
                with urllib.request.urlopen(URL + rel, timeout=60) as r, tmp.open("wb") as f:
                    shutil.copyfileobj(r, f)
                if not fichier_sain(tmp, sha):
                    raise RuntimeError(f"{nom} : empreinte sha256 inattendue")
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
        if nom.endswith(".tar.bz2"):
            with tarfile.open(dest) as t:
                t.extractall(cache, filter="data")
        if not fichier_sain(cible, sha_modele(rel, sha)):
            raise RuntimeError(f"{cible.name} : empreinte sha256 inattendue")


def lire_16k(wav: Path):
    """Mono 16 kHz float32 via ffmpeg (déjà requis par l'outil) : rééchantillonne et mélange les canaux."""
    import numpy as np

    brut = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-ar", str(SR),
                           "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(brut, dtype=np.float32)


def _sherpa(seg: Path, emb: Path, fils: int):
    import sherpa_onnx as so

    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)), num_threads=fils),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=fils),
        clustering=so.FastClusteringConfig(num_clusters=-1, threshold=SEUIL),
        min_duration_on=0.3, min_duration_off=0.5)
    if not cfg.validate():
        raise RuntimeError("configuration sherpa-onnx invalide")
    return so.OfflineSpeakerDiarization(cfg)


def vecteurs(audio, tours, emb: Path, fils: int) -> list:
    """Empreinte normée de chaque tour (None sous DUREE_MIN_EMPREINTE : trop bruitée)."""
    import numpy as np
    import sherpa_onnx as so

    ext = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=fils))
    sortie = []
    for d, f, _ in tours:
        if f - d < DUREE_MIN_EMPREINTE:
            sortie.append(None)
            continue
        st = ext.create_stream()
        st.accept_waveform(SR, audio[int(d * SR):int(f * SR)])
        st.input_finished()
        e = np.asarray(ext.compute(st))
        sortie.append(e / (np.linalg.norm(e) or 1))
    return sortie


def regrouper(tours, vecs, cible: int | None, fusion: float = FUSION, duree_min: float = DUREE_MIN_VOIX):
    """Fusionne les groupes de sherpa (son regroupement natif se trompe sur nos réunions), dans cet ordre : deux
    voix qui se ressemblent au-delà de `fusion` n'en font qu'une ; une voix de moins de `duree_min` s rejoint la
    plus proche ; enfin les deux plus proches fusionnent tant qu'il y en a plus que `cible`.
    Rend (tours renumérotés, centroïdes normés)."""
    import numpy as np

    groupe = {l: l for *_, l in tours}
    duree, somme = {}, {}
    for (d, f, l), v in zip(tours, vecs):
        duree[l] = duree.get(l, 0) + f - d
        if v is not None:
            somme[l] = somme.get(l, 0) + (f - d) * v
    for l in set(duree) - set(somme):  # que des tours trop courts pour une empreinte : traités à la fin
        duree.pop(l)

    def cos(a, b):
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

    def proches(parmi):
        return max((cos(somme[x], somme[y]), x, y) for x in parmi for y in somme if x != y and x in somme)

    while len(somme) > 1:
        sim, a, b = proches(list(somme))
        petits = [l for l in duree if duree[l] < duree_min]
        if sim > fusion:
            pass  # les deux voix les plus proches n'en font qu'une
        elif petits:  # une voix trop courte rejoint la plus proche
            _, a, b = proches([min(petits, key=duree.get)])
        elif not (cible and len(duree) > cible):
            break
        if duree[a] > duree[b]:
            a, b = b, a
        duree[b] += duree.pop(a)  # a rejoint b
        somme[b] = somme[b] + somme.pop(a)
        groupe = {k: (b if g == a else g) for k, g in groupe.items()}
    ordre = {g: i for i, g in enumerate(sorted(duree, key=duree.get, reverse=True))}
    finals = [ordre.get(groupe[l]) for *_, l in tours]
    # Un tour sans voix reconnue (groupe fait de bribes de moins de 1 s) prend la voix du tour voisin le plus proche.
    connus = [i for i, g in enumerate(finals) if g is not None]
    for i, g in enumerate(finals):
        if g is None and connus:
            finals[i] = finals[min(connus, key=lambda j: abs(j - i))]
    return ([(d, f, f"SPEAKER_{g or 0:02d}") for (d, f, _), g in zip(tours, finals)],
            {f"SPEAKER_{ordre[g]:02d}": [float(x) for x in v / np.linalg.norm(v)] for g, v in somme.items()})


def diariser_libre(wav: Path, nb: int | None, maxi: int | None) -> tuple[list, dict[str, list]]:
    seg, emb = fichier_libre(SEGMENTATION[0]), fichier_libre(EMPREINTE[0])
    if not prete("libre"):  # absents ou altérés : repris et vérifiés
        telecharger_libre()
    audio = lire_16k(Path(wav))
    fils = os.cpu_count() or 1
    res = _sherpa(seg, emb, fils).process(audio).sort_by_start_time()
    tours = [(s.start, s.end, s.speaker) for s in res]
    return regrouper(tours, vecteurs(audio, tours, emb, fils), nb or maxi)


# ---------------------------------------------------------------- méthode pyannote (avec clé)

FICHIER_JETON = reglages.DOSSIER_CONF / "hf_token"


def jeton_hf() -> str | None:
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    if FICHIER_JETON.exists():
        return FICHIER_JETON.read_text(encoding="utf-8").strip() or None
    return None


def charger_pipeline(modele: str = MODELE_PYANNOTE):
    from pyannote.audio import Pipeline

    import transcribe

    pipe = Pipeline.from_pretrained(modele, revision=transcribe.REVISIONS.get(modele), token=jeton_hf())
    if pipe is None:
        raise RuntimeError(f"Modèle {modele} inaccessible : accepte ses conditions sur "
                           f"https://huggingface.co/{modele} et vérifie la clé.")
    return pipe


def diariser_pyannote(wav: Path, nb: int | None, maxi: int | None) -> tuple[list, dict[str, list]]:
    import soundfile as sf
    import torch

    os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")
    pipe = charger_pipeline()
    data, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    onde = torch.from_numpy(data.T.copy())
    if onde.shape[0] > 1:
        onde = onde.mean(0, keepdim=True)
    entree = {"waveform": onde, "sample_rate": sr}
    # Nombre exact s'il est connu ; sinon plafond (invités de l'agenda : certains peuvent manquer).
    opts = {"num_speakers": nb} if nb else ({"max_speakers": maxi} if maxi else {})
    mps = torch.backends.mps.is_available() and not os.environ.get("REUNION_CPU")
    if mps:
        pipe.to(torch.device("mps"))
    try:
        sortie = pipe(entree, **opts)
    except Exception:
        if not mps:
            raise
        print("@repli")
        print("  MPS a échoué, nouvel essai sur le processeur...")
        pipe.to(torch.device("cpu"))
        sortie = pipe(entree, **opts)
    # La version « exclusive » (un seul locuteur à la fois) est faite pour l'alignement avec une transcription.
    ann = sortie.exclusive_speaker_diarization
    tours = [(t.start, t.end, lbl) for t, _, lbl in ann.itertracks(yield_label=True)]
    embs = sortie.speaker_embeddings
    labels = sortie.speaker_diarization.labels() if embs is not None else []
    return tours, {lbl: [float(x) for x in e] for lbl, e in zip(labels, embs if embs is not None else [])}


# ---------------------------------------------------------------- choix de la méthode

def methode(r: dict | None = None) -> str | None:
    """Méthode utilisée : pyannote si une clé est rangée (option avancée), sinon la méthode libre ; None si la
    séparation est désactivée ou impossible (sortie « Moi » / « Les autres », Q3)."""
    r = r or reglages.lire()
    if not r.get("locuteurs", True):
        return None
    if jeton_hf() and importlib.util.find_spec("pyannote"):
        return "pyannote"
    return "libre" if importlib.util.find_spec("sherpa_onnx") else None


def prete(m: str | None) -> bool:
    """Fichiers de la méthode présents sur cet ordinateur."""
    if m == "libre":
        return all(fichier_sain(fichier_libre(rel), sha_modele(rel, sha)) for rel, sha, _ in (SEGMENTATION, EMPREINTE))
    if m == "pyannote":
        import transcribe  # cache Hugging Face, partagé avec les fichiers de transcription

        return transcribe.en_cache(MODELE_PYANNOTE)
    return False


def telecharger(m: str) -> None:
    if m == "libre":
        telecharger_libre()
    elif m == "pyannote":
        charger_pipeline()


def octets_recus(m: str | None) -> int:
    """Octets déjà reçus pour la méthode, pour la barre de préparation."""
    if m == "libre":
        return sum(f.stat().st_size for f in cache_libre().glob("*") if f.is_file()) if cache_libre().exists() else 0
    if m == "pyannote":
        import transcribe

        return transcribe.taille_cache(MODELE_PYANNOTE)
    return 0


def modele_empreintes(m: str | None) -> str:
    return METHODES.get(m or "", METHODES["pyannote"])[0]


def diariser(wav: Path, nb: int | None, maxi: int | None, m: str = "libre") -> tuple[list, dict[str, list]]:
    """Seule entrée de la séparation des voix : nb = nombre exact s'il est connu, sinon maxi = plafond."""
    return (diariser_pyannote if m == "pyannote" else diariser_libre)(wav, nb, maxi)
