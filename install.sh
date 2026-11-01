#!/usr/bin/env bash
# Iso-Vault one-click installer. Safe to re-run: existing settings and passwords are kept.
#
#   curl -fsSL https://raw.githubusercontent.com/Cimlah/Iso-Vault/main/install.sh | bash
#   ./install.sh --help
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/Cimlah/Iso-Vault/main"
DEFAULT_DIR="iso-vault"
IMAGE="ghcr.io/cimlah/iso-vault"
DEFAULT_BOOT_PORT=8080
DEFAULT_UI_PORT=8081

ASSUME_YES=false
DHCP_MODE_FLAG=""
INTERFACE_FLAG=""
PROXY_HOST_FLAG=""
NO_PROXY=false
APPLY_PROXY=false
INSTALL_DIR=""

# Test hooks. They let tests/installer run the detection logic against stubbed commands.
CADDYFILE="${ISO_VAULT_CADDYFILE:-/etc/caddy/Caddyfile}"
ALLOW_ANY_OS="${ISO_VAULT_ALLOW_ANY_OS:-false}"

usage() {
    cat <<'EOF'
Iso-Vault installer

Options:
  --dhcp-mode MODE    proxy, server or upstream. By default it's chosen for you:
                      proxy if a DHCP server is found, otherwise you're asked.
  --interface NAME    Network interface to serve on.
  --dir PATH          Where to install. Default: ./iso-vault, or here if run from a checkout.
  --proxy-host NAME   Hostname for the web UI behind your reverse proxy.
  --no-proxy          Skip reverse proxy setup.
  --apply-proxy       Allow changing your Caddy config without asking. Needs --proxy-host.
  --yes               Don't ask questions. Never turns on server mode by itself.
  --help              Show this help.
EOF
}

say() { printf '%s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Checks by opening it, because /dev/tty always looks readable even with no terminal attached.
can_prompt() { [ "$ASSUME_YES" = false ] && { : </dev/tty; } 2>/dev/null; }

# Reads from the terminal directly, because stdin is the script itself under `curl | bash`.
ask() {
    local prompt="$1" default="${2:-}" reply=""
    if ! can_prompt; then
        printf '%s' "$default"
        return
    fi
    if [ -n "$default" ]; then
        printf '%s [%s]: ' "$prompt" "$default" >/dev/tty
    else
        printf '%s: ' "$prompt" >/dev/tty
    fi
    IFS= read -r reply </dev/tty || reply=""
    printf '%s' "${reply:-$default}"
}

confirm() {
    local prompt="$1" default="${2:-n}" hint="[y/N]" reply=""
    if ! can_prompt; then
        [ "$default" = y ]
        return
    fi
    [ "$default" = y ] && hint="[Y/n]"
    printf '%s %s: ' "$prompt" "$hint" >/dev/tty
    IFS= read -r reply </dev/tty || reply=""
    reply="${reply:-$default}"
    case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dhcp-mode)
                [ $# -ge 2 ] || die "--dhcp-mode needs a value."
                DHCP_MODE_FLAG="$2"
                shift 2
                ;;
            --interface)
                [ $# -ge 2 ] || die "--interface needs a value."
                INTERFACE_FLAG="$2"
                shift 2
                ;;
            --dir)
                [ $# -ge 2 ] || die "--dir needs a value."
                INSTALL_DIR="$2"
                shift 2
                ;;
            --proxy-host)
                [ $# -ge 2 ] || die "--proxy-host needs a value."
                PROXY_HOST_FLAG="$2"
                shift 2
                ;;
            --no-proxy) NO_PROXY=true; shift ;;
            --apply-proxy) APPLY_PROXY=true; shift ;;
            --yes|-y) ASSUME_YES=true; shift ;;
            --help|-h) usage; exit 0 ;;
            *) die "Unknown option: $1. Try --help." ;;
        esac
    done
    case "$DHCP_MODE_FLAG" in
        ""|proxy|server|upstream) ;;
        *) die "--dhcp-mode must be proxy, server or upstream." ;;
    esac
    if [ -n "$PROXY_HOST_FLAG" ] && ! valid_hostname "$PROXY_HOST_FLAG"; then
        die "--proxy-host must be a plain hostname, like isovault.example.com."
    fi
    if [ "$APPLY_PROXY" = true ] && [ -z "$PROXY_HOST_FLAG" ]; then
        die "--apply-proxy needs --proxy-host."
    fi
}

valid_hostname() {
    [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]
}

