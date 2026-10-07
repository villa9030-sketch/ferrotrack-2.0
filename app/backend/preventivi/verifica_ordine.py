"""Verifica automatica dei pezzi di un ordine, LATO SERVER.

Quando l'ufficio carica un ordine (pacchetto PDF + DXF) ogni pezzo si
controlla subito, con le stesse regole che il preventivatore applica nel
browser (frontend/preventivi.html):

- _wbControlliCoerenza (~riga 13366): peso del cartiglio, STEP omonimo, quote;
- _wbDaCorreggereAuto (~13480) e _wbContornoAuto (~13497): correzione del
  contorno col peso del cartiglio quando lo scarto e' grave;
- _densitaMateriale (~4646) e _pesoArticoloKg (~4681): densita' e peso.

In piu' il confronto con l'archivio di Lantek (il pezzo gia' tagliato in
passato), che il chiamante passa gia' letto: qui non ci si collega a Lantek.

Il riconoscimento dei contorni NON cambia: si chiamano solo le funzioni di
lettura di verifica_coerenza (verifica_pezzo, proponi_contorno). La
correzione e' una PROPOSTA da confermare ("Va bene" / "Rimetti com'era"),
mai una modifica definitiva.

Le soglie sono costanti di questo modulo: tools/banco_prova_archivio.py ne
ha una copia (peso_ok / peso_grave), da tenere allineata.
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import verifica_coerenza as _vc

logger = logging.getLogger(__name__)

# ── Soglie (stesse della pagina, preventivi.html) ──
# Peso (_wbControlliCoerenza ~13372): ok entro il 15% o entro 0,015 kg
# (il cartiglio arrotonda a 0,01 kg); grave sotto meta' o oltre il doppio,
# con almeno 0,05 kg di differenza.
PESO_TOLL_REL = 0.15
PESO_TOLL_ASS_KG = 0.015
PESO_GRAVE_MIN = 0.5
PESO_GRAVE_MAX = 2.0
PESO_GRAVE_ASS_KG = 0.05
PESO_CONF_MIN = 0.5                 # lettura del peso nel cartiglio abbastanza sicura
# STEP omonimo (_wbControlliCoerenza ~13379)
STEP_TOLL_SPESSORE_MM = 0.05
STEP_TOLL_AREA = 0.05
STEP_GRAVE_AREA = 0.15
STEP_TOLL_TAGLIO = 0.08
# Quote (_wbControlliCoerenza ~13409): un lato dell'ingombro e' una quota
QUOTE_TOLL_MM = 0.6
QUOTE_TOLL_REL = 0.01
# Lantek (nuovo): il pezzo come e' nell'archivio di Lantek
LANTEK_TOLL_AREA = 0.05
LANTEK_GRAVE_AREA = 0.15
LANTEK_TOLL_PERIMETRO = 0.08
LANTEK_TOLL_SPESSORE_MM = 0.05
# Contorno: sotto questa confidenza il riconoscimento non e' sicuro
CONFIDENZA_MIN = 0.7                # _wbDaCorreggereAuto ~13490 per materiale/spessore
DENSITA_ACCIAIO = 7.85              # _pesoArticoloKg ~4682: ripiego se il materiale non si sa


def peso_ok(app_kg: float, cart_kg: float) -> bool:
    """Peso calcolato coerente col cartiglio (preventivi.html ~13375)."""
    if not (cart_kg and cart_kg > 0):
        return False
    return abs(app_kg / cart_kg - 1) <= PESO_TOLL_REL or abs(app_kg - cart_kg) <= PESO_TOLL_ASS_KG


def peso_grave(app_kg: float, cart_kg: float) -> bool:
    """Scarto di peso grave: il contorno e' un'altra cosa (preventivi.html ~13376)."""
    if not (cart_kg and cart_kg > 0):
        return False
    r = app_kg / cart_kg
    return (r < PESO_GRAVE_MIN or r > PESO_GRAVE_MAX) and abs(app_kg - cart_kg) > PESO_GRAVE_ASS_KG


# ── Materiale e densita' (preventivi.html ~4602-4650) ──

def _num(x, default=0.0) -> float:
    try:
        v = float(str(x).replace(',', '.')) if x is not None and x != '' else default
        return v if v == v else default          # NaN -> default
    except (TypeError, ValueError):
        return default


def _sigla(m) -> str:
    """'c 75' -> 'C_75' (come _siglaMateriale del frontend)."""
    s = re.sub(r'[\s\-./+]+', '_', str(m or '').strip().upper())
    return re.sub(r'_+', '_', s).strip('_')


def _mat_personale(m, materiali: dict | None) -> dict | None:
    """Materiale aggiunto nelle Impostazioni (con "taglia come"), o None
    (_wbMatPersonale ~4631)."""
    mats = materiali or {}
    x = mats.get(_sigla(m)) or mats.get(str(m or '').strip().upper())
    return x if isinstance(x, dict) and x.get('taglio_come') else None


def famiglia_materiale(m, materiali: dict | None = None) -> str:
    """S235 / ZINCATO / INOX_304 / ALU / OTTONE, o '' (famigliaMateriale ~4636;
    stessa tabella di lantek_lookup.normalizza_materiale)."""
    from .lantek_lookup import normalizza_materiale, FAMIGLIE_RICETTE
    s = _sigla(m)
    if not s:
        return ''
    if s in FAMIGLIE_RICETTE:
        return s
    pers = _mat_personale(s, materiali)
    if pers and pers.get('taglio_come') in FAMIGLIE_RICETTE:
        return pers['taglio_come']
    return normalizza_materiale(s) or ''


def densita_materiale(m, materiali: dict | None = None) -> float:
    """kg/dm3 del materiale, 0 se non si sa (_densitaMateriale ~4646).
    Prima la densita' del materiale aggiunto, poi la tabella standard."""
    from .dxf_scanner import _DENSITA_STD
    pers = _mat_personale(m, materiali)
    if pers and _num(pers.get('densita_kg_dm3')) > 0:
        return _num(pers.get('densita_kg_dm3'))
    s = _sigla(m)
    return _DENSITA_STD.get(s) or _DENSITA_STD.get(famiglia_materiale(m, materiali)) or 0.0


