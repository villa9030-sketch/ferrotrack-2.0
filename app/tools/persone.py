"""Persone, PIN e dispositivi dal PC del server (riga di comando).

Serve soprattutto il primo giorno: il PRIMO amministratore si crea da qui,
perche' dalle pagine un amministratore lo crea solo un altro amministratore.
E serve in emergenza (tutti gli amministratori hanno dimenticato il PIN, un
dispositivo va registrato senza poter usare la pagina iniziale).

Uso (dalla cartella app, col Python dell'installazione):

    .venv\\Scripts\\python.exe tools\\persone.py elenco
    .venv\\Scripts\\python.exe tools\\persone.py crea "Elena Rossi" --ruolo Amministrazione
    .venv\\Scripts\\python.exe tools\\persone.py crea "Mario Bianchi" --ruolo Laser
    .venv\\Scripts\\python.exe tools\\persone.py crea "Luca Verdi" --ruolo Commerciale --no-admin
    .venv\\Scripts\\python.exe tools\\persone.py pin elena-rossi          (rimette il PIN)
    .venv\\Scripts\\python.exe tools\\persone.py admin luca-verdi si|no
    .venv\\Scripts\\python.exe tools\\persone.py disattiva luca-verdi | attiva luca-verdi

    .venv\\Scripts\\python.exe tools\\persone.py dispositivi
    .venv\\Scripts\\python.exe tools\\persone.py dispositivo "PC ufficio" ufficio
    .venv\\Scripts\\python.exe tools\\persone.py revoca <id>

    .venv\\Scripts\\python.exe tools\\persone.py modalita                 (mostra)
    .venv\\Scripts\\python.exe tools\\persone.py modalita protetto|transizione

    .venv\\Scripts\\python.exe tools\\persone.py cartella elenco
    .venv\\Scripts\\python.exe tools\\persone.py cartella aggiungi "D:\\Commesse"
    .venv\\Scripts\\python.exe tools\\persone.py cartella togli "D:\\Commesse"

Ruoli: Amministrazione, Commerciale, Laser, Operaio. Amministrazione e Laser
nascono amministratori (si cambia con --no-admin o col comando "admin").
Il PIN si chiede a video e non si vede mentre si scrive; --pin 1234 lo passa
direttamente (resta nella cronologia del terminale: usarlo solo per prove).

Stazioni per "dispositivo": commerciale, ufficio, laser, reparto (Tablet
officina), ore (Timbratrice). Stampa un indirizzo da aprire UNA volta sul
dispositivo: il codice diventa il suo cookie e sparisce dalla barra.
"""
import argparse
import getpass
import os
import socket
import sys

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

for _flusso in (sys.stdout, sys.stderr):
    try:
        _flusso.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def _chiedi_pin(args) -> str:
    from backend.accesso import errore_formato_pin
    if args.pin:
        pin = str(args.pin).strip()
    else:
        pin = getpass.getpass('PIN (da 4 a 8 cifre): ').strip()
        if getpass.getpass('Ripeti il PIN: ').strip() != pin:
            raise SystemExit('I due PIN non coincidono.')
    err = errore_formato_pin(pin)
    if err:
        raise SystemExit(err)
    return pin


def _registro(azione, dettaglio, entita='user', entita_id=''):
    from backend.database import AuditManager
    try:
        AuditManager.log(user_id=None, user_name='Riga di comando (server)', action=azione,
                         entity_type=entita, entity_id=entita_id, detail=dettaglio)
    except Exception:
        pass


def _persona(s, pid):
    from backend.models import User
    u = s.get(User, pid)
    if u is None or u.e_postazione:
        raise SystemExit('Persona non trovata: %s (vedi "elenco")' % pid)
    return u


def cmd_elenco(args):
    from backend.database import get_session
    from backend.models import User
    s = get_session()
    try:
        righe = [u for u in s.query(User).all() if not u.e_postazione]
        righe.sort(key=lambda u: (not u.is_active, not u.pin_hash, (u.name or '').lower()))
        print('%-24s %-28s %-15s %-6s %-6s %s' % ('ID', 'NOME', 'RUOLO', 'PIN', 'ADMIN', 'ATTIVA'))
        for u in righe:
            print('%-24s %-28s %-15s %-6s %-6s %s' % (
                u.id, (u.name or '')[:28], u.role or '', 'si' if u.pin_hash else '-',
                'si' if u.e_admin else '-', 'si' if u.is_active else 'no'))
        n_admin = sum(1 for u in righe if u.is_active and u.pin_hash and u.e_admin)
        print('\nAmministratori con PIN: %d%s' % (
            n_admin, '  <-- ne servono almeno due' if n_admin < 2 else ''))
    finally:
        s.close()
    return 0


