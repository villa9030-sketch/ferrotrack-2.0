"""Servizio DICHIARAZIONI ORE (operaio / giorno / cliente).

Regole non negoziabili implementate qui:
- Le ore NON sono legate a ordini o preventivi. Nessuna ripartizione implicita.
- Durate in MINUTI INTERI: somme esatte, nessun errore di virgola mobile.
- Il salvataggio della giornata e' ATOMICO (sostituisce l'intera giornata) e
  IDEMPOTENTE (stessa `richiesta_id` -> nessun doppio conteggio).
- Concorrenza ottimistica via `revisione`: niente sovrascritture silenziose fra
  tablet e ufficio.
- Giornata DICHIARATA (riga presente, anche con 0 minuti) != giornata MANCANTE.
- La data e' quella LOCALE (Europe/Rome), non UTC.

Il servizio non decide i permessi: quelli li verifica il livello API tramite
`auth_device.require_scope`. Qui si applicano solo le regole di dominio
(es. dal tablet si puo' correggere solo la giornata di OGGI).
"""
import logging
import uuid
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo('Europe/Rome')
except Exception:  # pragma: no cover - fallback estremo
    _TZ = None

from .database import get_session
from .models import User
from .models_ore import Cliente, GiornataOre, RigaOre

logger = logging.getLogger(__name__)

MINUTI_MAX_GIORNO = 1440          # 24h: oltre e' incompatibile con una giornata
PASSO_MINUTI = 30                 # il tablet lavora a mezz'ore
ETICHETTA_INTERNA = 'Attivita interne'


# ---------------------------------------------------------------------------
# Utilita' data/ora locale
# ---------------------------------------------------------------------------
def oggi_locale() -> date:
    """Data di OGGI in Europe/Rome (non UTC: a mezzanotte cambierebbe giorno)."""
    if _TZ is not None:
        return datetime.now(_TZ).date()
    return datetime.now().date()


