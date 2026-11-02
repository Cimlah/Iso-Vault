# Using a reverse proxy

If a reverse proxy already runs on your server, it can serve Iso-Vault's web UI at its own hostname, with HTTPS. The installer looks for one, and helps you set it up:

```bash
./install.sh --proxy-host isovault.example.com
```

## What goes through the proxy

Only the web UI.

| Traffic | Port | Through the proxy? |
|---|---|---|
| Web UI | `UI_PORT`, default 8081 | Yes |
| iPXE scripts, kernels, ISO files | `BOOT_PORT`, default 8080 | No, always direct to the LAN IP |
| DHCP, TFTP, NFS, SMB | Their standard ports | No, these aren't HTTP |

Boot traffic stays direct because the software a device runs while booting can't be relied on to trust your proxy's certificate, or to follow a redirect to HTTPS. It would also slow down multi-gigabyte downloads.

Iso-Vault never uses ports 80 or 443, so it won't collide with your proxy. If 8080 or 8081 are taken, the installer picks other ports.

## What the installer does

| Found | What happens |
|---|---|
| Caddy on the host | Shows you the exact change, and after you agree, adds the site. Runs `caddy validate` and reloads, restoring your config if either fails |
| Caddy in Docker | Writes `reverse-proxy/Caddyfile` for you to add |
| nginx, Traefik, Apache or HAProxy | Writes a ready-to-use file in `reverse-proxy/` for you to add |

If the proxy setup fails, the install still succeeds, and the UI stays available at `http://<server-ip>:8081`.

Unattended installs only change your Caddy config if you also pass `--apply-proxy`.

## Doing it by hand

Set the UI's public address in `.env`, then run `docker compose up -d`:

```
UI_PUBLIC_URL=https://isovault.example.com
```

### Caddy

```caddyfile
isovault.example.com {
    reverse_proxy 127.0.0.1:8081
}
```

For a name that isn't on the public internet, such as `isovault.lan`, add `tls internal` inside the block, and let your browser trust Caddy's local certificate.

### Caddy in Docker

Iso-Vault uses host networking, so it can't join your proxy's Docker network. Point the proxy at the server's LAN IP instead:

```caddyfile
isovault.example.com {
    reverse_proxy 192.168.1.10:8081
}
```

The same applies to any proxy running in Docker.

### nginx

```nginx
server {
    listen 80;
    server_name isovault.example.com;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Add your certificate settings to serve this over HTTPS.

### Traefik

Docker labels can't describe a container on host networking, so use Traefik's file provider:

```yaml
http:
  routers:
    iso-vault:
      rule: "Host(`isovault.example.com`)"
      service: iso-vault
      entryPoints: ["websecure"]
      tls: {}
  services:
    iso-vault:
      loadBalancer:
        servers:
          - url: "http://127.0.0.1:8081"
```

Traefik in Docker should use the server's LAN IP here.

## Closing the direct port

Once the proxy works and runs on the same host, you can stop the UI being reachable directly. Set this in `.env`, then run `docker compose up -d`:

```
UI_BIND=127.0.0.1
```

The installer does this for you when it sets up Caddy on the host. Don't do this if your proxy runs in Docker, because it wouldn't be able to reach the UI.

## Things that won't work

- **A sub-path, such as `example.com/isovault`.** Next.js fixes its base path when the image is built, so the UI needs its own hostname.
- **Putting the boot port behind the proxy.** Devices must reach it directly.

## Health checks

`GET /healthz` on the UI port needs no login, and reveals nothing.
