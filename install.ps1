# Installation de Réunion sous Windows 10 et 11. Relançable sans risque : sert aussi à mettre à jour.
# Se lance en double-cliquant sur install.cmd (pas besoin d'ouvrir un terminal).
# Rien n'est installé pour tout l'ordinateur : uv, Python et les bibliothèques restent dans ton compte.

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 sur un Windows 10 ancien n'active pas toujours TLS 1.2, exigé par les sites de téléchargement.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$App = $PSScriptRoot
$Py = Join-Path $App ".venv\Scripts\python.exe"

function Etape($texte) { Write-Host ""; Write-Host $texte -ForegroundColor Cyan }
function Echec($texte) { Write-Host ""; Write-Host $texte -ForegroundColor Red; exit 1 }

Write-Host "Installation de Réunion : environ 15 minutes et 4 Go la première fois (Python, bibliothèques, fichiers de transcription)."
Write-Host "Laissez cette fenêtre ouverte jusqu'au message « Installé »."

if (-not [Environment]::Is64BitOperatingSystem) { Echec "Windows 64 bits requis." }
if ([Environment]::OSVersion.Version.Major -lt 10) { Echec "Windows 10 ou 11 requis." }
$Lecteur = (Get-Item $App).PSDrive
# Dossier sur un partage réseau : pas de lecteur, place libre inconnue ($null -lt 5GB serait vrai). On ne bloque pas.
if ($null -ne $Lecteur.Free -and $Lecteur.Free -lt 5GB) {
    Echec ("Pas assez de place sur le disque {0}: : {1:N1} Go libres, il en faut 5 Go." -f $Lecteur.Name, ($Lecteur.Free / 1GB))
}

Etape "1/5 uv (gestionnaire Python)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    # winget d'abord (paquet signé du catalogue Microsoft) ; sinon l'installateur d'Astral, version figée, dans le
    # compte de l'utilisateur (sans droits administrateur).
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id astral-sh.uv -e --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue) -and -not (Test-Path (Join-Path $env:USERPROFILE ".local\bin\uv.exe"))) {
        try {
            Invoke-RestMethod https://astral.sh/uv/0.5.11/install.ps1 | Invoke-Expression
        } catch {
            Echec "Téléchargement de uv impossible : vérifiez la connexion à internet, puis relancez install.cmd."
        }
    }
    $env:Path = (Join-Path $env:USERPROFILE ".local\bin") + ";" + $env:Path
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Echec "uv reste introuvable après son installation. Redémarre l'ordinateur puis relance install.cmd." }

Etape "2/5 Python 3.12"
if (-not (Test-Path $Py)) {
    uv venv --python 3.12 (Join-Path $App ".venv")
    if ($LASTEXITCODE -ne 0) { Echec "Création de l'environnement Python impossible (voir le message ci-dessus)." }
}

Etape "3/5 Bibliothèques (5 à 10 minutes)"
# soundcard : capture du micro et du son de l'ordinateur (WASAPI) ; faster-whisper : transcription ;
# sherpa-onnx : séparation des voix sans compte ni clé ; pystray et pillow : icône de la zone de notification ;
# imageio-ffmpeg : fournit ffmpeg sans installation système.
uv pip install --python $Py faster-whisper sherpa-onnx soundcard soundfile numpy icalendar recurring-ical-events imageio-ffmpeg pystray pillow
if ($LASTEXITCODE -ne 0) { Echec "Installation des bibliothèques impossible (voir le message ci-dessus). Relance install.cmd." }
# pyannote (option avancée, plus de 1 Go) : seulement si une clé d'accès est déjà rangée ; sinon la page propose
# de l'installer quand la clé est collée.
if (Test-Path (Join-Path $env:USERPROFILE ".config\reunion\hf_token")) {
    uv pip install --python $Py "pyannote.audio>=4"
}
# ffmpeg rangé à côté de Python : transcribe.py ajoute ce dossier au PATH.
$env:REUNION_SCRIPTS = Join-Path $App ".venv\Scripts"
& $Py -c "import imageio_ffmpeg, os, shutil; shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), os.path.join(os.environ['REUNION_SCRIPTS'], 'ffmpeg.exe'))"
if ($LASTEXITCODE -ne 0) { Echec "Mise en place de ffmpeg impossible (voir le message ci-dessus)." }

$Vocabulaire = Join-Path $App "vocabulaire.txt"
if (-not (Test-Path $Vocabulaire)) { Copy-Item (Join-Path $App "vocabulaire.exemple.txt") $Vocabulaire }

# Aucun compte ni clé n'est demandé : les fichiers de transcription et de séparation des voix sont publics.
Etape "4/5 Fichiers de transcription et de séparation des voix (une seule fois)"
& $Py (Join-Path $App "transcribe.py") --telecharger
$Ok = $LASTEXITCODE -eq 0
& $Py (Join-Path $App "transcribe.py") --telecharger-separation
if (-not $Ok -or $LASTEXITCODE -ne 0) {
    Write-Host "Téléchargement incomplet. Pas bloquant : Réunion le reprend à son lancement." -ForegroundColor Yellow
}

Etape "5/5 Raccourcis"
$Shell = New-Object -ComObject WScript.Shell
foreach ($Dossier in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $Lien = $Shell.CreateShortcut((Join-Path $Dossier "Réunion.lnk"))
    # pythonw.exe : l'icône de la zone de notification, sans fenêtre noire (zone.py).
    $Lien.TargetPath = Join-Path $App ".venv\Scripts\pythonw.exe"
    $Lien.Arguments = "zone.py"
    $Lien.WorkingDirectory = $App
    $Lien.Description = "Enregistrer et transcrire une réunion, sur cet ordinateur"
    $Lien.Save()
}

Etape "Installé"
Write-Host "Double-cliquez sur « Réunion » (bureau ou menu Démarrer) : une icône apparaît près de l'horloge et la page s'ouvre."
Write-Host "Au premier lancement, la page vous guide : votre nom, le micro, le son de l'ordinateur."
Write-Host "Ne déplacez pas ce dossier ($App) : les raccourcis s'en servent."
