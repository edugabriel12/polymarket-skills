import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { RefreshCw, Database, CalendarDays, Inbox, BadgeInfo } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { PredictionCard } from "@/components/PredictionCard";

// Until next UTC midnight — matches the backend's once-per-day cache.
function msUntilEndOfDay() {
  const now = new Date();
  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
  return end.getTime() - now.getTime();
}

export function AnalysesTab() {
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["analyses"],
    queryFn: () => api.analyses(),
    staleTime: msUntilEndOfDay(),
    refetchOnWindowFocus: false,
  });

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <CalendarDays className="h-4 w-4" />
          <span>Previsões de {data?.date ?? "—"}</span>
          {data && (
            <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-xs font-semibold">
              <Database className="h-3 w-3" />
              {data.cached ? "cache do dia" : "calculado agora"}
            </span>
          )}
        </div>
        <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
          <RefreshCw className={isFetching ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
          Recalcular
        </Button>
      </div>

      {isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="skeleton h-56" />
          ))}
        </div>
      ) : data && data.suggestions.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.suggestions.map((s) => (
            <PredictionCard key={s.prediction_id ?? s.game} s={s} />
          ))}
        </div>
      ) : (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
          <Card className="flex flex-col items-center gap-3 p-10 text-center">
            <div className="rounded-2xl bg-gradient-to-br from-sky-500/20 to-violet-500/20 p-4">
              <Inbox className="h-8 w-8 text-violet-400" />
            </div>
            <div className="text-lg font-bold">Nenhuma entrada acionável hoje</div>
            <p className="max-w-md text-sm text-muted-foreground">
              O modelo não encontrou edge dentro do filtro 1.60x–3.0x — ou os jogos do dia ainda não
              estão disponíveis (requer conexão de rede com a Polymarket). Sessão vazia é normal.
            </p>
          </Card>
        </motion.div>
      )}

      {data && data.skipped.length > 0 && (
        <Card className="p-4">
          <div className="mb-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-muted-foreground">
            <BadgeInfo className="h-3.5 w-3.5" /> {data.skipped.length} jogos ignorados
          </div>
          <div className="flex flex-wrap gap-2">
            {data.skipped.slice(0, 12).map((sk, i) => (
              <span key={i} className="rounded-lg bg-muted px-2 py-1 text-xs text-muted-foreground">
                {sk.game.replace(/^mlb-/, "")}: {sk.reason}
              </span>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