def peso_articolo_kg(item: dict, materiali: dict | None = None) -> float:
    """area x spessore x densita' (_pesoArticoloKg ~4681)."""
    rho = densita_materiale(item.get('materiale'), materiali) or DENSITA_ACCIAIO
    return _num(item.get('area_dm2')) * (_num(item.get('spessore_mm')) / 100.0) * rho


def _famiglia_lantek(m, materiali: dict | None) -> str:
    """Famiglia larga per il confronto con Lantek: FERRO vale per
    S235/S275/S355/DC01/C75..., INOX per 304/316, ALLUMINIO per ALU."""
    f = famiglia_materiale(m, materiali)
    if not f and re.fullmatch(r'C_?\d{2}[A-Z_0-9]*', _sigla(m)):
        f = 'S235'                                  # acciai al carbonio C45, C75...
    return f or _sigla(m)


# ── Controlli (porting di _wbControlliCoerenza) ──

def _rel(x: float, y: float) -> float:
    return abs(x / y - 1) if y > 0 else 0.0


def _kg(x: float) -> str:
    return f'{x:.3f}'.rstrip('0').rstrip('.').replace('.', ',') if x < 1 else f'{x:.2f}'.replace('.', ',')


def _n(x: float, dec: int = 2) -> str:
    s = f'{x:.{dec}f}'.rstrip('0').rstrip('.')
    return s.replace('.', ',') or '0'


