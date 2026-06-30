import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Wallet, Trash2, Plus, FileText, AlertTriangle, SlidersHorizontal,
  ArrowLeft, ArrowRight, Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { KpiCards } from "@/components/KpiCards";
import { ConfidenceBreakdown } from "@/components/ConfidenceBreakdown";
import { CategoryBreakdown } from "@/components/CategoryBreakdown";
import { Charts } from "@/components/Charts";
import { DashBetList } from "@/components/DashBetList";
import { FilterSelector, fullSelection } from "@/components/FilterSelector";
import {
  wallets, uploadCsv, type WalletRecord, type WalletReport, type WalletFilters,
} from "@/lib/api";
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
        O CSV define estas faixas — e os resultados abaixo somam o CSV + as apostas ao vivo.
      </p>
    </Card>
  );
}

function WalletAnalysis({ rec }: { rec: WalletRecord }) {
  const a = rec.total_analysis ?? rec.analysis;   // Carteiras = TOTAL (CSV anexado + todas as ao vivo)
  return (
    <div className="space-y-6">
      <Thresholds rec={rec} />
      {a && a.n_markets > 0 ? (
        <>
          <div className="rounded-xl bg-sky-500/10 px-3 py-1.5 text-xs font-semibold text-sky-400">
            Total: CSV + ao vivo
            {a.live_settled != null && ` · ${a.live_settled} ao vivo liquidada(s)`}
            {!!a.live_open && ` · ${a.live_open} em aberto`}
          </div>
          <KpiCards overall={a.overall} nMarkets={a.n_markets} nTrades={a.n_trades} />
          {a.by_confidence && a.by_confidence.length > 0 && <ConfidenceBreakdown buckets={a.by_confidence} />}
          <Charts categories={a.by_category} />
          <CategoryBreakdown
            categories={a.by_category}
            renderBets={(cat) => (
              // The total aggregates CSV + live, but only LIVE bets are stored row-by-row, so the
              // drill lists the live bets (unfiltered) of this category; CSV bets stay in the totals.
              <DashBetList fetchKey={["wallet-bets-total", rec.id, cat]}
                fetcher={(p) => wallets.bets(rec.id, cat, p, 20, false)} />
            )}
          />
        </>
      ) : (
        <Card className="p-6 text-center text-sm text-muted-foreground">
          Sem resultados ainda.
          {!!a?.live_open && <div className="mt-1 text-xs">{a.live_open} aposta(s) em aberto.</div>}
        </Card>
      )}
    </div>
  );
}

// Read-only CSV analysis (Part 1) — same indicators as a wallet, no live-bets drill-in.
function CsvPreview({ report }: { report: WalletReport }) {
  return (
    <div className="space-y-6">
      <KpiCards overall={report.overall} nMarkets={report.n_markets} nTrades={report.n_trades} />
      {report.by_confidence && report.by_confidence.length > 0 && (
        <ConfidenceBreakdown buckets={report.by_confidence} />
      )}
      <Charts categories={report.by_category} />
      <CategoryBreakdown categories={report.by_category} />
    </div>
  );
}

function StepDot({ n, active, done, label }: {
  n: number; active?: boolean; done?: boolean; label: string;
}) {
  return (
    <span className={cn("flex items-center gap-2", active ? "text-foreground" : "text-muted-foreground")}>
      <span className={cn("flex h-6 w-6 items-center justify-center rounded-full text-xs font-bold",
        active ? "bg-sky-500 text-white" : done ? "bg-emerald-500 text-white" : "bg-muted")}>
        {n}
      </span>
      <span className="hidden sm:inline">{label}</span>
    </span>
  );
}

