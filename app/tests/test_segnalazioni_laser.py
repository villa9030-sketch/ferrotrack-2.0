"""Test: "Segnala un problema" dal tablet arriva anche al LASER (16/10/2026).

Stefano: "devono arrivare al laser". Copre:
 1. per ogni tipo (manca materiale, disegno, da rifare, altro): un avviso
    alla stazione laser (tipo 'segnalazione', da fare) e, come prima, uno
    per ogni postazione d'ufficio (tipo 'order'); le persone no
 2. doppione entro 10 minuti: nessun avviso in piu', ne' all'ufficio ne'
    al laser
 3. GET /api/laser/segnalazioni: le non risolte, per ordine, con id, stato,
    grave (rosso: da rifare, disegno), pezzo, nota, chi
 4. "Visto": stato vista, riga d'audit, avviso del laser letto; di nuovo:
    gia' fatto, nessuna riga in piu'
 5. "Risolto" con "quanti da rifare": audit col numero, sparisce dalle
    aperte, avviso informativo all'ufficio, avvisi del laser tolti quando
    sull'ordine non resta niente; "Riaperto" (l'Annulla) la rimette
 6. chi puo': il laser si; tablet di reparto, ufficio e nessuno no; esiti e
    quantita' non validi, segnalazione inesistente
 7. il tablet vede lo stato delle segnalazioni di oggi

Gira su DATABASE TEMPORANEO. Esecuzione:
    python app/tests/test_segnalazioni_laser.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, AuditLog, Notification  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_segn_laser_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from tests.accesso_aiuto import postazioni, persona, entra, modalita, PIN_UFFICIO  # noqa: E402
from backend import tablet_officina as tab  # noqa: E402

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome} {extra}')


def notifiche(oid, **filtro):
    s = models.SessionLocal()
    try:
        q = s.query(Notification).filter(Notification.order_id == oid)
        for k, v in filtro.items():
            q = q.filter(getattr(Notification, k) == v)
        return q.all()
    finally:
        s.close()


def audit(azione, oid=None):
    s = models.SessionLocal()
    try:
        q = s.query(AuditLog).filter(AuditLog.action == azione)
        if oid:
            q = q.filter(AuditLog.entity_id == oid)
        return q.order_by(AuditLog.timestamp.asc()).all()
    finally:
        s.close()


def main():
    modalita('protetto')
    postazioni()
    persona('uff-pin', 'Persona Ufficio', 'Amministrazione', pin=PIN_UFFICIO)
    s = models.SessionLocal()
    try:
        # una persona del laser: gli avvisi sono della stazione, non suoi
        s.add(User(id='mario', name='Mario', role='Laser', is_active=True))
        oggi = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for i, n in enumerate(('1252', '1300', '1301', '1302')):
            s.add(Order(id=f'o{i}', numero_ordine=n, cliente='DECA', status='RICEVUTO',
                        data_ricezione=datetime.utcnow(), data_consegna=oggi + timedelta(days=5),
                        taglio_richiesto=True, taglio_completato=(i == 0),
                        data_taglio_completato=datetime.utcnow() if i == 0 else None))
        s.commit()
    finally:
        s.close()

    rep = entra(A.app, 'reparto')
    las = entra(A.app, 'laser')
    uff = entra(A.app, 'ufficio', pin=PIN_UFFICIO)
    anon = A.app.test_client()

    print('\n1. Ogni tipo arriva al laser e all\'ufficio')
    tipi = list(tab.TIPI_SEGNALAZIONE)
    for i, tipo in enumerate(tipi):
        oid = f'o{i}'
        corpo = {'tipo': tipo, 'nota': 'serve subito' if tipo == 'altro' else ''}
        r = rep.post(f'/api/orders/{oid}/segnalazioni', json=corpo)
        j = r.get_json() or {}
        check(f'{tipo}: 201, un avviso al laser e uno all\'ufficio',
              r.status_code == 201 and j.get('avvisati_laser') == 1 and j.get('avvisati') == 1, (r.status_code, j))
        nl = notifiche(oid, user_id='postazione-laser')
        check(f'{tipo}: avviso del laser da fare, tipo suo',
              len(nl) == 1 and nl[0].notification_type == 'segnalazione' and nl[0].notification_category == 'attiva'
              and nl[0].title == 'Officina: ' + tab.TIPI_SEGNALAZIONE[tipo].lower(), [(n.notification_type, n.title) for n in nl])
        nu = notifiche(oid, user_id='postazione-amministrazione')
        check(f'{tipo}: all\'ufficio come prima (tipo order, da fare)',
              len(nu) == 1 and nu[0].notification_type == 'order' and nu[0].notification_category == 'attiva', nu)
        check(f'{tipo}: niente alla persona del laser', notifiche(oid, user_id='mario') == [])

    print('\n2. Doppione entro 10 minuti')
    r = rep.post('/api/orders/o2/segnalazioni', json={'tipo': 'da_rifare'})
    check('gia\' segnalato', r.status_code == 200 and r.get_json().get('gia_segnalato') is True)
    check('nessun avviso in piu\' al laser', len(notifiche('o2', user_id='postazione-laser')) == 1)
    check('nessun avviso in piu\' all\'ufficio', len(notifiche('o2', user_id='postazione-amministrazione')) == 1)

    print('\n3. Il laser legge le segnalazioni aperte')
    # pezzo da rifare sull'ordine gia' tagliato (o0, senza distinta: il
    # codice del pezzo non si controlla, come oggi)
    r =rep.post('/api/orders/o0/segnalazioni', json={'tipo': 'da_rifare', 'codice_pezzo': 'A1', 'nota': 'bava sul foro', 'da': 'Luca'})
    check('pezzo da rifare su ordine tagliato: 201', r.status_code == 201, r.get_json())
    check('anonimo: 401', anon.get('/api/laser/segnalazioni').status_code == 401)
    check('tablet di reparto: 403', rep.get('/api/laser/segnalazioni').status_code == 403)
    j = las.get('/api/laser/segnalazioni').get_json()
    od = j.get('ordini') or {}
    check('quattro ordini con segnalazioni', sorted(od) == ['o0', 'o1', 'o2', 'o3'], sorted(od))
    rif = next((x for x in od.get('o0', []) if x['tipo'] == 'da_rifare'), {})
    check('pezzo, nota, chi, quando, id', rif.get('codice_pezzo') == 'A1' and rif.get('nota') == 'bava sul foro'
          and rif.get('da') == 'Luca' and rif.get('quando') and rif.get('id'), rif)
    check('nuova e grave (rosso)', rif.get('stato') == 'nuova' and rif.get('grave') is True, rif)
    mm = (od.get('o0') or [{}])[-1]
    check('manca materiale: non grave (ambra)', mm.get('tipo') == 'manca_materiale' and mm.get('grave') is False, mm)
    dis = (od.get('o1') or [{}])[0]
    check('disegno: grave', dis.get('grave') is True, dis)
    check('l\'ufficio puo\' leggerle', uff.get('/api/laser/segnalazioni').status_code == 200)

    print('\n4. Visto')
    sid = rif['id']
    url = f'/api/segnalazioni-officina/{sid}/gestisci'
    check('tablet di reparto: 403', rep.post(url, json={'esito': 'visto'}).status_code == 403)
    check('ufficio: 403', uff.post(url, json={'esito': 'visto'}).status_code == 403)
    check('anonimo: 401', anon.post(url, json={'esito': 'visto'}).status_code == 401)
    check('esito non valido: 400', las.post(url, json={'esito': 'boh'}).status_code == 400)
    check('nessun corpo: 400', las.post(url, data='x').status_code == 400)
    check('segnalazione inesistente: 404',
          las.post('/api/segnalazioni-officina/nessuna/gestisci', json={'esito': 'visto'}).status_code == 404)
    r = las.post(url, json={'esito': 'visto'})
    j = r.get_json() or {}
    check('visto: 200, stato vista', r.status_code == 200 and (j.get('segnalazione') or {}).get('stato') == 'vista', j)
    g = audit(tab.AZIONE_GESTITA, 'o0')
    check('riga d\'audit "visto"', len(g) == 1 and f'segnalazione: {sid}' in g[0].detail and 'esito: visto' in g[0].detail,
          [x.detail for x in g])
    check('avvisi del laser su o0: letti', all(n.is_read for n in notifiche('o0', user_id='postazione-laser')))
    r = las.post(url, json={'esito': 'visto'})
    check('visto di nuovo: gia\' fatto, niente audit in piu\'',
          r.get_json().get('gia_fatto') is True and len(audit(tab.AZIONE_GESTITA, 'o0')) == 1)
    vis = next(x for x in las.get('/api/laser/segnalazioni').get_json()['ordini']['o0'] if x['id'] == sid)
    check('nell\'elenco: vista, con chi e quando', vis['stato'] == 'vista' and vis.get('visto_il') and vis.get('visto_da'), vis)

    print('\n5. Risolto, con quanti da rifare')
    check('quanti non valido (0): 400', las.post(url, json={'esito': 'risolto', 'quanti': 0}).status_code == 400)
    check('quanti non valido (testo): 400', las.post(url, json={'esito': 'risolto', 'quanti': 'tre'}).status_code == 400)
    r = las.post(url, json={'esito': 'risolto', 'quanti': 3, 'nota': 'rimesso in Lantek'})
    j = r.get_json() or {}
    check('risolto: 200, stato risolta', r.status_code == 200 and j['segnalazione']['stato'] == 'risolta'
          and j['segnalazione']['quanti'] == 3, j)
    g = audit(tab.AZIONE_GESTITA, 'o0')
    check('riga d\'audit "risolto" con il numero', len(g) == 2 and 'esito: risolto' in g[1].detail and 'quanti: 3' in g[1].detail,
          [x.detail for x in g])
    od = las.get('/api/laser/segnalazioni').get_json()['ordini']
    check('non e\' piu\' fra le aperte', all(x['id'] != sid for x in od.get('o0', [])), od.get('o0'))
    nu = notifiche('o0', user_id='postazione-amministrazione', notification_type='segnalazione_risolta')
    check('l\'ufficio sa che e\' risolto (informativo)', len(nu) == 1 and nu[0].notification_category == 'informativa'
          and 'pezzo A1' in nu[0].message and '3 da rifare' in nu[0].message and 'rimesso in Lantek' in nu[0].message,
          [(n.title, n.message) for n in nu])
    # sull'ordine o0 resta "manca materiale": i suoi avvisi del laser restano
    check('resta un problema aperto: gli avvisi del laser restano',
          len(notifiche('o0', user_id='postazione-laser', is_deleted=False)) == 2)
    altra = next(x for x in od['o0'] if x['tipo'] == 'manca_materiale')
    r = las.post(f'/api/segnalazioni-officina/{altra["id"]}/gestisci', json={'esito': 'risolto'})
    check('risolto senza quanti: 200', r.status_code == 200 and r.get_json()['aperte_ordine'] == 0, r.get_json())
    check('niente piu\' di aperto: avvisi del laser tolti',
          notifiche('o0', user_id='postazione-laser', is_deleted=False) == [])
    check('o0 sparisce dalle segnalazioni del laser', 'o0' not in las.get('/api/laser/segnalazioni').get_json()['ordini'])
    r = rep.post('/api/orders/o3/segnalazioni', json={'tipo': 'altro', 'nota': 'serve subito'})
    check('non risolto, entro 10 minuti: ancora doppione', r.get_json().get('gia_segnalato') is True)
    r = rep.post('/api/orders/o0/segnalazioni', json={'tipo': 'da_rifare', 'codice_pezzo': 'A1'})
    check('dopo "Risolto" lo stesso problema torna: non e\' un doppione',
          r.status_code == 201 and r.get_json().get('gia_segnalato') is False, r.get_json())
    nuova = [x for x in las.get('/api/laser/segnalazioni').get_json()['ordini'].get('o0', []) if x['id'] != sid]
    check('ed e\' di nuovo al laser', len(nuova) == 1 and nuova[0]['stato'] == 'nuova', nuova)
    las.post(f'/api/segnalazioni-officina/{nuova[0]["id"]}/gestisci', json={'esito': 'risolto'})

    print('\n6. Riaperto (Annulla)')
    r = las.post(url, json={'esito': 'riaperto'})
    check('riaperto: torna nuova', r.status_code == 200 and r.get_json()['segnalazione']['stato'] == 'nuova', r.get_json())
    od = las.get('/api/laser/segnalazioni').get_json()['ordini']
    check('di nuovo fra le aperte', any(x['id'] == sid for x in od.get('o0', [])))
    check('audit: visto, risolto, risolto, risolto, riaperto', len(audit(tab.AZIONE_GESTITA)) == 5)

    print('\n7. Il tablet vede lo stato')
    r = las.post(f'/api/segnalazioni-officina/{dis["id"]}/gestisci', json={'esito': 'risolto'})
    so = A._segnalazioni_di_oggi(['o1']).get('o1', [])
    check('segnalazione di oggi con stato risolta', so and so[0]['stato'] == 'risolta' and so[0].get('risolto_da'), so)

    print('\n8. Funzioni pure')
    d = tab.dettaglio_gestione('abc', 'risolto', 2, 'ok / fatto', 'Laser')
    g = tab.leggi_gestione(d)
    check('gestione: andata e ritorno', g == {'segnalazione_id': 'abc', 'esito': 'risolto', 'quanti': 2,
                                               'nota': 'ok / fatto', 'da': 'Laser'}, g)
    check('gestione: testo estraneo', tab.leggi_gestione('LOGIN ok') is None)
    st = tab.stato_segnalazione([{'esito': 'visto', 'da': 'L', 'quando': 't1'},
                                 {'esito': 'risolto', 'da': 'L', 'quando': 't2', 'quanti': 1},
                                 {'esito': 'riaperto', 'da': 'L', 'quando': 't3'},
                                 {'esito': 'visto', 'da': 'M', 'quando': 't4'}])
    check('stato: conta l\'ultimo passo', st['stato'] == 'vista' and st['visto_da'] == 'M' and st['quanti'] is None, st)


try:
    main()
except Exception as e:
    import traceback
    traceback.print_exc()
    KO.append(f'eccezione: {e}')
finally:
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass

print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
for k in KO:
    print('  -', k)
sys.exit(0 if not KO else 1)
