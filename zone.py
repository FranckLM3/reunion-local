"""Réunion sous Windows : une icône dans la zone de notification, sans fenêtre noire (spec BM-13 à BM-15, Q5).

Lancé par pythonw.exe (raccourcis créés par install.ps1). Le serveur tourne dans ce processus ; le menu a quatre
entrées : Enregistrer (ou « Arrêter - 12:04 »), Ouvrir mes réunions (aussi au clic gauche), Réglages..., Quitter
Réunion. Bibliothèque : pystray (avec Pillow pour dessiner l'icône).
"""

import sys
import threading
import time
import webbrowser

import serveur

URL = f"http://127.0.0.1:{serveur.PORT}"
VERT, ROUGE = (15, 110, 102), (196, 45, 60)
QUITTER_PENDANT = "Un enregistrement est en cours. L'arrêter et quitter ? Ce qui a été enregistré est gardé."


def chrono() -> str | None:
    e = serveur.enregistrement
    return serveur.relecture.heure(time.time() - e["debut"]) if e else None


def libelle_principal(_=None) -> str:
    c = chrono()
    return f"Arrêter - {c}" if c else "Enregistrer"


def infobulle() -> str:
    c = chrono()
    return f"Réunion - enregistrement {c}" if c else "Réunion"


def image(enregistre: bool):
    """Onde verte sur fond clair ; un point rouge pendant l'enregistrement (BM-14)."""
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=VERT)
    for x, h in ((14, 8), (23, 18), (32, 26), (41, 18), (50, 8)):
        d.rounded_rectangle((x - 3, 32 - h, x + 3, 32 + h), radius=3, fill="white")
    if enregistre:
        d.ellipse((38, 38, 62, 62), fill=ROUGE, outline="white", width=3)
    return im


def confirmer(texte: str) -> bool:
    """Boîte de dialogue de Windows, sans bibliothèque. ponytail: boutons OK et Annuler du système, pas les libellés
    « Arrêter et quitter » de la maquette (il faudrait une fenêtre à nous)."""
    import ctypes

    MB_OKCANCEL, MB_ICONQUESTION, MB_TOPMOST, IDOK = 0x1, 0x20, 0x40000, 1
    return ctypes.windll.user32.MessageBoxW(None, texte, "Réunion", MB_OKCANCEL | MB_ICONQUESTION | MB_TOPMOST) == IDOK


def page(ancre: str = "") -> None:
    webbrowser.open(serveur.adresse_page(serveur.JETON, ancre))  # avec le jeton de session (S2)


def basculer(icone, _=None) -> None:
    r = serveur.arreter() if serveur.enregistrement else serveur.demarrer({})
    if "erreur" in r:
        icone.notify(r["erreur"]["cause"], "Réunion")
        page()
    elif serveur.enregistrement:
        page("#enreg")
    else:
        page(f"#reunion/{r['nom']}")
    icone.update_menu()


def quitter(icone, _=None) -> None:
    if serveur.enregistrement and not confirmer(QUITTER_PENDANT):  # BM-15
        return
    serveur.garder_enregistrement()
    icone.stop()


def surveiller(icone) -> None:
    """Infobulle, point rouge et notification « Votre réunion est prête » (TR-07), une fois par seconde."""
    icone.visible = True
    etait, en_cours = None, set()
    while icone.visible:
        enregistre = serveur.enregistrement is not None
        if enregistre != etait:
            icone.icon, etait = image(enregistre), enregistre
            icone.update_menu()
        icone.title = infobulle()
        for nom, t in list(serveur.travaux.items()):
            if t["en_cours"]:
                en_cours.add(nom)
            elif nom in en_cours:
                en_cours.discard(nom)
                titre = serveur.debut_et_titre(nom)[1]
                icone.notify(f"Votre réunion est prête : {titre}" if t["code"] == 0
                             else f"La transcription de « {titre} » a échoué.", "Réunion")
        time.sleep(1)


def main() -> int:
    import pystray
    from pystray import Menu, MenuItem

    http = serveur.ouvrir_serveur()
    if not http:
        return 0  # une instance tourne déjà : sa page vient d'être ouverte
    threading.Thread(target=http.serve_forever, daemon=True).start()
    icone = pystray.Icon("reunion", image(False), "Réunion", Menu(
        MenuItem(libelle_principal, basculer),
        MenuItem("Ouvrir mes réunions", lambda: page(), default=True),  # clic gauche
        MenuItem("Réglages...", lambda: page("#reglages")),
        Menu.SEPARATOR,
        MenuItem("Quitter Réunion", quitter)))
    icone.run(setup=surveiller)
    http.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
