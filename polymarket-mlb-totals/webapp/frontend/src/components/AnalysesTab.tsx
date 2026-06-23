import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  RefreshCw,
  Clock,
  CalendarDays,
  Inbox,
  BadgeInfo,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { api, type Sport } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { PredictionCard } from "@/components/PredictionCard";
import { cn } from "@/lib/utils";

// Until next UTC midnight — matches the backend's once-per-day cache.
function msUntilEndOfDay() {
  const now = new Date();
  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
  return end.getTime() - now.getTime();
}

export function AnalysesTab({ sport }: { sport: Sport }) {
  // Read-only: the backend recomputes automatically ~10 min before each game (per-game
  // "waves"), so the page just serves the day's cache. No manual recalc button.
  const { data, isLoading } = useQuery({
    queryKey: ["analyses", sport],
    queryFn: () => api.analyses(sport),
    staleTime: msUntilEndOfDay(),
    refetchOnWindowFocus: false,
  });

  // Live schedule for the "next recalc" indicator (the loop reports next_wave to /api/health).
  const { data: health } = useQuery({
    queryKey: ["health"],
    queryFn: () => api.health(),
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
  });
  const nextWave = health?.sharp_close?.next_wave;
  const hhmm = (iso: string) =>
    new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const items = data?.suggestions ?? [];
  const n = items.length;
  const [index, setIndex] = useState(0);
  const [dir, setDir] = useState(0);

  // Reset to the first card whenever the data changes.
  useEffect(() => setIndex(0), [data]);

  const go = (d: number) => {
    if (n === 0) return;
    setDir(d);
    setIndex((i) => (i + d + n) % n);
  };

  // Keyboard navigation.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "ArrowLeft") go(-1);
      if (e.key === "ArrowRight") go(1);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [n]);

  const current = items[Math.min(index, Math.max(0, n - 1))];

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <CalendarDays className="h-4 w-4" />
          <span>Previsões de {data?.date ?? "—"}</span>
          {n > 0 && (
            <span className="rounded-full bg-gradient-to-r from-sky-500/20 to-violet-500/20 px-2 py-0.5 text-xs font-bold">
              {n} {n === 1 ? "entrada" : "entradas"}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          {data?.computed_at && (
            <span className="inline-flex items-center gap-1" title="Última atualização automática">
              <RefreshCw className="h-3 w-3" />
              auto · {hhmm(data.computed_at)}
            </span>
          )}
          <span
            className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 font-semibold"
            title="O modelo recalcula automaticamente ~10 min antes de cada jogo"
          >
            <Clock className="h-3 w-3" />
            {nextWave ? `próximo recálculo · ${hhmm(nextWave)}` : "sem mais recálculos hoje"}
          </span>
        </div>
      </div>

      {isLoading ? (
        <div className="skeleton mx-auto h-72 max-w-xl" />
      ) : n > 0 && current ? (
        <div className="relative px-0 sm:px-12">
          {/* Carousel viewport */}
          <div className="overflow-hidden">
            <AnimatePresence mode="wait" custom={dir}>
              <motion.div
                key={index}
                custom={dir}
                initial={{ opacity: 0, x: dir >= 0 ? 80 : -80 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: dir >= 0 ? -80 : 80 }}
                transition={{ duration: 0.25 }}
                drag="x"
                dragConstraints={{ left: 0, right: 0 }}
                dragElastic={0.2}
                onDragEnd={(_e, info) => {
                  if (info.offset.x < -80) go(1);
                  else if (info.offset.x > 80) go(-1);
                }}
                className="mx-auto max-w-xl cursor-grab active:cursor-grabbing"
              >
                <PredictionCard s={current} />
              </motion.div>
            </AnimatePresence>
          </div>

          {/* Prev / Next */}
          {n > 1 && (
            <>
              <button
                onClick={() => go(-1)}
                aria-label="Anterior"
                className="absolute left-0 top-[45%] -translate-y-1/2 rounded-full border border-border bg-card/90 p-2 shadow-lg backdrop-blur transition hover:bg-muted"
              >
                <ChevronLeft className="h-5 w-5" />
              </button>
              <button
                onClick={() => go(1)}
                aria-label="Próximo"
                className="absolute right-0 top-[45%] -translate-y-1/2 rounded-full border border-border bg-card/90 p-2 shadow-lg backdrop-blur transition hover:bg-muted"
              >
                <ChevronRight className="h-5 w-5" />
              </button>
            </>
          )}

          {/* Dots + counter */}
          {n > 1 && (
            <>
              <div className="mt-4 flex flex-wrap items-center justify-center gap-1.5">
                {items.map((_, i) => (
                  <button
                    key={i}
                    onClick={() => {
                      setDir(i > index ? 1 : -1);
                      setIndex(i);
                    }}
                    aria-label={`Ir para ${i + 1}`}
                    className={cn(
                      "h-2 rounded-full transition-all",
                      i === index
                        ? "w-5 bg-gradient-to-r from-sky-500 to-violet-500"
                        : "w-2 bg-muted hover:bg-border"
                    )}
                  />
                ))}
              </div>
              <div className="mt-1 text-center text-xs font-medium text-muted-foreground">
                {index + 1} / {n} · arraste, use as setas ◀ ▶ ou o teclado
              </div>
            </>
          )}
        </div>
      ) : (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
          <Card className="flex flex-col items-center gap-3 p-10 text-center">
            <div className="rounded-2xl bg-gradient-to-br from-sky-500/20 to-violet-500/20 p-4">
              <Inbox className="h-8 w-8 text-violet-400" />
            </div>
            <div className="text-lg font-bold">Nenhuma entrada acionável hoje</div>
            <p className="max-w-md text-sm text-muted-foreground">
              O modelo não encontrou edge dentro do filtro 1.50x–3.0x — ou os jogos do dia já
              começaram / ainda não estão disponíveis. Sessão vazia é normal.
            </p>
          </Card>
        </motion.div>
      )}

      {data && data.skipped.length > 0 && (
        <Card className="p-4">
          <div className="mb-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-muted-foreground">
            <BadgeInfo className="h-3.5 w-3.5" /> {data.skipped.length} mercados ignorados
          </div>
          <div className="flex flex-wrap gap-2">
            {data.skipped.slice(0, 16).map((sk, i) => (
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
