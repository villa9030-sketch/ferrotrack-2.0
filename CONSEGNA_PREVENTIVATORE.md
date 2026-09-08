# Consegna — Preventivatore (sezione 10)

Branch `stefano/refactor-ore-officina` · **non pushato**

> Completa il pacchetto di `CONSEGNA_ORE_OFFICINA.md`, che copre ore, ordini e
> tablet. Qui c'è solo il preventivatore.

---

## 1. Il percorso, prima e dopo

| | Prima | Adesso |
|---|---|---|
| **Prezzo** | Calcolato nel browser. Il server salvava il numero ricevuto senza verificarlo. Due formule diverse nel JavaScript. | Un solo calcolo, nel backend, sui dati salvati. Il browser fa l'anteprima, non decide. |
| **Editor vs accettazione** | Su un preventivo con assiemi il prezzo accettato era **più basso** di quello mostrato al cliente. | Stesso numero in editor, PDF, invio e accettazione. |
| **Accettazione** | Preventivo marcato ACCETTATO **prima** di creare l'ordine: se falliva restava una commessa senza ordine. | Prima l'ordine, poi lo stato. Se fallisce, l'ordine viene annullato e il preventivo resta riprovabile. |
| **Doppio clic** | Nessuna difesa. | Idempotente: restituisce l'ordine già creato. |
| **Prezzo sull'ordine** | Non trasferito: commesse senza valore. | Il totale concordato arriva su `prezzo_quotato`. |
| **Import XLSX** | Quantità letta e poi buttata: una riga da 50 pezzi entrava come 1. | Quantità conservate, duplicati sommati. |
| **Costo d'apporto** | Scritto in un campo che nessuno leggeva: filo e gas persi da ogni totale. | Nome unificato, entra nel prezzo. |
| **Salvataggio** | Timer legato allo stato globale: poteva scrivere sul documento sbagliato. Errori solo in console. | Destinatario fissato alla modifica, stato visibile, Riprova, dati conservati. |
| **Messaggi dal CAD** | Nessun controllo sull'origine; articolo indirizzato per posizione. | Origine validata, indirizzamento per ID, messaggi di altri preventivi rifiutati. |
| **Disegni omonimi** | Il secondo cancellava il primo. | Rinominati: `flangia (2).dxf`. |
| **PDF di un'offerta inviata** | Ricalcolato con la configurazione corrente: cambiava da solo. | Congelato all'invio. |
| **Prima di inviare** | Nessun controllo: i buchi si scoprivano dal cliente. | Verifica del server con il punto esatto da sistemare. |
| **File temporanei** | Cancellati anche se il trasferimento all'ordine falliva: disegno perso. | Conservati finché tutto è arrivato. |

---

## 2. Le regole economiche, scritte

Verificate e messe in un posto solo (`backend/preventivi/calcolo.py`):

```
costo pezzo    = per ogni articolo SCIOLTO: (base + lavorazioni) × qtà
                 base = override manuale, altrimenti stima, altrimenti materiale
                 lavorazioni = piega + saldatura + filettatura + svasatura
                             + apporto + pulizia
costo assiemi  = per ogni assieme: (proprio + componenti + tubolari e piastre
                 collegati) × qtà assieme
costo sciolti  = tubolari (materiale + taglio) e piastre non collegati

prezzo         = costo × (1 + generali%) × (1 + ricarico%)
totale lotto   = prezzo_pezzo × quantità + assiemi + tubolari + piastre
                 poi eventuale sconto%
```

**Due punti da sapere:**

1. **`margine` è un ricarico sul costo**, non un margine sul prezzo di vendita.
   Costo × 1,20 per il 20%. Il nome trae in inganno; la regola non è stata
   cambiata, solo scritta.
2. **Gli articoli dentro un assieme non si contano due volte.** Era il difetto
   dell'accettazione: sommava tutti gli articoli e ignorava l'assieme che li
   conteneva.

**Guardia**: se il calcolo dà zero ma il preventivo ha già un prezzo salvato,
quello **non** viene azzerato. Uno zero calcolato non è un prezzo confermato.

---

## 3. Due cose che cambiano i tuoi numeri

Vanno dette chiaro perché **fanno salire i prezzi**:

1. **Il costo del materiale d'apporto** (filo + gas) ora entra davvero nei
   totali. Prima veniva calcolato e perso.
2. **I preventivi con assiemi, tubolari o piastre** si accettano al prezzo
   giusto. Prima l'accettazione ne perdeva una parte.

Non sono cambi di regola commerciale: sono le regole che finalmente vengono
applicate. Se vuoi confrontare prima/dopo su un preventivo reale prima di
usarlo col cliente, si fa.

---

## 4. Coerenza fra editor, PDF e accettazione

| Test | Esito |
|---|---|
| Stesso totale in editor, PDF e accettazione | ✅ `test_calcolo_autorevole` (7) |
| Le righe del PDF sommano al totale | ✅ idem |
| Costo d'apporto uguale dopo salvataggio e riapertura | ✅ `test_costo_apporto` (3) |
| Il totale salvato è quello del server, non del browser | ✅ `test_calcolo_autorevole` (5) |
| Il prezzo sull'ordine è quello autorevole | ✅ idem (6) |
| Un'offerta inviata non cambia se cambia la configurazione | ✅ `test_preventivi_robustezza` (4) |
| L'accettazione rispetta il prezzo comunicato | ✅ idem (5) |

Confronto esplicito nel test: sul caso di prova la vecchia formula
dell'accettazione dava **230 €** dove quella corretta dà **965 €**.

---

## 5. I 10 criteri di accettazione (10.11)