check_platform() {
    local os
    os="$(uname -s)"
    if [ "$ALLOW_ANY_OS" != true ] && [ "$os" != Linux ]; then
        die "Iso-Vault has to run on Linux, because PXE needs direct access to your LAN.
Docker Desktop on macOS and Windows keeps containers in a VM that can't see LAN broadcasts.
Use a Linux machine, or a Linux VM with bridged networking."
    fi
    if [ "$ALLOW_ANY_OS" != true ] && grep -qsi microsoft /proc/version; then
        die "WSL can't see your LAN's broadcasts. Use a Linux machine, or a Linux VM with bridged networking."
    fi
}

install_docker() {
    say "Docker isn't installed. Iso-Vault needs Docker and the Compose plugin."
    say "Docker's official install script is at https://get.docker.com"
    if ! can_prompt || ! confirm "Download and run Docker's install script now?"; then
        die "Install Docker (https://docs.docker.com/engine/install/), then run this again."
    fi
    curl -fsSL https://get.docker.com | sudo sh
}

check_docker() {
    command -v docker >/dev/null 2>&1 || install_docker
    DOCKER=(docker)
    if ! docker info >/dev/null 2>&1; then
        if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
            DOCKER=(sudo docker)
        elif command -v sudo >/dev/null 2>&1 && can_prompt && sudo docker info >/dev/null 2>&1; then
            DOCKER=(sudo docker)
        else
            die "Can't talk to Docker. Is it running, and are you allowed to use it?"
        fi
    fi
    "${DOCKER[@]}" compose version >/dev/null 2>&1 \
        || die "The Docker Compose plugin is missing. See https://docs.docker.com/compose/install/linux/"
    local operating_system
    operating_system="$("${DOCKER[@]}" info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
    if [ "$ALLOW_ANY_OS" != true ] && [[ "$operating_system" == *"Docker Desktop"* ]]; then
        die "Docker Desktop keeps containers in a VM that can't see LAN broadcasts. Use Docker Engine on Linux."
    fi
}

prepare_dir() {
    if [ -z "$INSTALL_DIR" ]; then
        if [ -f compose.yml ] && [ -f Dockerfile ]; then
            INSTALL_DIR="."
        else
            INSTALL_DIR="$DEFAULT_DIR"
        fi
    fi
    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    INSTALL_DIR="$(pwd)"
    if [ ! -f compose.yml ]; then
        say "Downloading compose.yml"
        curl -fsSL "$REPO_RAW/compose.yml" -o compose.yml
    fi
    mkdir -p isos data reverse-proxy
}

# Reads one setting from .env, without sourcing it as a script.
env_value() {
    [ -f .env ] || return 0
    grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true
}

set_env() {
    local name="$1" value="$2"
    touch .env
    chmod 600 .env
    if grep -qE "^$name=" .env; then
        local temp
        temp="$(mktemp "${PWD}/.env.XXXXXX")"
        awk -v name="$name" -v value="$value" 'BEGIN { FS = "=" } $1 == name { print name "=" value; next } { print }' .env >"$temp"
        chmod 600 "$temp"
        mv "$temp" .env
    else
        printf '%s=%s\n' "$name" "$value" >>.env
    fi
}

keep_or_set_env() {
    [ -n "$(env_value "$1")" ] || set_env "$1" "$2"
}

generate_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$1"
    else
        head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

list_interfaces() {
    ip -o -4 addr show scope global 2>/dev/null \
        | awk '{ split($4, a, "/"); print $2, a[1] }' \
        | grep -vE '^(docker|br-|veth|virbr|tun|tap|wg|cni|flannel)' || true
}

default_interface() {
    ip -4 route show default 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }'
}

choose_interface() {
    if [ -n "$INTERFACE_FLAG" ]; then
        IFACE="$INTERFACE_FLAG"
    elif [ -n "$(env_value INTERFACE)" ] && [ "$(env_value INTERFACE)" != auto ]; then
        IFACE="$(env_value INTERFACE)"
    else
        IFACE="$(default_interface)"
        local all count
        all="$(list_interfaces)"
        count="$(printf '%s' "$all" | grep -c . || true)"
        if [ "$count" -gt 1 ] && can_prompt; then
            say "This machine has more than one network:"
            printf '%s\n' "$all" | sed 's/^/  /'
            IFACE="$(ask "Which interface should serve PXE clients?" "${IFACE:-$(printf '%s' "$all" | head -n1 | cut -d' ' -f1)}")"
        fi
        [ -n "$IFACE" ] || IFACE="$(printf '%s' "$all" | head -n1 | cut -d' ' -f1)"
    fi
    [ -n "$IFACE" ] || die "Couldn't find a network interface. Use --interface."
    SERVER_IP="$(ip -o -4 addr show dev "$IFACE" scope global 2>/dev/null | awk '{ split($4, a, "/"); print a[1]; exit }')"
    [ -n "$SERVER_IP" ] || die "Interface $IFACE has no IPv4 address."
    say "Serving on $IFACE ($SERVER_IP)"
}

