"""Il pacco di taglio della pagina laser, parte server (backend/api_pacchi.py).

Copre:
 1. tabelle nuove (pacchi_taglio, pacco_ordini): nascono da sole su un
    database che non le ha, e rifarlo non cambia niente (idempotente)
 2. crea: nasce PROPOSTO, numero progressivo; chi puo' e chi no; un ordine
    in UN solo pacco aperto; dentro solo ordini del laser (da mandare o in Lantek)
 3. aggiungi / togli ordini; l'ultimo non si toglie (si scioglie il pacco)
 4. stati: proposto -> confermato (e Annulla), passaggi non ammessi
 5. riepilogo per lamiera: ordini, pezzi, kg, minuti, tutti insieme
 6. "In Lantek" solo quando gli ordini sono in Lantek: invio con Lantek
    SIMULATO (il "Manda a Lantek" di sempre, ordine per ordine), con un
    codice tenuto fuori "da correggere in Lantek"
 7. "Segna tagliato" per lamiera e per pacco: lamiere segnate negli ordini,
    taglio fatto dell'ordine con tutte le lamiere tagliate (-> "Forse finiti"
    dell'ufficio), l'ordine con un codice da correggere resta da tagliare;
    Annulla rimette esattamente com'era
 8. sciogli e rimetti su; un ordine si manda ancora da solo; storico

Database TEMPORANEO, Lantek SIMULATO: nessun programma di Lantek parte.
Esecuzione: python app/tests/test_pacchi_taglio.py
"""
import os
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_pacchi_esempi_')
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_pacchi_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome}  {extra}')


print('1) Tabelle nuove su un database che non le ha')
Base.metadata.create_all(bind=_ENG)
with _ENG.begin() as c:
    c.execute(text('DROP TABLE pacco_ordini'))
    c.execute(text('DROP TABLE pacchi_taglio'))
check('database "vecchio": senza i pacchi', 'pacchi_taglio' not in inspect(_ENG).get_table_names())
Base.metadata.create_all(bind=_ENG)
nomi = inspect(_ENG).get_table_names()
check('all\'avvio nascono pacchi_taglio e pacco_ordini', 'pacchi_taglio' in nomi and 'pacco_ordini' in nomi, nomi)
Base.metadata.create_all(bind=_ENG)
check('rifarlo non cambia niente', sorted(inspect(_ENG).get_table_names()) == sorted(nomi))
col = {c['name'] for c in inspect(_ENG).get_columns('pacchi_taglio')}
check('colonne del pacco', {'id', 'numero', 'stato', 'note', 'creato_il', 'creato_da', 'aggiornato_il',
                            'confermato_il', 'in_lantek_il', 'tagliato_il'} <= col, col)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_pacchi_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

# ── Lantek simulato ─────────────────────────────────────────────────────
TL._leggi_lantek = lambda codici: None


def _pz(mat, sp):
    return {'esiste': True, 'materiale': mat, 'spessore': sp, 'revisioni': []}


PEZZI_LANTEK = {'A1-FE3-00': _pz('FERRO', 3.0), 'A1-IX2-00': _pz('INOX', 2.0),
                'B2-FE3-00': _pz('FERRO', 3.0), 'B2-SPESS-00': _pz('FERRO', 2.0),   # ordine 3 mm, Lantek 2: tenuto fuori
                'C3-FE5-00': _pz('FERRO', 5.0), 'D4-FE3-00': _pz('FERRO', 3.0)}
CHIAMATE = []
ORDINI_LT = {}


def pezzi_in_lantek(codici):
    vuoto = {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': []}
    return {'disponibile': True, 'errore': None,
            'pezzi': {c: dict(PEZZI_LANTEK[c], codice_lantek=c) if c in PEZZI_LANTEK else dict(vuoto) for c in codici}}


def xml_finto(contenuto, nome, cartella, attesa_s=300):
    CHIAMATE.append(('xml', contenuto))
    for c in PEZZI_LANTEK:
        if ('"%s"' % c).encode() in contenuto:
            ORDINI_LT[c] = 1.0
    return {'eseguito': True, 'errore': None, 'file_rapporto': 'x',
            'rapporto': {'totale': 1, 'ok': 1, 'avvisi': 0, 'con_errore': 0, 'errori': []}}


def mai(*a, **k):
    raise AssertionError('i pezzi nuovi non servono in questo test')


L.pezzi_in_lantek = pezzi_in_lantek
L.clienti_lantek = lambda: ['PROVA']
L.ordini_in_lantek = lambda commessa: dict(ORDINI_LT)
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.materiali_lantek_noti = lambda: {'FERRO', 'INOX'}
L.xmlimporter_disponibile = lambda: True
L.importa_pezzi_dxf = mai
L.importa_xml = xml_finto
L.expert_aperto = lambda: False   # Lantek Expert chiuso (sul PC vero puo' essere aperto)
from tests.lantek_finto import usa as _lantek_finto  # noqa: E402
_lantek_finto(L)   # dopo l'invio Lantek "ha creato" gli ordini dell'XML


def ordine(cliente, numero, righe, smista=True):
    """righe: [(codice, materiale, spessore, quantita)]"""
    pr = PreventivoManager.create(cliente, 'paolo', quantita=1, numero_ordine_cliente=numero)
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        {'codice': c, 'quantita': q, 'materiale': m, 'spessore_mm': sp, 'costo_base_override': 5,
         'perimetro_taglio_m': 0.8, 'n_forature': 2, 'area_dm2': 2.0, 'bbox_w_mm': 200, 'bbox_h_mm': 100}
        for c, m, sp, q in righe])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit()
    s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    if smista:
        A.OrderManager.smista(oid, True, user_id='laser')
    return oid


