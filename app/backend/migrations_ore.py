"""Migrazioni ADDITIVE del sottosistema ORE / ORDINI-UFFICIO / RIEPILOGO.

Idempotente: puo' essere eseguita a ogni avvio senza effetti collaterali.
Non elimina tabelle, colonne o dati storici.

Cosa fa:
 1. Aggiunge a `orders` le colonne che distinguono le fasi amministrative
    (lavorazione completata / consegna effettiva), oggi confuse in un unico stato.
 2. Popola l'anagrafica `clienti` dai nomi gia' presenti in orders/preventivi.
 3. Crea la configurazione `ore_attese` di default per gli operai attivi.

NOTA: non viene inventata nessuna tariffa oraria. Finche' l'ufficio non la
imposta, il riepilogo economico segnala il dato come ASSENTE (diverso da zero).
"""
import logging
import uuid
from datetime import datetime

from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)

# Colonne additive su `orders`: separano i passaggi che oggi collassano su
# DA_FATTURARE/CHIUSO. Nessuna sovrascrive campi esistenti (numero_ddt/data_ddt,
# numero_fattura/data_fattura, data_chiusura_amministrativa restano validi).
_ORDER_COLS = {
    # Lavorazione FISICA completata (dichiarata dall'impiegata su comunicazione
    # verbale dell'operaio). Diverso da "DDT preparato" e da "consegnato".
    'data_completamento_operativo': 'DATETIME',
    'completato_operativo_da': 'VARCHAR',
    # Merce effettivamente consegnata (diverso dal DDT preparato).
    'data_consegna_effettiva': 'DATETIME',
    'consegna_registrata_da': 'VARCHAR',
    'ddt_numero': 'VARCHAR',
    'ddt_data': 'DATETIME',
    'consegna_parziale': 'BOOLEAN',
    'note_consegna': 'TEXT',
}

# Colonne aggiunte a tabelle del sottosistema DOPO la loro prima creazione.
# `create_all` crea le tabelle mancanti ma NON aggiunge colonne a quelle esistenti:
# senza questo passaggio un database creato con una versione precedente si rompe.
_NUOVE_TABELLE_COLS = {
    'giornate_ore': {'ultima_richiesta_id': 'VARCHAR',
                     'scostamento_confermato': 'BOOLEAN'},
    # Fotografia economica scattata all'INVIO: un'offerta gia' mandata non deve
    # cambiare prezzo se qualcuno modifica i costi in configurazione.
    'preventivi': {'snapshot_economico': 'TEXT',
                   # Preventivo tecnico nascosto dietro un ordine caricato
                   # col pacchetto del cliente (PDF + disegni).
                   'solo_tecnico': 'BOOLEAN'},
    # Distingue una POSTAZIONE da cui si entra (timbratrice, laser, ufficio)
    # da una PERSONA di cui si contano le ore. Prima stavano mescolate.
    'users': {'e_postazione': 'BOOLEAN'},
    # Lo smistamento del laser: quali ordini passano da lui e quali no.
    'orders': {'taglio_richiesto': 'BOOLEAN',
               'smistato_il': 'DATETIME',
               'smistato_da': 'VARCHAR',
               # Calendario del laser: il giorno in cui il laserista vuole
               # tagliarlo (vuoto = il giorno di consegna) e, per gli ordini
               # senza disegni, la durata stimata a mano.
               'data_taglio_pianificata': 'DATE',
               'durata_laser_manuale_min': 'FLOAT',
               # Importato in Lantek (fase fra "da importare" e "tagliato")
               'importato_lantek_il': 'DATETIME',
               'importato_lantek_da': 'VARCHAR'},
}

# Con che valore nasce una colonna nuova sulle righe che c'erano gia'.
# Quello che NON e' elencato qui resta vuoto, di proposito: per un campo a tre
# stati "vuoto" e' lo stato "non ancora deciso", e riempirlo vorrebbe dire
# decidere al posto di qualcuno.
_VALORE_DI_PARTENZA = {
    # Chi c'era prima delle postazioni e' una persona, non un posto.
    ('users', 'e_postazione'): 0,
    # I preventivi che c'erano sono tutti offerte vere: nessuno e' tecnico.
    ('preventivi', 'solo_tecnico'): 0,
    # `orders.taglio_richiesto` NON sta qui: 0 vorrebbe dire "il laser ha
    # deciso che non va tagliato", e nessuno lo ha deciso. Se ne occupa
    # _smistamento_iniziale(), che guarda cosa e' gia' stato tagliato.
}

