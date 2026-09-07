"""Servizio RIEPILOGO ECONOMICO per cliente.

Confronta, per periodo e per cliente:
    fatturato - materiali attribuiti - costo delle ore = RESIDUO

Il risultato NON e' l'utile netto: e' il residuo dopo i SOLI costi inclusi qui
(materiali attribuiti e costo delle ore). Va sempre presentato come tale.

Regole implementate:
- Le ORE vengono dalle dichiarazioni giornaliere (fonte unica dal momento di
  attivazione). Le vecchie scansioni barcode NON vengono sommate: sono una
  fonte diversa e verrebbe un doppio conteggio.
- Le ATTIVITA' INTERNE restano separate: mai attribuite a un cliente.
- Il COSTO ORARIO e' aziendale standard con VALIDITA' TEMPORALE: ogni giornata
  e' valorizzata con la tariffa valida in quel giorno, cosi' una variazione
  futura non ricalcola lo storico. Nessun costo individuale.
- Il FATTURATO e' un dato amministrativo inserito a mano (il sistema non ha
  importi fatturati): va sempre mostrato come tale, mai dedotto da preventivi
  accettati o dal valore degli ordini.
- I MATERIALI sono acquisti ATTRIBUITI, non consumi misurati. Gli acquisti
  generici non vengono ripartiti: restano non attribuiti e si mostrano a parte.
- Si distingue sempre ZERO CONFERMATO da DATO ASSENTE.
"""
import calendar
import logging
import uuid
from datetime import date, datetime, timedelta

from .database import get_session
from .models import User
from .models_ore import (
    AnomaliaOre, CostoMaterialeCliente, CostoOrario, FatturatoCliente,
    GiornataOre, RigaOre,
)
from .ore_service import oggi_locale, parse_data

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Costo orario con validita' temporale
# ---------------------------------------------------------------------------
def elenco_tariffe() -> list:
    session = get_session()
    try:
        return [{
            'id': t.id, 'valido_dal': t.valido_dal.isoformat(),
            'euro_ora': float(t.euro_ora), 'nota': t.nota,
        } for t in session.query(CostoOrario).order_by(CostoOrario.valido_dal.desc()).all()]
    finally:
        session.close()


def imposta_tariffa(valido_dal, euro_ora, da: str = '', nota: str = None) -> dict:
    """Nuova tariffa valida DA una data. Le giornate precedenti restano
    valorizzate con la tariffa che era valida allora."""
    d = parse_data(valido_dal)
    if not d:
        return {'error': 'data di validita non valida'}
    try:
        v = float(euro_ora)
    except (TypeError, ValueError):
        return {'error': 'tariffa non numerica'}
    if v != v or v in (float('inf'), float('-inf')) or v < 0:
        return {'error': 'tariffa non valida'}
    session = get_session()
    try:
        t = session.query(CostoOrario).filter(CostoOrario.valido_dal == d).first()
        if t is None:
            t = CostoOrario(id=str(uuid.uuid4()), valido_dal=d)
            session.add(t)
        t.euro_ora = v
        t.nota = nota
        t.creato_il = datetime.utcnow()
        t.creato_da = da or None
        session.commit()
        return {'success': True}
    except Exception as e:
        session.rollback()
        logger.exception('imposta_tariffa fallita: %s', e)
        return {'error': 'errore interno'}
    finally:
        session.close()


def _tariffe_ordinate(session):
    return session.query(CostoOrario).order_by(CostoOrario.valido_dal.asc()).all()


def _tariffa_del_giorno(tariffe, giorno: date):
    """Tariffa valida in quel giorno, oppure None se non ne esiste una."""
    valida = None
    for t in tariffe:
        if t.valido_dal <= giorno:
            valida = t
        else:
            break
    return valida