def manda(cl, oid, tenuti_fuori=()):
    """Come la finestra "Manda a Lantek": cio' che si vede, meno i tenuti fuori;
    /importato solo se e' partito tutto (laser-lantek.js)."""
    inv = cl.get(f'/api/orders/{oid}/laser?fresco=1').get_json()['invio']
    codici = [c for c in inv['codici'] if c not in tenuti_fuori]
    r = cl.post(f'/api/orders/{oid}/lantek-invia', json={'codici': codici, 'nuovi': inv['nuovi'],
                                                          'esclusi': list(tenuti_fuori)})
    if r.status_code == 200 and not tenuti_fuori:
        cl.post(f'/api/orders/{oid}/importato', json={'annulla': False})
    return r


def ordine_db(oid):
    s = models.SessionLocal()
    try:
        return s.query(models.Order).filter(models.Order.id == oid).first()
    finally:
        s.close()


def main():
    from tests.accesso_aiuto import postazioni, entra, modalita, persona
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit()
    s.close()
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')
    rep = entra(A.app, 'reparto')
    persona('elena', 'Elena Ufficio', pin='7913')
    uff = entra(A.app, 'ufficio', pin='7913')

    o1 = ordine('Arredi Prova', '5001', [('A1-FE3-00', 'S235', 3, 4), ('A1-IX2-00', 'INOX', 2, 2)])
    o2 = ordine('Officine Prova', '5002', [('B2-FE3-00', 'S235', 3, 6), ('B2-SPESS-00', 'S235', 3, 1)])
    o3 = ordine('Telai Prova', '5003', [('C3-FE5-00', 'S235', 5, 3)])
    o4 = ordine('Da smistare', '5004', [('D4-FE3-00', 'S235', 3, 1)], smista=False)

    print('\n2) Crea un pacco')
    check('il tablet officina non crea pacchi', rep.post('/api/pacchi', json={'ordini': [o1]}).status_code in (401, 403))
    check("l'ufficio non crea pacchi (li guarda)", uff.post('/api/pacchi', json={'ordini': [o1]}).status_code in (401, 403))
    check("l'ufficio li guarda", uff.get('/api/pacchi').status_code == 200)
    check('senza ordini: 400', laser.post('/api/pacchi', json={'ordini': []}).status_code == 400)
    r = laser.post('/api/pacchi', json={'ordini': [o4]})
    check('un ordine da smistare non entra', r.status_code == 409 and r.get_json().get('codice') == 'non_ammesso', r.get_json())
    r = laser.post('/api/pacchi', json={'ordini': [o1, o2]})
    p = (r.get_json() or {}).get('pacco') or {}
    check('nasce PROPOSTO, numero 1, coi due ordini',
          r.status_code == 201 and p.get('stato') == 'proposto' and p.get('numero') == 1 and p.get('ordini') == [o1, o2],
          r.get_json())
    check('la parola dello stato', p.get('parola') == 'Proposto' and p.get('nome') == 'Pacco 1')
    pid = p['id']
    r = laser.post('/api/pacchi', json={'ordini': [o2, o3]})
    j = r.get_json() or {}
    check('un ordine in UN solo pacco aperto: 409, dice quale',
          r.status_code == 409 and j.get('codice') == 'in_altro_pacco' and 'pacco 1' in j.get('error', ''), j)
    check('niente pacco nuovo creato', len(laser.get('/api/pacchi').get_json()['pacchi']) == 1)

    print('\n3) Aggiungi e togli')
    u = f'/api/pacchi/{pid}/ordini'
    r = laser.post(u, json={'aggiungi': [o3]})
    check('aggiunto il terzo', r.status_code == 200 and r.get_json()['pacco']['ordini'] == [o1, o2, o3], r.get_json())
    r = laser.post(u, json={'togli': [o3]})
    check('tolto di nuovo', r.status_code == 200 and r.get_json()['pacco']['ordini'] == [o1, o2])
    r = laser.post(u, json={'togli': [o1, o2]})
    check("l'ultimo non si toglie: si scioglie il pacco", r.status_code == 409 and r.get_json().get('codice') == 'ultimo')
    check('aggiungere uno da smistare: no', laser.post(u, json={'aggiungi': [o4]}).status_code == 409)

    print('\n4) Stati')
    us = f'/api/pacchi/{pid}/stato'
    r = laser.post(us, json={'stato': 'tagliato'})
    check('tagliato non si mette con /stato', r.status_code == 409, r.get_json())
    r = laser.post(us, json={'stato': 'in_lantek'})
    check('da proposto non si salta a In Lantek', r.status_code == 409 and r.get_json().get('codice') == 'passaggio')
    r = laser.post(us, json={'stato': 'confermato'})
    check('confermato', r.status_code == 200 and r.get_json()['pacco']['stato'] == 'confermato'
          and r.get_json()['pacco']['confermato_il'], r.get_json())
    r = laser.post(us, json={'stato': 'proposto'})
    check('Annulla: di nuovo proposto', r.status_code == 200 and r.get_json()['pacco']['stato'] == 'proposto'
          and not r.get_json()['pacco']['confermato_il'])
    check('confermato di nuovo', laser.post(us, json={'stato': 'confermato'}).get_json()['pacco']['stato'] == 'confermato')
    check('il tablet officina non cambia lo stato', rep.post(us, json={'stato': 'proposto'}).status_code in (401, 403))

    print('\n5) Riepilogo per lamiera')
    r = laser.get(f'/api/pacchi/{pid}')
    d = r.get_json() or {}
    lam = {x['chiave']: x for x in d.get('lamiere') or []}
    check('due lamiere: S235 3 mm (due ordini) e Inox 2 mm', sorted(lam) == ['INOX|2', 'S235|3'], sorted(lam))
    f3 = lam.get('S235|3') or {}
    check('S235 3 mm: 2 ordini, 11 pezzi, 3 codici', f3.get('n_ordini') == 2 and f3.get('n_pezzi') == 11
          and f3.get('n_codici') == 3, f3)
    check('kg: area x spessore x densita\' x pezzi', abs((f3.get('kg') or 0) - 2.0 * 3 / 100 * 7.85 * 11) < 0.2, f3.get('kg'))
    check('minuti di laser e fogli', (f3.get('min') or 0) > 0 and (f3.get('n_fogli') or 0) >= 1, f3)
    check('niente ancora tagliato', not any(x['tagliata'] for x in lam.values()))

    print('\n6) In Lantek: il "Manda a Lantek" di sempre, ordine per ordine')
    r = laser.post(us, json={'stato': 'in_lantek'})
    j = r.get_json() or {}
    check('prima di mandarli: non sono in Lantek (409, dice quali)',
          r.status_code == 409 and j.get('codice') == 'non_in_lantek' and {x['id'] for x in j.get('ordini') or []} == {o1, o2}, j)
    r = manda(laser, o1)
    check('ordine 5001 mandato', r.status_code == 200 and r.get_json().get('verificati') == 2, r.get_json())
    r = manda(laser, o2, tenuti_fuori=('B2-SPESS-00',))
    j = r.get_json() or {}
    check('ordine 5002 mandato, B2-SPESS tenuto fuori da correggere',
          r.status_code == 200 and j.get('da_correggere') == ['B2-SPESS-00'] and j.get('mandati') == 1, j)
    xml = b''.join(c[1] for c in CHIAMATE if c[0] == 'xml')
    check('B2-SPESS non e\' partito', b'B2-SPESS-00' not in xml and b'B2-FE3-00' in xml)
    check('5002 resta "da mandare" (rosso: da correggere)', AL.stato_laser(ordine_db(o2)) == 'coda')
    r = laser.post(us, json={'stato': 'in_lantek'})
    check('ora il pacco e\' In Lantek (5002: manca solo il codice da correggere)',
          r.status_code == 200 and r.get_json()['pacco']['stato'] == 'in_lantek', r.get_json())
    r = laser.post(f'/api/pacchi/{pid}/ordini', json={'aggiungi': [o3]})
    check('in Lantek non si aggiungono ordini', r.status_code == 409 and r.get_json().get('codice') == 'gia_in_lantek')

    print('\n7) Segna tagliato')
    ut = f'/api/pacchi/{pid}/tagliato'
    check('lamiera che nessuno ha: 409', laser.post(ut, json={'chiave': 'S235|8'}).status_code == 409)
    r = laser.post(ut, json={'chiave': 'S235|3'})
    j = r.get_json() or {}
    fatto1 = j.get('fatto') or {}
    check('S235 3 mm segnata in tutti e due gli ordini',
          r.status_code == 200 and sorted(map(tuple, fatto1.get('lamiere') or [])) == sorted([(o1, 'S235|3'), (o2, 'S235|3')]), j)
    check('5001 ha ancora l\'inox: non e\' taglio fatto', not ordine_db(o1).taglio_completato and o1 not in fatto1.get('ordini', []))
    check('5002 ha un codice da correggere: resta da tagliare', j.get('trattenuti') == [o2] and not ordine_db(o2).taglio_completato, j)
    check('il pacco resta In Lantek', j['pacco']['stato'] == 'in_lantek')
    check('nella pagina laser la lamiera e\' tagliata', 'S235|3' in (j.get('lamiere_tagliate') or {}).get(o1, {}))
    r = laser.post(ut, json={'annulla': True, 'fatto': fatto1})
    check('Annulla: la lamiera torna da tagliare', r.status_code == 200 and not AL.lamiere_tagliate([o1, o2]), r.get_json())
    laser.post(ut, json={'chiave': 'S235|3'})
    r = laser.post(ut, json={})
    j = r.get_json() or {}
    fatto2 = j.get('fatto') or {}
    check('tutto tagliato: segnato l\'inox di 5001', [tuple(x) for x in fatto2.get('lamiere') or []] == [(o1, 'INOX|2')], fatto2)
    check('5001 taglio fatto', fatto2.get('ordini') == [o1] and ordine_db(o1).taglio_completato, fatto2)
    check('5002 resta da tagliare (codice da correggere)', not ordine_db(o2).taglio_completato)
    check('il pacco e\' Tagliato', j['pacco']['stato'] == 'tagliato' and j['pacco']['tagliato_il'], j.get('pacco'))
    ff = uff.get('/api/ordini/forse-finiti').get_json() or {}
    ids_ff = {x.get('id') or x.get('order_id') for x in (ff.get('ordini') or ff.get('data') or [])}
    check("l'ufficio lo vede in Forse finiti", o1 in ids_ff, ff)
    pp = laser.get('/api/pacchi').get_json()['pacchi']
    check('fra i pacchi: il tagliato, dopo gli aperti', [x['stato'] for x in pp] == ['tagliato'], pp)
    r = laser.post(ut, json={'annulla': True, 'fatto': fatto2})
    check('Annulla: 5001 di nuovo da tagliare, il pacco di nuovo In Lantek',
          r.status_code == 200 and not ordine_db(o1).taglio_completato and r.get_json()['pacco']['stato'] == 'in_lantek',
          r.get_json())
    check("Annulla tocca solo quel passo: S235 3 mm resta tagliata",
          'S235|3' in AL.lamiere_tagliate([o1]).get(o1, {}) and 'INOX|2' not in AL.lamiere_tagliate([o1]).get(o1, {}))

    print('\n8) Sciogli, rimetti su, un ordine da solo')
    r = laser.post(us, json={'stato': 'annullato'})
    check('sciolto', r.status_code == 200 and r.get_json()['pacco']['stato'] == 'annullato')
    check('sciolto: non e\' piu\' fra i pacchi', not laser.get('/api/pacchi').get_json()['pacchi'])
    r = laser.post('/api/pacchi', json={'ordini': [o1, o3]})
    p2 = (r.get_json() or {}).get('pacco') or {}
    check('libero: 5001 entra in un pacco nuovo, numero 2', r.status_code == 201 and p2.get('numero') == 2, r.get_json())
    r = laser.post(us, json={'stato': 'proposto'})
    check('il vecchio non si rimette su: 5001 sta nel pacco 2', r.status_code == 409
          and r.get_json().get('codice') == 'in_altro_pacco', r.get_json())
    r = manda(laser, o3)
    check('un ordine del pacco si manda ancora da solo', r.status_code == 200 and AL.stato_laser(ordine_db(o3)) == 'in_lantek',
          r.get_json())
    d = laser.get(f'/api/pacchi/{p2["id"]}').get_json()
    check('il pacco mostra le lamiere dei suoi ordini (anche di un altro spessore)',
          sorted(x['chiave'] for x in d['lamiere']) == ['INOX|2', 'S235|3', 'S235|5'], d.get('lamiere'))
    s = models.SessionLocal()
    az = [r[0] for r in s.execute(text("SELECT action FROM audit_log WHERE entity_type = 'pacchi_taglio'")).fetchall()]
    s.close()
    check('nello storico: creato, ordini, stati, tagliato',
          {'PACCO_CREATO', 'PACCO_ORDINI', 'PACCO_STATO', 'PACCO_TAGLIATO'} <= set(az), az)

    print(f'\n{OK} OK, {len(KO)} KO')
    if KO:
        print('Falliti:', KO)
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        try:
            _ENG.dispose()
            os.remove(_TMP)
        except OSError:
            pass
