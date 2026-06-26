import { useState } from "react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/components/ThemeProvider";
import { AnalysesTab } from "@/components/AnalysesTab";
import { ResultsTab } from "@/components/ResultsTab";
import { Moon, Sun, LineChart, Trophy, Sparkles } from "lucide-react";
import type { Sport } from "@/lib/api";
import { cn } from "@/lib/utils";

const SPORTS: { key: Sport; label: string; emoji: string }[] = [
  { key: "soccer", label: "Futebol", emoji: "⚽" },
  { key: "tennis", label: "Tênis", emoji: "🎾" },
];

const SPORT_SUBTITLE: Record<Sport, string> = {
  soccer: "Futebol · total de gols + BTTS (Dixon-Coles)",
  tennis: "Tênis · vencedor da partida (Elo por superfície)",
};

export default function App() {
  const { theme, toggle } = useTheme();
  const [sport, setSport] = useState<Sport>("soccer");

  return (
    <div className="min-h-screen">
      <header className="aurora border-b border-border">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-5 py-6">
          <div className="flex items-center gap-3">
            <div className="rounded-2xl bg-white/10 p-2.5 backdrop-blur">
              <Sparkles className="h-6 w-6 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-black tracking-tight">Polymarket Sports</h1>
              <p className="text-xs text-muted-foreground">{SPORT_SUBTITLE[sport]}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="inline-flex rounded-xl border border-border bg-card/60 p-1 backdrop-blur">
              {SPORTS.map((s) => (
                <button
                  key={s.key}
                  onClick={() => setSport(s.key)}
                  className={cn(
                    "rounded-lg px-3 py-1.5 text-sm font-bold transition-all",
                    sport === s.key
                      ? "bg-gradient-to-r from-sky-500 to-violet-500 text-white shadow"
                      : "text-muted-foreground hover:text-foreground"
                  )}
                >
                  <span className="mr-1">{s.emoji}</span>
                  {s.label}
                </button>
              ))}
            </div>
            <Button variant="ghost" size="icon" onClick={toggle} aria-label="Alternar tema">
              {theme === "dark" ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-5 py-6">
        <Tabs defaultValue="analyses">
          <TabsList className="mb-6">
            <TabsTrigger value="analyses">
              <LineChart className="h-4 w-4" /> Análises
            </TabsTrigger>
            <TabsTrigger value="results">
              <Trophy className="h-4 w-4" /> Resultados
            </TabsTrigger>
          </TabsList>
          <TabsContent value="analyses">
            <AnalysesTab sport={sport} />
          </TabsContent>
          <TabsContent value="results">
            <ResultsTab sport={sport} />
          </TabsContent>
        </Tabs>

        <footer className="mt-10 border-t border-border pt-4 text-center text-xs text-muted-foreground">
          Simulação paper-trading — não é recomendação financeira. Operar envolve risco de perda.
        </footer>
      </main>
    </div>
  );
}
