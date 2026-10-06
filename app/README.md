# FerroTrack — Schedulatore Laser

Sistema web per la gestione degli ordini di carpenteria metallica. Le ore le dichiarano gli operai dal tablet della timbratrice (una riga per cliente); in officina l'ordine si riconosce dal PDF d'ordine stampato. Pistole barcode e cartellini sono stati tolti (ottobre 2026).

## Avvio rapido

```bash
cd app
pip install -r requirements.txt
python run.py
```

Server su `http://localhost:5000`. Database SQLite e cartelle upload create al primo avvio. Per accesso da LAN: `http://<server-ip>:5000`.

Su Windows: usa `START_BACKEND.bat`. Per scoprire l'IP del server in rete: `FIND_IP.bat`.

## Struttura

```
app/
├── backend/                       Flask API + business logic
│   ├── app.py                     route HTTP, endpoint REST
│   ├── database.py                ORM SQLAlchemy + Manager (Order, User, Config, Preventivo, ...)
│   ├── models.py                  modelli DB (Order, Preventivo*, ...; OfficinaScan/Pistola solo storico)
│   ├── events.py                  OrderEventBus (hook gestionale esterno futuro)
│   ├── foglio_ordine.py           foglio d'ordine A4 da stampare (ordini senza PDF del cliente)
│   └── preventivi/                porting servizi Preventivatore desktop
│       ├── xlsx_importer.py       parsing Lantek XLSX
│       ├── dxf_scanner.py         analisi DXF: pieghe/saldature + area/perimetro
│       ├── laser_cost_estimator.py stima costo taglio laser da geometria + materiale
│       ├── cost_calculator.py     calcolo costi/totali con margine/sconto
│       ├── pdf_exporter.py        generazione PDF preventivo
│       ├── step_*.py              analisi STEP 3D (assiemi, tubolari, piastre)
│       └── contract.md            contratto JSON modello preventivo
│
├── frontend/                      pagine HTML (vanilla, no framework)
│   ├── login.html                 login + routing per ruolo
│   ├── impiegata.html             Elena: carico ordini, fatturazione, sospetti finiti
│   ├── capo-officina.html         Stefano/Paolo: KPI operai, ordini attivi, calendario
│   ├── laser.html                 Mirko: calendario laser + visualizzazione PDF
│   ├── preventivi.html            Commerciale: preventivi (import XLSX/DXF, calcolo, accetta)
│   ├── admin.html                 utenti, soglie sistema, backup, audit log
│   ├── operaio-info.html          tablet d'officina: ordini, pezzi, disegni, foglio d'ordine
│   └── archivio.html              storico ordini chiusi (read-only)
│
├── migrations/                    Alembic — versioning schema DB
│   └── versions/                  rev: 34f2aa86f9e3 baseline → 72e7509bff4a origine su orders
│
├── uploads/{pdfs,drawings}/       file caricati dagli utenti (gitignored)
├── database/scheduler.db          SQLite (gitignored)
├── app_config.json                soglie sistema + laser_config + preventivi_config
├── backup_config.json             impostazioni backup automatico
├── alembic.ini                    config Alembic
├── run.py                         entry point Flask + thread (backup, export, vigilanza)
└── backup_db.py                   backup automatico DB con scheduler
```

## Flusso operativo

FerroTrack ha **due punti di ingresso** ordini, indipendenti e paralleli:

**A) Elena carica PDF** (flusso storico):

1. **Elena** carica un PDF di ordine → sistema crea l'ordine; "Stampa ordine" stampa il PDF per il faldone dell'officina
2. **Mirko** vede l'ordine in `laser.html`, marca "taglio completato" quando finito
3. **Operai officina** dichiarano le ore dal tablet della timbratrice (per cliente)
4. **Capo officina** vede KPI ore/operaio e ordini in lavorazione in `capo-officina.html`
5. Capo o Elena chiudono l'ordine → status `DA_FATTURARE` → Elena emette DDT/fattura

**B) Commerciale accetta preventivo** (flusso nuovo, modulo Preventivatore unificato):

