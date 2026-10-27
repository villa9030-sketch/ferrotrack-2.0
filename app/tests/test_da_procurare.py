"""Da procurare: i pezzi che non si tagliano al laser (backend/procura.py).

Copre:
 A. lettura dei profili dalle descrizioni ("Tubo 40x40x2 Lg. 795",
    "Angolare 50x5", "Piatto 40x5 Lg. 200", "Tondo Ø20"...), niente falsi
    sulle lamiere; proposta per il tasto N (riga dell'ordine, cartiglio,
    sezione di tubo disegnata)
 B. barre da 6 m: first-fit decreasing con la lama, pezzi piu' lunghi di una
    barra, barra e lama dalle impostazioni
 C. righe tubolari della distinta: entrano da sole (T, o L con la "Lav.
    esterna"), quantita' x assieme, senza doppioni, aggiornate e tolte
 D. tasto N: classificazione T/L/M/C/A salvata con la decisione, controlli,
    cambio di decisione; laser tubo / meccanica = lavorazione esterna (+5)
 E. stati: ordinato (fornitore, data prevista), arrivato, annulla, storico,
    in ritardo; permessi (il tablet no)
 F. richiesta d'offerta: ZIP con il PDF e SOLO i disegni dei pezzi scelti
    (mai l'ordine del cliente, mai i DXF ripuliti)
 G. ordine eliminato / ripristinato; il tablet dice "mancano N"

Gira su DATABASE TEMPORANEO e LANTEK SIMULATO (nessun programma di Lantek).
Esecuzione: python app/tests/test_da_procurare.py
"""
import io
import json
import os
import sys
import tempfile
import uuid
import zipfile
from datetime import date, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_pc_esempi_')
_CFG = os.path.join(tempfile.mkdtemp(prefix='ft_pc_cfg_'), 'app_config.json')
with open(_CFG, 'w', encoding='utf-8') as _f:
    json.dump({'azienda': {'nome': 'Carpenteria di prova'}}, _f)
os.environ['FERROTRACK_CONFIG'] = _CFG

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo, AuditLog, DaProcurare, Notification  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_pc_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend import procura as PC  # noqa: E402
from backend.database import PreventivoManager, ConfigManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_pc_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

