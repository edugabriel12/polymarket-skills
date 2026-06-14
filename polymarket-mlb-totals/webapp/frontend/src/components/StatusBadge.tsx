import { Badge } from "@/components/ui/badge";
import { CheckCircle2, XCircle, Clock, MinusCircle } from "lucide-react";

const MAP: Record<string, { tone: "win" | "loss" | "pending" | "voidc"; icon: React.ElementType; label: string }> = {
  ACERTO: { tone: "win", icon: CheckCircle2, label: "ACERTO" },
  ERRO: { tone: "loss", icon: XCircle, label: "ERRO" },
  PENDENTE: { tone: "pending", icon: Clock, label: "PENDENTE" },
  ANULADO: { tone: "voidc", icon: MinusCircle, label: "ANULADO" },
};

export function StatusBadge({ status }: { status?: string }) {
  const s = MAP[status || "PENDENTE"] ?? MAP.PENDENTE;
  const Icon = s.icon;
  return (
    <Badge tone={s.tone}>
      <Icon className="h-3 w-3" />
      {s.label}
    </Badge>
  );
}
