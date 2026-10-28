"""QUADRATURA dell'ordine: tutto quello che il cliente ha ordinato arriva al laser? (23/10/2026)

Il caso vero: DECA A 001252 e' stato tagliato senza 45 componenti degli
assiemi. Nessuno se n'era accorto: l'ordine era "in Lantek" e i pezzi che
c'erano tornavano. Qui, ordine per ordine, tre liste messe a confronto:

  A  cosa ha ordinato il cliente: le righe del PDF d'ordine (nell'ordine del
     PDF) e gli assiemi esplosi con la distinta del disegno d'insieme
     (pacchetto originale del cliente) x quantita' dell'assieme;
  B  cosa c'e' in FerroTrack: i pezzi dell'ordine (pezzi per assieme x
     assiemi), col disegno, la decisione del laser e lo stato;
  C  cosa c'e' in Lantek: gli ordini di produzione col riferimento
     dell'ordine (SOLO LETTURA: SELECT, READ UNCOMMITTED,
     APP=FerroTrack-lettura, vedi lantek._connetti) e il registro degli
     invii di FerroTrack (lantek_invii).

Per ogni codice: ordinato, in FerroTrack, in Lantek (pianificati / fatti),
lo stato, il problema e cosa fare. Per l'ordine:
  QUADRA            tutto torna;
  NON_QUADRA        n problemi (anche se una parte non si e' potuta leggere);
  NON_VERIFICABILE  nessun problema trovato, ma qualcosa non si e' potuto
                    controllare (Lantek non risponde, PDF non letto,
                    distinta di un assieme non trovata...). MAI "quadra"
                    se Lantek non si e' letto.

Niente qui scrive: ne' sul database, ne' in Lantek, ne' sui file. E' un
controllo: decide chi lavora (memoria "controllo umano").

Costi: la prima volta un ordine con assiemi costa qualche secondo (si
leggono i PDF/DXF del pacchetto per le distinte). Per questo:
  - PDF d'ordine e distinte del pacchetto restano in memoria finche' i file
    non cambiano (data e dimensione);
  - Lantek si rilegge al massimo ogni 10 minuti per ordine, oppure subito
    se il registro degli invii di quell'ordine e' cambiato (dopo "Manda a
    Lantek" non si deve vedere "non mandato" per 10 minuti);
  - il risultato resta finche' non cambia niente di quello che entra nel
    confronto (ordine, pezzi, disegni caricati, Da procurare, invii) e
    Lantek e' quello di prima.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

# Dove Stefano tiene i pacchetti originali dei clienti (ORDINI\LS 1252 ...).
# Si cambia in app_config.json: {"quadratura": {"cartella_pacchetti": "..."}}
# o con FERROTRACK_PACCHETTI (prove).
CARTELLA_PACCHETTI = r'C:\Users\Stefano\Desktop\AUTOCAD\TAGLIO\DECA\ORDINI'
DURATA_LANTEK_S = 600
STATI_CHIUSI = {'CHIUSO', 'CONSEGNATO', 'FATTURATO', 'DA_FATTURARE', 'SPEDITO', 'ARCHIVIATO',
                'ANNULLATO', 'COMPLETATO'}
STATO_LT = {10: 'creato', 20: 'lanciato', 30: 'in lavoro', 40: 'fatto'}
ESCLUSE = {'sviluppo', 'piu_pezzi', 'non_laser'}

QUADRA, NON_QUADRA, NON_VERIFICABILE = 'QUADRA', 'NON_QUADRA', 'NON_VERIFICABILE'

# tipo di problema -> la parola che si vede
PAROLE = {
    'manca_ft': 'Manca in FerroTrack',
    'senza_disegno': 'Senza disegno',
    'bloccato': 'Bloccato',
    'non_mandato': 'Non mandato a Lantek',
    'qta': 'Quantità diversa',
    'extra_lantek': 'In più in Lantek',
    'extra_ft': 'In più in FerroTrack',
    'revisione': 'Revisione diversa',
    'non_procurato': 'Non in Da procurare',
    'sottoassieme': 'È un sottoassieme',
}


# ---------------------------------------------------------------------------
# Codici
# ---------------------------------------------------------------------------
def k(s) -> str:
    """Il codice senza revisione: '46SA00578-00.dxf' -> '46SA00578'."""
    from .preventivi.ordine_righe import chiave
    return chiave(s)


def kv(s) -> str:
    """Come k(), senza la lettera di variante DECA ('-G-')."""
    from .preventivi.ordine_righe import chiave_variante
    return chiave_variante(s)


def rev(s):
    """'X100-01' -> '01' (None se il codice non dice la revisione)."""
    s = str(s or '')
    b = os.path.splitext(os.path.basename(s))[0] if re.search(r'\.[A-Za-z]{3,4}$', s) else s
    b = re.sub(r'_cleaned$', '', b.strip(), flags=re.I)
    m = re.search(r'[-_](\d{2})$', b)
    return m.group(1) if m else None


def ia(x):
    """3.0 -> 3 (per scriverlo)."""
    try:
        f = float(x)
        return int(f) if f.is_integer() else round(f, 2)
    except (TypeError, ValueError):
        return x


def _intero(v, d=1) -> int:
    try:
        n = int(round(float(v)))
        return n if n > 0 else d
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------------------
# A: il pacchetto del cliente e le distinte degli assiemi
# ---------------------------------------------------------------------------
# un codice vero (non "SCALA:1:1" o testi del cartiglio letti come distinta)
CODICE_OK = re.compile(r'^(?=.*\d)(?=.*[A-Z])[A-Z0-9][A-Z0-9.\-_/]{4,}$|^\d{6,}$')
_RX_TUBO = re.compile(r'(^|\W)(T\.|TUBO|TUBOLARE|PROFILO|ANGOLARE|PIATTO)(\W|$)|T\.\s*QUADR|T\.\s*RETT|T\.\s*TONDO')


def cartella_pacchetti() -> str:
    v = os.environ.get('FERROTRACK_PACCHETTI')
    if v:
        return v
    try:
        from .database import ConfigManager
        c = ((ConfigManager.load_config() or {}).get('quadratura') or {}).get('cartella_pacchetti')
        if c:
            return c
    except Exception:
        pass
    return CARTELLA_PACCHETTI


def cartella_pacchetto(numero: str, radice: str | None = None):
    """'A 001252' -> ...\\ORDINI\\LS 1252 (la cartella col numero come parola)."""
    radice = radice or cartella_pacchetti()
    m = re.search(r'(\d+)', numero or '')
    if not m or not radice or not os.path.isdir(radice):
        return None
    n = str(int(m.group(1)))
    cand = []
    try:
        for d in sorted(os.listdir(radice)):
            p = os.path.join(radice, d)
            if os.path.isdir(p) and re.search(r'(?<!\d)0*' + n + r'(?!\d)', d) and not re.search(r'\d+-\d{2}$', d):
                cand.append(p)
    except OSError:
        return None
    return cand[0] if cand else None


class Pacchetto:
    """I file del pacchetto originale (DXF, PDF, DWG, STEP), per codice, e le
    distinte degli assiemi lette da PDF e DXF d'insieme (come
    assiemi_ordine.distinta_assieme). Solo lettura; i DWG non si convertono.

    boms (prove): {codice assieme: {codice: qta}} al posto dei file."""

    def __init__(self, cartelle=(), boms: dict | None = None, file_finti=()):
        self.cartelle = [c for c in cartelle if c and os.path.isdir(c)]
        self.idx = defaultdict(list)
        self._bom = {}
        self._boms_finte = None
        for c in self.cartelle:
            for root, _dirs, files in os.walk(c):
                for f in files:
                    self._aggiungi(os.path.join(root, f), os.path.relpath(os.path.join(root, f), c))
        for f in file_finti:
            self._aggiungi(f, f)
        if boms is not None:
            self._boms_finte = {k(a): {k(c): {'qta': int(q), 'codice': c, 'testo': '', 'profilo': False, 'dubbio': False}
                                       for c, q in righe.items()} for a, righe in boms.items()}

    def _aggiungi(self, path, rel):
        nome = os.path.basename(path)
        ext = os.path.splitext(nome)[1].lower()
        if ext not in ('.dxf', '.pdf', '.dwg', '.step', '.stp'):
            return
        self.idx[k(nome)].append({'path': path, 'ext': ext, 'rel': rel, 'nome': nome,
                                  'cleaned': '_cleaned' in nome.lower()})

    def __bool__(self):
        return bool(self.idx) or bool(self._boms_finte)

    def file_di(self, kk) -> list:
        return [f for f in self.idx.get(kk, []) if not f['cleaned']]

    def ha_dxf(self, kk) -> bool:
        return any(f['ext'] == '.dxf' for f in self.file_di(kk))

    def ha_dwg(self, kk) -> bool:
        return any(f['ext'] == '.dwg' for f in self.file_di(kk))

    def bom(self, codice) -> dict:
        """{'righe': {chiave: {qta, testo, codice, dubbio, profilo, qta_alt}}, 'fonti': [...]}"""
        kk = k(codice)
        if kk in self._bom:
            return self._bom[kk]
        if self._boms_finte is not None:
            res = {'righe': self._boms_finte.get(kk, {}), 'fonti': ['prova'] if kk in self._boms_finte else []}
            self._bom[kk] = res
            return res
        from .preventivi.distinte_pdf import leggi_distinta, leggi_distinta_dxf
        fonti = []
        for f in self.file_di(kk):
            d = None
            try:
                if f['ext'] == '.pdf':
                    d = leggi_distinta(f['path'])
                elif f['ext'] == '.dxf':
                    d = leggi_distinta_dxf(f['path'])
            except Exception:
                d = None
            if d and d.get('righe'):
                d['_fonte'] = f"{f['ext'][1:].upper()} {f['rel']}"
                fonti.append(d)
        out = {}
        for d in fonti:
            questa = {}
            for r in d.get('righe') or []:
                kr = k(r.get('codice'))
                try:
                    q = int(round(float(r.get('qta') or 0)))
                except (TypeError, ValueError):
                    q = 0
                if not kr or q <= 0 or kr == kk or not CODICE_OK.match(kr):
                    continue
                testo = ' - '.join(x for x in (r.get('descrizione'), r.get('descrizione_agg')) if x)
                y = questa.setdefault(kr, {'qta': 0, 'testo': testo[:120], 'codice': str(r.get('codice')).strip(),
                                           'profilo': bool(r.get('profilo'))})
                y['qta'] += q
            for kr, y in questa.items():
                x = out.get(kr)
                if x is None:
                    out[kr] = {**y, 'dubbio': False}
                    continue
                if x['qta'] != y['qta']:
                    x['dubbio'] = True
                    x['qta_alt'] = y['qta']
                x['profilo'] = x['profilo'] or y['profilo']
        res = {'righe': out, 'fonti': [d['_fonte'] for d in fonti]}
        self._bom[kk] = res
        return res


def lista_A(righe_pdf: list, pac: Pacchetto, assiemi_ft: set):
    """Cosa ha ordinato il cliente, NELL'ORDINE DEL PDF: ogni riga (sommata
    se ripetuta), e subito sotto un assieme i suoi componenti (x quantita'
    dell'assieme), fino a 3 livelli. -> (A, assiemi_A, note)

    A = {chiave: {codice, qta, origini, descr, tipo, padri, figli, livello, pos}}
    tipo: parte | tubo | assieme | sottoassieme."""
    righe = {}
    note = []
    for r in righe_pdf:
        kk = k(r['codice'])
        x = righe.setdefault(kk, {'codice': r['codice'], 'qta': 0, 'n_righe': 0,
                                  'descr': str(r.get('descrizione') or '')[:90], 'scritti': set()})
        x['qta'] += _intero(r.get('quantita'))
        x['n_righe'] += 1
        x['scritti'].add(r['codice'])
    A = {}

    def aggiungi(kk, codice, q, origine, descr, tipo, livello, padre=None):
        y = A.get(kk)
        if y is None:
            y = A[kk] = {'codice': codice, 'qta': 0, 'origini': [], 'descr': descr, 'tipo': tipo, 'padri': set(),
                         'figli': [], 'livello': livello, 'pos': len(A)}
        if padre:
            y['padri'].add(padre)
        y['qta'] += q
        y['origini'].append((origine, q))
        if tipo == 'parte' and y['tipo'] != 'parte':
            y['tipo'] = tipo

    def esplodi(cod_ass, molt, catena, livello):
        b = pac.bom(cod_ass)
        for kr, y in b['righe'].items():
            if kr in catena:
                continue
            q = y['qta'] * molt
            sub = pac.bom(y['codice']) if livello < 3 else {'righe': {}}
            if y.get('dubbio'):
                note.append(f"{y['codice']} nella distinta di {cod_ass}: PDF e DXF dicono quantità diverse "
                            f"({y['qta']} / {y.get('qta_alt')})")
            if sub['righe']:
                aggiungi(kr, y['codice'], q, f'sottoassieme di {k(cod_ass)}', y['testo'], 'sottoassieme', livello, k(cod_ass))
                A[kr]['figli'] = [(v['codice'], v['qta'], v['testo']) for v in sub['righe'].values()]
                esplodi(y['codice'], q, catena + [k(cod_ass)], livello + 1)
            else:
                tubo = y.get('profilo') or bool(_RX_TUBO.search(str(y.get('testo') or '').upper()))
                aggiungi(kr, y['codice'], q, f'componente di {k(cod_ass)}', y['testo'],
                         'tubo' if tubo else 'parte', livello, k(cod_ass))

    assiemi_A = {}
    for kk, x in righe.items():
        if len(x['scritti']) > 1:
            note.append(f"codice {kk} scritto in modi diversi nell'ordine: {', '.join(sorted(x['scritti']))}")
        b = pac.bom(x['codice'])
        if b['righe'] or kk in assiemi_ft:
            assiemi_A[kk] = {'codice': x['codice'], 'qta': x['qta'], 'fonti': b['fonti'],
                             'n_comp': len(b['righe']), 'descr': x['descr']}
            aggiungi(kk, x['codice'], x['qta'], 'riga ordine', x['descr'], 'assieme', 0)
            if b['righe']:
                esplodi(x['codice'], x['qta'], [kk], 1)
        else:
            aggiungi(kk, x['codice'], x['qta'], 'riga ordine' + (f" x{x['n_righe']}" if x['n_righe'] > 1 else ''),
                     x['descr'], 'parte', 0)
    return A, assiemi_A, note


def righe_riserva(pdf_bytes) -> list:
    """Righe 'CODICE descrizione 1,00Nr' di un Ordine Fornitore DECA che il
    lettore d'ordini non legge (A 001302)."""
    import io
    import pdfplumber
    rx = re.compile(r'^(?P<cod>[0-9A-Z][0-9A-Z.\-]{5,})\s+(?:C\d{2}-\d{3}(?:-\d+)?\s+)?(?P<desc>.*?)\s+(?P<q>\d+(?:[.,]\d+)?)\s*Nr')
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pg in pdf.pages:
            for riga in (pg.extract_text() or '').splitlines():
                m = rx.match(riga.strip())
                if m:
                    q = float(m['q'].replace('.', '').replace(',', '.')) if ',' in m['q'] else float(m['q'])
                    out.append({'codice': m['cod'], 'quantita': max(1, int(round(q))), 'descrizione': m['desc']})
    return out


def leggi_righe_pdf(dati: bytes) -> dict:
    """{'righe': [...], 'formato', 'note', 'data_ordine'} dal PDF d'ordine,
    coi lettori di FerroTrack (Ordine Fornitore DECA, ordini prezzati) e, se
    non leggono niente, col lettore di riserva."""
    from .preventivi.ordine_testo import leggi_ordine_fornitore
    from . import ordine_pdf_lantek as opl
    out = {'righe': [], 'formato': None, 'note': [], 'data_ordine': None, 'riserva': False}
    d = None
    try:
        d = leggi_ordine_fornitore(dati)
    except Exception:
        d = None
    if d and d.get('articoli'):
        out['righe'] = [{'codice': a['codice'], 'quantita': a['quantita'], 'descrizione': a.get('descrizione') or ''}
                        for a in d['articoli']]
        out['formato'] = 'Ordine Fornitore (DECA)'
        out['note'] += [str(n) for n in (d.get('note') or []) if 'non intere' in str(n)]
        return out
    try:
        out['righe'] = opl.righe_dal_pdf_bytes(dati)
    except Exception:
        out['righe'] = []
    out['formato'] = 'ordine prezzato'
    try:
        from .preventivi.ordine_prezzi import leggi_ordine_prezzato
        pz = leggi_ordine_prezzato(dati) or {}
        out['data_ordine'] = pz.get('data_ordine')
        out['formato'] = pz.get('formato') or out['formato']
    except Exception:
        pass
    if not out['righe']:
        try:
            out['righe'] = righe_riserva(dati)
        except Exception:
            out['righe'] = []
        if out['righe']:
            out['riserva'] = True
            out['formato'] = 'Ordine Fornitore (lettore di riserva)'
            out['note'].append("Il lettore d'ordini di FerroTrack non legge questo PDF: le righe le ha lette "
                               "la quadratura con un lettore di riserva.")
    return out


# ---------------------------------------------------------------------------
# B: FerroTrack
# ---------------------------------------------------------------------------
def controllo_pezzo(a):
    """Copia di app._controllo_pezzo (qui non si importa app.py)."""
    motivi, stato = [], None
    ab = a.get('abbinamento') or {}
    if a.get('dxf_filename') and ab.get('tipo') == 'somiglianza' and not ab.get('confermato'):
        motivi.append('disegno abbinato solo per somiglianza')
        stato = 'da_confermare'
    ev = a.get('esito_verifica') or {}
    if ev.get('stato') in ('da_guardare', 'corretto_da_confermare'):
        motivi.extend(m for m in (ev.get('motivi') or []) if m)
        stato = 'da_guardare' if ev['stato'] == 'da_guardare' else (stato or 'da_confermare')
    ca = a.get('contorno_auto') or {}
    if ca.get('stato') == 'da_confermare' and ev.get('stato') != 'confermato':
        motivi.append('contorno corretto in automatico')
        stato = stato or 'da_confermare'
    return {'stato': stato, 'motivi': motivi[:4]} if stato else None


def stato_motore(a):
    """Copia semplificata di app._stato_motore: sicuro | verificare | manca | deciso:..."""
    dl = a.get('decisione_laser') if isinstance(a.get('decisione_laser'), dict) else {}
    if dl.get('scelta'):
        return 'deciso:' + dl['scelta']
    manuale = (a.get('geometria_manuale_confermata') or a.get('pezzo_manuale')
               or a.get('geometry_source') in ('manual-click', 'sviluppo', 'step'))
    if not a.get('dxf_filename'):
        return 'sicuro' if manuale else 'manca'
    if manuale:
        return 'sicuro'
    ev = a.get('esito_verifica') if isinstance(a.get('esito_verifica'), dict) else {}
    verificato = ev.get('stato') in ('verificato', 'confermato')
    sicuro = not controllo_pezzo(a) and ev.get('stato') != 'da_guardare' and (
        verificato or (a.get('cleaned_status') == 'auto' and not a.get('dxf_needs_verify')))
    return 'sicuro' if sicuro else 'verificare'


def file_ordine(cartella: str | None) -> dict:
    """I DXF caricati nell'ordine (uploads/drawings/<id>): {caricati, pronti (LANTEK...), da_preparare}."""
    caricati, pronti, da_preparare = {}, {}, {}
    if cartella and os.path.isdir(cartella):
        for root, _d, files in os.walk(cartella):
            rel = os.path.relpath(root, cartella)
            for f in files:
                if not f.lower().endswith('.dxf'):
                    continue
                if rel == '.':
                    caricati[f.lower()] = f
                elif '_DA PREPARARE' in rel.upper():
                    da_preparare.setdefault(k(f), []).append(os.path.join(rel, f))
                elif rel.upper().startswith('LANTEK'):
                    pronti.setdefault(k(f), []).append(os.path.join(rel, f))
    return {'caricati': caricati, 'pronti': pronti, 'da_preparare': da_preparare}


def lista_B(prev: dict, files: dict, procura: dict):
    """I pezzi di FerroTrack come li conta la pagina laser (pezzi per assieme x assiemi)."""
    lotto = _intero(prev.get('quantita'))
    qta_ass = {a.get('codice_assieme'): _intero(a.get('qty')) for a in prev.get('assiemi') or [] if a.get('codice_assieme')}
    caricati, pronti, da_prep = files.get('caricati') or {}, files.get('pronti') or {}, files.get('da_preparare') or {}
    B = []
    for a in prev.get('articoli') or []:
        cod_ass = a.get('codice_assieme') or None
        molt = qta_ass.get(cod_ass, 1) if cod_ass else lotto
        od = a.get('ordine') if isinstance(a.get('ordine'), dict) else {}
        dl = a.get('decisione_laser') if isinstance(a.get('decisione_laser'), dict) else {}
        pf = a.get('pezzi_foglio') if isinstance(a.get('pezzi_foglio'), list) else []
        dxf = a.get('dxf_filename')
        base = {'assieme': cod_ass, 'materiale': a.get('materiale'), 'spessore': a.get('spessore_mm'),
                'ordine_stato': od.get('stato'), 'decisione': dl.get('scelta'),
                'motore': stato_motore(a), 'controllo': controllo_pezzo(a), 'articolo_id': a.get('id'),
                'tipo': 'lamiera'}
        if pf and dl.get('scelta') == 'piu_pezzi_confermati':
            for p in pf:
                c = p.get('codice') or ''
                B.append({**base, 'codice': c, 'qta': max(1, int(p.get('qta') or 1)), 'qta_per': None,
                          'foglio': a.get('codice'), 'dxf': None, 'dxf_caricato': True,
                          'pronto': pronti.get(k(c)), 'da_preparare': da_prep.get(k(c))})
            continue
        B.append({**base, 'codice': a.get('codice') or '', 'qta': _intero(a.get('quantita')) * molt,
                  'qta_per': _intero(a.get('quantita')) if cod_ass else None, 'foglio': None,
                  'dxf': dxf, 'dxf_caricato': bool(dxf and os.path.basename(dxf).lower() in caricati),
                  'pronto': pronti.get(k(a.get('codice'))), 'da_preparare': da_prep.get(k(a.get('codice')))})
    for t in prev.get('tubolari') or []:
        cod_ass = t.get('codice_assieme') or None
        B.append({'codice': t.get('posizione') or t.get('profilo') or '', 'tipo': 'tubolare', 'assieme': cod_ass,
                  'qta': _intero(t.get('qty')) * (qta_ass.get(cod_ass, 1) if cod_ass else 1),
                  'decisione': None, 'motore': None, 'controllo': None, 'ordine_stato': None, 'dxf': None,
                  'dxf_caricato': None, 'pronto': None, 'da_preparare': None, 'qta_per': None, 'foglio': None})
    for x in B:
        x['procura'] = procura.get(k(x['codice']))
    assiemi = {k(c): {'codice': c, 'qta': q} for c, q in qta_ass.items()}
    return B, assiemi


# ---------------------------------------------------------------------------
# C: Lantek (sola lettura)
# ---------------------------------------------------------------------------
def _raggruppa(rows) -> dict:
    per = {}
    for prd, st, q, made, n, d0, d1, _oref in rows:
        x = per.setdefault(str(prd), {'codice': str(prd), 'pianificati': 0.0, 'fatti': 0.0, 'stati': {}})
        q = float(q or 0)
        x['pianificati'] += q
        x['fatti'] += float(made or 0)
        s = STATO_LT.get(st, str(st))
        x['stati'][s] = x['stati'].get(s, 0) + q
    return per


def _lantek_per_commessa(cur, commessa) -> dict:
    like = commessa.replace('[', '[[]').replace('%', '[%]').replace('_', '[_]') + ' %'
    cur.execute('SELECT UPPER(LTRIM(RTRIM(PrdRef))), MState, SUM(Quantity), SUM(MadeQuan), COUNT(*), '
                'MIN(CrtDate), MAX(CrtDate), MAX(LTRIM(RTRIM(OrdRef))) '
                'FROM MMNN_MMOO_00000100 WHERE LTRIM(RTRIM(OrdRef)) = ? OR LTRIM(OrdRef) LIKE ? '
                'GROUP BY UPPER(LTRIM(RTRIM(PrdRef))), MState', [commessa, like])
    return _raggruppa(cur.fetchall())


def _lantek_per_cliente(cur, cliente_like, codici, dal) -> dict:
    """Ordini di produzione del cliente per quei codici dalla data dell'ordine
    (Poliform: in Lantek il riferimento e' vuoto)."""
    rows = []
    codici = sorted(set(codici))
    for i in range(0, len(codici), 400):
        bl = codici[i:i + 400]
        cur.execute('SELECT UPPER(LTRIM(RTRIM(PrdRef))), MState, SUM(Quantity), SUM(MadeQuan), COUNT(*), '
                    'MIN(CrtDate), MAX(CrtDate), MAX(LTRIM(RTRIM(OrdRef))) '
                    'FROM MMNN_MMOO_00000100 WHERE CusRef LIKE ? AND CrtDate >= ? AND UPPER(LTRIM(RTRIM(PrdRef))) IN ('
                    + ','.join('?' * len(bl)) + ') GROUP BY UPPER(LTRIM(RTRIM(PrdRef))), MState',
                    [cliente_like, dal] + bl)
        rows.extend(cur.fetchall())
    return _raggruppa(rows)


def leggi_lantek(commessa: str, codici_archivio: list, cliente_like: str | None = None,
                 codici_cliente: list | None = None, dal: str | None = None) -> dict:
    """{'ok', 'errore', 'L', 'archivio', 'per_cliente', 'letto_il'}. Non solleva mai."""
    from . import lantek as lt
    out = {'ok': False, 'errore': None, 'L': {}, 'archivio': {'disponibile': False, 'pezzi': {}},
           'per_cliente': False, 'letto_il': datetime.now().isoformat(timespec='seconds')}
    cfg = lt._config()
    if not cfg.get('abilitato'):
        out['errore'] = 'collegamento a Lantek disattivato'
        return out
    arch = lt.pezzi_in_lantek(codici_archivio) if codici_archivio else {'disponibile': True, 'pezzi': {}}
    out['archivio'] = arch
    try:
        con, cur = lt._connessione(cfg)
        try:
            L = _lantek_per_commessa(cur, commessa) if commessa else {}
            if cliente_like and codici_cliente:
                # anche i codici esatti di Lantek delle righe (08PA04452 -> 08PA04452-00)
                esatti = {str(p.get('codice_lantek')).upper() for p in (arch.get('pezzi') or {}).values()
                          if p.get('esiste') and p.get('codice_lantek')}
                # 28/10: un codice che ha SOLO ordini col numero d'ordine (mandati
                # da FerroTrack, anche "la differenza") si confronta per davvero;
                # se il cliente ne ha di piu' (messi a mano, senza numero) si
                # prendono quelli del cliente, "da_cliente": non si sa con
                # certezza quanti sono di quest'ordine, e niente differenza.
                Lc = _lantek_per_cliente(cur, cliente_like, sorted({c.upper() for c in codici_cliente} | esatti),
                                         dal or '2026-01-01')
                for c, x in Lc.items():
                    m = L.get(c)
                    if m and x['pianificati'] <= m['pianificati'] + 0.001:
                        continue
                    x['da_cliente'] = True
                    L[c] = x
                out['per_cliente'] = any(x.get('da_cliente') for x in L.values()) or not L
            out['L'] = L
        finally:
            con.close()
    except Exception as e:
        out['errore'] = 'Lantek non risponde: ' + str(e)[:160]
        return out
    out['ok'] = bool(arch.get('disponibile'))
    if not out['ok']:
        out['errore'] = arch.get('errore') or 'archivio pezzi di Lantek non letto'
    return out


# ---------------------------------------------------------------------------
# Il confronto (senza database, senza Lantek: tutto arriva in `d`)
# ---------------------------------------------------------------------------
def _stati_txt(l) -> str:
    return ', '.join(f'{s} {ia(q)}' for s, q in (l.get('stati') or {}).items())


def _riga(codice, **kw) -> dict:
    r = {'codice': codice, 'descrizione': '', 'livello': 0, 'assieme': None, 'tipo': 'parte',
         'ordinato': None, 'ferrotrack': None, 'lantek': None, 'lantek_fatti': None, 'lantek_stati': '',
         'stato': '', 'problemi': []}
    r.update(kw)
    return r


def _problema(r, tipo, testo, cosa):
    r['problemi'].append({'tipo': tipo, 'parola': PAROLE.get(tipo, tipo), 'testo': testo, 'cosa': cosa})


def confronta(d: dict) -> dict:
    """Il confronto A / B / C di un ordine. d:
      ordine     {id, numero, cliente, stato_laser, commessa}
      pdf        {'righe': [...], 'formato', 'note', 'riserva'} o None (PDF non trovato)
      prev       il preventivo dietro l'ordine (PreventivoManager.get) o None (ordine solo PDF)
      procura    {chiave: {tipo, stato, quantita, codice}}
      invii      {chiave: quantita'} dal registro lantek_invii
      lantek     leggi_lantek(...)
      pacchetto  Pacchetto
      files      file_ordine(...)
      solo_pdf   (ordini solo PDF) {'abb': abbinamenti, 'dist': distinta}
    """
    o = d['ordine']
    righe_out, ordine_prob, non_verif, note = [], [], [], []
    pdf = d.get('pdf')
    lt_ = d.get('lantek') or {'ok': False, 'errore': 'Lantek non letto', 'L': {}, 'archivio': {'pezzi': {}}}
    if not lt_.get('ok'):
        non_verif.append(lt_.get('errore') or 'Lantek non risponde')
    if pdf is None:
        non_verif.append("PDF dell'ordine del cliente non trovato: non so cosa ha ordinato")
    elif not pdf.get('righe'):
        non_verif.append("Il PDF dell'ordine non si legge: non so cosa ha ordinato il cliente")
    else:
        note.extend(pdf.get('note') or [])
    if d.get('prev') is None:
        _confronta_solo_pdf(d, righe_out, non_verif, note)
    else:
        _confronta_preventivo(d, righe_out, ordine_prob, non_verif, note)
    n = sum(1 for r in righe_out if r['problemi']) + len(ordine_prob)
    if n:
        esito = NON_QUADRA
        testo = f"Non quadra: {n} {'problema' if n == 1 else 'problemi'}"
    elif non_verif:
        esito = NON_VERIFICABILE
        testo = 'Quadratura non verificabile: ' + non_verif[0]
    else:
        esito = QUADRA
        testo = 'Quadratura: tutto torna'
    return {
        'order_id': o['id'], 'numero': o.get('numero'), 'cliente': o.get('cliente'),
        'stato_laser': o.get('stato_laser'), 'commessa': o.get('commessa'),
        'esito': esito, 'testo': testo, 'n_problemi': n, 'non_verificabile': non_verif,
        'problemi_ordine': ordine_prob, 'righe': righe_out,
        'problemi': [r for r in righe_out if r['problemi']],
        'note': note, 'lantek_ok': bool(lt_.get('ok')), 'lantek_letto_il': lt_.get('letto_il'),
        'formato_pdf': (pdf or {}).get('formato'), 'righe_pdf': len((pdf or {}).get('righe') or []),
        'calcolato_il': datetime.now().isoformat(timespec='seconds'),
    }


def _confronta_preventivo(d, righe_out, ordine_prob, non_verif, note):
    o = d['ordine']
    pac = d.get('pacchetto') or Pacchetto()
    procura = d.get('procura') or {}
    invii = d.get('invii') or {}
    lt_ = d.get('lantek') or {}
    L = lt_.get('L') or {}
    lantek_ok = bool(lt_.get('ok'))
    arch = (lt_.get('archivio') or {}).get('pezzi') or {}
    B, assiemi_ft = lista_B(d['prev'], d.get('files') or {}, procura)
    righe_pdf = (d.get('pdf') or {}).get('righe') or []
    A, assiemi_A, noteA = lista_A(righe_pdf, pac, set(assiemi_ft))
    note.extend(noteA)
    for a in assiemi_A.values():
        if not a['n_comp']:
            non_verif.append(f"distinta dell'assieme {a['codice']} non trovata nel pacchetto: componenti non verificabili")
    if righe_pdf and not pac and assiemi_A:
        non_verif.append('pacchetto originale del cliente non trovato')
    # l'ordine e' gia' (anche in parte) in Lantek? Prima dell'invio un codice
    # che non e' in Lantek e' solo "da mandare", non un problema.
    mandato = o.get('stato_laser') in ('in_lantek', 'tagliato') or bool(L) or bool(invii)

    Bk, Bv = defaultdict(list), defaultdict(list)
    for x in B:
        if x['codice']:
            Bk[k(x['codice'])].append(x)
            Bv[kv(x['codice'])].append(x)
    Lk, Lv = defaultdict(list), defaultdict(list)
    for c, x in L.items():
        Lk[k(c)].append(x)
        Lv[kv(c)].append(x)
    visti_B, visti_L = set(), set()

    def lamiere(bs):
        return [b for b in bs if b['tipo'] == 'lamiera' and b['decisione'] not in ESCLUSE]

    def ferro_lantek(r, kk, bs, qta_cliente=None):
        """Lantek e lo stato FerroTrack di un codice (righe B 'lamiera' non escluse)."""
        lam = lamiere(bs)
        escl = [b for b in bs if b['tipo'] == 'lamiera' and b['decisione'] in ESCLUSE]
        ls = Lk.get(kk) or (Lv.get(kv(bs[0]['codice'])) if bs else None) or []
        for l in ls:
            visti_L.add(l['codice'])
        if ls:
            r['lantek'] = ia(sum(l['pianificati'] for l in ls))
            r['lantek_fatti'] = ia(sum(l['fatti'] for l in ls))
            r['lantek_stati'] = '; '.join(f"{l['codice']}: {_stati_txt(l)}" for l in ls)
        if not lam:
            if not escl and bs:
                r['stato'] = 'tubolare in FerroTrack'
            if escl:
                b = escl[0]
                dec = b['decisione']
                r['stato'] = {'non_laser': 'non laser', 'sviluppo': 'va sviluppato', 'piu_pezzi': 'più pezzi'}.get(dec, dec)
                if dec == 'non_laser' and not (b.get('procura') or procura.get(kk)):
                    _problema(r, 'non_procurato', 'Tenuto fuori dal laser («non laser») ma non è in «Da procurare».',
                              'Mettilo in «Da procurare» (tubo, laser tubo, meccanica o commerciale).')
                if ls:
                    qe = sum(x['qta'] for x in escl)
                    if abs(qe - float(r['lantek'])) > 0.001:
                        _problema(r, 'qta', f"Messo in Lantek a mano: {r['lantek']} pz, in FerroTrack {qe}.",
                                  f"Correggi l'ordine di produzione in Lantek a {qta_cliente or qe}.")
                    else:
                        r['stato'] += ', messo in Lantek a mano'
                elif b.get('procura') or procura.get(kk):
                    p = b.get('procura') or procura.get(kk)
                    r['stato'] += f", in Da procurare ({p.get('stato') or '—'})"
            return
        b0 = lam[0]
        qft = sum(b['qta'] for b in lam)
        # cosa lo ferma in FerroTrack
        duri, morbidi = [], []
        if not b0['dxf'] and not b0['foglio']:
            duri.append(('senza_disegno', 'nessun disegno DXF abbinato'))
        elif b0['dxf'] and not b0['dxf_caricato']:
            duri.append(('senza_disegno', f"il DXF {b0['dxf']} non è nella cartella dell'ordine"))
        if any(b['ordine_stato'] == 'qta_da_confermare' for b in lam):
            duri.append(('bloccato', 'pezzi per assieme da confermare'))
        in_arch = bool((arch.get(b0['codice']) or {}).get('esiste'))
        if not b0['spessore'] and not in_arch and lantek_ok:
            duri.append(('bloccato', 'spessore mancante'))
        if b0['controllo']:
            morbidi.append(f"verifica disegno: {b0['controllo']['stato'].replace('_', ' ')}")
        if b0['motore'] == 'verificare' and not b0['controllo']:
            morbidi.append('disegno da verificare')
        if b0['da_preparare'] and not b0['pronto']:
            morbidi.append('DXF per Lantek in «_DA PREPARARE»')
        if ls:
            ql = sum(l['pianificati'] for l in ls)
            fatti = sum(l['fatti'] for l in ls)
            r['stato'] = 'in Lantek: ' + _stati_txt({'stati': {s: q for l in ls for s, q in l['stati'].items()}})
            if abs(ql - qft) > 0.001:
                giusta = qta_cliente if qta_cliente is not None else qft
                cosa = (f"Correggi l'ordine di produzione in Lantek a {giusta}." if abs(ql - giusta) > 0.001
                        else f'Lantek è giusto ({ia(ql)}): correggi la quantità in FerroTrack.')
                # in Lantek ne mancano e FerroTrack e' d'accordo col cliente:
                # la differenza si puo' mandare (stesso codice esatto in Lantek)
                if ql + 0.001 < giusta and abs(qft - giusta) < 0.001 and Lk.get(kk) and len(ls) == 1:
                    r['manda'] = {'codice': ls[0]['codice'], 'quantita': ia(giusta - ql)}
                    cosa = f"Mancano {ia(giusta - ql)}: «Manda la differenza» li manda a Lantek."
                _problema(r, 'qta', f'In FerroTrack {qft}, in Lantek {ia(ql)}.', cosa)
            revs_l = {rev(l['codice']) for l in ls}
            revs_b = {rev(b['codice']) for b in lam}
            if None not in revs_b and revs_l != revs_b:
                _problema(r, 'revisione', f"In FerroTrack {', '.join(sorted(b['codice'] for b in lam))}, "
                                          f"in Lantek {', '.join(l['codice'] for l in ls)}.",
                          'Controlla quale revisione va tagliata.')
            if o.get('stato_laser') == 'tagliato' and fatti + 0.001 < ql and ql:
                r['stato'] += f' (fatti {ia(fatti)})'
            return
        # non e' in Lantek
        if not lantek_ok and not duri:
            # Lantek non letto: non si sa se c'e'; non e' un problema, e l'ordine non "quadra"
            r['stato'] = 'Lantek non letto'
            return
        if duri:
            tipo = duri[0][0]
            motivi = '; '.join(m for _t, m in duri) + (f"; {'; '.join(morbidi)}" if morbidi else '')
            _problema(r, tipo, f'Non è in Lantek: {motivi}.',
                      'Carica il disegno del pezzo, poi mandalo a Lantek.' if tipo == 'senza_disegno'
                      else 'Sistemalo nella pagina laser, poi mandalo a Lantek.')
            r['stato'] = 'bloccato'
            return
        if not mandato:
            r['stato'] = 'da mandare' + (f" ({'; '.join(morbidi)})" if morbidi else '')
            return
        perche = []
        if morbidi:
            perche.extend(morbidi)
        if not in_arch:
            perche.append("il pezzo non è nell'archivio di Lantek")
        if kk in invii:
            perche.append(f'nel registro risulta mandato ({ia(invii[kk])} pz) ma in Lantek non c\'è')
        cosa = 'Mandalo a Lantek (pagina laser, «Manda a Lantek»), o mettilo a mano.'
        if in_arch and (qta_cliente is None or abs(qft - qta_cliente) < 0.001):
            r['manda'] = {'codice': (arch.get(b0['codice']) or {}).get('codice_lantek') or b0['codice'],
                          'quantita': ia(qft)}
            cosa = f"Mandalo a Lantek ({ia(qft)} pz) con «Manda la differenza», o mettilo a mano."
        _problema(r, 'non_mandato', 'Non è in Lantek' + (f": {'; '.join(perche)}" if perche else '') + '.', cosa)
        r['stato'] = 'non in Lantek'

    # --- revisioni del pacchetto
    def rev_pacchetto(r, a, kk):
        ra = rev(a['codice'])
        revs_p = {rev(f['nome']) for f in pac.file_di(kk) if f['ext'] in ('.dxf', '.pdf', '.dwg')}
        revs_p.discard(None)
        if ra and revs_p and ra not in revs_p:
            _problema(r, 'revisione', f"L'ordine chiede la revisione -{ra}, nel pacchetto c'è solo "
                                      + ', '.join('-' + x for x in sorted(revs_p)) + '.',
                      'Controlla quale revisione va tagliata prima di tagliare.')
        elif not ra and len(revs_p) > 1:
            _problema(r, 'revisione', "L'ordine non dice la revisione e nel pacchetto ce ne sono più d'una: "
                                      + ', '.join('-' + x for x in sorted(revs_p)) + '.',
                      'Controlla quale revisione va tagliata.')

    # ---------- A: nell'ordine del PDF
    for kk, a in sorted(A.items(), key=lambda x: x[1]['pos']):
        org = '; '.join(f'{o_} x{q_}' for o_, q_ in a['origini'][:3])
        r = _riga(a['codice'], descrizione=a['descr'], livello=a['livello'], ordinato=a['qta'],
                  assieme=next(iter(sorted(a['padri'])), None) if a['padri'] else None, tipo=a['tipo'], origine=org)
        righe_out.append(r)
        bs, modo = (Bk[kk], 'esatto') if kk in Bk else ((Bv[kv(a['codice'])], 'variante') if kv(a['codice']) in Bv else ([], None))
        rev_pacchetto(r, a, kk)
        if a['tipo'] in ('assieme', 'sottoassieme'):
            for b in bs:
                visti_B.add(id(b))
            in_ass = kk in assiemi_ft
            if in_ass:
                r['ferrotrack'] = assiemi_ft[kk]['qta']
                r['stato'] = 'assieme'
            lam = lamiere(bs)
            if lam and a['tipo'] == 'sottoassieme' and a.get('figli'):
                figli = ', '.join(f"{c} x{q}" for c, q, _t in a['figli'][:4])
                _problema(r, 'sottoassieme', f"In FerroTrack è un pezzo laser ({lam[0]['codice']} x{lam[0]['qta']}), "
                                             f"ma il disegno ha una distinta: è un sottoassieme ({figli}).",
                          'Se non si taglia: segnalo «non laser» e mettilo in «Da procurare».')
            if bs and not in_ass:
                # in FerroTrack e' un pezzo (un "SA" che si taglia, o tenuto fuori)
                r['ferrotrack'] = sum(b['qta'] for b in bs if b['tipo'] == 'lamiera')
                ferro_lantek(r, kk, bs, a['qta'])
            pr = procura.get(kk)
            if pr:
                if 'Da procurare' not in r['stato']:
                    r['stato'] = (r['stato'] + ', ' if r['stato'] else '') + f"in Da procurare ({pr.get('stato') or '—'})"
                if pr.get('quantita') is not None and abs(float(pr['quantita']) - a['qta']) > 0.001:
                    _problema(r, 'qta', f"Ne servono {a['qta']} ({org}), in «Da procurare» ({pr.get('tipo')}) "
                                        f"sono {ia(pr['quantita'])}.",
                              f"Correggi «Da procurare» a {a['qta']} (o gli assiemi nella pagina laser).")
            if a['tipo'] == 'assieme' and not in_ass and not bs:
                r['stato'] = 'non in FerroTrack'
                _problema(r, 'manca_ft', f"Assieme ordinato x{a['qta']}: in FerroTrack non c'è.",
                          "Aggiungi l'assieme all'ordine con «Aggiungi disegni / assiemi mancanti».")
            elif a['tipo'] == 'assieme' and in_ass and assiemi_ft[kk]['qta'] != a['qta']:
                _problema(r, 'qta', f"Assiemi: nell'ordine x{a['qta']}, in FerroTrack x{assiemi_ft[kk]['qta']}.",
                          f"Porta l'assieme a x{a['qta']} nella pagina laser (si ricalcolano pezzi e Da procurare).")
            if a['tipo'] == 'assieme' and in_ass and rev(assiemi_ft[kk]['codice']) and rev(a['codice']) \
                    and rev(assiemi_ft[kk]['codice']) != rev(a['codice']):
                _problema(r, 'revisione', f"Nell'ordine {a['codice']}, in FerroTrack {assiemi_ft[kk]['codice']}.",
                          'Controlla quale revisione va tagliata.')
            continue
        if a['tipo'] == 'tubo' and not bs:
            # tubo / profilo della distinta che FerroTrack non ha fra i pezzi
            padri_coperti = any(p in procura or any(b['decisione'] in ESCLUSE for b in Bk.get(p, [])) for p in a['padri'])
            if kk in procura:
                r['stato'] = f"in Da procurare ({procura[kk].get('stato') or '—'})"
            elif padri_coperti:
                r['stato'] = "con l'assieme in Da procurare"
            else:
                r['stato'] = 'tubo / profilo'
                _problema(r, 'non_procurato', f'Tubo o profilo della distinta ({org}): non è in FerroTrack né in «Da procurare».',
                          'Mettilo in «Da procurare» (tubo o laser tubo).')
            continue
        if not bs:
            if a['livello'] == 0 or pac.ha_dxf(kk) or pac.ha_dxf(kv(a['codice'])) or pac.ha_dwg(kk):
                if kk in procura:
                    r['stato'] = f"in Da procurare ({procura[kk].get('stato') or '—'})"
                    continue
                r['stato'] = 'non in FerroTrack'
                if pac.ha_dxf(kk) or pac.ha_dxf(kv(a['codice'])):
                    cosa = "Il DXF è nel pacchetto: aggiungilo all'ordine con «Aggiungi disegni»."
                elif pac.ha_dwg(kk):
                    cosa = "Nel pacchetto c'è solo il DWG: fallo diventare DXF e aggiungilo all'ordine."
                else:
                    cosa = "Aggiungilo all'ordine, o mettilo in «Da procurare» se non è da laser."
                _problema(r, 'manca_ft', f"Ordinato x{a['qta']} ({org}): in FerroTrack non c'è.", cosa)
            elif kk in procura:
                r['stato'] = f"in Da procurare ({procura[kk].get('stato') or '—'})"
            else:
                # componente senza disegno nel pacchetto (commerciale, viteria):
                # non e' da laser, si guarda a occhio
                r['stato'] = 'senza disegno nel pacchetto (commerciale?)'
            continue
        for b in bs:
            visti_B.add(id(b))
        r['ferrotrack'] = sum(b['qta'] for b in bs)
        if modo == 'variante':
            _problema(r, 'revisione', f"Nell'ordine {a['codice']}, in FerroTrack {', '.join(b['codice'] for b in bs)} (variante).",
                      'Controlla che sia lo stesso pezzo.')
        if len(bs) == 1 and rev(a['codice']) and rev(bs[0]['codice']) and rev(a['codice']) != rev(bs[0]['codice']):
            _problema(r, 'revisione', f"Nell'ordine {a['codice']}, in FerroTrack {bs[0]['codice']}.",
                      'Controlla quale revisione va tagliata.')
        if r['ferrotrack'] != a['qta']:
            dett = ''.join(f" ({b['qta_per']} per assieme x{assiemi_ft.get(k(b['assieme']), {}).get('qta')})"
                           for b in bs if b['qta_per'])
            _problema(r, 'qta', f"Ordinati {a['qta']} ({org}), in FerroTrack {r['ferrotrack']}{dett}.",
                      'Correggi i pezzi per assieme o gli assiemi nella pagina laser.' if any(b['assieme'] for b in bs)
                      else "Correggi la quantità dell'ordine in FerroTrack.")
        ferro_lantek(r, kk, bs, a['qta'])

    # ---------- in FerroTrack ma non ordinati
    for b in B:
        if id(b) in visti_B or not b['codice'] or b['tipo'] == 'tubolare':
            continue
        kk = k(b['codice'])
        bs = [x for x in Bk.get(kk, []) if id(x) not in visti_B]
        for x in bs:
            visti_B.add(id(x))
        r = _riga(b['codice'], ferrotrack=sum(x['qta'] for x in bs), assieme=b['assieme'], livello=1 if b['assieme'] else 0)
        righe_out.append(r)
        ass = b['assieme']
        if not righe_pdf:
            pass                                  # non si sa cosa ha ordinato: non e' "in piu'"
        elif ass and k(ass) in assiemi_A and not assiemi_A[k(ass)]['n_comp']:
            r['nota'] = f'componente di {ass}: distinta non letta'
        else:
            _problema(r, 'extra_ft', f"In FerroTrack x{r['ferrotrack']}" + (f" (assieme {ass})" if ass else '')
                      + ': il cliente non l\'ha ordinato (né riga né distinta).',
                      "Controlla l'ordine del cliente: forse va tolto.")
        ferro_lantek(r, kk, bs)

    # ---------- in Lantek ma non in FerroTrack
    for c, l in sorted(L.items()):
        if c in visti_L:
            continue
        kk = k(c)
        r = _riga(c, lantek=ia(l['pianificati']), lantek_fatti=ia(l['fatti']), lantek_stati=_stati_txt(l),
                  stato='in Lantek: ' + _stati_txt(l))
        righe_out.append(r)
        in_A = kk in A or kv(c) in {kv(a['codice']) for a in A.values()}
        if in_A:
            r['ordinato'] = A[kk]['qta'] if kk in A else None
            _problema(r, 'extra_lantek', f"In Lantek {ia(l['pianificati'])} pz ({_stati_txt(l)}): è nell'ordine "
                                         'del cliente ma non fra i pezzi di FerroTrack.',
                      "Aggiungilo all'ordine in FerroTrack, o controlla in Lantek.")
        else:
            _problema(r, 'extra_lantek', f"In Lantek {ia(l['pianificati'])} pz ({_stati_txt(l)}): non è né "
                                         "nell'ordine né in FerroTrack.",
                      'Controlla in Lantek: forse va tolto (o è di un altro ordine).')

    # disegni del pacchetto mai caricati e non citati: solo una nota
    if pac:
        caricati_k = {k(f) for f in ((d.get('files') or {}).get('caricati') or {}).values()}
        mai = []
        for kk, fs in sorted(pac.idx.items()):
            dx = [f for f in fs if f['ext'] in ('.dxf', '.dwg') and not f['cleaned']]
            if not dx or kk in caricati_k or kk in Bk or kv(dx[0]['nome']) in Bv or kk in A:
                continue
            if kk in assiemi_ft or pac.bom(dx[0]['nome'])['righe']:
                continue
            mai.append(dx[0]['rel'])
        if mai:
            note.append(f"{len(mai)} disegni del pacchetto non citati né dall'ordine né dalle distinte: "
                        + ', '.join(mai[:8]) + (' …' if len(mai) > 8 else ''))


def _confronta_solo_pdf(d, righe_out, non_verif, note):
    """Ordini caricati col solo PDF (Poliform, B&B): riga -> abbinamento -> pezzi Lantek."""
    o = d['ordine']
    righe_pdf = (d.get('pdf') or {}).get('righe') or []
    sp = d.get('solo_pdf') or {}
    abb = sp.get('abb') or {}
    lt_ = d.get('lantek') or {}
    L = lt_.get('L') or {}
    arch = (lt_.get('archivio') or {}).get('pezzi') or {}
    invii = d.get('invii') or {}
    mandato = o.get('stato_laser') in ('in_lantek', 'tagliato') or bool(L) or bool(invii)
    if lt_.get('per_cliente'):
        note.append("In Lantek questi ordini di produzione non portano il numero d'ordine: per ogni pezzo si "
                    "guardano quelli del cliente dalla data dell'ordine (non si può dire con certezza che siano di questo).")
    visti = set()
    # lo stesso pezzo di Lantek da piu' righe: si confronta il totale, sulla prima
    tot = defaultdict(float)
    for r0 in righe_pdf:
        cod = str(r0['codice']).strip()
        a = abb.get(cod.upper()) or abb.get(k(cod))
        if a and a.get('non_laser'):
            continue
        if a and a.get('pezzi'):
            for p in a['pezzi']:
                tot[str(p['codice']).strip().upper()] += p['quantita'] * r0['quantita']
        else:
            p = arch.get(cod) or {}
            tot[str(p.get('codice_lantek') or cod).strip().upper()] += r0['quantita']
    for r0 in righe_pdf:
        cod = str(r0['codice']).strip()
        a = abb.get(cod.upper()) or abb.get(k(cod))
        r = _riga(cod, descrizione=str(r0.get('descrizione') or '')[:90], ordinato=r0['quantita'])
        righe_out.append(r)
        if a and a.get('non_laser'):
            r['stato'] = 'non laser'
            continue
        if a and a.get('pezzi'):
            r['tipo'] = 'assieme'
            r['ferrotrack'] = r0['quantita']
            r['stato'] = 'abbinato'
            pezzi = [(p['codice'], p['quantita'] * r0['quantita']) for p in a['pezzi']]
            liv = 1
        else:
            p = arch.get(cod) or {}
            if not p.get('esiste') and lt_.get('ok'):
                if mandato:
                    _problema(r, 'manca_ft', 'Riga non abbinata a pezzi di Lantek.', 'Abbinala nella pagina laser.')
                    r['stato'] = 'non abbinata'
                else:
                    r['stato'] = 'da abbinare'
                continue
            pezzi = [((p.get('codice_lantek') or cod), r0['quantita'])]
            liv = 0
        for pc, q in pezzi:
            rr = r if liv == 0 else _riga(pc, livello=1, assieme=cod, ordinato=q)
            if liv:
                righe_out.append(rr)
            rr['ferrotrack'] = q
            pk = str(pc).strip().upper()
            x = L.get(pk)
            primo = pk not in visti
            visti.add(pk)
            if x:
                rr['lantek'], rr['lantek_fatti'], rr['lantek_stati'] = ia(x['pianificati']), ia(x['fatti']), _stati_txt(x)
                rr['stato'] = 'in Lantek: ' + _stati_txt(x) + (' (del cliente, dalla data dell\'ordine)' if x.get('da_cliente') else '')
                if primo and not x.get('da_cliente') and abs(x['pianificati'] - tot[pk]) > 0.001:
                    if x['pianificati'] < tot[pk]:
                        manca = tot[pk] - x['pianificati']
                        rr['manda'] = {'codice': pc, 'quantita': ia(manca)}
                        _problema(rr, 'qta', f"Nell'ordine {ia(tot[pk])} in tutto, in Lantek {ia(x['pianificati'])}.",
                                  f"Mancano {ia(manca)}: «Manda la differenza» li manda a Lantek.")
                    else:
                        _problema(rr, 'qta', f"Nell'ordine {ia(tot[pk])} in tutto, in Lantek {ia(x['pianificati'])}.",
                                  f"Correggi l'ordine di produzione in Lantek a {ia(tot[pk])}.")
            elif not lt_.get('ok'):
                rr['stato'] = 'Lantek non letto'
            elif not primo:
                rr['stato'] = 'non in Lantek (vedi sopra)'
            elif mandato:
                rr['stato'] = 'non in Lantek'
                rr['manda'] = {'codice': pc, 'quantita': ia(tot[pk])}
                _problema(rr, 'non_mandato', 'Nessun ordine di produzione in Lantek'
                          + (" dalla data dell'ordine" if lt_.get('per_cliente') else '') + '.',
                          f"Mandalo a Lantek ({ia(tot[pk])} pz) con «Manda la differenza», o mettilo a mano.")
            else:
                rr['stato'] = 'da mandare'
    for c, l in sorted(L.items()):
        if c in visti or l.get('da_cliente'):
            continue
        r = _riga(c, lantek=ia(l['pianificati']), lantek_fatti=ia(l['fatti']), lantek_stati=_stati_txt(l),
                  stato='in Lantek: ' + _stati_txt(l))
        righe_out.append(r)
        _problema(r, 'extra_lantek', f"In Lantek {ia(l['pianificati'])} pz: non è fra le righe dell'ordine.",
                  'Controlla in Lantek: forse va tolto (o è di un altro ordine).')


# ---------------------------------------------------------------------------
# Raccolta dei dati (database, file, Lantek)
# ---------------------------------------------------------------------------
_PDF_CACHE: dict = {}        # (path, mtime, size) -> leggi_righe_pdf(...)
_PAC_CACHE: dict = {}        # (cartelle, firma) -> Pacchetto
_LT_CACHE: dict = {}         # order_id -> (istante, firma invii, chiave codici, leggi_lantek(...))
_RIS_CACHE: dict = {}        # order_id -> (firma, istante lantek, risultato)
_CACHE_LOCK = threading.Lock()
_ORD_LOCKS: dict = {}


def _firma_file(p):
    try:
        st = os.stat(p)
        return (p, st.st_mtime_ns, st.st_size)
    except OSError:
        return (p, None, None)


def _pdf_righe(path: str) -> dict:
    f = _firma_file(path)
    with _CACHE_LOCK:
        c = _PDF_CACHE.get(f)
    if c is not None:
        return c
    with open(path, 'rb') as fh:
        res = leggi_righe_pdf(fh.read())
    with _CACHE_LOCK:
        if len(_PDF_CACHE) > 100:
            _PDF_CACHE.clear()
        _PDF_CACHE[f] = res
    return res


def _pacchetto(cartelle: list) -> Pacchetto:
    firma = []
    for c in cartelle:
        if c and os.path.isdir(c):
            for root, _d, files in os.walk(c):
                for f in files:
                    firma.append(_firma_file(os.path.join(root, f)))
    chiave = (tuple(cartelle), hashlib.sha1(repr(sorted(firma)).encode()).hexdigest())
    with _CACHE_LOCK:
        c = _PAC_CACHE.get(chiave)
    if c is not None:
        return c
    pac = Pacchetto(cartelle)
    with _CACHE_LOCK:
        for kk in [x for x in _PAC_CACHE if x[0] == chiave[0]]:
            _PAC_CACHE.pop(kk, None)
        if len(_PAC_CACHE) > 60:
            _PAC_CACHE.clear()
        _PAC_CACHE[chiave] = pac
    return pac


def _uploads() -> str:
    return (os.environ.get('FERROTRACK_UPLOADS')
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'uploads'))


