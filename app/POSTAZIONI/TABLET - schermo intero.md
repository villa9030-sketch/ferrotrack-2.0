# Tablet: l'icona a schermo intero

Dopo l'aggiornamento del 16/10/2026 i tablet in officina possono aprire
FerroTrack senza nessuna barra di Android, né in alto né in basso.
Per averlo bisogna rifare l'icona una volta su ogni tablet, perché
quella vecchia continua ad aprirsi con la barra sotto.

Ci sono tre icone, una per ogni uso del tablet:

| Pagina da aprire in Chrome | Icona che si installa |
|---|---|
| `http://192.168.1.10:5000/operaio-info.html` | FerroTrack Officina |
| `http://192.168.1.10:5000/ore.html` | FerroTrack Ore |
| `http://192.168.1.10:5000/dashboard-live.html` | FerroTrack Cartellone |

## 1. Togli l'icona vecchia

Tieni premuta l'icona FerroTrack sulla schermata del tablet e tocca
**Disinstalla** (o trascinala su "Disinstalla"). Non perdi niente: il
tablet resta registrato.

## 2. Controlla l'impostazione di Chrome (solo se l'hai rifatta da zero)

Va fatta una volta, e di solito c'è già. Se "Installa app" non compare:

1. In Chrome scrivi `chrome://flags` nella barra degli indirizzi.
2. Cerca **Insecure origins treated as secure**.
3. Nel riquadro scrivi `http://192.168.1.10:5000`, scegli **Enabled**.
4. Tocca **Relaunch** in basso.

## 3. Installa l'icona nuova

1. Apri in Chrome la pagina giusta (tabella sopra).
2. Menu di Chrome (i tre puntini) → **Installa app** (o **Aggiungi a
   schermata Home → Installa**).
3. Conferma. Sulla schermata compare l'icona col nome nuovo
   (es. "Officina").
4. Chiudi Chrome e apri FerroTrack **dall'icona**: si apre già sulla
   pagina giusta, senza barre.

Per far ricomparire per un attimo le barre di Android basta far scorrere
il dito dal bordo dello schermo. Spariscono da sole.

## Se non vuoi reinstallare

Aperta in Chrome, la pagina ha in alto a destra il pulsante
**Schermo intero**. Toccalo una volta. Se il tablet va in standby o
qualcuno preme Indietro, lo schermo intero torna al primo tocco sulla
pagina. Con l'icona nuova il pulsante non compare, perché non serve.
