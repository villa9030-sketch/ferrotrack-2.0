"""Gestione dei TOKEN DI DISPOSITIVO (da eseguire sul server, una volta).

I token sono l'identita' verificata dal server: sostituiscono la fiducia nello
`user_id` inviato dal browser. Vanno creati qui e poi inseriti UNA VOLTA nel
tablet / PC ufficio; gli operai non fanno alcun passaggio quotidiano.

Uso:
    python app/tools/device_token.py crea "Tablet officina 1" ore
    python app/tools/device_token.py crea "Tablet reparto piega" reparto
    python app/tools/device_token.py crea "PC ufficio Elena" ufficio
    python app/tools/device_token.py elenco
    python app/tools/device_token.py revoca <id>

Il token in chiaro viene mostrato SOLO alla creazione (nel DB c'e' solo l'hash).
Per configurare un tablet: apri la pagina con ?token=<token> una sola volta,
oppure incollalo nella schermata di configurazione del dispositivo.
"""
import os
import sys

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from backend.models import initialize_database  # noqa: E402
from backend.auth_device import SCOPES, crea_token, elenca_token, revoca_token  # noqa: E402


def _uso():
    print(__doc__)
    return 1


def main(argv):
    if len(argv) < 2:
        return _uso()
    cmd = argv[1].lower()

    # Assicura che le tabelle esistano (idempotente)
    initialize_database()

    if cmd == 'crea':
        if len(argv) < 4:
            print('Uso: crea "<etichetta>" <scope>')
            print('scope ammessi:', ', '.join(SCOPES))
            return 1
        label, scope = argv[2], argv[3]
        res = crea_token(label, scope, created_by='setup-cli')
        if res.get('error'):
            print('ERRORE:', res['error'])
            return 1
        print()
        print('=' * 64)
        print(f"  Dispositivo : {res['label']}")
        print(f"  Scope       : {res['scope']}")
        print(f"  ID          : {res['id']}")
        print()
        print(f"  TOKEN       : {res['token']}")
        print()
        print('  Copialo ORA: non sara' + chr(39) + ' piu' + chr(39) + ' visibile.')
        print('=' * 64)
        return 0

    if cmd == 'elenco':
        rows = elenca_token()
        if not rows:
            print('Nessun token configurato.')
            return 0
        print(f"{'ID':38} {'SCOPE':9} {'ATTIVO':7} ETICHETTA")
        for r in rows:
            print(f"{r['id']:38} {r['scope']:9} {'si' if r['is_active'] else 'no':7} {r['label']}")
        return 0

    if cmd == 'revoca':
        if len(argv) < 3:
            print('Uso: revoca <id>')
            return 1
        res = revoca_token(argv[2], da='setup-cli')
        if res.get('error'):
            print('ERRORE:', res['error'])
            return 1
        print('Token revocato.')
        return 0

    return _uso()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