def controlli_coerenza(item: dict, V: dict | None, materiali: dict | None = None) -> dict:
    """{peso, step, quote}: ognuno None (non controllabile) o un dict con ok.
    Porting fedele di _wbControlliCoerenza (preventivi.html ~13366)."""
    out = {'peso': None, 'step': None, 'quote': None}
    if not V:
        return out
    pc = V.get('peso_cartiglio') or {}
    app = peso_articolo_kg(item, materiali)
    cart = _num(pc.get('peso_kg'))
    if cart > 0 and _num(pc.get('confidence')) >= PESO_CONF_MIN and app > 0:
        out['peso'] = {'ok': peso_ok(app, cart), 'grave': peso_grave(app, cart), 'app': app, 'cartiglio': cart}
    S = V.get('step') or {}
    if S.get('ok'):
        diff, grave = [], False
        a_sp, a_area, a_per = _num(item.get('spessore_mm')), _num(item.get('area_dm2')), _num(item.get('perimetro_taglio_m'))
        s_sp, s_area, s_per = _num(S.get('spessore_mm')), _num(S.get('area_dm2')), _num(S.get('perimetro_taglio_m'))
        if abs(a_sp - s_sp) > STEP_TOLL_SPESSORE_MM:
            diff.append(f'spessore {_n(a_sp)} -> {_n(s_sp)} mm')
            grave = True
        if s_area > 0 and _rel(a_area, s_area) > STEP_TOLL_AREA:
            diff.append(f'area {_n(a_area)} -> {_n(s_area)} dm2')
            if _rel(a_area, s_area) > STEP_GRAVE_AREA:
                grave = True
        if s_per > 0 and _rel(a_per, s_per) > STEP_TOLL_TAGLIO:
            diff.append(f'taglio {_n(a_per)} -> {_n(s_per)} m')
        inn = int(_num(S.get('inneschi')))
        if inn > 0 and int(_num(item.get('n_forature'))) != inn:
            diff.append(f'inneschi {int(_num(item.get("n_forature")))} -> {inn}')
        if int(_num(item.get('pieghe'))) != int(_num(S.get('n_pieghe'))):
            diff.append(f'pieghe {int(_num(item.get("pieghe")))} -> {int(_num(S.get("n_pieghe")))}')
        out['step'] = {'ok': not diff, 'grave': grave, 'diff': diff, 'file': S.get('file')}
    # Senza peso e senza STEP: almeno un lato dell'ingombro deve essere una quota
    Q = V.get('quote') or []
    if (not out['peso'] and not out['step'] and Q and not item.get('geometria_manuale_confermata')
            and item.get('geometry_source') not in ('sviluppo', 'step')):
        w, h = _num(item.get('bbox_w_mm')), _num(item.get('bbox_h_mm'))
        L = V.get('ingombro_letto') or {}
        a_area = _num(item.get('area_dm2'))
        if (not (w > 0 and h > 0) and L and a_area > 0 and _num(L.get('area_dm2')) > 0
                and abs(_num(L.get('area_dm2')) / a_area - 1) <= 0.01):
            w, h = _num(L.get('bbox_w_mm')), _num(L.get('bbox_h_mm'))     # contorno scelto all'import
        if w > 0 and h > 0:
            def quotato(v):
                return any(abs(v - x) <= max(QUOTE_TOLL_MM, QUOTE_TOLL_REL * x) for x in Q)
            out['quote'] = {'ok': quotato(w) or quotato(h), 'w': w, 'h': h}
    return out


def controllo_lantek(item: dict, lantek: dict | None, materiali: dict | None = None) -> dict | None:
    """Confronto col pezzo nell'archivio di Lantek (nuovo, non c'e' nella pagina).
    None se non c'e' nulla da confrontare; altrimenti {ok, grave, diff, avvisi}."""
    if not lantek:
        return None
    diff, avvisi, grave, confrontati = [], [], False, 0
    l_area = _num(lantek.get('area_dm2'))
    a_area = _num(item.get('area_dm2'))
    if l_area > 0:
        confrontati += 1
        r = _rel(a_area, l_area)
        if r > LANTEK_GRAVE_AREA:
            diff.append(f'area {_n(a_area)} dm2, in Lantek {_n(l_area)} dm2 ({r * 100:.0f}% di differenza)')
            grave = True
        elif r > LANTEK_TOLL_AREA:
            diff.append(f'area {_n(a_area)} dm2, in Lantek {_n(l_area)} dm2 ({r * 100:.0f}%)')
    l_per = _num(lantek.get('perimetro_m'))
    if l_per > 0:
        confrontati += 1
        a_per = _num(item.get('perimetro_taglio_m'))
        if _rel(a_per, l_per) > LANTEK_TOLL_PERIMETRO:
            diff.append(f'taglio {_n(a_per)} m, in Lantek {_n(l_per)} m')
    l_sp = _num(lantek.get('spessore'))
    if l_sp > 0:
        confrontati += 1
        a_sp = _num(item.get('spessore_mm'))
        if abs(a_sp - l_sp) > LANTEK_TOLL_SPESSORE_MM:
            diff.append(f'spessore {_n(a_sp)} mm, in Lantek {_n(l_sp)} mm')
            grave = True
    l_mat = lantek.get('materiale')
    if l_mat and item.get('materiale'):
        if _famiglia_lantek(item.get('materiale'), materiali) != _famiglia_lantek(l_mat, materiali):
            avvisi.append(f'materiale {item.get("materiale")}, in Lantek {l_mat}')
    if not confrontati:
        return None
    # "d'accordo" = area (e, se ci sono, taglio e spessore) entro le tolleranze
    return {'ok': not diff and l_area > 0, 'grave': grave, 'diff': diff, 'avvisi': avvisi}


