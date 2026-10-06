# Contratto JSON modello Preventivo

Definizione del payload condiviso tra backend Flask, frontend `preventivi.html`,
e (in futuro) eventuali subscriber di `OrderEventBus`.

## Preventivo (oggetto principale)

```json
{
  "id": "uuid-string",
  "cliente": "Acme S.r.l.",
  "numero_ordine_cliente": "RFQ-2026-42",   // opzionale, se cliente fornisce un proprio numero
  "quantita": 10,
  "margine_pct": 25.0,
  "data_consegna_proposta": "2026-08-15",   // ISO date, può essere null in BOZZA
  "status": "BOZZA",                        // BOZZA | INVIATO | ACCETTATO | RIFIUTATO
  "versione": 1,
  "parent_preventivo_id": null,             // se snapshot di una versione precedente
  "totale_pezzo": 145.30,
  "totale_pezzo_con_margine": 181.63,
  "totale_pezzo_scontato": 172.55,
  "totale_lotto": 1725.50,
  "costi_montaggio_totale": 0.0,
  "costi_tubolari_totale": 0.0,
  "costi_piastre_totale": 0.0,
  "sconto_pct": 5.0,
  "created_by": "user-id-uuid",
  "data_creazione": "2026-06-30T14:30:00Z",
  "note": "Consegna preferibile entro Ferragosto",
  "articoli": [ /* vedi sotto */ ],
  "assiemi": [ /* opzionale */ ],
  "tubolari": [ /* opzionale */ ],
  "piastre": [ /* opzionale */ ]
}
```

## Articolo (figlio di preventivo, lista in `articoli`)

```json
{
  "id": "uuid-string",
  "codice": "20PA00693-00",
  "quantita": 2,
  "codice_assieme": "ASS-001",      // opzionale, se appartiene a un assieme

  // --- Geometria estratta da dxf_scanner ---
  "area_dm2": 4.5,
  "perimetro_taglio_m": 1.8,
  "n_forature": 6,
  "spessore_mm": 2.0,               // inserito dal commerciale (dropdown/input)
  "materiale": "S235",              // S235 | INOX_304 | INOX_316 | ALU_5754 | ...

  // --- Costo base materiale + taglio (da stimatore o XLSX Lantek) ---
  "costo_base_stimato": 12.45,      // calcolato da laser_cost_estimator
  "costo_base_override": null,      // se commerciale sovrascrive a mano, valore qui

  // --- Costi lavorazione (estratti dal DXF + applicati i coefficienti) ---
  "pieghe": 3,
  "saldatura_ml": 0.0,
  "filettatura_pz": 0,
  "svasatura_pz": 0,
  "costo_piega": 3.00,
  "costo_saldatura": 0.0,
  "costo_filettatura": 0.0,
  "costo_svasatura": 0.0,
  "costo_apporto": 0.0,
  "costo_pulizia": 0.0
}
```

## Stati e transizioni

```
[creazione]──► BOZZA ──invia──► INVIATO ──accetta──► ACCETTATO ──┐
                  ▲                │                              │
                  │                rifiuta                       crea Order FerroTrack
                  │                │                              │
                  │                ▼                              ▼
                  └──── modifica  RIFIUTATO                Order(origine='PREVENTIVO',
                       (crea v2)                                preventivo_id_origine=<id>)
```

- BOZZA → INVIATO: crea **snapshot immutabile** del preventivo. Modifiche successive partono da BOZZA v2 (`parent_preventivo_id`=snapshot v1).
- INVIATO → ACCETTATO: commerciale clicca "Manda in produzione". Si crea l'ordine FerroTrack atomicamente (transazione DB) + notifica capi. Per l'officina si stampa con "Stampa ordine" (`GET /api/orders/<id>/stampa`).
- INVIATO → RIFIUTATO: status finale, preventivo archiviato. Non crea ordine.

## Payload di "Accetta preventivo" (input)

```json
POST /api/preventivi/<id>/accetta
{
  "admin_id": "user-id",
  "data_consegna": "2026-08-15",   // conferma/sovrascrive data_consegna_proposta
  "note_aggiuntive": "Spedizione franco fabbrica"   // opzionale, finiscono in Order.note
}
```

Output:
```json
{
  "success": true,
  "preventivo_id": "...",
  "order_id": "...",
  "numero_ordine": "PREV-2026-0042"
}
```

## Coefficienti laser config (admin)

```json
GET /api/admin/laser-config

{
  "materiali": {
    "S235":     { "densita_kg_dm3": 7.85, "euro_kg": 1.20 },
    "INOX_304": { "densita_kg_dm3": 8.00, "euro_kg": 4.50 },
    "ALU_5754": { "densita_kg_dm3": 2.70, "euro_kg": 3.80 }
  },
  "velocita_taglio_m_h": {
    "S235":     { "1": 6000, "2": 3500, "3": 2500, "5": 1500, "10": 800 },
    "INOX_304": { "1": 4500, "2": 2800, "3": 2000, "5": 1100 },
    "ALU_5754": { "1": 8000, "2": 5000, "3": 3500 }
  },
  "euro_h_macchina": 85.0,
  "tempo_perforazione_sec": 0.5
}
```

Formula stimatore (`laser_cost_estimator.stima_base(articolo, config)`):
```
peso_kg = area_dm2 * (spessore_mm / 100) * materiale.densita_kg_dm3
costo_materiale = peso_kg * materiale.euro_kg
ore_taglio = (perimetro_taglio_m / velocita_taglio_m_h[materiale][spessore]) + (n_forature * tempo_perforazione_sec / 3600)
costo_taglio = ore_taglio * euro_h_macchina
base = costo_materiale + costo_taglio
```

## Note implementative

- Numero ordine FerroTrack auto-generato: `PREV-{anno}-{progressivo}` (es. `PREV-2026-0042`).
- `costo_base_override` ha priorità su `costo_base_stimato` se non null. Se commerciale lascia null, vale il valore stimato.
- Snapshot versioning: la duplicazione preventivo + figli è una transazione SQL singola (tutto o nulla).
- Soft delete: `is_deleted=True` invece di DELETE — coerente col pattern già usato in `Order`.
