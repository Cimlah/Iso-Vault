export type IsoStatus = "waiting" | "preparing" | "ready" | "error";

export interface Iso {
  id: string;
  name: string;
  relpath: string;
  group: string;
  size: number;
  status: IsoStatus;
  error: string;
  method: string;
  profile: string | null;
  profile_label: string | null;
  archs: string[];
  bootable: boolean;
  warnings: string[];
  drivers: string[];
}

export interface ServiceStatus {
  running: boolean;
  restarts: number;
  last_exit: number | null;
}

export interface Status {
  version: string;
  dhcp_mode: "proxy" | "server" | "upstream";
  configured_dhcp_mode: "proxy" | "server" | "upstream";
  server_ip: string;
  interface: string;
  boot_port: number;
  ui_port: number;
  features: string[];
  services: Record<string, ServiceStatus>;
  warnings: string[];
  upstream: { next_server: string; filenames: Record<string, string> };
}

export type PinMode = "once" | "always";

export interface Pin {
  iso_id: string;
  mode: PinMode;
  created: number;
}

export interface ClientRecord {
  arch: string;
  platform: string;
  ip: string;
  first_seen: number;
  last_seen: number;
  last_iso: string | null;
}

export interface ClientState {
  pins: Record<string, Pin>;
  clients: Record<string, ClientRecord>;
}

export interface Dashboard extends ClientState {
  status: Status;
  isos: Iso[];
}