def _chiave_auto(item: dict) -> str:
    """Stessa chiave di _wbChiaveAuto (~13475): dxf|spessore|materiale."""
    def js(v):
        if v is None or v == '' or v == 0:
            return ''
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    return f'{item.get("dxf_filename") or ""}|{js(item.get("spessore_mm"))}|{item.get("materiale") or ""}'


def da_correggere_auto(item: dict, ck: dict, materiali: dict | None = None) -> bool:
    """Porting di _wbDaCorreggereAuto (~13480): solo scarto di peso grave, su
    pezzi non scelti a mano, con materiale e spessore sicuri, una volta sola."""
    a = item
    if not a or not a.get('dxf_filename') or a.get('costo_base_override') is not None:
        return False
    if a.get('geometry_source') in ('sviluppo', 'step'):        # scelte esplicite
        return False
    if a.get('pezzo_manuale'):                                   # misure scritte a mano
        return False
    if not (_num(a.get('spessore_mm')) > 0) or not densita_materiale(a.get('materiale'), materiali):
        return False
    # Con materiale o spessore incerti il peso non dice nulla sul contorno
    for k in ('_materiale_conf', '_spessore_conf'):
        if a.get(k) is not None and _num(a.get(k), 1.0) < CONFIDENZA_MIN:
            return False
    avv = list(a.get('_spessore_warnings') or []) + list(a.get('avvisi_spessore') or [])
    if any(re.search('discordante', str(w), re.I) for w in avv):
        return False
    ca = a.get('contorno_auto')
    if isinstance(ca, dict) and ca.get('chiave') == _chiave_auto(a):   # gia' provato
        return False
    p = ck.get('peso')
    return bool(p and not p['ok'] and p['grave'])


# ── Esecuzione con limite di tempo (senza uccidere nulla) ──

class _Scaduto(Exception):
    pass


def _con_limite(fn, secondi: float, *args, **kw):
    """Esegue fn in un thread a parte e aspetta al massimo `secondi`. Se non
    finisce, si rinuncia (il thread finisce per conto suo: e' daemon e
    lavora solo in lettura)."""
    if secondi <= 0:
        raise _Scaduto()
    esito = {}

    def corri():
        try:
            esito['v'] = fn(*args, **kw)
        except BaseException as e:            # noqa: BLE001 - si riporta al chiamante
            esito['e'] = e
    t = threading.Thread(target=corri, name='verifica-ordine', daemon=True)
    t.start()
    t.join(secondi)
    if t.is_alive():
        raise _Scaduto()
    if 'e' in esito:
        raise esito['e']
    return esito.get('v')


# ── API ──

def _risultato(stato, motivi, fonti, correzione=None, verifica=None) -> dict:
    return {'esito': {'stato': stato, 'motivi': list(dict.fromkeys(motivi)),
                      'fonti': list(dict.fromkeys(fonti)),
                      'quando': datetime.datetime.now().isoformat(timespec='seconds')},
            'correzione': correzione, 'verifica': verifica}


def verifica_automatica(cartella: str, item: dict, *, dxf_cfg=None, materiali=None, lantek=None,
                        correggi=True, tempo_max_s=15.0) -> dict:
    """Controlla un pezzo e, se serve, propone la correzione del contorno.
    Non solleva mai eccezioni: in caso di errore il pezzo e' 'da_guardare'."""
    try:
        return _verifica(cartella, item or {}, dxf_cfg=dxf_cfg, materiali=materiali, lantek=lantek,
                         correggi=correggi, tempo_max_s=float(tempo_max_s or 0))
    except _Scaduto:
        return _risultato('da_guardare', ['controllo troppo lungo: guardalo a mano'], [])
    except Exception as e:  # noqa: BLE001 - mai eccezioni verso il chiamante
        logger.warning('verifica automatica %s fallita: %s', (item or {}).get('codice'), e)
        return _risultato('da_guardare', [f'controllo non riuscito ({str(e)[:80]})'], [])


