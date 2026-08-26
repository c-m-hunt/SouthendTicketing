> [!IMPORTANT]
> **Superseded — scheduled for retirement.**
>
> The site now deploys to the DigitalOcean cluster at
> `sufc-tickets.chris-hunt.net`, via GitHub Actions → ECR → the manifest in
> [`c-m-hunt/server-setup`](https://github.com/c-m-hunt/server-setup)
> (`tf/modules/sites/sufc-tickets`). This GCP deployment was left running
> deliberately, so something was serving while the new path was proven.
>
> Tear it down once the cluster host has been stable for a while.
>
> **Do not start until**
> - [ ] `sufc-tickets.chris-hunt.net` resolves and serves over HTTPS
> - [ ] The sales-over-time chart shows data accumulating from the cluster's
>       own CronJob — the one thing that proves the volume is persisting
>
> **Then, in this repo**
> - [ ] `deploy/compose.prod.yaml` — delete, or keep purely for running the
>       production image locally (`compose.yaml` already covers dev)
> - [ ] This file
> - [ ] The GCP deployment and sslip.io sections of the root `README.md`
>
> **And outside it**
> - [ ] Stop and delete the `southend-tickets` VM
> - [ ] Release its static IP, if one was reserved
> - [ ] Remove the firewall rule if nothing else uses it
> - [ ] Check for a cron or systemd timer on that VM doing refreshes. The
>       cluster CronJob polls every 15 minutes, and two of them hitting the
>       club's ticketing site is worth avoiding.

# Deploying to GCP

The app runs on a Google Compute Engine `e2-micro`, which sits inside the
[Always Free](https://cloud.google.com/free/docs/free-cloud-features#compute)
allowance: one `e2-micro` per billing account in `us-west1`, `us-central1` or
`us-east1`, plus 30GB of standard persistent disk and 1GB/month of egress.

SQLite needs a filesystem that survives a restart, which is why this is a VM
rather than one of the container platforms — as of 2026 none of the free PaaS
tiers (Render, Koyeb, Railway, Fly) still offer a persistent volume.

## What is provisioned

| | |
|---|---|
| Project | `mindful-torus-474012-m1` ("Personal") |
| Billing account | `01764F-2C6C10-EFC7E1` ("Personal") |
| Instance | `southend-tickets`, `e2-micro`, `us-central1-a` |
| Disk | 30GB `pd-standard` |
| Firewall | `allow-http-https` — tcp:80 and tcp:443 from anywhere |

## How it boots

`startup.sh` runs on every boot and is idempotent. It adds a 2GB swapfile
(1GB of RAM is not enough to build the image reliably), installs Docker,
checks out the repo at `/opt/southend-ticketing`, and brings up
`compose.prod.yaml`.

The branch is `update-2026`, not the repo default — `master` is still the
pre-ktckts site. Override with the `REPO_BRANCH` environment variable.

## Recreating it

```sh
gcloud compute instances create southend-tickets \
  --project=mindful-torus-474012-m1 \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-standard \
  --tags=http-server,https-server \
  --metadata-from-file=startup-script=deploy/startup.sh

gcloud compute firewall-rules create allow-http-https \
  --project=mindful-torus-474012-m1 \
  --allow=tcp:80,tcp:443 \
  --target-tags=http-server,https-server \
  --source-ranges=0.0.0.0/0
```

Run both from the repo root — `--metadata-from-file` resolves
`deploy/startup.sh` relative to the working directory.

## Deploying a change

Push to `update-2026`, then re-run the startup script on the box:

```sh
gcloud compute ssh southend-tickets --zone=us-central1-a \
  --project=mindful-torus-474012-m1 \
  --command='sudo google_metadata_script_runner startup'
```

The SQLite database lives in the `data` Docker volume, so it survives both a
rebuild and a `docker compose down`. It does not survive deleting the VM —
copy `/var/lib/docker/volumes/deploy_data/_data/app.db` off first if you ever
need to rebuild the instance.

## Serving over HTTPS

Currently live at **https://34.70.17.54.sslip.io/**.

Let's Encrypt will not issue a certificate for a bare IP, so the site borrows
a hostname from [sslip.io](https://sslip.io), a wildcard DNS service that
resolves `<ip>.sslip.io` back to that IP. That is a real name, so Caddy can
complete an ACME challenge against it and serve a genuine certificate.

The address is set in `deploy/.env` on the VM, which is untracked and so
survives the startup script's `git reset --hard`:

```sh
# /opt/southend-ticketing/deploy/.env
SITE_ADDRESS=34.70.17.54.sslip.io
```

Two caveats for anything longer-lived than a demo:

* The hostname contains the IP, so it changes if the instance's ephemeral
  address does. Reserve a static one before relying on it.
* `sslip.io` is not on the Public Suffix List, so every certificate issued
  under it shares one Let's Encrypt rate-limit bucket. Issuance can fail when
  other people have exhausted it.

Swapping to a real domain is the same mechanism — point it at the instance,
change `SITE_ADDRESS`, and restart:

```sh
cd /opt/southend-ticketing/deploy
echo "SITE_ADDRESS=tickets.example.com" | sudo tee .env
sudo docker compose -f compose.prod.yaml up -d
```

Reserve a static IP first, otherwise the address changes whenever the instance
stops:

```sh
gcloud compute addresses create southend-tickets-ip --region=us-central1 \
  --project=mindful-torus-474012-m1
```

## Watching the free tier

Egress is the one metered thing at 1GB/month. Inbound scraping from ktckts is
ingress and free; only page-serving counts against it, so a low-traffic site
stays well inside. Going over is billed at a few cents per GB rather than cut
off.