# Lantek simulato: niente programmi, niente database di Lantek
TL._leggi_lantek = lambda codici: None
L.pezzi_in_lantek = lambda codici: {'disponibile': True, 'errore': None, 'pezzi': {
    c: {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': []} for c in codici}}
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: {}
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.materiali_lantek_noti = lambda: {'FERRO', 'INOX'}
L.xmlimporter_disponibile = lambda: True
L.importa_pezzi_dxf = lambda *a, **k: {'eseguito': False, 'errore': 'bloccato nel test'}
L.importa_xml = lambda *a, **k: {'eseguito': False, 'errore': 'bloccato nel test'}
L.expert_aperto = lambda: False   # Lantek Expert chiuso (sul PC vero puo' essere aperto)

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


def audit(azione, oid=None):
    s = models.SessionLocal()
    try:
        q = s.query(AuditLog).filter(AuditLog.action == azione)
        if oid:
            q = q.filter(AuditLog.entity_id == oid)
        return [(a.user_name, a.detail) for a in q.all()]
    finally:
        s.close()


def avvisi_ufficio(oid):
    """Gli avvisi "Da procurare" alla campanella dell'Ufficio per un ordine."""
    s = models.SessionLocal()
    try:
        return [(n.message, n.is_read) for n in s.query(Notification).filter(
            Notification.user_id == 'postazione-amministrazione', Notification.order_id == oid,
            Notification.notification_type == 'procura', Notification.is_deleted == False).all()]  # noqa: E712
    finally:
        s.close()


def voci_db(oid):
    s = models.SessionLocal()
    try:
        return {r.chiave: r for r in s.query(DaProcurare).filter(DaProcurare.order_id == oid).all()}
    finally:
        s.close()


def nuovo_ordine(numero, articoli, tubolari=(), assiemi=()):
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente=numero)
    pid = pr['id'] if isinstance(pr, dict) else pr
    base = {'quantita': 1, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.8, 'n_forature': 0, 'area_dm2': 3.0, 'bbox_w_mm': 200, 'bbox_h_mm': 150}
    PreventivoManager.replace_articoli(pid, [dict(base, **a) for a in articoli])
    if assiemi:
        r = PreventivoManager.replace_assiemi(pid, list(assiemi))
        assert r.get('success'), r
    if tubolari:
        r = PreventivoManager.replace_tubolari(pid, list(tubolari))
        assert r.get('success'), r
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    return oid, pid


def articolo_id(pid, codice):
    p = PreventivoManager.get(pid, include_children=True)
    return next(a['id'] for a in p['articoli'] if a['codice'] == codice)


def riep(c, oid):
    AL.invalida(oid)
    PC._SYNC['t'] = 0
    return ((c.get('/api/laser/ordini').get_json() or {}).get('ordini') or {}).get(oid) or {}


def test_lettura():
    print('A) Profili scritti a parole')
    casi = [
        ('Tubo 40x40x2 Lg. 795', 'SHS', 'Tubo quadro 40x40 sp.2mm', 795),
        ('Tubo 60x40x3 L=1200', 'RHS', 'Tubo rett. 60x40 sp.3mm', 1200),
        ('Angolare 50x5', 'L', 'Angolare 50x50x5', None),
        ('Angolare 50x30x5 lg.300', 'L', 'Angolare 50x30x5', 300),
        ('Piatto 40x5 Lg. 200', 'FLAT', 'Piatto 40x5', 200),
        ('Tondo Ø20', 'BAR', 'Tondo pieno Ø20', None),
        ('Tubo Ø40x2 Lg.500', 'CHS', 'Tubo tondo Ø40 sp.2mm', 500),
        ('tubo tondo 33,7x2,6 lunghezza 600', 'CHS', 'Tubo tondo Ø33,7 sp.2,6mm', 600),
        ('T. quadrato saldato 80 X 80 X 3', 'SHS', 'Tubo quadro 80x80 sp.3mm', None),
        ('Tubo quadro 40x40 sp. 2 Lg. 1.200', 'SHS', 'Tubo quadro 40x40 sp.2mm', 1200),
        ('Tubo 40x40x2x795', 'SHS', 'Tubo quadro 40x40 sp.2mm', 795),
        ('UPN 80 lg 1000', 'TRAVE', 'UPN 80', 1000),
        ('Supporto - Tubo 30x30x2 - L=450', 'SHS', 'Tubo quadro 30x30 sp.2mm', 450),
        # i nomi del catalogo (righe tubolari del preventivo): stesso gruppo
        ('Quadro 40x40 sp.2mm', 'SHS', 'Tubo quadro 40x40 sp.2mm', None),
        ('Rett. 100x50 sp.4mm', 'RHS', 'Tubo rett. 100x50 sp.4mm', None),
        ('Tondo Ø16 sp.1.5mm', 'CHS', 'Tubo tondo Ø16 sp.1,5mm', None),
    ]
    for t, fam, nome, lung in casi:
        p = PC.leggi_profilo(t)
        check(f'{t!r} -> {nome}' + (f', {lung} mm' if lung else ''),
              p and p['famiglia'] == fam and p['nome'] == nome and (p['lunghezza_mm'] or None) == lung, p)
    for t in ('Lamiera sp.3', 'Piastra angolare 100x100 sp.5', 'Tolleranze angolari',
              'Piatto 190x55 sp.12 - 2xØ11 int.150 AISI 304', 'Nervatura per tondo Ø10 sfalsato di 3',
              'Angolare 30x62 H130 sp.4 AISI 304', 'Lamiera supporto scivolo angolare 52x48 sp4',
              'Angolare supporto guide', ''):
        check(f'niente profilo in {t!r}', PC.leggi_profilo(t) is None, PC.leggi_profilo(t))
    check('catalogo: 40x40x2 c\'e\', 41x41x2 no',
          PC.leggi_profilo('Tubo 40x40x2')['da_catalogo'] and not PC.leggi_profilo('Tubo 41x41x2')['da_catalogo'])
    check('stesso gruppo per "Quadro 40x40 sp.2mm" e "Tubo 40x40x2 Lg. 795"',
          PC.chiave_profilo('Quadro 40x40 sp.2mm') == PC.chiave_profilo('Tubo 40x40x2 Lg. 795'))

    print('\nA2) Proposta per il tasto N')
    p = PC.proposta(['Tubo 40x40x2 Lg. 795'], None, None)
    check('dalla riga dell\'ordine: T, profilo e lunghezza, alternativa L',
          p['tipo'] == 'tubo' and p['profilo'] == 'Tubo quadro 40x40 sp.2mm' and p['lunghezza_mm'] == 795
          and p['fonte'] == "riga dell'ordine" and p['alternativa'] == 'laser_tubo', p)
    p = PC.proposta([], 'Angolare 50x5', None)
    check('dal cartiglio: angolare, niente alternativa laser tubo',
          p['tipo'] == 'tubo' and p['profilo'] == 'Angolare 50x50x5' and p['fonte'] == 'cartiglio' and not p['alternativa'], p)
    p = PC.proposta(['Staffa di supporto'], None, None)
    check('niente da leggere: nessuna proposta', p['tipo'] is None, p)
    p = PC.proposta(['Particolare selezione AISI 304'], 'Angolare supporto guide', None, [620, 40])
    check('solo la parola ("Angolare supporto guide", come nell\'archivio): T, profilo da scrivere, lunghezza dal disegno',
          p['tipo'] == 'tubo' and p['profilo'] is None and p['solo_parola'] == 'ANGOLARE' and p['lunghezza_mm'] == 620
          and p['fonte'] == 'cartiglio', p)
    check('"S235" e\' il materiale, non lo spessore', (PC.leggi_profilo('Piatto 40x5 S235 Lg.300') or {}).get('nome') == 'Piatto 40x5'
          and PC.parola_profilo('Piatto CVE-025/050 S235JR') == 'PIATTO', PC.leggi_profilo('Piatto 40x5 S235 Lg.300'))
    check('niente parola per le lamiere', PC.parola_profilo('Lamiera fix tondo sp.4') is None
          and PC.parola_profilo('Angolare 40x55 H38 sp.3 AISI304') is None)
    # sezione di tubo disegnata (cerchi concentrici a parte) e foro svasato dentro un pezzo
    import ezdxf
    d = tempfile.mkdtemp(prefix='ft_pc_dxf_')
    doc = ezdxf.new()
    m = doc.modelspace()
    m.add_lwpolyline([(0, 0), (500, 0), (500, 40), (0, 40)], close=True)
    m.add_circle((600, 20), 20)
    m.add_circle((600, 20), 18)
    tubo = os.path.join(d, 'tubo.dxf')
    doc.saveas(tubo)
    p = PC.proposta([], None, tubo, [500, 40])
    check('sezione Ø40 sp.2 disegnata: T o L, lunghezza dal disegno',
          p['tipo'] == 'tubo' and p['sezione'] and p['profilo'] == 'Tubo tondo Ø40 sp.2mm'
          and p['lunghezza_mm'] == 500 and p['alternativa'] == 'laser_tubo', p)
    doc = ezdxf.new()
    m = doc.modelspace()
    m.add_lwpolyline([(0, 0), (300, 0), (300, 200), (0, 200)], close=True)
    m.add_circle((100, 100), 6.5)
    m.add_circle((100, 100), 3.25)
    lam = os.path.join(d, 'lamiera.dxf')
    doc.saveas(lam)
    check('foro svasato dentro una lamiera: niente sezione di tubo', PC.sezione_tubo(lam) is None, PC.sezione_tubo(lam))
    doc = ezdxf.new()
    m = doc.modelspace()
    m.add_lwpolyline([(1000, 0), (1040, 0), (1040, 40), (1000, 40)], close=True)
    m.add_lwpolyline([(1002, 2), (1038, 2), (1038, 38), (1002, 38)], close=True)
    q = os.path.join(d, 'quadro.dxf')
    doc.saveas(q)
    check('sezione quadra 40x40 sp.2', (PC.sezione_tubo(q) or {}).get('nome') == 'Tubo quadro 40x40 sp.2mm', PC.sezione_tubo(q))


def test_barre():
    print('\nB) Barre da 6 m')
    pz = [{'lunghezza_mm': x, 'id': i} for i, x in enumerate([2000] * 5 + [1500] * 3)]
    pb = PC.piano_barre(pz, 6000, 3)
    check('5x2000 + 3x1500 = 3 barre (first-fit decreasing, lama 3 mm)',
          pb['n_barre'] == 3 and [[t['lunghezza_mm'] for t in b['tagli']] for b in pb['barre']]
          == [[2000, 2000, 1500], [2000, 2000, 1500], [2000, 1500]], pb['barre'])
    check('metri e sfrido', pb['metri'] == 14.5 and pb['barre'][0]['usato_mm'] == 5506 and pb['barre'][0]['sfrido_mm'] == 494,
          (pb['metri'], pb['barre'][0]))
    pb = PC.piano_barre([{'lunghezza_mm': 3000}, {'lunghezza_mm': 3000}], 6000, 3)
    check('la lama conta: 2x3000 non stanno in una barra da 6000', pb['n_barre'] == 2, pb['n_barre'])
    pb = PC.piano_barre([{'lunghezza_mm': 3000}, {'lunghezza_mm': 3000}], 6000, 0)
    check('senza lama: una barra', pb['n_barre'] == 1)
    pb = PC.piano_barre([{'lunghezza_mm': 7000, 'id': 'x'}, {'lunghezza_mm': None, 'id': 'y'}, {'lunghezza_mm': 100, 'id': 'z'}], 6000, 3)
    check('piu\' lungo di una barra: segnalato e fuori dal piano; senza lunghezza a parte',
          pb['n_barre'] == 1 and [t['id'] for t in pb['troppo_lunghi']] == ['x'] and [t['id'] for t in pb['senza_lunghezza']] == ['y'],
          pb)
    # barra e lama dalle impostazioni
    check('impostazioni di partenza: 6000 mm, lama 3 mm', PC.config() == {'barra_mm': 6000.0, 'lama_mm': 3.0}, PC.config())
    cfg = ConfigManager.load_config() or {}
    cfg['procura'] = {'barra_mm': 12000, 'lama_mm': 2}
    with open(_CFG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    c = PC.config()
    check('barra 12 m e lama 2 mm da app_config', c == {'barra_mm': 12000.0, 'lama_mm': 2.0}, c)
    cfg['procura'] = {'barra_mm': 'x', 'lama_mm': 99}
    with open(_CFG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    check('valori assurdi: si torna a quelli di partenza', PC.config() == {'barra_mm': 6000.0, 'lama_mm': 3.0}, PC.config())
    cfg.pop('procura')
    with open(_CFG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)


def test_taglia_entro():
    print('\nB2) Taglia entro: laser tubo e meccanica dal fornitore = +5')
    from backend import taglia_entro as TE
    f = TE.lavorazioni_ordine([{'tipo': 'lamiera', 'lavorazioni': ['Taglio laser', 'Piegatura (2 pieghe)']}])
    base = TE.anticipo_da(TE.con_procura(TE.con_esterna(f, None, ''), []), None)
    con = TE.anticipo_da(TE.con_procura(TE.con_esterna(f, None, ''), ['meccanica dal fornitore']), None)
    check('piega 2 giorni -> 7 con la meccanica dal fornitore, e il motivo lo dice',
          base['giorni'] == 2 and con['giorni'] == 7 and 'da procurare: meccanica dal fornitore' in con['motivo'], (base, con))
    no = TE.anticipo_da(TE.con_procura(TE.con_esterna(f, False, ''), ['laser tubo dal fornitore']), None)
    check('anche con "Lavorazione esterna: no" (quella parla del dopo laser)', no['giorni'] == 7, no)
    due = TE.anticipo_da(TE.con_procura(TE.con_esterna(f, True, 'zincatura'), ['laser tubo dal fornitore']), None)
    check('zincatura + laser tubo: +5 una volta sola, tutte e due nel motivo', due['giorni'] == 7
          and 'zincatura' in due['motivo'] and 'laser tubo' in due['motivo'], due)
    an = TE.anticipo([{'tipo': 'lamiera', 'lavorazioni': ['Taglio laser']}], procura=['meccanica dal fornitore'])
    check('anticipo(..., procura=...): 1 + 5', an['giorni'] == 6, an)


def main():
    test_lettura()
    test_barre()
    test_taglia_entro()

    from tests.accesso_aiuto import postazioni, entra, modalita, persona, PIN_UFFICIO
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    persona('elena', 'Elena Ufficio', pin=PIN_UFFICIO)
    laser = entra(A.app, 'laser')
    rep = entra(A.app, 'reparto')
    uff = entra(A.app, 'ufficio', pin=PIN_UFFICIO)
    persona('carla', 'Carla Commerciale', ruolo='Commerciale', pin='3691')
    com = entra(A.app, 'commerciale', pin='3691')

    print('\nC) Righe tubolari della distinta: entrano da sole')
    oid, pid = nuovo_ordine('7001', [{'codice': 'P-LAM-00'}, {'codice': 'P-TUBO-00', 'codice_assieme': 'ASS1'},
                                     {'codice': 'P-ALTRO-00', 'codice_assieme': 'ASS1'}],
                           tubolari=[{'profilo': 'Quadro 40x40 sp.2mm', 'tipo': 'SHS', 'lunghezza_m': 0.795, 'codice_assieme': 'ASS1', 'qty': 2},
                                     {'profilo': 'Quadro 40x40 sp.2mm', 'tipo': 'SHS', 'lunghezza_m': 0.795, 'codice_assieme': 'ASS1', 'qty': 1},
                                     {'profilo': 'Rett. 100x50 sp.4mm', 'tipo': 'RHS', 'lunghezza_m': 1.2, 'codice_assieme': 'ASS1',
                                      'qty': 1, 'costo_esterno': 12.5, 'nota_esterno': 'laser tubo Rossi'}],
                           assiemi=[{'codice_assieme': 'ASS1', 'qty': 3}])
    d = riep(laser, oid)
    vv = voci_db(oid)
    tubi = [v for v in vv.values() if v.origine == 'tubolare']
    q = next((v for v in tubi if v.tipo == 'tubo'), None)
    r = next((v for v in tubi if v.tipo == 'laser_tubo'), None)
    check('due voci: righe uguali sommate (2+1) x 3 assiemi = 9 pz, 795 mm',
          len(tubi) == 2 and q and q.quantita == 9 and q.lunghezza_mm == 795 and q.profilo == 'Quadro 40x40 sp.2mm',
          [(v.tipo, v.profilo, v.quantita, v.lunghezza_mm) for v in tubi])
    check('con la "Lav. esterna" del preventivatore: laser tubo, la nota resta',
          r and r.quantita == 3 and r.lunghezza_mm == 1200 and r.nota == 'laser tubo Rossi', r and (r.quantita, r.nota))
    # 14/10/2026: il laser tubo e' due voci, il taglio e il materiale (barre) da comprare
    check('chip: 3 da procurare (tubo, taglio laser tubo, suo materiale), nessuno in ritardo',
          d.get('procura') == {'n': 3, 'n_da_ordinare': 3, 'n_ordinati': 0, 'n_scaduti': 0, 'n_materiale': 1},
          d.get('procura'))
    check('laser tubo dal fornitore = lavorazione esterna nel taglia entro (+5)',
          'laser tubo dal fornitore' in (d.get('taglia_entro') or {}).get('motivo', ''), d.get('taglia_entro'))
    check("avviso all'Ufficio: uno per l'ordine, \"3 pezzi da procurare per DECA S.r.l. 7001\" (col materiale del laser tubo)",
          avvisi_ufficio(oid) == [('3 pezzi da procurare per DECA S.r.l. 7001', False)], avvisi_ufficio(oid))
    riep(laser, oid)
    check('riletto: niente doppioni', len([v for v in voci_db(oid).values() if v.origine == 'tubolare']) == 2)
    # la distinta cambia: quantita' aggiornata, riga sparita tolta
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'BOZZA'
    s.commit(); s.close()
    PreventivoManager.replace_tubolari(pid, [{'profilo': 'Quadro 40x40 sp.2mm', 'tipo': 'SHS', 'lunghezza_m': 0.795,
                                              'codice_assieme': 'ASS1', 'qty': 4}])
    riep(laser, oid)
    tubi = [v for v in voci_db(oid).values() if v.origine == 'tubolare']
    check('distinta cambiata: 4x3 = 12 pz, la riga laser tubo (da ordinare) tolta',
          len(tubi) == 1 and tubi[0].quantita == 12 and tubi[0].tipo == 'tubo', [(v.tipo, v.quantita) for v in tubi])

    print('\nD) Tasto N: cosa procurare')
    aid = articolo_id(pid, 'P-TUBO-00')
    u = f'/api/orders/{oid}/pezzi/{aid}'
    AL_riga = AL.riga_ordine_di
    AL.riga_ordine_di = lambda order, art: ({'codice': art.get('codice'), 'righe': [
        {'pos': 3, 'quantita': 1, 'descrizione': 'Tubo 40x40x2 Lg. 795', 'commessa': None}]}
        if art and art.get('codice') == 'P-TUBO-00' else None)
    j = laser.get(u + '/procura-proposta').get_json() or {}
    check('proposta: T, Tubo quadro 40x40 sp.2mm, 795 mm, dalla riga dell\'ordine; 3 pz (1 x 3 assiemi)',
          j.get('success') and j['proposta']['tipo'] == 'tubo' and j['proposta']['profilo'] == 'Tubo quadro 40x40 sp.2mm'
          and j['proposta']['lunghezza_mm'] == 795 and j['quantita'] == 3 and j['attuale'] is None, j)
    r = laser.post(u + '/decisione', json={'scelta': 'non_laser', 'procura': {'tipo': 'xyz'}})
    check('tipo sconosciuto: 400', r.status_code == 400, r.status_code)
    r = laser.post(u + '/decisione', json={'scelta': 'non_laser', 'procura': {'tipo': 'altro'}})
    check('"Altro" senza nota: 400', r.status_code == 400 and 'nota' in (r.get_json() or {}).get('error', ''), r.get_json())
    r = laser.post(u + '/decisione', json={'scelta': 'non_laser', 'procura': {'tipo': 'tubo', 'lunghezza_mm': 'abc'}})
    check('lunghezza non valida: 400', r.status_code == 400, r.status_code)
    r = laser.post(u + '/decisione', json={'scelta': 'non_laser', 'procura': {'tipo': 'tubo', 'profilo': 'Tubo quadro 40x40 sp.2mm',
                                                                                'lunghezza_mm': 795}})
    j = r.get_json() or {}
    check('N + T: salvato, la voce torna nella risposta', r.status_code == 200 and (j.get('procura') or {}).get('tipo') == 'tubo', j)
    v = voci_db(oid).get(f'art:{aid}')
    check('voce: codice, descrizione della riga, 3 pz, 795 mm, materiale, da ordinare',
          v and v.codice == 'P-TUBO-00' and v.descrizione == 'Tubo 40x40x2 Lg. 795' and v.quantita == 3
          and v.lunghezza_mm == 795 and v.materiale == 'S235' and v.stato == 'da_ordinare' and v.creato_da == 'Laser',
          v and (v.codice, v.descrizione, v.quantita, v.lunghezza_mm, v.materiale, v.stato, v.creato_da))
    o = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    pz = next((p for p in o.get('pezzi') or [] if p['codice'] == 'P-TUBO-00'), {})
    check('il pezzo resta fuori da Lantek come prima', pz.get('escluso') == "non è da laser" and pz.get('decisione') == 'non_laser', pz)
    check("avviso raggruppato: sempre uno per l'ordine, col numero di adesso",
          avvisi_ufficio(oid) == [('2 pezzi da procurare per DECA S.r.l. 7001', False)], avvisi_ufficio(oid))
    check('l\'ordine aperto ha le sue voci', len(o.get('procura_voci') or []) == 2, o.get('procura_voci'))
    check('storico PROCURA_CLASSIFICA', any('P-TUBO-00' in d_ and 'Tubo/profilo' in d_ and '795 mm' in d_
                                            for _, d_ in audit('PROCURA_CLASSIFICA', oid)), audit('PROCURA_CLASSIFICA', oid))
    # stesso pezzo di nuovo con N: si aggiorna, niente doppioni
    laser.post(u + '/decisione', json={'scelta': 'non_laser', 'procura': {'tipo': 'tubo', 'profilo': 'Tubo quadro 40x40 sp.2mm', 'lunghezza_mm': 800}})
    vv = [v for v in voci_db(oid).values() if v.chiave == f'art:{aid}']
    check('di nuovo N: una voce sola, aggiornata (800 mm)', len(vv) == 1 and vv[0].lunghezza_mm == 800, [(v.chiave, v.lunghezza_mm) for v in vv])
    j = laser.get(u + '/procura-proposta').get_json() or {}
    check('la proposta dice che c\'e\' gia\'', (j.get('attuale') or {}).get('tipo') == 'tubo', j.get('attuale'))
    # cambio di decisione: la voce (da ordinare) sparisce
    laser.post(u + '/decisione', json={'scelta': 'sviluppo'})
    check('decisione cambiata in "va sviluppato": voce tolta', f'art:{aid}' not in voci_db(oid))
    check('storico PROCURA_TOLTA', len(audit('PROCURA_TOLTA', oid)) == 1, audit('PROCURA_TOLTA', oid))
    # meccanica: lavorazione esterna
    aid2 = articolo_id(pid, 'P-ALTRO-00')
    oid_m, pid_m = nuovo_ordine('7002', [{'codice': 'P-MEC-00'}, {'codice': 'P-L2-00'}])
    aid_m = articolo_id(pid_m, 'P-MEC-00')
    d0 = riep(laser, oid_m)
    g0 = (d0.get('taglia_entro') or {}).get('giorni')
    r = laser.post(f'/api/orders/{oid_m}/pezzi/{aid_m}/decisione',
                   json={'scelta': 'non_laser', 'procura': {'tipo': 'meccanica', 'nota': 'tornitura'}})
    d1 = riep(laser, oid_m)
    te = d1.get('taglia_entro') or {}
    check('meccanica dal fornitore: +5 giorni e il motivo lo dice',
          r.status_code == 200 and te.get('giorni') == g0 + 5 and 'meccanica dal fornitore' in te.get('motivo', ''),
          (g0, te))
    r = laser.post(f'/api/orders/{oid}/pezzi/{aid2}/decisione', json={'scelta': 'non_laser', 'procura': {'tipo': 'commerciale'}})
    v = voci_db(oid).get(f'art:{aid2}')
    check('commerciale: salvato, non conta come lavorazione esterna',
          r.status_code == 200 and v and v.tipo == 'commerciale'
          and 'commerciale' not in ((riep(laser, oid).get('taglia_entro') or {}).get('motivo') or ''), v and v.tipo)
    r = rep.post(f'/api/orders/{oid}/pezzi/{aid2}/decisione', json={'scelta': 'non_laser', 'procura': {'tipo': 'tubo'}})
    check('il tablet non decide', r.status_code == 403, r.status_code)
    AL.riga_ordine_di = AL_riga

    print('\nE) Stati: ordinato, arrivato, annulla')
    j = laser.get('/api/procura').get_json() or {}
    ids = {v['codice'] or v['profilo']: v['id'] for v in j.get('voci') or []}
    check('elenco "da ordinare": le voci dei due ordini', j.get('success') and j['conteggi']['da_ordinare'] == 3
          and len(j['voci']) == 3, (j.get('conteggi'), [v['codice'] for v in j.get('voci') or []]))
    g = next((x for x in j.get('gruppi') or [] if 'Tubo quadro 40x40' in x['profilo']), {})
    check('tubi: 12 pz da 795 mm = 9,54 m, 2 barre da 6 m', g.get('n_pezzi') == 12 and g.get('metri') == 9.54 and g.get('n_barre') == 2,
          {k: g.get(k) for k in ('profilo', 'n_pezzi', 'metri', 'n_barre')})
    tubo_id = next(v['id'] for v in j['voci'] if v['tipo'] == 'tubo')
    mec_id = next(v['id'] for v in j['voci'] if v['tipo'] == 'meccanica')
    com_id = next(v['id'] for v in j['voci'] if v['tipo'] == 'commerciale')
    r = laser.post('/api/procura/stato', json={'ids': [tubo_id, com_id], 'stato': 'ordinato'})
    check('ordinato senza fornitore: 400', r.status_code == 400, r.status_code)
    r = laser.post('/api/procura/stato', json={'ids': [tubo_id, com_id], 'stato': 'ordinato', 'fornitore': 'Rossi tubi',
                                               'data_prevista': (date.today() + timedelta(days=5)).isoformat()})
    j = r.get_json() or {}
    check('ordinato: 2 voci, risposta per annullare', r.status_code == 200 and j.get('n') == 2 and len(j.get('prima') or []) == 2, j)
    prima = j.get('prima')
    v = voci_db(oid)
    t = next(x for x in v.values() if x.id == tubo_id)
    check('fornitore, data prevista, ordinato il', t.stato == 'ordinato' and t.fornitore == 'Rossi tubi' and t.ordinato_il
          and t.data_prevista == date.today() + timedelta(days=5) and t.aggiornato_da == 'Laser', (t.stato, t.fornitore, t.data_prevista))
    check('storico PROCURA_STATO con fornitore', any('ordinato a Rossi tubi' in d_ for _, d_ in audit('PROCURA_STATO', oid)),
          audit('PROCURA_STATO', oid))
    r = laser.post('/api/procura/annulla', json={'prima': prima})
    t = next(x for x in voci_db(oid).values() if x.id == tubo_id)
    check('annulla: di nuovo da ordinare, senza fornitore', r.status_code == 200 and t.stato == 'da_ordinare' and not t.fornitore
          and not t.ordinato_il, (r.status_code, t.stato, t.fornitore))
    check('storico PROCURA_ANNULLA', len(audit('PROCURA_ANNULLA', oid)) == 1)
    # in ritardo: data prevista passata
    laser.post('/api/procura/stato', json={'ids': [mec_id], 'stato': 'ordinato', 'fornitore': 'Officina Bianchi',
                                           'data_prevista': (date.today() - timedelta(days=2)).isoformat()})
    d = riep(laser, oid_m)
    check('data prevista passata: in ritardo (chip rosso)', (d.get('procura') or {}).get('n_scaduti') == 1, d.get('procura'))
    j = uff.get('/api/procura/conteggio').get_json() or {}
    check('conteggio (anche dall\'ufficio): 3 aperti, 1 in ritardo', j.get('aperti') == 3 and j.get('scaduti') == 1, j)
    r = laser.post('/api/procura/stato', json={'ids': [mec_id], 'stato': 'arrivato'})
    d = riep(laser, oid_m)
    check('arrivato: il chip sparisce e niente piu\' +5', r.status_code == 200 and (d.get('procura') or {}).get('n') == 0
          and 'meccanica' not in ((d.get('taglia_entro') or {}).get('motivo') or '') and (d.get('taglia_entro') or {}).get('giorni') == g0,
          (d.get('procura'), d.get('taglia_entro')))
    j = laser.get('/api/procura?stato=arrivato').get_json() or {}
    check('filtro "arrivati"', [v['id'] for v in j.get('voci') or []] == [mec_id], [v['id'] for v in j.get('voci') or []])
    j = laser.get(f'/api/procura?stato=tutti&tipo=tubo&ordine={oid}').get_json() or {}
    check('filtri tipo + ordine', [v['id'] for v in j.get('voci') or []] == [tubo_id], [v['tipo'] for v in j.get('voci') or []])
    for metodo, url, corpo in (('get', '/api/procura', None), ('get', '/api/procura/conteggio', None),
                               ('post', '/api/procura/stato', {'ids': [tubo_id], 'stato': 'arrivato'}),
                               ('post', '/api/procura/richiesta-offerta', {'ids': [tubo_id]})):
        r = getattr(rep, metodo)(url, json=corpo) if corpo else getattr(rep, metodo)(url)
        check(f'tablet: {metodo.upper()} {url} -> 403', r.status_code == 403, r.status_code)
    r = laser.post('/api/procura/stato', json={'ids': [tubo_id], 'stato': 'boh'})
    check('stato sconosciuto: 400', r.status_code == 400)
    r = laser.post(f'/api/procura/{tubo_id}', json={'lunghezza_mm': 810, 'nota': 'saldato'})
    t = next(x for x in voci_db(oid).values() if x.id == tubo_id)
    check('correzione a mano (lunghezza, nota) con storico', r.status_code == 200 and t.lunghezza_mm == 810 and t.nota == 'saldato'
          and len(audit('PROCURA_MODIFICA', oid)) == 1, (r.status_code, t.lunghezza_mm))
    laser.post(f'/api/procura/{tubo_id}', json={'lunghezza_mm': 795, 'nota': ''})

    print('\nF) Richiesta d\'offerta: ZIP')
    # disegni dell'ordine e del preventivo
    od = os.path.join(A.DRAWINGS_FOLDER, oid)
    os.makedirs(od, exist_ok=True)
    pdir = os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp', pid)
    os.makedirs(pdir, exist_ok=True)
    for nome, dove in (('P-ALTRO-00.dxf', od), ('P-LAM-00.dxf', od), ('P-ALTRO-00.pdf', pdir), ('P-ALTRO-00.step', pdir),
                       ('P-ALTRO-00_cleaned.dxf', pdir), ('P-LAM-00.pdf', pdir), ('P-ALTRO-00 ordine.pdf', pdir)):
        with open(os.path.join(dove, nome), 'wb') as f:
            f.write(b'%PDF-1.4 prova' if nome.endswith('.pdf') else b'0\nSECTION\n0\nEOF\n')
    # l'ordine del cliente: anche se nel nome c'e' il codice, non parte mai
    with open(os.path.join(pdir, '_ordine_cliente.json'), 'w', encoding='utf-8') as f:
        json.dump({'file': 'P-ALTRO-00 ordine.pdf'}, f)
    j = laser.get('/api/procura?stato=tutti').get_json() or {}
    nd = {v['codice']: v['n_disegni'] for v in j.get('voci') or [] if v.get('codice')}
    check('disegno disponibile: P-ALTRO 3 file', nd.get('P-ALTRO-00') == 3, nd)
    r = laser.post('/api/procura/richiesta-offerta', json={'ids': [com_id, tubo_id]})
    check('ZIP scaricato', r.status_code == 200 and r.mimetype == 'application/zip'
          and 'Richiesta offerta' in r.headers.get('Content-Disposition', ''), (r.status_code, r.headers))
    z = zipfile.ZipFile(io.BytesIO(r.data))
    nomi = sorted(z.namelist())
    pdf = [n for n in nomi if not n.startswith('disegni/')]
    check('dentro: il PDF della richiesta + DXF, PDF e STEP di P-ALTRO, nient\'altro',
          len(pdf) == 1 and pdf[0].startswith('Richiesta offerta') and pdf[0].endswith('.pdf')
          and [n for n in nomi if n.startswith('disegni/')] == ['disegni/P-ALTRO-00.dxf', 'disegni/P-ALTRO-00.pdf', 'disegni/P-ALTRO-00.step'],
          nomi)
    testo = z.read(pdf[0])
    check('il PDF e\' un PDF', testo[:5] == b'%PDF-', testo[:10])
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(testo)) as doc:
            tutto = '\n'.join(pg.extract_text() or '' for pg in doc.pages)
        check("PDF in italiano: titolo, data, tubi in barre, codici",
              "Richiesta d'offerta" in tutto and date.today().strftime('%d/%m/%Y') in tutto and 'Tubo quadro 40x40 sp.2mm' in tutto
              and '2 da 6 m' in tutto and 'P-ALTRO-00' in tutto and 'Commerciale' in tutto, tutto[:600])
        check('nessun fornitore nel PDF', 'Rossi' not in tutto and 'Bianchi' not in tutto)
    except ImportError:
        print('  (pdfplumber non c\'e\': testo del PDF non controllato)')
    check('storico PROCURA_RICHIESTA', any('Richiesta' in d_ for _, d_ in audit('PROCURA_RICHIESTA', oid)))

    print('\nG) Ordine eliminato / tablet')
    j = rep.get('/api/officina/tablet').get_json() or {}
    o = next((x for x in j.get('ordini') or [] if x['id'] == oid), None)
    check('tablet: "mancano 2 da procurare" (non pronto per il montaggio)', o and o.get('da_procurare') == {'mancano': 2, 'in_ritardo': 0},
          o and o.get('da_procurare'))
    r = uff.delete(f'/api/orders/{oid}')
    check('ordine eliminato', r.status_code == 200, r.status_code)
    j = laser.get('/api/procura?stato=tutti').get_json() or {}
    check('le sue voci non si vedono piu\'', all(v['order_id'] != oid for v in j.get('voci') or []) and j['conteggi']['aperti'] == 0,
          j.get('conteggi'))
    r = laser.post('/api/procura/stato', json={'ids': [tubo_id], 'stato': 'arrivato'})
    check('e non si cambiano', r.status_code == 404, r.status_code)
    r = uff.post(f'/api/orders/{oid}/ripristina', json={})
    j = laser.get('/api/procura?stato=tutti').get_json() or {}
    check('ripristinato: tornano', r.status_code == 200 and sum(1 for v in j.get('voci') or [] if v['order_id'] == oid) == 2,
          (r.status_code, j.get('conteggi')))

    print("\nH) Ufficio (amministrazione): vede, ordina, riceve gli avvisi")
    j = uff.get('/api/procura?stato=tutti').get_json() or {}
    check("l'Ufficio vede le voci di tutti gli ordini", j.get('success') and len(j.get('voci') or []) >= 3, len(j.get('voci') or []))
    r = uff.post('/api/procura/stato', json={'ids': [com_id], 'stato': 'ordinato', 'fornitore': 'Ferramenta Verdi',
                                             'data_prevista': (date.today() + timedelta(days=3)).isoformat()})
    check("l'Ufficio segna ordinato", r.status_code == 200, r.get_json())
    check("storico con chi l'ha fatto (la persona col PIN)", any(u == 'Elena Ufficio' and 'Ferramenta Verdi' in d_
                                                               for u, d_ in audit('PROCURA_STATO', oid)), audit('PROCURA_STATO', oid))
    r = uff.post('/api/procura/richiesta-offerta', json={'ids': [com_id]})
    check("l'Ufficio prepara la richiesta d'offerta", r.status_code == 200 and r.mimetype == 'application/zip', r.status_code)
    r = uff.post('/api/procura/stato', json={'ids': [com_id], 'stato': 'arrivato'})
    check("l'Ufficio segna arrivato", r.status_code == 200)
    for metodo, url, corpo in (('get', '/api/procura', None), ('post', '/api/procura/stato', {'ids': [com_id], 'stato': 'ordinato'})):
        r = getattr(com, metodo)(url, json=corpo) if corpo else getattr(com, metodo)(url)
        check(f'Commerciale: {metodo.upper()} {url} -> 403', r.status_code == 403, r.status_code)
    # avviso letto: le voci nuove ne fanno nascere un altro
    s = models.SessionLocal()
    for n in s.query(Notification).filter(Notification.order_id == oid, Notification.notification_type == 'procura').all():
        n.is_read = True
    s.commit()
    s.close()
    aid3 = articolo_id(pid, 'P-LAM-00')
    laser.post(f'/api/orders/{oid}/pezzi/{aid3}/decisione', json={'scelta': 'non_laser', 'procura': {'tipo': 'meccanica'}})
    av = avvisi_ufficio(oid)
    nuovi = [m for m, letto in av if not letto]
    check('letto il primo, le voci nuove fanno un avviso nuovo (uno solo)',
          len(av) == 2 and len(nuovi) == 1 and nuovi[0].endswith('pezzi da procurare per DECA S.r.l. 7001'), av)
    j = uff.get('/api/notifications?limit=50').get_json() or {}
    msgs = [n['message'] for n in ((j.get('data') or {}).get('notifications') or [])]
    # stesso ordine e tipo: un avviso solo, il piu' recente (backend/avvisi.py)
    check("nella campanella dell'Ufficio", nuovi and any(m.startswith(nuovi[0]) for m in msgs), msgs)
    j = laser.get('/api/notifications?limit=50').get_json() or {}
    check('non in quella del laser', not any('da procurare' in (n.get('message') or '')
                                              for n in ((j.get('data') or {}).get('notifications') or [])))

    print('\n' + '=' * 60)
    print(f'{OK} OK, {len(KO)} KO')
    for k in KO:
        print('  KO:', k)
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        try:
            _ENG.dispose()
            os.unlink(_TMP)
        except OSError:
            pass
    sys.exit(code)
