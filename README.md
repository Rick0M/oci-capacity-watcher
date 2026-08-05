# OCI Capacity Watcher

Riprova automaticamente, ogni 5 minuti via GitHub Actions, a creare una VM Oracle Cloud
`VM.Standard.A1.Flex` (2 OCPU / 12 GB, Ubuntu 24.04 ARM) nella region `eu-milan-1`, finché
Oracle non ha capacità libera ("Out of host capacity" è l'esito quasi sempre, per giorni/settimane).

- Ogni esecuzione fa **un solo tentativo** (`--single-attempt`) e si ferma: verde se non c'è
  capacità (esito normale), rosso solo su un errore vero (credenziali, permessi, config).
- La chiave privata API vive **solo** nei GitHub Secrets del repo (`OCI_API_KEY_PEM`), mai nel
  codice — sicura anche essendo il repo pubblico.
- Su successo: l'IP pubblico finisce in `capacity_watcher_result.json` (committato dal workflow
  stesso), e i run successivi si fermano subito senza ritentare.
- Log completo di tutti i tentativi in `capacity_watcher.log`.
