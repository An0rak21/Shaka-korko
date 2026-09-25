#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ma_station.py — la station KORKO, réglée sur les mesures de la station A.

    python3 ma_station.py --sim --scenario journee          # simulateur + score
    python3 ma_station.py --source 192.168.8.100:8420       # la vraie station A
    KORKO_DEMO=1 python3 ma_station.py --source 192.168.8.100:8420   # réglage pitch

Ce qu'on a mesuré sur le vrai matériel (station A, 25/09) :
  - au râtelier : -59 dBm, très stable (±2 dB) ;
  - quelques mètres suffisent pour que la balise devienne muette ;
  - un corps collé contre la planche la rend muette aussi ;
  - relâchée, elle réapparaît en 1 s environ ;
  - cadence réelle : 0,75 paquet/s (un paquet sur quatre se perd).

D'où la règle, qui s'explique en une phrase à un exploitant :
  « Une planche est partie quand on ne l'entend plus depuis 4 minutes ;
    elle est revenue quand on l'entend nettement, 3 fois en 5 secondes. »

Le départ est horodaté au DÉBUT du silence : la décision arrive tard,
mais l'heure est juste. Un corps devant la balise, une photo, une planche
posée à côté du râtelier : aucun ne dure 4 minutes, aucun ne compte.

Une balise qui faiblit (paquets de plus en plus rares alors que la planche
est au râtelier) puis se tait n'est PAS un départ : c'est une pile à
changer. La station le signale au cloud (BALISE_MUETTE) sans ouvrir de départ.
Chez KORKO le compteur démarre au paiement, donc attendre 3 minutes ne
coûte rien au client : seul le SMS de départ arrive un peu plus tard.

