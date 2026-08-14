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
TWILIO_TO = os.environ["TWILIO_TO"]

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


def send_sms(body: str):
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    client.messages.create(body=body[:300], from_=TWILIO_FROM, to=TWILIO_TO)


def make_call():
    """Declenche un appel vocal automatique (texte-vers-parole en francais)."""
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    # TwiML inline : pas besoin d'heberger de fichier, Twilio genere la
    # voix a la volee. Le <Say> repete + <Pause> laisse le temps de
    # decrocher et d'ecouter avant que l'appel ne se termine.
    twiml = f"""
    <Response>
        <Say language="fr-FR">{CALL_MESSAGE}</Say>
        <Pause length="2"/>
        <Say language="fr-FR">{CALL_MESSAGE}</Say>
    </Response>
    """
    client.calls.create(twiml=twiml, from_=TWILIO_FROM, to=TWILIO_TO)


def notify(url: str):
    if ALERT_MODE in ("sms", "both"):
        send_sms(f"Nouvelle annonce Afedim ! {url}")
    if ALERT_MODE in ("call", "both"):
        for attempt in range(1, CALL_ATTEMPTS + 1):
            make_call()
            print(f"Appel {attempt}/{CALL_ATTEMPTS} declenche")
            if attempt < CALL_ATTEMPTS:
                time.sleep(CALL_GAP_SECONDS)


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

    for listing_id in new_ids:
        url = current[listing_id]
        notify(url)
        print(f"Alerte envoyee ({ALERT_MODE}) pour l'annonce {listing_id}")

    # Memorise toutes les annonces actuellement visibles (pas seulement
    # les nouvelles), pour ne pas re-notifier celles qui restent en ligne.
    save_seen(seen | set(current.keys()))


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print(f"Erreur reseau: {e}", file=sys.stderr)
        sys.exit(1)