# ---------------------------------------------------------------------------
# Periodo
# ---------------------------------------------------------------------------
def risolvi_periodo(anno=None, mese=None, dal=None, al=None) -> dict:
    """Normalizza il periodo richiesto.

    - anno+mese -> quel mese
    - solo anno -> cumulato annuale (fino a oggi se anno corrente)
    - dal/al    -> periodo personalizzato
    """
    d1, d2 = parse_data(dal), parse_data(al)
    if d1 and d2:
        tipo = 'personalizzato'
    elif anno and mese:
        a, m = int(anno), int(mese)
        d1 = date(a, m, 1)
        d2 = date(a, m, calendar.monthrange(a, m)[1])
        tipo = 'mese'
    elif anno:
        a = int(anno)
        d1 = date(a, 1, 1)
        d2 = date(a, 12, 31)
        tipo = 'anno'
    else:
        oggi = oggi_locale()
        d1 = date(oggi.year, oggi.month, 1)
        d2 = date(oggi.year, oggi.month, calendar.monthrange(oggi.year, oggi.month)[1])
        tipo = 'mese'
    if d1 > d2:
        d1, d2 = d2, d1

    # Mesi interessati (fatturato e materiali sono registrati per mese)
    mesi = []
    cur = date(d1.year, d1.month, 1)
    while cur <= d2:
        mesi.append((cur.year, cur.month))
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)

    primo = date(d1.year, d1.month, 1)
    ultimo = date(d2.year, d2.month, calendar.monthrange(d2.year, d2.month)[1])
    allineato = (d1 == primo and d2 == ultimo)
    return {'dal': d1, 'al': d2, 'tipo': tipo, 'mesi': mesi, 'allineato_ai_mesi': allineato}


# ---------------------------------------------------------------------------
# Riepilogo
# ---------------------------------------------------------------------------
def riepilogo(anno=None, mese=None, dal=None, al=None, cliente=None) -> dict:
    p = risolvi_periodo(anno, mese, dal, al)
    d1, d2, mesi = p['dal'], p['al'], p['mesi']

    session = get_session()
    try:
        tariffe = _tariffe_ordinate(session)

        # --- ORE dichiarate: per cliente e per giorno (per valorizzarle) -----
        righe = (session.query(RigaOre, GiornataOre)
                 .join(GiornataOre, RigaOre.giornata_id == GiornataOre.id)
                 .filter(GiornataOre.data >= d1, GiornataOre.data <= d2).all())

        minuti_cli = {}          # cliente -> minuti
        costo_cli = {}           # cliente -> euro
        minuti_interni = 0
        costo_interni = 0.0
        giorni_senza_tariffa = set()

        for r, g in righe:
            minuti = int(r.minuti or 0)
            if minuti <= 0:
                continue
            t = _tariffa_del_giorno(tariffe, g.data)
            if t is None:
                giorni_senza_tariffa.add(g.data)
                costo = None
            else:
                costo = minuti / 60.0 * float(t.euro_ora)
            if r.attivita_interna:
                minuti_interni += minuti
                if costo:
                    costo_interni += costo
                continue
            nome = r.cliente
            minuti_cli[nome] = minuti_cli.get(nome, 0) + minuti
            if costo is not None:
                costo_cli[nome] = costo_cli.get(nome, 0.0) + costo

        # --- FATTURATO (inserito a mano dall'ufficio) ------------------------
        fatt = {}
        if mesi:
            cond = [(FatturatoCliente.anno == a) & (FatturatoCliente.mese == m) for a, m in mesi]
            q = session.query(FatturatoCliente)
            filtro = cond[0]
            for c in cond[1:]:
                filtro = filtro | c
            for f in q.filter(filtro).all():
                fatt[f.cliente] = fatt.get(f.cliente, 0.0) + float(f.importo or 0)

        # --- MATERIALI attribuiti (e non attribuiti, tenuti separati) --------
        mat = {}
        mat_non_attribuiti = 0.0
        if mesi:
            cond = [(CostoMaterialeCliente.anno == a) & (CostoMaterialeCliente.mese == m)
                    for a, m in mesi]
            filtro = cond[0]
            for c in cond[1:]:
                filtro = filtro | c
            for x in session.query(CostoMaterialeCliente).filter(filtro).all():
                if x.cliente:
                    mat[x.cliente] = mat.get(x.cliente, 0.0) + float(x.importo or 0)
                else:
                    mat_non_attribuiti += float(x.importo or 0)

        # --- Giornate ancora mancanti nel periodo ---------------------------
        mancanti = session.query(AnomaliaOre).filter(
            AnomaliaOre.data >= d1, AnomaliaOre.data <= d2,
            AnomaliaOre.stato == 'aperta',
            AnomaliaOre.tipo == 'mancante').count()

        # --- Composizione righe cliente -------------------------------------
        nomi = set(minuti_cli) | set(fatt) | set(mat)
        if cliente:
            nomi = {n for n in nomi if n == cliente}

        clienti = []
        for n in sorted(nomi, key=lambda x: (x or '').lower()):
            minuti = minuti_cli.get(n, 0)
            ha_fatt = n in fatt
            ha_mat = n in mat
            costo_ore = costo_cli.get(n)
            costo_noto = (minuti == 0) or (costo_ore is not None and not giorni_senza_tariffa)
            f = fatt.get(n)
            m = mat.get(n)
            residuo = None
            if ha_fatt and costo_noto:
                residuo = float(f) - float(m or 0) - float(costo_ore or 0)
            clienti.append({
                'cliente': n,
                'minuti': minuti,
                'ore': round(minuti / 60.0, 2),
                'costo_ore': round(costo_ore, 2) if costo_ore is not None else None,
                'costo_ore_incompleto': bool(minuti and not costo_noto),
                'fatturato': round(float(f), 2) if ha_fatt else None,
                'fatturato_assente': not ha_fatt,
                'materiali': round(float(m), 2) if ha_mat else None,
                'materiali_assente': not ha_mat,
                'residuo': round(residuo, 2) if residuo is not None else None,
                'residuo_calcolabile': residuo is not None,
                # Se i materiali non sono stati inseriti il residuo li considera
                # zero: e' quindi SOVRASTIMATO. Va dichiarato, non nascosto.
                'residuo_parziale': bool(residuo is not None and not ha_mat),
            })

        tot_fatt = sum(c['fatturato'] or 0 for c in clienti)
        tot_mat = sum(c['materiali'] or 0 for c in clienti)
        tot_costo = sum(c['costo_ore'] or 0 for c in clienti)
        tot_min = sum(c['minuti'] for c in clienti)

        tariffa_corrente = _tariffa_del_giorno(tariffe, d2)
        return {
            'periodo': {
                'dal': d1.isoformat(), 'al': d2.isoformat(), 'tipo': p['tipo'],
                'allineato_ai_mesi': p['allineato_ai_mesi'],
                'mesi': [f'{a:04d}-{m:02d}' for a, m in mesi],
            },
            'costo_orario': ({'euro_ora': float(tariffa_corrente.euro_ora),
                              'valido_dal': tariffa_corrente.valido_dal.isoformat()}
                             if tariffa_corrente else None),
            'clienti': clienti,
            'attivita_interne': {
                'minuti': minuti_interni,
                'ore': round(minuti_interni / 60.0, 2),
                'costo_ore': round(costo_interni, 2) if minuti_interni else 0.0,
                'nota': 'Non attribuite ad alcun cliente',
            },
            'materiali_non_attribuiti': round(mat_non_attribuiti, 2),
            'totali': {
                'fatturato': round(tot_fatt, 2),
                'materiali_attribuiti': round(tot_mat, 2),
                'costo_ore': round(tot_costo, 2),
                'ore': round(tot_min / 60.0, 2),
                'residuo': round(tot_fatt - tot_mat - tot_costo, 2),
            },
            'avvisi': {
                'giornate_mancanti': mancanti,
                'tariffa_assente': tariffa_corrente is None,
                'giorni_senza_tariffa': sorted(d.isoformat() for d in giorni_senza_tariffa)[:10],
                'clienti_senza_fatturato': [c['cliente'] for c in clienti if c['fatturato_assente']],
                'periodo_non_allineato_ai_mesi': not p['allineato_ai_mesi'],
            },
            'note': [
                'Il residuo e\' calcolato dopo i SOLI costi inclusi qui '
                '(materiali attribuiti e costo delle ore): non e\' l\'utile netto.',
                'Il fatturato e i materiali sono dati amministrativi inseriti a mano '
                'e registrati per mese.',
                'I materiali sono acquisti attribuiti, non consumi effettivi misurati.',
                'Acquisti, lavoro e fatturazione possono cadere in mesi diversi: '
                'per un confronto sensato usare il cumulato annuale.',
            ],
        }
    finally:
        session.close()