def _verifica(cartella, item, *, dxf_cfg, materiali, lantek, correggi, tempo_max_s) -> dict:
    t0 = time.monotonic()

    def resto():
        return tempo_max_s - (time.monotonic() - t0)

    motivi, fonti = [], []
    dxf = item.get('dxf_filename')
    V = None
    if dxf or item.get('codice'):
        V = _con_limite(_vc.verifica_pezzo, resto(), cartella, os.path.basename(dxf) if dxf else None,
                        item.get('codice'), dxf_cfg or {})
        if isinstance(V, dict):
            V = dict(V, _chiave=f'{dxf or ""}|{item.get("codice") or ""}')   # come wbVerifica (~13428)

    mancano = []
    if not item.get('materiale'):
        mancano.append('materiale')
    if not (_num(item.get('spessore_mm')) > 0):
        mancano.append('spessore')
    if mancano:
        # Senza materiale o spessore non si puo' confrontare nulla col peso
        return _risultato('da_guardare', [f'manca {" e ".join(mancano)}: scrivilo a mano'], [], verifica=V)

    if item.get('costo_base_override') is not None:
        # prezzo scritto a mano: la geometria non conta per il costo
        return _risultato('non_verificabile', ['prezzo scritto a mano: la geometria non conta'], [], verifica=V)

    ck = controlli_coerenza(item, V, materiali)
    lk = controllo_lantek(item, lantek, materiali)
    correzione = None

    # Correzione automatica del contorno (_wbContornoAuto ~13496)
    if correggi and da_correggere_auto(item, ck, materiali):
        rho = densita_materiale(item.get('materiale'), materiali)
        cart = ck['peso']['cartiglio']
        dxf_path = os.path.join(cartella, os.path.basename(dxf))
        try:
            r = _con_limite(_vc.proponi_contorno, resto(), dxf_path, _num(item.get('spessore_mm')),
                            rho, cart, dxf_cfg or {})
        except _Scaduto:
            return _risultato('da_guardare', [
                f'peso {_kg(ck["peso"]["app"])} kg, nel cartiglio {_kg(cart)} kg',
                'controllo troppo lungo: guardalo a mano'], [], verifica=V)
        r = r or {}
        if r.get('trovato'):
            nuovo = dict(item, area_dm2=r.get('area_dm2'), perimetro_taglio_m=r.get('perimetro_taglio_m'),
                         n_forature=r.get('n_forature'), bbox_w_mm=r.get('bbox_width_mm'),
                         bbox_h_mm=r.get('bbox_height_mm'), geometry_source='peso-cartiglio')
            ck2 = controlli_coerenza(nuovo, V, materiali)
            lk2 = controllo_lantek(nuovo, lantek, materiali)
            if ck2['peso'] and ck2['peso']['ok'] and (lk2 is None or lk2['ok']):
                correzione = {
                    'area_dm2': r.get('area_dm2'), 'perimetro_taglio_m': r.get('perimetro_taglio_m'),
                    'n_forature': r.get('n_forature'), 'bbox_w_mm': r.get('bbox_width_mm'),
                    'bbox_h_mm': r.get('bbox_height_mm'), 'geometry_source': 'peso-cartiglio',
                    'contorno_auto': {
                        'stato': 'da_confermare', 'chiave': _chiave_auto(item), 'scarto': r.get('scarto_peso'),
                        'prima': {'area_dm2': item.get('area_dm2'),
                                  'perimetro_taglio_m': item.get('perimetro_taglio_m'),
                                  'n_forature': item.get('n_forature'),
                                  'bbox_w_mm': item.get('bbox_w_mm'), 'bbox_h_mm': item.get('bbox_h_mm'),
                                  'geometry_source': item.get('geometry_source') or None,
                                  'confermato': False}},
                }
                motivi.append(f'contorno corretto col peso del cartiglio ({_kg(cart)} kg): '
                              f'prima {_kg(ck["peso"]["app"])} kg, da confermare')
                fonti.append('peso')
                if lk2 is not None:
                    fonti.append('lantek')
                # gli altri controlli (STEP) si rifanno sul contorno nuovo
                if ck2['step'] and not ck2['step']['ok']:
                    motivi.append('lo STEP non torna: ' + ', '.join(ck2['step']['diff']))
                    return _risultato('da_guardare', motivi, fonti, correzione, V)
                if lk2 and lk2['avvisi']:
                    motivi.extend(lk2['avvisi'])
                return _risultato('corretto_da_confermare', motivi, fonti, correzione, V)
            motivi.append(f'peso {_kg(ck["peso"]["app"])} kg, nel cartiglio {_kg(cart)} kg')
            motivi.append(f'trovato un contorno di {_n(r.get("area_dm2") or 0)} dm2 col peso del cartiglio, '
                          'ma non torna con Lantek: scegli il contorno a mano')
            return _risultato('da_guardare', motivi, [], None, V)
        # non trovato: come 'non_trovato' nella pagina, si guarda a mano
        motivi.append(f'peso {_kg(ck["peso"]["app"])} kg, nel cartiglio {_kg(cart)} kg: '
                      'nessun contorno del disegno ha quel peso, scegli il contorno a mano')
        if V and V.get('profilo'):
            motivi.append(f'sembra un profilo da barra: "{V["profilo"]}"')
        return _risultato('da_guardare', motivi, [], None, V)

    # ── Esito dei controlli senza correzione ──
    problemi, conferme = [], []
    p = ck['peso']
    if p:
        if p['ok']:
            conferme.append('peso')
        else:
            problemi.append(f'peso {_kg(p["app"])} kg, nel cartiglio {_kg(p["cartiglio"])} kg'
                            + (' (molto diverso)' if p['grave'] else ''))
    s = ck['step']
    if s:
        if s['ok']:
            conferme.append('step')
        else:
            problemi.append(' '.join(['lo STEP', s.get('file') or '', 'non torna:']).replace('  ', ' ')
                            + ' ' + ', '.join(s['diff']))
    q = ck['quote']
    if q:
        if q['ok']:
            conferme.append('quote')
        else:
            problemi.append(f'ingombro {_n(q["w"], 1)} x {_n(q["h"], 1)} mm: nessun lato e\' una quota del disegno')
    if lk:
        if lk['ok']:
            conferme.append('lantek')
        else:
            problemi.append('diverso da Lantek: ' + '; '.join(lk['diff']))
        problemi.extend(lk['avvisi'])

    # Correzione gia' proposta in un giro precedente e ancora da confermare
    ca = item.get('contorno_auto')
    auto_aperta = isinstance(ca, dict) and ca.get('stato') == 'da_confermare' and ca.get('chiave') == _chiave_auto(item)

    if problemi:
        if V and V.get('profilo') and p and not p['ok']:
            problemi.append(f'sembra un profilo da barra: "{V["profilo"]}"')
        return _risultato('da_guardare', problemi, conferme, None, V)
    if conferme:
        if auto_aperta:
            return _risultato('corretto_da_confermare', ['contorno corretto in automatico, da confermare'],
                              conferme, None, V)
        return _risultato('verificato', [], conferme, None, V)
    # Nessun riferimento: dipende da quanto e' sicuro il riconoscimento
    conf = item.get('dxf_confidence')
    incerto = bool(item.get('dxf_needs_verify')) or (conf is not None and _num(conf, 1.0) < CONFIDENZA_MIN)
    if not dxf:
        return _risultato('da_guardare', ['nessun disegno DXF per questo pezzo'], [], None, V)
    if incerto and not item.get('geometria_manuale_confermata'):
        return _risultato('da_guardare', ['contorno incerto e niente per controllarlo '
                                          '(ne\' peso nel cartiglio, ne\' STEP, ne\' Lantek)'], [], None, V)
    return _risultato('non_verificabile', ['niente con cui controllarlo (ne\' peso nel cartiglio, ne\' STEP, '
                                           'ne\' Lantek): contorno riconosciuto con sicurezza'], [], None, V)


def verifica_ordine(cartella, items: list, *, dxf_cfg=None, materiali=None,
                    lantek_per_codice: dict | None = None, correggi=True, max_paralleli=4,
                    tempo_max_s=15.0) -> list:
    """Un risultato di verifica_automatica per ogni pezzo, nello stesso ordine."""
    items = list(items or [])
    if not items:
        return []
    lpc = lantek_per_codice or {}

    def uno(it):
        it = it or {}
        lk = lpc.get(it.get('codice')) if it.get('codice') else None
        return verifica_automatica(cartella, it, dxf_cfg=dxf_cfg, materiali=materiali, lantek=lk,
                                   correggi=correggi, tempo_max_s=tempo_max_s)
    out = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_paralleli or 1), len(items)))) as pool:
        fut = {pool.submit(uno, it): i for i, it in enumerate(items)}
        for f, i in fut.items():
            try:
                out[i] = f.result()
            except Exception as e:  # noqa: BLE001 - verifica_automatica non solleva, ma per sicurezza
                out[i] = _risultato('da_guardare', [f'controllo non riuscito ({str(e)[:80]})'], [])
    return out
