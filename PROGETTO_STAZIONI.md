# FerroTrack organizzato per stazioni

Decisioni prese con Stefano il 29–30 settembre 2026, durante il lavoro su
Ufficio, Laser e Banco lamiere. Questo documento dice **dove sta andando
l'app**: chi riprende il lavoro parte da qui, prima di toccare le pagine.

I prototipi visivi sono in `docs/mockup/`:

- `ferrotrack_stazioni.html`: l'app per stazioni, com'è stata approvata come
  direzione (pagina iniziale, Ufficio, Laser, Tablet officina, Timbratrice,
  foglio d'ordine).
- `ferrotrack_strade.html`: le tre alternative confrontate prima di
  scegliere (menu laterale, lavagna a colonne, postazione officina).

Sono pagine statiche, si aprono con un doppio clic.

---

## Il problema da risolvere

Stefano non era convinto di struttura, esperienza d'uso, colori, spazi e
schede. La diagnosi:

- **Troppe schede**: 8 nell'Ufficio, 5 nel Laser, 3 nelle Ore, più le code
  dentro le schede. Si cerca dove sta una cosa invece di farla.
- **Lo stesso ordine ha cinque facce**: Ufficio, Laser, Capo officina, Tablet
  e Archivio lo mostrano con campi e parole diverse.
- **Tutto grigio, piccolo e incorniciato**: fondo grigio, bordi ovunque, testo
  a 12–13 px, tanti colori di stato. Niente spicca.

Idea di Stefano, confermata: **ogni stazione è ottimizzata per quello che
deve fare**, e vede solo quello che le serve.

## Le stazioni

| Stazione | Dispositivo | Chi | Cosa fa | Come si entra |
|---|---|---|---|---|
| Commerciale | PC | chi fa i preventivi | preventivi, CAD, prezzi | PIN |
| Ufficio | PC | amministrazione | carica ordini, lavoro finito, DDT, consegne, fatture, ore, archivio | PIN |
| Laser | PC, **secondo monitor accanto a Lantek** | chi fa il nesting **e** chi taglia | ponte tra FerroTrack e Lantek | PC impostato, entra subito |
| Tablet officina | schermo in officina | tutti | ordini in lavorazione, sola lettura | si accende e mostra |
| Timbratrice | tablet all'ingresso | operai | ore della giornata | si accende e timbra |

Commerciale e Ufficio sono **persone diverse**. **Non esiste una postazione
del capo officina**.

Due linguaggi visivi:

- **Stazioni da scrivania** (Commerciale, Ufficio, Laser): chiare, dense,
  professionali. Fondo bianco, un solo colore d'accento (il verde LS
  `#1F6B47`), testo 14–15 px, menu laterale al posto delle schede.
- **Schermi d'officina** (Tablet officina, Timbratrice): fondo scuro,
  scritte grandi (18–64 px), giallo da segnaletica per l'azione, leggibili
  da lontano.

Il Laser **non** è uno schermo d'officina: è uno strumento da ufficio
tecnico usato accanto a Lantek.

## Pagina iniziale

