"""Disegni DWG da tutte le strade d'ingresso (preventivatore e ordine).

"Perche' il preventivatore non riesce a leggere i DWG?": il DWG si
convertiva solo caricando UN file; piu' file insieme, la cartella e il
pacchetto ZIP lo scartavano ("Batch supporta solo .dxf") o lo passavano al
motore cosi' com'era. Ora c'e' una sola conversione (preventivi/
dwg_conversione.py, ODA File Converter senza finestre, nel lavoratore
separato) usata ovunque. Qui:

 1. conversione: tutti i DWG in una chiamata, nessuna finestra sul desktop,
    il DWG rotto torna "DWG non leggibile: ..." e non ferma gli altri
 2. cache per impronta: lo stesso DWG non si riconverte
 3. tempo massimo: un convertitore bloccato si ferma (con i figli) e il
    disegno torna "DWG non leggibile: ... secondi"
 4. stesso riconoscimento del DXF da cui il DWG e' nato
 5. import di piu' file (cartella): DXF + DWG + DWG rotto + DWG col suo DXF
 6. import di un file DWG: il DWG resta accanto al DXF
 7. pacchetto dell'ufficio (da-pacchetto): DWG convertiti, rotto segnalato
 8. ordine: il DWG originale va nei disegni dell'ordine accanto al DXF

I DWG di prova si fanno al momento da DXF generati (niente disegni dei
clienti nel repository). Serve ODA File Converter: senza, il test si salta.
Gira su DATABASE e CARTELLE TEMPORANEI. Esecuzione:
    python app/tests/test_dwg_import.py
"""
import ctypes
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_dwg_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend.database import PreventivoManager, OrderManager  # noqa: E402
from backend.preventivi import dxf_cache  # noqa: E402
from backend.preventivi import analisi_isolata as AI  # noqa: E402
from backend.preventivi import dwg_conversione as DC  # noqa: E402
from backend.preventivi.dxf_batch_worker import process_single_dxf  # noqa: E402
from backend import lantek as _LT  # noqa: E402
_LT.pezzi_in_lantek = lambda codici: {'disponibile': False, 'errore': 'simulato', 'pezzi': {}}

_CARTELLE = tempfile.mkdtemp(prefix='ft_dwg_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
A.PDFS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'pdfs')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
os.makedirs(A.PDFS_FOLDER, exist_ok=True)
dxf_cache._CACHE_DB_PATH = os.path.join(_CARTELLE, 'dxf_cache.db')
DC.CACHE_DIR = os.path.join(_CARTELLE, 'cache_dwg')

CFG = {'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1], 'dxf_lunghezza_minima': 15.0,
       'dxf_tolleranza_centro': 1.0, 'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
       'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
       'dxf_filtra_zona_sviluppata': True}

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


def dxf_piastra(w=200, h=100, fori=4):
    import ezdxf
    doc = ezdxf.new('R2018')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    for i in range(fori):
        msp.add_circle((20 + i * 40, h / 2), 5)
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode('utf-8')


