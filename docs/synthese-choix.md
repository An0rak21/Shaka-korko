# KORKO — synthèse des choix (hackathon SHAKA)

## Concept et parcours
- Location de planches en liège en libre-service, sans app : un site web ouvert en scannant un QR code.
- Un QR code par planche, gravé dessus. Pas de QR sur le panneau (pour éviter la confusion).
- Planches nommées K1, K2… (K pour KORKO).
- Le panneau explique le concept en 3 étapes : 1. Scanne le QR code de ta planche · 2. Finalise ta résa · 3. À l'eau !!
- Première fois : scan → « Tu as scanné la K1 » → infos (prénom, nom, téléphone avec indicatif pays, e-mail facultatif) → paiement → photo de départ → GO.
- Déjà inscrit·e : scan → « Je la prends » → photo → GO.
- QR illisible : saisie manuelle du nom de la planche (K1).

## Tarif et paiement
- 15 premières minutes gratuites (le temps de rejoindre l'eau).
- Ensuite 0,15 € la minute, soit 4,50 € la demi-heure. Pas de plafond.
- Empreinte bancaire de 300 €, libérée au retour.
- Apple Pay, Google Pay ou carte. Cartes de test : 4242 acceptée, 0002 refusée, 9995 fonds insuffisants, 3184 validation bancaire.

## Armement et minuteries
- La réservation est armée dès que le paiement est validé ; le compteur démarre à ce moment-là (client déjà inscrit : au « Je la prends »).
- 30 min après, si la planche n'a pas quitté le râtelier : SMS de rappel qui prévient de l'annulation.
- 30 min plus tard, toujours sans mouvement : réservation annulée sans frais, empreinte libérée, planche remise à disposition.
- Après 3 h de session : SMS pour rappeler de raccrocher la planche.

## Communication
- Le SMS est le canal principal (Infobip), pour rester relié au service même page fermée : réservation, départ, rappel, annulation, retour avec le montant.
- Chaque SMS contient le lien pour rouvrir sa session.
- E-mail : à décider. Proposition : seulement pour le reçu de fin, si le client a donné son adresse.

## Photos et communauté
- Photo de la planche au départ et au retour : état des lieux, responsabilise le client. Stockées et visibles dans le tableau de bord.
- Tubes : points gagnés à chaque location, à dépenser chez les commerçants du coin ou pour ses prochaines sessions. Barème à valider.
- « Signaler un pépin » (planche, station ou service), avec photo et un texte sur la communauté KORKO.

## Station (matériel réel)
- Une planche est « partie » quand la station ne l'entend plus depuis 4 min (20 s en démo) ; « revenue » quand elle l'entend nettement 3 fois en 5 s.
- Balise qui faiblit = pile à changer, pas un départ. Radio coupée d'un coup = aucun faux départ. Raspberry redémarré = horloge du cloud recalée.
- Journal de la station écrit sur disque : rien ne se perd si le réseau tombe.

## Back-office et démo
- Tableau de bord /admin : état du parc, registre des réservations (export CSV), photos, signalements, boutons « Remettre au râtelier » et « Libérer ».
- Écran jury /live à projeter : état des planches, chrono, montant, SMS envoyés en direct.
- Temps accéléré ×15 en démo : 1 vraie minute = 15 min de session.
- Pied de page : clin d'œil aux organisateurs (SHAKA, Cité de l'Océan, NOTOX).

## Restent à décider
- Barème des tubes.
- E-mail pour le reçu de fin.
- Alerte « planche de retour » en cas de rupture.