Le journal des événements est écrit sur disque : si le réseau ou le courant
tombe, rien ne se perd, tout part au cloud dans l'ordre au retour du réseau.
"""

import json
import os
import sys
import urllib.request

from korko import Detecteur, lancer, planches_de

DEMO = os.environ.get("KORKO_DEMO") == "1"
CLOUD = os.environ.get("KORKO_CLOUD", "http://localhost:9000/evenements")
JOURNAL = os.environ.get("KORKO_JOURNAL", "journal_station.jsonl")

PLANCHER = -82            # dBm : en dessous, un paquet ne prouve rien
SEUIL_RETOUR = -72        # dBm : un paquet « net », preuve de présence au râtelier
SILENCE_DEPART = 20.0 if DEMO else 240.0   # secondes sans paquet net = partie
CADENCE_MIN = 12         # paquets par minute : en dessous, balise qui faiblit
PREUVES_RETOUR = 3        # paquets nets…
FENETRE_RETOUR = 5.0      # … dans cette fenêtre (s) = revenue


class Station(Detecteur):

    PERIODE_TIC = 1.0

    def __init__(self):
        self.station = "A"
        # état par balise : "la" (au râtelier), "partie", ou None (jamais vue)
        self.etat = {}
        self.dernier_net = {}     # balise -> t du dernier paquet net
        self.preuves = {}         # balise -> [t des paquets nets récents]
        self.etrangeres = {}      # balise étrangère -> t de dernière écoute
        self.recents = {}         # balise -> t des paquets nets de la dernière minute
        self.dernier_paquet = None  # t du dernier paquet reçu, toutes balises confondues
        self.reprise = None       # t où le flux est revenu après un gel
        self.a_envoyer = self._relire_journal()
        self.cloud_ok = None
        print("ma_station : %s, départ après %d s de silence, cloud %s"
              % ("réglage DÉMO" if DEMO else "réglage terrain",
                 SILENCE_DEPART, CLOUD), file=sys.stderr)

    # -- un paquet radio -----------------------------------------------------
    def observation(self, o):
        self.station = o.station
        if self.dernier_paquet is not None and o.t - self.dernier_paquet > SILENCE_DEPART \
                and any(e == "gel" for e in self.etat.values()):
            self.reprise = o.t
            print("ma_station : le flux radio est revenu", file=sys.stderr)
        self.dernier_paquet = o.t
        if o.rssi < PLANCHER:
            return
        net = o.rssi >= SEUIL_RETOUR
        chez_elle = o.balise in planches_de(o.station)

        if not chez_elle:
            if net and o.balise not in self.etrangeres:
                self.signaler("ETRANGERE", o.balise, o.t)
            if net:
                self.etrangeres[o.balise] = o.t
            return

        if not net:
            return
        self.dernier_net[o.balise] = o.t
        r = [t for t in self.recents.get(o.balise, []) if o.t - t <= 60] + [o.t]
        self.recents[o.balise] = r
        etat = self.etat.get(o.balise)

        if etat is None:                 # première écoute : elle est là,
            self.etat[o.balise] = "la"   # ce n'est pas un retour, mais on le dit
            self.a_envoyer.append({"t": round(o.t, 3), "station": self.station,
                                   "balise": o.balise, "evenement": "PRESENTE"})
            self._ecrire_journal()
            return
        if etat == "gel":                # le flux était gelé : elle n'était pas partie
            self.etat[o.balise] = "la"
            return
        if etat == "la":
            return
        # elle était partie : il faut plusieurs preuves nettes et rapprochées
        p = [t for t in self.preuves.get(o.balise, []) if o.t - t <= FENETRE_RETOUR]
        p.append(o.t)
        self.preuves[o.balise] = p
        if len(p) >= PREUVES_RETOUR:
            self.etat[o.balise] = "la"
            self.preuves[o.balise] = []
            self.signaler("RETOUR", o.balise, p[0])

    # -- chaque seconde, même quand plus rien n'arrive -------------------------
    def tic(self, t):
        # Toutes les planches se taisent en même temps et plus aucun paquet
        # n'arrive : c'est la radio ou le réseau de la station, pas des départs.
        muettes = [b for b, e in self.etat.items()
                   if e == "la" and t - self.dernier_net.get(b, t) > SILENCE_DEPART]
        if len(muettes) >= 2 and self.dernier_paquet is not None \
                and t - self.dernier_paquet > SILENCE_DEPART \
                and max(self.dernier_net[b] for b in muettes) - min(self.dernier_net[b] for b in muettes) < 5:
            for b in muettes:
                self.etat[b] = "gel"
            print("ma_station : toutes les planches se taisent en même temps — flux radio "
                  "gelé, aucun départ déclaré", file=sys.stderr)
        # le flux est revenu depuis 15 s : celles qu'on n'entend toujours pas sont parties
        if self.reprise is not None and t - self.reprise > 15:
            for b, e in list(self.etat.items()):
                if e == "gel":
                    self.etat[b] = "partie"
                    self.signaler("DEPART", b, self.dernier_net[b])
            self.reprise = None
        for b, etat in list(self.etat.items()):
            if etat == "la" and t - self.dernier_net.get(b, t) > SILENCE_DEPART:
                self.etat[b] = "partie"
                if self._faiblissait(b):
                    # pas un départ : une pile qui lâche, au râtelier
                    print("ma_station : %s s'est tue en faiblissant — pile ?" % b,
                          file=sys.stderr)
                    self.a_envoyer.append({"t": round(self.dernier_net[b], 3),
                                           "station": self.station, "balise": b,
                                           "evenement": "BALISE_MUETTE"})
                    self._ecrire_journal()
                    continue
                # horodaté au début du silence : la vraie heure de départ
                self.signaler("DEPART", b, self.dernier_net[b])
        for b, vu in list(self.etrangeres.items()):
            if t - vu > SILENCE_DEPART:            # l'étrangère est repartie
                del self.etrangeres[b]
        if self.vider():
            self.envoyer({"t": t, "station": self.station, "evenement": "TIC"})

    def _faiblissait(self, b):
        """Dernière minute avant le silence : trop peu de paquets, tous nets."""
        fin = self.dernier_net.get(b)
        r = [t for t in self.recents.get(b, []) if fin - t <= 60]
        return fin is not None and len(r) >= 2 and len(r) < CADENCE_MIN

    # -- sortie ----------------------------------------------------------------
    def signaler(self, type_, balise, t):
        {"DEPART": self.depart, "RETOUR": self.retour,
         "ETRANGERE": self.etrangere}[type_](balise, t, self.station)
        e = {"t": round(t, 3), "station": self.station,
             "balise": balise, "evenement": type_}
        self.a_envoyer.append(e)
        self._ecrire_journal()
        self.vider()

    def vider(self):
        """Envoie dans l'ordre ; s'arrête au premier échec. Rien ne se perd."""
        change = False
        while self.a_envoyer:
            if not self.envoyer(self.a_envoyer[0]):
                break
            self.a_envoyer.pop(0)
            change = True
        if change:
            self._ecrire_journal()
        return not self.a_envoyer

    def envoyer(self, evenement):
        try:
            urllib.request.urlopen(urllib.request.Request(
                CLOUD, json.dumps(evenement).encode("utf-8"),
                {"Content-Type": "application/json"}), timeout=0.5)
            ok = True
        except Exception:
            ok = False
        if ok != self.cloud_ok:
            print("ma_station : cloud %s" % ("joint" if ok else
                  "injoignable — événements gardés dans %s" % JOURNAL),
                  file=sys.stderr)
            self.cloud_ok = ok
        return ok

    # -- journal sur disque ------------------------------------------------------
    def _ecrire_journal(self):
        tmp = JOURNAL + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in self.a_envoyer:
                f.write(json.dumps(e) + "\n")
        os.replace(tmp, JOURNAL)

    def _relire_journal(self):
        if not os.path.exists(JOURNAL):
            return []
        with open(JOURNAL, encoding="utf-8") as f:
            reste = [json.loads(l) for l in f if l.strip()]
        if reste:
            print("ma_station : %d événement(s) en attente repris du journal"
                  % len(reste), file=sys.stderr)
        return reste


if __name__ == "__main__":
    lancer(Station)
