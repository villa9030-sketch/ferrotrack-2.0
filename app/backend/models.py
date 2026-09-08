from datetime import datetime
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Float, Text, JSON, ForeignKey, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import enum
import logging
import os
import uuid

logger = logging.getLogger(__name__)

# Di norma il database e' quello dell'installazione. FERROTRACK_DB lo sposta
# altrove: serve per provare l'applicazione su una COPIA, senza il rischio di
# scrivere per sbaglio sui dati veri della produzione.
DATABASE_PATH = os.environ.get('FERROTRACK_DB') or os.path.join(
    os.path.dirname(__file__), '..', 'database', 'scheduler.db')
DATABASE_URL = f'sqlite:///{DATABASE_PATH.replace(chr(92), "/")}'

Base = declarative_base()

class FaseCorrente(str, enum.Enum):
    LASER = "LASER"
    PIEGA = "PIEGA"
    SALDATURA = "SALDATURA"
    PULIZIA = "PULIZIA"
    COMPLETATO = "COMPLETATO"
    PARZIALE = "PARZIALE"

class Order(Base):
    """Modello Ordine con articoli tracciati per fase"""
    __tablename__ = 'orders'
    id = Column(String, primary_key=True)
    cliente = Column(String, nullable=False)
    numero_ordine = Column(String, nullable=True)  # NUOVO: numero ordine estratto/inserito dal PDF
    data_ricezione = Column(DateTime, default=datetime.utcnow, nullable=False)
    data_consegna = Column(DateTime, nullable=False)
    status = Column(String, default="RICEVUTO")
    fase_corrente = Column(String, default="LASER")  # LASER, PIEGA, SALDATURA, PULIZIA, COMPLETATO, PARZIALE
    operatore_assegnato = Column(String, ForeignKey('users.id'), nullable=True)  # Auto-assegnato da operator_clients
    prezzo_quotato = Column(Float, nullable=True)  # Prezzo quotato per calcolo margine

    note = Column(Text)
    is_deleted = Column(Boolean, default=False)  # Soft delete — mai cancellare fisicamente

    # Origine ordine (merge preventivatore — Fase 0)
    origine = Column(String, default='PDF')  # 'PDF' (Elena carica) | 'PREVENTIVO' (commerciale accetta)
    preventivo_id_origine = Column(String, nullable=True)  # FK debole verso preventivi.id (no constraint per legacy)

    # Chiusura amministrativa (DDT / Fattura)
    numero_ddt = Column(String, nullable=True)
    data_ddt = Column(DateTime, nullable=True)
    numero_fattura = Column(String, nullable=True)
    data_fattura = Column(DateTime, nullable=True)
    note_chiusura = Column(Text, nullable=True)
    data_chiusura_amministrativa = Column(DateTime, nullable=True)
    chiuso_da = Column(String, nullable=True)  # user_id di chi ha chiuso
    parent_order_id = Column(String, ForeignKey('orders.id'), nullable=True)  # ID ordine padre (per lotti)
    lotto_numero = Column(Integer, default=0)  # 0 = ordine normale, 1+ = lotto
    lotto_nome = Column(String, nullable=True)  # Nome personalizzato del lotto (es. "Pezzi grandi")
    visto_da_operatore = Column(Boolean, default=False)  # True = operatore ha aperto/preso visione dell'ordine
    data_presa_visione = Column(DateTime, nullable=True)  # Quando l'operatore ha visto l'ordine
    # Storico: serviva a sbloccare le scansioni con la pistola. Dismesse le
    # pistole, resta come pre-conferma del laser per la sua coda di lavoro; non
    # blocca piu' nulla a valle.
    taglio_completato = Column(Boolean, default=False)
    data_taglio_completato = Column(DateTime, nullable=True)
    taglio_completato_da = Column(String, ForeignKey('users.id'), nullable=True)
    # Chiusura OPERATIVA dell'ordine: la registra l'ufficio (impiegata) quando
    # l'officina comunica che il lavoro e' finito. Colonne create da
    # migrations_ore._migra_colonne_ordini.
    data_completamento_operativo = Column(DateTime, nullable=True)
    completato_operativo_da = Column(String, nullable=True)
    data_consegna_effettiva = Column(DateTime, nullable=True)
    consegna_registrata_da = Column(String, nullable=True)
    # DDT emesso da un altro sistema: qui si registra solo il riferimento.
    ddt_numero = Column(String, nullable=True)
    ddt_data = Column(DateTime, nullable=True)
    # Consegna parziale: resta un residuo, la pratica non e' chiudibile.
    consegna_parziale = Column(Boolean, default=False)
    note_consegna = Column(Text, nullable=True)
    files = relationship('OrderFile', back_populates='order', cascade='all, delete-orphan')
    processing_steps = relationship('ProcessingStep', back_populates='order', cascade='all, delete-orphan')
    notifications = relationship('OrderNotification', back_populates='order', cascade='all, delete-orphan')
    # lotti: query manuale con Order.parent_order_id == self.id