# Sends a DHCP DISCOVER from inside the image. It never takes a lease.
probe_dhcp() {
    "${DOCKER[@]}" run --rm --network host --cap-add NET_RAW --cap-add NET_BIND_SERVICE \
        -e "INTERFACE=$IFACE" "$RESOLVED_IMAGE" probe-dhcp --interface "$IFACE" 2>/dev/null || true
}

choose_mode() {
    if [ -n "$DHCP_MODE_FLAG" ]; then
        DHCP_MODE="$DHCP_MODE_FLAG"
        return
    fi
    if [ -n "$(env_value DHCP_MODE)" ]; then
        DHCP_MODE="$(env_value DHCP_MODE)"
        say "Keeping DHCP mode: $DHCP_MODE"
        return
    fi
    local result servers
    say "Looking for a DHCP server on your network (this takes no lease)..."
    result="$(probe_dhcp)"
    servers="$(printf '%s' "$result" | sed -n 's/.*"servers": \[\(.*\)\].*/\1/p' | tr -d '"')"
    if [ -n "$servers" ]; then
        say "Found a DHCP server at $servers, so Iso-Vault will run alongside it. No router changes needed."
        DHCP_MODE=proxy
    elif [ -z "$result" ]; then
        warn "Couldn't check for a DHCP server, so using proxy mode, which can't disrupt your network."
        DHCP_MODE=proxy
    elif ! can_prompt; then
        # A missed reply here could put a second DHCP server on a live LAN, so don't risk it.
        warn "No DHCP server answered. Staying in proxy mode, because nobody is here to confirm. Use --dhcp-mode server if you want leases."
        DHCP_MODE=proxy
    elif confirm "No DHCP server answered. Should Iso-Vault hand out IP addresses?" y; then
        DHCP_MODE=server
    else
        DHCP_MODE=proxy
    fi
}

# --- Reverse proxy ------------------------------------------------------------

port_owner() {
    ss -ltnpH "sport = :$1" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -n1 || true
}

published_proxy_container() {
    "${DOCKER[@]}" ps --filter publish=443 --filter publish=80 --format '{{.Names}} {{.Image}}' 2>/dev/null || true
}

known_proxy_name() {
    case "$1" in
        *caddy*) printf 'caddy' ;;
        *traefik*) printf 'traefik' ;;
        *nginx*|*openresty*) printf 'nginx' ;;
        *apache*|*httpd*) printf 'apache' ;;
        *haproxy*) printf 'haproxy' ;;
    esac
}

# Sets PROXY_KIND (caddy, nginx, traefik, apache, haproxy or empty) and PROXY_WHERE (host or docker).
detect_proxy() {
    PROXY_KIND=""
    PROXY_WHERE=""
    local line name
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        name="$(known_proxy_name "$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]')")"
        if [ -n "$name" ]; then
            PROXY_KIND="$name"
            PROXY_WHERE="docker"
            return
        fi
    done <<EOF
$(published_proxy_container)
EOF
    local port owner
    for port in 443 80; do
        owner="$(port_owner "$port")"
        if [ -n "$owner" ]; then
            name="$(known_proxy_name "$(printf '%s' "$owner" | tr '[:upper:]' '[:lower:]')")"
            if [ -n "$name" ]; then
                PROXY_KIND="$name"
                PROXY_WHERE="host"
                return
            fi
        fi
    done
    # Reading another user's process name needs root, so also ask systemd.
    local unit
    for unit in caddy traefik nginx apache2 httpd haproxy; do
        if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$unit" 2>/dev/null; then
            PROXY_KIND="$(known_proxy_name "$unit")"
            PROXY_WHERE="host"
            return
        fi
    done
}