- Ogni dispositivo viene **impostato una volta** come stazione ("Cosa è
  questo dispositivo?"). Per cambiarlo serve il PIN dell'amministratore.
- Da lì in poi il dispositivo apre sempre la sua stazione: niente elenco di
  postazioni da scegliere.
- Negli uffici (Commerciale, Ufficio) si entra con un **PIN** all'accensione
  e dopo un po' di inattività, perché lì ci sono prezzi e fatture. Oggi
  chiunque sulla rete può entrare come "Amministrazione": il PIN chiude
  questo buco.
- Il pannello verde di presentazione e la vecchia dashboard nascosta in
  `login.html` (circa 2000 righe mai mostrate) vanno tolti.

## Ufficio

- Menu laterale: **Oggi**; Ordini (in lavorazione, pronti per DDT, da
  fatturare, carica ordine); Clienti (risposte ai preventivi); Altro (ore,
  archivio, impostazioni).
- Si apre su **"Oggi"**: consegne in ritardo, ordini pronti per il DDT,
  fatture da registrare, ciascuno col pulsante del passo giusto.
- **"Lavoro finito" lo segna l'ufficio**, quando l'officina lo dice a voce.
- L'Archivio smette di essere una pagina separata e diventa una voce del
  menu.

## Laser: il ponte tra FerroTrack e Lantek

Risposte di Stefano:

- Il PC del laser lo usano **sia chi prepara i nesting sia chi taglia**.
- I disegni arrivano in Lantek da una **cartella condivisa**, importati a
  mano.
- Da Lantek esce una **lista dei pezzi tagliati**.
- **Fogli, sfridi e magazzino si gestiscono in Lantek.**

Quindi FerroTrack **non rifà il nesting né il magazzino**. Il suo compito è
far entrare in Lantek il lavoro giusto e far tornare in FerroTrack quello che
è stato tagliato, senza copiare niente a mano.

1. **Verso Lantek**: pezzi degli ordini smistati, raggruppati per lamiera.
   "Invia a Lantek" scrive nella cartella condivisa i DXF puliti e l'elenco
   delle quantità (CSV), con nomi leggibili. FerroTrack tiene traccia di
   cosa è già stato mandato.
2. **Da Lantek**: chi taglia carica la lista dei pezzi tagliati esportata da
   Lantek. FerroTrack segna i pezzi tagliati in ogni ordine, anche in parte
   ("DECA #01: tagliati 18 pezzi su 22"). "Lamiera tagliata" a mano resta
   come ripiego.
3. **Stima della lamiera** (Banco lamiere): resta come colpo d'occhio per
   pianificare ("oggi servono circa 4 fogli di S235 3 mm"), non come
   sostituto del nesting. L'idea di un magazzino sfridi in FerroTrack è
   **abbandonata**.

Layout (vedi mockup): sezioni Lamiere / Da smistare / Tagliati; a sinistra le
lamiere per materiale e spessore; a destra la lamiera scelta con formato
consigliato, anteprima del foglio con la linea di taglio, riquadro "Per
Lantek", tabella dei pezzi; azioni Disegni, Esporta per Lantek, Lamiera
tagliata.

Regole già decise per il Banco lamiere:

- Lista ordinata **per materiale e poi spessore** (l'urgenza resta come
  opzione).
- Formato **"Automatico"**: per ogni lamiera il formato, o la combinazione di
  formati, che consuma meno lamiera. Si può imporre un formato a mano.
- **S235, S275 e S355 sono la stessa lamiera** (stessa famiglia "S235" per
  prezzo e per il banco). Non separarle.
- Disposizione **per ingombro** (rettangolo del pezzo), non per sagoma.

## Tablet officina e Timbratrice

- Tablet officina: sola lettura, ordini in lavorazione dalla consegna più
  vicina, cosa fare, quanto è tagliato; ritardo col bordo rosso. Nessun
  pulsante.
- Timbratrice: tocchi il tuo nome; chi deve ancora dichiarare è in giallo.
  Funziona come oggi, cambia lo stile.

## Cartellini: via

I cartellini A6 **non servono più**. In officina l'ordine si riconosce dal
**PDF d'ordine stampato**:

- ordini arrivati col PDF del cliente: si stampa quello;
- ordini nati da un preventivo (senza PDF): "Stampa ordine" genera un
  **foglio d'ordine A4** con cliente, consegna e pezzi raggruppati per
  lamiera, con le lavorazioni.

Oggi il cartellino si apre dopo "Carica ordine", dopo l'accettazione di un
preventivo e da un pulsante nel pannello dell'ordine: tutti da togliere.

## Cosa sparisce

- Cartellini (sostituiti dal foglio d'ordine).
- Pagina del capo officina (`capo-officina.html`): quello che serve ancora
  passa all'Ufficio.
- Pagina Archivio separata (`archivio.html`): diventa una voce dell'Ufficio.
- Login con l'elenco delle postazioni.

## Fatto finora (fino al 30/09/2026)

- Base grafica comune `app/frontend/ft-ui.css` + `ft-ui.js` (date in ora
  locale, euro, toast, conferme). Sarà adattata al nuovo linguaggio (fondo
  bianco, verde LS, testo più grande).
- Flusso ordini unico: lavoro finito → DDT → consegna → fattura obbligatoria
  → archivio; un solo campo DDT; orari con fuso.
- Disegni e distinta di ogni ordine (`/api/orders/<id>/disegni`,
  `/distinta`), lista ordini leggera (`?aperti=1`), indici sul database.
- Ufficio (`impiegata.html`) e Laser (`laser.html`) ridisegnati sulla base
  comune: **sono il passo intermedio**, da riportare alla struttura per
  stazioni descritta sopra.
- Banco lamiere (`/api/laser/banco` + scheda "Lamiere" del Laser).
- Caricamento del **pacchetto ordine + disegni** in "Carica ordine": gli
  ordini senza preventivo nascono con distinta e disegni (scheda tecnica
  nascosta, `preventivi.solo_tecnico`).

## Fatto il 5-6/10/2026

- **Cartellini e codici a barre tolti del tutto** (PDF del cartellino,
  pistole, `scan_hub`, `python-barcode`). In officina si stampa il PDF del
  cliente o il **foglio d'ordine** (`GET /api/orders/<id>/stampa`,
  `backend/foglio_ordine.py`). Le tabelle vecchie restano nel database come
  storico.
- **Accesso per dispositivo + PIN** (`backend/accesso.py`, `api_accesso.py`,
  `frontend/ft-accesso.js`, `tools/persone.py`). Decisioni di Stefano:
  1 PIN per persona (unico, 4-8 cifre, niente 1111 o 1234); negli uffici il
  PIN si chiede **una volta al giorno** (vale fino alle 3 di notte, mai
  durante la giornata); **Amministrazione e cambio di stazione** vogliono il
  PIN di un amministratore. Laser, Tablet officina e Timbratrice entrano
  senza PIN. Modalità `transizione` (si entra ancora "come prima" finché i
  dispositivi non sono registrati) e `protetto`.
- **Tablet officina** rifatto (filtri, pezzi, disegni, foglio d'ordine,
  "Segnala un problema").
- **Uno stile per tutta l'app: quello del preventivatore** (indaco, Inter /
  Geist / JetBrains Mono, pannelli bianchi su fondo grigio chiaro, un solo
  logo LS). Le alternative "Acciaio" (verde LS) e "Tavola tecnica" sono state
  scartate: la seconda stanca a usarla tutto il giorno. Anche Tablet officina
  e Timbratrice sono chiari, con scritte e tasti più grandi (`ft-touch`).
