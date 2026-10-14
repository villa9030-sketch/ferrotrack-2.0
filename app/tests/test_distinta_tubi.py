"""Tubi riconosciuti nella distinta dell'assieme (DECA A 001221 BIS, 14/10/2026).

L'assieme 26T30SA0233-00 "Struttura x MTR": nella sua distinta cinque righe
sono sotto-assiemi di TUBO ma la descrizione non dice "tubo" ("Sostegno per
traversa alta telaio inferiore MTR - Lg 581"); il profilo sta nella distinta
del disegno del componente ("1 80105L2CB0055 00 T.Quadro Aisi304 50x 50x 3
L. 581 AISI 304 1"), che pdfplumber non vede come tabella (tutto in una
cella). Prima: nessuno dei cinque proposto come tubo. Le tre piastre
191700045/112/114 restano pezzi laser.

Copre:
 A. righe della distinta dal TESTO, "T.Rett...", lunghezza "Lg 581"/"L. 581"
    (e "L 50 x 5" NON e' una lunghezza)
 B. (se c'e' il pacchetto vero ls 1221) su una COPIA del pacchetto, preventivo
    rifatto dall'import su database temporaneo: "Leggi distinta dal PDF"
    (/distinte) propone i 5 tubi con profilo e lunghezza, non le piastre

Database e cartelle temporanei. python app/tests/test_distinta_tubi.py
"""
import io
import os
import shutil
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_tubi_esempi_')

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_tubi_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend.preventivi import dxf_cache, distinte_pdf as DP  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_tubi_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
A.PDFS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'pdfs')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
os.makedirs(A.PDFS_FOLDER, exist_ok=True)
dxf_cache._CACHE_DB_PATH = os.path.join(_CARTELLE, 'dxf_cache.db')
L.pezzi_in_lantek = lambda codici: {'disponibile': False, 'errore': 'simulato', 'pezzi': {}}
L.importa_pezzi_dxf = lambda *a, **k: {'eseguito': False, 'errore': 'bloccato nel test'}
L.importa_xml = lambda *a, **k: {'eseguito': False, 'errore': 'bloccato nel test'}

PACCHETTO = r'C:\Users\Stefano\Desktop\AUTOCAD\TAGLIO\DECA\ORDINI\ls 1221\ls 1221'
TUBI = {'26CCSA0038': ('Quadro', 581), '26CCSA0039': ('Quadro', 895), '26CCSA0040': ('150x50', 595),
        '26CCSA0041': ('Quadro', 304), '26CCSA0042': ('150x50', 595)}
PIASTRE = ('191700045', '191700112', '191700114')

OK, KO = 0, []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome}  {extra}')