class OrderFile(Base):
    __tablename__ = 'order_files'
    id = Column(String, primary_key=True)
    order_id = Column(String, ForeignKey('orders.id'), nullable=False)
    filename = Column(String, nullable=False)
    filepath = Column(String, nullable=False)
    file_type = Column(String)  # PDF, DXF
    # Impronta SHA256 del file mandato in produzione — garantisce che il file
    # tagliato sia verificabilmente quello registrato (preventivato ≡ prodotto).
    sha256 = Column(String, nullable=True)
    upload_date = Column(DateTime, default=datetime.utcnow)
    order = relationship('Order', back_populates='files')

class ProcessingStep(Base):
    """Fase di lavorazione di un ordine — traccia tempo per fase"""
    __tablename__ = 'processing_steps'
    id = Column(String, primary_key=True)
    order_id = Column(String, ForeignKey('orders.id'), nullable=False)
    fase = Column(String, nullable=False)  # LASER, PIEGA, SALDATURA, PULIZIA
    timestamp_inizio = Column(DateTime, nullable=True)  # NULL per LASER (no time tracking)
    timestamp_fine = Column(DateTime, nullable=True)
    operatore = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    fase_successiva = Column(String, nullable=True)  # Dove l'operatore ha mandato l'ordine dopo
    completamento_parziale = Column(Boolean, default=False)  # True = ordine non del tutto finito
    order = relationship('Order', back_populates='processing_steps')
    sessions = relationship('PhaseSession', back_populates='step', cascade='all, delete-orphan')

class PhaseSession(Base):
    """Sessione di lavoro su una fase — ogni avvio-pausa/completamento è una sessione"""
    __tablename__ = 'phase_sessions'
    id = Column(String, primary_key=True)
    step_id = Column(String, ForeignKey('processing_steps.id'), nullable=False)
    order_id = Column(String, ForeignKey('orders.id'), nullable=False)
    fase = Column(String, nullable=False)
    operatore = Column(String, nullable=True)
    timestamp_inizio = Column(DateTime, nullable=False)
    timestamp_fine = Column(DateTime, nullable=True)
    tipo_chiusura = Column(String, nullable=True)  # 'parziale' | 'totale' | None (aperta)
    note = Column(Text, nullable=True)
    step = relationship('ProcessingStep', back_populates='sessions')

class OrderNotification(Base):
    """Notifiche di completamento ordine"""
    __tablename__ = 'order_notifications'
    id = Column(String, primary_key=True)
    order_id = Column(String, ForeignKey('orders.id'), nullable=False)
    tempi_totali = Column(String)  # formato "2h 30min"
    viewed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    order = relationship('Order', back_populates='notifications')

class User(Base):
    """Utenti del sistema con ruoli e permessi"""
    __tablename__ = 'users'
    id = Column(String, primary_key=True)  # es: 'mirko-laser'
    name = Column(String, nullable=False)  # 'Mirko Sandionigi'
    role = Column(String, nullable=False)  # 'Operaio Laser', 'Amministratore', ecc
    initials = Column(String)  # 'LV'
    phase = Column(String)  # 'LASER', 'PIEGA', 'SALDATURA', 'ALL'
    permissions = Column(JSON, default=list)  # ['overview', 'lavorazione', 'supervisione', 'archive']
    machines = Column(JSON, default=list)  # ['CNC 01', 'Laser CO₂']
    is_capo = Column(Boolean, default=False)  # True = capo officina, controllo totale
    is_active = Column(Boolean, default=True)
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class OperatorClient(Base):
    """Assegnazioni fisse operatore-cliente"""
    __tablename__ = 'operator_clients'
    id = Column(String, primary_key=True)
    operator_id = Column(String, ForeignKey('users.id'), nullable=False)
    client_name = Column(String, nullable=False)  # Nome cliente (match esatto)
    operator = relationship('User')

class PhaseDelegation(Base):
    """Deleghe di fase: operatore principale delega una fase a un collega"""
    __tablename__ = 'phase_delegations'
    id = Column(String, primary_key=True)
    order_id = Column(String, ForeignKey('orders.id'), nullable=False)
    fase = Column(String, nullable=False)  # PIEGA, SALDATURA, PULIZIA
    operatore_principale = Column(String, ForeignKey('users.id'), nullable=False)
    operatore_delegato = Column(String, ForeignKey('users.id'), nullable=False)
    stato = Column(String, default='pending')  # pending, accepted, in_progress, completed, rejected
    delegata_da = Column(String, ForeignKey('users.id'), nullable=True)  # Chi ha creato la delega
    forzata = Column(Boolean, default=False)  # True = capo ha forzato senza accettazione
    data_delega = Column(DateTime, default=datetime.utcnow)
    scadenza = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
    tempo_inizio_delegato = Column(DateTime, nullable=True)
    tempo_fine_delegato = Column(DateTime, nullable=True)
    durata_effettiva = Column(Integer, nullable=True)  # secondi
    note_delegato = Column(Text, nullable=True)
    order = relationship('Order')