configure_proxy_settings() {
    PROXY_HOST=""
    PROXY_KIND=""
    PROXY_WHERE=""
    if [ "$NO_PROXY" = true ]; then
        return
    fi
    detect_proxy
    if [ -z "$PROXY_KIND" ]; then
        if [ -n "$PROXY_HOST_FLAG" ]; then
            warn "--proxy-host was given, but no reverse proxy was found. Writing a Caddy snippet you can adapt."
            PROXY_HOST="$PROXY_HOST_FLAG"
            PROXY_KIND=caddy
            PROXY_WHERE=host
        fi
        return
    fi
    say "Found $PROXY_KIND ($([ "$PROXY_WHERE" = docker ] && printf 'in Docker' || printf 'on this host'))."
    PROXY_HOST="$PROXY_HOST_FLAG"
    if [ -z "$PROXY_HOST" ] && can_prompt; then
        say "It can serve the web UI at its own hostname, with HTTPS. Sub-paths such as /isovault won't work."
        PROXY_HOST="$(ask "Hostname for the web UI (leave blank to skip)" "$(public_host_from_env)")"
    fi
    if [ -n "$PROXY_HOST" ] && ! valid_hostname "$PROXY_HOST"; then
        warn "'$PROXY_HOST' isn't a plain hostname, so skipping reverse proxy setup."
        PROXY_HOST=""
    fi
    if [ -z "$PROXY_HOST" ]; then
        return
    fi
    set_env UI_PUBLIC_URL "$(public_url_for "$PROXY_HOST" "$PROXY_KIND")"
}

public_host_from_env() {
    env_value UI_PUBLIC_URL | sed -E 's#^https?://##; s#/.*$##'
}

public_url_for() {
    local host="$1" kind="$2"
    # nginx and Apache snippets listen on port 80 until someone adds a certificate.
    case "$kind" in
        nginx|apache) printf 'http://%s' "$host" ;;
        *) printf 'https://%s' "$host" ;;
    esac
}

is_local_name() {
    case "$1" in
        *.lan|*.local|*.home.arpa|*.internal|*.test|*.localhost) return 0 ;;
        *) return 1 ;;
    esac
}

template_source() {
    local name="$1"
    if [ -f "templates/reverse-proxy/$name.tmpl" ]; then
        cat "templates/reverse-proxy/$name.tmpl"
    else
        curl -fsSL "$REPO_RAW/templates/reverse-proxy/$name.tmpl"
    fi
}

# Writes reverse-proxy/<file> for the detected proxy, and prints where it goes.
write_proxy_snippet() {
    [ -n "$PROXY_HOST" ] || return 0
    local upstream extension tls_line output
    if [ "$PROXY_WHERE" = docker ]; then
        # A host-networked container can't join Docker networks, so the proxy reaches it by LAN IP.
        upstream="$SERVER_IP:$UI_PORT"
    else
        upstream="127.0.0.1:$UI_PORT"
    fi
    case "$PROXY_KIND" in
        caddy) extension=Caddyfile ;;
        traefik) extension=traefik.yml ;;
        *) extension="$PROXY_KIND.conf" ;;
    esac
    tls_line=""
    if [ "$PROXY_KIND" = caddy ] && is_local_name "$PROXY_HOST"; then
        tls_line='    tls internal\n'
    fi
    output="reverse-proxy/$extension"
    PROXY_SNIPPET="$PWD/$output"
    template_source "$PROXY_KIND" | awk \
        -v host="$PROXY_HOST" -v upstream="$upstream" -v boot="$BOOT_PORT" -v tls="$tls_line" '
        { gsub(/@@HOSTNAME@@/, host); gsub(/@@UPSTREAM@@/, upstream); gsub(/@@BOOT_PORT@@/, boot); sub(/@@TLS_LINE@@/, tls); print }' >"$output"
}