# Ruoli considerati "operai di officina" per il seed della compilazione ore.
_RUOLI_OPERAI = ('Operaio Laser', 'Operaio Officina')


def _column_exists(insp, table, column):
    try:
        return column in [c['name'] for c in insp.get_columns(table)]
    except Exception:
        return False


def _migra_colonne_ordine(engine, insp):
    """Aggiunge le colonne di ciclo amministrativo se mancanti."""
    if 'orders' not in insp.get_table_names():
        return 0
    aggiunte = 0
    with engine.connect() as conn:
        for col, tipo in _ORDER_COLS.items():
            if not _column_exists(insp, 'orders', col):
                conn.execute(text(f'ALTER TABLE orders ADD COLUMN {col} {tipo}'))
                logger.info('migrations_ore: aggiunta colonna orders.%s', col)
                aggiunte += 1
        conn.commit()
    return aggiunte


def _migra_colonne_sottosistema(engine, insp):
    """Allinea le colonne delle tabelle del sottosistema ore (idempotente)."""
    aggiunte = 0
    with engine.connect() as conn:
        for tabella, colonne in _NUOVE_TABELLE_COLS.items():
            if tabella not in insp.get_table_names():
                continue
            for col, tipo in colonne.items():
                if not _column_exists(insp, tabella, col):
                    conn.execute(text(f'ALTER TABLE {tabella} ADD COLUMN {col} {tipo}'))
                    logger.info('migrations_ore: aggiunta colonna %s.%s', tabella, col)
                    aggiunte += 1
                    # Una colonna nuova nasce vuota sulle righe che c'erano
                    # gia', e "vuoto" non e' "falso": una domanda come "chi non
                    # e' una postazione?" non le troverebbe.
                    #
                    # Ma il valore di partenza non e' lo stesso per tutte, e
                    # sbagliarlo non da' errori: cambia solo, in silenzio, il
                    # significato dei dati. Per questo si dichiara colonna per
                    # colonna, e dove non e' dichiarato la colonna resta vuota.
                    partenza = _VALORE_DI_PARTENZA.get((tabella, col))
                    if partenza is not None:
                        conn.execute(text(
                            f'UPDATE {tabella} SET {col} = {partenza} '
                            f'WHERE {col} IS NULL'))
                        logger.info('migrations_ore: %s.%s parte da %s',
                                    tabella, col, partenza)
        conn.commit()
    return aggiunte


def _smistamento_iniziale(engine, insp):
    """Segna come "da tagliare" gli ordini che risultano gia' tagliati.

    E' l'unica deduzione sicura: se il taglio e' stato fatto, il pezzo passava
    dal laser. Tutto il resto resta da smistare, perche' distinguere un
    tubolare da una lamiera guardando il database non si puo', e sbagliare
    vorrebbe dire o riempire la coda del laser di roba che non lo riguarda, o
    far comparire in officina pezzi che nessuno ha ancora tagliato.
    """
    if 'orders' not in insp.get_table_names():
        return 0
    for col in ('taglio_richiesto', 'smistato_il'):
        if not _column_exists(insp, 'orders', col):
            return 0

    with engine.connect() as conn:
        # `smistato_il` c'e' solo se qualcuno ha deciso davvero. Dove manca, il
        # valore di `taglio_richiesto` non e' di nessuno: o non c'e' mai stato,
        # o ce l'ha messo una migrazione. In entrambi i casi si puo' sistemare.
        gia_tagliati = conn.execute(text(
            'UPDATE orders SET taglio_richiesto = 1 '
            'WHERE smistato_il IS NULL '
            '  AND taglio_completato = 1 '
            '  AND (taglio_richiesto IS NULL OR taglio_richiesto = 0)'))

        # Un ordine mai tagliato e mai smistato da nessuno deve tornare "da
        # guardare": lasciarlo a 0 vorrebbe dire mandarlo in officina dicendo
        # che il laser lo ha scartato, cosa che non e' successa.
        mai_visti = conn.execute(text(
            'UPDATE orders SET taglio_richiesto = NULL '
            'WHERE smistato_il IS NULL '
            '  AND (taglio_completato IS NULL OR taglio_completato = 0) '
            '  AND taglio_richiesto IS NOT NULL'))
        conn.commit()
        n, m = gia_tagliati.rowcount or 0, mai_visti.rowcount or 0

    if n:
        logger.info('migrations_ore: %d ordini gia\' tagliati segnati come '
                    '"da tagliare"', n)
    if m:
        logger.warning('migrations_ore: %d ordini rimessi fra quelli da '
                       'smistare (avevano un valore che nessuno aveva deciso)', m)
    return n + m


