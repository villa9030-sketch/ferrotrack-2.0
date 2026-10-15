# Riaddestramento periodico del modello del motore

Il modello (`backend/preventivi/modello_motore.json`: ranker dei candidati + "sicuro")
si riaddestra con i pezzi che Stefano conferma nel controllo del laser e nel
preventivatore. Si fa **ogni settimana**, oppure dopo una modifica al motore
(cambia `backend/preventivi`). **Non installa niente.**

## Quando e come

- Fuori orario o con poco carico: il PC e' anche il server di FerroTrack.
  Con `--workers 1` va bene anche mentre si lavora (il motore gira a priorita' bassa).
- Da questa cartella, con il Python dello studio (ha scikit-learn):

```
..\..\..\..\banco_motore\.venv_studio\Scripts\python.exe periodico.py --workers 1 --banco ..\..\..\..\banco_motore\riaddestra_<ultima>
```

`--banco` riusa i dump del banco Lantek dell'ultima volta se il codice del motore
non e' cambiato (`motore.txt` = hash git di `app/backend/preventivi`); altrimenti
li rifa' (circa 45 min DXF + 10 min DWG con 2 processi, il doppio con 1).

## Cosa fa

1. Copia `ferrotrack-2.0/app/database/scheduler.db` con sqlite `backup()` (sola lettura).
2. `raccogli_esempi.py`: esempi da `dati_motore/esempi.jsonl` e dal DB
   (`geometria_manuale_confermata`, `decisione_laser` giusto/scelto, contorno scelto).
   Scarta: cliente TEST, file `_cleaned`, proposte automatiche (peso-cartiglio,
   auto-accettato), contraddizioni con Lantek, disegni gia' nel banco, non_laser/sviluppo,
   file della copia di stress. Per lo stesso disegno vale l'ultima decisione.
3. Dump del motore sugli esempi; `prepara_esempi.py` ricava l'area esterna e mette
   da parte 1/3 dei preventivi/ordini (**tenuti fuori**, mai nell'addestramento).
4. `addestra.py` due volte sugli stessi dump: solo Lantek (`modelli_base`) e
   Lantek + esempi con peso 3 (`modelli_nuovo`).
5. `valuta_modelli.py`: modello attuale contro nuovo su banco completo, lista di
   regressione 1500, esempi tenuti fuori; e i due addestramenti a fold tenuti fuori.
6. Verdetto in `rapporto.txt`: MIGLIORE solo se in tutte e quattro le misure la
   precisione dei sicuri non scende e non c'e' nessun nuovo errore silenzioso
   (sicuro ma sbagliato), e in piu' il banco guadagna qualcosa.

## Se il verdetto e' MIGLIORE

Copiare `modelli_nuovo/modello_motore.json` su `app/backend/preventivi/modello_motore.json`
in un worktree (MAI direttamente in `ferrotrack-2.0`), commit, e installazione
**solo con l'OK di Stefano, fuori orario** (riavvio del server).

## Da guardare ogni volta

- `esempi_inventario.json`: contraddizioni con Lantek (da far vedere a Stefano),
  `stress_nel_registro` deve essere 0.
- `esempi_tutti.csv`: ogni esempio con il motivo per cui e' usato o no.
