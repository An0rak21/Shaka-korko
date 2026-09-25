#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mon_cloud.py — le cloud KORKO avec les règles de notre parcours.

Bibliothèque standard uniquement. Rien à installer.

    python3 mon_cloud.py                 réglages terrain
    KORKO_DEMO=1 python3 mon_cloud.py    réglages pitch (délais en secondes)

    tableau de bord   http://localhost:9000/admin
    page client       http://<ip-du-mac>:9000/p/K1   (le QR de la planche K1)
    accueil du site   http://<ip-du-mac>:9000/
    état brut         http://localhost:9000/parc
    les stations      POST /evenements

Nos règles :
  - on scanne la planche, on valide : la session démarre, 15 min offertes,
    puis 0,15 € la minute ;
  - la station voit la planche partir : SMS de départ ;
  - la planche n'a pas bougé 30 min après la validation : SMS de rappel ;
    toujours rien 30 min plus tard : réservation annulée, sans frais ;
  - la station voit la planche revenir : fin, montant, SMS de retour ;
  - planche raccrochée ailleurs, sortie sans réservation, balise qui
    s'éteint : alertes pour l'exploitant.

Le temps est celui du flux des stations (champ `t`), jamais time.time().
L'état est sauvé dans etat_cloud.json : un redémarrage ne perd rien.

SMS réels (optionnel) : définir INFOBIP_BASE_URL, INFOBIP_API_KEY et
éventuellement INFOBIP_FROM. Sans ça, les SMS s'affichent dans le terminal
et dans sms.log.
"""

import csv
import html
import io
import time
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from korko import STATIONS

DEMO = os.environ.get("KORKO_DEMO") == "1"
PORT = int(os.environ.get("KORKO_PORT", "9000"))
URL = os.environ.get("KORKO_URL", "http://localhost:%d" % PORT)
ETAT = os.environ.get("KORKO_ETAT", "etat_cloud.json")

OFFERTES = 15 * 60                    # secondes offertes
TARIF_MIN = 0.15                      # € la minute ensuite
CAUTION = 300
# Temps accéléré pour le pitch : KORKO_ACCEL=15 → 1 vraie minute = 15 minutes de session.
# Le compteur, le montant et les rappels suivent ce temps-là ; la station, elle, reste en temps réel.
ACCEL = float(os.environ.get("KORKO_ACCEL", "1") or 1)
if ACCEL > 1:
    RAPPEL, ANNUL, LONGUE = 30 * 60, 60 * 60, 3 * 3600
else:
    RAPPEL = 60 if DEMO else 30 * 60      # pas bougé : SMS
    ANNUL = 120 if DEMO else 60 * 60      # toujours pas : annulation
    LONGUE = 300 if DEMO else 3 * 3600    # session longue : petit rappel


def ecoule(s, h):
    """Temps de session (accéléré en démo) entre le paiement et l'instant h."""
    return (h - s["debut"]) * ACCEL

PARC = {b: st for st, bs in STATIONS.items() for b in bs}

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

etat = {"horloge": 0.0, "stations": {}, "journal": [], "sessions": {}, "historique": {},
        "reservations": [], "signalements": [],
        "planches": {b: {"origine": s, "ou": s, "statut": "au râtelier",
                          "sorties": 0} for b, s in PARC.items()}}


# ------------------------------------------------------------------ outils

def sauver():
    tmp = ETAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ETAT)


def charger():
    global etat
    if os.path.exists(ETAT):
        with open(ETAT, encoding="utf-8") as f:
            etat = json.load(f)
        print("  état repris de %s" % ETAT)


def num(b):
    """Nom affiché de la planche : korko-01 → K1 (K pour KORKO)."""
    return "K%d" % int(b.split("-")[-1])