// Part 1 (analyze) + Part 2 (configure name/address + forwarding filter) wizard.
function AddWizard({ onDone, onCancel }: { onDone: (id: number) => void; onCancel: () => void }) {
  const [step, setStep] = useState<"analyze" | "configure">("analyze");
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<WalletReport | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeErr, setAnalyzeErr] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [selection, setSelection] = useState<WalletFilters>({});
  const [busy, setBusy] = useState(false);
  const [addErr, setAddErr] = useState<string | null>(null);

  const onPick = async (f: File | null) => {
    setFile(f); setReport(null); setAnalyzeErr(null);
    if (!f) return;
    setAnalyzing(true);
    try {
      const rep = await uploadCsv(f);
      if (rep.error) setAnalyzeErr(rep.error);
      else setReport(rep);
    } catch (e) {
      setAnalyzeErr((e as Error).message);
    } finally {
      setAnalyzing(false);
    }
  };

  const proceed = () => {
    if (!report) return;
    setSelection(fullSelection(report.filter_tree ?? {}));   // pre-select everything
    setStep("configure");
  };

  const submit = async () => {
    if (!name.trim() || !address.trim() || !file) { setAddErr("Preencha nome e endereço."); return; }
    setBusy(true); setAddErr(null);
    try {
      const created = await wallets.add(name.trim(), address.trim(), file, selection);
      const c = created as WalletRecord & { error?: string };
      if (c?.error) setAddErr(c.error);
      else onDone(c.id);
    } catch (e) {
      setAddErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <StepDot n={1} active={step === "analyze"} done={step === "configure"} label="Análise do CSV" />
          <div className="h-px w-6 bg-border sm:w-10" />
          <StepDot n={2} active={step === "configure"} label="Adicionar carteira" />
        </div>
        <Button variant="ghost" onClick={onCancel}><ArrowLeft className="h-4 w-4" /> Cancelar</Button>
      </div>

      {step === "analyze" && (
        <div className="space-y-4">
          <Card className="space-y-3 p-4">
            <div className="flex items-center gap-2 text-sm font-bold">
              <FileText className="h-4 w-4" /> CSV de histórico da carteira
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={analyzing}>
                <FileText className="h-4 w-4" /> {file ? file.name : "Selecionar CSV"}
              </Button>
              <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden"
                onChange={(e) => onPick(e.target.files?.[0] ?? null)} />
              {analyzing && (
                <span className="flex items-center gap-1 text-xs text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" /> analisando…
                </span>
              )}
            </div>
            <p className="text-[11px] text-muted-foreground">
              Cabeçalho esperado: Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro
            </p>
            {analyzeErr && (
              <div className="flex items-center gap-2 text-xs text-rose-500">
                <AlertTriangle className="h-4 w-4" /> {analyzeErr}
              </div>
            )}
          </Card>

          {report && (
            <>
              <CsvPreview report={report} />
              <div className="flex justify-end">
                <Button onClick={proceed}>Prosseguir <ArrowRight className="h-4 w-4" /></Button>
              </div>
            </>
          )}
        </div>
      )}

      {step === "configure" && report && (
        <div className="space-y-4">
          <Card className="space-y-3 p-4">
            <div className="flex items-center gap-2 text-sm font-bold"><Wallet className="h-4 w-4" /> Dados da carteira</div>
            <div className="grid gap-2 sm:grid-cols-2">
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Nome (ex.: Oneger)"
                className="h-10 rounded-xl border border-border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-sky-500/40" />
              <input value={address} onChange={(e) => setAddress(e.target.value)} placeholder="Endereço (0x…)" spellCheck={false}
                className="h-10 rounded-xl border border-border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-sky-500/40" />
            </div>
            <p className="text-[11px] text-muted-foreground">
              O CSV deriva as faixas de confiança→unidade; o endereço é vigiado ao vivo.
            </p>
          </Card>

          <Card className="space-y-3 p-4">
            <div className="flex items-center gap-2 text-sm font-bold">
              <SlidersHorizontal className="h-4 w-4" /> Filtro de envio (Sports/Telegram)
            </div>
            <FilterSelector tree={report.filter_tree ?? {}} value={selection} onChange={setSelection} />
          </Card>

          {addErr && (
            <div className="flex items-center gap-2 text-xs text-rose-500">
              <AlertTriangle className="h-4 w-4" /> {addErr}
            </div>
          )}
          <div className="flex justify-between">
            <Button variant="outline" onClick={() => setStep("analyze")}><ArrowLeft className="h-4 w-4" /> Voltar</Button>
            <Button onClick={submit} disabled={busy}>
              <Plus className="h-4 w-4" /> {busy ? "Adicionando…" : "Adicionar carteira"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// Edit a saved wallet's forwarding filter in place (preserves its live history).
function EditFilters({ id, onDone, onCancel }: {
  id: number; onDone: () => void; onCancel: () => void;
}) {
  const { data: rec, isLoading } = useQuery({ queryKey: ["wallet", id], queryFn: () => wallets.get(id) });
  const [selection, setSelection] = useState<WalletFilters | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ready = rec && !("error" in rec);
  const tree = (ready ? rec.filter_tree : undefined) ?? {};
  // Seed from the wallet's stored filters; null (legacy = forward all) → everything selected.
  const value = selection ?? (ready ? (rec.filters ?? fullSelection(tree)) : {});

  const save = async () => {
    setBusy(true); setErr(null);
    try {
      await wallets.updateFilters(id, value);
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-bold">
          <SlidersHorizontal className="h-4 w-4" /> Editar filtro de envio{ready ? ` · ${rec.name}` : ""}
        </div>
        <Button variant="ghost" onClick={onCancel}><ArrowLeft className="h-4 w-4" /> Cancelar</Button>
      </div>
      {isLoading && <div className="text-sm text-muted-foreground">Carregando…</div>}
      {ready && (
        <>
          <Card className="p-4">
            <FilterSelector tree={tree} value={value} onChange={setSelection} />
          </Card>
          {err && (
            <div className="flex items-center gap-2 text-xs text-rose-500">
              <AlertTriangle className="h-4 w-4" /> {err}
            </div>
          )}
          <div className="flex justify-end">
            <Button onClick={save} disabled={busy}>{busy ? "Salvando…" : "Salvar filtro"}</Button>
          </div>
        </>
      )}
    </div>
  );
}

export function CarteirasTab() {
  const qc = useQueryClient();
  const [view, setView] = useState<"list" | "add" | "edit">("list");
  const [selected, setSelected] = useState<number | null>(null);

  const { data: list = [] } = useQuery({ queryKey: ["wallets"], queryFn: () => wallets.list() });
  const { data: rec } = useQuery({
    queryKey: ["wallet", selected],
    queryFn: () => wallets.get(selected!),
    enabled: selected != null && view === "list",
  });

  const remove = async (id: number) => {
    await wallets.remove(id);
    if (selected === id) setSelected(null);
    qc.invalidateQueries({ queryKey: ["wallets"] });
  };

  if (view === "add") {
    return (
      <AddWizard
        onCancel={() => setView("list")}
        onDone={(id) => { setView("list"); setSelected(id); qc.invalidateQueries({ queryKey: ["wallets"] }); }}
      />
    );
  }
  if (view === "edit" && selected != null) {
    return (
      <EditFilters
        id={selected}
        onCancel={() => setView("list")}
        onDone={() => {
          setView("list");
          qc.invalidateQueries({ queryKey: ["wallets"] });
          qc.invalidateQueries({ queryKey: ["wallet", selected] });
        }}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-bold uppercase tracking-wider text-muted-foreground">Carteiras vigiadas</h2>
        <Button onClick={() => setView("add")}><Plus className="h-4 w-4" /> Nova carteira</Button>
      </div>

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
              {!!w.n_markets && (
                <span className="flex items-center gap-1.5 text-[11px] font-semibold">
                  <span className="text-muted-foreground">{w.n_markets} ap.</span>
                  {w.win_rate != null && (
                    <span className="text-muted-foreground">{Math.round(w.win_rate * 100)}%</span>
                  )}
                  {w.total_pnl != null && (
                    <span className={w.total_pnl >= 0 ? "text-emerald-500" : "text-rose-500"}>
                      {w.total_pnl >= 0 ? "+" : "−"}{usd0(Math.abs(w.total_pnl))}
                    </span>
                  )}
                </span>
              )}
            </button>
            {w.filters && (
              <span title="Filtro de envio ativo" className="rounded-full bg-sky-500/15 px-1.5 py-0.5 text-[10px] font-bold text-sky-400">
                filtro
              </span>
            )}
            <button onClick={() => { setSelected(w.id); setView("edit"); }} aria-label="Editar filtros"
              className="text-muted-foreground hover:text-sky-500">
              <SlidersHorizontal className="h-3.5 w-3.5" />
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
