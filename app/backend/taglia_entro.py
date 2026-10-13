"""Taglia entro: il giorno entro cui un ordine va tagliato al laser.

Stefano (12/10/2026): gli ordini sono in fila per consegna, ma un ordine che
dopo il laser va ancora piegato, saldato o mandato fuori deve essere tagliato
PRIMA. Regola approvata (giorni lavorativi fissi, vale il piu' lungo, piu' le
aggiunte):

  solo taglio laser                       1 giorno lavorativo prima della consegna
  piegatura                               2
  saldatura / montaggio (anche assiemi)   4
  + filettature o svasature               +1
  + lavorazione esterna (zincatura,
    verniciatura, lav. esterna)           +5
    (anche i pezzi "da procurare" fatti fuori e non ancora arrivati:
    laser tubo e meccanica dal fornitore, backend/procura.py)

I numeri stanno in app_config, laser_calendario.anticipo_giorni (questi sono
i valori di partenza). I giorni lavorativi sono quelli del calendario del
laser: giorni della settimana + giorni speciali (ore 0 = fermo, ore > 0 =
si lavora anche se e' sabato).

La lavorazione esterna la puo' dire una persona (orders.lavorazione_esterna,
spunta "Lavorazione esterna" nella pagina laser e nell'ufficio): si' = +5,
no = niente +5 anche se le parole tornano; non detto = si deduce dalle parole
di note e lavorazioni, e il motivo chiede di confermarlo
("dedotto dalle note: conferma").

Se il laser ha scelto una data a mano (orders.data_taglio_pianificata, da
/pianifica-taglio) quella E' la data "taglia entro"; tolta, torna il calcolo.

Le lavorazioni si leggono dalla distinta dell'ordine con la STESSA traduzione
del tablet d'officina (tablet_officina.reparti_di): un assieme va sempre al
montaggio. Un ordine arrivato solo in PDF non ha la distinta: 1 giorno, e il
motivo lo dice.

Solo funzioni pure (niente database, niente Flask): si provano da sole.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .tablet_officina import _piano, reparti_di, testo_lavorazione

ANTICIPO_DEFAULT = {'taglio': 1, 'piega': 2, 'saldatura': 4, 'filettatura': 1, 'esterna': 5}
ANTICIPO_MAX = 30

# Lavorazioni fatte fuori. Non "zincato"/"zincata" da soli: e' la lamiera
# gia' zincata (materiale), non un passaggio dal fornitore.
RX_ESTERNA = re.compile(
    r'zincatur|zincat\w*\s+a\s+caldo|verniciat|vernice|cataforesi|anodizz|sabbiatur|brunitur'
    r'|galvanic|nichelatur|cromatur|lav(?:\.|orazion[ei])?\s*estern|conto\s*terzi'
    r'|trattament\w*\s+(?:termic|superficial)', re.IGNORECASE)


def anticipo_config(cal: dict | None) -> dict:
    """I giorni di anticipo del calendario, coi valori di partenza per quelli
    che mancano o non sono numeri sensati."""
    v = ((cal or {}).get('anticipo_giorni') or {}) if isinstance(cal, dict) else {}
    out = dict(ANTICIPO_DEFAULT)
    if isinstance(v, dict):
        for k in ANTICIPO_DEFAULT:
            try:
                n = int(round(float(v[k])))
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= n <= ANTICIPO_MAX:
                out[k] = n
    return out


def _esterna_in(testo) -> str | None:
    """La parola della lavorazione esterna trovata nel testo, o None."""
    m = RX_ESTERNA.search(_piano(testo or ''))
    return m.group(0) if m else None


def lavorazioni_ordine(righe, origine: str = 'distinta', note: str = '') -> dict:
    """Cosa deve ancora fare l'ordine dopo il laser, letto dalla distinta.
    Non dipende dalle impostazioni: si puo' tenere in cache (api_laser).

    righe: la distinta dell'ordine (_distinta_ordine: tipo, lavorazioni, ...;
    le righe tubo possono avere 'lav_esterna'). origine 'pdf' o 'nessuna' =
    ordine senza distinta. note: le note dell'ordine (ci si cerca una
    lavorazione esterna).

    {senza_distinta, base: taglio|piega|saldatura|montaggio, filettature,
     svasature, esterne: [parole trovate]}"""
    righe = [r for r in (righe or []) if isinstance(r, dict)]
    senza_distinta = origine in ('pdf', 'nessuna') or not righe
    reparti, filett, svas, esterne = set(), False, False, []
    for r in righe:
        reparti.update(reparti_di(r.get('lavorazioni'), r.get('tipo')))
        for lv in r.get('lavorazioni') or []:
            t = _piano(testo_lavorazione(lv))
            if 'svas' in t:
                svas = True
            if 'filett' in t or 'masch' in t or 'thread' in t:
                filett = True
            e = _esterna_in(t)
            if e and e not in esterne:
                esterne.append(e)
        if r.get('lav_esterna') and 'lav. esterna' not in esterne:
            esterne.append('lav. esterna')
    e = _esterna_in(note)
    if e and e not in esterne:
        esterne.append(e)
    if senza_distinta:
        base = 'taglio'
    elif 'saldatura' in reparti:
        base = 'saldatura'
    elif 'montaggio' in reparti:
        base = 'montaggio'
    elif 'piega' in reparti:
        base = 'piega'
    else:
        base = 'taglio'
    filettature = not senza_distinta and (filett or 'filettatura' in reparti and not svas)
    return {'senza_distinta': senza_distinta, 'base': base, 'filettature': bool(filettature),
            'svasature': bool(not senza_distinta and svas), 'esterne': esterne[:3]}


DEDOTTA = 'dedotto dalle note: conferma'


def con_esterna(fatti: dict | None, flag=None, nota: str = '') -> dict:
    """lavorazioni_ordine con la lavorazione esterna detta da una persona
    (orders.lavorazione_esterna): True = c'e' (nota: quale), False = non c'e'
    anche se le parole tornano, None = resta la deduzione dalle parole.
    Aggiunge 'esterna_fonte': 'detta' | 'dedotta' | None."""
    f = dict(fatti or {'senza_distinta': True, 'base': 'taglio'})
    if flag is True:
        f['esterne'] = [str(nota or '').strip()[:40] or 'lavorazione esterna']
        f['esterna_fonte'] = 'detta'
    elif flag is False:
        f['esterne'] = []
        f['esterna_fonte'] = 'detta'
    else:
        f['esterna_fonte'] = 'dedotta' if f.get('esterne') else None
    return f


def con_procura(fatti: dict | None, esterne_procura=None) -> dict:
    """lavorazioni_ordine (o con_esterna) con i pezzi "da procurare" fatti
    fuori e non ancora arrivati (backend/procura.py: laser tubo e meccanica
    dal fornitore). Contano come lavorazione esterna (+5) anche se la spunta
    "Lavorazione esterna" dice no: quella parla di cosa succede DOPO il
    laser, questi sono pezzi che arrivano da fuori. Aggiunge 'procura'."""
    f = dict(fatti or {'senza_distinta': True, 'base': 'taglio'})
    voci = [str(x).strip() for x in (esterne_procura or []) if str(x).strip()]
    if voci:
        f['procura'] = voci[:3]
    return f


def anticipo_da(fatti: dict | None, cal: dict | None = None) -> dict:
    """Giorni di anticipo e motivo da lavorazioni_ordine (o con_esterna),
    coi numeri del calendario. {giorni, motivo, lavorazioni, voci, noto,
    esterna, esterna_fonte}"""
    cfg = anticipo_config(cal)
    f = fatti or {'senza_distinta': True, 'base': 'taglio'}
    base = f.get('base') if f.get('base') in ('taglio', 'piega', 'saldatura', 'montaggio') else 'taglio'
    giorni = cfg['saldatura' if base == 'montaggio' else base]
    voci = [base]
    etichette = [] if base == 'taglio' else ['piegatura' if base == 'piega' else base]
    filett, svas = f.get('filettature'), f.get('svasature')
    if filett or svas:
        voci.append('filettatura')
        etichette.append('filettature e svasature' if filett and svas else 'svasature' if svas else 'filettature')
        giorni += cfg['filettatura']
    esterne = list(f.get('esterne') or [])
    # dedotta dalle parole (nessuno l'ha detta): si chiede di confermarla
    fonte = f.get('esterna_fonte') or ('dedotta' if esterne else None)
    est = 'lavorazione esterna'
    if fonte == 'dedotta':
        est += f' ({DEDOTTA})'
    elif esterne and esterne[0] != est:
        est += f' ({esterne[0]})'           # detta, con cosa: "zincatura"
    procura = list(f.get('procura') or [])
    if procura:
        # pezzi da procurare fatti fuori: si dice quali, nella stessa voce
        est = (est[:-1] + ', ' if est.endswith(')') else est + ' (') + 'da procurare: ' + ', '.join(procura) + ')'
    if esterne or procura:
        voci.append('esterna')
        etichette.append(est)
        giorni += cfg['esterna']
    if f.get('senza_distinta'):
        testa = 'ordine solo PDF, lavorazioni non note' + (f' + {est}' if esterne or procura else '')
    else:
        testa = ' + '.join(etichette) if etichette else 'solo taglio laser'
    return {'giorni': giorni, 'voci': voci, 'noto': not f.get('senza_distinta'), 'lavorazioni': testa,
            'motivo': f'{testa}: {_gg(giorni)} prima della consegna', 'esterna': esterne,
            'esterna_fonte': fonte if esterne or fonte == 'detta' else None, 'procura': procura}


def anticipo(righe, origine: str = 'distinta', note: str = '', cal: dict | None = None,
             esterna=None, esterna_nota: str = '', procura=None) -> dict:
    """Quanti giorni lavorativi prima della consegna va tagliato l'ordine
    (lavorazioni_ordine + con_esterna + con_procura + anticipo_da). esterna:
    la spunta dell'ordine (True / False / None = non detta); procura: i pezzi
    da procurare fatti fuori ("laser tubo dal fornitore", ...)."""
    return anticipo_da(con_procura(con_esterna(lavorazioni_ordine(righe, origine, note), esterna, esterna_nota),
                                   procura), cal)


def _gg(n) -> str:
    return f"{n} {'giorno lavorativo' if n == 1 else 'giorni lavorativi'}"


def _giorno(v) -> date | None:
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def lavorativo(g: date, cal: dict | None) -> bool:
    """Il laser lavora quel giorno? Prima i giorni speciali del calendario
    (ore 0 = fermo, ore > 0 = si lavora), poi i giorni della settimana."""
    cal = cal or {}
    ecc = (cal.get('eccezioni') or {}).get(g.isoformat())
    if isinstance(ecc, dict):
        try:
            return float(ecc.get('ore') or 0) > 0
        except (TypeError, ValueError):
            pass
    return g.isoweekday() in (cal.get('giorni') or [1, 2, 3, 4, 5])


def indietro(consegna: date, n: int, cal: dict | None) -> date:
    """Il giorno lavorativo che sta n giorni lavorativi prima della consegna
    (la consegna non conta). n = 0: la consegna stessa."""
    g, contati = consegna, 0
    for _ in range(400):                      # un calendario senza giorni lavorativi non blocca
        if contati >= n:
            return g
        g -= timedelta(days=1)
        if lavorativo(g, cal):
            contati += 1
    return consegna - timedelta(days=n)


def _breve(g: date) -> str:
    return g.strftime('%d/%m')


def taglia_entro(consegna, ant: dict | None, cal: dict | None, manuale=None) -> dict:
    """{data, giorni, motivo, fonte: 'calcolata' | 'a mano', calcolata, consegna}
    (date in 'AAAA-MM-GG'). manuale: la data scelta dal laser, se c'e'."""
    if not ant:
        n = anticipo_config(cal)['taglio']
        ant = {'giorni': n, 'voci': ['taglio'], 'noto': False,
               'lavorazioni': 'lavorazioni non lette', 'motivo': f'lavorazioni non lette: {_gg(n)} prima della consegna'}
    cons = _giorno(consegna)
    calc = indietro(cons, int(ant.get('giorni') or 0), cal) if cons else None
    out = {'data': calc.isoformat() if calc else None, 'giorni': ant.get('giorni'),
           'motivo': ant.get('motivo') if cons else 'senza data di consegna', 'fonte': 'calcolata',
           'calcolata': calc.isoformat() if calc else None, 'consegna': cons.isoformat() if cons else None,
           'voci': ant.get('voci') or [],
           # la lavorazione esterna: detta da una persona o dedotta dalle parole
           'esterna': list(ant.get('esterna') or []), 'esterna_fonte': ant.get('esterna_fonte')}
    man = _giorno(manuale)
    if man:
        out.update({'data': man.isoformat(), 'fonte': 'a mano',
                    'motivo': 'scelta a mano' + (f" · il calcolo dice {_breve(calc)} ({ant.get('lavorazioni') or 'lavorazioni'},"
                                                  f" {_gg(ant.get('giorni'))})" if calc else '')})
    return out


def per_ordine(order, righe, origine: str, cal: dict | None, procura=None) -> dict:
    """taglia_entro di un ordine (Order) con la sua distinta (procura: come
    in anticipo)."""
    ant = anticipo(righe, origine, getattr(order, 'note', '') or '', cal,
                   getattr(order, 'lavorazione_esterna', None), getattr(order, 'lavorazione_esterna_nota', '') or '',
                   procura)
    return taglia_entro(getattr(order, 'data_consegna', None), ant, cal,
                        getattr(order, 'data_taglio_pianificata', None))
