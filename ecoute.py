#!/usr/bin/env python3
"""ecoute.py — ce que la station entend, seconde par seconde.

    python3 ecoute.py 192.168.8.100

Une ligne par seconde et par balise : dernier signal reçu, il y a combien
de temps, et combien de paquets dans les 5 dernières secondes.
Une balise qui ne donne plus de nouvelles s'affiche « MUETTE ».
Ctrl-C pour arrêter.
"""
import json, socket, sys, time

hote = sys.argv[1] if len(sys.argv) > 1 else "192.168.8.100"
port = 8420
s = socket.create_connection((hote, port), timeout=8)
s.settimeout(0.2)
vu = {}          # balise -> [liste des (heure locale, rssi)]
tampon, prochain = b"", time.monotonic()
print("Écoute de %s:%d — éloigne la planche, reviens, observe.\n" % (hote, port))
try:
    while True:
        try:
            bloc = s.recv(4096)
            if not bloc:
                print("flux coupé"); break
            tampon += bloc
            *lignes, tampon = tampon.split(b"\n")
            for l in lignes:
                l = l.strip()
                if not l:
                    continue
                try:
                    d = json.loads(l)
                except ValueError:
                    continue
                if "rssi" in d:
                    vu.setdefault(d["balise"], []).append((time.monotonic(), d["rssi"]))
        except socket.timeout:
            pass
        now = time.monotonic()
        if now >= prochain:
            prochain = now + 1.0
            morceaux = []
            for b in sorted(vu):
                h = [x for x in vu[b] if now - x[0] <= 30]
                vu[b] = h if h else vu[b][-1:]
                dernier_t, dernier_r = vu[b][-1]
                age = now - dernier_t
                n5 = sum(1 for x in h if now - x[0] <= 5)
                if age > 5:
                    morceaux.append("%s MUETTE depuis %3.0f s" % (b, age))
                else:
                    morceaux.append("%s %4d dBm  (%d paquets/5 s)" % (b, dernier_r, n5))
            print(time.strftime("%H:%M:%S"), " | ".join(morceaux) or "aucune balise entendue", flush=True)
except KeyboardInterrupt:
    print()