| | Criterio | Dove |
|---|---|---|
| 1 | Creazione manuale → modifica → salvataggio, senza chiudere e riaprire | `test_preventivi_verifica` (4) |
| 2 | XLSX con quantità multiple e righe duplicate | `test_xlsx_quantita` |
| 3 | Stesso totale in editor, PDF, invio e accettazione | `test_calcolo_autorevole` |
| 4 | Campi economici uguali dopo salvataggio e riapertura | `test_costo_apporto` |
| 5 | Errore di rete: modifiche conservate e messaggio visibile | `test_preventivi_browser` |
| 6 | Cambio rapido di preventivo: niente sul documento sbagliato | `test_preventivi_browser` |
| 7 | Risposte fuori ordine: nessuna sovrascrittura silenziosa | `test_preventivi_browser` |
| 8 | Messaggio CAD obsoleto o non autorizzato: ignorato | `test_preventivi_browser` |
| 9 | File omonimi: nessuna sostituzione silenziosa | `test_preventivi_robustezza` |
| 10 | Errore di accettazione: nessuno stato parziale, nessun ordine doppio | `test_preventivi_accettazione` |

**Tutti e 10 coperti.**

---

## 6. Prestazioni: misurate, nessuna modifica

La specifica chiede di misurare prima di ottimizzare. Fatto, su dati reali:

| | |
|---|---|
| Apertura preventivo (23 articoli) | 67 ms |
| Ridisegno a 100 articoli | 40 ms |
| Ricalcolo totali a 100 articoli | 0,04 ms |
| Ridisegno a 200 articoli | 55 ms |
| Chiamate API duplicate all'apertura | 0 su 6 |
| Focus durante ridisegni e salvataggi | mantenuto |

Nessun problema, nemmeno al target di 100 DXF. **Non ho toccato niente**:
sarebbe stato lavoro inventato.

---

## 7. Accessibilità: misurata

Audit su 1500px e 390px:

- **0** pulsanti o link senza nome accessibile
- **0** campi senza etichetta
- **nessuno scorrimento orizzontale di pagina** a nessuna delle due larghezze
- focus da tastiera **ora visibile** (era cancellato da una regola CSS)
- gli stati della verifica si distinguono anche **senza colore** (✖ / ⚠ / ✓ più
  la parola)

**Resta**: due controlli sotto i 28px di altezza (il pulsante "Storico" e la
casella "Solo da rivedere"). Segnalati, non critici su desktop.

---

## 8. File

**Nuovi**

| File | Responsabilità |
|---|---|
| `backend/preventivi/calcolo.py` | Il calcolo autorevole del prezzo |
| `backend/preventivi/validazione.py` | Rifiuta l'impossibile, lascia salvare le bozze |
| `backend/preventivi/verifica.py` | È pronto per l'invio? Cosa manca e dove |

**Modificati**

| File | Cosa |
|---|---|
| `backend/database.py` | Accettazione atomica e idempotente, prezzo trasferito, congelamento all'invio, validazione nei `replace_*` |
| `backend/app.py` | Endpoint `totali` e `verifica`, gate su invio e accettazione, nomi file anti-collisione, pulizia condizionata |
| `backend/preventivi/xlsx_importer.py` | Quantità conservate e duplicati sommati |
| `backend/preventivi/cost_calculator.py` | Legge il nome canonico del costo d'apporto |
| `frontend/preventivi.html` | Salvataggio affidabile, pannello di verifica, ricerca, totale in vista, messaggi CAD validati, focus |
| `frontend/dxf-editor.html` | Rimanda gli identificativi stabili, parla solo con la propria origine |
| `models.py` / `migrations_ore.py` | Colonna additiva `snapshot_economico` |

**Endpoint nuovi**
- `GET /api/preventivi/<id>/totali` — totali autorevoli con la composizione
- `GET /api/preventivi/<id>/verifica` — errori, avvisi e riepilogo

---

## 9. Limiti rimasti

**Importazioni**
- Nell'XLSX, area, peso e perimetro sono presi **come stanno nel file**. Il
  costo è chiaramente un totale di riga (viene diviso per la quantità), ma per
  gli altri campi non è verificato: se una riga da 50 pezzi indica l'area di
  **un** pezzo o di tutti e 50 non lo so, e non l'ho indovinato. Da chiarire su
  un file reale.
- Il batch DXF rinomina i file omonimi, ma l'abbinamento all'assieme resta
  basato sul **nome originale**: due file omonimi in cartelle-assieme diverse
  finiscono sullo stesso assieme.

**Visualizzatori CAD**
- L'origine dei messaggi è validata e l'articolo indirizzato per ID, ma le
  finestre CAD **già aperte** prima di questo aggiornamento continuano a
  mandare il vecchio formato: il ripiego sull'indice resta per compatibilità.
  Si esaurisce chiudendo e riaprendo il CAD.
- Il visore 3D degli assiemi non è stato toccato.

**Calcolo**
- `cost_calculator.calcola_preventivo` resta nel codice ma **non è collegato**:
  il calcolo autorevole è `calcolo.py`. Non l'ho rimosso per non toccare quello
  che non serviva toccare, ma è codice morto.
- Il JavaScript continua a calcolare l'anteprima con la propria formula. È
  allineata a quella del server e c'è il confronto automatico all'accettazione,
  ma restano due implementazioni: la seconda va tenuta d'occhio.

---

## 10. Ripristino

```bash
# tutto il refactoring, ore comprese
git checkout db2220d

# solo il preventivatore, tenendo ore e ordini
git revert 113c77d 0486031 1202a39 07f2604 8095559 bc15926
```

Il database resta compatibile: `snapshot_economico` è una colonna additiva che
il codice vecchio ignora. Nessun dato preesistente è stato modificato, e i
backup automatici stanno in `app/database/backups/`.