def _seed_clienti(engine, insp):
    """Popola `clienti` con i nomi distinti gia' usati in orders e preventivi.

    Non cancella nulla: aggiunge solo i mancanti. Il match resta sul nome esatto
    per compatibilita' con lo storico (orders.cliente e' una stringa libera).
    """
    if 'clienti' not in insp.get_table_names():
        return 0
    nomi = set()
    with engine.connect() as conn:
        for tabella in ('orders', 'preventivi'):
            if tabella not in insp.get_table_names():
                continue
            # Un preventivo tecnico (pacchetto d'ordine) puo' portare un nome
            # letto male dal PDF, poi corretto sull'ordine: non fa anagrafica.
            filtro = (' AND COALESCE(solo_tecnico, 0) = 0'
                      if tabella == 'preventivi'
                      and _column_exists(insp, 'preventivi', 'solo_tecnico') else '')
            try:
                rows = conn.execute(text(
                    f'SELECT DISTINCT cliente FROM {tabella} '
                    f'WHERE cliente IS NOT NULL AND TRIM(cliente) <> ""' + filtro
                )).fetchall()
                for (nome,) in rows:
                    n = (nome or '').strip()
                    if n:
                        nomi.add(n)
            except Exception as e:
                logger.warning('migrations_ore: lettura clienti da %s fallita: %s', tabella, e)

        if not nomi:
            return 0
        esistenti = {r[0] for r in conn.execute(text('SELECT nome FROM clienti')).fetchall()}
        nuovi = sorted(nomi - esistenti)
        for nome in nuovi:
            conn.execute(
                text('INSERT INTO clienti (id, nome, attivo, created_at) '
                     'VALUES (:id, :nome, 1, :ts)'),
                {'id': str(uuid.uuid4()), 'nome': nome, 'ts': datetime.utcnow()},
            )
        conn.commit()
    if nuovi:
        logger.info('migrations_ore: %d clienti aggiunti in anagrafica', len(nuovi))
    return len(nuovi)


def _seed_ore_attese(engine, insp):
    """Crea la configurazione di default per gli operai attivi.

    Default prudente: 8h (480 min), lun-ven, tenuti alla compilazione.
    L'impiegata puo' modificare tutto dall'interfaccia: NON assumiamo che valga
    per sempre e per tutti.
    """
    if 'ore_attese' not in insp.get_table_names() or 'users' not in insp.get_table_names():
        return 0
    creati = 0
    with engine.connect() as conn:
        ruoli = ','.join(f"'{r}'" for r in _RUOLI_OPERAI)
        operai = conn.execute(text(
            f'SELECT id FROM users WHERE is_active = 1 AND role IN ({ruoli})'
        )).fetchall()
        gia = {r[0] for r in conn.execute(text('SELECT operatore_id FROM ore_attese')).fetchall()}
        for (uid,) in operai:
            if uid in gia:
                continue
            conn.execute(
                text('INSERT INTO ore_attese '
                     '(id, operatore_id, tenuto_alla_compilazione, minuti_attesi, '
                     ' giorni_settimana, aggiornata_il, aggiornata_da) '
                     'VALUES (:id, :op, 1, 480, :gg, :ts, :da)'),
                {'id': str(uuid.uuid4()), 'op': uid, 'gg': '[1, 2, 3, 4, 5]',
                 'ts': datetime.utcnow(), 'da': 'migrazione'},
            )
            creati += 1
        conn.commit()
    if creati:
        logger.info('migrations_ore: configurazione ore attese creata per %d operai', creati)
    return creati


