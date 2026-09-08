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
from .models import RUOLI_OPERAI, RUOLO_OPERAIO, User
from .models_ore import Cliente, GiornataOre, RigaOre

logger = logging.getLogger(__name__)

MINUTI_MAX_GIORNO = 1440          # 24h: oltre e' incompatibile con una giornata
PASSO_MINUTI = 30                 # il tablet lavora a mezz'ore
ETICHETTA_INTERNA = 'Attività interne'


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
        ruoli_officina = RUOLI_OPERAI
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


def _identificativo_da_nome(nome: str, presi: set) -> str:
    """Costruisce un identificativo leggibile dal nome, senza chiederlo a nessuno.

    "Mario Rossi" diventa "mario-rossi". Se c'e' gia', "mario-rossi-2". Serve
    solo al programma: chi aggiunge un operaio scrive un nome e basta.
    """
    import re
    import unicodedata
    piatto = unicodedata.normalize('NFKD', nome or '')
    piatto = piatto.encode('ascii', 'ignore').decode('ascii').lower()
    base = re.sub(r'[^a-z0-9]+', '-', piatto).strip('-') or 'operaio'
    base = base[:40]
    if base not in presi:
        return base
    n = 2
    while '%s-%d' % (base, n) in presi:
        n += 1
    return '%s-%d' % (base, n)


def aggiungi_operaio(nome: str, aggiunto_da: str = 'tablet') -> dict:
    """Aggiunge un nome alla bacheca. Serve solo il nome.

    Da oggi in avanti il nuovo operaio e' tenuto a dichiarare, ma NON per i
    giorni prima di adesso: chi arriva oggi non deve trovarsi addosso le
    mancanze di un mese in cui non c'era.
    """
    from datetime import date as _date
    from .models_ore import OreAttese

    nome = (nome or '').strip()
    if len(nome) < 2:
        return {'success': False, 'error': 'Scrivi il nome.',
                'codice': 'nome_mancante'}
    if len(nome) > 60:
        return {'success': False, 'error': 'Nome troppo lungo.',
                'codice': 'nome_lungo'}

    session = get_session()
    try:
        esistenti = session.query(User).filter(User.is_active == True).all()  # noqa: E712
        # Stesso nome, scritto uguale a meno di maiuscole e spazi: e' lui.
        piatto = ' '.join(nome.split()).lower()
        for u in esistenti:
            if ' '.join((u.name or '').split()).lower() == piatto:
                return {'success': False,
                        'error': 'C\'e\' gia\' %s sulla bacheca.' % u.name,
                        'codice': 'gia_presente', 'operatore_id': u.id}

        presi = {u.id for u in session.query(User).all()}
        oid = _identificativo_da_nome(nome, presi)
        iniziali = ''.join(p[0] for p in nome.split()[:2]).upper() or nome[:2].upper()

        session.add(User(
            id=oid, name=nome, role=RUOLO_OPERAIO, initials=iniziali,
            phase=None, permissions=[], machines=[],
            is_capo=False, is_active=True, e_postazione=False,
            created_at=datetime.utcnow(),
        ))
        # Tenuto a dichiarare dal giorno in cui e' stato aggiunto, non prima.
        session.add(OreAttese(
            id=str(uuid.uuid4()), operatore_id=oid,
            tenuto_alla_compilazione=True, minuti_attesi=480,
            giorni_settimana=[1, 2, 3, 4, 5],
            aggiornata_il=datetime.utcnow(), aggiornata_da=aggiunto_da,
        ))
        session.commit()
        logger.info('operaio aggiunto alla bacheca: %s (%s)', nome, oid)
        return {'success': True, 'operatore_id': oid, 'nome': nome,
                'attivo_dal': _date.today().isoformat()}
    except Exception as e:
        session.rollback()
        logger.exception('aggiunta operaio fallita')
        return {'success': False, 'error': str(e), 'codice': 'errore'}
    finally:
        session.close()


def togli_operaio(operatore_id: str) -> dict:
    """Toglie un nome dalla bacheca senza cancellare le ore gia' dichiarate.

    Chi se ne va non deve sparire dallo storico: le sue giornate restano dove
    sono, e restano attribuite a lui. Semplicemente non gli si chiede piu'
    niente e non compare piu' fra i pulsanti.
    """
    from .models_ore import OreAttese
    session = get_session()
    try:
        u = session.query(User).filter(User.id == operatore_id).first()
        if not u or u.e_postazione:
            return {'success': False, 'error': 'Non e\' un nome della bacheca.',
                    'codice': 'non_trovato'}
        u.is_active = False
        cfg = session.query(OreAttese).filter(
            OreAttese.operatore_id == operatore_id).first()
        if cfg is not None:
            # Cosi' smette anche di risultare "mancante" da domani in poi.
            cfg.tenuto_alla_compilazione = False
        session.commit()
        return {'success': True, 'nome': u.name}
    except Exception as e:
        session.rollback()
        logger.exception('rimozione operaio fallita')
        return {'success': False, 'error': str(e), 'codice': 'errore'}
    finally:
        session.close()


# Forme societarie: due scritture che differiscono solo per queste sono lo
# stesso cliente. Non si toccano i dati, si raggruppa quando si fanno i conti.
_FORME = ('srl', 's r l', 'srls', 'spa', 's p a', 'snc', 'sas', 'sapa',
          'ss', 'scarl', 'soc coop', 'coop')


