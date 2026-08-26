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

Caddy answers on `:80` by default, which is all a bare IP can do — Let's
Encrypt will not issue for an IP address. Point a domain at the instance and
set `SITE_ADDRESS`, and Caddy handles the certificate itself:

```sh
# in /opt/southend-ticketing/deploy on the VM
SITE_ADDRESS=tickets.example.com docker compose -f compose.prod.yaml up -d
```

Give the instance a static IP first, otherwise the address changes on stop:

```sh
gcloud compute addresses create southend-tickets-ip --region=us-central1 \
  --project=mindful-torus-474012-m1
```

## Watching the free tier

Egress is the one metered thing at 1GB/month. Inbound scraping from ktckts is
ingress and free; only page-serving counts against it, so a low-traffic site
stays well inside. Going over is billed at a few cents per GB rather than cut
off.