# Adds the snippet to a Caddy running on this host. Any failure restores the original file.
apply_caddy() {
    local site_file import_line backup needs_sudo=()
    site_file="$(dirname "$CADDYFILE")/iso-vault.caddy"
    import_line="import $site_file"
    if [ ! -f "$CADDYFILE" ]; then
        warn "$CADDYFILE doesn't exist, so add $PROXY_SNIPPET to your Caddy config yourself."
        return 1
    fi
    [ -w "$CADDYFILE" ] || needs_sudo=(sudo)

    say ""
    say "This will add the Iso-Vault site to Caddy:"
    say "  1. Create $site_file:"
    sed 's/^/       /' "$PROXY_SNIPPET"
    if ! grep -qxF "$import_line" "$CADDYFILE"; then
        say "  2. Add this line to the end of $CADDYFILE:"
        say "       $import_line"
    fi
    say "  3. Run caddy validate, then reload Caddy. If either fails, your config is restored."
    say ""
    if [ "$APPLY_PROXY" != true ] && ! { can_prompt && confirm "Make these changes?" y; }; then
        say "Left Caddy unchanged."
        return 1
    fi

    backup="$CADDYFILE.iso-vault-backup"
    "${needs_sudo[@]}" cp -p "$CADDYFILE" "$backup"
    "${needs_sudo[@]}" cp "$PROXY_SNIPPET" "$site_file"
    if ! grep -qxF "$import_line" "$CADDYFILE"; then
        printf '\n%s\n' "$import_line" | "${needs_sudo[@]}" tee -a "$CADDYFILE" >/dev/null
    fi

    if ! "${needs_sudo[@]}" caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
        restore_caddy "$backup" "$site_file" "${needs_sudo[@]}"
        warn "caddy validate rejected the change, so your config was restored."
        return 1
    fi
    if ! reload_caddy "${needs_sudo[@]}"; then
        restore_caddy "$backup" "$site_file" "${needs_sudo[@]}"
        warn "Caddy didn't reload, so your config was restored."
        return 1
    fi
    "${needs_sudo[@]}" rm -f "$backup"
    return 0
}

restore_caddy() {
    local backup="$1" site_file="$2"
    shift 2
    "$@" cp -p "$backup" "$CADDYFILE"
    "$@" rm -f "$backup" "$site_file"
}

reload_caddy() {
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet caddy 2>/dev/null; then
        "$@" systemctl reload caddy
    else
        "$@" caddy reload --config "$CADDYFILE" --adapter caddyfile
    fi
}

finish_proxy() {
    [ -n "$PROXY_HOST" ] || return 0
    write_proxy_snippet
    if [ "$PROXY_KIND" = caddy ] && [ "$PROXY_WHERE" = host ] && command -v caddy >/dev/null 2>&1; then
        if apply_caddy; then
            PROXY_STATUS=applied
            # The proxy works, so the plain-HTTP port no longer needs to be open to the LAN.
            # This waits until now, so a failed proxy setup can't lock anyone out of the UI.
            if [ "$(env_value UI_BIND)" != 127.0.0.1 ]; then
                set_env UI_BIND 127.0.0.1
                start_service
            fi
            return 0
        fi
    fi
    PROXY_STATUS=manual
}

# --- Ports and startup --------------------------------------------------------

port_in_use() {
    ss -ltnH "sport = :$1" 2>/dev/null | grep -q .
}

next_free_port() {
    local port="$1"
    while port_in_use "$port"; do
        port=$((port + 1))
    done
    printf '%s' "$port"
}

choose_ports() {
    BOOT_PORT="$(env_value BOOT_PORT)"
    UI_PORT="$(env_value UI_PORT)"
    # Saved ports stay as they are: a running Iso-Vault holds them, which isn't a conflict.
    if [ -z "$BOOT_PORT" ]; then
        BOOT_PORT="$(next_free_port "$DEFAULT_BOOT_PORT")"
        set_env BOOT_PORT "$BOOT_PORT"
    fi
    if [ -z "$UI_PORT" ]; then
        UI_PORT="$(next_free_port "$DEFAULT_UI_PORT")"
        [ "$UI_PORT" != "$BOOT_PORT" ] || UI_PORT="$(next_free_port $((BOOT_PORT + 1)))"
        set_env UI_PORT "$UI_PORT"
    fi
    if [ "$BOOT_PORT" != "$DEFAULT_BOOT_PORT" ] || [ "$UI_PORT" != "$DEFAULT_UI_PORT" ]; then
        say "Using ports $BOOT_PORT (boot) and $UI_PORT (web UI)."
    fi
}

resolve_image() {
    local tag
    tag="$(env_value ISO_VAULT_TAG)"
    tag="${tag:-latest}"
    RESOLVED_IMAGE="$IMAGE:$tag"
    if "${DOCKER[@]}" compose pull --quiet >/dev/null 2>&1; then
        return
    fi
    if [ -f Dockerfile ]; then
        say "Building the image locally. The first build takes a few minutes."
        "${DOCKER[@]}" compose build
        return
    fi
    die "Couldn't download $RESOLVED_IMAGE, and there's no Dockerfile here to build it from."
}

start_service() {
    "${DOCKER[@]}" compose up -d --remove-orphans
    say "Waiting for Iso-Vault to start..."
    local status
    for _ in $(seq 1 45); do
        status="$("${DOCKER[@]}" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' iso-vault 2>/dev/null || true)"
        case "$status" in
            healthy) return 0 ;;
            unhealthy|exited|dead) break ;;
        esac
        sleep 2
    done
    "${DOCKER[@]}" logs --tail 20 iso-vault >&2 || true
    die "Iso-Vault didn't start. The log above should say why."
}

