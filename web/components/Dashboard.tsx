"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { MAC_PATTERN, formatArch, formatArchs, formatBytes, formatRelative } from "@/lib/format";
import type { ClientRecord, Dashboard as Data, Iso, Pin, PinMode, Status } from "@/lib/types";
import { Logo } from "./Logo";

const POLL_MS = 5000;
// Without a limit, a request to a server that vanished could hang for minutes with no sign of trouble.
const REQUEST_TIMEOUT_MS = 4000;

const MODE_NAMES: Record<Status["dhcp_mode"], string> = {
  proxy: "Proxy, alongside your DHCP",
  server: "Handing out addresses",
  upstream: "Your router points here",
};

const STATUS_NAMES: Record<Iso["status"], string> = {
  waiting: "Waiting for copy to finish",
  preparing: "Preparing",
  ready: "Ready",
  error: "Can't read",
};

async function send(url: string, method: string, body?: unknown): Promise<string | null> {
  try {
    const response = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (response.status === 401) {
      window.location.assign("/login");
      return null;
    }
    if (response.ok) return null;
    const payload = (await response.json().catch(() => ({}))) as { error?: string };
    return payload.error ?? "That didn't work.";
  } catch {
    return "Can't reach the server.";
  }
}

export function Dashboard({ initial }: { initial: Data | null }) {
  const [data, setData] = useState<Data | null>(initial);
  const [offline, setOffline] = useState(initial === null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/dashboard", {
        cache: "no-store",
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      if (!response.ok) {
        setOffline(true);
        return;
      }
      setData((await response.json()) as Data);
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, []);

  useEffect(() => {
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  async function logOut() {
    await send("/api/logout", "POST");
    window.location.assign("/login");
  }

  return (
    <main className="page">
      <header className="topbar">
        <div className="brand">
          <Logo />
          Iso-Vault
        </div>
        <button type="button" onClick={logOut}>
          Log out
        </button>
      </header>

      {offline && (
        <p className="alert danger" role="alert">
          Can&apos;t reach the Iso-Vault core. Retrying automatically. Check <span className="mono">docker logs iso-vault</span>{" "}
          if this doesn&apos;t clear.
        </p>
      )}

      {data && (
        <>
          <Facts status={data.status} />
          {data.status.warnings.map((warning) => (
            <p className="alert" role="status" key={warning}>
              {warning}
            </p>
          ))}
          <IsoPanel isos={data.isos} />
          <DevicePanel data={data} onChange={refresh} />
          {data.status.dhcp_mode === "upstream" && <UpstreamPanel status={data.status} />}
        </>
      )}
    </main>
  );
}

function Facts({ status }: { status: Status }) {
  const services = Object.values(status.services);
  const stopped = services.filter((service) => !service.running).length;
  return (
    <dl className="facts">
      <div className="fact">
        <dt>DHCP</dt>
        <dd>{MODE_NAMES[status.dhcp_mode]}</dd>
      </div>
      <div className="fact">
        <dt>Server address</dt>
        <dd className="mono">{status.server_ip}</dd>
      </div>
      <div className="fact">
        <dt>Interface</dt>
        <dd className="mono">{status.interface}</dd>
      </div>
      <div className="fact">
        <dt>Services</dt>
        <dd>
          <span className="badge">
            <span className={`dot ${stopped ? "bad" : "ok"}`} />
            {stopped ? `${stopped} of ${services.length} stopped` : `All ${services.length} running`}
          </span>
        </dd>
      </div>
    </dl>
  );
}

function IsoPanel({ isos }: { isos: Iso[] }) {
  return (
    <section className="panel" aria-labelledby="isos-heading">
      <div className="panel-heading">
        <h2 id="isos-heading">ISOs</h2>
        <span className="muted">{isos.length === 1 ? "1 ISO" : `${isos.length} ISOs`}</span>
      </div>
      {isos.length === 0 ? (
        <div className="empty">
          <h3>No ISOs yet</h3>
          <p className="muted">
            Copy ISO files into the <span className="mono">isos</span> folder on the server. They appear here within
            seconds.
          </p>
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">OS family</th>
                <th scope="col">Architecture</th>
                <th scope="col">Size</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {isos.map((iso) => (
                <IsoRow iso={iso} key={iso.id} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function IsoRow({ iso }: { iso: Iso }) {
  const tone = iso.status === "error" || (iso.status === "ready" && !iso.bootable) ? "bad" : iso.status === "ready" ? "ok" : "busy";
  const label = iso.status === "ready" && !iso.bootable ? "Can't boot" : STATUS_NAMES[iso.status];
  return (
    <tr>
      <td>
        <div className="name">{iso.relpath}</div>
        {iso.error && <div className="note error">{iso.error}</div>}
        {iso.warnings.map((warning) => (
          <div className="note" key={warning}>
            {warning}
          </div>
        ))}
        {iso.drivers.length > 0 && <div className="detail">Loads network drivers: {iso.drivers.join(", ")}</div>}
      </td>
      <td data-label="OS family">{iso.profile_label ?? "–"}</td>
      <td data-label="Architecture">{iso.status === "ready" ? formatArchs(iso.archs) : "–"}</td>
      <td data-label="Size" style={{ whiteSpace: "nowrap" }}>
        {formatBytes(iso.size)}
      </td>
      <td data-label="Status">
        <span className="badge">
          <span className={`dot ${tone}`} />
          {label}
        </span>
      </td>
    </tr>
  );
}

function DevicePanel({ data, onChange }: { data: Data; onChange: () => Promise<void> }) {
  const bootable = useMemo(() => data.isos.filter((iso) => iso.bootable), [data.isos]);
  const macs = useMemo(() => {
    const seen = (mac: string) => data.clients[mac]?.last_seen ?? 0;
    return [...new Set([...Object.keys(data.clients), ...Object.keys(data.pins)])].sort((a, b) => seen(b) - seen(a));
  }, [data.clients, data.pins]);

  return (
    <section className="panel" aria-labelledby="devices-heading">
      <div className="panel-heading">
        <h2 id="devices-heading">Devices</h2>
        <span className="muted">Pin an ISO so a device boots it without using the menu</span>
      </div>
      {macs.length === 0 ? (
        <div className="empty">
          <h3>No devices yet</h3>
          <p className="muted">Devices appear here after they boot from the network, or you can add one below.</p>
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Device</th>
                <th scope="col">Type</th>
                <th scope="col">Last seen</th>
                <th scope="col">Pinned ISO</th>
              </tr>
            </thead>
            <tbody>
              {macs.map((mac) => (
                <DeviceRow
                  key={mac}
                  mac={mac}
                  client={data.clients[mac]}
                  pin={data.pins[mac]}
                  isos={bootable}
                  allIsos={data.isos}
                  onChange={onChange}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <AddDevice isos={bootable} onChange={onChange} />
    </section>
  );
}

function DeviceRow(props: {
  mac: string;
  client?: ClientRecord;
  pin?: Pin;
  isos: Iso[];
  allIsos: Iso[];
  onChange: () => Promise<void>;
}) {
  const { mac, client, pin, isos, allIsos, onChange } = props;
  const [isoId, setIsoId] = useState(pin?.iso_id ?? "");
  const [mode, setMode] = useState<PinMode>(pin?.mode ?? "once");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // A `once` pin clears itself on the server when the device boots, so follow that here.
  useEffect(() => {
    setIsoId(pin?.iso_id ?? "");
    setMode(pin?.mode ?? "once");
  }, [pin?.iso_id, pin?.mode]);

  const lastIso = allIsos.find((iso) => iso.id === client?.last_iso);
  const pinMissing = pin !== undefined && !isos.some((iso) => iso.id === pin.iso_id);
  const unchanged = pin?.iso_id === isoId && pin?.mode === mode;

  async function apply(action: () => Promise<string | null>) {
    setBusy(true);
    setError((await action()) ?? "");
    await onChange();
    setBusy(false);
  }

  const save = (event: FormEvent) => {
    event.preventDefault();
    void apply(() => send(`/api/pins/${encodeURIComponent(mac)}`, "PUT", { iso_id: isoId, mode }));
  };
  const clear = () => void apply(() => send(`/api/pins/${encodeURIComponent(mac)}`, "DELETE"));

  return (
    <tr>
      <td>
        <div className="name mono">{mac}</div>
        {client?.ip && <div className="muted mono">{client.ip}</div>}
      </td>
      <td data-label="Type">
        {client ? `${formatArch(client.arch)} ${client.platform === "pcbios" ? "BIOS" : "UEFI"}` : "Not seen yet"}
      </td>
      <td data-label="Last seen" style={{ whiteSpace: "nowrap" }}>
        <span suppressHydrationWarning>{client ? formatRelative(client.last_seen) : "–"}</span>
        {lastIso && <div className="muted">Booted {lastIso.name}</div>}
      </td>
      <td>
        <form className="pin-form" onSubmit={save}>
          <select
            className="iso-select"
            aria-label={`ISO for ${mac}`}
            value={isoId}
            onChange={(event) => setIsoId(event.target.value)}
          >
            <option value="">Show the menu</option>
            {pinMissing && pin && <option value={pin.iso_id}>Unavailable ISO</option>}
            {isos.map((iso) => (
              <option key={iso.id} value={iso.id}>
                {iso.relpath}
              </option>
            ))}
          </select>
          <select
            aria-label={`Pin type for ${mac}`}
            value={mode}
            onChange={(event) => setMode(event.target.value as PinMode)}
          >
            <option value="once">Next boot only</option>
            <option value="always">Every boot</option>
          </select>
          {isoId ? (
            <button type="submit" className="primary" disabled={busy || unchanged}>
              {pin ? "Update" : "Pin"}
            </button>
          ) : null}
          {pin && (
            <button type="button" disabled={busy} onClick={clear}>
              Unpin
            </button>
          )}
          {pinMissing && <span className="form-error">The pinned ISO isn&apos;t available, so this device gets the menu.</span>}
          {error && (
            <span className="form-error" role="alert">
              {error}
            </span>
          )}
        </form>
      </td>
    </tr>
  );
}

function AddDevice({ isos, onChange }: { isos: Iso[]; onChange: () => Promise<void> }) {
  const [mac, setMac] = useState("");
  const [isoId, setIsoId] = useState("");
  const [mode, setMode] = useState<PinMode>("once");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const value = mac.trim();
    if (!MAC_PATTERN.test(value)) {
      setError("Enter a MAC address like 52:54:00:12:34:56.");
      return;
    }
    if (!isoId) {
      setError("Choose an ISO.");
      return;
    }
    setBusy(true);
    const failure = await send(`/api/pins/${encodeURIComponent(value)}`, "PUT", { iso_id: isoId, mode });
    setError(failure ?? "");
    if (!failure) {
      setMac("");
      setIsoId("");
      await onChange();
    }
    setBusy(false);
  }

  return (
    <form className="add-device" onSubmit={submit}>
      <label className="field">
        MAC address
        <input
          type="text"
          className="mono"
          placeholder="52:54:00:12:34:56"
          spellCheck={false}
          value={mac}
          onChange={(event) => setMac(event.target.value)}
        />
      </label>
      <label className="field">
        ISO
        <select className="iso-select" value={isoId} onChange={(event) => setIsoId(event.target.value)}>
          <option value="">Choose an ISO</option>
          {isos.map((iso) => (
            <option key={iso.id} value={iso.id}>
              {iso.relpath}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        Pin type
        <select value={mode} onChange={(event) => setMode(event.target.value as PinMode)}>
          <option value="once">Next boot only</option>
          <option value="always">Every boot</option>
        </select>
      </label>
      <button type="submit" disabled={busy}>
        Pin device
      </button>
      {error && (
        <span className="form-error" role="alert">
          {error}
        </span>
      )}
    </form>
  );
}

function UpstreamPanel({ status }: { status: Status }) {
  return (
    <section className="panel" aria-labelledby="upstream-heading">
      <div className="panel-heading">
        <h2 id="upstream-heading">Router settings</h2>
        <span className="muted">Enter these in your DHCP server</span>
      </div>
      <dl className="values">
        <dt>Next server (option 66)</dt>
        <dd>{status.upstream.next_server}</dd>
        {Object.entries(status.upstream.filenames).map(([client, filename]) => (
          <div key={client} style={{ display: "contents" }}>
            <dt>Boot file for {client}</dt>
            <dd>{filename}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
