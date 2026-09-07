"""Modello dati del sottosistema ORE / DICHIARAZIONI / RIEPILOGO ECONOMICO.

Isolato da `models.py` (monolitico) ma condivide la stessa `Base`, cosi'
`Base.metadata.create_all()` crea anche queste tabelle. Tutte le tabelle sono
NUOVE (additive): nessuna modifica distruttiva allo schema esistente.

Principi:
- Le durate sono in MINUTI INTERI (mai float): somme esatte, niente errori di
  virgola mobile.
- La GIORNATA e' l'aggregato: se esiste una `GiornataOre` la giornata e' stata
  DICHIARATA (anche con 0 minuti); se non esiste e' NON DICHIARATA. I due casi
  non devono mai essere confusi.
- Le ore dichiarate appartengono a (operaio, giorno, cliente | attivita' interna)
  e NON sono legate a ordini o preventivi.
"""
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Date, Text, JSON,
    ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship

from .models import Base


# ---------------------------------------------------------------------------
# Identita' di dispositivo (sostituisce la fiducia nello user_id del browser)
# ---------------------------------------------------------------------------
class DeviceToken(Base):
    """Token di dispositivo verificato dal server.

    Il tablet viene configurato UNA VOLTA con un token; da quel momento gli
    operai non fanno alcun passaggio aggiuntivo. Il backend deriva i permessi
    DAL TOKEN, non da cio' che dichiara il client.

    Scope:
      'ore'     -> solo operazioni sulle dichiarazioni ore
      'reparto' -> sola lettura ordini e allegati
      'ufficio' -> operazioni dell'impiegata (correzioni, ordini)
    """
    __tablename__ = 'device_tokens'
    id = Column(String, primary_key=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    label = Column(String, nullable=False)          # es. "Tablet officina 1"
    scope = Column(String, nullable=False)          # 'ore' | 'reparto' | 'ufficio'
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Anagrafica clienti (prima erano solo stringhe libere sugli ordini)
# ---------------------------------------------------------------------------
class Cliente(Base):
    """Anagrafica clienti condivisa fra dichiarazioni ore, ordini e riepiloghi.

    Popolata alla migrazione dai nomi gia' presenti in orders/preventivi.
    `nome` resta la chiave logica (match esatto) per compatibilita' con lo storico.
    """
    __tablename__ = 'clienti'
    id = Column(String, primary_key=True)
    nome = Column(String, nullable=False, unique=True, index=True)
    attivo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    note = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Dichiarazioni ore
# ---------------------------------------------------------------------------
class GiornataOre(Base):
    """Dichiarazione di UNA giornata di UN operaio (aggregato atomico).

    L'esistenza della riga = "giornata dichiarata". Una giornata dichiarata con
    zero minuti e' diversa da una giornata mai dichiarata.
    `revisione` serve al controllo di concorrenza ottimistico fra tablet e
    ufficio (evita sovrascritture silenziose).
    """
    __tablename__ = 'giornate_ore'
    __table_args__ = (
        UniqueConstraint('operatore_id', 'data', name='uq_giornata_operatore_data'),
        Index('ix_giornate_data', 'data'),
    )
    id = Column(String, primary_key=True)
    operatore_id = Column(String, ForeignKey('users.id'), nullable=False)
    data = Column(Date, nullable=False)              # giorno LOCALE (Europe/Rome)
    revisione = Column(Integer, default=1, nullable=False)
    dichiarata_il = Column(DateTime, default=datetime.utcnow, nullable=False)
    aggiornata_il = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Tracciabilita': chi/cosa ha effettuato l'operazione (device o utente ufficio),
    # distinto dall'operaio SELEZIONATO (operatore_id) che e' una dichiarazione.
    origine = Column(String, default='tablet', nullable=False)   # 'tablet' | 'ufficio'
    device_label = Column(String, nullable=True)
    modificata_da = Column(String, nullable=True)
    # Chiave di idempotenza: un doppio tocco o un retry dopo timeout con la
    # stessa richiesta NON deve applicare due volte il salvataggio.
    ultima_richiesta_id = Column(String, nullable=True)
    # L'operaio ha confermato esplicitamente un totale diverso dalle ore attese
    # (es. mezza giornata). Serve a distinguere "ha sbagliato/dimenticato" da
    # "ha davvero lavorato meno e lo sa".
    scostamento_confermato = Column(Boolean, default=False)
    note = Column(Text, nullable=True)
    righe = relationship('RigaOre', back_populates='giornata',
                         cascade='all, delete-orphan')


class RigaOre(Base):
    """Ore di una giornata su un cliente, oppure su 'attivita' interna'.

    `minuti` e' intero. `attivita_interna=True` -> `cliente` e' NULL e la riga
    resta separata dai clienti in tutti i riepiloghi.
    """
    __tablename__ = 'righe_ore'
    __table_args__ = (
        UniqueConstraint('giornata_id', 'cliente', 'attivita_interna',
                         name='uq_riga_giornata_cliente'),
    )
    id = Column(String, primary_key=True)
    giornata_id = Column(String, ForeignKey('giornate_ore.id'), nullable=False, index=True)
    cliente = Column(String, nullable=True)
    attivita_interna = Column(Boolean, default=False, nullable=False)
    minuti = Column(Integer, nullable=False, default=0)
    giornata = relationship('GiornataOre', back_populates='righe')


# ---------------------------------------------------------------------------
# Configurazione ore attese + eccezioni (assenze / giornate ridotte)
# ---------------------------------------------------------------------------
class OreAttese(Base):
    """Chi e' tenuto a compilare, in quali giorni e per quante ore.

    Volutamente minimale: NON e' un sistema di presenze/paghe.
    """
    __tablename__ = 'ore_attese'
    id = Column(String, primary_key=True)
    operatore_id = Column(String, ForeignKey('users.id'), nullable=False, unique=True)
    tenuto_alla_compilazione = Column(Boolean, default=True, nullable=False)
    minuti_attesi = Column(Integer, default=480, nullable=False)      # 8h
    giorni_settimana = Column(JSON, default=lambda: [1, 2, 3, 4, 5])  # ISO: 1=lun
    aggiornata_il = Column(DateTime, default=datetime.utcnow)
    aggiornata_da = Column(String, nullable=True)


class EccezioneGiorno(Base):
    """Assenza / giornata ridotta / festivo per un operaio in una data."""
    __tablename__ = 'eccezioni_giorno'
    __table_args__ = (
        UniqueConstraint('operatore_id', 'data', name='uq_eccezione_operatore_data'),
    )
    id = Column(String, primary_key=True)
    operatore_id = Column(String, ForeignKey('users.id'), nullable=False)
    data = Column(Date, nullable=False)
    tipo = Column(String, nullable=False)              # 'assenza' | 'ridotta' | 'festivo'
    minuti_attesi = Column(Integer, nullable=True)     # valorizzato per 'ridotta'
    nota = Column(Text, nullable=True)
    creata_il = Column(DateTime, default=datetime.utcnow)
    creata_da = Column(String, nullable=True)


class AnomaliaOre(Base):
    """Anomalia rilevata sulla giornata di un operaio.

    tipo: 'mancante' (nessuna dichiarazione) | 'sotto' | 'sopra'.
    `notificata` evita notifiche duplicate a ogni avvio/controllo.
    """
    __tablename__ = 'anomalie_ore'
    __table_args__ = (
        UniqueConstraint('operatore_id', 'data', name='uq_anomalia_operatore_data'),
        Index('ix_anomalie_stato_data', 'stato', 'data'),
    )
    id = Column(String, primary_key=True)
    operatore_id = Column(String, ForeignKey('users.id'), nullable=False)
    data = Column(Date, nullable=False)
    tipo = Column(String, nullable=False)
    minuti_dichiarati = Column(Integer, default=0, nullable=False)
    minuti_attesi = Column(Integer, default=0, nullable=False)
    stato = Column(String, default='aperta', nullable=False)   # 'aperta' | 'risolta'
    rilevata_il = Column(DateTime, default=datetime.utcnow)
    risolta_il = Column(DateTime, nullable=True)
    notificata = Column(Boolean, default=False, nullable=False)


# ---------------------------------------------------------------------------
# Dati economici (riepilogo per cliente)
# ---------------------------------------------------------------------------
class CostoOrario(Base):
    """Costo orario aziendale STANDARD con validita' temporale.

    Una variazione futura non deve ricalcolare silenziosamente lo storico:
    ogni periodo usa la tariffa valida in quel momento.
    """
    __tablename__ = 'costo_orario'
    id = Column(String, primary_key=True)
    valido_dal = Column(Date, nullable=False, unique=True)
    euro_ora = Column(Float, nullable=False)
    creato_il = Column(DateTime, default=datetime.utcnow)
    creato_da = Column(String, nullable=True)
    nota = Column(Text, nullable=True)


class FatturatoCliente(Base):
    """Fatturato inserito manualmente dall'ufficio (fonte amministrativa).

    Il sistema NON dispone di importi fatturati: questo dato e' dichiarato a mano
    e va sempre presentato come tale, mai confuso con preventivi o valore ordini.
    """
    __tablename__ = 'fatturato_cliente'
    __table_args__ = (
        UniqueConstraint('cliente', 'anno', 'mese', name='uq_fatturato_cliente_periodo'),
    )
    id = Column(String, primary_key=True)
    cliente = Column(String, nullable=False, index=True)
    anno = Column(Integer, nullable=False)
    mese = Column(Integer, nullable=False)
    importo = Column(Float, nullable=False, default=0.0)
    riferimento = Column(String, nullable=True)     # es. n. fattura/e
    nota = Column(Text, nullable=True)
    inserito_il = Column(DateTime, default=datetime.utcnow)
    inserito_da = Column(String, nullable=True)
    aggiornato_il = Column(DateTime, default=datetime.utcnow)


class CostoMaterialeCliente(Base):
    """Costo materiali ATTRIBUITO a un cliente (o non attribuito se cliente NULL).

    Sono acquisti attribuiti, non consumi misurati: va dichiarato nell'interfaccia.
    Gli acquisti generici NON vanno ripartiti artificialmente: restano con
    cliente NULL e vengono mostrati separatamente.
    """
    __tablename__ = 'costi_materiali'
    id = Column(String, primary_key=True)
    cliente = Column(String, nullable=True, index=True)   # NULL = non attribuito/comune
    anno = Column(Integer, nullable=False)
    mese = Column(Integer, nullable=False)
    importo = Column(Float, nullable=False, default=0.0)
    descrizione = Column(String, nullable=True)
    riferimento = Column(String, nullable=True)
    inserito_il = Column(DateTime, default=datetime.utcnow)
    inserito_da = Column(String, nullable=True)
