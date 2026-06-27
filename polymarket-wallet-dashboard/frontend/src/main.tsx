import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider, QueryCache } from "@tanstack/react-query";
import { ThemeProvider } from "@/components/ThemeProvider";
import App from "@/App";
import "./index.css";

// Captura crashes de renderização (ex.: shape de dados inesperado) que, sem isso,
// deixariam a página em branco sem nenhum sinal — e os loga no console.
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("[ErrorBoundary] erro de renderização:", error, info);
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24, fontFamily: "monospace", color: "#e11d48" }}>
          <h2>Erro ao renderizar a aplicação</h2>
          <pre style={{ whiteSpace: "pre-wrap" }}>{this.state.error.message}</pre>
          <p style={{ color: "#64748b" }}>Abra o console do navegador (F12) para o stack completo.</p>
        </div>
      );
    }
    return this.props.children;
  }
}

const queryClient = new QueryClient({
  // Loga TODA query que falhar (qualquer aba/fluxo), identificada pela sua queryKey.
  queryCache: new QueryCache({
    onError: (error, query) =>
      console.error(`[query] ${JSON.stringify(query.queryKey)} falhou:`, error),
  }),
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <App />
        </ThemeProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>
);