def cmd_crea(args):
    from backend.api_accesso import crea_persona
    from backend.accesso import RUOLI_PERSONA
    if args.ruolo not in RUOLI_PERSONA:
        raise SystemExit('Ruolo sconosciuto. Ammessi: %s' % ', '.join(RUOLI_PERSONA))
    pin = _chiedi_pin(args)
    admin = None if args.admin is None else args.admin
    esito, _codice = crea_persona(args.nome, args.ruolo, admin=admin, pin=pin)
    if not esito.get('success'):
        raise SystemExit('ERRORE: %s' % esito.get('error'))
    p = esito['persona']
    _registro('PERSONA_CREATA', '%s (%s) da riga di comando%s' % (
        p['nome'], p['ruolo'], ', amministratore' if p['admin'] else ''), entita_id=p['id'])
    print('Creata: %s  (id %s, %s%s)' % (p['nome'], p['id'], p['ruolo'],
                                        ', amministratore' if p['admin'] else ''))
    return 0


def cmd_pin(args):
    from backend import accesso as A
    from backend.database import get_session
    pin = _chiedi_pin(args)
    s = get_session()
    try:
        u = _persona(s, args.id)
        if A.pin_gia_usato(s, pin, tranne_id=u.id):
            raise SystemExit('Questo PIN non si puo\' usare: scegline un altro.')
        u.pin_hash = A.impronta_pin(pin)
        u.pin_impostato_il = A.adesso()
        s.commit()
        nome = u.name
    finally:
        s.close()
    A.chiudi_sessioni(user_id=args.id)
    _registro('PIN_IMPOSTATO', '%s da riga di comando' % nome, entita_id=args.id)
    print('PIN impostato per %s.' % nome)
    return 0


def cmd_admin(args):
    from backend.database import get_session
    from backend import accesso as A
    s = get_session()
    try:
        u = _persona(s, args.id)
        vuole = args.valore.lower() in ('si', 'sì', 'yes', '1', 'true')
        if not vuole and u.e_admin and not any(x.e_admin and x.id != u.id for x in A.persone_con_pin(s)):
            raise SystemExit("E' l'unico amministratore: prima nominane un altro.")
        u.e_admin = vuole
        s.commit()
        nome = u.name
    finally:
        s.close()
    _registro('PERSONA_MODIFICATA', '%s: amministratore %s (riga di comando)' % (nome, 'si' if vuole else 'no'),
              entita_id=args.id)
    print('%s: amministratore %s.' % (nome, 'si' if vuole else 'no'))
    return 0


def _attiva(args, attiva: bool):
    from backend.database import get_session
    from backend import accesso as A
    s = get_session()
    try:
        u = _persona(s, args.id)
        if not attiva and u.e_admin and u.pin_hash and not any(
                x.e_admin and x.id != u.id for x in A.persone_con_pin(s)):
            raise SystemExit("E' l'unico amministratore: prima nominane un altro.")
        u.is_active = attiva
        s.commit()
        nome = u.name
    finally:
        s.close()
    if not attiva:
        A.chiudi_sessioni(user_id=args.id)
    _registro('PERSONA_MODIFICATA', '%s %s (riga di comando)' % (nome, 'attivata' if attiva else 'disattivata'),
              entita_id=args.id)
    print('%s %s.' % (nome, 'attivata' if attiva else 'disattivata'))
    return 0


def cmd_dispositivi(args):
    from backend.database import get_session
    from backend.models_ore import DeviceToken
    from backend.accesso import STAZIONI
    s = get_session()
    try:
        righe = s.query(DeviceToken).order_by(DeviceToken.is_active.desc(), DeviceToken.created_at).all()
        print('%-38s %-16s %-8s %s' % ('ID', 'STAZIONE', 'ATTIVO', 'NOME'))
        for r in righe:
            print('%-38s %-16s %-8s %s' % (r.id, (STAZIONI.get(r.scope) or {}).get('nome', r.scope),
                                           'si' if r.is_active else 'no', r.label))
    finally:
        s.close()
    return 0


def _ip_lan() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.connect(('192.168.1.1', 80))
            return so.getsockname()[0]
    except OSError:
        return 'localhost'


def cmd_dispositivo(args):
    from backend.accesso import STAZIONI, registra_dispositivo
    if args.stazione not in STAZIONI:
        raise SystemExit('Stazione sconosciuta. Ammesse: %s' % ', '.join(STAZIONI))
    segreto, disp = registra_dispositivo(args.stazione, args.nome, da='riga-di-comando')
    _registro('DISPOSITIVO_REGISTRATO', '%s (%s) da riga di comando' % (disp['nome'], args.stazione),
              entita='dispositivo', entita_id=disp['id'])
    porta = os.environ.get('FERROTRACK_PORT') or '5000'
    print()
    print('=' * 70)
    print('  Dispositivo : %s (%s)' % (disp['nome'], STAZIONI[args.stazione]['nome']))
    print('  Apri UNA volta, sul dispositivo, questo indirizzo:')
    print()
    print('    http://%s:%s/?codice=%s' % (_ip_lan(), porta, segreto))
    print()
    print('  (sul PC del server stesso: http://localhost:%s/?codice=...)' % porta)
    print('  Non sara\' piu\' mostrato. Se lo perdi: revoca e rifai.')
    print('=' * 70)
    return 0


