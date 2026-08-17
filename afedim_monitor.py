#!/usr/bin/env python3
"""
Surveillance Afedim -> SMS (sans dependre de leur systeme d'alerte)
=====================================================================
Interroge directement la page de resultats de recherche Afedim,
detecte les nouvelles annonces (par leur identifiant unique dans l'URL),
et envoie un SMS via Twilio pour chacune.

A lancer periodiquement (cron, GitHub Actions...). Toutes les 2-3 minutes
est un bon compromis entre reactivite et politesse envers leur serveur.

CONFIGURATION (variables d'environnement) :
  SEARCH_URL     l'URL de ta recherche filtree (voir exemple ci-dessous)
  STATE_FILE     chemin du fichier qui memorise les annonces deja vues
                 (par defaut: seen_listings.json, a cote du script)
  TWILIO_SID     Account SID Twilio
  TWILIO_TOKEN   Auth Token Twilio
  TWILIO_FROM    numero Twilio expediteur (ex: +1415...)
  TWILIO_TO      ton numero perso (ex: +336...)

Exemple de SEARCH_URL (Strasbourg, budget 0-800e) :
  https://www.afedim.fr/fr/location/annonces/Appartement-Maison/strasbourg-france/1-5-pieces/surface-0-100-m2/budget-0-800-euros/rayon-10-km/disponible-False/options-/exclusPlafondRess-False/Resultats

Dependances : pip install requests twilio
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

SEARCH_URL = os.environ["SEARCH_URL"]
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_listings.json"))

TWILIO_SID = os.environ["TWILIO_SID"]
TWILIO_TOKEN = os.environ["TWILIO_TOKEN"]
TWILIO_FROM = os.environ["TWILIO_FROM"]
TWILIO_TO = os.environ["TWILIO_TO"]  # numero appele en premier (ex: la copine)
TWILIO_TO_BACKUP = os.environ.get("TWILIO_TO_BACKUP", "")  # appele si TWILIO_TO ne repond pas (ex: toi)

# Combien de temps on attend qu'un appel soit decroche avant de le
# considerer comme "pas de reponse" (en secondes). Un appel non-decroche
# sonne generalement 20-30s avant de tomber sur repondeur/messagerie.
CALL_ANSWER_TIMEOUT = int(os.environ.get("CALL_ANSWER_TIMEOUT", "25"))

# "sms", "call", ou "both" (recommande : both -> l'appel reveille,
# le SMS garde le lien sous la main pour le relire au calme)
ALERT_MODE = os.environ.get("ALERT_MODE", "both").lower()

# Nombre d'appels et delai entre eux. Sur iPhone, le mode "Ne pas deranger"
# laisse passer un appel si le MEME numero rappelle dans les 3 minutes
# ("appels repetes") -> on reste sous ce seuil par defaut.
CALL_ATTEMPTS = int(os.environ.get("CALL_ATTEMPTS", "2"))
CALL_GAP_SECONDS = int(os.environ.get("CALL_GAP_SECONDS", "90"))

# Le message dit par la voix robotique lors de l'appel. Nombre repete
# deux fois pour qu'il soit bien entendu meme a moitie endormi.
CALL_MESSAGE = (
    "Nouvelle annonce Afedim disponible. Je repete. "
    "Nouvelle annonce Afedim disponible. Consultez votre telephone."
)

# Repere chaque annonce par son URL "Fiche", qui contient un ID stable
# (ex: .../2-pieces/0050103/Fiche). Le domaine est optionnel car le HTML
# du site utilise parfois des liens relatifs (juste le chemin, sans
# "https://www.afedim.fr" devant).
LISTING_PATTERN = re.compile(
    r'((?:https://www\.afedim\.fr)?/fr/location/annonces/[a-zA-Z\-]+/[a-z0-9\-]+/[a-z0-9\-]+pieces?/(\d{6,8})/Fiche)'
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def load_seen() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen: set):
    STATE_FILE.write_text(json.dumps(sorted(seen)))


def send_sms(body: str, to_number: str = None):
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    client.messages.create(body=body[:300], from_=TWILIO_FROM, to=to_number or TWILIO_TO)


def make_call(to_number: str):
    """Declenche un appel vers le numero donne et retourne son SID."""
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    twiml = f"""
    <Response>
        <Say language="fr-FR">{CALL_MESSAGE}</Say>
        <Pause length="2"/>
        <Say language="fr-FR">{CALL_MESSAGE}</Say>
    </Response>
    """
    call = client.calls.create(twiml=twiml, from_=TWILIO_FROM, to=to_number)
    return call.sid


def wait_for_call_outcome(call_sid: str) -> str:
    """Interroge Twilio jusqu'a ce que l'appel soit termine, et retourne
    son statut final ('completed' = decroche, 'no-answer'/'busy'/'failed' = pas decroche)."""
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    terminal_statuses = {"completed", "busy", "failed", "no-answer", "canceled"}
    elapsed = 0
    poll_interval = 3
    while elapsed < CALL_ANSWER_TIMEOUT + 15:  # marge de securite au-dela du timeout de sonnerie
        call = client.calls(call_sid).fetch()
        if call.status in terminal_statuses:
            return call.status
        time.sleep(poll_interval)
        elapsed += poll_interval
    return "timeout"


def call_until_answered(to_number: str, attempts: int, label: str) -> bool:
    """Essaie d'appeler to_number jusqu'a 'attempts' fois. Retourne True
    des qu'un appel est decroche (statut 'completed'), False si aucun ne l'a ete."""
    for attempt in range(1, attempts + 1):
        sid = make_call(to_number)
        print(f"Appel {attempt}/{attempts} vers {label} declenche (sid={sid})")
        status = wait_for_call_outcome(sid)
        print(f"Resultat de l'appel vers {label}: {status}")
        if status == "completed":
            return True
        if attempt < attempts:
            time.sleep(CALL_GAP_SECONDS)
    return False


def notify(url: str):
    if ALERT_MODE in ("sms", "both"):
        send_sms(f"Nouvelle annonce Afedim ! {url}", TWILIO_TO)
        if TWILIO_TO_BACKUP:
            send_sms(f"Nouvelle annonce Afedim ! {url}", TWILIO_TO_BACKUP)

    if ALERT_MODE in ("call", "both"):
        answered = call_until_answered(TWILIO_TO, CALL_ATTEMPTS, "numero principal")
        if not answered and TWILIO_TO_BACKUP:
            print("Pas de reponse du numero principal, appel du numero de secours...")
            call_until_answered(TWILIO_TO_BACKUP, CALL_ATTEMPTS, "numero de secours")


def fetch_current_listings() -> dict:
    """Retourne {id: url} pour toutes les annonces trouvees sur la page."""
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    matches = LISTING_PATTERN.findall(resp.text)
    listings = {}
    for url, listing_id in matches:
        full_url = url if url.startswith("http") else f"https://www.afedim.fr{url}"
        listings[listing_id] = full_url

    if not listings:
        # Diagnostic pour comprendre pourquoi rien n'a ete trouve : la page
        # a-t-elle seulement charge normalement ?
        print(f"[diagnostic] Statut HTTP: {resp.status_code}, taille reponse: {len(resp.text)} caracteres")
        if "biens disponibles" in resp.text or "Fiche" in resp.text:
            print("[diagnostic] Le mot 'Fiche' ou 'biens disponibles' est present, mais le motif de lien n'a pas matche -> probablement un souci de regex.")
        else:
            print("[diagnostic] Aucune trace d'annonce dans le HTML brut -> la page charge probablement son contenu via JavaScript (le contenu n'est pas dans le HTML initial recupere par 'requests').")

    return listings


def main():
    seen = load_seen()
    current = fetch_current_listings()

    if not current:
        print("Aucune annonce trouvee sur la page (0 resultat, ou page indisponible).")
        return

    new_ids = set(current.keys()) - seen

    if not new_ids:
        print(f"Rien de nouveau. {len(current)} annonce(s) toujours en ligne.")
        return

    # IMPORTANT : on memorise TOUT DE SUITE l'etat actuel, avant meme
    # d'essayer d'envoyer les alertes. Ainsi, si une notification echoue
    # (ex: probleme Twilio), la prochaine execution ne renverra pas en
    # double des alertes pour des annonces deja traitees.
    #
    # On remplace la memoire par les IDs actuellement en ligne (au lieu
    # d'accumuler indefiniment) : une annonce retiree du site (louee,
    # expiree...) disparait donc automatiquement du fichier. Si elle
    # revient un jour en ligne, elle sera de nouveau consideree comme
    # "nouvelle" et re-declenchera une alerte -- comportement voulu.
    save_seen(set(current.keys()))

    for listing_id in new_ids:
        url = current[listing_id]
        try:
            notify(url)
            print(f"Alerte envoyee ({ALERT_MODE}) pour l'annonce {listing_id}")
        except Exception as e:
            # On isole l'echec : les autres annonces de ce passage doivent
            # quand meme etre traitees, et l'etat est deja sauvegarde donc
            # pas de re-notification en boucle au prochain passage.
            print(f"Erreur lors de la notification pour {listing_id}: {e}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print(f"Erreur reseau: {e}", file=sys.stderr)
        sys.exit(1)
