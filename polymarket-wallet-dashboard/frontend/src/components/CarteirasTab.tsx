import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Wallet, Trash2, Plus, FileText, AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { KpiCards } from "@/components/KpiCards";
import { ConfidenceBreakdown } from "@/components/ConfidenceBreakdown";
import { CategoryBreakdown } from "@/components/CategoryBreakdown";
import { Charts } from "@/components/Charts";
import { wallets, type WalletRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

const UNIT_LABEL: Record<number, string> = { 1: "1U", 0.5: "0.5U", 0.25: "0.25U" };
const usd0 = (v: number) => `$${Math.round(v).toLocaleString("en-US")}`;

function Thresholds({ rec }: { rec: WalletRecord }) {
  const order = ["Alta", "Média", "Baixa"];
  const bands = order.filter((t) => rec.thresholds?.[t]);
  if (!bands.length) return null;
  return (
    <Card className="p-4">
      <div className="mb-2 text-xs font-bold uppercase tracking-wider text-muted-foreground">
        Faixas aprendidas do CSV (confiança → unidade)
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        {bands.map((t) => {
          const b = rec.thresholds[t];
          return (
            <div key={t} className="rounded-xl bg-muted/50 px-3 py-2">
              <div className="text-sm font-black">{t} · {UNIT_LABEL[b.unit] ?? `${b.unit}U`}</div>
              <div className="text-xs text-muted-foreground">posição ≥ {usd0(b.floor)}</div>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-[11px] text-muted-foreground">
        O CSV serve só pra estas faixas — os resultados abaixo são só das apostas ao vivo desde a adição.
      </p>
    </Card>
  );
}

function WalletAnalysis({ rec }: { rec: WalletRecord }) {
  const a = rec.analysis;
  return (
    <div className="space-y-6">
      <Thresholds rec={rec} />
      {a && a.n_markets > 0 ? (
        <>
          <div className="rounded-xl bg-sky-500/10 px-3 py-1.5 text-xs font-semibold text-sky-400">
            Apostas ao vivo · {a.live_settled} liquidada(s)
            {!!a.live_open && ` · ${a.live_open} em aberto`}
          </div>
          <KpiCards overall={a.overall} nMarkets={a.n_markets} nTrades={a.n_trades} />
          {a.by_confidence && a.by_confidence.length > 0 && <ConfidenceBreakdown buckets={a.by_confidence} />}
          <Charts categories={a.by_category} />
          <CategoryBreakdown categories={a.by_category} />
        </>
      ) : (
        <Card className="p-6 text-center text-sm text-muted-foreground">
          Ainda sem apostas liquidadas desde a adição.
          {!!a?.live_open && <div className="mt-1 text-xs">{a.live_open} aposta(s) em aberto.</div>}
        </Card>
      )}
    </div>
  );
}

export function CarteirasTab() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  const { data: list = [] } = useQuery({ queryKey: ["wallets"], queryFn: () => wallets.list() });
  const { data: rec } = useQuery({
    queryKey: ["wallet", selected],
    queryFn: () => wallets.get(selected!),
    enabled: selected != null,
  });

  const add = async () => {
    if (!name.trim() || !address.trim() || !file) {
      setErr("Preencha nome, endereço e selecione o CSV.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const created = await wallets.add(name.trim(), address.trim(), file);
      setName(""); setAddress(""); setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      await qc.invalidateQueries({ queryKey: ["wallets"] });
      if ((created as WalletRecord)?.id) setSelected((created as WalletRecord).id);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: number) => {
    await wallets.remove(id);
    if (selected === id) setSelected(null);
    qc.invalidateQueries({ queryKey: ["wallets"] });
  };

  return (
    <div className="space-y-6">
      {/* Add wallet */}
      <Card className="space-y-3 p-4">
        <div className="flex items-center gap-2 text-sm font-bold"><Plus className="h-4 w-4" /> Adicionar carteira vigiada</div>
        <div className="grid gap-2 sm:grid-cols-2">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Nome (ex.: Oneger)"
            className="h-10 rounded-xl border border-border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-sky-500/40" />
          <input value={address} onChange={(e) => setAddress(e.target.value)} placeholder="Endereço (0x…)" spellCheck={false}
            className="h-10 rounded-xl border border-border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-sky-500/40" />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={busy}>
            <FileText className="h-4 w-4" /> {file ? file.name : "CSV de histórico"}
          </Button>
          <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          <Button onClick={add} disabled={busy}><Plus className="h-4 w-4" /> Adicionar</Button>
        </div>
        <p className="text-[11px] text-muted-foreground">
          O CSV deriva as faixas de confiança→unidade; o endereço é vigiado ao vivo.
        </p>
        {err && (
          <div className="flex items-center gap-2 text-xs text-rose-500"><AlertTriangle className="h-4 w-4" /> {err}</div>
        )}
      </Card>

      {/* Wallet list */}
      <div className="flex flex-wrap gap-2">
        {list.length === 0 && <span className="text-sm text-muted-foreground">Nenhuma carteira adicionada.</span>}
        {list.map((w) => (
          <div key={w.id}
            className={cn("flex items-center gap-2 rounded-xl border px-3 py-1.5 text-sm transition",
              selected === w.id ? "border-sky-500 bg-sky-500/10" : "border-border bg-card hover:bg-muted")}>
            <button onClick={() => setSelected(w.id)} className="flex items-center gap-2 font-semibold">
              <Wallet className="h-4 w-4" /> {w.name}
              <span className="font-mono text-[11px] text-muted-foreground">
                {w.address.slice(0, 6)}…{w.address.slice(-4)}
              </span>
            </button>
            <button onClick={() => remove(w.id)} aria-label="Remover" className="text-muted-foreground hover:text-rose-500">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>

      {selected != null && rec && !("error" in rec) && <WalletAnalysis rec={rec} />}
    </div>
  );
}
