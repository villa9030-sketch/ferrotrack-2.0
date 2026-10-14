"""Il preventivo segue le righe del PDF d'ordine del cliente (LS 1153).

Stefano (14/10/2026): "ti avevo detto di rispettare i PDF, ancora
nell'ultimo preventivo gli assiemi erano tutti alla fine". Caso vero:
ordine DECA 1153, 78 righe / 74 codici, assiemi in mezzo ai pezzi.

Copre:
 1. sequenza: assiemi alla loro riga (anche in mezzo), componenti sotto,
    disegni non citati in fondo e segnati; senza ordine letto come prima
 2. abbinamento: 191700420 e 191700421 entrambi esatti; 25CCPA1005 (senza
    disegno) NON prende il disegno di 25CCPA1000; "-G-" = variante
 3. pacchetto: cartella "46SA00597" ↔ riga 46SA00597-00 (assieme rinominato,
    quantita' e descrizione dalla riga); riga senza disegno presente con
    avviso; "_cleaned" non e' un pezzo; componente in due assiemi
 4. PDF del preventivo: righe nell'ordine del cliente, codice come scritto
    dal cliente, riga senza disegno con la sua descrizione
 5. (se c'e') il pacchetto vero LS 1153: 74 codici nell'ordine del PDF

Esecuzione: python app/tests/test_ordine_pdf_sequenza.py
"""
import io
import os
import sys
import uuid
import zipfile

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

import backend.app  # noqa: E402,F401
A = sys.modules['backend.app']
from backend.preventivi.ordine_righe import sequenza, righe_senza_pezzo, chiave_variante  # noqa: E402
from backend.preventivi.rfq_importer import match_dxf_to_articoli, process_rfq_package  # noqa: E402

OK, KO = 0, []
PACCHETTO_1153 = r'C:\Users\Stefano\Desktop\AUTOCAD\TAGLIO\DECA\ORDINI\ls 1153-26\ls 1153-26'


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome} {extra}')


