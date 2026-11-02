export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < units.length - 1);
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

export function formatRelative(seconds: number, now: number = Date.now()): string {
  const elapsed = Math.max(0, Math.floor(now / 1000 - seconds));
  if (elapsed < 60) return "just now";
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)} min ago`;
  if (elapsed < 86400) return `${Math.floor(elapsed / 3600)} h ago`;
  return `${Math.floor(elapsed / 86400)} d ago`;
}

const ARCH_NAMES: Record<string, string> = { x86_64: "x64", arm64: "ARM64" };

export function formatArch(arch: string): string {
  return ARCH_NAMES[arch] ?? arch;
}

export function formatArchs(archs: string[]): string {
  return archs.length ? archs.map(formatArch).join(" + ") : "Unknown";
}

export const MAC_PATTERN = /^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$/i;
