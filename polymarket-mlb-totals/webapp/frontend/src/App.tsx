import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/components/ThemeProvider";
import { AnalysesTab } from "@/components/AnalysesTab";
import { ResultsTab } from "@/components/ResultsTab";
import { Moon, Sun, LineChart, Trophy, Sparkles } from "lucide-react";

export default function App() {
  const { theme, toggle } = useTheme();
  return (
    <div className="min-h-screen">
      <header className="aurora border-b border-border">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-5 py-6">
          <div className="flex items-center gap-3">
            <div className="rounded-2xl bg-white/10 p-2.5 backdrop-blur">
              <Sparkles className="h-6 w-6 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-black tracking-tight">MLB Totals</h1>
              <p className="text-xs text-muted-foreground">Modelo Binomial Negativa · mercado de total de runs</p>
            </div>
          </div>
          <Button variant="ghost" size="icon" onClick={toggle} aria-label="Alternar tema">
            {theme === "dark" ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
          </Button>
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
            <AnalysesTab />
          </TabsContent>
          <TabsContent value="results">
            <ResultsTab />
          </TabsContent>
        </Tabs>

        <footer className="mt-10 border-t border-border pt-4 text-center text-xs text-muted-foreground">
          Simulação paper-trading — não é recomendação financeira. Operar envolve risco de perda.
        </footer>
      </main>
    </div>
  );
}