def pdf_ordine(righe):
    """"Ordine Fornitore" nel formato DECA (ordine_testo)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    testo = ['DECA S.r.l.', 'Via della Fontana 6', 'Ordine Fornitore 1153 01/10/2026',
             'Codice Commessa Descrizione Q.ta U.M. Prezzo Consegna'] + righe + ['Note', 'fine']
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for r in testo:
        c.drawString(30, y, r)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


def dxf_finto(n=0):
    return f'0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n{n}\n'.encode()


def zip_pacchetto(pdf, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('ls 1153/OAFA202601153.pdf', pdf)
        for i, f in enumerate(files):
            z.writestr('ls 1153/' + f, dxf_finto(i))
    return buf.getvalue()


def art(codice, pos=None, ass=None, stato='ok'):
    a = {'codice': codice, 'quantita': 1, 'codice_assieme': ass}
    if pos is not None or stato == 'non_trovato':
        a['ordine'] = {'stato': stato, 'pos': pos, 'qta': 1}
    return a


def main():
    print('\n1) Sequenza delle righe')
    righe = [{'pos': 1, 'codice': '13SA0254-00'}, {'pos': 2, 'codice': '18C3X02-00'},
             {'pos': 3, 'codice': '19190A00002-00'}, {'pos': 4, 'codice': '19190P00001-01'},
             {'pos': 5, 'codice': '46SA00597-00'}]
    arts = [art('18C3X02-00', 2), art('19190P00001-01', 4), art('EXTRA-00', stato='non_trovato'),
            art('13PA00094-00', ass='13SA0254-00'), art('19190P00006-00', ass='19190A00002-00'),
            art('46PA00452-00', ass='46SA00597-00')]
    ass = ['13SA0254-00', '19190A00002-00', '46SA00597-00']
    seq = sequenza(arts, ass, righe)
    nomi = [v['codice'] if v['tipo'] == 'assieme' else arts[v['indice']]['codice'] for v in seq]
    check('assiemi alla loro riga, in mezzo ai pezzi',
          nomi == ['13SA0254-00', '18C3X02-00', '19190A00002-00', '19190P00001-01', '46SA00597-00', 'EXTRA-00'], nomi)
    check('disegno non citato: in fondo e segnato', seq[-1]['fuori_ordine'] and not any(v['fuori_ordine'] for v in seq[:-1]))
    # preventivi di prima: righe non salvate, assieme dove la lista mette i suoi pezzi
    arts2 = [art('18C3X02-00', 31), art('19190P00006-00', ass='19190A00002-00'), art('19190P00001-01', 112)]
    seq2 = sequenza(arts2, ['19190A00002-00'], None)
    nomi2 = [v['codice'] if v['tipo'] == 'assieme' else arts2[v['indice']]['codice'] for v in seq2]
    check('senza righe salvate: assieme dove stanno i suoi componenti',
          nomi2 == ['18C3X02-00', '19190A00002-00', '19190P00001-01'], nomi2)
    # preventivo di prima con righe salvate dopo: le pos dei pezzi sono righe
    # di TESTO del PDF (31, 112...), le righe dell'ordine 1, 2, 3: vale la riga
    arts4 = [art('18C3X02-00', 31), art('19190P00001-01', 112), art('19190P00006-00', ass='19190A00002-00'),
             art('18C3X02-00_cleaned', 31)]
    seq4 = sequenza(arts4, ['13SA0254-00', '19190A00002-00'], righe)
    nomi4 = [v['codice'] if v['tipo'] == 'assieme' else arts4[v['indice']]['codice'] for v in seq4]
    check('pos di prima (righe di testo) e righe salvate: vale la riga dell\'ordine',
          nomi4 == ['13SA0254-00', '18C3X02-00', '19190A00002-00', '19190P00001-01', '18C3X02-00_cleaned'], nomi4)
    seq3 = sequenza([art('B'), art('A')], ['ASS'], None)
    check('ordine mai letto: come prima (pezzi, poi assiemi)',
          [v.get('indice', v.get('codice')) for v in seq3] == [0, 1, 'ASS'])
    check('variante -G-: stessa chiave', chiave_variante('191700609-G-00') == chiave_variante('191700609-00'))
    manc = righe_senza_pezzo(righe + [{'pos': 6, 'codice': '25CCPA1005-00'}], arts, ass)
    check('righe dell\'ordine senza pezzo trovate', [r['codice'] for r in manc] == ['25CCPA1005-00'], manc)

    print('\n2) Abbinamento dei disegni')
    m, no = match_dxf_to_articoli(
        [{'codice': c, 'quantita': 1} for c in ('191700420-00', '191700421-00', '25CCPA1005-00', '191700609-G-00')],
        ['191700420-00.dxf', '191700421-00.DXF', '25CCPA1000-00.dxf', '191700609-00.dxf'])
    d = {a.codice: a for a in m}
    check('191700420-00 col suo disegno', d['191700420-00'].matched_dxf == '191700420-00.dxf'
          and d['191700420-00'].abbinamento == 'esatto')
    check('191700421-00 col suo disegno', d['191700421-00'].matched_dxf == '191700421-00.DXF')
    check('25CCPA1005-00 resta senza disegno (numero diverso)', d['25CCPA1005-00'].matched_dxf is None, d['25CCPA1005-00'])
    check('25CCPA1000-00.dxf resta libero', '25CCPA1000-00.dxf' in no)
    check('191700609-G-00 ↔ 191700609-00 come variante',
          d['191700609-G-00'].matched_dxf == '191700609-00.dxf' and d['191700609-G-00'].abbinamento == 'variante')

    print('\n3) Pacchetto con assiemi in mezzo')
    pdf = pdf_ordine([
        '13SA0254-00 C26-191-26 CVE 40 dritta saldata 1,00Nr 02/11/26',
        '191700420-00 C26-191-26 Staffa sensore AISI 304 1,00Nr 02/11/26',
        '191700421-00 C26-191-26 Staffa per sens. AISI 304 2,00Nr 02/11/26',
        '25CCPA1005-00 C26-191-26 Guida scorrimento 40x15 Lg.0500 Polizene 4,00Nr 02/11/26',
        '25CCSA385-00 C26-191-26 Spalla SX saldata 1,00Nr 02/11/26',
        '25NSISA0001-00 C26-191-26 Spalla DX SI saldata 1,00Nr 02/11/26',
        '46PA00455-G-00 C26-191-26 Staffa 220x50 sp.5 1,00Nr 02/11/26',
        '46SA00597-00 C26-191-26 Scivolo 550x200 sp.3 saldato 1,00Nr 02/11/26',
    ])
    files = ['13SA0254-00/13SA0254-00.DXF', '13SA0254-00/13PA00094-00.dxf',
             '191700420-00.dxf', '191700421-00.DXF', '191700421-00_cleaned.DXF',
             '25CCSA385-00/25CCSA385-00.DXF', '25CCSA385-00/25CCPA1000-00.dxf', '25CCSA385-00/191700043-00.dxf',
             '25NSISA0001-00/25NSISA0001-00.DXF', '25NSISA0001-00/191700043-00.dxf',
             '46PA00455-00.dxf',
             '46SA00597/46SA00597-00.DXF', '46SA00597/46PA00452-00.dxf']
    r = process_rfq_package(zip_pacchetto(pdf, files))
    check('pacchetto letto', r.success, r.error)
    cod = [a.codice for a in r.articoli]
    check('191700420-00 e 191700421-00 presenti', '191700420-00' in cod and '191700421-00' in cod, cod)
    check('nessun "_cleaned" come pezzo', not any('_CLEANED' in c.upper() for c in cod), cod)
    check('riga senza disegno presente', '25CCPA1005-00' in cod)
    check('riga senza disegno nell\'elenco e nell\'avviso', r.senza_disegno == ['25CCPA1005-00']
          and any('senza disegno' in w and '25CCPA1005-00' in w for w in r.warnings), r.warnings)
    check('cartella 46SA00597 → assieme 46SA00597-00', '46SA00597-00' in r.assiemi and '46SA00597' not in r.assiemi, r.assiemi)
    check('assieme rinominato: quantita\' e descrizione dalla riga',
          r.qta_assiemi.get('46SA00597-00') == 1 and 'Scivolo' in (r.desc_assiemi.get('46SA00597-00') or ''))
    check('componente dell\'assieme rinominato', any(a.codice == '46PA00452-00' and a.codice_assieme == '46SA00597-00'
                                                    for a in r.articoli))
    comp43 = sorted(a.codice_assieme for a in r.articoli if a.codice == '191700043-00')
    check('componente comune in tutti e due gli assiemi', comp43 == ['25CCSA385-00', '25NSISA0001-00'], comp43)
    check('25CCPA1000-00 resta componente di 25CCSA385-00',
          any(a.codice == '25CCPA1000-00' and a.codice_assieme == '25CCSA385-00' for a in r.articoli))
    # ordine: righe del PDF, componenti subito dopo la riga del loro assieme
    sequenza_cod = [a.codice for a in r.articoli]
    i13, i94 = sequenza_cod.index('13SA0254-00'), sequenza_cod.index('13PA00094-00')
    check('componente subito sotto il suo assieme', i94 == i13 + 1, sequenza_cod)
    check('assieme 25CCSA385 tra 25CCPA1005 e 25NSISA0001',
          sequenza_cod.index('25CCPA1005-00') < sequenza_cod.index('25CCSA385-00') < sequenza_cod.index('25NSISA0001-00'))
    check('righe dell\'ordine salvate con la posizione', [x['pos'] for x in r.righe_ordine] == list(range(1, 9)))
    g = next(a for a in r.articoli if a.codice == '46PA00455-G-00')
    check('codice del cliente come scritto (-G-), disegno 46PA00455-00', g.matched_dxf == '46PA00455-00.dxf')

    print('\n4) PDF del preventivo nell\'ordine del cliente')
    def pz(codice, pos, **kw):
        a = {'id': str(uuid.uuid4()), 'codice': codice, 'quantita': 1, 'materiale': 'S235', 'spessore_mm': 3,
             'area_dm2': 1.0, 'perimetro_taglio_m': 0.5, 'costo_base_stimato': 4.0}
        if pos is not None:
            a['ordine'] = {'stato': 'ok', 'pos': pos, 'qta': 1}
        a.update(kw)
        return a
    arts = [pz('18C3X02-00', 2), pz('191700609-00', 3),
            pz('25CCPA1005-00', 4, materiale='', spessore_mm=None, area_dm2=0, perimetro_taglio_m=0,
               costo_base_stimato=0, costo_base_override=2.5),
            pz('13PA00094-00', None, codice_assieme='13SA0254-00'),
            pz('46PA00452-00', None, codice_assieme='46SA00597-00'),
            pz('EXTRA-00', None)]
    arts[1]['ordine']['codice'] = '191700609-G-00'
    arts[2]['ordine'].update({'senza_disegno': True, 'descrizione': 'Guida scorrimento 40x15 Lg.0500 Polizene'})
    arts[5]['ordine'] = {'stato': 'non_trovato', 'pos': None, 'qta': None}
    prev = {'id': str(uuid.uuid4()), 'cliente': 'DECA', 'quantita': 1, 'margine_pct': 0, 'sconto_pct': 0,
            'numero_ordine_cliente': '1153', 'articoli': arts, 'tubolari': [], 'piastre': [],
            'assiemi': [{'codice_assieme': '13SA0254-00', 'qty': 1}, {'codice_assieme': '46SA00597-00', 'qty': 1}]}
    dd = A._preventivo_to_pdf_dati(prev)
    rc = [x['codice'] for x in dd['righe_cliente']]
    check('assiemi alla loro riga (non in fondo)',
          rc == ['18C3X02-00', '191700609-G-00', '25CCPA1005-00', '13SA0254-00', '46SA00597-00', 'EXTRA-00']
          , rc)
    check('codice del cliente come scritto', '191700609-G-00' in rc and '191700609-00' not in rc)
    r5 = next(x for x in dd['righe_cliente'] if x['codice'] == '25CCPA1005-00')
    check('riga senza disegno: descrizione dell\'ordine e prezzo a mano', 'Polizene' in r5['descrizione'] and r5['importo'] > 0, r5)
    rx = next(x for x in dd['righe_cliente'] if x['codice'] == 'EXTRA-00')
    check('disegno non nell\'ordine segnato', "non nell'ordine" in rx['descrizione'], rx)
    # stessi dati senza alcuna posizione: assiemi in fondo come prima
    for a in arts:
        a.pop('ordine', None)
    rc0 = [x['codice'] for x in A._preventivo_to_pdf_dati(prev)['righe_cliente']]
    check('senza ordine letto: come prima', rc0[-2:] == ['13SA0254-00', '46SA00597-00'], rc0)

    print('\n5) Pacchetto vero LS 1153')
    if not os.path.isdir(PACCHETTO_1153):
        print('  (pacchetto non presente: saltato)')
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as z:
            for dp, _, fs in os.walk(PACCHETTO_1153):
                for f in fs:
                    if os.path.splitext(f)[1].lower() in ('.dxf', '.pdf'):
                        p = os.path.join(dp, f)
                        z.write(p, 'ls 1153-26/' + os.path.relpath(p, PACCHETTO_1153).replace(os.sep, '/'))
        r = process_rfq_package(buf.getvalue())
        from backend.preventivi.ordine_righe import chiave
        cod_ordine = []
        for x in r.righe_ordine:
            if x['codice'] not in cod_ordine:
                cod_ordine.append(x['codice'])
        check('74 codici nell\'ordine', len(cod_ordine) == 74, len(cod_ordine))
        con_riga = [a for a in r.articoli if a.righe_ordine]
        check('74 righe nel preventivo (assiemi compresi)', len(con_riga) == 74, len(con_riga))
        check('nell\'ordine del PDF', [a.codice for a in con_riga] == cod_ordine)
        check('componenti subito sotto il loro assieme', all(
            r.articoli[i - 1].e_master or r.articoli[i - 1].codice_assieme == a.codice_assieme
            for i, a in enumerate(r.articoli) if a.codice_assieme and not a.e_master and not a.righe_ordine))
        check('25CCPA1005-00 e 25CCPA1007-00 senza disegno, segnalate',
              sorted(r.senza_disegno) == ['25CCPA1005-00', '25CCPA1007-00'], r.senza_disegno)
        check('46SA00597-00 assieme della sua riga', '46SA00597-00' in r.assiemi and '46SA00597' not in r.assiemi)
        check('nessun "_cleaned"', not any('_CLEANED' in a.codice.upper() for a in r.articoli))
        check('chiave stabile', chiave('25NSIPA0015-00_cleaned.DXF') == '25NSIPA0015')

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