def a_dwg(dxf_per_nome: dict) -> dict:
    """{nome: bytes DXF} -> {nome.dwg: bytes DWG}, con ODA (senza finestre)."""
    cart = tempfile.mkdtemp(prefix='ft_dwg_prep_')
    cin, cout = os.path.join(cart, 'in'), os.path.join(cart, 'out')
    os.makedirs(cin)
    os.makedirs(cout)
    for n, b in dxf_per_nome.items():
        with open(os.path.join(cin, n + '.dxf'), 'wb') as f:
            f.write(b)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    subprocess.run([DC.trova_oda(), cin, cout, 'ACAD2018', 'DWG', '0', '1', '*.DXF'],
                   startupinfo=si, creationflags=0x08000000, timeout=120,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = {}
    for n in dxf_per_nome:
        with open(os.path.join(cout, n + '.dwg'), 'rb') as f:
            out[n + '.dwg'] = f.read()
    shutil.rmtree(cart, ignore_errors=True)
    return out


class Sorveglianza:
    """Durante la conversione: finestre VISIBILI di processi ODA (EnumWindows)."""

    def __init__(self):
        import psutil
        self.psutil = psutil
        self.viste, self.oda = set(), set()
        self.fermo = threading.Event()
        self.th = threading.Thread(target=self._giro, daemon=True)

    def _giro(self):
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        pids = set()

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(hwnd, _):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(200)
                user32.GetWindowTextW(hwnd, buf, 200)
                self.viste.add(buf.value)
            return True
        while not self.fermo.is_set():
            pids = {p.pid for p in self.psutil.process_iter(['name'])
                    if 'odafileconverter' in (p.info['name'] or '').lower()}
            self.oda |= pids
            if pids:
                user32.EnumWindows(cb, 0)
            time.sleep(0.01)

    def __enter__(self):
        self.th.start()
        return self

    def __exit__(self, *a):
        time.sleep(0.2)
        self.fermo.set()
        self.th.join()


def scrivi(cartella, nome, dati):
    os.makedirs(cartella, exist_ok=True)
    p = os.path.join(cartella, nome)
    with open(p, 'wb') as f:
        f.write(dati)
    return p


def pdf_ordine(righe):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    testo = ['Officine Rossi S.r.l.', 'Via Roma 1 - 20100 Milano', 'Ordine Fornitore 4522 01/09/2026',
             'Codice Commessa Descrizione Q.ta U.M. Prezzo Consegna'] + righe + ['Note', 'fine']
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for riga in testo:
        c.drawString(40, y, riga)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def geo(r):
    g = (r or {}).get('geometria') or {}
    return (round(g.get('area_dm2') or 0, 3), round(g.get('perimetro_taglio_m') or 0, 3), g.get('n_forature'))


def main():
    if not DC.trova_oda():
        print('ODA File Converter non installato: test saltato.')
        return 0
    piastre = {'PZ-D100-00': dxf_piastra(200, 100, 4), 'PZ-D200-00': dxf_piastra(300, 150, 3),
               'PZ-D300-00': dxf_piastra(120, 80, 2), 'PZ-DOPPIO-00': dxf_piastra(90, 90, 1)}
    dwg = a_dwg(piastre)
    rotto = dwg['PZ-D100-00.dwg'][:len(dwg['PZ-D100-00.dwg']) // 3]

    print('\n1) Conversione: tutti in una chiamata, senza finestre, il rotto non ferma gli altri')
    d1 = os.path.join(_CARTELLE, 'c1')
    coppie = [(scrivi(d1, n, b), os.path.join(d1, n[:-4] + '.dxf')) for n, b in dwg.items()]
    coppie.append((scrivi(d1, 'ROTTO.dwg', rotto), os.path.join(d1, 'ROTTO.dxf')))
    coppie.append((scrivi(d1, 'NONDWG.dwg', b'questo non e un disegno' * 40), os.path.join(d1, 'NONDWG.dxf')))
    with Sorveglianza() as sv:
        t0 = time.time()
        es = AI.converti_dwg(coppie)
        dur = time.time() - t0
    buoni = [a for a, _b in coppie[:4]]
    check('i 4 DWG buoni convertiti', all(es[os.path.abspath(a)]['ok'] for a in buoni), es)
    check('il DXF scritto accanto', all(os.path.isfile(b) for _a, b in coppie[:4]))
    e_r = es[os.path.abspath(coppie[4][0])]
    e_n = es[os.path.abspath(coppie[5][0])]
    check('DWG troncato: "DWG non leggibile: file incompleto"',
          not e_r['ok'] and e_r['errore'].startswith('DWG non leggibile:') and 'incompleto' in e_r['errore'], e_r)
    check('file che non e\' un DWG: "DWG non leggibile"', not e_n['ok'] and 'DWG non leggibile' in e_n['errore'], e_n)
    check('nessun DXF per i rotti', not os.path.exists(coppie[4][1]) and not os.path.exists(coppie[5][1]))
    check('ODA c\'era (una sola chiamata)', len(sv.oda) == 1, sv.oda)
    check('nessuna finestra visibile sul desktop', not sv.viste, sv.viste)
    check('in pochi secondi', dur < 30, round(dur, 1))

    print('\n2) Cache per impronta')
    t0 = time.time()
    es2 = DC.converti_percorsi([(a, b + '.bis.dxf') for a, b in coppie[:4]])
    check('al secondo giro dalla cache, senza ODA',
          all(e['da_cache'] for e in es2.values()) and time.time() - t0 < 3, es2)

    print('\n3) Tempo massimo: un convertitore bloccato si ferma, con i suoi figli')
    finto = scrivi(os.path.join(_CARTELLE, 'finto'), 'odafinto.bat', b'@ping -n 60 127.0.0.1 > nul\r\n')
    vero, DC.trova_oda = DC.trova_oda, (lambda: finto)
    vecchi = DC._TEMPO_BASE_S, DC._TEMPO_PER_DISEGNO_S
    DC._TEMPO_BASE_S, DC._TEMPO_PER_DISEGNO_S = 1.0, 0.5
    d3 = os.path.join(_CARTELLE, 'c3')
    nuovi = [(scrivi(d3, f'X{i}.dwg', dwg['PZ-D200-00.dwg'] + bytes([i]) * 10), os.path.join(d3, f'X{i}.dxf'))
             for i in range(2)]
    import psutil
    prima = {p.pid for p in psutil.process_iter() if (p.name() or '').lower() == 'ping.exe'}
    t0 = time.time()
    es3 = DC.converti_percorsi(nuovi, tempo_max=2)
    dur = time.time() - t0
    DC.trova_oda = vero
    DC._TEMPO_BASE_S, DC._TEMPO_PER_DISEGNO_S = vecchi
    time.sleep(0.5)
    dopo = {p.pid for p in psutil.process_iter() if (p.name() or '').lower() == 'ping.exe'}
    check('fermato: lotto + un tentativo a testa, non minuti', dur < 15, round(dur, 1))
    check('torna "DWG non leggibile: ... secondi"',
          all((not e['ok']) and 'secondi' in e['errore'] for e in es3.values()), es3)
    check('nessun processo lasciato acceso', not (dopo - prima), dopo - prima)

    print('\n4) Stesso riconoscimento del DXF di partenza')
    for n in ('PZ-D100-00', 'PZ-D200-00'):
        da = os.path.join(_CARTELLE, 'r4a')
        ra = process_single_dxf(scrivi(da, n + '.dxf', piastre[n]), n + '.dxf', CFG)
        rb = AI.analizza(os.path.join(d1, n + '.dxf'), n + '.dxf', CFG)
        check(f'{n}: area, perimetro e fori uguali', geo(ra) == geo(rb) and geo(rb)[0] > 0, (geo(ra), geo(rb)))

    from tests.accesso_aiuto import postazioni, persona, entra, modalita, stazione_pronta
    modalita('protetto')
    postazioni()
    s = models.SessionLocal()
    s.add(User(id='commerciale', name='Marco', role='Commerciale', is_active=True))
    s.commit()
    s.close()

    print('\n5) Import di piu\' file (cartella): DXF + DWG + DWG rotto + DWG col suo DXF')
    comm = stazione_pronta(A.app, 'commerciale')
    pid = PreventivoManager.create('Cliente DWG', 'commerciale', quantita=1)['id']
    files = [(io.BytesIO(piastre['PZ-D300-00']), 'PZ-D300-00.dxf'),
             (io.BytesIO(dwg['PZ-D100-00.dwg']), 'PZ-D100-00.dwg'),
             (io.BytesIO(dwg['PZ-D200-00.dwg']), 'PZ-D200-00.DWG'),
             (io.BytesIO(dwg['PZ-D300-00.dwg']), 'PZ-D300-00.dwg'),
             (io.BytesIO(rotto), 'PZ-ROTTO-00.dwg')]
    paths = ['CART/' + n for _b, n in files]
    r = comm.post(f'/api/preventivi/{pid}/import-dxf-batch', data={'files': files, 'paths': paths},
                  content_type='multipart/form-data')
    j = r.get_json() or {}
    per = {x.get('filename'): x for x in j.get('results') or []}
    check('risponde 200', r.status_code == 200 and j.get('success'), (r.status_code, j.get('error')))
    check('DWG convertiti e letti (PZ-D100, PZ-D200)',
          all(per.get(n, {}).get('success') and geo(per[n])[0] > 0 for n in ('PZ-D100-00.dxf', 'PZ-D200-00.dxf')),
          list(per))
    check('il risultato dice da che DWG viene', (per.get('PZ-D200-00.dxf') or {}).get('dwg_originale') == 'PZ-D200-00.DWG',
          per.get('PZ-D200-00.dxf', {}).get('dwg_originale'))
    dop = per.get('PZ-D300-00.dwg') or {}
    check('DWG con anche il DXF: usato il DXF, detto', dop.get('saltato') and 'uso il DXF' in (dop.get('error') or ''),
          dop)
    check('il DXF del cliente letto', (per.get('PZ-D300-00.dxf') or {}).get('success'))
    ro = per.get('PZ-ROTTO-00.dwg') or {}
    check('DWG rotto: errore per nome, "DWG non leggibile: ..."',
          ro.get('success') is False and not ro.get('saltato') and (ro.get('error') or '').startswith('DWG non leggibile'),
          ro)
    cart = A._cartella_preventivo(pid)
    nomi = set(os.listdir(cart))
    check('nella cartella: DWG originale e DXF convertito', {'PZ-D100-00.dwg', 'PZ-D100-00.dxf'} <= nomi, nomi)
    check('il DWG rotto non resta', 'PZ-ROTTO-00.dwg' not in nomi, nomi)
    check('nessun messaggio "Batch supporta solo .dxf"', 'supporta solo' not in r.get_data(as_text=True))

    print('\n6) Import di un file DWG')
    pid2 = PreventivoManager.create('Cliente DWG 2', 'commerciale', quantita=1)['id']
    r = comm.post(f'/api/preventivi/{pid2}/import-dxf',
                  data={'file': (io.BytesIO(dwg['PZ-D200-00.dwg']), 'PZ-D200-00.dwg')},
                  content_type='multipart/form-data')
    j = r.get_json() or {}
    check('letto', r.status_code == 200 and j.get('success') and geo(j)[0] > 0, (r.status_code, j.get('error')))
    check('pezzo sul DXF, DWG originale accanto',
          j.get('filename') == 'PZ-D200-00.dxf' and j.get('dwg_originale') == 'PZ-D200-00.dwg', j.get('filename'))
    r = comm.post(f'/api/preventivi/{pid2}/import-dxf',
                  data={'file': (io.BytesIO(rotto), 'ROTTO.dwg')}, content_type='multipart/form-data')
    j = r.get_json() or {}
    check('DWG rotto: errore chiaro, non 500', r.status_code == 422
          and (j.get('error') or '').startswith('DWG non leggibile'), (r.status_code, j))
    r = comm.get(f'/api/preventivi/{pid2}/dwg/PZ-D200-00.dwg')
    check('il DWG originale si scarica', r.status_code == 200 and r.data == dwg['PZ-D200-00.dwg'], r.status_code)
    # salvato sul pezzo e riletto
    PreventivoManager.replace_articoli(pid2, [{'codice': 'PZ-D200-00', 'quantita': 2, 'materiale': 'S235',
                                               'spessore_mm': 3, 'dxf_filename': 'PZ-D200-00.dxf',
                                               'dwg_filename': 'PZ-D200-00.dwg'}])
    art = (PreventivoManager.get(pid2, include_children=True) or {}).get('articoli') or [{}]
    check('dwg_filename salvato sul pezzo', art[0].get('dwg_filename') == 'PZ-D200-00.dwg', art[0].get('dwg_filename'))

    print('\n7) Pacchetto dell\'ufficio: PDF d\'ordine + DWG')
    persona('ufficio', 'Amministrazione', 'Amministrazione', pin='2580')
    uff = entra(A.app, 'ufficio', pin='2580')
    cons = (datetime.now() + timedelta(days=60)).strftime('%d/%m/%y')
    pdf = pdf_ordine([f'PZ-D100-00 C26-101 Piastra sp.3 S235JR 4,00Nr {cons}',
                      f'PZ-D200-00 C26-101 Piastra sp.3 S235JR 2,00Nr {cons}',
                      f'PZ-DOPPIO-00 C26-101 Piastra sp.3 S235JR 1,00Nr {cons}',
                      f'PZ-ROTTO-00 C26-101 Piastra sp.3 S235JR 3,00Nr {cons}'])
    files = [(io.BytesIO(pdf), 'ORDINE 4522.pdf'),
             (io.BytesIO(dwg['PZ-D100-00.dwg']), 'PZ-D100-00.dwg'),
             (io.BytesIO(dwg['PZ-D200-00.dwg']), 'PZ-D200-00.dwg'),
             (io.BytesIO(dwg['PZ-DOPPIO-00.dwg']), 'PZ-DOPPIO-00.dwg'),
             (io.BytesIO(piastre['PZ-DOPPIO-00']), 'PZ-DOPPIO-00.dxf'),
             (io.BytesIO(rotto), 'PZ-ROTTO-00.dwg')]
    paths = ['P/' + n for _b, n in files]
    r = uff.post('/api/orders/da-pacchetto/analizza', data={'files': files, 'paths': paths},
                 content_type='multipart/form-data')
    d = r.get_json() or {}
    righe = {x.get('codice'): x for x in d.get('righe') or []}
    check('risponde 200', r.status_code == 200 and d.get('success'), (r.status_code, d.get('error')))
    a1, a2 = righe.get('PZ-D100-00') or {}, righe.get('PZ-D200-00') or {}
    check('DWG convertiti: pezzi letti con anteprima',
          all((x.get('area_dm2') or 0) > 0 and x.get('url_svg') and (x.get('disegno') or '').endswith('.dxf')
              for x in (a1, a2)), (a1, a2))
    check('quantita\' dall\'ordine', a1.get('quantita') == 4 and a2.get('quantita') == 2, (a1.get('quantita'), a2.get('quantita')))
    check('la riga ricorda il DWG', a1.get('disegno_dwg') == 'PZ-D100-00.dwg', a1.get('disegno_dwg'))
    dp = righe.get('PZ-DOPPIO-00') or {}
    check('DWG + DXF dello stesso codice: un pezzo solo, sul DXF', dp.get('disegno') == 'PZ-DOPPIO-00.dxf'
          and not dp.get('disegno_dwg') and list(righe).count('PZ-DOPPIO-00') == 1, dp)
    check('detto negli avvisi', any('uso il DXF' in a for a in d.get('avvisi') or []), d.get('avvisi'))
    ro = righe.get('PZ-ROTTO-00') or {}
    check('DWG rotto: la riga resta, "DWG non leggibile" detto all\'ufficio',
          ro and not ro.get('disegno') and any('DWG non leggibile' in a for a in ro.get('avvisi') or []), ro)
    check('e negli avvisi del pacchetto', any('DWG non leggibili' in a for a in d.get('avvisi') or []))
    pk = d.get('pacchetto_id')
    nomi = set(os.listdir(A._cartella_preventivo(pk))) if pk else set()
    check('nella cartella del pacchetto: DXF convertito e DWG originale',
          {'PZ-D100-00.dxf', 'PZ-D100-00.dwg'} <= nomi, nomi)

    print('\n8) Ordine: il DWG originale va con i disegni dell\'ordine')
    o = OrderManager.create_order(cliente='Cliente DWG 2', data_consegna=(datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d'),
                                  numero_ordine='T-DWG-1')
    oid = o.id if hasattr(o, 'id') else o['id']
    st = A._copy_cleaned_dxf_to_drawings(pid2, oid)
    dd = os.path.join(A.DRAWINGS_FOLDER, oid)
    nomi = set(os.listdir(dd)) if os.path.isdir(dd) else set()
    check('DXF e DWG nella cartella dell\'ordine', {'PZ-D200-00.dxf', 'PZ-D200-00.dwg'} <= nomi, (nomi, st.get('warnings')))
    r = uff.get(f'/api/orders/{oid}/disegni')
    dis = {x['nome']: x for x in (r.get_json() or {}).get('disegni') or []}
    check('elenco disegni dell\'ordine: tutti e due, anteprima solo per il DXF',
          'PZ-D200-00.dwg' in dis and dis['PZ-D200-00.dwg']['url_svg'] is None
          and dis.get('PZ-D200-00.dxf', {}).get('url_svg'), list(dis))

    AI.chiudi_tutti()
    print(f'\nRisultato: {OK} ok, {len(KO)} falliti')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        try:
            AI.chiudi_tutti()
            _ENG.dispose()
            os.remove(_TMP)
        except Exception:
            pass
        shutil.rmtree(_CARTELLE, ignore_errors=True)
    sys.exit(code)