def duree_txt(s):
    s = int(max(0, s))
    return "%d min" % (s // 60) if s < 3600 else "%dh%02d" % (s // 3600, s % 3600 // 60)


def montant(s):
    return max(0.0, (s - OFFERTES) / 60.0) * TARIF_MIN


def euros(v):
    return ("%.2f €" % v).replace(".", ",")


def note(texte):
    ligne = "[%9.1f] %s" % (etat["horloge"], texte)
    etat["journal"].insert(0, ligne)
    del etat["journal"][60:]
    print("  " + ligne, flush=True)


def charger_env(chemin="sms.env"):
    """Lit les identifiants SMS dans sms.env (CLE=valeur), jamais dans le code."""
    if not os.path.exists(chemin):
        return
    with open(chemin, encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if ligne and not ligne.startswith("#") and "=" in ligne:
                k, v = ligne.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fournisseur():
    if os.environ.get("BREVO_API_KEY"):
        return "brevo"
    if os.environ.get("TWILIO_SID") and os.environ.get("TWILIO_TOKEN"):
        return "twilio"
    if os.environ.get("INFOBIP_BASE_URL") and os.environ.get("INFOBIP_API_KEY"):
        return "infobip"
    return None


def envoyer_sms(client, texte):
    """Envoi réel. Renvoie None si c'est parti, sinon le message d'erreur."""
    f = fournisseur()
    expediteur = os.environ.get("SMS_EXPEDITEUR", "KORKO")
    try:
        if f == "brevo":
            req = urllib.request.Request(
                "https://api.brevo.com/v3/transactionalSMS/sms",
                json.dumps({"sender": expediteur, "recipient": client.lstrip("+"),
                            "content": texte, "type": "transactional"}).encode("utf-8"),
                {"api-key": os.environ["BREVO_API_KEY"], "Content-Type": "application/json",
                 "Accept": "application/json"})
        elif f == "twilio":
            import base64
            from urllib.parse import urlencode
            sid = os.environ["TWILIO_SID"]
            auth = base64.b64encode(("%s:%s" % (sid, os.environ["TWILIO_TOKEN"])).encode()).decode()
            req = urllib.request.Request(
                "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid,
                urlencode({"To": client, "From": os.environ.get("TWILIO_FROM", expediteur),
                           "Body": texte}).encode("utf-8"),
                {"Authorization": "Basic " + auth,
                 "Content-Type": "application/x-www-form-urlencoded"})
        elif f == "infobip":
            base = os.environ["INFOBIP_BASE_URL"].replace("https://", "").rstrip("/")
            req = urllib.request.Request(
                "https://%s/sms/2/text/advanced" % base,
                json.dumps({"messages": [dict({"destinations": [{"to": client.lstrip("+")}],
                                               "text": texte},
                                              **({"from": expediteur} if expediteur else {}))]}).encode("utf-8"),
                {"Authorization": "App " + os.environ["INFOBIP_API_KEY"],
                 "Content-Type": "application/json", "Accept": "application/json"})
        else:
            return "aucun fournisseur SMS configuré (sms.env)"
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return None
    except urllib.error.HTTPError as e:
        return "%s refuse l'envoi (%s) : %s" % (f, e.code, e.read().decode("utf-8", "replace")[:200])
    except Exception as e:
        return "%s injoignable : %s" % (f, e)


def sms(client, texte):
    note("SMS → %s : %s" % (client, texte))
    with open("sms.log", "a", encoding="utf-8") as f:
        f.write("%s\t%s\n" % (client, texte))
    if not fournisseur():
        return

    def partir():
        err = envoyer_sms(client, texte)
        note("   ↳ SMS réel %s" % ("envoyé via " + fournisseur() if not err else "NON envoyé : " + err))
    threading.Thread(target=partir, daemon=True).start()


def lien(b):
    s = etat["sessions"].get(b) or etat.get("historique", {}).get(b) or {}
    return "%s/s/%s%s" % (URL, num(b), "?k=" + s["jeton"] if s.get("jeton") else "")


def archiver(b, s, raison, duree, montant_):
    etat.setdefault("historique", {})[b] = dict(
        s, etat=raison, duree=duree, montant=round(montant_, 2), fin=etat["horloge"])
    maj_resa(s, statut=raison, fin=etat["horloge"], duree_min=round(duree / 60, 1),
             montant=round(montant_, 2))


def maj_resa(s, **champs):
    """Registre des réservations : une ligne par réservation, jamais effacée."""
    rid = (s or {}).get("rid")
    for r in etat.setdefault("reservations", []):
        if r["id"] == rid:
            r.update(champs)
            return


def heure(t):
    """Heure lisible d'un instant du flux (le Pi donne des secondes Unix)."""
    return time.strftime("%d/%m %H:%M:%S", time.localtime(t)) if t and t > 1e9 else ("t=%.0f s" % (t or 0))


# --------------------------------------------------------------- décisions

def reserver(b, client, prenom="", nom="", email="", tubes=True):
    """Le client a scanné la planche et validé : la session démarre."""
    if b not in PARC:
        return "Planche inconnue."
    if not etat["stations"]:
        return "La station n'est pas encore branchée au cloud."
    p = etat["planches"][b]
    if b in etat["sessions"]:
        return "La %s est déjà réservée. Scanne une autre planche." % num(b)
    if p["statut"] != "au râtelier":
        return "La %s n'est pas au râtelier pour l'instant." % num(b)
    rid = "R%04d" % (len(etat.setdefault("reservations", [])) + 1)
    etat["reservations"].append({"id": rid, "planche": num(b), "balise": b, "station": p["ou"],
                                 "prenom": prenom, "nom": nom, "tel": client, "email": email,
                                 "tubes": bool(tubes), "reservee": etat["horloge"],
                                 "depart": None, "fin": None, "statut": "réservée",
                                 "duree_min": None, "montant": None,
                                 "photo_depart": False, "photo_retour": False})
    etat["sessions"][b] = {"client": client, "prenom": prenom, "jeton": secrets.token_urlsafe(6),
                           "rid": rid,
                           "photo_retour": False,
                           "debut": etat["horloge"], "statut": "réservée",
                           "depart": None, "rappel": False, "longue": False}
    p["statut"] = "réservée"
    note("RÉSERVATION %s par %s" % (b, client))
    sms(client, "KORKO : la %s est réservée pour toi. 15 min offertes pour rejoindre l'eau. "
        "Ta session à tout moment : %s" % (num(b), lien(b)))
    sauver()
    return None


def cloturer(b, t, station, etrangere):
    p = etat["planches"][b]
    p["statut"], p["ou"] = "au râtelier", station
    s = etat["sessions"].get(b)
    if s:
        d = ecoule(s, t)
        archiver(b, s, "terminée", d, montant(d))
        texte = "KORKO : la %s est bien rentrée, merci ! %s · %s. Empreinte de %d € libérée." \
                % (num(b), duree_txt(d), euros(montant(d)), CAUTION)
        if etrangere:
            texte += " Tu l'as raccrochée à la station %s : pas de souci, on la ramène." % station
        sms(s["client"], texte + " Photo de retour = +10 tubes : " + lien(b))
        note("FIN %s — %s, %s" % (b, duree_txt(d), euros(montant(d))))
        del etat["sessions"][b]
    if etrangere:
        note("RÉÉQUILIBRAGE : %s est en %s, sa base est %s" % (b, station, p["origine"]))


def minuteries():
    """À chaque battement de station. Le temps long, c'est le métier du cloud."""
    h = etat["horloge"]
    for b, s in list(etat["sessions"].items()):
        if s["statut"] == "réservée":
            if ecoule(s, h) >= ANNUL:
                sms(s["client"], "KORKO : ta réservation de la %s est annulée, la planche "
                    "n'a pas bougé. Elle est de nouveau disponible. Aucun frais, empreinte "
                    "libérée." % num(b))
                etat["planches"][b]["statut"] = "au râtelier"
                archiver(b, s, "annulée", ecoule(s, h), 0.0)
                del etat["sessions"][b]
                note("ANNULATION %s (jamais partie)" % b)
            elif ecoule(s, h) >= RAPPEL and not s["rappel"]:
                s["rappel"] = True
                sms(s["client"], "KORKO : la %s n'a pas encore quitté le râtelier. Tout va "
                    "bien ? Sans mouvement d'ici %s, on annule ta réservation pour la remettre "
                    "à disposition. %s" % (num(b), duree_txt(ANNUL - RAPPEL), lien(b)))
        elif s["statut"] == "en mer" and ecoule(s, h) >= LONGUE and not s["longue"]:
            s["longue"] = True
            sms(s["client"], "KORKO : ta session avec la %s tourne depuis %s. Pense à la "
                "raccrocher en sortant de l'eau." % (num(b), duree_txt(ecoule(s, h))))


VERROU = threading.RLock()
DERNIER_EV = [time.monotonic()]


def horloge_de_secours():
    """Si la station se tait (aucune balise entendue, radio gelée), le temps
    de la session continue quand même : le cloud avance l'horloge au rythme
    réel. Dès que la station reparle, c'est de nouveau son temps qui fait foi."""
    while True:
        time.sleep(1)
        with VERROU:
            muet = time.monotonic() - DERNIER_EV[0]
            if muet > 3 and etat["horloge"] > 0:
                etat["horloge"] += 1.0
                minuteries()
                if int(etat["horloge"]) % 10 == 0:
                    sauver()


def temps_station(st, t, tic):
    """Le temps d'une station, recalé sur l'horloge du cloud.
    Si la station repart de zéro (Raspberry redémarré, flux relancé), son `t`
    recule : on garde un décalage pour que le temps du cloud continue d'avancer."""
    dec = etat.setdefault("decalages", {})
    vu = etat["stations"].get(st)
    tt = t + dec.get(st, 0.0)
    if tic and vu is not None and tt < vu - 5:      # seul un TIC donne l'heure « maintenant »
        dec[st] = vu - t
        tt = vu
        note("station %s : son horloge est repartie de zéro, recalée" % st)
    elif tic and vu is None and st not in dec and etat["horloge"] > t + 5:
        dec[st] = etat["horloge"] - t
        tt = etat["horloge"]
    return tt


def traiter(ev):
    DERNIER_EV[0] = time.monotonic()
    st, type_, b = ev.get("station"), ev.get("evenement"), ev.get("balise")
    ev["t"] = temps_station(st, float(ev.get("t", 0)), type_ == "TIC")
    etat["horloge"] = max(etat["horloge"], ev["t"])
    if st not in etat["stations"]:
        note("station %s branchée" % st)
    etat["stations"][st] = etat["horloge"]

    if type_ == "TIC":
        minuteries()
    elif b not in PARC:
        note("balise inconnue : %s" % b)
    elif type_ == "DEPART":
        p, s = etat["planches"][b], etat["sessions"].get(b)
        p["statut"], p["ou"] = "en mer", None
        p["sorties"] += 1
        if not s:
            p["statut"] = "sortie sans réservation"
            note("ALERTE : %s sortie de %s sans réservation" % (b, st))
        else:
            s["statut"], s["depart"] = "en mer", ev["t"]
            maj_resa(s, statut="en mer", depart=ev["t"])
            note("DÉPART %s — %s" % (b, s["client"]))
            sms(s["client"], "KORKO : c'est parti avec la %s ! Le compteur tourne "
                "(15 min offertes). Ta session : %s" % (num(b), lien(b)))
    elif type_ in ("RETOUR", "ETRANGERE"):
        cloturer(b, ev["t"], st, etrangere=(type_ == "ETRANGERE"))
    elif type_ == "PRESENTE":
        # la station (re)démarre et entend la planche au râtelier : on recale le parc
        p, s = etat["planches"][b], etat["sessions"].get(b)
        if s and s["statut"] == "en mer":
            cloturer(b, ev["t"], st, etrangere=(PARC[b] != st))
        elif not s and p["statut"] != "au râtelier":
            note("RECALAGE : %s entendue au râtelier %s (était « %s »)" % (num(b), st, p["statut"]))
            p["statut"], p["ou"] = "au râtelier", st
    elif type_ == "BALISE_MUETTE":
        etat["planches"][b]["statut"] = "balise à vérifier"
        note("MAINTENANCE : la balise de %s s'est tue au râtelier — pile ?" % b)
    sauver()


# ------------------------------------------------------------------ pages

STYLE = """<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{margin:0;font-family:-apple-system,system-ui,sans-serif;background:#FAF6F0;color:#0E2C2E}
main{max-width:420px;margin:0 auto;padding:28px 22px}h1{font-size:30px;line-height:1.1;margin:10px 0}
.eye{font-size:11px;letter-spacing:.3em;text-transform:uppercase}.num{font-size:88px;font-weight:800;line-height:1}
.box{background:#E1E8E3;border-radius:20px;padding:16px 18px;margin:16px 0}label{display:block;font-weight:600;margin:14px 0 6px}
input{width:100%;box-sizing:border-box;height:52px;border-radius:14px;border:1.5px solid #CFC5B6;padding:0 14px;font-size:17px}
button,.btn{display:block;width:100%;height:56px;margin-top:18px;border:0;border-radius:999px;background:#EDB34C;color:#0E2C2E;
font-size:17px;font-weight:700;text-align:center;line-height:56px;text-decoration:none}.muted{color:#3E4F50;font-size:14px}</style>"""


def page(titre, corps, rafraichir=None):
    meta = "<meta http-equiv=refresh content=%d>" % rafraichir if rafraichir else ""
    return ("<!doctype html><html lang=fr><meta charset=utf-8>%s<title>%s</title>%s"
            "<main><div class=eye>KORKO · surf anytime</div>%s</main>"
            % (meta, html.escape(titre), STYLE, corps))


def page_planche(b, erreur=None):
    p = etat["planches"][b]
    dispo = sum(1 for x, q in etat["planches"].items()
                if q["ou"] == p["ou"] and q["statut"] == "au râtelier")
    total = len(STATIONS.get(p["origine"], ()))
    err = "<div class=box style='background:#FBE4DF;color:#6E1B10'>%s</div>" % html.escape(erreur) if erreur else ""
    return page("Planche %s" % num(b), """
<h1>Tu as scanné la %s.</h1><div class=num>%s</div>
<p class=muted>%d libre(s) sur %d à la station %s</p>
<div class=box><b>15 min offertes, le temps d'aller à l'eau.</b><br>
Ensuite 0,15 € la minute, soit 4,50 € la demi-heure. Le compteur démarre dès que tu valides.</div>
%s<form method=post action=/reserver>
<input type=hidden name=balise value="%s">
<label for=prenom>Prénom</label><input id=prenom name=prenom autocomplete=given-name>
<label for=tel>Téléphone</label><input id=tel name=tel type=tel required placeholder="+33 6 12 34 56 78" autocomplete=tel>
<p class=muted>Démo : le paiement et l'empreinte de 300 € sont simulés.</p>
<button>Valider et réserver la %s</button></form>""" % (num(b), num(b), dispo, total,
                                                         p["origine"], err, b, num(b)))


def page_session(b):
    s = etat["sessions"].get(b)
    if not s:
        return page("Session", "<h1>Pas de session en cours sur la %s.</h1>"
                    "<a class=btn href=/p/%s>Réserver la %s</a>" % (num(b), b, num(b)), 10)
    d = ecoule(s, etat["horloge"])
    etape = ("GO ! La %s est à toi. Décroche-la et file à l'eau." % num(b)
             if s["statut"] == "réservée" else "À l'eau avec la %s." % num(b))
    return page("Ta session", """<h1>%s</h1><div class=num>%s</div>
<p style="font-size:24px;font-weight:700">%s</p>
<div class=box>Pour finir, raccroche la planche au râtelier. Rien à confirmer : la station le voit.
Tu peux fermer cette page, on t'envoie un SMS.</div>""" % (etape, duree_txt(d), euros(montant(d))), 5)


def tableau():
    h = etat["horloge"]
    l = ["STATIONS"] + ["  %s  dernier message il y a %.0f s" % (st, h - t)
                        for st, t in sorted(etat["stations"].items())] or ["  aucune"]
    l += ["", "PARC"]
    vues = set(etat["stations"]) or {"A"}
    for b, p in sorted(etat["planches"].items()):
        if p["origine"] not in vues and p["ou"] not in vues:
            continue
        s = etat["sessions"].get(b)
        l.append("  %-9s %-24s base %s  sorties %-3d %s%s" % (
            num(b), p["statut"], p["origine"], p["sorties"],
            "⚠ " + p["alerte"] + "  " if p.get("alerte") else "",
            "→ %s (%s, %s, %s)" % (s["client"], s["statut"], duree_txt(ecoule(s, h)),
                                   euros(montant(ecoule(s, h)))) if s else ""))
    l += ["", "JOURNAL"] + ["  " + x for x in etat["journal"][:20]]
    lignes = []
    for r in reversed(etat.get("reservations", [])):
        lignes.append("<tr>" + "".join("<td>%s</td>" % html.escape(str(v)) for v in (
            r["id"], r["planche"], ("%s %s" % (r["prenom"], r["nom"])).strip(), r["tel"],
            heure(r["reservee"]), heure(r["depart"]) if r["depart"] else "—",
            heure(r["fin"]) if r["fin"] else "—", r["statut"],
            "%s min" % r["duree_min"] if r["duree_min"] is not None else "—",
            euros(r["montant"]) if r["montant"] is not None else "—",
            "")) + "</tr>")
        cell = " ".join(
            "<a href='/admin/photos/%s' target=_blank>%s</a>" % (html.escape(v), n) if isinstance(v, str)
            else (n if v else "") for n, v in (("départ", r["photo_depart"]), ("retour", r["photo_retour"])))
        lignes[-1] = lignes[-1].replace("<td></td></tr>", "<td>%s</td></tr>" % cell)
    table = ("<h2 style='margin-top:28px'>Réservations (%d) · <a href=/admin/reservations.csv>export CSV</a></h2>"
             "<div style='overflow-x:auto'><table style='border-collapse:collapse;font-size:13px;width:100%%'>"
             "<tr style='text-align:left'><th>N°</th><th>Planche</th><th>Client</th><th>Tél.</th><th>Réservée</th>"
             "<th>Départ</th><th>Fin</th><th>Statut</th><th>Durée</th><th>Montant</th><th>Photos</th></tr>%s</table></div>"
             % (len(etat.get("reservations", [])), "".join(lignes) or "<tr><td colspan=11>Aucune réservation</td></tr>"))
    actions = ""
    for b, p in sorted(etat["planches"].items()):
        if PARC[b] in etat["stations"] and (p["statut"] != "au râtelier" or b in etat["sessions"]):
            actions += ("<form method=post action=/admin/remettre style='display:inline-block;margin:0 10px 10px 0'>"
                        "<input type=hidden name=b value='%s'><button style='height:44px;margin:0;padding:0 18px;"
                        "border-radius:999px;line-height:normal;width:auto'>%s</button></form>"
                        % (b, ("Libérer %s (annuler la résa en cours)" if b in etat["sessions"]
                               else "Remettre %s au râtelier") % num(b)))
    if actions:
        table = ("<h2 style='margin-top:20px'>Actions exploitant</h2><p class=muted>La machine se trompe ? "
                 "Tu as le dernier mot.</p>" + actions) + table
    sig = []
    for x in reversed(etat.get("signalements", [])):
        sig.append("<tr>" + "".join("<td>%s</td>" % v for v in (
            html.escape(x["id"]), heure(x["t"]), html.escape(x["cible"]), html.escape(x["planche"] or x["station"]),
            html.escape(x["type"]), html.escape(x["texte"]),
            "<a href='/admin/photos/%s' target=_blank>photo</a>" % html.escape(x["photo"]) if x["photo"] else "",
            html.escape(x["tel"]), html.escape(x["statut"]))) + "</tr>")
    table += ("<h2 style='margin-top:28px'>Signalements (%d)</h2><div style='overflow-x:auto'>"
              "<table style='border-collapse:collapse;font-size:13px;width:100%%'><tr style='text-align:left'>"
              "<th>N°</th><th>Heure</th><th>Sujet</th><th>Où</th><th>Pépin</th><th>Détail</th><th>Photo</th>"
              "<th>Tél.</th><th>Statut</th></tr>%s</table></div>"
              % (len(etat.get("signalements", [])), "".join(sig) or "<tr><td colspan=9>Aucun signalement</td></tr>"))
    return page("KORKO cloud", "<h1>Cloud · %s</h1><pre style='font-size:13px'>%s</pre>%s"
                % ("DÉMO" if DEMO else "terrain", html.escape("\n".join(l)), table), 3).replace(
                    "max-width:420px", "max-width:1100px")


def export_csv():
    f = io.StringIO()
    champs = ["id", "planche", "station", "prenom", "nom", "tel", "email", "tubes", "reservee",
              "depart", "fin", "statut", "duree_min", "montant", "photo_depart", "photo_retour"]
    w = csv.DictWriter(f, fieldnames=champs, delimiter=";", extrasaction="ignore")
    w.writeheader()
    for r in etat.get("reservations", []):
        w.writerow(dict(r, reservee=heure(r["reservee"]), depart=heure(r["depart"]) if r["depart"] else "",
                        fin=heure(r["fin"]) if r["fin"] else "",
                        montant=("%.2f" % r["montant"]).replace(".", ",") if r["montant"] is not None else ""))
    return "\ufeff" + f.getvalue()


def api_planche(b):
    p = etat["planches"][b]
    ici = p["ou"] or p["origine"]
    libres = sum(1 for x, q in etat["planches"].items() if q["ou"] == ici and q["statut"] == "au râtelier")
    return {"balise": b, "num": num(b), "station": ici, "libres": libres,
            "total": len(STATIONS.get(ici, ())), "statut": p["statut"],
            "disponible": p["statut"] == "au râtelier" and b not in etat["sessions"]}


def api_live():
    """Écran jury : l'état du râtelier et la session en cours, en gros."""
    h = etat["horloge"]
    planches = []
    vues = set(etat["stations"]) or {"A"}
    for b, p in sorted(etat["planches"].items()):
        if p["origine"] not in vues and p["ou"] not in vues:
            continue
        s = etat["sessions"].get(b)
        d = ecoule(s, h) if s else None
        planches.append({"num": num(b), "statut": p["statut"], "prenom": (s or {}).get("prenom", ""),
                         "etat": (s or {}).get("statut"), "duree": d,
                         "montant": montant(d) if d is not None else None})
    def masque(l):                       # écran public : numéro masqué
        tel, _, txt = l.split("SMS → ", 1)[1].partition(" : ")
        return "%s••••%s : %s" % (tel[:4], tel[-2:], txt)
    sms_ = [masque(l) for l in etat["journal"] if "SMS → " in l][:4]
    st = etat["stations"]
    return {"planches": planches, "sms": sms_, "accel": ACCEL,
            "station": bool(st) and h - max(st.values()) < 15,
            "journal": [l for l in etat["journal"] if "↳" not in l][:6]}


LIVE_HTML = """<!doctype html><html lang=fr><meta charset=utf-8><title>KORKO · en direct</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{margin:0;background:#0E2C2E;color:#FAF6F0;font-family:system-ui,-apple-system,sans-serif;padding:40px 48px}
header{display:flex;align-items:center;gap:24px;margin-bottom:36px}
header img{height:56px}header .sp{flex:1}
.pill{padding:8px 18px;border-radius:999px;font-weight:700;font-size:18px;background:#1c4447}
.pill.ok{background:#87ADA8;color:#0E2C2E}.pill.acc{background:#EDB34C;color:#0E2C2E}
.grille{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:28px}
.carte{background:#FAF6F0;color:#0E2C2E;border-radius:28px;padding:30px 34px;min-height:250px;transition:background .4s}
.carte.res{background:#EDB34C}.carte.mer{background:#87ADA8}
.num{font-size:84px;font-weight:800;line-height:1}.st{font-size:28px;font-weight:700;margin-top:8px}
.qui{font-size:22px;margin-top:6px}.chrono{font-size:64px;font-weight:800;font-variant-numeric:tabular-nums;margin-top:14px}
.eur{font-size:26px;font-weight:700}
h2{font-size:15px;letter-spacing:.3em;text-transform:uppercase;opacity:.7;margin:40px 0 12px}
.sms div{background:#1c4447;border-radius:18px;padding:14px 18px;margin-bottom:10px;font-size:18px;line-height:1.35}
</style>
<header><img src="/web/logo-cream.png" alt="KORKO"><div class=sp></div><span class=pill id=acc></span><span class=pill id=stn>Station A</span></header>
<div class=grille id=grille></div>
<h2>Derniers SMS envoyés</h2><div class=sms id=sms></div>
<script>
var L={}, eur=function(v){return v.toFixed(2).replace('.',',')+' €'};
function hm(s){s=Math.max(0,Math.floor(s));var h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+'h'+String(m).padStart(2,'0')}
function esc(t){var d=document.createElement('div');d.textContent=t;return d.innerHTML}
function rendu(){
  var g='';(L.planches||[]).forEach(function(p){
    var cl='',st='Au râtelier · libre',q='',c='';
    if(p.etat==='réservée'){cl='res';st='Réservée';}
    else if(p.etat==='en mer'){cl='mer';st="À l'eau";}
    else if(p.statut!=='au râtelier'){st=p.statut.charAt(0).toUpperCase()+p.statut.slice(1);}
    if(p.prenom)q='<div class=qui>par '+esc(p.prenom)+'</div>';
    if(p.duree!==null){var d=p.duree+(Date.now()-L.recu)/1000*L.accel;
      var m=Math.max(0,(d-900)/60)*0.15;
      c='<div class=chrono>'+hm(d)+'</div><div class=eur>'+(d<900?'15 min offertes · reste '+Math.ceil((900-d)/60)+' min':eur(m))+'</div>';}
    g+='<div class="carte '+cl+'"><div class=num>'+p.num+'</div><div class=st>'+st+'</div>'+q+c+'</div>';});
  document.getElementById('grille').innerHTML=g;
}
function maj(){fetch('/api/live').then(function(r){return r.json()}).then(function(d){
  L=d;L.recu=Date.now();
  document.getElementById('acc').textContent=d.accel>1?'Temps accéléré ×'+d.accel:'Temps réel';
  document.getElementById('acc').className='pill'+(d.accel>1?' acc':'');
  var s=document.getElementById('stn');s.className='pill'+(d.station?' ok':'');s.textContent=d.station?'Station A · en ligne':'Station A · hors ligne';
  document.getElementById('sms').innerHTML=d.sms.map(function(t){return '<div>'+esc(t)+'</div>'}).join('')||"<div>Aucun pour l'instant.</div>";
  rendu();}).catch(function(){})}
maj();setInterval(maj,1500);setInterval(rendu,250);
</script></html>"""


def api_session(b, jeton):
    s = etat["sessions"].get(b)
    if s and jeton and s.get("jeton") == jeton:
        return {"etat": s["statut"], "duree": ecoule(s, etat["horloge"]), "accel": ACCEL, "prenom": s.get("prenom", ""),
                "photo_retour": s.get("photo_retour", False)}
    h = etat.get("historique", {}).get(b)
    if h and jeton and h.get("jeton") == jeton:
        return {"etat": h["etat"], "duree": h["duree"], "montant": h["montant"],
                "prenom": h.get("prenom", ""), "photo_retour": h.get("photo_retour", False)}
    return {"etat": "aucune"}


PHOTOS = os.environ.get("KORKO_PHOTOS", "photos")


def api_photo(b, jeton, type_, image=None):
    """Photo de départ ou de retour : rangée dans photos/, notée dans le registre."""
    if type_ not in ("depart", "retour"):
        return {"erreur": "type de photo inconnu"}
    for s in (etat["sessions"].get(b), etat.get("historique", {}).get(b)):
        if s and jeton and s.get("jeton") == jeton:
            fichier = None
            if image and "," in image:
                import base64
                try:
                    brut = base64.b64decode(image.split(",", 1)[1])
                    os.makedirs(PHOTOS, exist_ok=True)
                    fichier = "%s-%s.jpg" % (s.get("rid", b), type_)
                    with open(os.path.join(PHOTOS, fichier), "wb") as f:
                        f.write(brut)
                except Exception as e:
                    note("photo illisible : %s" % e)
            s["photo_" + type_] = True
            maj_resa(s, **{"photo_" + type_: fichier or True})
            note("PHOTO %s de %s reçue%s" % (type_, b, " (%s)" % fichier if fichier else ""))
            sauver()
            return {"ok": True}
    return {"erreur": "session introuvable sur ce téléphone (rouvre le lien reçu par SMS)"}


def api_signalement(d):
    """Un usager signale un pépin : planche, station ou service."""
    cible = d.get("cible")
    if cible not in ("planche", "station", "service") or not d.get("type"):
        return {"erreur": "dis-nous ce qui concerne le pépin"}
    b = d.get("balise") if d.get("balise") in PARC else None
    liste = etat.setdefault("signalements", [])
    sid = "S%03d" % (len(liste) + 1)
    s = None
    if b and d.get("jeton"):
        s = etat["sessions"].get(b) or etat.get("historique", {}).get(b)
        if s and s.get("jeton") != d.get("jeton"):
            s = None
    fichier = None
    image = d.get("image")
    if image and "," in image:
        import base64
        try:
            os.makedirs(PHOTOS, exist_ok=True)
            fichier = "%s.jpg" % sid
            with open(os.path.join(PHOTOS, fichier), "wb") as f:
                f.write(base64.b64decode(image.split(",", 1)[1]))
        except Exception as e:
            fichier = None
            note("photo de signalement illisible : %s" % e)
    liste.append({"id": sid, "t": etat["horloge"], "cible": cible, "type": d.get("type"),
                  "planche": num(b) if (b and cible == "planche") else "",
                  "station": (etat["planches"][b]["ou"] or etat["planches"][b]["origine"]) if b else "",
                  "texte": (d.get("texte") or "")[:500], "photo": fichier,
                  "tel": (s or {}).get("client") or (d.get("tel") or ""), "statut": "à traiter"})
    if b and cible == "planche":
        etat["planches"][b]["alerte"] = "%s : %s" % (sid, d.get("type"))
    note("SIGNALEMENT %s — %s%s : %s" % (sid, cible, " " + num(b) if (b and cible == "planche") else "",
                                        d.get("type")))
    sauver()
    return {"ok": True, "id": sid}


TYPES = {".html": "text/html; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
         ".svg": "image/svg+xml"}


class Cloud(BaseHTTPRequestHandler):

    def repondre(self, corps, type_="text/html; charset=utf-8", code=200):
        corps = corps.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def lire(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")

    def json_(self, obj, code=200):
        self.repondre(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", code)

    def do_POST(self):
        u = urlparse(self.path)
        m = u.path.strip("/").split("/")
        if m[0] == "api":
            try:
                d = json.loads(self.lire() or "{}")
            except ValueError:
                d = {}
            if m[1:] == ["reserver"]:
                b = d.get("balise", "")
                tel = "".join(c for c in d.get("tel", "") if c.isdigit() or c == "+")
                if tel.startswith("00"):
                    tel = "+" + tel[2:]
                elif tel.startswith("0"):
                    tel = "+33" + tel[1:]
                err = reserver(b, tel, d.get("prenom", "").strip(), d.get("nom", "").strip(),
                               d.get("email", "").strip(), d.get("tubes", True))
                if err:
                    return self.json_({"erreur": err})
                return self.json_({"ok": True, "jeton": etat["sessions"][b]["jeton"]})
            if m[1:] == ["signalement"]:
                return self.json_(api_signalement(d))
            if len(m) == 3 and m[1] == "photo" and m[2] in PARC:
                return self.json_(api_photo(m[2], d.get("jeton"), d.get("type"), d.get("image")))
            return self.json_({"erreur": "inconnu"}, 404)
        if u.path == "/admin/remettre":
            b = parse_qs(self.lire()).get("b", [""])[0]
            if b in PARC:
                p = etat["planches"][b]
                s = etat["sessions"].pop(b, None)
                if s:                                   # réservation en cours : annulée sans frais
                    archiver(b, s, "annulée (exploitant)", ecoule(s, etat["horloge"]), 0.0)
                note("EXPLOITANT : %s remise au râtelier à la main (était « %s »)" % (num(b), p["statut"]))
                p["statut"], p["ou"] = "au râtelier", PARC[b]
                p.pop("alerte", None)
                sauver()
            self.send_response(303)
            self.send_header("Location", "/admin")
            self.end_headers()
            return
        if u.path == "/reserver":
            f = parse_qs(self.lire())
            b = f.get("balise", [""])[0]
            tel = "".join(c for c in f.get("tel", [""])[0] if c.isdigit() or c == "+")
            if tel.startswith("0"):
                tel = "+33" + tel[1:]
            if b not in PARC:
                return self.repondre(page("Erreur", "<h1>Planche inconnue.</h1>"), code=404)
            err = reserver(b, tel, f.get("prenom", [""])[0].strip())
            if err:
                return self.repondre(page_planche(b, err))
            return self.repondre(page_session(b))
        for ligne in self.lire().strip().splitlines():      # les stations
            try:
                with VERROU:
                    traiter(json.loads(ligne))
            except Exception as e:
                note("événement illisible : %s" % e)
        self.repondre("ok", "text/plain")

    def do_GET(self):
        u = urlparse(self.path)
        morceaux = u.path.strip("/").split("/")
        q = parse_qs(u.query)
        if u.path == "/api/live":
            return self.json_(api_live())
        if morceaux[0] == "api" and len(morceaux) == 3 and morceaux[2] in PARC:
            if morceaux[1] == "planche":
                return self.json_(api_planche(morceaux[2]))
            if morceaux[1] == "session":
                return self.json_(api_session(morceaux[2], q.get("k", [""])[0]))
        if morceaux[0] == "api":
            return self.json_({"erreur": "planche inconnue"}, 404)
        if morceaux[0] == "web" and len(morceaux) == 2:
            nom = os.path.basename(morceaux[1])
            f = os.path.join(WEB, nom)
            ext = os.path.splitext(nom)[1]
            if ext in TYPES and os.path.isfile(f):
                with open(f, "rb") as fh:
                    corps = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", TYPES[ext])
                self.send_header("Content-Length", str(len(corps)))
                self.send_header("Cache-Control", "max-age=300")
                self.end_headers()
                return self.wfile.write(corps)
            return self.repondre("introuvable", "text/plain", 404)
        if u.path == "/" or (len(morceaux) == 2 and morceaux[0] in ("p", "s")):
            with open(os.path.join(WEB, "app.html"), encoding="utf-8") as fh:
                return self.repondre(fh.read())
        if u.path == "/live":
            return self.repondre(LIVE_HTML)
        if u.path == "/parc":
            return self.repondre(json.dumps(etat, ensure_ascii=False, indent=1),
                                 "application/json; charset=utf-8")
        if u.path == "/arme":                                # API, pour les tests
            err = reserver(q.get("balise", [""])[0], q.get("client", ["+33600000000"])[0])
            return self.repondre(err or "ok", "text/plain; charset=utf-8")
        if len(morceaux) == 3 and morceaux[:2] == ["admin", "photos"]:
            f = os.path.join(PHOTOS, os.path.basename(morceaux[2]))
            if os.path.isfile(f):
                with open(f, "rb") as fh:
                    corps = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(corps)))
                self.end_headers()
                return self.wfile.write(corps)
            return self.repondre("introuvable", "text/plain", 404)
        if u.path == "/admin/reservations.csv":
            corps = export_csv().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=reservations-korko.csv")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            return self.wfile.write(corps)
        if u.path == "/admin":
            return self.repondre(tableau())
        return self.repondre("introuvable", "text/plain", 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    charger_env()
    if len(sys.argv) == 2 and sys.argv[1] == "--logs-sms" and fournisseur() == "twilio":
        import base64
        sid = os.environ["TWILIO_SID"]
        auth = base64.b64encode(("%s:%s" % (sid, os.environ["TWILIO_TOKEN"])).encode()).decode()
        req = urllib.request.Request(
            "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json?PageSize=10" % sid,
            headers={"Authorization": "Basic " + auth})
        try:
            res = json.load(urllib.request.urlopen(req, timeout=10)).get("messages", [])
        except urllib.error.HTTPError as e:
            sys.exit("Twilio refuse : %s %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        print("10 derniers SMS vus par Twilio :\n")
        for r in res:
            print("  %s  %-12s %s" % ((r.get("date_created") or "")[17:25], r.get("status", ""),
                                     (r.get("body") or "")[:70]))
            if r.get("error_code"):
                print("      ✗ erreur %s : %s" % (r["error_code"], r.get("error_message") or
                                                  "voir twilio.com/docs/api/errors/%s" % r["error_code"]))
        sys.exit(0)
    if len(sys.argv) == 2 and sys.argv[1] == "--logs-sms":
        base, cle = os.environ.get("INFOBIP_BASE_URL"), os.environ.get("INFOBIP_API_KEY")
        if not (base and cle):
            sys.exit("Pas d'identifiants Infobip dans sms.env")
        req = urllib.request.Request("https://%s/sms/1/logs?limit=10" % base.replace("https://", "").rstrip("/"),
                                     headers={"Authorization": "App " + cle, "Accept": "application/json"})
        try:
            res = json.load(urllib.request.urlopen(req, timeout=10)).get("results", [])
        except urllib.error.HTTPError as e:
            sys.exit("Infobip refuse : %s %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        print("10 derniers SMS vus par Infobip :\n")
        for r in res:
            st = r.get("status", {})
            print("  %s  %-10s %-22s %s" % (r.get("sentAt", "")[11:19], st.get("groupName", ""),
                                             st.get("name", ""), (r.get("text") or "")[:60]))
            if st.get("description"):
                print("      → %s" % st["description"])
            if r.get("error", {}).get("groupName") not in (None, "OK"):
                print("      ✗ %s : %s" % (r["error"].get("name"), r["error"].get("description")))
        sys.exit(0)
    if len(sys.argv) == 3 and sys.argv[1] == "--test-sms":
        f = fournisseur()
        print("Fournisseur : %s" % (f or "aucun — remplis sms.env"))
        if f:
            err = envoyer_sms(sys.argv[2], "KORKO : test d'envoi réussi. Surf anytime !")
            print("  ✓ SMS envoyé à %s" % sys.argv[2] if not err else "  ✗ " + err)
        sys.exit(0)
    charger()
    print("SMS : %s" % ("envoi réel via " + fournisseur() if fournisseur()
                         else "simulés (terminal + sms.log) — remplis sms.env pour de vrais SMS"))
    print("Cloud KORKO (%s) sur %s" % ("DÉMO" if DEMO else "terrain", URL))
    print("  écran jury : /live     tableau de bord : /admin     page client : /p/korko-01     état : /parc\n")
    threading.Thread(target=horloge_de_secours, daemon=True).start()
    ThreadingHTTPServer(("", PORT), Cloud).serve_forever()
