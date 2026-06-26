import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Send, CheckCircle2, AlertTriangle, Loader2, Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, type TelegramSaveResult } from "@/lib/api";

export function TelegramTab() {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<TelegramSaveResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const copyStart = async () => {
    try {
      await navigator.clipboard.writeText("/start");
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — the user can still type /start manually */
    }
  };

  const { data: status } = useQuery({ queryKey: ["telegram"], queryFn: () => api.telegramStatus() });

  const save = async () => {
    if (!token.trim()) {
      setErr("Cole o token do bot.");
      return;
    }
    setSaving(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.telegramSave(token.trim());
      setResult(r);
      if (r.ok) {
        setToken("");
        qc.invalidateQueries({ queryKey: ["telegram"] });
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <Card className="space-y-4 p-5">
        <div className="flex items-center gap-2 text-base font-black">
          <Send className="h-5 w-5 text-sky-400" /> Bot do Telegram
        </div>

        {status?.configured ? (
          <div className="flex items-center gap-2 rounded-xl bg-emerald-500/10 px-3 py-2 text-sm text-emerald-500">
            <CheckCircle2 className="h-4 w-4" /> Conectado · chat <span className="font-mono">{status.chat_id}</span>
          </div>
        ) : (
          <div className="flex items-center gap-2 rounded-xl bg-amber-500/10 px-3 py-2 text-sm text-amber-500">
            <AlertTriangle className="h-4 w-4" /> Ainda não configurado
          </div>
        )}

        <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
          <li>No Telegram, fale com <strong>@BotFather</strong> → <code>/newbot</code> e copie o <strong>token</strong>.</li>
          <li>Abra seu bot e envie <code>/start</code> (pra ele te enxergar).</li>
          <li>Cole o token abaixo e salve — o <strong>chat ID é preenchido sozinho</strong> e um alerta de teste é disparado.</li>
        </ol>

        {/* Prominent /start reminder — required for chat-id auto-discovery */}
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-sky-500/30 bg-sky-500/10 px-3 py-2.5 text-sm">
          <AlertTriangle className="h-4 w-4 shrink-0 text-sky-400" />
          <span>
            <strong>Importante:</strong> antes de salvar, abra seu bot no Telegram e envie{" "}
            <button
              onClick={copyStart}
              title="Copiar /start"
              className="inline-flex items-center gap-1 rounded-md bg-sky-500/20 px-1.5 py-0.5 font-mono font-bold text-sky-300 transition hover:bg-sky-500/30"
            >
              /start {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            </button>
            . Sem isso o chat ID não é detectado.
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Token do bot (ex.: 123456:ABC-DEF…)"
            spellCheck={false}
            className="h-10 min-w-[260px] flex-1 rounded-xl border border-border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-sky-500/40"
          />
          <Button onClick={save} disabled={saving}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            Salvar e testar
          </Button>
        </div>

        {err && (
          <div className="flex items-center gap-2 text-sm text-rose-500">
            <AlertTriangle className="h-4 w-4" /> {err}
          </div>
        )}

        {result && (
          result.ok ? (
            <div className="rounded-xl bg-emerald-500/10 px-3 py-2 text-sm text-emerald-500">
              <div className="flex items-center gap-2 font-bold">
                <CheckCircle2 className="h-4 w-4" /> Chat ID detectado: <span className="font-mono">{result.chat_id}</span>
              </div>
              <div className="mt-0.5 text-xs">
                {result.tested
                  ? "Alerta de teste enviado — confira seu Telegram. ✅"
                  : "Salvo, mas o alerta de teste falhou — verifique o token/conversa."}
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 rounded-xl bg-rose-500/10 px-3 py-2 text-sm text-rose-500">
              <AlertTriangle className="h-4 w-4" /> {result.error}
            </div>
          )
        )}
      </Card>
    </div>
  );
}