def chiave_cliente(nome: str) -> str:
    """Come si riconosce che due nomi sono lo stesso cliente.

    Ignora maiuscole, punteggiatura, spazi doppi e la forma societaria: cosi'
    "DECA", "deca" e "DECA S.r.l." finiscono nello stesso conto. Torna stringa
    vuota se non c'e' un nome: quel caso lo tratta chi chiama.
    """
    import re
    import unicodedata
    piatto = unicodedata.normalize('NFKD', nome or '')
    piatto = piatto.encode('ascii', 'ignore').decode('ascii').lower()
    piatto = re.sub(r'[^a-z0-9]+', ' ', piatto).strip()
    if not piatto:
        return ''
    parole = piatto.split()
    # Toglie la forma societaria solo se sta in coda: "SRL COSTRUZIONI" e'
    # un nome, "COSTRUZIONI SRL" e' "COSTRUZIONI".
    for n in (3, 2, 1):
        if len(parole) > n and ' '.join(parole[-n:]) in _FORME:
            parole = parole[:-n]
            break
    return ' '.join(parole) or piatto


def _nome_migliore(a: str, b: str) -> str:
    """Fra due scritture dello stesso cliente, quella da mostrare.

    Si tiene la piu' completa — "DECA S.r.l." dice piu' di "deca" — e a parita'
    quella con le maiuscole, che e' come si scrive il nome di un'azienda.
    """
    if not a:
        return b
    if not b:
        return a
    if len(a) != len(b):
        return a if len(a) > len(b) else b
    return a if sum(1 for c in a if c.isupper()) >= sum(1 for c in b if c.isupper()) else b


def raggruppa_per_cliente(voci) -> list:
    """Somma per cliente, unendo le scritture diverse dello stesso nome.

    `voci` sono coppie (nome, minuti). Torna un elenco ordinato dal cliente
    piu' lavorato, con il nome scritto per esteso.
    """
    somme = {}
    for nome, minuti in voci:
        k = chiave_cliente(nome)
        if not k:
            continue
        v = somme.get(k) or {'cliente': nome, 'minuti': 0}
        v['cliente'] = _nome_migliore(v['cliente'], nome)
        v['minuti'] += int(minuti or 0)
        somme[k] = v
    fuori = sorted(somme.values(), key=lambda x: -x['minuti'])
    for v in fuori:
        v['ore'] = round(v['minuti'] / 60.0, 2)
    return fuori


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
        'scostamento_confermato': bool(g.scostamento_confermato),
    }


def giornata_tutti(data=None) -> list:
    """Stato della giornata per OGNI operaio, per la bacheca della timbratrice.

    Una sola chiamata invece di una per operaio: il tablet resta acceso tutto il
    giorno e si aggiorna da solo, quindi non deve fare N richieste ogni volta.

    Per ciascuno: se ha gia' registrato, quanti minuti in totale e su quali
    clienti. `dichiarata=False` significa "non ha ancora registrato", che e'
    diverso da una giornata registrata a zero.
    """
    from .models_ore import GiornataOre, RigaOre

    d = parse_data(data) or oggi_locale()
    operai = elenco_operai()
    session = get_session()
    try:
        giornate = {g.operatore_id: g for g in session.query(GiornataOre).filter(
            GiornataOre.data == d).all()}
        righe_per_giornata = {}
        if giornate:
            for r in session.query(RigaOre).filter(
                    RigaOre.giornata_id.in_([g.id for g in giornate.values()])).all():
                righe_per_giornata.setdefault(r.giornata_id, []).append(r)

        out = []
        for op in operai:
            g = giornate.get(op['id'])
            voce = {**op, 'data': d.isoformat(), 'dichiarata': g is not None,
                    'totale_minuti': 0, 'righe': [],
                    'scostamento_confermato': bool(g.scostamento_confermato) if g else False}
            if g is not None:
                righe = righe_per_giornata.get(g.id, [])
                # Sulla bacheca si guarda, non si modifica: le righe si
                # raggruppano per cliente, con la stessa regola del totale in
                # cima. Altrimenti la scheda mostra "deca 6 h" e "DECA 2 h"
                # mentre sopra c'e' scritto "DECA 8 h", e i due numeri della
                # stessa schermata sembrano non tornare. Chi corregge la
                # giornata vede invece le righe come le ha scritte.
                interne = sum(int(r.minuti or 0) for r in righe if r.attivita_interna)
                per_cliente = raggruppa_per_cliente(
                    [(r.cliente, r.minuti) for r in righe if not r.attivita_interna])
                voce['righe'] = [
                    {'cliente': v['cliente'], 'attivita_interna': False,
                     'minuti': v['minuti']} for v in per_cliente]
                if interne:
                    voce['righe'].append(
                        {'cliente': None, 'attivita_interna': True, 'minuti': interne})
                voce['totale_minuti'] = sum(x['minuti'] for x in voce['righe'])
            out.append(voce)
        return out
    finally:
        session.close()


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
                   revisione_attesa=None, richiesta_id=None, note=None,
                   scostamento_confermato=None) -> dict:
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
                scostamento_confermato=bool(scostamento_confermato),
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
            # Ogni salvataggio ridichiara la conferma: se l'operaio corregge e
            # arriva alle ore attese, la vecchia conferma non deve restare appesa.
            g.scostamento_confermato = bool(scostamento_confermato)
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
        # Il dettaglio tecnico resta nei log del server: all'operaio va un
        # messaggio comprensibile, non un errore SQL.
        logger.exception('salva_giornata fallita: %s', e)
        return {'error': 'Errore interno durante il salvataggio. Riprova.',
                'codice': 'errore_server'}
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
