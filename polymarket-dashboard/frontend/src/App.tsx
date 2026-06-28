import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/components/ThemeProvider";
import { useAuth } from "@/components/AuthProvider";
import { AuthForms, VerifyEmail, ResetPassword } from "@/components/AuthView";
import { EntriesTab } from "@/components/EntriesTab";
import { ResultsTab } from "@/components/ResultsTab";
import { TelegramTab } from "@/components/TelegramTab";
import { Moon, Sun, LineChart, Trophy, Sparkles, Send, LogOut, Loader2 } from "lucide-react";

function Dashboard() {
  return (
    <Tabs defaultValue="entries">
      <TabsList className="mb-6">
        <TabsTrigger value="entries">
          <LineChart className="h-4 w-4" /> Entradas
        </TabsTrigger>
        <TabsTrigger value="results">
          <Trophy className="h-4 w-4" /> Resultados
        </TabsTrigger>
        <TabsTrigger value="telegram">
          <Send className="h-4 w-4" /> Telegram
        </TabsTrigger>
      </TabsList>
      <TabsContent value="entries">
        <EntriesTab />
      </TabsContent>
      <TabsContent value="results">
        <ResultsTab />
      </TabsContent>
      <TabsContent value="telegram">
        <TelegramTab />
      </TabsContent>
    </Tabs>
  );
}

export default function App() {
  const { theme, toggle } = useTheme();
  const { user, loading, logout } = useAuth();

  // Deep links from verification / reset e-mails render regardless of auth state.
  const path = window.location.pathname;
  const token = new URLSearchParams(window.location.search).get("token") || "";

  let content;
  if (path.startsWith("/verify")) {
    content = <VerifyEmail token={token} />;
  } else if (path.startsWith("/reset")) {
    content = <ResetPassword token={token} />;
  } else if (loading) {
    content = (
      <div className="flex min-h-[50vh] items-center justify-center text-muted-foreground">
        <Loader2 className="h-6 w-6 animate-spin" />
      </div>
    );
  } else if (!user) {
    content = <AuthForms />;
  } else {
    content = <Dashboard />;
  }

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
              <p className="text-xs text-muted-foreground">
                Entradas por categoria · resultados em unidades sugeridas
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {user && (
              <>
                <span className="hidden text-sm text-muted-foreground sm:inline">{user.email}</span>
                <Button variant="ghost" size="sm" onClick={logout} aria-label="Sair">
                  <LogOut className="h-4 w-4" /> Sair
                </Button>
              </>
            )}
            <Button variant="ghost" size="icon" onClick={toggle} aria-label="Alternar tema">
              {theme === "dark" ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-5 py-6">
        {content}

        <footer className="mt-10 border-t border-border pt-4 text-center text-xs text-muted-foreground">
          Exibição de entradas e resultados. Não é recomendação financeira.
        </footer>
      </main>
    </div>
  );
}
