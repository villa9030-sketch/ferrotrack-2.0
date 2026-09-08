# Consegna — Raccolta ore, controllo mancanze, ordini d'ufficio

Branch `stefano/refactor-ore-officina` · 12 commit da `db2220d` a `80078e6` · **non pushato**

> **Il preventivatore (sezione 10 della specifica) non è compreso**: è stato
> rinviato per ultimo su tua decisione. Quanto segue riguarda le sezioni 2–9 e
> 11–13.

---

## 1. Il nuovo flusso in breve

**Prima.** L'operaio scansionava il cartellino con la pistola WiFi a ogni
cambio ordine. I tempi venivano ricostruiti dalle scansioni, che nessuno
faceva in modo affidabile. L'avanzamento passava per cinque fasi produttive
che nessuno aggiornava.

**Adesso.** All'operaio si chiede una cosa sola, una volta al giorno:

> *Chi sei, per quale cliente hai lavorato, quante ore.*

```
OPERAIO                      UFFICIO                        DIREZIONE
tablet timbratrice           Elena                          Marco
─────────────────────        ──────────────────────         ─────────────────
tocca il proprio nome        vede le giornate mancanti      riepilogo mensile
ore per cliente              corregge e registra assenze    per cliente:
salva (obiettivo 8h)         registra i passaggi ordine     fatturato, materiali,
                                                            ore e costo
        │                              │                            │
        └──────────────► ore dichiarate ──────────────────────────► │
                                       │
                         ordini: aperto → pronto DDT →
                         consegnato → archivio
```

Le pistole barcode sono **dismesse**: erano un passaggio in più che non
alimentava più nulla di essenziale.

---

## 2. I tre tablet

| Tablet | Pagina | Cosa fa | Come si abilita |
|---|---|---|---|
| Timbratrice (1) | `/ore.html` | Bacheca della giornata: chi ha registrato, quante ore, per quali clienti. Si tocca il proprio nome per inserire o correggere. | Admin → Soglie sistema → **Tablet di officina**, tipo *Registrazione ore* |
| Reparto (2) | `/operaio-info.html` | Solo ordini in corso, in sola lettura. Nessuna azione. | Stessa schermata, tipo *Sola consultazione ordini* |
| — | `/ufficio-ore.html` | Controllo mancanze, riepilogo economico, configurazione | Token d'ufficio da riga di comando |

Nessuno dei tre chiede un login: si abilitano una volta sola con un token di
dispositivo e restano su quella pagina.

---

## 3. File e responsabilità

### Nuovi — la logica sta fuori dai file monolitici

| File | Responsabilità |
|---|---|
| `backend/models_ore.py` | 10 tabelle nuove (dichiarazioni, anomalie, tariffe, fatturato, materiali, dispositivi, clienti) |
| `backend/migrations_ore.py` | Migrazione additiva idempotente: colonne su `orders`, tabelle nuove, semina clienti e ore attese |
| `backend/auth_device.py` | Identità di dispositivo verificata dal server (token sha256, scope `ore`/`reparto`/`ufficio`) |
| `backend/ore_service.py` | Dichiarazioni ore: lettura, salvataggio atomico, idempotenza, concorrenza |
| `backend/anomalie_service.py` | Ore attese, eccezioni, rilevazione mancanze, notifiche non duplicate |
| `backend/riepilogo_service.py` | Riepilogo economico per cliente e periodo |
| `backend/ordini_service.py` | Ciclo amministrativo dell'ordine: le quattro fasi e le sei transizioni |
| `backend/api_ore.py` | 19 endpoint `/api/ore/*` (blueprint, una riga di innesto in `app.py`) |
| `frontend/ore.html` | Tablet della timbratrice |
| `frontend/ufficio-ore.html` | Schermata d'ufficio: da controllare, riepilogo, configurazione |
| `tools/device_token.py` | Creazione/revoca token da riga di comando |
| `tools/verifica_migrazione.py` | Prova la migrazione su una copia prima di attivarla |

### Modificati

| File | Cosa cambia |
|---|---|
| `backend/app.py` | Innesto blueprint; 9 endpoint `/api/ordini/*`; gestione tablet; init database disattivabile; `update_order` non risponde più "success" a vuoto |
| `backend/database.py` | Pistole disattivabili; KPI operai dalle dichiarazioni; "ordini fermi" senza scansioni; `fase` nel dizionario ordine |
| `backend/models.py` | 8 colonne additive su `Order` (completamento, DDT, consegna, residuo) |
| `frontend/impiegata.html` | Le quattro viste ordini; via la barra di avanzamento per fasi; finestre DDT e consegna |
| `frontend/operaio-info.html` | Bacheca ordini di reparto in sola lettura, senza login |
| `frontend/admin.html` | Abilitazione tablet con QR; interruttore pistole; soglia ordini fermi |
| `frontend/capo-officina.html` | Via tab Pistole e chiusura turno; nota sulla fonte delle ore |
| `run.py` | Il thread di vigilanza controlla anche le ore mancanti |
| `scan_hub/scan_hub.py` | Marcato dismesso, si ferma da solo al primo 410 |

