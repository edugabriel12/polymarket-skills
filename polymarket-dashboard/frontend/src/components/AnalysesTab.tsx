import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
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

export function AnalysesTab({ sport }: { sport: Sport }) {
  // The backend recomputes every sport at the top of each UTC hour and refreshes its cache,
  // so the panel just reads the cache — no manual recompute control. Refetch hourly to pick
  // up the freshest server-side recompute.
  const { data, isLoading } = useQuery({
    queryKey: ["analyses", sport],
    queryFn: () => api.analyses(sport),
    staleTime: 60 * 60 * 1000,        // 1h — matches the server's hourly refresh
    refetchInterval: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

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
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <CalendarDays className="h-4 w-4" />
          <span>Previsões de {data?.date ?? "—"}</span>
          {n > 0 && (
            <span className="rounded-full bg-gradient-to-r from-sky-500/20 to-violet-500/20 px-2 py-0.5 text-xs font-bold">
              {n} {n === 1 ? "entrada" : "entradas"}
            </span>
          )}
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
                {sk.game}: {sk.reason}
              </span>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
