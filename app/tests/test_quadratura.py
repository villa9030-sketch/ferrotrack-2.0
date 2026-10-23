# -*- coding: utf-8 -*-
"""Quadratura dell'ordine (backend/quadratura.py) su ordini INVENTATI.

Niente database, niente Lantek, niente file: il confronto riceve tutto in un
dizionario (righe del PDF, pezzi di FerroTrack, Lantek, distinte finte).

Si prova che:
  1. un componente d'assieme che manca in FerroTrack -> "Manca in FerroTrack"
  2. assiemi e quantita': ordine x3, FerroTrack x1 -> quantita' diverse
     (assieme, componente cliente/FerroTrack, FerroTrack/Lantek)
  3. revisione -00 nell'ordine, solo -01 nel pacchetto; -00 in FerroTrack,
     -01 in Lantek -> "Revisione diversa"
  4. esplosione degli assiemi: sottoassieme dentro assieme, quantita'
     moltiplicate, righe nell'ordine del PDF (componenti subito sotto)
  5. non mandato: ordine in Lantek con un codice che in Lantek non c'e';
     lo stesso ordine prima dell'invio: "da mandare", quadra
  6. Lantek non leggibile: MAI "quadra" (non verificabile, o non quadra se
     un problema c'e' gia')
  7. in piu' in Lantek; "non laser" senza "Da procurare"
  8. ordine solo PDF (Poliform): righe abbinate, una senza ordine di produzione
  9. il testo della notifica del mattino

    python app/tests/test_quadratura.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('FERROTRACK_SKIP_DB_INIT', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import quadratura as q  # noqa: E402

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, '' if cond else extra))


def art(codice, qta, assieme=None, dxf=True, **kw):
    a = {'id': codice, 'codice': codice, 'quantita': qta, 'codice_assieme': assieme, 'materiale': 'S235',
         'spessore_mm': 3, 'dxf_filename': (codice + '.dxf') if dxf else None, 'cleaned_status': 'auto',
         'esito_verifica': {'stato': 'verificato'}}
    a.update(kw)
    return a


def lt(**pezzi):
    """Lantek finto: codice=(pianificati, stato)."""
    L = {}
    for c, (n, s) in pezzi.items():
        c = c.replace('_', '-')
        L[c] = {'codice': c, 'pianificati': float(n), 'fatti': 0.0, 'stati': {s: float(n)}}
    return L


def ordine(righe_pdf, articoli, assiemi=(), L=None, stato='in_lantek', boms=None, file_pacchetto=(),
           procura=None, invii=None, lantek_ok=True, archivio=None):
    files = {'caricati': {(a['codice'] + '.dxf').lower(): a['codice'] + '.dxf' for a in articoli if a.get('dxf_filename')},
             'pronti': {q.k(a['codice']): ['LANTEK/x.dxf'] for a in articoli}, 'da_preparare': {}}
    arch = archivio if archivio is not None else {a['codice']: {'esiste': True, 'codice_lantek': a['codice']} for a in articoli}
    return {
        'ordine': {'id': 'o1', 'numero': 'A 009999', 'cliente': 'Cliente prova', 'stato_laser': stato, 'commessa': '9999'},
        'pdf': {'righe': [{'codice': c, 'quantita': n, 'descrizione': 'riga ' + c} for c, n in righe_pdf],
                'formato': 'prova', 'note': []},
        'prev': {'quantita': 1, 'articoli': articoli,
                 'assiemi': [{'codice_assieme': c, 'qty': n} for c, n in assiemi], 'tubolari': []},
        'procura': procura or {}, 'invii': invii or {}, 'files': files,
        'lantek': {'ok': lantek_ok, 'errore': None if lantek_ok else 'Lantek non risponde: prova',
                   'L': (L or {}) if lantek_ok else {}, 'archivio': {'disponibile': lantek_ok, 'pezzi': arch if lantek_ok else {}}},
        'pacchetto': q.Pacchetto(boms=boms or {}, file_finti=file_pacchetto),
    }


def riga(r, codice):
    return next((x for x in r['righe'] if q.k(x['codice']) == q.k(codice)), None)


def tipi(x):
    return [p['tipo'] for p in (x or {}).get('problemi') or []]


print('1) Componente d\'assieme che manca in FerroTrack')
d = ordine([('ASS1-00', 2)], [art('P1-00', 2, 'ASS1-00')], [('ASS1-00', 2)],
           L=lt(P1_00=(4, 'creato')), boms={'ASS1-00': {'P1': 2, 'P2': 1}}, file_pacchetto=['P2-00.dxf'])
r = q.confronta(d)
check('non quadra, 1 problema', r['esito'] == q.NON_QUADRA and r['n_problemi'] == 1, (r['esito'], r['n_problemi'], r['problemi']))
p2 = riga(r, 'P2')
check('P2: ordinati 2, manca in FerroTrack, cosa fare "Aggiungi disegni"',
      p2 and p2['ordinato'] == 2 and tipi(p2) == ['manca_ft'] and 'Aggiungi disegni' in p2['problemi'][0]['cosa'], p2)
check('P1 a posto (4 in FerroTrack e in Lantek)', riga(r, 'P1')['ferrotrack'] == 4 and not tipi(riga(r, 'P1')), riga(r, 'P1'))
check('testo "Non quadra: 1 problema"', r['testo'] == 'Non quadra: 1 problema', r['testo'])

print('\n2) Quantita\' diverse (A 001221 BIS)')
d = ordine([('ASS1-00', 2), ('ASS1-00', 1)], [art('P1-00', 1, 'ASS1-00')], [('ASS1-00', 1)],
           L=lt(P1_00=(6, 'in lavoro')), boms={'ASS1-00': {'P1': 2}})
r = q.confronta(d)
a = riga(r, 'ASS1')
check("assieme: nell'ordine x3 (2+1), FerroTrack x1", a['ordinato'] == 3 and a['ferrotrack'] == 1 and tipi(a) == ['qta'], a)
p1 = riga(r, 'P1')
check('P1: ordinati 6, FerroTrack 1, Lantek 6: due quantita\' diverse', p1['ordinato'] == 6 and p1['ferrotrack'] == 1
      and p1['lantek'] == 6 and tipi(p1) == ['qta', 'qta'], p1)
check('cosa fare: Lantek e\' giusto, correggi FerroTrack', 'Lantek è giusto' in p1['problemi'][1]['cosa'], p1['problemi'])

print('\n3) Revisioni -00 / -01')
d = ordine([('X100-00', 2), ('Y200-00', 1)], [art('X100-00', 2), art('Y200-00', 1)],
           L=lt(X100_00=(2, 'creato'), Y200_01=(1, 'creato')), file_pacchetto=['X100-01.dxf', 'X100-01.pdf'])
r = q.confronta(d)
x = riga(r, 'X100')
check("X100: l'ordine chiede -00, nel pacchetto solo -01", tipi(x) == ['revisione'] and '-00' in x['problemi'][0]['testo']
      and '-01' in x['problemi'][0]['testo'], x)
y = riga(r, 'Y200')
check('Y200: -00 in FerroTrack, -01 in Lantek', tipi(y) == ['revisione'] and 'Y200-01' in y['problemi'][0]['testo'], y)

print('\n4) Assiemi esplosi, nell\'ordine del PDF')
d = ordine([('R1-00', 1), ('ASS1-00', 2), ('R2-00', 3)],
           [art('R1-00', 1), art('R2-00', 3), art('C1-00', 3, 'ASS1-00'), art('C2-00', 1, 'ASS1-00')], [('ASS1-00', 2)],
           L=lt(R1_00=(1, 'creato'), R2_00=(3, 'creato'), C1_00=(6, 'creato'), C2_00=(2, 'creato')),
           boms={'ASS1-00': {'SA1': 1, 'C2': 1}, 'SA1': {'C1': 3}})
r = q.confronta(d)
ordine_righe = [q.k(x['codice']) for x in r['righe']]
check('righe: R1, ASS1, SA1, C1, C2, R2 (componenti subito sotto l\'assieme)',
      ordine_righe == ['R1', 'ASS1', 'SA1', 'C1', 'C2', 'R2'], ordine_righe)
c1 = riga(r, 'C1')
check('C1: 3 per sottoassieme x 1 x 2 assiemi = 6, livello 2', c1['ordinato'] == 6 and c1['livello'] == 2, c1)
check('tutto torna', r['esito'] == q.QUADRA and r['testo'] == 'Quadratura: tutto torna', (r['esito'], r['problemi']))

print('\n5) Non mandato a Lantek')
arts = [art('P1-00', 1), art('P3-00', 2)]
d = ordine([('P1-00', 1), ('P3-00', 2)], arts, L=lt(P1_00=(1, 'creato')))
r = q.confronta(d)
check('in Lantek: P3 "non mandato"', tipi(riga(r, 'P3')) == ['non_mandato'] and r['esito'] == q.NON_QUADRA, riga(r, 'P3'))
d = ordine([('P1-00', 1), ('P3-00', 2)], arts, L={}, stato='coda')
r = q.confronta(d)
check('prima dell\'invio: "da mandare", quadra', r['esito'] == q.QUADRA and riga(r, 'P3')['stato'].startswith('da mandare'),
      (r['esito'], r['problemi']))
d = ordine([('P1-00', 1), ('P3-00', 2)], [art('P1-00', 1), art('P3-00', 2, dxf=False)], L={}, stato='coda')
r = q.confronta(d)
check('prima dell\'invio, senza disegno: "Senza disegno" (non arriverebbe al laser)', tipi(riga(r, 'P3')) == ['senza_disegno'], riga(r, 'P3'))

print('\n6) Lantek non leggibile')
d = ordine([('P1-00', 1)], [art('P1-00', 1)], lantek_ok=False)
r = q.confronta(d)
check('mai "quadra": non verificabile, col perche\'', r['esito'] == q.NON_VERIFICABILE and 'Lantek' in r['testo'], (r['esito'], r['testo']))
d = ordine([('ASS1-00', 1)], [], [], boms={'ASS1-00': {'P1': 1}}, lantek_ok=False)
r = q.confronta(d)
check('con un problema gia\' trovato: non quadra, e dice che non ha controllato tutto',
      r['esito'] == q.NON_QUADRA and r['non_verificabile'], (r['esito'], r['non_verificabile']))
d = ordine([('ASS1-00', 1)], [art('P1-00', 1, 'ASS1-00')], [('ASS1-00', 1)], L=lt(P1_00=(1, 'creato')), boms={})
r = q.confronta(d)
check('distinta dell\'assieme non trovata: non verificabile', r['esito'] == q.NON_VERIFICABILE
      and 'distinta' in r['non_verificabile'][0], (r['esito'], r['non_verificabile']))
d = ordine([], [art('P1-00', 1)], L=lt(P1_00=(1, 'creato')))
d['pdf'] = None
r = q.confronta(d)
check('PDF dell\'ordine non trovato: non verificabile', r['esito'] == q.NON_VERIFICABILE, r['esito'])

print('\n7) In piu\' in Lantek; non laser senza Da procurare')
d = ordine([('P1-00', 1), ('P4-00', 2)], [art('P1-00', 1), art('P4-00', 2, decisione_laser={'scelta': 'non_laser'})],
           L=lt(P1_00=(1, 'creato'), Z9_00=(5, 'creato')))
r = q.confronta(d)
check('Z9 in Lantek, non nell\'ordine: "In più in Lantek"', tipi(riga(r, 'Z9')) == ['extra_lantek'], riga(r, 'Z9'))
check('P4 "non laser" ma non in Da procurare', tipi(riga(r, 'P4')) == ['non_procurato'], riga(r, 'P4'))
d['procura'] = {'P4': {'tipo': 'commerciale', 'stato': 'da_ordinare', 'quantita': 2, 'codice': 'P4-00'}}
r = q.confronta(d)
check('con Da procurare: P4 a posto', not tipi(riga(r, 'P4')), riga(r, 'P4'))

print('\n8) Ordine solo PDF (Poliform)')
d = ordine([('05H1', 10), ('05H2', 5), ('05H3', 4)], [], L=lt(PEZZO_A=(20, 'creato')))
d['prev'] = None
d['solo_pdf'] = {'abb': {'05H1': {'non_laser': False, 'pezzi': [{'codice': 'PEZZO-A', 'quantita': 2}]},
                         '05H2': {'non_laser': False, 'pezzi': [{'codice': 'PEZZO-B', 'quantita': 1}]},
                         '05H3': {'non_laser': True, 'pezzi': []}}}
r = q.confronta(d)
check('PEZZO-A 20 in Lantek: a posto', not tipi(riga(r, 'PEZZO-A')), riga(r, 'PEZZO-A'))
check('PEZZO-B senza ordine di produzione: "Non mandato a Lantek"', tipi(riga(r, 'PEZZO-B')) == ['non_mandato'], riga(r, 'PEZZO-B'))
check('05H3 non laser: nessun problema', not tipi(riga(r, '05H3')), riga(r, '05H3'))

print('\n9) La notifica del mattino')
t = q.testo_avviso([{'numero': 'A 001221 BIS', 'esito': q.NON_QUADRA, 'n_problemi': 9},
                    {'numero': 'A 001252', 'esito': q.NON_QUADRA, 'n_problemi': 6},
                    {'numero': 'A 001306', 'esito': q.QUADRA, 'n_problemi': 0}])
check('"Quadratura: 2 ordini non quadrano — A 001221 BIS (9), A 001252 (6)"',
      t == 'Quadratura: 2 ordini non quadrano — A 001221 BIS (9), A 001252 (6)', t)
check('tutto torna: nessuna notifica', q.testo_avviso([{'numero': 'X', 'esito': q.QUADRA, 'n_problemi': 0}]) is None)

print('\n%d/%d ok' % (sum(esiti), len(esiti)))
sys.exit(0 if all(esiti) else 1)