def _unifica_ddt(engine, insp):
    """Un DDT solo: copia numero_ddt/data_ddt nei campi buoni ddt_numero/ddt_data.

    La vecchia scheda "Ordini da fatturare" scriveva nelle colonne vecchie,
    la vista ordini in quelle nuove, e ogni schermata leggeva solo le sue: lo
    stesso ordine aveva il DDT per una pagina e non per l'altra. Si copia SOLO
    dove il campo nuovo e' vuoto: un valore gia' scritto non si tocca.

    Poi si sistemano le date DDT salvate come istante UTC (la vista ordini
    usava utcnow): un DDT ha una data di calendario, e un DDT delle 00:30
    italiane risultava del giorno prima. Si riconoscono perche' non sono a
    mezzanotte; quelle a mezzanotte sono gia' date e restano come sono.

    Idempotente: alla seconda esecuzione non trova piu' niente da fare.
    """
    if 'orders' not in insp.get_table_names():
        return 0
    servono = ('numero_ddt', 'data_ddt', 'ddt_numero', 'ddt_data')
    if not all(_column_exists(insp, 'orders', c) for c in servono):
        return 0
    from .orario import data_locale
    toccati = 0
    with engine.connect() as conn:
        toccati += conn.execute(text(
            "UPDATE orders SET ddt_numero = numero_ddt "
            "WHERE (ddt_numero IS NULL OR TRIM(ddt_numero) = '') "
            "AND numero_ddt IS NOT NULL AND TRIM(numero_ddt) != ''")).rowcount or 0
        toccati += conn.execute(text(
            "UPDATE orders SET ddt_data = data_ddt "
            "WHERE ddt_data IS NULL AND data_ddt IS NOT NULL "
            "AND ddt_numero = numero_ddt")).rowcount or 0
        righe = conn.execute(text(
            "SELECT id, ddt_data FROM orders WHERE ddt_data IS NOT NULL "
            "AND time(ddt_data) != '00:00:00'")).fetchall()
        for oid, valore in righe:
            try:
                istante = valore if isinstance(valore, datetime) else \
                    datetime.fromisoformat(str(valore))
            except ValueError:
                continue
            giorno = data_locale(istante)
            conn.execute(text("UPDATE orders SET ddt_data = :d WHERE id = :id"),
                         {'d': datetime(giorno.year, giorno.month, giorno.day)
                          .strftime('%Y-%m-%d %H:%M:%S.%f'), 'id': oid})
            toccati += 1
        conn.commit()
    if toccati:
        logger.info('migrations_ore: DDT unificati/normalizzati su %d campi', toccati)
    return toccati


# (tabella, colonne) degli indici che servono alle liste lette di continuo.
# Senza, ogni lettura degli ordini scorreva file, fasi e sessioni per intero:
# con qualche anno di archivio la coda del laser costava secondi a chiamata.
_INDICI = (
    ('orders', ('status',)),
    ('orders', ('is_deleted', 'data_consegna')),
    ('order_files', ('order_id',)),
    ('processing_steps', ('order_id',)),
    ('phase_sessions', ('order_id',)),
    ('phase_sessions', ('step_id',)),
    ('support_requests', ('order_id',)),
    ('notifications', ('user_id',)),
    ('preventivo_articoli', ('preventivo_id',)),
)


def _indici_prestazioni(engine, insp):
    """Crea gli indici mancanti. Idempotente (IF NOT EXISTS)."""
    tabelle = set(insp.get_table_names())
    creati = 0
    with engine.connect() as conn:
        for tab, cols in _INDICI:
            if tab not in tabelle or not all(_column_exists(insp, tab, c) for c in cols):
                continue
            nome = 'ix_ft_%s_%s' % (tab, '_'.join(cols))
            conn.execute(text('CREATE INDEX IF NOT EXISTS %s ON %s (%s)'
                              % (nome, tab, ', '.join(cols))))
            creati += 1
        conn.commit()
    return creati


def migrate_ore(engine):
    """Punto di ingresso unico. Sicuro da chiamare a ogni avvio."""
    try:
        insp = inspect(engine)
        cols = _migra_colonne_ordine(engine, insp)
        insp = inspect(engine)  # ricarica dopo gli ALTER
        cols += _migra_colonne_sottosistema(engine, insp)
        insp = inspect(engine)
        _smistamento_iniziale(engine, insp)
        _unifica_ddt(engine, insp)
        _indici_prestazioni(engine, insp)
        cli = _seed_clienti(engine, insp)
        ore = _seed_ore_attese(engine, insp)
        if cols or cli or ore:
            logger.info('migrations_ore completata: %d colonne, %d clienti, %d config operai',
                        cols, cli, ore)
        return {'colonne': cols, 'clienti': cli, 'ore_attese': ore}
    except Exception as e:
        # Una migrazione fallita non deve impedire l'avvio dell'applicazione.
        logger.exception('migrations_ore fallita: %s', e)
        return {'error': str(e)}
