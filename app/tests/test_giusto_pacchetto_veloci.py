"""Velocita' di "giusto" (pagina laser) e della verifica del pacchetto d'ordine,
senza cambiare i risultati (prova notturna 15->16/10: p95 7,1 s e 9,4 s).

Copre:
 1. verifica del pacchetto nei lavoratori (analisi_isolata.verifica_pezzi):
    stesso risultato di verifica_ordine nel server, pezzo per pezzo
 2. contorno preciso nel lavoratore = quello calcolato nel server
 3. "giusto" preparato in anticipo da /lettura: stessa risposta, stessa riga
    del pezzo e stesso DXF pulito del "giusto" calcolato al momento (come
    prima); e risponde in fretta (limite largo)
 4. preparazione in corso: "giusto" la aspetta invece di rifarla; senza
    preparazione (o non riuscita) fa come prima

Gira su DATABASE TEMPORANEO, cartelle temporanee, LANTEK SIMULATO.
Esecuzione: python app/tests/test_giusto_pacchetto_veloci.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
_ESEMPI = tempfile.mkdtemp(prefix='ft_gpv_esempi_')
os.environ['FERROTRACK_ESEMPI_DIR'] = _ESEMPI

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

import ezdxf  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_gpv_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend import api_contorno_ordine as ACO  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402
from backend.preventivi import verifica_ordine as vo  # noqa: E402
from backend.preventivi import analisi_isolata as ai  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_gpv_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

TL._leggi_lantek = lambda codici: None
_VUOTO = {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': []}
L.pezzi_in_lantek = lambda codici: {'disponibile': True, 'errore': None,
                                    'pezzi': {c: dict(_VUOTO) for c in codici}}
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: {}
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.materiali_lantek_noti = lambda: {'FERRO', 'INOX', 'ALLUMINIO', 'ZINCATO'}
L.xmlimporter_disponibile = lambda: True

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


AREA = (200 * 100 - 4 * 3.141592653589793 * 25) / 1e4
PESO = AREA * 2 / 100 * 7.85


def piastra(path, peso_testo=None, cornice=False, scostata=0.0):
    """Piastra 200 x 100 con 4 fori da 10 (come test_verifica_ordine)."""
    doc = ezdxf.new('R2010', units=4)
    doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    x0 = scostata
    msp.add_lwpolyline([(x0, 0), (x0 + 200, 0), (x0 + 200, 100), (x0, 100)], close=True)
    for cx, cy in ((20, 20), (180, 20), (180, 80), (20, 80)):
        msp.add_circle((x0 + cx, cy), 5)
    if cornice:
        msp.add_lwpolyline([(-100, -100), (300, -100), (300, 200), (-100, 200)], close=True)
    if peso_testo:
        msp.add_text(peso_testo, dxfattribs={'height': 5}).set_placement((0, -60))
    doc.saveas(path)


def norm_verifica(lista):
    out = json.loads(json.dumps(lista, default=str))
    for r in out:
        if r and r.get('esito'):
            r['esito'].pop('quando', None)
    return out


def prova_verifica():
    print('1) Verifica del pacchetto nei lavoratori')
    tmp = tempfile.mkdtemp(prefix='ft_gpv_ver_')
    try:
        peso = f'PESO kg {PESO:.2f}'.replace('.', ',')
        piastra(os.path.join(tmp, 'P1.dxf'), peso_testo=peso)
        piastra(os.path.join(tmp, 'P2.dxf'), peso_testo=peso, cornice=True)
        piastra(os.path.join(tmp, 'P3.dxf'))
        base = {'materiale': 'S235', 'spessore_mm': 2.0, 'area_dm2': round(AREA, 4), 'perimetro_taglio_m': 0.726,
                'n_forature': 5, 'pieghe': 0, 'bbox_w_mm': 200.0, 'bbox_h_mm': 100.0, 'dxf_confidence': 0.9,
                'dxf_needs_verify': False}
        items = [dict(base, codice='P1', dxf_filename='P1.dxf'),
                 # contorno sbagliato (la cornice): si corregge col peso del cartiglio
                 dict(base, codice='P2', dxf_filename='P2.dxf', area_dm2=12.0, bbox_w_mm=400.0, bbox_h_mm=300.0),
                 dict(base, codice='P3', dxf_filename='P3.dxf'),
                 dict(base, codice='P4', dxf_filename='P4.dxf', materiale=None)]
        lpc = {'P3': {'codice': 'P3', 'area_dm2': round(AREA, 4), 'perimetro_m': 0.726, 'spessore': 2.0,
                      'materiale': 'FERRO'}}
        t = time.perf_counter()
        a = vo.verifica_ordine(tmp, [dict(i) for i in items], dxf_cfg={}, lantek_per_codice=lpc)
        ta = time.perf_counter() - t
        t = time.perf_counter()
        b = ai.verifica_pezzi(tmp, [dict(i) for i in items], dxf_cfg={}, lantek_per_codice=lpc)
        tb = time.perf_counter() - t
        na, nb = norm_verifica(a), norm_verifica(b)
        check('stesso risultato, pezzo per pezzo', na == nb, json.dumps([na, nb])[:600])
        check('esiti attesi (verificato, corretto, ...)',
              [r['esito']['stato'] for r in nb][:2] == ['verificato', 'corretto_da_confermare'],
              [r['esito']['stato'] for r in nb])
        check('correzione del contorno uguale', na[1].get('correzione') == nb[1].get('correzione'))
        print(f'     nel server {ta:.2f} s, nei lavoratori {tb:.2f} s')
        # dati non trasportabili (non JSON): nel server, come prima
        strano = dict(items[0], _oggetto=object())
        c = norm_verifica(ai.verifica_pezzi(tmp, [strano], dxf_cfg={}))
        check('dati non JSON: verifica nel server, stesso esito', c[0]['esito'] == na[0]['esito'], c)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def extra(aid):
    s = models.SessionLocal()
    try:
        r = s.execute(text('SELECT area_dm2, perimetro_taglio_m, n_forature, geometry_source, extra_campi '
                           'FROM preventivo_articoli WHERE id = :i'), {'i': aid}).fetchone()
        e = json.loads(r[4] or '{}')
        return (r[0], r[1], r[2], r[3], e)
    finally:
        s.close()


def geometria_dxf(path):
    doc = ezdxf.readfile(path)
    out = []
    for e in doc.modelspace():
        if e.dxftype() == 'LWPOLYLINE':
            out.append((e.dxf.layer, bool(e.closed), [tuple(round(v, 6) for v in p[:2]) for p in e.get_points()]))
        else:
            out.append((e.dxftype(), e.dxf.layer))
    return sorted(out, key=str)


def prova_giusto():
    from tests.accesso_aiuto import postazioni, entra, modalita
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')

    base = {'quantita': 2, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.8, 'n_forature': 3, 'area_dm2': 2.9, 'bbox_w_mm': 200, 'bbox_h_mm': 100,
            'dxf_confidence': 0.45, 'dxf_needs_verify': True,
            'esito_verifica': {'stato': 'da_guardare', 'motivi': ['contorno incerto']}}
    codici = ['G-VEC-00', 'G-NUO-00', 'G-ATT-00', 'G-FAL-00']
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente='777')
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [dict(base, codice=c, dxf_filename=c + '.dxf') for c in codici])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    arts = {a['codice']: a['id'] for a in (PreventivoManager.get(pid, include_children=True) or {})['articoli']}
    cartella = os.path.join(A.DRAWINGS_FOLDER, oid)
    os.makedirs(cartella, exist_ok=True)
    for c in codici:
        piastra(os.path.join(cartella, c + '.dxf'))     # stesso disegno per tutti
    lamiera = A._nome_lamiera('S235', 3)
    pronti = os.path.join(cartella, 'LANTEK', lamiera)
    # come un ordine caricato col pacchetto: disegni dubbi "da preparare"
    da_prep = os.path.join(cartella, 'LANTEK', A.CARTELLA_DA_PREPARARE, lamiera)
    os.makedirs(pronti)
    os.makedirs(da_prep)
    for c in codici:
        shutil.copy2(os.path.join(cartella, c + '.dxf'), os.path.join(da_prep, c + '.dxf'))

    print('\n2) Contorno preciso nel lavoratore')
    perc = os.path.join(cartella, 'G-VEC-00.dxf')
    det = AL._lettura_motore(perc)
    gj = json.loads(A._get_dxf_geometry_cached(perc, AL._cfg_riconoscimento()) or '{}')
    sel = next((c for c in det.get('candidates') or [] if c.get('is_selected')), None)
    k = float(gj.get('scala_unita_mm') or 1.0)
    nel_server = ai.contorno_preciso(perc, sel['geometry'], float(sel['area_dm2']), k, AL._cfg_riconoscimento())
    isolato = ai.contorno_preciso_isolato(perc, sel['geometry'], float(sel['area_dm2']), k, AL._cfg_riconoscimento())
    check('trovato, coi 4 fori', nel_server and len(nel_server[1]) == 4, nel_server and len(nel_server[1]))
    check('uguale a quello del server', json.loads(json.dumps(nel_server)) == json.loads(json.dumps(isolato)))

    laser.get(f'/api/orders/{oid}/laser?fresco=1')      # la pagina legge l'ordine

    print('\n3) "giusto" preparato contro "giusto" di prima')
    vero_prep = AL._prep_per_giusto
    AL._prep_per_giusto = lambda percorso: None          # come prima: tutto al momento
    r = laser.post(f'/api/orders/{oid}/pezzi/{arts["G-VEC-00"]}/giusto', json={'serve_pulito': True})
    AL._prep_per_giusto = vero_prep
    vec = r.get_json() or {}
    check('come prima: contorno salvato', r.status_code == 200 and vec.get('modo') == 'contorno', (r.status_code, vec))

    r = laser.get(f'/api/orders/{oid}/pezzi/{arts["G-NUO-00"]}/lettura')
    check('lettura 200', r.status_code == 200)
    chiave = AL._chiave_prep(os.path.join(cartella, 'G-NUO-00.dxf'))
    voce = AL._PREP.get(chiave)
    check('la lettura ha avviato la preparazione', voce is not None)
    check('preparazione finita', voce is not None and voce['fatto'].wait(60) and voce['esito']
          and voce['esito']['preciso'], voce and voce.get('esito'))
    t = time.perf_counter()
    r = laser.post(f'/api/orders/{oid}/pezzi/{arts["G-NUO-00"]}/giusto', json={'serve_pulito': True})
    ms = (time.perf_counter() - t) * 1000
    nuo = r.get_json() or {}
    check('preparato: contorno salvato', r.status_code == 200 and nuo.get('modo') == 'contorno', (r.status_code, nuo))
    print(f'     "giusto" preparato: {ms:.0f} ms')
    check('risponde in fretta (< 1,5 s, limite largo)', ms < 1500, ms)
    togli = ('file_lantek', 'motivo')
    check('stessa risposta (a parte il nome del file)',
          {k: v for k, v in vec.items() if k not in togli} == {k: v for k, v in nuo.items() if k not in togli},
          (vec, nuo))
    check('stesso motivo', vec.get('motivo', '').replace('G-VEC-00', 'X') == nuo.get('motivo', '').replace('G-NUO-00', 'X'))
    ev, en = extra(arts['G-VEC-00']), extra(arts['G-NUO-00'])
    check('stessa riga del pezzo (area, perimetro, fori, origine)', ev[:4] == en[:4], (ev[:4], en[:4]))
    check('stesso contorno salvato e stesse misure',
          ev[4].get('contorno_scelto') == en[4].get('contorno_scelto')
          and (ev[4]['bbox_w_mm'], ev[4]['bbox_h_mm']) == (en[4]['bbox_w_mm'], en[4]['bbox_h_mm'])
          and ev[4]['decisione_laser']['scelta'] == en[4]['decisione_laser']['scelta'] == 'giusto')
    fv, fn = os.path.join(pronti, 'G-VEC-00.dxf'), os.path.join(pronti, 'G-NUO-00.dxf')
    check('stesso DXF pulito per Lantek', os.path.isfile(fv) and os.path.isfile(fn)
          and geometria_dxf(fv) == geometria_dxf(fn))
    check('preparazione usata e tolta', chiave not in AL._PREP)
    r = laser.post(f'/api/orders/{oid}/pezzi/{arts["G-NUO-00"]}/giusto', json={'serve_pulito': True})
    check('di nuovo: ora e\' la conferma di sempre', r.status_code == 200 and r.get_json().get('modo') == 'conferma',
          r.get_json())

    print('\n4) Preparazione in corso / non riuscita')
    perc_att = os.path.join(cartella, 'G-ATT-00.dxf')
    calcoli = []
    vero_calcola = AL._calcola_prep

    def lento(p):
        calcoli.append(p)
        time.sleep(0.5)
        return vero_calcola(p)
    AL._calcola_prep = lento
    try:
        o, a, p, _ = AL._pezzo(oid, arts['G-ATT-00'])
        AL.prepara_giusto(o, a, p)
        time.sleep(0.2)                                  # parte in sottofondo
        r = laser.post(f'/api/orders/{oid}/pezzi/{arts["G-ATT-00"]}/giusto', json={'serve_pulito': True})
        check('aspetta quella in corso: contorno salvato', r.status_code == 200 and r.get_json().get('modo') == 'contorno',
              r.get_json())
        check('calcolata una volta sola', calcoli == [perc_att], calcoli)
    finally:
        AL._calcola_prep = vero_calcola

    def rotto(p):
        raise RuntimeError('prova')
    AL._calcola_prep = rotto
    try:
        r = laser.post(f'/api/orders/{oid}/pezzi/{arts["G-FAL-00"]}/giusto', json={'serve_pulito': True})
        check('preparazione non riuscita: si fa come prima', r.status_code == 200 and r.get_json().get('modo') == 'contorno',
              r.get_json())
    finally:
        AL._calcola_prep = vero_calcola
    check('pulito anche per questi due', all(os.path.isfile(os.path.join(pronti, c + '.dxf')) for c in ('G-ATT-00', 'G-FAL-00')))


def main():
    try:
        prova_verifica()
        prova_giusto()
    finally:
        ai.chiudi_tutti()
    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    for k in KO:
        print('  -', k)
    shutil.rmtree(_CARTELLE, ignore_errors=True)
    shutil.rmtree(_ESEMPI, ignore_errors=True)
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