def dettaglio_cliente(cliente: str, anno=None, mese=None, dal=None, al=None) -> dict:
    """Dati che compongono i totali di un cliente (risalita alle fonti)."""
    p = risolvi_periodo(anno, mese, dal, al)
    d1, d2 = p['dal'], p['al']
    session = get_session()
    try:
        nomi = {u.id: u.name for u in session.query(User).all()}
        tariffe = _tariffe_ordinate(session)

        righe = (session.query(RigaOre, GiornataOre)
                 .join(GiornataOre, RigaOre.giornata_id == GiornataOre.id)
                 .filter(GiornataOre.data >= d1, GiornataOre.data <= d2,
                         RigaOre.cliente == cliente,
                         RigaOre.attivita_interna == False)  # noqa: E712
                 .order_by(GiornataOre.data.asc()).all())
        ore = []
        for r, g in righe:
            t = _tariffa_del_giorno(tariffe, g.data)
            ore.append({
                'data': g.data.isoformat(),
                'operatore': nomi.get(g.operatore_id, g.operatore_id),
                'minuti': int(r.minuti or 0),
                'ore': round(int(r.minuti or 0) / 60.0, 2),
                'euro_ora': float(t.euro_ora) if t else None,
                'costo': round(int(r.minuti or 0) / 60.0 * float(t.euro_ora), 2) if t else None,
                'origine': g.origine,
            })

        mesi = p['mesi']
        fatture, materiali = [], []
        if mesi:
            for a, m in mesi:
                for f in session.query(FatturatoCliente).filter(
                        FatturatoCliente.cliente == cliente,
                        FatturatoCliente.anno == a, FatturatoCliente.mese == m).all():
                    fatture.append({'periodo': f'{a:04d}-{m:02d}',
                                    'importo': float(f.importo or 0),
                                    'riferimento': f.riferimento, 'nota': f.nota,
                                    'inserito_da': f.inserito_da})
                for x in session.query(CostoMaterialeCliente).filter(
                        CostoMaterialeCliente.cliente == cliente,
                        CostoMaterialeCliente.anno == a,
                        CostoMaterialeCliente.mese == m).all():
                    materiali.append({'periodo': f'{a:04d}-{m:02d}',
                                      'importo': float(x.importo or 0),
                                      'descrizione': x.descrizione,
                                      'riferimento': x.riferimento})
        return {
            'cliente': cliente,
            'periodo': {'dal': d1.isoformat(), 'al': d2.isoformat()},
            'ore': ore,
            'fatturato': fatture,
            'materiali': materiali,
            'nota_fonti': 'Ore = dichiarazioni giornaliere degli operai. '
                          'Fatturato e materiali = inseriti a mano dall\'ufficio.',
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Inserimento dati amministrativi
# ---------------------------------------------------------------------------
def _valida_importo(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None, 'importo non numerico'
    if f != f or f in (float('inf'), float('-inf')):
        return None, 'importo non finito'
    if f < 0:
        return None, 'importo negativo'
    return round(f, 2), None


def _valida_periodo(anno, mese):
    try:
        a, m = int(anno), int(mese)
    except (TypeError, ValueError):
        return None, None, 'periodo non valido'
    if not (2000 <= a <= 2100) or not (1 <= m <= 12):
        return None, None, 'periodo fuori intervallo'
    return a, m, None


def salva_fatturato(cliente, anno, mese, importo, riferimento=None,
                    nota=None, da: str = '') -> dict:
    """Registra il fatturato di un cliente per un mese (dato amministrativo)."""
    cliente = (cliente or '').strip()
    if not cliente:
        return {'error': 'cliente obbligatorio'}
    a, m, err = _valida_periodo(anno, mese)
    if err:
        return {'error': err}
    imp, err = _valida_importo(importo)
    if err:
        return {'error': err}
    session = get_session()
    try:
        f = session.query(FatturatoCliente).filter(
            FatturatoCliente.cliente == cliente,
            FatturatoCliente.anno == a, FatturatoCliente.mese == m).first()
        if f is None:
            f = FatturatoCliente(id=str(uuid.uuid4()), cliente=cliente, anno=a, mese=m,
                                 inserito_il=datetime.utcnow(), inserito_da=da or None)
            session.add(f)
        f.importo = imp
        f.riferimento = riferimento
        f.nota = nota
        f.aggiornato_il = datetime.utcnow()
        session.commit()
        return {'success': True}
    except Exception as e:
        session.rollback()
        logger.exception('salva_fatturato fallita: %s', e)
        return {'error': 'errore interno'}
    finally:
        session.close()


def salva_materiale(cliente, anno, mese, importo, descrizione=None,
                    riferimento=None, da: str = '') -> dict:
    """Registra un costo materiali. `cliente` vuoto/None = NON attribuito
    (non viene ripartito artificialmente sui clienti)."""
    a, m, err = _valida_periodo(anno, mese)
    if err:
        return {'error': err}
    imp, err = _valida_importo(importo)
    if err:
        return {'error': err}
    session = get_session()
    try:
        session.add(CostoMaterialeCliente(
            id=str(uuid.uuid4()), cliente=(cliente or '').strip() or None,
            anno=a, mese=m, importo=imp, descrizione=descrizione,
            riferimento=riferimento, inserito_il=datetime.utcnow(),
            inserito_da=da or None))
        session.commit()
        return {'success': True}
    except Exception as e:
        session.rollback()
        logger.exception('salva_materiale fallita: %s', e)
        return {'error': 'errore interno'}
    finally:
        session.close()
