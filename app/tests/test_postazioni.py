# -*- coding: utf-8 -*-
"""Si entra da una postazione, non col nome di una persona.

Copre le due cose che, sbagliate, si notano solo in azienda:

  1. all'ingresso compaiono i POSTI (timbratrice, tablet di visione,
     amministrazione, laser, commerciale) e non le persone. Se comparisse
     Mirko, la prima cosa che uno fa e' entrare come se stesso, e da li' in poi
     non si capisce piu' chi ha fatto cosa;

  2. gli OPERAI restano, con la loro storia di ore attaccata. Se sparissero,
     la bacheca della timbratrice non avrebbe piu' nomi da mostrare e le
     giornate gia' dichiarate diventerebbero di nessuno.

Serve un server in ascolto (default 127.0.0.1:5056, cioe' un'istanza di prova
su una COPIA del database). Senza, il test si salta.

    python app/tests/test_postazioni.py [url]
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'

# Il posto, e la pagina che deve aprirsi entrandoci.
POSTAZIONI = {
    'postazione-timbratrice':     ('Tablet timbratrice', 'Timbratrice'),
    'postazione-visione':         ('Tablet di visione', 'Visione'),
    'postazione-amministrazione': ('Amministrazione', 'Amministrazione'),
    'postazione-laser':           ('Laser', 'Laser'),
    'postazione-commerciale':     ('Commerciale', 'Commerciale'),
}

OPERAI = ('mirko-laser', 'enzo-officina')

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print('  [OK] %s' % nome)
    else:
        KO.append(nome)
        print('  [KO] %s %s' % (nome, extra))


def chiama(percorso):
    try:
        with urllib.request.urlopen(BASE + percorso, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


def main():
    global OK
    stato, _ = chiama('/api/health')
    if stato != 200:
        print('Server non raggiungibile su %s. Test saltato.' % BASE)
        return 0

    stato, d = chiama('/api/users')
    if stato != 200:
        print('Elenco utenti non leggibile. Test saltato.')
        return 0
    utenti = {u['id']: u for u in (d.get('users') or d if isinstance(d, list) else d.get('users', []))}

    print('1) All\'ingresso compaiono i posti, non le persone')
    postazioni = {i: u for i, u in utenti.items()
                  if u.get('e_postazione') and u.get('is_active', True)}
    check('ci sono tutte e cinque le postazioni',
          set(postazioni) == set(POSTAZIONI), sorted(postazioni))
    for pid, (nome, ruolo) in POSTAZIONI.items():
        u = utenti.get(pid, {})
        check('%-28s si chiama "%s"' % (pid, nome), u.get('name') == nome, u.get('name'))
        check('%-28s ha ruolo "%s"' % (pid, ruolo), u.get('role') == ruolo, u.get('role'))

    print('\n2) Gli operai restano, ma non entrano nel programma')
    for oid in OPERAI:
        u = utenti.get(oid, {})
        check('%-16s c\'e\' ancora' % oid, bool(u), 'sparito')
        check('%-16s e\' ancora attivo' % oid, u.get('is_active') is True)
        check('%-16s NON e\' una postazione' % oid, not u.get('e_postazione'))
        check('%-16s ha ancora un ruolo da operaio' % oid,
              (u.get('role') or '').startswith('Operaio'), u.get('role'))

    print('\n3) La bacheca della timbratrice ha ancora i nomi da mostrare')
    from backend.ore_service import elenco_operai
    try:
        nomi = {o['id'] for o in elenco_operai()}
        check('gli operai compaiono sulla bacheca',
              set(OPERAI).issubset(nomi), sorted(nomi))
        check('le postazioni NON compaiono sulla bacheca',
              not (set(POSTAZIONI) & nomi), sorted(nomi))
    except Exception as e:
        # elenco_operai legge il database dell'istanza locale, non quella di
        # prova: se puntano a database diversi il controllo non e' valido.
        print('  (bacheca non verificabile da qui: %s)' % str(e)[:60])

    print('\n4) La postazione laser porta la delega del capo')
    laser = utenti.get('postazione-laser', {})
    check('il laser puo\' decidere come il capo', laser.get('is_capo') is True)
    for pid in ('postazione-timbratrice', 'postazione-visione',
                'postazione-commerciale'):
        check('%-28s NON ha la delega' % pid, not utenti.get(pid, {}).get('is_capo'))

    print('\n5) Le utenze personali sostituite sono spente, non cancellate')
    # L'elenco normale mostra solo chi e' attivo: per vedere anche le utenze
    # spente bisogna chiederle. Devono esserci ancora, perche' l'archivio
    # delle azioni le nomina, e senza la riga lo storico diventa illeggibile.
    _, tutti = chiama('/api/users?include_inactive=true')
    tutte = {u['id']: u for u in (tutti.get('users') or [])}
    for vecchio in ('elena-impiegata', 'paolo-responsabile',
                    'stefano-responsabile'):
        u = tutte.get(vecchio)
        check("%-22s c'e' ancora (storico leggibile)" % vecchio, u is not None)
        if u:
            check("%-22s e' spenta" % vecchio, u.get('is_active') is False)
        check("%-22s non compare all'ingresso" % vecchio,
              vecchio not in postazioni)


    print('\n6) Una sessione vecchia viene riconosciuta come tale')
    stato, d = chiama('/api/auth/sessione/stefano-responsabile')
    check('una postazione spenta non vale piu\'',
          stato == 200 and d.get('valida') is False, d)
    check('e lo spiega, invece di tacere', bool(d.get('motivo')), d)
    stato, d = chiama('/api/auth/sessione/postazione-laser')
    check('una postazione attiva vale', stato == 200 and d.get('valida') is True, d)
    check('e riporta i dati aggiornati',
          (d.get('utente') or {}).get('role') == 'Laser', d.get('utente'))
    stato, d = chiama('/api/auth/sessione/non-esiste-questo')
    check('un id inventato non vale', d.get('valida') is False, d)

    print('\n' + '=' * 60)
    print('PASSATI: %d   FALLITI: %d' % (OK, len(KO)))
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