**33 file, +7.932 righe.**

---

## 4. Migrazione e attivazione

La migrazione è **solo additiva**: nessuna tabella, colonna o riga viene
rimossa o riscritta.

```bash
# 1. Prova su una copia, senza toccare il database di lavoro
python app/tools/verifica_migrazione.py

# 2. Avvia normalmente: la migrazione parte da sola all'avvio
cd app && python run.py

# 3. Abilita il tablet della timbratrice
#    Admin → Soglie sistema → Tablet di officina → Abilita tablet
#    Inquadra il QR col tablet e aggiungi alla schermata Home

# 4. Ferma il vecchio ponte delle pistole sul PC di Elena
nssm stop FerroTrackScanHub
nssm remove FerroTrackScanHub confirm
```

**Verificato** partendo da un backup di agosto precedente al refactoring:
19 → 29 tabelle, 8 colonne aggiunte, 39 clienti e 2 configurazioni ore create,
28 ordini / 50 preventivi / 4 scansioni **tutti intatti**, migrazione
ripetibile senza effetti.

Ore attese predefinite: **8h, lunedì–venerdì**, per Operaio Laser e Operaio
Officina. Si cambiano per singola persona da *Ufficio ore → Configurazione*.

---

## 5. Lo storico

Niente è stato cancellato.

| Cosa | Dove sta ora |
|---|---|
| Scansioni con la pistola | Tabella `officina_scans`, intatta e consultabile dal dettaglio ordine |
| Tempi per ordine ricavati dalle scansioni | Restano leggibili; **non** entrano nel riepilogo economico (niente doppio conteggio) |
| Ordini già in `DA_FATTURARE` | Compaiono in *Pronti per DDT*, con la riga "già finito prima di questo sistema" |
| Stato `status` dell'ordine | Significato invariato; le nuove date sono colonne a parte |
| Pistole registrate | Tabella `pistole`, intatta |

Riattivando l'interruttore in *Admin → Rilevazione delle ore* tutto il vecchio
sistema torna a funzionare com'era.

---

## 6. Istruzioni brevi

### Per l'operaio (tablet alla timbratrice)

1. Tocca il tuo nome.
2. Tocca il cliente per cui hai lavorato, poi **+** per aggiungere mezz'ore.
3. Se hai lavorato per più clienti, aggiungine un altro.
4. Per pulizie, manutenzione o lavori interni usa **Attività interne**.
5. Guarda in basso: *Obiettivo 8 h*. Quando torna, premi **Salva**.
6. Se hai fatto meno di 8 ore va bene: premi Salva e conferma.

Nient'altro. Niente pistola, niente timer.

### Per l'impiegata

**Ufficio ore → Da controllare** — chi non ha registrato o non arriva alle ore
dovute. Correggi la giornata, oppure registra un'assenza o una giornata ridotta
se è giusto così. Le righe segnate *"confermata dall'operaio"* sono mezze
giornate volute, non dimenticanze.

**Ufficio ore → Riepilogo economico** — per ogni cliente: fatturato, materiali,
ore, costo delle ore, residuo. Fatturato e materiali li inserisci tu; le ore
arrivano dalle dichiarazioni. Un asterisco arancione segnala un residuo
calcolato senza tutti i materiali.

**Impiegata → Ordini** — quattro viste. Quando l'officina ti dice che ha finito:
*Lavorazione finita → prepara DDT*. Poi *Registra DDT*, poi *Registra consegna*
(completa o parziale), infine *Chiudi pratica*. Se sbagli, ogni passaggio si
annulla finché non ne hai fatto uno successivo.

Le notifiche delle giornate mancanti arrivano nella tua campanella, **il giorno
dopo** (la giornata in corso non viene segnalata).

---

## 7. Test

**314 verdi**, tutti su database temporanei. Nessun invio esterno, nessun
intervento sul database di lavoro.

