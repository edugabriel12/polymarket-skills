import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Play, Pause, RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import { pct, shortAddr, signedUsd } from "@/lib/utils";

export function WalletsTab() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const { data } = useQuery({ queryKey: ["wallets"], queryFn: api.wallets });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["wallets"] });
    qc.invalidateQueries({ queryKey: ["portfolio"] });
    qc.invalidateQueries({ queryKey: ["results"] });
    qc.invalidateQueries({ queryKey: ["entries"] });
  };

  const add = useMutation({
    mutationFn: () => api.addWallet(name.trim(), address.trim()),
    onSuccess: () => {
      setName("");
      setAddress("");
      setErr(null);
      refresh();
    },
    onError: (e: Error) => setErr(e.message),
  });

  const toggle = useMutation({
    mutationFn: (v: { id: number; active: boolean }) => api.toggleWallet(v.id, v.active),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.removeWallet(id),
    onSuccess: refresh,
  });
  const poll = useMutation({ mutationFn: () => api.poll(), onSuccess: refresh });

  const wallets = data?.wallets ?? [];

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Salvar carteira para copiar</CardTitle>
          <Button variant="outline" size="sm" onClick={() => poll.mutate()} disabled={poll.isPending}>
            <RefreshCw size={14} className={poll.isPending ? "animate-spin" : ""} />
            Verificar agora
          </Button>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-col gap-3 sm:flex-row">
            <input
              className="h-9 flex-1 rounded-xl border border-border bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-sky-500/40"
              placeholder="Nome (ex.: Whale #1)"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <input
              className="h-9 flex-[2] rounded-xl border border-border bg-background px-3 font-mono text-sm outline-none focus:ring-2 focus:ring-sky-500/40"
              placeholder="0x… (endereço público da carteira)"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
            />
            <Button
              onClick={() => add.mutate()}
              disabled={add.isPending || !name.trim() || !address.trim()}
            >
              <Plus size={16} />
              Salvar
            </Button>
          </div>
          {err && <div className="text-sm font-medium text-rose-400">{err}</div>}
          <p className="text-xs text-muted-foreground">
            Somente trades <b>posteriores</b> ao salvamento são copiados. Simulação em papel —
            não é recomendação financeira.
          </p>
        </CardContent>
      </Card>

      <div className="grid gap-3">
        {wallets.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              Nenhuma carteira salva ainda.
            </CardContent>
          </Card>
        )}
        {wallets.map((w) => (
          <Card key={w.wallet_id}>
            <CardContent className="flex flex-wrap items-center justify-between gap-3 py-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-bold">{w.name}</span>
                  <Badge tone={w.active ? "win" : "voidc"}>{w.active ? "ativa" : "pausada"}</Badge>
                </div>
                <div className="font-mono text-xs text-muted-foreground">{shortAddr(w.address)}</div>
              </div>
              <div className="flex items-center gap-6 text-sm">
                <Metric label="Entradas" value={String(w.n_entries)} />
                <Metric label="Executadas" value={pct(w.pct_executed)} />
                <Metric
                  label="P&L"
                  value={signedUsd(w.total_pnl)}
                  tone={w.total_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}
                />
                <div className="flex items-center gap-1.5">
                  <Button
                    variant="outline"
                    size="icon"
                    title={w.active ? "Pausar" : "Retomar"}
                    onClick={() => toggle.mutate({ id: w.wallet_id, active: !w.active })}
                  >
                    {w.active ? <Pause size={15} /> : <Play size={15} />}
                  </Button>
                  <Button
                    variant="outline"
                    size="icon"
                    title="Remover"
                    onClick={() => {
                      if (confirm(`Remover ${w.name}? Isso apaga suas entradas.`))
                        remove.mutate(w.wallet_id);
                    }}
                  >
                    <Trash2 size={15} />
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="text-right">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={`font-bold ${tone ?? ""}`}>{value}</div>
    </div>
  );
}