def main():
    print('A) Distinta dal testo, profili e lunghezze')
    testo = ('Revisione Descrizione\nNum. art. Codice REV Descrizione Descrizione Aggiuntiva Materiale TrattamentoQtà\n'
             '1 1 80105L2CB0004 00 T.Quadro Aisi304 50x 50x 3 L. 304 AISI 304 1\n'
             'Materiale Peso Kg Grado di finitura Trattamento\n')
    rr = DP._righe_testo(testo)
    check('riga letta dal testo (posizione ripetuta)', len(rr) == 1 and rr[0]['codice'] == '80105L2CB0004'
          and rr[0]['qta'] == 1 and rr[0]['lunghezza_mm'] == 304, rr)
    p = DP.profilo_da_descrizione('T.RettSaldato Aisi304 150x 50x 3 L. 595')
    check('"T.RettSaldato 150x50x3" e\' un tubo rettangolare', p and p['tipo'] == 'RHS' and p['kg_m'] > 0, p)
    check('"Lg 581" nella descrizione aggiuntiva', DP.lunghezza_da_testo('', 'inferiore MTR - Lg 581 - AISI304') == 581)
    check('"L 50 x 5" non e\' una lunghezza', DP.lunghezza_da_testo('Angolare L 50 x 5') is None)
    check('piastra: nessun profilo', DP.profilo_da_descrizione('Chiusura x tubolare 50x50 (50x43,6 sp.5)') is None)

    print('\nC) "È un tubo": la riga tubo tiene codice, disegni e il pezzo di prima')
    s = models.SessionLocal()
    s.add(User(id='prova-tubo', name='Prova', role='Commerciale', is_active=True))
    s.commit(); s.close()
    pv = A.PreventivoManager.create('DECA', 'prova-tubo', numero_ordine_cliente='T-1')
    pid0 = pv['id']
    pezzo = {'codice': '26CCSA0039-00', 'quantita': 2, 'codice_assieme': '26T30SA0233-00', 'materiale': 'INOX_304',
             'spessore_mm': 3, 'area_dm2': 3.0, 'perimetro_taglio_m': 2.0, 'dxf_filename': '26CCSA0039-00.dxf',
             'pdf_filename': '26CCSA0039-00.pdf'}
    tubo = {'codice_assieme': '26T30SA0233-00', 'profilo': 'Quadro 50x50 sp.3mm', 'tipo': 'SHS', 'materiale': 'acciaio',
            'qty': 2, 'lunghezza_m': 0.895, 'lunghezza_pezzo_mm': 895, 'kg_m': 4.25, 'peso_kg': 7.6,
            'costo_materiale': 11.4, 'n_tagli_dritti': 4, 'n_tagli_obliqui': 0, 'costo_taglio_totale': 4,
            'costo_esterno': 0, 'nota_esterno': 'Taglio laser tubo esterno', 'origine': 'pezzo',
            'posizione': '26CCSA0039-00', 'codice_pezzo': '26CCSA0039-00', 'disegno_pdf': '26CCSA0039-00.pdf',
            'disegno_dxf': '26CCSA0039-00.dxf', 'da_pezzo': pezzo}
    A.PreventivoManager.replace_assiemi(pid0, [{'codice_assieme': '26T30SA0233-00', 'qty': 3}])
    A.PreventivoManager.replace_tubolari(pid0, [tubo])
    prev = A.PreventivoManager.get(pid0)
    t = (prev.get('tubolari') or [{}])[0]
    check('salvati codice, disegni e pezzo di prima', t.get('codice_pezzo') == '26CCSA0039-00'
          and t.get('disegno_pdf') == '26CCSA0039-00.pdf' and (t.get('da_pezzo') or {}).get('dxf_filename') == '26CCSA0039-00.dxf', t)
    righe = A._distinta_da_preventivo(prev, {'26ccsa0039-00.pdf': {'nome': '26CCSA0039-00.pdf'}})
    rt = [r for r in righe if r['tipo'] == 'tubolare']
    check('distinta: riga tubo col codice del pezzo e il suo disegno', len(rt) == 1 and rt[0]['codice'] == '26CCSA0039-00'
          and rt[0]['disegno'] == '26CCSA0039-00.pdf' and rt[0]['quantita'] == 6, rt)
    check('distinta: nessuna lamiera per Lantek', not any(r['tipo'] == 'lamiera' for r in righe))
    from backend import procura
    voci = list(procura.voci_tubolari(righe).values())
    check('Da procurare: laser tubo (esterno), 6 pezzi da 895', len(voci) == 1 and voci[0]['tipo'] == 'laser_tubo'
          and voci[0]['quantita'] == 6 and voci[0]['lunghezza_mm'] == 895 and voci[0]['codice'] == '26CCSA0039-00', voci)

    print('\nB) Pacchetto vero A 001221 BIS (copia)')
    if not os.path.isdir(PACCHETTO):
        print('  (pacchetto non presente: saltato)')
    else:
        copia = os.path.join(_CARTELLE, 'ls 1221')
        shutil.copytree(PACCHETTO, copia, ignore=shutil.ignore_patterns('Thumbs.db', '*.step', '*.stp'))
        s = models.SessionLocal()
        s.add(User(id='comm', name='Commerciale', role='Commerciale', is_active=True))
        s.commit(); s.close()
        from tests.accesso_aiuto import postazioni, persona, entra, modalita
        modalita('protetto')
        postazioni()
        persona('comm', 'Commerciale', 'Commerciale', pin='3691')
        comm = entra(A.app, 'commerciale', pin='3691')
        files, paths = [], []
        for dp, _, fs in os.walk(copia):
            for f in sorted(fs):
                pth = os.path.join(dp, f)
                with open(pth, 'rb') as fh:
                    files.append((io.BytesIO(fh.read()), f))
                paths.append('ls 1221/' + os.path.relpath(pth, copia).replace(os.sep, '/'))
        r = comm.post('/api/preventivi/import-rfq-package', data={'files': files, 'paths': paths, 'force': '1'},
                      content_type='multipart/form-data')
        d = r.get_json() or {}
        check('preventivo rifatto dal pacchetto', r.status_code == 200 and d.get('success'), d.get('error'))
        pid = d.get('preventivo_id')
        prev = A.PreventivoManager.get(pid) or {}
        cod = {a['codice'] for a in prev.get('articoli') or []}
        check('i 5 tubi arrivano come pezzi laser (prima della distinta)',
              all(any(c.startswith(k) for c in cod) for k in TUBI), sorted(cod)[:10])
        ass = {x['codice_assieme']: x for x in prev.get('assiemi') or []}
        check('assieme 26T30SA0233-00 presente', '26T30SA0233-00' in ass, list(ass))
        rd = comm.get(f'/api/preventivi/{pid}/distinte?codici=26T30SA0233-00')
        D = (rd.get_json() or {}).get('distinte') or {}
        righe_ass = [x['codice'] for x in (D.get('26T30SA0233') or {}).get('righe') or []]
        check('distinta dell\'assieme: 8 righe', len(righe_ass) == 8, righe_ass)
        riconosciuti = []
        for k, (prof, lung) in TUBI.items():
            sub = D.get(k) or {}
            tubi = [x for x in sub.get('righe') or [] if x.get('profilo')]
            ok = len(tubi) == 1 and prof in tubi[0]['profilo']['nome'] and tubi[0]['lunghezza_mm'] == lung
            if ok:
                riconosciuti.append(k)
            check(f'{k}: tubo {prof} L {lung} proposto', ok, tubi)
        print('    tubi riconosciuti:', ', '.join(riconosciuti))
        for k in PIASTRE:
            sub = D.get(k) or {}
            check(f'{k}: piastra, resta pezzo laser', not any(x.get('profilo') for x in sub.get('righe') or []), sub)

    print('\n' + '=' * 60)
    print(f'{OK} OK, {len(KO)} KO')
    if KO:
        print('KO: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    sys.exit(code)
