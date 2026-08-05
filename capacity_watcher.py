#!/usr/bin/env python3
"""
capacity_watcher.py — Ritenta la creazione della VM Oracle ARM finche' non trova capacita' libera.

Usa la chiave API (~/.oci/config) invece del wizard del browser: le chiamate API si autenticano
ad ogni richiesta con la chiave firmata, quindi NON esiste una sessione interattiva che scade.

Comportamento:
  - Prova a creare l'istanza (launch_instance). Se l'errore e' "out of host capacity" (o simile,
    limite di risorse temporaneo), aspetta e RIPROVA. Su qualsiasi ALTRO errore (permessi,
    parametri sbagliati, quota esaurita, ecc.) SI FERMA e mostra l'errore per intero: non ha senso
    ritentare all'infinito un errore permanente.
  - Su successo: aspetta l'assegnazione dell'IP pubblico e lo stampa, poi termina.
  - Logga ogni tentativo su capacity_watcher.log.

USO
  python3 capacity_watcher.py --single-attempt      # una prova sola (usato da GitHub Actions)
  python3 capacity_watcher.py                        # ritenta a oltranza fino a successo/errore fatale
"""
from __future__ import annotations
import argparse, json, sys, time, random
import datetime as dt
from pathlib import Path

import oci

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "capacity_watcher.log"
RESULT_PATH = HERE / "capacity_watcher_result.json"

# --- config della VM (decisa in questa sessione: 2 OCPU / 12 GB, Ubuntu 24.04 ARM) ---
DISPLAY_NAME = "portfolio-ib-vps"
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2
MEMORY_GB = 12
AVAILABILITY_DOMAIN = "Heda:EU-MILAN-1-AD-1"
SUBNET_ID = "ocid1.subnet.oc1.eu-milan-1.aaaaaaaa4oyqafjoipguho72misyqbqobo25lslguhzhxyifi2ilb4hwauma"
IMAGE_ID = "ocid1.image.oc1.eu-milan-1.aaaaaaaajiazzxymeimzet7ztsarraqnq3h23egtemplssuu55mgcxvelyla"
SSH_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDsI/XbWx1N2ZOUqWDhxNbZ2URtXYYW0JiF6Wt5OQ09T portfolio-ib-vps"

RETRY_MIN_SEC = 100    # ~2 minuti (jitter). Verificato sicuro: centinaia di tentativi senza 429.
RETRY_MAX_SEC = 140
RATE_LIMIT_MIN_SEC = 270   # ~5 minuti (jitter). Backoff dedicato dopo un 429: temporaneo, non fatale.
RATE_LIMIT_MAX_SEC = 330


def log(msg: str) -> None:
    line = f"{dt.datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def is_capacity_error(e: oci.exceptions.ServiceError) -> bool:
    msg = (e.message or str(e)).lower()
    return e.status in (500, 503) and ("capacity" in msg or "insufficient" in msg)


def try_launch(compute: oci.core.ComputeClient, tenancy: str):
    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=tenancy,
        display_name=DISPLAY_NAME,
        availability_domain=AVAILABILITY_DOMAIN,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=OCPUS, memory_in_gbs=MEMORY_GB),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_ID, assign_public_ip=True,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=IMAGE_ID),
        metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
    )
    return compute.launch_instance(details).data


def wait_public_ip(compute: oci.core.ComputeClient, net: oci.core.VirtualNetworkClient,
                    tenancy: str, instance_id: str, timeout_s: int = 300) -> str | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for att in compute.list_vnic_attachments(compartment_id=tenancy, instance_id=instance_id).data:
            if att.lifecycle_state == "ATTACHED":
                vnic = net.get_vnic(att.vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip
        time.sleep(10)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--single-attempt", action="store_true", help="prova una sola volta (usato da GitHub Actions)")
    ap.add_argument("--max-hours", type=float, default=0, help="0 = nessun limite, altrimenti ore massime di tentativi")
    args = ap.parse_args()

    config = oci.config.from_file()
    tenancy = config["tenancy"]
    compute = oci.core.ComputeClient(config)
    net = oci.core.VirtualNetworkClient(config)

    log(f"Avvio: shape={SHAPE} ocpus={OCPUS} mem={MEMORY_GB}GB AD={AVAILABILITY_DOMAIN}")
    start = time.time()
    attempt = 0
    consecutive_network_errors = 0
    while True:
        attempt += 1
        try:
            instance = try_launch(compute, tenancy)
            log(f"SUCCESSO al tentativo #{attempt}! Instance OCID: {instance.id}  stato: {instance.lifecycle_state}")
            ip = wait_public_ip(compute, net, tenancy, instance.id)
            result = {"instance_id": instance.id, "public_ip": ip, "attempts": attempt,
                      "created_at": dt.datetime.now().isoformat()}
            RESULT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
            if ip:
                log(f"IP pubblico assegnato: {ip}")
                log(f"Connettiti con: ssh -i C:\\Users\\x\\.ssh\\id_ed25519 ubuntu@{ip}")
            else:
                log("Istanza creata ma l'IP pubblico non e' comparso entro 5 minuti: controlla la Console.")
            return
        except oci.exceptions.ServiceError as e:
            consecutive_network_errors = 0
            if e.status == 429:
                # rate limit: TEMPORANEO, non fatale. Backoff lungo e dedicato, poi si continua.
                wait_s = random.uniform(RATE_LIMIT_MIN_SEC, RATE_LIMIT_MAX_SEC)
                log(f"Tentativo #{attempt}: rate limit Oracle (429). Backoff lungo: {wait_s/60:.1f} min...")
            elif is_capacity_error(e):
                wait_s = random.uniform(RETRY_MIN_SEC, RETRY_MAX_SEC)
                log(f"Tentativo #{attempt}: nessuna capacita' disponibile (status {e.status}). "
                    f"Riprovo tra {wait_s:.0f}s...")
            else:
                log(f"ERRORE NON RECUPERABILE (status {e.status}, code {e.code}): {e.message}")
                log("Mi fermo: non ha senso ritentare un errore che non e' di capacita' ne' un rate limit.")
                sys.exit(1)
        except Exception as e:
            # errori di RETE (connessione interrotta, timeout, DNS, ecc.): SEMPRE temporanei, mai
            # un motivo per fermarsi: costa pochissimo continuare a riprovare, anche per ore.
            consecutive_network_errors += 1
            wait_s = random.uniform(RETRY_MIN_SEC, RETRY_MAX_SEC)
            log(f"Tentativo #{attempt}: errore di rete/connessione ({type(e).__name__}), "
                f"nessuna connettivita' da {consecutive_network_errors} tentativi. "
                f"Riprovo tra {wait_s:.0f}s...")

        if args.single_attempt:
            log("Modalita' --single-attempt: mi fermo qui.")
            return
        if args.max_hours and (time.time() - start) / 3600 >= args.max_hours:
            log(f"Raggiunto il limite di {args.max_hours}h senza successo. Mi fermo.")
            return
        time.sleep(wait_s)


if __name__ == "__main__":
    main()
