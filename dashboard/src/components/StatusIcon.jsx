import { CheckCircle, Circle, SpinnerGap, WarningCircle, XCircle } from "@phosphor-icons/react";

export function StatusIcon({ state, size = 22 }) {
  if (state === "done" || state === "success" || state === "reused") return <CheckCircle size={size} weight="fill" />;
  if (state === "skipped") return <Circle size={size} weight="fill" />;
  if (state === "active") return <SpinnerGap size={size} weight="bold" className="spin" />;
  if (state === "error") return <XCircle size={size} weight="fill" />;
  if (state === "warning") return <WarningCircle size={size} weight="fill" />;
  return <Circle size={size} weight="regular" />;
}