| File | Verdi | Copre |
|---|---|---|
| `test_ore_service.py` | 38 | Salvataggio, idempotenza, validazione, concorrenza |
| `test_api_ore.py` | 29 | Permessi verificati dal server, doppio invio, input invalidi |
| `test_anomalie_service.py` | 35 | Mancanze, eccezioni, recupero arretrati, notifiche uniche |
| `test_riepilogo_service.py` | 37 | Riconciliazione, cambio tariffa, zero vs dato assente |
| `test_pistole_dismesse.py` | 27 | Scansione rifiutata, storico intatto, KPI dalle dichiarazioni |
| `test_ore_attese_tablet.py` | 17 | Obiettivo 8h, conferma dello scostamento |
| `test_dispositivi_admin.py` | 27 | Abilitazione tablet, scope, revoca immediata |
| `test_bacheca_tablet.py` | 34 | Bacheca, zero vs non registrato, reparto senza accesso alle ore |
| `test_ordini_ufficio.py` | 47 | Quattro viste, permessi, sequenza, consegne parziali |
| `test_criteri_accettazione.py` | 23 | I 20 criteri della specifica, end-to-end |

Provati in browser: tablet ore (bacheca, inserimento, conferma, 390px), tablet
di reparto, abilitazione col QR, le quattro viste ordini con il flusso completo
fino alla consegna parziale.

**Un problema di sicurezza trovato dai test e corretto**: le transizioni degli
ordini si fidavano dello `user_id` inviato dal browser, quindi un tablet di
reparto che scriveva `user_id: elena-impiegata` riusciva a chiudere ordini. Ora
comanda lo scope del dispositivo.

---

## 8. Limiti e decisioni aperte

**Limiti noti**

1. **Identità di dispositivo, non di persona.** Il tablet è condiviso: il nome
   che l'operaio tocca è una dichiarazione, non un'autenticazione. Chi ha in
   mano il tablet può registrare ore a nome di un altro. Per distinguerlo
   servirebbe un PIN o un badge per persona.
2. **I PC dell'ufficio si fidano ancora del login del browser.** Il resto
   dell'applicazione (34 endpoint) non ha autenticazione vera. Le funzioni
   nuove sono protette; le vecchie no.
3. **Le notifiche arrivano il giorno dopo.** La giornata in corso non viene
   segnalata per non produrre falsi allarmi alle 9 del mattino.
4. **Nessun residuo per articolo.** Una consegna parziale dice *che* manca
   qualcosa, con una nota; non *quali pezzi*.
5. **Fatturato e materiali si inseriscono a mano.** Il sistema non li legge da
   nessuna parte.

**Decisioni che aspettano te**

- **Residuo per articolo**: serve sapere quali pezzi mancano? La struttura a
  lotti esiste ma non è mai stata usata (zero lotti nel database).
- **Sovrapposizione linguette**: in alto nella pagina di Elena restano
  *"Ordini da Fatturare"* e *"Archivio"*, che ora mostrano le stesse cose delle
  nuove viste con criteri diversi. Le ho lasciate perché contengono filtri ed
  export Excel. Da unificare o da tenere come strumenti di dettaglio?
- **Controllo serale**: vuoi che Elena sappia la sera stessa chi non ha
  registrato, invece del giorno dopo?
- **Ore attese diverse per persona**: oggi 8h lun–ven per tutti. Se qualcuno è
  part-time va impostato.

---

## 9. Ripristino

Tutto reversibile, in ordine di gravità.

**Tornare alle pistole senza toccare il codice**
Admin → Soglie sistema → *Rilevazione delle ore* → spunta *Riattiva la
rilevazione con pistola barcode*. Riavvia `scan_hub` sul PC di Elena. Le
dichiarazioni già inserite restano.

**Disabilitare un tablet**
Admin → Soglie sistema → Tablet di officina → *Revoca*. Da quel momento non
salva più nulla.

**Tornare al codice precedente**
```bash
git checkout db2220d          # ultimo commit prima del refactoring
```
Il database resta compatibile: le tabelle e le colonne nuove vengono ignorate
dal codice vecchio, che non le conosce. Nessun dato preesistente è stato
modificato.

**Ripristinare il database**
```bash
# I backup automatici stanno in app/database/backups/
copy app\database\backups\scheduler_pre_migration_<data>.db app\database\scheduler.db
```
Ci sono backup orari automatici più uno scattato prima di ogni migrazione.

---

## 10. Collegamento fra le tre parti

La specifica chiede di non dichiarare il lavoro completo se manca il
collegamento fra raccolta ore, controllo delle mancanze e riepilogo economico.
Il collegamento c'è ed è verificato dai test:

```
dichiarazione sul tablet
        │
        ├─► anomalie_service: confronta con le ore attese
        │        └─► anomalia aperta → notifica a Elena → correzione → si chiude da sola
        │
        └─► riepilogo_service: minuti per cliente × tariffa del periodo
                 └─► costo ore nel riepilogo mensile, riconciliabile con
                     fatturato e materiali
```

Salvare una giornata rivaluta subito l'anomalia (`rivaluta_giornata`), e le
stesse ore entrano nel riepilogo senza essere sommate alle vecchie scansioni.
Test: criteri 8, 10, 14 e 16.