class SupportRequest(Base):
    """Richieste di supporto: operatore invita collega a collaborare sull'intero ordine"""
    __tablename__ = 'support_requests'
    id = Column(String, primary_key=True)
    order_id = Column(String, ForeignKey('orders.id'), nullable=False)
    operatore_principale = Column(String, ForeignKey('users.id'), nullable=False)
    operatore_supporto = Column(String, ForeignKey('users.id'), nullable=False)
    stato = Column(String, default='pending')  # pending, accepted, rejected, revoked
    forzata = Column(Boolean, default=False)
    data_richiesta = Column(DateTime, default=datetime.utcnow)
    data_risposta = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
    order = relationship('Order')

class AuditLog(Base):
    """Log di audit per tracciare azioni degli utenti"""
    __tablename__ = 'audit_log'
    id = Column(String, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    user_id = Column(String, ForeignKey('users.id'), nullable=True)  # FK a users.id
    user_name = Column(String)  # Denormalizzato per query veloci
    action = Column(String, nullable=False)  # 'LOGIN', 'LOGOUT', 'START_PHASE', 'COMPLETE_PHASE', 'CREA_ORDINE'
    entity_type = Column(String)  # 'order', 'phase', 'user'
    entity_id = Column(String)  # order_id correlato
    detail = Column(Text)  # JSON stringificato con dettagli aggiuntivi
    ip_address = Column(String, nullable=True)

class Notification(Base):
    """Notifiche UI persisted - sistema tipo WhatsApp per supervisore"""
    __tablename__ = 'notifications'
    id = Column(String, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    user_id = Column(String, ForeignKey('users.id'), nullable=False)  # Supervisore/Admin che riceve
    order_id = Column(String, ForeignKey('orders.id'), nullable=True)  # Ordine correlato
    title = Column(String, nullable=False)  # "Nuovo ordine", "Ordine completato", ecc
    message = Column(String, nullable=False)  # Testo notifica
    notification_type = Column(String, default='order')  # 'order', 'completion', 'alert'
    notification_category = Column(String, default='informativa')  # 'informativa', 'attiva', 'delega', 'urgente'
    is_read = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)  # Soft delete


class Pistola(Base):
    """Pistola barcode WiFi assegnata a un operatore.

    L'ID hardware (pistola_id) viene configurato una sola volta nella pistola
    stessa e inviato a ogni scan; il sistema risale all'operatore di conseguenza.
    """
    __tablename__ = 'pistole'
    id = Column(String, primary_key=True)
    pistola_id = Column(String, unique=True, nullable=False)  # ID hw configurato sulla pistola
    operatore_id = Column(String, ForeignKey('users.id'), nullable=False)
    attiva = Column(Boolean, default=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class OfficinaScan(Base):
    """Sessione di lavoro su un ordine in officina, aperta/chiusa da scan barcode.

    Una scan apre una sessione (timestamp_inizio). Si chiude quando:
    - lo stesso operaio scansiona un altro ordine ('altro_ordine')
    - capo/impiegata sposta l'ordine a fase successiva ('cambio_fase')
    - job di fine turno chiude le residue ('fine_turno')
    - admin chiude manualmente ('manuale')

    Tempo totale ordine = SUM(timestamp_fine - timestamp_inizio) sulle scan chiuse.
    """
    __tablename__ = 'officina_scans'
    id = Column(String, primary_key=True)
    order_id = Column(String, ForeignKey('orders.id'), nullable=False)
    operatore_id = Column(String, ForeignKey('users.id'), nullable=False)
    pistola_id = Column(String, nullable=True)  # ID hw audit (denormalizzato per storico)
    timestamp_inizio = Column(DateTime, nullable=False, default=datetime.utcnow)
    timestamp_fine = Column(DateTime, nullable=True)  # NULL = sessione attiva
    chiusura_motivo = Column(String, nullable=True)
    # 'altro_ordine' | 'cambio_fase' | 'fine_turno' | 'manuale'


# ============================================================================
#  PREVENTIVI — moduli portati dal Preventivatore desktop (Tkinter → web)
# ============================================================================

class Preventivo(Base):
    """Preventivo cliente — input per il workflow 'Accetta → crea Order FerroTrack'.

    Stati: BOZZA → INVIATO (snapshot immutabile) → ACCETTATO (crea Order) | RIFIUTATO.
    Versioning: quando una BOZZA passa a INVIATO si duplica come snapshot;
    modifiche successive partono da BOZZA v2 con parent_preventivo_id=snapshot v1.
    """
    __tablename__ = 'preventivi'
    id = Column(String, primary_key=True)
    cliente = Column(String, nullable=False)
    numero_ordine_cliente = Column(String, nullable=True)  # se cliente fornisce un suo numero RFQ
    quantita = Column(Integer, nullable=False, default=1)
    margine_pct = Column(Float, nullable=False, default=0.0)
    sconto_pct = Column(Float, nullable=False, default=0.0)
    data_consegna_proposta = Column(DateTime, nullable=True)  # in BOZZA può essere null
    status = Column(String, nullable=False, default='BOZZA')  # BOZZA|INVIATO|ACCETTATO|RIFIUTATO
    versione = Column(Integer, nullable=False, default=1)
    parent_preventivo_id = Column(String, ForeignKey('preventivi.id'), nullable=True)  # snapshot link
    totale_pezzo = Column(Float, nullable=False, default=0.0)
    totale_pezzo_con_margine = Column(Float, nullable=False, default=0.0)
    totale_pezzo_scontato = Column(Float, nullable=False, default=0.0)
    totale_lotto = Column(Float, nullable=False, default=0.0)
    costi_montaggio_totale = Column(Float, nullable=False, default=0.0)
    costi_tubolari_totale = Column(Float, nullable=False, default=0.0)
    costi_piastre_totale = Column(Float, nullable=False, default=0.0)
    # JSON con totali e percentuali usate al momento dell'invio. Serve a non
    # far cambiare da solo il PDF di un'offerta gia' comunicata al cliente.
    snapshot_economico = Column(Text, nullable=True)
    created_by = Column(String, ForeignKey('users.id'), nullable=True)
    data_creazione = Column(DateTime, nullable=False, default=datetime.utcnow)
    note = Column(Text, nullable=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    # True = richiesta caricata da Elena (Impiegata), in attesa di prezzatura dal
    # commerciale. Marcatore d'origine: il badge "da prezzare" si mostra finché è BOZZA.
    da_prezzare = Column(Boolean, nullable=False, default=False)
    # Tracciamento invio email al cliente (indirizzo usato + quando).
    email_cliente = Column(String, nullable=True)
    email_inviata_il = Column(DateTime, nullable=True)


class PreventivoArticolo(Base):
    """Articolo di un preventivo. Include geometria DXF + costi laser stimati."""
    __tablename__ = 'preventivo_articoli'
    id = Column(String, primary_key=True)
    preventivo_id = Column(String, ForeignKey('preventivi.id', ondelete='CASCADE'), nullable=False)
    codice = Column(String, nullable=False)
    quantita = Column(Integer, nullable=False, default=1)
    codice_assieme = Column(String, nullable=True)  # se appartiene a un assieme
    # --- geometria estratta da dxf_scanner ---
    area = Column(Float, nullable=False, default=0.0)
    area_dm2 = Column(Float, nullable=False, default=0.0)
    perimetro_taglio_m = Column(Float, nullable=False, default=0.0)
    n_forature = Column(Integer, nullable=False, default=0)
    spessore_mm = Column(Float, nullable=True)  # inserito dal commerciale
    materiale = Column(String, nullable=True)  # 'S235'|'INOX_304'|'ALU_5754'|...
    dxf_filename = Column(String, nullable=True)  # file DXF associato (per preview + trova-pezzo)
    # --- CAD interno: geometria confermata dall'operatore (fail-safe) ---
    # True quando l'operatore ha identificato il contorno nel CAD interno e
    # confermato. Il gate invio blocca se un articolo con DXF non è confermato.
    geometria_manuale_confermata = Column(Boolean, nullable=False, default=False)
    # origine della geometria: 'manual-click' | 'manual-waypoints' | 'manual-override' | None
    geometry_source = Column(String, nullable=True)
    # per pezzi piegati senza vista sviluppo piatto: l'operatore stima l'area
    # a mano (come oggi). True = area_dm2 è una stima umana, non traccia esatta.
    area_stimata_piega = Column(Boolean, nullable=False, default=False)
    # DXF CANONICO: file pulito generato dal contorno confermato (solo pezzo+fori),
    # byte-identico a ciò che è stato preventivato → va in produzione. + impronta.
    canonical_dxf_filename = Column(String, nullable=True)
    canonical_dxf_sha256 = Column(String, nullable=True)
    # DXF "pulito" (solo pezzo, senza cartiglio/quote/viste) — usato per thumbnail
    # commerciale + passaggio a Mirko per il nesting Lantek. Popolato o
    # automaticamente durante l'import batch se il detector v3 dà confidence >= 0.5
    # (con sanity check area_ratio), oppure manualmente dall'editor.
    cleaned_dxf_filename = Column(String, nullable=True)
    cleaned_status = Column(String, nullable=True)  # 'auto'|'auto_review'|'manual'|None
    # --- costo base (taglio + materiale) ---
    costo_materiale = Column(Float, nullable=False, default=0.0)  # da XLSX Lantek se importato
    costo_base_stimato = Column(Float, nullable=False, default=0.0)  # da laser_cost_estimator
    costo_base_override = Column(Float, nullable=True)  # se commerciale sovrascrive
    # --- costi lavorazione (post-taglio) ---
    pieghe = Column(Integer, nullable=False, default=0)
    saldatura_ml = Column(Float, nullable=False, default=0.0)
    # Tempo di saldatura stimato (minuti) — usato per il costo a tempo×tariffa.
    # saldatura_ml resta come riferimento (auto da DXF) + guida consumabili/pulizia.
    saldatura_min = Column(Float, nullable=False, default=0.0)
    filettatura_pz = Column(Integer, nullable=False, default=0)
    svasatura_pz = Column(Integer, nullable=False, default=0)
    costo_piega = Column(Float, nullable=False, default=0.0)
    costo_saldatura = Column(Float, nullable=False, default=0.0)
    costo_filettatura = Column(Float, nullable=False, default=0.0)
    costo_svasatura = Column(Float, nullable=False, default=0.0)
    costo_apporto = Column(Float, nullable=False, default=0.0)
    costo_pulizia = Column(Float, nullable=False, default=0.0)


class PreventivoAssieme(Base):
    """Assieme 3D dentro un preventivo (da analisi STEP)."""
    __tablename__ = 'preventivo_assiemi'
    id = Column(String, primary_key=True)
    preventivo_id = Column(String, ForeignKey('preventivi.id', ondelete='CASCADE'), nullable=False)
    codice_assieme = Column(String, nullable=False)
    qty = Column(Integer, nullable=False, default=1)
    ore_montaggio = Column(Float, nullable=False, default=0.0)
    ore_puntatura = Column(Float, nullable=False, default=0.0)
    costo = Column(Float, nullable=False, default=0.0)  # costo montaggio
    costo_puntatura = Column(Float, nullable=False, default=0.0)
    costo_saldatura_assieme = Column(Float, nullable=False, default=0.0)
    saldatura_mt = Column(Float, nullable=False, default=0.0)
    peso_kg = Column(Float, nullable=False, default=0.0)
    componenti_qty = Column(JSON, nullable=True)  # {codice: qty, ...}


class PreventivoTubolare(Base):
    """Tubolare dentro un preventivo (da analisi STEP)."""
    __tablename__ = 'preventivo_tubolari'
    id = Column(String, primary_key=True)
    preventivo_id = Column(String, ForeignKey('preventivi.id', ondelete='CASCADE'), nullable=False)
    codice_assieme = Column(String, nullable=True)
    profilo = Column(String, nullable=False)
    tipo = Column(String, nullable=True)  # 'dritto'|'obliquo'|'sagomato'
    materiale = Column(String, nullable=True, default='acciaio')
    lunghezza_m = Column(Float, nullable=False, default=0.0)
    peso_kg = Column(Float, nullable=False, default=0.0)
    costo_materiale = Column(Float, nullable=False, default=0.0)
    costo_taglio_totale = Column(Float, nullable=False, default=0.0)
    n_tagli_dritti = Column(Integer, nullable=False, default=0)
    n_tagli_obliqui = Column(Integer, nullable=False, default=0)


class PreventivoPiastra(Base):
    """Piastra dentro un preventivo (da analisi STEP)."""
    __tablename__ = 'preventivo_piastre'
    id = Column(String, primary_key=True)
    preventivo_id = Column(String, ForeignKey('preventivi.id', ondelete='CASCADE'), nullable=False)
    codice_assieme = Column(String, nullable=True)
    spessore_mm = Column(Float, nullable=False, default=0.0)
    area_dm2 = Column(Float, nullable=False, default=0.0)
    peso_kg = Column(Float, nullable=False, default=0.0)
    costo = Column(Float, nullable=False, default=0.0)
    materiale = Column(String, nullable=True, default='acciaio')


# Configurazione database
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

from sqlalchemy import event

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Abilita WAL mode, FK enforcement e impostazioni ottimali per SQLite"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")       # Anti-corruzione su crash/blackout
    cursor.execute("PRAGMA foreign_keys=ON")         # Integrità referenziale reale
    cursor.execute("PRAGMA synchronous=NORMAL")      # Sicuro con WAL, più veloce di FULL
    cursor.execute("PRAGMA wal_autocheckpoint=1000") # Checkpoint ogni 1000 pagine WAL
    cursor.close()

def get_session():
    return SessionLocal()

def seed_users():
    """Inserisce gli utenti di default se non esistono e rimuove quelli vecchi"""
    session = SessionLocal()
    try:
        # Utenti reali del sistema
        default_users = [
            {
                'id': 'elena-impiegata',
                'name': 'Elena Colombo',
                'role': 'Impiegata',
                'initials': 'EC',
                'phase': None,
                'permissions': ['overview', 'supervisione'],
                'machines': []
            },
            {
                'id': 'paolo-responsabile',
                'name': 'Paolo Scola',
                'role': 'Capo Officina',
                'initials': 'PS',
                'phase': 'ALL',
                'is_capo': True,
                'permissions': ['overview', 'supervisione', 'lavorazione', 'archive'],
                'machines': ['Tutte']
            },
            {
                'id': 'stefano-responsabile',
                'name': 'Stefano Villa',
                'role': 'Capo Officina',
                'initials': 'SV',
                'phase': 'ALL',
                'is_capo': True,
                'permissions': ['overview', 'supervisione', 'lavorazione', 'archive'],
                'machines': ['Tutte']
            },
            {
                'id': 'mirko-laser',
                'name': 'Mirko Sandionigi',
                'role': 'Operaio Laser',
                'initials': 'MS',
                'phase': 'LASER',
                'permissions': ['overview', 'lavorazione'],
                'machines': ['Laser CO₂']
            },
            {
                'id': 'enzo-officina',
                'name': 'Enzo Masciari',
                'role': 'Operaio Officina',
                'initials': 'EM',
                'phase': 'OFFICINA',
                'permissions': ['overview', 'lavorazione'],
                'machines': []
            }
        ]

        # Inserisci/aggiorna utenti reali (non cancella utenti creati dinamicamente)
        for user_data in default_users:
            existing = session.query(User).filter(User.id == user_data['id']).first()
            if existing:
                for k, v in user_data.items():
                    if k != 'id':
                        setattr(existing, k, v)
            else:
                user = User(**user_data)
                session.add(user)

        session.commit()
        logger.info("Seed users completato — 5 utenti reali")
    except Exception as e:
        session.rollback()
        logger.warning("Seed users error: %s", e)
    finally:
        session.close()

def _backup_db_before_migration():
    """Crea uno snapshot del database prima di ogni migrazione"""
    import shutil, time
    db_path = os.path.abspath(DATABASE_PATH)
    if not os.path.exists(db_path):
        return  # DB non ancora creato, nessun backup necessario
    backup_dir = os.path.join(os.path.dirname(db_path), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(backup_dir, f'scheduler_pre_migration_{ts}.db')
    shutil.copy2(db_path, dst)
    logger.info('Snapshot pre-migrazione: %s', dst)


def initialize_database():
    """Crea le tabelle se non esistono e popola i dati di default"""
    _backup_db_before_migration()
    # Registra le tabelle del sottosistema ORE prima di create_all
    from . import models_ore  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # Migrazione: aggiunge colonne tempo a phase_delegations se mancanti
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if 'phase_delegations' in insp.get_table_names():
        existing = [c['name'] for c in insp.get_columns('phase_delegations')]
        new_cols = {
            'tempo_inizio_delegato': 'DATETIME',
            'tempo_fine_delegato': 'DATETIME',
            'durata_effettiva': 'INTEGER',
            'note_delegato': 'TEXT',
        }
        with engine.connect() as conn:
            for col, col_type in new_cols.items():
                if col not in existing:
                    conn.execute(text(f'ALTER TABLE phase_delegations ADD COLUMN {col} {col_type}'))
                    logger.info('Aggiunta colonna %s a phase_delegations', col)
            conn.commit()

    # Migrazione: popola phase_sessions per ProcessingSteps esistenti
    if 'phase_sessions' in insp.get_table_names():
        with engine.connect() as conn:
            count = conn.execute(text('SELECT COUNT(*) FROM phase_sessions')).scalar()
            if count == 0:
                existing_steps = conn.execute(text(
                    'SELECT id, order_id, fase, operatore, timestamp_inizio, timestamp_fine, completamento_parziale '
                    'FROM processing_steps WHERE timestamp_inizio IS NOT NULL'
                )).fetchall()
                for step in existing_steps:
                    session_id = str(uuid.uuid4())
                    tipo = 'parziale' if step[6] else ('totale' if step[5] else None)
                    conn.execute(text(
                        'INSERT INTO phase_sessions (id, step_id, order_id, fase, operatore, '
                        'timestamp_inizio, timestamp_fine, tipo_chiusura) '
                        'VALUES (:id, :step_id, :order_id, :fase, :op, :ts_in, :ts_fin, :tipo)'
                    ), {
                        'id': session_id, 'step_id': step[0], 'order_id': step[1],
                        'fase': step[2], 'op': step[3], 'ts_in': step[4],
                        'ts_fin': step[5], 'tipo': tipo
                    })
                conn.commit()
                if existing_steps:
                    logger.info('Create %d phase_sessions retroattive', len(existing_steps))

    # Migrazione: aggiunge notification_category a notifications se mancante
    if 'notifications' in insp.get_table_names():
        existing_notif = [c['name'] for c in insp.get_columns('notifications')]
        if 'notification_category' not in existing_notif:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE notifications ADD COLUMN notification_category VARCHAR DEFAULT 'informativa'"))
                conn.commit()
                logger.info('Aggiunta colonna notification_category a notifications')

    # Migrazione: aggiunge is_deleted a orders se mancante
    if 'orders' in insp.get_table_names():
        existing_orders = [c['name'] for c in insp.get_columns('orders')]
        if 'is_deleted' not in existing_orders:
            with engine.connect() as conn:
                conn.execute(text('ALTER TABLE orders ADD COLUMN is_deleted BOOLEAN DEFAULT 0'))
                conn.commit()
                logger.info('Aggiunta colonna is_deleted a orders')

    # Migrazione: aggiunge parent_order_id e lotto_numero a orders per sistema lotti
    if 'orders' in insp.get_table_names():
        existing_orders = [c['name'] for c in insp.get_columns('orders')]
        with engine.connect() as conn:
            if 'parent_order_id' not in existing_orders:
                conn.execute(text('ALTER TABLE orders ADD COLUMN parent_order_id TEXT REFERENCES orders(id)'))
                logger.info('Aggiunta colonna parent_order_id a orders')
            if 'lotto_numero' not in existing_orders:
                conn.execute(text('ALTER TABLE orders ADD COLUMN lotto_numero INTEGER DEFAULT 0'))
                logger.info('Aggiunta colonna lotto_numero a orders')
            if 'lotto_nome' not in existing_orders:
                conn.execute(text('ALTER TABLE orders ADD COLUMN lotto_nome TEXT'))
                logger.info('Aggiunta colonna lotto_nome a orders')
            conn.commit()

    # Migrazione: aggiunge colonne chiusura amministrativa a orders
    if 'orders' in insp.get_table_names():
        existing_orders = [c['name'] for c in insp.get_columns('orders')]
        new_order_cols = {
            'numero_ddt': 'TEXT',
            'data_ddt': 'DATETIME',
            'numero_fattura': 'TEXT',
            'data_fattura': 'DATETIME',
            'note_chiusura': 'TEXT',
            'data_chiusura_amministrativa': 'DATETIME',
            'chiuso_da': 'TEXT',
        }
        with engine.connect() as conn:
            for col, col_type in new_order_cols.items():
                if col not in existing_orders:
                    conn.execute(text(f'ALTER TABLE orders ADD COLUMN {col} {col_type}'))
                    logger.info('Aggiunta colonna %s a orders', col)
            conn.commit()

    # Migrazione: aggiunge visto_da_operatore e data_presa_visione a orders
    if 'orders' in insp.get_table_names():
        existing_orders = [c['name'] for c in insp.get_columns('orders')]
        with engine.connect() as conn:
            if 'visto_da_operatore' not in existing_orders:
                conn.execute(text('ALTER TABLE orders ADD COLUMN visto_da_operatore BOOLEAN DEFAULT 0'))
                # Segna tutti gli ordini esistenti come già visti (non generare falsi "NUOVO")
                conn.execute(text('UPDATE orders SET visto_da_operatore = 1'))
                logger.info('Aggiunta colonna visto_da_operatore a orders (esistenti segnati come visti)')
            if 'data_presa_visione' not in existing_orders:
                conn.execute(text('ALTER TABLE orders ADD COLUMN data_presa_visione DATETIME'))
                logger.info('Aggiunta colonna data_presa_visione a orders')
            conn.commit()

    # Migrazione: ordini COMPLETATO esistenti → DA_FATTURARE
    if 'orders' in insp.get_table_names():
        with engine.connect() as conn:
            migrated = conn.execute(text(
                "UPDATE orders SET status = 'DA_FATTURARE' "
                "WHERE status = 'COMPLETATO' AND data_chiusura_amministrativa IS NULL"
            )).rowcount
            conn.commit()
            if migrated:
                logger.info('Migrati %d ordini COMPLETATO → DA_FATTURARE', migrated)

    # Migrazione: aggiunge campi laser/taglio a orders
    if 'orders' in insp.get_table_names():
        existing_orders = [c['name'] for c in insp.get_columns('orders')]
        with engine.connect() as conn:
            if 'taglio_completato' not in existing_orders:
                conn.execute(text('ALTER TABLE orders ADD COLUMN taglio_completato BOOLEAN DEFAULT 0'))
                # Ordini esistenti pre-refactor: considera taglio gia` fatto per non bloccarli
                conn.execute(text('UPDATE orders SET taglio_completato = 1'))
                logger.info('Aggiunta colonna taglio_completato a orders (esistenti segnati come gia` tagliati)')
            if 'data_taglio_completato' not in existing_orders:
                conn.execute(text('ALTER TABLE orders ADD COLUMN data_taglio_completato DATETIME'))
                logger.info('Aggiunta colonna data_taglio_completato a orders')
            if 'taglio_completato_da' not in existing_orders:
                conn.execute(text('ALTER TABLE orders ADD COLUMN taglio_completato_da TEXT'))
                logger.info('Aggiunta colonna taglio_completato_da a orders')
            conn.commit()

    # Migrazione: hash file produzione su order_files (2026-08-05)
    if 'order_files' in insp.get_table_names():
        existing_of = [c['name'] for c in insp.get_columns('order_files')]
        if 'sha256' not in existing_of:
            with engine.connect() as conn:
                conn.execute(text('ALTER TABLE order_files ADD COLUMN sha256 VARCHAR'))
                conn.commit()
                logger.info('Aggiunta colonna sha256 a order_files')

    # Migrazione: DXF cleanup su preventivo_articoli (2026-07-06)
    if 'preventivo_articoli' in insp.get_table_names():
        existing_prev_art = [c['name'] for c in insp.get_columns('preventivo_articoli')]
        with engine.connect() as conn:
            if 'cleaned_dxf_filename' not in existing_prev_art:
                conn.execute(text('ALTER TABLE preventivo_articoli ADD COLUMN cleaned_dxf_filename VARCHAR'))
                logger.info('Aggiunta colonna cleaned_dxf_filename a preventivo_articoli')
            if 'cleaned_status' not in existing_prev_art:
                conn.execute(text('ALTER TABLE preventivo_articoli ADD COLUMN cleaned_status VARCHAR'))
                logger.info('Aggiunta colonna cleaned_status a preventivo_articoli')
            # CAD interno — geometria confermata (2026-08-03)
            if 'geometria_manuale_confermata' not in existing_prev_art:
                conn.execute(text('ALTER TABLE preventivo_articoli ADD COLUMN geometria_manuale_confermata BOOLEAN DEFAULT 0'))
                logger.info('Aggiunta colonna geometria_manuale_confermata a preventivo_articoli')
            if 'geometry_source' not in existing_prev_art:
                conn.execute(text('ALTER TABLE preventivo_articoli ADD COLUMN geometry_source VARCHAR'))
                logger.info('Aggiunta colonna geometry_source a preventivo_articoli')
            if 'area_stimata_piega' not in existing_prev_art:
                conn.execute(text('ALTER TABLE preventivo_articoli ADD COLUMN area_stimata_piega BOOLEAN DEFAULT 0'))
                logger.info('Aggiunta colonna area_stimata_piega a preventivo_articoli')
            # Saldatura a tempo (2026-08-05)
            if 'saldatura_min' not in existing_prev_art:
                conn.execute(text('ALTER TABLE preventivo_articoli ADD COLUMN saldatura_min FLOAT DEFAULT 0'))
                logger.info('Aggiunta colonna saldatura_min a preventivo_articoli')
            # DXF canonico (2026-08-05)
            if 'canonical_dxf_filename' not in existing_prev_art:
                conn.execute(text('ALTER TABLE preventivo_articoli ADD COLUMN canonical_dxf_filename VARCHAR'))
                logger.info('Aggiunta colonna canonical_dxf_filename a preventivo_articoli')
            if 'canonical_dxf_sha256' not in existing_prev_art:
                conn.execute(text('ALTER TABLE preventivo_articoli ADD COLUMN canonical_dxf_sha256 VARCHAR'))
                logger.info('Aggiunta colonna canonical_dxf_sha256 a preventivo_articoli')
            conn.commit()

    # Migrazione: intake richieste da Elena su preventivi (2026-08-18)
    if 'preventivi' in insp.get_table_names():
        existing_prev = [c['name'] for c in insp.get_columns('preventivi')]
        with engine.connect() as conn:
            if 'da_prezzare' not in existing_prev:
                conn.execute(text('ALTER TABLE preventivi ADD COLUMN da_prezzare BOOLEAN DEFAULT 0'))
                logger.info('Aggiunta colonna da_prezzare a preventivi')
            if 'email_cliente' not in existing_prev:
                conn.execute(text('ALTER TABLE preventivi ADD COLUMN email_cliente VARCHAR'))
                logger.info('Aggiunta colonna email_cliente a preventivi')
            if 'email_inviata_il' not in existing_prev:
                conn.execute(text('ALTER TABLE preventivi ADD COLUMN email_inviata_il DATETIME'))
                logger.info('Aggiunta colonna email_inviata_il a preventivi')
            conn.commit()

    # Migrazioni additive del sottosistema ORE (colonne ordine, seed clienti/config)
    from .migrations_ore import migrate_ore
    migrate_ore(engine)

    seed_users()