def _json_firma(x) -> str:
    return hashlib.sha1(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


def dati_database(order_id: str) -> dict | None:
    """Tutto quello che il confronto prende dal database e dai file dell'ordine
    (veloce). None se l'ordine non c'e'."""
    from .database import get_session, PreventivoManager
    from .models import Order, OrderFile, DaProcurare, LantekInvio
    from .api_laser import stato_laser
    from . import lantek as lt
    s = get_session()
    try:
        o = s.query(Order).filter(Order.id == order_id).first()
        if not o or o.is_deleted:
            return None
        f = (s.query(OrderFile).filter(OrderFile.order_id == order_id, OrderFile.file_type == 'PDF')
             .order_by(OrderFile.upload_date).first())
        pdf_path = f.filepath if f and f.filepath and os.path.isfile(f.filepath) else None
        procura = {}
        for p in s.query(DaProcurare).filter(DaProcurare.order_id == order_id).all():
            procura[k(p.codice)] = {'tipo': p.tipo, 'stato': p.stato, 'quantita': p.quantita, 'codice': p.codice}
        invii, invii_firma = defaultdict(float), []
        for r in s.query(LantekInvio).filter(LantekInvio.order_id == order_id).all():
            invii_firma.append((r.id, str(r.annullato_il)))
            if r.annullato_il is None:
                invii[k(r.codice)] += float(r.quantita or 0)
        ordine = {'id': o.id, 'numero': o.numero_ordine, 'cliente': o.cliente, 'stato_laser': stato_laser(o),
                  'commessa': lt.commessa_lantek(o.numero_ordine or ''), 'prev_id': o.preventivo_id_origine,
                  'status': o.status}
    finally:
        s.close()
    prev = PreventivoManager.get(ordine['prev_id'], include_children=True) if ordine['prev_id'] else None
    files = file_ordine(os.path.join(_uploads(), 'drawings', order_id))
    out = {'ordine': ordine, 'pdf_path': pdf_path, 'prev': prev, 'procura': procura, 'invii': dict(invii),
           'files': files, 'invii_firma': sorted(invii_firma)}
    out['firma'] = _json_firma({'o': ordine, 'pdf': _firma_file(pdf_path) if pdf_path else None, 'prev': prev,
                                'procura': procura, 'invii': sorted(invii_firma), 'files': files})
    return out


def _cartelle_pacchetto(ordine: dict) -> list:
    c = cartella_pacchetto(ordine.get('numero') or '')
    if c:
        return [c]
    # niente pacchetto sul Desktop: quello caricato col preventivo, se c'e'
    pid = str(ordine.get('prev_id') or '')
    if re.fullmatch(r'[0-9a-fA-F-]{36}', pid):
        p = os.path.join(_uploads(), 'preventivi_tmp', pid)
        if os.path.isdir(p):
            return [p]
    return []


def _lantek_ordine(order_id, db, pdf, codici_arch, cliente_like=None, codici_cliente=None, dal=None, forza=False):
    """Lantek per l'ordine: al massimo ogni 10 minuti, subito se gli invii sono cambiati."""
    firma_inv = _json_firma(db['invii_firma'])
    chiave = _json_firma([sorted(codici_arch), sorted(codici_cliente or []), cliente_like, dal])
    with _CACHE_LOCK:
        c = _LT_CACHE.get(order_id)
    if (c and not forza and time.time() - c[0] < DURATA_LANTEK_S and c[1] == firma_inv and c[2] == chiave
            and c[3].get('ok')):
        return c[3], c[0]
    res = leggi_lantek(db['ordine']['commessa'], codici_arch, cliente_like, codici_cliente, dal)
    ora = time.time()
    with _CACHE_LOCK:
        _LT_CACHE[order_id] = (ora, firma_inv, chiave, res)
    return res, ora


def calcola(order_id: str, forza_lantek: bool = False) -> dict | None:
    """La quadratura di un ordine, adesso (usa le cache dei file e di Lantek)."""
    with _CACHE_LOCK:
        lk = _ORD_LOCKS.setdefault(order_id, threading.Lock())
    with lk:
        db = dati_database(order_id)
        if db is None:
            return None
        o = db['ordine']
        pdf = _pdf_righe(db['pdf_path']) if db['pdf_path'] else None
        d = {'ordine': o, 'pdf': pdf, 'prev': db['prev'], 'procura': db['procura'], 'invii': db['invii'],
             'files': db['files']}
        if db['prev'] is not None:
            d['pacchetto'] = _pacchetto(_cartelle_pacchetto(o))
            B, _ass = lista_B(db['prev'], db['files'], db['procura'])
            codici = sorted({b['codice'] for b in B if b['tipo'] == 'lamiera' and b['codice']})
            lt_, t_lt = _lantek_ordine(order_id, db, pdf, codici, forza=forza_lantek)
        else:
            from . import ordine_pdf_lantek as opl
            righe = (pdf or {}).get('righe') or []
            codici_pdf = [r['codice'] for r in righe]
            abb = opl.abbinamenti(codici_pdf)
            # i codici delle righe e quelli dei pezzi abbinati (archivio Lantek)
            attesi = sorted({str(p['codice']).strip().upper() for a in abb.values() for p in a.get('pezzi') or []}
                            | {str(c).strip() for c in codici_pdf})
            cli = (o.get('cliente') or '').upper()
            cliente_like = 'POLI%' if 'POLI' in cli else (cli[:4] + '%' if cli else None)
            dal = (pdf or {}).get('data_ordine') or None
            lt_, t_lt = _lantek_ordine(order_id, db, pdf, codici_pdf, cliente_like, attesi, dal, forza=forza_lantek)
            d['solo_pdf'] = {'abb': abb}
        d['lantek'] = lt_
        ris = confronta(d)
        ris['firma'] = db['firma']
        ris['lantek_t'] = t_lt
        with _CACHE_LOCK:
            _RIS_CACHE[order_id] = (db['firma'], t_lt, ris)
        return ris


def in_cache(order_id: str) -> tuple:
    """(risultato o None, ancora valido?) senza calcolare niente di pesante."""
    with _CACHE_LOCK:
        c = _RIS_CACHE.get(order_id)
    if not c:
        return None, False
    try:
        db = dati_database(order_id)
    except Exception:
        logger.warning('quadratura: dati di %s non letti', order_id, exc_info=True)
        return c[2], False
    if db is None:
        return None, False
    valido = c[0] == db['firma'] and time.time() - c[1] < DURATA_LANTEK_S and c[2].get('lantek_ok')
    return c[2], bool(valido)


# --- calcolo in sottofondo (uno alla volta: non si ruba il server alle postazioni)
_CODA: queue.Queue = queue.Queue()
_IN_CODA: set = set()
_LAVORATORE = None


def _lavora():
    while True:
        oid = _CODA.get()
        try:
            calcola(oid)
        except Exception:
            logger.exception('quadratura di %s non riuscita', oid)
        finally:
            with _CACHE_LOCK:
                _IN_CODA.discard(oid)


def in_sottofondo(order_id: str) -> None:
    global _LAVORATORE
    with _CACHE_LOCK:
        if order_id in _IN_CODA:
            return
        _IN_CODA.add(order_id)
        if _LAVORATORE is None or not _LAVORATORE.is_alive():
            _LAVORATORE = threading.Thread(target=_lavora, name='quadratura', daemon=True)
            _LAVORATORE.start()
    _CODA.put(order_id)


def in_corso(order_id: str) -> bool:
    with _CACHE_LOCK:
        return order_id in _IN_CODA


def risultato(order_id: str, attendi: bool = False) -> dict:
    """{'pronto': bool, 'quadratura': risultato o None, 'vecchio': bool}.
    Se non c'e' o non e' piu' valido: attendi=True lo calcola adesso,
    altrimenti parte in sottofondo e si torna quello di prima (se c'e')."""
    r, valido = in_cache(order_id)
    if r is not None and valido:
        return {'pronto': True, 'quadratura': r, 'vecchio': False}
    if attendi:
        r = calcola(order_id)
        return {'pronto': r is not None, 'quadratura': r, 'vecchio': False}
    in_sottofondo(order_id)
    return {'pronto': False, 'quadratura': r, 'vecchio': r is not None}


def ordini_aperti() -> list:
    """Gli ordini da controllare: vivi, non chiusi, che passano (o forse passano) dal laser."""
    from .database import get_session
    from .models import Order
    from .api_laser import stato_laser
    s = get_session()
    try:
        out = []
        for o in s.query(Order).filter(Order.is_deleted == False).all():   # noqa: E712
            if (o.status or '').upper() in STATI_CHIUSI or stato_laser(o) == 'escluso':
                continue
            out.append({'id': o.id, 'numero': o.numero_ordine, 'cliente': o.cliente,
                        'ricevuto': o.data_ricezione})
        out.sort(key=lambda x: str(x['ricevuto'] or ''))
        return out
    finally:
        s.close()


def riassunto(r: dict | None) -> dict | None:
    """Quello che serve all'elenco (senza le righe)."""
    if not r:
        return None
    return {kk: r.get(kk) for kk in ('order_id', 'numero', 'cliente', 'esito', 'testo', 'n_problemi',
                                     'non_verificabile', 'calcolato_il', 'lantek_ok')}


def testo_avviso(risultati: list) -> str | None:
    """'Quadratura: 2 ordini non quadrano — A 001221 BIS, A 001252' (None se tutto torna)."""
    nq = [r for r in risultati if r and r.get('esito') == NON_QUADRA]
    nv = [r for r in risultati if r and r.get('esito') == NON_VERIFICABILE]
    if not nq and not nv:
        return None
    parti = []
    if nq:
        parti.append(f"{len(nq)} {'ordine non quadra' if len(nq) == 1 else 'ordini non quadrano'} — "
                     + ', '.join(f"{r.get('numero') or '?'} ({r['n_problemi']})" for r in nq))
    if nv:
        parti.append(f"{len(nv)} da verificare a mano — " + ', '.join(str(r.get('numero') or '?') for r in nv))
    return 'Quadratura: ' + '; '.join(parti)