1. **Commerciale** apre `preventivi.html` → "+ Nuovo preventivo" con cliente/quantità/margine
2. Importa DXF cliente → `dxf_scanner` estrae lavorazioni + area + perimetro; opzionalmente importa XLSX Lantek per costi materiale precisi
3. Per ogni articolo imposta materiale + spessore → click "Stima costo laser" usa `laser_cost_estimator` per generare il costo base (lamiera + taglio); l'utente può sovrascrivere con costo_base_override
4. Sistema mostra totali (pezzo, con margine, lotto) live
5. "Invia al cliente" → status BOZZA → INVIATO (preventivo diventa immutabile)
6. Cliente accetta esternamente → commerciale clicca "Accetta e manda in produzione" → modal preview → conferma
7. **Backend atomico**: status diventa ACCETTATO + crea Order FerroTrack con `origine='PREVENTIVO'` + numero auto `PREV-{anno}-{NNNN}` + notifica capi; "Stampa ordine" stampa il foglio d'ordine A4 (non c'e' un PDF del cliente)
8. Da qui il flusso prosegue identico al punto A (Mirko taglia, operai dichiarano le ore, capo chiude, Elena fattura). Elena distingue gli ordini da preventivo dal badge `PREV` nella sua lista

### Ridondanza "ordini sospetti finiti"

Se un ordine e' aperto da N giorni (default 10) e nessuno ne ha registrato il completamento, compare in una sezione rossa nel pannello capo + Elena. Soglia configurabile da `admin.html` → tab Soglie.

## Database

9 tabelle live:
- `orders` — ordini con cliente, numero, data consegna, status, **origine** (`PDF`|`PREVENTIVO`), **preventivo_id_origine**
- `users` — operai/capi/impiegate/admin/**commerciali** con ruoli e permessi
- `officina_scans`, `pistole` — storico delle pistole barcode (tolte): restano nel DB, l'app non le usa piu'
- `preventivi` — preventivi cliente con cliente, qty, margine, status (`BOZZA|INVIATO|ACCETTATO|RIFIUTATO`), totali
- `preventivo_articoli` — articoli del preventivo: codice, qty, materiale, spessore, area, perimetro, costi
- `preventivo_assiemi` — assiemi 3D (STEP) con ore montaggio/saldatura
- `preventivo_tubolari` — tubolari (profili) con lunghezza, peso, costi
- `preventivo_piastre` — piastre con spessore, area, peso, costo

Schema versionato con **Alembic** (`migrations/versions/`). Per modifiche:
`python -m alembic revision -m "..." --autogenerate` poi `python -m alembic upgrade head`.

Tabelle legacy (`processing_steps`, `phase_sessions`, `phase_delegations`, `support_requests`, `operator_clients`) restano in DB ma non vengono più scritte. Servono solo per consultare lo storico vecchio in `archivio.html`.

## API principali

| Metodo | Path | Scopo |
|---|---|---|
| GET/POST | `/api/orders` | Lista / crea ordini |
| POST | `/api/orders/<id>/close` | Chiudi ordine (status DA_FATTURARE) |
| POST | `/api/orders/<id>/mark-laser-done` | Marca taglio completato (Mirko) |
| GET | `/api/orders/<id>/stampa` | Ordine da stampare: PDF del cliente o foglio d'ordine A4 |
| GET | `/api/orders/sospetti-finiti` | Ordini probabilmente finiti |
| GET | `/api/capo/kpi-operai` | Ore dichiarate per operaio |
| GET/PUT | `/api/admin/config` | Soglie sistema |
| GET/POST | `/api/users` | CRUD utenti (riservato capi) |
| CRUD | `/api/preventivi` | CRUD preventivi (Commerciale/Admin/Capi) |
| POST | `/api/preventivi/<id>/import-xlsx` | Upload XLSX Lantek → estrae articoli |
| POST | `/api/preventivi/<id>/import-dxf` | Upload DXF → lavorazioni + area + perimetro |
| POST | `/api/preventivi/<id>/articoli/<a>/stima-base` | Calcola costo laser stimato |
| POST | `/api/preventivi/<id>/invia` | BOZZA → INVIATO (immutabile) |
| POST | `/api/preventivi/<id>/accetta` | INVIATO → ACCETTATO + crea Order FerroTrack |
| POST | `/api/preventivi/<id>/rifiuta` | INVIATO → RIFIUTATO |
| GET/PUT | `/api/admin/laser-config` | Coefficienti stimatore laser (€/kg, velocità taglio, €/h) |
| GET | `/api/admin/export-orders?format=csv\|json` | Export ordini per gestionale esterno |

## Tech stack

- **Backend**: Python 3.10+, Flask, SQLAlchemy (SQLite WAL mode)
- **Frontend**: HTML5 + CSS3 + JS vanilla (Geist + Inter fonts, zero framework)
- **PDF**: PyPDF2, pdfplumber (parsing ordini), reportlab (PDF preventivi)

## Sviluppo

- Sintassi: `python -m py_compile backend/*.py`
- Health check: `curl http://localhost:5000/api/health`
- Backup manuale DB: bottone in `admin.html` → tab Backup, oppure `POST /api/admin/backup`
- Audit log: `admin.html` → tab Audit log (azioni amministrative tracciate)
