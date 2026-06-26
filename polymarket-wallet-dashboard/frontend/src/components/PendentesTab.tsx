import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Cpu, Wallet } from "lucide-react";
import { Card } from "@/components/ui/card";
import { DashBetList } from "@/components/DashBetList";
import { wallets } from "@/lib/api";
import { cn } from "@/lib/utils";

// Apostas ainda EM ABERTO (não liquidadas): predições PENDENTE do modelo e
// posições OPEN das carteiras vigiadas. Mesma lista paginada dos Resultados,
// no modo "open" (sem resultado/P&L — só o que está em jogo agora).
export function PendentesTab() {
  const { data: list = [] } = useQuery({ queryKey: ["wallets"], queryFn: () => wallets.list() });
  const [entity, setEntity] = useState<"model" | number>("model");

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => setEntity("model")}
          className={cn(
            "flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-sm font-semibold transition",
            entity === "model" ? "border-violet-500 bg-violet-500/10" : "border-border bg-card hover:bg-muted",
          )}
        >
          <Cpu className="h-4 w-4" /> Modelo
        </button>
        {list.map((w) => (
          <button
            key={w.id}
            onClick={() => setEntity(w.id)}
            className={cn(
              "flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-sm font-semibold transition",
              entity === w.id ? "border-sky-500 bg-sky-500/10" : "border-border bg-card hover:bg-muted",
            )}
          >
            <Wallet className="h-4 w-4" /> {w.name}
          </button>
        ))}
      </div>

      <Card className="p-4">
        {entity === "model" ? (
          <DashBetList mode="open" fetchKey={["model-open"]} fetcher={(p) => wallets.modelOpenBets(p)} />
        ) : (
          <DashBetList mode="open" fetchKey={["wallet-open", entity]} fetcher={(p) => wallets.openBets(entity, p)} />
        )}
      </Card>
    </div>
  );
}
