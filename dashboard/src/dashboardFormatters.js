export const DEFAULT_TERMINOLOGY = {
  action: "deploy",
  in_progress: "deploying",
  completed: "deployed",
  noun: "deployment",
};

export function terminology(snapshot) {
  return snapshot.project.terminology || DEFAULT_TERMINOLOGY;
}

export function parseTime(value) {
  const time = value ? Date.parse(value) : Number.NaN;
  return Number.isNaN(time) ? null : time;
}

export function duration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
  return `${secs}s`;
}

export function relative(value, now) {
  const time = parseTime(value);
  if (!time) return "—";
  const delta = Math.round((now - time) / 1000);
  if (Math.abs(delta) < 2) return "just now";
  if (delta < 0) return `in ${duration(-delta)}`;
  return `${duration(delta)} ago`;
}

export function clockTime(value) {
  const time = value instanceof Date ? value.getTime() : parseTime(value);
  if (!time) return "--:--:--";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(time);
}

export function dateTime(value) {
  const time = parseTime(value);
  if (!time) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(time);
}

export function shortSha(value) {
  return value ? value.slice(0, 7) : "pending";
}