secret_from_file() {
    "${DOCKER[@]}" exec iso-vault python3 -c \
        'import json; print(json.load(open("/data/secrets.json"))["ADMIN_PASSWORD"])' 2>/dev/null || true
}

firewall_notes() {
    local tcp="$BOOT_PORT"
    [ "$(env_value UI_BIND)" = 127.0.0.1 ] || tcp="$tcp $UI_PORT"
    tcp="$tcp 111 445 2049"
    local udp="69 111 2049"
    [ "$DHCP_MODE" = upstream ] || udp="67 $udp"
    [ "$DHCP_MODE" != proxy ] || udp="$udp 4011"
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        say ""
        warn "ufw is active and may block network boots. These commands open what Iso-Vault needs:"
        local port
        for port in $tcp; do say "  sudo ufw allow $port/tcp"; done
        for port in $udp; do say "  sudo ufw allow $port/udp"; done
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        say ""
        warn "firewalld is active and may block network boots. These commands open what Iso-Vault needs:"
        local port
        for port in $tcp; do say "  sudo firewall-cmd --permanent --add-port=$port/tcp"; done
        for port in $udp; do say "  sudo firewall-cmd --permanent --add-port=$port/udp"; done
        say "  sudo firewall-cmd --reload"
    fi
}

print_summary() {
    local password url
    password="$(env_value ADMIN_PASSWORD)"
    [ -n "$password" ] || password="$(secret_from_file)"
    url="$(env_value UI_PUBLIC_URL)"
    if [ "$PROXY_STATUS" != applied ] || [ -z "$url" ]; then
        url="http://$SERVER_IP:$UI_PORT"
    fi

    step "Iso-Vault is running"
    say "ISO folder:  $INSTALL_DIR/isos"
    say "Web UI:      $url"
    say "Password:    ${password:-see data/secrets.json}"
    say "DHCP mode:   $DHCP_MODE"

    case "$PROXY_STATUS" in
        applied)
            say ""
            say "Caddy now serves the web UI at https://$PROXY_HOST."
            is_local_name "$PROXY_HOST" && say "It uses Caddy's own local certificate, so browsers may ask you to trust it."
            ;;
        manual)
            say ""
            say "To serve the web UI at $PROXY_HOST, add this file to your $PROXY_KIND config:"
            say "  $PROXY_SNIPPET"
            say "See docs/reverse-proxy.md. Until then, the UI is at http://$SERVER_IP:$UI_PORT"
            if [ "$PROXY_WHERE" = host ]; then
                say "Once the proxy works, set UI_BIND=127.0.0.1 in .env to close the direct port."
            fi
            ;;
    esac

    case "$DHCP_MODE" in
        upstream)
            say ""
            say "Now set these in your router's DHCP settings:"
            say "  Next server (option 66):     $SERVER_IP"
            say "  BIOS clients (arch 0):       undionly.kpxe"
            say "  x64 UEFI (arch 7 and 9):     snponly-x64.efi"
            say "  ARM64 UEFI (arch 11):        snponly-arm64.efi"
            say "If your router only takes one filename, use the one for your clients, or switch to proxy mode."
            ;;
        server)
            say ""
            say "Iso-Vault is handing out IP addresses. Turn off any other DHCP server on this network."
            ;;
    esac

    firewall_notes

    say ""
    say "Next: copy an ISO into the folder above, then boot a device from the network."
    say "Boot clients with Secure Boot turned off."
}

PROXY_STATUS=""
PROXY_SNIPPET=""

main() {
    parse_args "$@"
    step "Checking this machine"
    check_platform
    check_docker
    prepare_dir
    choose_interface

    step "Getting the Iso-Vault image"
    resolve_image

    step "Choosing how to fit into your network"
    choose_mode
    set_env DHCP_MODE "$DHCP_MODE"
    set_env INTERFACE "$IFACE"
    choose_ports

    keep_or_set_env ADMIN_PASSWORD "$(generate_secret 12)"
    keep_or_set_env SESSION_SECRET "$(generate_secret 32)"
    keep_or_set_env SMB_PASSWORD "$(generate_secret 16)"

    configure_proxy_settings

    step "Starting Iso-Vault"
    start_service

    finish_proxy
    print_summary
}

main "$@"
