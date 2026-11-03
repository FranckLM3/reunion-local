"""Icône Windows de la zone de notification (zone.py), sans Windows : pystray simulé. pytest test_zone.py"""

import sys
import time
import types
from unittest import mock

import serveur
import zone


def test_libelles_et_icone():
    assert zone.libelle_principal() == "Enregistrer" and zone.infobulle() == "Réunion"
    serveur.enregistrement = {"debut": time.time() - 724}
    try:
        assert zone.libelle_principal() == "Arrêter - 12:04"  # BM-13
        assert zone.infobulle() == "Réunion - enregistrement 12:04"  # BM-14
    finally:
        serveur.enregistrement = None
    repos, rec = zone.image(False), zone.image(True)
    assert repos.size == (64, 64) and repos.getpixel((52, 52)) != rec.getpixel((52, 52))  # point rouge


def test_menu_quatre_entrees_et_quitter():
    cree = {}

    class Icone:
        def __init__(self, nom, image, titre, menu):
            cree.update(menu=menu, titre=titre)

        def run(self, setup):
            cree["setup"] = setup

    class Item:
        def __init__(self, texte, action, default=False):
            self.texte, self.action, self.default = texte, action, default

    menu = type("Menu", (), {"SEPARATOR": "-", "__new__": lambda cls, *items: list(items)})
    faux = types.SimpleNamespace(Icon=Icone, Menu=menu, MenuItem=Item)
    http = mock.Mock()
    with mock.patch.dict(sys.modules, {"pystray": faux}), \
            mock.patch.object(serveur, "ouvrir_serveur", return_value=http):
        assert zone.main() == 0
    items = [i for i in cree["menu"] if i != "-"]
    textes = [i.texte if isinstance(i.texte, str) else i.texte() for i in items]
    assert textes == ["Enregistrer", "Ouvrir mes réunions", "Réglages...", "Quitter Réunion"]  # Q5 : 4 entrées
    assert [i.default for i in items] == [False, True, False, False]  # clic gauche : ouvrir mes réunions
    http.shutdown.assert_called_once()
    # BM-15 : quitter pendant un enregistrement demande confirmation ; Annuler ne quitte pas.
    icone = mock.Mock()
    serveur.enregistrement = {"debut": time.time()}
    try:
        with mock.patch.object(zone, "confirmer", return_value=False) as conf:
            zone.quitter(icone)
        assert conf.call_args.args[0] == zone.QUITTER_PENDANT and not icone.stop.called
        with mock.patch.object(zone, "confirmer", return_value=True), \
                mock.patch.object(serveur, "garder_enregistrement") as garder:
            zone.quitter(icone)
        assert garder.called and icone.stop.called
    finally:
        serveur.enregistrement = None