def cmd_revoca(args):
    from backend.auth_device import revoca_token
    from backend.accesso import chiudi_sessioni
    res = revoca_token(args.id, da='riga-di-comando')
    if res.get('error'):
        raise SystemExit('ERRORE: %s' % res['error'])
    chiudi_sessioni(device_id=args.id)
    _registro('REVOCA_DEVICE_TOKEN', 'da riga di comando', entita='dispositivo', entita_id=args.id)
    print('Dispositivo revocato: da adesso non entra piu\'.')
    return 0


def cmd_modalita(args):
    from backend import accesso as A
    if not args.valore:
        print('Modalita\' attuale: %s' % A.modalita())
        return 0
    esito = A.imposta_modalita(args.valore)
    if esito.get('error'):
        raise SystemExit('ERRORE: %s' % esito['error'])
    _registro('MODALITA_ACCESSO', '%s (riga di comando)' % args.valore, entita='accesso')
    print('Modalita\' impostata: %s. Vale subito, senza riavviare.' % args.valore)
    return 0


def cmd_cartella(args):
    from backend.database import ConfigManager, cartelle_consentite
    lista = cartelle_consentite()
    if args.azione == 'elenco':
        print('\n'.join(lista) if lista else 'Nessuna cartella consentita.')
        return 0
    if not args.percorso:
        raise SystemExit('Manca il percorso.')
    p = os.path.normpath(args.percorso.strip().strip('"'))
    if args.azione == 'aggiungi':
        if not os.path.isabs(p):
            raise SystemExit('Serve un percorso completo, es. D:\\Commesse')
        if p not in lista:
            lista.append(p)
    elif args.azione == 'togli':
        lista = [x for x in lista if os.path.normcase(x) != os.path.normcase(p)]
    esito = ConfigManager.save_riservata({'cartelle_disegni_consentite': lista})
    if esito.get('error'):
        raise SystemExit('ERRORE: %s' % esito['error'])
    print('Cartelle consentite: %s' % ('; '.join(lista) or 'nessuna'))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='Persone, PIN e dispositivi di FerroTrack',
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('elenco')
    c = sub.add_parser('crea')
    c.add_argument('nome')
    c.add_argument('--ruolo', default='Amministrazione')
    c.add_argument('--admin', dest='admin', action='store_true', default=None)
    c.add_argument('--no-admin', dest='admin', action='store_false')
    c.add_argument('--pin')
    c = sub.add_parser('pin')
    c.add_argument('id')
    c.add_argument('--pin')
    c = sub.add_parser('admin')
    c.add_argument('id')
    c.add_argument('valore')
    for nome in ('disattiva', 'attiva', 'revoca'):
        sub.add_parser(nome).add_argument('id')
    sub.add_parser('dispositivi')
    c = sub.add_parser('dispositivo')
    c.add_argument('nome')
    c.add_argument('stazione')
    c = sub.add_parser('modalita')
    c.add_argument('valore', nargs='?')
    c = sub.add_parser('cartella')
    c.add_argument('azione', choices=('elenco', 'aggiungi', 'togli'))
    c.add_argument('percorso', nargs='?')
    args = ap.parse_args(argv)

    if os.environ.get('FERROTRACK_SKIP_DB_INIT') != '1':
        # Se il server e' gia' partito con questa versione il database e'
        # pronto: non si rifanno le migrazioni (ognuna fa una copia del
        # database prima di partire). Altrimenti si preparano, come all'avvio.
        from sqlalchemy import inspect
        from backend import models
        insp = inspect(models.engine)
        pronto = ('sessioni_accesso' in insp.get_table_names()
                  and 'pin_hash' in [c['name'] for c in insp.get_columns('users')])
        if not pronto:
            models.initialize_database()

    return {
        'elenco': cmd_elenco, 'crea': cmd_crea, 'pin': cmd_pin, 'admin': cmd_admin,
        'disattiva': lambda a: _attiva(a, False), 'attiva': lambda a: _attiva(a, True),
        'dispositivi': cmd_dispositivi, 'dispositivo': cmd_dispositivo, 'revoca': cmd_revoca,
        'modalita': cmd_modalita, 'cartella': cmd_cartella,
    }[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
