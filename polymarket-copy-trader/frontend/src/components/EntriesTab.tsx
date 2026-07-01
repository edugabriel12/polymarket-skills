import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { EntryList } from "@/components/EntryList";
import { Button } from "@/components/ui/button";
import { useState } from "react";

export function EntriesTab() {
  const [wallet, setWallet] = useState<number | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const { data } = useQuery({ queryKey: ["wallets"], queryFn: api.wallets });

  const chip = (active: boolean) =>
    active ? "default" : ("outline" as "default" | "outline");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase text-muted-foreground">Carteira:</span>
        <Button size="sm" variant={chip(wallet === null)} onClick={() => setWallet(null)}>
          Todas
        </Button>
        {(data?.wallets ?? []).map((w) => (
          <Button key={w.wallet_id} size="sm" variant={chip(wallet === w.wallet_id)} onClick={() => setWallet(w.wallet_id)}>
            {w.name}
          </Button>
        ))}
        <span className="ml-4 text-xs font-semibold uppercase text-muted-foreground">Status:</span>
        {["Todas", "EXECUTED", "SKIPPED"].map((s) => {
          const val = s === "Todas" ? null : s;
          return (
            <Button key={s} size="sm" variant={chip(status === val)} onClick={() => setStatus(val)}>
              {s === "EXECUTED" ? "Executadas" : s === "SKIPPED" ? "Falhas" : "Todas"}
            </Button>
          );
        })}
      </div>

      <EntryList
        queryKey={["entries", wallet, status]}
        fetcher={(page) => api.entries(wallet, status, page)}
        showWallet
        emptyText="Nenhuma entrada ainda. Salve uma carteira e aguarde novos trades."
      />
    </div>
  );
}
