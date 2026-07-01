import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, ExternalLink } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { Entry, EntriesPage } from "@/lib/api";
import { pct, usd, signedUsd, shortAddr, cn } from "@/lib/utils";

// LIVE result cell — the core of the "Entradas" requirement.
function ResultCell({ e }: { e: Entry }) {
  if (e.status === "SKIPPED") {
    return (
      <div className="text-right">
        <Badge tone="voidc">não executada</Badge>
        <div className="mt-0.5 max-w-[220px] truncate text-xs text-muted-foreground" title={e.skip_reason ?? ""}>
          {e.skip_reason}
        </div>
      </div>
    );
  }
  if (e.result_status === "WIN")
    return (
      <div className="text-right">
        <Badge tone="win">Acerto</Badge>
        <div className="mt-0.5 text-xs font-semibold text-emerald-400">{signedUsd(e.realized_pnl ?? 0)}</div>
      </div>
    );
  if (e.result_status === "LOSS")
    return (
      <div className="text-right">
        <Badge tone="loss">Erro</Badge>
        <div className="mt-0.5 text-xs font-semibold text-rose-400">{signedUsd(e.realized_pnl ?? 0)}</div>
      </div>
    );
  if (e.result_status === "VOID") return <Badge tone="voidc">anulado</Badge>;
  // OPEN — show live price and, for buys, unrealized vs fill.
  const unreal =
    e.copy_action === "BUY" && e.current_price != null && e.avg_fill_price != null && e.shares != null
      ? (e.current_price - e.avg_fill_price) * e.shares
      : null;
  return (
    <div className="text-right">
      <Badge tone="pending">ao vivo</Badge>
      <div className="mt-0.5 text-xs text-muted-foreground">
        preço {e.current_price != null ? e.current_price.toFixed(3) : "—"}
        {unreal != null && (
          <span className={cn("ml-1 font-semibold", unreal >= 0 ? "text-emerald-400" : "text-rose-400")}>
            {signedUsd(unreal)}
          </span>
        )}
      </div>
    </div>
  );
}

function Row({ e, showWallet }: { e: Entry; showWallet: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl bg-muted/40 px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <Badge tone={e.copy_action === "BUY" ? "over" : "under"}>{e.copy_action === "BUY" ? "compra" : "venda"}</Badge>
          <span className="truncate font-semibold" title={e.market_question ?? ""}>
            {e.market_question || e.condition_id}
          </span>
          {e.market_url && (
            <a href={e.market_url} target="_blank" rel="noreferrer" className="text-muted-foreground hover:text-sky-400">
              <ExternalLink size={13} />
            </a>
          )}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
          {showWallet && <span className="font-medium text-foreground/80">{e.wallet_name ?? shortAddr(e.wallet_address ?? "")}</span>}
          <span>slippage {pct(e.slippage_pct)}</span>
          {e.volume_24h != null && e.volume_24h > 0 && <span>vol24h {usd(e.volume_24h)}</span>}
          <span>{new Date(e.created_at + "Z").toLocaleString("pt-BR")}</span>
        </div>
      </div>
      <div className="w-24 text-right">
        <div className="text-[11px] uppercase tracking-wide text-muted-foreground">Valor</div>
        <div className="font-bold">{usd(e.executed_usd ?? e.requested_usd)}</div>
      </div>
      <div className="w-40">
        <ResultCell e={e} />
      </div>
    </div>
  );
}

export function EntryList({
  queryKey,
  fetcher,
  showWallet = true,
  emptyText = "Nenhuma entrada.",
}: {
  queryKey: (number | string | null)[];
  fetcher: (page: number) => Promise<EntriesPage>;
  showWallet?: boolean;
  emptyText?: string;
}) {
  const [page, setPage] = useState(1);
  const { data, isLoading } = useQuery({
    queryKey: [...queryKey, page],
    queryFn: () => fetcher(page),
    placeholderData: keepPreviousData,
  });

  const total = data?.total ?? 0;
  const pageSize = data?.page_size ?? 20;
  const maxPage = Math.max(1, Math.ceil(total / pageSize));
  const entries = data?.entries ?? [];

  return (
    <Card>
      <CardContent className="space-y-2 py-4">
        {isLoading && !data ? (
          <div className="py-8 text-center text-sm text-muted-foreground">Carregando…</div>
        ) : entries.length === 0 ? (
          <div className="py-8 text-center text-sm text-muted-foreground">{emptyText}</div>
        ) : (
          entries.map((e) => <Row key={e.id} e={e} showWallet={showWallet} />)
        )}
        {total > pageSize && (
          <div className="flex items-center justify-between pt-2">
            <span className="text-xs text-muted-foreground">
              {total} entradas · página {page}/{maxPage}
            </span>
            <div className="flex gap-1.5">
              <Button variant="outline" size="icon" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                <ChevronLeft size={16} />
              </Button>
              <Button variant="outline" size="icon" disabled={page >= maxPage} onClick={() => setPage((p) => p + 1)}>
                <ChevronRight size={16} />
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