- **Effetti comuni** in `ft-ui.css` / `ft-ui.js`, brevi e solo su eventi
  veri: pagine in dissolvenza, liste che entrano in sequenza la prima volta,
  numeri che scorrono (`FT.conta`, `FT.contaDa`), ordine nuovo evidenziato
  (`FT.evidenzia`), spunta animata sui salvataggi. Spenti con "riduci
  movimento".
- Nuova **pagina iniziale** (logo, orologio, luce indaco sullo sfondo, la
  spunta all'ingresso). Corretto il giro a vuoto dopo "cambia": la pagina
  ripresentava "Cosa è questo dispositivo?" anche a cambio riuscito.

## Domande aperte

- **Esempio della lista pezzi tagliati di Lantek** (formato e colonne), per
  leggerla in automatico.
- **Struttura della cartella per Lantek**: per lamiera (`S235_3mm/`) o per
  ordine (`DECA_PREV-2026-0001/`), e il percorso di rete
  (`disegni_export_root` in `app_config.json` è ancora vuoto).
- **Pagina "Oggi"** dell'Ufficio: confermare che l'ufficio vuole aprire lì.
- **Dashboard grande** (`dashboard-live.html`): si usa su una TV? Se no, si
  toglie.
- `archivio.html` e `capo-officina.html` non hanno più link: da eliminare?
- Serve il DDT prima di poter registrare la consegna?

## Ordine di lavoro proposto

Una stazione alla volta; ognuna funziona da sola e si committa prima della
successiva.

1. Togliere i cartellini e aggiungere il foglio d'ordine stampato.
2. Laser come ponte con Lantek (esportazione nella cartella condivisa,
   lettura della lista pezzi tagliati, taglio per pezzo).
3. Ufficio con menu laterale e pagina "Oggi".
4. Pagina iniziale per dispositivo + PIN negli uffici.
5. Tablet officina e Timbratrice nello stile d'officina.
6. Togliere capo officina e archivio separato; decidere sulla dashboard.