def parse_data(valore) -> date | None:
    """Converte 'YYYY-MM-DD' (o date) in date. None se non valida."""
    if isinstance(valore, date) and not isinstance(valore, datetime):
        return valore
    if isinstance(valore, datetime):
        return valore.date()
    try:
        return datetime.strptime(str(valore)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Anagrafiche per il tablet
# ---------------------------------------------------------------------------
def elenco_operai() -> list:
    """Operai da mostrare come pulsanti sul tablet.

    Mostra gli utenti ATTIVI tenuti alla compilazione. Se la configurazione
    `ore_attese` non esiste ancora per un utente, si includono comunque i ruoli
    di officina: meglio un pulsante in piu' che un operaio che non puo' dichiarare.
    """
    from .models_ore import OreAttese
    session = get_session()
    try:
        attese = {a.operatore_id: a for a in session.query(OreAttese).all()}
        ruoli_officina = ('Operaio Laser', 'Operaio Officina')
        out = []
        for u in session.query(User).filter(User.is_active == True).all():  # noqa: E712
            cfg = attese.get(u.id)
            if cfg is not None:
                if not cfg.tenuto_alla_compilazione:
                    continue
            elif u.role not in ruoli_officina:
                continue
            out.append({'id': u.id, 'nome': u.name, 'iniziali': u.initials or ''})
        out.sort(key=lambda x: (x['nome'] or '').lower())
        return out
    finally:
        session.close()


def elenco_clienti() -> list:
    """Clienti selezionabili sul tablet.

    Vengono dall'anagrafica, NON dalle assegnazioni agli ordini: un cliente
    valido non deve sparire solo perche' non ha ordini aperti.
    """
    session = get_session()
    try:
        rows = session.query(Cliente).filter(Cliente.attivo == True).all()  # noqa: E712
        return sorted([{'nome': r.nome} for r in rows], key=lambda x: x['nome'].lower())
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Lettura giornata
# ---------------------------------------------------------------------------
def _serializza(g: GiornataOre) -> dict:
    righe = [{
        'cliente': r.cliente,
        'attivita_interna': bool(r.attivita_interna),
        'minuti': int(r.minuti or 0),
    } for r in (g.righe or [])]
    righe.sort(key=lambda r: (r['attivita_interna'], (r['cliente'] or '').lower()))
    return {
        'dichiarata': True,
        'operatore_id': g.operatore_id,
        'data': g.data.isoformat(),
        'revisione': int(g.revisione or 1),
        'righe': righe,
        'totale_minuti': sum(r['minuti'] for r in righe),
        'origine': g.origine,
        'aggiornata_il': g.aggiornata_il.isoformat() if g.aggiornata_il else None,
        'modificata_da': g.modificata_da,
    }


def leggi_giornata(operatore_id: str, data) -> dict:
    """Stato della giornata. `dichiarata=False` se non esiste ancora.

    Distinguere i due casi e' essenziale: una giornata non dichiarata non e'
    una giornata dichiarata con zero ore.
    """
    d = parse_data(data)
    if not d:
        return {'error': 'Data non valida', 'codice': 'data_non_valida'}
    session = get_session()
    try:
        g = session.query(GiornataOre).filter(
            GiornataOre.operatore_id == operatore_id,
            GiornataOre.data == d,
        ).first()
        if not g:
            return {
                'dichiarata': False, 'operatore_id': operatore_id,
                'data': d.isoformat(), 'revisione': 0,
                'righe': [], 'totale_minuti': 0,
            }
        return _serializza(g)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Validazione
# ---------------------------------------------------------------------------
def _valida_minuti(valore):
    """Ritorna (minuti_int, errore). Rifiuta negativi, non finiti, non numerici."""
    if isinstance(valore, bool):
        return None, 'valore non numerico'
    try:
        f = float(valore)
    except (TypeError, ValueError):
        return None, 'valore non numerico'
    if f != f or f in (float('inf'), float('-inf')):   # NaN / infinito
        return None, 'valore non finito'
    if f < 0:
        return None, 'valore negativo'
    if abs(f - round(f)) > 1e-9:
        return None, 'i minuti devono essere interi'
    m = int(round(f))
    if m > MINUTI_MAX_GIORNO:
        return None, 'valore superiore a una giornata'
    return m, None


def _normalizza_righe(righe, clienti_validi):
    """Valida e compatta le righe. Ritorna (righe_pulite, errore).

    - unisce eventuali duplicati sullo stesso cliente (somma)
    - scarta le righe a 0 minuti (la giornata resta comunque dichiarata)
    - verifica che il cliente esista in anagrafica
    """
    if righe is None:
        righe = []
    if not isinstance(righe, list):
        return None, 'formato righe non valido'

    acc_clienti = {}
    acc_interna = 0
    for i, r in enumerate(righe):
        if not isinstance(r, dict):
            return None, f'riga {i + 1}: formato non valido'
        minuti, err = _valida_minuti(r.get('minuti'))
        if err:
            return None, f'riga {i + 1}: {err}'
        interna = bool(r.get('attivita_interna'))
        if interna:
            acc_interna += minuti
            continue
        nome = (r.get('cliente') or '').strip()
        if not nome:
            return None, f'riga {i + 1}: cliente mancante'
        if nome not in clienti_validi:
            return None, f'cliente non riconosciuto: {nome}'
        acc_clienti[nome] = acc_clienti.get(nome, 0) + minuti

    pulite = [{'cliente': n, 'attivita_interna': False, 'minuti': m}
              for n, m in acc_clienti.items() if m > 0]
    if acc_interna > 0:
        pulite.append({'cliente': None, 'attivita_interna': True, 'minuti': acc_interna})

    totale = sum(p['minuti'] for p in pulite)
    if totale > MINUTI_MAX_GIORNO:
        return None, 'il totale supera le 24 ore'
    return pulite, None


# ---------------------------------------------------------------------------
# Salvataggio
# ---------------------------------------------------------------------------
def salva_giornata(operatore_id, data, righe, *, origine='tablet',
                   device_label=None, modificata_da=None,
                   revisione_attesa=None, richiesta_id=None, note=None) -> dict:
    """Salva (sostituendola) l'intera giornata di un operaio. Atomico.

    origine='tablet'  -> consentito SOLO il giorno corrente (le correzioni dei
                         giorni precedenti passano dall'ufficio, per tenere
                         semplice l'interfaccia del tablet).
    origine='ufficio' -> qualunque giorno non futuro.

    revisione_attesa: revisione letta dal client. Se non coincide con quella sul
                      server la richiesta viene RIFIUTATA (409) restituendo lo
                      stato corrente: nessuna sovrascrittura silenziosa.
    richiesta_id:     chiave di idempotenza. Ripetere la stessa richiesta (doppio
                      tocco, retry dopo timeout) non applica due volte il salvataggio.
    """
    d = parse_data(data)
    if not d:
        return {'error': 'Data non valida', 'codice': 'data_non_valida'}

    oggi = oggi_locale()
    if d > oggi:
        return {'error': 'Non si possono dichiarare ore per una data futura',
                'codice': 'data_futura'}
    if origine == 'tablet' and d != oggi:
        return {'error': 'Dal tablet si puo\' compilare solo la giornata di oggi. '
                         'Per correggere un giorno passato rivolgersi all\'ufficio.',
                'codice': 'giorno_non_corrente'}

    session = get_session()
    try:
        utente = session.query(User).filter(User.id == operatore_id).first()
        if not utente or not utente.is_active:
            return {'error': 'Operatore non valido o non attivo',
                    'codice': 'operatore_non_valido'}

        clienti_validi = {c.nome for c in session.query(Cliente)
                          .filter(Cliente.attivo == True).all()}  # noqa: E712
        pulite, err = _normalizza_righe(righe, clienti_validi)
        if err:
            return {'error': err, 'codice': 'righe_non_valide'}

        g = session.query(GiornataOre).filter(
            GiornataOre.operatore_id == operatore_id,
            GiornataOre.data == d,
        ).first()

        # --- Idempotenza: stessa richiesta gia' applicata -> restituisci lo stato
        if g is not None and richiesta_id and g.ultima_richiesta_id == richiesta_id:
            out = _serializza(g)
            out['idempotente'] = True
            return {'success': True, 'giornata': out}

        # --- Concorrenza ottimistica
        rev_corrente = int(g.revisione) if g is not None else 0
        if revisione_attesa is not None:
            try:
                rev_attesa = int(revisione_attesa)
            except (TypeError, ValueError):
                rev_attesa = -1
            if rev_attesa != rev_corrente:
                stato = _serializza(g) if g is not None else {
                    'dichiarata': False, 'operatore_id': operatore_id,
                    'data': d.isoformat(), 'revisione': 0, 'righe': [], 'totale_minuti': 0}
                return {'error': 'La giornata e\' stata modificata da qualcun altro. '
                                 'Controlla i dati aggiornati prima di salvare.',
                        'codice': 'conflitto', 'giornata': stato}

        ora = datetime.utcnow()
        if g is None:
            g = GiornataOre(
                id=str(uuid.uuid4()), operatore_id=operatore_id, data=d,
                revisione=1, dichiarata_il=ora, aggiornata_il=ora,
                origine=origine, device_label=device_label,
                modificata_da=modificata_da, ultima_richiesta_id=richiesta_id,
                note=note,
            )
            session.add(g)
            session.flush()
        else:
            g.revisione = rev_corrente + 1
            g.aggiornata_il = ora
            g.origine = origine
            g.device_label = device_label
            g.modificata_da = modificata_da
            g.ultima_richiesta_id = richiesta_id
            if note is not None:
                g.note = note
            # Sostituzione atomica: via le righe precedenti
            session.query(RigaOre).filter(RigaOre.giornata_id == g.id).delete(
                synchronize_session=False)

        for r in pulite:
            session.add(RigaOre(
                id=str(uuid.uuid4()), giornata_id=g.id,
                cliente=r['cliente'], attivita_interna=r['attivita_interna'],
                minuti=r['minuti'],
            ))

        session.commit()
        session.refresh(g)
        return {'success': True, 'giornata': _serializza(g)}
    except Exception as e:
        session.rollback()
        logger.exception('salva_giornata fallita')
        return {'error': str(e), 'codice': 'errore_server'}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Letture aggregate (usate da anomalie e riepilogo)
# ---------------------------------------------------------------------------
def giornate_periodo(dal: date, al: date, operatore_id: str = None) -> list:
    """Giornate dichiarate nel periodo (estremi inclusi)."""
    session = get_session()
    try:
        q = session.query(GiornataOre).filter(
            GiornataOre.data >= dal, GiornataOre.data <= al)
        if operatore_id:
            q = q.filter(GiornataOre.operatore_id == operatore_id)
        return [_serializza(g) for g in q.order_by(GiornataOre.data.desc()).all()]
    finally:
        session.close()


def minuti_per_cliente(dal: date, al: date) -> dict:
    """{cliente: minuti} nel periodo. Le attivita' interne restano SEPARATE
    sotto la chiave speciale `None` (mai attribuite a un cliente)."""
    session = get_session()
    try:
        rows = (session.query(RigaOre, GiornataOre)
                .join(GiornataOre, RigaOre.giornata_id == GiornataOre.id)
                .filter(GiornataOre.data >= dal, GiornataOre.data <= al).all())
        out = {}
        for r, _g in rows:
            chiave = None if r.attivita_interna else r.cliente
            out[chiave] = out.get(chiave, 0) + int(r.minuti or 0)
        return out
    finally:
        session.close()
